from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import graph_rag
import plan_validation
import targeting_expression


def _single_age_condition_plan() -> dict[str, Any]:
    return {
        "intent": "find_user_segment",
        "target_user": {"age_min": 30, "age_max": 39},
        "campaign_constraints": {},
    }


def _event_and_member_compilable_plan() -> dict[str, Any]:
    plan = graph_rag.build_query_plan(
        "최근 6개월 구매 있고 최근 1개월 구매 없는 고객",
        parser="rules",
    )
    # The event expression makes the event builder applicable.  This explicit
    # scope also makes the member builder applicable, so both lower entrypoints
    # have a meaningful positive control before admission is tested.
    plan["member_scope"] = "all"
    return plan


def _unresolved_canonical_state() -> dict[str, Any]:
    expression = targeting_expression.PredicateRef(
        predicate_kind="UnresolvedPredicate",
        semantic_key="unresolved:test",
        source_spans=((0, 3),),
        payload={"status": "unresolved"},
    )
    claim = targeting_expression.ConditionClaim(
        source_spans=expression.source_spans,
        expression_node_id=expression.expression_node_id,
        parent_expression_node_id=None,
        predicate_kind=expression.predicate_kind,
        semantic_key=expression.semantic_key,
        owner=None,
        status="unresolved",
        disposition="unresolved",
        origin_parser="test_parser",
        issues=({"code": "canonical_owner_missing"},),
    )
    return {
        "ownership_reconciliation_complete": True,
        "canonical_targeting_version": 1,
        "canonical_targeting_expression": expression.to_dict(),
        "canonical_targeting_validation": {"status": "valid", "issues": []},
        "condition_claims": [claim.to_dict()],
    }


def test_all_sql_entrypoints_share_the_same_explicit_clarification_blocker() -> None:
    control = _event_and_member_compilable_plan()

    assert graph_rag.compile_executable_plan(deepcopy(control)) is not None
    assert graph_rag.build_sql_template_candidate(deepcopy(control)) is not None
    assert graph_rag.build_member_targets_sql_candidate(deepcopy(control)) is not None
    assert graph_rag.build_event_expression_sql_candidate(deepcopy(control)) is not None

    blocked = deepcopy(control)
    blocked["set_expressions"] = [
        {
            "expression_id": "ambiguous-set-expression",
            "requires_clarification": True,
            "set_ast": {"type": "unknown_operand", "raw_text": "모호한 조건"},
            "blocking_claim_ids": ["claim-set"],
            "unresolved_span_ids": ["span-set"],
        }
    ]
    expected = plan_validation.validate_executable_plan(blocked)

    assert expected.status == plan_validation.CLARIFICATION_REQUIRED
    assert expected.blocking_claim_ids == ("claim-set",)
    assert expected.unresolved_span_ids == ("span-set",)

    entrypoints = (
        graph_rag.compile_executable_plan,
        graph_rag.build_sql_template_candidate,
        graph_rag.build_member_targets_sql_candidate,
        graph_rag.build_event_expression_sql_candidate,
    )
    for entrypoint in entrypoints:
        call_plan = deepcopy(blocked)

        assert entrypoint(call_plan) is None, entrypoint.__name__
        assert plan_validation.validate_executable_plan(call_plan) == expected


def test_valid_single_condition_sql_is_unchanged_by_the_admission_facade() -> None:
    plan = _single_age_condition_plan()

    facade_candidate = graph_rag.compile_executable_plan(deepcopy(plan))
    compatibility_candidate = graph_rag.build_sql_template_candidate(deepcopy(plan))
    lower_candidate = graph_rag.build_member_targets_sql_candidate(deepcopy(plan))

    assert facade_candidate is not None
    assert compatibility_candidate is not None
    assert lower_candidate is not None
    assert facade_candidate["id"] == "sql_template:member_targets"
    assert facade_candidate["sql"] == compatibility_candidate["sql"] == lower_candidate["sql"]
    assert "B.AGE >= 30" in facade_candidate["sql"]
    assert "B.AGE <= 39" in facade_candidate["sql"]


def test_builder_discovered_projection_loss_never_returns_partial_sql() -> None:
    plan = _single_age_condition_plan()
    plan["target_user"]["interests"] = ["golf"]

    # Interest support is discovered while the member builder projects logical
    # slots.  Admission before projection is therefore still executable, but
    # no public SQL path may return the age-only candidate.
    assert plan_validation.validate_executable_plan(plan).status == plan_validation.EXECUTABLE
    for entrypoint in (
        graph_rag.compile_executable_plan,
        graph_rag.build_sql_template_candidate,
        graph_rag.build_member_targets_sql_candidate,
    ):
        assert entrypoint(deepcopy(plan)) is None, entrypoint.__name__


