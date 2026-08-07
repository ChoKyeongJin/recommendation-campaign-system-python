"""값 정규화 계층 — 이미 추출된 의미 노드의 **값**만 정규화한다.

경계(이 모듈이 하면 안 되는 것):
  1. 사용자 원문 전체를 다시 해석하지 않는다. 입력은 노드 필드 값 하나다.
  2. 목적지 슬롯도, 실행 플랜 컨테이너도 모른다(컴파일러 소유).
  3. LLM 결과가 비었는지 여부에 따라 동작을 바꾸지 않는다(fill-if-empty 금지).
  4. missing/status 를 만들지도 고치지도 않는다.

즉 여기 있는 함수는 전부 `surface(또는 구조체) -> 타입드 값` 순수 변환이다. 삭제된 백필의
정규식 중 **값 문법**(금액 배수어·비교어·달력 월)만 이리로 옮겼고, 문형 감지(카트 문맥,
'구매한' 뒤따름 검사 같은 것)는 옮기지 않고 버렸다 — 그것이 이중 해석의 본체였다.

어휘 소유권: 단위/비교어는 `condition_normalizers`(normalization_lexicon.json)가 소유하고,
계수 단위의 **의미**와 엔터티 별칭은 도메인 선언이 소유한다(`semantic_domain_binding`).
지표 어휘는 호출자가 카탈로그로 주입한다. 이 모듈은 도메인 낱말을 하드코딩하지 않는다.
한국어 수사는 공통 리프 모듈 ``korean_number_normalizer``에서 가져오고, 금액 배수어
만/억/천만 이 값 문법의 단위 원자로 이 모듈에 남는다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import condition_normalizers
import korean_number_normalizer
import semantic_domain_binding

DEFAULT_CURRENCY = "KRW"

# 한국어 수 배수어. '금액 표현' 이라는 값 문법의 원자이지 문형 감지가 아니다.
_MAGNITUDES: tuple[tuple[str, int], ...] = (
    ("조", 1_000_000_000_000),
    ("억", 100_000_000),
    ("천만", 10_000_000),
    ("백만", 1_000_000),
    ("만", 10_000),
    ("천", 1_000),
)
_CURRENCY_MARKERS: tuple[str, ...] = ("원", "won", "krw", "₩")
_NUMBER_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)")
_MAGNITUDE_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)?\s*(?P<mag>" + "|".join(mag for mag, _ in _MAGNITUDES) + r")"
)
class NormalizationError(ValueError):
    """값 정규화 실패 — 내부 불량이지 '미지원'이 아니다."""

    def __init__(self, code: str, message: str, *, received: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.received = received


# ── 타입드 값 ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool):
            raise TypeError("money amount cannot be boolean")
        try:
            exact = self.amount if isinstance(self.amount, Decimal) else Decimal(str(self.amount))
        except (InvalidOperation, ValueError) as exc:
            raise TypeError("money amount must be decimal-compatible") from exc
        if not exact.is_finite():
            raise ValueError("money amount must be finite")
        object.__setattr__(self, "amount", exact)

    def to_dict(self) -> dict[str, Any]:
        return {"amount": decimal_json_value(self.amount), "currency": self.currency}


@dataclass(frozen=True)
class Quantity:
    value: int | Decimal
    unit: str | None = None

    def __post_init__(self) -> None:
        exact = exact_decimal(self.value)
        if exact is None:
            raise TypeError("quantity value must be a finite number")
        integral = exact.to_integral_value()
        object.__setattr__(self, "value", int(integral) if exact == integral else exact)

    def to_dict(self) -> dict[str, Any]:
        value = decimal_json_value(self.value) if isinstance(self.value, Decimal) else self.value
        return {"value": value, "unit": self.unit}


@dataclass(frozen=True)
class Ratio:
    value: Decimal
    unit: str = "percent"

    def __post_init__(self) -> None:
        exact = exact_decimal(self.value)
        if exact is None:
            raise TypeError("ratio value must be a finite number")
        if self.unit != "percent":
            raise ValueError("ratio unit must be 'percent'")
        object.__setattr__(self, "value", exact)

    def to_dict(self) -> dict[str, Any]:
        return {"value": decimal_json_value(self.value), "unit": self.unit}


@dataclass(frozen=True)
class CalendarMonth:
    year: int
    month: int

    def window(self) -> "Period":
        last_day = calendar.monthrange(self.year, self.month)[1]
        return Period(
            start=f"{self.year:04d}{self.month:02d}01",
            end=f"{self.year:04d}{self.month:02d}{last_day:02d}",
            label=f"{self.year}년 {self.month}월",
            kind="calendar_month",
        )


@dataclass(frozen=True)
class Period:
    """정규화된 기간. 실행 계층이 쓰는 YYYYMMDD 문자열 쌍으로 확정한다."""

    start: str
    end: str
    label: str | None = None
    kind: str = "absolute"

    def to_window(self) -> dict[str, Any]:
        window: dict[str, Any] = {"from": self.start, "to": self.end}
        if self.label:
            window["label"] = self.label
        return window


@dataclass(frozen=True)
class RelativeWindow:
    """상대 창('최근 90일'). 절대 날짜로 확정하려면 기준일이 필요하다."""

    value: int
    unit: str

    @property
    def days(self) -> int:
        if self.unit == "days":
            return self.value
        if self.unit == "weeks":
            return self.value * 7
        raise ValueError(f"{self.unit} requires a reference date before converting to days")

    def resolve(self, today: date) -> Period:
        end = today
        if self.unit == "months":
            lower_exclusive = _shift_calendar_date(today, months=-self.value)
            start = lower_exclusive + timedelta(days=1)
        elif self.unit == "years":
            lower_exclusive = _shift_calendar_date(today, years=-self.value)
            start = lower_exclusive + timedelta(days=1)
        else:
            start = today - timedelta(days=self.days - 1)
        return Period(
            start=start.strftime("%Y%m%d"),
            end=end.strftime("%Y%m%d"),
            label=f"최근 {self.value}{self.unit}",
            kind="relative",
        )


def _shift_calendar_date(reference: date, *, months: int = 0, years: int = 0) -> date:
    """Shift a date on calendar axes, clamping invalid month-end days."""

    total_month = reference.year * 12 + reference.month - 1 + months + years * 12
    year, zero_based_month = divmod(total_month, 12)
    month = zero_based_month + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class RankLimit:
    type: str  # "percent" | "count"
    value: int | Decimal

    def __post_init__(self) -> None:
        if self.type not in {"percent", "count"}:
            raise ValueError("rank limit type must be 'percent' or 'count'")
        exact = exact_decimal(self.value)
        if exact is None:
            raise TypeError("rank limit value must be a finite number")
        if self.type == "count":
            if exact != exact.to_integral_value() or exact <= 0:
                raise ValueError("rank count must be a positive integer")
            object.__setattr__(self, "value", int(exact))
            return
        if not Decimal("0") < exact < Decimal("100"):
            raise ValueError("rank percent must be between 0 and 100")
        object.__setattr__(self, "value", exact)

    def to_dict(self) -> dict[str, Any]:
        value = decimal_json_value(self.value) if isinstance(self.value, Decimal) else self.value
        return {"type": self.type, "value": value}


# ── Amount ───────────────────────────────────────────────────────────────────────
def _scalar_decimal(text: str) -> Decimal | None:
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    try:
        return Decimal(match.group("num").replace(",", ""))
    except InvalidOperation:
        return None


def exact_decimal(value: Any, *, allow_string: bool = False) -> Decimal | None:
    """Return a finite decimal without routing through binary floating point.

    Numeric strings are accepted only at an explicitly declared compatibility
    boundary.  Callers must opt in so ordinary domain normalization does not
    silently equate arbitrary strings and numbers.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        exact = value
    elif isinstance(value, (int, float)):
        try:
            exact = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    elif allow_string and isinstance(value, str) and re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?", value.strip()
    ):
        try:
            exact = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return exact if exact.is_finite() else None


