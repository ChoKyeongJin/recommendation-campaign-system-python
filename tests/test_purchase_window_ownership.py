"""같은 주문 도메인의 존재/부재 조건이 각자의 상대 기간을 소유하는 회귀."""

import graph_rag as g


PROMPT = "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원을 추출해서 이탈방지 캠페인을 만들어줘."


def test_recent_order_presence_and_purchase_absence_keep_separate_windows():
    plan = g.build_query_plan(PROMPT, parser="rules")
    target_user = plan["target_user"]

    assert target_user["purchase_membership"] == {
        "domain": "purchase",
        "operator": "exists",
        "window_days": 90,
    }
    assert target_user["purchase_inactivity"] == {
        "value": 30,
        "unit": "days",
        "min_days": 30,
    }

    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    assert g._purchase_membership_predicate(90) in sql
    assert g._purchase_inactivity_predicate(30) in sql
    assert g._purchase_inactivity_predicate(90) not in sql

    evidence = [g._condition_evidence(condition, sql) for condition in plan["semantic_conditions"]]
    assert all(item["satisfied"] and item["polarity_match"] for item in evidence)
    assert "exists_or_join" in evidence[0]["actual_evidence"]
    assert "not_exists" in evidence[1]["actual_evidence"]
