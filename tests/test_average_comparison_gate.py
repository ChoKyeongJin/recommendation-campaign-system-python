"""'평균 대비' 비교 미지원 게이트 + 전칭 수식어('전체') 오추출 차단 회귀 테스트.

배경(#10): "구매 금액이 전체 구매 회원 평균보다 높은 고객"이 (1) '전체'를 상품명으로 오추출해
PRODUCT_NAME LIKE N'%전체%' 로 새고, (2) purchase_history_targets 로 폴백해 '평균 이상 구매자'가
아니라 '카테고리에 전체가 든 상품 구매자'라는 그럴듯한 오답 SQL 을 조용히 냈다. 이제:
 - 전체/전부/모든/모두/평균 은 상품 토큰에서 제외한다(오추출 차단).
 - 주문 집계 지표('구매 금액')의 '평균 대비' 비교는 실DB 단일 서브쿼리로 표현 불가라 명시 미지원으로
   표시하고(build_sql_template_candidate=None), unsupported_reason/clarification 을 응답한다 —
   상품 텍스트 검색 등 엉뚱한 트랙으로 폴백하지 않는다.
 - 예치금/적립금 등 회원 컬럼의 '평균 대비'는 기존대로 member_metric_selection(vs_average)로 지원된다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_average_comparison_gate.py -q
"""

import networkx as nx

import graph_rag as g

Q10 = "구매 금액이 전체 구매 회원 평균보다 높은 고객을 찾아줘."


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# --- '전체' 등 전칭 수식어가 상품명으로 새지 않는다 ---

def test_universal_quantifier_not_extracted_as_product():
    # 전칭·집계 수식어는 상품 토큰에서 제외된다.
    for word in ("전체", "전부", "모든", "모두", "평균"):
        assert g._sanitize_purchase_object(word) is None, word
    # 실제 파이프라인에서도 Q10 의 '전체'가 purchase_object 로 새지 않는다.
    plan = _plan(Q10)
    assert plan["target_user"].get("purchase_object") is None


# --- #10: 주문 집계 지표의 평균 대비는 명시 미지원(폴백 금지) ---

def test_amount_average_comparison_marked_unsupported():
    plan = _plan(Q10)
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict)
    assert unsupported["reason"] == "average_comparison_metric_unsupported"
    # 어떤 빌더로도 폴백하지 않는다(그럴듯한 오답 SQL 방지).
    assert g.build_sql_template_candidate(plan) is None


def test_amount_average_comparison_surfaces_unsupported_reason_in_result():
    plan = _plan(Q10)
    res = g.build_sql_result(
        graph=nx.Graph(), query=Q10, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=Q10,
    )
    assert res["sql"] is None and res["is_success"] is False
    assert res["unsupported_reason"] == "average_comparison_metric_unsupported"
    assert res["failure_reason"] == "average_comparison_metric_unsupported"
    # 조용한 빈결과가 아니라 사용자에게 되물을 clarification 을 남긴다.
    assert res["clarification_questions"] and any("평균" in q for q in res["clarification_questions"])
    # 상품 텍스트 검색으로 새지 않았다(오답 재발 방지).
    assert "N'%전체%'" not in (res.get("blocked_sql") or "")


# --- 지원되는 회원 컬럼 평균 대비는 그대로 동작(과잉 차단 금지) ---

def test_member_column_average_comparison_still_supported():
    for query in ["예치금이 평균보다 높은 고객", "적립금이 전체 평균보다 높은 고객을 찾아줘.", "예치금이 평균 이상인 고객"]:
        plan = _plan(query)
        assert plan.get("unsupported") is None, query
        candidate = g.build_sql_template_candidate(plan)
        assert candidate is not None and candidate["id"] == "sql_template:member_metric_selection", query


# --- 오탐 방지: 평균 명사/파생 비율/금액 임계는 미지원으로 잡히면 안 된다 ---

def test_average_marker_does_not_overfire():
    # 지표 명사('평균 주문 금액')·파생 비율('하루 평균 …')·금액 임계('10만원 이상')는 평균 대비가 아니다.
    for query in ["평균 주문 금액이 10만원 이상인 고객", "하루 평균 로그인 횟수 3회 이상 고객", "구매 금액 10만원 이상 고객"]:
        plan = _plan(query)
        assert plan.get("unsupported") is None, query
        assert g.build_sql_template_candidate(plan) is not None, query
