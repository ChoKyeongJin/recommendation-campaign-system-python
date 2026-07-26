"""AND·OR 우선순위 회귀 코퍼스.

배경: OR(또는/이거나)이 임계 조건(구매 금액/횟수·잔액·카트)을 피연산자로 물면 union 컴파일러가 표현 못 해
조용히 AND 로 뭉개(분기 소실)거나 같은 방향 임계가 첫 값으로 붕괴했다. 회원 속성 OR 중 지역(→SIDO IN)·
연령(→구간)은 정상이지만 등급 OR('골드 또는 VIP')은 한 등급만 남던(골드 드롭) 걸 IN 으로 고쳤다.

정책: 임계가 낀 OR 은 미지원 게이트(mixed_and_or_precedence_unsupported)로 명시. 회원 속성 OR(등급/지역/
연령)은 IN·구간으로 정상 컴파일.

실행: docker compose exec -w /app -e PYTHONPATH=/app python pytest tests/test_and_or_precedence.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _reason(plan: dict):
    return (plan.get("unsupported") or {}).get("reason")


def _sql(plan: dict) -> str:
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None, "후보 없음"
    return cand["sql"]


# ── 임계가 낀 OR → 미지원 게이트 ──────────────────────────────────────────────
def test_metric_or_metric_is_gated():
    plan = _plan("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이면서 마케팅에 동의한 회원을 보여줘.")
    assert _reason(plan) == "mixed_and_or_precedence_unsupported"
    assert g.build_sql_template_candidate(plan) is None


def test_amount_or_cart_is_gated():
    plan = _plan("나이가 30세 이상이고 구매금액이 100,000원 이상이거나 장바구니 수량이 5개 이상인 고객을 찾아줘.")
    assert _reason(plan) == "mixed_and_or_precedence_unsupported"


# ── 회원 속성 OR 은 게이트하지 않고 정상 컴파일 ───────────────────────────────
def test_region_or_with_metric_still_compiles():
    # 지역 OR 은 SIDO IN 으로 접혀 AND 임계와 함께 정상('서울 또는 경기 … 구매 5회 이상').
    plan = _plan("서울 또는 경기 거주 회원 중 구매 횟수가 5회 이상인 고객")
    assert plan.get("unsupported") is None
    sql = _sql(plan)
    assert "B.SIDO IN ('경기', '서울')" in sql
    assert "COUNT(DISTINCT ORDER_ID) >= 5" in sql


def test_age_or_with_metric_collapses_to_range():
    # 연령 OR 은 구간으로 접혀 AND 임계와 함께 정상('20대 또는 30대이면서 구매 5회 이상').
    plan = _plan("20대 또는 30대이면서 구매 횟수가 5회 이상인 회원을 찾아줘.")
    assert plan.get("unsupported") is None
    sql = _sql(plan)
    assert "B.AGE >= 20" in sql and "B.AGE <= 39" in sql
    assert "COUNT(DISTINCT ORDER_ID) >= 5" in sql


# ── 등급 OR → GRADE IN (골드 드롭 회귀) ───────────────────────────────────────
def test_grade_or_expands_to_in():
    plan = _plan("골드 또는 VIP 회원 중 적립금이 10,000원 이상이고 예치금도 10,000원 이상인 고객을 추출해줘.")
    assert plan.get("unsupported") is None
    assert set(plan["target_user"]["lifecycle"]) >= {"gold_grade", "vip"}
    sql = _sql(plan)
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in sql
    assert "B.DEPOSIT_BALANCE_AMT >= 10000" in sql and "B.CARROT_BALANCE_AMT >= 10000" in sql


def test_grade_or_standalone():
    plan = _plan("골드 또는 VIP 회원을 찾아줘.")
    assert set(plan["target_user"]["lifecycle"]) == {"gold_grade", "vip"}


def test_grade_threshold_still_expands():
    # '골드 등급 이상'(임계)은 기존대로 서열 확장(회귀 방지).
    plan = _plan("골드 등급 이상 회원")
    assert set(plan["target_user"]["lifecycle"]) == {"gold_grade", "vip"}


def test_single_grade_not_affected():
    plan = _plan("VIP 회원을 찾아줘.")
    assert plan["target_user"]["lifecycle"] == ["vip"]


def test_pure_and_metrics_not_gated():
    # OR 없는 순수 AND 다중 임계는 게이트되지 않는다(오탐 방지).
    plan = _plan("30대 여성 중 구매 횟수가 5회 이상이고 구매금액이 500,000원 이상인 회원")
    assert plan.get("unsupported") is None
