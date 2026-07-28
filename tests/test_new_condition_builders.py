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


def test_balance_exact_equals():
    # "예치금 잔액이 0원인" = 등호. 연산자어(이상/이하)가 없어 임계값 패턴이 못 잡고 조건이 통째로
    # 드롭돼 '타겟 조건 인식(1/6)'에서 막히던 사례. 등호 폴백으로 '= N' 방출.
    assert "B.DEPOSIT_BALANCE_AMT = 0" in _sql("예치금 잔액이 0원인 회원을 찾아줘")
    assert "B.DEPOSIT_BALANCE_AMT = 0" in _sql("예치금 잔액 0원 회원")  # 재작성형(조사 탈락)
    assert "B.CARROT_BALANCE_AMT = 5000" in _sql("적립금 5000원인 회원")


def test_balance_presence_and_absence():
    # 숫자 없는 존재/부재형. "보유/있는" → > 0, "없는/미보유" → = 0. 숫자만 보던 파서가 못 잡아
    # 1/6 에서 막히던 사례.
    assert "B.DEPOSIT_BALANCE_AMT > 0" in _sql("예치금을 보유한 회원을 추출해줘")
    assert "B.DEPOSIT_BALANCE_AMT > 0" in _sql("예치금이 있는 회원")
    assert "B.DEPOSIT_BALANCE_AMT = 0" in _sql("예치금 없는 회원")
    # '미보유'가 '보유'를 포함하지만 부재로 정확히 갈린다.
    assert "B.DEPOSIT_BALANCE_AMT = 0" in _sql("예치금 미보유 회원")


def test_balance_verb_form_operator_not_misread_as_equals():
    # '50,000원을 초과하는' 동사형 부등호. 부사형만 보던 파서가 등호로 오분류(= 50000)하던 회귀.
    assert "B.DEPOSIT_BALANCE_AMT > 50000" in _sql("예치금 잔액이 50,000원을 초과하는 회원을 찾아줘")
    assert "B.DEPOSIT_BALANCE_AMT = 50000" not in _sql("예치금 잔액이 50,000원을 초과하는 회원을 찾아줘")
    assert "B.DEPOSIT_BALANCE_AMT < 50000" in _sql("예치금이 50,000원 미만인 회원")


def test_balance_range_becomes_between():
    # 'A원에서 B원 사이' 범위. 등호 폴백이 첫 숫자만 '= A'로 잡던 회귀 → >=A AND <=B.
    sql = _sql("예치금이 10,000원에서 50,000원 사이인 회원을 보여줘")
    assert "B.DEPOSIT_BALANCE_AMT >= 10000" in sql and "B.DEPOSIT_BALANCE_AMT <= 50000" in sql


def test_balance_comparison_phrase_and_negation():
    # 공용 비교 문법: 'N원보다 많은/적은' 비교형과 '보유하지 않은' 부정형이 등호/존재로 오폴백되던 회귀.
    assert "B.CARROT_BALANCE_AMT > 0" in _sql("적립금이 0원보다 많은 회원을 찾아줘")
    assert "B.CARROT_BALANCE_AMT = 0" in _sql("적립금을 보유하지 않은 고객을 찾아줘")
    assert "B.CARROT_BALANCE_AMT > 10000" in _sql("적립금이 10,000원을 초과한 회원을 보여줘")  # 동사형


def test_balance_column_to_column_comparison():
    # '적립금이 예치금보다 많은' = 두 잔액 컬럼 직접 비교(숫자 임계 아님).
    sql = _sql("적립금이 예치금보다 많은 회원을 보여줘")
    assert "B.CARROT_BALANCE_AMT > B.DEPOSIT_BALANCE_AMT" in sql


