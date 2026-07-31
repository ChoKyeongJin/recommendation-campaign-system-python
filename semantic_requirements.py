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
  2) 도메인별 지원 여부는 코드가 아니라 JSON capability registry(docs/data/requirement_capabilities.json)의
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
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import lexicon_patterns


# 기본 경로는 **모듈 기준 절대경로**다. 상대경로면 cwd 가 저장소 밖일 때 설정을 못 읽고
# 조용히 빈 레지스트리로 강등된다(증상이 예외가 아니라 '조금 다른 답'이라 눈에 띄지 않는다).
_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CAPABILITIES_PATH = _REPO_ROOT / "docs" / "data" / "requirement_capabilities.json"

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
    "logical_expression",
    "member_column_selection_filter",
    "member_metric_ranking",
    "member_metric_selection",
    "policy_constraints",
    "purchase_count_ranking",
    "region_density_target",
    "region_member_count_target",
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
_KOREAN_COUNT_ALT = lexicon_patterns.alternation("source_korean_count")
_ENTITY_COUNTER_ALT = lexicon_patterns.alternation("source_entity_counter")
_ENTITY_DOMAIN_ALT = lexicon_patterns.alternation("source_entity_domain")
_ALL_QUANTIFIER_ALT = lexicon_patterns.alternation("source_all_quantifier")
_SUPERLATIVE_ALT = lexicon_patterns.alternation("source_superlative")
_LATEST_SELECTOR_ALT = lexicon_patterns.alternation("source_latest_selector")
_SNAPSHOT_GRAIN_ALT = lexicon_patterns.alternation("source_snapshot_grain")
_SNAPSHOT_SYSTEM_ALT = lexicon_patterns.alternation("source_snapshot_system")
_SNAPSHOT_NOUN_ALT = lexicon_patterns.alternation("source_snapshot_noun")
_MEMBER_NOUN_ALT = lexicon_patterns.alternation("member_noun")
_RECURRENCE_RE = re.compile(
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
_KOREAN_COUNT_VALUES = {
    "한": 1,
    "두": 2,
    "세": 3,
    "네": 4,
    "다섯": 5,
}
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


def _requirement_span(
    query: str,
    plan: dict[str, Any],
    container: str,
    slot: str,
    value: Any,
) -> tuple[int, int, str]:
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
            return start, end, query[start:end]
    matched = _matched_term_span(query, plan, value)
    if matched is not None:
        return matched[0], matched[1], query[matched[0]:matched[1]]
    folded = query.casefold()
    for candidate in _value_text_candidates(value):
        start = folded.find(candidate.casefold())
        if start >= 0:
            return start, start + len(candidate), query[start:start + len(candidate)]
    # 정밀 구간을 아직 제공하지 않는 필터도 요구사항을 잃지 않는다. 이 경우 원문 전체를 보수적인 근거
    # 구간으로 둔다. source가 rules/llm인지 별도로 남으므로 소비자는 정밀 span과 구분할 수 있다.
    return 0, len(query), query


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
        start, end, source_text = _requirement_span(query, plan, container, slot, value)
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
            else _KOREAN_COUNT_VALUES.get(raw_count or "")
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


def capture_source_semantic_obligations(
    query: str,
) -> tuple[SourceRequirement, ...]:
    """Capture lossy semantic operators directly from the immutable source.

    These requirements are intentionally independent of parser output.  A
    parser may recognize a nearby number, date window, entity noun, or grade,
    but that does not discharge the recurrence/set/snapshot operator itself.
    Only an explicit compiler receipt can do so.
    """

    captured = [
        *_recurrence_obligations(query),
        *_entity_set_obligations(query),
        *_snapshot_obligations(query),
    ]
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
            or requirement.get("type") != "semantic_obligation"
        ):
            continue
        requirement_id = str(requirement.get("id") or "")
        receipt = receipts.get(requirement_id)
        if receipt is not None and receipt.get("status") == "compiled":
            continue
        base = requirement.get("base")
        kind = (
            str(base.get("name"))
            if isinstance(base, Mapping) and base.get("name")
            else "semantic_obligation"
        )
        default_reason = {
            "temporal_recurrence": (
                "주기별 반복 조건을 총 기간 집계로 축소하지 않고 컴파일했다는 근거가 없습니다."
            ),
            "referenced_entity_set": (
                "외부에서 지정된 엔터티 집합의 구체 값과 전체집합 조건을 컴파일했다는 근거가 없습니다."
            ),
            "snapshot_selector": (
                "회원별 최신 스냅샷 선택 조건을 컴파일했다는 근거가 없습니다."
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
