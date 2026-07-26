"""공용 순위 지시 문법(#9) 회귀 테스트: '{지표} 기준 상위/하위 N명 · 높은/낮은 순 · TOP N'.

배경: 순위 랭킹은 '많은/높은' 부사가 지표 바로 뒤에 붙는 관용 어순('구매금액이 많은 고객')만 잡혀,
'누적 구매 금액 기준 상위 100명', '상위 100명', '높은 순 50명', 'TOP 20', '하위/낮은 순' 은 인식되지
않아 result_limit(100)만 잡히고 랭킹 지표가 소실됐다(후보 없음). 이제 순위 지시를 공용 문법으로 뽑아
지표어와 결합하고, 방향(상위/높은 순=DESC, 하위/낮은 순=ASC)을 SQL 에 반영한다. 지표를 결정할 수
없는 순수 '상위 N명'은 result_limit 만 조용히 적용하지 않고 '무엇 기준인지' clarification 으로 되묻는다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_ranking_directive_targeting.py -q
"""

import networkx as nx

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _ranking(query: str):
    return _plan(query).get("member_metric_ranking")


# --- 지표 결합: 지표어와 순위 지시가 떨어져 있어도 결합 ---

def test_metric_criterion_top_n_phrasing():
    r = _ranking("누적 구매 금액 기준 상위 100명의 회원을 보여줘.")
    # limit_type 필드가 추가됨(count vs percent) — 핵심 필드만 확인한다.
    assert r["metric_id"] == "total_buy_amt" and r["metric_label"] == "매출"
    assert r["top_n"] == 100 and r["direction"] == "high" and r["limit_type"] == "count"


def test_bare_top_n_with_metric_in_sentence():
    assert _ranking("구매 금액 상위 100명")["metric_id"] == "total_buy_amt"
    assert _ranking("구매 횟수 높은 순 50명")["metric_id"] == "total_buy_cnt"


def test_top_n_keyword_form():
    r = _ranking("TOP 20 매출 고객")
    assert r["metric_id"] == "total_buy_amt" and r["top_n"] == 20 and r["direction"] == "high"


# --- 방향(하위/낮은 순 → ASC) ---

def test_low_direction_ranking_orders_ascending():
    r = _ranking("매출 하위 100명")
    assert r["direction"] == "low"
    sql = g.build_sql_template_candidate(_plan("매출 하위 100명"))["sql"]
    assert "ORDER BY C.TOTAL_BUY_AMT ASC" in sql


def test_low_direction_soonword():
    assert _ranking("객단가 낮은 순 30명")["direction"] == "low"


def test_high_direction_orders_descending():
    sql = g.build_sql_template_candidate(_plan("구매 금액 상위 100명"))["sql"]
    assert "ORDER BY C.TOTAL_BUY_AMT DESC" in sql


# --- 지표 없는 순수 순위 지시 → clarification(조용한 result_limit 금지) ---

def test_metricless_ranking_requests_clarification():
    plan = _plan("상위 100명의 회원을 보여줘.")
    assert plan.get("member_metric_ranking") is None
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict) and unsupported["reason"] == "ranking_metric_unspecified"
    assert g.build_sql_template_candidate(plan) is None


def test_metricless_ranking_surfaces_in_result():
    query = "상위 100명의 회원을 보여줘."
    plan = _plan(query)
    res = g.build_sql_result(
        graph=nx.Graph(), query=query, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=query,
    )
    assert res["sql"] is None
    assert res["unsupported_reason"] == "ranking_metric_unspecified"
    assert res["clarification_questions"]


# --- 오탐 방지: 다른 랭킹 트랙을 뺏지 않는다 ---

def test_adverbial_purchase_ranking_not_stolen():
    # '많이 구매한 사람 상위 10명'은 부사형 구매 건수 랭킹(purchase_count_ranking) 소유 — 뺏기면 안 된다.
    plan = _plan("많이 구매한 사람 상위 10명")
    assert plan.get("member_metric_ranking") is None
    assert plan.get("unsupported") is None
    assert g.build_sql_template_candidate(plan)["id"] == "sql_template:purchase_count_ranking"


def test_idiomatic_ranking_still_works():
    # 기존 관용 어순('구매금액이 많은 회원 100명')은 그대로 동작한다(방향 high).
    r = _ranking("구매금액이 많은 회원 100명")
    assert r["metric_id"] == "total_buy_amt" and r["top_n"] == 100 and r["direction"] == "high"


def test_plain_count_limit_not_treated_as_ranking():
    # 순위 방향어 없는 '30대 여성 100명'은 랭킹이 아니라 단순 행수 제한 — clarification 도 걸리면 안 된다.
    plan = _plan("30대 여성 회원 100명")
    assert plan.get("member_metric_ranking") is None
    assert plan.get("unsupported") is None
