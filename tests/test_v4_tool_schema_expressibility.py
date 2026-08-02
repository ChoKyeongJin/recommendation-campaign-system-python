"""V4 strict 도구 스키마가 canonical Event IR 의미를 실제로 표현하는지 검증한다.

계약은 슬롯마다 필드를 늘리지 않는다. 고정된 논리·관계 대수와 Semantic Catalog 심볼만으로
``기간 내 사건 집계 AND 다른 사건 부재`` 같은 조합을 표현할 수 있어야 한다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

import audience_runtime
import event_ir
from query_structurer.campaign_plan_v4 import CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA

_LLM_ROOT = {"intent", "campaign_constraints", "result_limit", "audience_requirement"}
_FORBIDDEN_LLM_ROOT = {
    "target_user",
    "exclude",
    "semantic_plan",
    "semantic_ir",
    "semantic_evidence",
    "unresolved",
    "event_expression",
    "aggregation_request",
    "set_expressions",
    "computed_metrics",
    "external_conditions",
    "condition_evaluations",
    "member_metric_ranking",
    "literal_bindings",
    "source_requirements",
}


def _closed_empty(node: object) -> bool:
    return (
        isinstance(node, dict)
        and node.get("type") == "object"
        and node.get("additionalProperties") is False
        and not node.get("properties")
    )


def _walk_schema(node: object, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(node, dict):
        return []
    found: list[tuple[str, dict[str, Any]]] = [(path, node)]
    for key, child in (node.get("properties") or {}).items():
        found.extend(_walk_schema(child, f"{path}.{key}"))
    if isinstance(node.get("items"), dict):
        found.extend(_walk_schema(node["items"], path + "[]"))
    for keyword in ("anyOf", "oneOf", "allOf"):
        for index, branch in enumerate(node.get(keyword) or []):
            found.extend(_walk_schema(branch, f"{path}.{keyword}[{index}]"))
    for name, child in (node.get("$defs") or {}).items():
        found.extend(_walk_schema(child, f"$defs.{name}"))
    return found


def _discriminators(schema: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for _, node in _walk_schema(schema):
        type_property = (node.get("properties") or {}).get("type")
        if isinstance(type_property, dict):
            values.update(str(value) for value in type_property.get("enum") or [])
    return values


def _catalog_symbol_enums(schema: dict[str, Any], discriminator: str) -> set[frozenset[str]]:
    enums: set[frozenset[str]] = set()
    for _, node in _walk_schema(schema):
        properties = node.get("properties") or {}
        type_property = properties.get("type")
        if not isinstance(type_property, dict) or discriminator not in (
            type_property.get("enum") or []
        ):
            continue
        name_property = properties.get("name")
        if isinstance(name_property, dict) and name_property.get("enum"):
            enums.add(frozenset(str(value) for value in name_property["enum"]))
    return enums


def _representative_payload() -> dict[str, Any]:
    first_evidence = {"text": "최근 30일 캠페인 발송 성공 횟수가 3회 이상", "start": 0, "end": 29}
    second_evidence = {"text": "구매반응이 없는", "start": 32, "end": 40}
    expression = {
        "type": "and",
        "operands": [
            {
                "type": "comparison",
                "operator": ">=",
                "left": {
                    "type": "aggregate",
                    "function": "count",
                    "relation": {
                        "type": "filter",
                        "relation": {
                            "type": "source",
                            "name": "campaign_contact_success",
                        },
                        "where": {
                            "type": "time_filter",
                            "field": {
                                "type": "field",
                                "name": "campaign_contact_success.occurred_at",
                            },
                            "window": {
                                "type": "rolling",
                                "start": None,
                                "end_exclusive": None,
                                "value": 30,
                                "unit": "day",
                            },
                        },
                    },
                    "expression": {
                        "type": "field",
                        "name": "campaign_contact_success.execution_id",
                    },
                    "distinct": True,
                },
                "right": {"type": "literal", "value": 3},
                "evidence": first_evidence,
            },
            {
                "type": "not",
                "operand": {
                    "type": "exists",
                    "relation": {
                        "type": "source",
                        "name": "campaign_purchase_response",
                    },
                    "evidence": second_evidence,
                },
            },
        ],
    }
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": "재반응 유도",
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": expression, "issues": []},
    }


def test_llm_schema_has_fixed_root_and_no_execution_or_legacy_slots() -> None:
    root = set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA.get("properties") or {})
    assert root == _LLM_ROOT
    assert set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA.get("required") or []) == _LLM_ROOT
    assert not (_FORBIDDEN_LLM_ROOT & root)


def test_llm_schema_has_no_inexpressible_closed_empty_nodes() -> None:
    holes = [
        path
        for path, node in _walk_schema(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA)
        if _closed_empty(node)
    ]
    assert not holes, f"V4 strict 스키마에 표현 불가능한 빈 닫힌 객체가 있다: {holes}"


def test_audience_requirement_exposes_the_complete_fixed_event_ir_algebra() -> None:
    expression_schema = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"][
        "audience_requirement"
    ]["properties"]["expression"]
    discriminators = _discriminators(expression_schema)
    assert event_ir.NODE_TYPES <= discriminators, (
        f"LLM Event IR 스키마에서 빠진 고정 대수 노드: {sorted(event_ir.NODE_TYPES - discriminators)}"
    )


def test_event_ir_catalog_enums_come_from_the_resolved_catalog() -> None:
    expression_schema = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"][
        "audience_requirement"
    ]["properties"]["expression"]
    catalog = audience_runtime.resolve_audience_catalog()
    # ``subject`` is the correlated base row, not an event relation.  The LLM source
    # enum therefore follows exactly the compiler-capable event/field projections.
    assert frozenset(catalog.compiler_events) in _catalog_symbol_enums(
        expression_schema, "source"
    )
    assert frozenset(catalog.compiler_fields) in _catalog_symbol_enums(
        expression_schema, "field"
    )


def test_schema_expresses_windowed_aggregate_and_negated_existence_composition() -> None:
    payload = _representative_payload()
    validator = Draft202012Validator(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)

    # JSON Schema와 런타임 역직렬화기가 같은 표현을 받아야 한다.
    expression = payload["audience_requirement"]["expression"]
    parsed = event_ir.condition_from_dict(expression)
    assert isinstance(parsed, event_ir.And)
    assert isinstance(parsed.operands[0], event_ir.Comparison)
    assert isinstance(parsed.operands[1], event_ir.Not)
