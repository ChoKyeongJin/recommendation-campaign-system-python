"""Read-only executable-plan admission checks.

This module is intentionally narrower than the planners and compilers.  It does
not resolve ownership, simplify boolean expressions, infer missing meaning, or
create user-facing messages.  It only consolidates failure states that an
earlier planning stage has already made explicit, plus structural IR validation
that can be performed without changing the plan.

The validator is suitable for an admission boundary: calls are deterministic,
idempotent, and never mutate (or defensively copy and mutate) the input plan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import condition_evaluation_ir
import event_ir
import targeting_expression as canonical_targeting_ir
from query_structurer.semantic_ir import validate_semantic_ir

PlanValidationStatus = Literal[
    "executable",
    "clarification_required",
    "semantic_conflict",
    "unsupported",
    "internal_invalid",
]

EXECUTABLE: PlanValidationStatus = "executable"
CLARIFICATION_REQUIRED: PlanValidationStatus = "clarification_required"
SEMANTIC_CONFLICT: PlanValidationStatus = "semantic_conflict"
UNSUPPORTED: PlanValidationStatus = "unsupported"
INTERNAL_INVALID: PlanValidationStatus = "internal_invalid"


@dataclass(frozen=True, slots=True)
class PlanValidationIssue:
    """One machine-readable reason why a plan is not executable.

    Deliberately absent are ``message`` and ``clarification_question`` fields.
    Rendering a user response belongs to a later boundary and must not leak into
    executable-plan validation.
    """

    code: str
    status: PlanValidationStatus
    path: str
    blocking_claim_ids: tuple[str, ...] = ()
    unresolved_span_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    status: PlanValidationStatus
    issues: tuple[PlanValidationIssue, ...]
    blocking_claim_ids: tuple[str, ...]
    unresolved_span_ids: tuple[str, ...]
    # Admission token bound to the exact plan content.  It is excluded from
    # semantic equality so existing callers comparing validation decisions keep
    # their contract; the SQL facade checks it explicitly before compilation.
    plan_fingerprint: str = field(default="", compare=False)


_STATUS_PRIORITY: dict[PlanValidationStatus, int] = {
    EXECUTABLE: 0,
    CLARIFICATION_REQUIRED: 1,
    UNSUPPORTED: 2,
    SEMANTIC_CONFLICT: 3,
    INTERNAL_INVALID: 4,
}

_SEMANTIC_CONFLICT_CODES = frozenset({
    "presence_absence_conflict",
    "aggregate_semantic_conflict",
    "semantic_conflict",
    "same_attribute_opposite_polarity",
    "conflicting_aggregate_conditions",
})

_PROJECTION_COLLECTION_KEYS = (
    "projection_losses",
    "projection_issues",
)
_PARTIAL_COERCION_COLLECTION_KEYS = (
    "partial_coercion_issues",
    "coercion_issues",
)
_MISSING_FIELD_KEYS = (
    "missing_required_fields",
    "required_fields_missing",
)

_CLAIM_ID_KEYS = (
    "claim_id",
    "claim_ids",
    "blocking_claim_ids",
    "dropped_claim_id",
    "dropped_claim_ids",
    "lost_claim_id",
    "lost_claim_ids",
)
_SPAN_ID_KEYS = (
    "span_id",
    "span_ids",
    "unresolved_span_ids",
    "source_span_id",
    "source_span_ids",
)

_CANONICAL_CLAIM_REQUIRED_FIELDS = frozenset({
    "claim_id",
    "source_spans",
    "expression_node_id",
    "parent_expression_node_id",
    "predicate_kind",
    "semantic_key",
    "owner",
    "status",
    "disposition",
    "origin_parser",
    "issues",
})


def validate_executable_plan(plan: Any) -> PlanValidationResult:
    """Return the PR-1A execution status without changing ``plan``.

    The accepted evidence is intentionally closed:

    * an explicit set-expression clarification flag;
    * structurally invalid semantic/event/condition-evaluation IR;
    * an explicit projection-loss or partial-coercion issue;
    * an explicitly unsupported operation or plan state;
    * an explicitly recorded missing required field;
    * an explicit semantic-conflict code.

    In particular, this function does not traverse a boolean AST looking for
    unknown operands and does not rerun ownership reconciliation.
    """

    if not isinstance(plan, Mapping):
        issue = PlanValidationIssue(
            code="plan_not_mapping",
            status=INTERNAL_INVALID,
            path="$",
        )
        return PlanValidationResult(INTERNAL_INVALID, (issue,), (), ())

    fingerprint = _plan_fingerprint(plan)

    issues: list[PlanValidationIssue] = []
    _collect_structural_ir_issues(plan, issues)
    _collect_set_expression_issues(plan, issues)
    _collect_projection_loss_issues(plan, issues)
    _collect_partial_coercion_issues(plan, issues)
    _collect_semantic_conflicts(plan, issues)
    _collect_unsupported_operations(plan, issues)
    _collect_missing_required_fields(plan, issues)
    _collect_canonical_ownership_issues(plan, issues)
    _collect_semantic_capability_issues(plan, issues)
    _collect_unresolved_source_conditions(plan, issues)

    unique_issues = _unique_issues(issues)
    if not unique_issues:
        return PlanValidationResult(EXECUTABLE, (), (), (), fingerprint)

    status = max(
        (issue.status for issue in unique_issues),
        key=_STATUS_PRIORITY.__getitem__,
    )
    claim_ids = _unique_strings(
        identifier
        for issue in unique_issues
        for identifier in issue.blocking_claim_ids
    )
    span_ids = _unique_strings(
        identifier
        for issue in unique_issues
        for identifier in issue.unresolved_span_ids
    )

    # A producer may already have aggregated these exact, explicitly named ID
    # sets.  Reading them is safe; discovering IDs by walking claims/spans would
    # amount to recomputing ownership and is deliberately not done here.
    claim_ids = _unique_strings((*claim_ids, *_ids_from_keys(
        plan, ("blocking_claim_ids", "canonical_blocking_claim_ids")
    )))
    span_ids = _unique_strings((*span_ids, *_ids_from_keys(
        plan, ("unresolved_span_ids", "canonical_unresolved_span_ids")
    )))
    return PlanValidationResult(status, unique_issues, claim_ids, span_ids, fingerprint)


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    """Return a deterministic admission token without changing ``plan``."""

    def default(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return {"__type__": type(value).__name__, "value": value.isoformat()}
        if isinstance(value, Path):
            return {"__type__": "Path", "value": str(value)}
        if isinstance(value, (set, frozenset)):
            return {
                "__type__": type(value).__name__,
                "value": sorted(value, key=repr),
            }
        return {
            "__type__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": repr(value),
        }

    encoded = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collect_structural_ir_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    if "semantic_ir" in plan:
        try:
            validate_semantic_ir(
                plan.get("semantic_ir"),
                plan.get("literal_bindings"),
                payload=plan,  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError):
            issues.append(_issue(
                INTERNAL_INVALID,
                "semantic_ir_schema_invalid",
                "semantic_ir",
                plan.get("semantic_ir"),
            ))

    if "event_expression" in plan:
        payload = plan.get("event_expression")
        try:
            if not isinstance(payload, Mapping) or not isinstance(payload.get("expression"), Mapping):
                raise event_ir.IrSchemaError("event expression payload is incomplete")
            event_ir.condition_from_dict(payload["expression"])
        except (KeyError, TypeError, ValueError, event_ir.IrSchemaError):
            issues.append(_issue(
                INTERNAL_INVALID,
                "event_expression_schema_invalid",
                "event_expression.expression",
                payload,
            ))

    if condition_evaluation_ir.PLAN_KEY in plan:
        validation_issues = condition_evaluation_ir.validate_evaluations(
            plan.get(condition_evaluation_ir.PLAN_KEY)
        )
        for validation_issue in validation_issues:
            status = _status_for_validation_code(validation_issue.code)
            issues.append(_issue(
                status,
                "condition_evaluation_ir_" + validation_issue.code,
                validation_issue.path,
            ))

    aggregation_validation = plan.get("aggregation_request_validation")
    if isinstance(aggregation_validation, Mapping) and aggregation_validation.get("valid") is False:
        raw_errors = aggregation_validation.get("errors")
        errors = raw_errors if isinstance(raw_errors, list) and raw_errors else [aggregation_validation]
        for index, error in enumerate(errors):
            source = error if isinstance(error, Mapping) else None
            code = _marker(source) or "aggregation_request_schema_invalid"
            status = _status_for_validation_code(code)
            path = _path(source, f"aggregation_request_validation.errors[{index}]")
            issues.append(_issue(status, "aggregation_request_" + code, path, source))


def _collect_set_expression_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    expressions = plan.get("set_expressions")
    if "set_expressions" in plan and not isinstance(expressions, list):
        issues.append(_issue(
            INTERNAL_INVALID,
            "set_expressions_schema_invalid",
            "set_expressions",
        ))
        return
    if not isinstance(expressions, list):
        return
    for index, expression in enumerate(expressions):
        if not isinstance(expression, Mapping):
            issues.append(_issue(
                INTERNAL_INVALID,
                "set_expression_schema_invalid",
                f"set_expressions[{index}]",
            ))
            continue
        if expression.get("requires_clarification") is True:
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "set_expression_requires_clarification",
                f"set_expressions[{index}]",
                expression,
            ))
        elif not isinstance(expression.get("set_ast"), Mapping):
            # Shape validation only.  The AST is not simplified or searched for
            # unknown operands here; that remains the reconciliation stage's job.
            issues.append(_issue(
                INTERNAL_INVALID,
                "set_expression_schema_invalid",
                f"set_expressions[{index}].set_ast",
                expression,
            ))


def _collect_projection_loss_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    if _explicit_flag(plan.get("projection_loss")):
        issues.append(_issue(
            INTERNAL_INVALID,
            "projection_loss",
            "projection_loss",
            plan.get("projection_loss"),
        ))

    for key in _PROJECTION_COLLECTION_KEYS:
        for index, item in _items(plan.get(key)):
            issues.append(_issue(
                INTERNAL_INVALID,
                _marker(item) or "projection_loss",
                _path(item, f"{key}[{index}]"),
                item,
            ))

    projection = plan.get("projection")
    if isinstance(projection, Mapping) and _marker(projection) in {
        "loss", "lossy", "partial", "projection_loss", "invalid"
    }:
        issues.append(_issue(
            INTERNAL_INVALID,
            "projection_loss",
            "projection",
            projection,
        ))

    delivery = plan.get("delivery_validation")
    if isinstance(delivery, Mapping):
        failure_reasons = _as_strings(delivery.get("failure_reasons"))
        if delivery.get("failure_reason") == "critical_conditions_dropped":
            failure_reasons = (*failure_reasons, "critical_conditions_dropped")
        if "critical_conditions_dropped" in failure_reasons:
            issues.append(_issue(
                INTERNAL_INVALID,
                "projection_loss",
                "delivery_validation",
                delivery,
            ))


def _collect_partial_coercion_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    if _explicit_flag(plan.get("partial_coercion")):
        issues.append(_issue(
            INTERNAL_INVALID,
            "partial_coercion",
            "partial_coercion",
            plan.get("partial_coercion"),
        ))

    for key in _PARTIAL_COERCION_COLLECTION_KEYS:
        for index, item in _items(plan.get(key)):
            if key == "coercion_issues" and not _is_partial_coercion_issue(item):
                continue
            issues.append(_issue(
                INTERNAL_INVALID,
                _marker(item) or "partial_coercion",
                _path(item, f"{key}[{index}]"),
                item,
            ))

    coercion = plan.get("coercion")
    if isinstance(coercion, Mapping) and (
        _marker(coercion) in {"partial", "partial_coercion", "item_dropped"}
        or coercion.get("partial") is True
    ):
        issues.append(_issue(
            INTERNAL_INVALID,
            "partial_coercion",
            "coercion",
            coercion,
        ))


def _collect_semantic_conflicts(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    conflicts = plan.get("semantic_conflicts")
    for index, conflict in _items(conflicts):
        issues.append(_issue(
            SEMANTIC_CONFLICT,
            _marker(conflict) or "semantic_conflict",
            _path(conflict, f"semantic_conflicts[{index}]"),
            conflict,
        ))


def _collect_unsupported_operations(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    semantic_ir = plan.get("semantic_ir")
    if isinstance(semantic_ir, Mapping):
        status = semantic_ir.get("status")
        if status == "unsupported":
            operations = semantic_ir.get("unsupported_operations")
            items = list(_items(operations))
            if not items:
                issues.append(_issue(
                    UNSUPPORTED,
                    "semantic_operation_unsupported",
                    "semantic_ir.unsupported_operations",
                    semantic_ir,
                ))
            for index, operation in items:
                issues.append(_issue(
                    UNSUPPORTED,
                    _marker(operation) or "semantic_operation_unsupported",
                    f"semantic_ir.unsupported_operations[{index}]",
                    operation,
                ))

    for key in ("unsupported_operations", "known_unsupported_operations"):
        for index, operation in _items(plan.get(key)):
            issues.append(_issue(
                UNSUPPORTED,
                _marker(operation) or "operation_unsupported",
                _path(operation, f"{key}[{index}]"),
                operation,
            ))

    unsupported = plan.get("unsupported")
    if unsupported:
        code = _marker(unsupported) or "plan_unsupported"
        status = SEMANTIC_CONFLICT if _is_semantic_conflict_code(code) else UNSUPPORTED
        issues.append(_issue(status, code, "unsupported", unsupported))


def _collect_missing_required_fields(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    semantic_ir = plan.get("semantic_ir")
    if isinstance(semantic_ir, Mapping) and semantic_ir.get("status") == "needs_clarification":
        missing_fields = semantic_ir.get("missing_fields")
        items = list(_items(missing_fields))
        if not items:
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "required_field_missing",
                "semantic_ir.missing_fields",
                semantic_ir,
            ))
        for index, field in items:
            name = field if isinstance(field, str) and field else str(index)
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "required_field_missing",
                f"semantic_ir.{name}",
                field,
            ))

    aggregation_request = plan.get("aggregation_request")
    if isinstance(aggregation_request, Mapping):
        for index, field in _items(aggregation_request.get("unresolvedFields")):
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "required_field_missing",
                f"aggregation_request.unresolvedFields[{index}]",
                field,
            ))

    for key in _MISSING_FIELD_KEYS:
        for index, field in _items(plan.get(key)):
            path = (
                field
                if isinstance(field, str) and field
                else _path(field, f"{key}[{index}]")
            )
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "required_field_missing",
                path,
                field,
            ))

    input_validation = plan.get("input_validation")
    if isinstance(input_validation, Mapping) and input_validation.get("is_satisfied") is False:
        for index, missing in _items(input_validation.get("missing_conditions")):
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                "required_field_missing",
                _path(missing, f"input_validation.missing_conditions[{index}]"),
                missing,
            ))

    computed_metrics = plan.get("computed_metrics")
    if isinstance(computed_metrics, list):
        for index, metric in enumerate(computed_metrics):
            if isinstance(metric, Mapping) and metric.get("requires_clarification") is True:
                issues.append(_issue(
                    CLARIFICATION_REQUIRED,
                    "required_field_missing",
                    f"computed_metrics[{index}]",
                    metric,
                ))

    resolutions = plan.get("semantic_resolutions")
    if isinstance(resolutions, list):
        for index, resolution in enumerate(resolutions):
            if isinstance(resolution, Mapping) and resolution.get("requires_clarification") is True:
                issues.append(_issue(
                    CLARIFICATION_REQUIRED,
                    "required_field_missing",
                    f"semantic_resolutions[{index}]",
                    resolution,
                ))

    policies = plan.get("policy_constraints")
    if isinstance(policies, list):
        for index, policy in enumerate(policies):
            if not isinstance(policy, Mapping):
                continue
            if (
                policy.get("sql_behavior") == "filter"
                and policy.get("requires_threshold") is True
                and policy.get("threshold_krw") is None
            ):
                issues.append(_issue(
                    CLARIFICATION_REQUIRED,
                    "required_field_missing",
                    f"policy_constraints[{index}].threshold_krw",
                    policy,
                ))


def _canonical_expression_shape_is_valid(
    payload: Any,
    path: str,
    issues: list[PlanValidationIssue],
) -> bool:
    """Check the serialized canonical-v1 envelope before typed deserialization."""

    if not isinstance(payload, Mapping):
        issues.append(_issue(
            INTERNAL_INVALID,
            "canonical_targeting_expression_schema_invalid",
            path,
        ))
        return False

    valid = True
    node_type = payload.get("type")
    if node_type not in {"predicate_ref", "and", "or", "not"}:
        issues.append(_issue(
            INTERNAL_INVALID,
            "canonical_targeting_expression_schema_invalid",
            f"{path}.type",
        ))
        return False

    for field_name in ("expression_node_id", "canonical_fingerprint"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            valid = False
            issues.append(_issue(
                INTERNAL_INVALID,
                "canonical_targeting_expression_schema_invalid",
                f"{path}.{field_name}",
            ))

    if node_type == "predicate_ref":
        for field_name in ("predicate_kind", "semantic_key", "source_spans", "payload"):
            if field_name not in payload:
                valid = False
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "canonical_targeting_expression_schema_invalid",
                    f"{path}.{field_name}",
                ))
        return valid

    if node_type in {"and", "or"}:
        children = payload.get("children")
        if not isinstance(children, list) or len(children) < 2:
            issues.append(_issue(
                INTERNAL_INVALID,
                "canonical_targeting_expression_schema_invalid",
                f"{path}.children",
            ))
            return False
        return all(
            _canonical_expression_shape_is_valid(child, f"{path}.children[{index}]", issues)
            for index, child in enumerate(children)
        ) and valid

    operand = payload.get("operand")
    if not isinstance(operand, Mapping):
        issues.append(_issue(
            INTERNAL_INVALID,
            "canonical_targeting_expression_schema_invalid",
            f"{path}.operand",
        ))
        return False
    return _canonical_expression_shape_is_valid(operand, f"{path}.operand", issues) and valid


def _canonical_claim_shape_is_valid(
    claim: Any,
    path: str,
    issues: list[PlanValidationIssue],
) -> bool:
    if not isinstance(claim, Mapping):
        issues.append(_issue(INTERNAL_INVALID, "condition_claim_schema_invalid", path))
        return False
    missing = sorted(_CANONICAL_CLAIM_REQUIRED_FIELDS - claim.keys())
    for field_name in missing:
        issues.append(_issue(
            INTERNAL_INVALID,
            "condition_claim_schema_invalid",
            f"{path}.{field_name}",
            claim,
        ))
    if missing:
        return False
    if not isinstance(claim.get("claim_id"), str) or not claim["claim_id"]:
        issues.append(_issue(
            INTERNAL_INVALID,
            "condition_claim_schema_invalid",
            f"{path}.claim_id",
            claim,
        ))
        return False
    return True


def _canonical_predicate_index(
    expression: canonical_targeting_ir.TargetingExpression,
) -> dict[str, list[tuple[canonical_targeting_ir.PredicateRef, str | None]]]:
    index: dict[str, list[tuple[canonical_targeting_ir.PredicateRef, str | None]]] = {}

    def visit(
        node: canonical_targeting_ir.TargetingExpression,
        parent_id: str | None,
    ) -> None:
        if isinstance(node, canonical_targeting_ir.PredicateRef):
            index.setdefault(node.expression_node_id, []).append((node, parent_id))
        elif isinstance(node, canonical_targeting_ir.Not):
            visit(node.operand, node.expression_node_id)
        elif isinstance(node, (canonical_targeting_ir.And, canonical_targeting_ir.Or)):
            for child in node.children:
                visit(child, node.expression_node_id)

    visit(expression, None)
    return index


def _collect_canonical_ownership_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    reconciliation_complete = plan.get("ownership_reconciliation_complete") is True
    expression_payload = plan.get("canonical_targeting_expression")
    expression_present = expression_payload is not None
    claims_payload = plan.get("condition_claims")

    event_payload = plan.get("event_expression")
    event_requires_canonical = (
        isinstance(event_payload, Mapping)
        and isinstance(event_payload.get("expression"), Mapping)
    )
    set_requires_canonical = (
        reconciliation_complete
        and isinstance(plan.get("set_expressions"), list)
        and bool(plan.get("set_expressions"))
    )

    # Compatibility policy: a legacy-only plan with no Event/Set Boolean source
    # remains executable.  Once either source exists (or a canonical bundle has
    # started), deleting the ownership metadata is an invalid hybrid plan.
    if not expression_present:
        incomplete_bundle = (
            "canonical_targeting_version" in plan
            or "condition_claims" in plan
            or (
                isinstance(plan.get("canonical_projection"), Mapping)
                and plan["canonical_projection"].get("status") == "supported"
            )
        )
        if event_requires_canonical or set_requires_canonical or incomplete_bundle:
            issues.append(_issue(
                INTERNAL_INVALID,
                "canonical_targeting_expression_missing",
                "canonical_targeting_expression",
            ))
    else:
        if not reconciliation_complete:
            issues.append(_issue(
                INTERNAL_INVALID,
                "ownership_reconciliation_incomplete",
                "ownership_reconciliation_complete",
            ))
        if plan.get("canonical_targeting_version") != 1:
            issues.append(_issue(
                INTERNAL_INVALID,
                "canonical_targeting_version_unsupported",
                "canonical_targeting_version",
            ))

    validation = plan.get("canonical_targeting_validation")
    if isinstance(validation, Mapping) and validation.get("status") == "invalid":
        entries = list(_items(validation.get("issues"))) or [(0, validation)]
        for index, item in entries:
            issues.append(_issue(
                INTERNAL_INVALID,
                _marker(item) or "condition_claim_invariant",
                _path(item, f"canonical_targeting_validation.issues[{index}]"),
                item,
            ))

    typed_expression: canonical_targeting_ir.TargetingExpression | None = None
    if expression_present:
        shape_valid = _canonical_expression_shape_is_valid(
            expression_payload, "canonical_targeting_expression", issues
        )
        if shape_valid:
            try:
                typed_expression = canonical_targeting_ir.targeting_expression_from_dict(
                    expression_payload
                )
            except canonical_targeting_ir.TargetingExpressionInvariantError:
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "canonical_targeting_identity_invalid",
                    "canonical_targeting_expression",
                ))
            except (TypeError, ValueError):
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "canonical_targeting_expression_schema_invalid",
                    "canonical_targeting_expression",
                ))

    if expression_present and not isinstance(claims_payload, list):
        issues.append(_issue(
            INTERNAL_INVALID,
            "canonical_claims_missing",
            "condition_claims",
        ))
        claims_payload = []

    typed_claims: list[canonical_targeting_ir.ConditionClaim] = []
    claims_structurally_valid = isinstance(claims_payload, list)
    for index, claim in enumerate(claims_payload if isinstance(claims_payload, list) else []):
        path = f"condition_claims[{index}]"
        if not _canonical_claim_shape_is_valid(claim, path, issues):
            claims_structurally_valid = False
            continue
        try:
            typed_claims.append(canonical_targeting_ir.ConditionClaim.from_dict(claim))
        except canonical_targeting_ir.ConditionClaimInvariantError:
            claims_structurally_valid = False
            issues.append(_issue(
                INTERNAL_INVALID,
                "condition_claim_identity_invalid",
                f"{path}.claim_id",
                claim,
            ))
        except (TypeError, ValueError):
            claims_structurally_valid = False
            issues.append(_issue(
                INTERNAL_INVALID,
                "condition_claim_schema_invalid",
                path,
                claim,
            ))

    if claims_structurally_valid and typed_claims:
        try:
            canonical_targeting_ir.validate_condition_claims(typed_claims)
        except canonical_targeting_ir.ConditionClaimInvariantError as exc:
            code = (
                "condition_claim_duplicate"
                if any("duplicate" in detail for detail in exc.issues)
                else "condition_claim_invariant"
            )
            issues.append(_issue(
                INTERNAL_INVALID,
                code,
                "condition_claims",
                {"claim_ids": [claim.claim_id for claim in typed_claims]},
            ))

    if typed_expression is not None and claims_structurally_valid:
        predicate_index = _canonical_predicate_index(typed_expression)
        for index, claim in enumerate(typed_claims):
            if claim.disposition != "owned":
                continue
            matches = predicate_index.get(claim.expression_node_id, [])
            exact_match = any(
                node.predicate_kind == claim.predicate_kind
                and node.semantic_key == claim.semantic_key
                and node.source_spans == claim.source_spans
                and parent_id == claim.parent_expression_node_id
                for node, parent_id in matches
            )
            if not exact_match:
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "condition_claim_expression_mismatch",
                    f"condition_claims[{index}]",
                    claims_payload[index] if isinstance(claims_payload, list) else None,
                ))

        for expression_node_id in sorted(predicate_index):
            node_claims = [
                claim for claim in typed_claims
                if claim.expression_node_id == expression_node_id
            ]
            if not any(claim.disposition == "owned" for claim in node_claims) and not any(
                claim.disposition in {"unresolved", "unsupported", "rejected_conflict"}
                for claim in node_claims
            ):
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "canonical_owner_missing",
                    f"canonical_targeting_expression.{expression_node_id}",
                ))

    for index, claim in enumerate(claims_payload if isinstance(claims_payload, list) else []):
        if not isinstance(claim, Mapping):
            continue
        disposition = claim.get("disposition")
        owner = claim.get("owner")
        if disposition == "owned" and not isinstance(owner, str):
            issues.append(_issue(
                INTERNAL_INVALID,
                "canonical_owner_missing",
                f"condition_claims[{index}].owner",
                claim,
            ))
        if disposition == "rejected_conflict":
            issues.append(_issue(
                SEMANTIC_CONFLICT,
                "canonical_claim_rejected_conflict",
                f"condition_claims[{index}]",
                claim,
            ))
        elif claim.get("status") == "unresolved" or disposition in {"unresolved", "unsupported"}:
            status = UNSUPPORTED if disposition == "unsupported" else CLARIFICATION_REQUIRED
            issues.append(_issue(
                status,
                "canonical_claim_unsupported" if status == UNSUPPORTED else "canonical_claim_unresolved",
                f"condition_claims[{index}]",
                claim,
            ))

    trace = plan.get("condition_reconciliation")
    if reconciliation_complete and isinstance(trace, Mapping) and trace.get("requires_clarification") is True:
        issues.append(_issue(
            CLARIFICATION_REQUIRED,
            "ownership_reconciliation_unresolved",
            "condition_reconciliation",
            trace,
        ))


def _collect_semantic_capability_issues(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    semantic = plan.get("event_semantic_validation")
    if isinstance(semantic, Mapping):
        status = semantic.get("status")
        first = next((item for _index, item in _items(semantic.get("issues"))), None)
        code = _marker(first) or "event_semantic_unknown"
        if status == "contradictory":
            issues.append(_issue(SEMANTIC_CONFLICT, code, "event_semantic_validation", first))
        elif status == "unknown":
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                code,
                "event_semantic_validation",
                first,
            ))
        elif status not in (None, "consistent"):
            issues.append(_issue(
                INTERNAL_INVALID,
                "event_semantic_status_invalid",
                "event_semantic_validation.status",
                semantic,
            ))

    capability = plan.get("event_compiler_capability")
    if isinstance(capability, Mapping):
        status = capability.get("status")
        if status in {"unsupported", "partially_supported"}:
            first = next((item for _index, item in _items(capability.get("issues"))), None)
            issues.append(_issue(
                UNSUPPORTED,
                "event_compiler_" + str(status),
                "event_compiler_capability",
                first,
            ))
        elif status not in (None, "supported"):
            issues.append(_issue(
                INTERNAL_INVALID,
                "event_compiler_capability_status_invalid",
                "event_compiler_capability.status",
                capability,
            ))

    projection = plan.get("canonical_projection")
    if isinstance(projection, Mapping):
        status = projection.get("status")
        if status in {"unsupported", "partially_supported"}:
            issues.append(_issue(
                UNSUPPORTED,
                "canonical_projection_" + str(status),
                "canonical_projection",
                projection,
            ))
        if projection.get("legacy_semantic_loss") is True:
            issues.append(_issue(
                UNSUPPORTED,
                "legacy_semantic_loss",
                "canonical_projection.legacy_semantic_loss",
                projection,
            ))
        silent_drop_count = projection.get("silent_drop_count")
        if isinstance(silent_drop_count, int) and silent_drop_count > 0:
            issues.append(_issue(
                INTERNAL_INVALID,
                "projection_silent_drop",
                "canonical_projection.silent_drop_count",
                projection,
            ))


def _collect_unresolved_source_conditions(
    plan: Mapping[str, Any], issues: list[PlanValidationIssue]
) -> None:
    unresolved = plan.get("unresolved_source_conditions")
    if isinstance(unresolved, list):
        for index, item in enumerate(unresolved):
            if not isinstance(item, Mapping):
                issues.append(_issue(
                    INTERNAL_INVALID,
                    "unresolved_source_condition_invalid",
                    f"unresolved_source_conditions[{index}]",
                ))
                continue
            if item.get("status") not in (None, "unresolved", "invalid", "schema_invalid", "internal_invalid"):
                continue
            path = _path(item, f"unresolved_source_conditions[{index}]")
            code = _marker(item)
            source = str(item.get("source") or "")
            if path.startswith("source_coverage.unresolved["):
                status = INTERNAL_INVALID
                issue_code = "projection_loss"
            elif item.get("status") in {"invalid", "schema_invalid", "internal_invalid"}:
                status = INTERNAL_INVALID
                issue_code = code or "ir_schema_invalid"
            elif source in {"event_ir", "condition_evaluation_ir"}:
                status = _status_for_validation_code(code) if code else INTERNAL_INVALID
                issue_code = code or "ir_schema_invalid"
            else:
                if plan.get("ownership_reconciliation_complete") is not True:
                    continue
                status = CLARIFICATION_REQUIRED
                issue_code = code or "required_field_missing"
            issues.append(_issue(
                status,
                issue_code,
                path,
                item,
            ))

    unresolved_v3 = plan.get("unresolved")
    if isinstance(unresolved_v3, list) and plan.get("ownership_reconciliation_complete") is True:
        for index, item in enumerate(unresolved_v3):
            if not isinstance(item, Mapping):
                continue
            issues.append(_issue(
                CLARIFICATION_REQUIRED,
                _marker(item) or "required_field_missing",
                _path(item, f"unresolved[{index}]"),
                item,
            ))


def _issue(
    status: PlanValidationStatus,
    code: str,
    path: str,
    source: Any = None,
) -> PlanValidationIssue:
    claim_ids = _ids_from_keys(source, _CLAIM_ID_KEYS)
    span_ids = _ids_from_keys(source, _SPAN_ID_KEYS)
    return PlanValidationIssue(
        code=_safe_code(code),
        status=status,
        path=path or "$",
        blocking_claim_ids=claim_ids,
        unresolved_span_ids=span_ids,
    )


def _status_for_validation_code(code: str | None) -> PlanValidationStatus:
    normalized = _safe_code(code or "")
    if _is_semantic_conflict_code(normalized):
        return SEMANTIC_CONFLICT
    if "unsupported" in normalized:
        return UNSUPPORTED
    if "missing" in normalized or "unresolved" in normalized:
        return CLARIFICATION_REQUIRED
    return INTERNAL_INVALID


def _is_semantic_conflict_code(code: str) -> bool:
    normalized = _safe_code(code)
    return normalized in _SEMANTIC_CONFLICT_CODES or "conflict" in normalized


def _is_partial_coercion_issue(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    marker = _marker(item)
    return (
        item.get("partial") is True
        or item.get("is_partial") is True
        or marker in {"partial", "partial_coercion", "item_dropped", "coercion_item_dropped"}
        or ("coerc" in marker and ("partial" in marker or "drop" in marker))
    )


def _explicit_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, Mapping):
        if value.get("detected") is True or value.get("is_lossy") is True or value.get("partial") is True:
            return True
        return _marker(value) in {
            "loss", "lossy", "partial", "projection_loss", "partial_coercion", "item_dropped"
        }
    return isinstance(value, (list, tuple)) and bool(value)


def _marker(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("code", "reason", "type", "kind", "status"):
            marker = value.get(key)
            if isinstance(marker, str) and marker.strip():
                return _safe_code(marker)
    if isinstance(value, str):
        return _safe_code(value)
    return ""


def _safe_code(value: str) -> str:
    normalized = value.strip().casefold().replace(" ", "_").replace("-", "_")
    return normalized or "plan_validation_issue"


def _path(value: Any, default: str) -> str:
    if isinstance(value, Mapping):
        path = value.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return default


def _items(value: Any) -> Iterable[tuple[int, Any]]:
    if isinstance(value, (list, tuple)):
        return enumerate(value)
    if value not in (None, False, "", (), []):
        return enumerate((value,))
    return ()


def _ids_from_keys(value: Any, keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    identifiers: list[str] = []
    for key in keys:
        identifiers.extend(_as_strings(value.get(key)))
    return _unique_strings(identifiers)


def _as_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _unique_issues(issues: Iterable[PlanValidationIssue]) -> tuple[PlanValidationIssue, ...]:
    seen: set[tuple[Any, ...]] = set()
    result: list[PlanValidationIssue] = []
    for issue in issues:
        signature = (
            issue.code,
            issue.status,
            issue.path,
            issue.blocking_claim_ids,
            issue.unresolved_span_ids,
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(issue)
    return tuple(result)


__all__ = [
    "CLARIFICATION_REQUIRED",
    "EXECUTABLE",
    "INTERNAL_INVALID",
    "SEMANTIC_CONFLICT",
    "UNSUPPORTED",
    "PlanValidationIssue",
    "PlanValidationResult",
    "PlanValidationStatus",
    "validate_executable_plan",
]
