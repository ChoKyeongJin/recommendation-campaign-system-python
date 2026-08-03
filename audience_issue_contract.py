"""Deterministic checks for model-authored audience clarification claims."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

import lexicon_patterns
import targeting_domain

_DURATION_RE = re.compile(
    r"(?:\d+(?:\.\d+)?|한|두|세|네|반)\s*(?:시간|일|주일|주|개월|달|분기|년)"
)
_EXTRA_TEMPORAL_QUALIFIERS = ("지난", "동안", "오랫동안", "장기", "예전", "과거")
_LATEST_TRANSITION_BRIDGE_RE = re.compile(
    r"^\s*(?:(?:에|의)|기준(?:으로)?)?\s*$"
)


def _term_pattern(term: str) -> re.Pattern[str] | None:
    normalized = unicodedata.normalize("NFKC", term).strip()
    if not normalized:
        return None
    pieces = [re.escape(piece) for piece in normalized.split()]
    body = r"\s*".join(pieces)
    if normalized.isascii() and all(char.isalnum() or char in "_-" for char in normalized):
        body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])"
    return re.compile(body, re.IGNORECASE)


def _term_spans(query: str, terms: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        pattern = _term_pattern(term)
        if pattern is not None:
            spans.extend((match.start(), match.end()) for match in pattern.finditer(query))
    return spans


def _current_subject_value_spans(
    query: str, catalog: Mapping[str, Any]
) -> list[tuple[int, int]]:
    fields = catalog.get("fields")
    domains = catalog.get("value_domains")
    if not isinstance(fields, Mapping) or not isinstance(domains, Mapping):
        return []
    spans: list[tuple[int, int]] = []
    for field_id, field in fields.items():
        if not str(field_id).startswith("subject.") or not isinstance(field, Mapping):
            continue
        domain = domains.get(field.get("value_domain"))
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            continue
        for canonical, declaration in values.items():
            aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
            terms = [str(canonical), *(str(alias) for alias in aliases or () if alias)]
            spans.extend(_term_spans(query, terms))
    # A long catalog phrase owns temporal-looking words inside it (for example
    # a declared value alias that itself starts with '최근').
    return sorted(spans, key=lambda span: (span[0], -(span[1] - span[0])))


def _has_external_temporal_qualifier(
    query: str, claim_spans: list[tuple[int, int]]
) -> bool:
    temporal_terms = {
        *_EXTRA_TEMPORAL_QUALIFIERS,
        *lexicon_patterns.vocabulary("source_latest_selector"),
        *lexicon_patterns.vocabulary("calendar_previous_month"),
        *lexicon_patterns.vocabulary("temporal_within_marker"),
        *lexicon_patterns.vocabulary("temporal_after_marker"),
    }
    temporal_spans = _term_spans(query, sorted(temporal_terms))
    temporal_spans.extend(
        (match.start(), match.end()) for match in _DURATION_RE.finditer(query)
    )
    return any(
        not any(start >= claim_start and end <= claim_end for claim_start, claim_end in claim_spans)
        for start, end in temporal_spans
    )


def fabricated_period_issue_for_current_catalog_value(
    query: str,
    issue: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> bool:
    """Reject a period omission invented for a catalog-grounded current value.

    This is deliberately one-way evidence. It proves only that a current
    subject value is already named and no separate temporal qualifier exists;
    inactivity phrases without such a catalog value remain untouched.
    """

    if issue.get("code") != "missing_argument" or issue.get("argument") != "period":
        return False
    claim_spans = _current_subject_value_spans(query, catalog)
    return bool(claim_spans) and not _has_external_temporal_qualifier(query, claim_spans)


def latest_transition_owns_period_issue(
    query: str,
    issue: Mapping[str, Any],
    semantic_plan: Any,
) -> bool:
    """Prove that ``최근`` selects the latest snapshot, not a duration.

    This receipt is intentionally narrow: the latest-selector evidence must be
    immediately attached to one unanchored directional transition node in the
    same clause.  A purchase phrase between ``최근`` and that node, multiple
    candidate nodes, or an unknown direction leaves the clarification intact.
    """

    if issue.get("code") != "missing_argument" or issue.get("argument") != "period":
        return False
    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    start, end, text = evidence.get("start"), evidence.get("end"), evidence.get("text")
    latest_terms = set(lexicon_patterns.vocabulary("source_latest_selector"))
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and isinstance(text, str)
        and query[start:end] == text
        and text.strip() in latest_terms
    ):
        return False
    nodes = semantic_plan.get("nodes") if isinstance(semantic_plan, Mapping) else None
    candidates: list[Mapping[str, Any]] = []
    for node in nodes or ():
        if not (
            isinstance(node, Mapping)
            and node.get("type") == "relation_predicate"
            and node.get("relation") == "transition"
            and targeting_domain.transition_direction(node.get("value")) is not None
            and node.get("period") in (None, "")
            and node.get("months") in (None, "")
        ):
            continue
        node_start, node_end = node.get("source_start"), node.get("source_end")
        if not (
            isinstance(node_start, int)
            and not isinstance(node_start, bool)
            and isinstance(node_end, int)
            and not isinstance(node_end, bool)
            and end <= node_start < node_end <= len(query)
            and node.get("source_span") == query[node_start:node_end]
            and _LATEST_TRANSITION_BRIDGE_RE.fullmatch(query[end:node_start])
        ):
            continue
        candidates.append(node)
    return len(candidates) == 1


__all__ = [
    "fabricated_period_issue_for_current_catalog_value",
    "latest_transition_owns_period_issue",
]
