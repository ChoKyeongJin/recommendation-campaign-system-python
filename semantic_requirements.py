"""공통 semantic requirement 계층 — 모든 자연어 조건을 먼저 'source requirement'로 기록하고,
base×qualifier capability 로 각 requirement 의 귀결(parsed/compiled/clarification/unsupported)을 회계한다.

배경/목표
---------
지금까지 '장바구니 조건 + 브랜드명 CJ제일제당'처럼 **base 조건(behavior/metric/dimension)에 붙은 한정자
(qualifier: 브랜드/상품/카테고리/지역/기간 등)** 가 그 base 빌더에서 컴파일되지 못하면 조용히 사라진 채
'성공'으로 나갔다. 브랜드 전용 감지기로 땜질하면 상품/카테고리/지역/기간마다 같은 특수 코드가 늘어난다.

이 모듈은 그 대신 **공통 계층**을 둔다:
  1) 원문에서 조건을 source requirement 로 기록한다(base + qualifiers + relation + operator + value +
     time_scope + negation + comparison_target + derived_formula + source_span). 지원 여부와 무관하게 '기록'이 먼저다.
  2) 도메인별 지원 여부는 코드가 아니라 JSON capability registry
     (docs/data/runtime/semantics/requirement_capabilities.json)의
     base→qualifier→{supported, message, compiler_strategy}로 관리한다.
  3) 공통 검증기(account_requirements)는 '특정 브랜드 인식 성공' 이 아니라, **모든 requirement 가
     parsed/compiled/clarification/unsupported 중 하나로 귀결됐는지**만 본다. 'detected' 로 남은(= 조용히
     사라진) requirement 가 있으면 실패로 승격한다.

이 모듈은 graph_rag 를 import 하지 않는다(순환 방지) — 순수 dict/dataclass in/out. graph_rag 의
build_sql_result 가 account_requirements(query, plan, sql)로 호출해 귀결을 얻고, unsupported/clarification 이
있으면 needs_clarification 로 응답한다.

현재 추출 범위: entity qualifier(브랜드/상품/카테고리/제품/품목명 + 값)와 평탄화 시 손실되는 조합
연산자(주기 반복, 외부 지정 집합, 최신 스냅샷 선택). 후자는 compiler receipt가 있어야만 해소된다.

실행: python -m pytest tests/test_source_semantic_contract.py -q  (이 모듈 일부만 덮는다 — 전용 스위트 없음)
"""

from __future__ import annotations

import common_utils

import json
import hashlib
import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import condition_normalizers
import korean_number_normalizer
import lexicon_patterns
import member_filters_config
import temporal_clause
from calendar_window import parse_calendar_window_spans
from semantic_normalizers import RelativeWindow, decimal_json_value, exact_decimal


# 기본 경로는 **모듈 기준 절대경로**다. 상대경로면 cwd 가 저장소 밖일 때 설정을 못 읽고
# 조용히 빈 레지스트리로 강등된다(증상이 예외가 아니라 '조금 다른 답'이라 눈에 띄지 않는다).
_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CAPABILITIES_PATH = (
    _REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "requirement_capabilities.json"
)

# requirement 귀결 상태. 'detected' 는 아직 회계 전(초기값). 검증기가 나머지 넷 중 하나로 확정해야 한다.
TERMINAL_STATUSES = frozenset({"parsed", "compiled", "clarification", "unsupported"})
# 응답 출고를 막아야 하는(= 사용자 확인이 필요한) 귀결.
BLOCKING_STATUSES = frozenset({"clarification", "unsupported"})

QUALIFIER_TYPES = frozenset({"entity", "dimension", "time_scope", "attribute"})
SOURCE_REQUIREMENTS_KEY = "source_requirements"
SOURCE_REQUIREMENTS_DIGEST_KEY = "source_requirements_digest"

# 실행/검색 메타데이터가 아니라 사용자 의미를 담는 plan 최상위 슬롯만 캡처한다. target_user/exclude/
# campaign_constraints 는 컨테이너 전체가 의미 슬롯이므로 별도 allow-list 없이 비어 있지 않은 값을 기록한다.
_PLAN_REQUIREMENT_SLOTS = frozenset({
    "aggregation_request",
    "condition_evaluations",
    "computed_metrics",
    "dimension_filters",
    "group_ranking_target",
    "member_column_selection_filter",
    "member_metric_ranking",
    "member_metric_selection",
    "policy_constraints",
    "purchase_count_ranking",
    "region_density_target",
    "result_limit",
    "set_expressions",
    "union_condition",
})
_NEGATIVE_TARGET_SLOTS = frozenset({"cart_absence", "purchase_inactivity"})


class RequirementCapabilityError(ValueError):
    """capability 레지스트리(JSON)가 스키마를 위반했을 때(로드 시 즉시 실패, 조용한 무시 방지)."""


class SourceRequirementIntegrityError(ValueError):
    """초기 캡처 뒤 source requirement 스냅샷이 변경됐을 때 발생한다."""


# entity qualifier 표면 표지(브랜드/상품/카테고리·제품·품목명 + 조사/콜론 + 값). 조사를 '필수'로 둬
# 일반 명사구('상품 구매한')가 값으로 오포착되는 것을 막고, 바로 뒤의 고유 값만 딴다. 값 뒤 조사는 뗀다.
_ENTITY_MARKER_DOMAIN = {
    "브랜드명": "brand", "브랜드": "brand",
    "상품명": "product", "제품명": "product", "품목명": "product",
    "카테고리명": "category", "카테고리": "category",
}
# 표지 목록의 단일 소스는 위 매핑이다 — 정규식에 같은 낱말을 한 번 더 나열하면 표지를 추가할 때
# 한쪽만 고쳐 조용히 어긋난다. 긴 표지 우선 정렬이 필수다('브랜드명'이 '브랜드'보다 먼저 와야 한다).
_ENTITY_QUALIFIER_RE = re.compile(
    "(" + "|".join(sorted(_ENTITY_MARKER_DOMAIN, key=lambda marker: (-len(marker), marker))) + ")"
    r"(?:이|가|은|는|:|=)\s*([가-힣A-Za-z0-9][가-힣A-Za-z0-9]+)"
)
_JOSA_TAIL_RE = re.compile(r"(을|를|이|가|은|는|인|의|와|과|도|만|에게|에서|에)$")
# '브랜드가 3개 이상'의 '3개'는 엔티티 **이름**이 아니라 가짓수(임계값)다 — 수량은 qualifier 값에서 뺀다.
# 숫자 + (한글 계수 단위)만 배제하므로 '3M'·'5th' 같은 실제 영문 브랜드명은 그대로 값으로 남는다.
_QUANTITY_VALUE_RE = re.compile(r"^\d[\d,]*\s*(?:개|종|종류|가지|품목|건|회|번|명|점|장|원|%|퍼센트)?$")

# 실행 슬롯보다 먼저 보존해야 하는 조합 의미. 이 표면어들은 특정 SQL 템플릿을 고르는 규칙이 아니라,
# 평탄한 ``window_days + threshold`` 같은 슬롯으로 낮출 때 사라질 수 있는 **연산자**를 감지한다.
# 감지된 의무는 compiler receipt가 없으면 unresolved로 남으므로, 새 컴파일러가 생기기 전까지 근사 SQL로
# 축소되지 않는다. 낱말은 parser_lexicon.json 에 두고 여기에는 결합 구조만 둔다.
_RECURRENCE_PREFIX_ALT = lexicon_patterns.alternation("source_recurrence_prefix")
_RECURRENCE_UNIVERSAL_ALT = lexicon_patterns.alternation("source_recurrence_universal")
_RECURRENCE_BUCKET_ALT = lexicon_patterns.alternation("source_recurrence_bucket")
_RECURRENCE_SUFFIX_ALT = lexicon_patterns.alternation("source_recurrence_suffix")
_RECURRENCE_ACTION_ALT = lexicon_patterns.alternation("source_recurrence_action")
_ENTITY_REFERENCE_ALT = lexicon_patterns.alternation("source_entity_reference")
_KOREAN_COUNT_ALT = korean_number_normalizer.native_korean_number_alternation()
_ENTITY_COUNTER_ALT = lexicon_patterns.alternation("source_entity_counter")
_ENTITY_DOMAIN_ALT = lexicon_patterns.alternation("source_entity_domain")
_ALL_QUANTIFIER_ALT = lexicon_patterns.alternation("source_all_quantifier")
_SUPERLATIVE_ALT = lexicon_patterns.alternation("source_superlative")
_LATEST_SELECTOR_ALT = lexicon_patterns.alternation("source_latest_selector")
_SNAPSHOT_GRAIN_ALT = lexicon_patterns.alternation("source_snapshot_grain")
_SNAPSHOT_SYSTEM_ALT = lexicon_patterns.alternation("source_snapshot_system")
_SNAPSHOT_NOUN_ALT = lexicon_patterns.alternation("source_snapshot_noun")
_MEMBER_NOUN_ALT = lexicon_patterns.alternation("member_noun")
_SET_SCOPE_ALT = lexicon_patterns.alternation("clause_scope_marker")
_RECURRENCE_RE = re.compile(
    # 한글은 \b 단어 경계가 성립하지 않아 접두 표지('매')가 낱말 내부에서 매칭될 수 있다:
    # '구매주기'의 '매주', '구매일'의 '매일'. 앞 글자가 한글 음절이면 낱말 내부로 보고 배제한다.
    rf"(?<![가-힣])"
    rf"(?P<marker>"
    rf"(?:{_RECURRENCE_PREFIX_ALT})\s*(?P<bucket>{_RECURRENCE_BUCKET_ALT})(?:{_RECURRENCE_SUFFIX_ALT})?"
    rf"|(?:{_RECURRENCE_UNIVERSAL_ALT})\s*(?P<bucket2>{_RECURRENCE_BUCKET_ALT})"
    rf"(?:{_RECURRENCE_SUFFIX_ALT})?"
    rf")"
)
_RECURRENCE_CONDITION_RE = re.compile(rf"(?:{_RECURRENCE_ACTION_ALT})")
_DEICTIC_ENTITY_SET_RE = re.compile(
    rf"(?P<reference>{_ENTITY_REFERENCE_ALT})\s*"
    rf"(?:(?P<count>\d+|{_KOREAN_COUNT_ALT})\s*(?:{_ENTITY_COUNTER_ALT})?\s*)?"
    rf"(?P<entity>{_ENTITY_DOMAIN_ALT})"
)
_ALL_QUANTIFIER_RE = re.compile(rf"(?:{_ALL_QUANTIFIER_ALT})")
_LATEST_SNAPSHOT_RE = re.compile(
    rf"(?:(?:{_MEMBER_NOUN_ALT})별\s*)?(?:{_SUPERLATIVE_ALT}\s*)?"
    rf"(?:{_LATEST_SELECTOR_ALT})\s*(?P<grain>{_SNAPSHOT_GRAIN_ALT})?\s*"
    rf"(?:{_SNAPSHOT_SYSTEM_ALT}\s*)?(?:{_SNAPSHOT_NOUN_ALT})",
    re.IGNORECASE,
)
_BUCKET_VALUES = {
    "일": "day",
    "주": "week",
    "주차": "week",
    "월": "month",
    "개월": "month",
    "분기": "quarter",
    "년": "year",
}
SOURCE_REQUIREMENT_RECEIPTS_KEY = "source_requirement_receipts"
_RECEIPT_TERMINAL_STATUSES = frozenset(
    {"compiled", "unsupported", "clarification"}
)


