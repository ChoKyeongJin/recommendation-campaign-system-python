"""통합 창 파서(_parse_duration_window) 회귀 (Stage C — 파서 역할 재분배).

배경: 시간창 파서가 슬롯마다 파편화(가입/로그인/미구매/미접속)돼 각자 다른 단위 부분집합만 지원했다
— 가입·로그인 파서는 '년'을 몰라 "1년 이내 가입"을 놓치고, 로그인 파서는 숫자를 강제해 "최근 로그인"을
못 잡았다. 이제 숫자형(3개월/2주/1년)·단어형(일주일/반년/한달)을 모두 잡는 통합 파서로 일원화한다.
각 슬롯의 문맥 게이트(가입 신호/로그인 신호/부정어)는 유지하고 숫자+단위 추출만 공유한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_duration_window_unification.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# ── 통합 파서 단위 커버리지 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,min_days,unit", [
    ("1년", 365, "years"),
    ("2주", 14, "weeks"),
    ("3개월", 90, "months"),
    ("90일", 90, "days"),
    ("일주일", 7, "days"),
    ("반년", 180, "days"),
    ("한달", 30, "days"),
])
def test_duration_parser_units(text, min_days, unit):
    out = g._parse_duration_window(text)
    assert out is not None and out["min_days"] == min_days


def test_duration_parser_exclude_past():
    assert g._parse_duration_window("3개월 전에", exclude_past=True) is None
    assert g._parse_duration_window("3개월 이내", exclude_past=True)["min_days"] == 90


def test_duration_parser_default_when_no_number():
    assert g._parse_duration_window("최근", require_number=False, default_days=30)["min_days"] == 30
    assert g._parse_duration_window("최근") is None  # 기본값 미지정이면 None


# ── 가입 창: '1년 이내 가입'(예전 년 미지원으로 놓치던 케이스) ─────────────────────────
def test_signup_year_window_rules_path():
    tu = _plan("최근 1년 이내 가입한 회원")["target_user"]
    assert tu.get("signup_target") == {"days": 365}


def test_signup_word_window():
    tu = _plan("한달 이내 가입한 고객")["target_user"]
    assert tu.get("signup_target") == {"days": 30}


def test_signup_signal_without_window_uses_default():
    # '신규가입'(창 없음)은 days=None → compile 이 기본창을 쓴다(기존 동작 보존).
    tu = _plan("신규가입 회원")["target_user"]
    assert tu.get("signup_target") == {"days": None}


# ── 미구매 창: 년/단어형 확장 ─────────────────────────────────────────────────────────
def test_purchase_inactivity_half_year():
    tu = _plan("반년 이상 미구매 회원")["target_user"]
    assert tu.get("purchase_inactivity") == {"value": 180, "unit": "days", "min_days": 180}


def test_purchase_inactivity_year():
    tu = _plan("1년 이상 구매하지 않은 회원")["target_user"]
    assert tu.get("purchase_inactivity") == {"value": 1, "unit": "years", "min_days": 365}