def test_shared_comparison_grammar_is_unit_agnostic():
    # 공용 문법은 단위만 바꿔 재사용된다(원/세/회 …) — 도메인별 재구현 없이.
    assert g._parse_amount_comparison("50,000원을 초과하는", "원") == [(">", 50000.0)]
    assert g._parse_amount_comparison("0원보다 많은", "원") == [(">", 0.0)]
    assert g._parse_amount_comparison("3천원에서 2만원 사이", "원") == [(">=", 3000.0), ("<=", 20000.0)]
    assert g._parse_amount_comparison("40세 이상", "세") == [(">=", 40.0)]
    assert g._parse_amount_comparison("5회 이하", "회|건|개|번") == [("<=", 5.0)]
    # bare_equals=False 면 연산자 없는 맨 숫자는 등호로 넘겨짚지 않는다(모호형 보호).
    assert g._parse_amount_comparison("3회", "회|건|개|번") is None
    assert g._parse_amount_comparison("0원", "원", bare_equals=True) == [("=", 0.0)]


def test_balance_ranking_and_stat_forms_defer_not_misfire():
    # 랭킹/퍼센타일/평균은 WHERE 임계로 표현 못 하므로 잔액 파서가 소유하지 않는다 —
    # 틀린 조건(> 0, = N)을 내지 말고 balance_conditions 를 비운다('보유액'의 '보유' 오탐 포함).
    for q in ["예치금이 가장 많은 회원 100명을 추출해줘",
              "예치금 보유액 기준 상위 5% 회원을 찾아줘",
              "예치금 잔액이 평균보다 높은 고객을 보여줘"]:
        plan = g.build_query_plan(q, parser="rules")
        assert not plan["target_user"].get("balance_conditions"), q


# ── A그룹: 무구매 ─────────────────────────────────────────────────────
def test_no_purchase_never_bought_phrasing():
    assert "no_purchase" in _plan("한 번도 구매하지 않은 회원")["target_user"]["behaviors"]
    assert "NOT EXISTS" in _sql("한 번도 구매하지 않은 회원")


def test_no_purchase_does_not_contaminate_cart():
    assert _plan("장바구니에 담고 구매 안 한 회원")["target_user"]["behaviors"] == ["cart_abandoner"]


# ── '쿠폰 사용 후 추가 구매 없는' = 쿠폰 EXISTS + 실주문 자체가 없음(anti-join) ────────
@pytest.mark.parametrize("query", [
    "추가 구매 없는 회원",
    "추가로 구매하지 않은 회원",
    "더 이상 구매 안 한 고객",
])
def test_additional_purchase_absence_maps_to_no_purchase(query):
    assert "no_purchase" in _plan(query)["target_user"]["behaviors"], query


def test_coupon_then_no_additional_purchase_combines():
    # '쿠폰 사용 후 추가 구매 없는': 쿠폰 사용 EXISTS(캠페인 반응)와 실주문 없음(anti-join)이 한 SQL 에
    # AND 결합돼야 한다 — 예전엔 '추가 구매 없는'이 통째로 드롭돼 쿠폰 EXISTS 만 남던 버그 회귀 방지.
    q = "쿠폰 사용 후 추가 구매 없는 회원"
    tu = _plan(q)["target_user"]
    assert "no_purchase" in tu["behaviors"]
    assert any(r["canonical"] == "coupon_used" for r in tu.get("campaign_responses") or [])
    sql = _sql(q)
    assert "EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.USE_CPN_CNT > 0" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in sql


def test_repurchase_negation_is_not_no_purchase():
    # '재구매하지 않은'(과거 구매는 있고 재구매만 없음)은 '실주문 자체가 없음'과 어의가 달라 no_purchase 로
    # 승격하지 않는다 — 실주문 전무 anti-join 으로 오분류되면 과거 구매자를 통째로 배제하는 오류.
    assert "no_purchase" not in (_plan("재구매하지 않은 고객")["target_user"].get("behaviors") or [])


