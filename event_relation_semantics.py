"""Typed Event IR relation scopes used by presence/absence validation.

The SQL compiler owns physical LIKE expansion.  This module only preserves
which catalog dimension a filter constrains and whether that exact predicate
or its logical complement is selected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import aggregate_semantics
import event_ir
import event_semantic_registry


def normalized_window(
    window: Any, anchor: datetime,
) -> aggregate_semantics.NormalizedWindow | None:
    if isinstance(window, event_ir.RollingWindow):
        return aggregate_semantics.rolling_window(anchor, window.days)
    interval = window
    if isinstance(window, event_ir.RelativeWindow):
        try:
            interval = event_ir.resolve_relative_window(window, anchor.date())
        except (event_ir.IrSchemaError, ValueError):
            return None
    if isinstance(interval, event_ir.AbsoluteInterval):
        return aggregate_semantics.NormalizedWindow(
            start=datetime.combine(interval.start, datetime.min.time()),
            end=datetime.combine(interval.end_exclusive, datetime.min.time()),
        )
    return None


def _semantic_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _semantic_payload(item)
            for key, item in value.items()
            if key != "evidence"
        }
    if isinstance(value, list):
        return [_semantic_payload(item) for item in value]
    return value


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        _semantic_payload(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _field_literal(
    comparison: event_ir.Comparison,
) -> tuple[event_ir.FieldRef, event_ir.Literal] | None:
    if isinstance(comparison.left, event_ir.FieldRef) and isinstance(
        comparison.right, event_ir.Literal
    ):
        return comparison.left, comparison.right
    if (
        comparison.operator in {"=", "!="}
        and isinstance(comparison.right, event_ir.FieldRef)
        and isinstance(comparison.left, event_ir.Literal)
    ):
        return comparison.right, comparison.left
    return None


def _comparison_scope(
    comparison: event_ir.Comparison,
    *,
    source: str,
    dimensions: Iterable[str],
    semantic_registry: event_semantic_registry.EventSemanticRegistry,
    negated: bool,
) -> tuple[str, str] | None:
    operands = _field_literal(comparison)
    if operands is None or comparison.operator not in {"=", "!="}:
        return None
    field, literal = operands
    dimension = semantic_registry.dimension_for_field(source, field.name)
    if dimension is None or dimension not in dimensions:
        return None

    complemented = negated != (comparison.operator == "!=")
    dimension_spec = semantic_registry.dimensions[dimension]
    if (
        not complemented
        and isinstance(literal.value, str)
        and literal.value in dimension_spec.values
    ):
        return dimension, literal.value

    # Normalize != and a surrounding Not to the same equality predicate plus
    # one complement bit. Evidence is intentionally excluded from identity.
    predicate = {
        "type": "comparison",
        "operator": "=",
        "field": field.name,
        "literal": {
            "python_type": type(literal.value).__name__,
            "value": literal.value,
        },
    }
    return dimension, event_semantic_registry.typed_predicate_value(
        _fingerprint(predicate), complemented=complemented
    )


def _condition_scopes(
    condition: event_ir.Condition,
    *,
    source: str,
    dimensions: tuple[str, ...],
    semantic_registry: event_semantic_registry.EventSemanticRegistry,
    values: dict[str, set[str]],
    opaque_filters: list[str],
    negated: bool = False,
) -> None:
    if isinstance(condition, event_ir.TimeFilter):
        return
    if isinstance(condition, event_ir.Not):
        _condition_scopes(
            condition.operand,
            source=source,
            dimensions=dimensions,
            semantic_registry=semantic_registry,
            values=values,
            opaque_filters=opaque_filters,
            negated=not negated,
        )
        return
    if isinstance(condition, event_ir.Comparison):
        scope = _comparison_scope(
            condition,
            source=source,
            dimensions=dimensions,
            semantic_registry=semantic_registry,
            negated=negated,
        )
        if scope is not None:
            dimension, value = scope
            values[dimension].add(value)
            return
    elif isinstance(condition, event_ir.And) and not negated:
        for operand in condition.operands:
            _condition_scopes(
                operand,
                source=source,
                dimensions=dimensions,
                semantic_registry=semantic_registry,
                values=values,
                opaque_filters=opaque_filters,
            )
        return

    payload: dict[str, Any] = condition.to_dict()
    if negated:
        payload = {"type": "not", "operand": payload}
    opaque_filters.append("opaque:" + _fingerprint(payload))


def relation_semantics(
    relation: event_ir.Relation,
    anchor: datetime,
    source: str,
    dimensions: tuple[str, ...],
    *,
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
) -> tuple[aggregate_semantics.NormalizedWindow | None, dict[str, frozenset[str] | None]]:
    """Return the normalized window and catalog-dimension scope of a relation."""

    registry = semantic_registry or event_semantic_registry.registry()
    windows: list[aggregate_semantics.NormalizedWindow] = []
    unresolved = False
    values: dict[str, set[str]] = {dimension: set() for dimension in dimensions}
    opaque_filters: list[str] = []
    for node in event_ir.walk(relation):
        if isinstance(node, event_ir.TimeFilter):
            window = normalized_window(node.window, anchor)
            if window is None:
                unresolved = True
            else:
                windows.append(window)
        elif isinstance(node, event_ir.Filter):
            _condition_scopes(
                node.where,
                source=source,
                dimensions=dimensions,
                semantic_registry=registry,
                values=values,
                opaque_filters=opaque_filters,
            )
        elif isinstance(node, (event_ir.TemporalRelation, event_ir.Join, event_ir.Group)):
            opaque_filters.append("opaque:" + _fingerprint(node.to_dict()))

    if unresolved:
        resolved_window = None
    else:
        resolved_window = aggregate_semantics.lifetime_window(anchor)
        for candidate in windows:
            narrowed = aggregate_semantics.intersect(resolved_window, candidate)
            if narrowed is None:
                resolved_window = None
                break
            resolved_window = narrowed
    if opaque_filters and dimensions:
        fallback = "product_scope" if "product_scope" in values else dimensions[0]
        values[fallback].add("opaque:" + _fingerprint(sorted(opaque_filters)))
    constraints = {
        dimension: frozenset(dimension_values) if dimension_values else None
        for dimension, dimension_values in values.items()
    }
    return resolved_window, constraints


__all__ = ["normalized_window", "relation_semantics"]
