"""요청된 구매주기·구매예정일 경과 캠페인 타겟 회귀 1건."""

import graph_rag as g


PROMPT = "회원별 평균 구매주기가 30일 이내이고 다음 구매예정일이 지난 고객을 대상으로 구매 알림 캠페인을 만들어줘."


def test_purchase_cycle_with_past_due_date_compiles_from_latest_member_snapshot():
    plan = g.build_query_plan(PROMPT, parser="rules")
    target = plan["target_user"]

    cycle = next(c for c in target["balance_conditions"] if c.get("label") == "buy_cycle")
    assert cycle["operator"] == "<="
    assert cycle["threshold"] == 30
    assert cycle["profile_source"]["table"] == "CRM_MB_MONTHCRMINFO"

    due = next(c for c in target["profile_date_conditions"] if c.get("metric_id") == "next_purchase_due_date")
    assert due["operator"] == "<"
    assert due["right_expression"] == "CONVERT(char(8), GETDATE(), 112)"

    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    assert sql.count("EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO M") == 1
    assert "M.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in sql
    assert "M.BUY_CYCLE <= 30" in sql
    assert "M.BUY_DUE_DATE < CONVERT(char(8), GETDATE(), 112)" in sql
    coverage = g.validate_sql_condition_coverage(sql, g.required_sql_conditions(plan))
    assert coverage["is_satisfied"] is True
