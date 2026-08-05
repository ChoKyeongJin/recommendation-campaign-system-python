"""검증기 테스트 — '조합은 됐지만 실행하면 안 되는' IR 을 잡는다."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    IssueSeverity,
    LogicOperator,
    MetricType,
    SortDirection,
    TimeUnit,
)
from nl_event_ir.models import (
    AbsoluteDateRange,
    ConditionGroup,
    EventCondition,
    EventIR,
    MetricCondition,
    RankSpec,
    RelativeTimeWindow,
    ScopeFilter,
)
from nl_event_ir.validator import EventIRValidator


def _ir(condition: EventCondition, rank: RankSpec | None = None) -> EventIR:
    return EventIR(target_entity=EntityType.MEMBER, condition=condition, rank=rank)


def _codes(issues: list) -> set[str]:
    return {issue.code for issue in issues}


def test_valid_ir_has_no_issues() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=RelativeTimeWindow(3, TimeUnit.MONTH),
            scopes=(
                ScopeFilter(
                    entity_type=EntityType.BRAND,
                    operator=ComparisonOperator.EQ,
                    value="나이키",
                    resolved_id="brand:nike",
                    confidence=1.0,
                ),
            ),
            metric=MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GTE, 2),
        )
    )
    assert EventIRValidator().validate(ir) == []


@pytest.mark.parametrize("value", [0, -1, -30])
def test_non_positive_time_window_is_rejected(value: int) -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=RelativeTimeWindow(value, TimeUnit.DAY),
        )
    )
    assert "time_window.non_positive" in _codes(EventIRValidator().validate(ir))


def test_reversed_date_range_is_rejected() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=AbsoluteDateRange(date(2026, 8, 1), date(2026, 7, 1)),
        )
    )
    assert "date_range.reversed" in _codes(EventIRValidator().validate(ir))


def test_empty_date_range_is_rejected() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=AbsoluteDateRange(None, None),
        )
    )
    assert "date_range.empty" in _codes(EventIRValidator().validate(ir))


def test_single_sided_date_range_is_allowed() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=AbsoluteDateRange(date(2026, 7, 1), None),
        )
    )
    assert EventIRValidator().validate(ir) == []


def test_negative_metric_value_is_rejected() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            metric=MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GTE, -1),
        )
    )
    assert "metric.negative_value" in _codes(EventIRValidator().validate(ir))


def test_collection_operator_with_scalar_is_rejected() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            metric=MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.IN, 3),
        )
    )
    assert "metric.operator_value_mismatch" in _codes(EventIRValidator().validate(ir))


def test_event_count_must_be_an_integer() -> None:
    """횟수에 소수가 오면 '2.5번 구매'라는 뜻이 되어 실행 단계에서 조용히 반올림된다."""

    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            metric=MetricCondition(
                MetricType.EVENT_COUNT, ComparisonOperator.GTE, Decimal("2.5")
            ),
        )
    )
    assert "metric.non_integer_count" in _codes(EventIRValidator().validate(ir))


def test_amount_metric_may_be_decimal() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            metric=MetricCondition(
                MetricType.SUM, ComparisonOperator.GTE, Decimal("100000.50")
            ),
        )
    )
    assert EventIRValidator().validate(ir) == []


def test_unresolved_scope_is_rejected() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            scopes=(
                ScopeFilter(
                    entity_type=EntityType.BRAND,
                    operator=ComparisonOperator.EQ,
                    value="어떤브랜드",
                    resolved_id=None,
                ),
            ),
        )
    )
    issues = EventIRValidator().validate(ir)
    assert "scope.unresolved" in _codes(issues)
    assert issues[0].severity is IssueSeverity.ERROR


def test_low_confidence_scope_is_a_warning_not_an_error() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            scopes=(
                ScopeFilter(
                    entity_type=EntityType.BRAND,
                    operator=ComparisonOperator.EQ,
                    value="나이",
                    resolved_id="brand:nike",
                    confidence=0.5,
                ),
            ),
        )
    )
    issues = EventIRValidator().validate(ir)
    assert "scope.low_confidence" in _codes(issues)
    assert all(issue.severity is IssueSeverity.WARNING for issue in issues)


def test_validates_every_branch_of_a_group() -> None:
    """그룹의 한쪽만 검사하면 다른 쪽 오류가 통과한다."""

    group = ConditionGroup(
        operator=LogicOperator.OR,
        children=(
            EventCondition(event_type=EventType.PURCHASE),
            EventCondition(
                event_type=EventType.LOGIN,
                time_window=RelativeTimeWindow(0, TimeUnit.DAY),
            ),
        ),
    )
    issues = EventIRValidator().validate(
        EventIR(target_entity=EntityType.MEMBER, condition=group)
    )
    assert "time_window.non_positive" in _codes(issues)
    assert issues[0].path.startswith("condition.children[1]")


def test_issue_path_points_at_the_offending_field() -> None:
    ir = _ir(
        EventCondition(
            event_type=EventType.PURCHASE,
            time_window=RelativeTimeWindow(-5, TimeUnit.DAY),
        )
    )
    assert EventIRValidator().validate(ir)[0].path == "condition.time_window.value"


def test_non_positive_rank_limit_is_rejected() -> None:
    ir = _ir(
        EventCondition(event_type=EventType.PURCHASE),
        rank=RankSpec(limit=0, direction=SortDirection.DESC),
    )
    assert "rank.non_positive_limit" in _codes(EventIRValidator().validate(ir))


def test_empty_condition_group_is_impossible_by_construction() -> None:
    with pytest.raises(ValueError, match="at least one child"):
        ConditionGroup(operator=LogicOperator.AND, children=())


def test_not_group_requires_exactly_one_child() -> None:
    with pytest.raises(ValueError, match="exactly one child"):
        ConditionGroup(
            operator=LogicOperator.NOT,
            children=(
                EventCondition(event_type=EventType.PURCHASE),
                EventCondition(event_type=EventType.LOGIN),
            ),
        )
