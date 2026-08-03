"""LogicalPlanner 계약 — 단계 순서와 WHERE/HAVING 귀속."""

from __future__ import annotations

from datetime import UTC, date, datetime

from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AggregateFunction,
    AggregateOperand,
    AndExpression,
    AttributeOperand,
    ComparisonExpression,
    ComparisonOperator,
    EntityRelation,
    LiteralOperand,
    TimeWindowExpression,
)
from query_pipeline.event_query.models import (
    CapabilityRequirements,
    EventQuerySpec,
    QueryOutput,
    QuerySource,
    ResolvedBinding,
    ResolvedMeasure,
    ResolvedSort,
)
from query_pipeline.planning.logical_planner import (
    AudienceLogicalPlanner,
    relation_root_entity,
    split_predicates,
)
from query_pipeline.planning.models import (
    LogicalAggregate,
    LogicalFilter,
    LogicalLimit,
    LogicalProject,
    LogicalScan,
    LogicalSort,
    plan_stages,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
EVENT_TYPE = AttributeOperand(entity="payment_events", attribute="event_type")
CUSTOMER = AttributeOperand(entity="payment_events", attribute="customer_id")
OCCURRED_AT = AttributeOperand(entity="payment_events", attribute="occurred_at")


def _spec(
    expression: object,
    *,
    output: QueryOutput,
    bindings: tuple[AttributeOperand, ...] = (),
) -> EventQuerySpec:
    return EventQuerySpec(
        id="spec-1",
        version="1",
        expression=expression,  # type: ignore[arg-type]
        output=output,
        bindings={
            operand.logical_name: ResolvedBinding(
                logical_name=operand.logical_name,
                entity=operand.entity,
                attribute=operand.attribute,
            )
            for operand in bindings
        },
        source=QuerySource(requirement_id="req-1", requirement_version="1"),
        capabilities=CapabilityRequirements(),
        created_at=NOW,
    )


def _failure_count_condition() -> ComparisonExpression:
    return ComparisonExpression(
        operator=ComparisonOperator.GTE,
        left=AggregateOperand(
            function=AggregateFunction.COUNT,
            relation=EntityRelation(entity="payment_events"),
        ),
        right=LiteralOperand(value=5),
    )


def test_event_query_spec_creates_logical_plan() -> None:
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=EVENT_TYPE,
        right=LiteralOperand(value="payment.failed"),
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(
            expression,
            output=QueryOutput(entity="payment_events"),
            bindings=(EVENT_TYPE,),
        )
    )
    stages = plan_stages(plan)
    assert [type(stage) for stage in stages] == [LogicalScan, LogicalFilter]
    assert stages[0].entity == "payment_events"


def test_filter_is_created_before_aggregate() -> None:
    expression = AndExpression(
        expressions=(
            ComparisonExpression(
                operator=ComparisonOperator.EQ,
                left=EVENT_TYPE,
                right=LiteralOperand(value="payment.failed"),
            ),
            TimeWindowExpression(
                attribute=OCCURRED_AT,
                window=AbsoluteWindow(
                    start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
                ),
            ),
            _failure_count_condition(),
        )
    )
    output = QueryOutput(
        entity="payment_events",
        dimensions=(CUSTOMER,),
        measures=(
            ResolvedMeasure(alias="failure_count", function=AggregateFunction.COUNT),
        ),
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(expression, output=output, bindings=(EVENT_TYPE, OCCURRED_AT, CUSTOMER))
    )
    stages = plan_stages(plan)
    assert [type(stage) for stage in stages] == [
        LogicalScan,
        LogicalFilter,
        LogicalAggregate,
        LogicalProject,
    ]
    row_filter = stages[1]
    assert isinstance(row_filter, LogicalFilter)
    # 행 술어에는 집계가 섞이지 않는다 — 섞이면 행 필터가 집계 뒤로 밀린다.
    assert isinstance(row_filter.predicate, AndExpression)
    assert len(row_filter.predicate.expressions) == 2


def test_having_condition_is_preserved() -> None:
    expression = AndExpression(
        expressions=(
            ComparisonExpression(
                operator=ComparisonOperator.EQ,
                left=EVENT_TYPE,
                right=LiteralOperand(value="payment.failed"),
            ),
            _failure_count_condition(),
        )
    )
    output = QueryOutput(
        entity="payment_events",
        dimensions=(CUSTOMER,),
        measures=(
            ResolvedMeasure(alias="failure_count", function=AggregateFunction.COUNT),
        ),
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(expression, output=output, bindings=(EVENT_TYPE, CUSTOMER))
    )
    aggregate = next(
        stage for stage in plan_stages(plan) if isinstance(stage, LogicalAggregate)
    )
    assert aggregate.having is not None
    assert isinstance(aggregate.having, ComparisonExpression)
    assert aggregate.having.operator is ComparisonOperator.GTE
    assert aggregate.group_by == (CUSTOMER,)


def test_sort_and_limit_follow_aggregate() -> None:
    amount = AttributeOperand(entity="orders", attribute="amount")
    customer = AttributeOperand(entity="orders", attribute="customer_id")
    output = QueryOutput(
        entity="orders",
        dimensions=(customer,),
        measures=(
            ResolvedMeasure(
                alias="order_amount", function=AggregateFunction.SUM, target=amount
            ),
        ),
        order_by=(ResolvedSort(name="order_amount", descending=True),),
        limit=10,
    )
    expression = TimeWindowExpression(
        attribute=AttributeOperand(entity="orders", attribute="ordered_at"),
        window=AbsoluteWindow(start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1)),
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(
            expression,
            output=output,
            bindings=(customer, amount, AttributeOperand(entity="orders", attribute="ordered_at")),
        )
    )
    assert [type(stage) for stage in plan_stages(plan)] == [
        LogicalScan,
        LogicalFilter,
        LogicalAggregate,
        LogicalSort,
        LogicalLimit,
        LogicalProject,
    ]


def test_correlated_aggregate_stays_a_row_predicate() -> None:
    """회원별 상관 집계는 HAVING 이 아니다 — 스캔 관계를 접지 않기 때문이다."""
    condition = ComparisonExpression(
        operator=ComparisonOperator.GTE,
        left=AggregateOperand(
            function=AggregateFunction.COUNT,
            relation=EntityRelation(entity="campaign_contact_success"),
        ),
        right=LiteralOperand(value=3),
    )
    row, aggregate = split_predicates(condition, "subject")
    assert row == (condition,)
    assert aggregate == ()

    # 같은 조건이라도 스캔 대상이 그 관계면 집계 술어다.
    row, aggregate = split_predicates(condition, "campaign_contact_success")
    assert row == ()
    assert aggregate == (condition,)


def test_relation_root_entity_unwraps_derived_relations() -> None:
    from query_pipeline.event_query.expressions import FilteredRelation

    relation = FilteredRelation(
        relation=EntityRelation(entity="purchase"),
        where=ComparisonExpression(
            operator=ComparisonOperator.EQ,
            left=AttributeOperand(entity="purchase", attribute="amount"),
            right=LiteralOperand(value=1),
        ),
    )
    assert relation_root_entity(relation) == "purchase"
