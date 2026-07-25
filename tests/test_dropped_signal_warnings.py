"""③ 놓침을 시끄럽게: 결정론 드롭 감지(_deterministic_dropped_conditions) 회귀.

원문에 정밀 추출된 신호(성별·수신동의·캠페인 반응·최근 로그인)가 최종 plan 슬롯에 하나도 안 잡히면
경고를 낸다. LLM 의미검증 게이트(auto 전용)의 보완재로, rules/auto 양쪽에서 항상 도는 비차단 자문.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_dropped_signal_warnings.py -q
"""

import graph_rag as g


def _warn(query: str, target_user: dict, exclude: dict | None = None) -> list[str]:
    plan = {"target_user": target_user, "exclude": exclude or {}}
    return g._deterministic_dropped_conditions(query, plan)


def test_recent_login_drop_is_flagged():
    assert "최근 로그인/접속 조건" in _warn("최근 로그인한 여성", {"gender": "female"})


def test_recent_login_present_no_warning():
    assert _warn("최근 로그인한 여성", {"gender": "female", "recent_login": {"min_days": 30}}) == []


def test_gender_drop_is_flagged():
    assert any("여성" in w for w in _warn("30대 여성 회원", {"age_min": 30}))


def test_gender_via_exclude_not_flagged():
    assert _warn("남성 제외 회원", {}, exclude={"gender": ["male"]}) == []


def test_campaign_response_captured_as_negated_not_flagged():
    # plan 이 부정 트랙(no_buy_response)으로 잡았으면 원문 '구매 반응' 신호는 보존으로 본다.
    plan_tu = {"campaign_responses": [{"canonical": "no_buy_response", "negated": True}]}
    assert _warn("캠페인 구매 이력이 없는 회원", plan_tu) == []


def test_campaign_response_drop_is_flagged():
    assert any("캠페인 반응" in w for w in _warn("쿠폰을 사용한 회원", {}))


def test_negated_login_phrasing_is_not_recent_login():
    # '미접속/휴면'은 최근 로그인(긍정) 드롭이 아니다(오탐 방지).
    assert "최근 로그인/접속 조건" not in _warn("최근 미접속 회원", {})


def test_fully_captured_query_has_no_warnings():
    # 앞 턴에서 고친 실제 쿼리: 모든 조건이 잡혀 경고가 없어야 한다(수정 회귀 겸용).
    plan_tu = {
        "gender": "female", "age_min": 30, "age_max": 39,
        "recent_login": {"min_days": 30},
        "campaign_responses": [{"canonical": "no_buy_response", "negated": True}],
    }
    q = "서울에 거주하는 30대 여성 중 최근 로그인은 했지만 최근 캠페인 구매 이력이 없는 회원"
    assert _warn(q, plan_tu) == []


def test_purchase_inactivity_drop_is_flagged():
    assert any("구매 미발생" in w for w in _warn("최근 90일 이내 구매가 없는 회원", {}))


def test_purchase_inactivity_captured_not_flagged():
    assert _warn("최근 90일 이내 구매가 없는 회원", {"purchase_inactivity": {"min_days": 90}}) == []


def test_campaign_buy_negation_not_flagged_as_purchase_inactivity():
    # '캠페인 구매 반응 없음'은 no_buy_response 로 잡히면 전체 미구매 드롭 경고가 아니다.
    assert _warn("캠페인 구매 이력이 없는 회원", {"campaign_responses": [{"canonical": "no_buy_response"}]}) == []


def test_cart_drop_is_flagged():
    assert "장바구니 조건" in _warn("장바구니를 보유한 회원", {})


def test_cart_captured_not_flagged():
    assert "장바구니 조건" not in _warn("장바구니를 보유한 회원", {"behaviors": ["cart_abandoner"]})


def test_cart_absence_not_flagged():
    assert _warn("장바구니가 없는 회원", {"cart_absence": True}) == []


def test_grade_cart_inactivity_all_dropped_flags_both():
    # 이번 사례: 카트·미구매가 둘 다 조용히 드롭되면 둘 다 경고한다.
    warns = _warn("GOLD 이상 회원 중 장바구니를 보유하고 최근 90일 이내 구매가 없는 회원", {})
    assert any("구매 미발생" in w for w in warns)
    assert "장바구니 조건" in warns


def test_empty_query_is_safe():
    assert g._deterministic_dropped_conditions("", {"target_user": {}}) == []
