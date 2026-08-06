"""단독 '작년/올해'의 연 전체 창 — 한정자 없는 자리의 뜻.

상대 연도 어휘는 이미 **앵커**로 쓰이고 있었다('작년 상반기' → 2025년 상반기, '작년 3월' →
2025년 3월). 없던 것은 한정자가 없는 자리의 뜻이다: '작년에 가장 많이 팔린' 의 '작년'은 어떤
창도 만들지 못했고, 그래서 그 기간이 통째로 빠진 채 **전 기간 랭킹**으로 컴파일될 수 있었다.

이 파일이 지키는 것은 셋이다:

  1. 단독 상대 연도가 기준일에 고정된 절대 연 창이 된다.
  2. 구체 한정자가 먼저 소비된다 — '작년 상반기'가 창 두 개가 되지 않는다.
  3. 비교 관용구('전년 대비')는 필터 창이 아니다.

기준일은 주입값만 쓴다(호스트 시계를 읽지 않는다).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import calendar_window  # noqa: E402
import canonical_audience_claims  # noqa: E402
import event_ir  # noqa: E402
import semantic_requirements  # noqa: E402
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

TODAY = date(2026, 8, 7)


def _windows(text: str) -> list[dict]:
    return [
        window for window, _start, _end in calendar_window.parse_calendar_window_spans(
            text, today=TODAY
        )
    ]


def test_standalone_last_year_is_the_whole_previous_year() -> None:
    assert _windows("작년에 구매한 고객") == [
        {"from": "20250101", "to": "20251231", "label": "2025년"}
    ]


def test_standalone_this_year_is_the_whole_current_year() -> None:
    assert _windows("올해 구매한 고객") == [
        {"from": "20260101", "to": "20261231", "label": "2026년"}
    ]


def test_standalone_year_equals_the_explicit_year_form() -> None:
    """'작년'과 '2025년'은 같은 창이어야 한다 — 표현이 달라도 뜻은 하나다."""
    assert _windows("작년에 구매한 고객") == _windows("2025년에 구매한 고객")


def test_half_year_qualifier_does_not_also_create_a_whole_year_window() -> None:
    assert _windows("작년 상반기에 구매한 고객") == [
        {"from": "20250101", "to": "20250630", "label": "2025년 상반기"}
    ]


def test_month_qualifier_does_not_also_create_a_whole_year_window() -> None:
    assert _windows("작년 3월에 구매한 고객") == [
        {"from": "20250301", "to": "20250331", "label": "2025년 3월"}
    ]


def test_quarter_qualifier_does_not_also_create_a_whole_year_window() -> None:
    assert _windows("작년 2분기 구매한 고객") == [
        {"from": "20250401", "to": "20250630", "label": "2025년 2분기"}
    ]


def test_year_over_year_comparison_is_not_a_filter_window() -> None:
    """'전년 대비'·'작년 대비'는 비교 기준이지 기간 필터가 아니다."""
    assert _windows("전년 대비 매출이 늘어난 회원") == []
    assert _windows("작년 대비 구매가 늘어난 회원") == []


def test_previous_year_word_still_works_as_a_qualifier_anchor() -> None:
    """'전년'을 단독 창에서 뺀 것이 앵커 기능까지 없앤 것은 아니다."""
    assert _windows("전년 3월에 구매한 회원") == [
        {"from": "20250301", "to": "20250331", "label": "2025년 3월"}
    ]


def test_standalone_year_needs_an_injected_reference_date() -> None:
    """기준일이 없으면 호스트 시계를 추측하지 않는다(미검출로 닫는다)."""
    assert calendar_window.parse_calendar_window_spans("작년에 구매한 고객") == []


# ── 랭킹 요청에서의 귀결 ──────────────────────────────────────────────────────────

RANKED_QUERY = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
CURRENT_DATE = "2026-08-07"


def test_the_year_becomes_an_application_owned_literal_binding() -> None:
    """창의 소유자는 리터럴 계층이다 — 기준일을 가진 유일한 계층이기 때문이다.

    의무 원장(:func:`semantic_requirements.capture_source_semantic_obligations`)은 기준일을
    받지 않는다. 그 원장은 여러 지점에서 다시 캡처되고 id 가 값에서 파생되므로, 일부 지점만
    기준일을 갖는 순간 같은 질의가 서로 다른 의무를 만들어 유령 미해결 항목이 생긴다.
    그래서 상대 연도의 창은 리터럴 바인딩이 소유하고, 누락은 그 계층이 막는다(아래 테스트).
    """
    bindings = extract_literal_bindings(RANKED_QUERY, current_date=CURRENT_DATE)
    windows = [item for item in bindings if item["kind"] == "date_window"]

    assert [item["text"] for item in windows] == ["작년"]
    assert windows[0]["normalized"]["from"] == "20250101"
    assert windows[0]["normalized"]["to"] == "20251231"
    assert windows[0]["normalized"]["event_ir_window"] == {
        "type": "interval", "start": "2025-01-01", "end_exclusive": "2026-01-01",
    }


def test_the_correct_year_window_passes_and_a_missing_one_is_blocked() -> None:
    import test_ranked_set_cardinality as ranked

    with_window = ranked.ranked_set_cardinality_expression()
    assert ranked.build_plan(with_window)["semantic_ir"]["status"] == "resolved"

    without_window = ranked.ranked_set_cardinality_expression()
    summarize = without_window["left"]["relation"]["right"]["relation"]["relation"]
    summarize["relation"] = summarize["relation"]["relation"]
    blocked = ranked.build_plan(without_window)["semantic_ir"]
    assert blocked["status"] == "needs_clarification", (
        "랭킹 기간이 빠졌는데 통과하면 전 기간 랭킹으로 뜻이 넓어진다"
    )


def test_a_different_year_window_is_blocked() -> None:
    import test_ranked_set_cardinality as ranked

    wrong = ranked.ranked_set_cardinality_expression()
    window = wrong["left"]["relation"]["right"]["relation"]["relation"]["relation"]["where"]["window"]
    window["start"], window["end_exclusive"] = "2024-01-01", "2025-01-01"

    assert ranked.build_plan(wrong)["semantic_ir"]["status"] == "needs_clarification"


def test_the_receipt_layer_rejects_a_wrong_window_for_an_absolute_year() -> None:
    """절대 연도 요청에서는 의무가 창을 직접 들고 있고, 영수증이 그것을 대조한다."""
    import test_ranked_set_cardinality as ranked

    absolute_query = "2025년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
    obligation = next(
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(
            absolute_query
        )
        if semantic_requirements.obligation_kind(requirement) == "ranked_entity_set"
    )
    value = dict(obligation.value)
    assert dict(value["time_window"])["from"] == "20250101"

    expression = event_ir.condition_from_dict(ranked.ranked_set_cardinality_expression())
    assert canonical_audience_claims.ranked_obligation_is_compiled(expression, value)

    other_year = {**value, "time_window": {"from": "20240101", "to": "20241231"}}
    assert not canonical_audience_claims.ranked_obligation_is_compiled(
        expression, other_year
    )
