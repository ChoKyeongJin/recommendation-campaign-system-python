"""로그인 횟수(TOTAL_LOGIN_CNT) 타겟팅 회귀 — 명사형 + 동사형(action_aliases) 전 표현.

배경: 수치 회원 지표를 numeric_filters 의 type 구동으로 일반화(_numeric_metric_filters)하고, 동사형 표현
('한 번도 로그인하지 않은/정확히 20번 로그인한/평균보다 많이 로그인')은 action_aliases + 날짜 게이트로
지표에 연결한다. 실패했던 질문을 RAG 문서가 아니라 이 회귀 코퍼스로 관리한다(개선안 5.4/8 원칙).

핵심 계약:
  * 비교/범위/정확값/이중경계 → balance_conditions (B.TOTAL_LOGIN_CNT <op> N)
  * 랭킹/상위%/평균대비 → member_metric_selection (TOP / TOP % / vs AVG, NULL 제외)
  * 부재('한 번도 …') → COALESCE(B.TOTAL_LOGIN_CNT, 0) = 0 (nullable 컬럼이라 NULL 회원 포함)
  * '로그인한 지 30일' 같은 날짜/최근성 조건은 count 로 오탐하지 않는다(게이트)

실행: python -m pytest tests/test_login_metric_targeting.py -q
"""

import pytest

import graph_rag as g


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(g.build_query_plan(query, parser="rules"))
    assert candidate, f"SQL 후보 생성 실패: {query!r}"
    return candidate.get("sql") or ""


# (질문, 생성 SQL 에 포함돼야 하는 술어 조각) — 명사형·동사형·비교·선택·부재·이중경계 전 범위.
_CASES = [
    ("누적 로그인 횟수가 10회 이상인 회원을 찾아줘.", "B.TOTAL_LOGIN_CNT >= 10"),
    ("로그인 횟수가 100회를 초과한 고객을 추출해줘.", "B.TOTAL_LOGIN_CNT > 100"),
    ("누적 접속 횟수가 5회 이하인 회원을 보여줘.", "B.TOTAL_LOGIN_CNT <= 5"),
    ("한 번도 로그인하지 않은 회원을 찾아줘.", "COALESCE(B.TOTAL_LOGIN_CNT, 0) = 0"),
    ("로그인 횟수가 10회에서 50회 사이인 고객을 추출해줘.", "B.TOTAL_LOGIN_CNT <= 50"),
    ("로그인을 정확히 20번 한 회원을 보여줘.", "B.TOTAL_LOGIN_CNT = 20"),
    ("로그인 횟수가 가장 많은 회원 100명을 찾아줘.", "TOP 100"),
    ("누적 로그인 횟수 기준 상위 10% 회원을 추출해줘.", "TOP 10 PERCENT"),
    ("평균보다 많이 로그인한 회원을 보여줘.", "AVG(TOTAL_LOGIN_CNT)"),
    ("로그인 횟수가 30회 이상이지만 100회 미만인 고객을 찾아줘.", "B.TOTAL_LOGIN_CNT < 100"),
]


@pytest.mark.parametrize("query, expected_fragment", _CASES)
def test_login_metric_queries_generate_expected_predicate(query, expected_fragment):
    sql = _sql(query)
    assert "TOTAL_LOGIN_CNT" in sql, f"로그인 지표 컬럼이 SQL 에 없음: {query!r}\n{sql}"
    assert expected_fragment in sql, f"기대 술어 누락: {expected_fragment!r}\n{sql}"


def test_range_query_emits_both_bounds():
    # '10회에서 50회 사이' → 하한+상한 둘 다.
    sql = _sql("로그인 횟수가 10회에서 50회 사이인 고객을 추출해줘.")
    assert "B.TOTAL_LOGIN_CNT >= 10" in sql and "B.TOTAL_LOGIN_CNT <= 50" in sql


def test_dual_bound_compound_emits_both_bounds():
    # '30회 이상이지만 100회 미만' → >=30 AND <100(공용 문법 이중경계).
    sql = _sql("로그인 횟수가 30회 이상이지만 100회 미만인 고객을 찾아줘.")
    assert "B.TOTAL_LOGIN_CNT >= 30" in sql and "B.TOTAL_LOGIN_CNT < 100" in sql


def test_zero_uses_coalesce_for_nullable_column():
    # nullable 컬럼이라 =0 은 COALESCE 로 NULL 회원을 포함해야 한다(missing_as_zero).
    sql = _sql("한 번도 로그인하지 않은 회원을 찾아줘.")
    assert "COALESCE(B.TOTAL_LOGIN_CNT, 0) = 0" in sql
    assert "B.TOTAL_LOGIN_CNT = 0" not in sql.replace("COALESCE(B.TOTAL_LOGIN_CNT, 0) = 0", "")


def test_vs_average_excludes_null_and_scopes_to_members():
    # 평균 모집단은 정상 회원 전체, NULL 제외(개선안 4.2 기본 범위).
    sql = _sql("평균보다 많이 로그인한 회원을 보여줘.")
    assert "AVG(TOTAL_LOGIN_CNT)" in sql and "IS NOT NULL" in sql


# ── 날짜/최근성 조건 오탐 방지 게이트(회귀 위험 지점) ────────────────────────────────────────
@pytest.mark.parametrize("query", [
    "최근 30일 이내 로그인한 회원을 찾아줘.",
    "최근 로그인한 회원을 보여줘.",
    "30일 이상 접속하지 않은 회원을 추출해줘.",
])
def test_date_and_recency_queries_do_not_fire_login_count(query):
    plan = g.build_query_plan(query, parser="rules")
    tu = plan.get("target_user", {})
    assert not isinstance(tu.get("balance_conditions"), list) or all(
        c.get("column") != "TOTAL_LOGIN_CNT" for c in tu["balance_conditions"]
    ), f"날짜/최근성 조건이 로그인 횟수 조건으로 오탐됨: {query!r}"
    sel = plan.get("member_metric_selection")
    assert not (isinstance(sel, dict) and sel.get("column") == "TOTAL_LOGIN_CNT"), (
        f"날짜/최근성 조건이 로그인 선택 전략으로 오탐됨: {query!r}"
    )


# ── 선언형 계약: 로그인 항목이 action_aliases + zero_semantics 를 선언한다 ─────────────────────
def test_login_entry_declares_action_and_zero_semantics():
    entry = next(
        (e for e in g._numeric_metric_filters() if e.get("canonical") == "total_login_count"),
        None,
    )
    assert entry is not None, "total_login_count 가 numeric_metric_filters 에 없음"
    assert entry.get("action_aliases"), "action_aliases 선언 누락(동사형 연결 불가)"
    assert isinstance(entry.get("zero_semantics"), dict) and entry["zero_semantics"].get("missing_as_zero")
