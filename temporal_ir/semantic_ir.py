"""Canonical Temporal Semantic IR — 사용자가 요청한 **시간 의미**를 그대로 담는 상위 IR.

왜 별도 계층인가
----------------
실행 IR(:mod:`event_ir`)은 "어떤 관계에 어떤 술어가 걸리는가"를 말한다. 그 계층에는 *왜* 그
구간이 그 구간인지가 남지 않는다. ``[2026-07-01, 2026-08-01)`` 만 남으면 그것이 '지난달 말
기준'이었는지 '7월 한 달 동안 한 번이라도'였는지 구분할 수 없고, 두 문장은 데이터 표현에 따라
답이 갈린다(월 스냅샷에서 전자는 한 행의 상태, 후자는 존재 판정이다).

그래서 이 모듈은 시간 의미를 **세 부분의 합성**으로 붙잡는다:

    selector    어느 시점/구간의 관측을 볼 것인가
    quantifier  그 관측들 중 몇 개가 성립해야 하는가
    predicate   각 관측에서 무엇이 성립해야 하는가

이 셋이 분리되어 있으면 '지난달 말 기준 VIP'와 '최근 3개월 매월 VIP'는 **새 연산자 문자열이
아니라 다른 조합**이다. 문장이 늘어도 타입은 늘지 않는다 — 그것이 이 파일의 유일한 설계 규칙이고,
:mod:`event_ir` 의 규칙("문장 유형마다 타입을 늘리지 않는다")을 시간 축에 그대로 옮긴 것이다.

이 모듈이 **모르는 것**
  - 한국어 표면형과 그 정규식(도메인 계층 소유).
  - 물리 테이블·컬럼·값 코드(:mod:`temporal_ir.catalog` 의 binding 소유).
  - 이 의미가 어떤 SQL 이 되는가(:mod:`temporal_ir.lowering` 소유).

``options: dict`` 를 쓰지 않는다
--------------------------------
선택 전략·버킷·동점자·missing/null/coverage 정책·전이 모드·빈 구간 정책은 전부 타입이거나
enum 이다. 자유 형식 사전에 담으면 잘못된 조합이 lowering 단계까지 살아 있다가 SQL 을 만들다
터지거나, 더 나쁘게는 **다른 뜻으로 조용히 성립**한다. 잘못된 조합은 객체 생성에서 막는다.

시간대와 기준 시각
------------------
이 계층에는 ``now()`` 가 없다. 상대 표현(``RelativeAnchor``/``RelativeWindow``)은 요청 기준
시각을 받아야만 절대 구간으로 확정되며, 그 확정은 :mod:`temporal_ir.calendar` 가 요청 문맥
(:class:`TemporalRequestContext`)의 시각으로 수행한다. 같은 입력·같은 기준 시각이면 같은 결과다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, TypeAlias

import event_ir

# 근거 구간은 실행 IR 과 **같은 값 객체**를 쓴다. 두 계층이 각자의 Evidence 를 가지면 같은 구절이
# 두 좌표계로 표현되고, 영수증이 그중 어느 쪽을 말하는지 알 수 없게 된다.
Evidence: TypeAlias = event_ir.Evidence


class TemporalIrError(ValueError):
    """상위 시간 IR 의 스키마·조합 위반(닫힌 어휘 밖의 값, 성립 불가능한 조합)."""


# ── 닫힌 어휘 ─────────────────────────────────────────────────────────────────────


class TimeUnit(StrEnum):
    """달력 칸의 단위. 실행 IR 의 창 단위(:data:`event_ir.WINDOW_UNITS`)와 같은 집합이다."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


# 굵기 순서. '일 단위 요청을 월 스냅샷으로 근사하지 않는다'의 판정 근거이며, 낱말이 아니라
# 이 순서표가 판정한다.
UNIT_RANK: dict[TimeUnit, int] = {
    TimeUnit.DAY: 0,
    TimeUnit.WEEK: 1,
    TimeUnit.MONTH: 2,
    TimeUnit.YEAR: 3,
}


class Boundary(StrEnum):
    """달력 칸의 어느 끝인가. ``END`` 는 칸 **안의 마지막 순간**이다(다음 칸의 시작이 아니다)."""

    START = "start"
    END = "end"


class WindowMode(StrEnum):
    """구간의 종류. 둘은 다른 집합이다 — '최근 30일'과 '지난 1개월'은 같은 뜻이 아니다."""

    CALENDAR = "calendar"
    ROLLING = "rolling"


class Direction(StrEnum):
    """열린 구간이 어느 쪽으로 열려 있는가."""

    BEFORE = "before"
    AT_OR_AFTER = "at_or_after"


class MissingPolicy(StrEnum):
    """기대되는 관측 **행 자체가 없을 때**. ``null`` (행은 있고 값이 없음)과 다른 축이다."""

    UNKNOWN = "unknown"  # 조건 불성립 — 대상에서 빠진다
    ERROR = "error"  # 적재 공백이 증명되면 평가하지 않는다


class NullPolicy(StrEnum):
    """관측 행은 있는데 **값이 NULL** 일 때."""

    EXCLUDE = "exclude"  # 그 관측은 술어를 만족하지 않는다(SQL 3값 논리 그대로)
    TREAT_AS_MISMATCH = "treat_as_mismatch"  # 전칭 판정에서 위반으로 센다
    ERROR = "error"


