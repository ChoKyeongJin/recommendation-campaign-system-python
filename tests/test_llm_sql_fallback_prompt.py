"""LLM SQL 폴백 시스템 프롬프트의 조건 추출·검증 계약 회귀 테스트."""

import json
from types import SimpleNamespace

import api
import graph_rag as g


def test_llm_sql_fallback_prompt_requires_condition_extraction_and_verification(monkeypatch):
    captured = {}

    def fake_chat_create(_client, **kwargs):
        captured.update(kwargs)
        content = '{"sql":"SELECT DISTINCT B.CUST_ID AS CUST_ID FROM CRM_MB_BASEINFO B","explanation":"ok"}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(g, "_openai_chat_create", fake_chat_create)
    monkeypatch.setattr(g, "_write_rag_llm_log", lambda *_args, **_kwargs: None)

    candidate = g._build_llm_sql_fallback_candidate(
        "최근 30일 구매 금액이 가장 많은 상위 10명",
        {"intent": "find_user_segment"},
        [],
        {"CRM_MB_BASEINFO"},
        "test-model",
    )

    assert candidate is not None
    system_prompt = captured["messages"][0]["content"]
    for field in (
        "target_entities",
        "period_conditions",
        "aggregation",
        "order_by",
        "top_n",
        "relationship_conditions",
        "deduplication_basis",
        "exclusion_conditions",
    ):
        assert field in system_prompt
    assert "condition_verification" in system_prompt
    assert "'가장 많이', '상위 N', '베스트 N'" in system_prompt
    assert "집계, 내림차순 정렬, 순위 제한" in system_prompt
    assert "하나라도 구현되지" in system_prompt


def test_aggregation_sql_validation_failure_is_retried(monkeypatch, tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps({
        "tables": {
            "sales": {
                "database": "CRMDW",
                "columns": [
                    {"name": "product_id", "type": "nvarchar(20)", "primary_key": False},
                    {"name": "quantity", "type": "bigint", "primary_key": False},
                ],
                "primary_key": [],
                "indexes": [],
            }
        }
    }), encoding="utf-8")
    aggregation_request = {
        "targetEntity": "product",
        "outputColumns": [{"entity": "product", "field": "productId", "table": "sales", "column": "product_id"}],
        "filters": [],
        "groupings": [{"entity": "product", "field": "productId", "table": "sales", "column": "product_id"}],
        "aggregations": [{"id": "sales_qty", "function": "sum", "entity": "sale", "field": "quantity",
                          "table": "sales", "column": "quantity", "distinct": False, "alias": "sales_qty"}],
        "derivedMetrics": [],
        "sorting": [{"metricId": "sales_qty", "direction": "desc"}],
        "ranking": {"enabled": True, "type": "top", "limit": 10, "partitionBy": [],
                    "orderByMetricId": "sales_qty", "tiePolicy": "include_exact_limit"},
        "postAggregationFilters": [], "relationConditions": [], "dateGrain": None, "comparison": None,
        "businessRules": {}, "assumptions": [], "unresolvedFields": [],
    }
    base_response = {
        "queryType": "aggregation", "requirementMappings": [], "condition_verification": [],
        "usedTables": [], "usedColumns": [], "assumptions": [], "unresolvedFields": [],
        "warnings": [], "confidence": 0.95, "explanation": "ok",
    }
    responses = [
        {**base_response, "sql": "SELECT product_id, SUM(quantity) sales_qty FROM sales GROUP BY product_id ORDER BY sales_qty DESC"},
        {**base_response, "sql": "SELECT TOP 10 product_id, SUM(quantity) sales_qty FROM sales GROUP BY product_id ORDER BY sales_qty DESC"},
    ]
    calls = []

    def fake_chat_create(_client, **kwargs):
        calls.append(kwargs)
        content = json.dumps(responses.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGGREGATION_SQL_MAX_RETRIES", "2")
    monkeypatch.setattr(g, "_openai_chat_create", fake_chat_create)
    monkeypatch.setattr(g, "_write_rag_llm_log", lambda *_args, **_kwargs: None)

    candidate = g._build_llm_sql_fallback_candidate(
        "판매량 상위 10개 상품",
        {"intent": "find_user_segment", "aggregation_request": aggregation_request},
        [], {"sales"}, "test-model", schema_path,
    )

    assert candidate is not None
    assert candidate["generation_attempt_count"] == 2
    assert candidate["aggregation_validation"]["valid"] is True
    assert "TOP 10" in candidate["sql"]
    repair_payload = json.loads(calls[1]["messages"][-1]["content"])
    assert any(error["code"] == "MISSING_TOP_N_LIMIT" for error in repair_payload["validation_errors"])


def test_execute_target_sql_blocks_failed_aggregation_validation():
    execution = api.execute_target_sql(
        "SELECT 1", True, 10,
        aggregation_validation={"valid": False, "errors": [{"code": "MISSING_AGGREGATION"}]},
    )
    assert execution["is_success"] is False
    assert execution["failure_reason"] == "aggregation_validation_failed"