def decimal_json_value(value: Decimal) -> int | float | str:
    """Serialize exactly representable JSON numbers numerically, otherwise as text."""

    if not value.is_finite():
        raise ValueError("decimal JSON value must be finite")
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    numeric = float(value)
    if Decimal(str(numeric)) == value:
        return numeric
    return format(value, "f")


def decimal_sql_text(value: Decimal) -> str:
    """Render a finite Decimal as a plain SQL numeric literal."""

    if not value.is_finite():
        raise ValueError("SQL decimal value must be finite")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


class AmountNormalizer:
    """'10만 원' → Money(100000, 'KRW'), '2개' → Quantity(2, 'item_quantity').

    입력은 노드의 value 필드 값이다(surface 문자열 / 숫자 / {amount,currency} / {value,unit}).
    문장이 아니다 — 문장에서 금액을 찾아내는 일은 이 계층의 책임이 아니다.
    """

    @staticmethod
    def is_money_surface(surface: str) -> bool:
        lowered = surface.strip().casefold()
        return any(marker in lowered for marker in _CURRENCY_MARKERS)

    @classmethod
    def normalize(cls, raw: Any, *, unit_hint: str | None = None) -> Money | Quantity:
        if isinstance(raw, Money) or isinstance(raw, Quantity):
            return raw
        if isinstance(raw, bool):
            raise NormalizationError("invalid_type", "수량 값이 boolean 이다", received=raw)
        if isinstance(raw, (int, float, Decimal)):
            unit = UnitNormalizer.normalize(unit_hint) if unit_hint else None
            return Quantity(value=_as_number(raw), unit=unit)
        if isinstance(raw, Mapping):
            # strict function calling 은 **모든 키를 항상 존재하게** 만든다(무표현은 null).
            # 그래서 키 존재 여부로 분기하면 `{"amount": null, "value": 2}` 가 금액으로 잘못
            # 읽힌다 — 값이 있는 키만 존재로 본다.
            raw = {key: value for key, value in raw.items() if value is not None}
            if "amount" in raw:
                amount = raw.get("amount")
                if isinstance(amount, str):
                    try:
                        exact_amount = Decimal(amount.replace(",", "").strip())
                    except InvalidOperation:
                        parsed = cls.normalize(amount)
                        if not isinstance(parsed, Money):
                            raise NormalizationError(
                                "invalid_amount",
                                "amount 문자열이 통화 금액이 아니다",
                                received=raw,
                            )
                        exact_amount = parsed.amount
                    return Money(
                        amount=exact_amount,
                        currency=str(raw.get("currency") or DEFAULT_CURRENCY),
                    )
                if not isinstance(amount, (int, float, Decimal)) or isinstance(amount, bool):
                    raise NormalizationError("invalid_amount", "amount 가 숫자가 아니다", received=raw)
                return Money(amount=Decimal(str(amount)), currency=str(raw.get("currency") or DEFAULT_CURRENCY))
            if "value" in raw:
                value = raw.get("value")
                if isinstance(value, str):
                    parsed = cls.normalize(value, unit_hint=raw.get("unit") or unit_hint)
                    return parsed
                if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
                    raise NormalizationError("invalid_value", "value 가 숫자가 아니다", received=raw)
                unit = UnitNormalizer.normalize(raw.get("unit") or unit_hint)
                return Quantity(value=_as_number(value), unit=unit)
            raise NormalizationError("invalid_quantity", "수량 객체에 amount/value 가 없다", received=raw)
        if isinstance(raw, str):
            return cls._from_surface(raw, unit_hint=unit_hint)
        raise NormalizationError("invalid_type", "수량 값의 타입을 알 수 없다", received=raw)

    @classmethod
    def _from_surface(cls, surface: str, *, unit_hint: str | None) -> Money | Quantity:
        text = surface.strip()
        if not text:
            raise NormalizationError("missing_value", "수량 표현이 비었다", received=surface)
        compact = re.sub(r"\s+", "", text)
        is_money = cls.is_money_surface(compact)
        base: Decimal | None = None

        magnitude = _MAGNITUDE_RE.search(compact)
        if magnitude is not None:
            multiplier = dict(_MAGNITUDES)[magnitude.group("mag")]
            head = magnitude.group("num")
            if head:
                base = Decimal(head.replace(",", "")) * multiplier
            else:
                # '만원'처럼 수사가 없으면 앞의 한자어 수사를 본다('이십만원').
                prefix = compact[: magnitude.start()]
                sino = korean_number_normalizer.parse_sino_korean_number(prefix)
                base = Decimal((sino or 1) * multiplier)
        if base is None:
            sino_only = korean_number_normalizer.parse_sino_korean_number(
                re.sub(r"[^가-힣]", "", compact)
            )
            base = Decimal(sino_only) if sino_only else _scalar_decimal(compact)
        if base is None:
            raise NormalizationError("invalid_number", f"수량을 읽지 못했다: {surface!r}", received=surface)
        if is_money:
            return Money(amount=base, currency=DEFAULT_CURRENCY)
        unit = UnitNormalizer.normalize(unit_hint) or UnitNormalizer.from_surface(compact)
        return Quantity(value=_as_number(base), unit=unit)


