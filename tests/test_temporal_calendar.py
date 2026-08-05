"""달력 계산의 고정 계약 — 경계·시간대·칸 산술.

시간 경계는 '대충 맞으면 되는' 값이 아니다. 1월에 '지난달'이 12월이 아니면 전년도 대상이
통째로 빠지고, 월 경계를 UTC 로 계산하면 KST 8월 1일 새벽 9시간이 7월 대상에 섞인다. 그런
오답은 SQL 을 봐도 드러나지 않으므로 여기서 값으로 고정한다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from temporal_ir import calendar as tcal  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")


def _context(now: datetime, timezone: str = "Asia/Seoul") -> sir.TemporalRequestContext:
    return sir.TemporalRequestContext(now=now, timezone=timezone)


def _previous_month_end() -> sir.RelativeAnchor:
    return sir.RelativeAnchor(offset=-1, unit="month", boundary="end")


# ── 시점 확정 ─────────────────────────────────────────────────────────────────────


def test_previous_month_end_stays_inside_that_month() -> None:
    """``Boundary.END`` 는 칸 **안의 마지막 순간**이다 — 다음 칸 시작이면 한 달이 어긋난다."""
    instant = tcal.resolve_anchor(_previous_month_end(), _context(datetime(2026, 8, 5, 9, tzinfo=SEOUL)))
    assert instant == datetime(2026, 7, 31, 23, 59, 59, 999999, tzinfo=SEOUL)
    bucket = tcal.bucket_containing(instant, sir.TimeUnit.MONTH, "Asia/Seoul")
    assert bucket.dates() == (datetime(2026, 7, 1).date(), datetime(2026, 8, 1).date())


def test_january_previous_month_is_the_previous_year_december() -> None:
    instant = tcal.resolve_anchor(_previous_month_end(), _context(datetime(2026, 1, 3, 10, tzinfo=SEOUL)))
    bucket = tcal.bucket_containing(instant, sir.TimeUnit.MONTH, "Asia/Seoul")
    assert bucket.dates() == (datetime(2025, 12, 1).date(), datetime(2026, 1, 1).date())


def test_leap_year_february_keeps_the_twenty_ninth() -> None:
    instant = tcal.resolve_anchor(_previous_month_end(), _context(datetime(2028, 3, 10, tzinfo=SEOUL)))
    assert instant.date() == datetime(2028, 2, 29).date()
    non_leap = tcal.resolve_anchor(_previous_month_end(), _context(datetime(2027, 3, 10, tzinfo=SEOUL)))
    assert non_leap.date() == datetime(2027, 2, 28).date()


def test_month_end_request_from_the_last_day_of_a_month() -> None:
    """말일에 요청해도 '지난달'은 지난달이다(경계에서 한 칸 밀리지 않는다)."""
    instant = tcal.resolve_anchor(
        _previous_month_end(), _context(datetime(2026, 8, 31, 23, 59, tzinfo=SEOUL))
    )
    assert instant.date() == datetime(2026, 7, 31).date()


def test_month_boundary_is_evaluated_in_the_business_timezone() -> None:
    """KST 8월 1일 08:00 은 UTC 로는 7월 31일이다 — 시간대에 따라 '지난달'이 갈린다."""
    now = datetime(2026, 8, 1, 8, 0, tzinfo=SEOUL)
    seoul = tcal.bucket_containing(
        tcal.resolve_anchor(_previous_month_end(), _context(now)), sir.TimeUnit.MONTH, "Asia/Seoul"
    )
    utc = tcal.bucket_containing(
        tcal.resolve_anchor(_previous_month_end(), _context(now, "UTC")), sir.TimeUnit.MONTH, "UTC"
    )
    assert seoul.dates()[0] == datetime(2026, 7, 1).date()
    assert utc.dates()[0] == datetime(2026, 6, 1).date()


def test_absolute_anchor_is_normalized_into_the_request_timezone() -> None:
    instant = tcal.resolve_anchor(
        sir.AbsoluteAnchor(instant=datetime(2026, 7, 31, 15, 0, tzinfo=UTC)),
        _context(datetime(2026, 8, 5, tzinfo=SEOUL)),
    )
    assert instant == datetime(2026, 8, 1, 0, 0, tzinfo=SEOUL)


# ── 구간 확정 ─────────────────────────────────────────────────────────────────────


def test_rolling_days_and_calendar_months_are_different_sets() -> None:
    context = _context(datetime(2026, 8, 5, tzinfo=SEOUL))
    rolling = tcal.resolve_window(
        sir.RelativeWindow(amount=30, unit="day", mode="rolling", include_current=True), context
    )
    calendar = tcal.resolve_window(
        sir.RelativeWindow(amount=1, unit="month", mode="calendar", include_current=False), context
    )
    assert rolling.dates() == (datetime(2026, 7, 6).date(), datetime(2026, 8, 5).date())
    assert calendar.dates() == (datetime(2026, 7, 1).date(), datetime(2026, 8, 1).date())
    assert rolling != calendar


def test_calendar_window_include_current_changes_the_span() -> None:
    context = _context(datetime(2026, 8, 5, tzinfo=SEOUL))
    excluding = tcal.resolve_window(
        sir.RelativeWindow(amount=3, unit="month", mode="calendar", include_current=False), context
    )
    including = tcal.resolve_window(
        sir.RelativeWindow(amount=3, unit="month", mode="calendar", include_current=True), context
    )
    assert excluding.dates() == (datetime(2026, 5, 1).date(), datetime(2026, 8, 1).date())
    assert including.dates() == (datetime(2026, 6, 1).date(), datetime(2026, 9, 1).date())


def test_rolling_month_uses_calendar_arithmetic_not_thirty_days() -> None:
    context = _context(datetime(2026, 3, 31, tzinfo=SEOUL))
    window = tcal.resolve_window(
        sir.RelativeWindow(amount=1, unit="month", mode="rolling", include_current=True), context
    )
    # 2월에는 31일이 없다 — 그 달의 마지막 날로 접고, 90일 근사를 쓰지 않는다.
    assert window.start.date() == datetime(2026, 2, 28).date()


def test_open_ended_window_needs_a_declared_bound() -> None:
    context = _context(datetime(2026, 8, 5, tzinfo=SEOUL))
    window = sir.OpenEndedWindow(
        anchor=sir.RelativeAnchor(offset=0, unit="month", boundary="start"),
        direction=sir.Direction.BEFORE,
    )
    with pytest.raises(tcal.CalendarError):
        tcal.resolve_window(window, context)
    bounded = tcal.resolve_window(
        window, context, open_bound=datetime(2017, 1, 1).date()
    )
    assert bounded.dates() == (datetime(2017, 1, 1).date(), datetime(2026, 8, 1).date())


# ── 칸 산술 ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end", "unit", "expected"),
    [
        ((2026, 5, 1), (2026, 8, 1), sir.TimeUnit.MONTH, 3),
        ((2026, 1, 1), (2027, 1, 1), sir.TimeUnit.MONTH, 12),
        ((2026, 8, 1), (2026, 8, 8), sir.TimeUnit.DAY, 7),
        ((2024, 2, 1), (2024, 3, 1), sir.TimeUnit.DAY, 29),
        ((2023, 2, 1), (2023, 3, 1), sir.TimeUnit.DAY, 28),
    ],
)
def test_expected_bucket_count(start, end, unit, expected) -> None:
    interval = tcal.TimeInterval(datetime(*start, tzinfo=SEOUL), datetime(*end, tzinfo=SEOUL))
    assert tcal.expected_bucket_count(interval, unit, "Asia/Seoul") == expected


def test_bucket_alignment_detects_partial_buckets() -> None:
    aligned = tcal.TimeInterval(
        datetime(2026, 7, 1, tzinfo=SEOUL), datetime(2026, 8, 1, tzinfo=SEOUL)
    )
    partial = tcal.TimeInterval(
        datetime(2026, 7, 6, tzinfo=SEOUL), datetime(2026, 8, 6, tzinfo=SEOUL)
    )
    assert tcal.is_bucket_aligned(aligned, sir.TimeUnit.MONTH, "Asia/Seoul")
    assert not tcal.is_bucket_aligned(partial, sir.TimeUnit.MONTH, "Asia/Seoul")
    assert tcal.is_bucket_aligned(partial, sir.TimeUnit.DAY, "Asia/Seoul")


def test_week_buckets_start_on_monday() -> None:
    bucket = tcal.bucket_containing(datetime(2026, 8, 5, 12, tzinfo=SEOUL), sir.TimeUnit.WEEK, "Asia/Seoul")
    assert bucket.start.weekday() == 0
    assert bucket.end_exclusive - bucket.start == timedelta(days=7)


def test_intervals_are_half_open_and_reject_empty_spans() -> None:
    interval = tcal.TimeInterval(
        datetime(2026, 7, 1, tzinfo=SEOUL), datetime(2026, 8, 1, tzinfo=SEOUL)
    )
    assert interval.contains(datetime(2026, 7, 31, 23, 59, tzinfo=SEOUL))
    assert not interval.contains(interval.end_exclusive)
    with pytest.raises(tcal.CalendarError):
        tcal.TimeInterval(interval.end_exclusive, interval.start)


def test_non_midnight_intervals_cannot_be_expressed_as_date_columns() -> None:
    interval = tcal.TimeInterval(
        datetime(2026, 7, 1, 9, tzinfo=SEOUL), datetime(2026, 8, 1, tzinfo=SEOUL)
    )
    with pytest.raises(tcal.CalendarError):
        interval.dates()


def test_unknown_timezone_fails_closed() -> None:
    with pytest.raises(tcal.CalendarError):
        tcal.zone("Mars/Olympus")
