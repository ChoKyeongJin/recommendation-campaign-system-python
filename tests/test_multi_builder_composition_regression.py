"""여러 빌더·미모델 의미가 한 프롬프트에 섞였을 때의 회귀 코퍼스(2차 배치 A/C군 + 창 누수).

배경: 회원 집계 조건이 서로 다른 빌더로 갈리면 승자만 SQL 을 만들고 나머지가 드롭됐다(#7 캠페인 구매건수,
#10 누적 구매액, #14 카트 종류). 카트 슬롯이 단일이라 카트 조건 2개 중 하나만 남았고(#14), '최근 N일 무주문'
창이 카트 보관기간에 새거나(#6) '누적' 집계에 다시 붙었다(#10). 미모델 의미(쿠폰 사용 건수 #15, 메시지
수신 횟수 #12)는 조용한 오답 대신 미지원 게이트로 명시한다.

실행: docker compose exec -w /app -e PYTHONPATH=/app python pytest tests/test_multi_builder_composition_regression.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(plan: dict) -> str:
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None, "후보 없음"
    return cand["sql"]


# ── #14: 카트 다중 조건 + 무구매 ──────────────────────────────────────────────
def test_cart_two_metrics_and_no_purchase_compose():
    plan = _plan("장바구니 총수량이 10개 이상이고 장바구니 상품 종류가 3종 이상이지만 구매 이력이 없는 고객을 추출해줘.")
    agg = plan["target_user"]["cart_aggregate"]
    metrics = {c["metric"]: (c["operator"], c["threshold"]) for c in (agg if isinstance(agg, list) else [agg])}
    assert metrics["cart_quantity"] == (">=", 10)
    assert metrics["cart_line_count"] == (">=", 3)
    sql = _sql(plan)
    assert "SUM(QTY) >= 10 AND COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)" in sql


def test_cart_single_condition_stays_dict():
    # 단일 카트 조건은 기존 dict 형태 유지(기존 테스트/리더 호환).
    agg = _plan("장바구니에 3개 이상 담은 회원")["target_user"]["cart_aggregate"]
    assert agg == {"metric": "cart_line_count", "operator": ">=", "threshold": 3}


# ── #10: 누적 구매액 + 최근 N일 무주문 합성(창 누수 없이) ──────────────────────
def test_cumulative_amount_composes_with_inactivity():
    plan = _plan("누적 구매액이 1,000,000원 이상인 회원 중 최근 180일 동안 주문이 없는 고객을 찾아줘.")
    aggs = [(c["metric_id"], c["operator"], c["threshold"], c.get("window_days")) for c in plan["target_user"]["aggregate_conditions"]]
    assert ("purchase_amount", ">=", 1000000.0, None) in aggs  # 누적=창 없음
    sql = _sql(plan)
    assert "SUM(PAYMENT_AMT) >= 1000000" in sql
    assert "DATEADD(DAY, -180, GETDATE())" in sql
    assert (plan.get("unsupported") is None)


def test_windowed_cumulative_metric_keeps_window_when_no_inactivity():
    # inactivity 가 없으면 '최근 90일 누적 구매금액'의 창은 그대로 유지된다(과도 억제 방지).
    plan = _plan("최근 90일 누적 구매 금액이 1,000,000원 이상인 회원을 찾아줘.")
    aggs = [(c["metric_id"], c.get("window_days")) for c in plan["target_user"]["aggregate_conditions"]]
    assert ("purchase_amount", 90) in aggs


# ── #7: 캠페인 구매건수 + 구매금액 합성 ───────────────────────────────────────
def test_campaign_buy_count_and_amount_compose():
    plan = _plan("마케팅 수신에 동의한 회원 중 캠페인 구매건수가 2건 이상이고 캠페인 구매금액이 100,000원 이상인 고객을 찾아줘.")
    tu = plan["target_user"]
    assert tu["campaign_buy_count"]["operator"] == ">=" and tu["campaign_buy_count"]["count"] == 2
    # order_count 로 이중 파싱된 집계는 걷어낸다.
    assert not tu.get("aggregate_conditions")
    sql = _sql(plan)
    assert "COUNT(DISTINCT CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)) >= 2" in sql
    assert "SUM(R.BUY_AMT) >= 100000" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql


# ── #6: 최근 N일 무주문이 카트 보관기간으로 새지 않음 ─────────────────────────
def test_recent_inactivity_not_stolen_as_cart_retention():
    plan = _plan("VIP 회원 중 장바구니에 상품을 5개 이상 담았지만 최근 7일 동안 구매하지 않은 고객을 보여줘.")
    assert plan["target_user"].get("cart_retention") is None
    assert plan["target_user"]["purchase_inactivity"]["min_days"] == 7
    sql = _sql(plan)
    assert "UPD_DT" not in sql  # 보관기간 필터 없음
    assert "DATEADD(DAY, -7, GETDATE())" in sql  # 무주문 anti-join 은 있음


def test_genuine_cart_retention_still_parsed():
    # '담아둔' 보관 표현은 그대로 보관기간으로 잡힌다(과도 억제 방지).
    plan = _plan("장바구니에 7일 이상 담아둔 회원을 찾아줘.")
    assert plan["target_user"]["cart_retention"]["min_days"] == 7


# ── #15 / #12: 미모델 의미 → 미지원 게이트 ────────────────────────────────────
def test_coupon_usage_count_is_unsupported():
    plan = _plan("쿠폰을 3개 이상 사용하고 캠페인 구매금액이 200,000원 이상인 30대 여성 회원을 보여줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "coupon_usage_count_unsupported"
    assert g.build_sql_template_candidate(plan) is None


def test_coupon_usage_existence_still_supported():
    # 건수 없는 '쿠폰을 사용한'(사용 여부)은 게이트되지 않는다.
    plan = _plan("쿠폰을 사용한 회원을 찾아줘.")
    assert plan.get("unsupported") is None


def test_message_received_count_is_unsupported():
    plan = _plan("캠페인 메시지를 3회 이상 받은 회원 중 오퍼 반응은 있었지만 구매건수는 0건인 고객을 보여줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "message_received_count_unsupported"
    assert g.build_sql_template_candidate(plan) is None


def test_response_frequency_still_supported():
    # '반응 횟수'(campaign_response_frequency)는 게이트되지 않는다.
    plan = _plan("최근 3개월 캠페인에 3회 이상 반응한 회원을 찾아줘.")
    assert plan.get("unsupported") is None
    assert plan["target_user"]["campaign_response_frequency"]["count"] == 3
