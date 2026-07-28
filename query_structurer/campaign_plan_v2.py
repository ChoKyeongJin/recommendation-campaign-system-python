from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


CAMPAIGN_QUERY_PLAN_VERSION = "2.0"
CAMPAIGN_INTENTS = {
    "recommend_campaign",
    "find_user_segment",
    "analyze_aggregation",
    "unknown",
}


class CampaignQueryPlanValidationError(ValueError):
    """Raised when the shared campaign planning/execution IR is malformed."""


class CampaignQueryPlanV2(dict[str, Any]):
    """The single mutable IR shared by query structuring and SQL execution.

    It deliberately remains a ``dict`` subtype because the existing compiler
    enriches plans in place.  The type gives that payload a versioned contract
    without introducing a second DTO and a lossy front-to-back conversion.
    """

    @property
    def schema_version(self) -> str:
        return str(self["schema_version"])

    @property
    def original_query(self) -> str:
        return str(self["original_query"])

    @property
    def normalized_query(self) -> str:
        return str(self["normalized_query"])

    @property
    def intent(self) -> str:
        return str(self["intent"])

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self))


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_TARGET_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    # Targeting slots are registry-driven and grow independently.  Known core
    # properties document the stable contract while extensions remain valid.
    "additionalProperties": True,
    "properties": {
        "gender": _nullable({"type": "string"}),
        "age_min": _nullable({"type": "integer"}),
        "age_max": _nullable({"type": "integer"}),
        "age_exclude_ranges": {"type": "array", "items": {"type": "object"}},
        "lifecycle": {"type": "array", "items": {"type": "string"}},
        "interests": {"type": "array", "items": {"type": "string"}},
        "preferred_channels": {"type": "array", "items": {"type": "string"}},
        "behaviors": {"type": "array", "items": {"type": "string"}},
        "purchase_object": _nullable({"type": "string"}),
        "purchase_date": _nullable({"type": "object"}),
        "price_sensitivity": _nullable({"type": "string"}),
        "inactivity_period": _nullable({"type": "object"}),
        "recent_login": _nullable({"type": "object"}),
        "purchase_inactivity": _nullable({"type": "object"}),
        "birthday_target": _nullable({"type": "object"}),
        "signup_target": _nullable({"type": "object"}),
        "aggregate_conditions": {"type": "array", "items": {"type": "object"}},
        "profile_date_conditions": {"type": "array", "items": {"type": "object"}},
        "campaign_responses": {"type": "array", "items": {"type": "object"}},
        "campaign_response_frequency": _nullable({"type": "object"}),
        "campaign_buy_amount": _nullable({"type": "object"}),
        "campaign_buy_count": _nullable({"type": "object"}),
        "cart_retention": _nullable({"type": "object"}),
        "cart_type": _nullable({"type": "object"}),
        "cart_aggregate": _nullable({"type": "object"}),
        "cart_absence": _nullable({"type": "boolean"}),
    },
}


_SOURCE_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "플래너가 최초 파싱 직후 봉인하는 원문 요구 스냅샷. LLM은 생성하지 않는다.",
    "required": ["id", "path", "polarity", "source", "source_text", "source_span", "value"],
    "properties": {
        "id": {"type": "string"},
        "path": _nullable({"type": "string"}),
        "polarity": {"type": "string", "enum": ["positive", "negative"]},
        "source": {"type": "string"},
        "source_text": {"type": "string"},
        "source_span": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
        },
        "value": {},
    },
}


CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA: dict[str, Any] = {
    "$id": "campaign-query-plan-v2",
    "type": "object",
    "additionalProperties": True,
    "required": [
        "schema_version",
        "original_query",
        "normalized_query",
        "intent",
        "target_user",
        "exclude",
        "campaign_constraints",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": CAMPAIGN_QUERY_PLAN_VERSION},
        "original_query": {"type": "string", "minLength": 1},
        "normalized_query": {"type": "string", "minLength": 1},
        "intent": {"type": "string", "enum": sorted(CAMPAIGN_INTENTS)},
        "target_user": _TARGET_USER_SCHEMA,
        "exclude": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "gender": {"type": "array", "items": {"type": "string"}},
                "interests": {"type": "array", "items": {"type": "string"}},
                "lifecycle": {"type": "array", "items": {"type": "string"}},
            },
        },
        "campaign_constraints": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "category": {"type": "array", "items": {"type": "string"}},
                "objective": _nullable({"type": "string"}),
                "offer_type": _nullable({"type": "string"}),
                "channels": {"type": "array", "items": {"type": "string"}},
                "sell_object": _nullable({"type": "string"}),
            },
        },
        "aggregation_request": _nullable({"type": "object"}),
        "set_expressions": {"type": "array", "items": {"type": "object"}},
        "computed_metrics": {"type": "array", "items": {"type": "object"}},
        "result_limit": _nullable({"type": "integer", "minimum": 1}),
        "source_requirements": {"type": "array", "items": _SOURCE_REQUIREMENT_SCHEMA},
        "source_requirements_digest": {"type": "string"},
    },
}


