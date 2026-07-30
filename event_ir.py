"""조건 의미 IR — **소수의 범용 연산자**로 조합하는 논리·관계 대수.

배경(이 계층이 생긴 이유): 자연어 조건이 파서 초반에 고정 슬롯(``purchase_date`` /
``purchase_inactivity`` / ``behaviors:no_purchase``)으로 압축되고 있었다. 그 슬롯 집합에는 **특정 절대
기간의 사건 부재**를 담을 자리가 없어서, '올해 상반기 구매 기록이 있는 고객 중 하반기 구매 기록이 없는
고객'이 창을 잃거나 **평생 무구매**로 왜곡됐다. 근본 원인은 낱말 누락이 아니라 의미를 **표현할 수단이
없다는 것**이었다.

그래서 이 모듈의 설계 규칙은 하나다:

    **문장 유형마다 타입을 늘리지 않는다.**

``PurchaseInFirstHalf`` / ``NoPurchaseInSecondHalf`` / ``RecentThirtyDayInactive`` 같은 타입은 만들지
않는다. 그런 타입은 고정 슬롯과 같은 병(표현형 하나당 자리 하나)을 이름만 바꿔 되풀이한다. 대신 아래
연산자를 **조합**한다 — 새 문장은 새 조합이지 새 타입이 아니다.

    논리   And · Or · Not
    스칼라 Literal · FieldRef · Arithmetic · Aggregate
    관계   Source · Filter · Join · Group
    조건   Comparison · Exists · TimeFilter · TemporalRelation
    시간   AbsoluteInterval · RollingWindow · RelativeWindow · Duration

업무 개념은 타입이 아니라 **레지스트리 심볼**이다. ``purchase`` / ``login`` / ``refund`` 는 전부
``Source(name=...)`` 의 이름이고, ``purchase.amount`` / ``customer.grade`` 는 ``FieldRef(name=...)`` 의
이름이다. 실제 테이블·컬럼 바인딩은 :mod:`event_compiler` 의 ``EVENT_REGISTRY`` / ``FIELD_REGISTRY`` 가
소유한다 — 새 이벤트·필드는 레지스트리 한 줄이고 이 파일은 손대지 않는다.

부재도 전용 타입이 아니다. ``구매 없음`` 은 ``Not(Exists(Filter(Source("purchase"), TimeFilter(...))))``
이며, :func:`existence` 는 그 조합을 만들어 주는 **편의 생성자**일 뿐 별도 노드가 아니다.

새 타입을 추가해도 되는 유일한 경우는 **여러 도메인에서 재사용되는 새 논리 관계**다. 예를 들어
'첫 구매 후 30일 이내 재구매'는 기존 연산자로 표현할 수 없는 두 사건 사이의 시간 관계라
:class:`TemporalRelation` 을 추가했고, 그 노드는 '가입 후 7일 이내 구매', '배송 후 30일 이내 반품',
'쿠폰 발급 후 14일 이내 사용'에 그대로 쓰인다. 문장 하나를 위한 타입은 설계 거부다.

계층: :mod:`event_parser`(자연어 → IR) → **이 모듈**(IR + 검증) → :mod:`event_compiler`(IR → SQL).
이 파일에는 한국어 표면어도 SQL 문자열도 없다 — 그 둘이 같은 노드에 섞이면 계층이 무너진다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 스키마 강제는 dataclass ``__post_init__`` +
:func:`node_from_dict` 의 닫힌 판별(discriminator)이 담당한다(pydantic 을 쓰지 않는 이유는 파서 import
체인에 새 런타임 의존을 넣지 않기 위해서다). LLM 출력용 JSON Schema 는 같은 정의에서 파생한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import lexicon_patterns

# ── 예외 ──────────────────────────────────────────────────────────────────────────


class SemanticLossError(Exception):
    """원문에 있던 의미(부정·시간·근거)가 IR 에서 사라졌다. 축소하지 말고 여기서 멈춘다."""


class IrSchemaError(ValueError):
    """IR 스키마 위반(닫힌 어휘 밖의 값, 필수 필드 누락, 잘못된 구간)."""


# ── 근거 구간 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evidence:
    """이 조건이 원문 어디에서 나왔는지. **원자 조건**(Comparison/Exists/TemporalRelation)에 필수다.

    And/Or/Not 같은 결합 노드는 근거를 따로 갖지 않는다 — 자식들의 구간 합집합이 곧 그 노드의 출처이고
    (:func:`evidence_span`), 결합자에 근거를 중복 저장하면 두 값이 갈라진다."""

    text: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise IrSchemaError("evidence.text must be a string")
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise IrSchemaError("evidence offsets must be integers")

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, raw: Any) -> "Evidence":
        if not isinstance(raw, dict):
            raise IrSchemaError("evidence must be an object")
        return cls(text=str(raw.get("text", "")), start=int(raw.get("start", -1)), end=int(raw.get("end", -1)))


# ── 시간 ──────────────────────────────────────────────────────────────────────────
# 절대 구간은 **반개구간**이다: start <= t < end_exclusive. 날짜 컬럼이 timestamp 인 경우 BETWEEN 은
# 마지막 날의 00:00 이후를 잘라먹는다 — 경계 규칙을 IR 층에서 한 번만 정한다.

WINDOW_UNITS: tuple[str, ...] = ("day", "week", "month", "year")
DURATION_UNITS: tuple[str, ...] = ("hour", *WINDOW_UNITS)
UNIT_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}
_UNIT_PLURAL: dict[str, str] = {"day": "days", "week": "weeks", "month": "months", "year": "years"}
_UNIT_ALIASES: dict[str, str] = {
    **{unit: unit for unit in DURATION_UNITS},
    **{plural: unit for unit, plural in _UNIT_PLURAL.items()},
    "hours": "hour",
}

_YMD8 = re.compile(r"^\d{8}$")


def canonical_unit(raw: Any) -> str | None:
    """'days'/'day' 등 표기 흔들림을 캐노니컬 단수형으로 접는다(모르는 단위는 None)."""
    return _UNIT_ALIASES.get(str(raw).strip().casefold()) if raw is not None else None


@dataclass(frozen=True)
class AbsoluteInterval:
    """달력상 확정된 반개구간 [start, end_exclusive)."""

    start: date
    end_exclusive: date
    type: str = "interval"

    def __post_init__(self) -> None:
        if not isinstance(self.start, date) or not isinstance(self.end_exclusive, date):
            raise IrSchemaError("interval bounds must be dates")
        if self.start >= self.end_exclusive:
            raise IrSchemaError(f"empty interval: {self.start} >= {self.end_exclusive}")

    @property
    def inclusive_end(self) -> date:
        """포함 끝일(YYYYMMDD 문자열 비교 관례를 쓰는 컬럼용)."""
        return self.end_exclusive - timedelta(days=1)

    def to_dict(self) -> dict[str, Any]:
        """직렬화. ``from``/``to``(YYYYMMDD 포함 구간)는 기존 달력 슬롯 소비자와의 **파생 호환 표기**다.

        캐노니컬은 ``start``/``end_exclusive`` 이고 :meth:`from_dict` 는 캐노니컬만 읽는다 — 호환 표기를
        입력으로 신뢰하면 두 진실이 생긴다. 이 표기가 필요한 이유는 plan 전체를 구조로 훑어 '이미 주인
        있는 달력 구간'을 세는 소비자(graph_rag 의 고아 창 판정)가 있기 때문이다."""
        return {
            "type": "interval",
            "start": self.start.isoformat(),
            "end_exclusive": self.end_exclusive.isoformat(),
            "from": self.start.strftime("%Y%m%d"),
            "to": self.inclusive_end.strftime("%Y%m%d"),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AbsoluteInterval":
        try:
            return cls(
                start=date.fromisoformat(str(raw["start"])),
                end_exclusive=date.fromisoformat(str(raw["end_exclusive"])),
            )
        except (KeyError, ValueError) as exc:
            raise IrSchemaError(f"invalid interval: {raw}") from exc

    @classmethod
    def from_calendar_window(cls, window: dict[str, Any]) -> "AbsoluteInterval | None":
        """기존 달력 창 표기 ``{from, to}``(YYYYMMDD 포함 구간) → 반개구간.

        :mod:`calendar_window` 가 달력 문법의 단일 소유자이므로 이 IR 은 그 산출물을 받아 경계 규칙만
        바꾼다 — 달력을 여기서 다시 구현하지 않는다."""
        start_raw, end_raw = str(window.get("from") or ""), str(window.get("to") or "")
        if not (_YMD8.match(start_raw) and _YMD8.match(end_raw)):
            return None
        start = date(int(start_raw[:4]), int(start_raw[4:6]), int(start_raw[6:]))
        end = date(int(end_raw[:4]), int(end_raw[4:6]), int(end_raw[6:]))
        if end < start:
            start, end = end, start
        return cls(start=start, end_exclusive=end + timedelta(days=1))

    def to_calendar_window(self) -> dict[str, str]:
        return {"from": self.start.strftime("%Y%m%d"), "to": self.inclusive_end.strftime("%Y%m%d")}


@dataclass(frozen=True)
class RollingWindow:
    """기준 시각으로부터 거슬러 세는 창('최근 30일'). 경계가 **실행 시점** 함수로 컴파일된다."""

    value: int
    unit: str
    type: str = "rolling"

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise IrSchemaError(f"rolling window value must be a positive int: {self.value!r}")
        if self.unit not in WINDOW_UNITS:
            raise IrSchemaError(f"unsupported rolling unit: {self.unit!r}")

    @property
    def days(self) -> int:
        return self.value * UNIT_DAYS[self.unit]

    @property
    def plural_unit(self) -> str:
        """기존 슬롯 표기(``{value, unit:'days', min_days}``)와 맞추기 위한 복수형."""
        return _UNIT_PLURAL[self.unit]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "rolling", "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RollingWindow":
        unit = canonical_unit(raw.get("unit"))
        if unit not in WINDOW_UNITS:
            raise IrSchemaError(f"unsupported rolling unit: {raw.get('unit')!r}")
        try:
            return cls(value=int(raw["value"]), unit=str(unit))
        except (KeyError, TypeError, ValueError) as exc:
            raise IrSchemaError(f"invalid rolling window: {raw}") from exc


@dataclass(frozen=True)
class RelativeWindow:
    """'N단위 전'이 가리키는 **달력 구간**('3개월 전' = 그 달 전체, '7년 전' = 그 해 전체).

    롤링 창과 다르다: 롤링은 길이('최근 30일')이고 이쪽은 과거의 한 시점이 속한 달력 칸이다. 컴파일러가
    기준일로 절대 구간으로 확정한다 — 계획 시점에 날짜가 드러나야 감사 가능하다는 기존 관례를 따른다."""

    value: int
    unit: str
    direction: str = "past"
    type: str = "relative"

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise IrSchemaError(f"relative window value must be a positive int: {self.value!r}")
        if self.unit not in WINDOW_UNITS:
            raise IrSchemaError(f"unsupported relative unit: {self.unit!r}")
        if self.direction != "past":
            raise IrSchemaError(f"unsupported relative direction: {self.direction!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "relative", "value": self.value, "unit": self.unit, "direction": self.direction}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RelativeWindow":
        unit = canonical_unit(raw.get("unit"))
        if unit not in WINDOW_UNITS:
            raise IrSchemaError(f"unsupported relative unit: {raw.get('unit')!r}")
        try:
            return cls(value=int(raw["value"]), unit=str(unit), direction=str(raw.get("direction", "past")))
        except (KeyError, TypeError, ValueError) as exc:
            raise IrSchemaError(f"invalid relative window: {raw}") from exc


@dataclass(frozen=True)
class Duration:
    """기간의 **길이**(창이 아니다). 시간 관계 연산자의 폭을 나타낸다('30일 이내')."""

    value: int
    unit: str
    type: str = "duration"

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise IrSchemaError(f"duration value must be a positive int: {self.value!r}")
        if self.unit not in DURATION_UNITS:
            raise IrSchemaError(f"unsupported duration unit: {self.unit!r}")

    @property
    def days(self) -> int | None:
        """일 단위 환산(시간 단위는 날짜 컬럼으로 표현 불가라 None)."""
        return None if self.unit == "hour" else self.value * UNIT_DAYS[self.unit]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "duration", "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Duration":
        unit = canonical_unit(raw.get("unit"))
        if unit not in DURATION_UNITS:
            raise IrSchemaError(f"unsupported duration unit: {raw.get('unit')!r}")
        try:
            return cls(value=int(raw["value"]), unit=str(unit))
        except (KeyError, TypeError, ValueError) as exc:
            raise IrSchemaError(f"invalid duration: {raw}") from exc


TimeWindow = AbsoluteInterval | RollingWindow | RelativeWindow


def resolve_relative_window(window: RelativeWindow, today: date | None = None) -> AbsoluteInterval:
    """'N단위 전'이 속한 달력 구간을 기준일로 확정한다(계획 시점 해석 — 기존 관례).

    시간 노드의 의미이므로 컴파일러가 아니라 여기 산다 — 컴파일러에 두면 슬롯 변환(호환 계층)이
    같은 계산을 한 벌 더 갖게 되고, 두 벌은 결국 갈라진다."""
    anchor = today or date.today()
    if window.unit == "year":
        year = anchor.year - window.value
        return AbsoluteInterval(start=date(year, 1, 1), end_exclusive=date(year + 1, 1, 1))
    if window.unit == "month":
        total = anchor.year * 12 + (anchor.month - 1) - window.value
        end = total + 1
        return AbsoluteInterval(
            start=date(total // 12, total % 12 + 1, 1),
            end_exclusive=date(end // 12, end % 12 + 1, 1),
        )
    target = anchor - timedelta(days=window.value * (7 if window.unit == "week" else 1))
    if window.unit == "week":
        start = target - timedelta(days=target.weekday())
        return AbsoluteInterval(start=start, end_exclusive=start + timedelta(days=7))
    return AbsoluteInterval(start=target, end_exclusive=target + timedelta(days=1))


def window_from_dict(raw: Any) -> TimeWindow:
    if not isinstance(raw, dict):
        raise IrSchemaError("time window must be an object")
    kind = raw.get("type")
    if kind == "interval":
        return AbsoluteInterval.from_dict(raw)
    if kind == "rolling":
        return RollingWindow.from_dict(raw)
    if kind == "relative":
        return RelativeWindow.from_dict(raw)
    raise IrSchemaError(f"unknown time window type: {kind!r}")


# ── 스칼라 표현 ───────────────────────────────────────────────────────────────────

COMPARISON_OPERATORS: frozenset[str] = frozenset({"=", "!=", ">", ">=", "<", "<="})
ARITHMETIC_OPERATORS: frozenset[str] = frozenset({"+", "-", "*", "/"})
AGGREGATE_FUNCTIONS: frozenset[str] = frozenset({"count", "sum", "avg", "min", "max"})


@dataclass(frozen=True)
class Literal:
    value: Any
    type: str = "literal"

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float, str, bool)):
            raise IrSchemaError(f"unsupported literal: {self.value!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "literal", "value": self.value}


@dataclass(frozen=True)
class FieldRef:
    """필드 **심볼**('purchase.amount', 'subject.grade'). 물리 컬럼은 FIELD_REGISTRY 가 소유한다."""

    name: str
    type: str = "field"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or "." not in self.name:
            raise IrSchemaError(f"field reference must be '<source>.<field>': {self.name!r}")

    @property
    def source(self) -> str:
        return self.name.split(".", 1)[0]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "field", "name": self.name}


@dataclass(frozen=True)
class Arithmetic:
    operator: str
    left: "Scalar"
    right: "Scalar"
    type: str = "arithmetic"

    def __post_init__(self) -> None:
        if self.operator not in ARITHMETIC_OPERATORS:
            raise IrSchemaError(f"unsupported arithmetic operator: {self.operator!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "arithmetic", "operator": self.operator,
            "left": self.left.to_dict(), "right": self.right.to_dict(),
        }


@dataclass(frozen=True)
class Aggregate:
    """관계에 대한 집계. 횟수·합계·평균·최소·최대가 **같은 노드**다(지표마다 타입을 만들지 않는다)."""

    function: str
    relation: "Relation"
    expression: "Scalar | None" = None  # count(*) 는 None
    distinct: bool = False
    type: str = "aggregate"

    def __post_init__(self) -> None:
        if self.function not in AGGREGATE_FUNCTIONS:
            raise IrSchemaError(f"unsupported aggregate function: {self.function!r}")
        if self.function != "count" and self.expression is None:
            raise IrSchemaError(f"aggregate '{self.function}' needs an expression")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "aggregate", "function": self.function, "distinct": self.distinct,
            "expression": self.expression.to_dict() if self.expression is not None else None,
            "relation": self.relation.to_dict(),
        }


Scalar = Literal | FieldRef | Arithmetic | Aggregate


# ── 관계 표현 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    """업무 이벤트/엔터티의 **심볼**('purchase', 'login', 'refund'). 새 이벤트는 레지스트리 한 줄이다."""

    name: str
    type: str = "source"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise IrSchemaError("source needs a non-empty name")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "source", "name": self.name}


