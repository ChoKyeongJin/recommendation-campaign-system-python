from __future__ import annotations

from datetime import date

import networkx as nx

import graph_rag
from calendar_window import parse_calendar_window
from entity_set import compile_entity_set_predicate, parse_entity_set_condition
from query_structurer.campaign_plan_v2 import (
    attach_campaign_query_plan_v2_identity,
    validate_campaign_query_plan_v2,
)


QUERY = "이번달에 제일 잘팔린 상품 3개 중에서 2개만 구매한 사람 뽑아줘"


def test_relative_calendar_month_is_resolved_from_the_request_date() -> None:
    assert parse_calendar_window("이번달", today=date(2026, 7, 31)) == {
        "from": "20260701",
        "to": "20260731",
        "label": "2026년 7월",
    }
    assert parse_calendar_window("지난달", today=date(2026, 1, 3)) == {
        "from": "20251201",
        "to": "20251231",
        "label": "2025년 12월",
    }


def test_ranked_set_cardinality_is_one_closed_semantic_node() -> None:
    config = graph_rag._entity_set_config()
    node = parse_entity_set_condition(QUERY, config, today=date(2026, 7, 31))

    assert node is not None
    assert node["limit"] == 3
    assert node["window"] == {
        "from": "20260701",
        "to": "20260731",
        "label": "2026년 7월",
    }
    assert node["cardinality"] == {"operator": "=", "value": 2}
    assert node["derived_set_ast"]["cardinality"] == {"operator": "=", "value": 2}

    predicate = compile_entity_set_predicate(node, config)
    assert predicate is not None
    assert "SELECT TOP 3 D.PRODUCT_ID" in predicate
    assert "D.ORDER_DATE BETWEEN '20260701' AND '20260731'" in predicate
    assert "COUNT(DISTINCT OD.PRODUCT_ID)" in predicate
    assert predicate.endswith(") = 2")


def test_full_plan_does_not_turn_entity_cardinality_into_result_limit_or_literal_product() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    node = plan["target_user"]["entity_set_condition"]

    assert node["cardinality"] == {"operator": "=", "value": 2}
    assert plan["result_limit"] is None
    assert plan.get("event_expression") is None
    assert plan.get("event_compiler_capability") is None
    assert plan.get("unsupported") is None
    assert plan.get("unresolved_source_conditions") == []
    assert plan["target_user"].get("purchase_object") is None

    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)
    assert candidate is not None
    assert "COUNT(DISTINCT OD.PRODUCT_ID)" in candidate["sql"]
    assert ") = 2" in candidate["sql"]

    sql_result = graph_rag.build_sql_result(
        nx.Graph(),
        QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=QUERY,
    )
    assert sql_result["is_success"] is True
    assert "COUNT(DISTINCT OD.PRODUCT_ID)" in sql_result["sql"]
    assert ") = 2" in sql_result["sql"]


def test_cardinality_operator_is_compositional_not_phrase_specific() -> None:
    config = graph_rag._entity_set_config()
    cases = {
        "이번달 판매량 상위 상품 5개 중 3개 이상 구매한 고객": (">=", 3),
        "지난달 많이 팔린 제품 10개 가운데 4개 이하 구입한 회원": ("<=", 4),
        "2025년 베스트 상품 7개 중에서 2개 초과 주문한 구매자": (">", 2),
    }

    for query, expected in cases.items():
        node = parse_entity_set_condition(query, config, today=date(2026, 7, 31))
        assert node is not None, query
        assert (node["cardinality"]["operator"], node["cardinality"]["value"]) == expected


def test_compiled_entity_set_clears_stale_conceptual_unsupported_evidence() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    plan["unresolved_source_conditions"] = [
        {
            "path": "source_coverage.conceptual_targeting",
            "label": "이번달",
            "source_text": "이번달",
            "reason": "영문 모델 사유",
            "status": "unresolved",
            "source": "conceptual_targeting",
        },
        {
            "path": "source_coverage.conceptual_targeting",
            "label": "제일 잘팔린 상품 3개",
            "source_text": "제일 잘팔린 상품 3개",
            "reason": "영문 모델 사유",
            "status": "unresolved",
            "source": "conceptual_targeting",
        },
        {
            "path": "source_coverage.conceptual_targeting",
            "label": "2개만 구매한 사람",
            "source_text": "2개만 구매한 사람",
            "reason": "영문 모델 사유",
            "status": "unresolved",
            "source": "conceptual_targeting",
        },
    ]

    assert graph_rag._refresh_unresolved_source_conditions(QUERY, plan) == []


def test_source_authority_repairs_a_lossy_llm_plan_without_phrase_specific_fallback() -> None:
    lossy_payload = {
        "intent": "find_user_segment",
        "target_user": {
            "purchase_inactivity": {
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
            },
            "aggregate_conditions": [{
                "metric_id": "product_purchase_count",
                "operator": ">=",
                "threshold": 2,
                "aggregation_scope": "per_member",
                "scope": {
                    "category": "best_selling_products",
                    "time_period": "current_month",
                },
            }],
        },
        "exclude": {},
        "campaign_constraints": {},
    }
    semantic_plan = validate_campaign_query_plan_v2(
        attach_campaign_query_plan_v2_identity(lossy_payload, QUERY),
        query=QUERY,
    )

    plan = graph_rag.build_query_plan(
        QUERY,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": QUERY, "channel": ""},
    )

    node = plan["target_user"]["entity_set_condition"]
    assert node["limit"] == 3
    assert node["cardinality"] == {"operator": "=", "value": 2}
    assert plan["result_limit"] is None
    assert plan.get("event_expression") is None
    assert plan.get("unsupported") is None
    assert plan["unresolved_source_conditions"] == []
