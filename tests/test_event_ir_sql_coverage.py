"""Compiler-aligned SQL coverage for signed Event IR atoms."""

from __future__ import annotations

import event_compiler
import event_ir
import graph_rag


def _exists(source: str, *, days: int = 30) -> event_ir.Exists:
    return event_ir.Exists(event_ir.Filter(
        event_ir.Source(source),
        event_ir.TimeFilter(
            event_ir.FieldRef(f"{source}.occurred_at"),
            event_ir.RollingWindow(days, "day"),
        ),
    ))


def _plan(expression: event_ir.Condition) -> dict:
    return {
        "event_expression": {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
        },
        "audience_requirement": {"expression": expression.to_dict(), "issues": []},
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
    }


def _compiled(expression: event_ir.Condition) -> str:
    return event_compiler.compile_condition(
        expression, graph_rag._event_compile_context()
    ).sql


def _coverage(expression: event_ir.Condition, sql: str) -> dict:
    return graph_rag.validate_sql_condition_coverage(
        sql, graph_rag.required_sql_conditions(_plan(expression))
    )


def test_negative_subject_column_uses_compiled_predicate_not_lexical_not_exists() -> None:
    positive = _exists("login", days=180)
    negative = event_ir.Not(positive)
    condition = graph_rag.required_sql_conditions(_plan(negative))[0]
    compiled = _compiled(negative)

    assert compiled in condition["all_terms"]
    assert "not exists" not in condition["all_terms"]
    assert condition["none_terms"] == []
    assert _coverage(negative, compiled)["is_satisfied"]
    assert not _coverage(negative, _compiled(positive))["is_satisfied"]
    assert not _coverage(negative, compiled.replace("-180", "-150"))["is_satisfied"]


def test_positive_subject_column_rejects_the_negative_wrapper() -> None:
    positive = _exists("login", days=180)
    negative_sql = _compiled(event_ir.Not(positive))
    condition = graph_rag.required_sql_conditions(_plan(positive))[0]

    assert condition["all_terms"] == [_compiled(positive)]
    assert condition["none_terms"] == [negative_sql]
    assert _coverage(positive, _compiled(positive))["is_satisfied"]
    # The positive predicate is a substring of NOT(positive); none_terms is
    # what preserves polarity instead of accepting that accidental match.
    assert not _coverage(positive, negative_sql)["is_satisfied"]


def test_negative_fact_table_keeps_not_exists_and_table_guards() -> None:
    positive = _exists("purchase")
    negative = event_ir.Not(positive)
    compiled = _compiled(negative)
    condition = graph_rag.required_sql_conditions(_plan(negative))[0]

    assert compiled in condition["all_terms"]
    assert "not exists" in condition["all_terms"]
    assert "CRM_SL_ORDERHEADERMALL" in condition["all_terms"]
    assert _coverage(negative, compiled)["is_satisfied"]
    assert not _coverage(negative, _compiled(positive))["is_satisfied"]
    assert not _coverage(
        negative,
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "WHERE NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL)",
    )["is_satisfied"]


def test_positive_fact_table_rejects_not_exists_despite_exists_substring() -> None:
    positive = _exists("purchase")
    positive_sql = _compiled(positive)
    negative_sql = _compiled(event_ir.Not(positive))
    condition = graph_rag.required_sql_conditions(_plan(positive))[0]

    assert "exists" in condition["all_terms"]
    assert "CRM_SL_ORDERHEADERMALL" in condition["all_terms"]
    assert condition["none_terms"] == [negative_sql]
    assert _coverage(positive, positive_sql)["is_satisfied"]
    assert not _coverage(positive, negative_sql)["is_satisfied"]