def test_public_lower_builders_do_not_expose_unvalidated_wrapped_functions() -> None:
    builders = graph_rag._sql_target_builders()

    assert builders
    assert all(getattr(builder, "_requires_plan_validation", False) for builder in builders)
    assert all(not hasattr(builder, "__wrapped__") for builder in builders)


def test_removing_canonical_bundle_from_event_plan_is_invalid_not_legacy_fallback() -> None:
    plan = _event_and_member_compilable_plan()
    assert plan.get("event_expression")
    assert plan.get("canonical_targeting_expression")

    for key in (
        "canonical_targeting_expression",
        "canonical_targeting_version",
        "condition_claims",
        "canonical_projection",
        "canonical_targeting_validation",
        "canonical_blocking_claim_ids",
        "canonical_unresolved_span_ids",
        "ownership_reconciliation_complete",
    ):
        plan.pop(key, None)

    validation = plan_validation.validate_executable_plan(plan)

    assert validation.status == plan_validation.INTERNAL_INVALID
    assert "canonical_targeting_expression_missing" in {
        issue.code for issue in validation.issues
    }
    assert graph_rag.compile_executable_plan(deepcopy(plan)) is None
    assert graph_rag.build_event_expression_sql_candidate(deepcopy(plan)) is None


def test_reconciled_legacy_only_single_condition_keeps_compatibility_policy() -> None:
    plan = _single_age_condition_plan()
    plan.update({
        "set_expressions": [],
        "canonical_targeting_validation": {"status": "valid", "issues": []},
        "ownership_reconciliation_complete": True,
    })

    assert plan_validation.validate_executable_plan(plan).status == plan_validation.EXECUTABLE
    candidate = graph_rag.compile_executable_plan(plan)
    assert candidate is not None
    assert candidate["id"] == "sql_template:member_targets"


@pytest.mark.parametrize(
    ("canonical_state", "expected_status", "expected_code"),
    [
        (
            _unresolved_canonical_state(),
            plan_validation.CLARIFICATION_REQUIRED,
            "canonical_claim_unresolved",
        ),
        (
            {
                "event_semantic_validation": {
                    "status": "unknown",
                    "issues": [{"code": "semantic_domain_unknown"}],
                }
            },
            plan_validation.CLARIFICATION_REQUIRED,
            "semantic_domain_unknown",
        ),
        (
            {
                "event_compiler_capability": {
                    "status": "partially_supported",
                    "issues": [{"code": "event_field_not_registered"}],
                }
            },
            plan_validation.UNSUPPORTED,
            "event_compiler_partially_supported",
        ),
        (
            {
                "canonical_projection": {
                    "status": "partially_supported",
                    "silent_drop_count": 0,
                    "legacy_semantic_loss": False,
                }
            },
            plan_validation.UNSUPPORTED,
            "canonical_projection_partially_supported",
        ),
    ],
    ids=(
        "unresolved-canonical-claim",
        "semantic-unknown",
        "partial-compiler-capability",
        "partial-canonical-projection",
    ),
)
def test_pr1b_canonical_blockers_stop_facade_and_direct_builder(
    canonical_state: dict[str, Any],
    expected_status: plan_validation.PlanValidationStatus,
    expected_code: str,
) -> None:
    plan = _single_age_condition_plan()
    plan.update(deepcopy(canonical_state))

    validation = plan_validation.validate_executable_plan(plan)

    assert validation.status == expected_status
    assert expected_code in {issue.code for issue in validation.issues}
    assert graph_rag.compile_executable_plan(deepcopy(plan)) is None
    assert graph_rag.build_sql_template_candidate(deepcopy(plan)) is None
    assert graph_rag.build_member_targets_sql_candidate(deepcopy(plan)) is None


def test_stale_validation_result_cannot_compile_a_semantically_changed_plan() -> None:
    plan = _single_age_condition_plan()
    admitted = plan_validation.validate_executable_plan(plan)
    assert admitted.status == plan_validation.EXECUTABLE

    # The validation status remains executable, but this is no longer the plan
    # for which ``admitted`` was obtained.  A validated result/token must be
    # bound to plan content, not merely compared by status and issue tuples.
    plan["target_user"] = {"age_min": 40, "age_max": 49}

    assert plan_validation.validate_executable_plan(plan).status == plan_validation.EXECUTABLE
    assert graph_rag.compile_executable_plan(plan, validation_result=admitted) is None
