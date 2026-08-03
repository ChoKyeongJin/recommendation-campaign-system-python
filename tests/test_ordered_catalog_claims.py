"""Ordered catalog claims require an application-owned comparison receipt."""

from __future__ import annotations

import pytest

import audience_runtime
import canonical_audience_claims
import event_compiler
import event_ir
from query_structurer import audience_execution
from query_structurer.semantic_ir import extract_literal_bindings


GRADE_QUERY = "골드 이상 등급인 회원"


def _comparison(
    query: str,
    *,
    operator: str = ">=",
    field: str = "subject.grade",
    value: str = "gold_grade",
    evidence_end: int | None = None,
) -> event_ir.Comparison:
    end = len(query) if evidence_end is None else evidence_end
    return event_ir.Comparison(
        operator,
        event_ir.FieldRef(field),
        event_ir.Literal(value),
        evidence=event_ir.Evidence(query[:end], 0, end),
    )


def _claim_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: list[dict] | None = None,
) -> list[dict]:
    return canonical_audience_claims.canonical_claim_issues(
        query,
        expression,
        extract_literal_bindings(query, current_date="2026-08-04")
        if bindings is None else bindings,
        audience_runtime.catalog_snapshot(),
    )


def test_run_audience_resolver_accepts_ordered_grade_and_compiles_ranked_in() -> None:
    expression = _comparison(GRADE_QUERY)
    payload = {
        "audience_requirement": {"expression": expression.to_dict(), "issues": []},
        "literal_bindings": extract_literal_bindings(
            GRADE_QUERY, current_date="2026-08-04"
        ),
    }

    resolution = audience_execution.run_audience_resolver(
        payload, GRADE_QUERY, current_date="2026-08-04"
    )

    assert resolution is not None
    assert resolution.issues == []
    assert resolution.expression is not None
    sql = event_compiler.compile_condition(
        resolution.expression,
        audience_runtime.resolve_audience_catalog().compile_context(literals=True),
    ).sql
    assert sql == (
        "B.EMART_GRADE_CD IN "
        "('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')"
    )


@pytest.mark.parametrize(
    ("surface", "operator", "physical_values"),
    [
        ("이상", ">=", ["GOLD", "VIP"]),
        ("이하", "<=", ["WELCOME", "FAMILY", "SILVER", "GOLD"]),
        ("초과", ">", ["VIP"]),
        ("미만", "<", ["WELCOME", "FAMILY", "SILVER"]),
    ],
)
def test_all_ordered_operators_require_and_consume_their_exact_receipt(
    surface: str, operator: str, physical_values: list[str]
) -> None:
    query = f"골드 {surface} 등급인 회원"
    expression = _comparison(query, operator=operator)

    assert _claim_issues(query, expression) == []
    sql = event_compiler.compile_condition(
        expression,
        audience_runtime.resolve_audience_catalog().compile_context(literals=True),
    ).sql
    assert sql == "B.EMART_GRADE_CD IN (" + ", ".join(
        f"'MEM_GRADE_CD.{value}'" for value in physical_values
    ) + ")"


@pytest.mark.parametrize(
    ("operator", "value"),
    [
        (">", "gold_grade"),
        (">=", "silver_grade"),
    ],
)
def test_wrong_ordered_operator_or_value_stays_blocked(
    operator: str, value: str
) -> None:
    issues = _claim_issues(
        GRADE_QUERY, _comparison(GRADE_QUERY, operator=operator, value=value)
    )

    assert any(
        issue["argument"] == "catalog_value.subject.grade" for issue in issues
    )


def test_missing_or_tampered_operator_binding_stays_blocked() -> None:
    expression = _comparison(GRADE_QUERY)
    missing = _claim_issues(GRADE_QUERY, expression, [])
    bindings = extract_literal_bindings(GRADE_QUERY, current_date="2026-08-04")
    tampered = [
        {**binding, "normalized": "<="}
        if binding.get("kind") == "comparison_operator" else binding
        for binding in bindings
    ]

    assert any(
        issue["argument"] == "catalog_value.subject.grade" for issue in missing
    )
    assert any(
        issue["argument"] == "catalog_value.subject.grade"
        for issue in _claim_issues(GRADE_QUERY, expression, tampered)
    )


def test_operator_outside_comparison_evidence_does_not_authorize_ordering() -> None:
    grade_end = GRADE_QUERY.index(" ")  # only the canonical value, not '이상'
    issues = _claim_issues(
        GRADE_QUERY,
        _comparison(GRADE_QUERY, evidence_end=grade_end),
    )

    assert any(
        issue["argument"] == "catalog_value.subject.grade" for issue in issues
    )


def test_unordered_value_domain_cannot_consume_size_operator() -> None:
    query = "여성 이상 회원"
    issues = _claim_issues(
        query,
        _comparison(query, field="subject.gender", value="female"),
    )

    assert any(
        issue["argument"] == "catalog_value.subject.gender" for issue in issues
    )