class EmptyWindowPolicy(StrEnum):
    """구간에 관측이 하나도 없을 때 전칭(all/every) 판정을 어떻게 할 것인가."""

    REQUIRE_OBSERVATION = "require_observation"  # 관측이 없으면 불성립
    VACUOUS_TRUE = "vacuous_true"  # 공허참


class TransitionMode(StrEnum):
    """'값이 바뀌었다'의 뜻. 세 가지는 서로 다른 집합이다(§10)."""

    DIRECT_OBSERVED_TRANSITION = "direct_observed_transition"  # 관측 순서상 A 바로 다음 B
    ENDPOINT_CHANGE = "endpoint_change"  # 구간 시작 관측 A, 종료 관측 B
    ANY_CHANGE = "any_change"  # 구간 안 어딘가에서 A→B


class ObservationSemantics(StrEnum):
    """'바뀌지 않았다'의 뜻."""

    OBSERVED_VALUES_EQUAL = "observed_values_equal"  # 관측된 값이 모두 같다
    NEVER_CHANGED = "never_changed"  # 관측 사이에도 바뀐 적이 없다(연속 유효성 필요)


class RelationKind(StrEnum):
    """두 사건 발생 시점 사이의 관계."""

    WITHIN_AFTER = "within_after"
    WITHIN_BEFORE = "within_before"


class OccurrenceSelector(StrEnum):
    """여러 번 발생하는 사건에서 **어느 발생**을 기준으로 삼는가.

    '첫 구매 후 30일 이내'와 '마지막 구매 후 30일 이내'는 다른 집합이다. 기준 사건이 한 번만
    발생하는 표현(가입 같은 singleton)에서는 두 값이 같으므로 생략할 수 있고, 여러 번 발생할 수
    있는 표현에서는 생략을 허용하지 않는다(추측 금지).
    """

    FIRST = "first"
    LAST = "last"


class PreviousKind(StrEnum):
    """'직전'의 세 가지 뜻(§10). 하나로 합치면 누락 월과 값 반복에서 서로 다른 답이 섞인다."""

    BUCKET = "bucket"  # 바로 이전 달력 칸
    OBSERVATION = "observation"  # 실제 존재하는 이전 관측 행
    DISTINCT_VALUE = "distinct_value"  # 현재값과 다른 마지막 관측값


class CoveragePolicy(StrEnum):
    """적재 범위 판정을 결과에 어떻게 반영할 것인가(의미 지원 여부와 **다른 축**)."""

    ADVISE = "advise"  # SQL 을 만들고 경고를 붙인다
    BLOCK = "block"  # 평가 불가로 답한다


COMPARISON_OPERATORS: frozenset[str] = event_ir.COMPARISON_OPERATORS


