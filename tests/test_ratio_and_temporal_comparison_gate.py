"""D(비율 'A 대비 B') + E(기간/시점 비교) 미지원 게이트 회귀 테스트.

세 표현 모두 이전엔 조용히 엉뚱한 트랙으로 분해됐다:
 - D '구매 횟수 대비 구매 금액이 높은' → '구매 금액 높은'만 남아 매출 랭킹으로 오답.
 - E-1 '최근 90일 객단가가 이전 90일보다 증가' → 롤링 기간 비교 미탐지 → 조용한 None.
 - E-2 '첫 구매 금액보다 최근 구매 금액이 큰' → '첫 구매'=order_count=1 + 랭킹으로 분해 → 무관한 SQL.
이제 각각 명시 미지원(unsupported_reason)으로 SQL 생성을 중단하고, 다른 트랙으로 폴백하지 않는다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_ratio_and_temporal_comparison_gate.py -q
"""

import networkx as nx

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _reason(query: str):
    return (_plan(query).get("unsupported") or {}).get("reason")


# --- D: 비율 'A 대비 B' ---

def test_ratio_expression_unsupported_with_operands():
    plan = _plan("구매 횟수 대비 구매 금액이 높은 회원을 추출해줘.")
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict)
    assert unsupported["reason"] == "unregistered_ratio_metric"
    assert unsupported["numerator"] == "purchase_amount"
    assert unsupported["denominator"] == "order_count"
    # 매출 랭킹 등 다른 트랙으로 폴백하지 않는다.
    assert g.build_sql_template_candidate(plan) is None


def test_ratio_variant_order_count_synonym():
    assert _reason("주문 수 대비 결제 금액이 큰 고객") == "unregistered_ratio_metric"


def test_ratio_surfaces_in_result():
    query = "구매 횟수 대비 구매 금액이 높은 회원"
    plan = _plan(query)
    res = g.build_sql_result(
        graph=nx.Graph(), query=query, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=query,
    )
    assert res["sql"] is None and res["unsupported_reason"] == "unregistered_ratio_metric"


# --- E-1: 롤링 기간 대 기간 비교 ---

def test_rolling_period_comparison_unsupported():
    assert _reason("최근 90일 객단가가 이전 90일보다 증가한 고객을 보여줘.") == "period_over_period_comparison_not_supported"
    assert _reason("직전 30일보다 최근 30일 구매 금액이 늘어난 회원") == "period_over_period_comparison_not_supported"


def test_calendar_period_comparison_still_unsupported():
    assert _reason("지난달 결제 금액이 이번 달보다 많은 고객을 추출해줘.") == "period_over_period_comparison_not_supported"
    assert _reason("전월 대비 당월 구매 금액이 증가한 회원") == "period_over_period_comparison_not_supported"


def test_single_window_not_flagged_as_period_comparison():
    # 단일 창 임계('최근 90일 … 20만원 이상')는 기간 비교가 아니다 — 정상 집계.
    plan = _plan("최근 90일 구매 금액이 200,000원 이상인 고객")
    assert plan.get("unsupported") is None
    assert g.build_sql_template_candidate(plan) is not None


# --- E-2: 회원 내 시점(첫/최근) 값 비교 ---

def test_intra_member_temporal_comparison_unsupported():
    plan = _plan("첫 구매 금액보다 최근 구매 금액이 큰 회원을 찾아줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "intra_member_temporal_metric_comparison_not_supported"
    # '첫 구매'=order_count=1 이나 구매금액 랭킹으로 분해되지 않는다.
    assert g.build_sql_template_candidate(plan) is None


def test_first_purchase_alone_still_supported():
    # 단독 '첫 구매 고객'(시점 비교 아님)은 기존대로 first_purchase 로 처리(미지원 아님).
    plan = _plan("첫 구매 고객에게 쿠폰")
    assert (plan.get("unsupported") or {}).get("reason") != "intra_member_temporal_metric_comparison_not_supported"
    assert g.build_sql_template_candidate(plan) is not None


# --- 오탐 방지: 평균 대비는 D 로 오인하지 않는다(각자 사유) ---

def test_average_comparison_not_misclassified_as_ratio():
    assert _reason("구매 금액이 평균 대비 높은 고객") == "average_comparison_metric_unsupported"
