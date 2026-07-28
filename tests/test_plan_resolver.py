"""rules/LLM 후보와 단일 플랜 resolver의 경계 계약."""

from __future__ import annotations

import copy

import graph_rag as g
import plan_resolver
from query_structurer import build_campaign_query_plan_v2_fallback


def test_resolver_owns_scalar_conflicts_and_does_not_mutate_candidates() -> None:
    rules = {"intent": "find_user_segment", "target_user": {"gender": "female", "lifecycle": ["vip"]}}
    llm = {"intent": "find_user_segment", "target_user": {"gender": "male", "lifecycle": ["dormant"]}}
    before = copy.deepcopy((rules, llm))

    plan = plan_resolver.resolve_plan_candidates([
        plan_resolver.PlanCandidate("rules", rules, priority=300),
        plan_resolver.PlanCandidate("llm_query_structurer", llm, priority=100),
    ])

    assert (rules, llm) == before
    assert plan["target_user"]["gender"] == "female"
    assert plan["target_user"]["lifecycle"] == ["vip", "dormant"]
    conflict = next(item for item in plan["plan_resolution"]["conflicts"] if item["path"] == "target_user.gender")
    assert conflict["winner"] == "rules"
    assert conflict["rejected"] == "llm_query_structurer"


def test_concrete_lower_priority_intent_can_fill_unknown() -> None:
    plan = plan_resolver.resolve_plan_candidates([
        plan_resolver.PlanCandidate("rules", {"intent": "unknown"}, priority=300),
        plan_resolver.PlanCandidate("llm_query_structurer", {"intent": "find_user_segment"}, priority=100),
    ])

    assert plan["intent"] == "find_user_segment"
    assert plan["plan_resolution"]["conflicts"] == []


def test_build_query_plan_routes_rules_and_llm_through_the_resolver() -> None:
    query = "여성 VIP 회원을 추출해줘"
    llm_plan = build_campaign_query_plan_v2_fallback(query)
    llm_plan["intent"] = "find_user_segment"
    llm_plan["target_user"] = {"gender": "male", "lifecycle": ["dormant"]}

    plan = g.build_query_plan(query, parser="rules", query_plan_v2=llm_plan)

    assert plan["target_user"]["gender"] == "female"
    assert [item["source"] for item in plan["plan_resolution"]["candidates"]] == [
        "rules",
        "llm_query_structurer",
    ]
    assert any(item["path"] == "target_user.gender" for item in plan["plan_resolution"]["conflicts"])