def _enum(value: Any, enum_type: type[StrEnum], label: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise TemporalIrError(
            f"{label}: 알 수 없는 값 {value!r} (허용: {[item.value for item in enum_type]})"
        ) from exc


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TemporalIrError(f"{label} 은 양의 정수여야 합니다: {value!r}")
    return value


def _aware(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TemporalIrError(f"{label} 은 datetime 이어야 합니다: {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalIrError(f"{label} 은 timezone-aware 여야 합니다(naive datetime 금지)")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TemporalIrError(f"{label} 은 정수 또는 십진 문자열이어야 합니다: {value!r}")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise TemporalIrError(f"{label} 을 십진수로 읽을 수 없습니다: {value!r}") from exc


def _symbol(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemporalIrError(f"{label} 은 비어 있지 않은 문자열이어야 합니다")
    return value.strip()


# ── 시점(anchor) ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReferenceAnchor:
    """요청 기준 시각 그 자체('지금')."""

    kind: str = "reference"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "reference"}


@dataclass(frozen=True, slots=True)
class RelativeAnchor:
    """기준 시각에서 ``offset`` 칸 이동한 달력 칸의 한쪽 끝.

    ``previous_month_end`` 같은 **이름**을 두지 않는 이유가 이 세 필드다. 이름표를 늘리면 새
    표현마다 이름과 분기가 하나씩 늘지만, (칸 단위, 이동량, 끝) 세 값은 '지난달 말'·'3년 전
    초'·'어제'를 전부 같은 계산으로 만든다.
    """

    offset: int
    unit: TimeUnit
    boundary: Boundary
    kind: str = "relative"

    def __post_init__(self) -> None:
        if isinstance(self.offset, bool) or not isinstance(self.offset, int):
            raise TemporalIrError(f"relative anchor offset 은 정수여야 합니다: {self.offset!r}")
        object.__setattr__(self, "unit", _enum(self.unit, TimeUnit, "relative anchor unit"))
        object.__setattr__(
            self, "boundary", _enum(self.boundary, Boundary, "relative anchor boundary")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "relative",
            "offset": self.offset,
            "unit": str(self.unit),
            "boundary": str(self.boundary),
        }


@dataclass(frozen=True, slots=True)
class AbsoluteAnchor:
    """달력상 확정된 순간(timezone-aware)."""

    instant: datetime
    kind: str = "absolute"

    def __post_init__(self) -> None:
        _aware(self.instant, "absolute anchor instant")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "absolute", "instant": self.instant.isoformat()}


Anchor: TypeAlias = ReferenceAnchor | RelativeAnchor | AbsoluteAnchor


def anchor_from_dict(raw: Any) -> Anchor:
    if not isinstance(raw, dict):
        raise TemporalIrError("anchor 는 객체여야 합니다")
    kind = raw.get("kind")
    if kind == "reference":
        return ReferenceAnchor()
    if kind == "relative":
        return RelativeAnchor(
            offset=int(raw.get("offset", 0)),
            unit=_enum(raw.get("unit"), TimeUnit, "relative anchor unit"),
            boundary=_enum(raw.get("boundary"), Boundary, "relative anchor boundary"),
        )
    if kind == "absolute":
        return AbsoluteAnchor(instant=_parse_instant(raw.get("instant")))
    raise TemporalIrError(f"알 수 없는 anchor 종류: {kind!r}")


def _parse_instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, "instant")
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise TemporalIrError(f"ISO-8601 시각이 아닙니다: {value!r}") from exc
    return _aware(parsed, "instant")


# ── 구간(window) ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AbsoluteWindow:
    """달력상 확정된 반개구간 ``[start, end_exclusive)``."""

    start: datetime
    end_exclusive: datetime
    kind: str = "absolute"

    def __post_init__(self) -> None:
        _aware(self.start, "window start")
        _aware(self.end_exclusive, "window end_exclusive")
        if self.start >= self.end_exclusive:
            raise TemporalIrError(
                f"빈 구간입니다: {self.start.isoformat()} >= {self.end_exclusive.isoformat()}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "absolute",
            "start": self.start.isoformat(),
            "end_exclusive": self.end_exclusive.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RelativeWindow:
    """기준 시각에서 거슬러 세는 구간.

    ``mode`` 와 ``include_current`` 를 **둘 다 요구**하는 이유는 추측 금지다. '최근 3개월'은
    말하는 사람에 따라 (a) 오늘부터 90일, (b) 완결된 직전 3개월, (c) 이번 달 포함 3개월 중
    하나이고, 셋은 다른 집합이다. 기본값을 두면 그 선택이 코드 안에 숨는다.
    """

    amount: int
    unit: TimeUnit
    mode: WindowMode
    include_current: bool
    kind: str = "relative"

    def __post_init__(self) -> None:
        _positive_int(self.amount, "relative window amount")
        object.__setattr__(self, "unit", _enum(self.unit, TimeUnit, "relative window unit"))
        object.__setattr__(self, "mode", _enum(self.mode, WindowMode, "relative window mode"))
        if not isinstance(self.include_current, bool):
            raise TemporalIrError("relative window include_current 는 boolean 이어야 합니다")
        if self.mode is WindowMode.ROLLING and self.include_current is False:
            # 롤링 구간은 기준 시각에서 거슬러 세므로 '현재 칸 제외'라는 개념이 없다.
            raise TemporalIrError(
                "rolling 구간에는 include_current=False 를 쓸 수 없습니다"
                " — 롤링은 칸이 아니라 길이입니다"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "relative",
            "amount": self.amount,
            "unit": str(self.unit),
            "mode": str(self.mode),
            "include_current": self.include_current,
        }


@dataclass(frozen=True, slots=True)
class LifetimeWindow:
    """시간 제한 없음('구매 이력이 있는 회원').

    구간의 부재를 ``None`` 으로 표현하지 않고 타입으로 세우는 이유: ``None`` 은 '평생'과 '구간을
    아직 못 정했다'를 같은 값으로 만든다. 앞의 것은 완성된 의미이고 뒤의 것은 결핍이다.
    """

    kind: str = "lifetime"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "lifetime"}


@dataclass(frozen=True, slots=True)
class OpenEndedWindow:
    """한쪽이 열린 구간('X 이전', 'X 이후').

    실행 IR 에는 열린 시간 필터가 없다. lowering 은 열린 쪽을 **바인딩이 선언한 적재 범위**로
    닫는다 — 선언 범위 밖에는 행이 없다는 것이 카탈로그의 계약이므로 결과 집합이 같고, 선언이
    없으면 닫지 않고 미지원으로 답한다(근사하지 않는다).
    """

    anchor: Anchor
    direction: Direction
    kind: str = "open_ended"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "direction", _enum(self.direction, Direction, "open window direction")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "open_ended",
            "anchor": self.anchor.to_dict(),
            "direction": str(self.direction),
        }


TemporalWindow: TypeAlias = AbsoluteWindow | RelativeWindow | OpenEndedWindow | LifetimeWindow


def window_from_dict(raw: Any) -> TemporalWindow:
    if not isinstance(raw, dict):
        raise TemporalIrError("window 는 객체여야 합니다")
    kind = raw.get("kind")
    if kind == "absolute":
        return AbsoluteWindow(
            start=_parse_instant(raw.get("start")),
            end_exclusive=_parse_instant(raw.get("end_exclusive")),
        )
    if kind == "relative":
        return RelativeWindow(
            amount=int(raw.get("amount", 0)),
            unit=_enum(raw.get("unit"), TimeUnit, "relative window unit"),
            mode=_enum(raw.get("mode"), WindowMode, "relative window mode"),
            include_current=bool(raw.get("include_current")),
        )
    if kind == "lifetime":
        return LifetimeWindow()
    if kind == "open_ended":
        return OpenEndedWindow(
            anchor=anchor_from_dict(raw.get("anchor")),
            direction=_enum(raw.get("direction"), Direction, "open window direction"),
        )
    raise TemporalIrError(f"알 수 없는 window 종류: {kind!r}")


