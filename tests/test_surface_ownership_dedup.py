"""같은 어구를 두 번 해석하지 않는다 — 표면 근거 기반 소유권.

배경: ``2026년3월 구매에서 가장 많이 팔린상품 5개를 구매한 고객`` 은 순위 절(entity_set)로 완전히
파싱된 뒤, 같은 '가장 많이'를 집계 표지로 다시 읽은 분석 계약이 플랜을 덮어써
``unresolved_aggregate_metric`` 으로 끝났다(지표가 상품 판매량이라 회원 지표 레지스트리에 없다).

고치는 기준은 '조건의 종류'가 아니라 **근거 표현이 같은가**다. 그래서 이 파일이 지키는 것은 셋이다.

  1. 해석은 자기 근거를 원문 좌표로 밝힌다(analytical_intent 의 evidence).
  2. 조건은 자기가 읽은 구간을 소유로 선언한다(slot_ownership 의 소유 대장).
  3. 근거가 **전부** 남의 소유일 때만 중복으로 보고 막는다 — 하나라도 절 밖이면 진짜 요구다.

특정 케이스가 아니라 이 계약을 검증한다: 소유를 선언하는 조건이면 무엇이든(entity_set 이 아니어도)
같은 보호를 받는지까지 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import analytical_intent
import graph_rag
import lexicon_llm
import slot_ownership

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "data" / "sql_schema.json"

# 실제로 실패했던 문장과, 그 실행에서 LLM 표면 신호 추출이 돌려준 근거(운영 로그 그대로).
RANKING_QUERY = "2026년3월 구매에서 가장 많이 팔린상품 5개를 구매한 고객"
RANKING_SIGNALS = {"aggregate_max": ("가장많이팔린상품5개",)}
# 프롬프트 재작성(LLM)이 만들어 내는 변형 — 같은 요구이므로 결과도 같아야 한다.
REWRITTEN_QUERY = "2026년 3월에 가장 많이 판매된 상품 5개를 구매한 고객"
REWRITTEN_SIGNALS = {"aggregate_max": ("가장많이판매된상품5개",)}


def _build_plan(query: str, signals: dict[str, tuple[str, ...]]) -> dict:
    """순위 절 확정 → 분석 계약 적용까지, 파이프라인과 같은 순서로 돌린다."""
    plan: dict = {"intent": "find_user_segment", "target_user": {}}
    with lexicon_llm.signal_scope(query, lambda _query: signals):
        graph_rag._apply_entity_set_condition(query, plan)
        graph_rag._apply_analytical_intent(query, plan, SCHEMA)
    return plan


# ── 1. 해석은 자기 근거를 밝힌다 ────────────────────────────────────────────────────────────
def test_intent_reports_the_surface_that_produced_it() -> None:
    """결정론 집계어로 읽은 해석은 그 낱말의 원문 구간을 근거로 남긴다."""
    query = "여성 회원의 총 구매금액 알려줘"
    intent = analytical_intent.analyze_analytical_intent(query)
    assert intent is not None
    roles = {item["role"]: item for item in intent[analytical_intent.EVIDENCE_KEY]}
    assert roles["aggregate_function"]["surface"] == "총 구매금액"
    start, end = roles["metric"]["span"]
    assert query[start:end] == roles["metric"]["surface"] == "구매금액"


def test_llm_signal_evidence_is_traced_back_to_the_query() -> None:
    """LLM 이 메운 집계 표지도 근거를 남긴다 — 근거 없는 판정은 소유권 비교의 대상이 될 수 없다."""
    with lexicon_llm.signal_scope(RANKING_QUERY, lambda _query: RANKING_SIGNALS):
        intent = analytical_intent.analyze_analytical_intent(RANKING_QUERY)
    assert intent is not None and intent["aggregate_function"] == "MAX"
    evidence = intent[analytical_intent.EVIDENCE_KEY]
    assert [item["role"] for item in evidence] == ["aggregate_function"]
    start, end = evidence[0]["span"]
    assert RANKING_QUERY[start:end] == "가장 많이 팔린상품 5개"


def test_evidence_is_absent_when_nothing_triggered_an_aggregate_reading() -> None:
    """신호가 없으면 해석 자체가 없다(기존 결정론 경로) — 근거도 판정도 만들지 않는다."""
    assert analytical_intent.analyze_analytical_intent(RANKING_QUERY) is None


# ── 2. 소유 대장 ────────────────────────────────────────────────────────────────────────
def test_ledger_matches_by_span_within_one_coordinate_system() -> None:
    plan: dict = {}
    slot_ownership.record_owned_span(
        plan, owner="ranking_clause", span=[0, 10], source_text="0123456789 밖의 다른 절",
    )
    source = "0123456789 밖의 다른 절"
    assert slot_ownership.owning_condition(plan, [2, 4], source_text=source)["owner"] == "ranking_clause"
    # 같은 좌표계인데 겹치지 않으면 문장의 다른 절이다 — 소유가 아니다.
    assert slot_ownership.owning_condition(plan, [12, 14], source_text=source) is None


def test_exact_span_owner_rejects_merely_overlapping_larger_clause() -> None:
    """파괴적 중복 제거는 부분 겹침이 아니라 같은 좌표의 완전 일치만 허용한다."""
    source = "블루 후보군 2019년 구매 랭킹"
    ranking_span = [7, len(source)]
    plan: dict = {}
    slot_ownership.record_owned_span(
        plan, owner="purchase_count_ranking", span=ranking_span, source_text=source
    )

    assert slot_ownership.owns_exact_span(
        plan, owner="purchase_count_ranking", span=ranking_span, source_text=source
    )
    assert not slot_ownership.owns_exact_span(
        plan, owner="purchase_count_ranking", span=[0, len(source)], source_text=source
    )
    assert not slot_ownership.owns_exact_span(
        plan,
        owner="purchase_count_ranking",
        span=ranking_span,
        source_text=f"{source} 중 남성 제외",
    )


def test_ledger_falls_back_to_the_surface_when_coordinates_differ() -> None:
    """재작성본과 원문처럼 좌표계가 다르면 구간을 비교할 수 없다 — 그때만 같은 표현인지로 본다."""
    plan: dict = {}
    slot_ownership.record_owned_span(
        plan, owner="ranking_clause", span=[0, 21], source_text="가장 많이 팔린 상품 5개를 구매한 고객",
    )
    other_coordinates = "전혀 다른 좌표계의 문장"
    assert slot_ownership.owning_condition(
        plan, [0, 5], source_text=other_coordinates, surface="가장 많이",
    )["owner"] == "ranking_clause"
    assert slot_ownership.owning_condition(
        plan, [0, 2], source_text=other_coordinates, surface="평균",
    ) is None


def test_claiming_a_slot_declares_ownership_even_with_no_value_to_claim() -> None:
    """소유는 슬롯 값의 유무와 무관하다 — 값이 없다고 어구를 안 읽은 것이 아니다."""
    plan: dict = {"target_user": {}}
    slot_ownership.claim_slots(
        plan, (("target_user", "purchase_date"),),
        owner="some_condition", reason="테스트", source_text="원문 문장", owner_span=[0, 2],
    )
    assert slot_ownership.owning_condition(plan, [0, 2], source_text="원문 문장")["owner"] == "some_condition"


# ── 3. 중복 해석만 막는다 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("query", "signals"),
    [(RANKING_QUERY, RANKING_SIGNALS), (REWRITTEN_QUERY, REWRITTEN_SIGNALS)],
)
def test_ranking_clause_survives_the_aggregate_reading(query: str, signals: dict) -> None:
    """순위 절이 소유한 '가장 많이'가 집계 표지로 다시 읽혀 플랜을 뒤집으면 안 된다."""
    plan = _build_plan(query, signals)
    assert plan.get("unsupported") is None
    assert plan["intent"] == "find_user_segment"
    assert isinstance(plan["target_user"].get("entity_set_condition"), dict)
    assert "analytical_intent" not in plan


def test_the_block_is_recorded_as_a_decision() -> None:
    """조용히 건너뛰지 않는다 — 무엇이 왜 막혔는지 감사 로그가 답한다."""
    plan = _build_plan(RANKING_QUERY, RANKING_SIGNALS)
    blocked = [
        item for item in plan.get("decisions", [])
        if item.get("filter") == "apply_analytical_intent" and "entity_set_condition" in str(item.get("reason"))
    ]
    assert blocked, "중복 해석을 막은 사실이 감사 로그에 없다"


def test_an_aggregate_request_outside_the_owned_clause_still_applies() -> None:
    """근거가 하나라도 순위 절 밖이면 그 절은 진짜 집계 요구다 — 억제하지 않는다."""
    query = "2026년 3월에 가장 많이 팔린 상품 5개를 구매한 고객의 평균 구매금액 알려줘"
    plan = _build_plan(query, RANKING_SIGNALS)
    assert plan.get("analytical_intent"), "순위 절 밖의 '평균 구매금액' 요구까지 억제됐다"
    assert plan["analytical_intent"]["metric"] == "purchase_amount"


def test_a_plain_aggregate_question_is_unaffected() -> None:
    """소유를 주장하는 조건이 없으면 분석 계약은 예전 그대로 적용된다."""
    plan = _build_plan("여성 회원의 총 구매금액 알려줘", {})
    assert plan["intent"] == "analyze_aggregation"
    assert plan.get("unsupported") is None


def test_protection_is_not_specific_to_the_ranking_clause() -> None:
    """소유를 선언한 조건이면 무엇이든 같은 보호를 받는다 — 이 규칙은 조건 종류를 모른다."""
    query = "여성 회원의 총 구매금액 알려줘"
    plan: dict = {"intent": "find_user_segment", "target_user": {}}
    slot_ownership.record_owned_span(
        plan, owner="some_future_condition", span=[0, len(query)], source_text=query,
        reason="이 조건이 문장 전체를 읽었다고 선언",
    )
    graph_rag._apply_analytical_intent(query, plan, SCHEMA)
    assert plan.get("unsupported") is None
    assert "analytical_intent" not in plan
