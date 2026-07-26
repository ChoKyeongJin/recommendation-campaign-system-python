"""복합 조건(여러 지표가 한 문장에) 회귀 코퍼스.

배경: 한 프롬프트에 지표가 둘 이상 오면 (1) 절 분리가 동사 연결어미('구매했고')를 못 끊어 같은 방향
임계 둘이 첫 값 하나로 붕괴하거나, (2) 지표 동의어 뒤 고정 길이 window 가 옆 절까지 삼켜 다른 지표의
임계를 훔쳐오거나(모순·드롭), (3) 두 잔액의 '합'을 조용히 각 컬럼 개별 임계로 분해하는 오답이 있었다.

실행: docker compose exec -w /app -e PYTHONPATH=/app python pytest tests/test_compound_condition_regression.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _aggs(plan: dict):
    return [(c["metric_id"], c["operator"], c["threshold"], c.get("window_days"))
            for c in (plan["target_user"].get("aggregate_conditions") or [])]


def _balances(plan: dict):
    return [(c["column"], c["operator"], c["threshold"]) for c in (plan["target_user"].get("balance_conditions") or [])]


def _sql(plan: dict) -> str:
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None, "후보 없음"
    return cand["sql"]


def test_verb_connective_splits_three_metrics():
    # 회귀(#13): '5회 이상 구매했고 구매금액이 500,000원 이상이며 10종 이상' 이 purchase_amount>=5 로 붕괴했다.
    plan = _plan("최근 90일 동안 5회 이상 구매했고 구매금액이 500,000원 이상이며 서로 다른 상품을 10종 이상 구매한 회원을 찾아줘.")
    aggs = set(_aggs(plan))
    assert ("order_count", ">=", 5.0, 90) in aggs
    assert ("purchase_amount", ">=", 500000.0, 90) in aggs
    assert ("distinct_product_count", ">=", 10.0, 90) in aggs
    sql = _sql(plan)
    assert "COUNT(DISTINCT ORDER_ID) >= 5" in sql
    assert "SUM(PAYMENT_AMT) >= 500000" in sql
    assert "COUNT(DISTINCT PRODUCT_ID) >= 10" in sql


def test_member_metric_window_does_not_steal_next_clause_bound():
    # 회귀(#8): '로그인 100회 이상이지만 구매 횟수 1회 이하'에서 로그인이 구매의 <=1 을 훔쳐
    # TOTAL_LOGIN_CNT >= 100 AND <= 1(모순·공집합)이 됐다.
    plan = _plan("누적 로그인 횟수가 100회 이상이지만 구매 횟수는 1회 이하인 회원을 추출해줘.")
    assert _balances(plan) == [("TOTAL_LOGIN_CNT", ">=", 100.0)]
    assert ("order_count", "<=", 1.0, None) in _aggs(plan)
    sql = _sql(plan)
    assert "B.TOTAL_LOGIN_CNT >= 100" in sql and "TOTAL_LOGIN_CNT <= 1" not in sql


def test_dual_bound_same_metric_kept_across_boundary():
    # window 절단이 같은 지표의 이중경계('30회 이상이지만 100회 미만')까지 끊으면 안 된다(연속은 숫자로 시작).
    plan = _plan("로그인 횟수가 30회 이상이지만 100회 미만인 고객을 찾아줘.")
    balances = set(_balances(plan))
    assert ("TOTAL_LOGIN_CNT", ">=", 30.0) in balances
    assert ("TOTAL_LOGIN_CNT", "<", 100.0) in balances


def test_base_metric_and_ratio_coexist():
    # 회귀(#9): '로그인 일수 30일 이상 + 하루 평균 로그인 3회 이상' 에서 원 지표가 드롭되거나(평균 표지 흡수),
    # 파생 비율이 원 지표에 덮여 사라졌다 — 둘 다 살아야 한다.
    plan = _plan("로그인 일수는 30일 이상이고 하루 평균 로그인 횟수는 3회 이상인 활동 회원을 보여줘.")
    cols = {(c["column"], c["operator"], c["threshold"], c.get("column_expr") is not None)
            for c in plan["target_user"]["balance_conditions"]}
    assert ("TOTAL_LOGIN_DAYS", ">=", 30.0, False) in cols        # 원 지표
    assert ("TOTAL_LOGIN_CNT", ">=", 3.0, True) in cols            # 파생 비율(CNT/DAYS)
    sql = _sql(plan)
    assert "B.TOTAL_LOGIN_DAYS >= 30" in sql
    assert "CAST(B.TOTAL_LOGIN_CNT AS FLOAT) / NULLIF(B.TOTAL_LOGIN_DAYS, 0) >= 3" in sql


def test_balance_column_sum_is_unsupported_not_split():
    # 회귀(#4): '예치금과 적립금의 합이 50,000원 이상'을 각 컬럼 >=50000 두 조건으로 분해하면 훨씬 좁은
    # 오답. 조용한 분해 대신 명시 미지원.
    plan = _plan("최근 30일 안에 로그인한 회원 중 예치금과 적립금의 합이 50,000원 이상인 고객을 찾아줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "column_sum_unsupported"
    assert plan["target_user"].get("balance_conditions") is None
    assert g.build_sql_template_candidate(plan) is None


def test_single_balance_still_parses():
    # 게이트가 단일 잔액 조건(합 문맥 아님)을 막지 않는다.
    plan = _plan("적립금이 10,000원 이상이고 예치금이 50,000원 이상인 회원을 추출해줘.")
    assert plan.get("unsupported") is None
    balances = set(_balances(plan))
    assert ("CARROT_BALANCE_AMT", ">=", 10000.0) in balances
    assert ("DEPOSIT_BALANCE_AMT", ">=", 50000.0) in balances
