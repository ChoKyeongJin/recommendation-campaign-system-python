"""Fail-close receipts for ordered Semantic Catalog value comparisons."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


ORDERED_OPERATORS = frozenset({">=", "<=", ">", "<"})
_REVERSED_OPERATOR = {">=": "<=", "<=": ">=", ">": "<", "<": ">"}


def _field_name(node: Any) -> str | None:
    if isinstance(node, Mapping) and node.get("type") == "field":
        name = node.get("name")
        return str(name) if isinstance(name, str) and name else None
    return None


def _literal_value(node: Any) -> Any:
    if isinstance(node, Mapping) and node.get("type") == "literal":
        return node.get("value")
    return None


def _effective_comparison(
    comparison: Mapping[str, Any],
) -> tuple[str, str, Any] | None:
    operator = comparison.get("operator")
    if operator not in ORDERED_OPERATORS:
        return None
    left, right = comparison.get("left"), comparison.get("right")
    left_field, right_field = _field_name(left), _field_name(right)
    if left_field is not None and isinstance(right, Mapping) and right.get("type") == "literal":
        return left_field, str(operator), _literal_value(right)
    if right_field is not None and isinstance(left, Mapping) and left.get("type") == "literal":
        return right_field, _REVERSED_OPERATOR[str(operator)], _literal_value(left)
    return None


def _valid_evidence_span(
    query: str,
    evidence: Any,
    fallback_start: int,
    fallback_end: int,
) -> tuple[int, int] | None:
    if isinstance(evidence, Mapping):
        start, end, text = evidence.get("start"), evidence.get("end"), evidence.get("text")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(query)
            and text == query[start:end]
        ):
            return start, end
    if 0 <= fallback_start < fallback_end <= len(query):
        return fallback_start, fallback_end
    return None


def ordered_comparison_consumes_claim(
    query: str,
    comparison: Mapping[str, Any],
    bindings: Iterable[Mapping[str, Any]],
    *,
    field_ids: frozenset[str],
    canonical: str,
    domain: Mapping[str, Any],
    evidence_start: int,
    evidence_end: int,
    negated: bool,
    expected_negative: bool,
) -> bool:
    """Prove that one ordered comparison consumes its source value/operator.

    A non-equality operator is accepted only when the materialized catalog owns
    a complete order and an application-extracted operator binding with the
    same normalized operator lies in this comparison's evidence span.
    """

    if negated != expected_negative or domain.get("ordered") is not True:
        return False
    order = domain.get("order")
    values = domain.get("values")
    if (
        not isinstance(order, list)
        or not order
        or not isinstance(values, Mapping)
        or canonical not in values
        or canonical not in order
        or any(not isinstance(value, str) or value not in values for value in order)
    ):
        return False
    effective = _effective_comparison(comparison)
    if effective is None:
        return False
    field_name, operator, literal = effective
    if field_name not in field_ids or literal != canonical:
        return False
    span = _valid_evidence_span(
        query, comparison.get("evidence"), evidence_start, evidence_end
    )
    if span is None:
        return False
    start, end = span
    return any(
        isinstance(binding, Mapping)
        and binding.get("kind") == "comparison_operator"
        and binding.get("normalized") == operator
        and isinstance(binding.get("start"), int)
        and not isinstance(binding.get("start"), bool)
        and isinstance(binding.get("end"), int)
        and not isinstance(binding.get("end"), bool)
        and start <= binding["start"] < binding["end"] <= end
        and binding.get("text") == query[binding["start"]:binding["end"]]
        for binding in bindings
    )


__all__ = ["ORDERED_OPERATORS", "ordered_comparison_consumes_claim"]
