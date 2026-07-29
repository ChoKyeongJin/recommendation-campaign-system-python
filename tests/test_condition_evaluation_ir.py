from __future__ import annotations

import copy

import networkx as nx

import condition_evaluation_ir
import graph_rag


QUERY = "2019년 3월에 같은 상품을 동시 구매한 고객 수"


def test_same_product_co_purchase_is_lossless_condition_evaluation_ir() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")

    evaluation = plan["condition_evaluations"][0]
    assert evaluation["decision_target"] == {"entity": "member", "key": "member_no"}
    assert evaluation["evaluation_scope"]["time_range"] == {
        "field": "order_date", "from": "20190301", "to": "20190331",
    }
    assert evaluation["grouping_unit"] == {
        "entity": "order_product", "keys": ["member_no", "order_id", "product_id"],
    }
    assert evaluation["measure"]["field"] == "order_quantity"
    assert evaluation["aggregation"] == {"function": "sum", "measure": "order_quantity"}
    assert evaluation["comparison"] == {"operator": "gte", "value": 2}
    assert evaluation["final_result"]["aggregation"]["function"] == "count_distinct"
    assert plan["unresolved_source_conditions"] == []


def test_condition_and_final_result_grains_compile_as_separate_stages() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=QUERY,
    )

    assert result["is_success"] is True
    sql = result["sql"]
    assert "GROUP BY D.MEMBER_NO, D.ORDER_ID, D.PRODUCT_ID" in sql
    assert "HAVING SUM(D.ORDER_QTY) >= 2" in sql
    assert "SELECT DISTINCT MEMBER_NO\n    FROM CONDITION_GROUPS" in sql
    assert "COUNT(DISTINCT M.MEMBER_NO) AS CUSTOMER_COUNT" in sql
    assert result["delivery_validation"]["actual_grain"] == "member_count"
    assert result["condition_evaluation_validation"] == {"ran": True, "valid": True, "errors": []}


def test_unknown_grouping_or_aggregation_combination_fails_closed() -> None:
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    broken = copy.deepcopy(plan)
    broken["condition_evaluations"][0]["grouping_unit"]["keys"] = ["member_no", "product_id"]
    broken["condition_evaluations"][0]["aggregation"]["function"] = "count"

    graph_rag._refresh_unresolved_source_conditions(QUERY, broken)

    unresolved = broken["unresolved_source_conditions"]
    assert any(item["path"].endswith("grouping_unit.keys") for item in unresolved)
    assert any(item["path"].endswith("aggregation.function") for item in unresolved)
    assert graph_rag.build_sql_template_candidate(broken) is None
    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, broken, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=QUERY,
    )
    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "query_plan_required_conditions_missing"


def test_detected_condition_without_supported_final_unit_is_unresolved() -> None:
    query = "2019년 3월에 같은 상품을 동시 구매한 고객"
    plan = graph_rag.build_query_plan(query, parser="rules")

    assert not plan.get("condition_evaluations")
    assert any(item.get("path") == "final_result" for item in plan["unresolved_source_conditions"])


def test_validated_evaluation_locks_sql_generation_to_the_compiler(monkeypatch) -> None:
    """검증된 조건 판정 IR이 있으면 자유 SQL/타겟팅 IR 후보를 아예 세우지 않는다.

    경쟁 후보를 세우면 그 후보가 2단계 구조(WITH CONDITION_GROUPS → QUALIFIED_MEMBERS)를 만들지
    못해 탈락하고, 그 탈락 사유가 응답에 실려 '구조 보장 실패'로 보인다.
    """
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    assert graph_rag.condition_evaluation_locked(plan) is True

    def _fail(*args, **kwargs):  # pragma: no cover - 호출되면 잠금이 깨진 것
        raise AssertionError("조건 판정 IR 잠금 상태에서 LLM 후보를 세웠다")

    monkeypatch.setattr(graph_rag, "_build_llm_targeting_ir_candidate", _fail)
    monkeypatch.setattr(graph_rag, "_build_llm_sql_fallback_candidate", _fail)

    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=QUERY, llm_model="gpt-5-mini",
    )

    assert result["is_success"] is True
    assert result["llm_fallback_used"] is False
    assert result["selected"]["id"] == graph_rag.CONDITION_EVALUATION_CANDIDATE_ID


def test_shipped_sql_must_preserve_the_two_stage_structure() -> None:
    """출고 직전 SQL이 2단계 구조·IR 기간을 잃으면 성공으로 나가지 않는다(마지막 방어선)."""
    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    flattened = (
        "SELECT COUNT(DISTINCT D.MEMBER_NO) AS CUSTOMER_COUNT "
        "FROM CRM_SL_ORDERDETAILMALL D WHERE D.MEMBER_NO IS NOT NULL"
    )
    original = graph_rag.build_condition_evaluation_sql_candidate

    def _flattened_candidate(query_plan):
        candidate = original(query_plan)
        if candidate is not None:
            candidate["sql"] = flattened
        return candidate

    graph_rag.build_condition_evaluation_sql_candidate = _flattened_candidate
    try:
        result = graph_rag.build_sql_result(
            nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=QUERY,
        )
    finally:
        graph_rag.build_condition_evaluation_sql_candidate = original

    assert result["is_success"] is False
    assert result["sql"] is None
    assert any(
        error["code"] == "compiled_semantics_not_guaranteed"
        for error in result["selected"]["condition_evaluation_validation"]["errors"]
    )


def test_compiled_sql_validator_rejects_flattened_purchase_exists_sql() -> None:
    evaluation = condition_evaluation_ir.build_same_product_co_purchase_evaluation(
        QUERY, {"from": "20190301", "to": "20190331"},
    )
    flattened = (
        "SELECT COUNT(DISTINCT D.MEMBER_NO) FROM CRM_SL_ORDERDETAILMALL D "
        "WHERE D.ORDER_DATE BETWEEN '20190301' AND '20190331'"
    )

    issues = condition_evaluation_ir.validate_compiled_sql(evaluation, flattened)
    assert any(issue.code == "compiled_semantics_not_guaranteed" for issue in issues)
