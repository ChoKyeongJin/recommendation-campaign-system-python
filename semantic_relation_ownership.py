"""Strict ownership bridges for mixed Event IR + semantic relation plans.

The structuring model may split one request across ``audience_requirement``
and a ``relation_predicate``.  This module contains the small amount of
application-owned evidence joining needed to validate that split without
letting a broad history node hide an unrelated audience condition.
"""

from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from collections.abc import Iterable, Mapping, MutableMapping
from datetime import date, timedelta
from typing import Any

import plan_schema
import targeting_domain
import temporal_semantics


class RelationOwnershipError(ValueError):
    """A relation node cannot be grounded to one exact source span."""


def _relation_nodes(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    plan = payload.get("semantic_plan")
    nodes = plan.get("nodes") if isinstance(plan, Mapping) else None
    return [
        node for node in (nodes or ())
        if isinstance(node, Mapping) and node.get("type") == "relation_predicate"
    ]


def normalize_relation_node_spans(payload: MutableMapping[str, Any], query: str) -> None:
    """Repair a wrong model offset only when ``source_span`` occurs once.

    Correct offsets remain valid even when the same text occurs elsewhere: in
    that case the coordinates themselves disambiguate the claim.  An invalid
    or missing coordinate with zero/multiple occurrences fails closed.
    """
    for raw in _relation_nodes(payload):
        if not isinstance(raw, MutableMapping):
            continue
        text = raw.get("source_span")
        start, end = raw.get("source_start"), raw.get("source_end")
        if not isinstance(text, str) or not text:
            raise RelationOwnershipError("relation_predicate.source_span must be non-empty")
        if (
            isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and 0 <= start < end <= len(query) and query[start:end] == text
        ):
            continue
        positions: list[int] = []
        cursor = 0
        while (position := query.find(text, cursor)) >= 0:
            positions.append(position)
            cursor = position + 1
        if len(positions) != 1:
            raise RelationOwnershipError(
                "relation_predicate.source_span must identify exactly one source occurrence"
            )
        raw["source_start"] = positions[0]
        raw["source_end"] = positions[0] + len(text)

    bindings = payload.get("literal_bindings")
    bindings = bindings if isinstance(bindings, list) else []
    for raw in _relation_nodes(payload):
        bounds = _node_span(raw, query)
        if bounds is None:
            continue
        owned_windows = [
            binding for binding in bindings
            if isinstance(binding, Mapping)
            and binding.get("kind") == "date_window"
            and isinstance(binding.get("start"), int)
            and isinstance(binding.get("end"), int)
            and bounds[0] <= binding["start"] < binding["end"] <= bounds[1]
            and isinstance(binding.get("normalized"), Mapping)
            and isinstance(binding["normalized"].get("event_ir_window"), Mapping)
        ]
        if len(owned_windows) == 1 and isinstance(raw, MutableMapping):
            # Dates are application-owned literals.  The model's calendar
            # wrapper is replaced by the already-normalized Event IR window.
            raw["period"] = dict(owned_windows[0]["normalized"]["event_ir_window"])
        elif len(owned_windows) > 1:
            raise RelationOwnershipError(
                "relation_predicate period is covered by multiple date windows"
            )


def _span(value: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    start, end = value.get("start"), value.get("end")
    if (
        isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
    ):
        return start, end
    return None


def _node_span(node: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    text = node.get("source_span")
    start, end = node.get("source_start"), node.get("source_end")
    if (
        node.get("id") and node.get("type") == "relation_predicate"
        and node.get("subject") and node.get("relation")
        and isinstance(text, str)
        and isinstance(start, int) and isinstance(end, int)
        and 0 <= start < end <= len(query) and query[start:end] == text
    ):
        return start, end
    return None


def _sections(catalog: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    fields = catalog.get("fields")
    metrics = catalog.get("metrics")
    domains = catalog.get("value_domains")
    return (
        fields if isinstance(fields, Mapping) else {},
        metrics if isinstance(metrics, Mapping) else {},
        domains if isinstance(domains, Mapping) else {},
    )


def _attribute_domains(node: Mapping[str, Any], catalog: Mapping[str, Any]) -> set[str]:
    fields, metrics, domains = _sections(catalog)
    attribute = str(node.get("attribute") or "")
    found: set[str] = set()
    if attribute in domains:
        found.add(attribute)
    if attribute.startswith("member_") and attribute.removeprefix("member_") in domains:
        found.add(attribute.removeprefix("member_"))
    field = fields.get(attribute)
    if isinstance(field, Mapping) and isinstance(field.get("value_domain"), str):
        found.add(str(field["value_domain"]))
    metric = metrics.get(attribute)
    if isinstance(metric, Mapping):
        for key in ("expression_field", "prev_expression_field"):
            declaration = fields.get(metric.get(key))
            if isinstance(declaration, Mapping) and isinstance(declaration.get("value_domain"), str):
                found.add(str(declaration["value_domain"]))
    return found


def _canonical_values(raw: Any, domain: Mapping[str, Any]) -> set[str]:
    normalized = "".join(str(raw or "").split()).casefold()
    if not normalized:
        return set()
    values = domain.get("values")
    if not isinstance(values, Mapping):
        return set()
    matched: set[str] = set()
    for canonical, declaration in values.items():
        aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
        terms = [canonical, *(aliases if isinstance(aliases, list) else [])]
        if normalized in {"".join(str(term).split()).casefold() for term in terms}:
            matched.add(str(canonical))
    if matched:
        return matched
    # Axis-qualified aliases may ground a bare transition endpoint after the
    # claim reconciler has already selected this domain (e.g. 가치등급 ... 골드).
    def qualified_match(term: str) -> bool:
        if term == normalized:
            return True
        if term.startswith(normalized):
            after = term[len(normalized):len(normalized) + 1]
            return not (
                normalized[-1].isascii()
                and after.isascii()
                and (after.isalnum() or after == "_")
            )
        if term.endswith(normalized):
            before = term[-len(normalized) - 1:-len(normalized)]
            return not (
                normalized[0].isascii()
                and before.isascii()
                and (before.isalnum() or before == "_")
            )
        return False

    for canonical, declaration in values.items():
        aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
        qualified = ["".join(str(term).split()).casefold() for term in aliases or ()]
        if any(qualified_match(term) for term in qualified):
            matched.add(str(canonical))
    return matched


def _source_term_hits(query: str, term: str) -> list[tuple[int, int]]:
    """Find a catalog term while treating Korean particles as boundaries.

    ``str.isalnum`` considers Korean particles alphanumeric, so the stricter
    cue matcher intentionally used by axis selection cannot find ``VIP`` in
    ``VIP로``.  Transition endpoints need that ordinary Korean spelling while
    still rejecting an ASCII identifier embedded in another ASCII word.
    """
    folded, needle = query.casefold(), term.strip().casefold()
    if not needle:
        return []

    def ascii_word(char: str) -> bool:
        return bool(char) and char.isascii() and (char.isalnum() or char == "_")

    hits: list[tuple[int, int]] = []
    cursor = 0
    while (start := folded.find(needle, cursor)) >= 0:
        end = start + len(needle)
        before = folded[start - 1] if start else ""
        after = folded[end] if end < len(folded) else ""
        if not (
            (needle[0].isascii() and ascii_word(before))
            or (needle[-1].isascii() and ascii_word(after))
        ):
            hits.append((start, end))
        cursor = start + max(1, len(needle))
    return hits


def _canonical_source_hits(
    query: str, canonical: str, domain: Mapping[str, Any]
) -> list[tuple[int, int]]:
    values = domain.get("values")
    declaration = values.get(canonical) if isinstance(values, Mapping) else None
    aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
    terms = [canonical, *(aliases if isinstance(aliases, list) else [])]
    hits = {
        hit
        for term in terms
        if isinstance(term, str)
        for hit in _source_term_hits(query, term)
    }
    # One canonical can have both a short and a qualified alias at the same
    # position.  The qualified occurrence is the useful grounding span.
    return sorted(
        hit for hit in hits
        if not any(
            other[0] <= hit[0] and hit[1] <= other[1] and other != hit
            for other in hits
        )
    )


_ATTRIBUTE_DIRECTION_BRIDGE_RE = re.compile(
    r"^\s*(?:(?:이|가|은|는|의|을|를)\s*)?$"
)


def _domain_axis_hits(
    query: str, domain_id: str, catalog: Mapping[str, Any]
) -> list[tuple[int, int]]:
    """Find catalog-declared attribute-axis terms for one value domain."""
    fields, metrics, _domains = _sections(catalog)
    terms: set[str] = set()
    domain_fields: set[str] = set()
    for field_id, declaration in fields.items():
        if not isinstance(declaration, Mapping) or declaration.get("value_domain") != domain_id:
            continue
        domain_fields.add(str(field_id))
        aliases = declaration.get("aliases")
        terms.update(
            str(term).strip()
            for term in (declaration.get("label"), *(aliases or ()))
            if isinstance(term, str) and term.strip()
        )
    for declaration in metrics.values():
        if not isinstance(declaration, Mapping) or not {
            str(declaration.get("expression_field") or ""),
            str(declaration.get("prev_expression_field") or ""),
        } & domain_fields:
            continue
        aliases = declaration.get("aliases")
        terms.update(
            str(term).strip()
            for term in (declaration.get("label"), *(aliases or ()))
            if isinstance(term, str) and term.strip()
        )
    hits = {
        hit
        for term in terms
        for hit in _source_term_hits(query, term)
    }
    return sorted(
        hit
        for hit in hits
        if not any(
            other[0] <= hit[0] and hit[1] <= other[1] and other != hit
            for other in hits
        )
    )


def _canonicalize_subject_scoped_field_leaf(
    raw: MutableMapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Expand one source-scoped field leaf to its full catalog id.

    Live relation nodes occasionally shorten
    ``member_month_snapshot.prev_grade`` to ``prev_grade``.  A leaf by itself
    is not globally authoritative: another source may declare the same leaf.
    Expand it only when the node names one real catalog source through
    ``subject``/``source`` and exactly one field on that source owns the leaf.
    Conflicting source hints, generic subjects, and duplicate leaves remain
    untouched so the existing compiler fails closed.
    """
    fields, _metrics, _domains = _sections(catalog)
    attribute = raw.get("attribute")
    if not isinstance(attribute, str):
        return
    leaf = attribute.strip()
    if not leaf or leaf in fields or "." in leaf:
        return

    catalog_sources = {
        str(declaration.get("source"))
        for declaration in fields.values()
        if isinstance(declaration, Mapping)
        and isinstance(declaration.get("source"), str)
        and str(declaration.get("source")).strip()
    }
    source_hints = {
        value.strip()
        for value in (raw.get("subject"), raw.get("source"))
        if isinstance(value, str)
        and value.strip() in catalog_sources
    }
    if len(source_hints) != 1:
        return
    source_id = next(iter(source_hints))
    normalized_leaf = leaf.casefold()
    candidates = [
        str(field_id)
        for field_id, declaration in fields.items()
        if isinstance(declaration, Mapping)
        and declaration.get("source") == source_id
        and str(field_id).rsplit(".", 1)[-1].casefold() == normalized_leaf
    ]
    if len(candidates) == 1:
        raw["attribute"] = candidates[0]


def _normalize_anchored_snapshot_metric(
    raw: MutableMapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Route a physical snapshot field to its unique declared temporal metric.

    A field id is a valid compiler symbol, but its automatically-derived metric
    has only the source's conservative default time binding.  The explicit
    application metric owns the richer snapshot binding and, importantly, its
    declared data-coverage window.  Live models sometimes emit the field id
    (``member_month_snapshot.grade``) where the relation node schema asks for a
    metric.  Select the richer owner only for an anchored ``AS_OF`` relation or
    a complete value-to-value ``CHANGE_BETWEEN`` relation, and only when one
    catalog declaration uniquely binds that exact field, supports the requested
    temporal operator, and names a real coverage declaration.  A transition
    additionally needs the explicit field metric to name its transition metric;
    this keeps a physical field without a declared previous-value binding closed.

    This does not infer an attribute from prose or invent transition endpoints.
    It merely makes the already-declared time/transition/coverage bindings
    reachable for a lossless field/metric wire variant.
    """
    temporal_operator = targeting_domain.temporal_operator_of(raw.get("relation"))
    anchored_as_of = (
        temporal_operator == temporal_semantics.AS_OF
        and _period_bounds(raw.get("period")) is not None
    )
    complete_transition = (
        temporal_operator == temporal_semantics.CHANGE_BETWEEN
        and raw.get("from_value") not in (None, "")
        and raw.get("to_value") not in (None, "")
    )
    if not (anchored_as_of or complete_transition):
        return
    _fields, metrics, _domains = _sections(catalog)
    attribute = raw.get("attribute")
    if not isinstance(attribute, str):
        return
    normalized_attribute = "".join(attribute.split()).casefold()
    times = catalog.get("times")
    coverage = catalog.get("data_coverage")
    if not isinstance(times, Mapping) or not isinstance(coverage, Mapping):
        return
    candidates: list[str] = []
    for metric_id, declaration in metrics.items():
        aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else None
        terms = [metric_id, *(aliases if isinstance(aliases, list) else [])]
        owns_attribute = (
            isinstance(declaration, Mapping)
            and (
                declaration.get("expression_field") == attribute
                or normalized_attribute
                in {"".join(str(term).split()).casefold() for term in terms}
            )
        )
        if not (
            isinstance(declaration, Mapping)
            and declaration.get("kind") == "field"
            and owns_attribute
        ):
            continue
        time_id = declaration.get("time")
        time = times.get(time_id) if isinstance(time_id, str) else None
        coverage_id = declaration.get("coverage")
        if not (
            isinstance(time, Mapping)
            and temporal_operator in (time.get("temporal_operators") or ())
            and isinstance(coverage_id, str)
            and coverage_id in coverage
        ):
            continue
        if complete_transition:
            transition_id = declaration.get("transition_metric")
            transition = metrics.get(transition_id) if isinstance(transition_id, str) else None
            if not (
                isinstance(transition, Mapping)
                and transition.get("kind") == "transition"
                and transition.get("expression_field") == declaration.get("expression_field")
                and transition.get("prev_expression_field")
            ):
                continue
        candidates.append(str(metric_id))
    if len(candidates) == 1:
        raw["attribute"] = candidates[0]


_PREVIOUS_VALUE_BRIDGE_RE = re.compile(r"^\s*(?:(?:이|가|은|는|의)\s*)?$")
_RELATION_ONLY_AUDIENCE_TAIL_RE = re.compile(
    r"^(?:이었|였)?던?\s*회원(?:을?\s*(?:찾아줘|보여줘|추출해줘))?[.!?]?\s*$"
)


def _normalize_immediately_preceding_field(
    raw: MutableMapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> None:
    """Turn an exact previous-field AS_OF wire variant into a transition claim.

    ``직전 등급이 골드였던`` is occasionally emitted as ``prev_grade`` plus
    ``as_of``.  The previous field and its transition owner are both declared
    by the catalog.  We rewrite only when one IMMEDIATELY_PRECEDING marker, one
    transition metric, and one source-grounded value agree.  The resulting
    from-only transition follows the existing PREV_* execution path.
    """
    if (
        targeting_domain.temporal_operator_of(raw.get("relation"))
        != temporal_semantics.AS_OF
        or raw.get("period") not in (None, "", {})
    ):
        return
    fields, metrics, domains = _sections(catalog)
    attribute = raw.get("attribute")
    field = fields.get(attribute) if isinstance(attribute, str) else None
    domain_id = field.get("value_domain") if isinstance(field, Mapping) else None
    domain = domains.get(domain_id) if isinstance(domain_id, str) else None
    if not isinstance(domain, Mapping):
        return
    transitions = [
        declaration
        for declaration in metrics.values()
        if isinstance(declaration, Mapping)
        and declaration.get("kind") == "transition"
        and declaration.get("prev_expression_field") == attribute
    ]
    if len(transitions) != 1:
        return
    transition = transitions[0]
    current_field = transition.get("expression_field")
    owners = [
        str(metric_id)
        for metric_id, declaration in metrics.items()
        if isinstance(declaration, Mapping)
        and declaration.get("kind") == "field"
        and declaration.get("expression_field") == current_field
    ]
    if len(owners) != 1:
        return
    bounds = _node_span(raw, query)
    if bounds is None:
        return
    markers = [
        marker
        for marker in targeting_domain.temporal_lexicon().detect(query)
        if marker.operator == temporal_semantics.IMMEDIATELY_PRECEDING
        and bounds[0] <= marker.start < marker.end <= bounds[1]
    ]
    values = _canonical_values(raw.get("value"), domain)
    if len(markers) != 1 or len(values) != 1:
        return
    canonical = next(iter(values))
    hits = [
        hit
        for hit in _canonical_source_hits(query, canonical, domain)
        if markers[0].end <= hit[0]
        and hit[0] - markers[0].end <= 16
        and _PREVIOUS_VALUE_BRIDGE_RE.fullmatch(query[markers[0].end:hit[0]])
    ]
    if len(hits) != 1:
        return
    raw["attribute"] = owners[0]
    raw["relation"] = "transition"
    raw["from_value"] = canonical
    raw["to_value"] = None
    raw["value"] = None
    end = max(bounds[1], hits[0][1])
    if _RELATION_ONLY_AUDIENCE_TAIL_RE.fullmatch(query[end:]):
        end = len(query)
    raw["source_start"] = bounds[0]
    raw["source_end"] = end
    raw["source_span"] = query[bounds[0]:end]


def _normalize_directional_transition(
    raw: MutableMapping[str, Any],
    query: str,
    domain_id: str,
    catalog: Mapping[str, Any],
) -> None:
    """Ground a bare directional cue and its immediately attached axis.

    Live models sometimes emit ``source_span='승급'`` and leave ``value`` null.
    The direction vocabulary is domain-owned, and the axis must be immediately
    adjacent (``등급이 승급``).  A purchase clause or any other phrase between
    the axis and cue prevents this normalization.
    """
    if raw.get("relation") != "transition" or any(
        raw.get(key) not in (None, "") for key in ("from_value", "to_value")
    ):
        return
    bounds = _node_span(raw, query)
    if bounds is None:
        return
    markers = [
        marker
        for marker in targeting_domain.temporal_lexicon().detect(query)
        if marker.operator == temporal_semantics.CHANGE_BETWEEN
        and bounds[0] <= marker.start < marker.end <= bounds[1]
        and targeting_domain.transition_direction(marker.text) is not None
    ]
    if len(markers) != 1:
        return
    marker = markers[0]
    axis_hits = [
        hit
        for hit in _domain_axis_hits(query, domain_id, catalog)
        if hit[1] <= marker.start
        and marker.start - hit[1] <= 8
        and _ATTRIBUTE_DIRECTION_BRIDGE_RE.fullmatch(query[hit[1]:marker.start])
    ]
    if len(axis_hits) != 1:
        return
    axis = axis_hits[0]
    raw["value"] = marker.text
    raw["source_start"] = min(bounds[0], axis[0])
    raw["source_end"] = max(bounds[1], marker.end)
    raw["source_span"] = query[raw["source_start"]:raw["source_end"]]


def _comparison_field_literal(
    value: Any, query: str
) -> tuple[str, Any, tuple[int, int]] | None:
    if not isinstance(value, Mapping) or value.get("type") != "comparison":
        return None
    if value.get("operator") != "=":
        return None
    left, right = value.get("left"), value.get("right")
    if not (
        isinstance(left, Mapping)
        and left.get("type") == "field"
        and isinstance(left.get("name"), str)
        and isinstance(right, Mapping)
        and right.get("type") == "literal"
    ):
        return None
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    start, end, text = evidence.get("start"), evidence.get("end"), evidence.get("text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and isinstance(text, str)
        and query[start:end] == text
    ):
        return None
    return str(left["name"]), right.get("value"), (start, end)


def promote_snapshot_as_of_expression(
    payload: MutableMapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Move one fully grounded snapshot equality to its temporal owner.

    A live structurer variant emitted ``member_month_snapshot.grade =
    gold_grade`` as Event IR while leaving ``semantic_plan.nodes`` empty.  The
    comparison itself is unambiguous, but Event IR cannot own the adjacent
    ``이번 달 기준`` anchor, so validation retried and the repair response
    discarded the comparison.  This bridge accepts only the closed, refutable
    shape: one equality, one declared snapshot field owner, one catalog value,
    one calendar window, one AS_OF marker, and no unclaimed query text.
    """

    requirement = payload.get("audience_requirement")
    if not isinstance(requirement, MutableMapping) or requirement.get("issues") != []:
        return None
    expression = requirement.get("expression")
    if not isinstance(expression, Mapping) or set(expression) - {
        "type", "operator", "left", "right", "evidence",
    }:
        return None
    left, right = expression.get("left"), expression.get("right")
    if not (
        isinstance(left, Mapping)
        and set(left) == {"type", "name"}
        and isinstance(right, Mapping)
        and set(right) == {"type", "value"}
    ):
        return None
    parsed = _comparison_field_literal(expression, query)
    if parsed is None:
        return None

    semantic_plan = payload.get("semantic_plan")
    nodes = semantic_plan.get("nodes") if isinstance(semantic_plan, MutableMapping) else None
    if not isinstance(nodes, list) or nodes:
        return None
    if payload.get("intent") not in {"find_user_segment", "recommend_campaign"}:
        return None
    if payload.get("result_limit") is not None:
        return None
    def has_claim_value(value: Any) -> bool:
        if value is None or value is False or value == "":
            return False
        if isinstance(value, Mapping):
            return any(has_claim_value(child) for child in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(has_claim_value(child) for child in value)
        return True

    if any(
        has_claim_value(payload.get(container))
        for container in ("target_user", "exclude", "campaign_constraints")
    ):
        return None
    allowed_condition_keys = {
        "audience_requirement",
        "intent",
        "result_limit",
        "semantic_plan",
    }
    if any(
        key not in allowed_condition_keys and has_claim_value(payload.get(key))
        for key in plan_schema.names(plan_schema.CONDITION)
    ) or has_claim_value(payload.get("unresolved")):
        return None

    field_id, raw_value, evidence_bounds = parsed
    fields, metrics, domains = _sections(catalog)
    times = catalog.get("times")
    coverages = catalog.get("data_coverage")
    field = fields.get(field_id)
    if not all(
        isinstance(section, Mapping)
        for section in (field, times, coverages)
    ):
        return None
    owners: list[tuple[str, Mapping[str, Any]]] = []
    for metric_id, declaration in metrics.items():
        if not (
            isinstance(declaration, Mapping)
            and declaration.get("kind") == "field"
            and declaration.get("expression_field") == field_id
            and declaration.get("source") == field.get("source")
            and "=" in (declaration.get("allowed_operators") or ())
        ):
            continue
        time_id = declaration.get("time")
        time = times.get(time_id) if isinstance(time_id, str) else None
        coverage_id = declaration.get("coverage")
        if (
            isinstance(time, Mapping)
            and temporal_semantics.AS_OF in (time.get("temporal_operators") or ())
            and isinstance(coverage_id, str)
            and coverage_id in coverages
        ):
            owners.append((str(metric_id), declaration))
    if len(owners) != 1:
        return None
    metric_id, _owner = owners[0]

    domain_id = field.get("value_domain")
    domain = domains.get(domain_id) if isinstance(domain_id, str) else None
    if not isinstance(domain, Mapping):
        return None
    canonical_values = _canonical_values(raw_value, domain)
    if len(canonical_values) != 1:
        return None
    canonical = next(iter(canonical_values))

    bindings = payload.get("literal_bindings")
    if not isinstance(bindings, list) or len(bindings) != 1:
        return None
    binding = bindings[0]
    normalized = binding.get("normalized") if isinstance(binding, Mapping) else None
    period = normalized.get("event_ir_window") if isinstance(normalized, Mapping) else None
    binding_bounds = _span(binding, query) if isinstance(binding, Mapping) else None
    period_bounds = _period_bounds(period)
    if not (
        isinstance(binding, Mapping)
        and binding.get("kind") == "date_window"
        and binding_bounds is not None
        and isinstance(period, Mapping)
        and period.get("type") == "interval"
        and period_bounds is not None
        and period_bounds[0].day == 1
        and period_bounds[0].year == period_bounds[1].year
        and period_bounds[0].month == period_bounds[1].month
        and period_bounds[1].day == monthrange(
            period_bounds[1].year, period_bounds[1].month
        )[1]
    ):
        return None

    # ``이번 달 기준`` is intentionally recognized only inside this closed
    # promotion.  Adding it to the shared history lexicon makes a purchase
    # period such as ``이번 달 기준 구매금액 ... VIP 상품`` look like a grade
    # AS_OF claim merely because an unrelated grade-shaped token occurs later.
    marker_match = re.match(
        r"\s*(?:말\s*)?기준(?=\s|$)", query[binding_bounds[1]:]
    )
    if marker_match is None:
        return None
    marker_start = binding_bounds[0]
    marker_end = binding_bounds[1] + marker_match.end()
    value_hits = [
        hit
        for hit in _canonical_source_hits(query, canonical, domain)
        if evidence_bounds[0] <= hit[0] < hit[1] <= evidence_bounds[1]
    ]
    axis_hits = [
        hit
        for hit in _domain_axis_hits(query, str(domain_id), catalog)
        if evidence_bounds[0] <= hit[0] < hit[1] <= evidence_bounds[1]
    ]
    if len(value_hits) != 1 or len(axis_hits) != 1:
        return None
    value_hit, axis_hit = value_hits[0], axis_hits[0]
    query_start = len(query) - len(query.lstrip())
    query_end = len(query.rstrip())
    if not (
        marker_start == binding_bounds[0] == query_start
        and binding_bounds[1] <= marker_end <= value_hit[0]
        and query[marker_end:value_hit[0]].strip() == ""
        and value_hit[0] <= axis_hit[0] < axis_hit[1] <= value_hit[1]
        and evidence_bounds[0] == value_hit[0]
        and evidence_bounds[1] in {value_hit[1], query_end}
        and _RELATION_ONLY_AUDIENCE_TAIL_RE.fullmatch(query[value_hit[1]:query_end])
    ):
        return None

    node = {
        "id": f"as-of-{hashlib.sha256(query.encode('utf-8')).hexdigest()[:12]}",
        "type": "relation_predicate",
        "source_span": query[query_start:query_end],
        "source_start": query_start,
        "source_end": query_end,
        "confidence": 1.0,
        "subject": "member",
        "attribute": metric_id,
        "relation": "as_of",
        "value": canonical,
        "value_comparison": "eq",
        "period": dict(period),
    }
    nodes.append(node)
    requirement["expression"] = None
    requirement["issues"] = []
    return {
        "field": field_id,
        "metric": metric_id,
        "value": canonical,
        "period": dict(period),
        "node_id": node["id"],
    }


def _physical_value_hits(
    query: str,
    canonical: str,
    domain: Mapping[str, Any],
    all_domains: Mapping[str, Any],
) -> list[tuple[int, int]]:
    """Ground a selected-axis endpoint through catalog-equivalent surfaces."""
    values = domain.get("values")
    declaration = values.get(canonical) if isinstance(values, Mapping) else None
    if not isinstance(declaration, Mapping):
        return []
    physical = str(declaration.get("physical") or "").casefold().rsplit(".", 1)[-1]
    terms: set[str] = {canonical}
    for other_domain in all_domains.values():
        other_values = other_domain.get("values") if isinstance(other_domain, Mapping) else None
        if not isinstance(other_values, Mapping):
            continue
        for other_canonical, other in other_values.items():
            if not isinstance(other, Mapping):
                continue
            other_physical = (
                str(other.get("physical") or "").casefold().rsplit(".", 1)[-1]
            )
            if physical and other_physical != physical:
                continue
            if not physical and str(other_canonical) != canonical:
                continue
            aliases = other.get("aliases")
            terms.update(
                str(term)
                for term in (other_canonical, other.get("physical"), *(aliases or ()))
                if isinstance(term, str) and term
            )
    hits = {hit for term in terms for hit in _source_term_hits(query, term)}
    return sorted(
        hit
        for hit in hits
        if not any(
            other[0] <= hit[0] and hit[1] <= other[1] and other != hit
            for other in hits
        )
    )


def promote_snapshot_transition_expression(
    payload: MutableMapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Move one model-proposed snapshot transition to its canonical owner.

    This adapter is intentionally limited to a mixed ``And`` expression with
    one independently executable Event IR condition plus one exact previous /
    current snapshot equality pair.  Field identity selects a declared
    transition metric; source evidence must contain that domain's axis, both
    catalog-grounded endpoints, and one ordered change marker.  The two
    snapshot comparisons are removed as the relation node is added, so the
    same condition never has two authorities.
    """
    requirement = payload.get("audience_requirement")
    if not isinstance(requirement, MutableMapping) or requirement.get("issues"):
        return []
    expression = requirement.get("expression")
    if not isinstance(expression, MutableMapping) or expression.get("type") != "and":
        return []
    operands = expression.get("operands")
    if not isinstance(operands, list) or len(operands) < 3:
        return []
    plan = payload.get("semantic_plan")
    nodes = plan.get("nodes") if isinstance(plan, MutableMapping) else None
    if not isinstance(nodes, list) or any(
        isinstance(node, Mapping) and node.get("type") == "relation_predicate"
        for node in nodes
    ):
        return []

    fields, metrics, domains = _sections(catalog)
    parsed = [_comparison_field_literal(operand, query) for operand in operands]
    candidates: list[dict[str, Any]] = []
    for metric_id, declaration in metrics.items():
        if not isinstance(declaration, Mapping) or declaration.get("kind") != "transition":
            continue
        current_field = str(declaration.get("expression_field") or "")
        previous_field = str(declaration.get("prev_expression_field") or "")
        current_spec, previous_spec = fields.get(current_field), fields.get(previous_field)
        if not isinstance(current_spec, Mapping) or not isinstance(previous_spec, Mapping):
            continue
        domain_id = current_spec.get("value_domain")
        if not isinstance(domain_id, str) or previous_spec.get("value_domain") != domain_id:
            continue
        domain = domains.get(domain_id)
        if not isinstance(domain, Mapping):
            continue
        previous = [
            (index, item) for index, item in enumerate(parsed)
            if item is not None and item[0] == previous_field
        ]
        current = [
            (index, item) for index, item in enumerate(parsed)
            if item is not None and item[0] == current_field
        ]
        if len(previous) != 1 or len(current) != 1:
            continue
        previous_index, previous_item = previous[0]
        current_index, current_item = current[0]
        if previous_index == current_index:
            continue
        from_values = _canonical_values(previous_item[1], domain)
        to_values = _canonical_values(current_item[1], domain)
        if len(from_values) != 1 or len(to_values) != 1:
            continue
        from_value, to_value = next(iter(from_values)), next(iter(to_values))
        if from_value == to_value:
            continue
        from_hits = [
            hit
            for hit in _physical_value_hits(query, from_value, domain, domains)
            if previous_item[2][0] <= hit[0] < hit[1] <= previous_item[2][1]
        ]
        to_hits = [
            hit
            for hit in _physical_value_hits(query, to_value, domain, domains)
            if current_item[2][0] <= hit[0] < hit[1] <= current_item[2][1]
        ]
        if len(from_hits) != 1 or len(to_hits) != 1 or from_hits[0][1] > to_hits[0][0]:
            continue
        start = min(previous_item[2][0], current_item[2][0])
        end = max(previous_item[2][1], current_item[2][1])
        axis_hits = [
            hit for hit in _domain_axis_hits(query, domain_id, catalog)
            if start <= hit[0] < hit[1] <= end and hit[1] <= from_hits[0][0]
        ]
        markers = [
            marker
            for marker in targeting_domain.temporal_lexicon().detect(query)
            if marker.operator == temporal_semantics.CHANGE_BETWEEN
            and start <= marker.start
            and marker.end <= end
            and marker.start <= from_hits[0][0]
            and to_hits[0][1] <= marker.end
        ]
        if len(axis_hits) != 1 or len(markers) != 1:
            continue
        remaining = [
            operand for index, operand in enumerate(operands)
            if index not in {previous_index, current_index}
        ]
        if not remaining:
            continue
        candidates.append({
            "metric_id": str(metric_id),
            "from_value": from_value,
            "to_value": to_value,
            "source_start": min(axis_hits[0][0], markers[0].start, start),
            "source_end": max(markers[0].end, end),
            "removed": {previous_index, current_index},
            "remaining": remaining,
        })
    if len(candidates) != 1:
        return []
    selected = candidates[0]
    start, end = selected["source_start"], selected["source_end"]
    digest = hashlib.sha256(
        f"{query}\0{start}\0{end}\0{selected['metric_id']}".encode()
    ).hexdigest()[:12]
    node = {
        "id": f"semantic-relation-{digest}",
        "type": "relation_predicate",
        "source_span": query[start:end],
        "source_start": start,
        "source_end": end,
        "subject": "member",
        "attribute": selected["metric_id"],
        "relation": "transition",
        "from_value": selected["from_value"],
        "to_value": selected["to_value"],
    }
    nodes.append(node)
    remaining = selected["remaining"]
    requirement["expression"] = (
        remaining[0] if len(remaining) == 1 else {"type": "and", "operands": remaining}
    )
    return [{
        "node_id": node["id"],
        "metric_id": selected["metric_id"],
        "from_value": selected["from_value"],
        "to_value": selected["to_value"],
        "source_span": node["source_span"],
    }]


def normalize_relation_node_claims(
    payload: MutableMapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> None:
    """Normalize uniquely attributable relation endpoints from the catalog.

    The model sometimes emits a valid canonical value from the wrong sibling
    domain (``vip`` for a worth-grade transition) or gives only the relation
    cue (``승급``) as the node span.  The attribute selects one catalog domain;
    only a single canonical endpoint match is rewritten.  A narrow transition
    span is expanded only when one ordered source pair grounds both endpoints
    in the same punctuation-delimited clause.  Ambiguous repetitions remain
    untouched and are rejected by the existing claim checks.
    """
    _fields, _metrics, domains = _sections(catalog)
    for raw in _relation_nodes(payload):
        if not isinstance(raw, MutableMapping):
            continue
        _canonicalize_subject_scoped_field_leaf(raw, catalog)
        _normalize_immediately_preceding_field(raw, query, catalog)
        _normalize_anchored_snapshot_metric(raw, catalog)
        domain_ids = _attribute_domains(raw, catalog)
        if len(domain_ids) != 1:
            continue
        domain_id = next(iter(domain_ids))
        domain = domains.get(domain_id)
        if not isinstance(domain, Mapping):
            continue
        _normalize_directional_transition(raw, query, domain_id, catalog)

        canonical: dict[str, str] = {}
        for key in ("value", "from_value", "to_value"):
            if raw.get(key) is None:
                continue
            matches = _canonical_values(raw.get(key), domain)
            if len(matches) != 1:
                continue
            canonical[key] = next(iter(matches))
            raw[key] = canonical[key]

        if (
            raw.get("relation") != "transition"
            or "from_value" not in canonical
            or "to_value" not in canonical
        ):
            continue
        node_bounds = _node_span(raw, query)
        if node_bounds is None:
            continue

        separators = ",.;!?\n"
        clause_start = max((query.rfind(mark, 0, node_bounds[0]) for mark in separators), default=-1) + 1
        clause_ends = [
            position
            for mark in separators
            if (position := query.find(mark, node_bounds[1])) >= 0
        ]
        clause_end = min(clause_ends) if clause_ends else len(query)
        from_hits = [
            hit for hit in _canonical_source_hits(query, canonical["from_value"], domain)
            if clause_start <= hit[0] < hit[1] <= clause_end
        ]
        to_hits = [
            hit for hit in _canonical_source_hits(query, canonical["to_value"], domain)
            if clause_start <= hit[0] < hit[1] <= clause_end
        ]
        candidates = [
            (from_hit, to_hit)
            for from_hit in from_hits
            for to_hit in to_hits
            if from_hit[1] <= to_hit[0]
            and max(node_bounds[1], to_hit[1]) - min(node_bounds[0], from_hit[0]) <= 64
        ]
        if len(candidates) != 1:
            continue
        from_hit, to_hit = candidates[0]
        start = min(node_bounds[0], from_hit[0], to_hit[0])
        end = max(node_bounds[1], from_hit[1], to_hit[1])
        raw["source_start"] = start
        raw["source_end"] = end
        raw["source_span"] = query[start:end]


def semantic_plan_owns_entire_audience(
    payload: Mapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> bool:
    """Whether one grounded relation node accounts for the entire request.

    This is an ingress deferral receipt, not an execution receipt.  The graph
    pipeline must still lower the node before SQL can be emitted.
    """
    nodes = _relation_nodes(payload)
    if len(nodes) != 1:
        return False
    node = nodes[0]
    bounds = _node_span(node, query)
    if bounds is None or not _attribute_domains(node, catalog):
        return False
    start = len(query) - len(query.lstrip())
    end = len(query.rstrip())
    return (
        bounds == (start, end)
        and targeting_domain.temporal_operator_of(node.get("relation")) is not None
    )


def _attribute_coverage(
    node: Mapping[str, Any], catalog: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]] | None:
    """Resolve one declared coverage owner without guessing an attribute."""
    fields, metrics, domains = _sections(catalog)
    sources = catalog.get("sources")
    times = catalog.get("times")
    coverages = catalog.get("data_coverage")
    if not all(isinstance(section, Mapping) for section in (sources, times, coverages)):
        return None
    attribute = str(node.get("attribute") or "")
    normalized = "".join(attribute.split()).casefold()

    selected: list[Mapping[str, Any]] = []
    for metric_id, declaration in metrics.items():
        if not isinstance(declaration, Mapping):
            continue
        aliases = declaration.get("aliases")
        terms = [metric_id, *(aliases if isinstance(aliases, list) else [])]
        if normalized in {"".join(str(term).split()).casefold() for term in terms}:
            selected.append(declaration)
    if not selected and attribute in fields:
        selected = [
            declaration
            for declaration in metrics.values()
            if isinstance(declaration, Mapping)
            and attribute
            in {
                declaration.get("expression_field"),
                declaration.get("prev_expression_field"),
            }
        ]
    if not selected and attribute in sources:
        selected = [
            declaration
            for declaration in metrics.values()
            if isinstance(declaration, Mapping)
            and declaration.get("source") == attribute
        ]
    if not selected and attribute in domains:
        domain_fields = {
            field_id
            for field_id, declaration in fields.items()
            if isinstance(declaration, Mapping)
            and declaration.get("value_domain") == attribute
        }
        selected = [
            declaration
            for declaration in metrics.values()
            if isinstance(declaration, Mapping)
            and {
                declaration.get("expression_field"),
                declaration.get("prev_expression_field"),
            }
            & domain_fields
        ]

    coverage_ids: set[str] = set()
    for declaration in selected:
        coverage_id = declaration.get("coverage")
        if isinstance(coverage_id, str) and coverage_id in coverages:
            coverage_ids.add(coverage_id)
        time_id = declaration.get("time")
        time = times.get(time_id) if isinstance(time_id, str) else None
        time_coverage = time.get("coverage") if isinstance(time, Mapping) else None
        if isinstance(time_coverage, str) and time_coverage in coverages:
            coverage_ids.add(time_coverage)
    if len(coverage_ids) != 1:
        return None
    coverage_id = next(iter(coverage_ids))
    declaration = coverages.get(coverage_id)
    return (
        (coverage_id, declaration)
        if isinstance(declaration, Mapping)
        else None
    )


def _coverage_date(raw: Any, *, end: bool = False) -> date | None:
    text = str(raw or "").strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        if re.fullmatch(r"\d{6}", text):
            year, month = int(text[:4]), int(text[4:6])
            return date(year, month, monthrange(year, month)[1] if end else 1)
        return date.fromisoformat(text)
    except ValueError:
        return None


def _period_bounds(period: Any) -> tuple[date, date] | None:
    if not isinstance(period, Mapping):
        return None
    kind = str(period.get("type") or "")
    if kind == "interval":
        start = _coverage_date(period.get("start"))
        exclusive = _coverage_date(period.get("end_exclusive"))
        return (
            (start, exclusive - timedelta(days=1))
            if start and exclusive and start < exclusive
            else None
        )
    if kind == "absolute" or (
        not kind and period.get("from") is not None and period.get("to") is not None
    ):
        # ``semantic_pipeline.normalize_plan`` serializes a typed Period back
        # to the compiler's inclusive ``{from,to}`` window and intentionally
        # drops its wrapper type.  Coverage projection runs both before and
        # after that rebuild, so it must understand the normalized shape too.
        start = _coverage_date(period.get("from"))
        end = _coverage_date(period.get("to"), end=True)
        return (start, end) if start and end and start <= end else None
    if kind == "calendar_month":
        year, month = period.get("year"), period.get("month")
        if (
            isinstance(year, int)
            and not isinstance(year, bool)
            and isinstance(month, int)
            and not isinstance(month, bool)
            and 1 <= month <= 12
        ):
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    return None


def _available_months(coverage: Mapping[str, Any]) -> int | None:
    start = _coverage_date(coverage.get("from"))
    end = _coverage_date(coverage.get("to"), end=True)
    if start is None or end is None or start > end:
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _required_observation_months(node: Mapping[str, Any], operator: str) -> int | None:
    months = node.get("months")
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        period = node.get("period")
        if (
            isinstance(period, Mapping)
            and period.get("type") == "relative"
            and str(period.get("unit") or "") in {"month", "months"}
            and isinstance(period.get("value"), int)
            and not isinstance(period.get("value"), bool)
        ):
            months = int(period["value"])
        else:
            months = None
    if operator in {
        temporal_semantics.THROUGHOUT_INTERVAL,
        temporal_semantics.UNCHANGED_THROUGHOUT,
        temporal_semantics.EVERY_SUBINTERVAL,
    }:
        return months
    if operator == temporal_semantics.CHANGE_COUNT:
        count = node.get("count")
        comparison = str(node.get("count_operator") or ">=")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            if comparison in {">", "gt"}:
                return max(months or 0, count + 2)
            if comparison in {">=", "gte", "=", "==", "eq"}:
                return max(months or 0, count + 1)
            # '<'/'<=' can be satisfied with zero changes.  Without an
            # explicit observation window, the threshold alone proves no
            # minimum snapshot depth and must not manufacture a gap.
            return months
    return None


def relation_data_coverage_gaps(
    payload: Mapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Prove definite monthly-snapshot coverage gaps from catalog declarations."""
    gaps: list[dict[str, Any]] = []
    for node in _relation_nodes(payload):
        owner = _attribute_coverage(node, catalog)
        operator = targeting_domain.temporal_operator_of(node.get("relation"))
        bounds = _node_span(node, query)
        if owner is None or operator is None or bounds is None:
            continue
        coverage_id, coverage = owner
        coverage_start = _coverage_date(coverage.get("from"))
        coverage_end = _coverage_date(coverage.get("to"), end=True)
        period_bounds = _period_bounds(node.get("period"))
        reason = ""
        required = _required_observation_months(node, operator)
        available = _available_months(coverage)
        if (
            period_bounds is not None
            and coverage_start is not None
            and coverage_end is not None
            and (period_bounds[0] < coverage_start or period_bounds[1] > coverage_end)
        ):
            reason = (
                f"window {period_bounds[0].isoformat()}..{period_bounds[1].isoformat()} "
                f"is outside complete coverage "
                f"{coverage_start.isoformat()}..{coverage_end.isoformat()}"
            )
        elif required is not None and available is not None and required > available:
            reason = (
                f"{operator} requires {required} distinct monthly snapshots, "
                f"but coverage {coverage_id!r} declares {available}"
            )
        if reason:
            gaps.append({
                "node_id": str(node.get("id") or ""),
                "kind": "data_coverage_gap",
                "reason": reason,
                "evidence": query[bounds[0]:bounds[1]],
            })
    return gaps


def project_relation_data_coverage(
    payload: MutableMapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Project a proven gap to the existing fail-closed semantic IR channel."""
    gaps = relation_data_coverage_gaps(payload, query, catalog)
    if gaps:
        payload["semantic_ir"] = {
            "status": "unsupported",
            "operations": [],
            "missing_fields": [],
            "missing_field_causes": [],
            "failure_kind": "unsupported",
            "policy_applications": [],
            "unsupported_operations": [
                {key: gap[key] for key in ("kind", "reason", "evidence")}
                for gap in gaps
            ],
            "message": "요청한 이력 조건이 현재 월별 스냅샷 적재 범위를 벗어납니다.",
        }
    return gaps


def _text_hits(query: str, term: str) -> list[tuple[int, int]]:
    folded, needle = query.casefold(), term.strip().casefold()
    if not needle:
        return []
    hits: list[tuple[int, int]] = []
    cursor = 0
    while (start := folded.find(needle, cursor)) >= 0:
        end = start + len(needle)
        before = folded[start - 1] if start else ""
        after = folded[end] if end < len(folded) else ""
        if (
            (not needle[0].isascii() or not before.isalnum())
            and (not needle[-1].isascii() or not after.isalnum())
        ):
            hits.append((start, end))
        cursor = start + max(1, len(needle))
    return hits


def reconcile_axis_scoped_claims(
    query: str, claims: Iterable[Mapping[str, Any]], catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Attribute an unqualified value to a nearby catalog-declared axis.

    Korean particles commonly appear between an axis alias and its values
    (``가치등급이 골드에서 VIP``), while the value dictionary deliberately
    stores the reusable aliases ``가치등급 골드`` / ``가치등급 VIP``.  A
    longest, nearby axis cue selects the domain; value identities are still
    derived exclusively from that domain's declared aliases.
    """
    fields, _metrics, domains = _sections(catalog)
    domain_fields: dict[str, list[str]] = {}
    cues: list[tuple[str, int, int]] = []
    for field_id, declaration in fields.items():
        if not isinstance(declaration, Mapping):
            continue
        domain_id = declaration.get("value_domain")
        if not isinstance(domain_id, str) or domain_id not in domains:
            continue
        domain_fields.setdefault(domain_id, []).append(str(field_id))
        aliases = declaration.get("aliases")
        terms = [declaration.get("label"), *(aliases if isinstance(aliases, list) else [])]
        for term in terms:
            if not isinstance(term, str) or len(term.strip()) < 2:
                continue
            cues.extend((domain_id, start, end) for start, end in _text_hits(query, term))
    # A specific cue (가치등급) owns a contained generic cue (등급).
    cues = [
        cue for cue in cues
        if not any(
            other[0] != cue[0] and other[1] <= cue[1] and cue[2] <= other[2]
            and other[2] - other[1] > cue[2] - cue[1]
            for other in cues
        )
    ]

    def clause_bounds(position: int) -> tuple[int, int]:
        start = max(query.rfind(token, 0, position) for token in (",", ".", ";")) + 1
        ends = [index for token in (",", ".", ";") if (index := query.find(token, position)) >= 0]
        return start, min(ends) if ends else len(query)

    def target_canonical(domain_id: str, text: str) -> str | None:
        domain = domains.get(domain_id)
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            return None
        surface = "".join(text.split()).casefold()
        matches: set[str] = set()
        for canonical, declaration in values.items():
            aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
            terms = [canonical, *(aliases if isinstance(aliases, list) else [])]
            normalized = ["".join(str(term).split()).casefold() for term in terms]
            if any(term == surface or term.startswith(surface) or term.endswith(surface) for term in normalized):
                matches.add(str(canonical))
        return next(iter(matches)) if len(matches) == 1 else None

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in claims:
        claim = dict(raw)
        for start, end in claim.get("hits") or ():
            clause_start, clause_end = clause_bounds(start)
            nearby = [
                cue for cue in cues
                if clause_start <= cue[1] < clause_end
                and min(abs(start - cue[2]), abs(cue[1] - end)) <= 32
            ]
            cue = min(
                nearby,
                key=lambda item: (
                    min(abs(start - item[2]), abs(item[1] - end)),
                    -(item[2] - item[1]),
                ),
                default=None,
            )
            domain_id, canonical = str(claim.get("domain")), str(claim.get("canonical"))
            if cue is not None and cue[0] != domain_id:
                redirected = target_canonical(cue[0], query[start:end])
                if redirected is not None:
                    domain_id, canonical = cue[0], redirected
            key = domain_id, canonical
            row = merged.setdefault(key, {
                "domain": domain_id,
                "canonical": canonical,
                "fields": domain_fields.get(domain_id, list(claim.get("fields") or ())),
                "hits": [],
            })
            if (start, end) not in row["hits"]:
                row["hits"].append((start, end))
    return list(merged.values())


def _catalog_issue_owned(
    issue: Mapping[str, Any], node: Mapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> bool:
    argument = str(issue.get("argument") or "")
    if not argument.startswith("catalog_value."):
        return False
    fields, _metrics, domains = _sections(catalog)
    field = fields.get(argument.removeprefix("catalog_value."))
    domain_id = field.get("value_domain") if isinstance(field, Mapping) else None
    domain = domains.get(domain_id) if isinstance(domain_id, str) else None
    if not isinstance(domain_id, str) or not isinstance(domain, Mapping):
        return False
    if domain_id not in _attribute_domains(node, catalog):
        return False
    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    issue_values = _canonical_values(evidence.get("text"), domain)
    node_values = set().union(*(
        _canonical_values(node.get(key), domain)
        for key in ("value", "from_value", "to_value")
    ))
    return bool(issue_values & node_values)


_BINDING_INDEX_RE = re.compile(r"^literal_bindings\[(\d+)](?:\.|$)")


def _literal_issue_owned(
    issue: Mapping[str, Any], node: Mapping[str, Any], bindings: Iterable[Mapping[str, Any]]
) -> bool:
    match = _BINDING_INDEX_RE.match(str(issue.get("argument") or ""))
    rows = list(bindings)
    if match is None or int(match.group(1)) >= len(rows):
        return False
    binding = rows[int(match.group(1))]
    normalized = binding.get("normalized")
    period = node.get("period")
    if binding.get("kind") == "date_window" and isinstance(normalized, Mapping) and isinstance(period, Mapping):
        expected_window = normalized.get("event_ir_window")
        if isinstance(expected_window, Mapping) and dict(period) == dict(expected_window):
            return True
        start = str(normalized.get("from") or "")
        return bool(
            len(start) >= 6
            and int(period.get("year") or 0) == int(start[:4])
            and int(period.get("month") or 0) == int(start[4:6])
        )
    value = normalized.get("value") if isinstance(normalized, Mapping) else normalized
    return value in {
        node.get("months"), node.get("count"), node.get("count_operator"),
        node.get("value_comparison"), node.get("value"),
    }


def relation_node_owns_issue(
    issue: Mapping[str, Any],
    node: Mapping[str, Any],
    query: str,
    catalog: Mapping[str, Any],
    bindings: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """Return true only for an issue structurally owned by this exact node."""
    node_bounds = _node_span(node, query)
    evidence = issue.get("evidence")
    issue_bounds = _span(evidence, query) if isinstance(evidence, Mapping) else None
    if node_bounds is None or issue_bounds is None or not (
        node_bounds[0] <= issue_bounds[0] and issue_bounds[1] <= node_bounds[1]
    ):
        return False
    argument = str(issue.get("argument") or "")
    if argument.startswith("catalog_value."):
        return _catalog_issue_owned(issue, node, query, catalog)
    if argument.startswith("literal_bindings["):
        return _literal_issue_owned(issue, node, bindings)
    if argument == "source_semantics.member_state_history":
        return bool(_attribute_domains(node, catalog))
    return False


def semantic_plan_owns_issue(
    issue: Mapping[str, Any], payload: Mapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> bool:
    bindings = payload.get("literal_bindings")
    bindings = bindings if isinstance(bindings, list) else []
    return any(
        relation_node_owns_issue(issue, node, query, catalog, bindings)
        for node in _relation_nodes(payload)
    )


def lowered_relation_receipt_owns_issue(
    issue: Mapping[str, Any], plan: Mapping[str, Any], query: str, catalog: Mapping[str, Any]
) -> bool:
    """A graph-level issue is stale only after its exact node was lowered."""
    event = plan.get("event_expression")
    receipts = event.get("receipts") if isinstance(event, Mapping) else None
    lowered = {
        str(receipt.get("node_id"))
        for receipt in (receipts or ())
        if isinstance(receipt, Mapping)
        and receipt.get("status") == "lowered"
        and isinstance(receipt.get("expression_fingerprint"), str)
        and len(str(receipt.get("expression_fingerprint"))) == 64
        and receipt.get("catalog_symbols")
    }
    if not lowered:
        return False
    bindings = plan.get("literal_bindings")
    bindings = bindings if isinstance(bindings, list) else []
    return any(
        str(node.get("id")) in lowered
        and relation_node_owns_issue(issue, node, query, catalog, bindings)
        for node in _relation_nodes(plan)
    )


def relation_requirement_owned_by_lowered_node(
    requirement: Any, plan: Mapping[str, Any], query: str
) -> bool:
    span = getattr(requirement, "source_span", None)
    if not isinstance(span, Mapping):
        return False
    bounds = _span(span, query)
    if bounds is None:
        return False
    event = plan.get("event_expression")
    receipts = event.get("receipts") if isinstance(event, Mapping) else None
    lowered = {
        str(item.get("node_id")) for item in (receipts or ())
        if isinstance(item, Mapping) and item.get("status") == "lowered"
    }
    return any(
        str(node.get("id")) in lowered
        and (node_bounds := _node_span(node, query)) is not None
        and node_bounds[0] <= bounds[0] and bounds[1] <= node_bounds[1]
        for node in _relation_nodes(plan)
    )
