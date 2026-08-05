"""Event IR 모델 — canonical enum 과 typed value 만 담는 immutable 자료구조.

이 파일의 불변식 하나: **여기 있는 어떤 필드도 한국어 표면어를 의미로 쓰지 않는다.**
``ScopeFilter.value`` 만이 원문 문자열을 보존하는데, 그것은 표면어가 아니라 **데이터 값**
(브랜드명·상품명)이고 ``resolved_id`` 로 실제 식별자에 접지된다.

수치 정책(CLAUDE.md 14): 금액처럼 정밀도가 필요한 값에는 ``float`` 를 쓰지 않는다. 그래서
:class:`MetricCondition` 의 ``value`` 는 ``int | Decimal`` 이다. 반대로 ``confidence`` /
``score`` 는 도메인 금액이나 비율이 아니라 **탐색 점수**이므로 ``float`` 를 유지한다 —
두 종류를 같은 타입으로 뭉개면 어느 쪽 정밀도 규칙이 적용되는지가 흐려진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, TypeAlias

from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    IssueSeverity,
    LogicOperator,
    MetricType,
    Polarity,
    SortDirection,
    TimeUnit,
)
from nl_event_ir.tokenizer import SemanticToken

__all__ = [
    "AbsoluteDateRange",
    "ConditionGroup",
    "ConditionNode",
    "EventCondition",
    "EventIR",
    "IRDeserializationError",
    "condition_from_dict",
    "event_ir_from_dict",
    "MetricCondition",
    "NormalizedText",
    "ParseResult",
    "RelativeTimeWindow",
    "RankSpec",
    "ScopeFilter",
    "TextEdit",
    "TimeWindow",
    "ValidationIssue",
]


# ── 정규화 텍스트와 위치 매핑 ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TextEdit:
    """정규화가 바꾼 구간 하나. 원문 좌표와 정규화 좌표를 함께 들고 있다.

    ``두 번`` → ``2번`` 처럼 길이가 바뀌는 치환이 있으므로, 정규화 텍스트에서 뽑은 토큰
    스팬을 원문 스팬으로 되돌리려면 이 기록이 필요하다. 이것이 없으면 사용자에게 "원문의
    어디를 그렇게 읽었는지" 를 보여줄 수 없다.
    """

    original_start: int
    original_end: int
    normalized_start: int
    normalized_end: int


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """원문 + 정규화 결과 + 둘 사이의 위치 대응.

    ``edits`` 는 정규화 좌표 기준으로 오름차순 · 비중첩이어야 한다. 편집이 하나도 없으면
    두 좌표계는 동일하고 :meth:`to_original_span` 은 항등 함수가 된다.
    """

    original: str
    normalized: str
    edits: tuple[TextEdit, ...] = ()

    def to_original_span(self, start: int, end: int) -> tuple[int, int]:
        """정규화 좌표의 ``[start, end)`` 를 원문 좌표로 되돌린다.

        편집 구간 **안쪽**에 걸리는 경계는 그 편집 구간의 원문 경계로 스냅한다. 편집은
        분해 불가능한 단위이기 때문이다(``2번`` 의 ``2`` 하나만 원문 ``두 번`` 의 어디에
        대응하는지는 정의되지 않는다).
        """

        return (
            self._map_offset(start, snap_to_edit_end=False),
            self._map_offset(end, snap_to_edit_end=True),
        )

    def _map_offset(self, offset: int, *, snap_to_edit_end: bool) -> int:
        delta = 0
        for edit in self.edits:
            if edit.normalized_end <= offset:
                # 편집 전체가 offset 왼쪽에 있다 — 누적 길이 차이만 반영한다.
                delta += (edit.original_end - edit.original_start) - (
                    edit.normalized_end - edit.normalized_start
                )
                continue
            if edit.normalized_start < offset < edit.normalized_end:
                return edit.original_end if snap_to_edit_end else edit.original_start
            # 편집이 offset 오른쪽에 있다. edits 가 정렬돼 있으므로 더 볼 필요가 없다.
            break
        return offset + delta


# ── 시간 표현 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RelativeTimeWindow:
    """기준 시각으로부터 거슬러 올라가는 상대 기간.

    단위를 일수로 환산하지 않는다 — ``3개월`` 은 90일이 아니다. 실제 경계는 평가 시점의
    기준 시각과 함께 컴파일 단계가 계산한다.
    """

    value: int
    unit: TimeUnit

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit.value}


@dataclass(frozen=True, slots=True)
class AbsoluteDateRange:
    """달력상 확정된 구간. 양끝은 포함(inclusive)이며 한쪽만 있을 수 있다."""

    start: date | None
    end: date | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
        }


TimeWindow: TypeAlias = RelativeTimeWindow | AbsoluteDateRange


# ── 조건 구성 요소 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScopeFilter:
    """이벤트의 범위를 좁히는 한정자. 브랜드·상품·카테고리 같은 **데이터 값**을 가리킨다.

    ``value`` 는 사용자가 쓴 원문 값이고, ``resolved_id`` 는 repository 가 접지한 식별자다.
    ``resolved_id`` 가 ``None`` 이면 아직 접지되지 않은 것이며, 그 상태로 SQL 을 만들면
    안 된다(검증기가 잡는다).
    """

    entity_type: EntityType
    operator: ComparisonOperator
    value: str
    resolved_id: str | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type.value,
            "operator": self.operator.value,
            "value": self.value,
            "resolved_id": self.resolved_id,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class MetricCondition:
    """이벤트 집합에 대한 집계 비교. ``최근 3개월 2번 이상`` 의 '2번 이상' 부분이다."""

    metric_type: MetricType
    operator: ComparisonOperator
    value: int | Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_type": self.metric_type.value,
            "operator": self.operator.value,
            # Decimal 은 JSON 기본 타입이 아니다. 정밀도를 잃지 않도록 문자열로 낸다
            # (CLAUDE.md 39: 직렬화 규칙을 명시한다).
            "value": self.value if isinstance(self.value, int) else str(self.value),
        }


@dataclass(frozen=True, slots=True)
class RankSpec:
    """순위 한정('상위 N명'). 정렬 방향을 표면어에서 추측하지 않고 명시적으로 들고 있다."""

    limit: int
    direction: SortDirection

    def to_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "direction": self.direction.value}


# ── 조건 트리 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EventCondition:
    """단일 사건 조건. '무엇을 / 있나 없나 / 언제 / 어떤 범위로 / 얼마나'가 한 노드에 모인다.

    시간과 횟수를 사건에 **직접** 붙여 두는 것이 이 모델의 요점이다. 전역 목록으로 따로
    뽑아 두었다가 나중에 짝지으면, 극성이 다른 두 조건이 한 문장에 있을 때 창이 반대편에
    붙는 결함이 생긴다.
    """

    event_type: EventType
    polarity: Polarity = Polarity.POSITIVE
    time_window: TimeWindow | None = None
    scopes: tuple[ScopeFilter, ...] = ()
    metric: MetricCondition | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "polarity": self.polarity.value,
            "time_window": self.time_window.to_dict() if self.time_window is not None else None,
            "scopes": [scope.to_dict() for scope in self.scopes],
            "metric": self.metric.to_dict() if self.metric is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ConditionGroup:
    """조건들의 논리 결합. ``NOT`` 은 자식이 정확히 하나여야 한다."""

    operator: LogicOperator
    children: tuple[ConditionNode, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ValueError("ConditionGroup requires at least one child condition")
        if self.operator is LogicOperator.NOT and len(self.children) != 1:
            raise ValueError(
                f"LogicOperator.NOT takes exactly one child, got {len(self.children)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator.value,
            "children": [child.to_dict() for child in self.children],
        }


ConditionNode: TypeAlias = EventCondition | ConditionGroup


@dataclass(frozen=True, slots=True)
class EventIR:
    """파서의 최종 산출물.

    ``condition`` 을 :data:`ConditionNode` 로 둔 것은 의도적이다. 단일 조건 문장은 그대로
    :class:`EventCondition` 이 들어가고(최소 사양과 동일), ``또는``/``그리고`` 가 있는 문장은
    같은 필드에 :class:`ConditionGroup` 이 들어간다 — 복수 조건을 위해 모델을 바꿀 필요가 없다.
    """

    target_entity: EntityType
    condition: ConditionNode
    rank: RankSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_entity": self.target_entity.value,
            "condition": self.condition.to_dict(),
        }
        if self.rank is not None:
            payload["rank"] = self.rank.to_dict()
        return payload


# ── 검증 결과와 파싱 결과 ─────────────────────────────────────────────────────────


# ── 역직렬화 ─────────────────────────────────────────────────────────────────────
#
# ``to_dict`` 만 있고 역이 없으면 저장한 IR 을 다시 읽을 수 없고, round-trip 테스트도
# ``json.loads(json.dumps(x)) == x`` 라는 **json 모듈에 대한 동어반복**이 되어 버린다.
# 아래 함수들이 있어야 '직렬화가 의미를 보존하는가'를 실제로 검사할 수 있다(CLAUDE.md 40).


class IRDeserializationError(Exception):
    """저장된 IR 을 모델로 되돌릴 수 없다. 무엇이 어긋났는지 함께 전달한다."""


def event_ir_from_dict(payload: dict[str, Any]) -> EventIR:
    """``EventIR.to_dict()`` 의 역함수."""

    if not isinstance(payload, dict):
        raise IRDeserializationError(f"EventIR payload must be an object, got {type(payload).__name__}")
    try:
        target = EntityType(payload["target_entity"])
    except KeyError as exc:
        raise IRDeserializationError("EventIR payload has no 'target_entity'") from exc
    except ValueError as exc:
        raise IRDeserializationError(
            f"unknown target_entity: {payload.get('target_entity')!r}"
        ) from exc

    raw_rank = payload.get("rank")
    rank = None
    if raw_rank is not None:
        rank = RankSpec(
            limit=int(raw_rank["limit"]),
            direction=SortDirection(raw_rank["direction"]),
        )
    return EventIR(
        target_entity=target,
        condition=condition_from_dict(payload.get("condition")),
        rank=rank,
    )


def condition_from_dict(payload: Any) -> ConditionNode:
    """조건 노드를 판별해 복원한다. 그룹과 단일 조건은 키로 구분된다."""

    if not isinstance(payload, dict):
        raise IRDeserializationError(
            f"condition must be an object, got {type(payload).__name__}"
        )
    if "operator" in payload and "children" in payload:
        try:
            operator = LogicOperator(payload["operator"])
        except ValueError as exc:
            raise IRDeserializationError(
                f"unknown logic operator: {payload['operator']!r}"
            ) from exc
        return ConditionGroup(
            operator=operator,
            children=tuple(condition_from_dict(child) for child in payload["children"]),
        )

    try:
        event_type = EventType(payload["event_type"])
    except KeyError as exc:
        raise IRDeserializationError("condition has no 'event_type'") from exc
    except ValueError as exc:
        raise IRDeserializationError(
            f"unknown event_type: {payload.get('event_type')!r}"
        ) from exc

    return EventCondition(
        event_type=event_type,
        polarity=Polarity(payload.get("polarity", Polarity.POSITIVE.value)),
        time_window=_time_window_from_dict(payload.get("time_window")),
        scopes=tuple(_scope_from_dict(scope) for scope in payload.get("scopes", ())),
        metric=_metric_from_dict(payload.get("metric")),
    )


def _time_window_from_dict(payload: Any) -> TimeWindow | None:
    if payload is None:
        return None
    if "unit" in payload:
        return RelativeTimeWindow(
            value=int(payload["value"]), unit=TimeUnit(payload["unit"])
        )
    return AbsoluteDateRange(
        start=date.fromisoformat(payload["start"]) if payload.get("start") else None,
        end=date.fromisoformat(payload["end"]) if payload.get("end") else None,
    )


def _scope_from_dict(payload: Any) -> ScopeFilter:
    return ScopeFilter(
        entity_type=EntityType(payload["entity_type"]),
        operator=ComparisonOperator(payload["operator"]),
        value=payload["value"],
        resolved_id=payload.get("resolved_id"),
        confidence=payload.get("confidence"),
    )


def _metric_from_dict(payload: Any) -> MetricCondition | None:
    if payload is None:
        return None
    raw_value = payload["value"]
    # 금액은 ``to_dict`` 에서 문자열로 나갔다. 문자열이면 Decimal 로 되돌려야 정밀도가
    # 보존된다 — float 으로 받으면 라운드트립이 값을 바꾼다(CLAUDE.md 14).
    value: int | Decimal = raw_value if isinstance(raw_value, int) else Decimal(str(raw_value))
    return MetricCondition(
        metric_type=MetricType(payload["metric_type"]),
        operator=ComparisonOperator(payload["operator"]),
        value=value,
    )


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """구조화된 검증 결과 한 건. 문자열 오류 대신 이 타입을 쓴다(CLAUDE.md 34).

    ``code`` 는 기계가 분기하는 안정적인 키이고, ``message`` 는 사람이 읽는 설명이며,
    ``path`` 는 IR 안에서 문제가 있는 위치다.
    """

    code: str
    message: str
    path: str
    severity: IssueSeverity = IssueSeverity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class ParseResult:
    """파싱 한 번의 전체 결과.

    ``ir`` 이 ``None`` 이라고 해서 조용히 실패하지 않는다 — ``issues`` 에 이유가 남고
    ``fallback_required`` 로 다음 단계(semantic fallback)가 필요함을 명시한다(CLAUDE.md 11).
    """

    original_text: str
    normalized_text: str
    tokens: tuple[SemanticToken, ...]
    ir: EventIR | None
    issues: tuple[ValidationIssue, ...] = ()
    fallback_required: bool = False
    fallback_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # 토큰 스팬은 **정규화 텍스트** 좌표계다. 원문 좌표가 필요할 때(사용자에게 '문장의 이
    # 부분을 이렇게 읽었다'를 보여줄 때)를 위해 대응표를 함께 들고 나간다. 이것이 없으면
    # 정규화기가 만든 위치 정보가 파이프라인 밖으로 나오지 못한다.
    normalization: NormalizedText | None = None

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is IssueSeverity.ERROR)

    def original_span(self, token: SemanticToken) -> tuple[int, int]:
        """토큰이 원문의 어느 구간을 읽은 것인지 돌려준다."""

        if self.normalization is None:
            return token.span
        return self.normalization.to_original_span(token.start, token.end)

    def original_text_of(self, token: SemanticToken) -> str:
        start, end = self.original_span(token)
        return self.original_text[start:end]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_text": self.original_text,
            "normalized_text": self.normalized_text,
            "ir": self.ir.to_dict() if self.ir is not None else None,
            "issues": [issue.to_dict() for issue in self.issues],
            "fallback_required": self.fallback_required,
            "fallback_reason": self.fallback_reason,
        }
