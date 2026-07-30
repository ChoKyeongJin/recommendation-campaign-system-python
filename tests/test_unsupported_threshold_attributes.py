"""미지원 속성 임계값 fail-close — 스키마에 없는 속성이 데려온 숫자를 다른 지표가 재사용하지 않는다.

'인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는 사람'이 '상품 수량 50만개 이상 AND 최근 90일
무주문'(정의상 공집합)으로 컴파일되던 사고의 회귀 테스트다. 핵심은 문장을 막는 것이 아니라 **숫자의
소유권**이다 — 미지원 속성이 가져간 숫자는 다른 지표·단위·도메인 폴백이 다시 쓸 수 없어야 한다.
"""

from __future__ import annotations

import pytest

import aggregate_parser_config as config
import aggregate_spans as spans
import graph_rag

# 사용자에게 절대 새면 안 되는 내부 표현.
INTERNAL_TERMS = ("AGG", "NOT EXISTS", "unsupported_attribute", "metric unresolved", "semantic conflict")

BLOCKED_QUERIES = [
    "인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는 사람 뽑아줘",
    "면적 100평 이상 매장의 3개월 미구매 고객",
    "평수 30평 이상인 매장의 고객",
    "매출 10억 이상 지점의 2개월 이내 방문자",
    "50만 명 이상의 인구를 가진 도시 중 최근 3개월 미구매 고객",
    "100평 이상인 면적의 매장 고객",
    "도시 인구가 약 50만 이상인 곳의 고객",
]

PASSING_QUERIES = [
    "나이 30 이상",
    "구매금액 10만원 이상",
    "구매금액 100000 이상",
    "나이 30 이상 40 미만",
    "3개 이상 구매한 고객",
    "상품 5개 이상 담은 장바구니",
    "3건 이상 주문한 회원",
    "5,000개 이상 구매한 고객",
]


def _plan(query: str) -> dict:
    return graph_rag.build_query_plan(query, parser="rules")


# ── 1. 차단 ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", BLOCKED_QUERIES)
def test_unsupported_attribute_thresholds_fail_closed(query: str) -> None:
    plan = _plan(query)
    unsupported = plan.get("unsupported")
    assert isinstance(unsupported, dict), f"미지원으로 닫히지 않았다: {query}"
    assert unsupported["reason"] == "unsupported_threshold_attribute"
    assert not plan["target_user"].get("aggregate_conditions")
    assert graph_rag.build_sql_template_candidate(plan) is None
    assert graph_rag.build_aggregate_targets_sql_candidate(plan) is None


@pytest.mark.parametrize("query", BLOCKED_QUERIES)
def test_clarification_is_user_facing(query: str) -> None:
    unsupported = _plan(query)["unsupported"]
    clarification = unsupported["clarification"]
    assert clarification and clarification == unsupported["message"]
    for term in INTERNAL_TERMS:
        assert term not in clarification
        assert term not in unsupported["message"]


def test_the_reported_query_names_the_attribute_and_offers_the_rest() -> None:
    unsupported = _plan(BLOCKED_QUERIES[0])["unsupported"]
    assert "인구" in unsupported["clarification"]
    assert "시군구" in unsupported["clarification"]          # 사용자가 고쳐 쓸 수 있는 표현
    assert "미구매 기간 조건" in unsupported["clarification"]  # 나머지 조건 제안


def test_unsupported_number_is_not_reused_by_another_metric() -> None:
    """50만이 상품 수량 임계값으로 되살아나지 않는다 — 재사용 금지가 이 수정의 본체다."""
    plan = _plan(BLOCKED_QUERIES[0])
    conditions = plan["target_user"].get("aggregate_conditions") or []
    assert all(condition.get("threshold") != 500000.0 for condition in conditions)
    assert conditions == []


# ── 2. 통과(기존 동작 유지) ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", PASSING_QUERIES)
def test_supported_thresholds_still_parse(query: str) -> None:
    assert _plan(query).get("unsupported") is None, query


def test_supported_threshold_values_are_unchanged() -> None:
    assert _plan("구매금액 10만원 이상")["target_user"]["aggregate_conditions"][0]["threshold"] == 100000.0
    assert _plan("5,000개 이상 구매한 고객")["target_user"]["aggregate_conditions"][0]["threshold"] == 5000.0
    assert _plan("3건 이상 주문한 회원")["target_user"]["aggregate_conditions"][0]["metric_id"] == "order_count"


