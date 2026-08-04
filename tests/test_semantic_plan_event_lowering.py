from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import semantic_plan  # noqa: E402
from resolved_semantic_catalog import resolve_semantic_catalog  # noqa: E402
from semantic_plan_event_lowering import (  # noqa: E402
    FAILED,
    LOWERED,
    MISSING_ARGUMENT,
    PARTIALLY_SUPPORTED,
    SUPPORTED,
    lower_semantic_plan,
)


def _runtime() -> dict:
    return {
        "sources": {
            "observation": {
                "table": "FACT_OBSERVATION",
                "alias": "OBS",
                "event_subject_key": "ACCOUNT_ID",
                "time_column": "OBSERVED_ON",
                "time_format": "date",
                "coverage": "history",
            },
            "alert": {
                "table": "FACT_ALERT",
                "alias": "ALT",
                "event_subject_key": "ACCOUNT_ID",
                "time_column": "CREATED_ON",
                "time_format": "date",
            },
        },
        "fields": {
            "observation.sample_id": {
                "source": "observation", "column": "SAMPLE_ID", "data_type": "string",
            },
            "observation.quality": {
                "source": "observation", "column": "QUALITY_CODE", "data_type": "string",
            },
            "alert.severity": {
                "source": "alert", "column": "SEVERITY_CODE", "data_type": "string",
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
                "allowed_operators": [">=", "<"],
                "where": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": "observation.quality"},
                    "right": {"type": "literal", "value": "valid"},
                },
            },
            "critical_alert": {
                "source": "alert",
                "kind": "existence",
                "data_type": "boolean",
                "allowed_operators": ["=", "!="],
                "where": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": "alert.severity"},
                    "right": {"type": "literal", "value": "critical"},
                },
            },
        },
        "data_coverage": {
            "history": {
                "available_from": "2025-01-01",
                "complete_through": "2026-12-31",
                "max_lookback_days": 365,
            }
        },
    }


def _catalog():
    return resolve_semantic_catalog(runtime_config=_runtime())


def _node(payload: dict) -> semantic_plan.SemanticNode:
    return semantic_plan.node_from_dict(payload)


def _plan(*nodes: semantic_plan.SemanticNode) -> semantic_plan.SemanticPlanV2:
    return semantic_plan.SemanticPlanV2(nodes=list(nodes))


def test_aggregate_and_existence_lower_to_one_canonical_expression_and_compile() -> None:
    catalog = _catalog()
    plan = _plan(
        _node({
            "id": "n1",
            "type": "aggregate_predicate",
            "source_span": "valid observations at least three times",
            "scope": "telemetry",
            "metric": "valid_sample_count",
            "operator": ">=",
            "value": 3,
            "period": {"value": 30, "unit": "days"},
        }),
        _node({
            "id": "n2",
            "type": "predicate",
            "source_span": "without a critical alert",
            "subject": "member",
            "metric": "critical_alert",
            "operator": "=",
            "value": False,
        }),
    )

    result = lower_semantic_plan(plan, catalog)

    assert result.status == SUPPORTED and result.executable
    assert isinstance(result.expression, event_ir.And)
    aggregate, absence = result.expression.operands
    assert isinstance(aggregate, event_ir.Comparison)
    assert isinstance(aggregate.left, event_ir.Aggregate)
    assert aggregate.left.function == "count" and aggregate.left.distinct is True
    assert isinstance(absence, event_ir.Not)
    assert isinstance(absence.operand, event_ir.Exists)
    assert [receipt.status for receipt in result.receipts] == [LOWERED, LOWERED]
    assert all(receipt.expression_fingerprint for receipt in result.receipts)

    sql = event_compiler.compile_expression(
        result.expression,
        context=catalog.compile_context(literals=True),
    ).sql
    assert "COUNT(DISTINCT OBS.SAMPLE_ID)" in sql
    assert "OBS.QUALITY_CODE = 'valid'" in sql
    assert "NOT EXISTS (SELECT 1 FROM FACT_ALERT ALT" in sql
    assert "ALT.SEVERITY_CODE = 'critical'" in sql


