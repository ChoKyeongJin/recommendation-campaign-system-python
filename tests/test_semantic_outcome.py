from __future__ import annotations

from typing import get_args

import pytest

import query_structurer
from query_structurer.semantic_ir import (
    SEMANTIC_FAILURE_KINDS,
    SEMANTIC_IR_LLM_JSON_SCHEMA,
    SEMANTIC_IR_STATUSES,
    empty_semantic_ir,
    validate_semantic_ir,
)
from query_structurer.semantic_outcome import (
    SEMANTIC_OUTCOME_WIRE_SPEC,
    FailureKind,
    SemanticOutcome,
    SemanticStatus,
    parse_semantic_outcome_projection,
    semantic_outcome_json_schema,
)


def test_unsupported_outcome_is_built_atomically() -> None:
    operation = {"kind": "unsupported_semantics", "reason": "unsupported", "evidence": "x"}
    outcome = SemanticOutcome.unsupported(operations=[operation], message="cannot execute")
    operation["reason"] = "mutated outside"

    projected = outcome.to_legacy_dict()
    assert projected["status"] == "unsupported"
    assert projected["operations"] == []
    assert projected["unsupported_operations"] == [
        {"kind": "unsupported_semantics", "reason": "unsupported", "evidence": "x"}
    ]

    projected["unsupported_operations"][0]["reason"] = "mutated projection"
    assert outcome.to_legacy_dict()["unsupported_operations"][0]["reason"] == "unsupported"


def test_unsupported_factory_rejects_an_invalid_intermediate_state() -> None:
    with pytest.raises(ValueError, match="requires operations"):
        SemanticOutcome.unsupported(operations=[])


def test_compatibility_facade_projects_typed_outcome() -> None:
    operation = {"kind": "x", "reason": "y", "evidence": "z"}

    assert empty_semantic_ir(
        status="unsupported",
        unsupported_operations=[operation],
        message="unsupported",
    ) == SemanticOutcome.unsupported(
        operations=[operation],
        message="unsupported",
    ).to_legacy_dict()


def test_outcome_schema_enums_are_derived_from_typed_contract() -> None:
    assert SEMANTIC_IR_STATUSES == frozenset(get_args(SemanticStatus))
    assert SEMANTIC_FAILURE_KINDS == frozenset(get_args(FailureKind))

    properties = SEMANTIC_IR_LLM_JSON_SCHEMA["properties"]
    assert set(properties["status"]["enum"]) == SEMANTIC_IR_STATUSES
    assert set(properties["failure_kind"]["enum"]) == {*SEMANTIC_FAILURE_KINDS, None}


def test_schema_and_runtime_parser_share_the_wire_declaration() -> None:
    assert SEMANTIC_IR_LLM_JSON_SCHEMA == semantic_outcome_json_schema()
    assert set(SEMANTIC_IR_LLM_JSON_SCHEMA["required"]) == {
        field.name for field in SEMANTIC_OUTCOME_WIRE_SPEC.fields if field.required_on_input
    }

    projection = empty_semantic_ir(missing_fields=["audience.requirement"])
    del projection["missing_field_causes"]
    del projection["failure_kind"]

    parsed = parse_semantic_outcome_projection(projection)
    assert parsed["missing_field_causes"] == []
    assert parsed["failure_kind"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["policy_applications"].append(
            {"policy_id": "policy", "fields": [], "extra": "x"}
        ),
        lambda value: value["unsupported_operations"].append(
            {"kind": " ", "reason": "reason", "evidence": "evidence"}
        ),
    ],
)
def test_runtime_parser_enforces_generated_closed_shape(mutate) -> None:
    projection = empty_semantic_ir(missing_fields=["audience.requirement"])
    mutate(projection)

    with pytest.raises(ValueError):
        parse_semantic_outcome_projection(projection)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("failure_kind", "invented_failure", "failure_kind"),
        ("missing_field_causes", ["not-an-object"], "missing_field_causes"),
    ],
)
def test_runtime_validator_enforces_typed_optional_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    projection = empty_semantic_ir(
        missing_fields=["audience.requirement"],
        failure_kind="user_clarification",
    )
    projection[field] = value

    with pytest.raises(ValueError, match=message):
        validate_semantic_ir(projection, [])


def test_public_exports_do_not_advertise_deleted_legacy_operations() -> None:
    assert "materialize_semantic_operations" not in query_structurer.__all__
    assert all(hasattr(query_structurer, name) for name in query_structurer.__all__)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SemanticOutcome(status="bogus"),
        lambda: SemanticOutcome.needs_clarification(missing_fields=[]),
        lambda: SemanticOutcome.needs_clarification(missing_fields=["   "]),
        lambda: SemanticOutcome(status="resolved", missing_fields=("x",)),
        lambda: SemanticOutcome(status="resolved", failure_kind="invented"),
        lambda: SemanticOutcome.policy_applied(
            applications=[{"policy_id": "policy", "fields": []}]
        ),
        lambda: SemanticOutcome.unsupported(
            operations=[{"kind": "x", "reason": " ", "evidence": "z"}]
        ),
    ],
)
def test_typed_outcome_cannot_represent_invalid_state(factory) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_direct_constructor_copies_nested_records() -> None:
    operation = {"kind": "x", "reason": "reason", "evidence": "z"}
    outcome = SemanticOutcome(status="unsupported", unsupported_operations=(operation,))

    operation["reason"] = "mutated"

    assert outcome.to_legacy_dict()["unsupported_operations"][0]["reason"] == "reason"