@dataclass(frozen=True)
class Filter:
    relation: "Relation"
    where: "Condition"
    type: str = "filter"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "filter", "relation": self.relation.to_dict(), "where": self.where.to_dict()}


@dataclass(frozen=True)
class Join:
    """두 소스의 조인(예: 주문상세 ⨝ 상품). ``on`` 은 두 FieldRef 의 Comparison 이다."""

    left: "Relation"
    right: Source
    on: "Comparison"
    type: str = "join"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "join", "left": self.left.to_dict(), "right": self.right.to_dict(), "on": self.on.to_dict()}


@dataclass(frozen=True)
class Group:
    """집계의 **grain** 을 명시하는 노드. 없으면 주체(회원) 단위 집계다.

    grain 을 노드로 드러내는 이유: '3개 이상 구매'가 회원 단위인지 주문 단위인지는 업무 규칙이고,
    그 규칙이 컴파일러 안에 숨어 있으면 문장마다 다른 답이 나와도 드러나지 않는다."""

    relation: "Relation"
    keys: tuple[FieldRef, ...]
    type: str = "group"

    def __post_init__(self) -> None:
        if not self.keys:
            raise IrSchemaError("group needs at least one key")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "group", "relation": self.relation.to_dict(), "keys": [key.to_dict() for key in self.keys]}


