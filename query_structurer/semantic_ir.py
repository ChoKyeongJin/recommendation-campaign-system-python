from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any

from calendar_window import parse_calendar_window_spans


SEMANTIC_IR_STATUSES = frozenset(
    {"resolved", "policy_applied", "needs_clarification", "unsupported"}
)

_COMPARISON_TERMS: tuple[tuple[str, str], ...] = (
    ("이상", ">="),
    ("초과", ">"),
    ("이하", "<="),
    ("미만", "<"),
    (">=", ">="),
    ("<=", "<="),
    (">", ">"),
    ("<", "<"),
)
_COMPARISON_RE = re.compile(
    "|".join(re.escape(surface) for surface, _canonical in _COMPARISON_TERMS)
)
_PERCENT_RE = re.compile(r"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|퍼센트|프로)")
_NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")


SEMANTIC_IR_LLM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "operations",
        "missing_fields",
        "policy_applications",
        "unsupported_operations",
        "message",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": sorted(SEMANTIC_IR_STATUSES),
        },
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "metric_id", "direction", "bindings"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["period_over_period_change"],
                    },
                    "metric_id": {"type": "string", "minLength": 1},
                    "direction": {
                        "type": "string",
                        "enum": ["increase", "decrease"],
                    },
                    "bindings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "literal_id"],
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "baseline",
                                        "current",
                                        "threshold",
                                        "comparison",
                                    ],
                                },
                                "literal_id": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "policy_applications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["policy_id", "fields"],
                "properties": {
                    "policy_id": {"type": "string", "minLength": 1},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "unsupported_operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "reason", "evidence"],
                "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string"},
                },
            },
        },
        "message": {"type": ["string", "null"]},
    },
}


def _number(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied)


def extract_literal_bindings(
    query: str,
    *,
    current_date: str | date | None = None,
) -> list[dict[str, Any]]:
    """Extract value atoms without assigning business meaning between them.

    Dates, numbers, percentages, and comparison operators are application-owned.
    The LLM may only connect the returned IDs to semantic roles; it cannot submit
    replacement values in the semantic operation payload.
    """

    if not isinstance(query, str) or not query:
        return []
    reference_date: date | None
    if isinstance(current_date, date):
        reference_date = current_date
    elif isinstance(current_date, str):
        try:
            reference_date = date.fromisoformat(current_date)
        except ValueError:
            reference_date = None
    else:
        reference_date = None

    literals: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    counters: dict[str, int] = {}

    def append(kind: str, start: int, end: int, value: Any, normalized: Any) -> None:
        counters[kind] = counters.get(kind, 0) + 1
        literals.append(
            {
                "id": f"{kind}_{counters[kind]}",
                "kind": kind,
                "text": query[start:end],
                "start": start,
                "end": end,
                "value": value,
                "normalized": normalized,
            }
        )
        occupied.append((start, end))

    for window, start, end in parse_calendar_window_spans(query, today=reference_date):
        append(
            "date_window",
            start,
            end,
            query[start:end],
            {"from": window["from"], "to": window["to"], "label": window.get("label")},
        )

    for match in _PERCENT_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value"))
            append("percentage", match.start(), match.end(), value, {"value": value, "unit": "percent"})

    comparison_map = dict(_COMPARISON_TERMS)
    for match in _COMPARISON_RE.finditer(query):
        append(
            "comparison_operator",
            match.start(),
            match.end(),
            match.group(0),
            comparison_map[match.group(0)],
        )

    for match in _NUMBER_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group(0))
            append("number", match.start(), match.end(), value, value)

    return sorted(literals, key=lambda item: (item["start"], item["end"], item["kind"]))


def empty_semantic_ir(
    status: str = "needs_clarification",
    *,
    missing_fields: list[str] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "operations": [],
        "missing_fields": list(missing_fields or []),
        "policy_applications": [],
        "unsupported_operations": [],
        "message": message,
    }