# ── 선택 전략(selection strategy) ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExactBucket:
    """기준 시점이 속한 달력 칸의 관측만 본다(그 칸이 없으면 missing)."""

    kind: str = "exact_bucket"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "exact_bucket"}


@dataclass(frozen=True, slots=True)
class LatestAtOrBefore:
    """기준 시점 이전(포함)의 **마지막 관측**을 본다 — 칸이 비어도 이전 값을 끌어온다."""

    kind: str = "latest_at_or_before"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "latest_at_or_before"}


SelectionStrategy: TypeAlias = ExactBucket | LatestAtOrBefore

_STRATEGIES: dict[str, Callable[[], "SelectionStrategy"]] = {
    "exact_bucket": ExactBucket,
    "latest_at_or_before": LatestAtOrBefore,
}


def strategy_from_dict(raw: Any) -> SelectionStrategy:
    if not isinstance(raw, dict) or str(raw.get("kind")) not in _STRATEGIES:
        raise TemporalIrError(f"알 수 없는 선택 전략: {raw!r}")
    return _STRATEGIES[str(raw["kind"])]()


# ── selector ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AsOfSelector:
    """한 시점의 상태."""

    anchor: Anchor
    strategy: SelectionStrategy
    missing_policy: MissingPolicy = MissingPolicy.UNKNOWN
    kind: str = "as_of"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "missing_policy", _enum(self.missing_policy, MissingPolicy, "missing_policy")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "as_of",
            "anchor": self.anchor.to_dict(),
            "strategy": self.strategy.to_dict(),
            "missing_policy": str(self.missing_policy),
        }


@dataclass(frozen=True, slots=True)
class WindowSelector:
    """한 구간의 관측 전부.

    ``bucket`` 은 '몇 칸으로 나눠 보는가'이며 전칭 판정(every_bucket)에서만 뜻이 있다.
    """

    window: TemporalWindow
    bucket: TimeUnit | None = None
    anchor: Anchor = ReferenceAnchor()
    missing_policy: MissingPolicy = MissingPolicy.UNKNOWN
    empty_window_policy: EmptyWindowPolicy = EmptyWindowPolicy.REQUIRE_OBSERVATION
    kind: str = "window"

    def __post_init__(self) -> None:
        if self.bucket is not None:
            object.__setattr__(self, "bucket", _enum(self.bucket, TimeUnit, "window bucket"))
        object.__setattr__(
            self, "missing_policy", _enum(self.missing_policy, MissingPolicy, "missing_policy")
        )
        object.__setattr__(
            self,
            "empty_window_policy",
            _enum(self.empty_window_policy, EmptyWindowPolicy, "empty_window_policy"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "window",
            "window": self.window.to_dict(),
            "bucket": str(self.bucket) if self.bucket is not None else None,
            "anchor": self.anchor.to_dict(),
            "missing_policy": str(self.missing_policy),
            "empty_window_policy": str(self.empty_window_policy),
        }


@dataclass(frozen=True, slots=True)
class PreviousSelector:
    """'직전'의 세 뜻 중 하나(:class:`PreviousKind`)."""

    anchor: Anchor
    previous_kind: PreviousKind
    missing_policy: MissingPolicy = MissingPolicy.UNKNOWN
    kind: str = "previous"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "previous_kind", _enum(self.previous_kind, PreviousKind, "previous_kind")
        )
        object.__setattr__(
            self, "missing_policy", _enum(self.missing_policy, MissingPolicy, "missing_policy")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "previous",
            "anchor": self.anchor.to_dict(),
            "previous_kind": str(self.previous_kind),
            "missing_policy": str(self.missing_policy),
        }


Selector: TypeAlias = AsOfSelector | WindowSelector | PreviousSelector


def selector_from_dict(raw: Any) -> Selector:
    if not isinstance(raw, dict):
        raise TemporalIrError("selector 는 객체여야 합니다")
    kind = raw.get("kind")
    if kind == "as_of":
        return AsOfSelector(
            anchor=anchor_from_dict(raw.get("anchor")),
            strategy=strategy_from_dict(raw.get("strategy")),
            missing_policy=_enum(
                raw.get("missing_policy", MissingPolicy.UNKNOWN), MissingPolicy, "missing_policy"
            ),
        )
    if kind == "window":
        bucket = raw.get("bucket")
        anchor = raw.get("anchor")
        return WindowSelector(
            window=window_from_dict(raw.get("window")),
            bucket=_enum(bucket, TimeUnit, "window bucket") if bucket else None,
            anchor=anchor_from_dict(anchor) if isinstance(anchor, dict) else ReferenceAnchor(),
            missing_policy=_enum(
                raw.get("missing_policy", MissingPolicy.UNKNOWN), MissingPolicy, "missing_policy"
            ),
            empty_window_policy=_enum(
                raw.get("empty_window_policy", EmptyWindowPolicy.REQUIRE_OBSERVATION),
                EmptyWindowPolicy,
                "empty_window_policy",
            ),
        )
    if kind == "previous":
        return PreviousSelector(
            anchor=anchor_from_dict(raw.get("anchor")),
            previous_kind=_enum(raw.get("previous_kind"), PreviousKind, "previous_kind"),
            missing_policy=_enum(
                raw.get("missing_policy", MissingPolicy.UNKNOWN), MissingPolicy, "missing_policy"
            ),
        )
    raise TemporalIrError(f"알 수 없는 selector 종류: {kind!r}")