Relation = Source | Filter | Join | Group


# ── 조건(불리언) ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Comparison:
    operator: str
    left: Scalar
    right: Scalar
    evidence: Evidence | None = None
    type: str = "comparison"

    def __post_init__(self) -> None:
        if self.operator not in COMPARISON_OPERATORS:
            raise IrSchemaError(f"unsupported comparison operator: {self.operator!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "comparison", "operator": self.operator,
            "left": self.left.to_dict(), "right": self.right.to_dict(),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class Exists:
    """관계에 행이 하나라도 있는가. 부재는 :class:`Not` 로 감싼다 — 전용 타입을 두지 않는다."""

    relation: Relation
    evidence: Evidence | None = None
    type: str = "exists"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "exists", "relation": self.relation.to_dict(),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class TimeFilter:
    """필드가 창 안에 드는가. 두 개의 Comparison 으로 풀 수도 있지만 노드로 두는 이유는 '이 창은 하나의
    시간 표현'이라는 사실이 검증(시간 소실 탐지)과 근거 계산에 필요하기 때문이다."""

    field: FieldRef
    window: TimeWindow
    type: str = "time_filter"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "time_filter", "field": self.field.to_dict(), "window": self.window.to_dict()}


@dataclass(frozen=True)
class EventReference:
    """시간 관계의 피연산자 — 어떤 소스의 어느 발생 시점인가."""

    source: str
    selector: str = "any"  # first | last | any | subsequent
    type: str = "event_reference"

    def __post_init__(self) -> None:
        if self.selector not in ("first", "last", "any", "subsequent"):
            raise IrSchemaError(f"unsupported event selector: {self.selector!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "event_reference", "source": self.source, "selector": self.selector}


@dataclass(frozen=True)
class TemporalRelation:
    """두 사건 발생 시점 사이의 관계('A 후 D 이내에 B').

    기존 연산자로는 표현할 수 없어 추가한 **범용** 관계다 — 특정 문장을 위한 타입이 아니라는 근거는
    같은 노드가 '첫 구매 후 30일 이내 재구매', '가입 후 7일 이내 구매', '배송 후 30일 이내 반품',
    '쿠폰 발급 후 14일 이내 사용'에 그대로 쓰인다는 점이다(소스 이름만 다르다)."""

    operator: str
    left: EventReference
    right: EventReference
    duration: Duration
    evidence: Evidence | None = None
    type: str = "temporal_relation"

    def __post_init__(self) -> None:
        if self.operator not in ("within_after", "within_before"):
            raise IrSchemaError(f"unsupported temporal operator: {self.operator!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "temporal_relation", "operator": self.operator,
            "left": self.left.to_dict(), "right": self.right.to_dict(),
            "duration": self.duration.to_dict(),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class Not:
    operand: "Condition"
    type: str = "not"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not", "operand": self.operand.to_dict()}


@dataclass(frozen=True)
class And:
    operands: tuple["Condition", ...]
    type: str = "and"

    def __post_init__(self) -> None:
        if not self.operands:
            raise IrSchemaError("and needs at least one operand")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "and", "operands": [operand.to_dict() for operand in self.operands]}


@dataclass(frozen=True)
class Or:
    operands: tuple["Condition", ...]
    type: str = "or"

    def __post_init__(self) -> None:
        if not self.operands:
            raise IrSchemaError("or needs at least one operand")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "or", "operands": [operand.to_dict() for operand in self.operands]}


