from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias


Intent = Literal[
    "fact_lookup",
    "comparison",
    "aggregation",
    "summary",
    "causal_analysis",
    "timeline",
    "multi_hop",
    "recommendation",
    "unknown",
]
Complexity = Literal["simple", "moderate", "complex"]
MetadataOperator = Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "contains"]
Operation = Literal["find", "filter", "compare", "aggregate", "rank", "summarize", "explain", "build_timeline"]
OutputFormat = Literal["text", "table", "list", "json"]
MetadataValue: TypeAlias = str | int | float | bool | list[str]


@dataclass(frozen=True)
class StructuringContext:
    current_date: str
    timezone: str | None = None
    conversation_context: str | None = None


@dataclass(frozen=True)
class QueryStructuringInput:
    query: str
    context: StructuringContext


@dataclass(frozen=True)
class Subject:
    name: str
    type: str | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TimeRange:
    from_date: str | None = None
    to_date: str | None = None
    original_expression: str | None = None


@dataclass(frozen=True)
class MetadataConstraint:
    expression: str
    field: str | None = None
    operator: MetadataOperator | None = None
    value: MetadataValue | None = None


@dataclass(frozen=True)
class Constraints:
    metadata: list[MetadataConstraint]
    time_range: TimeRange | None = None


@dataclass(frozen=True)
class InformationNeed:
    id: str
    target: str
    subject_refs: list[str]
    description: str


@dataclass(frozen=True)
class Dependency:
    from_id: str
    to_id: str
    reason: str


@dataclass(frozen=True)
class OutputPreference:
    format: OutputFormat | None = None
    language: str | None = None


@dataclass(frozen=True)
class PlannerHints:
    requires_multiple_retrievals: bool
    requires_sequential_execution: bool
    requires_comparison: bool
    requires_aggregation: bool
    reason: str


@dataclass(frozen=True)
class StructuredQuery:
    original_query: str
    normalized_query: str
    intent: Intent
    complexity: Complexity
    subjects: list[Subject]
    constraints: Constraints
    information_needs: list[InformationNeed]
    operations: list[Operation]
    dependencies: list[Dependency]
    planner_hints: PlannerHints
    output_preference: OutputPreference | None = None

    def to_dict(self) -> dict[str, Any]:
        constraints: dict[str, Any] = {
            "metadata": [
                _without_none(
                    {
                        "field": constraint.field,
                        "expression": constraint.expression,
                        "operator": constraint.operator,
                        "value": constraint.value,
                    }
                )
                for constraint in self.constraints.metadata
            ]
        }
        if self.constraints.time_range is not None:
            constraints["timeRange"] = _without_none(
                {
                    "from": self.constraints.time_range.from_date,
                    "to": self.constraints.time_range.to_date,
                    "originalExpression": self.constraints.time_range.original_expression,
                }
            )

        result: dict[str, Any] = {
            "originalQuery": self.original_query,
            "normalizedQuery": self.normalized_query,
            "intent": self.intent,
            "complexity": self.complexity,
            "subjects": [
                _without_none({"name": subject.name, "type": subject.type, "aliases": subject.aliases or None})
                for subject in self.subjects
            ],
            "constraints": constraints,
            "informationNeeds": [
                {
                    "id": need.id,
                    "target": need.target,
                    "subjectRefs": need.subject_refs,
                    "description": need.description,
                }
                for need in self.information_needs
            ],
            "operations": self.operations,
            "dependencies": [
                {"from": dependency.from_id, "to": dependency.to_id, "reason": dependency.reason}
                for dependency in self.dependencies
            ],
            "plannerHints": {
                "requiresMultipleRetrievals": self.planner_hints.requires_multiple_retrievals,
                "requiresSequentialExecution": self.planner_hints.requires_sequential_execution,
                "requiresComparison": self.planner_hints.requires_comparison,
                "requiresAggregation": self.planner_hints.requires_aggregation,
                "reason": self.planner_hints.reason,
            },
        }
        if self.output_preference is not None:
            result["outputPreference"] = _without_none(
                {"format": self.output_preference.format, "language": self.output_preference.language}
            )
        return result


class QueryStructurer(Protocol):
    def structure(self, input: QueryStructuringInput) -> StructuredQuery:
        """Converts one user question and its supplied context into a StructuredQuery."""


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}