# ── quantifier ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExistsQuantifier:
    """선택된 관측 중 **하나라도** 성립."""

    kind: str = "exists"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "exists"}


@dataclass(frozen=True, slots=True)
class NoneQuantifier:
    """선택된 관측 중 **어느 것도** 성립하지 않음."""

    kind: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "none"}


@dataclass(frozen=True, slots=True)
class AllObservationsQuantifier:
    """**관측된** 모든 시점에서 성립(관측 사이는 말하지 않는다)."""

    kind: str = "all_observations"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "all_observations"}


@dataclass(frozen=True, slots=True)
class EveryBucketQuantifier:
    """구간을 나눈 **모든 칸**에 성립하는 관측이 최소 하나씩 있다(빈 칸이 하나라도 있으면 불성립).

    ``AllObservationsQuantifier`` 와 다르다: 저쪽은 '관측된 것이 전부 성립'이라 관측이 드문
    구간에서도 참이 될 수 있고, 이쪽은 **칸의 개수**를 세므로 빠진 칸이 곧 거짓이다.
    """

    kind: str = "every_bucket"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "every_bucket"}


@dataclass(frozen=True, slots=True)
class ConsecutiveBucketsQuantifier:
    """성립하는 칸이 **끊기지 않고 이어서** 나타난다.

    ``EveryBucketQuantifier`` 와 다르다. 저쪽은 요청한 구간이 덮는 칸이 **전부** 성립하는지를
    묻고(구간이 곧 답의 범위), 이쪽은 구간 **안 어딘가**에 길이 ``bucket_count`` 의 이어진
    구간이 있는지를 묻는다. 앵커가 있는 '최근 3개월 연속'은 앞의 질문이고, 앵커가 없는
    '아무 3개월이나 연속'은 뒤의 질문이다 — 두 집합이 다르므로 합치지 않는다.

    뒤의 질문은 주체별 정렬과 칸 사이 간격 판정(gap-and-island)이 필요하고, 실행 IR 에
    그 primitive(PartitionBy/OrderBy/Lag)가 없다. 그래서 이 quantifier 는 **의미는 정의되고
    낮춤은 거절**되는 상태로 선언된다 — 총 칸 수 비교로 근사하면 다른 집합이 조용히 나간다.
    """

    bucket_count: int
    kind: str = "consecutive_buckets"

    def __post_init__(self) -> None:
        if not isinstance(self.bucket_count, int) or isinstance(self.bucket_count, bool):
            raise TemporalIrError("consecutive_buckets: bucket_count 는 정수여야 한다")
        if self.bucket_count < 2:
            # 1칸 '연속'은 연속이 아니라 존재다. 그 뜻은 다른 quantifier 가 이미 갖고 있으므로
            # 여기서 받으면 같은 집합을 두 이름이 말하게 된다.
            raise TemporalIrError(
                "consecutive_buckets: bucket_count 는 2 이상이어야 한다"
                "(1칸은 연속이 아니라 존재 조건이다)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "consecutive_buckets", "bucket_count": self.bucket_count}


@dataclass(frozen=True, slots=True)
class ThroughoutQuantifier:
    """구간 **내내**(관측 사이 포함) 성립 — 연속 유효성을 아는 표현에서만 답할 수 있다."""

    kind: str = "throughout"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "throughout"}


@dataclass(frozen=True, slots=True)
class LatestObservationQuantifier:
    """**가장 최근 관측**이 성립.

    ``ExistsQuantifier`` 와 반드시 구분한다(§13). '최근 30일 안에 로그인 이벤트가 하나라도
    있었다'와 '마지막 로그인이 최근 30일 이내다'는 다른 질의이고, 마지막 값만 보관하는 표현은
    앞의 질문에 답할 수 없다. 결과가 우연히 같아도 계약이 다르므로 같은 quantifier 로 합치지
    않는다 — 합치는 순간 어느 쪽 뜻으로 SQL 이 나왔는지 알 수 없다.
    """

    kind: str = "latest_observation"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "latest_observation"}


Quantifier: TypeAlias = (
    ExistsQuantifier
    | NoneQuantifier
    | AllObservationsQuantifier
    | EveryBucketQuantifier
    | ConsecutiveBucketsQuantifier
    | ThroughoutQuantifier
    | LatestObservationQuantifier
)

_QUANTIFIERS: dict[str, Callable[[], "Quantifier"]] = {
    "exists": ExistsQuantifier,
    "none": NoneQuantifier,
    "all_observations": AllObservationsQuantifier,
    "every_bucket": EveryBucketQuantifier,
    "throughout": ThroughoutQuantifier,
    "latest_observation": LatestObservationQuantifier,
}
# 인자를 갖는 quantifier 는 무인자 생성자로 복원할 수 없다 — 복원 규칙을 따로 둔다.
_PARAMETERIZED_QUANTIFIERS: dict[str, Callable[[dict[str, Any]], "Quantifier"]] = {
    "consecutive_buckets": lambda raw: ConsecutiveBucketsQuantifier(
        bucket_count=raw.get("bucket_count")  # type: ignore[arg-type]
    ),
}


