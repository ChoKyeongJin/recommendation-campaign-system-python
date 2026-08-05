"""달력 계산 — 상대 시간 표현을 요청 기준 시각으로 **절대 구간**으로 확정한다.

이 모듈에는 업무 낱말도 컬럼 이름도 없다. 입력은 (anchor | window, 기준 시각, 시간대, 칸 단위)
이고 출력은 반개구간 ``[start, end_exclusive)`` 하나다.

세 가지 규칙만 지킨다.

1. **반개구간**. 끝은 포함하지 않는다 — 포함 끝을 쓰면 날짜 컬럼이 timestamp 인 순간 마지막
   날의 00:00 이후가 조용히 잘린다.
2. **timezone-aware**. naive datetime 은 만들지도 받지도 않는다. 월 경계는 시간대에 따라
   실제로 다른 순간이다(KST 8월 1일 00:00 은 UTC 7월 31일 15:00).
3. **칸 산술은 달력으로**. '3개월 전'을 90일로 바꾸지 않는다. 월·연은 칸 산술,
   일·주는 길이 산술이며 윤년과 월말은 칸 산술이 알아서 처리한다.

``Boundary.END`` 는 칸 **안의 마지막 순간**(다음 칸 시작 - 1μs)이다. 다음 칸의 시작으로 두면
'지난달 말 기준'이 이번 달 칸을 가리켜 한 달이 통째로 어긋난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from temporal_ir import semantic_ir as sir

_MICROSECOND = timedelta(microseconds=1)


class CalendarError(ValueError):
    """달력 계산 계약 위반(알 수 없는 시간대, 표현할 수 없는 칸 산술)."""


def zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarError(f"알 수 없는 시간대입니다: {timezone!r}") from exc


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """반개구간 ``[start, end_exclusive)`` — 두 끝 모두 timezone-aware."""

    start: datetime
    end_exclusive: datetime

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("end_exclusive", self.end_exclusive)):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise CalendarError(f"interval {label} 은 timezone-aware datetime 이어야 합니다")
        if self.start >= self.end_exclusive:
            raise CalendarError(
                f"빈 구간입니다: {self.start.isoformat()} >= {self.end_exclusive.isoformat()}"
            )

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end_exclusive

    def dates(self) -> tuple[date, date]:
        """실행 IR 의 ``AbsoluteInterval`` 이 쓰는 (시작일, 끝일 미포함).

        경계가 자정이 아니면 그 구간은 날짜 컬럼으로 표현할 수 없다 — 잘라서 근사하지 않고
        호출자가 미지원으로 답할 수 있도록 오류로 알린다.
        """
        for label, value in (("start", self.start), ("end_exclusive", self.end_exclusive)):
            if (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0):
                raise CalendarError(
                    f"자정 경계가 아닌 구간은 날짜 컬럼으로 표현할 수 없습니다({label}="
                    f"{value.isoformat()})"
                )
        return self.start.date(), self.end_exclusive.date()

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end_exclusive": self.end_exclusive.isoformat()}


def _local(year: int, month: int, day: int, tz: ZoneInfo) -> datetime:
    return datetime(year, month, day, tzinfo=tz)


def _month_start(value: datetime, tz: ZoneInfo) -> datetime:
    return _local(value.year, value.month, 1, tz)


def _add_months(anchor: datetime, months: int, tz: ZoneInfo) -> datetime:
    """달의 1일 기준 칸 산술(월말 보정이 필요 없는 유일한 지점)."""
    total = anchor.year * 12 + (anchor.month - 1) + months
    return _local(total // 12, total % 12 + 1, 1, tz)


def bucket_containing(instant: datetime, unit: sir.TimeUnit, timezone: str) -> TimeInterval:
    """그 순간이 속한 달력 칸."""
    tz = zone(timezone)
    local = instant.astimezone(tz)
    if unit is sir.TimeUnit.DAY:
        start = _local(local.year, local.month, local.day, tz)
        return TimeInterval(start, start + timedelta(days=1))
    if unit is sir.TimeUnit.WEEK:
        start = _local(local.year, local.month, local.day, tz) - timedelta(days=local.weekday())
        return TimeInterval(start, start + timedelta(days=7))
    if unit is sir.TimeUnit.MONTH:
        start = _month_start(local, tz)
        return TimeInterval(start, _add_months(start, 1, tz))
    start = _local(local.year, 1, 1, tz)
    return TimeInterval(start, _local(local.year + 1, 1, 1, tz))


def shift_bucket(bucket: TimeInterval, unit: sir.TimeUnit, offset: int, timezone: str) -> TimeInterval:
    """칸을 ``offset`` 만큼 이동(음수는 과거)."""
    if offset == 0:
        return bucket
    tz = zone(timezone)
    start = bucket.start.astimezone(tz)
    if unit is sir.TimeUnit.DAY:
        return bucket_containing(start + timedelta(days=offset), unit, timezone)
    if unit is sir.TimeUnit.WEEK:
        return bucket_containing(start + timedelta(weeks=offset), unit, timezone)
    if unit is sir.TimeUnit.MONTH:
        return bucket_containing(_add_months(start, offset, tz), unit, timezone)
    return bucket_containing(_local(start.year + offset, 1, 1, tz), unit, timezone)


def resolve_anchor(anchor: sir.Anchor, context: sir.TemporalRequestContext) -> datetime:
    """시점 표현 → 절대 순간. 기준 시각은 **문맥에서만** 온다."""
    if isinstance(anchor, sir.AbsoluteAnchor):
        return anchor.instant.astimezone(zone(context.timezone))
    now = context.now.astimezone(zone(context.timezone))
    if isinstance(anchor, sir.ReferenceAnchor):
        return now
    bucket = shift_bucket(
        bucket_containing(now, anchor.unit, context.timezone),
        anchor.unit,
        anchor.offset,
        context.timezone,
    )
    if anchor.boundary is sir.Boundary.START:
        return bucket.start
    return bucket.end_exclusive - _MICROSECOND


def resolve_window(
    window: sir.TemporalWindow,
    context: sir.TemporalRequestContext,
    *,
    anchor: sir.Anchor | None = None,
    open_bound: date | None = None,
) -> TimeInterval:
    """구간 표현 → 절대 구간.

    ``open_bound`` 는 한쪽이 열린 구간을 닫는 **선언된 적재 경계**다. 호출자(lowering)가
    카탈로그에서 읽어 전달하며, 없으면 열린 구간은 확정되지 않는다.
    """
    tz = zone(context.timezone)
    if isinstance(window, sir.AbsoluteWindow):
        return TimeInterval(window.start.astimezone(tz), window.end_exclusive.astimezone(tz))

    anchor_instant = resolve_anchor(anchor or sir.ReferenceAnchor(), context)

    if isinstance(window, sir.OpenEndedWindow):
        edge = resolve_anchor(window.anchor, context)
        if open_bound is None:
            raise CalendarError(
                "한쪽이 열린 구간은 선언된 적재 경계 없이는 확정할 수 없습니다"
            )
        bound = _local(open_bound.year, open_bound.month, open_bound.day, tz)
        if window.direction is sir.Direction.BEFORE:
            return TimeInterval(bound, edge)
        return TimeInterval(edge, bound)

    if isinstance(window, sir.LifetimeWindow):
        # '평생'은 구간이 아니다 — 시간 필터를 만들지 않는 쪽이 그 뜻이며, 여기서 임의의 구간을
        # 지어내면 lowering 이 만들지 않았을 경계가 생긴다.
        raise CalendarError("시간 제한 없는 구간(lifetime)은 절대 구간으로 확정하지 않습니다")

    if window.mode is sir.WindowMode.ROLLING:
        # 롤링은 길이다 — 칸에 맞추지 않는다. 월/연은 그래도 달력으로 뺀다(90일 근사 금지).
        if window.unit is sir.TimeUnit.DAY:
            start = anchor_instant - timedelta(days=window.amount)
        elif window.unit is sir.TimeUnit.WEEK:
            start = anchor_instant - timedelta(weeks=window.amount)
        elif window.unit is sir.TimeUnit.MONTH:
            start = _shift_same_day(anchor_instant, -window.amount, tz)
        else:
            start = _shift_same_day(anchor_instant, -12 * window.amount, tz)
        return TimeInterval(start, anchor_instant)

    current = bucket_containing(anchor_instant, window.unit, context.timezone)
    if window.include_current:
        first = shift_bucket(current, window.unit, -(window.amount - 1), context.timezone)
        return TimeInterval(first.start, current.end_exclusive)
    first = shift_bucket(current, window.unit, -window.amount, context.timezone)
    return TimeInterval(first.start, current.start)


def _shift_same_day(instant: datetime, months: int, tz: ZoneInfo) -> datetime:
    """같은 날짜의 N개월 전/후. 그 날짜가 없는 달(1/31 → 2월)은 그 달의 마지막 날로 접는다."""
    total = instant.year * 12 + (instant.month - 1) + months
    year, month = total // 12, total % 12 + 1
    last_day = (_add_months(_local(year, month, 1, tz), 1, tz) - timedelta(days=1)).day
    return datetime(
        year,
        month,
        min(instant.day, last_day),
        instant.hour,
        instant.minute,
        instant.second,
        instant.microsecond,
        tzinfo=tz,
    )


def enumerate_buckets(
    interval: TimeInterval, unit: sir.TimeUnit, timezone: str
) -> tuple[TimeInterval, ...]:
    """구간이 덮는 칸 전부(부분적으로만 걸친 칸도 포함)."""
    buckets: list[TimeInterval] = []
    cursor = bucket_containing(interval.start, unit, timezone)
    while cursor.start < interval.end_exclusive:
        buckets.append(cursor)
        cursor = shift_bucket(cursor, unit, 1, timezone)
        if len(buckets) > 4096:  # 구조적 폭주 방지(요청 구간이 잘못 확정된 경우)
            raise CalendarError("구간이 표현 가능한 칸 수를 넘었습니다")
    return tuple(buckets)


def expected_bucket_count(interval: TimeInterval, unit: sir.TimeUnit, timezone: str) -> int:
    """전칭 판정이 기대하는 칸의 개수."""
    return len(enumerate_buckets(interval, unit, timezone))


def is_bucket_aligned(interval: TimeInterval, unit: sir.TimeUnit, timezone: str) -> bool:
    """두 경계가 모두 칸 경계인가. 아니면 그 구간은 그 칸 단위로 답할 수 없다."""
    start_bucket = bucket_containing(interval.start, unit, timezone)
    end_bucket = bucket_containing(interval.end_exclusive, unit, timezone)
    return interval.start == start_bucket.start and interval.end_exclusive == end_bucket.start


__all__ = [
    "CalendarError",
    "TimeInterval",
    "bucket_containing",
    "enumerate_buckets",
    "expected_bucket_count",
    "is_bucket_aligned",
    "resolve_anchor",
    "resolve_window",
    "shift_bucket",
    "zone",
]
