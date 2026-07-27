"""Final acceptance cases for deterministic purchase-amount analytics."""

import networkx as nx
import pytest

import graph_rag as g


CASES = [
    ("전체 구매 금액을 알려줘.", "purchase_amount", [], ["SUM(O.PAYMENT_AMT)", "CRM_SL_ORDERHEADERMALL"]),
    ("최근 30일 총 구매금액을 알려줘.", "purchase_amount", [], ["DATEADD(DAY, -30", "O.ORDER_DATE"]),
    ("VIP 회원의 구매금액 합계를 알려줘.", "purchase_amount", [], ["B.EMART_GRADE_CD", "MEM_GRADE_CD.VIP"]),
    ("여성 회원 구매금액 합계를 알려줘.", "purchase_amount", [], ["B.GENDER_CD", "GENDER_CD.FEMALE"]),
    ("브랜드별 구매금액 합계를 알려줘.", "purchase_amount", ["brand"], ["D.BRAND_ID", "GROUP BY D.BRAND_ID"]),
    ("카테고리별 구매금액을 알려줘.", "purchase_amount", ["category"], ["P.CATEGORY", "GROUP BY P.CATEGORY"]),
    ("시도별 구매금액을 알려줘.", "purchase_amount", ["sido"], ["B.SIDO", "GROUP BY B.SIDO"]),
    ("회원등급별 구매금액 합계를 알려줘.", "purchase_amount", ["member_grade"], ["B.EMART_GRADE_CD", "GROUP BY B.EMART_GRADE_CD"]),
    ("캠페인 구매금액 합계를 알려줘.", "campaign_purchase_amount", [], ["SUM(R.BUY_AMT)", "R.BUY_RSPN_YN = 'Y'"]),
    ("쿠폰 사용으로 발생한 구매금액 합계를 알려줘.", "coupon_purchase_amount", [], ["SUM(R.OFFR_BUY_AMT)", "R.USE_CPN_CNT > 0"]),
]


@pytest.mark.parametrize(("query", "metric", "dimensions", "sql_fragments"), CASES)
def test_registered_purchase_amount_analytics(query, metric, dimensions, sql_fragments):
    plan = g.build_query_plan(query, parser="rules")
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )

    assert plan["intent"] == "analyze_aggregation"
    assert plan["detected_intent"] == {
        "query_type": "aggregate",
        "aggregate_function": "SUM",
        "metric": metric,
        "dimensions": dimensions,
        "filters": plan["detected_intent"]["filters"],
    }
    assert result["is_success"] is True, (query, result.get("failure_reason"))
    assert result["aggregation_validation"]["valid"] is True
    assert result["delivery_validation"]["is_satisfied"] is True
    assert result["semantic_invariants"]["ok"] is True
    assert result["confidence"]["level"] == "높음"
    assert "SELECT DISTINCT" not in result["sql"].upper()
    assert "MEM_GRADE_CD" not in result["sql"] or "EMART_GRADE_CD" in result["sql"]
    for fragment in sql_fragments:
        assert fragment in result["sql"]
