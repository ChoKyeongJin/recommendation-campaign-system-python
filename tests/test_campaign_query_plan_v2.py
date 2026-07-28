import json

import pytest

import graph_rag as g
from entity_set import build_derived_set_ast
from query_structurer import (
    CAMPAIGN_QUERY_PLAN_VERSION,
    CampaignQueryPlanV2,
    CampaignQueryPlanValidationError,
    LLMCampaignQueryPlanStructurer,
    QueryPlannerInput,
    QueryStructuringInput,
    StructuringContext,
    build_campaign_query_plan_v2_fallback,
    call_query_planner,
    validate_campaign_query_plan_v2,
)


def test_rules_planner_returns_the_versioned_shared_ir():
    query = "20대 여성 회원에게 재구매 캠페인을 만들어줘"

    plan = g.build_query_plan(query, parser="rules")

    assert isinstance(plan, CampaignQueryPlanV2)
    assert plan["schema_version"] == CAMPAIGN_QUERY_PLAN_VERSION
    assert plan["original_query"] == query
    assert plan["normalized_query"]
    assert "structured_query" not in plan
    assert plan["target_user"]["gender"] == "female"


def test_adapter_passes_campaign_v2_without_a_front_ir_conversion():
    plan = build_campaign_query_plan_v2_fallback("여성 회원을 추출해줘")
    received = []

    def create_plan(query, *, query_plan_v2=None):
        received.append(query_plan_v2)
        return query_plan_v2

    result = call_query_planner(
        create_plan,
        QueryPlannerInput(query=plan.original_query, query_plan=plan),
    )

    assert result is plan
    assert received == [plan]


def test_campaign_v2_rejects_wrong_version_and_query_identity():
    plan = build_campaign_query_plan_v2_fallback("VIP 회원")
    plan["schema_version"] = "1.0"
    with pytest.raises(CampaignQueryPlanValidationError, match="schema_version"):
        validate_campaign_query_plan_v2(plan, query="VIP 회원")

    plan["schema_version"] = CAMPAIGN_QUERY_PLAN_VERSION
    with pytest.raises(CampaignQueryPlanValidationError, match="original_query"):
        validate_campaign_query_plan_v2(plan, query="다른 질문")


def test_campaign_v2_rejects_malformed_derived_set_scope_filter():
    query = "인기 상품 구매 고객"
    plan = build_campaign_query_plan_v2_fallback(query)
    ast = build_derived_set_ast(
        member_relation="purchase",
        rank_relation="purchase",
        entity="product",
        measure="sales_quantity",
        direction="top",
        limit=5,
        filters=[{
            "type": "dimension_filter",
            "dimension": "category",
            "operator": "raw_sql",
            "value": "어린이건강",
        }],
    )
    plan["target_user"]["entity_set_condition"] = {"derived_set_ast": ast}

    with pytest.raises(CampaignQueryPlanValidationError, match="invalid_derived_set_filter_operator"):
        validate_campaign_query_plan_v2(plan, query=query)


def test_campaign_structurer_retries_and_returns_campaign_v2():
    query = "최근 30일 구매 회원에게 캠페인을 만들어줘"
    payload = build_campaign_query_plan_v2_fallback(query).to_dict()
    payload["intent"] = "recommend_campaign"
    responses = iter(["not json", json.dumps(payload, ensure_ascii=False)])
    events = []

    result = LLMCampaignQueryPlanStructurer(
        lambda _messages: next(responses),
        on_event=lambda event, data: events.append((event, data)),
    ).structure(
        QueryStructuringInput(
            query=query,
            context=StructuringContext(current_date="2026-07-28", timezone="Asia/Seoul"),
        )
    )

    assert isinstance(result, CampaignQueryPlanV2)
    assert result["intent"] == "recommend_campaign"
    assert [event for event, _ in events] == [
        "campaign_query_plan_v2_attempt_failed",
        "campaign_query_plan_v2_success",
    ]