Condition = And | Or | Not | Comparison | Exists | TimeFilter | TemporalRelation

# 원자 조건: 그 자체로 하나의 사용자 의도인 노드(결합자가 아닌 것). 근거·검증의 단위다.
ATOM_TYPES = (Comparison, Exists, TemporalRelation)

# 노드 타입 목록(닫힌 집합). 계약 테스트가 '문장이 늘어도 이 목록은 늘지 않는다'를 강제한다.
NODE_TYPES: frozenset[str] = frozenset({
    "and", "or", "not",
    "literal", "field", "arithmetic", "aggregate",
    "source", "filter", "join", "group",
    "comparison", "exists", "time_filter", "temporal_relation", "event_reference",
    "interval", "rolling", "relative", "duration",
})


# ── 역직렬화(닫힌 판별자) ─────────────────────────────────────────────────────────

def _evidence_or_none(raw: Any) -> Evidence | None:
    return Evidence.from_dict(raw) if isinstance(raw, dict) else None


def scalar_from_dict(raw: Any) -> Scalar:
    if not isinstance(raw, dict):
        raise IrSchemaError("scalar must be an object")
    kind = raw.get("type")
    if kind == "literal":
        return Literal(value=raw.get("value"))
    if kind == "field":
        return FieldRef(name=str(raw.get("name") or ""))
    if kind == "arithmetic":
        return Arithmetic(
            operator=str(raw.get("operator")),
            left=scalar_from_dict(raw.get("left")),
            right=scalar_from_dict(raw.get("right")),
        )
    if kind == "aggregate":
        expression = raw.get("expression")
        return Aggregate(
            function=str(raw.get("function")),
            relation=relation_from_dict(raw.get("relation")),
            expression=scalar_from_dict(expression) if isinstance(expression, dict) else None,
            distinct=bool(raw.get("distinct")),
        )
    raise IrSchemaError(f"unknown scalar type: {kind!r}")


def relation_from_dict(raw: Any) -> Relation:
    if not isinstance(raw, dict):
        raise IrSchemaError("relation must be an object")
    kind = raw.get("type")
    if kind == "source":
        return Source(name=str(raw.get("name") or ""))
    if kind == "filter":
        return Filter(relation=relation_from_dict(raw.get("relation")), where=condition_from_dict(raw.get("where")))
    if kind == "join":
        on = condition_from_dict(raw.get("on"))
        if not isinstance(on, Comparison):
            raise IrSchemaError("join 'on' must be a comparison")
        right = relation_from_dict(raw.get("right"))
        if not isinstance(right, Source):
            raise IrSchemaError("join right side must be a source")
        return Join(left=relation_from_dict(raw.get("left")), right=right, on=on)
    if kind == "group":
        keys = raw.get("keys")
        if not isinstance(keys, list) or not keys:
            raise IrSchemaError("group needs keys")
        return Group(
            relation=relation_from_dict(raw.get("relation")),
            keys=tuple(FieldRef(name=str(key.get("name") or "")) for key in keys if isinstance(key, dict)),
        )
    raise IrSchemaError(f"unknown relation type: {kind!r}")


def condition_from_dict(raw: Any) -> Condition:
    if not isinstance(raw, dict):
        raise IrSchemaError("condition must be an object")
    kind = raw.get("type")
    if kind == "and":
        return And(operands=tuple(_operands(raw)))
    if kind == "or":
        return Or(operands=tuple(_operands(raw)))
    if kind == "not":
        return Not(operand=condition_from_dict(raw.get("operand")))
    if kind == "comparison":
        return Comparison(
            operator=str(raw.get("operator")),
            left=scalar_from_dict(raw.get("left")),
            right=scalar_from_dict(raw.get("right")),
            evidence=_evidence_or_none(raw.get("evidence")),
        )
    if kind == "exists":
        return Exists(relation=relation_from_dict(raw.get("relation")), evidence=_evidence_or_none(raw.get("evidence")))
    if kind == "time_filter":
        field_raw = raw.get("field")
        if not isinstance(field_raw, dict):
            raise IrSchemaError("time_filter needs a field")
        return TimeFilter(field=FieldRef(name=str(field_raw.get("name") or "")), window=window_from_dict(raw.get("window")))
    if kind == "temporal_relation":
        return TemporalRelation(
            operator=str(raw.get("operator")),
            left=_event_reference(raw.get("left")),
            right=_event_reference(raw.get("right")),
            duration=Duration.from_dict(raw.get("duration") if isinstance(raw.get("duration"), dict) else {}),
            evidence=_evidence_or_none(raw.get("evidence")),
        )
    raise IrSchemaError(f"unknown condition type: {kind!r}")


