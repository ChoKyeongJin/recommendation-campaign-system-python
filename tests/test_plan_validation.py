from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

import plan_validation as validation
import targeting_expression


def _valid_canonical_plan() -> dict[str, object]:
    expression = targeting_expression.PredicateRef(
        predicate_kind="MemberPredicate",
        semantic_key="member.age:30:39",
        source_spans=((4, 7),),
        payload={"age_min": 30, "age_max": 39},
    )
    claim = targeting_expression.ConditionClaim(
        source_spans=expression.source_spans,
        expression_node_id=expression.expression_node_id,
        parent_expression_node_id=None,
        predicate_kind=expression.predicate_kind,
        semantic_key=expression.semantic_key,
        owner="set_expressions",
        status="resolved",
        disposition="owned",
        origin_parser="test_parser",
        issues=(),
    )
    return {
        "ownership_reconciliation_complete": True,
        "canonical_targeting_version": 1,
        "canonical_targeting_expression": expression.to_dict(),
        "condition_claims": [claim.to_dict()],
        "canonical_targeting_validation": {"status": "valid", "issues": []},
        "canonical_projection": {
            "status": "supported",
            "projected_node_ids": [expression.expression_node_id],
            "unprojected_node_ids": [],
            "silent_drop_count": 0,
            "legacy_semantic_loss": False,
        },
    }


def test_empty_plan_has_no_inferred_blocker() -> None:
    result = validation.validate_executable_plan({})

    assert result == validation.PlanValidationResult(
        status="executable",
        issues=(),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )


def test_validation_types_are_frozen() -> None:
    issue = validation.PlanValidationIssue(
        code="required_field_missing",
        status="clarification_required",
        path="semantic_ir.baseline",
    )
    result = validation.PlanValidationResult(
        status="clarification_required",
        issues=(issue,),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )

    with pytest.raises(FrozenInstanceError):
        issue.code = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.status = "executable"  # type: ignore[misc]


def test_set_expression_uses_only_explicit_clarification_state() -> None:
    plan = {
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "requires_clarification": True,
                "clarification_question": "This user-facing text must not enter the result.",
                "blocking_claim_ids": ["claim-set"],
                "unresolved_span_ids": ["span-set"],
            }
        ]
    }

    result = validation.validate_executable_plan(plan)

    assert result.status == "clarification_required"
    assert result.blocking_claim_ids == ("claim-set",)
    assert result.unresolved_span_ids == ("span-set",)
    assert result.issues[0].code == "set_expression_requires_clarification"
    assert not hasattr(result.issues[0], "message")


def test_boolean_ast_and_ownership_are_not_recomputed() -> None:
    plan = {
        "set_expressions": [
            {
                "requires_clarification": False,
                "set_ast": {"type": "unknown_operand", "raw_text": "unowned"},
            }
        ],
        "condition_reconciliation": {
            "issues": [{"type": "authoritative_owner_conflict", "is_blocking": True}],
            "requires_clarification": True,
        },
        "decisions": [
            {"action": "drop", "slot": "target_user.gender", "reason": "intentional owner transfer"}
        ],
    }

    assert validation.validate_executable_plan(plan).status == "executable"


def test_missing_set_ast_is_structural_invalid_without_boolean_recomputation() -> None:
    result = validation.validate_executable_plan({
        "set_expressions": [{"requires_clarification": False, "set_ast": None}]
    })

    assert result.status == "internal_invalid"
    assert result.issues[0].code == "set_expression_schema_invalid"


def test_semantic_ir_hybrid_is_internal_invalid() -> None:
    plan = {
        "target_user": {},
        "literal_bindings": [],
        "semantic_ir": {
            "status": "resolved",
            "operations": [],
            "missing_fields": ["baseline"],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": None,
        },
    }

    result = validation.validate_executable_plan(plan)

    assert result.status == "internal_invalid"
    assert any(issue.code == "semantic_ir_schema_invalid" for issue in result.issues)


