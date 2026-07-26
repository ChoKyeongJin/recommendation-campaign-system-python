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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CAPABILITIES_PATH = Path("docs/data/requirement_capabilities.json")

# requirement 귀결 상태. 'detected' 는 아직 회계 전(초기값). 검증기가 나머지 넷 중 하나로 확정해야 한다.
TERMINAL_STATUSES = frozenset({"parsed", "compiled", "clarification", "unsupported"})
# 응답 출고를 막아야 하는(= 사용자 확인이 필요한) 귀결.
BLOCKING_STATUSES = frozenset({"clarification", "unsupported"})

QUALIFIER_TYPES = frozenset({"entity", "dimension", "time_scope", "attribute"})


class RequirementCapabilityError(ValueError):
    """capability 레지스트리(JSON)가 스키마를 위반했을 때(로드 시 즉시 실패, 조용한 무시 방지)."""


# entity qualifier 표면 표지(브랜드/상품/카테고리·제품·품목명 + 조사/콜론 + 값). 조사를 '필수'로 둬
# 일반 명사구('상품 구매한')가 값으로 오포착되는 것을 막고, 바로 뒤의 고유 값만 딴다. 값 뒤 조사는 뗀다.
_ENTITY_QUALIFIER_RE = re.compile(
    r"(브랜드명|브랜드|상품명|카테고리명|카테고리|제품명|품목명)"
    r"(?:이|가|은|는|:|=)\s*([가-힣A-Za-z0-9][가-힣A-Za-z0-9]+)"
)
_ENTITY_MARKER_DOMAIN = {
    "브랜드명": "brand", "브랜드": "brand",
    "상품명": "product", "제품명": "product", "품목명": "product",
    "카테고리명": "category", "카테고리": "category",
}
_JOSA_TAIL_RE = re.compile(r"(을|를|이|가|은|는|인|의|와|과|도|만|에게|에서|에)$")


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
@dataclass
class Qualifier:
    type: str  # entity | dimension | time_scope | attribute
    domain: str  # brand | product | category | region | ...
    raw_value: str
    resolved_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "domain": self.domain, "raw_value": self.raw_value, "resolved_value": self.resolved_value}


@dataclass
class SourceRequirement:
    id: str
    type: str  # qualified_condition | base_condition | comparison | derived | ...
    base: dict[str, Any]  # {type: behavior|metric|dimension|set, name: str}
    qualifiers: list[Qualifier] = field(default_factory=list)
    relation: str = "applies_to"  # applies_to | compared_with | excluded_from | grouped_by
    operator: Any = None
    value: Any = None
    time_scope: Any = None
    negation: bool = False
    comparison_target: Any = None
    derived_formula: Any = None
    source_text: str = ""
    source_span: dict[str, int] = field(default_factory=lambda: {"start": 0, "end": 0})
    status: str = "detected"  # detected → (parsed|compiled|clarification|unsupported)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "base": self.base,
            "qualifiers": [q.to_dict() for q in self.qualifiers],
            "relation": self.relation,
            "operator": self.operator,
            "value": self.value,
            "time_scope": self.time_scope,
            "negation": self.negation,
            "comparison_target": self.comparison_target,
            "derived_formula": self.derived_formula,
            "source_text": self.source_text,
            "source_span": self.source_span,
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
        domain = _ENTITY_MARKER_DOMAIN.get(marker, "entity")
        requirements.append(SourceRequirement(
            id=f"req_{index + 1}",
            type="qualified_condition",
            base=dict(base),
            qualifiers=[Qualifier(type="entity", domain=domain, raw_value=value)],
            relation="applies_to",
            source_text=match.group(0),
            source_span={"start": match.start(), "end": match.end()},
            status="detected",
        ))
    return requirements


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
    for req in requirements:
        for qualifier in req.qualifiers:
            cap = registry.qualifier_capability(base_name, qualifier.domain)
            if cap is not None and not cap.get("supported", False):
                req.status = "unsupported"
                req.message = str(cap.get("message") or f"현재 {registry.base_label(base_name)}에는 이 조건을 함께 적용할 수 없습니다.")
                break
            # 지원됨: 반영 확인 — ① 구조화 evidence 우선, ② SQL 리터럴 부분문자열(canonical 보정 대비 정규화) 폴백.
            if _evidence_marks_compiled(qualifier, applied_requirements) or _normalize_value(qualifier.raw_value) in normalized_sql:
                req.status = "compiled"
                qualifier.resolved_value = qualifier.raw_value
            else:
                req.status = "clarification"
                req.message = (
                    f"'{qualifier.raw_value}' 조건이 생성 SQL 에 반영되지 않았습니다 — 원문에는 있으나 "
                    f"실DB 타겟 추출로 컴파일되지 못한 것으로 보입니다. 의도가 맞는지 확인해 주세요."
                )
    return RequirementAccounting(requirements=requirements)