def _operands(raw: dict[str, Any]) -> list[Condition]:
    operands = raw.get("operands")
    if not isinstance(operands, list) or not operands:
        raise IrSchemaError("boolean node needs a non-empty operand list")
    return [condition_from_dict(item) for item in operands]


def _event_reference(raw: Any) -> EventReference:
    if not isinstance(raw, dict):
        raise IrSchemaError("event reference must be an object")
    return EventReference(source=str(raw.get("source") or ""), selector=str(raw.get("selector") or "any"))


# 외부에서 쓰는 단일 진입점(예전 이름 유지 — plan/LLM 페이로드 역직렬화).
node_from_dict = condition_from_dict
expression_from_dict = condition_from_dict
Expression = Condition


# ── 편의 생성자(새 노드가 아니라 조합) ─────────────────────────────────────────────

TIME_FIELD_SUFFIX = "occurred_at"


def time_field(source: str) -> FieldRef:
    """소스의 발생 시각 필드 심볼. 이벤트를 등록하면 이 필드는 자동으로 생긴다."""
    return FieldRef(name=f"{source}.{TIME_FIELD_SUFFIX}")


def event_relation(source: str, window: TimeWindow | None = None) -> Relation:
    relation: Relation = Source(name=source)
    if window is not None:
        relation = Filter(relation=relation, where=TimeFilter(field=time_field(source), window=window))
    return relation


def existence(
    source: str,
    *,
    negated: bool = False,
    window: TimeWindow | None = None,
    evidence: Evidence | None = None,
) -> Condition:
    """'<사건>이 (기간 안에) 있다/없다' 조합. ``not_exists`` 는 편의 문법일 뿐 ``Not(Exists(...))`` 다."""
    node: Condition = Exists(relation=event_relation(source, window), evidence=evidence)
    return Not(operand=node) if negated else node


def conjunction(operands: list[Condition]) -> Condition:
    if not operands:
        raise IrSchemaError("cannot build an empty conjunction")
    return operands[0] if len(operands) == 1 else And(operands=tuple(operands))


def disjunction(operands: list[Condition]) -> Condition:
    if not operands:
        raise IrSchemaError("cannot build an empty disjunction")
    return operands[0] if len(operands) == 1 else Or(operands=tuple(operands))


# ── 순회 ──────────────────────────────────────────────────────────────────────────


def walk(node: Any) -> Iterator[Any]:
    """IR 트리의 모든 노드(조건·관계·스칼라·시간)를 깊이 우선으로."""
    if node is None:
        return
    yield node
    for child in _children(node):
        yield from walk(child)


def _children(node: Any) -> list[Any]:
    if isinstance(node, (And, Or)):
        return list(node.operands)
    if isinstance(node, Not):
        return [node.operand]
    if isinstance(node, Comparison):
        return [node.left, node.right]
    if isinstance(node, Exists):
        return [node.relation]
    if isinstance(node, TimeFilter):
        return [node.field, node.window]
    if isinstance(node, TemporalRelation):
        return [node.left, node.right, node.duration]
    if isinstance(node, Filter):
        return [node.relation, node.where]
    if isinstance(node, Join):
        return [node.left, node.right, node.on]
    if isinstance(node, Group):
        return [node.relation, *node.keys]
    if isinstance(node, Aggregate):
        return [node.relation] + ([node.expression] if node.expression is not None else [])
    if isinstance(node, Arithmetic):
        return [node.left, node.right]
    return []


def iter_atoms(expression: Condition) -> Iterator[Comparison | Exists | TemporalRelation]:
    """원자 조건(결합자가 아닌 노드)을 등장 순서대로."""
    for node in walk(expression):
        if isinstance(node, ATOM_TYPES):
            yield node


def iter_signed_atoms(
    expression: Condition, negated: bool = False
) -> Iterator[tuple[Comparison | Exists | TemporalRelation, bool]]:
    """원자 조건 + **그 위에 쌓인 Not 의 유효 극성**.

    ``iter_atoms`` 만 쓰면 ``Not(Exists(...))`` 의 부정이 보이지 않는다 — 원자만 보고 SQL 을 만들거나
    커버리지를 판정하면 극성이 조용히 뒤집힌다(이 리팩터가 고친 바로 그 사고의 축소판)."""
    if isinstance(expression, Not):
        yield from iter_signed_atoms(expression.operand, not negated)
        return
    if isinstance(expression, (And, Or)):
        for operand in expression.operands:
            yield from iter_signed_atoms(operand, negated)
        return
    if isinstance(expression, ATOM_TYPES):
        yield expression, negated


def node_type_names(expression: Condition) -> set[str]:
    """트리에 등장한 노드 타입 이름 집합(계약 테스트가 닫힌 집합인지 확인한다)."""
    return {getattr(node, "type") for node in walk(expression) if hasattr(node, "type")}


def sources(expression: Condition) -> set[str]:
    """참조된 소스 심볼 전부(레지스트리 검증·라벨용)."""
    names = {node.name for node in walk(expression) if isinstance(node, Source)}
    names |= {node.source for node in walk(expression) if isinstance(node, EventReference)}
    names |= {node.source for node in walk(expression) if isinstance(node, FieldRef)}
    return names


def field_names(expression: Condition) -> set[str]:
    return {node.name for node in walk(expression) if isinstance(node, FieldRef)}


def time_windows(expression: Condition) -> list[TimeWindow]:
    return [node.window for node in walk(expression) if isinstance(node, TimeFilter)]


def count_time_constraints(expression: Condition) -> int:
    return len(time_windows(expression))


def has_operator(expression: Condition, operator: str) -> bool:
    return any(getattr(node, "type", None) == operator for node in walk(expression))


def count_negations(expression: Condition) -> int:
    return sum(1 for node in walk(expression) if isinstance(node, Not))


def evidence_span(expression: Condition) -> tuple[int, int] | None:
    """식 전체가 덮는 원문 구간(소유권 판정용). 근거가 하나도 없으면 None."""
    spans = [
        (atom.evidence.start, atom.evidence.end)
        for atom in iter_atoms(expression)
        if atom.evidence is not None and atom.evidence.start >= 0 and atom.evidence.end > atom.evidence.start
    ]
    if not spans:
        return None
    return min(start for start, _ in spans), max(end for _, end in spans)


