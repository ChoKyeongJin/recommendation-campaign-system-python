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
