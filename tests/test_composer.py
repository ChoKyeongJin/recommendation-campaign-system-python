"""composer 단독 테스트 — 토큰을 직접 만들어 조합 규칙만 검증한다.

문자열을 넣지 않는 것이 요점이다. composer 는 텍스트를 읽지 않으므로, 텍스트로 테스트하면
추출기의 결함이 composer 의 결함처럼 보인다.
"""

from __future__ import annotations

from datetime import date

from nl_event_ir.composer import EventIRComposer
from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    IssueSeverity,
    LogicOperator,
    MetricType,
    Polarity,
    TimeUnit,
)
from nl_event_ir.models import (
    AbsoluteDateRange,
    ConditionGroup,
    EventCondition,
    MetricCondition,
    RelativeTimeWindow,
)
from nl_event_ir.resolver.base import EntityCandidate, EntityResolution
from nl_event_ir.tokenizer import SemanticToken, TokenKind

NIKE = EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0)
ADIDAS = EntityCandidate(EntityType.BRAND, "brand:adidas", "아디다스", 1.0)


def _token(kind: TokenKind, value: object, start: int, length: int = 2) -> SemanticToken:
    return SemanticToken(
        kind=kind,
        value=value,
        start=start,
        end=start + length,
        raw_text="x" * length,
    )


def _resolved(candidate: EntityCandidate, start: int) -> SemanticToken:
    return _token(
        TokenKind.ENTITY_VALUE,
        EntityResolution(
            raw_value=candidate.canonical_name,
            candidates=(candidate,),
            selected=candidate,
        ),
        start,
        len(candidate.canonical_name),
    )


def _codes(issues: tuple) -> set[str]:
    return {issue.code for issue in issues}


# ── 기본 조합 ────────────────────────────────────────────────────────────────────


def test_composes_single_event_condition() -> None:
    tokens = [
        _token(TokenKind.TIME_WINDOW, RelativeTimeWindow(3, TimeUnit.MONTH), 0),
        _resolved(NIKE, 10),
        _token(
            TokenKind.COUNT_CONDITION,
            MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GTE, 2),
            15,
        ),
        _token(TokenKind.EVENT, EventType.PURCHASE, 22),
        _token(TokenKind.TARGET, EntityType.MEMBER, 25),
    ]
    outcome = EventIRComposer().compose("", tokens)

    assert outcome.issues == ()
    assert outcome.ir is not None
    assert outcome.ir.target_entity is EntityType.MEMBER
    condition = outcome.ir.condition
    assert isinstance(condition, EventCondition)
    assert condition.event_type is EventType.PURCHASE
    assert condition.time_window == RelativeTimeWindow(3, TimeUnit.MONTH)
    assert condition.metric == MetricCondition(
        MetricType.EVENT_COUNT, ComparisonOperator.GTE, 2
    )
    assert condition.scopes[0].resolved_id == "brand:nike"


def test_missing_event_is_an_error_not_a_guess() -> None:
    outcome = EventIRComposer().compose(
        "", [_token(TokenKind.TARGET, EntityType.MEMBER, 0)]
    )
    assert outcome.ir is None
    assert "event.missing" in _codes(outcome.issues)


def test_missing_target_defaults_to_member_but_says_so() -> None:
    outcome = EventIRComposer().compose(
        "", [_token(TokenKind.EVENT, EventType.PURCHASE, 0)]
    )
    assert outcome.ir is not None
    assert outcome.ir.target_entity is EntityType.MEMBER
    assert "target.defaulted" in _codes(outcome.issues)


# ── 귀속 ─────────────────────────────────────────────────────────────────────────


def test_modifiers_attach_to_the_nearest_event() -> None:
    """사건이 둘이면 각 창은 자기 절의 사건에 붙어야 한다.

    전역 목록으로 뽑아 뒤에 짝짓는 방식이 깨지는 지점이 정확히 여기다.
    """

    tokens = [
        _token(TokenKind.TIME_WINDOW, RelativeTimeWindow(1, TimeUnit.MONTH), 0),
        _token(TokenKind.EVENT, EventType.PURCHASE, 5),
        _token(TokenKind.LOGIC, LogicOperator.AND, 10),
        _token(TokenKind.TIME_WINDOW, RelativeTimeWindow(7, TimeUnit.DAY), 20),
        _token(TokenKind.EVENT, EventType.LOGIN, 25),
        _token(TokenKind.TARGET, EntityType.MEMBER, 30),
    ]
    outcome = EventIRComposer().compose("", tokens)

    group = outcome.ir.condition
    assert isinstance(group, ConditionGroup)
    assert group.operator is LogicOperator.AND
    first, second = group.children
    assert first.event_type is EventType.PURCHASE
    assert first.time_window == RelativeTimeWindow(1, TimeUnit.MONTH)
    assert second.event_type is EventType.LOGIN
    assert second.time_window == RelativeTimeWindow(7, TimeUnit.DAY)


def test_polarity_attaches_to_its_own_event() -> None:
    tokens = [
        _token(TokenKind.EVENT, EventType.PURCHASE, 0),
        _token(TokenKind.EVENT, EventType.LOGIN, 20),
        _token(TokenKind.POLARITY, Polarity.NEGATIVE, 23),
        _token(TokenKind.TARGET, EntityType.MEMBER, 30),
    ]
    outcome = EventIRComposer().compose("", tokens)

    group = outcome.ir.condition
    assert isinstance(group, ConditionGroup)
    purchase, login = group.children
    assert purchase.polarity is Polarity.POSITIVE
    assert login.polarity is Polarity.NEGATIVE