# ── 존재 조건 뷰(파생 — 타입이 아니다) ─────────────────────────────────────────────
# graph_rag 의 커버리지·라벨·드롭 판정은 '어떤 소스의 존재/부재가 어느 창에 걸렸나'만 알면 된다.
# 그 질문에 답하려고 매번 트리를 훑는 코드를 복제하지 않도록 **읽기 전용 투영**을 제공한다.
# 이것은 IR 노드가 아니라 조회 결과다 — 저장하거나 컴파일하지 않는다.


@dataclass(frozen=True)
class ExistenceView:
    source: str
    negated: bool
    window: TimeWindow | None
    evidence: Evidence | None


def existence_views(expression: Condition, negated: bool = False) -> list[ExistenceView]:
    """``Not(Exists(Filter(Source, TimeFilter)))`` 조합을 (소스, 극성, 창)으로 읽어 준다."""
    if isinstance(expression, Not):
        return existence_views(expression.operand, not negated)
    if isinstance(expression, (And, Or)):
        return [view for operand in expression.operands for view in existence_views(operand, negated)]
    if isinstance(expression, Exists):
        relation_sources = {
            node.name for node in walk(expression.relation) if isinstance(node, Source)
        }
        windows = [
            node.window for node in walk(expression.relation) if isinstance(node, TimeFilter)
        ]
        if len(relation_sources) == 1 and len(windows) <= 1:
            return [ExistenceView(
                next(iter(relation_sources)),
                negated,
                windows[0] if windows else None,
                expression.evidence,
            )]
    return []


def covers_existence(expression: Condition, source: str, negated: bool) -> bool:
    return any(view.source == source and view.negated == negated for view in existence_views(expression))


# ── 의미 보존 검증 ─────────────────────────────────────────────────────────────────
# 검증이 파서와 **다른 근거**를 봐야 의미가 있다. 그래서 부정 탐지는 구매 전용 정규식이 아니라
# 도메인 무관 부정 어휘를 쓴다 — 파서가 쓰는 바로 그 정규식으로 파서를 검사하면, 낱말이 빠졌을 때
# 파서와 검증이 **같이** 못 보고 조용히 통과한다.

_NEGATION_ALT = lexicon_patterns.alternation("generic_negation")
_COUNT_UNIT_ALT = lexicon_patterns.alternation("event_count_unit")
GENERIC_NEGATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?:{_NEGATION_ALT})"),
    # Numeric boundaries are required on both sides.  Without them the last
    # zero of ``5,000개`` is mistaken for the negative expression ``0개``.
    re.compile(rf"(?<![\d,.])0(?![\d,.])\s*(?:{_COUNT_UNIT_ALT})"),
)


def source_contains_negation(text: str) -> bool:
    """원문에 부정 표현이 있는가(도메인 무관 — 어느 사건이든)."""
    if not text:
        return False
    compact = text.replace(" ", "")
    return any(pattern.search(compact) or pattern.search(text) for pattern in GENERIC_NEGATION_PATTERNS)


def validate_evidence(expression: Condition) -> None:
    """모든 **원자 조건**이 유효한 원문 근거 구간을 갖는지."""
    atoms = list(iter_atoms(expression))
    if not atoms:
        raise SemanticLossError("조건이 하나도 없습니다(no atomic condition in the expression)")
    for atom in atoms:
        if atom.evidence is None or not atom.evidence.text.strip():
            raise SemanticLossError(f"조건에 원문 근거가 없습니다: {atom.type}")
        if atom.evidence.start >= atom.evidence.end:
            raise SemanticLossError(
                f"근거 구간이 잘못되었습니다: [{atom.evidence.start}, {atom.evidence.end})"
            )


def validate_negation_preserved(source_text: str, expression: Condition) -> None:
    """원문에 부정이 있는데 IR 에 부정 노드가 하나도 없으면 실패.

    ``Not`` 노드뿐 아니라 ``= 0`` / ``< N`` 같은 부정형 비교도 부정으로 인정한다 — 같은 뜻을 다른
    연산자로 적었다는 이유로 fail-close 하면 검증이 잡음이 된다."""
    if not source_contains_negation(source_text):
        return
    negative_comparison = any(
        isinstance(atom, Comparison)
        and (atom.operator in ("<", "<=", "!=") or (atom.operator == "=" and getattr(atom.right, "value", None) == 0))
        for atom in iter_atoms(expression)
    )
    if count_negations(expression) == 0 and not negative_comparison:
        raise SemanticLossError(
            "원문에 부정 표현이 있는데 IR 에 부정 조건이 없습니다"
            " (source contains negation but IR has no negative predicate)"
        )


def validate_time_preserved(parsed_time_span_count: int, expression: Condition) -> None:
    """달력 파서가 읽은 시간 표현 수와 IR 의 시간 조건 수가 다르면 실패(창이 조용히 사라지는 것 방지)."""
    ir_time_count = count_time_constraints(expression)
    if parsed_time_span_count != ir_time_count:
        raise SemanticLossError(
            f"시간 조건 수가 일치하지 않습니다: source={parsed_time_span_count}, ir={ir_time_count}"
        )


def validate_expression(
    source_text: str,
    expression: Condition,
    *,
    parsed_time_span_count: int | None = None,
) -> None:
    """세 검증을 한 번에(근거 → 부정 보존 → 시간 보존). 실패는 예외이고, 축소 폴백은 없다."""
    validate_evidence(expression)
    validate_negation_preserved(source_text, expression)
    if parsed_time_span_count is not None:
        validate_time_preserved(parsed_time_span_count, expression)


# ── LLM 구조화 출력 스키마 ─────────────────────────────────────────────────────────
# 자유 형식 JSON 을 허용하지 않는다. 같은 정의에서 파생하므로 코드와 스키마가 갈라지지 않는다.


