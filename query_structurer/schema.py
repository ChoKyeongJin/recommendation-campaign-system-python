from __future__ import annotations

from datetime import date
from typing import Any, get_args

from .types import (
    Complexity as ComplexityType,
    Constraints,
    Dependency,
    InformationNeed,
    Intent as IntentType,
    MetadataConstraint,
    MetadataOperator as MetadataOperatorType,
    Operation as OperationType,
    OutputPreference,
    OutputFormat as OutputFormatType,
    PlannerHints,
    StructuredQuery,
    Subject,
    TimeRange,
)


def _literal_values(alias: Any) -> set[str]:
    values = get_args(alias)
    if not values or not all(isinstance(value, str) for value in values):
        raise TypeError(f"expected a string Literal alias, got {alias!r}")
    return set(values)


# 런타임 validator와 JSON Schema가 공개 타입 계약에서 파생된다. 값을 추가할 곳은 types.py의
# Literal 선언 하나뿐이며, 이 모듈에 같은 문자열 목록을 다시 적지 않는다.
INTENTS = _literal_values(IntentType)
COMPLEXITIES = _literal_values(ComplexityType)
METADATA_OPERATORS = _literal_values(MetadataOperatorType)
OPERATIONS = _literal_values(OperationType)
OUTPUT_FORMATS = _literal_values(OutputFormatType)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict-output representation of an optional value.

    OpenAI strict structured outputs require every object property to be listed
    in ``required``.  Optional application values therefore stay present in the
    wire payload and use JSON ``null`` when they are unknown.
    """

    return {"anyOf": [schema, {"type": "null"}]}


_SUBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "type", "aliases"],
    "properties": {
        "name": {"type": "string"},
        "type": _nullable({"type": "string"}),
        "aliases": {"type": "array", "items": {"type": "string"}},
    },
}

_TIME_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["from", "to", "originalExpression"],
    "properties": {
        "from": _nullable({"type": "string"}),
        "to": _nullable({"type": "string"}),
        "originalExpression": _nullable({"type": "string"}),
    },
}

_METADATA_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
    ]
}

_METADATA_CONSTRAINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["expression", "field", "operator", "value"],
    "properties": {
        "expression": {"type": "string"},
        "field": _nullable({"type": "string"}),
        "operator": _nullable({"type": "string", "enum": sorted(METADATA_OPERATORS)}),
        "value": _METADATA_VALUE_SCHEMA,
    },
}

_CONSTRAINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["timeRange", "metadata"],
    "properties": {
        "timeRange": _nullable(_TIME_RANGE_SCHEMA),
        "metadata": {"type": "array", "items": _METADATA_CONSTRAINT_SCHEMA},
    },
}

_INFORMATION_NEED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "target", "subjectRefs", "description"],
    "properties": {
        "id": {"type": "string"},
        "target": {"type": "string"},
        "subjectRefs": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
    },
}

_DEPENDENCY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["from", "to", "reason"],
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "reason": {"type": "string"},
    },
}

_OUTPUT_PREFERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["format", "language"],
    "properties": {
        "format": _nullable({"type": "string", "enum": sorted(OUTPUT_FORMATS)}),
        "language": _nullable({"type": "string"}),
    },
}

_PLANNER_HINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "requiresMultipleRetrievals",
        "requiresSequentialExecution",
        "requiresComparison",
        "requiresAggregation",
        "reason",
    ],
    "properties": {
        "requiresMultipleRetrievals": {"type": "boolean"},
        "requiresSequentialExecution": {"type": "boolean"},
        "requiresComparison": {"type": "boolean"},
        "requiresAggregation": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

STRUCTURED_QUERY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "originalQuery",
        "normalizedQuery",
        "intent",
        "complexity",
        "subjects",
        "constraints",
        "informationNeeds",
        "operations",
        "dependencies",
        "outputPreference",
        "plannerHints",
    ],
    "properties": {
        "originalQuery": {"type": "string"},
        "normalizedQuery": {"type": "string"},
        "intent": {"type": "string", "enum": sorted(INTENTS)},
        "complexity": {"type": "string", "enum": sorted(COMPLEXITIES)},
        "subjects": {"type": "array", "items": _SUBJECT_SCHEMA},
        "constraints": _CONSTRAINTS_SCHEMA,
        "informationNeeds": {"type": "array", "items": _INFORMATION_NEED_SCHEMA},
        "operations": {"type": "array", "items": {"type": "string", "enum": sorted(OPERATIONS)}},
        "dependencies": {"type": "array", "items": _DEPENDENCY_SCHEMA},
        "outputPreference": _nullable(_OUTPUT_PREFERENCE_SCHEMA),
        "plannerHints": _PLANNER_HINTS_SCHEMA,
    },
}

STRUCTURED_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_structured_query",
        "description": "사용자 질문의 의미와 정보 요구를 검증 가능한 StructuredQuery로 제출한다.",
        "strict": True,
        "parameters": STRUCTURED_QUERY_JSON_SCHEMA,
    },
}


class StructuredQueryValidationError(ValueError):
    pass


def validate_structured_query(payload: Any, *, query: str) -> StructuredQuery:
    root = _object(payload, "root")
    _keys(
        root,
        "root",
        required={
            "originalQuery",
            "normalizedQuery",
            "intent",
            "complexity",
            "subjects",
            "constraints",
            "informationNeeds",
            "operations",
            "dependencies",
            "plannerHints",
        },
        optional={"outputPreference"},
    )

    original_query = _string(root["originalQuery"], "originalQuery")
    if original_query != query:
        raise StructuredQueryValidationError("originalQuery must exactly match the input query")
    normalized_query = _string(root["normalizedQuery"], "normalizedQuery")
    intent = _enum(root["intent"], "intent", INTENTS)
    complexity = _enum(root["complexity"], "complexity", COMPLEXITIES)
    subjects = [_subject(value, index) for index, value in enumerate(_array(root["subjects"], "subjects"))]
    constraints = _constraints(root["constraints"])
    information_needs = [_information_need(value, index) for index, value in enumerate(_array(root["informationNeeds"], "informationNeeds"))]
    if not information_needs:
        raise StructuredQueryValidationError("informationNeeds must contain at least one item")
    _validate_unique_ids(information_needs)
    operations = [_enum(value, f"operations[{index}]", OPERATIONS) for index, value in enumerate(_array(root["operations"], "operations"))]
    if not operations:
        raise StructuredQueryValidationError("operations must contain at least one item")
    dependencies = [_dependency(value, index) for index, value in enumerate(_array(root["dependencies"], "dependencies"))]
    _validate_dependencies(dependencies, information_needs)
    output_preference = _output_preference(root.get("outputPreference"))
    planner_hints = _planner_hints(root["plannerHints"])

    return StructuredQuery(
        original_query=original_query,
        normalized_query=normalized_query,
        intent=intent,
        complexity=complexity,
        subjects=subjects,
        constraints=constraints,
        information_needs=information_needs,
        operations=operations,
        dependencies=dependencies,
        output_preference=output_preference,
        planner_hints=planner_hints,
    )


def build_fallback(query: str) -> StructuredQuery:
    return StructuredQuery(
        original_query=query,
        normalized_query=query,
        intent="unknown",
        complexity="simple",
        subjects=[],
        constraints=Constraints(metadata=[]),
        information_needs=[InformationNeed(id="need_1", target="answer", subject_refs=[], description=query)],
        operations=["find"],
        dependencies=[],
        planner_hints=PlannerHints(
            requires_multiple_retrievals=False,
            requires_sequential_execution=False,
            requires_comparison=False,
            requires_aggregation=False,
            reason="질문 구조화에 실패하여 원본 질문을 그대로 전달한다.",
        ),
    )


def _subject(value: Any, index: int) -> Subject:
    data = _object(value, f"subjects[{index}]")
    _keys(data, f"subjects[{index}]", required={"name"}, optional={"type", "aliases"})
    aliases = [_string(item, f"subjects[{index}].aliases[{alias_index}]") for alias_index, item in enumerate(_array(data.get("aliases", []), f"subjects[{index}].aliases"))]
    return Subject(name=_string(data["name"], f"subjects[{index}].name"), type=_optional_string(data.get("type"), f"subjects[{index}].type"), aliases=aliases)


def _constraints(value: Any) -> Constraints:
    data = _object(value, "constraints")
    _keys(data, "constraints", required={"metadata"}, optional={"timeRange"})
    metadata = [_metadata_constraint(item, index) for index, item in enumerate(_array(data["metadata"], "constraints.metadata"))]
    time_range = _time_range(data["timeRange"]) if data.get("timeRange") is not None else None
    return Constraints(metadata=metadata, time_range=time_range)


def _time_range(value: Any) -> TimeRange:
    data = _object(value, "constraints.timeRange")
    _keys(data, "constraints.timeRange", required=set(), optional={"from", "to", "originalExpression"})
    from_date = _optional_date(data.get("from"), "constraints.timeRange.from")
    to_date = _optional_date(data.get("to"), "constraints.timeRange.to")
    if from_date and to_date and from_date > to_date:
        raise StructuredQueryValidationError("constraints.timeRange.from cannot be after to")
    return TimeRange(
        from_date=from_date,
        to_date=to_date,
        original_expression=_optional_string(data.get("originalExpression"), "constraints.timeRange.originalExpression"),
    )


def _metadata_constraint(value: Any, index: int) -> MetadataConstraint:
    data = _object(value, f"constraints.metadata[{index}]")
    _keys(data, f"constraints.metadata[{index}]", required={"expression"}, optional={"field", "operator", "value"})
    metadata_value = data.get("value")
    if metadata_value is not None and not _valid_metadata_value(metadata_value):
        raise StructuredQueryValidationError(f"constraints.metadata[{index}].value has an unsupported type")
    operator = _optional_enum(data.get("operator"), f"constraints.metadata[{index}].operator", METADATA_OPERATORS)
    return MetadataConstraint(
        expression=_string(data["expression"], f"constraints.metadata[{index}].expression"),
        field=_optional_string(data.get("field"), f"constraints.metadata[{index}].field"),
        operator=operator,
        value=metadata_value,
    )


def _information_need(value: Any, index: int) -> InformationNeed:
    data = _object(value, f"informationNeeds[{index}]")
    _keys(data, f"informationNeeds[{index}]", required={"id", "target", "subjectRefs", "description"}, optional=set())
    return InformationNeed(
        id=_string(data["id"], f"informationNeeds[{index}].id"),
        target=_string(data["target"], f"informationNeeds[{index}].target"),
        subject_refs=[_string(item, f"informationNeeds[{index}].subjectRefs[{ref_index}]") for ref_index, item in enumerate(_array(data["subjectRefs"], f"informationNeeds[{index}].subjectRefs"))],
        description=_string(data["description"], f"informationNeeds[{index}].description"),
    )


def _dependency(value: Any, index: int) -> Dependency:
    data = _object(value, f"dependencies[{index}]")
    _keys(data, f"dependencies[{index}]", required={"from", "to", "reason"}, optional=set())
    return Dependency(
        from_id=_string(data["from"], f"dependencies[{index}].from"),
        to_id=_string(data["to"], f"dependencies[{index}].to"),
        reason=_string(data["reason"], f"dependencies[{index}].reason"),
    )


def _output_preference(value: Any) -> OutputPreference | None:
    if value is None:
        return None
    data = _object(value, "outputPreference")
    _keys(data, "outputPreference", required=set(), optional={"format", "language"})
    return OutputPreference(
        format=_optional_enum(data.get("format"), "outputPreference.format", OUTPUT_FORMATS),
        language=_optional_string(data.get("language"), "outputPreference.language"),
    )


def _planner_hints(value: Any) -> PlannerHints:
    data = _object(value, "plannerHints")
    _keys(
        data,
        "plannerHints",
        required={
            "requiresMultipleRetrievals",
            "requiresSequentialExecution",
            "requiresComparison",
            "requiresAggregation",
            "reason",
        },
        optional=set(),
    )
    return PlannerHints(
        requires_multiple_retrievals=_boolean(data["requiresMultipleRetrievals"], "plannerHints.requiresMultipleRetrievals"),
        requires_sequential_execution=_boolean(data["requiresSequentialExecution"], "plannerHints.requiresSequentialExecution"),
        requires_comparison=_boolean(data["requiresComparison"], "plannerHints.requiresComparison"),
        requires_aggregation=_boolean(data["requiresAggregation"], "plannerHints.requiresAggregation"),
        reason=_string(data["reason"], "plannerHints.reason"),
    )


def _validate_unique_ids(information_needs: list[InformationNeed]) -> None:
    ids = [need.id for need in information_needs]
    if len(ids) != len(set(ids)):
        raise StructuredQueryValidationError("informationNeeds ids must be unique")


def _validate_dependencies(dependencies: list[Dependency], information_needs: list[InformationNeed]) -> None:
    known_ids = {need.id for need in information_needs}
    for dependency in dependencies:
        if dependency.from_id == dependency.to_id:
            raise StructuredQueryValidationError("dependency from and to cannot be the same")
        if dependency.from_id not in known_ids or dependency.to_id not in known_ids:
            raise StructuredQueryValidationError("dependency must reference informationNeeds ids")


def _keys(data: dict[str, Any], path: str, *, required: set[str], optional: set[str]) -> None:
    missing = required - set(data)
    unexpected = set(data) - required - optional
    if missing:
        raise StructuredQueryValidationError(f"{path} is missing required fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise StructuredQueryValidationError(f"{path} has unsupported fields: {', '.join(sorted(unexpected))}")


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StructuredQueryValidationError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise StructuredQueryValidationError(f"{path} must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuredQueryValidationError(f"{path} must be a non-empty string")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _enum(value: Any, path: str, allowed: set[str]) -> str:
    result = _string(value, path)
    if result not in allowed:
        raise StructuredQueryValidationError(f"{path} must be one of: {', '.join(sorted(allowed))}")
    return result


def _optional_enum(value: Any, path: str, allowed: set[str]) -> str | None:
    if value is None:
        return None
    return _enum(value, path, allowed)


def _optional_date(value: Any, path: str) -> str | None:
    if value is None:
        return None
    result = _string(value, path)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise StructuredQueryValidationError(f"{path} must be an ISO date") from exc
    return result


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise StructuredQueryValidationError(f"{path} must be a boolean")
    return value


def _valid_metadata_value(value: Any) -> bool:
    if isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
