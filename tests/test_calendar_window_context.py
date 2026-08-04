"""달력 창은 주입된 기준일만 사용하고 calendar 단위를 일수로 근사하지 않는다."""

from __future__ import annotations

from datetime import date

import calendar_window as cw


_FIXED_TODAY = date(2024, 3, 31)


def test_explicit_absolute_calendar_window_does_not_require_reference_date() -> None:
    assert cw.parse_calendar_window("2024-02") == {
        "from": "20240201",
        "to": "20240229",
        "label": "2024-02",
    }
    assert cw.parse_half_or_quarter_window("2024년 2분기") == {
        "from": "20240401",
        "to": "20240630",
        "label": "2024년 2분기",
    }


def test_relative_calendar_expressions_fail_closed_without_reference_date() -> None:
    assert cw.parse_calendar_window("지난달") is None
    assert cw.parse_calendar_window("2분기") is None
    assert cw.parse_half_or_quarter_window("상반기") is None
    assert cw.parse_relative_year_window("작년") is None
    assert cw.parse_relative_past_window("3개월 전") is None


def test_injected_reference_date_resolves_relative_windows_with_calendar_boundaries() -> None:
    assert cw.parse_calendar_window("지난달", today=_FIXED_TODAY) == {
        "from": "20240201",
        "to": "20240229",
        "label": "2024년 2월",
    }
    assert cw.parse_relative_year_window("작년", today=_FIXED_TODAY) == {
        "from": "20230101",
        "to": "20231231",
        "label": "2023년",
    }
    assert cw.parse_relative_past_window("1개월 전", today=_FIXED_TODAY) == {
        "from": "20240201",
        "to": "20240229",
        "label": "2024년 2월",
        cw.SOURCE_TEMPORAL_KIND_KEY: cw.KIND_PAST_POINT,
    }


def test_duration_parser_preserves_calendar_units_without_approximate_days() -> None:
    numeric = cw.parse_duration_window("최근 3개월")
    assert numeric is not None
    assert numeric["value"] == 3 and numeric["unit"] == "months"
    assert "min_days" not in numeric

    word = cw.parse_duration_window("최근 한달")
    assert word is not None
    assert word["value"] == 1 and word["unit"] == "months"
    assert "min_days" not in word

    year = cw.parse_duration_window("최근 1년")
    assert year is not None
    assert year["value"] == 1 and year["unit"] == "years"
    assert "min_days" not in year


def test_fixed_duration_units_keep_exact_day_projection() -> None:
    window = cw.parse_duration_window("최근 2주")
    assert window is not None
    assert window["value"] == 2 and window["unit"] == "weeks"
    assert window["min_days"] == 14


def test_unified_time_parser_preserves_calendar_duration_shape() -> None:
    assert cw.parse_time_windows("최근 한달", include_duration=True) == [
        {"value": 1, "unit": "months", "label": "최근 1개월"}
    ]


def test_relative_past_span_scan_is_pure_but_does_not_create_a_window() -> None:
    assert cw.parse_relative_past_window_span("3개월 전 구매") == (0, 6)
    assert cw.parse_relative_past_window("3개월 전 구매") is None
    assert cw.parse_time_window_span("3개월 전 구매") is None
    assert cw.parse_time_window_span("3개월 전 구매", today=_FIXED_TODAY) == (0, 6)


# ── 시각 한정어 ────────────────────────────────────────────────────────────────────────
def _times(text: str) -> list[tuple[str, str]]:
    return [(w["from_time"], w["to_time"]) for w in cw.parse_calendar_windows(text) if "from_time" in w]


def test_meridiem_words_convert_to_a_24_hour_span() -> None:
    """'밤 11시'는 23시다. 이관 전에는 낱말이 정규식 리터럴('오전|오후')에, 변환이 if 문에 따로
    있어서 밤·새벽·저녁·아침은 아예 시각으로 읽히지 않았다(2026-08-04)."""
    assert _times("2026년 7월 1일 밤 11시") == [("230000", "235959")]
    assert _times("2026년 7월 1일 새벽 2시") == [("020000", "025959")]
    assert _times("2026년 7월 1일 저녁 7시") == [("190000", "195959")]
    assert _times("2026년 7월 1일 아침 9시") == [("090000", "095959")]


def test_existing_am_pm_behaviour_is_unchanged() -> None:
    """이관은 기존 두 낱말의 뜻을 바꾸지 않는다 — 정오·자정 접힘 포함."""
    assert _times("2026년 7월 1일 오전 9시") == [("090000", "095959")]
    assert _times("2026년 7월 1일 오후 6시 30분") == [("183000", "183059")]
    assert _times("2026년 7월 1일 오전 12시") == [("000000", "005959")]
    assert _times("2026년 7월 1일 오후 12시") == [("120000", "125959")]


def test_midnight_folds_to_zero_for_night() -> None:
    assert _times("2026년 7월 1일 밤 12시") == [("000000", "005959")]


def test_hours_outside_a_words_declared_range_stay_uninterpreted() -> None:
    """'밤 2시'는 다음 날 새벽일 수 있어 어느 날의 02시인지 확정할 수 없다 — 창을 만들지 않는다.

    시각만 조용히 버리고 날짜 창을 내면 '그날 하루 전체'로 의미가 넓어진 채 실행된다.
    """
    assert cw.parse_calendar_windows("2026년 7월 1일 밤 2시") == []
    assert cw.parse_calendar_windows("2026년 7월 1일 새벽 9시") == []
    assert cw.parse_calendar_windows("2026년 7월 1일 저녁 11시") == []


def test_every_declared_meridiem_word_is_reachable_from_the_pattern() -> None:
    """표에만 있고 정규식에 없는 낱말은 죽은 선언이다 — 표에서 정규식을 파생하는 이유."""
    for word, rule in cw.MERIDIEM_RULES.items():
        assert _times(f"2026년 7월 1일 {word} {rule.min_hour}시"), f"'{word}' 가 시각으로 안 읽힌다"
