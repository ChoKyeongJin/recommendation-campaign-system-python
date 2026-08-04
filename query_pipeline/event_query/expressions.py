"""실행 가능한 사건 표현식(Event Expression) — **검증을 통과한 뒤에만** 존재하는 타입.

`audience_requirement.expression` 과 이 타입의 결정적 차이는 모양이 아니라 **누가 만들 수
있는가**다. 여기 있는 노드는 :class:`~query_pipeline.requirement.models.AudienceRequirement`
에서 곧바로 만들어질 수 없고, :class:`~query_pipeline.requirement.resolver.RequirementResolver`
가 심볼 해결·capability 확인을 마친 뒤에만 생성된다. 그래서 SQL 컴파일러의 입력에
'미해결'이 섞일 여지가 타입 수준에서 없다.

물리 스키마는 여기 없다. 노드는 논리 **entity/attribute** 만 말하고(``payment_events``,
``customer_id``), 그것이 어느 테이블/컬럼인지는 컴파일 계층의
:class:`~query_pipeline.compiler.models.SchemaBindings` 가 소유한다.

이 대수는 저장소의 기존 :mod:`event_ir` 대수를 **그대로 미러링**한다(축소가 아니다).
비교/존재/시간창/시간관계/부정/논리결합 + 관계 대수(source·filter·join·group·project·
summarize·order·limit) + 스칼라(literal·attribute·arithmetic·aggregate) 전부가 여기 있다.
event_ir 로의 왕복은 :mod:`query_pipeline.event_query.event_ir_bridge` 가 담당하고,
그 왕복은 계약 테스트가 노드 타입 전수로 강제한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from query_pipeline.base import StrictModel

# ── 값 ────────────────────────────────────────────────────────────────────────────

# ``None`` 은 스칼라 값이 아니다. 기존 :class:`event_ir.Literal` 이 None 을 거부하고,
# 이 저장소는 '값 미기입(NULL)'과 '0'을 서로 다른 노드로 구분한다 — 리터럴 None 을 허용하면
# 그 구분이 리터럴 하나로 뭉개진다.
ScalarValue: TypeAlias = str | int | float | bool
LiteralValue: TypeAlias = ScalarValue | tuple[ScalarValue, ...]


class ComparisonOperator(str, Enum):
    """비교 연산자.

    ``IN``/``BETWEEN``/``CONTAINS`` 는 요구 계층에서 흔히 나오지만 사건 IR 컴파일러가
    표현하지 못한다. 여기서 **말할 수는 있게** 두는 이유는 그래야 capability 검사와
    컴파일러가 "지원하지 않는다"고 명시적으로 실패할 수 있기 때문이다 — 어휘에서 빼면
    같은 요청이 파싱 오류로 바뀌어 사유가 사라진다.
    """

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    BETWEEN = "between"
    CONTAINS = "contains"


class ArithmeticOperator(str, Enum):
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class AggregateFunction(str, Enum):
    """실행 가능한 집계. 요구 계층의 :class:`MetricKind` 보다 **좁다**(median 이 없다)."""

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class RelationCorrelation(str, Enum):
    """관계가 주체(회원)에 상관되는가, 전역인가."""

    SUBJECT = "subject"
    NONE = "none"


class JoinKind(str, Enum):
    INNER = "inner"
    SEMI = "semi"
    ANTI = "anti"


class WindowUnit(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class DurationUnit(str, Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class TemporalOperator(str, Enum):
    WITHIN_AFTER = "within_after"
    WITHIN_BEFORE = "within_before"


class EventSelector(str, Enum):
    FIRST = "first"
    LAST = "last"
    ANY = "any"
    SUBSEQUENT = "subsequent"


# ── 근거 ──────────────────────────────────────────────────────────────────────────


class SourceEvidence(StrictModel):
    """이 원자 조건이 원문 어디에서 나왔는지(반개구간 [start, end))."""

    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> SourceEvidence:
        if self.end <= self.start:
            raise ValueError("evidence span must be non-empty: start < end")
        return self


# ── 시간 ──────────────────────────────────────────────────────────────────────────


class AbsoluteWindow(StrictModel):
    """달력상 확정된 반개구간 [start, end_exclusive)."""

    kind: Literal["interval"] = "interval"
    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def _non_empty(self) -> AbsoluteWindow:
        if self.start >= self.end_exclusive:
            raise ValueError(f"empty interval: {self.start} >= {self.end_exclusive}")
        return self


class RollingWindow(StrictModel):
    """기준 시점에서 거슬러 세는 **길이**('최근 30일')."""

    kind: Literal["rolling"] = "rolling"
    value: int = Field(ge=1)
    unit: WindowUnit


class RelativeWindow(StrictModel):
    """과거의 한 **시점**이 속한 달력 칸('3개월 전' = 그 달 전체)."""

    kind: Literal["relative"] = "relative"
    value: int = Field(ge=1)
    unit: WindowUnit
    direction: Literal["past"] = "past"


TimeWindow: TypeAlias = Annotated[
    AbsoluteWindow | RollingWindow | RelativeWindow,
    Field(discriminator="kind"),
]


class DurationSpec(StrictModel):
    """기간의 길이(창이 아니다). 시간 관계 연산자의 폭."""

    kind: Literal["duration"] = "duration"
    value: int = Field(ge=1)
    unit: DurationUnit


# ── 스칼라(피연산자) ──────────────────────────────────────────────────────────────


class LiteralOperand(StrictModel):
    kind: Literal["literal"] = "literal"
    value: LiteralValue


class AttributeOperand(StrictModel):
    """논리 속성 참조. 테이블·컬럼 이름을 **알지 못한다**."""

    kind: Literal["attribute"] = "attribute"
    entity: str = Field(min_length=1)
    attribute: str = Field(min_length=1)

    @property
    def logical_name(self) -> str:
        """바인딩 사전의 키(``<entity>.<attribute>``)."""
        return f"{self.entity}.{self.attribute}"


class ArithmeticOperand(StrictModel):
    kind: Literal["arithmetic"] = "arithmetic"
    operator: ArithmeticOperator
    left: EventOperand
    right: EventOperand


class AggregateOperand(StrictModel):
    """관계에 대한 집계 스칼라. ``expression=None`` 은 ``count(*)`` 뿐이다."""

    kind: Literal["aggregate"] = "aggregate"
    function: AggregateFunction
    relation: EventRelation
    expression: EventOperand | None = None
    distinct: bool = False

    @model_validator(mode="after")
    def _expression_required(self) -> AggregateOperand:
        if self.function is not AggregateFunction.COUNT and self.expression is None:
            raise ValueError(f"aggregate '{self.function.value}' needs an expression")
        return self


EventOperand: TypeAlias = Annotated[
    LiteralOperand | AttributeOperand | ArithmeticOperand | AggregateOperand,
    Field(discriminator="kind"),
]


class NamedOperand(StrictModel):
    """관계 지역 출력 별칭(카탈로그 필드 id 가 아니다)."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expression: EventOperand


