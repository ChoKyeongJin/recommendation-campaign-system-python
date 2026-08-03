"""Truth-table contract for multi-channel consent cardinality."""

from __future__ import annotations

import itertools

import pytest

import audience_runtime
import canonical_audience_claims
import consent_cardinality
import event_ir
import graph_rag
from query_structurer import audience_execution
from query_structurer.campaign_plan_v4 import (
    attach_campaign_query_plan_v4_identity,
    validate_campaign_query_plan_v4,
)
from query_structurer.semantic_ir import extract_literal_bindings

ANY_QUERY = "이메일, 문자, 앱푸시 중 하나 이상에 동의한 회원을 보여줘."
EXACT_TWO_QUERY = "이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원을 찾아줘."
FIELDS = (
    "subject.email_consent",
    "subject.sms_consent",
    "subject.app_push_consent",
)


def _comparison(
    query: str, field: str, value: str = "agreed", operator: str = "=",
) -> event_ir.Comparison:
    return event_ir.Comparison(
        operator,
        event_ir.FieldRef(field),
        event_ir.Literal(value),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )


def _any_expression(*, fields: tuple[str, ...] = FIELDS, value: str = "agreed") -> event_ir.Condition:
    return event_ir.Or(tuple(_comparison(ANY_QUERY, field, value) for field in fields))


def _exact_two_expression(*, negative_style: str = "declined") -> event_ir.Condition:
    branches: list[event_ir.Condition] = []
    for excluded in FIELDS:
        atoms = []
        for field in FIELDS:
            if field != excluded:
                atoms.append(_comparison(EXACT_TWO_QUERY, field))
            elif negative_style == "declined":
                atoms.append(_comparison(EXACT_TWO_QUERY, field, "declined"))
            else:
                atoms.append(_comparison(EXACT_TWO_QUERY, field, "agreed", "!="))
        branches.append(event_ir.And(tuple(atoms)))
    return event_ir.Or(tuple(branches))


def _bindings(query: str) -> list[dict]:
    return extract_literal_bindings(query, current_date="2026-08-04")


def _issues(query: str, expression: event_ir.Condition) -> list[dict]:
    return canonical_audience_claims.canonical_claim_issues(
        query,
        expression,
        _bindings(query),
        audience_runtime.catalog_snapshot(),
    )


def _resolver_issues(query: str, expression: event_ir.Condition) -> list[dict]:
    result = audience_execution.run_audience_resolver(
        {
            "audience_requirement": {"expression": expression.to_dict(), "issues": []},
            "literal_bindings": _bindings(query),
        },
        query,
        current_date="2026-08-04",
    )
    assert result is not None
    return result.issues


def _validated_v4_plan(query: str, expression: event_ir.Condition) -> dict:
    enriched = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {"expression": expression.to_dict(), "issues": []},
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )
    return validate_campaign_query_plan_v4(
        enriched, query=query, raw_query=query, require_semantic=True
    )


def test_any_of_three_or_consumes_the_cardinality_literal_and_resolves() -> None:
    expression = _any_expression()
    bindings = _bindings(ANY_QUERY)
    assert bindings == [{
        "id": "comparison_operator_1",
        "kind": "comparison_operator",
        "text": "이상",
        "start": 18,
        "end": 20,
        "value": "이상",
        "normalized": ">=",
    }]

    validation = consent_cardinality.validate_consent_cardinality(
        ANY_QUERY, expression, bindings, audience_runtime.catalog_snapshot()
    )
    assert validation is not None and validation.equivalent
    assert validation.consumed_binding_indices == frozenset({0})
    assert _issues(ANY_QUERY, expression) == []
    assert _resolver_issues(ANY_QUERY, expression) == []
    plan = _validated_v4_plan(ANY_QUERY, expression)
    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["event_expression"]["expression"] == expression.to_dict()


def test_top_level_cardinality_or_is_grouped_under_the_member_policy() -> None:
    candidate = graph_rag.build_event_expression_sql_candidate(
        dict(_validated_v4_plan(ANY_QUERY, _any_expression()))
    )

    assert candidate is not None
    assert "MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'\n  AND ((B.EMAIL_YN = 'Y')" in candidate["sql"]
    assert "OR (B.SMS_YN = 'Y') OR (B.APP_PUSH_YN = 'Y'))" in candidate["sql"]


