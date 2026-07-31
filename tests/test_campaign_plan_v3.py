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
        "semantic_ir": {
            "status": "resolved",
            "operations": [],
            "missing_fields": [],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": None,
        },
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


def test_llm_first_rules_fallback_closes_dependencies_after_candidate_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "2019년 상반기에 두부랑 음료수 산 사람들 찾아줘"

    monkeypatch.setattr(
        graph_rag,
        "_build_llm_object_candidate",
        lambda *_args, **_kwargs: {
            "target_user": {
                "purchase_object": "두부",
                "purchase_objects": [
                    {"value": "두부", "kind": None},
                    {"value": "음료수", "kind": None},
                ],
            },
            "campaign_constraints": {},
        },
    )
    monkeypatch.setattr(
        graph_rag,
        "_apply_product_master_resolution",
        lambda _query, _plan: None,
    )

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2_factory=lambda _legacy: {"intent": "unknown"},
        precomputed_scopes={"mode": "rules", "targeting": query, "channel": ""},
    )

    assert plan["parser"]["type"] == "rules"
    assert plan["parser"]["fallback_used"] is True
    assert plan["parser"]["authority"] == "llm_first"
    assert plan["target_user"]["purchase_date"] == {
        "from": "20190101",
        "to": "20190630",
        "label": "2019년 상반기 구매",
    }
    date_claim = next(
        claim
        for claim in plan["condition_claims"]
        if claim["semantic_key"].startswith("legacy:purchase_date:")
    )
    assert date_claim["source_spans"] == [{"start": 0, "end": 9}]
    assert plan.get("unresolved_source_conditions") in (None, [])
    candidate = graph_rag.build_purchase_history_targets_sql_candidate(plan)
    assert candidate is not None
    assert "20190101" in candidate["sql"]
    assert "20190630" in candidate["sql"]
    assert "두부" in candidate["sql"]
    assert "음료수" in candidate["sql"]


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


def test_llm_first_runtime_skips_legacy_source_ir_validation_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {"purchase_membership": {"domain": "purchase", "operator": "exists"}},
        "exclude": {},
        "campaign_constraints": {},
        "parser": {"authority": "llm_first"},
    }
    monkeypatch.delenv("SOURCE_AUTHORITATIVE_IR_VALIDATION", raising=False)
    monkeypatch.setattr(
        graph_rag,
        "_validate_source_authoritative_stages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy IR validation must be skipped")
        ),
    )

    graph_rag._apply_source_authoritative_stages(
        "2026년 3월에 같은 상품을 동시 구매한 고객수",
        plan,
        sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
        normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
    )

    assert plan["parser"]["source_validation"] == {
        "ran": False,
        "is_satisfied": True,
        "skipped": True,
        "reason": "disabled_by_configuration",
        "divergent_slots": [],
    }
    assert plan.get("unresolved_source_conditions") in (None, [])


def test_llm_first_rules_fallback_runs_source_authoritative_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "parser": {
            "authority": "llm_first",
            "type": "rules",
            "fallback_used": True,
        },
    }
    calls: list[str] = []
    monkeypatch.delenv("SOURCE_AUTHORITATIVE_IR_VALIDATION", raising=False)
    monkeypatch.setattr(
        graph_rag,
        "_run_source_authoritative_stages",
        lambda query, *_args, **_kwargs: calls.append(query),
    )

    graph_rag._apply_source_authoritative_stages(
        "2019년 상반기에 두부랑 음료수 산 사람들 찾아줘",
        plan,
        sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
        normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
    )

    assert calls == ["2019년 상반기에 두부랑 음료수 산 사람들 찾아줘"]
    assert "source_validation" not in plan["parser"]


def test_llm_first_runtime_can_reenable_legacy_source_ir_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "parser": {"authority": "llm_first"},
    }
    calls: list[str] = []
    monkeypatch.setenv("SOURCE_AUTHORITATIVE_IR_VALIDATION", "on")
    monkeypatch.setattr(
        graph_rag,
        "_validate_source_authoritative_stages",
        lambda query, *_args, **_kwargs: calls.append(query),
    )

    graph_rag._apply_source_authoritative_stages(
        "여성 회원",
        plan,
        sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
        normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
    )

    assert calls == ["여성 회원"]


def test_entity_set_source_validation_does_not_block_compilable_top_products() -> None:
    """원문 재검증이 되살린 일반 구매조건은 Top-N 집합 소유권에 다시 흡수돼야 한다."""
    planning_query = "2026년 3월 구매에서 가장 많이 팔린 상품 5개를 구매한 고객"
    source_query = planning_query + " 리스트"

    def unavailable_factory(_legacy_plan: dict) -> dict:
        raise RuntimeError("offline")

    plan = graph_rag.build_query_plan(
        planning_query,
        parser="auto",
        original_query=source_query,
        query_plan_v2_factory=unavailable_factory,
        precomputed_scopes={"mode": "rules", "targeting": planning_query, "channel": ""},
    )
    assert plan["parser"]["fallback_used"] is True
    assert plan["parser"]["authority"] == "llm_first"

    result = graph_rag._validate_source_authoritative_stages(
        source_query,
        plan,
        sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
        normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
    )

    assert result["is_satisfied"] is True
    assert result["divergent_slots"] == []
    assert plan.get("unresolved_source_conditions") in (None, [])
    assert plan.get("semantic_conditions") == []
    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)
    assert candidate is not None
    assert candidate["id"] == "sql_template:entity_set_targets"
    assert "SELECT TOP 5 D.PRODUCT_ID" in candidate["sql"]
    assert "D.ORDER_DATE BETWEEN '20260301' AND '20260331'" in candidate["sql"]
    assert "ORDER BY SUM(D.ORDER_QTY) DESC" in candidate["sql"]
    assert candidate["dropped_conditions"] == []


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
