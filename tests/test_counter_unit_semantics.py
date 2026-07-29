from __future__ import annotations

import json
from types import SimpleNamespace

import networkx as nx
import pytest

import graph_rag
import targeting_ir
from query_structurer.campaign_plan_v3 import CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA
from query_structurer.semantic_ir import extract_literal_bindings


QUERY = "2019년 5월에 같은 브랜드에서 2개 이상 구매한 사람중 여자만 구해줘"


def test_counter_literals_preserve_business_meaning() -> None:
    query = "2개 이상, 3회 이상, 4종 이상 구매"
    counters = [
        item for item in extract_literal_bindings(query)
        if item["kind"] == "number_with_unit"
    ]

    assert [item["normalized"] for item in counters] == [
        {"value": 2, "surface_unit": "개", "semantic_unit": "item_quantity"},
        {"value": 3, "surface_unit": "회", "semantic_unit": "order_count"},
        {"value": 4, "surface_unit": "종", "semantic_unit": "distinct_product_count"},
    ]


def test_rewrite_guard_rejects_counter_semantic_drift() -> None:
    dropped = graph_rag._rewrite_dropped_signals(
        "2개 이상 구매한 고객",
        "2회 이상 구매한 고객",
    )

    assert "수량 단위 '2 상품 수량'" in dropped


def test_rewrite_guard_rejects_gender_polarity_drift() -> None:
    dropped = graph_rag._rewrite_dropped_signals(
        "여자만 빼줘",
        "여성 고객",
    )

    assert "성별 극성 '여성 제외'" in dropped


def test_scope_guard_rejects_dropped_gender_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert graph_rag._gender_polarity_signals("여성 제외") == {"female:exclude"}
    assert (
        graph_rag._gender_polarity_signals("여성 제외")
        - graph_rag._gender_polarity_signals("여성 고객")
    ) == {"female:exclude"}

    import openai

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai, "OpenAI", lambda: object())
    monkeypatch.setattr(
        graph_rag,
        "_openai_chat_create",
        lambda *_args, **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"targeting": "여성 고객", "channel": ""},
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        ),
    )

    assert graph_rag._llm_split_prompt_scopes(
        "여성 제외", "auto", "test-model", None
    ) is None


def test_same_brand_quantity_is_per_brand_item_sum() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")

    assert plan["target_user"]["gender"] == "female"
    assert plan["target_user"]["purchase_date"] == {
        "from": "20190501",
        "to": "20190531",
        "label": "2019년 5월 구매",
    }
    assert plan["target_user"]["aggregate_conditions"] == [
        {
            "metric_id": "total_item_quantity",
            "operator": ">=",
            "threshold": 2.0,
            "window_days": None,
            "label": "상품 수량",
            "aggregation_scope": "per_brand",
        }
    ]


@pytest.mark.parametrize(
    ("counter", "metric_id"),
    [
        ("2개", "total_item_quantity"),
        ("2회", "order_count"),
        ("2건", "order_count"),
        ("2종", "distinct_product_count"),
    ],
)
def test_counter_units_route_to_distinct_metrics(counter: str, metric_id: str) -> None:
    query = f"같은 브랜드에서 {counter} 이상 구매한 고객"
    condition = graph_rag.build_query_plan(query, parser="rules")["target_user"][
        "aggregate_conditions"
    ][0]

    assert condition["metric_id"] == metric_id
    assert condition["aggregation_scope"] == "per_brand"


def test_same_brand_quantity_compiles_to_member_brand_having() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=QUERY,
    )

    assert result["is_success"] is True
    sql = result["sql"]
    assert "FROM CRM_SL_ORDERDETAILMALL D" in sql
    assert "D.ORDER_DATE BETWEEN '20190501' AND '20190531'" in sql
    assert "GROUP BY D.MEMBER_NO, D.BRAND_ID" in sql
    assert "HAVING SUM(D.ORDER_QTY) >= 2" in sql
    assert "같은" not in sql


def test_auto_plan_reconciles_exclude_only_gender_before_sql() -> None:
    query = "2019년 상반기에 같은 브랜드에서 2개 이상 구매한 사람중 여자만 빼줘"
    semantic_plan = {
        "schema_version": "3.0",
        "original_query": query,
        "intent": "find_user_segment",
        # 실제 장애 로그처럼 LLM이 같은 값을 포함/제외 양쪽에 제출한 후보.
        "target_user": {"gender": "female"},
        "exclude": {"gender": ["female"]},
        "campaign_constraints": {},
        "semantic_evidence": [],
        "unresolved": [],
        "semantic_ir": {
            "status": "resolved",
            "operations": [],
            "missing_fields": [],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": None,
        },
    }
    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        # 실제 장애처럼 스코프 분리 결과가 제외 극성을 잃은 경우도 원문 권위 후처리가 복원해야 한다.
        precomputed_scopes={
            "mode": "llm",
            "targeting": "여성, 2019년 상반기에 같은 브랜드에서 2개 이상 구매한 고객",
            "channel": "",
        },
    )

    assert plan["target_user"]["gender"] is None
    assert plan["exclude"]["gender"] == ["female"]
    assert graph_rag._verify_plan_semantic_conflicts(plan) == []

    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    assert result["is_success"] is True
    assert "B.GENDER_CD <> 'GENDER_CD.FEMALE'" in result["sql"]


def test_llm_aggregate_condition_coercion_keeps_per_brand_scope() -> None:
    coerced = targeting_ir.SLOT_SHAPES["aggregate_conditions"].coerce(
        [
            {
                "metric_id": "total_item_quantity",
                "operator": ">=",
                "threshold": 2,
                "window_days": None,
                "window": None,
                "aggregation_scope": "per_brand",
                "scope": None,
                "label": None,
            }
        ],
        allowed={"total_item_quantity"},
    )

    assert coerced == [
        {
            "metric_id": "total_item_quantity",
            "operator": ">=",
            "threshold": 2,
            "window_days": None,
            "aggregation_scope": "per_brand",
        }
    ]
    aggregate_schema = (
        CAMPAIGN_QUERY_PLAN_V3_LLM_JSON_SCHEMA["properties"]["target_user"]
        ["properties"]["aggregate_conditions"]
    )
    aggregate_items = next(
        branch["items"] for branch in aggregate_schema["anyOf"]
        if branch.get("type") == "array"
    )
    assert "aggregation_scope" in aggregate_items["properties"]


def test_executable_count_condition_clears_stale_llm_missing_fields() -> None:
    plan = {
        "target_user": {
            "purchase_date": {"from": "20190501", "to": "20190531"},
            "aggregate_conditions": [
                {
                    "metric_id": "total_item_quantity",
                    "operator": ">=",
                    "threshold": 2,
                    "aggregation_scope": "per_brand",
                }
            ],
        },
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["purchase_date", "purchase_buy_count"],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": "값을 확인해 주세요.",
        },
    }

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert [item["field"] for item in plan["semantic_ir_reconciliation"]["resolved_fields"]] == [
        "purchase_date",
        "purchase_buy_count",
    ]
