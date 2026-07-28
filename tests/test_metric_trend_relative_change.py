"""사용자 요청 문장: 절대 두 기간의 구매금액이 10% 이상 증가한 고객."""

import graph_rag as g


def test_2019_feb_to_mar_purchase_amount_increased_at_least_ten_percent():
    query = "2019년 2월과 3월의 구매금액차이가 10% 이상 증가한 고객 리스트"

    plan = g.build_query_plan(query)
    trend = plan["target_user"]["metric_trend"]
    assert trend["metric_id"] == "purchase_amount"
    assert trend["baseline"] == {"from": "20190201", "to": "20190228", "label": "2019년 2월"}
    assert trend["current"] == {"from": "20190301", "to": "20190331", "label": "2019년 3월"}
    assert trend["relative_change"] == {
        "unit": "percent",
        "comparisons": [{"operator": ">=", "value": 10.0}],
    }

    candidate = g.build_metric_trend_targets_sql_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    assert "ORDER_DATE BETWEEN '20190201' AND '20190228'" in sql
    assert "ORDER_DATE BETWEEN '20190301' AND '20190331'" in sql
    assert "INNER JOIN (" in sql
    assert "M2.TREND_VALUE > 0" in sql
    assert "((M.TREND_VALUE - M2.TREND_VALUE) * 100.0 / NULLIF(M2.TREND_VALUE, 0)) >= 10" in sql
