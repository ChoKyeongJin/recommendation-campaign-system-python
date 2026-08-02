from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_compiler  # noqa: E402
from resolved_semantic_catalog import (  # noqa: E402
    DataCoverageSpec,
    FieldSpec,
    GrainSpec,
    JoinSpec,
    MetricSpec,
    OperatorSpec,
    SourceSpec,
    TimeSpec,
    resolve_semantic_catalog,
)


def runtime_catalog() -> dict:
    return {
        "sources": {
            "observation": {
                "table": "FACT_OBSERVATION",
                "alias": "OBS",
                "event_subject_key": "ACCOUNT_ID",
                "time_column": "OBSERVED_ON",
                "time_format": "date",
                "coverage": "observation_history",
            },
            "device": {
                "table": "DIM_DEVICE",
                "alias": "DEV",
                "event_subject_key": "ACCOUNT_ID",
                "time_column": "REGISTERED_ON",
                "time_format": "date",
            },
        },
        "fields": {
            "observation.sample_id": {
                "source": "observation", "column": "SAMPLE_ID", "data_type": "string",
            },
            "observation.device_id": {
                "source": "observation", "column": "DEVICE_ID", "data_type": "string",
            },
            "observation.quality": {
                "source": "observation", "column": "QUALITY_CODE", "data_type": "string",
            },
            "device.device_id": {
                "source": "device", "column": "DEVICE_ID", "data_type": "string",
            },
            "device.serial": {
                "source": "device",
                "column": "SERIAL_NO",
                "expression": "UPPER({alias}.SERIAL_NO)",
                "data_type": "string",
            },
            "subject.tier": {
                "source": "subject", "column": "TIER_CODE", "data_type": "string",
            },
        },
        "metrics": {
            "valid_sample_count": {
                "source": "observation",
                "kind": "aggregate",
                "function": "count",
                "expression": "observation.sample_id",
                "distinct": True,
                "data_type": "number",
                "grain": "subject",
                "allowed_operators": ["gte"],
                "where": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": "observation.quality"},
                    "right": {"type": "literal", "value": "valid"},
                },
            },
        },
        "joins": [
            {
                "id": "observation_device",
                "left_source": "observation",
                "right_source": "device",
                "left_field": "observation.device_id",
                "right_field": "device.device_id",
                "cardinality": "many_to_one",
            }
        ],
        "grains": {"per_device": {"keys": ["observation.device_id"]}},
        "operators": {
            "gte": {"symbol": ">=", "value_types": ["number"]},
        },
        "times": {
            "observation.required_time": {
                "field": "observation.occurred_at",
                "required": True,
                "coverage": "observation_history",
            }
        },
        "data_coverage": {
            "observation_history": {
                "available_from": "2025-01-01",
                "complete_through": "2026-12-31",
                "max_lookback_days": 365,
                "timezone": "UTC",
            }
        },
    }


def test_facade_resolves_all_typed_specs_from_compiler_and_runtime() -> None:
    catalog = resolve_semantic_catalog(runtime_config=runtime_catalog())

    assert isinstance(catalog.source("observation"), SourceSpec)
    assert isinstance(catalog.field("observation.sample_id"), FieldSpec)
    assert isinstance(catalog.metric("valid_sample_count"), MetricSpec)
    assert isinstance(catalog.join("observation_device"), JoinSpec)
    assert isinstance(catalog.grain("per_device"), GrainSpec)
    assert isinstance(catalog.resolve_operator("gte"), OperatorSpec)
    assert isinstance(catalog.time("observation.required_time"), TimeSpec)
    assert isinstance(catalog.coverage("observation_history"), DataCoverageSpec)

    metric = catalog.metric("valid_sample_count")
    assert metric.aggregate_function == "count"
    assert metric.expression_field == "observation.sample_id"
    assert metric.distinct is True
    assert metric.time == "observation.event_time"
    assert catalog.resolve_operator(metric.allowed_operators[0]).symbol == ">="


def test_compiler_context_uses_the_same_resolved_physical_bindings() -> None:
    catalog = resolve_semantic_catalog(runtime_config=runtime_catalog())
    context = catalog.compile_context(literals=True)

    assert context.event_spec("observation").table == "FACT_OBSERVATION"
    assert context.field_spec("observation.sample_id").column == "SAMPLE_ID"
    assert context.field_spec("device.serial").expression == "UPPER({alias}.SERIAL_NO)"
    assert context.field_spec("observation.occurred_at").column == "OBSERVED_ON"


def test_existing_compiler_entries_are_projected_without_duplicate_metric_config() -> None:
    catalog = resolve_semantic_catalog(runtime_config=runtime_catalog())

    # Every compiler event gets a conservative existence metric, and every
    # compiler field gets a field metric.  Runtime config only declares the
    # aggregate that cannot be inferred from a column alone.
    assert catalog.metric("observation").kind == "existence"
    assert catalog.metric("subject.tier").kind == "field"
    assert catalog.metric("subject.tier").expression_field == "subject.tier"


def test_resolved_mappings_are_immutable() -> None:
    catalog = resolve_semantic_catalog(runtime_config=runtime_catalog())
    with pytest.raises(TypeError):
        catalog.metrics["new"] = catalog.metric("valid_sample_count")  # type: ignore[index]