def condition_json_schema(depth: int = 3) -> dict[str, Any]:
    """조건 노드 JSON Schema($ref 없이 depth 만큼 펼친 형태 — strict tool 스키마 호환)."""
    evidence_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "이 조건의 근거가 된 원문 구절 그대로"},
            "start": {"type": "integer"}, "end": {"type": "integer"},
        },
        "required": ["text", "start", "end"],
    }
    window_schema = {
        "type": "object",
        "description": (
            "기간. {type:'interval', start:'YYYY-MM-DD', end_exclusive:'YYYY-MM-DD'}(끝 미포함) | "
            "{type:'rolling'|'relative', value:int, unit:'day'|'week'|'month'|'year'}"
        ),
        "properties": {
            "type": {"type": "string", "enum": ["interval", "rolling", "relative"]},
            "start": {"type": "string"}, "end_exclusive": {"type": "string"},
            "value": {"type": "integer"}, "unit": {"type": "string", "enum": list(WINDOW_UNITS)},
        },
        "required": ["type"],
    }
    source_schema = {
        "type": "object",
        "description": "업무 이벤트 심볼(purchase/login/... — 레지스트리에 등록된 이름).",
        "properties": {"type": {"type": "string", "enum": ["source"]}, "name": {"type": "string"}},
        "required": ["type", "name"],
    }
    time_filter_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["time_filter"]},
            "field": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["field"]}, "name": {"type": "string"}}, "required": ["type", "name"]},
            "window": window_schema,
        },
        "required": ["type", "field", "window"],
    }
    relation_schema = {
        "type": "object",
        "description": "관계. {type:'source'} 또는 {type:'filter', relation:<source>, where:<time_filter>}",
        "properties": {
            "type": {"type": "string", "enum": ["source", "filter"]},
            "name": {"type": "string"},
            "relation": source_schema,
            "where": time_filter_schema,
        },
        "required": ["type"],
    }
    scalar_schema = {
        "type": "object",
        "description": "스칼라. {type:'literal', value} | {type:'field', name:'<source>.<field>'} | "
                       "{type:'aggregate', function:'count'|'sum'|'avg'|'min'|'max', relation, expression?, distinct?}",
        "properties": {
            "type": {"type": "string", "enum": ["literal", "field", "aggregate"]},
            "value": {}, "name": {"type": "string"},
            "function": {"type": "string", "enum": sorted(AGGREGATE_FUNCTIONS)},
            "distinct": {"type": "boolean"},
            "relation": relation_schema,
        },
        "required": ["type"],
    }
    atom_schemas = [
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["exists"]},
                "relation": relation_schema, "evidence": evidence_schema,
            },
            "required": ["type", "relation", "evidence"],
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["comparison"]},
                "operator": {"type": "string", "enum": sorted(COMPARISON_OPERATORS)},
                "left": scalar_schema, "right": scalar_schema, "evidence": evidence_schema,
            },
            "required": ["type", "operator", "left", "right", "evidence"],
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["temporal_relation"]},
                "operator": {"type": "string", "enum": ["within_after", "within_before"]},
                "left": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["event_reference"]},
                    "source": {"type": "string"},
                    "selector": {"type": "string", "enum": ["first", "last", "any", "subsequent"]},
                }, "required": ["type", "source"]},
                "right": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["event_reference"]},
                    "source": {"type": "string"},
                    "selector": {"type": "string", "enum": ["first", "last", "any", "subsequent"]},
                }, "required": ["type", "source"]},
                "duration": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["duration"]},
                    "value": {"type": "integer"},
                    "unit": {"type": "string", "enum": list(DURATION_UNITS)},
                }, "required": ["type", "value", "unit"]},
                "evidence": evidence_schema,
            },
            "required": ["type", "operator", "left", "right", "duration", "evidence"],
        },
    ]

    def boolean_schema(level: int) -> dict[str, Any]:
        operand = (
            {"anyOf": atom_schemas}
            if level <= 1
            else {"anyOf": [*atom_schemas, boolean_schema(level - 1), {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["not"]}, "operand": {"anyOf": atom_schemas}},
                "required": ["type", "operand"],
            }]}
        )
        return {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["and", "or"]},
                "operands": {"type": "array", "items": operand, "minItems": 1},
            },
            "required": ["type", "operands"],
        }

    return {"anyOf": [*atom_schemas, {
        "type": "object",
        "properties": {"type": {"type": "string", "enum": ["not"]}, "operand": {"anyOf": atom_schemas}},
        "required": ["type", "operand"],
    }, boolean_schema(depth)]}


expression_json_schema = condition_json_schema


# ── 기존 슬롯 호환(파생 출력) ──────────────────────────────────────────────────────
# 기존 슬롯은 **기준 모델이 아니라 파생 출력**이다. 변환은 의미가 완전히 보존될 때만 하고,
# 조금이라도 줄어들면 None 을 돌려 IR 을 그대로 컴파일하게 한다.

# 사건별 legacy 슬롯 매핑. 키는 ``<극성>_<창 종류>`` 이고, 빠진 조합은 **그 슬롯이 없다**는 뜻이다 —
# ``purchase`` 에 ``not_exists_absolute`` 가 없는 것이 이 리팩터의 발단이다(특정 기간 부재를 담을 자리).
LEGACY_SLOT_BINDINGS: dict[str, dict[str, str]] = {
    "purchase": {
        "exists_absolute": "purchase_date",
        "exists_lifetime": "purchase_membership",
        "not_exists_rolling": "purchase_inactivity",
        "not_exists_lifetime": "behaviors:no_purchase",
    },
    "login": {
        "exists_rolling": "recent_login",
        "not_exists_rolling": "inactivity_period",
    },
}


def _window_kind(window: TimeWindow | None) -> str:
    if window is None:
        return "lifetime"
    return "rolling" if isinstance(window, RollingWindow) else "absolute"


def _render_legacy_slot(slot: str, window: TimeWindow | None, label: str) -> dict[str, Any] | None:
    """슬롯 이름 → 그 슬롯의 표기. 슬롯마다 shape 이 다른 것은 **기존 사실**이라 여기 한 곳에 모은다."""
    if slot == "purchase_date" and isinstance(window, AbsoluteInterval):
        return {"purchase_date": {**window.to_calendar_window(), "label": label}}
    if slot == "purchase_membership":
        return {"purchase_membership": {"domain": "purchase", "operator": "exists"}}
    if slot == "behaviors:no_purchase":
        return {"behaviors": ["no_purchase"]}
    if isinstance(window, RollingWindow):
        shape = {"value": window.value, "unit": window.plural_unit, "min_days": window.days}
        if slot in ("recent_login", "inactivity_period"):
            # 이 두 슬롯은 빌더 계약상 sql_interval 을 함께 갖는다.
            shape["sql_interval"] = f"{window.value} {window.plural_unit}"
        return {slot: shape}
    return None


def _is_exists_window_union(expression: Condition) -> bool:
    """'2018, 2019년에 구매' 처럼 **같은 조건의 여러 구간 합집합**인가.

    이 모양은 기존 창 슬롯이 ``windows`` 목록으로 이미 무손실 표현한다 — IR 이 가로챌 이유가 없다."""
    if not isinstance(expression, Or):
        return False
    views = existence_views(expression)
    return (
        len(views) == len(expression.operands)
        and len({view.source for view in views}) == 1
        and not any(view.negated for view in views)
        and all(isinstance(view.window, AbsoluteInterval) for view in views)
    )


