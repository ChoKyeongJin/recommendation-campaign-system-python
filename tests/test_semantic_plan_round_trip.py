from __future__ import annotations

import json

import pytest

import semantic_plan


def _full_plan() -> semantic_plan.SemanticPlanV2:
    return semantic_plan.plan_from_dict(
        {
            "version": semantic_plan.SEMANTIC_PLAN_VERSION,
            "source_query": "최근 30일 구매액이 100000원 이상인 고객",
            "nodes": [
                {
                    "id": "root",
                    "type": "logical_expression",
                    "source_span": "최근 30일 구매액이 100000원 이상",
                    "source_start": 0,
                    "source_end": 23,
                    "operator": "and",
                    "children": [
                        {
                            "id": "purchase-threshold",
                            "type": "aggregate_predicate",
                            "source_span": "최근 30일 구매액이 100000원 이상",
                            "source_start": 0,
                            "source_end": 23,
                            "scope": "purchase",
                            "metric": "purchase_amount",
                            "operator": ">=",
                            "value": {
                                "amount": "100000.1234567890123456789",
                                "currency": "KRW",
                            },
                            "aggregation": "sum",
                            "period": {"value": 30, "unit": "days"},
                            "producer": "deterministic",
                        }
                    ],
                }
            ],
            "conflicts": [{"status": "ambiguous", "source_span": "고객"}],
            "uncovered_requirements": [
                {"source_span": "최근", "reason": "test coverage record"}
            ],
            "capability_verdicts": [
                {"node_id": "purchase-threshold", "failure_code": "data_unavailable"}
            ],
            "validation_errors": [
                {"node_id": "purchase-threshold", "failure_code": "validation_mismatch"}
            ],
            "structurer_issues": [
                {"node_id": "purchase-threshold", "reason": "test issue"}
            ],
            "notes": ["round-trip"],
        }
    )


def test_full_plan_json_round_trip_preserves_persisted_and_derived_state() -> None:
    original = _full_plan()
    wire = json.loads(json.dumps(original.to_dict(), ensure_ascii=False))

    restored = semantic_plan.plan_from_dict(wire)

    assert restored.to_dict() == original.to_dict()
    assert restored.nodes[0] is not original.nodes[0]
    assert restored.conflicts is not original.conflicts


def test_full_plan_serialization_is_byte_for_byte_deterministic() -> None:
    plan = _full_plan()

    def encoded() -> bytes:
        return json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    assert encoded() == encoded()


def test_unknown_semantic_plan_version_fails_closed() -> None:
    with pytest.raises(semantic_plan.SemanticPlanError, match="지원하지 않는"):
        semantic_plan.plan_from_dict({"version": "999", "nodes": []})


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("conflicts", ["not-an-object"]),
        ("uncovered_requirements", {}),
        ("capability_verdicts", [None]),
        ("validation_errors", "invalid"),
        ("structurer_issues", [1]),
        ("notes", [{"not": "a string"}]),
    ],
)
def test_invalid_persisted_ledgers_fail_closed(field_name: str, invalid: object) -> None:
    with pytest.raises(semantic_plan.SemanticPlanError):
        semantic_plan.plan_from_dict({"nodes": [], field_name: invalid})
