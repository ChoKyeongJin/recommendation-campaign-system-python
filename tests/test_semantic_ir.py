from __future__ import annotations

import copy

import networkx as nx
import pytest

import graph_rag
from query_structurer.campaign_plan_v3 import (
    attach_campaign_query_plan_v3_identity,
    validate_campaign_query_plan_v3,
)
from query_structurer.semantic_ir import (
    extract_literal_bindings,
    materialize_semantic_operations,
)


QUERY = "2026년 2월과 3월의 구매 금액 차이가 10% 이상 증가한 고객 리스트"


def _semantic_ir(status: str = "resolved") -> dict:
    return {
        "status": status,
        "operations": [
            {
                "kind": "period_over_period_change",
                "metric_id": "purchase_amount",
                "direction": "increase",
                "bindings": [
                    {"role": "baseline", "literal_id": "date_window_1"},
                    {"role": "current", "literal_id": "date_window_2"},
                    {"role": "threshold", "literal_id": "percentage_1"},
                    {"role": "comparison", "literal_id": "comparison_operator_1"},
                ],
            }
        ],
        "missing_fields": [],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }


def _payload(semantic_ir: dict) -> dict:
    return {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {"objective": "purchase"},
        "aggregation_request": None,
        "set_expressions": [],
        "computed_metrics": [],
        "result_limit": None,
        "semantic_evidence": [
            {
                "path": "semantic_ir.operations[0]",
                "text": QUERY,
                "start": 0,
                "end": len(QUERY),
                "confidence": 0.99,
            }
        ],
        "semantic_ir": semantic_ir,
        "unresolved": [],
    }


def test_literal_extractor_owns_dates_percentage_and_operator() -> None:
    literals = extract_literal_bindings(QUERY, current_date="2026-07-29")

    assert [(item["id"], item["normalized"]) for item in literals] == [
        (
            "date_window_1",
            {"from": "20260201", "to": "20260228", "label": "2026년 2월"},
        ),
        (
            "date_window_2",
            {"from": "20260301", "to": "20260331", "label": "2026년 3월"},
        ),
        ("percentage_1", {"value": 10, "unit": "percent"}),
        ("comparison_operator_1", ">="),
    ]


def test_semantic_ir_materializes_metric_trend_without_model_values() -> None:
    literals = extract_literal_bindings(QUERY, current_date="2026-07-29")
    slots = materialize_semantic_operations(_semantic_ir(), literals)

    assert slots["metric_trend"]["baseline"]["from"] == "20260201"
    assert slots["metric_trend"]["current"]["to"] == "20260331"
    assert slots["metric_trend"]["relative_change"] == {
        "unit": "percent",
        "comparisons": [{"operator": ">=", "value": 10}],
    }


def test_resolved_v3_ir_projects_into_existing_sql_compiler() -> None:
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(
            _payload(_semantic_ir()), QUERY, current_date="2026-07-29"
        ),
        query=QUERY,
    )
    plan = graph_rag.build_query_plan(
        QUERY,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": QUERY, "channel": ""},
    )

    assert plan["parser"]["authority"] == "llm_first"
    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["target_user"]["metric_trend"]["baseline"]["from"] == "20260201"
    sql = graph_rag.build_metric_trend_targets_sql_candidate(plan)["sql"]
    assert "ORDER_DATE BETWEEN '20260201' AND '20260228'" in sql
    assert "NULLIF(M2.TREND_VALUE, 0)) >= 10" in sql


def test_unique_evidence_text_repairs_model_offset() -> None:
    payload = _payload(_semantic_ir())
    payload["semantic_evidence"] = [
        {
            "path": "semantic_ir.operations[0].metric_id",
            "text": "구매 금액",
            "start": 13,
            "end": 18,
            "confidence": 0.9,
        }
    ]
    attached = attach_campaign_query_plan_v3_identity(
        payload, QUERY, current_date="2026-07-29"
    )

    plan = validate_campaign_query_plan_v3(attached, query=QUERY)

    assert QUERY[plan["semantic_evidence"][0]["start"]:plan["semantic_evidence"][0]["end"]] == "구매 금액"


