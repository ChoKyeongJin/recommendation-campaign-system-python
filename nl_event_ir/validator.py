"""Event IR 검증 — 조합은 됐지만 **실행하면 안 되는** IR 을 걸러 낸다.

composer 가 보는 것은 '토큰을 붙일 수 있는가'이고, 검증기가 보는 것은 '붙인 결과가 말이
되는가'다. 두 관심사를 한 곳에 두면 조합 규칙과 업무 규칙이 섞여, 조합을 고칠 때마다 업무
규칙이 함께 흔들린다.

결과는 문자열이 아니라 :class:`~nl_event_ir.models.ValidationIssue` 다. 호출부가 ``code``
로 분기할 수 있어야 '경고는 통과시키고 오류만 막는' 정책을 밖에서 정할 수 있다.
"""

from __future__ import annotations

from decimal import Decimal

from nl_event_ir.enums import ComparisonOperator, IssueSeverity, MetricType
from nl_event_ir.models import (
    AbsoluteDateRange,
    ConditionGroup,
    ConditionNode,
    EventCondition,
    EventIR,
    RelativeTimeWindow,
    ValidationIssue,
)

__all__ = ["EventIRValidator"]

# 이 값 미만으로 접지된 엔티티는 사람이 확인해야 한다고 본다.
_DEFAULT_MINIMUM_SCOPE_CONFIDENCE = 0.8

# 집합 연산자. 스칼라 값과 함께 쓰일 수 없다.
_COLLECTION_OPERATORS = frozenset({ComparisonOperator.IN, ComparisonOperator.NOT_IN})


class EventIRValidator:
    """IR 트리를 훑으며 구조적·의미적 문제를 모은다."""

    def __init__(
        self,
        *,
        minimum_scope_confidence: float = _DEFAULT_MINIMUM_SCOPE_CONFIDENCE,
    ) -> None:
        self._minimum_scope_confidence = minimum_scope_confidence

    def validate(self, ir: EventIR) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        self._validate_node(ir.condition, "condition", issues)
        if ir.rank is not None and ir.rank.limit <= 0:
            issues.append(
                ValidationIssue(
                    code="rank.non_positive_limit",
                    message=f"순위 개수는 1 이상이어야 합니다. 받은 값: {ir.rank.limit}.",
                    path="rank.limit",
                    severity=IssueSeverity.ERROR,
                )
            )
        return issues

    # ── 트리 순회 ────────────────────────────────────────────────────────────────

    def _validate_node(
        self,
        node: ConditionNode,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        if isinstance(node, ConditionGroup):
            for index, child in enumerate(node.children):
                self._validate_node(child, f"{path}.children[{index}]", issues)
            return
        self._validate_condition(node, path, issues)

    def _validate_condition(
        self,
        condition: EventCondition,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        self._validate_time_window(condition, path, issues)
        self._validate_metric(condition, path, issues)
        self._validate_scopes(condition, path, issues)

    # ── 개별 규칙 ────────────────────────────────────────────────────────────────

    def _validate_time_window(
        self,
        condition: EventCondition,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        window = condition.time_window
        if window is None:
            return

        if isinstance(window, RelativeTimeWindow):
            if window.value <= 0:
                issues.append(
                    ValidationIssue(
                        code="time_window.non_positive",
                        message=(
                            f"기간은 1 이상이어야 합니다. 받은 값: {window.value}{window.unit.value}."
                        ),
                        path=f"{path}.time_window.value",
                        severity=IssueSeverity.ERROR,
                    )
                )
            return

        if isinstance(window, AbsoluteDateRange):
            if window.start is None and window.end is None:
                issues.append(
                    ValidationIssue(
                        code="date_range.empty",
                        message="날짜 구간의 시작과 끝이 모두 비어 있습니다.",
                        path=f"{path}.time_window",
                        severity=IssueSeverity.ERROR,
                    )
                )
                return
            if (
                window.start is not None
                and window.end is not None
                and window.start > window.end
            ):
                issues.append(
                    ValidationIssue(
                        code="date_range.reversed",
                        message=(
                            f"날짜 구간의 시작({window.start.isoformat()})이 "
                            f"끝({window.end.isoformat()})보다 뒤입니다."
                        ),
                        path=f"{path}.time_window",
                        severity=IssueSeverity.ERROR,
                    )
                )

    def _validate_metric(
        self,
        condition: EventCondition,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        metric = condition.metric
        if metric is None:
            return

        if metric.operator in _COLLECTION_OPERATORS:
            issues.append(
                ValidationIssue(
                    code="metric.operator_value_mismatch",
                    message=(
                        f"집합 연산자 '{metric.operator.value}' 는 스칼라 값 "
                        f"{metric.value!r} 와 함께 쓸 수 없습니다."
                    ),
                    path=f"{path}.metric.operator",
                    severity=IssueSeverity.ERROR,
                )
            )

        if metric.value < 0:
            issues.append(
                ValidationIssue(
                    code="metric.negative_value",
                    message=f"집계 임계값은 0 이상이어야 합니다. 받은 값: {metric.value}.",
                    path=f"{path}.metric.value",
                    severity=IssueSeverity.ERROR,
                )
            )

        if metric.metric_type is MetricType.EVENT_COUNT and isinstance(metric.value, Decimal):
            issues.append(
                ValidationIssue(
                    code="metric.non_integer_count",
                    message=(
                        f"횟수는 정수여야 합니다. 받은 값: {metric.value} "
                        f"({type(metric.value).__name__})."
                    ),
                    path=f"{path}.metric.value",
                    severity=IssueSeverity.ERROR,
                )
            )

    def _validate_scopes(
        self,
        condition: EventCondition,
        path: str,
        issues: list[ValidationIssue],
    ) -> None:
        for index, scope in enumerate(condition.scopes):
            scope_path = f"{path}.scopes[{index}]"
            if scope.resolved_id is None:
                issues.append(
                    ValidationIssue(
                        code="scope.unresolved",
                        message=(
                            f"'{scope.value}' 가 실제 데이터에 접지되지 않았습니다. "
                            "접지되지 않은 값으로는 조회를 만들 수 없습니다."
                        ),
                        path=scope_path,
                        severity=IssueSeverity.ERROR,
                    )
                )
                continue
            if (
                scope.confidence is not None
                and scope.confidence < self._minimum_scope_confidence
            ):
                issues.append(
                    ValidationIssue(
                        code="scope.low_confidence",
                        message=(
                            f"'{scope.value}' → {scope.resolved_id} 접지 신뢰도가 "
                            f"{scope.confidence:.2f} 로 기준({self._minimum_scope_confidence:.2f})"
                            " 미만입니다."
                        ),
                        path=scope_path,
                        severity=IssueSeverity.WARNING,
                    )
                )