# ── B그룹: 캠페인 반응(MCS_CAMP_MBR_RSPN_FT) ──────────────────────────
@pytest.mark.parametrize("query,predicate", [
    ("오퍼에 반응한 회원", "R.OFFR_RSPN_YN = 'Y'"),
    ("캠페인 보고 구매한 회원", "R.BUY_RSPN_YN = 'Y'"),
    ("쿠폰을 사용한 회원", "R.USE_CPN_CNT > 0"),
])
def test_campaign_response(query, predicate):
    sql = _sql(query)
    assert "MCS_CAMP_MBR_RSPN_FT R" in sql
    # MBR_NO(문자열)↔MEMBER_NO(숫자) 타입 불일치 가드를 통과하려면 캐스트 조인이어야 한다.
    assert "TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql
    assert predicate in sql


def test_campaign_contact_sources_member_list():
    # 접촉(발송) 성공의 소스는 반응 팩트가 아니라 셀 발송 대상 명단(Z_CAMP_MBR)이다 — 반응 팩트는
    # 반응자 중심 적재라(데모는 구매반응자뿐) '발송 성공 & 구매반응 없음'이 구조적으로 공집합이 됐다.
    sql = _sql("최근 캠페인 문자를 받은 회원")
    assert "Z_CAMP_MBR M" in sql
    assert "TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql
    assert "M.CONTAC_SUCC_YN = 'Y'" in sql


def test_campaign_response_combines_with_member_attribute():
    sql = _sql("쿠폰을 사용한 여성 회원")
    assert "R.USE_CPN_CNT > 0" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


def test_campaign_builder_registered():
    assert g.build_campaign_response_targets_sql_candidate in g._sql_target_builders()


# ── 발송 성공(접촉 성공) 표면어 + 조합 회귀 ─────────────────────────────
@pytest.mark.parametrize("query", [
    "발송에 성공한 회원",
    "발송은 성공했지만 반응 없는 회원",
    "전송 성공한 회원",
    "캠페인에서 발송은 성공했지만 구매하지 않은 회원만 보여줘.",
])
def test_send_success_maps_to_contact_success(query):
    # '발송 성공/전송 성공'(조사 포함)은 접촉 성공(발송 명단 CONTAC_SUCC_YN='Y')으로 컴파일돼야 한다 —
    # 리터럴 표면어만 나열하던 파서가 이 표현을 놓쳐 조건이 통째로 새던 버그 방지.
    plan = _plan(query)
    responses = plan["target_user"].get("campaign_responses") or []
    assert any(r["canonical"] == "campaign_contact" for r in responses), query
    assert "M.CONTAC_SUCC_YN = 'Y'" in _sql(query)


def test_contact_success_frequency_with_no_buy_response_campaign():
    query = "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 재반응 유도 캠페인을 만들어줘."
    plan = _plan(query)
    frequency = plan["target_user"]["campaign_response_frequency"]
    responses = plan["target_user"].get("campaign_responses") or []

    assert frequency["event"] == "campaign_contact"
    assert frequency["operator"] == ">="
    assert frequency["count"] == 3
    assert any(response.get("canonical") == "no_buy_response" for response in responses)

    sql = _sql(query)
    assert "FROM Z_CAMP_MBR M" in sql
    assert "M.CONTAC_SUCC_YN = 'Y'" in sql
    assert "M.CELL_TYPE_CD = 'T'" in sql
    assert "COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) >= 3" in sql
    assert "NOT EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    assert "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')" not in sql


def test_campaign_send_success_combines_with_no_purchase():
    # '발송 성공 + 무구매' 조합: 캠페인 접촉 성공 EXISTS 와 무구매 anti-join 이 둘 다 남아야 한다.
    # 전용 캠페인 빌더가 no_purchase 를 조용히 버리던(또는 그 반대) 버그 회귀 방지.
    plan = {
        "intent": "recommend_campaign",
        "target_user": {
            "behaviors": ["no_purchase"],
            "campaign_responses": [{"canonical": "campaign_contact", "predicate": "R.CNCT_SCS_YN = 'Y'"}],
        },
        "campaign_constraints": {"objective": "purchase"},
    }
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    sql = cand["sql"]
    assert "EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.CNCT_SCS_YN = 'Y'" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in sql
