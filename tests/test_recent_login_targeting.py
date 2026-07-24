"""최근 로그인(recent_login, 긍정형 접속 창) 타겟 회귀.

배경: '최근 6개월 동안 로그인한 30~39세 여성'에서 로그인 최근성 조건이 파서에 없어 경고 없이
탈락하고 성별·연령·정상상태만 SQL 에 남았다. 미접속(inactivity_period)은 부정형 키워드
('미접속/로그인하지 않/휴면')만 잡아 긍정형 '로그인한'을 못 본다.

고정 내용:
  - '최근 N개월/N일 (이내·동안) 로그인·접속한' 이 target_user.recent_login {min_days, sql_interval} 로 잡힌다.
  - compile_member_target_conditions 가 LAST_LOGIN_DATE >= (GETDATE()-N일) 술어를 만들어
    성별/연령과 AND 결합한다 — anchor 기본 getdate(0명이 나와도 요청 기간을 왜곡하지 않는다).
  - 부정형(미접속/로그인하지 않은/휴면)·창 없는 로그인 언급('앱으로 로그인한')·'N개월 전' 은 잡지 않는다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_recent_login_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_recent_months_login_captured():
    tu = _plan("최근 6개월 동안 로그인한 30~39세 여성 회원만 찾아줘")["target_user"]
    assert tu.get("recent_login") == {"value": 6, "unit": "months", "min_days": 180, "sql_interval": "6 months"}
    # 부정형 미접속으로 오분류되면 안 된다.
    assert tu.get("inactivity_period") is None


def test_recent_days_login_captured():
    tu = _plan("최근 90일 이내 접속한 고객")["target_user"]
    assert tu.get("recent_login") == {"value": 90, "unit": "days", "min_days": 90, "sql_interval": "90 days"}


def test_negative_login_stays_inactivity():
    # 부정형은 기존 미접속 트랙 소관 — recent_login 이 뺏어가면 안 된다.
    tu = _plan("6개월 이상 로그인하지 않은 고객")["target_user"]
    assert tu.get("recent_login") is None
    assert tu.get("inactivity_period") is not None
    assert tu["inactivity_period"]["min_days"] == 180


def test_dormant_not_captured():
    tu = _plan("180일 미접속 휴면 고객")["target_user"]
    assert tu.get("recent_login") is None


def test_login_without_window_not_captured():
    # 창 없는 로그인 언급은 최근성 조건이 아니다(로그인 채널 등 다른 트랙 소관).
    tu = _plan("앱으로 로그인한 사용자")["target_user"]
    assert tu.get("recent_login") is None


def test_months_ago_not_captured():
    # 'N개월 전에 로그인한'(과거 시점 언급)은 최근 창으로 잡지 않는다.
    tu = _plan("6개월 전에 로그인한 고객")["target_user"]
    assert tu.get("recent_login") is None


def test_predicate_uses_getdate_anchor_lower_bound():
    pred = g._member_recent_login_predicate(180)
    assert "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -180, GETDATE()), 112)" in pred
    assert "LEN(B.LAST_LOGIN_DATE) = 8" in pred


def test_compile_combines_with_gender_and_age():
    plan = _plan("최근 6개월 동안 로그인한 30~39세 여성 회원만 찾아줘")
    compiled = g.compile_member_target_conditions(plan)
    assert compiled["has_signal"] is True
    assert compiled["unsupported"] == []
    predicates = " AND ".join(compiled["predicates"])
    assert "B.AGE >= 30" in predicates
    assert "B.AGE <= 39" in predicates
    assert "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -180, GETDATE()), 112)" in predicates
    assert "recent_login" in compiled["labels"]


def test_llm_plan_postprocess_sets_recent_login():
    # LLM 경로도 결정론 파서가 recent_login 을 확정한다(플랜 구조만 흉내낸 후처리 직접 호출).
    plan = {"target_user": {}}
    g._apply_recent_login_filter("최근 3개월간 로그인한 고객", plan)
    assert plan["target_user"]["recent_login"]["min_days"] == 90


# ── Stage C(통합 창 파서) 회귀: 숫자 없는 최근 로그인 기본창 + 주 단위 ──────────────────
def test_bare_recent_login_uses_default_window():
    # 숫자 없는 '최근 로그인'(최근성 표지 O)은 기본 창(recently.default_days=30)으로 잡힌다.
    tu = _plan("최근 로그인 했지만 구매 안 한 회원")["target_user"]
    assert tu.get("recent_login") == {"value": 30, "unit": "days", "min_days": 30, "sql_interval": "30 days"}


def test_recent_login_week_unit():
    tu = _plan("최근 2주 로그인한 회원")["target_user"]
    assert tu.get("recent_login") == {"value": 2, "unit": "weeks", "min_days": 14, "sql_interval": "2 weeks"}


def test_bare_login_without_recency_marker_still_none():
    # 최근성 표지 없는 로그인 언급은 기본창을 주지 않는다(기존 동작 보존).
    assert _plan("앱으로 로그인한 사용자")["target_user"].get("recent_login") is None
