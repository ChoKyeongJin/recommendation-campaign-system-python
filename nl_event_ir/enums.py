"""Canonical enumerations — the only vocabulary the IR is allowed to speak.

이 모듈에는 **한국어 표면어가 없다**. 표면어는 :mod:`nl_event_ir.aliases` 가 소유하고,
여기에는 그 표면어들이 정규화되어 도달하는 canonical 값만 있다. 이 분리가 이 패키지의
1번 설계 원칙(Event IR 은 한국어 표면 표현을 포함하지 않는다)을 구조적으로 강제한다.

새 값을 추가하기 전에 확인할 것: 새 enum 멤버는 **새 의미**일 때만 추가한다. 같은 의미의
다른 말투(``구매``/``구입``/``샀다``)는 여기가 아니라 alias 파일 한 줄이다.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ComparisonOperator",
    "EntityType",
    "EventType",
    "IssueSeverity",
    "LogicOperator",
    "MetricType",
    "Polarity",
    "SortDirection",
    "TimeUnit",
]


class EventType(StrEnum):
    """사용자가 일으킨 사건의 종류."""

    PURCHASE = "purchase"
    LOGIN = "login"
    CART = "cart"
    SIGNUP = "signup"


class EntityType(StrEnum):
    """조건이 가리키는 대상 또는 범위 한정자의 종류."""

    BRAND = "brand"
    PRODUCT = "product"
    CATEGORY = "category"
    MEMBER = "member"


class ComparisonOperator(StrEnum):
    """스칼라 비교 연산자."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


class LogicOperator(StrEnum):
    """조건 그룹의 결합 방식."""

    AND = "and"
    OR = "or"
    NOT = "not"


class TimeUnit(StrEnum):
    """상대 기간의 단위.

    ``MONTH`` 와 ``YEAR`` 를 ``DAY`` 로 환산하지 않는다 — 달의 길이가 다르므로 환산은
    의미를 바꾼다(CLAUDE.md 16). 환산은 SQL 컴파일 단계의 책임이다.
    """

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class MetricType(StrEnum):
    """이벤트 집합에 적용되는 집계 종류."""

    EVENT_COUNT = "event_count"
    SUM = "sum"
    AVERAGE = "average"
    MAXIMUM = "maximum"
    MINIMUM = "minimum"


class SortDirection(StrEnum):
    """순위 표현의 정렬 방향. ``상위`` 라는 말만 보고 내림차순을 가정하지 않는다(CLAUDE.md 64)."""

    ASC = "asc"
    DESC = "desc"


class Polarity(StrEnum):
    """조건의 극성. ``NEGATIVE`` 는 '그 사건이 없음'을 뜻한다."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class IssueSeverity(StrEnum):
    """검증 결과의 심각도.

    ``ERROR`` 는 IR 을 신뢰할 수 없다는 뜻이고 fallback 으로 넘어간다.
    ``WARNING`` 은 IR 은 만들어졌지만 사람이 확인할 값이 있다는 뜻이다.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