# ── 논리 결합 ────────────────────────────────────────────────────────────────────


def test_or_between_scopes_becomes_a_condition_group() -> None:
    """``A 또는 B 를 구매`` 를 scopes 튜플에 담으면 '둘 다 산 사람'이 되어 뜻이 뒤집힌다."""

    tokens = [
        _resolved(ADIDAS, 0),
        _token(TokenKind.LOGIC, LogicOperator.OR, 5),
        _resolved(NIKE, 8),
        _token(TokenKind.EVENT, EventType.PURCHASE, 15),
        _token(TokenKind.TARGET, EntityType.MEMBER, 20),
    ]
    outcome = EventIRComposer().compose("", tokens)

    group = outcome.ir.condition
    assert isinstance(group, ConditionGroup)
    assert group.operator is LogicOperator.OR
    assert [child.scopes[0].resolved_id for child in group.children] == [
        "brand:adidas",
        "brand:nike",
    ]


def test_two_scopes_without_or_stay_conjunctive() -> None:
    tokens = [
        _resolved(ADIDAS, 0),
        _resolved(NIKE, 8),
        _token(TokenKind.EVENT, EventType.PURCHASE, 15),
        _token(TokenKind.TARGET, EntityType.MEMBER, 20),
    ]
    outcome = EventIRComposer().compose("", tokens)

    condition = outcome.ir.condition
    assert isinstance(condition, EventCondition)
    assert len(condition.scopes) == 2


# ── canonical 형태 고정 ──────────────────────────────────────────────────────────


def test_zero_count_is_folded_into_negative_polarity() -> None:
    """``한 번도 ~ 않다`` 는 횟수 0 과 부정이 같은 뜻이다. IR 에 두 번 담지 않는다."""

    tokens = [
        _token(
            TokenKind.COUNT_CONDITION,
            MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.EQ, 0),
            0,
        ),
        _token(TokenKind.EVENT, EventType.LOGIN, 5),
        _token(TokenKind.TARGET, EntityType.MEMBER, 10),
    ]
    outcome = EventIRComposer().compose("", tokens)

    condition = outcome.ir.condition
    assert condition.polarity is Polarity.NEGATIVE
    assert condition.metric is None


# ── 충돌·모호 ────────────────────────────────────────────────────────────────────


def test_conflicting_metrics_stop_composition() -> None:
    """서로 다른 집계를 한 사건에 담을 자리가 없다. 하나를 버리면 조건이 조용히 사라진다."""

    tokens = [
        _token(
            TokenKind.COUNT_CONDITION,
            MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GTE, 2),
            0,
        ),
        _token(
            TokenKind.COUNT_CONDITION,
            MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.LTE, 5),
            4,
        ),
        _token(TokenKind.EVENT, EventType.PURCHASE, 10),
    ]
    outcome = EventIRComposer().compose("", tokens)

    assert outcome.ir is None
    assert "metric.conflict" in _codes(outcome.issues)


def test_ambiguous_entity_stops_composition() -> None:
    ambiguous = _token(
        TokenKind.ENTITY_VALUE,
        EntityResolution(
            raw_value="애플",
            candidates=(
                EntityCandidate(EntityType.BRAND, "brand:apple", "애플", 1.0),
                EntityCandidate(EntityType.PRODUCT, "product:apple", "애플", 1.0),
            ),
            selected=None,
        ),
        0,
    )
    outcome = EventIRComposer().compose(
        "", [ambiguous, _token(TokenKind.EVENT, EventType.PURCHASE, 5)]
    )

    assert outcome.ir is None
    assert "entity.ambiguous" in _codes(outcome.issues)


def test_unknown_entity_is_reported_not_silently_dropped() -> None:
    unknown = _token(
        TokenKind.ENTITY_VALUE,
        EntityResolution(raw_value="동안", candidates=(), selected=None),
        0,
    )
    outcome = EventIRComposer().compose(
        "", [unknown, _token(TokenKind.EVENT, EventType.PURCHASE, 5)]
    )

    assert outcome.ir is not None
    assert outcome.ir.condition.scopes == ()
    assert "동안" in outcome.unresolved_values


def test_multiple_time_windows_on_one_event_warn() -> None:
    tokens = [
        _token(TokenKind.TIME_WINDOW, RelativeTimeWindow(3, TimeUnit.MONTH), 0),
        _token(TokenKind.DATE_RANGE, AbsoluteDateRange(date(2026, 1, 1), date(2026, 2, 1)), 4),
        _token(TokenKind.EVENT, EventType.PURCHASE, 10),
    ]
    outcome = EventIRComposer().compose("", tokens)

    assert "time_window.multiple" in _codes(outcome.issues)
    assert all(
        issue.severity is IssueSeverity.WARNING
        for issue in outcome.issues
        if issue.code == "time_window.multiple"
    )


def test_assumed_aggregation_raises_a_warning() -> None:
    token = SemanticToken(
        kind=TokenKind.AGGREGATE,
        value=MetricCondition(MetricType.SUM, ComparisonOperator.GTE, 100000),
        start=0,
        end=5,
        raw_text="금액이 100000원 이상",
        confidence=0.7,
    )
    outcome = EventIRComposer().compose(
        "", [token, _token(TokenKind.EVENT, EventType.PURCHASE, 10)]
    )

    assert "metric.aggregation_assumed" in _codes(outcome.issues)
