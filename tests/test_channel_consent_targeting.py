"""채널 수신동의(consent) 타겟 회귀.

배경: '앱푸시 수신에 동의한 VIP 회원'에서 '앱푸시'가 발송 채널 어휘(app_push)로만 매칭돼
preferred_channels 로 들어가고, 선호 채널은 실DB(CRM_MB_BASEINFO) 미지원이라 dropped 로 조용히
탈락했다. 실컬럼 APP_PUSH_YN(nchar(1) 'Y'/'N', CRMDW 실값 확인)이 있는데도 매핑이 없었다.

고정 내용:
  - '<채널> 수신(에) 동의한' 은 consent canonical(lifecycle)로 승격돼 eq_filters 가 `= 'Y'` 컴파일.
  - 동의 문맥의 채널어는 preferred_channels/campaign channels 에서 제거된다(dropped 방지).
  - '수신 거부/미동의/동의하지 않' 은 exclude.lifecycle 로 `<> 'Y'` 컴파일.
  - 동의 문맥 없는 채널 언급('앱푸시로 홍보')은 기존 채널 트랙 그대로(consent 미승격).
  - 컬럼 소유: member_target_filters.json eq_filters consent 카테고리(APP_PUSH_YN/SMS_YN/EMAIL_YN/AGREE_YN).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_channel_consent_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_app_push_consent_promoted_from_channel():
    plan = _plan("서울 강남구에 거주하면서 앱푸시 수신에 동의한 VIP 회원만 보여줘.")
    tu = plan["target_user"]
    assert "app_push_optin" in tu["lifecycle"]
    # 동의 문맥의 채널어는 채널 트랙에서 제거된다(미지원 dropped 방지).
    assert "app_push" not in tu["preferred_channels"]
    assert "app_push" not in plan["campaign_constraints"]["channels"]


def test_app_push_consent_sql_contains_yn_predicate():
    plan = _plan("서울 강남구에 거주하면서 앱푸시 수신에 동의한 VIP 회원만 보여줘.")
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    assert "B.APP_PUSH_YN = 'Y'" in cand["sql"]
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in cand["sql"]
    assert "B.SIGUNGU IN ('강남구')" in cand["sql"]
    # 수신동의가 조건으로 반영됐으므로 채널 탈락(dropped)이 없어야 한다.
    assert not cand.get("dropped_conditions")


def test_sms_email_marketing_consent_columns():
    sms = g.compile_member_target_conditions(_plan("SMS 수신 동의한 고객"))
    assert any("B.SMS_YN = 'Y'" in p for p in sms["predicates"])
    email = g.compile_member_target_conditions(_plan("이메일 수신동의 회원"))
    assert any("B.EMAIL_YN = 'Y'" in p for p in email["predicates"])
    marketing = g.compile_member_target_conditions(_plan("마케팅 동의한 회원"))
    assert any("B.AGREE_YN = 'Y'" in p for p in marketing["predicates"])


def test_consent_refusal_becomes_exclusion():
    plan = _plan("앱푸시 수신 거부한 고객")
    assert "app_push_optin" in plan["exclude"]["lifecycle"]
    assert "app_push_optin" not in plan["target_user"]["lifecycle"]
    compiled = g.compile_member_target_conditions(plan)
    assert any("B.APP_PUSH_YN <> 'Y'" in p for p in compiled["predicates"])


def test_consent_negation_phrasings():
    for query in ("앱푸시 수신에 동의하지 않은 고객", "푸시 수신 미동의 고객", "앱푸시 동의 안 한 회원"):
        plan = _plan(query)
        assert "app_push_optin" in plan["exclude"]["lifecycle"], query
        assert "app_push_optin" not in plan["target_user"]["lifecycle"], query


def test_channel_without_consent_context_untouched():
    # 동의 문맥이 없으면 기존 채널 트랙 그대로 — consent 로 승격하지 않는다.
    plan = _plan("VIP 회원에게 앱푸시로 홍보 보내줘")
    assert "app_push_optin" not in plan["target_user"]["lifecycle"]
    assert "app_push" in plan["campaign_constraints"]["channels"]


def test_consent_combines_with_recent_login():
    # 다른 결정론 필터와 AND 결합되는지(수신동의 + 최근 로그인 창).
    plan = _plan("최근 3개월간 로그인했고 앱푸시 수신에 동의한 여성 회원")
    compiled = g.compile_member_target_conditions(plan)
    predicates = " AND ".join(compiled["predicates"])
    assert "B.APP_PUSH_YN = 'Y'" in predicates
    assert "B.LAST_LOGIN_DATE >=" in predicates