def requires_general_ir(expression: Condition) -> bool:
    """단일 창 슬롯 모델이 **담지 못하는 모양**인가(사건 종류와 무관한 형태 판정).

    이 판정이 사건 목록이 아니라 모양을 보는 이유: 슬롯 모델의 한계는 '어떤 사건이냐'가 아니라
    '극성 하나에 창 하나'라는 구조 자체다. 사건마다 예외를 적기 시작하면 새 사건이 생길 때마다
    그 목록을 고쳐야 하고, 하나 빠뜨리면 IR 이 멀쩡한 기존 경로를 조용히 가로챈다.

    담지 못하는 모양:

    * 시간 관계(TemporalRelation) — 슬롯 모델에 두 사건 사이의 관계를 담을 자리가 없다.
    * 절대 기간이 붙은 부재 — 자리가 아예 없다(이 리팩터의 발단).
    * 절대 기간이 붙은 집계 — ``aggregate_conditions`` 는 롤링 일수(``window_days``)만 받는다.
    * 원자 조건이 둘 이상 — 슬롯은 극성별 창을 하나씩만 갖는다(단, ``exists`` 창 합집합은 예외:
      기존 창 슬롯의 ``windows`` 목록이 그대로 표현한다).
    """
    atoms = list(iter_atoms(expression))
    if any(isinstance(atom, TemporalRelation) for atom in atoms):
        return True
    if len(atoms) > 1:
        return not _is_exists_window_union(expression)
    if any(
        isinstance(atom, Comparison) and isinstance(atom.left, Aggregate)
        and any(isinstance(window, AbsoluteInterval) for window in time_windows(atom))
        for atom in atoms
    ):
        return True
    return any(
        view.negated and isinstance(view.window, AbsoluteInterval)
        for view in existence_views(expression)
    )


def _aggregate_legacy_condition(atom: Comparison) -> dict[str, Any] | None:
    """집계 비교 → 기존 ``aggregate_conditions`` 항목(무손실일 때만).

    절대 창 집계는 여기 오지 않는다 — :func:`requires_general_ir` 가 먼저 걸러낸다(기존 슬롯은
    롤링 일수만 받아 '올해 상반기 합계'를 '최근 N일 합계'로 바꿔야 하는데 그건 다른 조건이다)."""
    aggregate, literal = atom.left, atom.right
    if not isinstance(aggregate, Aggregate) or not isinstance(literal, Literal):
        return None
    relation = aggregate.relation
    window: TimeWindow | None = None
    if isinstance(relation, Filter) and isinstance(relation.where, TimeFilter):
        window = relation.where.window
        relation = relation.relation
    if not isinstance(relation, Source):
        return None
    metric = "order_count" if aggregate.function == "count" else None
    if aggregate.function == "sum" and isinstance(aggregate.expression, FieldRef):
        metric = "purchase_amount"
    if metric is None:
        return None
    return {
        "metric_id": metric,
        "operator": atom.operator,
        "threshold": literal.value,
        "window_days": window.days if isinstance(window, RollingWindow) else None,
    }


def try_convert_to_legacy_slots(expression: Condition, *, today: date | None = None) -> dict[str, Any] | None:
    """완전한 의미 보존이 가능할 때만 기존 슬롯 표기로 변환한다(아니면 None).

    라우팅 판정은 :func:`requires_general_ir` 가 하고, 이 함수는 그 판정이 '슬롯이 담는다'일 때
    **어떤 슬롯 표기가 되는지**를 렌더한다(둘의 답이 갈라지지 않도록 여기서도 같은 함수를 먼저 본다).

    변환하지 **않는** 경우를 명시한다 — 이것이 이 함수의 존재 이유다:

    * 절대 기간이 붙은 부재 → ``no_purchase``(평생 무주문)로도 롤링 ``purchase_inactivity``로도
      바꾸지 않는다. 둘 다 다른 집합이다.
    * 극성이 다른 두 창 → 기존 슬롯에는 극성별 창 자리가 하나뿐이라 한쪽이 반드시 사라진다.
    * 시간 관계 → 담을 슬롯이 없다.
    """
    if requires_general_ir(expression):
        return None
    atoms = list(iter_atoms(expression))
    if not atoms:
        return None

    def resolved(window: TimeWindow | None) -> TimeWindow | None:
        # '3년 전' 같은 상대 창은 슬롯 표기(YYYYMMDD)가 없으므로 기준일로 달력 구간을 확정한다.
        return resolve_relative_window(window, today) if isinstance(window, RelativeWindow) else window

    if all(isinstance(atom, Comparison) for atom in atoms):
        conditions = [_aggregate_legacy_condition(atom) for atom in atoms]  # type: ignore[arg-type]
        if all(condition is not None for condition in conditions):
            return {"target_user": {"aggregate_conditions": conditions}}
        return None

    views = existence_views(expression)
    if len(views) != len(atoms) or any(view.source not in LEGACY_SLOT_BINDINGS for view in views):
        return None

    if len(views) > 1:
        # 같은 조건의 창 나열(합집합)만 여기 온다(requires_general_ir 가 나머지를 걸러낸다).
        windows = sorted(
            (window.to_calendar_window() for view in views if isinstance(window := resolved(view.window), AbsoluteInterval)),
            key=lambda window: (window["from"], window["to"]),
        )
        if len(windows) != len(views):
            return None
        label = views[0].evidence.text.strip() if views[0].evidence else ""
        return {"target_user": {"purchase_date": {
            "from": windows[0]["from"], "to": windows[-1]["to"], "windows": windows, "label": label,
        }}}

    view = views[0]
    window = resolved(view.window)
    key = f"{'not_exists' if view.negated else 'exists'}_{_window_kind(window)}"
    slot = LEGACY_SLOT_BINDINGS[view.source].get(key)
    if slot is None:
        return None  # 그 조합을 담을 슬롯이 없다(예: 절대 기간 부재) — 그래서 IR 이 필요하다
    rendered = _render_legacy_slot(slot, window, view.evidence.text.strip() if view.evidence else "")
    return {"target_user": rendered} if rendered is not None else None


def unsupported_payload(reason: str, expression: Condition) -> dict[str, Any]:
    """컴파일 불가를 **의미를 보존한 채** 알리는 표준 표기(축소 폴백 금지의 반대편)."""
    return {"status": "unsupported", "reason": reason, "preserved_expression": expression.to_dict()}
