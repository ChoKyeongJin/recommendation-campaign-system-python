from __future__ import annotations

import json

import networkx as nx

import graph_rag
from aggregation_requirements import SchemaMetadata
from sql_guard import load_allowed_tables, load_schema_columns, validate_sql


QUERY = "2019년 상반기에 같은 브랜드에서 2개 이상 구매한 사람중 여자만 추출해줘"


def test_catalog_backed_aggregate_ir_compiles_purchase_quantity_without_raw_column_choice() -> None:
    capabilities = graph_rag._targeting_aggregate_capabilities()
    per_brand = capabilities["total_item_quantity"]["scopes"]["per_brand"]

    assert per_brand["table"] == "CRM_SL_ORDERDETAILMALL"
    assert per_brand["column"] == "ORDER_QTY"

    expression = {
        "and": [
            {"member_filter": "female"},
            {
                "aggregate_threshold": {
                    "relation": "purchase",
                    "metric_id": "total_item_quantity",
                    "aggregation_scope": "per_brand",
                    "operator": ">=",
                    "threshold": 2,
                    "period": "2019년 상반기",
                }
            },
        ]
    }
    candidate = graph_rag._compile_targeting_ir_candidate(expression)

    assert candidate is not None
    assert "GROUP BY D.MEMBER_NO, D.BRAND_ID" in candidate["sql"]
    assert "HAVING SUM(D.ORDER_QTY) >= 2" in candidate["sql"]
    assert "D.QTY" not in candidate["sql"]
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in candidate["sql"]


def test_catalog_removal_closes_aggregate_ir_capability(tmp_path) -> None:
    payload = json.loads(graph_rag.DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    detail = payload["tables"]["CRM_SL_ORDERDETAILMALL"]
    detail["columns"] = [
        column for column in detail["columns"] if column.get("name") != "ORDER_QTY"
    ]
    catalog = tmp_path / "schema_catalog.json"
    catalog.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    capabilities = graph_rag._targeting_aggregate_capabilities(catalog)

    assert "total_item_quantity" not in capabilities






def test_real_unresolved_condition_blocks_every_llm_fallback(monkeypatch) -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    plan["unresolved_source_conditions"] = [
        {
            "path": None,
            "condition": "정의되지 않은 별도 조건",
            "reason": "실행 슬롯을 확정할 수 없습니다.",
            "source": "llm_semantic_ir",
        }
    ]
    monkeypatch.setattr(
        graph_rag,
        "_build_llm_targeting_ir_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("IR fallback called")),
    )
    monkeypatch.setattr(
        graph_rag,
        "_build_llm_sql_fallback_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SQL fallback called")),
    )

    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        llm_model="test-model", original_query=QUERY,
    )

    assert result["is_success"] is False
    # fail-close 게이트가 여럿이라 어느 게이트가 먼저 막는지는 배선에 달려 있다(2026-08-02
    # 이후엔 의미 coverage 게이트가 먼저다). 계약은 '자유 SQL/IR 폴백으로 우회하지 않는다'이다.
    assert result["failure_reason"] in {
        "query_plan_required_conditions_missing",
        "semantic_ir_needs_clarification",
        # 결핍 원인 분류(2026-08-02) 이후 구조화기·설정 실패는 별도 코드로 갈린다.
        "semantic_structurer_failure",
        "semantic_registry_gap",
        "unresolved_source_conditions",
    }
    assert result["interpretation_status"] == "needs_clarification"
    assert result["llm_fallback_used"] is False


def test_sql_guard_distinguishes_order_quantity_from_cart_quantity() -> None:
    schema_columns = load_schema_columns(graph_rag.DEFAULT_SCHEMA_PATH)
    allowed_tables = load_allowed_tables(graph_rag.DEFAULT_SCHEMA_PATH)
    bad = validate_sql(
        "SELECT D.MEMBER_NO FROM CRM_SL_ORDERDETAILMALL D "
        "GROUP BY D.MEMBER_NO, D.BRAND_ID HAVING SUM(D.QTY) >= 2",
        allowed_tables=allowed_tables,
        default_limit=None,
        dialect="tsql",
        schema_columns=schema_columns,
    )
    good = validate_sql(
        "SELECT D.MEMBER_NO FROM CRM_SL_ORDERDETAILMALL D "
        "GROUP BY D.MEMBER_NO, D.BRAND_ID HAVING SUM(D.ORDER_QTY) >= 2",
        allowed_tables=allowed_tables,
        default_limit=None,
        dialect="tsql",
        schema_columns=schema_columns,
    )

    assert bad["is_valid"] is False
    assert any("crm_sl_orderdetailmall.qty" in issue["message"] for issue in bad["issues"])
    assert good["is_valid"] is True