def quantifier_from_dict(raw: Any) -> Quantifier:
    if not isinstance(raw, dict):
        raise TemporalIrError(f"알 수 없는 quantifier: {raw!r}")
    kind = str(raw.get("kind"))
    if kind in _PARAMETERIZED_QUANTIFIERS:
        return _PARAMETERIZED_QUANTIFIERS[kind](raw)
    if kind not in _QUANTIFIERS:
        raise TemporalIrError(f"알 수 없는 quantifier: {raw!r}")
    return _QUANTIFIERS[kind]()


# ── 비교와 술어 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Comparison:
    """값 도메인 canonical 값과의 비교. **왼쪽은 없다** — 어느 컬럼인지는 binding 이 안다."""

    operator: str
    value: str
    kind: str = "comparison"

    def __post_init__(self) -> None:
        operator = event_ir.canonical_comparison_operator(self.operator)
        if operator is None:
            raise TemporalIrError(f"지원하지 않는 비교 연산자: {self.operator!r}")
        object.__setattr__(self, "operator", operator)
        _symbol(self.value, "comparison value")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "comparison", "operator": self.operator, "value": self.value}


@dataclass(frozen=True, slots=True)
class NumericComparison:
    """수치 비교. 정밀도가 필요한 값이므로 :class:`~decimal.Decimal` 로 담는다."""

    operator: str
    value: Decimal
    kind: str = "numeric_comparison"

    def __post_init__(self) -> None:
        operator = event_ir.canonical_comparison_operator(self.operator)
        if operator is None:
            raise TemporalIrError(f"지원하지 않는 비교 연산자: {self.operator!r}")
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "value", _decimal(self.value, "numeric comparison value"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "numeric_comparison",
            "operator": self.operator,
            "value": str(self.value),
        }


@dataclass(frozen=True, slots=True)
class AnyValueChange:
    """값이 무엇에서 무엇으로든 바뀐 사건."""

    kind: str = "any_value_change"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "any_value_change"}


@dataclass(frozen=True, slots=True)
class ValueChange:
    """지정한 두 값 사이의 변화."""

    from_value: str
    to_value: str
    kind: str = "value_change"

    def __post_init__(self) -> None:
        _symbol(self.from_value, "value change from_value")
        _symbol(self.to_value, "value change to_value")
        if self.from_value == self.to_value:
            raise TemporalIrError("값 변화는 서로 다른 두 값이 필요합니다")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "value_change", "from_value": self.from_value, "to_value": self.to_value}


ChangeSelector: TypeAlias = AnyValueChange | ValueChange


@dataclass(frozen=True, slots=True)
class StatePredicate:
    """관측 시점의 **상태 값**에 대한 조건."""

    comparison: Comparison
    null_policy: NullPolicy = NullPolicy.EXCLUDE
    kind: str = "state"

    def __post_init__(self) -> None:
        object.__setattr__(self, "null_policy", _enum(self.null_policy, NullPolicy, "null_policy"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "state",
            "comparison": self.comparison.to_dict(),
            "null_policy": str(self.null_policy),
        }


@dataclass(frozen=True, slots=True)
class OccurrencePredicate:
    """관측(사건) 그 자체 — 값 조건 없음. '구매했다'·'로그인했다'가 이 술어다."""

    kind: str = "occurrence"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "occurrence"}


@dataclass(frozen=True, slots=True)
class TransitionPredicate:
    """값이 A 에서 B 로 바뀌었다. ``transition_mode`` 가 그 '바뀜'의 뜻을 정한다."""

    from_value: str
    to_value: str
    transition_mode: TransitionMode
    kind: str = "transition"

    def __post_init__(self) -> None:
        _symbol(self.from_value, "transition from_value")
        _symbol(self.to_value, "transition to_value")
        object.__setattr__(
            self, "transition_mode", _enum(self.transition_mode, TransitionMode, "transition_mode")
        )
        if self.from_value == self.to_value:
            raise TemporalIrError("전이는 서로 다른 두 값이 필요합니다")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "transition",
            "from_value": self.from_value,
            "to_value": self.to_value,
            "transition_mode": str(self.transition_mode),
        }


@dataclass(frozen=True, slots=True)
class ChangeCountPredicate:
    """구간 안의 값 변경 횟수."""

    transition: ChangeSelector
    comparison: NumericComparison
    kind: str = "change_count"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "change_count",
            "transition": self.transition.to_dict(),
            "comparison": self.comparison.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UnchangedPredicate:
    """값이 바뀌지 않았다. 관측 기준인지 연속 기준인지를 반드시 밝힌다(§10)."""

    observation_semantics: ObservationSemantics
    kind: str = "unchanged"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_semantics",
            _enum(self.observation_semantics, ObservationSemantics, "observation_semantics"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "unchanged", "observation_semantics": str(self.observation_semantics)}


@dataclass(frozen=True, slots=True)
class Duration:
    """기간의 **길이**(구간이 아니다)."""

    amount: int
    unit: TimeUnit
    kind: str = "duration"

    def __post_init__(self) -> None:
        _positive_int(self.amount, "duration amount")
        object.__setattr__(self, "unit", _enum(self.unit, TimeUnit, "duration unit"))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "duration", "amount": self.amount, "unit": str(self.unit)}


