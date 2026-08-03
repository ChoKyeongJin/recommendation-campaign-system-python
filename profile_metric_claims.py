"""Declared profile metric phrase -> application-owned SemanticPlan predicate.

The live structurer can correctly identify that a monthly-snapshot metric is
outside Event IR and still fail to emit the narrow ``semantic_plan`` node that
owns that axis.  This module closes only the fully declared case.  It does not
scan arbitrary prose or invent a threshold:

* exactly one unsupported issue identifies a registered scalar metric alias;
* the whole query matches one metric/threshold/operator member-list phrase;
* the only literal bindings are one unit-bearing duration and one comparison;
* metric unit, allowed operator, physical source, and targeting snapshot binding
  all agree in the metric registry.

The duration binding is deliberately used as a *unit-bearing scalar*.  The
generic extractor labels every bare duration ``rolling_duration`` by default,
so ownership is decided by the closed syntax here instead: the duration must
sit immediately between the metric and its comparison operator, with no
``최근``/``동안``/``전`` remainder.  That distinction prevents ``30일`` from
becoming either an invented rolling window or the Sino-Korean number ``일``
(one) during surface normalization.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import event_ir
import metric_registry
from semantic_normalizers import NormalizationError, OperatorNormalizer

OWNER = "profile_metric_claims.catalog_literal_operator"
_MEMBER_SUFFIX = re.compile(
    r"(?:인\s*)?(?:회원|고객)(?:을|를)?"
    r"(?:\s*(?:찾아\s*줘|찾아\s*주세요|추출해\s*줘|추출해\s*주세요))?"
    r"\s*[.!]?\s*$"
)


@dataclass(frozen=True)
class ProfileMetricSynthesis:
    """One predicate plus the catalog/literal receipt that proves it."""

    node: dict[str, Any]
    receipt: dict[str, Any]


def _span(row: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    start, end, text = row.get("start"), row.get("end"), row.get("text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and isinstance(text, str)
        and 0 <= start < end <= len(query)
        and query[start:end] == text
    ):
        return None
    return start, end


def _issue_span(issue: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    evidence = issue.get("evidence")
    return _span(evidence, query) if isinstance(evidence, Mapping) else None


def _operator_symbols(spec: metric_registry.MetricSpec) -> frozenset[str]:
    symbols: set[str] = set()
    for token in spec.operators_allowed:
        try:
            symbol = OperatorNormalizer.normalize(token)
        except NormalizationError:
            continue
        if symbol in {">", ">=", "<", "<="}:
            symbols.add(symbol)
    return frozenset(symbols)


def _profile_scalar_specs() -> tuple[metric_registry.MetricSpec, ...]:
    """Return only metrics whose physical latest-snapshot route is complete."""

    try:
        registry = metric_registry.MetricRegistry.load()
        profile_metrics, _date_states = registry.profile_slot_vocab()
    except metric_registry.MetricSpecError:
        return ()
    eligible: list[metric_registry.MetricSpec] = []
    for spec in registry.all():
        declared = profile_metrics.get(spec.metric_id)
        source = declared.get("profile_source") if isinstance(declared, Mapping) else None
        if not (
            spec.semantic_type == "scalar"
            and spec.source is not None
            and spec.units is not None
            and isinstance(source, Mapping)
            and source.get("table") == spec.source.table
            and source.get("column") == spec.source.column
            and isinstance(source.get("grain_filter"), str)
            and source.get("grain_filter")
            and _operator_symbols(spec)
        ):
            continue
        eligible.append(spec)
    return tuple(eligible)


def _alias_candidates(
    query: str,
) -> list[tuple[metric_registry.MetricSpec, str, int, int]]:
    candidates: list[tuple[metric_registry.MetricSpec, str, int, int]] = []
    for spec in _profile_scalar_specs():
        aliases = sorted(
            {spec.name, *spec.aliases_nouns}, key=lambda item: (-len(item), item)
        )
        for alias in aliases:
            cursor = 0
            while (start := query.find(alias, cursor)) >= 0:
                end = start + len(alias)
                cursor = end
                candidates.append((spec, alias, start, end))
    return candidates


def _declared_issue_arguments(spec: metric_registry.MetricSpec) -> frozenset[str]:
    declared = spec.raw.get("issue_arguments")
    return frozenset(
        {spec.metric_id}
        | {
            item
            for item in declared or []
            if isinstance(item, str) and item.strip()
        }
    )


def _whole_phrase_matches(
    query: str,
    *,
    alias: str,
    alias_start: int,
    value_text: str,
    value_bounds: tuple[int, int],
    operator_text: str,
    operator_bounds: tuple[int, int],
) -> bool:
    """Accept one closed member predicate and no unowned prose remainder."""

    if alias_start != len(query) - len(query.lstrip()):
        return False
    alias_end = alias_start + len(alias)
    value_start, value_end = value_bounds
    operator_start, operator_end = operator_bounds
    if not (alias_end <= value_start < value_end <= operator_start < operator_end):
        return False
    between_metric_and_value = query[alias_end:value_start]
    between_value_and_operator = query[value_end:operator_start]
    if re.fullmatch(r"(?:이|가|은|는)?\s*", between_metric_and_value) is None:
        return False
    if re.fullmatch(r"\s*", between_value_and_operator) is None:
        return False
    if query[value_start:value_end] != value_text:
        return False
    if query[operator_start:operator_end] != operator_text:
        return False
    return _MEMBER_SUFFIX.fullmatch(query[operator_end:]) is not None


def synthesize_profile_metric_predicate(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: list[Any],
    semantic_plan: Any,
) -> ProfileMetricSynthesis | None:
    """Synthesize one declared scalar profile predicate, or fail closed."""

    if issue.get("code") != "unsupported_semantics":
        return None
    nodes = semantic_plan.get("nodes") if isinstance(semantic_plan, Mapping) else None
    if nodes != [] or len(literal_bindings) != 2:
        return None
    issue_bounds = _issue_span(issue, query)
    if issue_bounds is None:
        return None

    bindings = [binding for binding in literal_bindings if isinstance(binding, Mapping)]
    if len(bindings) != 2:
        return None
    durations = [binding for binding in bindings if binding.get("kind") == "duration"]
    operators = [
        binding for binding in bindings if binding.get("kind") == "comparison_operator"
    ]
    if len(durations) != 1 or len(operators) != 1:
        return None
    duration, comparison = durations[0], operators[0]
    duration_bounds, operator_bounds = _span(duration, query), _span(comparison, query)
    normalized_duration = duration.get("normalized")
    operator = comparison.get("normalized")
    if not (
        duration_bounds is not None
        and operator_bounds is not None
        and isinstance(normalized_duration, Mapping)
        # The generic duration extractor uses rolling_duration as its default
        # even without a temporal cue.  Past-point/boundary kinds are explicit
        # time semantics and can never be a scalar threshold here.
        and normalized_duration.get("temporal_kind") in (None, "", "rolling_duration")
        and isinstance(normalized_duration.get("value"), (int, float))
        and not isinstance(normalized_duration.get("value"), bool)
        and normalized_duration.get("value") >= 0
        and isinstance(operator, str)
    ):
        return None

    metric_candidates = _alias_candidates(query)
    metric_ids = {spec.metric_id for spec, _alias, _start, _end in metric_candidates}
    if len(metric_ids) != 1:
        return None
    metric_id = next(iter(metric_ids))
    candidates = [item for item in metric_candidates if item[0].metric_id == metric_id]
    spec, alias, alias_start, alias_end = min(
        candidates, key=lambda item: (-(item[3] - item[2]), item[2], item[1])
    )

    issue_start, issue_end = issue_bounds
    issue_owns_alias = issue_start <= alias_start and alias_end <= issue_end
    issue_owns_literals = (
        issue_start <= duration_bounds[0]
        and duration_bounds[1] <= issue_end
        and issue_start <= operator_bounds[0]
        and operator_bounds[1] <= issue_end
    )
    if (
        str(issue.get("argument") or "") not in _declared_issue_arguments(spec)
        or not (issue_owns_alias or issue_owns_literals)
    ):
        return None

    binding_unit = normalized_duration.get("semantic_unit")
    declared_unit = spec.units.canonical if spec.units is not None else None
    if (
        event_ir.canonical_unit(binding_unit) is None
        or event_ir.canonical_unit(binding_unit) != event_ir.canonical_unit(declared_unit)
        or operator not in _operator_symbols(spec)
        or not _whole_phrase_matches(
            query,
            alias=alias,
            alias_start=alias_start,
            value_text=str(duration.get("text")),
            value_bounds=duration_bounds,
            operator_text=str(comparison.get("text")),
            operator_bounds=operator_bounds,
        )
    ):
        return None

    threshold = normalized_duration["value"]
    source_start, source_end = alias_start, operator_bounds[1]
    digest_material = "\0".join(
        (
            query,
            metric_id,
            str(duration.get("id") or ""),
            str(comparison.get("id") or ""),
            str(threshold),
            operator,
        )
    )
    node_id = "profile-metric-" + hashlib.sha256(
        digest_material.encode("utf-8")
    ).hexdigest()[:12]
    node = {
        "id": node_id,
        "type": "predicate",
        "source_span": query[source_start:source_end],
        "source_start": source_start,
        "source_end": source_end,
        "subject": "member",
        "metric": metric_id,
        "operator": operator,
        # Use the application-owned number, not the ambiguous surface "30일".
        "value": threshold,
        "unit": binding_unit,
        "producer": OWNER,
    }
    receipt = {
        "owner": OWNER,
        "node_id": node_id,
        "metric_id": metric_id,
        "metric_alias": alias,
        "source": {
            "table": spec.source.table if spec.source is not None else None,
            "column": spec.source.column if spec.source is not None else None,
            "snapshot_filter": spec.raw.get("targeting", {}).get("grain_filter"),
        },
        "threshold": {
            "binding_id": duration.get("id"),
            "value": threshold,
            "unit": binding_unit,
            "start": duration_bounds[0],
            "end": duration_bounds[1],
        },
        "comparison": {
            "binding_id": comparison.get("id"),
            "operator": operator,
            "start": operator_bounds[0],
            "end": operator_bounds[1],
        },
        "consumed_literal_binding_ids": [duration.get("id"), comparison.get("id")],
        "issue": {
            "code": issue.get("code"),
            "argument": issue.get("argument"),
            "start": issue_bounds[0],
            "end": issue_bounds[1],
            "ownership": (
                "metric_alias" if issue_owns_alias else "threshold_and_operator"
            ),
        },
    }
    return ProfileMetricSynthesis(node=node, receipt=receipt)


__all__ = [
    "OWNER",
    "ProfileMetricSynthesis",
    "synthesize_profile_metric_predicate",
]
