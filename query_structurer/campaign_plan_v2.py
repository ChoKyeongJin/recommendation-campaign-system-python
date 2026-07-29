from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from entity_set import derived_set_ast_error
import targeting_ir


CAMPAIGN_QUERY_PLAN_VERSION = "2.1"
QUERY_IDENTITY_DIGEST_KEY = "query_identity_digest"
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
    def raw_query(self) -> str:
        return str(self["raw_query"])

    @property
    def planning_query(self) -> str:
        return str(self["planning_query"])

    @property
    def normalized_query(self) -> str:
        return str(self["normalized_query"])

    @property
    def intent(self) -> str:
        return str(self["intent"])

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self))


def campaign_query_identity_digest(payload: dict[str, Any]) -> str:
    """원문/타겟 원문/실제 파싱문의 결합 무결성 해시."""
    identity = {
        "raw_query": payload.get("raw_query"),
        "original_query": payload.get("original_query"),
        "planning_query": payload.get("planning_query"),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def verify_campaign_query_identity(payload: dict[str, Any]) -> bool:
    expected = payload.get(QUERY_IDENTITY_DIGEST_KEY)
    actual = campaign_query_identity_digest(payload)
    if not isinstance(expected, str) or expected != actual:
        raise CampaignQueryPlanValidationError("query identity changed after initial capture")
    return True


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
        "aggregate_conditions": copy.deepcopy(
            targeting_ir.SLOT_SHAPES["aggregate_conditions"].schema
        ),
        "profile_date_conditions": {"type": "array", "items": {"type": "object"}},
        "campaign_responses": {"type": "array", "items": {"type": "object"}},
        "campaign_response_frequency": _nullable({"type": "object"}),
        "campaign_buy_amount": _nullable({"type": "object"}),
        "campaign_buy_count": _nullable({"type": "object"}),
        "cart_retention": _nullable({"type": "object"}),
        "cart_type": _nullable({"type": "object"}),
        "cart_aggregate": _nullable({"type": "object"}),
        "cart_absence": _nullable({"type": "boolean"}),
        "entity_set_condition": _nullable({
            "type": "object",
            "description": "집계 → 랭킹 → 회원 집합으로 구성된 파생 집합 조건.",
            "properties": {
                "derived_set_ast": {"$ref": "#/$defs/derivedSetMemberNode"},
            },
        }),
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
    "$defs": {
        "derivedSetDimensionFilter": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "dimension", "operator", "value"],
            "properties": {
                "type": {"const": "dimension_filter"},
                "dimension": {"type": "string", "minLength": 1},
                "operator": {"enum": ["equals", "contains"]},
                "value": {"type": "string", "minLength": 1},
            },
        },
        "derivedSetAggregationNode": {
            "type": "object",
            "required": ["type", "relation", "group_by", "measure"],
            "properties": {
                "type": {"const": "aggregation"},
                "relation": {"type": "string", "minLength": 1},
                "group_by": {"type": "string", "minLength": 1},
                "measure": {"type": "string", "minLength": 1},
                "window": {"type": "object"},
                "filters": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/derivedSetDimensionFilter"},
                },
            },
        },
        "derivedSetRankingNode": {
            "type": "object",
            "required": ["type", "direction", "limit", "source"],
            "properties": {
                "type": {"const": "ranking"},
                "direction": {"enum": ["top", "bottom"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "source": {"$ref": "#/$defs/derivedSetAggregationNode"},
            },
        },
        "derivedSetMemberNode": {
            "type": "object",
            "required": ["type", "relation", "exists", "source"],
            "properties": {
                "type": {"const": "member_set"},
                "relation": {"type": "string", "minLength": 1},
                "exists": {"type": "boolean"},
                "source": {"$ref": "#/$defs/derivedSetRankingNode"},
            },
        },
    },
    "required": [
        "schema_version",
        "raw_query",
        "original_query",
        "planning_query",
        "normalized_query",
        "intent",
        "target_user",
        "exclude",
        "campaign_constraints",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": CAMPAIGN_QUERY_PLAN_VERSION},
        "raw_query": {
            "type": "string",
            "minLength": 1,
            "description": "API에 입력된 사용자 문자열 전체. 정규화하거나 절을 잘라내지 않는다.",
        },
        "original_query": {"type": "string", "minLength": 1},
        "planning_query": {
            "type": "string",
            "minLength": 1,
            "description": "정규화와 스코프 분리 후 실제 파서가 해석한 문자열.",
        },
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
        "condition_evaluations": {"type": "array", "items": {"type": "object"}},
        "set_expressions": {"type": "array", "items": {"type": "object"}},
        "computed_metrics": {"type": "array", "items": {"type": "object"}},
        "result_limit": _nullable({"type": "integer", "minimum": 1}),
        "source_requirements": {"type": "array", "items": _SOURCE_REQUIREMENT_SCHEMA},
        "source_requirements_digest": {"type": "string"},
        QUERY_IDENTITY_DIGEST_KEY: {"type": "string"},
        "strict_source_coverage": {"type": "boolean"},
        "unresolved_source_conditions": {"type": "array", "items": {"type": "object"}},
    },
}


_APPLICATION_OWNED_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "raw_query",
        "original_query",
        "planning_query",
        "normalized_query",
        "source_requirements",
        "source_requirements_digest",
        QUERY_IDENTITY_DIGEST_KEY,
        "strict_source_coverage",
        "unresolved_source_conditions",
        "condition_evaluations",
    }
)


