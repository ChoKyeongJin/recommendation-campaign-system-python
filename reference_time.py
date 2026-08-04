"""Request-scoped calendar boundaries used by deterministic compilers.

Semantic SQL must not decide what "today" means at database execution time.
Callers capture a calendar date (or an already-localized, timezone-aware
instant) once at the request boundary and pass it through to these helpers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from collections.abc import Mapping, Sequence
from typing import Any
from typing import TypeAlias


ReferenceDate: TypeAlias = date | datetime

_RELATIVE_WINDOW_KEYS = frozenset(
    {"days", "window_days", "windowDays", "min_days", "max_days"}
)


class ReferenceTimeError(ValueError):
    """A semantic time boundary was missing or was not deterministic."""


def calendar_date(reference_date: ReferenceDate | None) -> date:
    """Return the request calendar date, rejecting implicit or naive clocks."""
    if isinstance(reference_date, datetime):
        if reference_date.tzinfo is None or reference_date.utcoffset() is None:
            raise ReferenceTimeError("reference datetime must be timezone-aware")
        return reference_date.date()
    if isinstance(reference_date, date):
        return reference_date
    raise ReferenceTimeError("relative date window requires reference_date")


def relative_day_char8(
    days: int,
    *,
    reference_date: ReferenceDate | None,
) -> str:
    """Resolve ``reference_date - days`` as an eight-digit calendar literal."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ReferenceTimeError("relative day count must be a non-negative integer")
    boundary = calendar_date(reference_date) - timedelta(days=days)
    return boundary.strftime("%Y%m%d")


def payload_requires_reference_date(value: Any) -> bool:
    """Detect unresolved request-relative calendar semantics in a plan payload."""

    if isinstance(value, Mapping):
        if isinstance(value.get("birthday_target"), Mapping):
            return True
        if value.get("anchor") == "reference_date":
            return True
        for key, child in value.items():
            if (
                key in _RELATIVE_WINDOW_KEYS
                and isinstance(child, int)
                and not isinstance(child, bool)
                and child > 0
            ):
                return True
            if payload_requires_reference_date(child):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(payload_requires_reference_date(item) for item in value)
    return False
