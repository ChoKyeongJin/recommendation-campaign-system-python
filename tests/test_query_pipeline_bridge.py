"""사건 IR ↔ 실행 표현 왕복 계약 — 노드 타입 **전수**로 무손실을 강제한다.

이 테스트가 없으면 새 계층은 '조용한 축소'의 새 출구가 된다. event_ir 의 노드 목록은
닫힌 집합(``event_ir.NODE_TYPES``)이므로, 새 노드가 생기면 여기서 즉시 드러난다.
"""

from __future__ import annotations

import pytest

import event_ir
from query_pipeline.event_query import event_ir_bridge
from query_pipeline.event_query.expressions import (
    AggregateConditionExpression,
    AggregateDefinition,
    AggregateFunction,
    ComparisonOperator,
    EntityRelation,
    LiteralOperand,
    node_kinds,
)

EVIDENCE = {"text": "지난달 구매", "start": 0, "end": 6}

COMPARISON = {
    "type": "comparison",
    "operator": ">=",
    "left": {"type": "field", "name": "purchase.amount"},
    "right": {"type": "literal", "value": 10000},
    "evidence": EVIDENCE,
}

TIME_FILTER = {
    "type": "time_filter",
    "field": {"type": "field", "name": "purchase.occurred_at"},
    "window": {"type": "interval", "start": "2026-07-01", "end_exclusive": "2026-08-01"},
}

EXISTS = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": TIME_FILTER,
    },
    "evidence": EVIDENCE,
}

AGGREGATE_COMPARISON = {
    "type": "comparison",
    "operator": ">=",
    "left": {
        "type": "aggregate",
        "function": "count",
        "distinct": False,
        "expression": None,
        "relation": {"type": "source", "name": "purchase"},
    },
    "right": {"type": "literal", "value": 3},
    "evidence": EVIDENCE,
}

ARITHMETIC_COMPARISON = {
    "type": "comparison",
    "operator": ">",
    "left": {
        "type": "arithmetic",
        "operator": "*",
        "left": {"type": "field", "name": "purchase.amount"},
        "right": {"type": "literal", "value": 2},
    },
    "right": {"type": "literal", "value": 100},
    "evidence": EVIDENCE,
}

TEMPORAL = {
    "type": "temporal_relation",
    "operator": "within_after",
    "left": {"type": "event_reference", "source": "signup", "selector": "first"},
    "right": {"type": "event_reference", "source": "purchase", "selector": "any"},
    "duration": {"type": "duration", "value": 7, "unit": "day"},
    "evidence": EVIDENCE,
}

RANKED_SET = {
    "type": "exists",
    "relation": {
        "type": "join",
        "kind": "semi",
        "left": {"type": "source", "name": "purchase_line"},
        "right": {
            "type": "limit",
            "count": 3,
            "relation": {
                "type": "order",
                "keys": [
                    {"name": "measure_value", "direction": "desc"},
                    {"name": "entity_key", "direction": "asc"},
                ],
                "relation": {
                    "type": "summarize",
                    "relation": {
                        "type": "source",
                        "name": "purchase_line",
                        "correlation": "none",
                    },
                    "keys": [
                        {
                            "name": "entity_key",
                            "expression": {
                                "type": "field",
                                "name": "purchase_line.product_name",
                            },
                        }
                    ],
                    "measures": [
                        {
                            "name": "measure_value",
                            "function": "sum",
                            "distinct": False,
                            "expression": {
                                "type": "field",
                                "name": "purchase_line.quantity",
                            },
                        }
                    ],
                },
            },
        },
        "on": {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": "purchase_line.product_name"},
            "right": {"type": "field", "name": "purchase_line.product_name"},
            "evidence": EVIDENCE,
        },
    },
    "evidence": EVIDENCE,
}

GROUPED_PROJECT = {
    "type": "exists",
    "relation": {
        "type": "project",
        "relation": {
            "type": "group",
            "relation": {"type": "source", "name": "purchase"},
            "keys": [{"type": "field", "name": "purchase.amount"}],
        },
        "items": [
            {
                "name": "amount_alias",
                "expression": {"type": "field", "name": "purchase.amount"},
            }
        ],
    },
    "evidence": EVIDENCE,
}

BOOLEAN_TREE = {
    "type": "and",
    "operands": [
        {"type": "or", "operands": [COMPARISON, EXISTS]},
        {"type": "not", "operand": TIME_FILTER},
    ],
}

ROLLING = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {"type": "rolling", "value": 30, "unit": "day"},
        },
    },
    "evidence": EVIDENCE,
}

RELATIVE = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {
                "type": "relative",
                "value": 3,
                "unit": "month",
                "direction": "past",
            },
        },
    },
    "evidence": EVIDENCE,
}

WIRES: dict[str, dict] = {
    "comparison": COMPARISON,
    "time_filter": TIME_FILTER,
    "exists": EXISTS,
    "aggregate": AGGREGATE_COMPARISON,
    "arithmetic": ARITHMETIC_COMPARISON,
    "temporal_relation": TEMPORAL,
    "ranked_set": RANKED_SET,
    "grouped_project": GROUPED_PROJECT,
    "boolean_tree": BOOLEAN_TREE,
    "rolling": ROLLING,
    "relative": RELATIVE,
}


@pytest.mark.parametrize("name", sorted(WIRES))
def test_wire_roundtrip_is_lossless(name: str) -> None:
    wire = WIRES[name]
    canonical = event_ir.condition_from_dict(wire).to_dict()
    expression = event_ir_bridge.from_wire(wire)
    assert event_ir_bridge.to_wire(expression) == canonical


def test_every_event_ir_node_type_is_covered() -> None:
    """event_ir 의 닫힌 노드 목록 전부가 위 코퍼스에 등장한다."""
    seen: set[str] = set()
    for wire in WIRES.values():
        for node in event_ir.walk(event_ir.condition_from_dict(wire)):
            node_type = getattr(node, "type", None)
            if isinstance(node_type, str):
                seen.add(node_type)
    missing = sorted(event_ir.NODE_TYPES - seen - {"event_reference"})
    assert not missing, f"왕복 코퍼스가 덮지 않는 노드 타입: {missing}"


def test_unrepresentable_operator_raises_instead_of_narrowing() -> None:
    """IN/BETWEEN/CONTAINS 는 사건 IR 에 없다 — 축소가 아니라 예외다."""
    condition = AggregateConditionExpression(
        aggregate=AggregateDefinition(
            function=AggregateFunction.COUNT,
            relation=EntityRelation(entity="purchase"),
        ),
        operator=ComparisonOperator.BETWEEN,
        value=LiteralOperand(value=(1, 5)),
    )
    with pytest.raises(event_ir_bridge.ExpressionBridgeError, match="between"):
        event_ir_bridge.to_event_ir(condition)


def test_set_literal_cannot_reach_event_ir() -> None:
    from query_pipeline.event_query.expressions import (
        AttributeOperand,
        ComparisonExpression,
    )

    condition = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=AttributeOperand(entity="purchase", attribute="amount"),
        right=LiteralOperand(value=(1, 2)),
    )
    with pytest.raises(event_ir_bridge.ExpressionBridgeError, match="단일 스칼라"):
        event_ir_bridge.to_event_ir(condition)


def test_node_kinds_reports_the_whole_tree() -> None:
    kinds = node_kinds(event_ir_bridge.from_wire(RANKED_SET))
    assert {"exists", "join", "limit", "order", "summarize", "entity"} <= kinds
