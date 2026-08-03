"""Closed natural-language claim for a recent, still-active cart line.

The CRM definition of an unpaid cart is physical state, not member-wide
purchase absence: ``ODS_MALL_OMS_CART.KEEP_YN = 'Y'``.  This adapter owns only
the narrow Korean surface construction needed to select that registered event
source.  It deliberately does not turn "did not pay" into
``Not(Exists(purchase))`` because that would exclude members who bought some
other product during the same period.
"""

from __future__ import annotations

import re
from typing import Any

import event_ir
import semantic_fields

_RECENT_ACTIVE_CART_RE = re.compile(
    r"최근\s*(?P<days>[1-9]\d*)\s*일\s*"
    r"(?:장바구니|카트)\s*에\s*"
    r"(?:담아\s*두고|담고)\s*"
    r"(?:결제|구매)(?:를)?\s*"
    r"(?:하지\s*않은|안\s*한)\s*"
    r"(?:회원|고객|사람)"
    r"(?:\s*(?:을|를))?"
    r"(?:\s*(?:찾아\s*줘|추출해\s*줘|보여\s*줘))?"
    r"[.!?]?"
)


def _rolling_days_value(
    query: str, literal_bindings: list[dict[str, Any]]
) -> int | None:
    match = _RECENT_ACTIVE_CART_RE.fullmatch(query)
    if match is None:
        return None
    durations = [
        item for item in literal_bindings
        if isinstance(item, dict) and item.get("kind") == "duration"
    ]
    if len(durations) != 1:
        return None
    binding = durations[0]
    normalized = binding.get("normalized")
    start, end = binding.get("start"), binding.get("end")
    value = binding.get("value")
    if not (
        isinstance(normalized, dict)
        and normalized.get("temporal_kind") == "rolling_duration"
        and normalized.get("semantic_unit") == "days"
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and query[start:end] == binding.get("text")
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value == int(match.group("days"))
    ):
        return None
    return value


def synthesize_recent_active_cart(
    query: str, literal_bindings: list[dict[str, Any]]
) -> event_ir.Exists | None:
    """Return one active-cart existence atom for the closed audience phrase."""

    days = _rolling_days_value(query, literal_bindings)
    if days is None:
        return None
    evidence = event_ir.Evidence(query, 0, len(query))
    return event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("active_cart"),
            event_ir.TimeFilter(
                event_ir.FieldRef("active_cart.occurred_at"),
                event_ir.RollingWindow(days, "day"),
            ),
        ),
        evidence=evidence,
    )


def owns_recent_active_cart_claim(
    query: str,
    raw_expression: Any,
    literal_bindings: list[dict[str, Any]],
) -> bool:
    """Whether this exact query/IR pair proves the narrow unpaid-cart claim.

    Merely mentioning the ``active_cart`` source is not enough: that would let
    a model use one kept cart row to hide a distinct member-wide request such
    as ``구매 이력이 없는``.  Re-run the closed source parser and compare
    the complete semantic tree, excluding provenance only.  Nearby phrases or
    an expression with any extra/missing atom therefore fail closed.
    """

    if not isinstance(literal_bindings, list):
        return False
    expected = synthesize_recent_active_cart(query, literal_bindings)
    if expected is None:
        return False
    try:
        actual = (
            raw_expression
            if isinstance(raw_expression, event_ir.Condition)
            else event_ir.condition_from_dict(raw_expression)
        )
    except (event_ir.IrSchemaError, KeyError, TypeError, ValueError):
        return False
    return semantic_fields.strip_provenance(
        actual.to_dict()
    ) == semantic_fields.strip_provenance(expected.to_dict())


def _simple_time_scoped_exists(node: event_ir.Condition, source: str) -> bool:
    if not isinstance(node, event_ir.Exists):
        return False
    relation = node.relation
    if isinstance(relation, event_ir.Source):
        return relation.name == source
    return bool(
        isinstance(relation, event_ir.Filter)
        and isinstance(relation.relation, event_ir.Source)
        and relation.relation.name == source
        and isinstance(relation.where, event_ir.TimeFilter)
        and relation.where.field == event_ir.FieldRef(f"{source}.occurred_at")
    )


def is_refutable_model_shape(raw_expression: Any) -> bool:
    """Recognize only the two model shapes whose over-constraint is provable.

    A single cart existence atom is the desired logical shape.  The observed
    model output adds member-wide ``Not(Exists(purchase))``; the registered
    ``KEEP_YN='Y'`` cart state makes that second atom both unnecessary and
    semantically too broad.  Any product filter, comparison, OR, or additional
    audience atom fails closed here.
    """

    try:
        expression = event_ir.condition_from_dict(raw_expression)
    except (event_ir.IrSchemaError, KeyError, TypeError, ValueError):
        return False
    if (
        _simple_time_scoped_exists(expression, "cart")
        or _simple_time_scoped_exists(expression, "active_cart")
    ):
        return True
    if not isinstance(expression, event_ir.And) or len(expression.operands) != 2:
        return False
    cart_atoms = [
        operand for operand in expression.operands
        if _simple_time_scoped_exists(operand, "cart")
        or _simple_time_scoped_exists(operand, "active_cart")
    ]
    purchase_absence_atoms = [
        operand for operand in expression.operands
        if isinstance(operand, event_ir.Not)
        and _simple_time_scoped_exists(operand.operand, "purchase")
    ]
    return len(cart_atoms) == 1 and len(purchase_absence_atoms) == 1


__all__ = [
    "is_refutable_model_shape",
    "owns_recent_active_cart_claim",
    "synthesize_recent_active_cart",
]