def test_logical_expression_preserves_and_or_not_shape() -> None:
    logical = _node({
        "id": "logic",
        "type": "logical_expression",
        "source_span": "not either condition",
        "operator": "not",
        "children": [{
            "id": "or",
            "type": "logical_expression",
            "source_span": "either condition",
            "operator": "or",
            "children": [
                {
                    "id": "alert",
                    "type": "predicate",
                    "source_span": "has a critical alert",
                    "subject": "member",
                    "metric": "critical_alert",
                    "operator": "=",
                    "value": True,
                },
                {
                    "id": "tier",
                    "type": "predicate",
                    "source_span": "tier is gold",
                    "subject": "member",
                    "metric": "subject.tier",
                    "operator": "=",
                    "value": "gold",
                },
            ],
        }],
    })

    result = lower_semantic_plan(_plan(logical), _catalog())

    assert result.status == SUPPORTED
    assert isinstance(result.expression, event_ir.Not)
    assert isinstance(result.expression.operand, event_ir.Or)
    assert {receipt.node_id for receipt in result.receipts} == {"alert", "tier", "or", "logic"}


def test_temporal_qualifier_without_its_interval_fails_as_missing_argument() -> None:
    node = _node({
        "id": "recent",
        "type": "aggregate_predicate",
        "source_span": "recent valid observations",
        "scope": "telemetry",
        "metric": "valid_sample_count",
        "operator": ">=",
        "value": 3,
        "temporal": {
            "operator": "WITHIN_INTERVAL",
            "missing_arguments": ["interval"],
        },
    })

    result = lower_semantic_plan(_plan(node), _catalog())

    assert not result.executable
    assert result.failures[0].node_id == "recent"
    assert result.failures[0].failure_code == MISSING_ARGUMENT


def test_one_unknown_metric_blocks_the_whole_plan_instead_of_narrowing_it() -> None:
    plan = _plan(
        _node({
            "id": "good", "type": "aggregate_predicate", "source_span": "enough samples",
            "scope": "telemetry", "metric": "valid_sample_count", "operator": ">=", "value": 3,
        }),
        _node({
            "id": "bad", "type": "predicate", "source_span": "unknown property",
            "subject": "member", "metric": "unregistered_metric", "operator": "=", "value": True,
        }),
    )

    result = lower_semantic_plan(plan, _catalog())

    assert result.status == PARTIALLY_SUPPORTED
    assert result.expression is None and not result.executable
    assert [(item.node_id, item.status) for item in result.receipts] == [
        ("good", LOWERED), ("bad", FAILED),
    ]


def test_declared_data_coverage_does_not_restrict_sql_lowering() -> None:
    node = _node({
        "id": "history",
        "type": "aggregate_predicate",
        "source_span": "five hundred day history",
        "scope": "telemetry",
        "metric": "valid_sample_count",
        "operator": ">=",
        "value": 3,
        "period": {"value": 500, "unit": "days"},
    })

    result = lower_semantic_plan(_plan(node), _catalog())

    assert result.status == SUPPORTED
    assert result.executable and result.expression is not None
    assert result.failures == ()
    assert any(
        isinstance(node, event_ir.RollingWindow)
        and node.value == 500
        and node.unit == "day"
        for node in event_ir.walk(result.expression)
    )


def test_lowering_fingerprint_is_deterministic_and_ignores_evidence_offsets() -> None:
    first = _node({
        "id": "a", "type": "aggregate_predicate", "source_span": "first wording",
        "source_start": 0, "source_end": 13,
        "scope": "telemetry", "metric": "valid_sample_count", "operator": ">=", "value": 3,
    })
    second = _node({
        "id": "b", "type": "aggregate_predicate", "source_span": "another wording",
        "source_start": 40, "source_end": 55,
        "scope": "telemetry", "metric": "valid_sample_count", "operator": ">=", "value": 3,
    })

    left = lower_semantic_plan(_plan(first), _catalog())
    right = lower_semantic_plan(_plan(second), _catalog())

    assert left.expression_fingerprint == right.expression_fingerprint
    assert left.receipts[0].expression_fingerprint == right.receipts[0].expression_fingerprint
