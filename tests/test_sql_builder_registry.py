"""실CRM 타겟 SQL 빌더 레지스트리 계약(contract) 회귀.

배경: build_sql_template_candidate 가 intent 별로 if-체인을 복붙해 갖고 있어, 새 빌더를 한쪽 분기에만
등록하면 다른 의도에서 조용히 빠졌다(장바구니 이탈이 find_user_segment 에서 누락됐던 버그).

고정 내용:
  - 빌더 목록을 단일 소스(_sql_target_builders)로 통합 → 분기 중복 제거.
  - build_sql_template_candidate 는 두 의도(recommend_campaign/find_user_segment)에서 같은 목록을 태운다.
  - 아래 계약 테스트가 '알려진 조건 유형'이 두 의도 양쪽에서 SQL 을 만드는지 검증한다 —
    새 조건 유형을 추가할 때 이 목록(_TYPE_PROMPTS)에 한 줄 추가하면 누락이 테스트로 잡힌다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_sql_builder_registry.py -q
"""

import pytest

import graph_rag as g

# 조건 유형 -> 대표 프롬프트(오디언스 조건만; 발송 동사 유무로 intent 를 강제하지 않는다).
_TYPE_PROMPTS = {
    "cart_abandoner": "장바구니에 담고 아직 안 산 회원",
    "cart_aggregate": "장바구니에 3개 이상 상품을 담은 회원",
    "campaign_response": "쿠폰을 사용한 회원",
    "purchase_history": "생수를 구매한 고객",
    "order_count": "첫 구매 고객",
    "aggregate": "최근 90일 누적 구매 금액 10만원 이상 고객",
    "member": "서울 거주 30대 여성",
}


def _plan(query: str, intent: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    plan["intent"] = intent  # 두 의도 모두에서 같은 빌더 목록을 태우는지 강제 검증
    return plan


@pytest.mark.parametrize("intent", ["recommend_campaign", "find_user_segment"])
@pytest.mark.parametrize("condition_type,prompt", list(_TYPE_PROMPTS.items()))
def test_known_condition_type_builds_sql_for_both_intents(condition_type, prompt, intent):
    cand = g.build_sql_template_candidate(_plan(prompt, intent))
    assert cand is not None, f"{condition_type} ({intent})에서 SQL 미생성 — 빌더가 레지스트리에 연결됐는지 확인"
    assert cand["sql"].strip()


def test_cart_builder_is_registered():
    # 이번 버그 회귀: 장바구니 빌더가 단일 레지스트리에 실제로 들어 있어야 한다.
    assert g._build_cart_targets_candidate in g._sql_target_builders()


def test_non_target_intent_yields_no_candidate():
    # 실추출 의도가 아니면(예: unknown) 빌더를 태우지 않는다.
    plan = g.build_query_plan("서울 거주 30대 여성", parser="rules")
    plan["intent"] = "unknown"
    assert g.build_sql_template_candidate(plan) is None
