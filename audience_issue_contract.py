"""Deterministic checks for model-authored audience clarification claims."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

import lexicon_patterns

_DURATION_RE = re.compile(
    r"(?:\d+(?:\.\d+)?|한|두|세|네|반)\s*(?:시간|일|주일|주|개월|달|분기|년)"
)
_EXTRA_TEMPORAL_QUALIFIERS = ("지난", "동안", "오랫동안", "장기", "예전", "과거")


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


# `latest_transition_owns_period_issue` 는 2026-08-05 제거됐다. 그 함수는 '최근'이 기간이
# 아니라 최신 스냅샷 선택임을 증명해 SemanticPlan 전이 노드에 기간 결핍 issue 를 유예하는
# 영수증이었고, 그 노드를 컴파일하는 경로가 폐기되면서 유예받을 소비자가 사라졌다.


__all__ = [
    "fabricated_period_issue_for_current_catalog_value",
]
