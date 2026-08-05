"""이탈 위험 문형("N개월 주문은 있었지만 M일 구매가 없는")의 회귀 고정.

2026-08-02 실측: 같은 프롬프트를 5회 돌려 4회가 `semantic_ir_unsupported` 로 막혔고, 통과한 1회는
**LLM 이 노드를 더 못 만든 덕분**이었다(타입 확정 단계에서 폐기 → 결정론 합성이 통과). 즉 판정이
뒤집혀 있었다. 원인은 하나가 아니라 넷이었고, 각각을 따로 고정한다:

  C1 존재 표현의 착지 슬롯 부재 — `order_count > 0` 이 임계 슬롯의 양수 도메인에 걸렸다.
     → 카운트 대수(존재/부재/항진식/모순)와 존재 슬롯으로 환원.
  C2 결정론 구제가 게이트 뒤에 있었다 → 순서는 tests/test_deterministic_rescue_ordering.py 소관.
  C3 절대 기간이 무음 드롭(set-then-pop no-op) → 후행 창은 환산, 과거 달력 구간은 fail-close.
  C4 컨테이너 노드가 자식이 덮지 않는 구간까지 커버로 청구 → 커버리지 거짓말.

2026-08-05: 노드 → 실행 슬롯 컴파일 계층이 폐기되면서 '노드 경로만으로 통과하는가'를 묻던
단언(15종)이 함께 삭제됐다. 같은 날 커버리지 단언 3종(C4·기간 앵커)도 삭제됐다 — 원문 앵커
공급자 등록(`targeting_domain.install()`)이 제거되어 `semantic_coverage` 가 아무것도 미커버로
보고하지 않게 됐고, 통과해도 증명이 없기 때문이다. 남은 계약은 그 계층 없이도 성립하는
것들이다 — 카운트 대수(C1), 소유권 소거. 결정론 구제 자체(C2)와 그 순서는
tests/test_deterministic_rescue_ordering.py 가 그대로 소유한다.

2026-08-05(정정): 위 15종 중 `test_calendar_window_reaches_sql_without_database_clock_reanchoring`
은 삭제하면 안 됐다 — 컴파일 계층은 입력 dict 를 만들던 픽스처였고 단언의 대상은 SQL 빌더
(`graph_rag.build_aggregate_targets_sql_candidate`)였다. 픽스처를 리터럴 dict 로 바꿔
tests/test_aggregate_window_sql_anchoring.py 에 복원했다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
from semantic_normalizers import (  # noqa: E402
    COUNT_ABSENCE,
    COUNT_CONTRADICTION,
    COUNT_EXISTENCE,
    COUNT_TAUTOLOGY,
    COUNT_THRESHOLD,
    CountThresholdNormalizer,
)

_FIXED_TODAY = date(2026, 3, 31)


# 노드 → 실행 슬롯 컴파일(`_compile`)을 태우던 테스트 15종은 2026-08-05 삭제됐다 — 그 컴파일
# 계층이 폐기됐다. SemanticPlan 노드를 만들던 헬퍼(`_aggregate_node`)와 그 프롬프트 상수도
# 마지막 소비자(커버리지·소거 단언)와 함께 사라졌다.


# ── C1. 카운트 대수 ────────────────────────────────────────────────────────────────
def test_count_algebra_separates_presence_from_threshold() -> None:
    """'있음/없음'과 'N건 이상'은 표현이 아니라 값으로 갈린다 — 정수 카운트 위의 순수 대수."""
    classify = CountThresholdNormalizer.classify
    assert classify(">", 0) == COUNT_EXISTENCE
    assert classify(">=", 1) == COUNT_EXISTENCE
    assert classify(">", 0.5) == COUNT_EXISTENCE, "정수 카운트에서 '>0.5' 는 '>=1' 과 같다"
    assert classify("=", 0) == COUNT_ABSENCE
    assert classify("<", 1) == COUNT_ABSENCE
    assert classify("<=", 0) == COUNT_ABSENCE
    assert classify(">=", 0) == COUNT_TAUTOLOGY, "'0건 이상'은 조건이 아니라 전원이다"
    assert classify("<", 0) == COUNT_CONTRADICTION
    assert classify(">=", 2) == COUNT_THRESHOLD
    assert classify(">", 1) == COUNT_THRESHOLD


# ── C0. 거부 사유의 정직성 ─────────────────────────────────────────────────────────
def test_rejection_reason_names_the_value_domain_not_the_vocabulary() -> None:
    """어휘에 **있는** 지표를 두고 '어휘에 없다'고 말하지 않는다(운영자를 엉뚱한 카탈로그로 보낸다)."""
    import targeting_ir

    allowed = graph_rag._llm_slot_allowed()["aggregate_metrics"]
    assert "order_count" in allowed, "전제: 이 지표는 실행 어휘에 있다"
    reason = targeting_ir.slot_rejection_reason(
        "aggregate_conditions", {"metric_id": "order_count", "operator": ">", "threshold": 0},
        allowed=allowed,
    )
    assert reason and "어휘" not in reason, reason
    assert "임계값" in reason, reason


# ── C4. 컨테이너 커버리지 ───────────────────────────────────────────────────────────
# C4 단언(컨테이너 청구·기간 앵커 청구 3종)은 2026-08-05 삭제됐다. 판정기 `semantic_coverage`
# 는 원문 앵커 공급자가 있어야 무엇이 미커버인지 말할 수 있는데, 그 공급자를 등록하던
# `targeting_domain.install()` 이 같은 날 제거됐다(소비자였던 SemanticPlanV2 파이프라인 폐기).
# 공급자가 없으면 커버리지 검증은 무해하게 통과하므로 이 단언들은 참을 증명하지 못한다 —
# 통과하는 채로 두면 없는 안전망을 광고하게 된다.


# ── C2. 소유권 소거 ────────────────────────────────────────────────────────────────
# 소거 단언 3종(슬롯 소유 구간의 실패 소거·미청구 구간 보존·원자 없는 구간의 증명 불가)도
# 2026-08-05 삭제됐다. 판정기 `semantic_plan_bridge.supersede_slot_owned_failures` 는 구간의
# **값 원자**(원문 앵커)를 세어 중복을 증명하는데, 앵커 공급자 등록이 같은 날 제거되어
# 원자가 항상 0이 됐다. 그러면 긍정 방향은 증명 불가로 뒤집히고(실측: superseded=[]),
# 부정 방향 둘은 무조건 참이 되어 아무것도 증명하지 못한다. 판정기 자체가 SemanticPlanV2
# 전용이라 다음 단계에서 모듈과 함께 사라진다.


# ── 적대적 리뷰에서 확인된 결함의 회귀 고정(2026-08-02) ──────────────────────────────


def test_classify_rejects_fuzzy_operators_instead_of_inverting_polarity() -> None:
    """'!=' 안에서 별칭 '=' 를 찾아내던 관대함이 `order_count != 0`(주문 있음)을 부재로 뒤집었다.

    분류가 곧 슬롯 선택이므로, 모르는 표기는 관대하게 넘기지 말고 **환원하지 않는다**.
    """
    for surface in ("!=", "<>", "≠", "not", ""):
        assert CountThresholdNormalizer.classify(surface, 0) == COUNT_THRESHOLD, surface
    # 정상 표기는 그대로 판정된다.
    assert CountThresholdNormalizer.classify("=", 0) == COUNT_ABSENCE
    assert CountThresholdNormalizer.classify("eq", 0) == COUNT_ABSENCE
    assert CountThresholdNormalizer.classify("이상", 1) == COUNT_EXISTENCE


def test_classify_survives_non_finite_thresholds() -> None:
    """inf 는 floor/ceil 에서 OverflowError 를 던지는데, 그건 노드별 예외 처리를 빠져나가
    파이프라인 전체를 죽이고 형제 노드가 만든 슬롯까지 함께 버린다."""
    for value in (float("inf"), float("-inf"), float("nan")):
        assert CountThresholdNormalizer.classify(">", value) == COUNT_THRESHOLD


def test_lapsed_claim_does_not_swallow_an_interposed_condition() -> None:
    """정규식의 `.{0,24}?` 사이에 무관한 조건이 낄 수 있다 — 매치 전체를 청구하면 그 조건이
    '이미 표현됨'으로 처리돼 조용히 사라진다. 청구는 **각 절**의 구간이어야 한다."""
    import behavior_demotion
    import slot_ownership

    query = "최근 3개월 주문은 있었지만 서울에 사는 30대 중 최근 30일간 구매가 없는 회원을 추출해줘"
    plan: dict = {}
    assert behavior_demotion.normalize_lapsed_purchase_pattern(
        plan, source_text=query, reference_date=_FIXED_TODAY
    )
    for slot in ("purchase_membership", "purchase_inactivity"):
        text = slot_ownership.slot_span(plan, slot)["text"]
        assert "서울" not in text and "30대" not in text, f"{slot} 청구가 남의 절을 삼켰다: {text!r}"


# 기간 앵커 청구 2종(`test_a_node_claims_as_many_period_anchors_as_it_owns` /
# `test_a_single_period_node_still_claims_only_one_anchor`)도 위 C4 와 같은 이유로
# 2026-08-05 삭제됐다 — 앵커 공급자가 없는 커버리지 검증은 어느 방향으로도 증명하지 못한다.
