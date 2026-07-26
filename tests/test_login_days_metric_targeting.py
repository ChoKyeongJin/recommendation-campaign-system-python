"""로그인 일수(TOTAL_LOGIN_DAYS) 타겟팅 회귀 — 측정 단위 '일' + 최근성 게이트 + 하루 평균(비율).

배경: 로그인 '일수'는 로그인 '횟수'와 같은 numeric_filters 일반화를 타지만 측정 단위가 '회'가 아니라 '일'이라,
숫자와 연산자 사이에 끼는 '30일 이상'을 파싱하려면 항목별 unit('일')이 필요하다. 또 '접속한 날이 10일 미만'
같은 누적 일수 임계는 '최근 10일 이내 로그인'(recent_login)으로 뒤집히면 안 되고('N일+비교연산자' 게이트),
'하루 평균 로그인 횟수'는 총 횟수가 아니라 CNT/DAYS 비율(ratio_filters)로 컴파일돼야 한다. 실패했던 질문을
RAG 문서가 아니라 이 회귀 코퍼스로 관리한다(개선안 5.4/8 원칙). 자매 파일: test_login_metric_targeting.py.

실행: python -m pytest tests/test_login_days_metric_targeting.py -q
"""

import pytest

import graph_rag as g


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(g.build_query_plan(query, parser="rules"))
    assert candidate, f"SQL 후보 생성 실패: {query!r}"
    return candidate.get("sql") or ""


# (질문, SQL 에 포함돼야 하는 술어 조각). '일' 단위 비교/범위/부재/랭킹/평균대비/복합/비율 전 범위.
_CASES = [
    ("누적 로그인 일수가 30일 이상인 회원을 찾아줘.", "B.TOTAL_LOGIN_DAYS >= 30"),
    ("접속한 날이 10일 미만인 고객을 추출해줘.", "B.TOTAL_LOGIN_DAYS < 10"),
    ("누적 로그인 일수가 100일을 초과한 회원을 보여줘.", "B.TOTAL_LOGIN_DAYS > 100"),
    ("로그인한 날이 하루도 없는 회원을 추출해줘.", "COALESCE(B.TOTAL_LOGIN_DAYS, 0) = 0"),
    ("누적 로그인 일수가 가장 많은 고객 50명을 보여줘.", "TOP 50"),
    ("누적 로그인 일수가 평균 이상인 회원을 찾아줘.", "AVG(TOTAL_LOGIN_DAYS)"),
]


@pytest.mark.parametrize("query, expected_fragment", _CASES)
def test_login_days_queries_generate_expected_predicate(query, expected_fragment):
    sql = _sql(query)
    assert "TOTAL_LOGIN_DAYS" in sql, f"로그인 일수 컬럼이 SQL 에 없음: {query!r}\n{sql}"
    assert expected_fragment in sql, f"기대 술어 누락: {expected_fragment!r}\n{sql}"


def test_range_query_emits_both_day_bounds():
    # '7일에서 30일 사이' → 하한+상한 둘 다('일' 단위 range).
    sql = _sql("로그인 일수가 7일에서 30일 사이인 고객을 찾아줘.")
    assert "B.TOTAL_LOGIN_DAYS >= 7" in sql and "B.TOTAL_LOGIN_DAYS <= 30" in sql


def test_recency_does_not_hijack_cumulative_day_threshold():
    # '접속한 날이 10일 미만' 은 최근성(LAST_LOGIN_DATE) 이 아니라 누적 일수 임계여야 한다.
    sql = _sql("접속한 날이 10일 미만인 고객을 추출해줘.")
    assert "LAST_LOGIN_DATE" not in sql, f"누적 일수 임계가 최근성 조건으로 뒤집힘\n{sql}"
    assert "B.TOTAL_LOGIN_DAYS < 10" in sql


def test_recency_window_still_fires_for_genuine_recent_login():
    # 진짜 최근성('최근 30일 이내 로그인')은 여전히 LAST_LOGIN_DATE 최근성으로 잡혀야 한다(게이트 오버킬 방지).
    plan = g.build_query_plan("최근 30일 이내 로그인한 회원을 찾아줘.", parser="rules")
    tu = plan.get("target_user", {})
    assert isinstance(tu.get("recent_login"), dict), "진짜 최근 로그인 창이 사라짐"
    assert not isinstance(tu.get("balance_conditions"), list) or all(
        c.get("column") != "TOTAL_LOGIN_DAYS" for c in tu["balance_conditions"]
    )


