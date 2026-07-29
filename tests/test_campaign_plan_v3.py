from __future__ import annotations

import copy
import json

import pytest

import graph_rag
from query_structurer.campaign_plan_v3 import (
    CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA,
    CAMPAIGN_QUERY_PLAN_V3_TOOL,
    attach_campaign_query_plan_v3_identity,
    validate_campaign_query_plan_v3,
)
from query_structurer.structurer import LLMCampaignQueryPlanV3Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext


def _input(query: str = "서울 여성 회원") -> QueryStructuringInput:
    return QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-07-29", timezone="Asia/Seoul"
        ),
    )


def _assert_strict_objects(schema: object) -> None:
    if isinstance(schema, list):
        for item in schema:
            _assert_strict_objects(item)
        return
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required") or []) == set(properties)
    for value in schema.values():
        _assert_strict_objects(value)


def _payload(query: str = "서울 여성 회원") -> dict:
    start = query.index("여성")
    return {
        "intent": "find_user_segment",
        "target_user": {"gender": "female"},
        "exclude": {},
        "campaign_constraints": {},
        "aggregation_request": None,
        "set_expressions": [],
        "computed_metrics": [],
        "result_limit": None,
        "semantic_evidence": [
            {
                "path": "target_user.gender",
                "text": "여성",
                "start": start,
                "end": start + 2,
                "confidence": 0.99,
            }
        ],
        "unresolved": [],
    }


def test_v3_tool_uses_strict_fixed_schema() -> None:
    assert CAMPAIGN_QUERY_PLAN_V3_TOOL["function"]["strict"] is True
    _assert_strict_objects(CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA)


def test_v3_structurer_binds_values_to_source_evidence() -> None:
    plan = LLMCampaignQueryPlanV3Structurer(
        lambda _messages: json.dumps(_payload(), ensure_ascii=False),
        max_retries=0,
    ).structure(_input())

    assert plan["schema_version"] == "3.0"
    assert plan["target_user"]["gender"] == "female"
    assert plan["semantic_evidence"][0]["text"] == "여성"


def test_v3_rejects_evidence_not_present_in_source() -> None:
    payload = _payload()
    payload["semantic_evidence"][0].update(text="남성", start=3, end=5)
    with pytest.raises(ValueError, match="original_query"):
        validate_campaign_query_plan_v3(
            attach_campaign_query_plan_v3_identity(payload, "서울 여성 회원"),
            query="서울 여성 회원",
        )


def test_auto_calls_semantic_factory_before_legacy_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    original = graph_rag._build_rule_query_plan

    def observed_rules(*args, **kwargs):
        events.append("rules")
        return original(*args, **kwargs)

    def unavailable_factory(_legacy_plan: dict):
        events.append("llm")
        raise RuntimeError("offline")

    monkeypatch.setattr(graph_rag, "_build_rule_query_plan", observed_rules)
    plan = graph_rag.build_query_plan(
        "서울 여성 회원",
        parser="auto",
        query_plan_v2_factory=unavailable_factory,
        precomputed_scopes={"mode": "rules", "targeting": "서울 여성 회원", "channel": ""},
    )

    assert events[:2] == ["llm", "rules"]
    assert plan["parser"]["fallback_used"] is True
    assert plan["parser"]["authority"] == "llm_first"


def test_llm_first_candidate_has_conflict_authority() -> None:
    query = "여성 회원"
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(_payload(query), query), query=query
    )
    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": query, "channel": ""},
    )

    priorities = {
        item["source"]: item["priority"]
        for item in plan["plan_resolution"]["candidates"]
    }
    assert priorities["llm_query_structurer"] > priorities["rules"]
    assert plan["target_user"]["gender"] == "female"
    assert copy.deepcopy(plan["semantic_evidence"])[0]["text"] == "여성"


def test_legacy_source_validation_does_not_mutate_v3_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {"gender": "female"},
        "exclude": {},
        "campaign_constraints": {},
        "parser": {"authority": "llm_first"},
    }

    def disagree(_query: str, candidate: dict, **_kwargs) -> None:
        candidate["target_user"]["gender"] = "male"

    monkeypatch.setattr(graph_rag, "_run_source_authoritative_stages", disagree)
    result = graph_rag._validate_source_authoritative_stages(
        "여성 회원",
        plan,
        sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
        normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
    )

    assert plan["target_user"]["gender"] == "female"
    assert result["is_satisfied"] is False
    assert plan["unresolved_source_conditions"][0]["source"] == "legacy_source_validator"


def test_shadow_authority_keeps_legacy_conflict_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "여성 회원"
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(_payload(query), query), query=query
    )
    monkeypatch.setenv("QUERY_PLAN_AUTHORITY", "shadow")

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2_factory=lambda _legacy: semantic_plan,
        precomputed_scopes={"mode": "shadow", "targeting": query, "channel": ""},
    )

    priorities = {
        item["source"]: item["priority"]
        for item in plan["plan_resolution"]["candidates"]
    }
    assert priorities["rules"] > priorities["llm_query_structurer"]
    assert plan["parser"]["authority"] == "shadow"