@pytest.mark.parametrize("negative_style", ["declined", "not-agreed"])
def test_exact_two_dnf_is_accepted_for_both_catalog_complement_forms(
    negative_style: str,
) -> None:
    expression = _exact_two_expression(negative_style=negative_style)

    validation = consent_cardinality.validate_consent_cardinality(
        EXACT_TWO_QUERY,
        expression,
        _bindings(EXACT_TWO_QUERY),
        audience_runtime.catalog_snapshot(),
    )
    assert validation is not None and validation.equivalent
    assert validation.mode == "exact" and validation.count == 2
    assert set(validation.field_ids) == set(FIELDS)
    assert _issues(EXACT_TWO_QUERY, expression) == []
    assert _resolver_issues(EXACT_TWO_QUERY, expression) == []
    plan = _validated_v4_plan(EXACT_TWO_QUERY, expression)
    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["event_expression"]["expression"] == expression.to_dict()


def test_exact_two_expression_can_be_synthesized_from_the_catalog_contract() -> None:
    bindings = _bindings(EXACT_TWO_QUERY)
    expression = consent_cardinality.synthesize_exact_consent_cardinality(
        EXACT_TWO_QUERY,
        bindings,
        audience_runtime.catalog_snapshot(),
    )

    assert expression is not None
    validation = consent_cardinality.validate_consent_cardinality(
        EXACT_TWO_QUERY,
        expression,
        bindings,
        audience_runtime.catalog_snapshot(),
    )
    assert validation is not None and validation.equivalent
    assert validation.mode == "exact" and validation.count == 2


def test_at_least_cardinality_is_not_synthesized_by_the_exact_count_contract() -> None:
    assert consent_cardinality.synthesize_exact_consent_cardinality(
        ANY_QUERY,
        _bindings(ANY_QUERY),
        audience_runtime.catalog_snapshot(),
    ) is None


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(
            event_ir.And(tuple(_comparison(ANY_QUERY, field) for field in FIELDS)),
            id="and-reduction",
        ),
        pytest.param(_any_expression(fields=FIELDS[:2]), id="one-field-missing"),
        pytest.param(
            _any_expression(fields=(FIELDS[0], FIELDS[1], "subject.marketing_consent")),
            id="wrong-consent-field",
        ),
        pytest.param(_any_expression(value="declined"), id="wrong-domain-value-polarity"),
        pytest.param(_any_expression(value="unknown"), id="unregistered-value"),
    ],
)
def test_any_of_three_fails_closed_unless_the_whole_truth_table_matches(
    expression: event_ir.Condition,
) -> None:
    issues = _issues(ANY_QUERY, expression)

    assert any(issue["argument"] == "consent_cardinality" for issue in issues)
    assert any(issue["argument"] == "literal_bindings[0]" for issue in issues)


def _extra_exact_branch(bits: tuple[bool, bool, bool]) -> event_ir.Condition:
    return event_ir.And(tuple(
        _comparison(EXACT_TWO_QUERY, field, "agreed" if enabled else "declined")
        for field, enabled in zip(FIELDS, bits, strict=True)
    ))


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(_any_expression(), id="allows-one-or-three"),
        pytest.param(
            event_ir.Or((_exact_two_expression(), _extra_exact_branch((True, True, True)))),
            id="also-allows-three",
        ),
        pytest.param(
            event_ir.Or((_exact_two_expression(), _extra_exact_branch((True, False, False)))),
            id="also-allows-one",
        ),
        pytest.param(
            event_ir.Or(tuple(
                event_ir.And(tuple(
                    _comparison(
                        EXACT_TWO_QUERY,
                        field if field != FIELDS[2] else "subject.marketing_consent",
                        "agreed" if enabled else "declined",
                    )
                    for field, enabled in zip(FIELDS, bits, strict=True)
                ))
                for bits in itertools.product((False, True), repeat=3)
                if sum(bits) == 2
            )),
            id="wrong-field",
        ),
    ],
)
def test_exact_two_fails_closed_for_any_truth_table_drift(
    expression: event_ir.Condition,
) -> None:
    issues = _issues(EXACT_TWO_QUERY, expression)

    assert any(issue["argument"] == "consent_cardinality" for issue in issues)