def _as_number(value: Decimal | float | int) -> int | Decimal:
    exact = exact_decimal(value)
    if exact is None:
        raise NormalizationError("invalid_number", "수량 값이 유한 숫자가 아니다", received=value)
    integral = exact.to_integral_value()
    return int(integral) if exact == integral else exact


def _is_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, Decimal):
        return value.is_finite() and value == value.to_integral_value()
    if isinstance(value, str):
        return value.strip().isdigit()
    return False


# ── Operator ─────────────────────────────────────────────────────────────────────
class OperatorNormalizer:
    """'이상' → '>=', 'gte' → '>='. 어휘는 normalization_lexicon.json 소유."""

    _EXTRA_ALIASES = {
        "gte": ">=", "gt": ">", "lte": "<=", "lt": "<", "eq": "=", "==": "=", "=": "=",
    }

    @classmethod
    def normalize(cls, raw: Any) -> str:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise NormalizationError("missing_operator", "연산자가 비었다", received=raw)
        text = str(raw).strip()
        canonical = condition_normalizers.canonical_operator(text)
        if canonical:
            return canonical
        alias = cls._EXTRA_ALIASES.get(text.casefold())
        if alias:
            return alias
        # '이상'이 다른 어구에 붙어 온 경우('10만 원 이상')는 값과 함께 온 표면이다.
        for surface, symbol in condition_normalizers.operator_aliases().items():
            if surface and surface in text:
                return symbol
        raise NormalizationError("invalid_operator", f"연산자를 알 수 없다: {raw!r}", received=raw)

    @classmethod
    def normalize_or_none(cls, raw: Any) -> str | None:
        try:
            return cls.normalize(raw)
        except NormalizationError:
            return None


