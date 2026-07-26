"""#5 '구매 금액 0원' 의미 정책 + #8 기간 대 기간 비교 미지원 회귀 테스트.

두 사례 모두 이전에는 조용히 아무 후보도 만들지 않았다(silent None). 이제:
 - #5: '구매 금액 0원'을 코드가 무구매로 단정하지 않는다(zero_amount_semantics.maps_to_no_purchase).
   기본(false)이면 '0원 결제 vs 평생 무주문' 모호성을 clarification 으로 되묻고, 정책을 켜면(true)
   무구매(no_purchase, 주문 anti-join)로 컴파일한다. 캠페인 문맥('캠페인 구매금액 0원')은 캠페인 반응
   트랙이 소유하므로 이 게이트가 양보한다.
 - #8: '지난달 대비/보다 이번 달' 같은 기간 대 기간 비교는 미지원임을 명시한다
   (unsupported_reason=period_over_period_comparison_not_supported). 조용한 None 대신 SQL 생성을 중단한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_zero_amount_and_period_comparison.py -q
"""

import networkx as nx

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _result(query: str, plan: dict) -> dict:
    return g.build_sql_result(
        graph=nx.Graph(), query=query, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=query,
    )


# --- #5: 0원 의미 정책 ---

def test_zero_amount_default_requests_clarification():
    query = "구매 금액이 0원인 회원을 추출해줘."
    plan = _plan(query)
    # 무구매로 단정하지 않는다(기본 정책).
    assert "no_purchase" not in plan["target_user"].get("behaviors", [])
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict) and unsupported["reason"] == "zero_amount_semantics_requires_policy"
    assert g.build_sql_template_candidate(plan) is None
    res = _result(query, plan)
    assert res["sql"] is None and res["unsupported_reason"] == "zero_amount_semantics_requires_policy"
    assert res["clarification_questions"]


def test_zero_amount_maps_to_no_purchase_when_policy_enabled(monkeypatch):
    monkeypatch.setitem(g._MEMBER_TARGET_FILTERS, "zero_amount_semantics", {"maps_to_no_purchase": True})
    plan = _plan("구매 금액이 0원인 회원을 추출해줘.")
    assert plan.get("unsupported") is None
    assert "no_purchase" in plan["target_user"].get("behaviors", [])
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None and candidate["id"] == "sql_template:order_count_targets"
    assert "CRM_SL_ORDERHEADERMALL" in candidate["sql"]


def test_campaign_zero_amount_not_hijacked_by_zero_gate():
    # '캠페인 구매금액 0원'은 캠페인 반응 트랙(no_buy_response) 소유 — 0원 게이트가 뺏으면 안 된다.
    plan = _plan("캠페인 구매금액이 0원인 회원을 찾아줘")
    assert (plan.get("unsupported") or {}).get("reason") != "zero_amount_semantics_requires_policy"
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None and "NOT EXISTS" in candidate["sql"]


def test_nonzero_amount_not_treated_as_zero():
    # 100,000원(끝자리 0)·10만원 등은 0원 조건이 아니다.
    for query in ["구매 금액이 100,000원 이상인 회원", "누적 구매액이 50,000원 이하인 회원"]:
        plan = _plan(query)
        assert plan.get("unsupported") is None, query
        assert g.build_sql_template_candidate(plan) is not None, query


# --- #8: 기간 대 기간 비교 미지원 ---

def test_period_over_period_marked_unsupported():
    query = "지난달 결제 금액이 이번 달보다 많은 고객을 추출해줘."
    plan = _plan(query)
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict)
    assert unsupported["reason"] == "period_over_period_comparison_not_supported"
    assert g.build_sql_template_candidate(plan) is None
    res = _result(query, plan)
    assert res["sql"] is None
    assert res["unsupported_reason"] == "period_over_period_comparison_not_supported"


def test_single_period_threshold_not_flagged():
    # 단일 구간 임계('지난달 … 10만원보다 많은')는 기간 대 기간 비교가 아니다 — 정상 집계로 처리.
    for query in ["지난달 결제 금액이 10만원보다 많은 고객", "올해 누적 구매 금액이 500,000원을 넘는 회원"]:
        plan = _plan(query)
        assert (plan.get("unsupported") or {}).get("reason") != "period_over_period_comparison_not_supported", query
        assert g.build_sql_template_candidate(plan) is not None, query
