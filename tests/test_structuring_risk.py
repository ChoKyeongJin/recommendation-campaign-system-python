"""구조화 난이도 라우팅 — 가짓수 집계 문장을 1차부터 복구 등급 모델로 보낸다.

이 판정이 틀려도 뜻은 바뀌지 않는다(모델 등급만 바뀐다). 그래서 여기서 재는 것은 정확도가
아니라 **경계**다: 무엇이 가짓수 집계이고 무엇이 아닌지, 그리고 그 경계가 라우팅에 실제로
연결돼 있는지.
"""

from __future__ import annotations

import pytest

import lexicon_patterns
import structuring_risk

# 실측 라이브 프롬프트(docs/data/test_baselines/live_prompts.json #4·#38·#46·#48).
# 넷 모두 1차 싼 모델이 집계 모양을 잃었거나 거짓 미지원으로 끝난 문장이다.
DISTINCT_COUNT_QUERIES = [
    "장바구니에 서로 다른 상품을 3개 이상 담아둔 회원",
    "장바구니에 서로 다른 상품을 정확히 네 종류 담은 회원을 보여줘.",
    "서로 다른 브랜드를 정확히 두 개 구매한 회원을 보여줘.",
    "최근 30일 장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원",
]

# 가짓수 집계가 아닌 문장. 여기까지 승격하면 모든 요청이 비싼 모델로 가서 강등 자체가 무의미해진다.
PLAIN_QUERIES = [
    "최근 3개월 내 주문한 적은 있지만 최근 30일간 구매가 없는 회원",
    "이십만원 이상 구매한 회원 중 남자는 제외해줘",
    "최근 3개월 동안 등급이 두 번 이상 변경된 회원을 찾아줘.",
    "현재 등급이 VIP인 회원",
]


@pytest.mark.parametrize("query", DISTINCT_COUNT_QUERIES, ids=range(len(DISTINCT_COUNT_QUERIES)))
def test_distinct_count_queries_prefer_the_repair_grade_model(query: str) -> None:
    assert structuring_risk.prefers_repair_grade_structuring(query), (
        f"가짓수 집계 문장이 싼 모델로 간다: {query!r}"
    )


@pytest.mark.parametrize("query", PLAIN_QUERIES, ids=range(len(PLAIN_QUERIES)))
def test_ordinary_queries_keep_the_cheap_first_pass(query: str) -> None:
    assert not structuring_risk.prefers_repair_grade_structuring(query), (
        f"가짓수 집계가 아닌 문장까지 승격됐다: {query!r}"
    )


def test_marker_alone_is_not_a_distinct_count() -> None:
    """수식어만으로는 세지 않는다 — '여러 상품을 구매한'은 가짓수 임계값이 없다."""
    assert not structuring_risk.states_distinct_count("여러 상품을 구매한 회원")


def test_threshold_alone_is_not_a_distinct_count() -> None:
    """임계값만으로는 가짓수가 아니다 — '3개 구매'는 수량·건수다."""
    assert not structuring_risk.states_distinct_count("상품을 3개 이상 구매한 회원")


def test_month_duration_is_not_a_count_threshold() -> None:
    """'3개월'의 '개'는 계수 단위가 아니라 기간 단위다 — 여기서 갈리지 않으면 거의 모든
    기간 조건이 가짓수로 오인된다."""
    assert not structuring_risk.states_distinct_count("최근 3개월 동안 다른 지역에서 구매한 회원")


def test_empty_input_is_not_a_risk() -> None:
    assert not structuring_risk.prefers_repair_grade_structuring("")
    assert not structuring_risk.states_distinct_count("   ")


def test_vocabulary_lives_in_the_lexicon_not_in_the_module() -> None:
    """낱말은 사전이 소유한다 — 모듈에 리터럴로 되돌아오면 여기서 걸린다."""
    marker_terms = set(lexicon_patterns.terms("distinct_count_marker"))
    assert {"서로", "다른", "여러", "종류", "가짓수"} <= marker_terms
    # scope_distinct_modifier 보다 넓다(명사 축을 더 갖는다).
    assert set(lexicon_patterns.terms("scope_distinct_modifier")) < marker_terms


def test_routing_reads_this_module() -> None:
    """라우팅 배선이 살아 있는가 — 판정만 있고 부르는 곳이 없으면 아무것도 바뀌지 않는다."""
    import inspect

    import graph_rag

    source = inspect.getsource(graph_rag._structure_campaign_query_plan_v4)
    assert "structuring_risk.prefers_repair_grade_structuring" in source
