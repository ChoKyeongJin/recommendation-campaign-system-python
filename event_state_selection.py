"""Catalog-declared *state* selection: which event source one clause really asks for.

Some registered sources are not separate events but the same event in a
particular physical state.  ``ODS_MALL_OMS_CART.KEEP_YN = 'Y'`` is the cart line
that has not been paid for; it is the *cart* event plus a state predicate, not a
member-wide purchase anti-join.  Asking for "a cart line kept and not paid" is
therefore a request for that state source, and answering it with
``cart AND NOT EXISTS(purchase)`` is wrong: it drops a member who bought some
other product during the same period.

The rule "cart surface + purchase negation in one clause selects the kept-cart
source" is a domain fact, so it lives in the catalog as a source-level
``selected_by`` declaration, not in a Korean sentence template here.  A similar
axis (an unused coupon, say) becomes one more declaration, not one more regex.

What this module owns is the *structure* of that judgement, and it is the same
three questions for every declaration:

* the two surfaces (base source alias, negated event) sit in **one clause**;
* the literal ledger is one rolling duration and it is **entirely consumed**;
* everything else in the request is **frame** — audience nouns, particles,
  request verbs, and a recency marker that merely restates the owned duration.

Nothing here reads model prose.  A missing declaration, an extra condition, a
past-point window, or a negation belonging to another clause all fail closed
onto the ordinary structurer path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import audience_frame
import event_ir
import lexicon_patterns
import semantic_fields

CATALOG_KEY = "selected_by"

Span = tuple[int, int]


@dataclass(frozen=True)
class StateSelection:
    """One catalog declaration: ``base`` + negated ``event`` selects ``selected``."""

    selected: str
    base: str
    negated_event: str
    window: str


@dataclass(frozen=True)
class StateSelectionClaim:
    """One proven selection plus the source spans that proved it."""

    selection: StateSelection
    expression: event_ir.Exists
    owned_spans: tuple[Span, ...]

    @property
    def filter_name(self) -> str:
        return f"event_state_selection.{self.selection.selected}"


def _sources(catalog: Mapping[str, Any] | None) -> Mapping[str, Any]:
    sources = catalog.get("sources") if isinstance(catalog, Mapping) else None
    return sources if isinstance(sources, Mapping) else {}


def declarations(catalog: Mapping[str, Any] | None) -> tuple[StateSelection, ...]:
    """Every complete ``selected_by`` declaration in the catalog.

    A declaration is ignored unless both referenced sources exist and the
    selected source carries a time column, because the synthesized atom is a
    time-scoped existence over exactly that source.
    """

    sources = _sources(catalog)
    found: list[StateSelection] = []
    for selected, declaration in sources.items():
        if not isinstance(declaration, Mapping):
            continue
        spec = declaration.get(CATALOG_KEY)
        if not isinstance(spec, Mapping):
            continue
        base, negated_event, window = (
            spec.get("source"),
            spec.get("negated_event"),
            spec.get("window"),
        )
        if not all(
            isinstance(value, str) and value for value in (base, negated_event, window)
        ):
            continue
        if not isinstance(sources.get(base), Mapping) or not isinstance(
            sources.get(negated_event), Mapping
        ):
            continue
        if not declaration.get("time_column"):
            continue
        found.append(
            StateSelection(
                selected=str(selected),
                base=str(base),
                negated_event=str(negated_event),
                window=str(window),
            )
        )
    return tuple(sorted(found, key=lambda item: item.selected))


def _surface_terms(sources: Mapping[str, Any], name: str) -> tuple[str, ...]:
    declaration = sources.get(name)
    if not isinstance(declaration, Mapping):
        return ()
    aliases = declaration.get("aliases")
    return tuple(
        term
        for term in (
            declaration.get("label"),
            *(aliases if isinstance(aliases, list) else ()),
        )
        if isinstance(term, str) and term
    )


def _content_terms() -> tuple[str, ...]:
    """Domain nouns that make an intervening phrase a *different* condition.

    ``다른 상품을`` between the cart surface and the purchase negation adds a
    product scope, so the two surfaces no longer describe one state.  The words
    are data; only "a scope noun in between means another clause" is structure.
    """

    return tuple(
        term
        for name in ("product_noun", "source_entity_domain")
        for term in lexicon_patterns.vocabulary(name)
    )


def _recency_terms() -> tuple[str, ...]:
    return tuple(lexicon_patterns.vocabulary("temporal_recency_marker"))


def _rolling_window(
    query: str, bindings: Sequence[Any], selection: StateSelection
) -> tuple[int, str, Span] | None:
    """The single duration literal this selection may own, or ``None``."""

    rows = [binding for binding in bindings if isinstance(binding, Mapping)]
    if len(rows) != len(bindings):
        return None
    durations = [binding for binding in rows if binding.get("kind") == "duration"]
    if len(durations) != 1:
        return None
    binding = durations[0]
    normalized = binding.get("normalized")
    if not isinstance(normalized, Mapping):
        return None
    unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
    value = normalized.get("value")
    start, end = binding.get("start"), binding.get("end")
    if not (
        # ``30일 전`` is a past point, not a rolling window; the declaration says
        # which kind this selection accepts and the ledger answers it.
        normalized.get("temporal_kind") == selection.window
        and unit in event_ir.WINDOW_UNITS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        and binding.get("value") == value
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and query[start:end] == binding.get("text")
    ):
        return None
    return value, str(unit), (start, end)


def _ledger_consumed(bindings: Sequence[Any], owned: Sequence[Span]) -> bool:
    """Every extracted literal must sit inside a span this claim owns."""

    for binding in bindings:
        if not isinstance(binding, Mapping):
            return False
        start, end = binding.get("start"), binding.get("end")
        if not (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
        ):
            return False
        if not any(
            owned_start <= start and end <= owned_end for owned_start, owned_end in owned
        ):
            return False
    return True


def _claim(
    query: str,
    bindings: Sequence[Any],
    sources: Mapping[str, Any],
    selection: StateSelection,
) -> StateSelectionClaim | None:
    window = _rolling_window(query, bindings, selection)
    if window is None:
        return None
    value, unit, duration_span = window

    base_terms = _surface_terms(sources, selection.base)
    base_spans = audience_frame.surface_spans(query, base_terms)
    if not base_spans:
        return None
    negation_spans = audience_frame.local_negation_spans(
        query, _surface_terms(sources, selection.negated_event)
    )
    # Two separate negations of the same event are two clauses; do not guess.
    if len(negation_spans) != 1:
        return None
    negation_span = negation_spans[0]

    stems = audience_frame.alias_stems(base_terms)
    content_terms = _content_terms()
    if not any(
        audience_frame.in_same_clause(
            query, base_span, negation_span, stems=stems, content_terms=content_terms
        )
        for base_span in base_spans
    ):
        return None

    owned: tuple[Span, ...] = (duration_span, negation_span, *base_spans)
    if not _ledger_consumed(bindings, owned):
        return None
    if not audience_frame.is_frame_only(query, owned, extra_terms=_recency_terms()):
        return None

    expression = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source(selection.selected),
            event_ir.TimeFilter(
                event_ir.FieldRef(f"{selection.selected}.occurred_at"),
                event_ir.RollingWindow(value, unit),
            ),
        ),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )
    return StateSelectionClaim(
        selection=selection, expression=expression, owned_spans=owned
    )


def synthesize_state_selection(
    query: str,
    literal_bindings: Any,
    catalog: Mapping[str, Any] | None,
) -> StateSelectionClaim | None:
    """Prove at most one declared state selection for this whole request."""

    if not isinstance(query, str) or not query or not isinstance(literal_bindings, list):
        return None
    sources = _sources(catalog)
    claims = [
        claim
        for selection in declarations(catalog)
        if (claim := _claim(query, literal_bindings, sources, selection)) is not None
    ]
    # Two declarations proving the same request means the request is ambiguous.
    return claims[0] if len(claims) == 1 else None


def owns_state_selection_claim(
    query: str,
    raw_expression: Any,
    literal_bindings: Any,
    catalog: Mapping[str, Any] | None,
) -> bool:
    """Whether this exact query/IR pair *is* the proven state selection.

    Mentioning the state source is not enough: one kept cart row must not hide a
    distinct member-wide request such as ``구매 이력이 없는``.  The complete
    semantic tree is compared, provenance excluded, so any extra or missing atom
    fails closed.
    """

    claim = synthesize_state_selection(query, literal_bindings, catalog)
    if claim is None:
        return False
    try:
        actual = (
            raw_expression
            if isinstance(raw_expression, event_ir.Condition)
            else event_ir.condition_from_dict(raw_expression)
        )
    except (event_ir.IrSchemaError, KeyError, TypeError, ValueError):
        return False
    return semantic_fields.strip_provenance(
        actual.to_dict()
    ) == semantic_fields.strip_provenance(claim.expression.to_dict())


def owned_negation_spans(
    query: str,
    present_sources: Iterable[str],
    catalog: Mapping[str, Any] | None,
) -> dict[str, tuple[Span, ...]]:
    """negated event -> the source spans a present state source already owns.

    This is span-grained ownership, and deliberately weaker than
    :func:`owns_state_selection_claim`: the drop detector asks "is *this* piece
    of the request represented?", not "is the whole request this one claim?".
    Ownership still requires the same clause, which is what stops a kept-cart
    atom from swallowing a separate member-wide purchase absence.
    """

    if not isinstance(query, str) or not query:
        return {}
    sources = _sources(catalog)
    present = {str(name) for name in present_sources or ()}
    found: dict[str, set[Span]] = {}
    content_terms = _content_terms()
    for selection in declarations(catalog):
        if selection.selected not in present:
            continue
        base_terms = _surface_terms(sources, selection.base)
        base_spans = audience_frame.surface_spans(query, base_terms)
        if not base_spans:
            continue
        stems = audience_frame.alias_stems(base_terms)
        for negation_span in audience_frame.local_negation_spans(
            query, _surface_terms(sources, selection.negated_event)
        ):
            if any(
                audience_frame.in_same_clause(
                    query,
                    base_span,
                    negation_span,
                    stems=stems,
                    content_terms=content_terms,
                )
                for base_span in base_spans
            ):
                found.setdefault(selection.negated_event, set()).add(negation_span)
    return {event: tuple(sorted(spans)) for event, spans in found.items()}


def _time_scoped_exists(node: event_ir.Condition, source: str) -> bool:
    if not isinstance(node, event_ir.Exists):
        return False
    relation = node.relation
    if isinstance(relation, event_ir.Source):
        return relation.name == source
    return bool(
        isinstance(relation, event_ir.Filter)
        and isinstance(relation.relation, event_ir.Source)
        and relation.relation.name == source
        and isinstance(relation.where, event_ir.TimeFilter)
        and relation.where.field == event_ir.FieldRef(f"{source}.occurred_at")
    )


def is_refutable_model_shape(
    raw_expression: Any, catalog: Mapping[str, Any] | None
) -> bool:
    """Recognize only the model shapes whose over-constraint is provable.

    A single state existence atom is the desired logical shape.  Models also
    emit the base source plus a member-wide ``Not(Exists(negated event))``; the
    declared state predicate makes that second atom both unnecessary and
    semantically too broad.  Any product filter, comparison, OR, or additional
    audience atom fails closed here.
    """

    try:
        expression = event_ir.condition_from_dict(raw_expression)
    except (event_ir.IrSchemaError, KeyError, TypeError, ValueError):
        return False
    for selection in declarations(catalog):
        if _time_scoped_exists(expression, selection.base) or _time_scoped_exists(
            expression, selection.selected
        ):
            return True
        if not isinstance(expression, event_ir.And) or len(expression.operands) != 2:
            continue
        base_atoms = [
            operand
            for operand in expression.operands
            if _time_scoped_exists(operand, selection.base)
            or _time_scoped_exists(operand, selection.selected)
        ]
        absence_atoms = [
            operand
            for operand in expression.operands
            if isinstance(operand, event_ir.Not)
            and _time_scoped_exists(operand.operand, selection.negated_event)
        ]
        if len(base_atoms) == 1 and len(absence_atoms) == 1:
            return True
    return False


__all__ = [
    "CATALOG_KEY",
    "StateSelection",
    "StateSelectionClaim",
    "declarations",
    "is_refutable_model_shape",
    "owned_negation_spans",
    "owns_state_selection_claim",
    "synthesize_state_selection",
]