def _unique(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s and s not in out:
            out.append(s)
    return out


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _normalize_value(text: str) -> str:
    """엔티티 값 비교용 정규화: 영숫자·한글만 남긴다('알로&루'→'알로루', 'A-BC '→'abc').

    시스템이 사용자 표기('알로루')를 실DB 브랜드명('알로&루')으로 canonical 보정하므로, 원문 값과 SQL 의
    canonical 값이 특수문자에서 달라진다. 같은 정규화로 비교해야 정상 컴파일을 '누락'으로 오탐하지 않는다
    구현은 common_utils.normalize_entity_term 이 단일 소유한다(예전에는 graph_rag 와 각자 구현하고
    동일성을 주석으로만 약속했다)."""
    return common_utils.normalize_entity_term(text)


# ── source requirement 스키마 ─────────────────────────────────────────────────────────────
class FrozenDict(Mapping[str, Any]):
    """JSON 객체의 읽기 호환성을 유지하는 재귀 불변 mapping."""

    __slots__ = ("_items", "_dict")

    def __init__(self, items: Any = ()) -> None:
        normalized = tuple(items.items()) if isinstance(items, Mapping) else tuple(items)
        self._items = tuple((str(key), value) for key, value in normalized)
        self._dict = dict(self._items)

    def __getitem__(self, key: str) -> Any:
        return self._dict[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __repr__(self) -> str:
        return f"FrozenDict({self._dict!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return self


def _freeze_json(value: Any) -> Any:
    """JSON 호환 값을 재귀적으로 불변 표현으로 바꾼다."""
    if isinstance(value, dict):
        return FrozenDict(sorted((str(key), _freeze_json(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """``_freeze_json`` 결과를 API 직렬화 가능한 새 객체로 되돌린다."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class Qualifier:
    type: str  # entity | dimension | time_scope | attribute
    domain: str  # brand | product | category | region | ...
    raw_value: str
    resolved_value: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved_value", _freeze_json(self.resolved_value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "domain": self.domain,
            "raw_value": self.raw_value,
            "resolved_value": _thaw_json(self.resolved_value),
        }


@dataclass(frozen=True)
class SourceRequirement:
    id: str
    type: str  # qualified_condition | base_condition | comparison | derived | ...
    base: Any  # frozen {type: behavior|metric|dimension|set, name: str}
    qualifiers: tuple[Qualifier, ...] = field(default_factory=tuple)
    relation: str = "applies_to"  # applies_to | compared_with | excluded_from | grouped_by
    operator: Any = None
    value: Any = None
    time_scope: Any = None
    negation: bool = False
    comparison_target: Any = None
    derived_formula: Any = None
    source_text: str = ""
    source_span: Any = field(default_factory=lambda: (0, 0))
    path: str | None = None
    polarity: str = "positive"
    source: str = "rules"
    status: str = "detected"  # detected → (parsed|compiled|clarification|unsupported)
    message: str | None = None
    # 스팬 정밀도. 기존 source 필드는 후보 출처(rules/llm)이지 정밀도 신호가 아니라서, 전문장
    # 폴백 (0,len(query)) 과 정밀 스팬을 소비자가 구별할 수 없었다 — 이 필드가 그 구별의 단일 신호다.
    # exact(파서 기록/표면형 일치) | evidence(LLM 증거 스팬) | literal(리터럴 바인딩) |
    # registry(레지스트리 동의어) | whole_query(폴백).
    span_precision: str = "whole_query"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", _freeze_json(self.base))
        object.__setattr__(self, "qualifiers", tuple(self.qualifiers))
        for name in ("value", "time_scope", "comparison_target", "derived_formula"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))
        span = self.source_span
        if isinstance(span, Mapping):
            span = (span.get("start", 0), span.get("end", 0))
        if not (
            isinstance(span, (list, tuple))
            and len(span) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in span)
        ):
            span = (0, 0)
        object.__setattr__(
            self,
            "source_span",
            FrozenDict({"start": max(0, span[0]), "end": max(0, span[1])}),
        )
        if self.polarity not in {"positive", "negative"}:
            raise ValueError("source requirement polarity must be positive or negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "base": _thaw_json(self.base),
            "qualifiers": [q.to_dict() for q in self.qualifiers],
            "relation": self.relation,
            "operator": self.operator,
            "value": _thaw_json(self.value),
            "time_scope": _thaw_json(self.time_scope),
            "negation": self.negation,
            "comparison_target": _thaw_json(self.comparison_target),
            "derived_formula": _thaw_json(self.derived_formula),
            "source_text": self.source_text,
            "source_span": dict(self.source_span),
            "path": self.path,
            "polarity": self.polarity,
            "source": self.source,
            "status": self.status,
            "message": self.message,
            "span_precision": self.span_precision,
        }


# ── capability 레지스트리 ─────────────────────────────────────────────────────────────────
@dataclass
class RequirementRegistry:
    capabilities: dict[str, Any]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CAPABILITIES_PATH) -> "RequirementRegistry":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RequirementCapabilityError(f"[{Path(path).name}] 읽기/파싱 실패: {exc}") from exc
        caps = payload.get("capabilities") if isinstance(payload, dict) else None
        if not isinstance(caps, dict) or not caps:
            raise RequirementCapabilityError(f"[{Path(path).name}] 'capabilities' 객체 필요")
        for base_name, base_spec in caps.items():
            if not isinstance(base_spec, dict):
                raise RequirementCapabilityError(f"[{base_name}] base 정의는 객체여야 함")
            qualifiers = base_spec.get("qualifiers")
            if not isinstance(qualifiers, dict):
                raise RequirementCapabilityError(f"[{base_name}] qualifiers 객체 필요")
            for q_domain, q_spec in qualifiers.items():
                if not isinstance(q_spec, dict) or not isinstance(q_spec.get("supported"), bool):
                    raise RequirementCapabilityError(f"[{base_name}.{q_domain}] supported(bool) 필수")
                # 미지원인데 안내 메시지가 없으면 조용한 차단이 된다 — 로드 시 강제.
                if not q_spec["supported"] and not (isinstance(q_spec.get("message"), str) and q_spec["message"].strip()):
                    raise RequirementCapabilityError(f"[{base_name}.{q_domain}] 미지원이면 message 필수")
        return cls(capabilities=caps)

    def qualifier_capability(self, base_name: str, qualifier_domain: str) -> dict[str, Any] | None:
        """base×qualifier capability 를 조회한다. base 에 해당 qualifier 가 없으면 _default 로 폴백."""
        base = self.capabilities.get(base_name)
        if isinstance(base, dict):
            spec = (base.get("qualifiers") or {}).get(qualifier_domain)
            if isinstance(spec, dict):
                return spec
        default = self.capabilities.get("_default")
        if isinstance(default, dict):
            return (default.get("qualifiers") or {}).get(qualifier_domain)
        return None

    def base_label(self, base_name: str) -> str:
        base = self.capabilities.get(base_name) or self.capabilities.get("_default") or {}
        return str(base.get("label") or base_name) if isinstance(base, dict) else base_name


# ── 추출: 원문 → source requirements ──────────────────────────────────────────────────────
def extract_entity_qualifier_requirements(query: str, base: dict[str, Any]) -> list[SourceRequirement]:
    """원문에서 entity qualifier(브랜드/상품/카테고리명 + 값)를 source requirement 로 기록한다.

    지원 여부는 여기서 판정하지 않는다 — 일단 '기록'(status=detected)만 한다. base 는 회계 단계에서 이
    qualifier 가 붙는 조건(장바구니/구매/쿠폰 등)을 넣는다."""
    requirements: list[SourceRequirement] = []
    for index, match in enumerate(_ENTITY_QUALIFIER_RE.finditer(query or "")):
        marker, raw = match.group(1), match.group(2)
        value = _JOSA_TAIL_RE.sub("", raw)
        if len(value) < 2:
            continue
        if _QUANTITY_VALUE_RE.match(value):
            continue  # '브랜드가 3개 이상' = 가짓수 조건(집계 지표 소유)이지 브랜드명 한정자가 아니다
        domain = _ENTITY_MARKER_DOMAIN.get(marker, "entity")
        requirements.append(SourceRequirement(
            id=f"req_{index + 1}",
            type="qualified_condition",
            base=dict(base),
            qualifiers=(Qualifier(type="entity", domain=domain, raw_value=value),),
            relation="applies_to",
            source_text=match.group(0),
            source_span={"start": match.start(), "end": match.end()},
            status="detected",
        ))
    return requirements


def _non_empty(value: Any) -> bool:
    # False도 "동의하지 않음" 같은 명시 요구일 수 있으므로 빈 값으로 버리지 않는다.
    return value not in (None, "", [], {})


def _iter_requirement_values(plan: dict[str, Any]):
    """플랜의 사용자 의미 슬롯을 ``(container, slot, path, value)`` 로 평탄화한다."""
    for container_name in ("target_user", "exclude", "campaign_constraints"):
        container = plan.get(container_name)
        if not isinstance(container, dict):
            continue
        for slot, raw in container.items():
            if not _non_empty(raw):
                continue
            if isinstance(raw, list):
                for index, value in enumerate(raw):
                    if _non_empty(value):
                        yield container_name, slot, f"{container_name}.{slot}[{index}]", value
            else:
                yield container_name, slot, f"{container_name}.{slot}", raw

    for slot in sorted(_PLAN_REQUIREMENT_SLOTS):
        raw = plan.get(slot)
        if not _non_empty(raw):
            continue
        if isinstance(raw, list):
            for index, value in enumerate(raw):
                if _non_empty(value):
                    yield "plan", slot, f"{slot}[{index}]", value
        else:
            yield "plan", slot, slot, raw


def _negative_requirement(container: str, slot: str, value: Any) -> bool:
    if value is False:
        return True
    if container == "exclude" or slot in _NEGATIVE_TARGET_SLOTS or slot == "age_exclude_ranges":
        return True
    if isinstance(value, dict) and bool(value.get("negated")):
        return True
    if slot == "behaviors" and isinstance(value, str) and value.startswith("no_"):
        return True
    return False


def _slot_span(plan: dict[str, Any], container: str, slot: str, value: Any) -> dict[str, Any] | None:
    store = plan.get("_slot_spans")
    if not isinstance(store, dict):
        return None
    candidates = []
    if isinstance(value, str):
        candidates.append(f"{container}.{slot}:{value}")
    candidates.append(f"{container}.{slot}" if container != "plan" else f"plan.{slot}")
    for key in candidates:
        entry = store.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("start"), int) and isinstance(entry.get("end"), int):
            return entry
    return None


def _matched_term_span(query: str, plan: dict[str, Any], value: Any) -> tuple[int, int] | None:
    canonical = value if isinstance(value, str) else None
    if canonical is None and isinstance(value, dict):
        canonical = next(
            (value.get(key) for key in ("canonical", "metric_id", "dimension_id") if isinstance(value.get(key), str)),
            None,
        )
    if not canonical:
        return None
    for match in plan.get("matched_terms", []) or []:
        if not isinstance(match, dict) or match.get("canonical") != canonical:
            continue
        surface = match.get("matched_text")
        if isinstance(surface, str) and surface:
            start = query.casefold().find(surface.casefold())
            if start >= 0:
                return start, start + len(surface)
    return None


def _value_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []
    candidates: list[str] = []
    for key in ("source_text", "expression_text", "formula_text", "label", "ko_label", "value"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            candidates.append(item.strip())
    return candidates


_PATH_INDEX_RE = re.compile(r"\[\d+\]")


def _unique_span(candidates: list[tuple[int, int]]) -> tuple[int, int] | None:
    """후보 스팬이 정확히 하나일 때만 채택한다(fail-close — 복수 후보에서 임의 선택하면
    소유권 판정이 잘못된 좌표로 오염된다. condition_evaluation_ir 의 스팬 규칙과 동일)."""
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else None


def _evidence_span(query: str, plan: dict[str, Any], path: str) -> tuple[int, int] | None:
    """LLM semantic_evidence 의 슬롯 경로 일치 스팬. V4 검증기가 query[start:end]==text 를 보장하는
    가장 신뢰도 높은 재료지만, 경로 표기가 자유 문자열이라 인덱스 유무 차이는 흡수하고
    본문 불일치(다른 좌표계)는 버린다."""
    entries = plan.get("semantic_evidence")
    if not isinstance(entries, list) or not path:
        return None
    valid = [
        entry for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("start"), int)
        and isinstance(entry.get("end"), int)
        and 0 <= entry["start"] < entry["end"] <= len(query)
    ]
    matches = [entry for entry in valid if entry["path"] == path]
    if not matches:
        # 인덱스 제거 폴백은 형제 원소 오귀속 위험이 있다 — 요구 경로가 무인덱스이거나 [0]일 때만,
        # 그리고 **무인덱스 증거**만 후보로 삼는다([1]+ 원소가 [0]의 스팬을 상속하면 안 된다).
        if _PATH_INDEX_RE.search(path) and not path.endswith("[0]"):
            return None
        stripped = _PATH_INDEX_RE.sub("", path)
        matches = [
            entry for entry in valid
            if not _PATH_INDEX_RE.search(entry["path"]) and entry["path"] == stripped
        ]
    spans = sorted({(entry["start"], entry["end"]) for entry in matches})
    if len(spans) != 1:
        return None
    start, end = spans[0]
    recorded_text = matches[0].get("text")
    if isinstance(recorded_text, str) and recorded_text and recorded_text != query[start:end]:
        return None
    return start, end


def _literal_binding_span(query: str, plan: dict[str, Any], value: Any) -> tuple[int, int] | None:
    """리터럴 바인딩(앱 소유 결정론 추출)의 정규화값과 슬롯 값의 구조 동치로 스팬을 복원한다.
    날짜창은 from/to 정확 일치, 수치는 대표 필드(threshold/count/...) 하나만 대조한다 —
    한 조건 안의 여러 숫자를 전부 풀어 비교하면 우연 일치가 는다."""
    literals = plan.get("literal_bindings")
    if not isinstance(literals, list):
        return None

    def span_of(item: dict[str, Any]) -> tuple[int, int] | None:
        start, end = item.get("start"), item.get("end")
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(query)):
            return None
        # 리터럴은 다른 문자열(재작성 프롬프트 등) 기준으로 추출됐을 수 있다 — 본문이 일치할 때만
        # 이 query 좌표계의 스팬으로 신뢰한다.
        text = item.get("text")
        if isinstance(text, str) and text and query[start:end] != text:
            return None
        return start, end

    if isinstance(value, Mapping) and isinstance(value.get("from"), str) and isinstance(value.get("to"), str):
        candidates = []
        for item in literals:
            if not isinstance(item, dict) or item.get("kind") != "date_window":
                continue
            normalized = item.get("normalized")
            if (
                isinstance(normalized, Mapping)
                and normalized.get("from") == value["from"]
                and normalized.get("to") == value["to"]
            ):
                span = span_of(item)
                if span is not None:
                    candidates.append(span)
        return _unique_span(candidates)

    primary: Decimal | None = exact_decimal(value, allow_string=True)
    if isinstance(value, bool):
        primary = None
    elif isinstance(value, Mapping):
        for key in ("threshold", "count", "amount", "value", "min_days", "max_days"):
            candidate = value.get(key)
            exact = exact_decimal(candidate, allow_string=True)
            if exact is not None:
                primary = exact
                break
    if primary is None:
        return None
    candidates = []
    for item in literals:
        if not isinstance(item, dict) or item.get("kind") not in {"number", "number_with_unit", "percentage"}:
            continue
        normalized = item.get("normalized")
        norm_value = normalized.get("value") if isinstance(normalized, Mapping) else normalized
        normalized_decimal = exact_decimal(norm_value, allow_string=True)
        if normalized_decimal is not None and normalized_decimal == primary:
            span = span_of(item)
            if span is not None:
                candidates.append(span)
    return _unique_span(candidates)


def _registry_synonym_span(query: str, slot: str, value: Any) -> tuple[int, int] | None:
    """canonical 값(레지스트리 어휘)의 원문 표면형을 동의어 사전으로 역탐색한다.
    현재는 주문 횟수 행동(behaviors)만 — 레지스트리가 synonyms 를 선언하는 유일한 canonical 슬롯이다."""
    if slot != "behaviors" or not isinstance(value, str):
        return None
    spec = member_filters_config.behavior_spec(value)
    if not isinstance(spec, dict):
        return None
    folded = query.casefold()
    candidates: list[tuple[int, int]] = []
    for synonym in spec.get("synonyms") or []:
        if not isinstance(synonym, str) or not synonym:
            continue
        needle = synonym.casefold()
        start = folded.find(needle)
        while start >= 0:
            candidates.append((start, start + len(synonym)))
            start = folded.find(needle, start + 1)
    return _unique_span(candidates)


def _requirement_span(
    query: str,
    plan: dict[str, Any],
    container: str,
    slot: str,
    path: str,
    value: Any,
) -> tuple[int, int, str, str]:
    recorded = _slot_span(plan, container, slot, value)
    if recorded is not None:
        start, end = recorded["start"], recorded["end"]
        recorded_source = recorded.get("source")
        same_coordinates = (
            not isinstance(recorded_source, str)
            or recorded_source.startswith(query)
            or query.startswith(recorded_source)
        )
        if same_coordinates and 0 <= start < end <= len(query):
            return start, end, query[start:end], "exact"
    matched = _matched_term_span(query, plan, value)
    if matched is not None:
        return matched[0], matched[1], query[matched[0]:matched[1]], "exact"
    folded = query.casefold()
    for candidate in _value_text_candidates(value):
        start = folded.find(candidate.casefold())
        if start >= 0:
            return start, start + len(candidate), query[start:start + len(candidate)], "exact"
    # 결정론 스팬 재구성 — LLM 슬롯은 위 세 경로가 전부 비어 전문장 폴백으로 떨어져 왔고, 그러면
    # 스팬 기반 중복 판별·소유권 회수·리터럴 소비 감사가 전부 무력화된다. 후보가 유일할 때만 채택.
    evidence = _evidence_span(query, plan, path)
    if evidence is not None:
        return evidence[0], evidence[1], query[evidence[0]:evidence[1]], "evidence"
    literal = _literal_binding_span(query, plan, value)
    if literal is not None:
        return literal[0], literal[1], query[literal[0]:literal[1]], "literal"
    registry = _registry_synonym_span(query, slot, value)
    if registry is not None:
        return registry[0], registry[1], query[registry[0]:registry[1]], "registry"
    # 정밀 구간을 복원하지 못한 필터도 요구사항을 잃지 않는다. 이 경우 원문 전체를 보수적인 근거
    # 구간으로 두고 span_precision="whole_query" 로 표시한다 — 소비자는 이 신호로 정밀 span 과 구분한다.
    return 0, len(query), query, "whole_query"


def _stable_requirement_id(
    *, path: str, polarity: str, source: str, span: tuple[int, int], value: Any
) -> str:
    payload = json.dumps(
        {
            "path": path,
            "polarity": polarity,
            "source": source,
            "span": list(span),
            "value": _thaw_json(_freeze_json(value)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sr_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def capture_plan_source_requirements(
    query: str,
    plan: dict[str, Any],
    *,
    source: str = "rules",
) -> tuple[SourceRequirement, ...]:
    """파서가 인식한 모든 사용자 의미 슬롯을 불변 SourceRequirement로 캡처한다.

    이 함수는 조건 소유권 조정·분석 라우팅보다 먼저 호출하는 것을 전제로 한다. 이후 plan 슬롯이
    ``pop``/이동되더라도 반환 객체는 frozen + 재귀 frozen 값이라 바뀌지 않는다.
    """
    captured: list[SourceRequirement] = []
    for container, slot, path, value in _iter_requirement_values(plan):
        # 조건 객체 안의 source(예: 데이터 원천/테이블명)와 파서 provenance를 섞지 않는다.
        item_source = source
        negative = _negative_requirement(container, slot, value)
        polarity = "negative" if negative else "positive"
        start, end, source_text, precision = _requirement_span(query, plan, container, slot, path, value)
        captured.append(SourceRequirement(
            id=_stable_requirement_id(
                path=path, polarity=polarity, source=item_source, span=(start, end), value=value
            ),
            type="source_condition",
            base={"type": container, "name": slot},
            relation="excluded_from" if negative else "applies_to",
            value=value,
            negation=negative,
            source_text=source_text,
            source_span=(start, end),
            path=path,
            polarity=polarity,
            source=item_source,
            status="captured",
            span_precision=precision,
        ))
    return tuple(captured)


def _semantic_obligation(
    query: str,
    *,
    kind: str,
    span: tuple[int, int],
    value: dict[str, Any],
) -> SourceRequirement:
    """Create an immutable requirement for meaning that must not be flattened."""

    start, end = span
    path = f"source_semantics.{kind}"
    return SourceRequirement(
        id=_stable_requirement_id(
            path=path,
            polarity="positive",
            source="source_semantic_contract",
            span=span,
            value=value,
        ),
        type="semantic_obligation",
        base={"type": "semantic_operator", "name": kind},
        relation="must_preserve",
        value=value,
        source_text=query[start:end],
        source_span=span,
        path=path,
        polarity="positive",
        source="source_semantic_contract",
        status="detected",
    )


SEMANTIC_OBLIGATION_TYPE = "semantic_obligation"


def obligation_kind(requirement: Any) -> str:
    """semantic obligation 의 종류 이름(``base.name``). 의무가 아니면 빈 문자열.

    캡처 직후의 :class:`SourceRequirement` 와 원장에 실린 dict 를 **둘 다** 읽는다. 같은
    판정을 소비자마다 다시 적으면 한쪽만 고쳐진 상태가 조용히 생긴다(원장/영수증/프롬프트/
    반박이 각각 이 값을 본다).
    """

    if isinstance(requirement, SourceRequirement):
        requirement_type, base = requirement.type, requirement.base
    elif isinstance(requirement, Mapping):
        requirement_type, base = requirement.get("type"), requirement.get("base")
    else:
        return ""
    if requirement_type != SEMANTIC_OBLIGATION_TYPE or not isinstance(base, Mapping):
        return ""
    name = base.get("name")
    return str(name) if isinstance(name, str) and name else ""


def _span_bounds(span: Any) -> tuple[int, int] | None:
    """source span 을 (start, end) 로 읽는다. 좌표가 없거나 빈 구간이면 None."""

    if isinstance(span, Mapping):
        start, end = span.get("start"), span.get("end")
    elif isinstance(span, (list, tuple)) and len(span) == 2:
        start, end = span
    else:
        return None
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
        return None
    return (start, end) if start < end else None


def requirement_span(requirement: Any) -> tuple[int, int] | None:
    """requirement 의 원문 구간 (start, end). 좌표가 없으면 None.

    :class:`SourceRequirement` 와 원장 dict 를 둘 다 읽는다 — :func:`obligation_kind` 와 같은
    이유다(소비자가 좌표 읽는 법을 각자 적으면 한쪽만 고쳐진 상태가 조용히 생긴다).
    """
    if isinstance(requirement, SourceRequirement):
        return _span_bounds(requirement.source_span)
    if isinstance(requirement, Mapping):
        return _span_bounds(requirement.get("source_span"))
    return _span_bounds(requirement)


def spans_overlap(left: Any, right: Any) -> bool:
    """두 원문 구간이 실제로 겹치는가.

    비교할 수 없는 좌표(없음·빈 구간)는 **False** 다 — 이 술어의 소비자는 '겹치면 개입한다'
    이므로, 모르는 것을 참으로 답하면 근거 없는 개입이 된다.
    """

    left_bounds, right_bounds = _span_bounds(left), _span_bounds(right)
    if left_bounds is None or right_bounds is None:
        return False
    return left_bounds[0] < right_bounds[1] and right_bounds[0] < left_bounds[1]


def _recurrence_obligations(query: str) -> list[SourceRequirement]:
    requirements: list[SourceRequirement] = []
    for match in _RECURRENCE_RE.finditer(query):
        # "매월 뉴스레터" 같은 캠페인 일정 명사를 타겟 조건으로 오인하지 않는다. 주기 표지와 같은
        # 절에 대상 결과를 바꾸는 행동/상태 술어가 있을 때만 의미 의무로 봉인한다.
        clause_start = max(
            query.rfind(",", 0, match.start()),
            query.rfind(".", 0, match.start()),
            query.rfind(";", 0, match.start()),
        ) + 1
        following_boundaries = [
            index
            for token in (",", ".", ";")
            if (index := query.find(token, match.end())) >= 0
        ]
        clause_end = min(following_boundaries) if following_boundaries else len(query)
        clause = query[clause_start:clause_end]
        if _RECURRENCE_CONDITION_RE.search(clause) is None:
            continue
        raw_bucket = match.group("bucket") or match.group("bucket2")
        requirements.append(_semantic_obligation(
            query,
            kind="temporal_recurrence",
            span=(match.start(), match.end()),
            value={
                "quantifier": "every",
                "bucket": _BUCKET_VALUES[raw_bucket],
                "surface": match.group("marker"),
            },
        ))
    return requirements


def _entity_set_obligations(query: str) -> list[SourceRequirement]:
    requirements: list[SourceRequirement] = []
    for match in _DEICTIC_ENTITY_SET_RE.finditer(query):
        tail_end = min(len(query), match.end() + 24)
        quantifier = _ALL_QUANTIFIER_RE.search(query[match.end():tail_end])
        if quantifier is None:
            continue
        raw_count = match.group("count")
        cardinality = (
            int(raw_count)
            if isinstance(raw_count, str) and raw_count.isdigit()
            else korean_number_normalizer.parse_native_korean_number(raw_count)
        )
        entity = match.group("entity")
        domain = {
            "브랜드": "brand",
            "상품": "product",
            "제품": "product",
            "품목": "product",
            "카테고리": "category",
        }[entity]
        requirements.append(_semantic_obligation(
            query,
            kind="referenced_entity_set",
            span=(match.start(), quantifier.end() + match.end()),
            value={
                "reference": "deictic",
                "entity_domain": domain,
                "cardinality": cardinality,
                "set_quantifier": "all",
            },
        ))
    return requirements


def _configured_term_hits(query: str, values: Mapping[str, Any]) -> list[tuple[int, int, str]]:
    """Find catalog-owned aliases without copying their vocabulary into code."""
    hits: list[tuple[int, int, str]] = []
    folded = query.casefold()
    for canonical, declaration in values.items():
        if isinstance(declaration, Mapping):
            terms = declaration.get("terms")
        elif isinstance(declaration, list):
            terms = declaration
        else:
            continue
        candidates = [canonical, *(terms if isinstance(terms, list) else [])]
        for candidate in candidates:
            term = str(candidate or "").strip()
            if not term:
                continue
            cursor = 0
            needle = term.casefold()
            while (start := folded.find(needle, cursor)) >= 0:
                hits.append((start, start + len(term), str(canonical)))
                cursor = start + max(1, len(term))
    return sorted(set(hits))


@lru_cache(maxsize=1)
def _ranked_cardinality_pattern() -> re.Pattern[str]:
    """'중 2개 이상' — 랭킹 집합과 회원 행동의 **교집합 개수** 임계 문법.

    비교어 어휘는 공용 비교 문법(:mod:`condition_normalizers`)이 소유하고, 집합 범위 표지와
    계수 단위는 어휘 사전이 소유한다 — 여기에 낱말을 다시 적지 않는다. 계수 단위를 필수로 둬
    '중 3년 이상' 같은 기간 표현이 개수로 오포착되는 것을 막는다.
    """
    words = sorted(
        condition_normalizers.comparison_word_operators(), key=lambda word: (-len(word), word)
    )
    comparison_alt = "|".join(re.escape(word) for word in words)
    return re.compile(
        rf"\s*(?:{_SET_SCOPE_ALT})\s*(?P<value>\d[\d,]*)\s*(?:{_ENTITY_COUNTER_ALT})"
        rf"(?:\s*(?P<operator>{comparison_alt}|만))?"
    )


def _ranked_cardinality_at(query: str, position: int) -> tuple[dict[str, Any], int] | None:
    """랭킹 구절 **바로 뒤**의 교집합 개수 임계. (값, 구절 끝 위치) 또는 None.

    ``상위 5개 중 2개 이상`` 의 '2개'는 랭킹 집합 크기(5)도, 같은 상품을 산 횟수도 아니다 —
    회원이 산 상품과 상위 5개 집합의 **교집합에 들어가는 서로 다른 엔터티 수**다.

    비교 표지가 없거나 '만'이면 정확히 그 개수(``=``)다. 같은 표면형에 대한 이 저장소의 기존
    판정(:mod:`entity_set` 카디널리티 표)과 같은 답을 쓴다 — 여기서 다른 규칙을 고르면 한
    어구의 뜻이 경로마다 달라진다.
    """
    match = _ranked_cardinality_pattern().match(query, position)
    if match is None:
        return None
    return (
        {
            "operator": condition_normalizers.canonical_operator(match.group("operator")) or "=",
            "value": int(match.group("value").replace(",", "")),
            "distinct": True,
        },
        match.end(),
    )


def _obligation_window(
    binding: temporal_clause.ScopeWindowBinding | None,
) -> dict[str, Any] | None:
    """결속된 창을 의무 값의 창 표기로. 결속이 없으면 ``None`` (기간을 지어내지 않는다)."""
    if binding is None:
        return None
    return {
        "from": binding.window.get("from"),
        "to": binding.window.get("to"),
        "label": binding.window.get("label"),
    }


def _ranked_entity_set_candidates(
    query: str,
) -> tuple[dict[tuple[Any, ...], tuple[int, int]], list[tuple[int, int, str]]]:
    """랭킹 구절의 **기하와 계약**만 (기준일도, 창도 보지 않는다).

    창 결속(:func:`ranked_window_scope_bindings`)과 의무 기록이 같은 앵커를 써야 하므로 이
    계산은 한 곳에만 있다 — 둘이 각자 구절을 찾으면 같은 문장에서 다른 절 경계를 갖게 된다.
    반환: ``{랭킹 계약 키: (구절 시작, 구절 끝)}`` 와 관계 표면어 히트.
    """
    # Canonical relation vocabulary and bindings share the exact same catalog
    # snapshot as schema validation and SQL compilation.  The legacy SQL target
    # registry is intentionally not another semantic owner.
    import audience_runtime

    raw_catalog = audience_runtime.load_audience_catalog_config()
    recipes = raw_catalog.get("relation_recipes")
    config = (
        recipes.get("ranked_entity_membership")
        if isinstance(recipes, Mapping) else None
    )
    if not config:
        return {}, []
    directions = _configured_term_hits(query, config.get("directions") or {})
    entities = _configured_term_hits(query, config.get("entities") or {})
    if not directions or not entities:
        return {}, []

    # A rank cardinality must be adjacent to the entity phrase.  The grammar is
    # deliberately domain-neutral: it does not know products, brands, or a
    # particular Top-N case.
    number_hits = [
        (match.start(), match.end(), int(match.group("value").replace(",", "")))
        for match in re.finditer(
            r"(?<![\d.])(?P<value>\d[\d,]*)(?![\d,])"
            r"(?!\s*(?:%|퍼센트|프로))\s*(?:개|종류|종)?",
            query,
        )
    ]
    candidates: list[tuple[int, int, int, str, str]] = []
    for direction_start, direction_end, direction in directions:
        for entity_start, entity_end, entity in entities:
            nearby = [
                (start, end, value)
                for start, end, value in number_hits
                if -12 <= start - entity_end <= 12 or -12 <= entity_start - end <= 12
            ]
            if not nearby:
                continue
            number_start, number_end, limit = min(
                nearby,
                key=lambda item: min(abs(item[0] - entity_end), abs(entity_start - item[1])),
            )
            span_start = min(direction_start, entity_start, number_start)
            span_end = max(direction_end, entity_end, number_end)
            candidates.append((span_start, span_end, limit, direction, entity))
    if not candidates:
        return {}, []

    measure_hits = _configured_term_hits(query, config.get("measures") or {})
    relation_hits = _configured_term_hits(query, config.get("relations") or {})
    default_measure = str(config.get("defaultMeasure") or "") or None
    default_relation = str(config.get("defaultRankRelation") or "") or None
    resolved_candidates: dict[tuple[Any, ...], tuple[int, int]] = {}
    for span_start, span_end, limit, direction, entity in candidates:
        measure = min(
            measure_hits,
            key=lambda item: min(abs(item[0] - span_end), abs(span_start - item[1])),
            default=(0, 0, default_measure),
        )[2]
        relation = min(
            relation_hits,
            key=lambda item: min(abs(item[0] - span_end), abs(span_start - item[1])),
            default=(0, 0, default_relation),
        )[2]
        relation_declaration = (config.get("relations") or {}).get(relation)
        relation_declaration = (
            relation_declaration if isinstance(relation_declaration, Mapping) else {}
        )
        entity_fields = relation_declaration.get("canonicalEntities")
        entity_field = (
            entity_fields.get(entity)
            if isinstance(entity_fields, Mapping) and isinstance(entity_fields.get(entity), str)
            else None
        )
        measure_declarations = relation_declaration.get("canonicalMeasures")
        measure_declaration = (
            measure_declarations.get(measure)
            if isinstance(measure_declarations, Mapping)
            and isinstance(measure_declarations.get(measure), Mapping)
            else {}
        )
        source = relation_declaration.get("canonicalSource")
        source = str(source) if isinstance(source, str) and source else None
        measure_function = measure_declaration.get("function")
        measure_function = (
            str(measure_function) if isinstance(measure_function, str) and measure_function else None
        )
        measure_field = measure_declaration.get("field")
        measure_field = str(measure_field) if isinstance(measure_field, str) and measure_field else None
        measure_distinct = bool(measure_declaration.get("distinct", False))
        key = (
            limit, direction, entity, measure, relation, source,
            entity_field, measure_function, measure_field, measure_distinct,
        )
        previous = resolved_candidates.get(key)
        resolved_candidates[key] = (
            min(span_start, previous[0]) if previous else span_start,
            max(span_end, previous[1]) if previous else span_end,
        )
    return resolved_candidates, relation_hits


def _ranked_scope_anchors(
    ranking_span: tuple[int, int],
    relation: Any,
    relation_hits: Sequence[tuple[int, int, str]],
) -> tuple[tuple[str, int, int], ...]:
    """랭킹 구절 하나의 창 결속 앵커. 행동 앵커는 구절 **뒤**의 같은 관계 표면어만 쓴다.

    어순 제약(구절 뒤)이 있어야 구절 안의 지표 낱말('구매 건수'의 '구매')이 행동 앵커로 오인되지
    않는다 — 그렇게 되면 랭킹 창이 회원 행동 창으로 조용히 옮겨 붙는다.
    """
    span_start, span_end = ranking_span
    return (
        (temporal_clause.SCOPE_RANKING, span_start, span_end),
        *(
            (temporal_clause.SCOPE_MEMBERSHIP, hit_start, hit_end)
            for hit_start, hit_end, canonical in relation_hits
            if canonical == relation and hit_start >= span_end
        ),
    )


def ranked_window_scope_bindings(
    query: str,
    windows: Sequence[tuple[Mapping[str, Any], int, int]],
) -> list[temporal_clause.ScopeWindowBinding]:
    """이 문장의 달력 창들이 랭킹 요청의 **어느 절**에 결속되는가.

    창의 *값*은 호출자가 준다 — 상대 연도('작년')는 기준일을 가진 계층(리터럴 바인딩)만 풀 수
    있고, 이 원장은 기준일을 받지 않기 때문이다(같은 질의가 지점마다 다른 의무를 만들면 유령
    미해결이 생긴다). 여기서 정하는 것은 값이 아니라 **소속**이며 그 판정은 순수 기하다.

    반환은 결속된 것만이다. 결속되지 않은 창은 이 요청의 절 어디에도 붙지 않는다는 뜻이고,
    그 처리는 호출자가 정한다(추측해서 아무 절에나 붙이지 않는다).
    """
    if not windows:
        return []
    resolved_candidates, relation_hits = _ranked_entity_set_candidates(query)
    bindings: list[temporal_clause.ScopeWindowBinding] = []
    for key, ranking_span in resolved_candidates.items():
        relation = key[4]
        bindings.extend(temporal_clause.bind_window_scopes(
            query,
            windows=windows,
            anchors=_ranked_scope_anchors(ranking_span, relation, relation_hits),
        ))
    return bindings


def _ranked_entity_set_obligations(query: str) -> list[SourceRequirement]:
    """Capture catalog-declared rank -> entity-set -> membership composition.

    This is a loss guard, not a query builder.  It records the fixed relational
    operators implied by a sentence so an incomplete model expression cannot
    silently degrade to a plain event-existence filter.  Business vocabulary
    and defaults come exclusively from ``entity_set_targets``.

    기간은 절마다 다르다: 랭킹 구절 앞의 창은 모집단의 기간(``time_window``)이고, 행동 동사
    앞의 창은 회원이 그 행동을 한 기간(``membership_time_window``)이다. 상대 연도는 기준일이
    필요해 이 원장이 풀 수 없으므로 여기 실리는 것은 절대 창뿐이다 — 상대 창의 결속은 같은
    판정기(:func:`ranked_window_scope_bindings`)를 기준일 있는 계층이 호출해 얻는다.
    """
    resolved_candidates, relation_hits = _ranked_entity_set_candidates(query)
    requirements: list[SourceRequirement] = []
    for (
        limit, direction, entity, measure, relation, source,
        entity_field, measure_function, measure_field, measure_distinct,
    ), (span_start, span_end) in resolved_candidates.items():
        bindings = temporal_clause.bind_window_scopes(
            query,
            windows=parse_calendar_window_spans(query),
            anchors=_ranked_scope_anchors((span_start, span_end), relation, relation_hits),
        )
        scoped_windows: dict[str, temporal_clause.ScopeWindowBinding] = {}
        for binding in bindings:
            scoped_windows.setdefault(binding.scope, binding)
        ranking_binding = scoped_windows.get(temporal_clause.SCOPE_RANKING)
        membership_binding = scoped_windows.get(temporal_clause.SCOPE_MEMBERSHIP)
        if ranking_binding is not None:
            span_start = ranking_binding.window_span[0]
        time_window = _obligation_window(ranking_binding)
        membership_time_window = _obligation_window(membership_binding)
        value: dict[str, Any] = {
            "direction": direction,
            "limit": limit,
            "entity_domain": entity,
            "measure": measure,
            "rank_relation": relation,
            "membership_relation": relation,
            "source": source,
            "entity_field": entity_field,
            "measure_function": measure_function,
            "measure_field": measure_field,
            "measure_distinct": measure_distinct,
            "time_window": time_window,
        }
        # 개수 임계는 **선택 항목**이다. 없는 요청(‘상위 5개를 구매한 회원’)의 의무는 예전과
        # 바이트 동일해야 한다 — 키를 항상 실으면 모든 랭킹 의무의 id 가 한꺼번에 바뀐다.
        cardinality_hit = _ranked_cardinality_at(query, span_end)
        if cardinality_hit is not None:
            cardinality, span_end = cardinality_hit
            value["cardinality"] = cardinality
        # 회원 행동 기간도 같은 이유로 **있을 때만** 싣는다. 기본 결속(랭킹 전용)의 의무 id 와
        # 직렬화는 이 변경 전과 바이트 동일해야 한다.
        if membership_time_window is not None:
            value["membership_time_window"] = membership_time_window
            # 그 창의 원문 구간도 이 의무가 소유한다 — 소유자를 밝히지 않으면 다른 파서가 같은
            # 글자를 다시 읽거나(이중 소유) 주인 없는 창으로 남는다.
            window_start, window_end = membership_binding.window_span
            if window_start >= span_end:
                span_end = max(span_end, window_end)
        requirements.append(_semantic_obligation(
            query,
            kind="ranked_entity_set",
            span=(span_start, span_end),
            value=value,
        ))
    return requirements


_RANKING_LIMIT_RE = re.compile(
    r"(?<![\d.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>%|\ud37c\uc13c\ud2b8|\ud504\ub85c|\uba85|\uac1c)"
)
_RANKING_PERCENT_UNITS = frozenset({"%", "\ud37c\uc13c\ud2b8", "\ud504\ub85c"})


def _ranking_limit_hits(
    query: str, allowed_units: frozenset[str]
) -> list[tuple[int, int, dict[str, Any]]]:
    """Parse count/percentage rank sizes without turning units into thresholds."""
    hits: list[tuple[int, int, dict[str, Any]]] = []
    for match in _RANKING_LIMIT_RE.finditer(query):
        unit = "percent" if match.group("unit") in _RANKING_PERCENT_UNITS else "count"
        if unit not in allowed_units:
            continue
        try:
            value = Decimal(match.group("value").replace(",", ""))
        except InvalidOperation:
            continue
        if not value.is_finite() or value <= 0:
            continue
        if unit == "percent":
            if value >= 100:
                continue
            wire_value: int | float | str = decimal_json_value(value)
        else:
            if value != value.to_integral_value():
                continue
            wire_value = int(value)
        hits.append(
            (match.start(), match.end(), {"type": unit, "value": wire_value})
        )
    return hits


def _ranking_clause_bounds_at(query: str, position: int, end: int) -> tuple[int, int]:
    """Find a ranking clause while keeping decimal points inside the clause."""
    boundaries = [
        match.start()
        for match in re.finditer(r"(?<!\d),(?!\d)|;|(?<!\d)\.(?!\d)", query)
    ]
    starts = [index for index in boundaries if index < position]
    ends = [index for index in boundaries if index >= end]
    return (max(starts) + 1 if starts else 0), (min(ends) if ends else len(query))


def _member_metric_ranking_obligations(query: str) -> list[SourceRequirement]:
    """Capture member ranking semantics from the materialized catalog recipe."""
    import audience_runtime

    catalog = audience_runtime.catalog_snapshot()
    recipes = catalog.get("relation_recipes")
    config = (
        recipes.get("member_metric_ranking")
        if isinstance(recipes, Mapping)
        else None
    )
    if not isinstance(config, Mapping):
        return []
    directions = _configured_term_hits(query, config.get("directions") or {})
    entities = _configured_term_hits(query, config.get("entities") or {})
    metric_hits = _configured_term_hits(query, config.get("metrics") or {})
    if not directions or not entities or not metric_hits:
        return []

    # Prefer the longest catalog phrase at an overlapping position.  Thus
    # "최대 구매금액" resolves to that metric rather than its shorter
    # "구매금액" suffix, without hard-coding either phrase here.
    longest_metric_hits = [
        hit
        for hit in metric_hits
        if not any(
            other[0] <= hit[0]
            and hit[1] <= other[1]
            and (other[1] - other[0]) > (hit[1] - hit[0])
            for other in metric_hits
        )
    ]
    declared_units = config.get("limit_units")
    if (
        not isinstance(declared_units, list)
        or any(unit not in {"count", "percent"} for unit in declared_units)
    ):
        return []
    limit_hits = _ranking_limit_hits(query, frozenset(declared_units))
    if not limit_hits:
        return []

    metric_declarations = config.get("metrics")
    policy = config.get("policy")
    if not isinstance(metric_declarations, Mapping) or not isinstance(policy, Mapping):
        return []
    requirements: list[SourceRequirement] = []
    seen: set[tuple[Any, ...]] = set()
    for metric_start, metric_end, metric_id in longest_metric_hits:
        clause_start, clause_end = _ranking_clause_bounds_at(
            query, metric_start, metric_end
        )
        clause_directions = [
            item for item in directions if clause_start <= item[0] and item[1] <= clause_end
        ]
        clause_entities = [
            item for item in entities if clause_start <= item[0] and item[1] <= clause_end
        ]
        clause_limits = [
            item for item in limit_hits if clause_start <= item[0] and item[1] <= clause_end
        ]
        if not clause_directions or not clause_entities or not clause_limits:
            continue
        direction_start, direction_end, direction = min(
            clause_directions,
            key=lambda item: min(abs(item[0] - metric_end), abs(metric_start - item[1])),
        )
        entity_start, entity_end, _entity = min(
            clause_entities,
            key=lambda item: min(abs(item[0] - direction_end), abs(direction_start - item[1])),
        )
        limit_start, limit_end, limit = min(
            clause_limits,
            key=lambda item: min(abs(item[0] - direction_end), abs(direction_start - item[1])),
        )
        if min(abs(limit_start - direction_end), abs(direction_start - limit_end)) > 16:
            continue
        declaration = metric_declarations.get(metric_id)
        if not isinstance(declaration, Mapping):
            continue
        key = (metric_id, direction, limit["type"], limit["value"])
        if key in seen:
            continue
        seen.add(key)
        span_start = min(metric_start, direction_start, entity_start, limit_start)
        span_end = max(metric_end, direction_end, entity_end, limit_end)
        requirements.append(
            _semantic_obligation(
                query,
                kind="member_metric_ranking",
                span=(span_start, span_end),
                value={
                    "direction": direction,
                    "limit": limit,
                    "metric_id": metric_id,
                    "source": declaration.get("source"),
                    "entity_field": declaration.get("entity_field"),
                    "measure_function": declaration.get("measure_function"),
                    "measure_field": declaration.get("measure_field"),
                    "population": policy.get("population"),
                    "tie_policy": policy.get("tie_policy"),
                    "null_policy": policy.get("null_policy"),
                    "missing_policy": policy.get("missing_policy"),
                    "small_population_policy": policy.get("small_population_policy"),
                },
            )
        )
    return requirements


def _snapshot_obligations(query: str) -> list[SourceRequirement]:
    requirements: list[SourceRequirement] = []
    for match in _LATEST_SNAPSHOT_RE.finditer(query):
        context = query[max(0, match.start() - 12):match.end()].casefold()
        if not any(marker in context for marker in ("crm", "회원", "고객")):
            continue
        requirements.append(_semantic_obligation(
            query,
            kind="snapshot_selector",
            span=(match.start(), match.end()),
            value={
                "selector": "latest",
                "partition_by": "member",
                "grain": "month" if match.group("grain") else None,
            },
        ))
    return requirements


# 속성 시점·이력 한정어의 의무 기록.
#
# '지난달 말 기준 …'·'… 에서 … 로 승급'·'3개월 내내 유지'가 현재 값 필터로 조용히 축소되면
# 표면상 성공하는 오답이 된다 — 한정어를 원장에 기록해 컴파일 영수증 없이는 통과하지 못하게 한다.
#
# 2026-08-02 일반화: 표지 정규식과 그 안의 값 어휘(등급/상태 값)를 여기 나열하지 않는다.
# 어휘는 eq_filters/attribute_catalog 단일 소유에서 파생하고, 표지는 도메인 계층
# (`targeting_domain.temporal_lexicon`)이 **범용 시간 연산자**로 사상해 준다. 값이 늘면
# 설정 한 줄로 함께 열리고, 같은 낱말이 세 모듈의 정규식에 재등장하지 않는다.
TEMPORAL_QUALIFIER_KIND = "member_state_history"


def _clause_bounds_at(query: str, position: int, end: int) -> tuple[int, int]:
    clause_start = max(
        query.rfind(",", 0, position),
        query.rfind(".", 0, position),
        query.rfind(";", 0, position),
    ) + 1
    following = [
        index for token in (",", ".", ";") if (index := query.find(token, end)) >= 0
    ]
    return clause_start, (min(following) if following else len(query))


def _member_state_history_obligations(query: str) -> list[SourceRequirement]:
    """시간 한정어 마커 → 의무. 마커의 **범용 연산자**를 값에 함께 기록한다."""
    import targeting_domain  # 도메인 어휘 조립(지연 import — 순환 없음)

    try:
        lexicon = targeting_domain.temporal_lexicon()
    except Exception:  # noqa: BLE001 — 어휘 파생 실패로 요구 기록이 죽으면 안 된다.
        return []
    markers = lexicon.detect(
        query, clause_bounds=lambda text, position: _clause_bounds_at(text, position, position)
    )
    return [
        _semantic_obligation(
            query,
            kind=TEMPORAL_QUALIFIER_KIND,
            span=(marker.start, marker.end),
            value={"marker": marker.text, "temporal_operator": marker.operator},
        )
        for marker in markers
        # 이 원장이 기록하는 것은 '이 절이 **이력 관측**을 요구한다'이다. '현재 등급이 VIP'는
        # 그 요구가 아니므로(현재값 자산이 그대로 답한다) 기록하지 않는다 — 기록하면 아무도
        # 방면할 수 없는 의무가 남아 이력이 필요 없는 문장이 미귀결로 막힌다.
        if not targeting_domain.selects_current_value(marker.operator, marker.text)
    ]


# ── 변화의 크기(change magnitude) ─────────────────────────────────────────────────────
#
# '증가/감소'는 **방향**이고 '10% 이상'·'5만원 이상'·'2배'·'절반으로'는 **크기**다. 크기가
# 어느 계층에서도 모델링되지 않던 동안, 방향만 담은 비교가 그 자리의 소유권까지 주장했다 —
# 근거는 먹고 의미는 안 실은 채로. 그 결과 두 갈래의 피해가 났다(실측 2026-08-08).
#
#   숫자형('10% 이상')   리터럴 정산이 막아 SQL 0건 + 종결 불가 재시도
#   낱말형('절반으로')   리터럴 원장에도 안 잡혀 **'조금이라도 줄어든 전원'이 그대로 출고**
#
# 그래서 크기는 리터럴이 있든 없든 **원문에서 먼저 의무로 기록**한다. 낮출 수 있으면 영수증으로
# 방면되고, 못 낮추면 미귀결로 남아 출고를 막는다(fail-close). 이 파일이 이미 갖고 있는 계약을
# 그대로 쓰는 것이고 새 게이트를 만들지 않는다.
CHANGE_MAGNITUDE_KIND = "change_magnitude"

# 크기와 방향 낱말이 같은 절 안에서 이 거리 이내에 있어야 한 요구로 묶는다. 랭킹 의무
# (:func:`_member_metric_ranking_obligations`, 16자)와 같은 계열의 국소 인접 판정이다.
_CHANGE_MAGNITUDE_ADJACENCY = 16


@dataclass(frozen=True)
class ChangeMagnitude:
    """변화의 크기 하나. 비율은 **유리수**로, 절대 증분은 Decimal 로 들고 있는다.

    비율에 float 를 쓰지 않는 이유는 CLAUDE.md §14 이자 실측이다 — 비율형
    ``(L-R)/NULLIF(R,0) >= 0.1`` 은 정수 지표에서 조용히 틀리고(``(11-10)/10 = 0``),
    :class:`event_ir.Literal` 은 애초에 ``Decimal`` 을 거부한다. 그래서 비교는 나눗셈 없이
    **교차곱**으로 낮춘다(:mod:`lowering_planner`).

        비율   ``L * denominator  OP  R * numerator``
        절대   ``(L - R) OP amount``  (감소면 ``R - L``)

    ``numerator/denominator`` 는 기준값에 곱할 배수다 — '10% 이상 증가'는 11/10, '20% 감소'는
    4/5, '2배'는 2/1, '절반'은 1/2. 방향이 비교의 향을 정하고(증가 ``>=`` / 감소 ``<=``),
    이상·초과 같은 경계 낱말은 **포함 여부만** 정한다. 이 분리가 극성 반전을 구조적으로 막는다.
    """

    kind: str  # "ratio" | "absolute"
    direction: str  # "increase" | "decrease"
    inclusive: bool
    numerator: int | None
    denominator: int | None
    amount: Decimal | None
    unit: str  # percent | multiple | fraction | 통화코드
    source_text: str
    source_span: tuple[int, int]

    def to_value(self) -> dict[str, Any]:
        """원장에 실을 JSON 값. ``Decimal`` 은 정확 문자열로 남긴다."""
        value: dict[str, Any] = {
            "magnitude_kind": self.kind,
            "direction": self.direction,
            "inclusive": self.inclusive,
            "unit": self.unit,
            "text": self.source_text,
        }
        if self.numerator is not None and self.denominator is not None:
            value["numerator"] = self.numerator
            value["denominator"] = self.denominator
        if self.amount is not None:
            value["amount"] = decimal_json_value(self.amount)
        return value


def _ratio_from_percent(percent: Decimal, direction: str) -> tuple[int, int] | None:
    """퍼센트 변화 → 기준값에 곱할 유리수. 10%↑ → 11/10, 20%↓ → 4/5.

    소수 퍼센트도 정확히 담는다('10.5%' → 2105/2000 → 421/400). 나눗셈을 쓰지 않으므로
    자릿수가 늘 뿐 값이 흔들리지 않는다.
    """
    try:
        scaled = (percent * 100).to_integral_exact()
    except (InvalidOperation, ValueError):
        return None
    if scaled != percent * 100:
        return None
    delta = int(scaled)
    if delta < 0:
        return None
    numerator = 10_000 + delta if direction == "increase" else 10_000 - delta
    if numerator <= 0:
        # '100% 이상 감소'는 0 이하가 되어 뜻이 서지 않는다 — 추측하지 않고 크기를 세우지 않는다.
        return None
    return _reduce_fraction(numerator, 10_000)


def _reduce_fraction(numerator: int, denominator: int) -> tuple[int, int]:
    divisor = math.gcd(numerator, denominator) or 1
    return numerator // divisor, denominator // divisor


@lru_cache(maxsize=1)
def _change_bound_patterns() -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """경계 낱말('이상/이하' vs '초과/미만'). 포함 여부만 정하고 방향은 정하지 않는다."""

    def compile_alternation(name: str) -> re.Pattern[str] | None:
        alternation = lexicon_patterns.alternation(name)
        return re.compile(alternation) if alternation else None

    return compile_alternation("change_inclusive_bound"), compile_alternation("change_strict_bound")


@lru_cache(maxsize=1)
def _change_direction_pattern() -> re.Pattern[str] | None:
    increase = lexicon_patterns.alternation("trend_increase")
    decrease = lexicon_patterns.alternation("trend_decrease")
    if not increase or not decrease:
        return None
    return re.compile(f"(?P<increase>{increase})|(?P<decrease>{decrease})")


@lru_cache(maxsize=1)
def _change_wordform_pattern() -> re.Pattern[str] | None:
    """숫자 없이 크기를 말하는 낱말('절반으로'). 왼쪽 한글 경계가 필수다 — '기반으로' 오탐."""
    alternation = lexicon_patterns.alternation("change_half_word")
    if not alternation:
        return None
    return re.compile(f"{lexicon_patterns.HANGUL_LEFT_BOUNDARY}(?:{alternation})")


@lru_cache(maxsize=1)
def _change_multiple_pattern() -> re.Pattern[str] | None:
    """'배' 단위. 리터럴 스캐너는 '2배'에서 숫자 2 만 주고 단위를 버린다."""
    alternation = lexicon_patterns.alternation("change_multiple_unit")
    return re.compile(f"(?:{alternation})") if alternation else None


def _change_bound(query: str, position: int) -> tuple[bool, int] | None:
    """``position`` 바로 뒤의 경계 낱말 → (포함 여부, 끝 위치). 없으면 None."""
    inclusive_pattern, strict_pattern = _change_bound_patterns()
    tail = re.match(r"\s*", query[position:])
    cursor = position + (tail.end() if tail else 0)
    for pattern, inclusive in ((inclusive_pattern, True), (strict_pattern, False)):
        if pattern is None:
            continue
        found = pattern.match(query, cursor)
        if found is not None:
            return inclusive, found.end()
    return None


def _nearest_direction(
    query: str, span: tuple[int, int]
) -> tuple[str, tuple[int, int]] | None:
    """크기 표면어와 같은 절 안의 가장 가까운 방향 낱말. 없으면 None(크기만으로는 요구가 아니다)."""
    pattern = _change_direction_pattern()
    if pattern is None:
        return None
    clause_start, clause_end = _clause_bounds_at(query, span[0], span[1])
    best: tuple[int, str, tuple[int, int]] | None = None
    for match in pattern.finditer(query, clause_start, clause_end):
        distance = (
            match.start() - span[1] if match.start() >= span[1] else span[0] - match.end()
        )
        if distance < 0 or distance > _CHANGE_MAGNITUDE_ADJACENCY:
            continue
        direction = "increase" if match.lastgroup == "increase" else "decrease"
        if best is None or distance < best[0]:
            best = (distance, direction, match.span())
    return (best[1], best[2]) if best is not None else None


def _change_magnitude_candidates(
    query: str, owned: Sequence[tuple[int, int]]
) -> list[ChangeMagnitude]:
    """원문의 변화 크기 전부. 리터럴이 있는 형태와 낱말형을 **같은 의미축**으로 모은다.

    ``owned`` 는 다른 의무가 이미 청구한 구간이다. 같은 리터럴을 두 의미가 청구하면 한 어구가
    조건 둘이 된다 — '상위 10% 회원 중 … 증가한' 의 ``10%`` 는 랭킹의 것이지 변화 크기가
    아니다(실측 오탐). 소유 대장이 이 저장소의 중복 해석 방지 원칙이다.
    """
    from query_structurer.semantic_ir import (  # 지연 import(순환 방지)
        extract_literal_bindings,
    )

    try:
        bindings = extract_literal_bindings(query)
    except Exception:  # 리터럴 스캔 실패로 요구 기록이 죽으면 안 된다.
        bindings = []

    def owned_by_another(span: tuple[int, int]) -> bool:
        return any(start <= span[0] and span[1] <= end for start, end in owned)

    multiple_pattern = _change_multiple_pattern()
    found: list[ChangeMagnitude] = []
    claimed: list[tuple[int, int]] = []

    def claim(span: tuple[int, int]) -> bool:
        if any(span[0] < end and start < span[1] for start, end in claimed):
            return False
        claimed.append(span)
        return True

    for binding in bindings:
        kind = binding.get("kind")
        if kind not in {"percentage", "money", "number"}:
            continue
        start, end = int(binding.get("start", 0)), int(binding.get("end", 0))
        if owned_by_another((start, end)):
            continue  # 랭킹 '상위 10%' 등 다른 의미가 이미 청구한 리터럴
        normalized = binding.get("normalized")
        # '2배'처럼 숫자 뒤에 배수 단위가 붙으면 스캐너가 버린 단위를 여기서 되찾는다.
        multiple_end = None
        if multiple_pattern is not None:
            tail = multiple_pattern.match(query, end)
            multiple_end = tail.end() if tail is not None else None
        if kind == "number" and multiple_end is None:
            continue  # 맨 숫자는 크기가 아니다('상위 10명'의 10)
        span_end = multiple_end if multiple_end is not None else end
        bound = _change_bound(query, span_end)
        if bound is not None:
            span_end = bound[1]
        direction = _nearest_direction(query, (start, span_end))
        if direction is None:
            continue
        direction_name, direction_span = direction
        inclusive = bound[0] if bound is not None else True
        span = (min(start, direction_span[0]), max(span_end, direction_span[1]))

        if multiple_end is not None:
            raw = binding.get("value")
            try:
                multiple = exact_decimal(raw)
            except (InvalidOperation, TypeError, ValueError):
                continue
            if multiple is None or multiple <= 0:
                continue
            fraction = _ratio_from_decimal(multiple)
            if fraction is None or not claim(span):
                continue
            found.append(ChangeMagnitude(
                kind="ratio", direction=direction_name, inclusive=inclusive,
                numerator=fraction[0], denominator=fraction[1], amount=None,
                unit="multiple", source_text=query[span[0]:span[1]], source_span=span,
            ))
            continue

        if kind == "percentage":
            raw = normalized.get("value") if isinstance(normalized, Mapping) else None
            try:
                percent = exact_decimal(raw)
            except (InvalidOperation, TypeError, ValueError):
                continue
            if percent is None:
                continue
            fraction = _ratio_from_percent(percent, direction_name)
            if fraction is None or not claim(span):
                continue
            found.append(ChangeMagnitude(
                kind="ratio", direction=direction_name, inclusive=inclusive,
                numerator=fraction[0], denominator=fraction[1], amount=None,
                unit="percent", source_text=query[span[0]:span[1]], source_span=span,
            ))
            continue

        raw = normalized.get("amount") if isinstance(normalized, Mapping) else None
        currency = normalized.get("currency") if isinstance(normalized, Mapping) else None
        try:
            amount = exact_decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount is None or not claim(span):
            continue
        found.append(ChangeMagnitude(
            kind="absolute", direction=direction_name, inclusive=inclusive,
            numerator=None, denominator=None, amount=amount,
            unit=str(currency or "KRW"), source_text=query[span[0]:span[1]], source_span=span,
        ))

    wordform = _change_wordform_pattern()
    if wordform is not None:
        for match in wordform.finditer(query):
            if owned_by_another(match.span()):
                continue
            direction = _nearest_direction(query, match.span())
            if direction is None:
                continue
            direction_name, direction_span = direction
            span = (min(match.start(), direction_span[0]), max(match.end(), direction_span[1]))
            if not claim(span):
                continue
            found.append(ChangeMagnitude(
                kind="ratio", direction=direction_name, inclusive=True,
                numerator=1, denominator=2, amount=None, unit="fraction",
                source_text=query[span[0]:span[1]], source_span=span,
            ))

    return sorted(found, key=lambda item: item.source_span)


def _ratio_from_decimal(multiple: Decimal) -> tuple[int, int] | None:
    """배수 Decimal → 유리수. '2배'→2/1, '1.5배'→3/2."""
    try:
        scaled = (multiple * 1000).to_integral_exact()
    except (InvalidOperation, ValueError):
        return None
    if scaled != multiple * 1000:
        return None
    return _reduce_fraction(int(scaled), 1000)


def _non_magnitude_obligations(query: str) -> list[SourceRequirement]:
    """변화 크기를 뺀 나머지 의무 전부. 캡처 순서가 곧 소유권 우선순위다."""
    return [
        *_recurrence_obligations(query),
        *_entity_set_obligations(query),
        *_member_metric_ranking_obligations(query),
        *_ranked_entity_set_obligations(query),
        *_snapshot_obligations(query),
        *_member_state_history_obligations(query),
    ]


def _obligation_spans(
    requirements: Sequence[SourceRequirement],
) -> tuple[tuple[int, int], ...]:
    spans = []
    for requirement in requirements:
        bounds = _span_bounds(requirement.source_span)
        if bounds is not None:
            spans.append(bounds)
    return tuple(spans)


@lru_cache(maxsize=64)
def _claimed_obligation_spans(query: str) -> tuple[tuple[int, int], ...]:
    return _obligation_spans(_non_magnitude_obligations(query))


def parse_change_magnitudes(
    query: str, *, owned: Sequence[tuple[int, int]] | None = None
) -> tuple[ChangeMagnitude, ...]:
    """원문의 변화 크기 요구 전부(공개 API).

    :mod:`lowering_planner` 가 이 결과를 그대로 :class:`ComparisonObligation` 의 임계로 싣는다 —
    캡처와 낮춤이 **같은 파싱**을 보게 해서 "원장은 요구가 있다는데 계획은 모른다"가 생기지 않게
    한다. 파싱 실패는 전부 fail-safe 로 빈 결과다(추측 금지).

    ``owned`` 를 주지 않으면 다른 의무의 청구 구간을 직접 계산한다(캐시). 이미 계산해 둔
    호출자는 그대로 넘겨 같은 일을 두 번 하지 않는다.
    """
    if not isinstance(query, str) or not query.strip():
        return ()
    try:
        claimed = _claimed_obligation_spans(query) if owned is None else tuple(owned)
        return tuple(_change_magnitude_candidates(query, claimed))
    except Exception:  # 크기 파싱 실패로 원장 캡처 전체가 죽으면 안 된다.
        return ()


def _change_magnitude_obligations(
    query: str, owned: Sequence[tuple[int, int]]
) -> list[SourceRequirement]:
    """변화 크기 → 의무. 숫자형·낱말형을 구별하지 않고 같은 kind 로 기록한다."""
    return [
        _semantic_obligation(
            query,
            kind=CHANGE_MAGNITUDE_KIND,
            span=magnitude.source_span,
            value=magnitude.to_value(),
        )
        for magnitude in parse_change_magnitudes(query, owned=owned)
    ]


def capture_source_semantic_obligations(
    query: str,
) -> tuple[SourceRequirement, ...]:
    """Capture lossy semantic operators directly from the immutable source.

    These requirements are intentionally independent of parser output.  A
    parser may recognize a nearby number, date window, entity noun, or grade,
    but that does not discharge the recurrence/set/snapshot operator itself.
    Only an explicit compiler receipt can do so.
    """

    # 변화 크기는 **마지막**이다 — 앞선 의무가 이미 청구한 리터럴을 다시 청구하지 않도록
    # 그들의 구간을 소유 대장으로 넘긴다('상위 10%'의 10% 는 랭킹의 것이다).
    others = _non_magnitude_obligations(query)
    captured = [*others, *_change_magnitude_obligations(query, _obligation_spans(others))]
    unique: dict[str, SourceRequirement] = {}
    for requirement in captured:
        unique.setdefault(requirement.id, requirement)
    return tuple(unique.values())


def unresolved_semantic_obligations(
    plan: Mapping[str, Any],
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Return source obligations without a terminal compiler disposition.

    ``query`` is a fail-close backstop for manually constructed or legacy plans
    that do not yet carry the immutable source ledger.  It is inspected without
    mutating an already sealed snapshot.
    """

    receipts: dict[str, Mapping[str, Any]] = {}
    raw_receipts = plan.get(SOURCE_REQUIREMENT_RECEIPTS_KEY)
    if isinstance(raw_receipts, list):
        for item in raw_receipts:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("requirement_id"), str)
                and item.get("status") in _RECEIPT_TERMINAL_STATUSES
            ):
                receipts[str(item["requirement_id"])] = item

    ledger_requirements = [
        requirement
        for requirement in (plan.get(SOURCE_REQUIREMENTS_KEY) or [])
        if isinstance(requirement, Mapping)
    ]
    known_ids = {
        str(requirement.get("id") or "")
        for requirement in ledger_requirements
    }
    if isinstance(query, str) and query:
        ledger_requirements.extend(
            requirement.to_dict()
            for requirement in capture_source_semantic_obligations(query)
            if requirement.id not in known_ids
        )

    unresolved: list[dict[str, Any]] = []
    for requirement in ledger_requirements:
        if (
            not isinstance(requirement, Mapping)
            or requirement.get("type") != SEMANTIC_OBLIGATION_TYPE
        ):
            continue
        requirement_id = str(requirement.get("id") or "")
        receipt = receipts.get(requirement_id)
        if receipt is not None and receipt.get("status") == "compiled":
            continue
        kind = obligation_kind(requirement) or SEMANTIC_OBLIGATION_TYPE
        default_reason = {
            "temporal_recurrence": (
                "주기별 반복 조건을 총 기간 집계로 축소하지 않고 컴파일했다는 근거가 없습니다."
            ),
            "referenced_entity_set": (
                "외부에서 지정된 엔터티 집합의 구체 값과 전체집합 조건을 컴파일했다는 근거가 없습니다."
            ),
            "ranked_entity_set": (
                "정렬·개수 제한으로 만든 엔터티 집합과 회원 행동의 집합 관계를 컴파일했다는 근거가 없습니다."
            ),
            "snapshot_selector": (
                "회원별 최신 스냅샷 선택 조건을 컴파일했다는 근거가 없습니다."
            ),
            "member_state_history": (
                "등급·상태의 시점/이력 조건(기준월·직전·전이·유지·변경 이력)을 "
                "컴파일했다는 근거가 없습니다."
            ),
        }.get(kind, "원문의 조합 의미를 보존했다는 컴파일 근거가 없습니다.")
        unresolved.append({
            "id": requirement_id,
            "path": requirement.get("path"),
            "label": requirement.get("source_text") or kind,
            "source_text": requirement.get("source_text") or "",
            "source_span": requirement.get("source_span"),
            "reason": (
                str(receipt.get("reason"))
                if receipt is not None and receipt.get("reason")
                else default_reason
            ),
            "status": (
                str(receipt.get("status"))
                if receipt is not None
                else "unresolved"
            ),
            "source": "source_semantic_contract",
            "requirement_id": requirement_id,
            "semantic_kind": kind,
        })
    return unresolved


_ADVISORY_LITERAL_KIND_LABELS = {
    "date_window": "기간",
    "duration": "기간",
    "percentage": "백분율",
    "number_with_unit": "수량",
    "number": "숫자",
}

_DURATION_UNIT_ALIASES = {
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
    "year": "years",
    "years": "years",
    "일": "days",
    "주": "weeks",
    "주일": "weeks",
    "개월": "months",
    "달": "months",
    "년": "years",
}
_DURATION_DAY_FIELDS = frozenset({"window_days", "min_days", "max_days"})


def _positive_duration_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _duration_unit(value: Any) -> str | None:
    return _DURATION_UNIT_ALIASES.get(str(value).strip().casefold()) if value is not None else None


def _resolved_duration(
    value: int,
    unit: str,
    reference_date: date,
) -> tuple[str, str, int]:
    period = RelativeWindow(value=value, unit=unit).resolve(reference_date)
    start = date(int(period.start[:4]), int(period.start[4:6]), int(period.start[6:8]))
    end = date(int(period.end[:4]), int(period.end[4:6]), int(period.end[6:8]))
    return period.start, period.end, (end - start).days + 1


def _duration_literal_parts(
    item: Mapping[str, Any],
    normalized: Any,
    query: str | None,
) -> tuple[int, str] | None:
    kind = item.get("kind")
    if kind == "duration" and isinstance(normalized, Mapping):
        if normalized.get("temporal_kind") not in (None, "rolling_duration"):
            return None
        value = _positive_duration_value(normalized.get("value"))
        unit = _duration_unit(normalized.get("semantic_unit") or normalized.get("unit"))
        return (value, unit) if value is not None and unit is not None else None
    # 이전 scanner의 맨 숫자 리터럴 호환. 단위는 원문 인접 표면에서만 읽고, 월/년을 고정
    # 일수로 바꾸지 않는다.
    if (
        kind == "number"
        and query
        and isinstance(item.get("end"), int)
        and 0 <= item["end"] <= len(query)
    ):
        value = _positive_duration_value(normalized)
        tail = query[item["end"]:item["end"] + 2]
        unit = next(
            (_duration_unit(surface) for surface in ("개월", "달", "주", "년", "일") if tail.startswith(surface)),
            None,
        )
        return (value, unit) if value is not None and unit is not None else None
    return None


def _duration_is_consumed(
    value: int,
    unit: str,
    *,
    reference_date: date | None,
    relative_windows: set[tuple[int, str, str, str]],
    absolute_windows: set[tuple[str, str]],
    duration_days: set[int],
) -> bool:
    resolved = (
        _resolved_duration(value, unit, reference_date)
        if isinstance(reference_date, date)
        else None
    )
    matching_relative = [
        (start, end)
        for candidate_value, candidate_unit, start, end in relative_windows
        if candidate_value == value and candidate_unit == unit
    ]
    if matching_relative:
        # value/unit/from/to 를 모두 보존해도 월/년 경계는 주입 기준일로 다시 계산해 일치해야 한다.
        # 일/주는 고정 길이라 기준일 없이도 단위·구간을 구조적으로 대응할 수 있다.
        if resolved is not None and (resolved[0], resolved[1]) in matching_relative:
            return True
        if resolved is None and unit in {"days", "weeks"}:
            return True
    if resolved is not None:
        if (resolved[0], resolved[1]) in absolute_windows:
            return True
        return resolved[2] in duration_days
    # 일/주는 기준일과 무관한 고정 길이다. 월/년은 기준일 없이는 일수로 비교하지 않는다.
    fixed_days = value if unit == "days" else value * 7 if unit == "weeks" else None
    return fixed_days in duration_days if fixed_days is not None else False


def unconsumed_literal_advisories(
    plan: dict[str, Any],
    query: str | None = None,
    *,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    """원문에서 결정론 추출된 리터럴 중 어떤 실행 의미에도 소비되지 않은 것의 비차단 진단.

    배경: '최근 3개월 주문은 있었지만'의 '3', '상위 10%'의 '10%'가 literal_bindings 에 바인딩만
    되고 어느 슬롯도 참조하지 않은 채 플랜이 흘러가는 조용한 드롭이 반복됐다(2026-08-01 실사고).
    결정론 드롭 감지기(_deterministic_dropped_conditions)는 숫자/기간 family 를 오탐 우려로 명시
    제외하므로, 이 감사가 그 사각을 **비차단 자문**으로 채운다. 차단이 아니다 — 여기 항목이
    있어도 SQL 은 나간다. 차단으로 승격하려면 graph_rag 의 semantic invariants 인자에 넘기는
    한 곳만 바꾸면 되도록 게이트 지점을 하나로 모아 둔다.

    소비 판정 3축(하나라도 성립하면 소비됨):
      1. semantic_ir.operations[].bindings[].literal_id 명시 참조
      2. 실행 슬롯 값과의 구조 동치(날짜창 from/to, 숫자, 달력 duration 일치)
      3. 정밀 스팬(span_precision != whole_query)을 가진 source requirement 가 리터럴 스팬을 포함
    comparison_operator 리터럴은 항상 제외한다 — 연산자는 값 리터럴에 붙는 부속이라 단독 소비
    개념이 없고, 전부 잡으면 잡음만 는다.
    """
    literals = plan.get("literal_bindings")
    if not isinstance(literals, list) or not literals:
        return []

    consumed_ids: set[str] = set()
    semantic_ir = plan.get("semantic_ir")
    if isinstance(semantic_ir, Mapping):
        for operation in semantic_ir.get("operations") or []:
            if not isinstance(operation, Mapping):
                continue
            for binding in operation.get("bindings") or []:
                if isinstance(binding, Mapping) and isinstance(binding.get("literal_id"), str):
                    consumed_ids.add(binding["literal_id"])

    numbers: set[Decimal] = set()
    windows: set[tuple[str, str]] = set()
    relative_windows: set[tuple[int, str, str, str]] = set()
    duration_days: set[int] = set()

    def collect(node: Any) -> None:
        if node is None or isinstance(node, bool) or isinstance(node, str):
            return
        if isinstance(node, (int, float, Decimal)):
            exact = exact_decimal(node)
            if exact is not None:
                numbers.add(exact)
            return
        if isinstance(node, Mapping):
            frm, to = node.get("from"), node.get("to")
            if isinstance(frm, str) and isinstance(to, str):
                windows.add((frm, to))
                duration_value = _positive_duration_value(node.get("value"))
                duration_unit = _duration_unit(node.get("unit"))
                if duration_value is not None and duration_unit is not None:
                    relative_windows.add((duration_value, duration_unit, frm, to))
            for key, child in node.items():
                if key in _DURATION_DAY_FIELDS:
                    duration_day = _positive_duration_value(child)
                    if duration_day is not None:
                        duration_days.add(duration_day)
                if key in {"threshold", "count", "amount", "value", "percent", "top_n"}:
                    exact = exact_decimal(child, allow_string=True)
                    if exact is not None:
                        numbers.add(exact)
                collect(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                collect(child)

    for container in ("target_user", "exclude", "campaign_constraints"):
        collect(plan.get(container))
    for slot in _PLAN_REQUIREMENT_SLOTS:
        collect(plan.get(slot))

    precise_spans: list[tuple[int, int]] = []
    for requirement in plan.get(SOURCE_REQUIREMENTS_KEY) or []:
        if not isinstance(requirement, Mapping):
            continue
        if requirement.get("span_precision") in (None, "whole_query"):
            continue
        span = requirement.get("source_span")
        if isinstance(span, Mapping) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int):
            precise_spans.append((span["start"], span["end"]))

    advisories: list[dict[str, Any]] = []
    for index, item in enumerate(literals):
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        if kind == "comparison_operator":
            continue
        literal_id = item.get("id")
        if isinstance(literal_id, str) and literal_id in consumed_ids:
            continue
        normalized = item.get("normalized")
        if kind == "date_window" and isinstance(normalized, Mapping):
            if (normalized.get("from"), normalized.get("to")) in windows:
                continue
        else:
            norm_value = normalized.get("value") if isinstance(normalized, Mapping) else normalized
            duration_parts = _duration_literal_parts(item, normalized, query)
            if duration_parts is not None and _duration_is_consumed(
                *duration_parts,
                reference_date=reference_date,
                relative_windows=relative_windows,
                absolute_windows=windows,
                duration_days=duration_days,
            ):
                continue
            if duration_parts is not None:
                norm_value = None  # duration의 숫자 부분을 일반 숫자 동치로 소비하지 않는다.
            normalized_decimal = exact_decimal(norm_value, allow_string=True)
            if normalized_decimal is not None and normalized_decimal in numbers:
                continue
        start, end = item.get("start"), item.get("end")
        spanned = isinstance(start, int) and isinstance(end, int)
        if spanned and any(s <= start and end <= e for s, e in precise_spans):
            continue
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        kind_label = _ADVISORY_LITERAL_KIND_LABELS.get(str(kind), str(kind))
        advisories.append({
            "id": f"lit_{literal_id or index}",
            "path": f"literal_bindings[{index}]",
            "literal_id": literal_id,
            "kind": kind,
            "label": f"{kind_label} '{text}'",
            "source_text": text,
            "source_span": {"start": start, "end": end} if spanned else None,
            "normalized": json.loads(json.dumps(normalized, ensure_ascii=False, default=str)),
            "reason": "원문에서 추출된 리터럴이 어떤 실행 조건에도 소비되지 않았습니다(조용한 드롭 후보).",
            "status": "advisory",
            "source": "literal_binding_audit",
        })
    return advisories


def _requirements_payload(requirements: Any) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for requirement in requirements or ():
        if isinstance(requirement, SourceRequirement):
            payload.append(requirement.to_dict())
        elif isinstance(requirement, dict):
            payload.append(json.loads(json.dumps(requirement, ensure_ascii=False, default=str)))
    return payload


def source_requirements_digest(requirements: Any) -> str:
    canonical = json.dumps(
        _requirements_payload(requirements), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_source_requirements(plan: dict[str, Any], *groups: Any) -> None:
    """불변 요구 스냅샷을 plan에 한 번 부착하고 무결성 해시를 봉인한다."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in _requirements_payload(group):
            requirement_id = str(item.get("id") or "")
            if requirement_id and requirement_id not in seen:
                seen.add(requirement_id)
                merged.append(item)
    plan[SOURCE_REQUIREMENTS_KEY] = merged
    plan[SOURCE_REQUIREMENTS_DIGEST_KEY] = source_requirements_digest(merged)


def verify_source_requirements(plan: dict[str, Any]) -> bool:
    """부착 이후 변경 여부를 검사한다. 스냅샷이 없는 수동 plan은 기존 호환을 위해 통과한다."""
    expected = plan.get(SOURCE_REQUIREMENTS_DIGEST_KEY)
    if expected is None:
        return True
    actual = source_requirements_digest(plan.get(SOURCE_REQUIREMENTS_KEY))
    if not isinstance(expected, str) or actual != expected:
        raise SourceRequirementIntegrityError("source requirements changed after initial capture")
    return True


def record_source_requirement_receipt(
    plan: dict[str, Any],
    *,
    requirement_id: str,
    status: str,
    compiler: str,
    evidence: Any = None,
    reason: str | None = None,
) -> None:
    """Record a compiler-owned terminal disposition outside the sealed ledger."""

    if status not in _RECEIPT_TERMINAL_STATUSES:
        raise ValueError(f"non-terminal source requirement status: {status!r}")
    known_ids = {
        str(item.get("id") or "")
        for item in plan.get(SOURCE_REQUIREMENTS_KEY) or []
        if isinstance(item, Mapping)
    }
    if requirement_id not in known_ids:
        raise ValueError(f"unknown source requirement id: {requirement_id!r}")
    receipt = {
        "requirement_id": requirement_id,
        "status": status,
        "compiler": compiler,
        "evidence": _thaw_json(_freeze_json(evidence)),
        "reason": reason,
    }
    receipts = plan.setdefault(SOURCE_REQUIREMENT_RECEIPTS_KEY, [])
    if not isinstance(receipts, list):
        raise ValueError(f"{SOURCE_REQUIREMENT_RECEIPTS_KEY} must be a list")
    receipts[:] = [
        item
        for item in receipts
        if not (
            isinstance(item, Mapping)
            and item.get("requirement_id") == requirement_id
        )
    ]
    receipts.append(receipt)


def discharge_source_semantic_obligations(
    plan: dict[str, Any],
    query: str,
    *,
    kinds: frozenset[str] | set[str],
    status: str,
    compiler: str,
    evidence: Any = None,
    reason: str | None = None,
    value_filter: Callable[[str, Any], bool] | None = None,
    requirement_filter: Callable[[Any], bool] | None = None,
) -> list[str]:
    """컴파일 산출물이 실재할 때 원문 의무를 영수증으로 귀결한다(첫 컴파일러 경로, W5-4).

    봉인된 원장은 절대 변형하지 않는다 — 영수증은 원장 밖 리스트에 쌓인다
    (record_source_requirement_receipt). 원장에 없는 의무(수동/레거시 plan)는 조용히
    건너뛴다: 없는 원장에 영수증을 강제로 만드는 것보다 fail-close 로 남는 쪽이 안전하다.
    """
    known_ids = {
        str(item.get("id") or "")
        for item in plan.get(SOURCE_REQUIREMENTS_KEY) or []
        if isinstance(item, Mapping)
    }
    discharged: list[str] = []
    for requirement in capture_source_semantic_obligations(query):
        kind = str(requirement.base.get("name") or "")
        if kind not in kinds:
            continue
        if value_filter is not None and not value_filter(kind, requirement.value):
            continue
        if requirement_filter is not None and not requirement_filter(requirement):
            continue
        if requirement.id not in known_ids:
            continue
        record_source_requirement_receipt(
            plan,
            requirement_id=requirement.id,
            status=status,
            compiler=compiler,
            evidence=evidence,
            reason=reason,
        )
        discharged.append(requirement.id)
    return discharged


# ── 회계: 각 requirement 를 parsed/compiled/clarification/unsupported 로 귀결 ────────────────
@dataclass
class RequirementAccounting:
    requirements: list[SourceRequirement]

    def unresolved(self) -> list[SourceRequirement]:
        """검증기 계약: terminal 상태로 귀결되지 못한(= 조용히 사라진) requirement."""
        return [r for r in self.requirements if r.status not in TERMINAL_STATUSES]

    def blocking(self) -> list[SourceRequirement]:
        """출고를 막아야 하는 귀결(unsupported/clarification) + 미귀결(detected)."""
        return [r for r in self.requirements if r.status in BLOCKING_STATUSES or r.status not in TERMINAL_STATUSES]

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.requirements]


def _evidence_marks_compiled(qualifier: "Qualifier", applied_requirements: list[dict[str, Any]] | None) -> bool:
    """빌더가 반환한 구조화 evidence(applied_requirements)가 이 qualifier 를 반영했다고 증명하는가.

    SQL 문자열 검색이 아니라 evidence 로 확인한다 — dimension 코드 치환(브랜드명→BRAND_ID 코드 'A')이나
    canonical 보정으로 원문 값이 SQL 문자열에 그대로 없더라도 정상 컴파일을 누락으로 오탐하지 않기 위함.
    domain 이 같고 (값이 evidence values 에 있거나, 코드로 치환돼 값 비교가 불가능한 경우)면 반영으로 본다."""
    if not applied_requirements:
        return False
    target = _normalize_value(qualifier.raw_value)
    for ev in applied_requirements:
        if not isinstance(ev, dict) or ev.get("qualifier") != qualifier.domain:
            continue
        values = [_normalize_value(str(v)) for v in (ev.get("values") or [])]
        source_values = [_normalize_value(str(v)) for v in (ev.get("source_values") or []) if v]
        if target in values or target in source_values:
            return True
        # 코드 치환 경로: 원문 값이 코드로 바뀌어 값 비교가 불가능하다 — 빌더가 이 domain 을 적용했다는
        # evidence 자체를 신뢰한다(source_values 가 없을 때의 하이브리드 코드 경로).
        if ev.get("resolved_via") == "code":
            return True
    return False


def account_requirements(
    query: str,
    base_name: str,
    base_type: str,
    sql: str | None,
    registry: RequirementRegistry,
    applied_requirements: list[dict[str, Any]] | None = None,
) -> RequirementAccounting:
    """원문의 qualifier requirement 를 추출하고, base×qualifier capability + 반영 evidence 로 귀결시킨다.

      * capability.supported=false → status=unsupported(+message). 조용한 드롭 대신 명시 안내.
      * supported=true 이고 (구조화 evidence 또는 SQL 리터럴)로 반영 확인되면 → compiled(정상 반영).
      * supported=true 인데 반영 근거가 없으면 → clarification(원문엔 있으나 컴파일 안 됨 = 사일런트 드롭).

    반영 확인은 **구조화 evidence 우선, SQL 문자열 검색은 폴백**이다(요청 5) — bind/코드 치환·canonical
    보정에서 문자열 검색이 정상 컴파일을 누락으로 오탐하는 것을 막는다. 브랜드 인식 성공 여부가 아니라
    '모든 requirement 가 귀결됐는지'만 본다."""
    base = {"type": base_type, "name": base_name}
    requirements = extract_entity_qualifier_requirements(query, base)
    normalized_sql = _normalize_value(sql or "")
    resolved_requirements: list[SourceRequirement] = []
    for req in requirements:
        resolved = req
        for qualifier in req.qualifiers:
            cap = registry.qualifier_capability(base_name, qualifier.domain)
            if cap is not None and not cap.get("supported", False):
                resolved = replace(
                    req,
                    status="unsupported",
                    message=str(
                        cap.get("message")
                        or f"현재 {registry.base_label(base_name)}에는 이 조건을 함께 적용할 수 없습니다."
                    ),
                )
                break
            # 지원됨: 반영 확인 — ① 구조화 evidence 우선, ② SQL 리터럴 부분문자열(canonical 보정 대비 정규화) 폴백.
            if _evidence_marks_compiled(qualifier, applied_requirements) or _normalize_value(qualifier.raw_value) in normalized_sql:
                resolved_qualifiers = tuple(
                    replace(item, resolved_value=item.raw_value) if item is qualifier else item
                    for item in req.qualifiers
                )
                resolved = replace(req, status="compiled", qualifiers=resolved_qualifiers)
            else:
                resolved = replace(
                    req,
                    status="clarification",
                    message=(
                        f"'{qualifier.raw_value}' 조건이 생성 SQL 에 반영되지 않았습니다 — 원문에는 있으나 "
                        f"실DB 타겟 추출로 컴파일되지 못한 것으로 보입니다. 의도가 맞는지 확인해 주세요."
                    ),
                )
        resolved_requirements.append(resolved)
    return RequirementAccounting(requirements=resolved_requirements)
