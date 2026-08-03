"""Literal receipts for a catalog event's rolling-window absence predicate."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

import event_ir
import lexicon_patterns


_CLOSED_DORMANT_LOGIN_ABSENCE_RE = re.compile(
    r"^\s*[1-9]\d*\s*(?:일|주|개월|달|년)\s*이상\s*"
    r"(?:접속|로그인)\s*하지\s*않은\s*휴면\s*(?:고객|회원)\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def _compact(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _source_is_grounded(
    query: str,
    source: event_ir.Source,
    evidence: event_ir.Evidence,
    catalog: Mapping[str, Any],
) -> bool:
    sources = catalog.get("sources")
    declaration = sources.get(source.name) if isinstance(sources, Mapping) else None
    if not isinstance(declaration, Mapping) or not declaration.get("time_column"):
        return False
    aliases = declaration.get("aliases")
    terms = [
        declaration.get("label"),
        *(aliases if isinstance(aliases, list) else []),
    ]
    evidence_text = query[evidence.start:evidence.end]
    source_named = any(
        token and token in _compact(evidence_text)
        for term in terms
        if (token := _compact(term))
    )
    negative = any(
        _compact(term) in _compact(evidence_text)
        for term in lexicon_patterns.vocabulary("generic_negation")
    )
    return (
        evidence.text == evidence_text
        and 0 <= evidence.start < evidence.end <= len(query)
        and source_named
        and negative
    )


def consumed_literal_binding_indices(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> frozenset[int]:
    """Consume ``duration + >=`` only for the exact ``NOT EXISTS rolling`` shape.

    ``N개월 이상 미접속`` is represented as no event inside the latest N-month
    window.  The comparison word has no standalone IR Comparison node, so the
    source literal ledger needs this structural receipt.
    """
    if not isinstance(expression, event_ir.Not) or not isinstance(
        expression.operand, event_ir.Exists
    ):
        return frozenset()
    exists = expression.operand
    relation = exists.relation
    if not isinstance(relation, event_ir.Filter) or not isinstance(
        relation.relation, event_ir.Source
    ):
        return frozenset()
    source = relation.relation
    time_filter = relation.where
    if not isinstance(time_filter, event_ir.TimeFilter) or not isinstance(
        time_filter.window, event_ir.RollingWindow
    ):
        return frozenset()
    if time_filter.field.name != f"{source.name}.occurred_at" or exists.evidence is None:
        return frozenset()
    if not _source_is_grounded(query, source, exists.evidence, catalog):
        return frozenset()

    rows = list(bindings)
    durations: list[tuple[int, Mapping[str, Any]]] = []
    operators: list[tuple[int, Mapping[str, Any]]] = []
    for index, binding in enumerate(rows):
        if not isinstance(binding, Mapping):
            continue
        normalized = binding.get("normalized")
        if binding.get("kind") == "duration" and isinstance(normalized, Mapping):
            if (
                normalized.get("value") == time_filter.window.value
                and event_ir.canonical_unit(normalized.get("semantic_unit"))
                == time_filter.window.unit
                and normalized.get("temporal_kind") == "rolling_duration"
            ):
                durations.append((index, binding))
        elif binding.get("kind") == "comparison_operator" and normalized == ">=":
            operators.append((index, binding))
    if len(durations) != 1 or len(operators) != 1:
        return frozenset()
    duration_index, duration = durations[0]
    operator_index, operator = operators[0]
    duration_end = duration.get("end")
    operator_start, operator_end = operator.get("start"), operator.get("end")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (duration_end, operator_start, operator_end)
    ):
        return frozenset()
    if not (
        duration_end <= operator_start < operator_end <= exists.evidence.start
        and not query[duration_end:operator_start].strip()
        and not query[operator_end:exists.evidence.start].strip()
    ):
        return frozenset()
    return frozenset({duration_index, operator_index})


def _source_evidence_candidates(
    query: str, catalog: Mapping[str, Any]
) -> list[tuple[str, event_ir.Evidence]]:
    sources = catalog.get("sources")
    if not isinstance(sources, Mapping):
        return []
    negations = tuple(
        str(term) for term in lexicon_patterns.vocabulary("generic_negation") if term
    )
    candidates: set[tuple[str, int, int]] = set()
    for source_name, declaration in sources.items():
        if not isinstance(declaration, Mapping) or not declaration.get("time_column"):
            continue
        aliases = declaration.get("aliases")
        terms = [
            declaration.get("label"),
            *(aliases if isinstance(aliases, list) else []),
        ]
        source_spans = {
            match.span()
            for term in terms
            if isinstance(term, str) and term
            for match in re.finditer(re.escape(term), query, flags=re.IGNORECASE)
        }
        for source_start, source_end in source_spans:
            for negative in negations:
                for match in re.finditer(re.escape(negative), query, flags=re.IGNORECASE):
                    negative_start, negative_end = match.span()
                    overlaps_prefixed_source = (
                        negative_start <= source_start and negative_end >= source_end
                    )
                    follows_source = (
                        source_end <= negative_start
                        and negative_start - source_end <= 8
                    )
                    if not (overlaps_prefixed_source or follows_source):
                        continue
                    evidence_start = min(source_start, negative_start)
                    evidence_end = max(source_end, negative_end)
                    # Include the Korean inflection attached to a short generic
                    # negation stem (``않`` -> ``않은``), but stop at the next
                    # word so evidence cannot absorb an unrelated condition.
                    while (
                        evidence_end < len(query)
                        and not query[evidence_end].isspace()
                        and query[evidence_end] not in ",.;:!?()[]{}"
                    ):
                        evidence_end += 1
                    candidates.add((str(source_name), evidence_start, evidence_end))
    return [
        (source_name, event_ir.Evidence(query[start:end], start, end))
        for source_name, start, end in sorted(candidates)
    ]


def synthesize_rolling_absence(
    query: str,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> event_ir.Condition | None:
    """Build ``NOT EXISTS(source in rolling window)`` from owned literals.

    Synthesis succeeds only when one catalog time source is locally negated and
    the resulting structural receipt consumes *every* extracted literal.  The
    receipt therefore remains the authority for the duration, ``>=`` polarity,
    source, time field, window kind, and source evidence adjacency.
    """
    rows = list(bindings)
    durations: list[tuple[int, str]] = []
    for binding in rows:
        if not isinstance(binding, Mapping) or binding.get("kind") != "duration":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
        value = normalized.get("value")
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            and unit in event_ir.WINDOW_UNITS
            and normalized.get("temporal_kind") == "rolling_duration"
        ):
            durations.append((value, str(unit)))
    if len(durations) != 1:
        return None

    value, unit = durations[0]
    expressions: dict[str, event_ir.Condition] = {}
    for source_name, evidence in _source_evidence_candidates(query, catalog):
        expression: event_ir.Condition = event_ir.Not(event_ir.Exists(
            event_ir.Filter(
                event_ir.Source(source_name),
                event_ir.TimeFilter(
                    event_ir.FieldRef(f"{source_name}.occurred_at"),
                    event_ir.RollingWindow(value, unit),
                ),
            ),
            evidence=evidence,
        ))
        consumed = consumed_literal_binding_indices(
            query, expression, rows, catalog
        )
        if consumed != frozenset(range(len(rows))):
            continue
        key = repr(expression.to_dict())
        expressions.setdefault(key, expression)
    return next(iter(expressions.values())) if len(expressions) == 1 else None


def synthesize_closed_dormant_login_absence(
    query: str,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> event_ir.Condition | None:
    """Recover one complete dormant-login phrase after total LLM failure.

    The general synthesizer proves the duration, unit, ``>=`` polarity,
    catalog source, local negation, and complete literal ledger.  This
    fallback-only wrapper adds the missing whole-query proof: the request must
    contain exactly that one login-absence condition and the dormant audience
    noun.  Nearby wording, an extra condition, or an ambiguous unit therefore
    stays on the ordinary fail-closed structurer path.
    """
    if not isinstance(query, str) or not _CLOSED_DORMANT_LOGIN_ABSENCE_RE.fullmatch(
        query
    ):
        return None
    expression = synthesize_rolling_absence(query, bindings, catalog)
    if not (
        isinstance(expression, event_ir.Not)
        and isinstance(expression.operand, event_ir.Exists)
        and isinstance(expression.operand.relation, event_ir.Filter)
        and isinstance(expression.operand.relation.relation, event_ir.Source)
        and expression.operand.relation.relation.name == "login"
    ):
        return None
    return expression


def normalize_rolling_absence_evidence(
    query: str,
    raw_expression: Any,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Narrow an over-broad absence atom evidence to its owned source clause.

    Models often attach ``N개월 이상`` to the ``Exists`` evidence even though
    duration and comparison are separate application-owned literals.  We only
    rewrite that span when the parsed expression, after removing evidence, is
    byte-for-byte equal to the independently synthesized and fully receipted
    rolling-absence expression.  Invalid offsets, different structure, extra
    literals, or a non-``>=`` operator therefore remain untouched and fail
    through the ordinary validator.
    """
    if not isinstance(raw_expression, MutableMapping):
        return None
    try:
        proposed = event_ir.condition_from_dict(dict(raw_expression))
    except event_ir.IrSchemaError:
        return None
    expected = synthesize_rolling_absence(query, bindings, catalog)
    if expected is None:
        return None
    if not (
        isinstance(proposed, event_ir.Not)
        and isinstance(proposed.operand, event_ir.Exists)
        and isinstance(expected, event_ir.Not)
        and isinstance(expected.operand, event_ir.Exists)
        and proposed.operand.evidence is not None
        and expected.operand.evidence is not None
    ):
        return None

    proposed_wire = proposed.to_dict()
    expected_wire = expected.to_dict()
    proposed_exists = proposed_wire.get("operand")
    expected_exists = expected_wire.get("operand")
    if not isinstance(proposed_exists, dict) or not isinstance(expected_exists, dict):
        return None
    proposed_exists.pop("evidence", None)
    expected_exists.pop("evidence", None)
    if proposed_wire != expected_wire:
        return None

    actual_evidence = proposed.operand.evidence
    expected_evidence = expected.operand.evidence
    if not (
        0 <= actual_evidence.start < actual_evidence.end <= len(query)
        and actual_evidence.text == query[actual_evidence.start:actual_evidence.end]
        and actual_evidence.start <= expected_evidence.start
        and expected_evidence.end <= actual_evidence.end
    ):
        return None
    if actual_evidence == expected_evidence:
        return None

    raw_exists = raw_expression.get("operand")
    if not isinstance(raw_exists, MutableMapping):
        return None
    raw_exists["evidence"] = expected_evidence.to_dict()
    return {
        "source": expected.operand.relation.relation.name,
        "from": actual_evidence.to_dict(),
        "to": expected_evidence.to_dict(),
    }


__all__ = [
    "consumed_literal_binding_indices",
    "normalize_rolling_absence_evidence",
    "synthesize_closed_dormant_login_absence",
    "synthesize_rolling_absence",
]