class NamedMeasure(StrictModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    function: AggregateFunction
    expression: EventOperand | None = None
    distinct: bool = False

    @model_validator(mode="after")
    def _expression_required(self) -> NamedMeasure:
        if self.function is not AggregateFunction.COUNT and self.expression is None:
            raise ValueError(f"measure '{self.function.value}' needs an expression")
        return self


class RelationSortKey(StrictModel):
    """직전 관계가 만든 출력 별칭 또는 보존된 카탈로그 필드 id 로 정렬한다."""

    name: str = Field(min_length=1)
    direction: SortDirection = SortDirection.ASC


# ── 관계 ──────────────────────────────────────────────────────────────────────────


class EntityRelation(StrictModel):
    kind: Literal["entity"] = "entity"
    entity: str = Field(min_length=1)
    correlation: RelationCorrelation = RelationCorrelation.SUBJECT


class FilteredRelation(StrictModel):
    kind: Literal["filter"] = "filter"
    relation: EventRelation
    where: EventExpression


class JoinedRelation(StrictModel):
    kind: Literal["join"] = "join"
    left: EventRelation
    right: EventRelation
    on: ComparisonExpression
    join: JoinKind = JoinKind.INNER


class GroupedRelation(StrictModel):
    kind: Literal["group"] = "group"
    relation: EventRelation
    keys: tuple[AttributeOperand, ...] = Field(min_length=1)


class ProjectedRelation(StrictModel):
    kind: Literal["project"] = "project"
    relation: EventRelation
    items: tuple[NamedOperand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> ProjectedRelation:
        names = [item.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("project output names must be unique")
        return self


class SummarizedRelation(StrictModel):
    kind: Literal["summarize"] = "summarize"
    relation: EventRelation
    keys: tuple[NamedOperand, ...] = ()
    measures: tuple[NamedMeasure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> SummarizedRelation:
        names = [key.name for key in self.keys] + [
            measure.name for measure in self.measures
        ]
        if len(names) != len(set(names)):
            raise ValueError("summarize output names must be unique")
        return self


class OrderedRelation(StrictModel):
    kind: Literal["order"] = "order"
    relation: EventRelation
    keys: tuple[RelationSortKey, ...] = Field(min_length=1)


class LimitedRelation(StrictModel):
    kind: Literal["limit"] = "limit"
    relation: EventRelation
    count: int | None = Field(default=None, ge=1)
    percent: Decimal | None = Field(default=None, gt=0, lt=100)

    @model_validator(mode="after")
    def _exactly_one_limit_unit(self) -> LimitedRelation:
        if (self.count is None) == (self.percent is None):
            raise ValueError("limit requires exactly one of count or percent")
        return self


EventRelation: TypeAlias = Annotated[
    EntityRelation | FilteredRelation | JoinedRelation | GroupedRelation | ProjectedRelation | SummarizedRelation | OrderedRelation | LimitedRelation,
    Field(discriminator="kind"),
]


# ── 조건 ──────────────────────────────────────────────────────────────────────────


class ComparisonExpression(StrictModel):
    kind: Literal["comparison"] = "comparison"
    operator: ComparisonOperator
    left: EventOperand
    right: EventOperand
    evidence: SourceEvidence | None = None


class ExistsExpression(StrictModel):
    """관계에 행이 하나라도 있는가. 부재는 :class:`NotExpression` 으로 감싼다."""

    kind: Literal["exists"] = "exists"
    relation: EventRelation
    evidence: SourceEvidence | None = None


class TimeWindowExpression(StrictModel):
    """속성이 창 안에 드는가. 두 비교로 풀 수도 있지만 '하나의 시간 표현'이라는 사실이
    시간 소실 검출과 근거 계산에 필요해 노드로 둔다."""

    kind: Literal["time_filter"] = "time_filter"
    attribute: AttributeOperand
    window: TimeWindow


class EventOccurrence(StrictModel):
    """시간 관계의 피연산자 — 어느 entity 의 어느 발생 시점인가."""

    kind: Literal["occurrence"] = "occurrence"
    entity: str = Field(min_length=1)
    selector: EventSelector = EventSelector.ANY


class TemporalRelationExpression(StrictModel):
    """두 사건 발생 시점 사이의 관계('A 후 D 이내 B')."""

    kind: Literal["temporal_relation"] = "temporal_relation"
    operator: TemporalOperator
    left: EventOccurrence
    right: EventOccurrence
    duration: DurationSpec
    evidence: SourceEvidence | None = None


class NotExpression(StrictModel):
    kind: Literal["not"] = "not"
    expression: EventExpression


class AndExpression(StrictModel):
    kind: Literal["and"] = "and"
    expressions: tuple[EventExpression, ...] = Field(min_length=1)


class OrExpression(StrictModel):
    kind: Literal["or"] = "or"
    expressions: tuple[EventExpression, ...] = Field(min_length=1)


class AggregateDefinition(StrictModel):
    function: AggregateFunction
    target: EventOperand | None = None
    relation: EventRelation | None = None
    distinct: bool = False

    @model_validator(mode="after")
    def _target_required(self) -> AggregateDefinition:
        if self.function is not AggregateFunction.COUNT and self.target is None:
            raise ValueError(f"aggregate '{self.function.value}' needs a target")
        return self


class AggregateConditionExpression(StrictModel):
    """집계 결과에 대한 임계 비교의 **축약 표기**.

    같은 뜻을 :class:`ComparisonExpression` (좌변이 :class:`AggregateOperand`) 로도 쓸 수
    있다. 두 표기를 실행 단계까지 함께 들고 가면 같은 절이 두 모양으로 존재하고, 이
    저장소가 반복해서 다친 '이중 해석'이 된다 — 그래서
    :func:`canonicalize_expression` 이 사양 생성 시점에 비교 노드로 **접는다**.
    :class:`~query_pipeline.event_query.models.EventQuerySpec` 안에는 이 노드가 남지 않는다.
    """

    kind: Literal["aggregate_condition"] = "aggregate_condition"
    aggregate: AggregateDefinition
    operator: ComparisonOperator
    value: LiteralOperand
    evidence: SourceEvidence | None = None


EventExpression: TypeAlias = Annotated[
    ComparisonExpression | ExistsExpression | TimeWindowExpression | TemporalRelationExpression | NotExpression | AndExpression | OrExpression | AggregateConditionExpression,
    Field(discriminator="kind"),
]


for _model in (
    ArithmeticOperand,
    AggregateOperand,
    NamedOperand,
    NamedMeasure,
    FilteredRelation,
    JoinedRelation,
    GroupedRelation,
    ProjectedRelation,
    SummarizedRelation,
    OrderedRelation,
    LimitedRelation,
    ComparisonExpression,
    ExistsExpression,
    NotExpression,
    AndExpression,
    OrExpression,
    AggregateDefinition,
    AggregateConditionExpression,
):
    _model.model_rebuild()


# ── 파생 뷰(표현 위를 걷는 유일한 구현) ──────────────────────────────────────────

_ATOM_TYPES: tuple[type[StrictModel], ...] = (
    ComparisonExpression,
    ExistsExpression,
    TemporalRelationExpression,
    AggregateConditionExpression,
)


def canonicalize_expression(expression: EventExpression) -> EventExpression:
    """축약 표기를 정본 노드로 접는다(현재는 aggregate_condition → comparison 하나)."""
    if isinstance(expression, AggregateConditionExpression):
        definition = expression.aggregate
        relation = definition.relation
        if relation is None:
            raise ValueError(
                "aggregate_condition needs a relation before it can be executed"
            )
        return ComparisonExpression(
            operator=expression.operator,
            left=AggregateOperand(
                function=definition.function,
                relation=relation,
                expression=definition.target,
                distinct=definition.distinct,
            ),
            right=expression.value,
            evidence=expression.evidence,
        )
    if isinstance(expression, NotExpression):
        return NotExpression(expression=canonicalize_expression(expression.expression))
    if isinstance(expression, AndExpression):
        return AndExpression(
            expressions=tuple(canonicalize_expression(item) for item in expression.expressions)
        )
    if isinstance(expression, OrExpression):
        return OrExpression(
            expressions=tuple(canonicalize_expression(item) for item in expression.expressions)
        )
    return expression


def walk(node: object) -> list[StrictModel]:
    """표현/관계/스칼라 트리의 모든 노드(자기 자신 포함, 선순회).

    필드 순회에 ``dict(model)`` 을 쓰지 않는다: ``Order``/``Group``/``Summarize`` 노드에는
    ``keys`` 라는 **필드**가 있고, ``dict()`` 는 그 이름을 매핑 프로토콜의 ``keys()`` 로
    호출한다(TypeError). 선언된 필드 이름으로 도는 것이 유일하게 안전한 순회다.
    """
    found: list[StrictModel] = []
    if isinstance(node, StrictModel):
        found.append(node)
        for name in type(node).model_fields:
            found.extend(walk(getattr(node, name)))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(walk(item))
    return found


def attribute_operands(expression: EventExpression) -> tuple[AttributeOperand, ...]:
    """표현이 참조하는 모든 논리 속성(중복 제거, 논리명 순)."""
    seen: dict[str, AttributeOperand] = {}
    for node in walk(expression):
        if isinstance(node, AttributeOperand):
            seen.setdefault(node.logical_name, node)
    return tuple(seen[name] for name in sorted(seen))


def entities(expression: EventExpression) -> tuple[str, ...]:
    """표현이 참조하는 모든 논리 entity(속성·관계·사건 시점 전부)."""
    names: set[str] = set()
    for node in walk(expression):
        if isinstance(node, (AttributeOperand, EntityRelation, EventOccurrence)):
            names.add(node.entity)
    return tuple(sorted(names))


def atoms(expression: EventExpression) -> tuple[EventExpression, ...]:
    """원자 조건(결합자가 아닌 노드) — 근거·검증의 단위."""
    return tuple(node for node in walk(expression) if isinstance(node, _ATOM_TYPES))  # type: ignore[misc]


def node_kinds(expression: EventExpression) -> frozenset[str]:
    """표현에 등장한 노드 종류 집합(capability 요구 도출에 쓴다)."""
    kinds: set[str] = set()
    for node in walk(expression):
        kind = getattr(node, "kind", None)
        if isinstance(kind, str):
            kinds.add(kind)
    return frozenset(kinds)


__all__ = [
    "AbsoluteWindow",
    "AggregateConditionExpression",
    "AggregateDefinition",
    "AggregateFunction",
    "AggregateOperand",
    "AndExpression",
    "ArithmeticOperand",
    "ArithmeticOperator",
    "AttributeOperand",
    "ComparisonExpression",
    "ComparisonOperator",
    "DurationSpec",
    "DurationUnit",
    "EntityRelation",
    "EventExpression",
    "EventOccurrence",
    "EventOperand",
    "EventRelation",
    "EventSelector",
    "ExistsExpression",
    "FilteredRelation",
    "GroupedRelation",
    "JoinKind",
    "JoinedRelation",
    "LimitedRelation",
    "LiteralOperand",
    "LiteralValue",
    "NamedMeasure",
    "NamedOperand",
    "NotExpression",
    "OrExpression",
    "OrderedRelation",
    "ProjectedRelation",
    "RelationCorrelation",
    "RelationSortKey",
    "RelativeWindow",
    "RollingWindow",
    "ScalarValue",
    "SortDirection",
    "SourceEvidence",
    "SummarizedRelation",
    "TemporalOperator",
    "TemporalRelationExpression",
    "TimeWindow",
    "TimeWindowExpression",
    "WindowUnit",
    "atoms",
    "attribute_operands",
    "canonicalize_expression",
    "entities",
    "node_kinds",
    "walk",
]
