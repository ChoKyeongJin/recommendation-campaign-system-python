from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from targeting_expression import (
    And,
    ConditionClaim,
    ConditionClaimInvariantError,
    Not,
    Or,
    PredicateRef,
    TargetingExpression,
    TargetingExpressionInvariantError,
    condition_claim_invariant_issues,
    targeting_expression_from_dict,
    validate_condition_claims,
)


def predicate(
    semantic_key: str,
    *,
    span: tuple[int, int],
    evidence_text: str = "evidence",
    threshold: int = 1,
) -> PredicateRef:
    return PredicateRef(
        predicate_kind="event",
        semantic_key=semantic_key,
        source_spans=(span,),
        payload={
            "operator": "exists",
            "threshold": threshold,
            "evidence": {"text": evidence_text, "start": span[0], "end": span[1]},
        },
    )


def claim(
    expression: PredicateRef,
    *,
    owner: str | None = "event_expression",
    status: str = "resolved",
    disposition: str = "owned",
    origin_parser: str = "rules",
) -> ConditionClaim:
    return ConditionClaim(
        source_spans=expression.source_spans,
        expression_node_id=expression.expression_node_id,
        parent_expression_node_id=None,
        predicate_kind=expression.predicate_kind,
        semantic_key=expression.semantic_key,
        owner=owner,
        status=status,
        disposition=disposition,
        origin_parser=origin_parser,
        issues=(),
    )


def test_typed_nodes_are_frozen_dataclasses() -> None:
    leaf = predicate("event.purchase.exists", span=(0, 4))

    with pytest.raises(FrozenInstanceError):
        leaf.semantic_key = "changed"  # type: ignore[misc]


def test_fingerprint_excludes_source_spans_and_evidence_text() -> None:
    left = predicate("event.purchase.exists", span=(0, 4), evidence_text="parser-a")
    right = predicate("event.purchase.exists", span=(20, 24), evidence_text="parser-b")

    assert left.canonical_fingerprint == right.canonical_fingerprint
    assert left.expression_node_id == right.expression_node_id
    assert left.to_dict()["source_spans"] != right.to_dict()["source_spans"]
    assert left.to_dict()["payload"]["evidence"] != right.to_dict()["payload"]["evidence"]


def test_fingerprint_changes_when_semantic_payload_changes() -> None:
    first = predicate("event.purchase.count", span=(0, 4), threshold=1)
    second = predicate("event.purchase.count", span=(0, 4), threshold=2)

    assert first.canonical_fingerprint != second.canonical_fingerprint
    assert first.expression_node_id != second.expression_node_id


def test_predicate_payload_accepts_any_json_safe_shape() -> None:
    expression = PredicateRef(
        predicate_kind="legacy",
        semantic_key="legacy.values",
        source_spans=(),
        payload=[{"value": 1}, None, True],
    )

    assert expression.to_dict()["payload"] == [{"value": 1}, None, True]
    assert PredicateRef.from_dict(expression.to_dict()) == expression


def test_predicate_payload_rejects_non_json_values() -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        PredicateRef(
            predicate_kind="legacy",
            semantic_key="legacy.invalid",
            payload={"value": object()},
        )


@pytest.mark.parametrize("operator", ["or", "OR", "union", "any-of", "또는", "||"])
def test_or_fingerprint_is_invariant_to_child_order_and_alias(operator: str) -> None:
    first = predicate("event.purchase.exists", span=(0, 4))
    second = predicate("event.login.exists", span=(8, 12))
    canonical = Or((first, second))
    reordered = Or((second, first), operator=operator)

    assert canonical.canonical_fingerprint == reordered.canonical_fingerprint
    assert canonical.expression_node_id == reordered.expression_node_id


def test_and_fingerprint_is_invariant_to_child_order() -> None:
    first = predicate("member.grade.vip", span=(0, 3))
    second = predicate("member.gender.female", span=(5, 8))

    assert And((first, second)).canonical_fingerprint == And((second, first)).canonical_fingerprint


def test_nested_expression_round_trips_without_losing_predicate_payload() -> None:
    first = predicate("event.purchase.exists", span=(0, 4))
    second = predicate("event.login.exists", span=(8, 12))
    expression: TargetingExpression = And((Not(first), Or((first, second), operator="혹은")))

    serialized = expression.to_dict()
    restored = TargetingExpression.from_dict(serialized)

    assert restored == expression
    assert restored.to_dict() == serialized
    assert restored.expression_node_id == expression.expression_node_id


def test_deserializer_accepts_boolean_aliases_but_serializes_canonically() -> None:
    first = predicate("event.purchase.exists", span=(0, 4)).to_dict()
    second = predicate("event.login.exists", span=(8, 12)).to_dict()

    restored = targeting_expression_from_dict({"operator": "union", "children": [first, second]})

    assert isinstance(restored, Or)
    assert restored.to_dict()["type"] == "or"


def test_deserializer_rejects_tampered_content_hash() -> None:
    serialized = predicate("event.purchase.exists", span=(0, 4)).to_dict()
    serialized["semantic_key"] = "event.login.exists"

    with pytest.raises(TargetingExpressionInvariantError):
        targeting_expression_from_dict(serialized)


def test_condition_claim_id_and_round_trip_are_content_derived() -> None:
    expression = predicate("event.purchase.exists", span=(0, 4))
    first = claim(expression)
    second = claim(expression)

    assert first.claim_id == second.claim_id
    assert ConditionClaim.from_dict(first.to_dict()) == first
    assert hash(ConditionClaim.from_dict(first.to_dict())) == hash(first)


def test_claim_validator_allows_one_owner_and_one_suppressed_duplicate() -> None:
    expression = predicate("event.purchase.exists", span=(0, 4))
    owned = claim(expression, origin_parser="rules")
    suppressed = claim(
        expression,
        disposition="suppressed_duplicate",
        origin_parser="llm",
    )

    assert validate_condition_claims((owned, suppressed)) == (owned, suppressed)


def test_claim_validator_rejects_duplicate_span_and_node_owners() -> None:
    first_expression = predicate("event.purchase.exists", span=(0, 4))
    second_expression = predicate("event.login.exists", span=(0, 4))
    first = claim(first_expression, owner="event_expression", origin_parser="rules")
    second = claim(second_expression, owner="legacy_login", origin_parser="legacy")

    issues = condition_claim_invariant_issues((first, second))

    assert any("duplicate owned source span" in issue for issue in issues)
    with pytest.raises(ConditionClaimInvariantError):
        validate_condition_claims((first, second))


def test_claim_validator_rejects_multiple_owners_for_same_semantic_node() -> None:
    first_expression = predicate("event.purchase.exists", span=(0, 4), evidence_text="a")
    second_expression = predicate("event.purchase.exists", span=(8, 12), evidence_text="b")
    first = claim(first_expression, owner="event_expression", origin_parser="rules")
    second = claim(second_expression, owner="legacy_purchase", origin_parser="legacy")

    issues = condition_claim_invariant_issues((first, second))

    assert any("duplicate owned expression node" in issue for issue in issues)


def test_claim_validator_rejects_dangling_suppressed_duplicate() -> None:
    expression = predicate("event.purchase.exists", span=(0, 4))
    suppressed = claim(expression, disposition="suppressed_duplicate")

    with pytest.raises(ConditionClaimInvariantError):
        validate_condition_claims((suppressed,))
