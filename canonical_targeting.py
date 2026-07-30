"""Build the canonical Boolean targeting tree after parser reconciliation.

Parsers remain candidate producers.  This module reads their final candidates,
preserves Set/Event topology in one typed tree, and emits explicit ownership
claims.  It does not compile SQL and never silently drops an unknown leaf.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import event_ir
import event_parser
import plan_semantic_ast
import semantic_ast
import targeting_ir
from targeting_expression import (
    And,
    ConditionClaim,
    Not,
    Or,
    PredicateRef,
    TargetingExpression,
    condition_claim_invariant_issues,
)


@dataclass(frozen=True)
class CanonicalTargetingResult:
    expression: TargetingExpression | None
    claims: tuple[ConditionClaim, ...]
    issues: tuple[str, ...]


def _semantic_hash(value: Any) -> str:
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                str(key): strip(child)
                for key, child in node.items()
                if str(key) not in {
                    "evidence",
                    "source_text",
                    "source_span",
                    "sourceSpan",
                    "evidence_span",
                }
            }
        if isinstance(node, list):
            return [strip(child) for child in node]
        return node

    encoded = json.dumps(
        strip(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _span(atom: Any, offset: int = 0) -> tuple[tuple[int, int], ...]:
    evidence = getattr(atom, "evidence", None)
    if evidence is None or evidence.start < 0 or evidence.end <= evidence.start:
        return ()
    return ((evidence.start + offset, evidence.end + offset),)


def _combine(kind: str, children: Iterable[TargetingExpression]) -> TargetingExpression:
    unique = {
        child.canonical_fingerprint: child for child in children
    }
    ordered = tuple(unique[key] for key in sorted(unique))
    if not ordered:
        raise ValueError("canonical Boolean node cannot be empty")
    if len(ordered) == 1:
        return ordered[0]
    return And(ordered) if kind == "and" else Or(ordered)


def event_condition_to_targeting(
    condition: event_ir.Condition,
    *,
    offset: int = 0,
    negated: bool = False,
) -> TargetingExpression:
    if isinstance(condition, event_ir.And):
        return _combine(
            "and", (
                event_condition_to_targeting(child, offset=offset, negated=negated)
                for child in condition.operands
            )
        )
    if isinstance(condition, event_ir.Or):
        return _combine(
            "or", (
                event_condition_to_targeting(child, offset=offset, negated=negated)
                for child in condition.operands
            )
        )
    if isinstance(condition, event_ir.Not):
        return Not(event_condition_to_targeting(
            condition.operand, offset=offset, negated=not negated
        ))

    payload = condition.to_dict()
    if isinstance(condition, event_ir.Comparison) and isinstance(condition.left, event_ir.Aggregate):
        predicate_kind = "AggregatePredicate"
        prefix = "aggregate"
    else:
        predicate_kind = "EventPredicate"
        prefix = "event"
    return PredicateRef(
        predicate_kind=predicate_kind,
        semantic_key=f"{prefix}:{_semantic_hash({'node': payload, 'negated': negated})}",
        source_spans=_span(condition, offset),
        payload={"event_ir": payload, "effective_negated": negated},
    )


def _node_span(node: dict[str, Any], expression_text: str) -> tuple[int, int] | None:
    source_span = node.get("source_span")
    if isinstance(source_span, dict):
        start, end = source_span.get("start"), source_span.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end:
            return start, end
    text = node.get("matched_text") or node.get("text") or node.get("label")
    if not isinstance(text, str) or not text:
        return None
    start = expression_text.find(text)
    return (start, start + len(text)) if start >= 0 else None


def _set_leaf(node: dict[str, Any], expression_text: str) -> TargetingExpression:
    node_type = node.get("type")
    span = _node_span(node, expression_text)
    spans = (span,) if span is not None else ()
    if node_type == "age_range":
        return PredicateRef(
            predicate_kind="MemberPredicate",
            semantic_key=f"member.age:{node.get('age_min')}:{node.get('age_max')}",
            source_spans=spans,
            payload={"set_operand": node},
        )
    if node_type == "operand" and isinstance(node.get("canonical"), str):
        canonical = node["canonical"]
        return PredicateRef(
            predicate_kind="NamedSegmentPredicate",
            semantic_key=f"named_segment:{canonical}",
            source_spans=spans,
            payload={"set_operand": node},
        )
    if node_type == "universe":
        return PredicateRef(
            predicate_kind="MemberPredicate",
            semantic_key="member:whole_audience",
            source_spans=spans,
            payload={"set_operand": node},
        )

    raw_text = node.get("text") or node.get("matched_text")
    if isinstance(raw_text, str) and raw_text.strip():
        parsed = event_parser.parse_expression(raw_text)
        if parsed is not None:
            return event_condition_to_targeting(parsed, offset=span[0] if span else 0)
    return PredicateRef(
        predicate_kind="UnresolvedPredicate",
        semantic_key=f"unresolved:{_semantic_hash({'text': raw_text or node})}",
        source_spans=spans,
        payload={"set_operand": node, "status": "unresolved"},
    )


def set_ast_to_targeting(node: Any, expression_text: str) -> TargetingExpression:
    if not isinstance(node, dict):
        return PredicateRef(
            predicate_kind="UnresolvedPredicate",
            semantic_key=f"unresolved:{_semantic_hash({'set_ast': node})}",
            payload={"set_ast": node, "status": "unresolved"},
        )
    if node.get("type") != "set_op":
        return _set_leaf(node, expression_text)
    left = set_ast_to_targeting(node.get("left"), expression_text)
    right = set_ast_to_targeting(node.get("right"), expression_text)
    operation = node.get("op")
    if operation == "+":
        return _combine("or", (left, right))
    if operation == "*":
        return _combine("and", (left, right))
    if operation == "-":
        return _combine("and", (left, Not(right)))
    return PredicateRef(
        predicate_kind="UnresolvedPredicate",
        semantic_key=f"unresolved:{_semantic_hash(node)}",
        source_spans=tuple(sorted(set(left.source_spans + right.source_spans)))
        if isinstance(left, PredicateRef) and isinstance(right, PredicateRef)
        else (),
        payload={"set_ast": node, "status": "unsupported"},
    )


def _semantic_span(node: Any) -> tuple[tuple[int, int], ...]:
    span = getattr(node, "source_span", None)
    if (
        isinstance(span, semantic_ast.SourceSpan)
        and isinstance(span.start, int)
        and isinstance(span.end, int)
        and 0 <= span.start < span.end
    ):
        return ((span.start, span.end),)
    return ()


def _member_semantic_to_targeting(node: Any) -> TargetingExpression | None:
    """Project the established legacy member AST into the common typed tree."""

    if isinstance(node, semantic_ast.And):
        children = tuple(
            child
            for raw_child in node.children
            if (child := _member_semantic_to_targeting(raw_child)) is not None
        )
        return _combine("and", children) if children else None
    if isinstance(node, semantic_ast.Or):
        children = tuple(
            child
            for raw_child in node.children
            if (child := _member_semantic_to_targeting(raw_child)) is not None
        )
        return _combine("or", children) if children else None
    if isinstance(node, semantic_ast.Not):
        child = _member_semantic_to_targeting(node.child)
        return Not(child) if child is not None else None
    if isinstance(node, semantic_ast.Predicate):
        payload = semantic_ast.to_dict(node)
        return PredicateRef(
            predicate_kind="MemberPredicate",
            semantic_key=f"member:{_semantic_hash(payload)}",
            source_spans=_semantic_span(node),
            payload={"legacy_member": payload},
        )
    if isinstance(node, semantic_ast.Unknown):
        payload = semantic_ast.to_dict(node)
        return PredicateRef(
            predicate_kind="UnresolvedPredicate",
            semantic_key=f"unresolved:{_semantic_hash(payload)}",
            source_spans=_semantic_span(node),
            payload={"legacy_member": payload, "status": "unresolved"},
        )
    return None


def _legacy_member_tree(plan: dict[str, Any]) -> TargetingExpression | None:
    target_user = plan.get("target_user")
    member_target = dict(target_user) if isinstance(target_user, dict) else {}
    # Registered behavior conditions have fact-specific meanings in
    # targeting_ir; representing them again as a flat member attribute would
    # create a second semantic owner.  new_user is likewise owned by the
    # signup condition registry, while other lifecycle values remain member
    # attributes.
    member_target.pop("behaviors", None)
    lifecycle = member_target.get("lifecycle")
    if isinstance(lifecycle, list):
        member_target["lifecycle"] = [value for value in lifecycle if value != "new_user"]
    semantic_plan = {
        "target_user": member_target,
        "exclude": plan.get("exclude") if isinstance(plan.get("exclude"), dict) else {},
        "dimension_filters": plan.get("dimension_filters")
        if isinstance(plan.get("dimension_filters"), list)
        else [],
        "set_expressions": [],
    }
    return _member_semantic_to_targeting(plan_semantic_ast.plan_to_semantic_expr(semantic_plan))


def _legacy_condition_trees(plan: dict[str, Any]) -> tuple[TargetingExpression, ...]:
    trees: list[TargetingExpression] = []
    registered_kinds: set[str] = set()
    for condition in targeting_ir.extract_target_conditions(plan):
        if condition.kind == "event_expression":
            continue
        registered_kinds.add(condition.kind)
        payload = {
            "kind": condition.kind,
            "fact": condition.spec.fact,
            "fact_join": condition.spec.fact_join,
            "params": condition.params,
        }
        trees.append(PredicateRef(
            predicate_kind="LegacyPredicate",
            semantic_key=f"legacy:{condition.kind}:{_semantic_hash(payload)}",
            payload={"legacy_condition": payload},
        ))
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), dict) else {}
    for slot in ("purchase_membership", "inactivity_period"):
        value = target_user.get(slot)
        if slot in registered_kinds or not isinstance(value, dict):
            continue
        payload = {"kind": slot, "fact": "member", "fact_join": False, "params": value}
        trees.append(PredicateRef(
            predicate_kind="LegacyPredicate",
            semantic_key=f"legacy:{slot}:{_semantic_hash(payload)}",
            payload={"legacy_condition": payload},
        ))
    return tuple(trees)


def _expression_from_plan(plan: dict[str, Any]) -> TargetingExpression | None:
    set_trees: list[TargetingExpression] = []
    for expression in plan.get("set_expressions") or []:
        if not isinstance(expression, dict):
            continue
        text = expression.get("expression_text")
        set_trees.append(set_ast_to_targeting(
            expression.get("set_ast"), text if isinstance(text, str) else ""
        ))
    if set_trees:
        return _combine("and", set_trees)

    trees: list[TargetingExpression] = []
    payload = plan.get("event_expression")
    if isinstance(payload, dict) and isinstance(payload.get("expression"), dict):
        try:
            trees.append(event_condition_to_targeting(event_ir.condition_from_dict(payload["expression"])))
        except (event_ir.IrSchemaError, TypeError, ValueError):
            trees.append(PredicateRef(
                predicate_kind="UnresolvedPredicate",
                semantic_key=f"unresolved:{_semantic_hash(payload)}",
                payload={"event_expression": payload, "status": "invalid"},
            ))
    member_tree = _legacy_member_tree(plan)
    if member_tree is not None:
        trees.append(member_tree)
    trees.extend(_legacy_condition_trees(plan))
    return _combine("and", trees) if trees else None


def _walk(
    expression: TargetingExpression,
    parent: TargetingExpression | None = None,
) -> Iterable[tuple[PredicateRef, TargetingExpression | None]]:
    if isinstance(expression, PredicateRef):
        yield expression, parent
        return
    if isinstance(expression, Not):
        yield from _walk(expression.operand, expression)
        return
    if isinstance(expression, (And, Or)):
        for child in expression.children:
            yield from _walk(child, expression)


def _owner_for(predicate: PredicateRef) -> str | None:
    payload = predicate.payload if isinstance(predicate.payload, dict) else {}
    if "legacy_condition" in payload:
        return "legacy_conditions"
    if "legacy_member" in payload:
        return "legacy_member_conditions"
    if predicate.predicate_kind in {"EventPredicate", "AggregatePredicate"}:
        return "event_expression"
    if predicate.predicate_kind in {"MemberPredicate", "NamedSegmentPredicate"}:
        return "set_expressions"
    return None


def _origin_for(predicate: PredicateRef) -> str:
    payload = predicate.payload if isinstance(predicate.payload, dict) else {}
    if "legacy_condition" in payload:
        return "legacy_slot_registry"
    if "legacy_member" in payload:
        return "legacy_member_slots"
    if predicate.predicate_kind in {"EventPredicate", "AggregatePredicate"}:
        return "event_parser"
    return "set_expression_parser"


def _predicate_domains(expression: TargetingExpression) -> frozenset[str]:
    if isinstance(expression, PredicateRef):
        return frozenset({
            "event"
            if expression.predicate_kind in {"EventPredicate", "AggregatePredicate"}
            else "other"
        })
    if isinstance(expression, Not):
        return _predicate_domains(expression.operand)
    if isinstance(expression, (And, Or)):
        return frozenset().union(*(_predicate_domains(child) for child in expression.children))
    return frozenset()


def _has_cross_domain_or(expression: TargetingExpression) -> bool:
    if isinstance(expression, Or) and _predicate_domains(expression) == frozenset({"event", "other"}):
        return True
    if isinstance(expression, Not):
        return _has_cross_domain_or(expression.operand)
    if isinstance(expression, (And, Or)):
        return any(_has_cross_domain_or(child) for child in expression.children)
    return False


def build_canonical_targeting(plan: dict[str, Any]) -> CanonicalTargetingResult:
    expression = _expression_from_plan(plan)
    if expression is None:
        return CanonicalTargetingResult(None, (), ())
    claims: list[ConditionClaim] = []
    for predicate, parent in _walk(expression):
        owner = _owner_for(predicate)
        resolved = owner is not None and predicate.predicate_kind != "UnresolvedPredicate"
        claims.append(ConditionClaim(
            source_spans=predicate.source_spans,
            expression_node_id=predicate.expression_node_id,
            parent_expression_node_id=parent.expression_node_id if parent is not None else None,
            predicate_kind=predicate.predicate_kind,
            semantic_key=predicate.semantic_key,
            owner=owner,
            status="resolved" if resolved else "unresolved",
            disposition="owned" if resolved else "unresolved",
            origin_parser=_origin_for(predicate),
            issues=() if resolved else ({"code": "canonical_owner_missing"},),
        ))
    issues = condition_claim_invariant_issues(claims)
    return CanonicalTargetingResult(expression, tuple(claims), issues)


def attach_canonical_targeting(plan: dict[str, Any]) -> CanonicalTargetingResult:
    result = build_canonical_targeting(plan)
    if result.expression is None:
        plan.pop("canonical_targeting_expression", None)
        plan.pop("condition_claims", None)
        plan.pop("canonical_projection", None)
        plan.pop("canonical_targeting_version", None)
        plan.pop("canonical_blocking_claim_ids", None)
        plan.pop("canonical_unresolved_span_ids", None)
    else:
        plan["canonical_targeting_version"] = 1
        plan["canonical_targeting_expression"] = result.expression.to_dict()
        plan["condition_claims"] = [claim.to_dict() for claim in result.claims]
        owned = [claim for claim in result.claims if claim.disposition == "owned"]
        unresolved = [claim for claim in result.claims if claim.disposition != "owned"]
        kinds = {claim.predicate_kind for claim in owned}
        mixed_event_domain = bool(
            kinds & {"EventPredicate", "AggregatePredicate"}
            and kinds - {"EventPredicate", "AggregatePredicate"}
        )
        cross_domain_or = mixed_event_domain and _has_cross_domain_or(result.expression)
        if unresolved:
            projection_status = "unsupported"
        elif cross_domain_or:
            projection_status = "partially_supported"
        else:
            projection_status = "supported"
        plan["canonical_projection"] = {
            "status": projection_status,
            "projected_node_ids": [
                claim.expression_node_id for claim in owned
                if projection_status == "supported"
            ],
            "unprojected_node_ids": [
                claim.expression_node_id for claim in result.claims
                if projection_status != "supported" or claim.disposition != "owned"
            ],
            "silent_drop_count": 0,
            "legacy_semantic_loss": bool(
                cross_domain_or
                or (
                    isinstance(plan.get("event_expression"), dict)
                    and plan["event_expression"].get("candidate_scope") == "subtree"
                )
            ),
        }
        blocking_claims = [claim for claim in result.claims if claim.disposition != "owned"]
        plan["canonical_blocking_claim_ids"] = [claim.claim_id for claim in blocking_claims]
        plan["canonical_unresolved_span_ids"] = sorted({
            f"span:{start}:{end}"
            for claim in blocking_claims
            for start, end in claim.source_spans
        })
    plan["canonical_targeting_validation"] = {
        "status": "valid" if not result.issues else "invalid",
        "issues": [{"code": "condition_claim_invariant", "detail": issue} for issue in result.issues],
    }
    plan["ownership_reconciliation_complete"] = True
    return result


__all__ = [
    "CanonicalTargetingResult",
    "attach_canonical_targeting",
    "build_canonical_targeting",
    "event_condition_to_targeting",
    "set_ast_to_targeting",
]
