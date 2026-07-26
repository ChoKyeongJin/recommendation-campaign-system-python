"""세그먼트(쿠폰 도메인) 의미 파서 — JSON 스펙 기반 지표/연산자/capability 게이트.

배경/목표
---------
쿠폰 관련 자연어 조건("쿠폰 5회 이상 사용", "쿠폰 한 개당 구매금액 5만원 이상", "쿠폰 수보다 구매건수가
많은" …)을 지금까지는 graph_rag.py 안의 문장별 정규식과 어순 의존 게이트로 처리했다. 그 결과 같은 의미가
어순에 따라 차단되거나 통과하고(임계값이 조용히 USE_CPN_CNT>0 으로 축소), 파생 지표의 분모가 사라지고,
지표 간 비교가 순위로 오해되는 결함이 있었다.

이 모듈은 그 처리를 다음 단계로 분리한다(정규식은 '후보 추출'에만, 판정은 정규화된 의미 노드 기준):

    원문 → 후보 추출 → 지표 정규화 → 연산자 정규화 → 값/범위 정규화 → 지표 관계 정규화
         → 의미 노드(IR) 완성 → capability 게이트 → 지원/미지원 판정

핵심 원칙(구조):
  * 지표 이름·별칭·단위·지원 연산자·지원 여부·파생 정의·미지원 코드/문구는 코드가 아니라 JSON
    (docs/data/segment_metrics.json, docs/data/segment_operators.json)에 둔다.
  * 의미 노드를 먼저 '완성'한다 — 지원 여부와 무관하게 metric/operator/value/range/negation/비교대상/
    파생식을 보존한다. 지원 여부는 그 다음 capability 게이트에서만 판정한다.
  * 미지원이라고 해서 의미 노드의 일부(임계값·분모·비교대상)를 삭제하지 않는다.

graph_rag.py 는 _apply_coupon_semantics(→ interpret())로 이 모듈을 호출하고, 반환된 판정에 따라
campaign_responses(존재 여부) 를 붙이거나 plan['unsupported'] 를 남긴다. 이 모듈은 graph_rag 를 import
하지 않는다(순환 방지) — 순수 dict/dataclass in/out.

실행: python -m pytest tests/test_coupon_semantics.py -q
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_METRICS_PATH = Path("docs/data/segment_metrics.json")
DEFAULT_OPERATORS_PATH = Path("docs/data/segment_operators.json")

# capability 가 선언할 수 있는 연산(operation). condition.type → operation 매핑의 치역.
OPERATIONS = frozenset({"filter", "existence_filter", "ranking", "metric_comparison"})
# 비교 연산자 정규 토큰(IR operator). JSON operators 의 키가 이 집합이어야 한다.
OPERATOR_IDS = frozenset({"eq", "gt", "gte", "lt", "lte", "between"})
# 존재 여부 capability 의 연산자(임계 비교와 구분 — 0 초과/0 등호만).
EXISTENCE_OPERATOR_IDS = frozenset({"gt_zero", "eq_zero"})

_NUMBER_RE = re.compile(r"\d[\d,]*")
_AMOUNT_RE = re.compile(r"(\d[\d,]*)\s*(억|만|천)?\s*원")
_AMOUNT_MULTIPLIER = {"억": 100_000_000, "만": 10_000, "천": 1_000}


class SegmentSemanticsError(ValueError):
    """세그먼트 스펙(JSON)이 스키마를 위반했을 때. 어느 지표/필드가 왜 틀렸는지 메시지에 담아, 설정 오류가
    런타임에서 조용히 무시되지 않게 한다(로드 시 즉시 실패)."""


# ── 로드된 스펙(불변) ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Capability:
    operation: str
    supported: bool
    supported_operators: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnsupportedMessage:
    code: str
    message: str
    clarification: str = ""


@dataclass(frozen=True)
class Formula:
    type: str
    numerator: str
    denominator: str
    zero_denominator_policy: str = "exclude"


@dataclass(frozen=True)
class SegmentMetric:
    metric_id: str
    domain: str
    value_type: str
    aliases: tuple[str, ...]
    units: tuple[str, ...]
    usage_verbs: tuple[str, ...] = ()
    negative_expressions: tuple[str, ...] = ()
    source: dict[str, str] | None = None
    formula: Formula | None = None
    capabilities: dict[str, Capability] = field(default_factory=dict)
    unsupported_messages: dict[str, UnsupportedMessage] = field(default_factory=dict)

    def capability(self, operation: str) -> Capability | None:
        return self.capabilities.get(operation)

    def message(self, operation: str) -> UnsupportedMessage | None:
        return self.unsupported_messages.get(operation)


@dataclass(frozen=True)
class OperatorGrammar:
    # operator_id → 표면 표현(공백 제거) 튜플
    operators: dict[str, tuple[str, ...]]
    between_markers: tuple[str, ...]
    ranking_markers: tuple[str, ...]
    comparison_markers: tuple[str, ...]
    comparison_more: tuple[str, ...]
    comparison_less: tuple[str, ...]
    derived_per: tuple[str, ...]


# ── 의미 노드(IR) ─────────────────────────────────────────────────────────────────────────
@dataclass
class SegmentCondition:
    """정규화된 의미 노드. type 에 따라 채워지는 필드가 다르다. 지원 여부와 무관하게 원문 의미를 보존한다."""

    type: str  # metric_filter | existence_filter | ranking | metric_comparison | derived_metric_filter
    metric: str  # capability 조회 기준 지표(비교/파생도 게이트 대상 지표를 여기에)
    operator: str | None = None
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = None
    exists: bool | None = None
    direction: str | None = None  # ranking: asc|desc
    limit: int | None = None
    left_metric: str | None = None
    right_metric: str | None = None
    formula: dict[str, Any] | None = None
    source_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != ""}


@dataclass(frozen=True)
class CapabilityResult:
    supported: bool
    operation: str
    code: str | None = None
    message: str | None = None
    clarification: str | None = None


@dataclass
class Interpretation:
    """interpret() 결과: 완성된 의미 노드 + capability 판정 + (존재 여부일 때) EXISTS 술어."""

    condition: SegmentCondition
    capability: CapabilityResult
    existence_predicate: str | None = None


# ── 로더/검증 ─────────────────────────────────────────────────────────────────────────────
def _require(cond: bool, where: str, message: str) -> None:
    if not cond:
        raise SegmentSemanticsError(f"[{where}] {message}")


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(s for s in value if isinstance(s, str) and s.strip())


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _compact_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    # 표면 표현은 공백 제거 형태로 비교한다(질의 공백 편차 흡수). 길이순(긴 것 우선)으로 정렬해
    # 부분문자열 오탐(예: '구매금액' 이 '구매' 로 먼저 잡히는 것)을 줄인다.
    seen: list[str] = []
    for v in values:
        c = _compact(v)
        if c and c not in seen:
            seen.append(c)
    return tuple(sorted(seen, key=len, reverse=True))


@dataclass
class SegmentSemanticsRegistry:
    metrics: dict[str, SegmentMetric]
    operators: OperatorGrammar

    @classmethod
    def load(
        cls,
        metrics_path: Path | str = DEFAULT_METRICS_PATH,
        operators_path: Path | str = DEFAULT_OPERATORS_PATH,
    ) -> "SegmentSemanticsRegistry":
        metrics = _load_metrics(Path(metrics_path))
        operators = _load_operators(Path(operators_path))
        _validate_cross(metrics, operators)
        return cls(metrics=metrics, operators=operators)

    def metric_for_alias(self, compact_query: str, *, domains: set[str] | None = None) -> tuple[str, str] | None:
        """질의(공백 제거)에 등장하는 (metric_id, 매칭 별칭). 긴 별칭 우선. domains 로 도메인 제한."""
        best: tuple[str, str] | None = None
        best_len = 0
        for metric in self.metrics.values():
            if domains is not None and metric.domain not in domains:
                continue
            for alias in _compact_tuple(metric.aliases):
                if alias in compact_query and len(alias) > best_len:
                    best = (metric.metric_id, alias)
                    best_len = len(alias)
        return best


def _load_metrics(path: Path) -> dict[str, SegmentMetric]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentSemanticsError(f"[{path.name}] 읽기/파싱 실패: {exc}") from exc
    _require(isinstance(payload, dict), path.name, "최상위는 객체여야 함")
    raw_metrics = payload.get("metrics")
    _require(isinstance(raw_metrics, dict) and bool(raw_metrics), path.name, "'metrics' 객체 필요(1개 이상)")

    metrics: dict[str, SegmentMetric] = {}
    for metric_id, data in raw_metrics.items():
        _require(isinstance(data, dict), metric_id, "지표 정의는 객체여야 함")
        _require(isinstance(data.get("domain"), str) and data["domain"].strip(), metric_id, "domain 필수")
        _require(isinstance(data.get("value_type"), str) and data["value_type"].strip(), metric_id, "value_type 필수")
        aliases = _str_tuple(data.get("aliases"))
        _require(bool(aliases), metric_id, "aliases 최소 1개 필요")
        units = _str_tuple(data.get("units"))

        # capability 파싱 + 검증
        capabilities: dict[str, Capability] = {}
        raw_caps = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
        for op, cap_raw in raw_caps.items():
            _require(op in OPERATIONS, metric_id, f"capability 연산 미지원: {op!r}(허용 {sorted(OPERATIONS)})")
            _require(isinstance(cap_raw, dict), metric_id, f"capability[{op}] 는 객체여야 함")
            _require(isinstance(cap_raw.get("supported"), bool), metric_id, f"capability[{op}].supported(bool) 필수")
            ops_allowed = _str_tuple(cap_raw.get("supported_operators"))
            valid_ids = OPERATOR_IDS | EXISTENCE_OPERATOR_IDS
            bad = [o for o in ops_allowed if o not in valid_ids]
            _require(not bad, metric_id, f"capability[{op}].supported_operators 미지원 토큰: {bad}")
            capabilities[op] = Capability(operation=op, supported=cap_raw["supported"], supported_operators=ops_allowed)

        # 미지원 안내 메시지 파싱
        messages: dict[str, UnsupportedMessage] = {}
        raw_msgs = data.get("unsupported_messages") if isinstance(data.get("unsupported_messages"), dict) else {}
        for op, msg_raw in raw_msgs.items():
            _require(op in OPERATIONS, metric_id, f"unsupported_messages 연산 미지원: {op!r}")
            _require(isinstance(msg_raw, dict), metric_id, f"unsupported_messages[{op}] 는 객체여야 함")
            code = msg_raw.get("code")
            message = msg_raw.get("message")
            _require(isinstance(code, str) and code.strip(), metric_id, f"unsupported_messages[{op}].code 필수")
            _require(isinstance(message, str) and message.strip(), metric_id, f"unsupported_messages[{op}].message 필수")
            messages[op] = UnsupportedMessage(
                code=code.strip(), message=message.strip(),
                clarification=(msg_raw.get("clarification") or "").strip(),
            )

        # 미지원(supported=false)인 게이트 연산은 안내 메시지가 반드시 있어야 한다(조용한 차단 방지).
        for op in ("filter", "ranking", "metric_comparison"):
            cap = capabilities.get(op)
            if cap is not None and not cap.supported:
                _require(op in messages, metric_id,
                         f"capability[{op}] 가 미지원인데 unsupported_messages[{op}] 안내가 없음")

        formula = None
        if isinstance(data.get("formula"), dict):
            f = data["formula"]
            for key in ("type", "numerator", "denominator"):
                _require(isinstance(f.get(key), str) and f[key].strip(), metric_id, f"formula.{key} 필수")
            formula = Formula(
                type=f["type"].strip(), numerator=f["numerator"].strip(),
                denominator=f["denominator"].strip(),
                zero_denominator_policy=(f.get("zero_denominator_policy") or "exclude").strip(),
            )

        source = None
        if isinstance(data.get("source"), dict):
            s = data["source"]
            for key in ("table", "alias", "column"):
                _require(isinstance(s.get(key), str) and s[key].strip(), metric_id, f"source.{key} 필수")
            source = {"table": s["table"].strip(), "alias": s["alias"].strip(), "column": s["column"].strip()}

        metrics[metric_id] = SegmentMetric(
            metric_id=metric_id,
            domain=data["domain"].strip(),
            value_type=data["value_type"].strip(),
            aliases=aliases,
            units=units,
            usage_verbs=_str_tuple(data.get("usage_verbs")),
            negative_expressions=_str_tuple(data.get("negative_expressions")),
            source=source,
            formula=formula,
            capabilities=capabilities,
            unsupported_messages=messages,
        )
    return metrics


def _load_operators(path: Path) -> OperatorGrammar:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentSemanticsError(f"[{path.name}] 읽기/파싱 실패: {exc}") from exc
    _require(isinstance(payload, dict), path.name, "최상위는 객체여야 함")
    raw_ops = payload.get("operators") if isinstance(payload.get("operators"), dict) else {}
    _require(bool(raw_ops), path.name, "'operators' 객체 필요")

    operators: dict[str, tuple[str, ...]] = {}
    between_markers: tuple[str, ...] = ()
    for op_id, spec in raw_ops.items():
        _require(op_id in OPERATOR_IDS, path.name, f"operator id 미지원: {op_id!r}(허용 {sorted(OPERATOR_IDS)})")
        _require(isinstance(spec, dict), path.name, f"operators[{op_id}] 는 객체여야 함")
        exprs = _compact_tuple(_str_tuple(spec.get("expressions")))
        _require(bool(exprs), path.name, f"operators[{op_id}].expressions 최소 1개 필요")
        operators[op_id] = exprs
        if op_id == "between":
            between_markers = exprs

    markers = payload.get("markers") if isinstance(payload.get("markers"), dict) else {}

    def m(key: str) -> tuple[str, ...]:
        return _compact_tuple(_str_tuple(markers.get(key)))

    return OperatorGrammar(
        operators=operators,
        between_markers=between_markers,
        ranking_markers=m("ranking"),
        comparison_markers=m("comparison"),
        comparison_more=m("comparison_more"),
        comparison_less=m("comparison_less"),
        derived_per=m("derived_per"),
    )


def _validate_cross(metrics: dict[str, SegmentMetric], operators: OperatorGrammar) -> None:
    # 파생 지표의 formula operand 가 실재 지표를 가리키는지(드리프트 방지).
    for metric in metrics.values():
        if metric.formula is not None:
            for role in ("numerator", "denominator"):
                ref = getattr(metric.formula, role)
                _require(ref in metrics, metric.metric_id, f"formula.{role} 가 미등록 지표: {ref!r}")
        # 지원 filter capability 의 연산자는 임계 연산자여야 한다(존재 연산자 gt_zero 등 혼입 방지).
        cap = metric.capability("filter")
        if cap is not None and cap.supported:
            bad = [o for o in cap.supported_operators if o not in OPERATOR_IDS]
            _require(not bad, metric.metric_id, f"filter.supported_operators 에 임계 연산자 아님: {bad}")
    # 별칭 충돌(같은 표면 별칭이 두 지표를 가리키면 정규화가 모호해진다).
    seen: dict[str, str] = {}
    for metric in metrics.values():
        for alias in _compact_tuple(metric.aliases):
            if alias in seen and seen[alias] != metric.metric_id:
                raise SegmentSemanticsError(
                    f"[{metric.metric_id}] 별칭 충돌: {alias!r} 가 {seen[alias]} 와 중복"
                )
            seen[alias] = metric.metric_id


# ── 후보 추출 헬퍼 ────────────────────────────────────────────────────────────────────────
def _first(compact: str, needles: tuple[str, ...]) -> int:
    """needles 중 하나라도 compact 에 있으면 가장 이른 위치, 없으면 -1."""
    best = -1
    for n in needles:
        i = compact.find(n)
        if i != -1 and (best == -1 or i < best):
            best = i
    return best


def _has_any(compact: str, needles: tuple[str, ...]) -> bool:
    return any(n in compact for n in needles)


def _numbers_with_unit(compact: str, units: tuple[str, ...]) -> list[tuple[int, int, int]]:
    """(값, 시작, 끝) 목록 — 숫자 바로 뒤에 지표 단위가 붙은 것만(예: '5회','3개'). 명(名) 등 비단위 제외."""
    if not units:
        return []
    unit_alt = "|".join(re.escape(u) for u in units)
    pattern = re.compile(r"(\d[\d,]*)\s*(?:" + unit_alt + r")")
    out: list[tuple[int, int, int]] = []
    for m in pattern.finditer(compact):
        out.append((int(m.group(1).replace(",", "")), m.start(), m.end()))
    return out


def _detect_operator(after: str, grammar: OperatorGrammar) -> str | None:
    """숫자+단위 뒤 텍스트에서 가장 이른 연산자 표현을 찾아 operator id 로. between 은 별도 처리."""
    best_id: str | None = None
    best_pos = -1
    for op_id, exprs in grammar.operators.items():
        if op_id == "between":
            continue
        pos = _first(after, exprs)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos
            best_id = op_id
    return best_id


def _parse_amount(compact: str) -> int | None:
    """'5만원'→50000, '50,000원'→50000. 원 표기 우선, 없으면 첫 숫자."""
    m = _AMOUNT_RE.search(compact)
    if m:
        base = int(m.group(1).replace(",", ""))
        mult = _AMOUNT_MULTIPLIER.get(m.group(2) or "", 1)
        return base * mult
    m2 = _NUMBER_RE.search(compact)
    return int(m2.group(0).replace(",", "")) if m2 else None


# 건수 명사 접미(횟수/건수/개수/회수)로 끝나는 쿠폰 별칭은 '사용 건수' 의미로 본다 — 사용 동사가 없어도
# ('쿠폰 소진 횟수' 처럼 JSON 에 별칭만 추가해도) 인식된다. 짧은 '수' 단독은 오탐('쿠폰수령') 위험이라 제외.
_COUNT_NOUN_SUFFIXES = ("횟수", "회수", "건수", "개수")


def _has_usage_sense(compact: str, coupon: SegmentMetric) -> bool:
    """쿠폰 '사용 건수' 의미가 있는가 — 사용/이용 등 동사, 또는 건수 명사로 끝나는 쿠폰 별칭. '보유'(소지)는 제외.

    별칭 기반이라 JSON 에 새 별칭('쿠폰 소진 횟수')을 추가하면 파서 코드 수정 없이 인식된다."""
    if "쿠폰" not in compact:
        return False
    if _has_any(compact, coupon.usage_verbs):
        return True
    return any(
        alias in compact and alias.endswith(_COUNT_NOUN_SUFFIXES)
        for alias in _compact_tuple(coupon.aliases)
    )


# ── 해석(extract → normalize → gate) ──────────────────────────────────────────────────────
def interpret(query: str, registry: SegmentSemanticsRegistry) -> Interpretation | None:
    """쿠폰 도메인 조건을 하나의 의미 노드로 정규화하고 capability 게이트를 적용한다.

    쿠폰 관련 의도가 없으면 None(호출 측은 기존 경로 유지). 반환 시 condition 은 지원 여부와 무관하게
    원문 의미를 보존하고, capability.supported 로 지원/미지원을 알린다."""
    compact = _compact(query)
    if "쿠폰" not in compact:
        return None

    coupon = registry.metrics.get("coupon_usage_count")
    derived = registry.metrics.get("purchase_amount_per_coupon")
    grammar = registry.operators
    if coupon is None:
        return None

    # 1) 파생 지표('쿠폰 한 개당 구매금액') — 분모/비율 의미 보존.
    derived_interp = _try_derived(compact, query, derived, grammar, registry)
    if derived_interp is not None:
        return derived_interp

    # 2) 지표 간 비교('쿠폰 수보다 구매건수가 많은') — 순위로 오해하지 않게 비교 대상 보존.
    comparison_interp = _try_comparison(compact, query, coupon, grammar, registry)
    if comparison_interp is not None:
        return comparison_interp

    usage = _has_usage_sense(compact, coupon)
    if not usage:
        # '쿠폰' 만 있고 사용 의미가 없으면(발송/보유 등) 우리 관심 밖 — 기존 경로에 맡긴다.
        return None

    # 3) 순위('쿠폰 사용 횟수 상위 100명') — 임계보다 먼저(숫자는 순위 한도).
    if _has_any(compact, grammar.ranking_markers):
        limit = None
        nums = _NUMBER_RE.findall(compact)
        if nums:
            limit = int(nums[-1].replace(",", ""))
        direction = "asc" if _has_any(compact, grammar.comparison_less) else "desc"
        cond = SegmentCondition(type="ranking", metric="coupon_usage_count",
                                direction=direction, limit=limit, source_text=query)
        return _gate(cond, "ranking", coupon)

    # 4) 임계 필터('쿠폰 5회 초과 사용', '1회에서 3회 사이') — 임계값/범위 보존.
    threshold = _try_threshold(compact, query, coupon, grammar)
    if threshold is not None:
        # '1개 이상'(gte,≤1)·'0개 초과'(gt,<1)은 사실상 '쿠폰을 사용한'(존재)과 동치 — 회원별 집계 없이
        # EXISTS(USE_CPN_CNT>0)로 처리한다(더 싸고 의미 동일). 그 외 임계(≥2, >5, 범위 등)는 그대로 둔다.
        if _threshold_is_at_least_one(threshold):
            return _existence(query, coupon, exists=True)
        return _gate(threshold, "filter", coupon)

    # 5) 존재 여부(사용/미사용) — 지원. 부정 표현은 exists=false.
    exists = not _has_any(compact, _compact_tuple(coupon.negative_expressions))
    return _existence(query, coupon, exists=exists)


def _threshold_is_at_least_one(cond: SegmentCondition) -> bool:
    """임계가 '1 이상'(모든 사용자 ≥1) 을 의미하는가 — 존재(사용 여부)와 동치."""
    if cond.operator == "gte" and cond.value is not None and cond.value <= 1:
        return True
    if cond.operator == "gt" and cond.value is not None and cond.value < 1:
        return True
    return False


def _existence(query: str, coupon: SegmentMetric, *, exists: bool) -> Interpretation:
    """쿠폰 사용 여부(존재/부재) 의미 노드 + capability 판정 + EXISTS 술어."""
    cond = SegmentCondition(type="existence_filter", metric="coupon_usage_count",
                            exists=exists, source_text=query)
    result = _gate(cond, "existence_filter", coupon)
    if result.capability.supported and coupon.source is not None:
        s = coupon.source
        result.existence_predicate = f"{s['alias']}.{s['column']} > 0"
    return result


def _try_derived(compact, query, derived, grammar, registry) -> Interpretation | None:
    if derived is None:
        return None
    alias_hit = any(a in compact for a in _compact_tuple(derived.aliases))
    # 별칭 직접 매칭이 아니어도 '쿠폰' + 개당/건당 표지 + 구매금액 이면 파생으로 인식(어순 독립).
    amount_metric = registry.metrics.get("purchase_amount")
    per_marker = _has_any(compact, grammar.derived_per)
    amount_alias = amount_metric is not None and any(a in compact for a in _compact_tuple(amount_metric.aliases))
    if not (alias_hit or ("쿠폰" in compact and per_marker and amount_alias)):
        return None
    value = _parse_amount(compact)
    # 연산자: 금액 뒤 텍스트에서. 없으면 gte 기본(파생은 어차피 미지원이라 값 보존이 목적).
    after = compact
    m = _AMOUNT_RE.search(compact)
    if m:
        after = compact[m.end():]
    operator = _detect_operator(after, grammar) or "gte"
    cond = SegmentCondition(
        type="derived_metric_filter", metric="purchase_amount_per_coupon",
        operator=operator, value=value, unit="원",
        formula={"type": derived.formula.type, "numerator": derived.formula.numerator,
                 "denominator": derived.formula.denominator} if derived.formula else None,
        source_text=query,
    )
    return _gate(cond, "filter", derived)


def _metric_ending(text: str, registry, grammar) -> str | None:
    """text 의 끝부분에 걸린 지표(별칭이 접미로 등장). 숫자+단위로 끝나면 None(임계값이지 지표 아님)."""
    # 숫자+단위로 끝나면 비교 지표가 아님(예: '...5회').
    for metric in registry.metrics.values():
        for u in metric.units:
            if re.search(r"\d[\d,]*\s*" + re.escape(u) + r"$", text):
                return None
    best = None
    best_len = 0
    for metric in registry.metrics.values():
        for alias in _compact_tuple(metric.aliases):
            if text.endswith(alias) and len(alias) > best_len:
                best, best_len = metric.metric_id, len(alias)
    return best


def _metric_leading(text: str, registry) -> str | None:
    """text 의 앞부분(주어 위치)에서 조사 앞까지의 지표. '구매건수가많은' → purchase_count."""
    best = None
    best_len = 0
    for metric in registry.metrics.values():
        for alias in _compact_tuple(metric.aliases):
            if text.startswith(alias) and len(alias) > best_len:
                best, best_len = metric.metric_id, len(alias)
    return best


def _try_comparison(compact, query, coupon, grammar, registry) -> Interpretation | None:
    # 형태 A: '<지표A>보다 <지표B>가 <많은/큰>' → B > A
    idx = compact.find("보다")
    if idx != -1:
        left, right = compact[:idx], compact[idx + 2:]
        left_metric = _metric_ending(left, registry, grammar)
        right_metric = _metric_leading(right, registry)
        if left_metric and right_metric and left_metric != right_metric:
            involves_coupon = "coupon_usage_count" in (left_metric, right_metric)
            if involves_coupon:
                if _has_any(right, grammar.comparison_less):
                    greater, lesser, op = left_metric, right_metric, "gt"
                else:  # 기본: 많은/큰
                    greater, lesser, op = right_metric, left_metric, "gt"
                return _make_comparison(greater, lesser, op, query, coupon, registry)
    # 형태 B: '<지표B>가 <지표A>를 초과/넘는' (보다 없이) → B > A
    for metric in registry.metrics.values():
        for alias in _compact_tuple(metric.aliases):
            m = re.search(re.escape(alias) + r"(?:을|를)", compact)
            if not m:
                continue
            obj_metric = metric.metric_id  # A(비교 대상, 목적어)
            head = compact[:m.start()]
            subj_metric = _metric_ending(head.rstrip("이가은는"), registry, grammar)
            # '초과/넘는' 등 more 연산자가 뒤따르는가
            tail = compact[m.end():]
            if subj_metric and subj_metric != obj_metric and _has_any(tail, grammar.comparison_more):
                if "coupon_usage_count" in (subj_metric, obj_metric):
                    return _make_comparison(subj_metric, obj_metric, "gt", query, coupon, registry)
    return None


def _make_comparison(greater, lesser, op, query, coupon, registry) -> Interpretation:
    cond = SegmentCondition(
        type="metric_comparison", metric="coupon_usage_count",
        operator=op, left_metric=greater, right_metric=lesser, source_text=query,
    )
    return _gate(cond, "metric_comparison", coupon)


def _try_threshold(compact, query, coupon, grammar) -> SegmentCondition | None:
    nums = _numbers_with_unit(compact, coupon.units)
    if not nums:
        return None
    # 범위: 숫자+단위 2개 이상 + (사이/부터/에서) 또는 (이상 & 이하)
    gte_hit = _has_any(compact, grammar.operators.get("gte", ()))
    lte_hit = _has_any(compact, grammar.operators.get("lte", ()))
    between_marker = _has_any(compact, grammar.between_markers)
    if len(nums) >= 2 and (between_marker or (gte_hit and lte_hit)):
        values = sorted(v for v, _, _ in nums)
        return SegmentCondition(type="metric_filter", metric="coupon_usage_count",
                                operator="between", min_value=float(values[0]),
                                max_value=float(values[-1]), unit="count", source_text=query)
    value, _, end = nums[0]
    operator = _detect_operator(compact[end:], grammar) or "eq"
    return SegmentCondition(type="metric_filter", metric="coupon_usage_count",
                            operator=operator, value=float(value), unit="count", source_text=query)


# ── capability 게이트 ─────────────────────────────────────────────────────────────────────
_CONDITION_OPERATION = {
    "metric_filter": "filter",
    "derived_metric_filter": "filter",
    "existence_filter": "existence_filter",
    "ranking": "ranking",
    "metric_comparison": "metric_comparison",
}


def _gate(condition: SegmentCondition, operation: str, metric: SegmentMetric) -> Interpretation:
    """정규화된 의미 노드에 대해 metric.capabilities[operation] 을 조회해 지원/미지원을 판정한다.

    미지원이면 JSON 의 unsupported_messages[operation] 에서 코드/문구를 가져온다(임의 문자열 생성 금지)."""
    cap = metric.capability(operation)
    if cap is None or not cap.supported:
        msg = metric.message(operation)
        result = CapabilityResult(
            supported=False, operation=operation,
            code=msg.code if msg else f"{metric.metric_id}_{operation}_unsupported",
            message=msg.message if msg else f"'{metric.metric_id}' 의 {operation} 조건은 지원되지 않습니다.",
            clarification=msg.clarification if msg else "",
        )
        return Interpretation(condition=condition, capability=result)
    # 지원되는 연산자인지(필터/존재) 검사
    if condition.operator and cap.supported_operators and condition.operator not in cap.supported_operators:
        msg = metric.message(operation)
        result = CapabilityResult(
            supported=False, operation=operation,
            code=(msg.code if msg else f"{metric.metric_id}_operator_unsupported"),
            message=(msg.message if msg else f"'{metric.metric_id}' 는 {condition.operator} 연산을 지원하지 않습니다."),
            clarification=(msg.clarification if msg else ""),
        )
        return Interpretation(condition=condition, capability=result)
    return Interpretation(condition=condition, capability=CapabilityResult(supported=True, operation=operation))


def validate_condition_capability(condition: SegmentCondition, registry: SegmentSemanticsRegistry) -> CapabilityResult:
    """외부(테스트)에서 의미 노드 하나의 capability 판정만 필요할 때. interpret() 내부와 동일 규칙."""
    operation = _CONDITION_OPERATION.get(condition.type)
    metric = registry.metrics.get(condition.metric)
    if operation is None or metric is None:
        return CapabilityResult(supported=False, operation=operation or "unknown",
                                code="unknown_metric_or_operation", message="알 수 없는 지표/연산")
    return _gate(condition, operation, metric).capability
