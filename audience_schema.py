"""Compact JSON Schema for the canonical audience Event IR.

The runtime IR is recursive, so expanding every possible child at every depth
duplicates the same algebra many times and also duplicates every catalog symbol
enum.  This module describes the fixed algebra once with ``$defs``/``$ref``.

Business source and field identifiers deliberately remain canonical strings.
Their membership in the resolved Semantic Catalog is an execution-time concern;
embedding the catalog in this schema would make its size and structure depend on
the number of business concepts again.
"""

from __future__ import annotations

import copy
from typing import Any

import event_ir


SCHEMA_ID = "urn:recommendation-campaign:audience-expression:v1"
SOURCE_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
FIELD_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
OUTPUT_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _object(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required or tuple(properties)),
    }
    if description:
        schema["description"] = description
    return schema


def _tag(name: str) -> dict[str, Any]:
    return {"type": "string", "enum": [name]}


_DEFINITIONS: dict[str, Any] = {
    "evidence": _object(
        {
            "text": {"type": "string", "minLength": 1},
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 0},
        },
        description="Exact query substring with zero-based [start,end) offsets for this semantic atom.",
    ),
    "source_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": SOURCE_ID_PATTERN,
        "description": "Canonical Semantic Catalog source id; membership is runtime-validated.",
    },
    "field_id": {
        "type": "string",
        "minLength": 3,
        "maxLength": 255,
        "pattern": FIELD_ID_PATTERN,
        "description": "Canonical <source>.<field> id; membership is runtime-validated.",
    },
    "output_name": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": OUTPUT_NAME_PATTERN,
    },
    "interval": _object(
        {
            "type": _tag("interval"),
            "start": {"type": "string", "format": "date"},
            "end_exclusive": {"type": "string", "format": "date"},
        }
    ),
    "rolling": _object(
        {
            "type": _tag("rolling"),
            "value": {"type": "integer", "minimum": 1},
            "unit": {"type": "string", "enum": list(event_ir.WINDOW_UNITS)},
        }
    ),
    "relative": _object(
        {
            "type": _tag("relative"),
            "value": {"type": "integer", "minimum": 1},
            "unit": {"type": "string", "enum": list(event_ir.WINDOW_UNITS)},
            "direction": {"type": "string", "enum": ["past"]},
        }
    ),
    "window": {
        "anyOf": [_ref("interval"), _ref("rolling"), _ref("relative")]
    },
    "duration": _object(
        {
            "type": _tag("duration"),
            "value": {"type": "integer", "minimum": 1},
            "unit": {"type": "string", "enum": list(event_ir.DURATION_UNITS)},
        }
    ),
    "literal": _object(
        {
            "type": _tag("literal"),
            "value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                ]
            },
        }
    ),
    "field": _object(
        {"type": _tag("field"), "name": _ref("field_id")},
        description="Scalar catalog field reference; subject.* fields refer directly to the current member row.",
    ),
    "arithmetic": _object(
        {
            "type": _tag("arithmetic"),
            "operator": {
                "type": "string",
                "enum": sorted(event_ir.ARITHMETIC_OPERATORS),
            },
            "left": _ref("scalar"),
            "right": _ref("scalar"),
        }
    ),
    "aggregate": _object(
        {
            "type": _tag("aggregate"),
            "function": {
                "type": "string",
                "enum": sorted(event_ir.AGGREGATE_FUNCTIONS),
            },
            "relation": _ref("relation"),
            "expression": {"anyOf": [_ref("scalar"), {"type": "null"}]},
            "distinct": {"type": "boolean"},
        },
        description="Scalar aggregate over relation rows. Use null expression only for count(*); compare the result in a Comparison.",
    ),
    "scalar": {
        "anyOf": [
            _ref("literal"),
            _ref("field"),
            _ref("arithmetic"),
            _ref("aggregate"),
        ]
    },
    "source_subject": _object(
        {"type": _tag("source"), "name": _ref("source_id")},
        description="Catalog event source correlated to the current member; omit correlation.",
    ),
    "source_scoped": _object(
        {
            "type": _tag("source"),
            "name": _ref("source_id"),
            "correlation": {"type": "string", "enum": ["none"]},
        },
        description="Global uncorrelated catalog event source used for cross-member ranking or aggregation.",
    ),
    "source": {"anyOf": [_ref("source_subject"), _ref("source_scoped")]},
    "filter": _object(
        {
            "type": _tag("filter"),
            "relation": _ref("relation"),
            "where": _ref("condition"),
        },
        description="Relation rows restricted by where. Put a TimeFilter here to scope an aggregate or summary period.",
    ),
    "join_inner": _object(
        {
            "type": _tag("join"),
            "left": _ref("relation"),
            "right": _ref("relation"),
            "on": _ref("comparison"),
        }
    ),
    "join_kind": _object(
        {
            "type": _tag("join"),
            "left": _ref("relation"),
            "right": _ref("relation"),
            "on": _ref("comparison"),
            "kind": {"type": "string", "enum": ["semi", "anti"]},
        },
        description="Membership join: semi keeps left rows matching the right derived set; anti keeps non-matches.",
    ),
    "join": {"anyOf": [_ref("join_inner"), _ref("join_kind")]},
    "group": _object(
        {
            "type": _tag("group"),
            "relation": _ref("relation"),
            "keys": {
                "type": "array",
                "items": _ref("field"),
                "minItems": 1,
            },
        }
    ),
    "named_expression": _object(
        {"name": _ref("output_name"), "expression": _ref("scalar")},
        description="Creates a relation-local output alias; the alias is not a catalog FieldRef id.",
    ),
    "named_measure": _object(
        {
            "name": _ref("output_name"),
            "function": {
                "type": "string",
                "enum": sorted(event_ir.AGGREGATE_FUNCTIONS),
            },
            "expression": {"anyOf": [_ref("scalar"), {"type": "null"}]},
            "distinct": {"type": "boolean"},
        }
    ),
    "sort_key": _object(
        {
            "name": {"anyOf": [_ref("output_name"), _ref("field_id")]},
            "direction": {"type": "string", "enum": ["asc", "desc"]},
        },
        description="Orders by an output alias produced by the immediately preceding relation or a preserved catalog field.",
    ),
    "project": _object(
        {
            "type": _tag("project"),
            "relation": _ref("relation"),
            "items": {
                "type": "array",
                "items": _ref("named_expression"),
                "minItems": 1,
            },
        }
    ),
    "summarize": _object(
        {
            "type": _tag("summarize"),
            "relation": _ref("relation"),
            "keys": {"type": "array", "items": _ref("named_expression")},
            "measures": {
                "type": "array",
                "items": _ref("named_measure"),
                "minItems": 1,
            },
        },
        description="Groups relation rows by named keys and emits named aggregate measures.",
    ),
    "order": _object(
        {
            "type": _tag("order"),
            "relation": _ref("relation"),
            "keys": {
                "type": "array",
                "items": _ref("sort_key"),
                "minItems": 1,
            },
        },
        description="Deterministically orders a derived relation; measure rank key precedes entity tie-break keys.",
    ),
    "limit_count": _object(
        {
            "type": _tag("limit"),
            "relation": _ref("relation"),
            "count": {"type": "integer", "minimum": 1},
        },
        description="Limits an internal ordered relation to an absolute row count; this is not the root member result_limit.",
    ),
    "limit_percent": _object(
        {
            "type": _tag("limit"),
            "relation": _ref("relation"),
            "percent": {"type": "number", "minimum": 0, "maximum": 100},
        },
        description="Limits an internal ordered relation to a percentage of its declared population. Runtime validation enforces the open interval 0 < percent < 100.",
    ),
    # Two closed wire shapes survive provider strictification.  A single object
    # with optional count/percent fields would become required-and-nullable and
    # lose the exactly-one invariant at the LLM boundary.
    "limit": {"anyOf": [_ref("limit_count"), _ref("limit_percent")]},
    "relation": {
        "anyOf": [
            _ref("source"),
            _ref("filter"),
            _ref("join"),
            _ref("group"),
            _ref("project"),
            _ref("summarize"),
            _ref("order"),
            _ref("limit"),
        ]
    },
    "comparison": _object(
        {
            "type": _tag("comparison"),
            "operator": {
                "type": "string",
                "enum": sorted(event_ir.COMPARISON_OPERATORS),
            },
            "left": _ref("scalar"),
            "right": _ref("scalar"),
            "evidence": _ref("evidence"),
        },
        description="Boolean scalar comparison. Evidence covers the source phrase containing its value and operator wording.",
    ),
    "exists": _object(
        {
            "type": _tag("exists"),
            "relation": _ref("relation"),
            "evidence": _ref("evidence"),
        },
        description="Boolean condition asserting relation membership/existence; wrap a semi/anti Join here at expression root.",
    ),
    "time_filter": _object(
        {
            "type": _tag("time_filter"),
            "field": _ref("field"),
            "window": _ref("window"),
        },
        description="Temporal row predicate used as Filter.where for the relation whose period it scopes.",
    ),
    "event_reference": _object(
        {
            "type": _tag("event_reference"),
            "source": _ref("source_id"),
            "selector": {
                "type": "string",
                "enum": ["first", "last", "any", "subsequent"],
            },
        }
    ),
    "temporal_relation": _object(
        {
            "type": _tag("temporal_relation"),
            "operator": {
                "type": "string",
                "enum": ["within_after", "within_before"],
            },
            "left": _ref("event_reference"),
            "right": _ref("event_reference"),
            "duration": _ref("duration"),
            "evidence": _ref("evidence"),
        }
    ),
    "not": _object(
        {"type": _tag("not"), "operand": _ref("condition")}
    ),
    "and": _object(
        {
            "type": _tag("and"),
            "operands": {
                "type": "array",
                "items": _ref("condition"),
                "minItems": 1,
            },
        }
    ),
    "or": _object(
        {
            "type": _tag("or"),
            "operands": {
                "type": "array",
                "items": _ref("condition"),
                "minItems": 1,
            },
        }
    ),
    "condition": {
        "anyOf": [
            _ref("comparison"),
            _ref("exists"),
            _ref("time_filter"),
            _ref("temporal_relation"),
            _ref("not"),
            _ref("and"),
            _ref("or"),
        ]
    },
}


_AUDIENCE_EXPRESSION_SCHEMA: dict[str, Any] = {
    "$id": SCHEMA_ID,
    "$ref": "#/$defs/condition",
    "$defs": _DEFINITIONS,
}


def audience_expression_json_schema() -> dict[str, Any]:
    """Return a defensive copy of the fixed, catalog-size-independent schema."""

    return copy.deepcopy(_AUDIENCE_EXPRESSION_SCHEMA)


__all__ = [
    "FIELD_ID_PATTERN",
    "OUTPUT_NAME_PATTERN",
    "SCHEMA_ID",
    "SOURCE_ID_PATTERN",
    "audience_expression_json_schema",
]