def test_v3_rejects_model_literal_not_extracted_from_source() -> None:
    semantic_ir = _semantic_ir()
    semantic_ir["operations"][0]["bindings"][2]["literal_id"] = "percentage_99"
    payload = attach_campaign_query_plan_v3_identity(
        _payload(semantic_ir), QUERY, current_date="2026-07-29"
    )

    with pytest.raises(ValueError, match="unknown literal"):
        validate_campaign_query_plan_v3(payload, query=QUERY)


def test_two_unspecified_months_require_clarification_and_never_compile() -> None:
    query = "두 월의 구매 금액을 비교한 고객 리스트"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["comparison_month_1", "comparison_month_2"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "비교할 두 월을 지정해 주세요.",
    }
    payload = attach_campaign_query_plan_v3_identity(
        _payload(semantic_ir), query, current_date="2026-07-29"
    )
    # Replace evidence because _payload uses the resolved example source.
    payload["semantic_evidence"] = []
    plan = validate_campaign_query_plan_v3(payload, query=query)

    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert result is not None
    assert result["sql"] is None
    assert result["interpretation_status"] == "needs_clarification"
    assert result["clarification_questions"] == ["비교할 두 월을 지정해 주세요."]


def test_entity_ranking_window_resolves_stale_purchase_date_missing_field() -> None:
    query = "2019년 5월에 가장 잘 팔린 제품 5개를 산 고객 추출해줘"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["purchase_date"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }
    payload = _payload(semantic_ir)
    payload["target_user"] = {
        "entity_set_condition": {
            "derived_set_ast": {
                "type": "member_set",
                "relation": "top",
                "exists": True,
                "source": {
                    "type": "ranking",
                    "direction": "top",
                    "limit": 5,
                    "source": {
                        "type": "aggregation",
                        "relation": "purchase",
                        "group_by": "product_id",
                        "measure": "count",
                        "window": {},
                        "filters": [
                            {
                                "type": "dimension_filter",
                                "dimension": "purchase_date",
                                "operator": "contains",
                                "value": "2019년 5월",
                            }
                        ],
                    },
                },
            }
        }
    }
    payload["semantic_evidence"] = []
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(
            payload, query, current_date="2026-07-29"
        ),
        query=query,
    )

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": query, "channel": ""},
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert plan["semantic_ir_reconciliation"]["resolved_fields"] == [
        {
            "field": "purchase_date",
            "resolved_by": "target_user.entity_set_condition.window",
            "reason": "ranking_window_owns_purchase_date",
        }
    ]
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is True
    assert "SELECT TOP 5 D.PRODUCT_ID" in result["sql"]
    assert "D.ORDER_DATE BETWEEN '20190501' AND '20190531'" in result["sql"]


def test_unowned_purchase_date_missing_field_still_blocks() -> None:
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["purchase_date"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "구매 기간을 지정해 주세요.",
    }
    plan = {"semantic_ir": semantic_ir, "target_user": {}}

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)
    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert result is not None
    assert result["clarification_questions"] == ["구매 기간을 지정해 주세요."]


def test_unsupported_operation_is_distinct_from_clarification() -> None:
    semantic_ir = {
        "status": "unsupported",
        "operations": [],
        "missing_fields": [],
        "policy_applications": [],
        "unsupported_operations": [
            {
                "kind": "forecast_purchase_growth",
                "reason": "forecast_not_supported",
                "evidence": "예측",
            }
        ],
        "message": "미래 구매금액 예측은 지원하지 않습니다.",
    }
    result = graph_rag._semantic_ir_blocking_sql_result(
        {"semantic_ir": copy.deepcopy(semantic_ir)}
    )

    assert result is not None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"
