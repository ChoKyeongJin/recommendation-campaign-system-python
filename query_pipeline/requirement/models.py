"""AudienceRequirement — 사용자가 **원하는 것**. 실행 가능한 조건이 아니다.

세 가지 계약이 이 모듈의 전부다.

1. 여기서는 결핍(missing)·모호(ambiguous)·추론(inferred)이 **정상 상태**다. 값 상태를
   discriminated union 으로 두어 "값이 있다"와 "값이 확정됐다"를 구분한다.
2. 이 타입은 SQL 컴파일러도 LogicalPlanner 도 받을 수 없다. 두 계층의 시그니처가 각각
   :class:`~query_pipeline.event_query.models.EventQuerySpec` 과
   :class:`~query_pipeline.planning.models.LogicalPlan` 을 요구하므로, 요구를 그대로
   넘기는 호출은 정적 타입 검사에서 실패한다.
3. 요구 표현은 **두 갈래 중 하나**만 쓴다(둘 다 채우면 검증 오류다):

   * ``expression`` — LLM 이 canonical 사건 대수로 제안한 트리(아직 검증 전이므로
     실행 타입이 아니라 :class:`ProposedExpression` 이라는 별도 타입이다)
   * ``constraints`` — 필드/연산자/값(상태 포함) 목록

   같은 요청을 두 모양으로 동시에 들고 가면 어느 쪽이 실행됐는지가 사후에 갈린다. 이
   저장소가 canonical/legacy 이중 표현으로 겪은 사고와 같은 종류라 구조로 막는다.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from query_pipeline.base import StrictModel
from query_pipeline.event_query.expressions import LiteralValue
from query_pipeline.requirement.issues import RequirementIssue


class IntentKind(str, Enum):
    FIND = "find"
    COUNT = "count"
    AGGREGATE = "aggregate"
    COMPARE = "compare"
    MONITOR = "monitor"


class RequirementReference(StrictModel):
    """논리 이름 참조. ``namespace`` 는 entity, ``name`` 은 attribute/지표 이름이다."""

    namespace: str | None = None
    name: str = Field(min_length=1)

    @property
    def logical_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


class RequirementIntent(StrictModel):
    kind: IntentKind
    target: RequirementReference | None = None


class MetricKind(str, Enum):
    """요구 계층이 말할 수 있는 지표.

    :class:`~query_pipeline.event_query.expressions.AggregateFunction` 보다 **넓다** —
    ``median`` 은 실행 계층이 표현하지 못하지만 사용자는 요청할 수 있어야 한다. 어휘에서
    빼면 같은 요청이 '파싱 실패'가 되어 사유(unsupported)가 사라진다.
    """

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    MEDIAN = "median"


class RequirementMetric(StrictModel):
    kind: MetricKind
    target: RequirementReference | None = None
    alias: str | None = None


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class RequirementOrder(StrictModel):
    target: RequirementReference
    direction: SortDirection


class RequirementOutput(StrictModel):
    dimensions: tuple[RequirementReference, ...] = ()
    metrics: tuple[RequirementMetric, ...] = ()
    order_by: tuple[RequirementOrder, ...] = ()
    limit: int | None = Field(default=None, ge=1)


class RequirementContext(StrictModel):
    locale: str | None = None
    timezone: str | None = None
    audience_id: str | None = None


class SourceMapping(StrictModel):
    """원문 구간 → 요구 트리 경로. 근거 없는 채택을 사후에 찾아내는 좌표계다."""

    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    target_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> SourceMapping:
        if self.source_end <= self.source_start:
            raise ValueError("source span must be non-empty: source_start < source_end")
        return self


# ── 값 상태 ───────────────────────────────────────────────────────────────────────


class ResolvedRequirementValue(StrictModel):
    """사용자가 명시했고 그대로 쓸 수 있는 값."""

    state: Literal["resolved"] = "resolved"
    value: LiteralValue


class InferredRequirementValue(StrictModel):
    """추론된 값(자연어 기간 표현 등). 정규화 전이므로 문자열일 수 있다."""

    state: Literal["inferred"] = "inferred"
    value: LiteralValue
    confidence: float = Field(ge=0, le=1)


class AmbiguousRequirementValue(StrictModel):
    """후보는 있으나 하나로 좁히지 못한 값."""

    state: Literal["ambiguous"] = "ambiguous"
    candidates: tuple[LiteralValue, ...] = Field(min_length=1)


class MissingRequirementValue(StrictModel):
    """값 자체가 없다. ``expected_type`` 은 되묻기 문장을 만드는 근거다."""

    state: Literal["missing"] = "missing"
    expected_type: str = Field(min_length=1)


RequirementValue: TypeAlias = Annotated[
    ResolvedRequirementValue | InferredRequirementValue | AmbiguousRequirementValue | MissingRequirementValue,
    Field(discriminator="state"),
]


class RequirementOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    BETWEEN = "between"
    CONTAINS = "contains"


class RequirementConstraint(StrictModel):
    id: str = Field(min_length=1)
    field: RequirementReference
    operator: RequirementOperator
    value: RequirementValue


class ProposedExpression(StrictModel):
    """LLM 이 제안한 canonical 사건 대수 트리 — **검증 전**.

    ``payload`` 를 실행 타입(EventExpression)으로 두지 않는 이유가 이 리팩터링의 핵심이다.
    이 시점의 트리는 (a) 심볼이 카탈로그에 있는지, (b) 근거 구간이 원문과 맞는지,
    (c) 컴파일러가 표현할 수 있는지 아무것도 확인되지 않았다. 실행 타입으로 선언하면 그
    세 가지가 확인된 것처럼 보이고, 그 착각이 곧 '검증 전 IR 이 SQL 이 되는 경로'다.

    ``JsonValue`` 는 pydantic 이 제공하는 재귀 JSON 타입이다(``Any`` 가 아니다) — 값의
    문법 검증은 :func:`event_ir.condition_from_dict` 의 닫힌 판별자가 단독으로 한다.
    """

    kind: Literal["proposed_expression"] = "proposed_expression"
    payload: JsonValue


class RequirementSource(StrictModel):
    text: str = Field(min_length=1)
    mappings: tuple[SourceMapping, ...] = ()


class AudienceRequirement(StrictModel):
    """사용자 요구의 전부. 실행 가능성은 아직 아무것도 주장하지 않는다."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)

    intent: RequirementIntent
    expression: ProposedExpression | None = None
    constraints: tuple[RequirementConstraint, ...] = ()
    output: RequirementOutput | None = None
    context: RequirementContext | None = None

    issues: tuple[RequirementIssue, ...] = ()

    source: RequirementSource
    created_at: datetime

    @model_validator(mode="after")
    def _single_interpretation(self) -> AudienceRequirement:
        if self.expression is not None and self.constraints:
            raise ValueError(
                "요구 표현은 expression 과 constraints 중 하나만 씁니다"
                " — 같은 절을 두 모양으로 들고 가면 무엇이 실행됐는지가 갈립니다"
            )
        return self

    @property
    def blocking_issues(self) -> tuple[RequirementIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocks_execution)


__all__ = [
    "AmbiguousRequirementValue",
    "AudienceRequirement",
    "InferredRequirementValue",
    "IntentKind",
    "MetricKind",
    "MissingRequirementValue",
    "ProposedExpression",
    "RequirementConstraint",
    "RequirementContext",
    "RequirementIntent",
    "RequirementMetric",
    "RequirementOperator",
    "RequirementOrder",
    "RequirementOutput",
    "RequirementReference",
    "RequirementSource",
    "RequirementValue",
    "ResolvedRequirementValue",
    "SortDirection",
    "SourceMapping",
]
