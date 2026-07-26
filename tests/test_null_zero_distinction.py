"""NULL('정보 없음') vs 0('0원/0회/0건') 구분 회귀 코퍼스.

배경: 값이 '없다'(미기입, NULL)와 값이 '0이다'는 다른 대상인데, 파서가 이를 뭉개거나(예치금 '정보 없음'을
=0 으로), 조용히 드롭하거나(로그인 '0회'), 심지어 정반대로 뒤집었다(캠페인 구매건수 '없거나 0건' →
구매반응 EXISTS). 이제 표현별로 세 갈래를 구분한다:
  * '정보가 없는 / 입력되지 않은'      → col IS NULL (값 자체 미기입)
  * '0원 / 0회'                       → col = 0 (NULL 제외)
  * '없거나 0원'                      → (col IS NULL OR col = 0)
  * '한 번도 …'(nullable 지표)        → COALESCE(col,0)=0 (NULL 포함, 기존 유지)

특히 '구매 횟수가 0회'와 '한 번도 구매하지 않은'은 둘 다 주문 anti-join(NOT EXISTS)으로 컴파일해야 한다 —
집계 HAVING COUNT(...)=0 은 그룹에 안 나타나 항상 공집합이라 표현 불가하기 때문이다. '구매 이력은 있지만
결제금액 합계가 0원'은 반대로 SUM=0(GROUP BY 서브쿼리라 주문 있는 회원만)으로 표현 가능하다.

실행: python -m pytest tests/test_null_zero_distinction.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    cand = g.build_sql_template_candidate(_plan(query))
    assert cand is not None, f"{query!r}: SQL 후보 없음"
    return cand["sql"]


# ── 회원 지표(예치금/로그인): IS NULL / =0 / NULL OR 0 세 갈래 ─────────────────────────────
def test_deposit_missing_is_null():
    # '정보가 없는' = 값 미기입(NULL). =0(0원 잔액)과 구분한다.
    sql = _sql("예치금 정보가 없는 회원을 찾아줘.")
    assert "B.DEPOSIT_BALANCE_AMT IS NULL" in sql
    assert "DEPOSIT_BALANCE_AMT = 0" not in sql


def test_deposit_zero_is_equals():
    sql = _sql("예치금이 0원인 회원을 찾아줘.")
    assert "B.DEPOSIT_BALANCE_AMT = 0" in sql
    assert "IS NULL" not in sql


def test_deposit_missing_or_zero_is_both():
    sql = _sql("예치금이 없거나 0원인 회원을 찾아줘.")
    assert "(B.DEPOSIT_BALANCE_AMT IS NULL OR B.DEPOSIT_BALANCE_AMT = 0)" in sql


def test_login_missing_is_null_not_coalesce():
    # '로그인 횟수 정보가 없는' = IS NULL — '0회'(=0)나 '한 번도'(COALESCE=0)와 다른 세 번째 의미.
    sql = _sql("로그인 횟수 정보가 없는 고객을 추출해줘.")
    assert "B.TOTAL_LOGIN_CNT IS NULL" in sql
    assert "COALESCE" not in sql


def test_login_zero_count_excludes_null():
    # '0회' = 명시적 0 → = 0(NULL 제외). COALESCE 로 NULL 을 끌어들이지 않는다.
    sql = _sql("로그인 횟수가 0회인 고객을 추출해줘.")
    assert "B.TOTAL_LOGIN_CNT = 0" in sql
    assert "COALESCE" not in sql and "IS NULL" not in sql


def test_login_never_still_coalesces_null():
    # 회귀: '한 번도 로그인하지 않은'(nullable 지표 부재)은 여전히 COALESCE=0(NULL 포함).
    sql = _sql("한 번도 로그인하지 않은 회원을 찾아줘.")
    assert "COALESCE(B.TOTAL_LOGIN_CNT, 0) = 0" in sql


# ── 무구매(anti-join) vs 결제 SUM=0: COUNT=0 은 anti-join, SUM=0 은 HAVING ────────────────
def test_no_purchase_history_anti_join():
    sql = _sql("구매 이력이 없는 회원을 찾아줘.")
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)" in sql


def test_zero_purchase_count_maps_to_no_purchase():
    # '구매 횟수가 0회' = 집계 COUNT=0(공집합) → no_purchase anti-join 으로 컴파일한다.
    plan = _plan("구매 횟수가 0회인 회원")
    assert "no_purchase" in plan["target_user"].get("behaviors", [])
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None and cand["id"] == "sql_template:order_count_targets"
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in cand["sql"]
    # 공집합 HAVING COUNT=0 으로 새면 안 된다.
    assert "HAVING COUNT" not in cand["sql"]


def test_zero_count_and_never_purchase_compile_identically():
    # 사용자 지적: '구매 횟수 0회'와 '한 번도 구매하지 않은'은 같은 anti-join 이어야 한다.
    assert _sql("구매 횟수가 0회인 회원") == _sql("한 번도 구매하지 않은 회원")


def test_exact_zero_count_reversed_order_also_no_purchase():
    # 어순이 반대인 '정확히 0회 구매한'(집계 order_count '='0)도 anti-join 으로 승격한다.
    plan = _plan("정확히 0회 구매한 회원")
    assert "no_purchase" in plan["target_user"].get("behaviors", [])
    assert "HAVING COUNT" not in _sql("정확히 0회 구매한 회원")


def test_purchase_exists_but_zero_sum_uses_having_sum():
    # '구매 이력은 있지만 결제금액 합계가 0원' = 주문 있고 SUM=0 → HAVING SUM(PAYMENT_AMT)=0
    # (GROUP BY MEMBER_NO 서브쿼리라 주문 있는 회원만 포함 = '구매 이력 있음' 자동 보장).
    plan = _plan("구매 이력은 있지만 결제금액 합계가 0원인 회원을 찾아줘.")
    assert plan.get("unsupported") is None
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "HAVING SUM(PAYMENT_AMT) = 0" in sql
    assert "GROUP BY MEMBER_NO" in sql
    assert "no_purchase" not in plan["target_user"].get("behaviors", [])


def test_bare_zero_amount_still_requires_policy():
    # 회귀: 구매 존재 단언이 없는 '구매 금액이 0원'은 여전히 모호 — 무주문 정책 필요(미지원 게이트).
    plan = _plan("구매 금액이 0원인 회원을 추출해줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "zero_amount_semantics_requires_policy"


# ── 카트 수량 미입력(QTY IS NULL) vs 수량 임계(SUM(QTY)) ───────────────────────────────────
def test_cart_quantity_missing_is_null_exists():
    sql = _sql("장바구니 수량이 입력되지 않은 고객을 추출해줘.")
    assert "EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART A" in sql
    assert "A.QTY IS NULL" in sql


def test_cart_quantity_threshold_unaffected():
    # 회귀: '수량 5개 이상'은 여전히 HAVING SUM(QTY)>=5 (미입력 감지가 임계 파싱을 가로채면 안 된다).
    sql = _sql("장바구니 상품 수량이 5개 이상인 고객을 추출해줘.")
    assert "SUM(QTY) >= 5" in sql
    assert "QTY IS NULL" not in sql


# ── 캠페인 구매건수 0/없음: 정반대(EXISTS 구매) 반전 방지 → no_buy_response(NOT EXISTS) ──────
def test_campaign_zero_buy_count_is_negation_not_inverted():
    plan = _plan("캠페인 구매건수가 없거나 0건인 회원을 보여줘.")
    responses = plan["target_user"].get("campaign_responses") or []
    assert any(r.get("canonical") == "no_buy_response" and r.get("negated") for r in responses), responses
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "NOT EXISTS" in sql and "R.BUY_RSPN_YN = 'Y'" in sql


def test_campaign_positive_buy_count_still_exists():
    # 회귀: '캠페인 구매건수 2건 이상'(양수)은 여전히 긍정(EXISTS 구매반응)이어야 한다 — 부정으로 안 뒤집힘.
    plan = _plan("캠페인 구매건수가 2건 이상인 회원")
    responses = plan["target_user"].get("campaign_responses") or []
    assert not any(r.get("canonical") == "no_buy_response" for r in responses), responses


# ── 오탐 방지: 명시 0 뒤 비교어('0회 이상')는 무주문이 아니다 ──────────────────────────────
def test_zero_with_comparison_is_not_no_purchase():
    plan = _plan("구매 횟수가 0회 이상인 회원")
    assert "no_purchase" not in plan["target_user"].get("behaviors", [])


def test_calendar_period_zero_count_not_lifetime_no_purchase():
    # '올해 …0회'는 그 기간 무주문이라 평생 무주문(no_purchase)으로 승격하지 않는다.
    plan = _plan("올해 주문 횟수가 0회인 고객")
    assert "no_purchase" not in plan["target_user"].get("behaviors", [])
