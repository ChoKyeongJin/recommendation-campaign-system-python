"""'구매/주문 이력 없음' 표현형 → no_purchase 결정론 추출 회귀 코퍼스.

배경: "구매 이력이 없는 회원가입 고객" 같은 자연스러운 표현이 결정론(rules) 파서에서 no_purchase 로
안 잡혀(behaviors 빈 배열) LLM 에 의존했고, LLM/렉시콘이 '구매 안 함'을 장바구니 이탈(cart_abandoner)로
오분류해 엉뚱하게 장바구니 템플릿이 선택되던 문제를 고정한다. no_purchase 는 주문 anti-join
(order_count_targets, CRM_SL_ORDERHEADERMALL)이 정답이다.

정규화 사전(no_purchase 규칙)의 활용/조사 변형 동의어로 결정론 추출을 보장하고, 장바구니 이탈
표현이 no_purchase 로 오염되지 않는지(회귀)도 함께 고정한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_no_purchase_phrasing.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    # 실제 파이프라인(retrieve)과 동일하게 회원/주문 신호로 unknown intent 를 승격한다.
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# 결정론(rules)만으로 no_purchase 가 잡혀야 하는 표현형.
NO_PURCHASE_PHRASINGS = [
    "구매 이력이 없는 회원가입 고객",
    "구매 이력이 없는 고객",
    "구매 이력 없는 고객",
    "주문 이력이 없는 회원",
    "주문 내역이 없는 고객",
    "구매 내역이 없는 고객",
    "구매한 적 없는 고객",
    "구입한 적이 없는 회원",
    "가입만 하고 구매 안 한 고객",
    "미구매 고객",  # 기존 동작 유지(회귀 방지)
]


def test_no_purchase_phrasings_extract_behavior():
    for query in NO_PURCHASE_PHRASINGS:
        plan = _plan(query)
        behaviors = plan["target_user"].get("behaviors", [])
        assert "no_purchase" in behaviors, f"{query!r} -> {behaviors} (no_purchase 미추출)"


def test_no_purchase_phrasings_select_order_count_template():
    for query in NO_PURCHASE_PHRASINGS:
        plan = _plan(query)
        candidate = g.build_sql_template_candidate(plan)
        assert candidate is not None, f"{query!r}: 후보 없음"
        assert candidate["id"] == "sql_template:order_count_targets", f"{query!r} -> {candidate['id']}"
        # 주문 anti-join 이 정답 — 장바구니 테이블을 참조하면 안 된다.
        assert "ODS_MALL_OMS_CART" not in candidate["sql"], f"{query!r}: 장바구니 참조 오류"
        assert "CRM_SL_ORDERHEADERMALL" in candidate["sql"], f"{query!r}: 주문 헤더 미참조"


# 장바구니 이탈 표현은 여전히 cart_abandoner 여야 한다(no_purchase 로 오염 금지).
CART_ABANDONER_PHRASINGS = [
    "장바구니에 담고 구매 안 한 회원",
    "장바구니 이탈 고객",
]


def test_cart_phrasings_not_contaminated_by_no_purchase():
    for query in CART_ABANDONER_PHRASINGS:
        plan = _plan(query)
        behaviors = plan["target_user"].get("behaviors", [])
        assert "cart_abandoner" in behaviors, f"{query!r} -> {behaviors}"
        assert "no_purchase" not in behaviors, f"{query!r}: no_purchase 로 오염됨 -> {behaviors}"