def test_invalid_event_expression_is_internal_invalid() -> None:
    result = validation.validate_executable_plan({
        "event_expression": {"expression": {"type": "does_not_exist"}}
    })

    assert result.status == "internal_invalid"
    assert result.issues[0].code == "event_expression_schema_invalid"


def test_canonical_expression_schema_and_required_ids_are_validated() -> None:
    plan = _valid_canonical_plan()
    expression = plan["canonical_targeting_expression"]
    assert isinstance(expression, dict)
    expression.pop("canonical_fingerprint")

    result = validation.validate_executable_plan(plan)

    assert result.status == validation.INTERNAL_INVALID
    assert "canonical_targeting_expression_schema_invalid" in {
        issue.code for issue in result.issues
    }


def test_canonical_expression_content_identity_is_validated() -> None:
    plan = _valid_canonical_plan()
    expression = plan["canonical_targeting_expression"]
    assert isinstance(expression, dict)
    expression["expression_node_id"] = "expr_forged"

    result = validation.validate_executable_plan(plan)

    assert result.status == validation.INTERNAL_INVALID
    assert "canonical_targeting_identity_invalid" in {
        issue.code for issue in result.issues
    }


def test_condition_claim_schema_and_content_identity_are_validated() -> None:
    missing_field_plan = _valid_canonical_plan()
    missing_field_claim = missing_field_plan["condition_claims"][0]
    assert isinstance(missing_field_claim, dict)
    missing_field_claim.pop("origin_parser")

    missing_result = validation.validate_executable_plan(missing_field_plan)
    assert missing_result.status == validation.INTERNAL_INVALID
    assert "condition_claim_schema_invalid" in {
        issue.code for issue in missing_result.issues
    }

    forged_id_plan = _valid_canonical_plan()
    forged_claim = forged_id_plan["condition_claims"][0]
    assert isinstance(forged_claim, dict)
    forged_claim["claim_id"] = "claim_forged"

    forged_result = validation.validate_executable_plan(forged_id_plan)
    assert forged_result.status == validation.INTERNAL_INVALID
    assert "condition_claim_identity_invalid" in {
        issue.code for issue in forged_result.issues
    }


def test_duplicate_and_dangling_owned_claims_are_rejected() -> None:
    duplicate_plan = _valid_canonical_plan()
    duplicate_plan["condition_claims"].append(
        deepcopy(duplicate_plan["condition_claims"][0])
    )

    duplicate_result = validation.validate_executable_plan(duplicate_plan)
    assert duplicate_result.status == validation.INTERNAL_INVALID
    assert "condition_claim_duplicate" in {
        issue.code for issue in duplicate_result.issues
    }

    dangling_plan = _valid_canonical_plan()
    other_expression = targeting_expression.PredicateRef(
        predicate_kind="MemberPredicate",
        semantic_key="member.age:40:49",
        source_spans=((4, 7),),
        payload={"age_min": 40, "age_max": 49},
    )
    dangling_claim = targeting_expression.ConditionClaim(
        source_spans=other_expression.source_spans,
        expression_node_id=other_expression.expression_node_id,
        parent_expression_node_id=None,
        predicate_kind=other_expression.predicate_kind,
        semantic_key=other_expression.semantic_key,
        owner="set_expressions",
        status="resolved",
        disposition="owned",
        origin_parser="test_parser",
        issues=(),
    )
    dangling_plan["condition_claims"] = [dangling_claim.to_dict()]

    dangling_result = validation.validate_executable_plan(dangling_plan)
    assert dangling_result.status == validation.INTERNAL_INVALID
    assert {
        "condition_claim_expression_mismatch",
        "canonical_owner_missing",
    }.issubset({issue.code for issue in dangling_result.issues})


def test_explicit_projection_loss_blocks_and_collects_ids() -> None:
    plan = {
        "projection_losses": [
            {
                "code": "projection_loss",
                "path": "target_user.aggregate_conditions[1]",
                "lost_claim_ids": ["claim-2", "claim-2"],
                "source_span_id": "span-2",
            }
        ]
    }

    result = validation.validate_executable_plan(plan)

    assert result.status == "internal_invalid"
    assert result.blocking_claim_ids == ("claim-2",)
    assert result.unresolved_span_ids == ("span-2",)
    assert result.issues[0].path == "target_user.aggregate_conditions[1]"


