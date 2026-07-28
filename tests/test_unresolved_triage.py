"""미해석 표현 큐와 A/B/C 분류 계약.

이 루프의 전제는 "못 푼 것이 표시로 남는다"이다. 조용히 사라지면 큐에 안 쌓이고, 큐가 비면
루프가 도는 것처럼 보이지만 실제로는 아무것도 관찰되지 않는다. 그래서 여기서 확인하는 것은
분류 정확도가 아니라 **수집이 실제로 되는가**와 **애매할 때 어느 쪽으로 기우는가**다.
"""

from __future__ import annotations

import unresolved_triage as triage


def test_plan_without_unresolved_markers_yields_nothing() -> None:
    assert triage.extract({"intent": "find_user_segment", "target_user": {"gender": "female"}}) is None


def test_each_unresolved_marker_is_collected() -> None:
    for key in triage.UNRESOLVED_PLAN_KEYS:
        plan = {"original_query": "테스트 표현", key: ["something"]}
        case = triage.extract(plan)
        assert case is not None, f"{key} 가 미해석 근거로 수집되지 않았다"
        assert key in case["evidence"]
        assert case["query"] == "테스트 표현"


def test_no_condition_detector_catches_a_totally_unknown_expression() -> None:
    """표시에 의존하지 않는 탐지 — 신호 감지 자체가 실패한 경우가 이 루프의 진짜 사각이다."""
    plan = {"original_query": "혼수 준비중인 고객에게 쿠폰 발송", "target_user": {}, "exclude": {}}
    case = triage.extract(plan)
    assert case is not None
    assert triage.NO_CONDITION_MARKER in case["evidence"]


def test_no_condition_detector_ignores_whole_audience_requests() -> None:
    """'전체 회원' 은 조건이 없는 것이 정상이다 — 오탐이 나면 큐가 잡음으로 덮인다."""
    for query in ("전체 회원에게 쿠폰 발송", "모든 고객에게 캠페인 추천"):
        plan = {"original_query": query, "target_user": {}, "exclude": {}}
        assert triage.detect_no_condition(plan, query) is None


def test_no_condition_detector_is_quiet_when_conditions_exist() -> None:
    plan = {"original_query": "30대 여성 고객", "target_user": {"gender": "female", "age_min": 30}}
    assert triage.detect_no_condition(plan, None) is None


def test_no_condition_detector_ignores_non_audience_text() -> None:
    """오디언스 서술이 아닌 문장(회원 명사 없음)은 조건이 없어도 미해석이 아니다."""
    plan = {"original_query": "매출 합계 알려줘", "target_user": {}}
    assert triage.detect_no_condition(plan, None) is None


def test_empty_containers_do_not_count_as_conditions() -> None:
    """선초기화된 빈 리스트를 조건으로 세면 탐지기가 영영 발동하지 않는다."""
    plan = {"original_query": "혼수 준비중인 고객에게 쿠폰 발송",
            "target_user": {"lifecycle": [], "interests": [], "gender": None}}
    assert triage.detect_no_condition(plan, None) is not None


def test_queue_round_trip(tmp_path) -> None:
    log = tmp_path / "unresolved.jsonl"
    for _ in range(2):
        triage.record({"query": "같은 상품을 동시 구매한 고객", "evidence": {"unsupported": "co_purchase"}}, path=log)
    triage.record({"query": "혼수 준비중인 고객", "evidence": {"failure_log": ["no mapping"]}}, path=log)

    rows = triage.triage(triage.load(log))
    assert [row["count"] for row in rows] == [2, 1]  # 빈도순
    assert rows[0]["query"] == "같은 상품을 동시 구매한 고객"
    assert rows[0]["decision"] == ""  # 사람이 채우는 칸


def test_record_is_a_noop_without_a_log_path(monkeypatch) -> None:
    monkeypatch.delenv(triage.LOG_PATH_ENV, raising=False)
    assert triage.record({"query": "x"}) is False


def test_parameter_shaped_expression_drafts_as_b() -> None:
    kind, _ = triage.classify("장바구니에 담은 지 17일 이상 지난 고객")
    assert kind == triage.CLASS_PARAMETER


def test_unfamiliar_expression_drafts_as_capability() -> None:
    """기존 어휘와 겹치는 뼈대가 없으면 가장 비싼 C 로 보낸다(회복 가능한 쪽으로 기운다)."""
    kind, _ = triage.classify("웨딩 준비 단계별 여정 이탈")
    assert kind == triage.CLASS_CAPABILITY


def test_near_miss_expression_drafts_as_lexicon() -> None:
    """기존 어휘가 뼈대를 이루고 낯선 낱말 한둘만 남으면 사전 문제로 본다."""
    kind, reason = triage.classify("장바구니를 방치한 고객")
    assert kind == triage.CLASS_LEXICON
    assert "방치" in reason or "낯선" in reason


def test_class_vocabulary_is_closed() -> None:
    for query in ("아무 말", "3회 이상 구매", "장바구니 이탈 고객"):
        kind, reason = triage.classify(query)
        assert kind in triage.CLASSES
        assert reason


def test_summary_counts_every_class() -> None:
    rows = triage.triage([
        {"query": "장바구니를 방치한 고객"},
        {"query": "17일 이상 지난 고객"},
        {"query": "웨딩 준비 단계별 여정 이탈"},
    ])
    summary = triage.summary(rows)
    assert set(summary) == set(triage.CLASSES)
    assert sum(summary.values()) == 3


def test_corrupt_and_empty_queue_lines_are_skipped(tmp_path) -> None:
    log = tmp_path / "unresolved.jsonl"
    log.write_text('{"query": "정상"}\n{ broken\n{"evidence": {}}\n\n', encoding="utf-8")
    rows = triage.load(log)
    assert [row["query"] for row in rows] == ["정상"]  # query 없는 줄은 사례가 아니다