def _has_plan_meaning(payload: dict[str, Any]) -> bool:
    def has_value(value: Any) -> bool:
        if value is None or value is False or value == "" or value == [] or value == {}:
            return False
        if isinstance(value, dict):
            return any(has_value(child) for child in value.values())
        if isinstance(value, list):
            return any(has_value(child) for child in value)
        return True

    return any(
        has_value(payload.get(key))
        for key in ("target_user", "exclude", "campaign_constraints", "aggregation_request", "set_expressions")
    )


def validate_semantic_ir(
    semantic_ir: Any,
    literal_bindings: Any,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(semantic_ir, dict):
        raise ValueError("semantic_ir must be an object")
    expected_keys = set(SEMANTIC_IR_LLM_JSON_SCHEMA["required"])
    if set(semantic_ir) != expected_keys:
        raise ValueError("semantic_ir fields do not match the closed schema")
    status = semantic_ir.get("status")
    if status not in SEMANTIC_IR_STATUSES:
        raise ValueError("semantic_ir.status is invalid")
    for key in ("operations", "missing_fields", "policy_applications", "unsupported_operations"):
        if not isinstance(semantic_ir.get(key), list):
            raise ValueError(f"semantic_ir.{key} must be an array")
    if semantic_ir.get("message") is not None and not isinstance(semantic_ir.get("message"), str):
        raise ValueError("semantic_ir.message must be a string or null")

    literal_items = literal_bindings if isinstance(literal_bindings, list) else []
    literal_by_id = {
        item.get("id"): item
        for item in literal_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(literal_by_id) != len(literal_items):
        raise ValueError("literal_bindings must contain unique object IDs")

    for index, operation in enumerate(semantic_ir["operations"]):
        if not isinstance(operation, dict):
            raise ValueError(f"semantic_ir.operations[{index}] must be an object")
        if set(operation) != {"kind", "metric_id", "direction", "bindings"}:
            raise ValueError(f"semantic_ir.operations[{index}] fields are invalid")
        if operation.get("kind") != "period_over_period_change":
            raise ValueError(f"semantic_ir.operations[{index}].kind is unsupported")
        if not isinstance(operation.get("metric_id"), str) or not operation["metric_id"].strip():
            raise ValueError(f"semantic_ir.operations[{index}].metric_id is required")
        if operation.get("direction") not in {"increase", "decrease"}:
            raise ValueError(f"semantic_ir.operations[{index}].direction is invalid")
        bindings = operation.get("bindings")
        if not isinstance(bindings, list):
            raise ValueError(f"semantic_ir.operations[{index}].bindings must be an array")
        by_role: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"role", "literal_id"}:
                raise ValueError(f"semantic_ir.operations[{index}] contains an invalid binding")
            role, literal_id = binding.get("role"), binding.get("literal_id")
            if role in by_role:
                raise ValueError(f"semantic_ir.operations[{index}] contains duplicate role {role}")
            literal = literal_by_id.get(literal_id)
            if literal is None:
                raise ValueError(f"semantic_ir.operations[{index}] references unknown literal {literal_id}")
            expected_kind = {
                "baseline": "date_window",
                "current": "date_window",
                "threshold": "percentage",
                "comparison": "comparison_operator",
            }.get(str(role))
            if expected_kind is None or literal.get("kind") != expected_kind:
                raise ValueError(f"semantic_ir.operations[{index}].{role} has the wrong literal kind")
            by_role[str(role)] = literal
        if not {"baseline", "current"} <= set(by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires baseline and current date literals")
        if ("threshold" in by_role) != ("comparison" in by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires threshold and comparison together")
        if by_role["baseline"]["id"] == by_role["current"]["id"]:
            raise ValueError(f"semantic_ir.operations[{index}] requires two distinct periods")

    missing_fields = semantic_ir["missing_fields"]
    policy_applications = semantic_ir["policy_applications"]
    unsupported = semantic_ir["unsupported_operations"]
    if not all(isinstance(item, str) and item.strip() for item in missing_fields):
        raise ValueError("semantic_ir.missing_fields must contain non-empty strings")
    if status == "needs_clarification" and not missing_fields:
        raise ValueError("needs_clarification requires missing_fields")
    if status != "needs_clarification" and missing_fields:
        raise ValueError("missing_fields is only valid for needs_clarification")
    if status == "policy_applied" and not policy_applications:
        raise ValueError("policy_applied requires policy_applications")
    for index, application in enumerate(policy_applications):
        if not isinstance(application, dict) or set(application) != {"policy_id", "fields"}:
            raise ValueError(f"semantic_ir.policy_applications[{index}] is invalid")
        if not isinstance(application.get("policy_id"), str) or not application["policy_id"].strip():
            raise ValueError(f"semantic_ir.policy_applications[{index}].policy_id is required")
        fields = application.get("fields")
        if not isinstance(fields, list) or not fields or not all(
            isinstance(field, str) and field.strip() for field in fields
        ):
            raise ValueError(f"semantic_ir.policy_applications[{index}].fields is invalid")
    if status != "policy_applied" and policy_applications:
        raise ValueError("policy_applications is only valid for policy_applied")
    if status == "unsupported" and not unsupported:
        raise ValueError("unsupported requires unsupported_operations")
    for index, item in enumerate(unsupported):
        if not isinstance(item, dict) or set(item) != {"kind", "reason", "evidence"}:
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] is invalid")
        if not all(isinstance(item.get(key), str) for key in ("kind", "reason", "evidence")):
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] must contain strings")
        if not item["kind"].strip() or not item["reason"].strip():
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] requires kind and reason")
    if status != "unsupported" and unsupported:
        raise ValueError("unsupported_operations is only valid for unsupported")
    if status in {"needs_clarification", "unsupported"} and semantic_ir["operations"]:
        raise ValueError(f"{status} cannot contain executable operations")
    if status in {"resolved", "policy_applied"} and not semantic_ir["operations"]:
        if payload is None or not _has_plan_meaning(payload):
            raise ValueError(f"{status} requires an operation or another grounded plan condition")
    return copy.deepcopy(semantic_ir)