def _campaign_query_plan_v2_llm_schema() -> dict[str, Any]:
    """Tool 호출에서 모델이 결정할 필드만 노출한다."""
    schema = copy.deepcopy(CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA)
    schema.pop("$id", None)
    schema["required"] = [
        key for key in schema.get("required", []) if key not in _APPLICATION_OWNED_PLAN_FIELDS
    ]
    properties = schema.get("properties", {})
    for key in _APPLICATION_OWNED_PLAN_FIELDS:
        properties.pop(key, None)
    return schema


CAMPAIGN_QUERY_PLAN_V2_LLM_JSON_SCHEMA = _campaign_query_plan_v2_llm_schema()


CAMPAIGN_QUERY_PLAN_V2_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_campaign_query_plan_v2",
        "description": "캠페인 타겟팅 요청을 실행기와 공유하는 QueryPlan v2로 제출한다.",
        "parameters": CAMPAIGN_QUERY_PLAN_V2_LLM_JSON_SCHEMA,
    },
}


def attach_campaign_query_plan_v2_identity(payload: Any, query: str) -> Any:
    """모델 출력에 애플리케이션이 소유하는 버전·질의 identity를 결정론적으로 붙인다."""
    if not isinstance(payload, dict):
        return payload
    enriched = copy.deepcopy(payload)
    enriched.update(
        {
            "schema_version": CAMPAIGN_QUERY_PLAN_VERSION,
            "raw_query": query,
            "original_query": query,
            "planning_query": query,
            "normalized_query": query,
        }
    )
    return enriched


def build_campaign_query_plan_v2_fallback(query: str) -> CampaignQueryPlanV2:
    plan = CampaignQueryPlanV2(
        schema_version=CAMPAIGN_QUERY_PLAN_VERSION,
        raw_query=query,
        original_query=query,
        planning_query=query,
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
    plan[QUERY_IDENTITY_DIGEST_KEY] = campaign_query_identity_digest(plan)
    return plan


def validate_campaign_query_plan_v2(
    payload: Any,
    *,
    query: str | None = None,
    raw_query: str | None = None,
) -> CampaignQueryPlanV2:
    if not isinstance(payload, dict):
        raise CampaignQueryPlanValidationError("campaign QueryPlan v2 must be an object")
    version = payload.get("schema_version")
    if version != CAMPAIGN_QUERY_PLAN_VERSION:
        raise CampaignQueryPlanValidationError(
            f"schema_version must be {CAMPAIGN_QUERY_PLAN_VERSION}"
        )
    preserved_raw_query = _non_empty_string(payload.get("raw_query"), "raw_query")
    if raw_query is not None and preserved_raw_query != raw_query:
        raise CampaignQueryPlanValidationError("raw_query must exactly match the API input query")
    original_query = _non_empty_string(payload.get("original_query"), "original_query")
    if query is not None and original_query != query:
        raise CampaignQueryPlanValidationError("original_query must exactly match the input query")
    if original_query not in preserved_raw_query:
        raise CampaignQueryPlanValidationError("original_query must be preserved within raw_query")
    _non_empty_string(payload.get("planning_query"), "planning_query")
    _non_empty_string(payload.get("normalized_query"), "normalized_query")
    if payload.get("intent") not in CAMPAIGN_INTENTS:
        raise CampaignQueryPlanValidationError("intent is not supported by campaign QueryPlan v2")
    for key in ("target_user", "exclude", "campaign_constraints"):
        if not isinstance(payload.get(key), dict):
            raise CampaignQueryPlanValidationError(f"{key} must be an object")
    entity_set = payload["target_user"].get("entity_set_condition")
    if isinstance(entity_set, dict) and "derived_set_ast" in entity_set:
        ast_error = derived_set_ast_error(entity_set.get("derived_set_ast"))
        if ast_error:
            raise CampaignQueryPlanValidationError(
                f"target_user.entity_set_condition.derived_set_ast is invalid: {ast_error}"
            )
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
    unresolved_source_conditions = payload.get("unresolved_source_conditions", [])
    if not isinstance(unresolved_source_conditions, list) or not all(
        isinstance(item, dict) for item in unresolved_source_conditions
    ):
        raise CampaignQueryPlanValidationError("unresolved_source_conditions must be an array of objects")
    strict_source_coverage = payload.get("strict_source_coverage")
    if strict_source_coverage is not None and not isinstance(strict_source_coverage, bool):
        raise CampaignQueryPlanValidationError("strict_source_coverage must be a boolean")
    actual_identity_digest = campaign_query_identity_digest(payload)
    supplied_identity_digest = payload.get(QUERY_IDENTITY_DIGEST_KEY)
    if supplied_identity_digest is not None and supplied_identity_digest != actual_identity_digest:
        raise CampaignQueryPlanValidationError("query identity digest mismatch")
    validated = CampaignQueryPlanV2(copy.deepcopy(payload))
    validated[QUERY_IDENTITY_DIGEST_KEY] = actual_identity_digest
    return validated


def as_campaign_query_plan_v2(
    plan: dict[str, Any],
    *,
    original_query: str,
    raw_query: str | None = None,
    planning_query: str | None = None,
    normalized_query: str | None = None,
) -> CampaignQueryPlanV2:
    """Attach the v2 identity to an existing execution plan without remapping it."""

    payload = copy.deepcopy(dict(plan))
    payload["schema_version"] = CAMPAIGN_QUERY_PLAN_VERSION
    payload["raw_query"] = raw_query or payload.get("raw_query") or original_query
    payload["original_query"] = original_query
    payload["planning_query"] = planning_query or payload.get("planning_query") or original_query
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
    payload.setdefault("unresolved_source_conditions", [])
    payload[QUERY_IDENTITY_DIGEST_KEY] = campaign_query_identity_digest(payload)
    return validate_campaign_query_plan_v2(
        payload,
        query=original_query,
        raw_query=payload["raw_query"],
    )


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