CAMPAIGN_QUERY_PLAN_V2_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_campaign_query_plan_v2",
        "description": "캠페인 타겟팅 요청을 실행기와 공유하는 QueryPlan v2로 제출한다.",
        "parameters": CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA,
    },
}


def build_campaign_query_plan_v2_fallback(query: str) -> CampaignQueryPlanV2:
    return CampaignQueryPlanV2(
        schema_version=CAMPAIGN_QUERY_PLAN_VERSION,
        original_query=query,
        normalized_query=query,
        intent="unknown",
        target_user={},
        exclude={"gender": [], "interests": [], "lifecycle": []},
        campaign_constraints={
            "category": [],
            "objective": None,
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        aggregation_request=None,
        set_expressions=[],
        computed_metrics=[],
        result_limit=None,
    )


def validate_campaign_query_plan_v2(
    payload: Any,
    *,
    query: str | None = None,
) -> CampaignQueryPlanV2:
    if not isinstance(payload, dict):
        raise CampaignQueryPlanValidationError("campaign QueryPlan v2 must be an object")
    version = payload.get("schema_version")
    if version != CAMPAIGN_QUERY_PLAN_VERSION:
        raise CampaignQueryPlanValidationError(
            f"schema_version must be {CAMPAIGN_QUERY_PLAN_VERSION}"
        )
    original_query = _non_empty_string(payload.get("original_query"), "original_query")
    if query is not None and original_query != query:
        raise CampaignQueryPlanValidationError("original_query must exactly match the input query")
    _non_empty_string(payload.get("normalized_query"), "normalized_query")
    if payload.get("intent") not in CAMPAIGN_INTENTS:
        raise CampaignQueryPlanValidationError("intent is not supported by campaign QueryPlan v2")
    for key in ("target_user", "exclude", "campaign_constraints"):
        if not isinstance(payload.get(key), dict):
            raise CampaignQueryPlanValidationError(f"{key} must be an object")
    for key in ("set_expressions", "computed_metrics"):
        value = payload.get(key, [])
        if not isinstance(value, list):
            raise CampaignQueryPlanValidationError(f"{key} must be an array")
    result_limit = payload.get("result_limit")
    if result_limit is not None and (
        not isinstance(result_limit, int) or isinstance(result_limit, bool) or result_limit < 1
    ):
        raise CampaignQueryPlanValidationError("result_limit must be a positive integer or null")
    source_requirements = payload.get("source_requirements")
    if source_requirements is not None:
        if not isinstance(source_requirements, list):
            raise CampaignQueryPlanValidationError("source_requirements must be an array")
        for index, requirement in enumerate(source_requirements):
            _validate_source_requirement(requirement, index)
        digest = payload.get("source_requirements_digest")
        if digest is not None:
            actual = hashlib.sha256(
                json.dumps(
                    source_requirements,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if not isinstance(digest, str) or digest != actual:
                raise CampaignQueryPlanValidationError("source_requirements digest mismatch")
    return CampaignQueryPlanV2(copy.deepcopy(payload))


def as_campaign_query_plan_v2(
    plan: dict[str, Any],
    *,
    original_query: str,
    normalized_query: str | None = None,
) -> CampaignQueryPlanV2:
    """Attach the v2 identity to an existing execution plan without remapping it."""

    payload = copy.deepcopy(dict(plan))
    payload["schema_version"] = CAMPAIGN_QUERY_PLAN_VERSION
    payload["original_query"] = original_query
    payload["normalized_query"] = (
        normalized_query
        or payload.get("normalized_query")
        or (payload.get("retrieval") or {}).get("query")
        or original_query
    )
    payload.setdefault("intent", "unknown")
    payload.setdefault("target_user", {})
    payload.setdefault("exclude", {"gender": [], "interests": [], "lifecycle": []})
    payload.setdefault("campaign_constraints", {})
    payload.setdefault("set_expressions", [])
    payload.setdefault("computed_metrics", [])
    payload.setdefault("result_limit", None)
    return validate_campaign_query_plan_v2(payload, query=original_query)


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignQueryPlanValidationError(f"{path} must be a non-empty string")
    return value


def _validate_source_requirement(value: Any, index: int) -> None:
    path = f"source_requirements[{index}]"
    if not isinstance(value, dict):
        raise CampaignQueryPlanValidationError(f"{path} must be an object")
    for key in ("id", "source", "source_text"):
        _non_empty_string(value.get(key), f"{path}.{key}")
    if value.get("polarity") not in {"positive", "negative"}:
        raise CampaignQueryPlanValidationError(f"{path}.polarity is invalid")
    span = value.get("source_span")
    if not isinstance(span, dict):
        raise CampaignQueryPlanValidationError(f"{path}.source_span must be an object")
    start, end = span.get("start"), span.get("end")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (start, end)):
        raise CampaignQueryPlanValidationError(f"{path}.source_span must contain integer start/end")
    if start < 0 or end < start:
        raise CampaignQueryPlanValidationError(f"{path}.source_span is out of range")
