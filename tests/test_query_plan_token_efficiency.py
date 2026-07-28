from __future__ import annotations

import json

import graph_rag
from query_structurer.campaign_plan_v2 import (
    CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA,
    CAMPAIGN_QUERY_PLAN_V2_TOOL,
)
from query_structurer.prompt import build_campaign_query_plan_v2_user_prompt
from query_structurer.structurer import LLMCampaignQueryPlanStructurer
from query_structurer.types import QueryStructuringInput, StructuringContext


def _input(query: str = "20대 여성 고객") -> QueryStructuringInput:
    return QueryStructuringInput(
        query=query,
        context=StructuringContext(current_date="2026-07-29", timezone="Asia/Seoul"),
    )


def test_campaign_structuring_prompt_does_not_duplicate_tool_schema() -> None:
    prompt = build_campaign_query_plan_v2_user_prompt(_input())

    assert "[Campaign QueryPlan v2 JSON Schema]" not in prompt
    assert json.dumps(CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA, ensure_ascii=False, indent=2) not in prompt
    assert CAMPAIGN_QUERY_PLAN_V2_TOOL["function"]["parameters"]


def test_campaign_structurer_injects_application_owned_identity() -> None:
    response = json.dumps(
        {
            "intent": "find_user_segment",
            "target_user": {"gender": "female"},
            "exclude": {},
            "campaign_constraints": {},
        },
        ensure_ascii=False,
    )
    plan = LLMCampaignQueryPlanStructurer(lambda _messages: response, max_retries=0).structure(_input())

    assert plan["schema_version"] == "2.1"
    assert plan["raw_query"] == "20대 여성 고객"
    assert plan["original_query"] == "20대 여성 고객"
    assert plan["planning_query"] == "20대 여성 고객"
    assert plan["normalized_query"] == "20대 여성 고객"
    tool_properties = CAMPAIGN_QUERY_PLAN_V2_TOOL["function"]["parameters"]["properties"]
    assert "raw_query" not in tool_properties
    assert "source_requirements" not in tool_properties


def test_rules_and_complete_auto_plans_skip_v2_factory() -> None:
    calls: list[str] = []

    def factory(_plan: dict) -> graph_rag.CampaignQueryPlanV2:
        calls.append("called")
        raise AssertionError("complete deterministic plans must not call the v2 structurer")

    scopes = {"mode": "rules", "targeting": "20대 여성 고객", "channel": ""}
    rules_plan = graph_rag.build_query_plan(
        "20대 여성 고객",
        parser="rules",
        query_plan_v2_factory=factory,
        precomputed_scopes=scopes,
    )
    auto_plan = graph_rag.build_query_plan(
        "20대 여성 고객",
        parser="auto",
        query_plan_v2_factory=factory,
        precomputed_scopes=scopes,
    )

    assert calls == []
    assert rules_plan["parser"]["type"] == "rules"
    assert auto_plan["parser"]["skip_reason"] == "deterministic_plan_complete"

    aggregate_query = "2019년 3월에 같은 상품을 동시 구매한 고객수"
    aggregate_plan = graph_rag.build_query_plan(
        aggregate_query,
        parser="auto",
        query_plan_v2_factory=factory,
        precomputed_scopes={"mode": "llm", "targeting": aggregate_query, "channel": ""},
    )
    assert calls == []
    assert aggregate_plan["parser"]["skip_reason"] == "deterministic_plan_complete"


def test_precomputed_scopes_avoid_second_split(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_rag,
        "split_prompt_scopes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected second split")),
    )
    scopes = {"mode": "llm", "targeting": "20대 여성 고객", "channel": ""}

    plan = graph_rag.build_query_plan(
        "20대 여성 고객",
        parser="rules",
        precomputed_scopes=scopes,
    )

    assert plan["retrieval"]["targeting_query"] == "20대 여성 고객"


def test_generation_prompts_exclude_internal_audit_fields() -> None:
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"gender": "female", "marker": "TARGET_MARKER"},
        "decisions": [{"reason": "AUDIT_SECRET"}],
        "plan_resolution": {"conflicts": ["RESOLVER_SECRET"]},
        "source_requirements_digest": "DIGEST_SECRET",
        "_slot_spans": {"target_user.gender": [0, 2]},
    }
    sql_result = {"is_success": True, "safe_sql": "SELECT 1"}
    answer = graph_rag.render_answer_prompt(
        "여성 고객",
        query_plan,
        {"prompt": "context"},
        sql_result,
    )
    message = graph_rag.render_message_prompt(
        "여성 고객",
        query_plan,
        sql_result,
        {
            "channel": "lms",
            "channel_policy": {},
            "selected_channel_policy": {},
            "campaigns": [],
            "target_context": {},
            "message_examples": [],
        },
    )

    for prompt in (answer, message):
        assert "TARGET_MARKER" in prompt
        assert "AUDIT_SECRET" not in prompt
        assert "RESOLVER_SECRET" not in prompt
        assert "DIGEST_SECRET" not in prompt


def test_non_aggregation_planner_prompt_omits_aggregation_schema() -> None:
    prompt = graph_rag._query_plan_user_prompt(
        "20대 여성 고객",
        {"intent": "find_user_segment", "target_user": {"gender": "female"}},
    )

    assert "[Aggregation Schema Metadata]" not in prompt


def test_aggregation_schema_context_is_bounded() -> None:
    plan = graph_rag._build_rule_query_plan("2019년 3월 상품별 구매 고객 수")
    context = graph_rag._aggregation_schema_prompt_context(
        "2019년 3월 상품별 구매 고객 수",
        graph_rag.DEFAULT_SCHEMA_PATH,
        query_plan=plan,
    )

    assert len(context) <= 6
    assert all(len(table.get("columns", [])) <= 20 for table in context)


def test_aggregation_planner_prompt_includes_bounded_schema() -> None:
    query = "구매금액 합계 알려줘"
    plan = graph_rag._build_rule_query_plan(query)

    prompt = graph_rag._query_plan_user_prompt(query, plan)

    assert "[Aggregation Schema Metadata]" in prompt