# ── Unit ─────────────────────────────────────────────────────────────────────────
class UnitNormalizer:
    """계수 단위 표면 → **의미 단위**. 어느 표면이 어떤 의미인지는 도메인 선언이 소유한다.

    기간 단위(일/주/개월)는 condition_normalizers 의 canonical 단위로 넘긴다.
    """

    @staticmethod
    def counter_semantics() -> dict[str, str]:
        return semantic_domain_binding.counter_units()

    @classmethod
    def semantic_units(cls) -> frozenset[str]:
        return frozenset(cls.counter_semantics().values())

    @classmethod
    def normalize(cls, raw: Any) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        counters = cls.counter_semantics()
        if text in set(counters.values()):
            return text
        if text in counters:
            return counters[text]
        canonical = condition_normalizers.canonical_unit(text)
        return canonical or text

    @classmethod
    def from_surface(cls, surface: str) -> str | None:
        """수량 표면에 붙어 온 계수 단위만 읽는다(표면 목록은 도메인 선언에서 파생)."""
        counters = cls.counter_semantics()
        if not counters:
            return None
        # 긴 표면형 먼저 — '종류'가 '종'에 먹히지 않게 한다.
        alternation = "|".join(
            re.escape(term) for term in sorted(counters, key=lambda item: (-len(item), item))
        )
        match = re.search(rf"({alternation})\s*$", surface.strip())
        return counters.get(match.group(1)) if match else None


