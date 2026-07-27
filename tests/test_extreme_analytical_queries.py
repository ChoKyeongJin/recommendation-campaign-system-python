"""Final acceptance cases requested for /target-sql extreme analytics."""

import networkx as nx
import pytest

import graph_rag as g


CASES = [
    (
        "가장 많이 구매한 회원의 구매금액을 알려줘.",
        "ranking", "purchase_amount", "SUM", "DESC",
        ["TOP 1 O.MEMBER_NO AS CUST_ID", "SUM(O.PAYMENT_AMT)", "ORDER BY SUM(O.PAYMENT_AMT) DESC"],
    ),
    (
        "가장 적게 구매한 회원의 구매금액을 알려줘.",
        "ranking", "purchase_amount", "SUM", "ASC",
        ["TOP 1 O.MEMBER_NO AS CUST_ID", "SUM(O.PAYMENT_AMT)", "ORDER BY SUM(O.PAYMENT_AMT) ASC"],
    ),
    (
        "최고 구매금액을 알려줘.",
        "aggregate", "purchase_amount", "MAX", None,
        ["MAX(O.PAYMENT_AMT)", "CRM_SL_ORDERHEADERMALL"],
    ),
    (
        "최소 구매금액을 알려줘.",
        "aggregate", "purchase_amount", "MIN", None,
        ["MIN(O.PAYMENT_AMT)", "CRM_SL_ORDERHEADERMALL"],
    ),
    (
        "가장 최근 구매일을 알려줘.",
        "aggregate", "purchase_date", "MAX", None,
        ["MAX(O.ORDER_DATE) AS PURCHASE_DATE", "CRM_SL_ORDERHEADERMALL"],
    ),
    (
        "가장 오래된 구매일을 알려줘.",
        "aggregate", "purchase_date", "MIN", None,
        ["MIN(O.ORDER_DATE) AS PURCHASE_DATE", "CRM_SL_ORDERHEADERMALL"],
    ),
    (
        "로그인을 가장 많이 한 회원의 로그인 횟수를 알려줘.",
        "ranking", "login_count", "MAX", "DESC",
        ["TOP 1 B.MEMBER_NO AS CUST_ID", "MAX(B.TOTAL_LOGIN_CNT)", "ORDER BY MAX(B.TOTAL_LOGIN_CNT) DESC"],
    ),
    (
        "적립금이 가장 많은 회원을 알려줘.",
        "ranking", "carrot_balance", "MAX", "DESC",
        ["TOP 1 B.MEMBER_NO AS CUST_ID", "MAX(B.CARROT_BALANCE_AMT)", "ORDER BY MAX(B.CARROT_BALANCE_AMT) DESC"],
    ),
    (
        "예치금이 가장 많은 회원을 알려줘.",
        "ranking", "deposit_balance", "MAX", "DESC",
        ["TOP 1 B.MEMBER_NO AS CUST_ID", "MAX(B.DEPOSIT_BALANCE_AMT)", "ORDER BY MAX(B.DEPOSIT_BALANCE_AMT) DESC"],
    ),
    (
        "장바구니 상품이 가장 많은 회원을 알려줘.",
        "ranking", "cart_product_quantity", "SUM", "DESC",
        ["TOP 1 C.CART_ID AS CUST_ID", "SUM(C.QTY)", "GROUP BY C.CART_ID", "C.KEEP_YN = 'Y'"],
    ),
]


@pytest.mark.parametrize(("query", "query_type", "metric", "function", "direction", "sql_fragments"), CASES)
def test_extreme_analytical_query_contract(query, query_type, metric, function, direction, sql_fragments):
    plan = g.build_query_plan(query, parser="rules")
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )

    intent = plan["detected_intent"]
    assert plan["intent"] == "analyze_aggregation"
    assert plan["capability_check"]["passed"] is True
    assert intent["query_type"] == query_type
    assert intent["metric"] == metric
    assert intent["aggregate_function"] == function
    assert intent["result_shape"] == ("single_member" if query_type == "ranking" else "scalar")
    assert intent["target_entity"] == ("member" if query_type == "ranking" else None)
    assert result["is_success"] is True, (query, result.get("failure_reason"), result.get("selected"))
    assert result["aggregation_validation"]["valid"] is True
    assert result["intent_sql_contract"]["valid"] is True
    assert result["intent_sql_contract"]["actual_shape"] == intent["result_shape"]
    assert result["delivery_validation"]["is_satisfied"] is True
    assert not [issue for issue in result["selected"]["validation"]["issues"] if issue["severity"] == "error"]
    assert "ORDER_PAY_AMT" not in result["sql"]
    assert "PAY_AMT" not in result["sql"]
    if direction:
        assert plan["analytical_intent"]["ranking_direction"] == direction
    for fragment in sql_fragments:
        assert fragment in result["sql"]