def materialize_semantic_operations(
    semantic_ir: dict[str, Any], literal_bindings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project validated semantic relations into legacy execution slots.

    Only deterministic literal values are copied.  The model owns relation,
    direction, and metric selection; SQL and physical fields remain compiler-owned.
    """

    if semantic_ir.get("status") not in {"resolved", "policy_applied"}:
        return {}
    literal_by_id = {item["id"]: item for item in literal_bindings}
    slots: dict[str, Any] = {}
    for operation in semantic_ir.get("operations", []):
        if operation.get("kind") != "period_over_period_change":
            continue
        by_role = {
            binding["role"]: literal_by_id[binding["literal_id"]]
            for binding in operation.get("bindings", [])
        }
        baseline = copy.deepcopy(by_role["baseline"]["normalized"])
        current = copy.deepcopy(by_role["current"]["normalized"])
        direction = operation["direction"]
        arrow = "증가" if direction == "increase" else "감소"
        trend: dict[str, Any] = {
            "metric_id": operation["metric_id"],
            "direction": direction,
            "baseline": baseline,
            "current": current,
        }
        threshold = by_role.get("threshold")
        comparison = by_role.get("comparison")
        threshold_label = ""
        if threshold is not None and comparison is not None:
            value = threshold["normalized"]["value"]
            operator = comparison["normalized"]
            trend["relative_change"] = {
                "unit": "percent",
                "comparisons": [{"operator": operator, "value": value}],
            }
            operator_label = {">=": "이상", ">": "초과", "<=": "이하", "<": "미만"}[operator]
            threshold_label = f" {value:g}% {operator_label}" if isinstance(value, float) else f" {value}% {operator_label}"
        trend["label"] = (
            f"{baseline.get('label') or baseline['from']}→{current.get('label') or current['from']}"
            f"{threshold_label} {arrow}"
        )
        slots["metric_trend"] = trend
    return slots
