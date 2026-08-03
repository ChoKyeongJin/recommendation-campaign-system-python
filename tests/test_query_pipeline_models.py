"""모델 계약 — 요구는 미확정을 담고, 사양은 담지 못한다."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AggregateConditionExpression,
    AggregateDefinition,
    AggregateFunction,
    AndExpression,
    AttributeOperand,
    ComparisonExpression,
    ComparisonOperator,
    EntityRelation,
    LiteralOperand,
    canonicalize_expression,
)
from query_pipeline.event_query.models import (
    CapabilityRequirements,
    EventQuerySpec,
    EventQuerySpecError,
    QueryOutput,
    QuerySource,
    ResolvedBinding,
)
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
    IntentKind,
    ProposedExpression,
    RequirementConstraint,
    RequirementIntent,
    RequirementOperator,
    RequirementReference,
    RequirementSource,
)
from query_pipeline.requirement.parser import InvalidLlmOutput, validate_llm_output

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _requirement(**overrides: object) -> AudienceRequirement:
    payload: dict[str, object] = {
        "id": "req-1",
        "version": "1",
        "intent": RequirementIntent(kind=IntentKind.FIND),
        "source": RequirementSource(text="지난달 결제를 많이 실패한 고객을 보여줘"),
        "created_at": NOW,
    }
    payload.update(overrides)
    return AudienceRequirement.model_validate(payload)


def _spec(expression: object, bindings: dict[str, ResolvedBinding]) -> EventQuerySpec:
    return EventQuerySpec(
        id="spec-1",
        version="1",
        expression=expression,  # type: ignore[arg-type]
        output=QueryOutput(entity="payment_events"),
        bindings=bindings,
        source=QuerySource(requirement_id="req-1", requirement_version="1"),
        capabilities=CapabilityRequirements(),
        created_at=NOW,
    )


def test_audience_requirement_allows_ambiguous_value() -> None:
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
                    kind=ResolutionKind.USE_DEFAULT, default_value=5
                ),
            ),
        ),
    )
    value = requirement.constraints[0].value
    assert value.state == "ambiguous"
    assert value.candidates == (3, 5, 10)
    # 해소 수단이 선언된 ERROR 는 실행을 막지 않는다 — 되묻기와 기본값 적용은 다른 결말이다.
    assert requirement.blocking_issues == ()


def test_requirement_rejects_two_interpretations() -> None:
    with pytest.raises(ValidationError):
        _requirement(
            expression=ProposedExpression(payload={"type": "exists"}),
            constraints=(
                RequirementConstraint(
                    id="c1",
                    field=RequirementReference(name="event_type"),
                    operator=RequirementOperator.EQ,
                    value=AmbiguousRequirementValue(candidates=("a",)),
                ),
            ),
        )


def test_event_query_spec_rejects_unresolved_placeholder() -> None:
    attribute = AttributeOperand(entity="payment_events", attribute="event_type")
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=attribute,
        right=LiteralOperand(value="payment.failed"),
    )
    with pytest.raises(EventQuerySpecError, match="해결되지 않은 속성 참조"):
        _spec(expression, {})


def test_event_query_spec_rejects_shorthand_aggregate_condition() -> None:
    expression = AggregateConditionExpression(
        aggregate=AggregateDefinition(
            function=AggregateFunction.COUNT,
            relation=EntityRelation(entity="payment_events"),
        ),
        operator=ComparisonOperator.GTE,
        value=LiteralOperand(value=5),
    )
    with pytest.raises(EventQuerySpecError, match="aggregate_condition"):
        _spec(expression, {})

    # 정본으로 접으면 같은 뜻이 통과한다(축약 표기는 사양 밖에서만 존재한다).
    folded = canonicalize_expression(expression)
    assert isinstance(folded, ComparisonExpression)
    assert _spec(folded, {}).expression is folded


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AudienceRequirement.model_validate(
            {
                "id": "req-1",
                "version": "1",
                "intent": {"kind": "find"},
                "source": {"text": "x"},
                "created_at": NOW,
                "unexpected": 1,
            }
        )
    with pytest.raises(ValidationError):
        AttributeOperand.model_validate(
            {"entity": "payment_events", "attribute": "amount", "column": "AMT"}
        )


def test_invalid_llm_output_returns_structured_error() -> None:
    result = validate_llm_output("{not json")
    assert isinstance(result, InvalidLlmOutput)
    assert result.status == "invalid_output"
    assert result.issues[0].kind is IssueKind.INVALID
    assert result.issues[0].severity is IssueSeverity.ERROR


def test_valid_llm_output_returns_requirement() -> None:
    result = validate_llm_output(
        {
            "id": "req-1",
            "version": "1",
            "intent": {"kind": "find"},
            "source": {"text": "여성 회원을 찾아줘"},
            "created_at": NOW.isoformat(),
        }
    )
    assert result.status == "success"
    assert result.requirement.intent.kind is IntentKind.FIND


def test_models_are_frozen() -> None:
    operand = AttributeOperand(entity="payment_events", attribute="amount")
    with pytest.raises(ValidationError):
        operand.entity = "orders"  # type: ignore[misc]


def test_absolute_window_rejects_empty_interval() -> None:
    with pytest.raises(ValidationError):
        AbsoluteWindow(start=NOW.date(), end_exclusive=NOW.date())


def test_and_expression_requires_operands() -> None:
    with pytest.raises(ValidationError):
        AndExpression(expressions=())
