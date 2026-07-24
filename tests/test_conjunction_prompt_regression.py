"""조건 나열형 복합 프롬프트 회귀 — 3중 소실로 no_verified_sql 이 나던 사례 고정.

배경 프롬프트: "최근 6개월 동안 로그인한 적이 있고, 서울 또는 경기 거주이며, 30~49세 여성이고,
앱푸시와 SMS 수신에 모두 동의했으며, 장바구니에 일반상품을 2개 이상 담아두었지만 최근 캠페인에서는
구매하지 않은 회원만 찾아줘." 가 실API 에서 no_verified_sql 로 실패했다. 원인 3중:
  (1) 수신동의 접미어가 '수신에 모두 동의'(조사 뒤 부사) 어순을 허용하지 않아 동의 승격 실패 →
      채널어가 선호/발송 채널로 남아 미지원 거부의 직접 원인.
  (2) 정규화 조사 목록에 연결어미 '이고/이며'가 없어 '여성이고'가 매칭 실패 → 성별 소실.
  (3) 캠페인 구매반응 '부정' 트랙 부재 → '캠페인에서는 구매하지 않은'이 전체 주문 미구매
      (purchase_inactivity, 창은 로그인 절 '최근 6개월'을 탈취)로 오배정.
고친 내용: 동의 접미어 부사 허용, KOREAN_PARTICLES 에 이고/이며/이면서 추가, campaign_responses
negated(NOT EXISTS) 트랙 + 캠페인 문맥 유일 부정 시 purchase_inactivity/no_purchase 정리.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_conjunction_prompt_regression.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


FULL_PROMPT = (
    "최근 6개월 동안 로그인한 적이 있고, 서울 또는 경기 거주이며, 30~49세 여성이고, "
    "앱푸시와 SMS 수신에 모두 동의했으며, 장바구니에 일반상품을 2개 이상 담아두었지만 "
    "최근 캠페인에서는 구매하지 않은 회원만 찾아줘."
)


# ── (1) 수신동의: '수신에 모두 동의' 어순 ─────────────────────────────────────────────
def test_consent_adverb_between_particle_and_verb():
    tu = _plan("앱푸시와 SMS 수신에 모두 동의한 회원")["target_user"]
    assert "app_push_optin" in tu["lifecycle"]
    assert "sms_optin" in tu["lifecycle"]
    assert "app_push" not in tu.get("preferred_channels", [])
    assert "sms" not in tu.get("preferred_channels", [])


def test_consent_refusal_with_adverb():
    plan = _plan("앱푸시와 SMS 수신을 모두 거부한 회원")
    assert "app_push_optin" in plan["exclude"]["lifecycle"]
    assert "sms_optin" in plan["exclude"]["lifecycle"]


# ── (2) 연결어미 뒤 회원속성: '여성이고' ─────────────────────────────────────────────
def test_gender_survives_connective_ending():
    tu = _plan("30~49세 여성이고 서울 거주인 회원")["target_user"]
    assert tu.get("gender") == "female"
    assert tu.get("age_min") == 30 and tu.get("age_max") == 49


# ── (3) 캠페인 구매반응 부정: NOT EXISTS + 오배정 정리 ──────────────────────────────
def test_campaign_no_buy_parsed_as_negated_response():
    tu = _plan("최근 캠페인에서는 구매하지 않은 회원")["target_user"]
    negated = [r for r in tu.get("campaign_responses", []) if r.get("negated")]
    assert negated and negated[0]["canonical"] == "no_buy_response"
    # 긍정 구매반응이 부정문 부분문자열('캠페인…구매')로 오탐하지 않는다.
    assert not [r for r in tu.get("campaign_responses", []) if r.get("canonical") == "buy_response"]
    # 전체 주문 미구매로 오배정되지 않는다(창 탈취 방지).
    assert not tu.get("purchase_inactivity")
    assert "no_purchase" not in tu.get("behaviors", [])


def test_campaign_no_buy_compiles_to_not_exists():
    compiled = g.compile_member_target_conditions(_plan("캠페인에서 구매하지 않은 회원"))
    assert any(
        p.startswith("NOT EXISTS") and "MCS_CAMP_MBR_RSPN_FT" in p and "R.BUY_RSPN_YN = 'Y'" in p
        for p in compiled["predicates"]
    )


def test_generic_no_purchase_untouched():
    # 캠페인 문맥 없는 전체 미구매는 기존 트랙 유지(회귀 방지).
    tu = _plan("구매 이력이 없는 고객")["target_user"]
    assert "no_purchase" in tu.get("behaviors", [])
    assert not [r for r in tu.get("campaign_responses", []) if r.get("negated")]


def test_both_negations_keep_order_track():
    # 캠페인 부정과 별개의 전체 미구매가 함께 오면 주문 트랙을 지우지 않는다.
    tu = _plan("최근 90일 구매하지 않았고 캠페인에서도 구매하지 않은 회원")["target_user"]
    assert tu.get("purchase_inactivity")
    assert [r for r in tu.get("campaign_responses", []) if r.get("negated")]


def test_contact_success_plus_no_purchase_behavior_preserved():
    # 문서화된 기존 동작: '발송 성공 후 미구매'(캠페인-구매 비인접)는 전체 무주문 트랙 유지.
    tu = _plan("캠페인 발송 성공 후 미구매 회원")["target_user"]
    assert "no_purchase" in tu.get("behaviors", [])
    assert not [r for r in tu.get("campaign_responses", []) if r.get("negated")]


# ── 어순 무관 '구매반응' 부정: 발송 절이 캠페인과 구매 사이에 끼는 프롬프트 ─────────────
GRADE_CAMPAIGN_CART_PROMPT = (
    "GOLD 등급 이상 회원 중 최근 캠페인 발송에 성공했지만 구매 반응이 없는 회원, "
    "장바구니에 상품이 있으며 블랙리스트가 아닌 활동 회원"
)


def test_rspn_negation_without_campaign_adjacency():
    tu = _plan("캠페인 발송에 성공했지만 구매 반응이 없는 회원")["target_user"]
    canonicals = {r.get("canonical"): r for r in tu.get("campaign_responses", [])}
    assert "campaign_contact" in canonicals
    assert canonicals.get("no_buy_response", {}).get("negated") is True
    # 부정문 부분문자열('구매반응')로 긍정 구매반응이 정반대 EXISTS 로 서지 않는다.
    assert "buy_response" not in canonicals


def test_rspn_negation_prunes_no_purchase():
    plan = {"target_user": {"behaviors": ["no_purchase"]}}
    g._apply_campaign_response_filter("캠페인 발송에 성공했지만 구매 반응이 없는 회원", plan)
    assert plan["target_user"]["behaviors"] == []


def test_positive_buy_response_still_matches():
    tu = _plan("구매 반응이 있는 회원")["target_user"]
    canonicals = {r.get("canonical") for r in tu.get("campaign_responses", [])}
    assert "buy_response" in canonicals and "no_buy_response" not in canonicals


def test_separate_generic_negation_keeps_order_track():
    # 캠페인 부정과 별개의 전체 미구매(스팬 비겹침)는 주문 트랙 유지.
    tu = _plan("최근 90일 구매하지 않았고 구매 반응도 없는 회원")["target_user"]
    assert tu.get("purchase_inactivity")
    assert [r for r in tu.get("campaign_responses", []) if r.get("negated")]


# ── 장바구니 '존재' 표현 승격 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", [
    "장바구니에 상품이 있으며 블랙리스트가 아닌 활동 회원",
    "장바구니에 상품을 담아둔 회원",
    "장바구니에 담은 상품이 있는 고객",
])
def test_cart_presence_promoted(query):
    assert "cart_abandoner" in _plan(query)["target_user"].get("behaviors", []), query


def test_cart_presence_negative_not_promoted():
    assert "cart_abandoner" not in _plan("장바구니에 담지 않은 회원")["target_user"].get("behaviors", [])


def test_grade_campaign_cart_prompt_builds_correct_sql():
    plan = _plan(GRADE_CAMPAIGN_CART_PROMPT)
    tu = plan["target_user"]
    assert "cart_abandoner" in tu.get("behaviors", [])
    canonicals = {r.get("canonical"): r for r in tu.get("campaign_responses", [])}
    assert "campaign_contact" in canonicals and canonicals["no_buy_response"]["negated"] is True

    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    sql = cand["sql"]
    # 카트 보관 + 등급 서열 + 플래그 + 접촉성공(발송 명단) EXISTS + 구매반응 NOT EXISTS 전부 반영.
    assert "ODS_MALL_OMS_CART" in sql
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in sql
    assert "B.ACTIVITY_MEMBER_YN = 'Y'" in sql
    assert "B.BLACKLIST_YN <> 'Y'" in sql
    assert "EXISTS (SELECT 1 FROM Z_CAMP_MBR M" in sql
    assert "AND M.CONTAC_SUCC_YN = 'Y')" in sql
    assert "NOT EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "AND R.BUY_RSPN_YN = 'Y')" in sql
    # 전체 주문 미구매 anti-join 으로 새지 않는다.
    assert "CRM_SL_ORDERHEADERMALL" not in sql


# ── 종합: 원 프롬프트가 모든 조건을 실은 SQL 로 컴파일된다 ───────────────────────────
def test_full_prompt_builds_sql_with_all_conditions():
    plan = _plan(FULL_PROMPT)
    tu = plan["target_user"]
    assert tu.get("gender") == "female"
    assert "app_push_optin" in tu["lifecycle"] and "sms_optin" in tu["lifecycle"]
    assert [r for r in tu.get("campaign_responses", []) if r.get("negated")]
    assert not tu.get("purchase_inactivity")

    cand = g.build_sql_template_candidate(plan)
    assert cand is not None, "SQL 미생성"
    sql = cand["sql"]
    # 장바구니(일반상품 2개 이상) 빌더 기반 + 나머지 조건 전부 AND 결합.
    assert "CART_TYPE_CD = 'CART_TYPE_CD.GENERAL'" in sql
    assert ">= 2" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.AGE >= 30" in sql and "B.AGE <= 49" in sql
    assert "B.SIDO IN (" in sql
    assert "B.LAST_LOGIN_DATE >=" in sql
    assert "B.APP_PUSH_YN = 'Y'" in sql
    assert "B.SMS_YN = 'Y'" in sql
    assert "NOT EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    # 전체 주문 미구매 anti-join 으로 새지 않는다.
    assert "CRM_SL_ORDERHEADERMALL" not in sql
