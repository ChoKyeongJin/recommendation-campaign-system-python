"""Structural literal receipts for rolling event inactivity."""

from __future__ import annotations

from datetime import date

import pytest

import audience_runtime
import canonical_audience_claims
import event_compiler
import event_ir
import rolling_absence_claims
from query_structurer import audience_execution
from query_structurer.semantic_ir import extract_literal_bindings

QUERY = "6개월 이상 접속하지 않은 휴면 고객"
NEGATIVE_SPAN = "접속하지 않은"


def _expression(
    *,
    months: int = 6,
    source: str = "login",
    negated: bool = True,
    evidence_text: str = NEGATIVE_SPAN,
) -> event_ir.Condition:
    start = QUERY.index(evidence_text)
    exists: event_ir.Condition = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source(source),
            event_ir.TimeFilter(
                event_ir.FieldRef(f"{source}.occurred_at"),
                event_ir.RollingWindow(months, "month"),
            ),
        ),
        evidence=event_ir.Evidence(evidence_text, start, start + len(evidence_text)),
    )
    return event_ir.Not(exists) if negated else exists


def _bindings(query: str = QUERY) -> list[dict]:
    return extract_literal_bindings(query, current_date="2026-08-04")


def _issues(expression: event_ir.Condition) -> list[dict]:
    return canonical_audience_claims.canonical_claim_issues(
        QUERY,
        expression,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )


def test_six_month_login_absence_consumes_duration_and_gte_structurally() -> None:
    expression = _expression()
    consumed = rolling_absence_claims.consumed_literal_binding_indices(
        QUERY,
        expression,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )

    assert consumed == frozenset({0, 1})
    assert _issues(expression) == []
    resolution = audience_execution.run_audience_resolver(
        {
            "audience_requirement": {"expression": expression.to_dict(), "issues": []},
            "literal_bindings": _bindings(),
        },
        QUERY,
        current_date="2026-08-04",
    )
    assert resolution is not None and resolution.issues == []

    sql = event_compiler.compile_expression(
        expression,
        context=audience_runtime.resolve_audience_catalog().compile_context(
            literals=True, today=date(2026, 8, 4)
        ),
    ).sql
    assert "LAST_LOGIN_DATE" in sql
    assert "DATEADD(DAY, -180" in sql
    assert sql.startswith("NOT (CASE WHEN")


def test_rolling_absence_expression_can_be_synthesized_from_owned_literals() -> None:
    expression = rolling_absence_claims.synthesize_rolling_absence(
        QUERY,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )

    assert expression is not None
    assert expression.to_dict() == _expression().to_dict()
    assert _issues(expression) == []


def test_closed_dormant_login_absence_is_recovered_for_structurer_fallback() -> None:
    expression = rolling_absence_claims.synthesize_closed_dormant_login_absence(
        QUERY,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )

    assert expression is not None
    assert expression.to_dict() == _expression().to_dict()


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("6개월 이하 접속하지 않은 휴면 고객", id="wrong-operator"),
        pytest.param("6개월 이상 접속한 휴면 고객", id="positive-login"),
        pytest.param("6 이상 접속하지 않은 휴면 고객", id="ambiguous-unit"),
        pytest.param("6개월 이상 접속하지 않은 고객", id="missing-dormant-audience"),
        pytest.param(
            "6개월 이상 접속하지 않은 휴면 고객 중 여성",
            id="additional-condition",
        ),
        pytest.param("6개월 이상 구매하지 않은 휴면 고객", id="wrong-source"),
        pytest.param(
            "최근 90일 동안 주문하지 않은 회원을 추출해줘.",
            id="live-66-fact-purchase-absence",
        ),
    ],
)
def test_closed_dormant_login_absence_fallback_rejects_nearby_phrases(
    query: str,
) -> None:
    assert rolling_absence_claims.synthesize_closed_dormant_login_absence(
        query,
        _bindings(query),
        audience_runtime.catalog_snapshot(),
    ) is None


def test_overbroad_rolling_absence_evidence_is_narrowed_after_structure_proof() -> None:
    raw = _expression().to_dict()
    broad = "6개월 이상 접속하지 않은"
    raw["operand"]["evidence"] = {
        "text": broad,
        "start": 0,
        "end": len(broad),
    }

    correction = rolling_absence_claims.normalize_rolling_absence_evidence(
        QUERY,
        raw,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )

    assert correction == {
        "source": "login",
        "from": {"text": broad, "start": 0, "end": len(broad)},
        "to": {
            "text": NEGATIVE_SPAN,
            "start": QUERY.index(NEGATIVE_SPAN),
            "end": QUERY.index(NEGATIVE_SPAN) + len(NEGATIVE_SPAN),
        },
    }
    assert raw["operand"]["evidence"] == correction["to"]


@pytest.mark.parametrize(
    ("raw", "query"),
    [
        pytest.param(_expression(months=5).to_dict(), QUERY, id="wrong-window"),
        pytest.param(_expression(negated=False).to_dict(), QUERY, id="positive-exists"),
        pytest.param(_expression(source="purchase").to_dict(), QUERY, id="wrong-source"),
        pytest.param(_expression(evidence_text="접속").to_dict(), QUERY, id="narrow-ungrounded-evidence"),
        pytest.param(_expression().to_dict(), "6개월 이하 접속하지 않은 휴면 고객", id="wrong-operator"),
    ],
)
def test_rolling_absence_evidence_normalization_fails_closed_on_semantic_drift(
    raw: dict,
    query: str,
) -> None:
    assert rolling_absence_claims.normalize_rolling_absence_evidence(
        query,
        raw,
        _bindings(query),
        audience_runtime.catalog_snapshot(),
    ) is None


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("6개월 이하 접속하지 않은 휴면 고객", id="wrong-operator"),
        pytest.param("6개월 이상 접속한 휴면 고객", id="positive-source"),
        pytest.param("6개월 이상 접속하지 않은 30세 고객", id="unowned-literal"),
    ],
)
def test_rolling_absence_synthesis_fails_closed_without_a_complete_receipt(
    query: str,
) -> None:
    assert rolling_absence_claims.synthesize_rolling_absence(
        query,
        _bindings(query),
        audience_runtime.catalog_snapshot(),
    ) is None


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(_expression(months=5), id="wrong-window-value"),
        pytest.param(_expression(negated=False), id="positive-exists"),
        pytest.param(_expression(source="purchase"), id="wrong-source"),
        pytest.param(_expression(evidence_text="접속"), id="negation-outside-evidence"),
    ],
)
def test_rolling_absence_receipt_fails_closed_on_structural_drift(
    expression: event_ir.Condition,
) -> None:
    consumed = rolling_absence_claims.consumed_literal_binding_indices(
        QUERY,
        expression,
        _bindings(),
        audience_runtime.catalog_snapshot(),
    )

    assert consumed == frozenset()
    assert any(
        issue["argument"].startswith("literal_bindings[")
        for issue in _issues(expression)
    )


def test_less_than_or_equal_word_is_not_consumed_as_rolling_absence_gte() -> None:
    query = "6개월 이하 접속하지 않은 휴면 고객"
    span = "접속하지 않은"
    start = query.index(span)
    expression = event_ir.Not(event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("login"),
            event_ir.TimeFilter(
                event_ir.FieldRef("login.occurred_at"),
                event_ir.RollingWindow(6, "month"),
            ),
        ),
        evidence=event_ir.Evidence(span, start, start + len(span)),
    ))

    assert rolling_absence_claims.consumed_literal_binding_indices(
        query,
        expression,
        _bindings(query),
        audience_runtime.catalog_snapshot(),
    ) == frozenset()