# ── 3. 한 절의 복수 숫자(절 전체 플래그가 아님) ──────────────────────────────────────────
def test_and_expression_keeps_the_supported_condition_in_the_offer() -> None:
    plan = _plan("인구 50만 이상이고 구매금액 10만원 이상인 고객")
    unsupported = plan["unsupported"]
    assert unsupported["reason"] == "unsupported_threshold_attribute"
    assert "인구" in unsupported["clarification"]
    # 지원되는 쪽은 '나머지 조건'으로 제안된다 — 절 전체를 미지원으로 뭉개지 않았다는 증거.
    assert "누적 구매 금액" in unsupported["clarification"]


def test_or_expression_does_not_offer_the_remainder() -> None:
    """OR 에서 한 쪽을 빼면 원래 의미가 달라지므로 '나머지만 조회'를 제안하면 안 된다."""
    clarification = _plan("인구 50만 이상이거나 구매금액 10만원 이상인 고객")["unsupported"]["clarification"]
    assert "OR" in clarification or "또는" in clarification
    assert "나머지 조건" not in clarification


def test_a_global_metric_anchor_does_not_rescue_a_distant_number() -> None:
    """앞쪽에 '구매금액'이 있다는 이유로 '인구 50만'을 구매금액으로 읽지 않는다."""
    plan = _plan("구매금액을 분석해서 인구 50만 이상 도시의 고객 조회")
    assert plan["unsupported"]["reason"] == "unsupported_threshold_attribute"
    assert not plan["target_user"].get("aggregate_conditions")


# ── 4. 속성 결합 단위 테스트(어순·경계·모호) ─────────────────────────────────────────────
def _bind(clause: str):
    return graph_rag._bind_threshold_candidates(clause)


@pytest.mark.parametrize(
    ("clause", "surface"),
    [
        ("인구가 50만 이상", "인구"),            # 속성이 숫자 왼쪽
        ("50만 명 이상의 인구", "인구"),          # 속성이 숫자 오른쪽
        ("도시 인구가 약 50만 이상", "인구"),      # 수식어가 사이에
        ("100평 이상인 면적의 매장", "면적"),      # 오른쪽 + 단위 있음
        ("최소 10억 매출이 나는 지점", "매출"),    # 선행 비교어 + 문맥 별칭
    ],
)
def test_attribute_binds_from_either_side(clause: str, surface: str) -> None:
    bound = _bind(clause)
    assert bound, clause
    claimed = [c for c in bound if c.ownership == spans.CLAIMED_UNSUPPORTED]
    assert claimed and claimed[0].attribute_ref.surface == surface


def test_supported_attribute_claims_its_own_number() -> None:
    bound = _bind("구매금액 10만원 이상")
    assert bound[0].ownership == spans.CLAIMED_SUPPORTED
    assert bound[0].consumed_by.startswith("supported_attribute:")


def test_conjunction_boundary_stops_attribute_search() -> None:
    """'이고' 너머의 속성은 이 숫자의 것이 아니다."""
    bound = _bind("인구 50만 이상이고 구매금액 10만원 이상")
    by_value = {c.value_span.text: c for c in bound}
    assert by_value["50만"].ownership == spans.CLAIMED_UNSUPPORTED
    assert by_value["10만"].ownership == spans.CLAIMED_SUPPORTED


def test_context_alias_needs_its_context() -> None:
    """'매출'은 지점/매장 문맥에서만 미지점 매출로 본다 — 회원 지표 랭킹 문맥을 뺏지 않는다."""
    with_context = _bind("매출 10억 이상 지점")
    assert any(c.ownership == spans.CLAIMED_UNSUPPORTED for c in with_context)
    without_context = _bind("매출 10억 이상 회원")
    assert all(c.ownership != spans.CLAIMED_UNSUPPORTED for c in without_context)


def test_generic_unknown_attribute_fails_closed_with_a_generic_hint() -> None:
    """힌트 목록에 없어도 조사 앵커가 붙은 미지의 속성이면 조용히 숫자를 넘기지 않는다."""
    bound = _bind("체류시간이 30 이상")
    claimed = [c for c in bound if c.ownership == spans.CLAIMED_UNSUPPORTED]
    assert claimed and claimed[0].attribute_ref.key == "unknown_attribute"
    assert claimed[0].attribute_ref.message_key == config.rules().generic_unsupported_message_key


def test_numbers_without_any_attribute_stay_unclaimed() -> None:
    bound = _bind("3개 이상 구매")
    assert bound and all(c.ownership == spans.UNCLAIMED for c in bound)