def test_current_source_coverage_marker_is_projection_loss() -> None:
    result = validation.validate_executable_plan({
        "unresolved_source_conditions": [
            {
                "id": "usr_deadbeef",
                "path": "source_coverage.unresolved[0]",
                "status": "unresolved",
                "label": "dropped source condition",
            }
        ]
    })

    assert result.status == "internal_invalid"
    assert result.issues[0].code == "projection_loss"
    # Current ``usr_*`` values identify unresolved records, not source spans.
    assert result.unresolved_span_ids == ()


def test_partial_coercion_issue_blocks_without_inspecting_values() -> None:
    result = validation.validate_executable_plan({
        "coercion_issues": [
            {
                "code": "partial_coercion",
                "path": "target_user.aggregate_conditions[1]",
                "claim_id": "claim-coercion",
                "span_id": "span-coercion",
            }
        ]
    })

    assert result.status == "internal_invalid"
    assert result.blocking_claim_ids == ("claim-coercion",)
    assert result.unresolved_span_ids == ("span-coercion",)


def test_known_unsupported_semantic_operation_is_unsupported() -> None:
    plan = {
        "literal_bindings": [],
        "semantic_ir": {
            "status": "unsupported",
            "operations": [],
            "missing_fields": [],
            "policy_applications": [],
            "unsupported_operations": [
                {
                    "kind": "forecast_purchase_growth",
                    "reason": "forecast_not_supported",
                    "evidence": "forecast",
                }
            ],
            "message": None,
        },
    }

    result = validation.validate_executable_plan(plan)

    assert result.status == "unsupported"
    assert any(issue.path == "semantic_ir.unsupported_operations[0]" for issue in result.issues)


def test_explicit_presence_absence_conflict_has_semantic_conflict_status() -> None:
    result = validation.validate_executable_plan({
        "unsupported": {"reason": "presence_absence_conflict"}
    })

    assert result.status == "semantic_conflict"
    assert result.issues[0].code == "presence_absence_conflict"


def test_explicit_missing_fields_require_clarification() -> None:
    plan = {
        "literal_bindings": [],
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["baseline", "current"],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": "A renderer may use this later.",
        },
        "aggregation_request": {"unresolvedFields": ["metric.field"]},
    }

    result = validation.validate_executable_plan(plan)

    assert result.status == "clarification_required"
    assert {issue.path for issue in result.issues} == {
        "semantic_ir.baseline",
        "semantic_ir.current",
        "aggregation_request.unresolvedFields[0]",
    }
    assert all(not hasattr(issue, "message") for issue in result.issues)


def test_schema_invalid_status_has_precedence_over_other_blockers() -> None:
    result = validation.validate_executable_plan({
        "event_expression": {},
        "unsupported": {"reason": "event_not_registered"},
        "missing_required_fields": ["target_user.metric"],
    })

    assert result.status == "internal_invalid"
    assert {issue.status for issue in result.issues} == {
        "internal_invalid",
        "unsupported",
        "clarification_required",
    }


def test_validation_is_pure_idempotent_and_does_not_require_deepcopy() -> None:
    class NoDeepcopyDict(dict):
        def __deepcopy__(self, _memo):  # pragma: no cover - must never be called
            raise AssertionError("validate_executable_plan must not deepcopy its input")

    plan = NoDeepcopyDict({
        "set_expressions": [
            {"requires_clarification": True, "blocking_claim_ids": ["claim-1"]}
        ],
        "metadata": {"untouched": [1, 2, 3]},
    })
    before = json.dumps(plan, sort_keys=True)

    first = validation.validate_executable_plan(plan)
    second = validation.validate_executable_plan(plan)

    assert first == second
    assert json.dumps(plan, sort_keys=True) == before


def test_non_mapping_plan_is_internal_invalid() -> None:
    result = validation.validate_executable_plan([])

    assert result.status == "internal_invalid"
    assert result.issues[0].code == "plan_not_mapping"