def test_compound_days_and_count_emit_both_columns():
    # '일수 30 이상 AND 횟수 100 이상' — '30일'이 옆 절 '100회'로 오염되지 않고 둘 다 정확히.
    sql = _sql("로그인 일수가 30일 이상이고 로그인 횟수가 100회 이상인 회원을 추출해줘.")
    assert "B.TOTAL_LOGIN_DAYS >= 30" in sql and "B.TOTAL_LOGIN_CNT >= 100" in sql


def test_compound_count_and_days_upper_bound_not_dropped():
    # '횟수 100 이상이지만 일수 10 이하' — 일수 상한이 조용히 드롭되지 않는다.
    sql = _sql("로그인 횟수는 100회 이상이지만 로그인 일수는 10일 이하인 고객을 찾아줘.")
    assert "B.TOTAL_LOGIN_CNT >= 100" in sql and "B.TOTAL_LOGIN_DAYS <= 10" in sql


def test_daily_average_login_is_ratio_not_total_count():
    # '하루 평균 로그인 횟수 3회 이상' = CNT/DAYS 비율(분모 0 은 NULLIF), 총 횟수(CNT>=3)가 아니다.
    sql = _sql("하루 평균 로그인 횟수가 3회 이상인 회원을 보여줘.")
    assert "CAST(B.TOTAL_LOGIN_CNT AS FLOAT) / NULLIF(B.TOTAL_LOGIN_DAYS, 0) >= 3" in sql
    assert "B.TOTAL_LOGIN_CNT >= 3" not in sql, f"비율이 총 횟수 임계로 오탐됨\n{sql}"


def test_zero_uses_coalesce_for_nullable_day_column():
    # nullable 컬럼이라 =0 은 COALESCE 로 NULL 회원을 포함해야 한다.
    sql = _sql("로그인한 날이 하루도 없는 회원을 추출해줘.")
    assert "COALESCE(B.TOTAL_LOGIN_DAYS, 0) = 0" in sql


# ── 선언형 계약: 일수 항목이 unit/action/zero 를, 비율 항목이 numerator/denominator 를 선언한다 ──────────
def test_login_days_entry_declares_action_zero():
    # P1(단위) 이후: '일' 단위는 numeric_filters 가 아니라 통합 레지스트리(units)가 소유한다.
    # action_aliases/zero_semantics 는 아직 numeric_filters 에 남아 있다(다음 단계에서 이관).
    entry = next(
        (e for e in g._numeric_metric_filters() if e.get("canonical") == "total_login_days"),
        None,
    )
    assert entry is not None, "total_login_days 가 numeric_metric_filters 에 없음"
    assert entry.get("action_aliases"), "action_aliases 선언 누락"
    assert isinstance(entry.get("zero_semantics"), dict) and entry["zero_semantics"].get("missing_as_zero")


def test_login_days_unit_resolves_from_registry():
    # 측정 단위 '일'의 유일 소스는 이제 docs/data/metrics/total_login_days.json(레지스트리)다.
    # _metric_window_grammar 가 그 units 를 읽어 '일'로 해석해야 '30일 이상'이 파싱된다.
    entry = next(
        e for e in g._numeric_metric_filters() if e.get("canonical") == "total_login_days"
    )
    unit, bare_equals = g._metric_window_grammar(entry)
    assert unit == "일" and bare_equals is False
    assert entry.get("unit") is None, "단위 정의는 numeric_filters 에서 제거돼야 한다(레지스트리로 이관)"


def test_daily_avg_ratio_entry_declares_numerator_denominator():
    entry = next(
        (e for e in g._ratio_metric_filters() if e.get("canonical") == "daily_avg_login_count"),
        None,
    )
    assert entry is not None, "daily_avg_login_count 가 ratio_filters 에 없음"
    assert entry.get("numerator_column") == "B.TOTAL_LOGIN_CNT"
    assert entry.get("denominator_column") == "B.TOTAL_LOGIN_DAYS"
