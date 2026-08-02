"""Generic relation algebra contracts for ranking and audience complements."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import aggregate_semantics
import event_compiler
import event_ir
import graph_rag
from sql_dialect import get_dialect


def _ranking_registry() -> dict[str, event_compiler.EventSpec]:
    return event_compiler.resolve_registry({
        "member_purchase": event_compiler.EventSpec(
            table="MEMBER_PURCHASE",
            alias="MP",
            subject_key="MEMBER_ID",
            event_subject_key="MEMBER_ID",
            time_column="PURCHASED_AT",
            time_format="date",
        ),
        "global_sale": event_compiler.EventSpec(
            table="GLOBAL_SALE",
            alias="GS",
            subject_key="MEMBER_ID",
            event_subject_key="MEMBER_ID",
            time_column="SOLD_AT",
            time_format="date",
        ),
    })


def _ranking_fields(
    registry: dict[str, event_compiler.EventSpec],
) -> dict[str, event_compiler.FieldSpec]:
    return event_compiler.resolve_fields(registry, {
        "member_purchase.product_id": event_compiler.FieldSpec(
            source="member_purchase", column="PRODUCT_ID", data_type="string"
        ),
        "global_sale.product_id": event_compiler.FieldSpec(
            source="global_sale", column="PRODUCT_ID", data_type="string"
        ),
        "global_sale.quantity": event_compiler.FieldSpec(
            source="global_sale", column="QUANTITY", data_type="number"
        ),
    })


def _top_products() -> event_ir.Relation:
    sales_2019 = event_ir.Filter(
        relation=event_ir.Source(name="global_sale", correlation="none"),
        where=event_ir.TimeFilter(
            field=event_ir.FieldRef(name="global_sale.occurred_at"),
            window=event_ir.AbsoluteInterval(
                start=date(2019, 1, 1), end_exclusive=date(2020, 1, 1)
            ),
        ),
    )
    totals = event_ir.Summarize(
        relation=sales_2019,
        keys=(
            event_ir.NamedExpression(
                name="product_id",
                expression=event_ir.FieldRef(name="global_sale.product_id"),
            ),
        ),
        measures=(
            event_ir.NamedMeasure(
                name="sold_quantity",
                function="sum",
                expression=event_ir.FieldRef(name="global_sale.quantity"),
            ),
        ),
    )
    ranked = event_ir.Order(
        relation=totals,
        keys=(
            event_ir.SortKey(name="sold_quantity", direction="desc"),
            event_ir.SortKey(name="product_id", direction="asc"),
        ),
    )
    return event_ir.Limit(relation=ranked, count=10)


def _ranking_expression() -> event_ir.Condition:
    return event_ir.Exists(
        relation=event_ir.Join(
            left=event_ir.Source(name="member_purchase"),
            right=_top_products(),
            kind="semi",
            on=event_ir.Comparison(
                operator="=",
                left=event_ir.FieldRef(name="member_purchase.product_id"),
                right=event_ir.FieldRef(name="global_sale.product_id"),
            ),
        )
    )


def test_ranking_relation_round_trips_and_walks_all_generic_operators() -> None:
    expression = _ranking_expression()

    assert event_ir.condition_from_dict(expression.to_dict()) == expression
    assert {
        "source", "filter", "summarize", "order", "limit", "join", "exists"
    } <= event_ir.node_type_names(expression)
    assert event_ir.sources(expression) == {"member_purchase", "global_sale"}
    assert event_ir.field_names(expression) == {
        "member_purchase.product_id",
        "global_sale.product_id",
        "global_sale.quantity",
        "global_sale.occurred_at",
    }


def test_tsql_compiles_global_rank_then_correlated_semi_join() -> None:
    registry = _ranking_registry()
    sql = event_compiler.compile_expression_sql(
        _ranking_expression(),
        registry=registry,
        fields=_ranking_fields(registry),
        dialect=get_dialect("tsql"),
    )

    assert sql.startswith("EXISTS (SELECT 1 FROM MEMBER_PURCHASE MP WHERE ")
    assert "MP.MEMBER_ID = B.MEMBER_ID" in sql
    assert "GS.MEMBER_ID = B.MEMBER_ID" not in sql
    assert "GS.SOLD_AT >= '2019-01-01'" in sql
    assert "GS.SOLD_AT < '2020-01-01'" in sql
    assert (
        "SELECT TOP 10 GS.PRODUCT_ID AS product_id, "
        "SUM(GS.QUANTITY) AS sold_quantity FROM GLOBAL_SALE GS"
    ) in sql
    assert "GROUP BY GS.PRODUCT_ID" in sql
    assert "ORDER BY SUM(GS.QUANTITY) DESC, GS.PRODUCT_ID ASC" in sql
    assert "EXISTS (SELECT 1 FROM (SELECT TOP 10" in sql
    assert "MP.PRODUCT_ID = ER1.product_id" in sql


def test_suffix_limit_dialect_keeps_order_before_limit() -> None:
    registry = _ranking_registry()
    sql = event_compiler.compile_expression_sql(
        _ranking_expression(),
        registry=registry,
        fields=_ranking_fields(registry),
        dialect=get_dialect("postgres"),
    )

    assert "SELECT TOP" not in sql
    assert (
        "ORDER BY SUM(GS.QUANTITY) DESC, GS.PRODUCT_ID ASC LIMIT 10"
    ) in sql


def test_ranking_composition_is_reported_as_compiler_supported() -> None:
    registry = _ranking_registry()
    context = event_compiler.CompileContext(
        registry=registry,
        fields=_ranking_fields(registry),
        dialect=get_dialect("tsql"),
        literals=True,
    )

    result = event_compiler.validate_compiler_capability(
        _ranking_expression(), context=context
    )

    assert result.status == event_compiler.CAPABILITY_SUPPORTED
    assert result.issues == ()


def test_same_source_rank_relation_is_one_semantic_event_domain() -> None:
    global_rows = event_ir.Source("purchase_line", correlation="none")
    summary = event_ir.Summarize(
        global_rows,
        keys=(event_ir.NamedExpression(
            "product_id", event_ir.FieldRef("purchase_line.product_id")
        ),),
        measures=(event_ir.NamedMeasure(
            "quantity", "sum", event_ir.FieldRef("purchase_line.quantity")
        ),),
    )
    expression = event_ir.Exists(event_ir.Join(
        event_ir.Source("purchase_line"),
        event_ir.Limit(event_ir.Order(summary, (
            event_ir.SortKey("quantity", "desc"),
            event_ir.SortKey("product_id", "asc"),
        )), 10),
        event_ir.Comparison(
            "=",
            event_ir.FieldRef("purchase_line.product_id"),
            event_ir.FieldRef("purchase_line.product_id"),
        ),
        kind="semi",
    ))

    predicate = graph_rag._event_predicate_factory(
        expression, False, datetime(2026, 8, 2, tzinfo=timezone.utc)
    )

    assert predicate is not None
    assert predicate.domain == "purchase_line"
    assert predicate.polarity == aggregate_semantics.PRESENCE


def test_project_is_a_materializable_generic_relation() -> None:
    registry = _ranking_registry()
    fields = _ranking_fields(registry)
    projected = event_ir.Project(
        relation=event_ir.Source(name="global_sale", correlation="none"),
        items=(
            event_ir.NamedExpression(
                name="product_id",
                expression=event_ir.FieldRef(name="global_sale.product_id"),
            ),
        ),
    )
    expression = event_ir.Exists(
        relation=event_ir.Join(
            left=event_ir.Source(name="member_purchase"),
            right=projected,
            kind="semi",
            on=event_ir.Comparison(
                operator="=",
                left=event_ir.FieldRef(name="member_purchase.product_id"),
                right=event_ir.FieldRef(name="global_sale.product_id"),
            ),
        )
    )

    assert event_ir.condition_from_dict(expression.to_dict()) == expression
    sql = event_compiler.compile_expression_sql(
        expression, registry=registry, fields=fields
    )
    assert (
        "EXISTS (SELECT 1 FROM (SELECT GS.PRODUCT_ID AS product_id "
        "FROM GLOBAL_SALE GS) AS ER0"
    ) in sql


def test_not_comparison_is_a_two_valued_audience_complement() -> None:
    registry = event_compiler.resolve_registry()
    fields = event_compiler.resolve_fields(registry, {
        "subject.segment": event_compiler.FieldSpec(
            source="subject", column="SEGMENT", data_type="string"
        )
    })
    expression = event_ir.Not(
        operand=event_ir.Comparison(
            operator="=",
            left=event_ir.FieldRef(name="subject.segment"),
            right=event_ir.Literal(value="active"),
        )
    )

    sql = event_compiler.compile_expression_sql(
        expression, registry=registry, fields=fields, dialect=get_dialect("ansi")
    )
    assert sql == (
        "NOT (CASE WHEN (B.SEGMENT = 'active') THEN 1 ELSE 0 END = 1)"
    )

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE members (SEGMENT TEXT)")
        connection.executemany(
            "INSERT INTO members VALUES (?)", [("active",), ("inactive",), (None,)]
        )
        rows = connection.execute(
            f"SELECT SEGMENT FROM members B WHERE {sql} ORDER BY SEGMENT IS NULL"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [("inactive",), (None,)]


def test_not_exists_keeps_the_direct_sql_optimization() -> None:
    sql = event_compiler.compile_expression_sql(
        event_ir.Not(operand=event_ir.Exists(relation=event_ir.Source("purchase")))
    )

    assert sql.startswith("NOT EXISTS (")
    assert "CASE WHEN" not in sql
