from __future__ import annotations

from typing import get_args

from query_structurer import schema, types


def test_schema_enums_are_derived_from_public_literal_types() -> None:
    pairs = (
        (schema.INTENTS, types.Intent),
        (schema.COMPLEXITIES, types.Complexity),
        (schema.METADATA_OPERATORS, types.MetadataOperator),
        (schema.OPERATIONS, types.Operation),
        (schema.OUTPUT_FORMATS, types.OutputFormat),
    )

    for runtime_values, public_type in pairs:
        assert runtime_values == set(get_args(public_type))


def test_json_schema_uses_the_same_derived_enums() -> None:
    properties = schema.STRUCTURED_QUERY_JSON_SCHEMA["properties"]

    assert set(properties["intent"]["enum"]) == schema.INTENTS
    assert set(properties["complexity"]["enum"]) == schema.COMPLEXITIES
    assert set(properties["operations"]["items"]["enum"]) == schema.OPERATIONS
