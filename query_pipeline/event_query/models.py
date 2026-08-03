"""EventQuerySpec — **검증과 해석이 끝난** 실행 가능한 조건.

이 타입이 존재하는 이유 하나: SQL 컴파일러가 받을 수 있는 유일한 조건 표현을 만들기
위해서다. ``audience_requirement`` 는 결핍·모호·미지원을 담을 수 있어야 하고, 실행 IR 은
담아서는 안 된다. 두 요구가 같은 타입 위에 있으면 "검증 전 IR 이 SQL 이 되는 경로"가 항상
열려 있다 — 이 저장소에서 그 경로의 값은 '조용히 다른 오디언스'였다.

사양에 있어서는 안 되는 상태(계약):

    missing / ambiguous  — 타입에 없다(요구 계층 전용 값 상태다)
    해결되지 않은 참조    — 모든 AttributeOperand 가 bindings 에 있어야 한다
    미지원 error         — capability 확인을 통과하지 못하면 사양이 만들어지지 않는다
    축약 표기 잔재        — aggregate_condition 은 정본 비교 노드로 접힌 뒤에만 들어온다
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import Field, model_validator

from query_pipeline.base import StrictModel
from query_pipeline.event_query.expressions import (
    AggregateConditionExpression,
    AggregateFunction,
    AttributeOperand,
    EventExpression,
    attribute_operands,
    walk,
)
from query_pipeline.event_query.receipts import Assumption, ResolutionReceipt


class EventQuerySpecError(Exception):
    """사양 계약 위반. 미해결 상태가 실행 계층으로 새는 유일한 출구를 막는다.

    ``ValueError`` 를 **상속하지 않는** 이유: pydantic 은 검증기가 올린 ``ValueError`` 를
    ``ValidationError`` 로 감싼다. 감싸이면 호출자가 "형태가 틀렸다"와 "미해결이 남았다"를
    구분하지 못하고, 이 리팩터링이 노린 '어느 단계에서 막혔는가'가 사라진다.
    """


class ResolvedBinding(StrictModel):
    """논리 이름 → 확정된 entity/attribute.

    물리 테이블·컬럼은 여기 없다(그것은 컴파일 계층의 SchemaBindings 소유다). 여기 있는 것은
    "이 논리 이름이 어느 entity 의 어느 attribute 인가"의 확정이다.
    """

    logical_name: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    attribute: str | None = None


class QuerySource(StrictModel):
    """이 사양이 어느 요구에서 나왔는가(역추적 단위)."""

    requirement_id: str = Field(min_length=1)
    requirement_version: str = Field(min_length=1)


class CapabilityRequirements(StrictModel):
    """이 사양을 실행하려면 실행 계층이 무엇을 할 줄 알아야 하는가."""

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


class ResolvedMeasure(StrictModel):
    """확정된 출력 지표. ``target=None`` 은 ``count(*)`` 뿐이다."""

    alias: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    function: AggregateFunction
    target: AttributeOperand | None = None
    distinct: bool = False

    @model_validator(mode="after")
    def _target_required(self) -> ResolvedMeasure:
        if self.function is not AggregateFunction.COUNT and self.target is None:
            raise EventQuerySpecError(
                f"measure '{self.alias}' 는 {self.function.value} 대상 속성이 필요합니다"
            )
        return self


class ResolvedSort(StrictModel):
    """정렬 키. ``name`` 은 출력 별칭이거나 논리 속성 이름이다."""

    name: str = Field(min_length=1)
    descending: bool = False


class QueryOutput(StrictModel):
    """무엇을 조회하는가 — 스캔 시작 entity, 그룹 축, 지표, 정렬, 상한.

    사용자 명세에는 없던 필드지만 :class:`~query_pipeline.planning.models.LogicalPlan`
    이 "조회·필터·집계·정렬 순서"를 표현하려면 그 재료가 사양 안에 있어야 한다. 요구
    계층(``AudienceRequirement.output``)에서 확정되어 여기로 넘어온다 — Planner 가
    요구를 다시 읽는 경로를 만들지 않기 위해서다.
    """

    entity: str = Field(min_length=1)
    dimensions: tuple[AttributeOperand, ...] = ()
    measures: tuple[ResolvedMeasure, ...] = ()
    order_by: tuple[ResolvedSort, ...] = ()
    limit: int | None = Field(default=None, ge=1)

    @property
    def is_aggregated(self) -> bool:
        return bool(self.measures)


class EventQuerySpec(StrictModel):
    """실행 가능한 오디언스 조건. LogicalPlanner 의 유일한 입력."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)

    expression: EventExpression
    output: QueryOutput

    bindings: Mapping[str, ResolvedBinding]
    assumptions: tuple[Assumption, ...] = ()
    receipts: tuple[ResolutionReceipt, ...] = ()

    source: QuerySource
    capabilities: CapabilityRequirements

    created_at: datetime

    @model_validator(mode="after")
    def validate_ready_state(self) -> EventQuerySpec:
        """미해결 표식이 하나라도 남아 있으면 사양이 아니다."""
        for node in walk(self.expression):
            if isinstance(node, AggregateConditionExpression):
                raise EventQuerySpecError(
                    "aggregate_condition 축약 표기는 사양에 들어올 수 없습니다"
                    " — canonicalize_expression 으로 비교 노드로 접으십시오"
                )
        unbound = [
            operand.logical_name
            for operand in attribute_operands(self.expression)
            if operand.logical_name not in self.bindings
        ]
        if unbound:
            raise EventQuerySpecError(
                "해결되지 않은 속성 참조가 남았습니다: " + ", ".join(sorted(unbound))
            )
        for logical_name, binding in self.bindings.items():
            if binding.logical_name != logical_name:
                raise EventQuerySpecError(
                    f"바인딩 키와 논리 이름이 다릅니다: {logical_name!r} != {binding.logical_name!r}"
                )
        return self


__all__ = [
    "CapabilityRequirements",
    "EventQuerySpec",
    "EventQuerySpecError",
    "QueryOutput",
    "QuerySource",
    "ResolvedBinding",
    "ResolvedMeasure",
    "ResolvedSort",
]
