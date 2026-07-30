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

현재 추출 범위(v1): entity qualifier(브랜드/상품/카테고리/제품/품목명 + 값). 스키마·레지스트리·검증기는
도메인 공통이라, 새 qualifier 종류(dimension/time_scope 등)는 추출기 detector 하나 + JSON 항목만 추가하면 된다.

실행: python -m pytest tests/test_semantic_requirements.py -q
"""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


DEFAULT_CAPABILITIES_PATH = Path("docs/data/requirement_capabilities.json")

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
    (graph_rag._normalize_product_term 과 동일 규칙)."""
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").casefold())


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