# ── Period / DateTime ────────────────────────────────────────────────────────────
class DateTimeNormalizer:
    """날짜 표기 → 'YYYYMMDD'. 문장에서 날짜를 찾아내지 않는다(값 하나만 본다)."""

    _DATE_RE = re.compile(r"^(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})$")
    _KOREAN_RE = re.compile(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일$")

    @classmethod
    def normalize(cls, raw: Any) -> str:
        if isinstance(raw, date):
            return raw.strftime("%Y%m%d")
        text = re.sub(r"\s+", "", str(raw or ""))
        for pattern in (cls._DATE_RE, cls._KOREAN_RE):
            match = pattern.match(text)
            if match:
                year, month, day = (int(part) for part in match.groups())
                try:
                    return date(year, month, day).strftime("%Y%m%d")
                except ValueError as exc:
                    raise NormalizationError("invalid_date", f"날짜가 유효하지 않다: {raw!r}", received=raw) from exc
        raise NormalizationError("invalid_date", f"날짜를 읽지 못했다: {raw!r}", received=raw)


class PeriodNormalizer:
    """기간 노드 값 → Period 또는 RelativeWindow.

    받는 형태(LLM 노출 스키마와 1:1):
      {type:'calendar_month', year, month} / {type:'absolute', from, to} /
      {type:'relative', value, unit} / '2026년 2월'(표면).

    ``interval`` 은 LLM 노출형이 아니라 literal binding 계층이 만든 내부 canonical
    창이다. ``end_exclusive`` 를 inclusive ``Period.end`` 로 바꿀 때만 받아, Event IR
    날짜창과 기존 SQL 컴파일러의 기간 계약 사이 의미를 보존한다.
    """

    _MONTH_SURFACE_RE = re.compile(r"^(?P<year>\d{4})년\s*(?P<month>\d{1,2})월$")
    # 숫자 뒤 기간 단위 표면의 소유자는 :func:`condition_normalizers.numeric_duration_unit_semantics`
    # 하나다. 여기 손 목록이 따로 있던 동안 그 목록만 낡아서, 다른 계층이 이미 읽는 표면
    # ('3개월간')을 이 계층은 못 읽었다 — 같은 문장이 어느 경로로 오느냐에 따라 기간이
    # 보이기도, 안 보이기도 했다.
    _RELATIVE_DURATION_UNITS: dict[str, str] = condition_normalizers.numeric_duration_unit_semantics()
    _RELATIVE_SURFACE_RE = re.compile(
        r"^최근\s*(?P<value>\d+)\s*(?P<unit>"
        + "|".join(
            re.escape(surface)
            for surface in sorted(_RELATIVE_DURATION_UNITS, key=lambda item: (-len(item), item))
        )
        + r")$"
    )

    @classmethod
    def normalize(cls, raw: Any, *, today: date | None = None) -> Period | RelativeWindow:
        if isinstance(raw, (Period, RelativeWindow)):
            return raw
        if isinstance(raw, CalendarMonth):
            return raw.window()
        if isinstance(raw, str):
            return cls._from_surface(raw, today=today)
        if not isinstance(raw, Mapping):
            raise NormalizationError("invalid_period", f"기간 값을 알 수 없다: {raw!r}", received=raw)

        # strict function calling 은 모든 키를 항상 존재하게 만든다(무표현은 null). 그래서
        # 선언된 `type` 이 그 종류의 인자를 갖추지 못한 채 오는 일이 생긴다
        # (실측: type='absolute' 인데 from/to 는 null 이고 value/unit 만 채워짐).
        # 선언보다 **실제로 채워진 인자**를 믿는다 — 그쪽이 사용자가 말한 것이다.
        raw = {key: value for key, value in raw.items() if value is not None}
        kind = str(raw.get("type") or "").strip()
        if not kind or not cls._has_arguments(kind, raw):
            kind = cls._infer_kind(raw) or kind
        if kind == "calendar_month":
            year, month = raw.get("year"), raw.get("month")
            if not _is_int(year) or not _is_int(month) or not 1 <= int(month) <= 12:
                raise NormalizationError("invalid_period", "calendar_month 에 year/month 가 필요하다", received=raw)
            return CalendarMonth(year=int(year), month=int(month)).window()
        if kind == "absolute":
            start, end = raw.get("from"), raw.get("to")
            if start is None or end is None:
                raise NormalizationError("invalid_period", "absolute 기간에 from/to 가 필요하다", received=raw)
            return Period(
                start=DateTimeNormalizer.normalize(start),
                end=DateTimeNormalizer.normalize(end),
                label=str(raw["label"]) if raw.get("label") else None,
            )
        if kind == "relative":
            value, unit = raw.get("value"), raw.get("unit")
            if not _is_int(value) or int(value) < 1:
                raise NormalizationError("invalid_period", "relative 기간에 양의 value 가 필요하다", received=raw)
            canonical = condition_normalizers.canonical_unit(unit)
            if canonical is None:
                raise NormalizationError(
                    "invalid_period_unit",
                    f"지원하지 않는 relative 기간 단위입니다: {unit!r}",
                    received=raw,
                )
            window = RelativeWindow(value=int(value), unit=canonical)
            return window.resolve(today) if today is not None else window
        if kind == "interval":
            start_raw, end_exclusive_raw = raw.get("start"), raw.get("end_exclusive")
            if start_raw is None or end_exclusive_raw is None:
                raise NormalizationError(
                    "invalid_period",
                    "interval 에 start/end_exclusive 가 필요하다",
                    received=raw,
                )
            start = DateTimeNormalizer.normalize(start_raw)
            end_exclusive = DateTimeNormalizer.normalize(end_exclusive_raw)
            start_date = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
            end_exclusive_date = date(
                int(end_exclusive[:4]),
                int(end_exclusive[4:6]),
                int(end_exclusive[6:8]),
            )
            if end_exclusive_date <= start_date:
                raise NormalizationError(
                    "invalid_period",
                    "interval end_exclusive 는 start 보다 뒤여야 한다",
                    received=raw,
                )
            return Period(
                start=start,
                end=(end_exclusive_date - timedelta(days=1)).strftime("%Y%m%d"),
                label=str(raw["label"]) if raw.get("label") else None,
            )
        raise NormalizationError("invalid_period", f"알 수 없는 기간 종류: {kind!r}", received=raw)

    # 기간 종류별 **필수 인자**. `_infer_kind` 와 같은 표를 두 번 쓰지 않도록 여기 하나만 둔다.
    _KIND_ARGUMENTS: dict[str, tuple[str, ...]] = {
        "calendar_month": ("year", "month"),
        "absolute": ("from", "to"),
        "relative": ("value",),
        "interval": ("start", "end_exclusive"),
    }

    @classmethod
    def _has_arguments(cls, kind: str, raw: Mapping[str, Any]) -> bool:
        required = cls._KIND_ARGUMENTS.get(kind)
        if required is None:
            return False
        return all(raw.get(name) is not None for name in required)

    @staticmethod
    def _infer_kind(raw: Mapping[str, Any]) -> str:
        if raw.get("year") is not None and raw.get("month") is not None:
            return "calendar_month"
        if raw.get("from") is not None or raw.get("to") is not None:
            return "absolute"
        if raw.get("value") is not None:
            return "relative"
        return ""

    @classmethod
    def _from_surface(cls, surface: str, *, today: date | None) -> Period | RelativeWindow:
        text = surface.strip()
        month = cls._MONTH_SURFACE_RE.match(text)
        if month:
            return CalendarMonth(year=int(month.group("year")), month=int(month.group("month"))).window()
        relative = cls._RELATIVE_SURFACE_RE.match(re.sub(r"\s+", "", text))
        if relative:
            # 단위를 읽은 표와 canonical 단위를 얻는 표가 같아야 한다. 예전에는 매칭은 손 목록으로
            # 하고 canonical 은 다른 표에서 얻은 뒤 ``or "days"`` 로 물러섰는데, 그 폴백은 표가
            # 어긋나는 순간 '3개월'을 조용히 3일로 바꾼다(§20 암묵적 변환 금지).
            unit = cls._RELATIVE_DURATION_UNITS.get(relative.group("unit"))
            if unit is None:  # pragma: no cover - 매칭 표와 같은 표라 도달하지 않는다
                raise NormalizationError(
                    "invalid_period_unit",
                    f"지원하지 않는 relative 기간 단위입니다: {relative.group('unit')!r}",
                    received=surface,
                )
            window = RelativeWindow(value=int(relative.group("value")), unit=unit)
            return window.resolve(today) if today is not None else window
        raise NormalizationError("invalid_period", f"기간 표현을 읽지 못했다: {surface!r}", received=surface)


class RankLimitNormalizer:
    """'상위 10%' → RankLimit('percent', 10), '상위 100명' → RankLimit('count', 100)."""

    _SURFACE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<kind>%|퍼센트|프로|명|개|건)")

    @classmethod
    def normalize(cls, raw: Any) -> RankLimit:
        if isinstance(raw, RankLimit):
            return raw
        try:
            if isinstance(raw, Mapping):
                kind = str(raw.get("type") or "").strip()
                value = raw.get("value")
                if kind not in {"percent", "count"}:
                    raise ValueError("unknown rank limit type")
                # Fractional percentages use the exact-string compatibility
                # representation emitted by RankLimit.to_dict(). Count strings
                # remain invalid to avoid broad implicit coercion.
                if kind == "percent" and isinstance(value, str):
                    exact = exact_decimal(value, allow_string=True)
                    if exact is None:
                        raise ValueError("invalid decimal percentage")
                    value = exact
                elif not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
                    raise TypeError("rank limit value is not numeric")
                return RankLimit(type=kind, value=value)
            if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
                return RankLimit(type="count", value=raw)
            if isinstance(raw, str):
                match = cls._SURFACE_RE.search(raw)
                if match is None:
                    raise ValueError("rank limit surface did not match")
                kind = "percent" if match.group("kind") in {"%", "퍼센트", "프로"} else "count"
                return RankLimit(type=kind, value=Decimal(match.group("value")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise NormalizationError(
                "invalid_rank_limit",
                f"랭킹 제한을 읽지 못했다: {raw!r}",
                received=raw,
            ) from exc
        raise NormalizationError("invalid_rank_limit", f"랭킹 제한 타입을 알 수 없다: {raw!r}", received=raw)


class RatioNormalizer:
    """Normalize an explicit percentage while retaining Decimal precision."""

    _SURFACE_RE = re.compile(r"^\s*(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로)\s*$")

    @classmethod
    def normalize(cls, raw: Any) -> Ratio:
        if isinstance(raw, Ratio):
            return raw
        value = raw
        unit = "percent"
        if isinstance(raw, Mapping):
            value = raw.get("value")
            unit = str(raw.get("unit") or "percent").strip().casefold()
            if unit in {"%", "퍼센트", "프로"}:
                unit = "percent"
        elif isinstance(raw, str):
            match = cls._SURFACE_RE.fullmatch(raw)
            if match is None:
                raise NormalizationError(
                    "invalid_ratio",
                    f"비율 값을 읽지 못했다: {raw!r}",
                    received=raw,
                )
            value = match.group("value")

        exact = exact_decimal(value, allow_string=True)
        if exact is None or unit != "percent":
            raise NormalizationError(
                "invalid_ratio",
                "비율은 명시적인 percent 값이어야 한다",
                received=raw,
            )
        return Ratio(value=exact, unit="percent")


# ── Metric / Entity ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MetricCatalogEntry:
    metric_id: str
    labels: tuple[str, ...] = ()


class MetricResolver:
    """지표 라벨/동의어 → metric_id. 어휘는 **주입**받는다(레지스트리 소유).

    같은 표면이 여러 metric_id 로 해석되면 조용히 하나를 고르지 않고 후보 전체를 돌려준다
    (충돌 판정은 resolver 의 일이다).
    """

    def __init__(self, entries: list[MetricCatalogEntry] | None = None) -> None:
        self._entries: list[MetricCatalogEntry] = list(entries or [])

    @classmethod
    def from_specs(cls, specs: Any, *, id_key: str = "metric_id") -> "MetricResolver":
        """레지스트리 스펙(dict 맵 또는 리스트)에서 카탈로그를 만든다."""
        entries: list[MetricCatalogEntry] = []
        items: list[tuple[str, Any]] = []
        if isinstance(specs, Mapping):
            items = [(str(key), value) for key, value in specs.items()]
        elif isinstance(specs, (list, tuple)):
            for spec in specs:
                if isinstance(spec, Mapping) and spec.get(id_key):
                    items.append((str(spec[id_key]), spec))
        for metric_id, spec in items:
            labels = {metric_id}
            if isinstance(spec, Mapping):
                for key in ("ko_label", "name", "label"):
                    if isinstance(spec.get(key), str) and spec[key].strip():
                        labels.add(spec[key].strip())
                for key in ("synonyms", "aliases", "aliases_nouns"):
                    for term in spec.get(key) or []:
                        if isinstance(term, str) and term.strip():
                            labels.add(term.strip())
            entries.append(MetricCatalogEntry(metric_id=metric_id, labels=tuple(sorted(labels))))
        return cls(entries)

    @staticmethod
    def _compact(text: str) -> str:
        return re.sub(r"\s+", "", text or "").casefold()

    def candidates(self, surface: Any) -> tuple[str, ...]:
        text = self._compact(str(surface or ""))
        if not text:
            return ()
        exact = [entry.metric_id for entry in self._entries if entry.metric_id.casefold() == text]
        if exact:
            return tuple(exact)
        label_hits = [
            entry.metric_id
            for entry in self._entries
            if any(self._compact(label) == text for label in entry.labels)
        ]
        if label_hits:
            return tuple(dict.fromkeys(label_hits))
        # 부분 일치는 **가장 긴 라벨**만 인정한다(짧은 일반어의 승자독식 방지).
        scored: list[tuple[int, str]] = []
        for entry in self._entries:
            for label in entry.labels:
                compact_label = self._compact(label)
                if len(compact_label) >= 2 and compact_label in text:
                    scored.append((len(compact_label), entry.metric_id))
        if not scored:
            return ()
        best = max(score for score, _ in scored)
        return tuple(dict.fromkeys(metric_id for score, metric_id in scored if score == best))

    def resolve(self, surface: Any) -> str | None:
        hits = self.candidates(surface)
        return hits[0] if len(hits) == 1 else None


class EntityResolver:
    """엔터티 표면 → canonical. 별칭 사전은 도메인 선언이 소유하고, 호출자가 덧댈 수 있다."""

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        declared = {
            str(key).casefold(): str(value)
            for key, value in semantic_domain_binding.entity_aliases().items()
        }
        self._aliases = {
            **declared,
            **{str(k).casefold(): str(v) for k, v in (aliases or {}).items()},
        }

    def resolve(self, surface: Any) -> str | None:
        if surface is None:
            return None
        text = re.sub(r"\s+", "", str(surface)).casefold()
        return self._aliases.get(text)


# ── 카운트 임계 대수 ─────────────────────────────────────────────────────────────
# 정수 카운트에 대한 (연산자, 임계값) 의 **의미 분류**. 도메인 낱말도 슬롯 이름도 모른다 —
# 어느 슬롯으로 가는지는 컴파일러가 정하고, 여기서는 대수만 판정한다.
#
# 왜 대수인가: '있음/없음'과 'N건 이상'은 표현이 아니라 값으로 갈린다.
#     COUNT > 0  ≡  COUNT >= 1   ≡  존재
#     COUNT = 0  ≡  COUNT < 1    ≡  부재
#     COUNT >= 0                 ≡  항진식(조건이 아니다)
# 이 분류가 없으면 (a) 존재 표현('주문은 있었지만' → order_count > 0)이 임계 슬롯의 양수
# 도메인에 걸려 요청 전체가 막히고, (b) 부재 표현이 임계 비교로 컴파일돼 극성이 뒤집히고,
# (c) 항진식이 조건인 척 SQL 에 들어간다. 실측(2026-08-02): (a) 로 4/4 실패.
COUNT_TAUTOLOGY = "tautology"
COUNT_EXISTENCE = "existence"
COUNT_ABSENCE = "absence"
COUNT_CONTRADICTION = "contradiction"
COUNT_THRESHOLD = "threshold"


class CountThresholdNormalizer:
    """카운트 임계 비교의 의미 분류기(순수 대수 — 카운트는 음수가 아닌 정수라는 것만 안다)."""

    # 이 분류기만 쓰는 **엄격** 연산자 표. 일반 정규화기(OperatorNormalizer)는 '10만 원 이상'처럼
    # 값과 붙어 온 표면을 살리려고 부분 문자열 폴백을 갖는데, 그 관대함이 여기서는 극성을 뒤집는다:
    # '!=' 안에서 별칭 '=' 가 발견돼 `order_count != 0`(주문 있음)이 부재로 분류됐다(실측).
    # 분류가 곧 슬롯 선택이라 모르는 표기는 관대하게 넘기지 말고 **환원하지 않는 편**이 옳다.
    _STRICT_OPERATORS: dict[str, str] = {
        ">=": ">=", ">": ">", "<=": "<=", "<": "<", "=": "=", "==": "=",
        "gte": ">=", "gt": ">", "lte": "<=", "lt": "<", "eq": "=",
        "이상": ">=", "초과": ">", "이하": "<=", "미만": "<", "같음": "=", "동일": "=",
    }

    @classmethod
    def classify(cls, operator: Any, threshold: Any) -> str:
        """(연산자, 임계값) → COUNT_* 분류. 판정 불가면 COUNT_THRESHOLD(기존 경로 유지)."""
        symbol = cls._STRICT_OPERATORS.get(str(operator).strip()) if operator is not None else None
        value = exact_decimal(threshold, allow_string=True)
        # NaN·무한대는 정수 카운트의 경계가 아니다.
        if symbol is None or value is None:
            return COUNT_THRESHOLD
        # 정수 카운트 위의 하한/상한을 정수로 좁힌다: 'COUNT > 0.5' 는 'COUNT >= 1' 과 같다.
        if symbol == ">":
            return cls._from_lower_bound(_floor_int(value) + 1)
        if symbol == ">=":
            return cls._from_lower_bound(_ceil_int(value))
        if symbol == "=":
            if value < 0 or value != value.to_integral_value():
                return COUNT_CONTRADICTION
            return COUNT_ABSENCE if value == 0 else COUNT_THRESHOLD
        if symbol == "<":
            return cls._from_upper_bound(_ceil_int(value) - 1)
        if symbol == "<=":
            return cls._from_upper_bound(_floor_int(value))
        return COUNT_THRESHOLD

    @staticmethod
    def _from_lower_bound(minimum: int) -> str:
        """'COUNT >= minimum' 의 의미."""
        if minimum <= 0:
            return COUNT_TAUTOLOGY
        if minimum == 1:
            return COUNT_EXISTENCE
        return COUNT_THRESHOLD

    @staticmethod
    def _from_upper_bound(maximum: int) -> str:
        """'COUNT <= maximum' 의 의미."""
        if maximum < 0:
            return COUNT_CONTRADICTION
        if maximum == 0:
            return COUNT_ABSENCE
        return COUNT_THRESHOLD


def _floor_int(value: Decimal) -> int:
    import math

    return int(math.floor(value))


def _ceil_int(value: Decimal) -> int:
    import math

    return int(math.ceil(value))


__all__ = [
    "AmountNormalizer",
    "COUNT_ABSENCE",
    "COUNT_CONTRADICTION",
    "COUNT_EXISTENCE",
    "COUNT_TAUTOLOGY",
    "COUNT_THRESHOLD",
    "CalendarMonth",
    "CountThresholdNormalizer",
    "DEFAULT_CURRENCY",
    "DateTimeNormalizer",
    "decimal_json_value",
    "decimal_sql_text",
    "EntityResolver",
    "exact_decimal",
    "MetricCatalogEntry",
    "MetricResolver",
    "Money",
    "NormalizationError",
    "OperatorNormalizer",
    "Period",
    "PeriodNormalizer",
    "Quantity",
    "Ratio",
    "RatioNormalizer",
    "RankLimit",
    "RankLimitNormalizer",
    "RelativeWindow",
    "UnitNormalizer",
]
