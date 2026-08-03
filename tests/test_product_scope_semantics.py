"""Presence/absence reasoning for open-text product scopes in Event IR."""

from __future__ import annotations

from datetime import datetime, timezone

import aggregate_semantics
import aggregate_parser_config
import event_ir
import event_semantic_registry
import graph_rag


ANCHOR = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _product_match(value: str) -> event_ir.Comparison:
    return event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef("purchase_line.product_text"),
        right=event_ir.Literal(value),
    )


def _purchases(where: event_ir.Condition) -> event_ir.Exists:
    return event_ir.Exists(
        event_ir.Filter(event_ir.Source("purchase_line"), where)
    )


def _validate(expression: event_ir.Condition) -> aggregate_semantics.BooleanValidationResult:
    dimensions = event_semantic_registry.registry().domain_dimensions("purchase_line") or ()
    domains = {
        "purchase_line": aggregate_parser_config.SemanticDomain(
            presence_node_kinds=frozenset(),
            absence_node_kinds=frozenset(),
            event_dimensions=dimensions,
        )
    }
    return aggregate_semantics.validate_boolean_expression(
        expression,
        lambda atom, negated: graph_rag._event_predicate_factory(atom, negated, ANCHOR),
        domains=domains,
    )


def test_absent_product_and_present_exact_complement_are_consistent() -> None:
    """#61: no feed purchase AND at least one searchable non-feed purchase."""

    expression = event_ir.And(
        (
            event_ir.Not(_purchases(_product_match("사료"))),
            _purchases(event_ir.Not(_product_match("사료"))),
        )
    )

    result = _validate(expression)

    assert result.status == aggregate_semantics.CONSISTENT


def test_graph_validation_attaches_consistent_receipt_for_product_complement() -> None:
    expression = event_ir.And(
        (
            event_ir.Not(_purchases(_product_match("사료"))),
            _purchases(event_ir.Not(_product_match("사료"))),
        )
    )
    plan = {"event_expression": {"expression": expression.to_dict()}}

    graph_rag._attach_event_semantic_validation(plan)

    assert plan["event_semantic_validation"]["status"] == aggregate_semantics.CONSISTENT
    assert plan["event_semantic_validation"]["issues"] == []


def test_simple_product_absence_and_other_product_presence_are_individually_safe() -> None:
    """#59 and #60 do not invent a presence/absence conflict on their own."""

    absent = _validate(event_ir.Not(_purchases(_product_match("사료"))))
    other = _validate(_purchases(event_ir.Not(_product_match("사료"))))

    assert absent.status == aggregate_semantics.CONSISTENT
    assert other.status == aggregate_semantics.CONSISTENT


def test_identical_product_presence_and_absence_remain_contradictory() -> None:
    expression = event_ir.And(
        (
            _purchases(_product_match("사료")),
            event_ir.Not(_purchases(_product_match("사료"))),
        )
    )

    result = _validate(expression)

    assert result.status == aggregate_semantics.CONTRADICTORY
    assert {issue.code for issue in result.issues} == {"presence_absence_conflict"}


def test_different_open_text_literals_are_not_assumed_disjoint() -> None:
    """LIKE scopes for dynamic strings can overlap (for example one name can contain both)."""

    expression = event_ir.And(
        (
            _purchases(_product_match("간식")),
            event_ir.Not(_purchases(_product_match("사료"))),
        )
    )

    result = _validate(expression)

    assert result.status == aggregate_semantics.SEMANTIC_UNKNOWN
    assert {issue.code for issue in result.issues} == {"semantic_scope_unknown"}
