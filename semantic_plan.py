"""SemanticPlanV2 — 사용자 요구를 최종 슬롯이 아닌 **의미 노드**로 소유하는 중간 표현.

왜 필요했나: 이 시스템은 같은 원문을 두 번 해석했다. LLM 이 실행 슬롯(query_plan)과
`semantic_ir.missing_fields` 를 만들고, 그 뒤 결정론 백필(numeric_condition_backfill 등)이
같은 문장을 정규식으로 다시 읽어 빈 슬롯을 채우고, 다시 `_drop_*_missing_fields` 가 앞
단계의 결핍 보고를 사후 삭제했다. 의미의 소유자가 없으니 새 조건마다 백필 함수 하나와
결핍 삭제 함수 하나가 늘었다.

SemanticPlanV2 는 그 소유자다:

  - **모든 의미는 노드가 소유한다.** 기간 대 기간 증감은 두 기간과 비교 관계를 한 노드가
    가지므로 `purchase_date` 결핍이 애초에 생기지 않는다(사후 삭제할 것이 없다).
  - **missing 은 계산값이다.** `required_fields(node) - populated_fields(node)`.
  - **status 는 파생값이다.** `derive_status(...)`.
  - **노드 추가 = 이 파일의 클래스 하나.** 백필 함수도, 결핍 삭제 함수도 늘지 않는다.

순수 모듈 규약: graph_rag 를 import 하지 않는다. 실행 슬롯 이름(`target_user.cart_aggregate`
등)을 **모른다** — 그 지식은 legacy_plan_compiler 하나만 갖는다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator


SEMANTIC_PLAN_VERSION = "2.0"


# ── 실패 분류(닫힌 집합) ─────────────────────────────────────────────────────────
# 사용자에게 '미지원'으로 보일 수 있는 것과 아닌 것을 구분하는 것이 이 집합의 존재 이유다.
# execution_failure/internal_fault 는 능력의 부재가 아니라 우리 쪽 사고다.
MISSING_ARGUMENT = "missing_argument"
AMBIGUOUS_REQUIREMENT = "ambiguous_requirement"
UNSUPPORTED_SEMANTICS = "unsupported_semantics"
UNSUPPORTED_DATA_GRAIN = "unsupported_data_grain"
DATA_UNAVAILABLE = "data_unavailable"
VALIDATION_MISMATCH = "validation_mismatch"
EXECUTION_FAILURE = "execution_failure"
INTERNAL_FAULT = "internal_fault"

FAILURE_CODES: frozenset[str] = frozenset({
    MISSING_ARGUMENT,
    AMBIGUOUS_REQUIREMENT,
    UNSUPPORTED_SEMANTICS,
    UNSUPPORTED_DATA_GRAIN,
    DATA_UNAVAILABLE,
    VALIDATION_MISMATCH,
    EXECUTION_FAILURE,
    INTERNAL_FAULT,
})

# 사용자에게 "지원하지 않습니다"로 표시해도 되는 코드. 나머지는 내부 사고이므로
# 확인 요청/오류로 안내한다(미지원으로 뭉개면 능력이 없다고 거짓말하는 것이다).
USER_FACING_UNSUPPORTED: frozenset[str] = frozenset({
    UNSUPPORTED_SEMANTICS,
    UNSUPPORTED_DATA_GRAIN,
    DATA_UNAVAILABLE,
})
INTERNAL_FAILURE_CODES: frozenset[str] = frozenset({
    EXECUTION_FAILURE,
    INTERNAL_FAULT,
    VALIDATION_MISMATCH,
})

# 최종 status(파생값 — 어떤 생산자도 직접 쓰지 않는다).
STATUS_RESOLVED = "resolved"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNSUPPORTED = "unsupported"
STATUS_INVALID = "invalid"
STATUSES: frozenset[str] = frozenset({
    STATUS_RESOLVED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_AMBIGUOUS,
    STATUS_UNSUPPORTED,
    STATUS_INVALID,
})


class SemanticPlanError(ValueError):
    """SemanticPlanV2 의 구조 계약 위반."""


# ── 필드 선언 ────────────────────────────────────────────────────────────────────
# kind 는 **값의 종류**다(정규화기 dispatch 키). 목적지 슬롯이 아니다 — 노드는 자기 값이
# 어디로 컴파일되는지 모른다.
VALUE_KINDS: frozenset[str] = frozenset({
    "metric",        # 지표 라벨/canonical → metric_id (MetricResolver)
    "operator",      # 비교어/기호 → >=,>,<=,<,= (OperatorNormalizer)
    "quantity",      # 수량/금액 → Money 또는 수 (AmountNormalizer)
    "period",        # 기간 표현 → Period(from,to) (PeriodNormalizer)
    "rank_limit",    # 상위 N/N% → RankLimit (PeriodNormalizer 와 별개)
    "entity",        # 회원/상품/브랜드 → canonical entity (EntityResolver)
    "scope",         # 집계 대상 도메인(cart/order/campaign/profile)
    "relation",      # 관계·전이·증감 종류
    "unit",          # 개/회/건/종 → 의미 단위 (UnitNormalizer)
    "text",          # 자유 텍스트(라벨·근거)
    "flag",          # bool
    "nodes",         # 하위 노드 목록(LogicalExpression)
    "raw",           # 정규화 대상이 아닌 구조체(이미 검증된 AST 등)
})


@dataclass(frozen=True)
class FieldSpec:
    """노드 필드 하나의 선언. 이 선언 하나가 missing 계산·LLM 스키마·정규화기 dispatch 를 모두 만든다."""

    name: str
    kind: str
    required: bool = True
    description: str = ""
    enum: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in VALUE_KINDS:
            raise SemanticPlanError(f"{self.name}: 알 수 없는 값 종류 {self.kind!r}")


def _empty(value: Any) -> bool:
    """'채워지지 않음'의 단일 정의 — populated_fields 가 이걸로 계산된다."""
    return value is None or value == "" or value == [] or value == {}


# ── 노드 ─────────────────────────────────────────────────────────────────────────
@dataclass
class SemanticNode:
    """의미 노드 하나. 공통 필드 + 타입별 값(values).

    values 를 dict 로 두는 이유: 필드 선언(FIELDS)이 단일 권위여야 missing 계산·스키마
    생성·정규화가 갈라지지 않기 때문이다. 타입별 접근은 속성으로 열어 둔다
    (`node.metric` 은 `node.values["metric"]`).
    """

    TYPE: ClassVar[str] = ""
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = ()

    id: str
    source_span: str = ""
    source_start: int | None = None
    source_end: int | None = None
    confidence: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    # 이 노드를 만든 생산자(감사·충돌 판정용). 의미가 아니라 계보다.
    producer: str = "llm"

    @property
    def type(self) -> str:
        return self.TYPE

    def __getattr__(self, name: str) -> Any:
        # dataclass 필드가 아닌 이름만 여기 온다 — 선언된 값 필드로 위임한다.
        if name.startswith("_") or name in {"values", "TYPE", "FIELDS"}:
            raise AttributeError(name)
        for spec in type(self).FIELDS:
            if spec.name == name:
                return self.values.get(name)
        raise AttributeError(name)

    # ── 스키마 파생 ──
    @classmethod
    def field_spec(cls, name: str) -> FieldSpec | None:
        for spec in cls.FIELDS:
            if spec.name == name:
                return spec
        return None

    @classmethod
    def required_field_names(cls) -> frozenset[str]:
        return frozenset(spec.name for spec in cls.FIELDS if spec.required)

    def populated_field_names(self) -> frozenset[str]:
        return frozenset(
            name for name, value in self.values.items() if not _empty(value)
        )

    def missing_fields(self) -> tuple[str, ...]:
        """`required_fields(node) - populated_fields(node)` — 계산값이지 보고값이 아니다."""
        missing = type(self).required_field_names() - self.populated_field_names()
        return tuple(sorted(missing))

    def children(self) -> tuple["SemanticNode", ...]:
        return ()

    # ── 직렬화 ──
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.TYPE,
            "source_span": self.source_span,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "confidence": self.confidence,
        }
        for spec in type(self).FIELDS:
            if spec.name not in self.values:
                continue
            value = self.values[spec.name]
            if spec.kind == "nodes":
                payload[spec.name] = [child.to_dict() for child in value or []]
            else:
                payload[spec.name] = copy.deepcopy(value)
        if self.producer != "llm":
            payload["producer"] = self.producer
        return payload


@dataclass
class Predicate(SemanticNode):
    """단일 속성/지표 술어. '평균 구매주기가 30일 이내', '잔액이 1만 원 이상'."""

    TYPE: ClassVar[str] = "predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("subject", "entity", required=True, description="술어의 주어(기본 member)"),
        FieldSpec("metric", "metric", required=True, description="지표/속성 canonical 또는 원문 라벨"),
        FieldSpec("operator", "operator", required=True, description="비교 연산자('이상' 등 원문 표현 허용)"),
        FieldSpec("value", "quantity", required=True, description="비교 대상 값('30일'·'1만 원' 등)"),
        FieldSpec("unit", "unit", required=False, description="수량의 단위(개/회/건/종/일)"),
        FieldSpec("period", "period", required=False, description="측정 기간"),
        FieldSpec("negated", "flag", required=False, description="부정 술어인가"),
        FieldSpec("state", "relation", required=False, description="상대 상태(있음/없음/오늘 등 날짜 상태)"),
    )


@dataclass
class AggregatePredicate(SemanticNode):
    """집계 임계 술어. '장바구니 상품 종류가 2개 이상', '10만 원 이상 구매한'."""

    TYPE: ClassVar[str] = "aggregate_predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("scope", "scope", required=True,
                  description="집계 대상 도메인",
                  enum=("cart", "order", "campaign", "profile")),
        FieldSpec("metric", "metric", required=True, description="집계 지표"),
        FieldSpec("operator", "operator", required=True, description="비교 연산자"),
        FieldSpec("value", "quantity", required=True, description="임계값"),
        FieldSpec("aggregation", "relation", required=False,
                  description="집계 함수", enum=("sum", "avg", "count", "max", "min")),
        FieldSpec("unit", "unit", required=False, description="수량 단위"),
        FieldSpec("period", "period", required=False, description="집계 창"),
        FieldSpec("grain", "scope", required=False,
                  description="집계 그레인", enum=("per_member", "per_order", "per_product", "per_brand")),
        FieldSpec("qualifier", "text", required=False, description="브랜드/카테고리 등 한정어"),
        FieldSpec("event", "relation", required=False, description="캠페인 이벤트 canonical(scope=campaign)"),
    )


@dataclass
class MetricComparison(SemanticNode):
    """기간 대 기간 비교. '2026년 2월과 3월의 구매금액이 증가한'.

    **이 노드가 두 기간과 비교 관계를 모두 소유한다** — 그래서 별도 purchase_date 결핍이
    구조적으로 생길 수 없다(사후 삭제가 필요 없어진 지점).
    """

    TYPE: ClassVar[str] = "metric_comparison"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("metric", "metric", required=True, description="비교 지표"),
        FieldSpec("baseline", "period", required=True, description="기준 기간"),
        FieldSpec("current", "period", required=True, description="비교 기간"),
        FieldSpec("relation", "relation", required=True,
                  description="변화 방향", enum=("increase", "decrease")),
        FieldSpec("threshold", "quantity", required=False, description="변화율 임계값"),
        FieldSpec("threshold_operator", "operator", required=False, description="변화율 비교 연산자"),
        FieldSpec("scope", "scope", required=False, description="집계 도메인(기본 order)"),
    )


@dataclass
class RankedSet(SemanticNode):
    """랭킹 집합. '구매금액 상위 10% 회원', '가장 많이 팔린 상품 10개'."""

    TYPE: ClassVar[str] = "ranked_set"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("entity", "entity", required=True, description="랭킹 대상 엔터티(member/product/…)"),
        FieldSpec("metric", "metric", required=True, description="랭킹 기준 지표"),
        FieldSpec("direction", "relation", required=True,
                  description="정렬 방향", enum=("descending", "ascending")),
        FieldSpec("limit", "rank_limit", required=True, description="상위 N명/N% 제한"),
        FieldSpec("period", "period", required=False, description="랭킹 계산 창"),
        FieldSpec("qualifier", "text", required=False, description="랭킹 범위 한정어"),
    )


@dataclass
class EntitySetMembership(SemanticNode):
    """파생 집합 소속. '가장 많이 팔린 상품 10개를 구매한 회원'.

    랭킹 집합(RankedSet)과 회원 관계를 잇는다 — 대상 상품은 계산 결과이므로 리터럴
    상품 조건(purchase_object)이 결핍일 수 없다.
    """

    TYPE: ClassVar[str] = "entity_set_membership"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("member_entity", "entity", required=True, description="소속을 판정할 엔터티(member)"),
        FieldSpec("relation", "relation", required=True, description="집합과의 관계(purchase/cart 등)"),
        FieldSpec("ranked_set", "nodes", required=True, description="소속 대상 랭킹 집합(RankedSet 1개)"),
        FieldSpec("negated", "flag", required=False, description="비소속(구매하지 않은)인가"),
        FieldSpec("cardinality", "quantity", required=False, description="교집합 개수 조건"),
        FieldSpec("cardinality_operator", "operator", required=False, description="교집합 개수 연산자"),
    )

    def children(self) -> tuple[SemanticNode, ...]:
        return tuple(self.values.get("ranked_set") or ())


@dataclass
class RelationPredicate(SemanticNode):
    """엔터티 사이/속성 시점의 관계. '지난달 말 기준 VIP', '골드→VIP 승급', '같은 상품 동시 구매'."""

    TYPE: ClassVar[str] = "relation_predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("subject", "entity", required=True, description="주어 엔터티(member)"),
        FieldSpec("attribute", "metric", required=True, description="속성/대상 canonical(등급·상태·상품)"),
        FieldSpec("relation", "relation", required=True,
                  description=(
                      "관계 종류. as_of=특정 시점의 값, transition=값 전이, held_throughout=기간 내내 유지, "
                      "stable=한 번도 안 바뀜, changed_n_times=N회 변경, ever=한 번이라도, "
                      "never=한 번도 아님, exists_every_month=모든 월에 존재, co_purchase=동시 구매."
                  ),
                  enum=("as_of", "transition", "held_throughout", "stable", "changed_n_times",
                        "ever", "never", "exists_every_month", "co_purchase")),
        FieldSpec("value", "text", required=False, description="시점/보유 판정의 속성 값"),
        FieldSpec("value_comparison", "relation", required=False,
                  description="값 비교(등급 순서)", enum=("eq", "gte", "lte")),
        FieldSpec("from_value", "text", required=False, description="전이 출발 값"),
        FieldSpec("to_value", "text", required=False, description="전이 도착 값"),
        FieldSpec("period", "period", required=False, description="시점 앵커(달력 월)"),
        FieldSpec("months", "quantity", required=False, description="관측 개월 수"),
        FieldSpec("count", "quantity", required=False, description="변경 횟수"),
        FieldSpec("count_operator", "operator", required=False, description="변경 횟수 연산자"),
    )


@dataclass
class LogicalExpression(SemanticNode):
    """논리 결합. and / or / not."""

    TYPE: ClassVar[str] = "logical_expression"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("operator", "relation", required=True,
                  description="논리 연산자", enum=("and", "or", "not")),
        FieldSpec("children", "nodes", required=True, description="피연산 노드 목록"),
    )

    def children(self) -> tuple[SemanticNode, ...]:
        return tuple(self.values.get("children") or ())


NODE_CLASSES: tuple[type[SemanticNode], ...] = (
    Predicate,
    AggregatePredicate,
    MetricComparison,
    RankedSet,
    EntitySetMembership,
    RelationPredicate,
    LogicalExpression,
)

NODE_CLASS_BY_TYPE: dict[str, type[SemanticNode]] = {
    cls.TYPE: cls for cls in NODE_CLASSES
}

# 노드 타입별 필수 필드(파생 — 손 목록이 아니다). 문서/테스트가 이 이름으로 읽는다.
NODE_REQUIREMENTS: dict[str, frozenset[str]] = {
    cls.TYPE: cls.required_field_names() for cls in NODE_CLASSES
}


# ── 플랜 ─────────────────────────────────────────────────────────────────────────
@dataclass
class SemanticPlanV2:
    """한 요청의 의미 노드 전체 + 파생 판정."""

    version: str = SEMANTIC_PLAN_VERSION
    source_query: str = ""
    nodes: list[SemanticNode] = field(default_factory=list)
    # 같은 원문 근거에 서로 다른 의미가 감지된 사건(조용한 승자 선택 금지).
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # 원문에 근거가 있는데 노드가 없는 구간(coverage verifier 산출).
    uncovered_requirements: list[dict[str, Any]] = field(default_factory=list)
    # capability 판정 결과(노드 id → verdict dict).
    capability_verdicts: list[dict[str, Any]] = field(default_factory=list)
    # 스키마·정규화 검증 실패(내부 불량 — 미지원과 구분된다).
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def walk(self) -> Iterator[SemanticNode]:
        def _walk(nodes: list[SemanticNode] | tuple[SemanticNode, ...]) -> Iterator[SemanticNode]:
            for node in nodes:
                yield node
                yield from _walk(node.children())

        yield from _walk(self.nodes)

    def node_by_id(self, node_id: str) -> SemanticNode | None:
        for node in self.walk():
            if node.id == node_id:
                return node
        return None

    def missing_fields(self) -> tuple[str, ...]:
        """`<node_id>.<field>` 형태의 결핍 목록(계산값)."""
        out: list[str] = []
        for node in self.walk():
            for name in node.missing_fields():
                out.append(f"{node.id}.{name}")
        return tuple(out)

    def unsupported_operations(self) -> tuple[dict[str, Any], ...]:
        """사용자에게 '미지원'으로 보여도 되는 capability 판정만 추린다."""
        return tuple(
            verdict for verdict in self.capability_verdicts
            if verdict.get("failure_code") in USER_FACING_UNSUPPORTED
        )

    def internal_failures(self) -> tuple[dict[str, Any], ...]:
        """내부 사고(미지원으로 표시하면 안 되는 것)."""
        return tuple(
            item for item in [*self.capability_verdicts, *self.validation_errors]
            if item.get("failure_code") in INTERNAL_FAILURE_CODES
        )

    def status(self) -> str:
        return derive_status(
            missing_fields=self.missing_fields(),
            unsupported_operations=self.unsupported_operations(),
            validation_errors=self.validation_errors,
            conflicts=self.conflicts,
            uncovered_requirements=self.uncovered_requirements,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_query": self.source_query,
            "nodes": [node.to_dict() for node in self.nodes],
            "conflicts": copy.deepcopy(self.conflicts),
            "uncovered_requirements": copy.deepcopy(self.uncovered_requirements),
            "capability_verdicts": copy.deepcopy(self.capability_verdicts),
            "validation_errors": copy.deepcopy(self.validation_errors),
            "missing_fields": list(self.missing_fields()),
            "status": self.status(),
            "notes": list(self.notes),
        }


def derive_status(
    *,
    missing_fields: tuple[str, ...] | list[str],
    unsupported_operations: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    validation_errors: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    conflicts: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    uncovered_requirements: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> str:
    """최종 status 는 계산값이다 — 어떤 생산자(LLM 포함)도 직접 쓰지 않는다.

    우선순위: 내부 불량 > 미지원 > 모호 > 결핍/미커버 > resolved. 내부 불량이 가장 먼저인
    이유는, 우리 쪽 사고를 '지원하지 않는 요청'으로 표시하면 사용자가 능력의 부재로
    오인하기 때문이다.
    """
    if validation_errors:
        return STATUS_INVALID
    if unsupported_operations:
        return STATUS_UNSUPPORTED
    if conflicts:
        return STATUS_AMBIGUOUS
    if missing_fields or uncovered_requirements:
        return STATUS_NEEDS_CLARIFICATION
    return STATUS_RESOLVED


# ── 역직렬화 ─────────────────────────────────────────────────────────────────────
def node_from_dict(payload: Any, *, producer: str = "llm") -> SemanticNode:
    """LLM/후보 생산자의 dict 를 타입드 노드로 만든다(선언되지 않은 필드는 버린다)."""
    if not isinstance(payload, dict):
        raise SemanticPlanError("의미 노드는 객체여야 한다")
    node_type = payload.get("type")
    cls = NODE_CLASS_BY_TYPE.get(str(node_type))
    if cls is None:
        raise SemanticPlanError(f"알 수 없는 의미 노드 타입: {node_type!r}")
    node_id = payload.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise SemanticPlanError(f"{node_type}: id 는 비어 있지 않은 문자열이어야 한다")
    values: dict[str, Any] = {}
    for spec in cls.FIELDS:
        if spec.name not in payload:
            continue
        raw = payload[spec.name]
        if _empty(raw):
            continue
        if spec.kind == "nodes":
            items = raw if isinstance(raw, list) else [raw]
            values[spec.name] = [
                node_from_dict(item, producer=producer) for item in items
            ]
        else:
            values[spec.name] = copy.deepcopy(raw)
    confidence = payload.get("confidence")
    return cls(
        id=node_id.strip(),
        source_span=str(payload.get("source_span") or ""),
        source_start=_int_or_none(payload.get("source_start")),
        source_end=_int_or_none(payload.get("source_end")),
        confidence=float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
        values=values,
        producer=str(payload.get("producer") or producer),
    )


def plan_from_dict(payload: Any, *, source_query: str = "", producer: str = "llm") -> SemanticPlanV2:
    if not isinstance(payload, dict):
        raise SemanticPlanError("SemanticPlanV2 는 객체여야 한다")
    raw_nodes = payload.get("nodes")
    if raw_nodes is None:
        raw_nodes = []
    if not isinstance(raw_nodes, list):
        raise SemanticPlanError("SemanticPlanV2.nodes 는 배열이어야 한다")
    plan = SemanticPlanV2(
        source_query=source_query or str(payload.get("source_query") or ""),
        nodes=[node_from_dict(item, producer=producer) for item in raw_nodes],
    )
    _assign_unique_ids(plan)
    return plan


def _assign_unique_ids(plan: SemanticPlanV2) -> None:
    """중복 id 는 병합·충돌 판정을 망가뜨리므로 결정론적으로 재부여한다."""
    seen: set[str] = set()
    counter = 0
    for node in plan.walk():
        if node.id not in seen:
            seen.add(node.id)
            continue
        while True:
            counter += 1
            candidate = f"{node.id}-{counter}"
            if candidate not in seen:
                node.id = candidate
                seen.add(candidate)
                break


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# ── LLM 노출 스키마(선언에서 파생) ───────────────────────────────────────────────
# 값 종류별 JSON 표현. 목적지 슬롯 이름은 어디에도 없다 — LLM 은 슬롯을 모른다.
_KIND_SCHEMA: dict[str, dict[str, Any]] = {
    "metric": {"type": "string"},
    "operator": {"type": "string"},
    "quantity": {
        "anyOf": [
            {"type": "number"},
            {"type": "string"},
            {
                "type": "object",
                "description": "금액은 {amount, currency}, 그 외 수량은 {value, unit}.",
                "properties": {
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                },
            },
        ]
    },
    "period": {
        "type": "object",
        "description": (
            "기간. 달력 월은 {type:'calendar_month', year, month}, 절대 구간은 "
            "{type:'absolute', from:'YYYY-MM-DD', to:'YYYY-MM-DD'}, 상대 창은 "
            "{type:'relative', value, unit:'days'|'weeks'|'months'|'years'}."
        ),
        "properties": {
            "type": {"type": "string", "enum": ["calendar_month", "absolute", "relative"]},
            "year": {"type": "integer"},
            "month": {"type": "integer"},
            "from": {"type": "string"},
            "to": {"type": "string"},
            "value": {"type": "integer"},
            "unit": {"type": "string"},
            "label": {"type": "string"},
        },
    },
    "rank_limit": {
        "type": "object",
        "description": "랭킹 제한. {type:'percent'|'count', value:number}.",
        "properties": {
            "type": {"type": "string", "enum": ["percent", "count"]},
            "value": {"type": "number"},
        },
    },
    "entity": {"type": "string"},
    "scope": {"type": "string"},
    "relation": {"type": "string"},
    "unit": {"type": "string"},
    "text": {"type": "string"},
    "flag": {"type": "boolean"},
    "raw": {"type": "object"},
}


def _field_schema(spec: FieldSpec, *, node_ref: str) -> dict[str, Any]:
    if spec.kind == "nodes":
        return {
            "type": "array",
            "description": spec.description,
            "items": {"$ref": node_ref},
        }
    schema = copy.deepcopy(_KIND_SCHEMA[spec.kind])
    if spec.enum:
        schema = {"type": "string", "enum": list(spec.enum)}
    if spec.description:
        schema["description"] = spec.description
    return schema


_COMMON_NODE_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string", "description": "노드 식별자(요청 내 유일, 예: req-1)."},
    "type": {"type": "string", "enum": sorted(NODE_CLASS_BY_TYPE)},
    "source_span": {
        "type": "string",
        "description": "이 노드의 근거가 되는 원문 구절 그대로(요약·재작성 금지).",
    },
    "source_start": {"type": "integer", "description": "source_span 의 원문 시작 인덱스."},
    "source_end": {"type": "integer", "description": "source_span 의 원문 끝 인덱스."},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
}


def semantic_node_json_schema(*, node_ref: str = "#/$defs/semanticNode") -> dict[str, Any]:
    """노드 선언에서 파생한 JSON 스키마(손으로 쓴 두 번째 권위를 만들지 않는다)."""
    properties: dict[str, Any] = copy.deepcopy(_COMMON_NODE_PROPERTIES)
    for cls in NODE_CLASSES:
        for spec in cls.FIELDS:
            schema = _field_schema(spec, node_ref=node_ref)
            existing = properties.get(spec.name)
            if existing is None:
                properties[spec.name] = schema
                continue
            if existing != schema:
                # 같은 이름을 여러 노드가 서로 다른 종류로 쓰면 union 으로 노출한다.
                variants = existing.get("anyOf") if "anyOf" in existing else [existing]
                if schema not in variants:
                    variants = [*variants, schema]
                properties[spec.name] = {"anyOf": variants}
    return {
        "type": "object",
        "description": (
            "의미 노드 하나. 타입별 필수 필드는 시스템이 검증하며, 확실하지 않은 필드는 "
            "지어내지 말고 비워 둔다(시스템이 결핍으로 계산한다)."
        ),
        "properties": properties,
        "required": ["id", "type", "source_span"],
    }


def semantic_plan_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            "사용자 원문의 의미 노드 목록. 실행 슬롯·SQL·missing/status 는 만들지 않는다 — "
            "시스템이 이 노드에서 계산한다."
        ),
        "properties": {
            "nodes": {
                "type": "array",
                "description": "원문에서 확인되는 조건 하나당 노드 하나.",
                "items": {"$ref": "#/$defs/semanticNode"},
            }
        },
        "required": ["nodes"],
    }


def node_requirement_documentation() -> dict[str, dict[str, Any]]:
    """노드 타입별 필수/선택 필드 문서(테스트·프롬프트가 읽는 파생 표)."""
    return {
        cls.TYPE: {
            "required": sorted(spec.name for spec in cls.FIELDS if spec.required),
            "optional": sorted(spec.name for spec in cls.FIELDS if not spec.required),
            "description": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else "",
        }
        for cls in NODE_CLASSES
    }


__all__ = [
    "AMBIGUOUS_REQUIREMENT",
    "AggregatePredicate",
    "DATA_UNAVAILABLE",
    "EXECUTION_FAILURE",
    "EntitySetMembership",
    "FAILURE_CODES",
    "FieldSpec",
    "INTERNAL_FAILURE_CODES",
    "INTERNAL_FAULT",
    "LogicalExpression",
    "MISSING_ARGUMENT",
    "MetricComparison",
    "NODE_CLASSES",
    "NODE_CLASS_BY_TYPE",
    "NODE_REQUIREMENTS",
    "Predicate",
    "RankedSet",
    "RelationPredicate",
    "SEMANTIC_PLAN_VERSION",
    "STATUSES",
    "STATUS_AMBIGUOUS",
    "STATUS_INVALID",
    "STATUS_NEEDS_CLARIFICATION",
    "STATUS_RESOLVED",
    "STATUS_UNSUPPORTED",
    "SemanticNode",
    "SemanticPlanError",
    "SemanticPlanV2",
    "UNSUPPORTED_DATA_GRAIN",
    "UNSUPPORTED_SEMANTICS",
    "USER_FACING_UNSUPPORTED",
    "VALIDATION_MISMATCH",
    "derive_status",
    "node_from_dict",
    "node_requirement_documentation",
    "plan_from_dict",
    "semantic_node_json_schema",
    "semantic_plan_json_schema",
]
