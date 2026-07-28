"""B(지표 간 최장 일치) + C(절 경계 기반 파싱 + 상충 검사) 회귀 테스트.

배경:
 - B: 최장 일치가 지표 '내부'에서만 동작해, '평균 결제 금액'이 average_order_amount(전체 정확 일치)와
   purchase_amount('결제 금액' 부분 일치)에 동시에 걸려 조건이 중복 생성됐다. 이제 전체 지표 후보의 매칭
   span 을 모아 겹치면 더 긴 span 을 남기고 포함된 짧은 후보는 버린다(서로 다른 절이면 둘 다 유지).
 - C: 동의어 뒤 고정 40자 방식이 뒤 절까지 삼켜 '누적 구매 금액 … 이상이지만 평균 주문 금액 … 미만' 에서
   purchase_amount 에 하한+상한(>=100만 AND <3만)이 함께 잡히는 모순을 만들었다. 이제 파싱 범위를 다음
   지표/대조 접속어에서 끊고, 그래도 같은 지표에 하한>상한 이 생기면 절 과포획으로 보고 clarification 한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_aggregate_metric_span_and_clause.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _conds(query: str):
    return _plan(query)["target_user"].get("aggregate_conditions") or []


def _pairs(query: str):
    return [(c["metric_id"], c["operator"], c["threshold"]) for c in _conds(query)]


# --- B: 지표 간 최장 일치(부분 span 포함 시 짧은 후보 제거) ---

def test_longer_metric_span_wins_over_contained_shorter():
    # '평균 결제 금액' = average_order_amount 만 남고, 그 안의 '결제 금액'(purchase_amount)은 버려진다.
    pairs = _pairs("평균 결제 금액이 30,000원 이하인 회원을 보여줘.")
    assert pairs == [("average_order_amount", "<=", 30000.0)]


def test_average_order_synonyms_not_double_matched():
    assert [m for m, *_ in _pairs("평균 주문 금액이 50,000원 이상인 회원")] == ["average_order_amount"]
    assert [m for m, *_ in _pairs("객단가가 100,000원을 초과하는 고객")] == ["average_order_amount"]


def test_metric_synonyms_ignore_internal_whitespace_and_case():
    # 레지스트리에 모든 공백 조합을 중복 등록하지 않아도 같은 복합 명사로 해석한다.
    for phrase in ("평균 주문금액", "평균주문금액", "평균   주문   금액"):
        assert _pairs(f"최근 6개월 {phrase}이 10만 원 이상인 회원") == [
            ("average_order_amount", ">=", 100000.0)
        ]


def test_distinct_clause_metrics_both_kept():
    # 서로 다른 절에 있는 지표는 둘 다 유지된다(겹치지 않으므로).
    pairs = _pairs("주문 횟수는 10회 이상이고 평균 주문 금액은 50,000원 이상인 고객을 찾아줘.")
    assert ("order_count", ">=", 10.0) in pairs
    assert ("average_order_amount", ">=", 50000.0) in pairs


# --- C: 절 경계 파싱(뒤 절 조건이 앞 지표로 새지 않음) ---

def test_clause_boundary_prevents_over_capture():
    pairs = _pairs("누적 구매 금액은 1,000,000원 이상이지만 평균 주문 금액은 30,000원 미만인 회원을 추출해줘.")
    assert pairs == [
        ("purchase_amount", ">=", 1000000.0),
        ("average_order_amount", "<", 30000.0),
    ]
    # 금지: purchase_amount 에 상한(<30000)이 함께 잡히면 안 된다.
    assert ("purchase_amount", "<", 30000.0) not in pairs


def test_legitimate_range_preserved():
    # 단일 지표 범위('에서~사이')는 그대로 하한+상한으로 유지된다.
    assert _pairs("구매 금액 10만원에서 50만원 사이인 고객") == [
        ("purchase_amount", ">=", 100000.0),
        ("purchase_amount", "<=", 500000.0),
    ]


def test_single_threshold_unchanged():
    assert _pairs("누적 구매 금액이 100,000원 이상인 회원") == [("purchase_amount", ">=", 100000.0)]


# --- C 안전장치: 같은 지표 하한>상한(불가능 범위) → clarification ---

def test_conflicting_range_on_same_metric_requests_clarification():
    plan = _plan("구매 금액이 100만원 이상이지만 30만원 미만인 고객")
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict)
    assert unsupported["reason"] == "conflicting_aggregate_conditions"
    assert g.build_sql_template_candidate(plan) is None


def test_valid_range_not_flagged_as_conflict():
    # lo<=hi 정상 범위는 상충이 아니다(clarification 걸리면 안 됨).
    plan = _plan("구매 금액 10만원에서 50만원 사이인 고객")
    assert plan.get("unsupported") is None
    assert g.build_sql_template_candidate(plan) is not None
