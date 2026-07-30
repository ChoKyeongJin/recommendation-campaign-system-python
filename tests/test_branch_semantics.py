"""Branch-aware semantic validation for Event IR.

The validator must reason about Boolean branches before deciding whether a
presence/absence pair is contradictory.  These tests deliberately keep atom
translation outside the validator so that Boolean reasoning and parser/SQL
projection remain separate contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import aggregate_semantics as semantics
import event_ir
import event_semantic_registry as scope_registry

ANCHOR = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _BoundAtom:
    node: event_ir.Exists
    predicate: semantics.EventPredicate


def _atom(
    domain: str,
    source_id: str,
    *,
    days: int = 30,
    constraints: Mapping[str, frozenset[str] | None] | None = None,
) -> _BoundAtom:
    node = event_ir.Exists(relation=event_ir.Source(domain))
    return _BoundAtom(
        node=node,
        predicate=semantics.EventPredicate(
            domain=domain,
            polarity=semantics.PRESENCE,
            window=semantics.rolling_window(ANCHOR, days),
            constraints=dict(constraints or {}),
            source_kind="event_expression",
            source_id=source_id,
        ),
    )


def _resolver(*bound_atoms: _BoundAtom) -> Callable[[object, bool], semantics.EventPredicate | None]:
    by_identity = {id(bound.node): bound.predicate for bound in bound_atoms}

    def resolve(atom: object, negated: bool) -> semantics.EventPredicate | None:
        predicate = by_identity.get(id(atom))
        if predicate is None:
            return None
        polarity = semantics.ABSENCE if negated else semantics.PRESENCE
        return replace(predicate, polarity=polarity)

    return resolve


def _issue_codes(result: semantics.BooleanValidationResult) -> set[str]:
    codes: set[str] = set()
    for issue in result.issues:
        if isinstance(issue, Mapping):
            code = issue.get("code")
        else:
            code = getattr(issue, "code", None)
        if isinstance(code, str):
            codes.add(code)
    return codes


def test_conflict_inside_one_and_branch_is_contradictory() -> None:
    present = _atom("purchase", "purchase-present", days=30)
    absent = _atom("purchase", "purchase-absent", days=180)
    expression = event_ir.And((present.node, event_ir.Not(absent.node)))

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(present, absent),
    )

    assert result.status == semantics.CONTRADICTORY
    assert len(result.branches) == 1


def test_opposite_predicates_in_different_or_branches_are_consistent() -> None:
    """Flattening this OR would incorrectly report a presence/absence conflict."""
    present = _atom("purchase", "purchase-present", days=30)
    absent = _atom("purchase", "purchase-absent", days=180)
    expression = event_ir.Or((present.node, event_ir.Not(absent.node)))

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(present, absent),
    )

    assert result.status == semantics.CONSISTENT
    assert len(result.branches) == 2


def test_or_is_contradictory_only_when_every_branch_is_contradictory() -> None:
    purchase_present = _atom("purchase", "purchase-present", days=30)
    purchase_absent = _atom("purchase", "purchase-absent", days=180)
    login_present = _atom("login", "login-present", days=30)
    login_absent = _atom("login", "login-absent", days=180)
    expression = event_ir.Or(
        (
            event_ir.And((purchase_present.node, event_ir.Not(purchase_absent.node))),
            event_ir.And((login_present.node, event_ir.Not(login_absent.node))),
        )
    )

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(purchase_present, purchase_absent, login_present, login_absent),
    )

    assert result.status == semantics.CONTRADICTORY
    assert len(result.branches) == 2


def test_an_unknown_or_branch_keeps_the_whole_expression_unknown() -> None:
    known = _atom("purchase", "known-purchase", days=30)
    unknown = _atom("unregistered-event", "unknown-event", days=30)
    expression = event_ir.Or((known.node, unknown.node))

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(known, unknown),
    )

    assert result.status == semantics.SEMANTIC_UNKNOWN
    assert "semantic_domain_unknown" in _issue_codes(result)


def test_registered_login_presence_and_absence_conflict() -> None:
    present = _atom("login", "login-present", days=30)
    absent = _atom("login", "login-absent", days=180)
    expression = event_ir.And((present.node, event_ir.Not(absent.node)))

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(present, absent),
    )

    assert result.status == semantics.CONTRADICTORY


def test_scope_registry_distinguishes_disjoint_from_unknown() -> None:
    registry = scope_registry.registry()

    assert semantics.classify_scope_relation(
        {"channel": frozenset({"online"})},
        {"channel": frozenset({"offline"})},
        ("channel",),
        semantic_registry=registry,
    ) == scope_registry.DISJOINT
    assert semantics.classify_scope_relation(
        {"channel": frozenset({"online"})},
        {"channel": frozenset({"partner-marketplace"})},
        ("channel",),
        semantic_registry=registry,
    ) == scope_registry.UNKNOWN


def test_one_registered_disjoint_dimension_is_not_erased_by_an_unknown_dimension() -> None:
    registry = scope_registry.registry()

    assert semantics.classify_scope_relation(
        {
            "channel": frozenset({"online"}),
            "brand": frozenset({"brand-a"}),
        },
        {
            "channel": frozenset({"offline"}),
            "brand": frozenset({"brand-b"}),
        },
        ("channel", "brand"),
        semantic_registry=registry,
    ) == scope_registry.DISJOINT

    assert semantics.classify_scope_relation(
        {
            "channel": frozenset({"online"}),
            "brand": frozenset({"brand-a"}),
        },
        {
            "channel": frozenset({"online"}),
            "brand": frozenset({"brand-b"}),
        },
        ("channel", "brand"),
        semantic_registry=registry,
    ) == scope_registry.UNKNOWN


def test_disjoint_scopes_are_consistent_but_unregistered_scopes_are_unknown() -> None:
    online = _atom(
        "purchase",
        "online-present",
        constraints={"channel": frozenset({"online"})},
    )
    offline = _atom(
        "purchase",
        "offline-absent",
        days=180,
        constraints={"channel": frozenset({"offline"})},
    )
    unknown = _atom(
        "purchase",
        "unknown-scope-absent",
        days=180,
        constraints={"channel": frozenset({"partner-marketplace"})},
    )

    disjoint_result = semantics.validate_boolean_expression(
        event_ir.And((online.node, event_ir.Not(offline.node))),
        _resolver(online, offline),
    )
    unknown_result = semantics.validate_boolean_expression(
        event_ir.And((online.node, event_ir.Not(unknown.node))),
        _resolver(online, unknown),
    )

    assert disjoint_result.status == semantics.CONSISTENT
    assert unknown_result.status == semantics.SEMANTIC_UNKNOWN
    assert "semantic_scope_unknown" in _issue_codes(unknown_result)


def test_unregistered_event_domain_is_unknown_not_safe() -> None:
    event = _atom("refund", "refund-present", days=30)

    result = semantics.validate_boolean_expression(event.node, _resolver(event))

    assert result.status == semantics.SEMANTIC_UNKNOWN
    assert "semantic_domain_unknown" in _issue_codes(result)


def test_branch_expansion_stops_at_the_configured_complexity_limit() -> None:
    left = tuple(_atom("purchase", f"left-{index}") for index in range(3))
    right = tuple(_atom("login", f"right-{index}") for index in range(3))
    expression = event_ir.And(
        (
            event_ir.Or(tuple(bound.node for bound in left)),
            event_ir.Or(tuple(bound.node for bound in right)),
        )
    )

    result = semantics.validate_boolean_expression(
        expression,
        _resolver(*left, *right),
        max_branches=8,
    )

    assert result.status == semantics.SEMANTIC_UNKNOWN
    assert "semantic_complexity_limit" in _issue_codes(result)
    assert len(result.branches) <= 8
