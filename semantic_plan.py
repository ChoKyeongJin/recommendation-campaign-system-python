"""SemanticPlanV2 — 사용자 요구를 최종 슬롯이 아닌 **의미 노드**로 소유하는 중간 표현.

왜 필요했나: 이 시스템은 같은 원문을 두 번 해석했다. LLM 이 실행 슬롯(query_plan)과
`semantic_ir.missing_fields` 를 만들고, 그 뒤 결정론 백필이 같은 문장을 정규식으로 다시
읽어 빈 슬롯을 채우고, 다시 결핍 삭제 함수가 앞 단계의 결핍 보고를 사후 삭제했다.
의미의 소유자가 없으니 새 조건마다 백필 함수 하나와 결핍 삭제 함수 하나가 늘었다.

SemanticPlanV2 는 그 소유자다:

  - **모든 의미는 노드가 소유한다.** 기간 대 기간 증감은 두 기간과 비교 관계를 한 노드가
    가지므로 별도 기간 필드의 결핍이 애초에 생기지 않는다(사후 삭제할 것이 없다).
  - **missing 은 계산값이다.** `required_fields(node) - populated_fields(node)`.
  - **status 는 파생값이다.** `derive_status(...)`.
  - **노드 추가 = 이 파일의 클래스 하나.** 백필 함수도, 결핍 삭제 함수도 늘지 않는다.

**범용 코어 규약**(2026-08-02 계층 분리):
  - graph_rag 를 import 하지 않는다.
  - 실행 슬롯 이름도, 실행 플랜 컨테이너 이름도 모른다(컴파일러 소유).
  - 도메인 값 어휘(집계 도메인·엔터티·속성 관계 …)를 **하드코딩하지 않는다**. 필드 선언은
    `vocabulary=` 로 도메인 어휘 키만 가리키고, 실제 값은 도메인 플러그인이 준다
    (`semantic_domain_binding`). 여기 남는 enum 은 도메인과 무관한 논리 연산자뿐이다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Sequence

import semantic_domain_binding


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

# ── 결핍의 원인(닫힌 집합) ──────────────────────────────────────────────────────
# "왜 이 필드가 비었는가"를 구분하지 않으면 전부 사용자 확인 요청이 된다. 사용자는
# `member_entity` 를 정해 줄 수 없다 — 그건 우리 쪽 사고이지 사용자의 정보 누락이 아니다.
CAUSE_USER_OMISSION = "user_omission"      # 원문에 정말 없다 → 물어봐야 한다
CAUSE_MODEL_OMISSION = "model_omission"    # 구간은 청구해 놓고 못 채웠다 → 재방출
CAUSE_TYPE_MISMATCH = "type_mismatch"      # 타입/필드가 어긋났다 → 재분류 또는 재방출
CAUSE_REGISTRY_GAP = "registry_gap"        # 실행 어휘가 비어 있다 → 설정/컴파일러 결함
MISSING_CAUSES: frozenset[str] = frozenset({
    CAUSE_USER_OMISSION,
    CAUSE_MODEL_OMISSION,
    CAUSE_TYPE_MISMATCH,
    CAUSE_REGISTRY_GAP,
})

# 원인 → 실패의 **성격**. 사용자 노출 문구와 failure_reason 이 이 값으로 갈린다.
FAILURE_KIND_USER = "user_clarification"
FAILURE_KIND_STRUCTURER = "structurer_failure"
FAILURE_KIND_SYSTEM = "system_failure"
FAILURE_KIND_UNSUPPORTED = "unsupported"
FAILURE_KIND_BY_CAUSE: dict[str, str] = {
    CAUSE_USER_OMISSION: FAILURE_KIND_USER,
    CAUSE_MODEL_OMISSION: FAILURE_KIND_STRUCTURER,
    CAUSE_TYPE_MISMATCH: FAILURE_KIND_STRUCTURER,
    CAUSE_REGISTRY_GAP: FAILURE_KIND_SYSTEM,
}


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
    "entity",        # 대상 엔터티 → canonical entity (EntityResolver)
    "scope",         # 집계 대상 도메인(도메인 어휘)
    "relation",      # 관계·전이·증감 종류(도메인 어휘)
    "temporal",      # 시간 한정어 → TemporalQualifier (temporal_semantics)
    "unit",          # 계수 단위 → 의미 단위 (UnitNormalizer)
    "text",          # 자유 텍스트(라벨·근거)
    "flag",          # bool
    "nodes",         # 하위 노드 목록(LogicalExpression)
    "raw",           # 정규화 대상이 아닌 구조체(이미 검증된 AST 등)
})


@dataclass(frozen=True)
class FieldSpec:
    """노드 필드 하나의 선언. 이 선언 하나가 missing 계산·LLM 스키마·정규화기 dispatch 를 모두 만든다.

    `enum` 은 **도메인과 무관한** 닫힌 집합에만 쓴다(예: 논리 연산자 and/or/not).
    도메인 값 목록은 `vocabulary=` 로 어휘 키만 가리키고, 실제 값은 도메인 플러그인이 준다 —
    새 집계 도메인·새 엔터티는 코어 수정 없이 선언 한 줄로 열린다.
    """

    name: str
    kind: str
    required: bool = True
    description: str = ""
    enum: tuple[str, ...] = ()
    vocabulary: str = ""
    # 시스템이 계산하는 필드(생산자에게 노출하지 않는다 — status/missing 과 같은 규약).
    derived: bool = False

    def __post_init__(self) -> None:
        if self.kind not in VALUE_KINDS:
            raise SemanticPlanError(f"{self.name}: 알 수 없는 값 종류 {self.kind!r}")
        if self.enum and self.vocabulary:
            raise SemanticPlanError(
                f"{self.name}: enum 과 vocabulary 를 함께 쓸 수 없다(값의 소유자가 둘이 된다)"
            )

    def allowed_values(self) -> tuple[str, ...]:
        """이 필드가 받을 수 있는 값(도메인 어휘 포함). 빈 튜플이면 제약 없음."""
        if self.enum:
            return self.enum
        if self.vocabulary:
            return semantic_domain_binding.vocabulary(self.vocabulary)
        return ()


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

    def missing_cause(self, name: str) -> str:
        """이 필드가 비어 있는 **원인**. 값이 아니라 선언에서 계산한다.

        어휘가 선언됐는데 실행 값 목록이 비어 있으면 생산자를 탓할 수 없다 — 고를 것이
        애초에 없었다(실측: `profile_metric_id` 가 값 1개뿐이라 그 하나를 강제로 고르게 됐다).
        그 밖의 결핍은 구간을 청구해 놓고 채우지 못한 것이므로 생산자 누락이다.
        """
        spec = type(self).field_spec(name)
        if spec is None:
            return CAUSE_MODEL_OMISSION
        if spec.vocabulary and not spec.allowed_values():
            return CAUSE_REGISTRY_GAP
        return CAUSE_MODEL_OMISSION

    def missing_field_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"node_id": self.id, "field": name, "path": f"{self.id}.{name}",
             "node_type": self.TYPE, "source_span": self.source_span,
             "cause": self.missing_cause(name)}
            for name in self.missing_fields()
        )

    def invalid_values(self) -> tuple[tuple[str, Any, tuple[str, ...]], ...]:
        """닫힌 어휘 밖의 값 — 생산자(LLM 등)의 계약 위반이지 '미지원'이 아니다.

        어휘가 선언되지 않은 필드는 제약이 없다(빈 튜플). 선언된 어휘를 벗어난 값만
        (필드, 받은 값, 허용 목록)으로 보고한다.
        """
        offenders: list[tuple[str, Any, tuple[str, ...]]] = []
        for spec in type(self).FIELDS:
            allowed = spec.allowed_values()
            value = self.values.get(spec.name)
            if not allowed or _empty(value) or not isinstance(value, str):
                continue
            if value not in allowed:
                offenders.append((spec.name, value, allowed))
        return tuple(offenders)

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
    """단일 속성/지표 술어 — 주어의 한 지표를 값과 비교한다."""

    TYPE: ClassVar[str] = "predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("subject", "entity", required=True, description="술어의 주어 엔터티"),
        FieldSpec("metric", "metric", required=True, description="지표/속성 canonical 또는 원문 라벨"),
        FieldSpec("operator", "operator", required=True, description="비교 연산자(원문 표현 허용)"),
        FieldSpec("value", "quantity", required=True, description="비교 대상 값"),
        FieldSpec("unit", "unit", required=False, description="수량의 단위"),
        FieldSpec("period", "period", required=False, description="측정 기간"),
        FieldSpec("negated", "flag", required=False, description="부정 술어인가"),
        FieldSpec("state", "relation", required=False, description="상대 상태(날짜 상태 등)"),
        FieldSpec("temporal", "temporal", required=False, derived=True,
                  description="시간 한정어(범용 연산자 — 시스템이 파생한다)"),
    )


@dataclass
class AggregatePredicate(SemanticNode):
    """집계 임계 술어 — 한 도메인의 집계값을 임계와 비교한다."""

    TYPE: ClassVar[str] = "aggregate_predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("scope", "scope", required=True,
                  description="집계 대상 도메인", vocabulary="aggregate_scope"),
        FieldSpec("metric", "metric", required=True, description="집계 지표"),
        FieldSpec("operator", "operator", required=True, description="비교 연산자"),
        FieldSpec("value", "quantity", required=True, description="임계값"),
        FieldSpec("aggregation", "relation", required=False,
                  description="집계 함수", vocabulary="aggregation_function"),
        FieldSpec("unit", "unit", required=False, description="수량 단위"),
        FieldSpec("period", "period", required=False, description="집계 창"),
        FieldSpec("grain", "scope", required=False,
                  description="집계 그레인", vocabulary="aggregate_grain"),
        FieldSpec("qualifier", "text", required=False, description="집계 범위 한정어"),
        FieldSpec("event", "relation", required=False, description="이벤트 canonical(이벤트형 도메인)"),
        FieldSpec("temporal", "temporal", required=False, derived=True,
                  description="시간 한정어(범용 연산자 — 시스템이 파생한다)"),
    )


@dataclass
class MetricComparison(SemanticNode):
    """기간 대 기간 비교.

    **이 노드가 두 기간과 비교 관계를 모두 소유한다** — 그래서 별도 기간 조건의 결핍이
    구조적으로 생길 수 없다(사후 삭제가 필요 없어진 지점).
    """

    TYPE: ClassVar[str] = "metric_comparison"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("metric", "metric", required=True, description="비교 지표"),
        FieldSpec("baseline", "period", required=True, description="기준 기간"),
        FieldSpec("current", "period", required=True, description="비교 기간"),
        FieldSpec("relation", "relation", required=True,
                  description="변화 방향", vocabulary="trend_relation"),
        FieldSpec("threshold", "quantity", required=False, description="변화율 임계값"),
        FieldSpec("threshold_operator", "operator", required=False, description="변화율 비교 연산자"),
        FieldSpec("scope", "scope", required=False,
                  description="집계 도메인", vocabulary="aggregate_scope"),
    )


@dataclass
class RankedSet(SemanticNode):
    """랭킹 집합 — 한 지표로 정렬한 상위/하위 N(또는 N%) 집합."""

    TYPE: ClassVar[str] = "ranked_set"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("entity", "entity", required=True, description="랭킹 대상 엔터티"),
        FieldSpec("metric", "metric", required=True, description="랭킹 기준 지표"),
        FieldSpec("direction", "relation", required=True,
                  description="정렬 방향", vocabulary="rank_direction"),
        FieldSpec("limit", "rank_limit", required=True, description="상위 N/N% 제한"),
        FieldSpec("period", "period", required=False, description="랭킹 계산 창"),
        FieldSpec("qualifier", "text", required=False, description="랭킹 범위 한정어"),
    )


@dataclass
class EntitySetMembership(SemanticNode):
    """파생 집합 소속 — 계산으로 정해지는 집합에 주어가 속하는가.

    랭킹 집합(RankedSet)과 주어를 잇는다 — 대상이 계산 결과이므로 리터럴 대상 조건이
    결핍일 수 없다.
    """

    TYPE: ClassVar[str] = "entity_set_membership"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("member_entity", "entity", required=True, description="소속을 판정할 주어 엔터티"),
        FieldSpec("relation", "relation", required=True, description="집합과의 관계"),
        FieldSpec("ranked_set", "nodes", required=True, description="소속 대상 랭킹 집합(1개)"),
        FieldSpec("negated", "flag", required=False, description="비소속인가"),
        FieldSpec("cardinality", "quantity", required=False, description="교집합 개수 조건"),
        FieldSpec("cardinality_operator", "operator", required=False, description="교집합 개수 연산자"),
    )

    def children(self) -> tuple[SemanticNode, ...]:
        return tuple(self.values.get("ranked_set") or ())


@dataclass
class RelationPredicate(SemanticNode):
    """엔터티 사이/속성 시점의 관계 — 시간 한정어를 갖는 술어의 일반형.

    `relation` 은 도메인 어휘(도메인 플러그인 선언)이고, 그중 시간 축을 갖는 값은
    `temporal_semantics` 의 **범용 연산자**로 정규화돼 `temporal` 필드에 실린다.
    """

    TYPE: ClassVar[str] = "relation_predicate"
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("subject", "entity", required=True, description="주어 엔터티"),
        FieldSpec("attribute", "metric", required=True, description="속성/대상 canonical"),
        FieldSpec("relation", "relation", required=True,
                  description="관계 종류(도메인 어휘)", vocabulary="attribute_relation"),
        FieldSpec("temporal", "temporal", required=False, derived=True,
                  description="시간 한정어(범용 연산자 — relation 에서 파생된다)"),
        FieldSpec("value", "text", required=False, description="시점/보유 판정의 속성 값"),
        FieldSpec("value_comparison", "relation", required=False,
                  description="값 비교(서열)", vocabulary="value_comparison"),
        FieldSpec("from_value", "text", required=False, description="전이 출발 값"),
        FieldSpec("to_value", "text", required=False, description="전이 도착 값"),
        FieldSpec("period", "period", required=False, description="시점 앵커/구간"),
        FieldSpec("months", "quantity", required=False, description="관측 구간 길이"),
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
    # 정규화 계층이 타입을 확정하지 못한 후보(구조화기 실패 — 사용자에게 물을 것이 아니다).
    # 각 항목은 최소 source_span 을 갖고, 그 구간이 재방출 요청의 입력이 된다.
    structurer_issues: list[dict[str, Any]] = field(default_factory=list)
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

    def missing_field_records(self) -> tuple[dict[str, Any], ...]:
        """결핍 + **원인**. 원인 없는 결핍 보고는 전부 사용자 확인 요청이 되어 버린다.

        마지막에 자리표시자 판정을 얹는 이유: 그 구절은 어떤 타입으로도 채울 수 없고
        **오직 사용자만** 값을 줄 수 있다. 구조화기 실패로 뭉개면 답할 수 없는 오류가 된다.
        """
        records = [dict(record) for record in self._raw_missing_records()]
        for record in records:
            omission = semantic_domain_binding.user_omission_reason(
                record.get("source_span") or ""
            )
            if omission:
                record["cause"] = CAUSE_USER_OMISSION
                record["question"] = omission.get("question")
                record["matched"] = omission.get("matched")
        return tuple(records)

    def _raw_missing_records(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        for node in self.walk():
            records.extend(node.missing_field_records())
        for issue in self.structurer_issues:
            records.append({
                "node_id": issue.get("node_id"),
                "field": None,
                "path": issue.get("path") or f"structurer:{issue.get('node_id') or ''}",
                "node_type": issue.get("proposed_type"),
                "source_span": issue.get("source_span"),
                "cause": CAUSE_TYPE_MISMATCH,
                "reason": issue.get("reason"),
            })
        for item in self.uncovered_requirements:
            records.append({
                "node_id": None,
                "field": None,
                "path": f"uncovered:{item.get('source_span')}",
                "node_type": None,
                "source_span": item.get("source_span"),
                "cause": CAUSE_MODEL_OMISSION,
                "reason": item.get("reason"),
            })
        return tuple(records)

    def failure_kind(self) -> str | None:
        """실패의 성격 — 사용자 문구·failure_reason 이 이 값으로 갈린다.

        우선순위는 `derive_status` 와 같은 이유로 '내부 사고 > 미지원 > 사용자'다. 우리 쪽
        사고를 사용자 질문으로 바꾸면 사용자는 답할 수 없는 것을 요구받는다.
        """
        causes = {record.get("cause") for record in self.missing_field_records()}
        # 사용자 정보 누락이 가장 먼저다 — 그것만이 사용자가 실제로 해결할 수 있는 결핍이고,
        # 물어보면 나머지 축들도 함께 다시 해석된다.
        if CAUSE_USER_OMISSION in causes:
            return FAILURE_KIND_USER
        if self.validation_errors:
            return FAILURE_KIND_SYSTEM
        if self.unsupported_operations():
            return FAILURE_KIND_UNSUPPORTED
        if self.conflicts:
            causes.add(CAUSE_TYPE_MISMATCH)
        for cause in (CAUSE_REGISTRY_GAP, CAUSE_TYPE_MISMATCH, CAUSE_MODEL_OMISSION):
            if cause in causes:
                return FAILURE_KIND_BY_CAUSE[cause]
        return None

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
            structurer_issues=self.structurer_issues,
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
            "structurer_issues": copy.deepcopy(self.structurer_issues),
            "missing_fields": list(self.missing_fields()),
            "missing_field_causes": [dict(record) for record in self.missing_field_records()],
            "failure_kind": self.failure_kind(),
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
    structurer_issues: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
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
    if missing_fields or uncovered_requirements or structurer_issues:
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
    "temporal": {
        "type": "object",
        "description": (
            "시간 한정어. {operator, anchor?, interval?, count?, subinterval_unit?}. "
            "operator 는 시스템이 정의한 범용 연산자 식별자다."
        ),
        "properties": {
            "operator": {"type": "string"},
            "anchor": {"type": "object"},
            "interval": {"type": "object"},
            "count": {"type": "number"},
            "subinterval_unit": {"type": "string"},
        },
    },
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
    allowed = spec.allowed_values()
    if allowed:
        schema = {"type": "string", "enum": list(allowed)}
    # 값 **모양** 설명(kind 소유)과 그 필드가 **무엇인지**(FieldSpec 소유)는 둘 다 필요하다.
    # 필드 설명으로 덮어쓰면 모양 안내가 통째로 사라진다 — period 가 그랬고, 그래서 모델은
    # 상대 창을 표현할 방법을 모른 채 '최근 3개월'을 임의의 절대 구간으로 환산해 보냈다
    # (실측 2026-08-02: 같은 어구가 런마다 20260502 / 20260505 / 2026년 5월 로 갈렸다).
    description = spec.description
    shape_hint = str(schema.get("description") or "")
    if shape_hint and description and shape_hint not in description:
        description = f"{description}. {shape_hint}"
    glossary = semantic_domain_binding.vocabulary_glossary(spec.vocabulary) if spec.vocabulary else {}
    if glossary:
        listed = ", ".join(
            f"{value}={glossary[value]}" for value in allowed if value in glossary
        )
        if listed:
            description = f"{description} ({listed})" if description else listed
    if description:
        schema["description"] = description
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


def semantic_node_variant_schema(
    cls: type[SemanticNode], *, node_ref: str = "#/$defs/semanticNode"
) -> dict[str, Any]:
    """노드 타입 하나의 스키마 — **그 타입의 필드만** 담고, 필수 필드는 required 로 선언한다.

    이 두 가지가 핵심이다:
      ① 다른 타입의 필드를 넣지 않는다 → strict 변환이 `additionalProperties:false` 를 붙이므로
         타입 A 의 payload 를 담은 타입 B 노드는 **스키마 단계에서 거부**된다.
      ② 필수 필드를 required 에 넣는다 → strict 변환기는 required 항목만 non-nullable 로 남기므로
         `null` 로 비워 제출하는 길이 막힌다(실측: 전 필드 nullable 이라 전부 null 인 노드가 통과).
    """
    properties: dict[str, Any] = copy.deepcopy(_COMMON_NODE_PROPERTIES)
    properties["type"] = {
        "type": "string",
        "enum": [cls.TYPE],
        "description": semantic_domain_binding.condition_label(cls.TYPE) or (cls.__doc__ or "").strip().splitlines()[0],
    }
    required = ["id", "type", "source_span"]
    for spec in cls.FIELDS:
        if spec.derived:
            # 파생 필드는 생산자에게 노출하지 않는다 — 노출하면 LLM 이 계산값을 지어낸다.
            continue
        properties[spec.name] = _field_schema(spec, node_ref=node_ref)
        if spec.required:
            required.append(spec.name)
    return {
        "type": "object",
        "description": (cls.__doc__ or "").strip().splitlines()[0],
        "properties": properties,
        "required": required,
    }


def node_classes_for(node_types: Sequence[str] | None) -> tuple[type[SemanticNode], ...]:
    """방출 대상 노드 클래스를 이름으로 고른다(없는 이름은 조용히 넘기지 않고 예외).

    오타를 허용하면 "그 타입만 노출했다"는 계약이 빈 목록으로 조용히 무너진다.
    """
    if node_types is None:
        return NODE_CLASSES
    unknown = sorted(set(node_types) - set(NODE_CLASS_BY_TYPE))
    if unknown:
        raise SemanticPlanError(f"알 수 없는 의미 노드 타입: {unknown}")
    return tuple(NODE_CLASS_BY_TYPE[name] for name in node_types)


def node_type_is_recursive(cls: type[SemanticNode]) -> bool:
    """자식 노드를 갖는 타입인가 — 노출면을 좁힐 때 `$defs` 참조가 필요한지의 판정."""
    return any(spec.kind == "nodes" for spec in cls.FIELDS)


def semantic_node_json_schema(
    *, node_ref: str = "#/$defs/semanticNode", node_types: Sequence[str] | None = None
) -> dict[str, Any]:
    """노드 선언에서 파생한 **타입 판별 union**(손으로 쓴 두 번째 권위를 만들지 않는다).

    `oneOf` 가 아니라 `anyOf` 다 — OpenAI strict function calling 이 `oneOf` 를 받지 않는다.
    변형이 늘어나는 유일한 방법은 `NODE_CLASSES` 에 클래스를 추가하는 것이다.

    `node_types` 로 노출면을 좁힐 수 있다(부분 노출은 여전히 **선언에서 파생**된다).
    """
    return {
        "description": (
            "의미 노드 하나. **타입을 먼저 고르고 그 타입의 필수 필드를 전부 채운다** — "
            "다른 타입의 필드는 쓸 수 없다. 확신이 없으면 그 조건의 노드를 만들지 마라."
        ),
        "anyOf": [
            semantic_node_variant_schema(cls, node_ref=node_ref)
            for cls in node_classes_for(node_types)
        ],
    }


def semantic_plan_json_schema(*, node_types: Sequence[str] | None = None) -> dict[str, Any]:
    """의미 노드 목록 스키마.

    `node_types` 를 주면 그 타입만 방출할 수 있는 좁은 노출면이 되고, 노드 스키마를
    `$defs/semanticNode` 참조 대신 **인라인**한다 — 좁힌 노출면은 다른 스키마 문서(예:
    canonical audience 대수) 안에 embed 되므로 남의 `$defs` 에 이름을 얹으면 안 된다.
    자식 노드를 갖는 타입은 그 참조 없이 표현할 수 없으므로 좁힌 노출면에서 거부한다.
    """
    if node_types is None:
        items: dict[str, Any] = {"$ref": "#/$defs/semanticNode"}
    else:
        recursive = [cls.TYPE for cls in node_classes_for(node_types) if node_type_is_recursive(cls)]
        if recursive:
            raise SemanticPlanError(
                f"자식 노드를 갖는 타입은 좁힌 노출면에 담을 수 없다: {sorted(recursive)}"
            )
        items = semantic_node_json_schema(node_types=node_types)
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
                "items": items,
            }
        },
        "required": ["nodes"],
    }


def node_requirement_documentation() -> dict[str, dict[str, Any]]:
    """노드 타입별 필수/선택 필드 문서(테스트·프롬프트가 읽는 파생 표)."""
    return {
        cls.TYPE: {
            "required": sorted(spec.name for spec in cls.FIELDS if spec.required),
            "optional": sorted(
                spec.name for spec in cls.FIELDS if not spec.required and not spec.derived
            ),
            "derived": sorted(spec.name for spec in cls.FIELDS if spec.derived),
            "vocabularies": {
                spec.name: list(spec.allowed_values())
                for spec in cls.FIELDS
                if spec.allowed_values()
            },
            "description": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else "",
        }
        for cls in NODE_CLASSES
    }


# 의미 노드 방출 계약. **낱말 목록이 아니라 계약**이므로 여기(선언 옆)가 소유자다 —
# 프롬프트와 검증기가 서로 다른 규칙을 갖게 되면 그 차이가 조용한 실패로 돌아온다.
# 2026-08-02 이전에는 이 규칙들이 프로덕션이 import 하지 않는 모듈에만 있었고, 라이브
# 프롬프트에는 노드 타입 이야기가 한 줄도 없었다(그 결과가 전 요청의 타입 오분류다).
NODE_EMISSION_RULES: tuple[str, ...] = (
    "원문에서 확인되는 조건 하나당 노드 하나. 조건이 여럿이면 노드도 여럿이다.",
    "**타입을 먼저 고르고 그 타입의 필수 필드를 전부 채운다.** 다른 타입의 필드를 채운 노드는"
    " 스키마에서 거부되거나 시스템이 버린다.",
    "모든 노드에 source_span(원문 구절 그대로)과 source_start/source_end 를 붙인다."
    " 원문에 없는 문구를 source_span 으로 쓰지 마라.",
    "확실하지 않은 필드는 비워 두지 말고 **그 노드를 만들지 마라** — 필수 필드를 채울 수 없으면"
    " 그 조건은 노드로 표현할 수 없다는 뜻이다.",
    "기간·시점은 **조건 노드의 period 필드**다. 기간만으로 노드를 만들지 마라"
    " ('최근 1년 동안 2회 이상 구매' 는 노드 하나이고 period 가 그 안에 있다).",
    "'최근 N일/N주/N개월/N년' 은 **상대 창 그대로** 낸다: {type:'relative', value:N, unit:...}."
    " 직접 날짜로 환산해 {from,to} 로 보내지 마라 — 기준일 확정은 시스템 몫이고, 환산본은"
    " 런마다 달라져 같은 문장이 다른 기간이 된다. {type:'absolute'} 는 원문에 연·월·일이"
    " 명시된 구간('2026년 5월', '3월 1일부터')일 때만 쓴다.",
    "원문이 횟수를 말하지 않는 '있다/없다'는 임계 비교가 아니라 **존재·부재**다. 그 의미를"
    " 표현하는 실행 슬롯이 있으면 슬롯으로 내라(규칙 9). 굳이 노드로 낸다면 존재는 '1건 이상',"
    " 부재는 '0건'으로 정확히 적어라 — 원문에 없는 임계값을 지어내면 대상이 달라진다.",
    "기간 대 기간 비교(증가/감소)는 metric_comparison 하나로 표현한다 — baseline 과 current 를"
    " 그 노드가 소유하므로 별도 기간 조건 노드를 만들지 마라.",
    "대상이 계산으로 정해질 때만 entity_set_membership 을 쓰고(예: '가장 많이 팔린 상품 10개를"
    " 구매한 회원'), 그 안의 ranked_set 이 랭킹을 소유한다. 임계값 비교는 여기가 아니다.",
    "조건들의 결합이 AND 가 아니면(OR/부정) logical_expression 으로 감싼다.",
    "실행 슬롯으로 이미 표현한 조건(연령·성별 등 프로필 값)은 노드로 중복 표현하지 마라.",
)


def node_type_guidance(*, node_types: Sequence[str] | None = None) -> str:
    """노드 타입 경계 안내(선언에서 **생성**). 손으로 쓴 두 번째 권위를 만들지 않는다.

    노출면을 좁히면 규칙도 함께 좁힌다. 규칙 선별은 손 큐레이션이 아니라 **파생**이다:
    노출되지 않은 타입 이름을 언급하는 규칙은 지킬 수 없는 지시라서 뺀다(모델에게 존재하지
    않는 선택지를 알려주면 그 타입으로 내려다 스키마에서 거부된다).
    """
    exposed = {cls.TYPE for cls in node_classes_for(node_types)}
    hidden = set(NODE_CLASS_BY_TYPE) - exposed
    rules = [
        rule for rule in NODE_EMISSION_RULES
        if not any(name in rule for name in hidden)
    ]
    lines = ["[의미 노드 방출 규칙]"]
    lines += [f"{index}. {rule}" for index, rule in enumerate(rules, start=1)]
    lines.append("")
    lines.append("[노드 타입과 필수 필드]")
    documentation = node_requirement_documentation()
    for node_type, spec in ((name, documentation[name]) for name in sorted(exposed)):
        label = semantic_domain_binding.condition_label(node_type)
        title = f"- {node_type}" + (f"({label})" if label and label != node_type else "")
        lines.append(f"{title}: 필수 {{{', '.join(spec['required'])}}}")
        if spec["optional"]:
            lines.append(f"    선택 {{{', '.join(spec['optional'])}}}")
        if spec["description"]:
            lines.append(f"    {spec['description']}")
    return "\n".join(lines)


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
    "node_classes_for",
    "node_from_dict",
    "node_requirement_documentation",
    "node_type_is_recursive",
    "plan_from_dict",
    "semantic_node_json_schema",
    "semantic_plan_json_schema",
]