@dataclass(frozen=True, slots=True)
class TemporalRelationPredicate:
    """두 사건 발생 시점 사이의 관계.

    읽는 방향은 ``left_binding <relation> right_binding`` 이다 — ``within_after`` 면
    "left 가 right 이후 duration 이내에 발생했다"(가입 후 7일 이내 구매 = left 구매,
    right 가입). ``anchor_occurrence`` 는 기준(right)이 여러 번 발생할 수 있을 때 어느 발생을
    잡는지이며, 그때는 생략할 수 없다.
    """

    left_binding: str
    right_binding: str
    relation: RelationKind
    duration: Duration
    anchor_occurrence: OccurrenceSelector | None = None
    kind: str = "temporal_relation"

    def __post_init__(self) -> None:
        _symbol(self.left_binding, "temporal relation left_binding")
        _symbol(self.right_binding, "temporal relation right_binding")
        object.__setattr__(self, "relation", _enum(self.relation, RelationKind, "relation"))
        if self.anchor_occurrence is not None:
            object.__setattr__(
                self,
                "anchor_occurrence",
                _enum(self.anchor_occurrence, OccurrenceSelector, "anchor_occurrence"),
            )
        if self.left_binding == self.right_binding and self.anchor_occurrence is None:
            raise TemporalIrError(
                "같은 관측 사이의 시간 관계에는 기준 발생(anchor_occurrence)이 필요합니다"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "temporal_relation",
            "left_binding": self.left_binding,
            "right_binding": self.right_binding,
            "relation": str(self.relation),
            "duration": self.duration.to_dict(),
            "anchor_occurrence": (
                str(self.anchor_occurrence) if self.anchor_occurrence else None
            ),
        }


Predicate: TypeAlias = (
    StatePredicate
    | OccurrencePredicate
    | TransitionPredicate
    | ChangeCountPredicate
    | UnchangedPredicate
    | TemporalRelationPredicate
)


def predicate_from_dict(raw: Any) -> Predicate:
    if not isinstance(raw, dict):
        raise TemporalIrError("predicate 는 객체여야 합니다")
    kind = raw.get("kind")
    if kind == "state":
        return StatePredicate(
            comparison=_comparison_from_dict(raw.get("comparison")),
            null_policy=_enum(
                raw.get("null_policy", NullPolicy.EXCLUDE), NullPolicy, "null_policy"
            ),
        )
    if kind == "occurrence":
        return OccurrencePredicate()
    if kind == "transition":
        return TransitionPredicate(
            from_value=str(raw.get("from_value") or ""),
            to_value=str(raw.get("to_value") or ""),
            transition_mode=_enum(raw.get("transition_mode"), TransitionMode, "transition_mode"),
        )
    if kind == "change_count":
        return ChangeCountPredicate(
            transition=_change_selector_from_dict(raw.get("transition")),
            comparison=_numeric_comparison_from_dict(raw.get("comparison")),
        )
    if kind == "unchanged":
        return UnchangedPredicate(
            observation_semantics=_enum(
                raw.get("observation_semantics"), ObservationSemantics, "observation_semantics"
            )
        )
    if kind == "temporal_relation":
        duration = raw.get("duration")
        if not isinstance(duration, dict):
            raise TemporalIrError("temporal relation 에는 duration 이 필요합니다")
        occurrence = raw.get("anchor_occurrence")
        return TemporalRelationPredicate(
            left_binding=str(raw.get("left_binding") or ""),
            right_binding=str(raw.get("right_binding") or ""),
            relation=_enum(raw.get("relation"), RelationKind, "relation"),
            anchor_occurrence=(
                _enum(occurrence, OccurrenceSelector, "anchor_occurrence") if occurrence else None
            ),
            duration=Duration(
                amount=int(duration.get("amount", 0)),
                unit=_enum(duration.get("unit"), TimeUnit, "duration unit"),
            ),
        )
    raise TemporalIrError(f"알 수 없는 predicate 종류: {kind!r}")


def _comparison_from_dict(raw: Any) -> Comparison:
    if not isinstance(raw, dict):
        raise TemporalIrError("comparison 은 객체여야 합니다")
    return Comparison(operator=str(raw.get("operator") or ""), value=str(raw.get("value") or ""))


def _numeric_comparison_from_dict(raw: Any) -> NumericComparison:
    if not isinstance(raw, dict):
        raise TemporalIrError("numeric comparison 은 객체여야 합니다")
    return NumericComparison(
        operator=str(raw.get("operator") or ""), value=_decimal(raw.get("value"), "value")
    )


def _change_selector_from_dict(raw: Any) -> ChangeSelector:
    if not isinstance(raw, dict):
        raise TemporalIrError("change selector 는 객체여야 합니다")
    if raw.get("kind") == "any_value_change":
        return AnyValueChange()
    if raw.get("kind") == "value_change":
        return ValueChange(
            from_value=str(raw.get("from_value") or ""), to_value=str(raw.get("to_value") or "")
        )
    raise TemporalIrError(f"알 수 없는 change selector: {raw.get('kind')!r}")


