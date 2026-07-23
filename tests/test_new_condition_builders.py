"""새로 채운 타겟 조건 회귀 (A그룹 회원속성 + B그룹 캠페인 반응).

배경: supported_condition_hint 가 광고하지만 실제로는 SQL 이 안 만들어지던 조건들을 일괄 구현했다.
  A그룹(회원 테이블에 데이터 존재): 임직원/프리미엄/멤버십/SNS(Y/N), 가입 디바이스 채널(앱/PC/모바일웹),
    적립금/예치금 임계값, 무구매('한 번도 구매 안 한').
  B그룹(캠페인 반응 팩트 MCS_CAMP_MBR_RSPN_FT): 캠페인 접촉/오퍼 반응/구매 반응/쿠폰 사용.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_new_condition_builders.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    cand = g.build_sql_template_candidate(_plan(query))
    assert cand is not None, f"{query!r}: SQL 미생성"
    return cand["sql"]


# ── A그룹: 회원 Y/N 플래그 ─────────────────────────────────────────────
@pytest.mark.parametrize("query,column", [
    ("임직원 회원", "B.EMPLOYEE_YN = 'Y'"),
    ("프리미엄 회원", "B.PREMIUM_YN = 'Y'"),
    ("멤버십 회원", "B.MEMBERSHIP_YN = 'Y'"),
    ("소셜 가입한 회원", "B.SNS_REG_YN = 'Y'"),
])
def test_member_yn_flags(query, column):
    assert column in _sql(query)


def test_yn_flag_registered_in_eq_filters():
    assert g.MEMBER_EQ_FILTERS.get("employee") == ("member_type", "B.EMPLOYEE_YN", "Y")
    assert g.MEMBER_EQ_FILTERS.get("premium_member") == ("member_type", "B.PREMIUM_YN", "Y")


# ── A그룹: 가입 디바이스 채널 ─────────────────────────────────────────
@pytest.mark.parametrize("query,value", [
    ("앱으로 가입한 회원", "DEVICE_TYPE_CD.APP"),
    ("PC로 가입한 회원", "DEVICE_TYPE_CD.PC"),
    ("모바일웹으로 가입한 회원", "DEVICE_TYPE_CD.MW"),
])
def test_signup_device_channel(query, value):
    assert f"B.REG_CHANNEL_CD = '{value}'" in _sql(query)


def test_signup_device_requires_signup_context():
    # '가입' 문맥이 없으면(앱푸시 동의 등) 가입 채널로 오인하지 않는다.
    plan = _plan("앱푸시 수신동의한 회원")
    assert "app_signup" not in plan["target_user"].get("lifecycle", [])


# ── A그룹: 적립금/예치금 임계값 ───────────────────────────────────────
def test_balance_threshold():
    assert "B.CARROT_BALANCE_AMT >= 5000" in _sql("적립금 5천원 이상인 회원")
    assert "B.DEPOSIT_BALANCE_AMT >= 100000" in _sql("예치금 10만원 이상 회원")


def test_balance_combines_with_member_attribute():
    sql = _sql("적립금 3000원 이상인 30대 여성")
    assert "B.CARROT_BALANCE_AMT >= 3000" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


# ── A그룹: 무구매 ─────────────────────────────────────────────────────
def test_no_purchase_never_bought_phrasing():
    assert "no_purchase" in _plan("한 번도 구매하지 않은 회원")["target_user"]["behaviors"]
    assert "NOT EXISTS" in _sql("한 번도 구매하지 않은 회원")


def test_no_purchase_does_not_contaminate_cart():
    assert _plan("장바구니에 담고 구매 안 한 회원")["target_user"]["behaviors"] == ["cart_abandoner"]


# ── B그룹: 캠페인 반응(MCS_CAMP_MBR_RSPN_FT) ──────────────────────────
@pytest.mark.parametrize("query,predicate", [
    ("최근 캠페인 문자를 받은 회원", "R.CNCT_SCS_YN = 'Y'"),
    ("오퍼에 반응한 회원", "R.OFFR_RSPN_YN = 'Y'"),
    ("캠페인 보고 구매한 회원", "R.BUY_RSPN_YN = 'Y'"),
    ("쿠폰을 사용한 회원", "R.USE_CPN_CNT > 0"),
])
def test_campaign_response(query, predicate):
    sql = _sql(query)
    assert "MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.MBR_NO = B.MEMBER_NO" in sql
    assert predicate in sql


def test_campaign_response_combines_with_member_attribute():
    sql = _sql("쿠폰을 사용한 여성 회원")
    assert "R.USE_CPN_CNT > 0" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


def test_campaign_builder_registered():
    assert g.build_campaign_response_targets_sql_candidate in g._sql_target_builders()
