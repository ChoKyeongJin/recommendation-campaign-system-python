from aggregation_ast import (
    Aggregate, AggregateCondition, AggregateExpression, AttributeCondition,
    ComputedAggregateCondition, DerivedMetricCondition, Period, Scope,
    compile_condition, validate_condition,
)


def test_conditional_count_ast_compiles_with_bound_values():
    condition = AggregateCondition(
        Scope("customer", "order", ("customer_id",)),
        Aggregate("COUNT", "order", event_filter=AttributeCondition("order.status", "EQ", "COMPLETED")),
        "GTE", 5,
    )
    result = compile_condition(condition, "tsql")
    assert result.status == "ok"
    assert result.sql == "COUNT(CASE WHEN order.status = :filter_0 THEN 1 END) >= :threshold_1"
    assert result.params == {"filter_0": "COMPLETED", "threshold_1": 5}


def test_count_distinct_is_not_reduced_to_plain_count():
    condition = AggregateCondition(
        Scope("customer", "purchase", ("customer_id",)),
        Aggregate("COUNT_DISTINCT", "purchase", "product_id", distinct=True),
        "GTE", 10,
    )
    result = compile_condition(condition, "postgres")
    assert result.sql == "COUNT(DISTINCT product_id) >= :threshold_0"


def test_computed_ratio_has_safe_zero_division():
    expression = AggregateExpression(
        "DIVIDE", Aggregate("SUM", "refund", "amount", field_type="decimal"),
        Aggregate("SUM", "purchase", "amount", field_type="decimal"), zero_division="NULL",
    )
    result = compile_condition(ComputedAggregateCondition(expression, "LTE", .1), "postgres")
    assert result.status == "ok"
    assert "NULLIF(SUM(amount), 0)" in result.sql
    assert result.params == {"threshold_0": .1}


def test_division_without_policy_fails_before_sql_generation():
    expression = AggregateExpression("DIVIDE", Aggregate("COUNT", "a"), Aggregate("COUNT", "b"))
    issues = validate_condition(ComputedAggregateCondition(expression, "GTE", .2))
    assert "zero_division_policy_required" in {issue.code for issue in issues}


def test_type_and_dialect_validation_are_structured():
    invalid_type = AggregateCondition(
        Scope("customer", "purchase", ("customer_id",)),
        Aggregate("SUM", "purchase", "purchased_at", field_type="date"), "GTE", 1,
    )
    assert compile_condition(invalid_type).status == "validation_error"

    unsupported = AggregateCondition(
        Scope("customer", "purchase", ("customer_id",)),
        Aggregate("MODE", "purchase", "product_id"), "EQ", "A",
    )
    assert compile_condition(unsupported, "tsql").status == "unsupported_aggregation"


def test_period_belongs_to_aggregate_event_scope():
    condition = AggregateCondition(
        Scope("customer", "customer", ("customer_id",)),
        Aggregate("COUNT", "purchase", period=Period("2026-01-01", "2026-04-01")), "GTE", 5,
    )
    assert "period_scope_mismatch" in {i.code for i in validate_condition(condition)}


def test_unregistered_derived_metric_is_not_guessed():
    result = compile_condition(DerivedMetricCondition("UNKNOWN_METRIC", "GTE", 30))
    assert result.status == "unsupported_metric"
    assert result.sql is None