# ── 조건 ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TemporalCondition:
    """시간 의미 하나. ``metric`` 은 '무엇을', ``binding`` 은 '어느 관측 방식으로'다.

    ``binding`` 이 ``None`` 이면 :mod:`temporal_ir.catalog` 의 resolver 가 **요청한 연산을
    지원하는** binding 을 고른다. 후보가 둘 이상이고 우선순위 선언이 없으면 고르지 않는다 —
    임의 선택은 절반의 확률로 다른 집합을 내보내고, 그 오답은 실행 결과로만 드러난다.
    """

    metric: str
    selector: Selector
    quantifier: Quantifier
    predicate: Predicate
    binding: str | None = None
    evidence: Evidence | None = None
    kind: str = "temporal_condition"

    def __post_init__(self) -> None:
        _symbol(self.metric, "temporal condition metric")
        if self.binding is not None:
            _symbol(self.binding, "temporal condition binding")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "temporal_condition",
            "metric": self.metric,
            "binding": self.binding,
            "selector": self.selector.to_dict(),
            "quantifier": self.quantifier.to_dict(),
            "predicate": self.predicate.to_dict(),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    def with_binding(self, binding: str) -> "TemporalCondition":
        return replace(self, binding=binding)


def anchor_of(condition: TemporalCondition) -> Anchor | None:
    """이 조건이 가리키는 기준 시점(구간 선택자는 자신의 anchor 를 갖는다)."""
    selector = condition.selector
    if isinstance(selector, (AsOfSelector, PreviousSelector)):
        return selector.anchor
    if isinstance(selector, WindowSelector):
        return selector.anchor
    return None  # pragma: no cover - selector 어휘가 닫혀 있어 도달 불가


def condition_from_dict(raw: Any) -> TemporalCondition:
    if not isinstance(raw, dict):
        raise TemporalIrError("temporal condition 은 객체여야 합니다")
    if raw.get("kind") != "temporal_condition":
        raise TemporalIrError(f"temporal condition 이 아닙니다: {raw.get('kind')!r}")
    evidence = raw.get("evidence")
    binding = raw.get("binding")
    return TemporalCondition(
        metric=str(raw.get("metric") or ""),
        selector=selector_from_dict(raw.get("selector")),
        quantifier=quantifier_from_dict(raw.get("quantifier")),
        predicate=predicate_from_dict(raw.get("predicate")),
        binding=str(binding) if binding else None,
        evidence=Evidence.from_dict(evidence) if isinstance(evidence, dict) else None,
    )


# ── 요청 문맥 ─────────────────────────────────────────────────────────────────────


DEFAULT_TIMEZONE = "Asia/Seoul"


@dataclass(frozen=True, slots=True)
class TemporalRequestContext:
    """요청 하나의 기준 시각과 정책. **여기 없는 시계는 쓰지 않는다**(§12·§15).

    ``coverage_policy`` 의 기본이 ``ADVISE`` 인 이유: 적재 범위 밖이라는 사실은 SQL 을 만들지
    못할 이유가 아니라 **결과 해석의 정보**다. 의미를 표현할 수 없는 경우(unsupported)와는
    다른 축이며, 그 둘을 하나로 묶으면 "데이터가 아직 없어서" 표현 가능한 질의가 거절된다.
    """

    now: datetime
    timezone: str = DEFAULT_TIMEZONE
    coverage_policy: CoveragePolicy = CoveragePolicy.ADVISE

    def __post_init__(self) -> None:
        _aware(self.now, "request context now")
        _symbol(self.timezone, "request context timezone")
        object.__setattr__(
            self, "coverage_policy", _enum(self.coverage_policy, CoveragePolicy, "coverage_policy")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "timezone": self.timezone,
            "coverage_policy": str(self.coverage_policy),
        }


__all__ = [
    "AbsoluteAnchor",
    "AbsoluteWindow",
    "AllObservationsQuantifier",
    "Anchor",
    "AnyValueChange",
    "AsOfSelector",
    "Boundary",
    "ChangeCountPredicate",
    "ChangeSelector",
    "Comparison",
    "CoveragePolicy",
    "DEFAULT_TIMEZONE",
    "Direction",
    "Duration",
    "EmptyWindowPolicy",
    "Evidence",
    "ConsecutiveBucketsQuantifier",
    "EveryBucketQuantifier",
    "ExactBucket",
    "ExistsQuantifier",
    "LatestAtOrBefore",
    "LatestObservationQuantifier",
    "LifetimeWindow",
    "MissingPolicy",
    "NoneQuantifier",
    "NullPolicy",
    "NumericComparison",
    "ObservationSemantics",
    "OccurrenceSelector",
    "OccurrencePredicate",
    "OpenEndedWindow",
    "Predicate",
    "PreviousKind",
    "PreviousSelector",
    "Quantifier",
    "ReferenceAnchor",
    "RelationKind",
    "RelativeAnchor",
    "RelativeWindow",
    "SelectionStrategy",
    "Selector",
    "StatePredicate",
    "TemporalCondition",
    "TemporalIrError",
    "TemporalRelationPredicate",
    "TemporalRequestContext",
    "TemporalWindow",
    "ThroughoutQuantifier",
    "TimeUnit",
    "TransitionMode",
    "TransitionPredicate",
    "UNIT_RANK",
    "UnchangedPredicate",
    "ValueChange",
    "WindowMode",
    "WindowSelector",
    "anchor_from_dict",
    "anchor_of",
    "condition_from_dict",
    "predicate_from_dict",
    "quantifier_from_dict",
    "selector_from_dict",
    "strategy_from_dict",
    "window_from_dict",
]
