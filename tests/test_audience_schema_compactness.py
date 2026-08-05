from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator

import audience_runtime
from query_structurer.campaign_plan_v4 import (
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    attach_campaign_query_plan_v4_identity,
)


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _enum_value_count(schema: dict[str, Any]) -> int:
    return sum(
        len(node["enum"])
        for node in _walk(schema)
        if isinstance(node.get("enum"), list)
    )


def _canonical_bytes(schema: dict[str, Any]) -> bytes:
    return json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(schema: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(schema)).hexdigest()


def _representative_payload() -> dict[str, Any]:
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": {
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
                                "relation": {"type": "source", "name": "purchase"},
                                "where": {
                                    "type": "time_filter",
                                    "field": {
                                        "type": "field",
                                        "name": "purchase.occurred_at",
                                    },
                                    "window": {
                                        "type": "rolling",
                                        "value": 30,
                                        "unit": "day",
                                    },
                                },
                            },
                            "expression": {
                                "type": "field",
                                "name": "purchase.order_id",
                            },
                            "distinct": True,
                        },
                        "right": {"type": "literal", "value": 3},
                        "evidence": {
                            "text": "최근 30일 구매 3회 이상",
                            "start": 0,
                            "end": 14,
                        },
                    },
                    {
                        "type": "not",
                        "operand": {
                            "type": "comparison",
                            "operator": "=",
                            "left": {
                                "type": "field",
                                "name": "subject.gender",
                            },
                            "right": {"type": "literal", "value": "M"},
                            "evidence": {
                                "text": "남자는 제외",
                                "start": 18,
                                "end": 24,
                            },
                        },
                    },
                ],
            },
            "issues": [],
        },
    }


def test_strict_llm_schema_has_bounded_enum_cardinality() -> None:
    Draft202012Validator.check_schema(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA)
    assert _enum_value_count(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA) < 1000

    shallow = audience_runtime.audience_expression_json_schema(depth=1)
    deep = audience_runtime.audience_expression_json_schema(depth=64)
    assert _fingerprint(deep) == _fingerprint(shallow)
    assert len(_canonical_bytes(deep)) == len(_canonical_bytes(shallow))


def test_catalog_extension_does_not_change_schema_structure_or_size(
    tmp_path: Path,
) -> None:
    baseline = audience_runtime.audience_expression_json_schema()
    extended_config = copy.deepcopy(audience_runtime.catalog_snapshot())
    extended_config.setdefault("sources", {})["synthetic_signal"] = {
        "table": "FACT_SYNTHETIC_SIGNAL",
        "alias": "SS",
        "subject_key": "MEMBER_NO",
        "event_subject_key": "MEMBER_NO",
        "time_column": "OCCURRED_ON",
        "time_format": "char8",
        "binding": "fact_table",
        "grain": "event",
        "label": "Synthetic signal",
    }
    extended_config.setdefault("fields", {})["synthetic_signal.value"] = {
        "source": "synthetic_signal",
        "column": "SIGNAL_VALUE",
        "data_type": "number",
        "label": "Synthetic value",
    }
    catalog_path = tmp_path / "audience_catalog_extended.json"
    catalog_path.write_text(
        json.dumps(extended_config, ensure_ascii=False), encoding="utf-8"
    )

    extended_catalog = audience_runtime.resolve_audience_catalog(catalog_path)
    assert "synthetic_signal" in extended_catalog.compiler_events
    assert "synthetic_signal.value" in extended_catalog.compiler_fields

    extended = audience_runtime.audience_expression_json_schema(path=catalog_path)
    assert _fingerprint(extended) == _fingerprint(baseline)
    assert len(_canonical_bytes(extended)) == len(_canonical_bytes(baseline))


def test_strict_schema_accepts_recursive_boolean_relation_and_scalar_composition() -> None:
    validator = Draft202012Validator(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA)
    errors = list(validator.iter_errors(_representative_payload()))
    assert not errors, "\n".join(error.message for error in errors)


def test_catalog_membership_remains_a_fail_closed_runtime_check() -> None:
    query = "미등록 사건이 있는 회원"
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": {
                "type": "exists",
                "relation": {"type": "source", "name": "unregistered_event"},
                "evidence": {"text": query, "start": 0, "end": len(query)},
            },
            "issues": [],
        },
    }
    # The compact schema checks canonical shape, not business catalog membership.
    assert not list(
        Draft202012Validator(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA).iter_errors(
            payload
        )
    )

    enriched = attach_campaign_query_plan_v4_identity(
        payload, query, current_date="2026-08-02"
    )
    assert enriched.get("event_expression") is None
    assert enriched["semantic_ir"]["status"] == "unsupported"
    assert {
        (issue["code"], issue["argument"])
        for issue in enriched["audience_requirement"]["issues"]
    } == {("unsupported_semantics", "unregistered_event")}
