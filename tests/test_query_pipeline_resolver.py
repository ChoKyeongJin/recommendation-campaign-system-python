"""RequirementResolver 계약 — 미해결이면 사양이 만들어지지 않는다."""

from __future__ import annotations

from datetime import UTC, date, datetime

from query_pipeline_fixtures import clock, id_factory, resolution_context, run

from query_pipeline.compiler.capability import DeclaredCapabilityProfile
from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AndExpression,
    ComparisonExpression,
    RollingWindow,
    TimeWindowExpression,
    WindowUnit,
)
from query_pipeline.event_query.receipts import ReceiptAction
from query_pipeline.requirement.issues import (
    IssueKind,
    IssueResolution,
    IssueSeverity,
    RequirementIssue,
    ResolutionKind,
)
from query_pipeline.requirement.models import (
    AmbiguousRequirementValue,
    AudienceRequirement,
    InferredRequirementValue,
    IntentKind,
    MissingRequirementValue,
    RequirementConstraint,
    RequirementContext,
    RequirementIntent,
    RequirementOperator,
    RequirementReference,
    RequirementSource,
    ResolvedRequirementValue,
)
from query_pipeline.requirement.resolver import (
    DefaultRequirementResolver,
    resolve_period_phrase,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
QUERY = "지난달 결제를 많이 실패한 고객을 보여줘"


def _resolver() -> DefaultRequirementResolver:
    return DefaultRequirementResolver(clock=clock(), id_factory=id_factory())


def _requirement(
    *,
    constraints: tuple[RequirementConstraint, ...] = (),
    issues: tuple[RequirementIssue, ...] = (),
) -> AudienceRequirement:
    return AudienceRequirement(
        id="req-1",
        version="1",
        intent=RequirementIntent(
            kind=IntentKind.FIND, target=RequirementReference(name="customer")
        ),
        constraints=constraints,
        issues=issues,
        context=RequirementContext(timezone="Asia/Seoul", locale="ko-KR"),
        source=RequirementSource(text=QUERY),
        created_at=NOW,
    )


def _event_type_constraint() -> RequirementConstraint:
    return RequirementConstraint(
        id="constraint-event",
        field=RequirementReference(name="event_type"),
        operator=RequirementOperator.EQ,
        value=ResolvedRequirementValue(value="payment.failed"),
    )


def _last_month_constraint() -> RequirementConstraint:
    return RequirementConstraint(
        id="constraint-time",
        field=RequirementReference(name="occurred_at"),
        operator=RequirementOperator.BETWEEN,
        value=InferredRequirementValue(value="지난달", confidence=0.95),
    )


def test_unresolved_requirement_does_not_create_spec() -> None:
    requirement = _requirement(
        constraints=(
            RequirementConstraint(
                id="constraint-count",
                field=RequirementReference(name="failure_count"),
                operator=RequirementOperator.GTE,
                value=AmbiguousRequirementValue(candidates=(3, 5, 10)),
            ),
        )
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "unresolved"
    assert not hasattr(result, "spec")
    assert result.issues[0].kind is IssueKind.AMBIGUOUS
    assert result.issues[0].candidates == (3, 5, 10)


def test_error_issue_returns_unresolved_result() -> None:
    requirement = _requirement(
        constraints=(_event_type_constraint(),),
        issues=(
            RequirementIssue(
                id="policy",
                kind=IssueKind.POLICY_DENIED,
                severity=IssueSeverity.ERROR,
                path="$",
                message="이 오디언스는 정책상 조회할 수 없습니다.",
            ),
        ),
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "unresolved"
    assert [issue.kind for issue in result.issues] == [IssueKind.POLICY_DENIED]


def test_resolved_requirement_returns_ready_spec() -> None:
    requirement = _requirement(constraints=(_event_type_constraint(),))
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "ready"
    spec = result.spec
    assert isinstance(spec.expression, ComparisonExpression)
    assert spec.source.requirement_id == "req-1"
    assert spec.bindings["payment_events.event_type"].entity == "payment_events"
    assert "operator.eq" in spec.capabilities.required


def test_last_month_is_converted_using_context_timezone() -> None:
    requirement = _requirement(
        constraints=(_event_type_constraint(), _last_month_constraint())
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "ready"
    expression = result.spec.expression
    assert isinstance(expression, AndExpression)
    window_node = next(
        node
        for node in expression.expressions
        if isinstance(node, TimeWindowExpression)
    )
    window = window_node.window
    # KST 기준 2026-08-03 의 '지난달' = 2026-07 전체(반개구간).
    assert window.start == date(2026, 7, 1)
    assert window.end_exclusive == date(2026, 8, 1)
    normalized = [
        receipt
        for receipt in result.spec.receipts
        if receipt.action is ReceiptAction.NORMALIZED_VALUE
    ]
    assert normalized and "Asia/Seoul" in normalized[0].reason
    assert any(item.value == "지난달" for item in result.spec.assumptions)


def _period_constraint(phrase: str) -> RequirementConstraint:
    return RequirementConstraint(
        id="constraint-time",
        field=RequirementReference(name="occurred_at"),
        operator=RequirementOperator.BETWEEN,
        value=InferredRequirementValue(value=phrase, confidence=0.9),
    )


def _window(phrase: str) -> object:
    result = run(
        _resolver().resolve(
            _requirement(constraints=(_event_type_constraint(), _period_constraint(phrase))),
            resolution_context(),
        )
    )
    assert result.status == "ready", getattr(result, "issues", None)
    expression = result.spec.expression
    assert isinstance(expression, AndExpression)
    return next(
        node.window
        for node in expression.expressions
        if isinstance(node, TimeWindowExpression)
    )


def test_rolling_period_is_not_folded_into_plan_time_dates() -> None:
    """'최근 N일'은 **길이**다. 계획 시점 날짜로 접으면 그 창이 그 날로 고정된다.

    `event_compiler` 가 명문화한 규칙(롤링 경계는 실행 시점 함수로 렌더한다)을 요구 계층에서
    먼저 지킨다 — 여기서 접히면 아래 계층은 접힌 사실조차 볼 수 없다.
    """
    window = _window("최근 30일")
    assert isinstance(window, RollingWindow)
    assert (window.value, window.unit) == (30, WindowUnit.DAY)


def test_the_same_rolling_phrase_resolves_identically_on_any_day() -> None:
    resolutions = {
        resolve_period_phrase(
            "최근 30일", now=datetime(year, month, 3, tzinfo=UTC), timezone="Asia/Seoul"
        ).window  # type: ignore[union-attr]
        for year, month in ((2025, 1), (2026, 8), (2026, 12))
    }
    assert len(resolutions) == 1


def test_calendar_owner_opens_expressions_the_local_table_never_had() -> None:
    """표면 문법을 위임한 이득 — 소유자가 아는 표현이 전부 열린다."""
    assert _window("2025년 3월") == AbsoluteWindow(
        start=date(2025, 3, 1), end_exclusive=date(2025, 4, 1)
    )
    assert _window("작년") == AbsoluteWindow(
        start=date(2025, 1, 1), end_exclusive=date(2026, 1, 1)
    )
    assert _window("올해 상반기") == AbsoluteWindow(
        start=date(2026, 1, 1), end_exclusive=date(2026, 7, 1)
    )
    assert _window("일주일") == RollingWindow(value=7, unit=WindowUnit.DAY)


def test_past_point_phrase_becomes_the_calendar_cell_it_names() -> None:
    """'3개월 전'은 길이가 아니라 **시점**이다 — 그 달 전체이지 최근 90일이 아니다.

    두 뜻을 같은 창으로 만들면 값·근거·SQL 가드가 전부 통과한 채 다른 오디언스가 나온다.
    """
    assert _window("3개월 전") == AbsoluteWindow(
        start=date(2026, 5, 1), end_exclusive=date(2026, 6, 1)
    )


def test_period_normalization_only_applies_to_range_operators() -> None:
    """범위가 아닌 자리의 문자열은 창이 되지 않는다.

    표면 문법 소유자는 문장을 **훑는** 스캐너다. 아무 문자열에나 걸면 '2025년 신년 프로모션'
    같은 값이 조용히 시간 창이 되고, 그것은 오류가 아니라 다른 집합으로 나온다.
    """
    requirement = _requirement(
        constraints=(
            RequirementConstraint(
                id="constraint-event",
                field=RequirementReference(name="event_type"),
                operator=RequirementOperator.EQ,
                value=ResolvedRequirementValue(value="2025년 신년 프로모션"),
            ),
        )
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "ready"
    expression = result.spec.expression
    assert isinstance(expression, ComparisonExpression)
    assert expression.right.value == "2025년 신년 프로모션"  # type: ignore[union-attr]


def test_unreadable_period_is_reported_instead_of_guessed() -> None:
    requirement = _requirement(
        constraints=(_event_type_constraint(), _period_constraint("언젠가")),
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "unresolved"
    assert result.issues[0].id == "unresolved-period:constraint-time"


def test_default_value_creates_resolution_receipt() -> None:
    requirement = _requirement(
        constraints=(
            _event_type_constraint(),
            RequirementConstraint(
                id="constraint-count",
                field=RequirementReference(name="failure_count"),
                operator=RequirementOperator.GTE,
                value=AmbiguousRequirementValue(candidates=(3, 5, 10)),
            ),
        ),
        issues=(
            RequirementIssue(
                id="issue-count",
                kind=IssueKind.AMBIGUOUS,
                severity=IssueSeverity.ERROR,
                path="$.constraints[1].value",
                message="'많이'의 기준이 명확하지 않습니다.",
                candidates=(3, 5, 10),
                resolution=IssueResolution(
                    kind=ResolutionKind.USE_DEFAULT, default_value=5
                ),
            ),
        ),
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "ready"
    applied = [
        receipt
        for receipt in result.spec.receipts
        if receipt.action is ReceiptAction.APPLIED_DEFAULT
    ]
    assert applied and applied[0].after == 5
    assert [item.value for item in result.spec.assumptions] == [5]


def test_selected_candidate_must_be_one_of_the_candidates() -> None:
    requirement = _requirement(
        constraints=(
            RequirementConstraint(
                id="constraint-count",
                field=RequirementReference(name="failure_count"),
                operator=RequirementOperator.GTE,
                value=AmbiguousRequirementValue(candidates=(3, 5, 10)),
            ),
        ),
        issues=(
            RequirementIssue(
                id="issue-count",
                kind=IssueKind.AMBIGUOUS,
                severity=IssueSeverity.ERROR,
                path="$.constraints[0].value",
                message="'많이'의 기준이 명확하지 않습니다.",
                candidates=(3, 5, 10),
                resolution=IssueResolution(
                    kind=ResolutionKind.SELECT_CANDIDATE, default_value=7
                ),
            ),
        ),
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "unresolved"
    assert result.issues[0].id == "invalid-candidate:constraint-count"


def test_missing_value_uses_policy_default() -> None:
    requirement = _requirement(
        constraints=(
            RequirementConstraint(
                id="constraint-count",
                field=RequirementReference(name="failure_count"),
                operator=RequirementOperator.GTE,
                value=MissingRequirementValue(expected_type="integer"),
            ),
        )
    )
    without_policy = run(_resolver().resolve(requirement, resolution_context()))
    assert without_policy.status == "unresolved"
    assert without_policy.issues[0].kind is IssueKind.MISSING

    with_policy = run(
        _resolver().resolve(
            requirement,
            resolution_context(defaults={"$.constraints[0].value": 3}),
        )
    )
    assert with_policy.status == "ready"
    assert [item.value for item in with_policy.spec.assumptions] == [3]


def test_unsupported_capability_returns_issue() -> None:
    requirement = _requirement(constraints=(_event_type_constraint(),))
    result = run(
        _resolver().resolve(
            requirement,
            resolution_context(capability_profile=DeclaredCapabilityProfile(())),
        )
    )
    assert result.status == "unresolved"
    assert {issue.kind for issue in result.issues} == {IssueKind.UNSUPPORTED}
    assert any("operator.eq" in issue.message for issue in result.issues)


def test_unknown_field_is_reported_as_schema_mismatch() -> None:
    requirement = _requirement(
        constraints=(
            RequirementConstraint(
                id="constraint-unknown",
                field=RequirementReference(name="does_not_exist"),
                operator=RequirementOperator.EQ,
                value=ResolvedRequirementValue(value=1),
            ),
        )
    )
    result = run(_resolver().resolve(requirement, resolution_context()))
    assert result.status == "unresolved"
    assert result.issues[0].kind is IssueKind.SCHEMA_MISMATCH


def test_requirement_without_any_condition_is_unresolved() -> None:
    result = run(_resolver().resolve(_requirement(), resolution_context()))
    assert result.status == "unresolved"
    assert result.issues[0].id == "missing-audience-expression"
