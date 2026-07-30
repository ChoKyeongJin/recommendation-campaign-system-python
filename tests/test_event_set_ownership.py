"""Event/Set Boolean meaning and ownership acceptance contracts.

These tests deliberately use only the deterministic rules parser.  The
canonical tree is the contract under test: parser-specific candidates may
change, but an OR branch must neither disappear nor be flattened to AND before
SQL admission.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterable
from typing import Any

import pytest

os.environ.setdefault("CONDITION_SLOT_LLM_FALLBACK", "off")
os.environ.setdefault("SURFACE_LEXICON_LLM", "off")
os.environ.setdefault("TARGET_OBJECT_LLM_FALLBACK", "false")

import canonical_targeting
import graph_rag
import plan_validation
from targeting_expression import (
    And,
    ConditionClaim,
    Not,
    Or,
    PredicateRef,
    TargetingExpression,
    targeting_expression_from_dict,
    validate_condition_claim_invariants,
)

PURCHASE_OR_LOGIN = "최근 1개월 구매한 고객 또는 최근 1개월 로그인한 고객"
PURCHASE_OR_AGE = "최근 1개월 구매한 고객 또는 30대 고객"
LOGIN_OR_VIP = "최근 1개월 로그인한 고객 또는 VIP 고객"
NAMED_SEGMENT_UNION = "VIP 고객군과 휴면 예정 고객군의 합집합"
PURCHASE_AND_AGE = "최근 6개월 구매 있고 최근 1개월 구매 없는 고객이고 30대 고객"


@pytest.fixture(autouse=True)
def _offline_rules_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "off")
    monkeypatch.setenv("TARGET_OBJECT_LLM_FALLBACK", "false")
    monkeypatch.setattr(
        graph_rag,
        "_apply_llm_condition_slot_fallback",
        lambda *_args, **_kwargs: None,
    )


def _plan(query: str) -> dict[str, Any]:
    plan = graph_rag.build_query_plan(query, parser="rules")
    assert (plan.get("parser") or {}).get("type") == "rules"
    return plan


def _canonical_expression(plan: dict[str, Any]) -> TargetingExpression:
    payload = plan.get("canonical_targeting_expression")
    assert isinstance(payload, dict), "canonical targeting expression must be attached"
    return targeting_expression_from_dict(payload)


def _claims(plan: dict[str, Any]) -> tuple[ConditionClaim, ...]:
    payloads = plan.get("condition_claims")
    assert isinstance(payloads, list), "condition ownership claims must be attached"
    return tuple(ConditionClaim.from_dict(payload) for payload in payloads)


def _predicates(expression: TargetingExpression) -> Iterable[PredicateRef]:
    if isinstance(expression, PredicateRef):
        yield expression
    elif isinstance(expression, Not):
        yield from _predicates(expression.operand)
    elif isinstance(expression, (And, Or)):
        for child in expression.children:
            yield from _predicates(child)


@pytest.mark.parametrize("connective", ["또는", "혹은", "이거나", "거나", "아니면"])
def test_all_or_connectives_share_the_purchase_login_fingerprint(
    connective: str,
) -> None:
    query = f"최근 1개월 구매한 고객 {connective} 최근 1개월 로그인한 고객"
    plan = _plan(query)
    expression = _canonical_expression(plan)

    assert isinstance(expression, Or)
    assert expression.canonical_fingerprint == _canonical_expression(
        _plan(PURCHASE_OR_LOGIN)
    ).canonical_fingerprint
    assert plan.get("set_expressions") == []


def test_purchase_or_login_has_one_canonical_owner_per_event_claim() -> None:
    plan = _plan(PURCHASE_OR_LOGIN)
    expression = _canonical_expression(plan)
    claims = _claims(plan)

    assert isinstance(expression, Or)
    leaves = tuple(_predicates(expression))
    assert len(leaves) == 2
    assert {leaf.predicate_kind for leaf in leaves} == {"EventPredicate"}
    assert len({leaf.semantic_key for leaf in leaves}) == 2

    assert len(claims) == 2
    assert {claim.expression_node_id for claim in claims} == {
        leaf.expression_node_id for leaf in leaves
    }
    assert all(claim.owner == "event_expression" for claim in claims)
    assert all(claim.status == "resolved" for claim in claims)
    assert all(claim.disposition == "owned" for claim in claims)
    assert all(
        claim.parent_expression_node_id == expression.expression_node_id
        for claim in claims
    )
    assert len({claim.expression_node_id for claim in claims}) == len(claims)
    assert validate_condition_claim_invariants(claims) == claims


@pytest.mark.parametrize(
    ("query", "member_predicate_kind", "member_semantic_prefix"),
    [
        (PURCHASE_OR_AGE, "MemberPredicate", "member.age:"),
        (LOGIN_OR_VIP, "NamedSegmentPredicate", "named_segment:vip"),
    ],
    ids=["purchase-or-age", "login-or-vip"],
)
def test_mixed_event_set_or_is_preserved_and_blocked_from_sql(
    query: str,
    member_predicate_kind: str,
    member_semantic_prefix: str,
) -> None:
    plan = _plan(query)
    expression = _canonical_expression(plan)
    leaves = tuple(_predicates(expression))

    assert isinstance(expression, Or)
    assert {leaf.predicate_kind for leaf in leaves} == {
        "EventPredicate",
        member_predicate_kind,
    }
    assert any(
        leaf.semantic_key.startswith(member_semantic_prefix)
        for leaf in leaves
        if leaf.predicate_kind == member_predicate_kind
    )
    assert (plan.get("event_expression") or {}).get("candidate_scope") == "subtree"
    assert len(plan.get("set_expressions") or []) == 1
    assert plan["set_expressions"][0]["set_ast"]["op"] == "+"
    projection = plan["canonical_projection"]
    assert projection["status"] == "partially_supported"
    assert projection["silent_drop_count"] == 0
    assert projection["legacy_semantic_loss"] is True

    validation = plan_validation.validate_executable_plan(plan)
    assert validation.status == plan_validation.UNSUPPORTED
    assert "canonical_projection_partially_supported" in {
        issue.code for issue in validation.issues
    }
    assert graph_rag.compile_executable_plan(copy.deepcopy(plan)) is None


def test_named_segment_union_remains_a_canonical_or() -> None:
    plan = _plan(NAMED_SEGMENT_UNION)
    expression = _canonical_expression(plan)
    leaves = tuple(_predicates(expression))

    assert len(plan.get("set_expressions") or []) == 1
    set_expression = plan["set_expressions"][0]
    assert set_expression["set_ast"]["op"] == "+"
    assert set_expression["requires_clarification"] is False

    assert isinstance(expression, Or)
    assert {leaf.predicate_kind for leaf in leaves} == {"NamedSegmentPredicate"}
    assert {leaf.semantic_key for leaf in leaves} == {
        "named_segment:vip",
        "named_segment:inactive_90d",
    }
    claims = _claims(plan)
    assert all(claim.owner == "set_expressions" for claim in claims)
    assert all(claim.disposition == "owned" for claim in claims)
    assert plan["canonical_projection"]["status"] == "supported"
    assert plan["canonical_projection"]["silent_drop_count"] == 0
    assert plan["canonical_projection"]["legacy_semantic_loss"] is False


def test_event_and_legacy_member_condition_share_one_supported_canonical_and() -> None:
    plan = _plan(PURCHASE_AND_AGE)
    expression = _canonical_expression(plan)
    leaves = tuple(_predicates(expression))

    assert isinstance(expression, And)
    assert {leaf.predicate_kind for leaf in leaves} == {
        "EventPredicate",
        "MemberPredicate",
    }
    assert {claim.owner for claim in _claims(plan)} == {
        "event_expression",
        "legacy_member_conditions",
    }
    assert plan["canonical_projection"]["status"] == "supported"
    assert plan["canonical_projection"]["legacy_semantic_loss"] is False

    candidate = graph_rag.compile_executable_plan(copy.deepcopy(plan))
    assert candidate is not None
    assert "EXISTS" in candidate["sql"]
    assert "B.AGE >= 30" in candidate["sql"]
    assert "B.AGE <= 39" in candidate["sql"]


@pytest.mark.parametrize(
    "query",
    [PURCHASE_OR_LOGIN, PURCHASE_OR_AGE, LOGIN_OR_VIP, NAMED_SEGMENT_UNION],
    ids=["event-or-event", "event-or-age", "event-or-segment", "segment-union"],
)
def test_claim_invariants_hold_and_canonical_attachment_is_idempotent(
    query: str,
) -> None:
    plan = _plan(query)
    before = copy.deepcopy(plan)

    result = canonical_targeting.attach_canonical_targeting(plan)

    assert plan == before
    assert result.expression is not None
    assert result.expression.to_dict() == plan["canonical_targeting_expression"]
    assert [claim.to_dict() for claim in result.claims] == plan["condition_claims"]
    assert validate_condition_claim_invariants(result.claims) == result.claims
    assert {claim.expression_node_id for claim in result.claims} == {
        predicate.expression_node_id
        for predicate in _predicates(result.expression)
    }


def test_late_event_candidate_collection_keeps_the_same_canonical_owner_map() -> None:
    expected = _plan(PURCHASE_OR_AGE)
    reordered = copy.deepcopy(expected)
    for key in (
        "event_expression",
        "event_semantic_validation",
        "event_compiler_capability",
        "canonical_targeting_expression",
        "condition_claims",
        "canonical_projection",
        "canonical_targeting_validation",
    ):
        reordered.pop(key, None)

    graph_rag._reconcile_condition_ownership(reordered)

    assert _canonical_expression(reordered).canonical_fingerprint == (
        _canonical_expression(expected).canonical_fingerprint
    )
    assert {
        (claim.expression_node_id, claim.owner, claim.disposition)
        for claim in _claims(reordered)
    } == {
        (claim.expression_node_id, claim.owner, claim.disposition)
        for claim in _claims(expected)
    }
