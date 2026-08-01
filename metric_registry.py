"""통합 지표 스펙 레지스트리 — 신규 지표 추가 구조 개선안 P0(스키마 + 로더).

배경/목표
---------
지금까지 새 수치 컬럼을 하나 추가할 때마다 graph_rag.py 안에 그 컬럼 전용 파싱 로직(단위·동의어·NULL·0건·
최근성 예외·우선순위·집계·파생·실패분기·테스트)을 함께 넣어야 했다. 지표가 늘수록 "컬럼 등록"이 아니라
"컬럼마다 미니 파서 추가"가 되어 유지보수·회귀 비용이 급증한다.

이 모듈은 그 정보를 **코드가 아니라 스펙(단일 소스)** 으로 옮기기 위한 첫 단계다. 지표별 정보를 선언형
스펙으로 읽어들이고 검증만 한다 — **아직 graph_rag 파이프라인에는 연결하지 않는다**(P0 격리 원칙). 이후
단계에서 단위 → zero_semantics → 최근성 → 비율 순으로 기존 로직을 이 레지스트리로 점진 교체한다.

스키마(= 아래 데이터클래스가 규범)
--------------------------------
공통:
  metric_id      : str, 전역 유일
  name           : str, 사람이 읽는 라벨
  semantic_type  : "scalar" | "ratio" | "date"  (전략 디스패치 키; 같은 타입이면 파서 코드 공유)
  data_type      : "integer" | "decimal" | "money" | "date_string"
  aliases.nouns  : list[str], 최소 1개 (명사형 표면어)
  aliases.actions: list[str], 선택 (동사형 표면어)
  operators.allowed : list[Operator]  (허용 연산자 화이트리스트)
  null_semantics    : dict, 선택
  priority_hints    : {prefer_when_terms: [...], conflicts_with: [...], yields_to_cumulative_days: bool}
  aggregation       : {default: str, supported: [str]}, 선택

semantic_type 별 필수:
  scalar :
    source.{table, alias, column}
    units.{canonical, expressions:[...]}   # 측정 단위 표면어(회/일/원 …). '30일 이상'의 '일'이 여기.
    zero_semantics.{missing_as_zero: bool, comparison_strategy: str}   # 선택
    time_semantics.supports_recent_period = false  # 누적 스칼라는 최근성 아님
  ratio :
    derivation.numerator.{table, alias, column}
    derivation.denominator.{table, alias, column}
    derivation.formula : str  # "{numerator}"/"{denominator}" 치환 토큰 사용. 기본 CAST/NULLIF 제공.
    units.{...}  # 임계 단위(하루 평균 '회')
  date :
    source.{table, alias, column, date_format}
    time_semantics.supports_recent_period = true
    time_semantics.{anchor, default_days, windows.{recent, inactive}}
      windows.<name>.{operator, include_null}   # recency(>=)·inactivity(<) 를 한 지표로 표현

로더는 이 스펙을 읽고 규칙을 검증할 뿐, SQL 을 만들지 않는다(그건 이후 단계의 전략 컴파일러 몫).
JSON 을 쓰는 이유: 레포 전 설정이 JSON(docs/data/*.json) 이라 포맷·의존성을 통일한다(개선안 YAML 예시와
키 구조는 동일 — 나중에 YAML 로 바꿔도 스키마는 그대로).
로드 건전성: 전용 레지스트리 계약 테스트는 삭제됐다(현재 가드 없음).
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 기본 경로는 **모듈 기준 절대경로**다. 상대경로면 cwd 가 저장소 밖일 때 설정을 못 읽고
# 조용히 빈 레지스트리로 강등된다(증상이 예외가 아니라 '조금 다른 답'이라 눈에 띄지 않는다).
_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_METRIC_SPEC_DIR = _REPO_ROOT / "docs" / "data" / "metrics"

SEMANTIC_TYPES = frozenset({"scalar", "ratio", "date"})
DATA_TYPES = frozenset({"integer", "decimal", "money", "date_string"})
# 연산자 화이트리스트: 수치 비교 + 날짜 창(WITHIN 최근/BEFORE 미접속). 스펙이 이 밖의 토큰을 쓰면 검증 실패.
OPERATORS = frozenset({"EQ", "NE", "GT", "GTE", "LT", "LTE", "BETWEEN", "WITHIN", "BEFORE"})

# 파생 비율 기본 식 — 분모 0(로그인 일수 0)은 NULLIF 로 NULL 화해 나눗셈 예외를 막고 해당 회원을 자연 제외.
DEFAULT_RATIO_FORMULA = "CAST({numerator} AS FLOAT) / NULLIF({denominator}, 0)"

# 지표 스펙 연산자 토큰 → 비교 기호(수치 프로필 슬롯 coerce 화이트리스트). EQ/BETWEEN 은 슬롯 비교
# 문법(부등호 4종) 밖이라 제외한다.
_PROFILE_OPERATOR_SYMBOLS = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


@functools.lru_cache(maxsize=1)
def profile_slot_vocab() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """기본 스펙 디렉터리의 프로필 슬롯 어휘(모듈 수준 캐시). 스펙 오류는 어휘 없음(슬롯 침묵)으로
    강등한다 — 지표 스펙 한 파일의 문제가 타겟팅 파이프라인 전체를 죽이지 않는다."""
    try:
        return MetricRegistry.load().profile_slot_vocab()
    except MetricSpecError:
        return {}, {}


class MetricSpecError(ValueError):
    """지표 스펙이 스키마를 위반했을 때(필수 누락·타입 오류·중복 id·미지원 연산자 등). 어떤 metric_id 의
    어떤 필드가 왜 틀렸는지 메시지에 담아, 신규 지표를 스펙만으로 추가할 때 즉시 원인을 알 수 있게 한다."""


@dataclass(frozen=True)
class ColumnRef:
    table: str
    alias: str
    column: str
    date_format: str | None = None

    @property
    def qualified(self) -> str:
        """'B.LAST_LOGIN_DATE' 형태(별칭.컬럼). 컴파일러가 좌변으로 그대로 쓴다."""
        return f"{self.alias}.{self.column}"


@dataclass(frozen=True)
class Units:
    canonical: str
    expressions: tuple[str, ...]


@dataclass(frozen=True)
class Derivation:
    numerator: ColumnRef
    denominator: ColumnRef
    formula: str = DEFAULT_RATIO_FORMULA

    def render(self) -> str:
        """분자/분모 컬럼을 formula 토큰에 치환한 좌변 식."""
        return self.formula.format(
            numerator=self.numerator.qualified, denominator=self.denominator.qualified
        )


@dataclass(frozen=True)
class TimeSemantics:
    supports_recent_period: bool = False
    anchor: str | None = None
    default_days: int | None = None
    windows: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str
    name: str
    semantic_type: str
    data_type: str
    aliases_nouns: tuple[str, ...]
    aliases_actions: tuple[str, ...] = ()
    operators_allowed: tuple[str, ...] = ()
    source: ColumnRef | None = None
    units: Units | None = None
    derivation: Derivation | None = None
    zero_semantics: dict[str, Any] = field(default_factory=dict)
    null_semantics: dict[str, Any] = field(default_factory=dict)
    time_semantics: TimeSemantics = field(default_factory=TimeSemantics)
    priority_hints: dict[str, Any] = field(default_factory=dict)
    aggregation: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # 원본(진단·미래 필드 무손실용)


# ── 파싱 헬퍼(원자적 검증) ──────────────────────────────────────────────────────────────────
def _require(cond: bool, metric_id: str, message: str) -> None:
    if not cond:
        raise MetricSpecError(f"[{metric_id or '<no metric_id>'}] {message}")


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(s for s in value if isinstance(s, str) and s.strip())


def _parse_column_ref(data: Any, metric_id: str, where: str) -> ColumnRef:
    _require(isinstance(data, dict), metric_id, f"{where} 는 객체여야 함")
    for key in ("table", "alias", "column"):
        _require(isinstance(data.get(key), str) and data[key].strip(), metric_id, f"{where}.{key} 누락")
    fmt = data.get("date_format")
    return ColumnRef(
        table=data["table"].strip(), alias=data["alias"].strip(),
        column=data["column"].strip(), date_format=fmt if isinstance(fmt, str) and fmt else None,
    )


def _parse_spec(data: dict[str, Any]) -> MetricSpec:
    """dict 하나를 MetricSpec 으로 검증·정규화. 위반 시 MetricSpecError(어느 지표 어느 필드인지 포함)."""
    metric_id = data.get("metric_id") if isinstance(data.get("metric_id"), str) else ""
    _require(bool(metric_id and metric_id.strip()), metric_id, "metric_id 필수(문자열)")
    metric_id = metric_id.strip()
    _require(isinstance(data.get("name"), str) and data["name"].strip(), metric_id, "name 필수(문자열)")

    semantic_type = data.get("semantic_type")
    _require(semantic_type in SEMANTIC_TYPES, metric_id,
             f"semantic_type 는 {sorted(SEMANTIC_TYPES)} 중 하나여야 함(받음: {semantic_type!r})")
    data_type = data.get("data_type")
    _require(data_type in DATA_TYPES, metric_id,
             f"data_type 는 {sorted(DATA_TYPES)} 중 하나여야 함(받음: {data_type!r})")

    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    nouns = _str_list(aliases.get("nouns"))
    _require(bool(nouns), metric_id, "aliases.nouns 는 최소 1개 필요(명사형 표면어)")
    actions = _str_list(aliases.get("actions"))

    operators_allowed = _str_list((data.get("operators") or {}).get("allowed"))
    unknown_ops = [op for op in operators_allowed if op not in OPERATORS]
    _require(not unknown_ops, metric_id, f"operators.allowed 미지원 토큰: {unknown_ops}(허용 {sorted(OPERATORS)})")

    units = None
    units_raw = data.get("units")
    if isinstance(units_raw, dict):
        exprs = _str_list(units_raw.get("expressions"))
        canonical = units_raw.get("canonical")
        _require(isinstance(canonical, str) and canonical.strip(), metric_id, "units.canonical 누락")
        _require(bool(exprs), metric_id, "units.expressions 는 최소 1개 필요")
        units = Units(canonical=canonical.strip(), expressions=exprs)

    source = None
    if isinstance(data.get("source"), dict):
        source = _parse_column_ref(data["source"], metric_id, "source")

    derivation = None
    if isinstance(data.get("derivation"), dict):
        d = data["derivation"]
        formula = d.get("formula")
        derivation = Derivation(
            numerator=_parse_column_ref(d.get("numerator"), metric_id, "derivation.numerator"),
            denominator=_parse_column_ref(d.get("denominator"), metric_id, "derivation.denominator"),
            formula=formula.strip() if isinstance(formula, str) and formula.strip() else DEFAULT_RATIO_FORMULA,
        )

    ts_raw = data.get("time_semantics") if isinstance(data.get("time_semantics"), dict) else {}
    time_semantics = TimeSemantics(
        supports_recent_period=bool(ts_raw.get("supports_recent_period")),
        anchor=ts_raw.get("anchor") if isinstance(ts_raw.get("anchor"), str) else None,
        default_days=ts_raw.get("default_days") if isinstance(ts_raw.get("default_days"), int) else None,
        windows=ts_raw.get("windows") if isinstance(ts_raw.get("windows"), dict) else {},
    )

    spec = MetricSpec(
        metric_id=metric_id,
        name=data["name"].strip(),
        semantic_type=semantic_type,
        data_type=data_type,
        aliases_nouns=nouns,
        aliases_actions=actions,
        operators_allowed=operators_allowed,
        source=source,
        units=units,
        derivation=derivation,
        zero_semantics=data.get("zero_semantics") if isinstance(data.get("zero_semantics"), dict) else {},
        null_semantics=data.get("null_semantics") if isinstance(data.get("null_semantics"), dict) else {},
        time_semantics=time_semantics,
        priority_hints=data.get("priority_hints") if isinstance(data.get("priority_hints"), dict) else {},
        aggregation=data.get("aggregation") if isinstance(data.get("aggregation"), dict) else {},
        raw=data,
    )
    _validate_by_semantic_type(spec)
    return spec


def _validate_by_semantic_type(spec: MetricSpec) -> None:
    """semantic_type 별 필수 조합 검증(전략이 소비할 최소 계약)."""
    mid = spec.metric_id
    if spec.semantic_type == "scalar":
        _require(spec.source is not None, mid, "scalar 은 source.{table,alias,column} 필요")
        _require(spec.units is not None, mid, "scalar 은 units.expressions 필요(측정 단위 표면어)")
        _require(not spec.time_semantics.supports_recent_period, mid,
                 "scalar 은 누적 지표라 time_semantics.supports_recent_period=false 여야 함")
    elif spec.semantic_type == "ratio":
        _require(spec.derivation is not None, mid, "ratio 는 derivation.{numerator,denominator} 필요")
        _require(spec.units is not None, mid, "ratio 는 units.expressions 필요(임계 단위)")
    elif spec.semantic_type == "date":
        _require(spec.source is not None, mid, "date 는 source.{table,alias,column} 필요")
        _require(spec.time_semantics.supports_recent_period, mid,
                 "date 는 time_semantics.supports_recent_period=true 여야 함")
        _require(bool(spec.time_semantics.windows), mid,
                 "date 는 time_semantics.windows(recent/inactive) 필요")


@dataclass
class MetricRegistry:
    """검증된 MetricSpec 모음 + 조회. graph_rag 미연결(P0). 이후 단계에서 이 레지스트리를 파서/컴파일러/
    검증·신뢰도·트레이스가 공통 소스로 참조하게 한다(다운스트림 드리프트 = 'auto만 실패' 함정 방지)."""

    specs: tuple[MetricSpec, ...]

    @classmethod
    def load(cls, spec_dir: Path | str = DEFAULT_METRIC_SPEC_DIR) -> "MetricRegistry":
        """디렉터리의 *.json 지표 스펙을 모두 읽어 검증한다. 파일명 정렬로 결정론 순서. 중복 id 는 에러."""
        directory = Path(spec_dir)
        specs: list[MetricSpec] = []
        seen: dict[str, str] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MetricSpecError(f"[{path.name}] 읽기/파싱 실패: {exc}") from exc
            _require(isinstance(payload, dict), payload.get("metric_id", "") if isinstance(payload, dict) else "",
                     f"{path.name}: 최상위는 지표 객체 하나여야 함")
            spec = _parse_spec(payload)
            if spec.metric_id in seen:
                raise MetricSpecError(
                    f"[{spec.metric_id}] metric_id 중복: {path.name} 와 {seen[spec.metric_id]}"
                )
            seen[spec.metric_id] = path.name
            specs.append(spec)
        return cls(specs=tuple(specs))

    def all(self) -> tuple[MetricSpec, ...]:
        return self.specs

    def by_id(self, metric_id: str) -> MetricSpec | None:
        return next((s for s in self.specs if s.metric_id == metric_id), None)

    def by_semantic_type(self, semantic_type: str) -> list[MetricSpec]:
        return [s for s in self.specs if s.semantic_type == semantic_type]

    def resolve_alias(self, text: str) -> list[tuple[MetricSpec, str]]:
        """텍스트에 등장하는 (지표, 매칭 명사 별칭) 목록. 긴 별칭 우선(부분문자열 오탐 완화). 단순 포함
        검색 — 실제 파이프라인 연결 시엔 창/게이트가 더해지지만, 여기선 스펙 자체의 별칭 도달성만 본다."""
        hits: list[tuple[MetricSpec, str]] = []
        for spec in self.specs:
            for alias in sorted(spec.aliases_nouns, key=len, reverse=True):
                if alias in text:
                    hits.append((spec, alias))
                    break
        return hits

    def profile_slot_vocab(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """targeting.enabled 스펙에서 LLM 프로필 슬롯 닫힌 어휘를 파생한다: (수치 지표 맵, 날짜 상대상태 맵).

        수치(scalar)는 balance_conditions, 날짜 relative_states 는 profile_date_conditions 로 컴파일된다.
        물리 바인딩(profile_source: 테이블·별칭·회원키·grain_filter)은 스펙 targeting 블록에서만 채워
        LLM 은 논리 canonical(metric_id/state)만 고른다. 회원키 컬럼을 소스 기본값으로 보충하지 않는다 —
        물리 지식의 소유자는 스펙이고, targeting 블록이 불완전하면 그 지표는 어휘에 싣지 않는다."""
        metrics: dict[str, dict[str, Any]] = {}
        date_states: dict[str, dict[str, Any]] = {}
        for spec in self.specs:
            targeting = spec.raw.get("targeting") if isinstance(spec.raw.get("targeting"), dict) else {}
            member_column = targeting.get("member_column")
            base_member_column = targeting.get("base_member_column")
            if not (
                targeting.get("enabled")
                and spec.source is not None
                and isinstance(member_column, str) and member_column
                and isinstance(base_member_column, str) and base_member_column
            ):
                continue
            profile_source: dict[str, Any] = {
                "table": spec.source.table,
                "alias": spec.source.alias,
                "column": spec.source.column,
                "member_column": member_column,
                "base_member_column": base_member_column,
            }
            if isinstance(targeting.get("grain_filter"), str) and targeting["grain_filter"]:
                profile_source["grain_filter"] = targeting["grain_filter"]
            if spec.semantic_type == "scalar":
                operators = frozenset(
                    _PROFILE_OPERATOR_SYMBOLS[token]
                    for token in spec.operators_allowed
                    if token in _PROFILE_OPERATOR_SYMBOLS
                )
                metrics[spec.metric_id] = {
                    "column": spec.source.column,
                    "label": spec.name,
                    "operators": operators or None,
                    "profile_source": profile_source,
                }
            elif spec.semantic_type == "date":
                states: dict[str, dict[str, Any]] = {}
                for state in targeting.get("relative_states") or []:
                    if not isinstance(state, dict):
                        continue
                    name = state.get("state")
                    operator = state.get("operator")
                    anchor = state.get("anchor_expression")
                    if (
                        all(isinstance(value, str) and value for value in (name, operator, anchor))
                        and operator in {"=", ">", ">=", "<", "<="}
                    ):
                        states[name] = {
                            "operator": operator,
                            "anchor_expression": anchor,
                            "label": f"{spec.name}({name})",
                        }
                if states:
                    date_states[spec.metric_id] = {
                        "label": spec.name,
                        "states": states,
                        "profile_source": profile_source,
                    }
        return metrics, date_states

    def lint_against_catalog(self, columns_by_table: dict[str, set[str]]) -> list[str]:
        """스펙이 참조하는 컬럼이 스키마 카탈로그에 실재하는지 점검(경고 리스트). DB 이식성 목표와 연결 —
        실DB 가 바뀌면 이 lint 로 스펙과의 어긋남을 조기 발견한다. 로드 시 강제하진 않는다(카탈로그 주입식)."""
        warnings: list[str] = []

        def check(ref: ColumnRef | None, label: str, mid: str) -> None:
            if ref is None:
                return
            cols = columns_by_table.get(ref.table)
            if cols is None:
                warnings.append(f"[{mid}] {label}: 테이블 {ref.table} 이 카탈로그에 없음")
            elif ref.column not in cols:
                warnings.append(f"[{mid}] {label}: 컬럼 {ref.table}.{ref.column} 이 카탈로그에 없음")

        for spec in self.specs:
            check(spec.source, "source", spec.metric_id)
            if spec.derivation is not None:
                check(spec.derivation.numerator, "derivation.numerator", spec.metric_id)
                check(spec.derivation.denominator, "derivation.denominator", spec.metric_id)
        return warnings
