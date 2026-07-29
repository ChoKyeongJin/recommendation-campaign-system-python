from __future__ import annotations

import copy
from typing import Any

from .campaign_plan_v2 import (
    CAMPAIGN_INTENTS,
    CAMPAIGN_QUERY_PLAN_V2_LLM_JSON_SCHEMA,
    QUERY_IDENTITY_DIGEST_KEY,
    CampaignQueryPlanV2,
    CampaignQueryPlanValidationError,
    campaign_query_identity_digest,
)


CAMPAIGN_QUERY_PLAN_V3_VERSION = "3.0"


class CampaignQueryPlanV3(CampaignQueryPlanV2):
    """LLM-owned semantic plan.

    V3 deliberately remains compatible with the mutable V2 execution plan while
    adding two contracts that were previously implicit: every accepted value has
    source evidence, and anything the model cannot express is returned as an
    unresolved item instead of being guessed.
    """


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "null":
        return schema
    if isinstance(schema.get("type"), list) and "null" in schema["type"]:
        return schema
    if "anyOf" in schema and any(
        isinstance(item, dict) and item.get("type") == "null"
        for item in schema["anyOf"]
    ):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _strictify(schema: Any, *, required_here: bool = True) -> Any:
    """Return the fixed strict-function-calling form of a JSON schema.

    Chat Completions strict tools require every object property to be required
    and every object to reject additional properties.  Fields that used to be
    optional become required-but-nullable, keeping the semantic distinction
    without permitting an open-ended payload.
    """

    if isinstance(schema, list):
        return [_strictify(item) for item in schema]
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    out = {key: copy.deepcopy(value) for key, value in schema.items()}
    for keyword in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(keyword), list):
            out[keyword] = [_strictify(item) for item in out[keyword]]
    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"])
    if isinstance(out.get("$defs"), dict):
        out["$defs"] = {
            name: _strictify(value) for name, value in out["$defs"].items()
        }

    properties = out.get("properties")
    if isinstance(properties, dict):
        originally_required = set(out.get("required") or [])
        strict_properties: dict[str, Any] = {}
        for name, value in properties.items():
            strict_value = _strictify(value)
            if name not in originally_required:
                strict_value = _nullable(strict_value)
            strict_properties[name] = strict_value
        out["properties"] = strict_properties
        out["required"] = list(strict_properties)
        out["additionalProperties"] = False
    elif out.get("type") == "object":
        out["properties"] = {}
        out["required"] = []
        out["additionalProperties"] = False

    return out


_EVIDENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "text", "start", "end", "confidence"],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_UNRESOLVED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "reason", "evidence"],
    "properties": {
        "path": {"type": ["string", "null"]},
        "reason": {"type": "string", "minLength": 1},
        "evidence": {"type": "string"},
    },
}


def _build_llm_schema() -> dict[str, Any]:
    schema = copy.deepcopy(CAMPAIGN_QUERY_PLAN_V2_LLM_JSON_SCHEMA)
    properties = schema.setdefault("properties", {})
    properties["semantic_evidence"] = {
        "type": "array",
        "items": _EVIDENCE_ITEM_SCHEMA,
    }
    properties["unresolved"] = {
        "type": "array",
        "items": _UNRESOLVED_ITEM_SCHEMA,
    }
    required = set(schema.get("required") or [])
    required.update({"semantic_evidence", "unresolved"})
    schema["required"] = [name for name in properties if name in required]
    return _strictify(schema)


CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA = _build_llm_schema()

CAMPAIGN_QUERY_PLAN_V3_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_campaign_query_plan_v3",
        "description": (
            "사용자 원문의 캠페인/타겟팅 의미를 QueryPlan v3로 제출한다. "
            "SQL이나 물리 컬럼을 만들지 않고 원문 근거와 미해결 의미를 함께 반환한다."
        ),
        "strict": True,
        "parameters": CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA,
    },
}


def attach_campaign_query_plan_v3_identity(payload: Any, query: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    enriched = copy.deepcopy(payload)
    enriched.update(
        {
            "schema_version": CAMPAIGN_QUERY_PLAN_V3_VERSION,
            "raw_query": query,
            "original_query": query,
            "planning_query": query,
            "normalized_query": query,
        }
    )
    enriched[QUERY_IDENTITY_DIGEST_KEY] = campaign_query_identity_digest(enriched)
    return enriched


def build_campaign_query_plan_v3_fallback(query: str) -> CampaignQueryPlanV3:
    payload = attach_campaign_query_plan_v3_identity(
        {
            "intent": "unknown",
            "target_user": {},
            "exclude": {},
            "campaign_constraints": {},
            "aggregation_request": None,
            "set_expressions": [],
            "computed_metrics": [],
            "result_limit": None,
            "semantic_evidence": [],
            "unresolved": [
                {
                    "path": None,
                    "reason": "llm_structuring_unavailable",
                    "evidence": query,
                }
            ],
        },
        query,
    )
    return CampaignQueryPlanV3(payload)


def validate_campaign_query_plan_v3(
    payload: Any,
    *,
    query: str | None = None,
) -> CampaignQueryPlanV3:
    if not isinstance(payload, dict):
        raise CampaignQueryPlanValidationError("campaign QueryPlan v3 must be an object")
    if payload.get("schema_version") != CAMPAIGN_QUERY_PLAN_V3_VERSION:
        raise CampaignQueryPlanValidationError(
            f"schema_version must be {CAMPAIGN_QUERY_PLAN_V3_VERSION}"
        )
    for key in ("raw_query", "original_query", "planning_query", "normalized_query"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CampaignQueryPlanValidationError(f"{key} must be a non-empty string")
    if query is not None and payload["original_query"] != query:
        raise CampaignQueryPlanValidationError(
            "original_query must exactly match the input query"
        )
    if payload.get("intent") not in CAMPAIGN_INTENTS:
        raise CampaignQueryPlanValidationError("intent is not supported")
    for key in ("target_user", "exclude", "campaign_constraints"):
        if not isinstance(payload.get(key), dict):
            raise CampaignQueryPlanValidationError(f"{key} must be an object")

    evidence = payload.get("semantic_evidence")
    if not isinstance(evidence, list):
        raise CampaignQueryPlanValidationError("semantic_evidence must be an array")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] must be an object"
            )
        text = item.get("text")
        start, end = item.get("start"), item.get("end")
        confidence = item.get("confidence")
        if not isinstance(text, str) or text not in payload["original_query"]:
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}].text must occur in original_query"
            )
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] must contain integer start/end"
            )
        if start < 0 or end < start or payload["original_query"][start:end] != text:
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] span does not match original_query"
            )
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}].confidence must be between 0 and 1"
            )

    unresolved = payload.get("unresolved")
    if not isinstance(unresolved, list) or not all(
        isinstance(item, dict) and isinstance(item.get("reason"), str)
        for item in unresolved
    ):
        raise CampaignQueryPlanValidationError("unresolved must be an array of objects")

    expected_digest = campaign_query_identity_digest(payload)
    if payload.get(QUERY_IDENTITY_DIGEST_KEY) != expected_digest:
        raise CampaignQueryPlanValidationError("query identity digest mismatch")
    return CampaignQueryPlanV3(copy.deepcopy(payload))
