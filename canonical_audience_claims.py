"""Fail-close source-claim coverage for canonical audience Event IR.

The structuring model proposes a fixed algebra tree; it does not own literal
values or permission to omit a source operator.  This module compares the
application-extracted literals and immutable semantic obligations with that
tree before it can become executable SQL.  It contains no campaign/query
template and recognizes business vocabulary only through the semantic catalog.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import event_ir
import lexicon_patterns
import semantic_requirements


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _semantic_tokens(atom: event_ir.Condition) -> list[tuple[tuple[Any, ...], str, Any]]:
    """Return uniquely-addressed literals/windows/operators from one atom."""
    root = atom.to_dict()
    tokens: list[tuple[tuple[Any, ...], str, Any]] = []

    def visit(value: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, Mapping):
            node_type = value.get("type")
            if node_type == "literal" and isinstance(value.get("value"), (int, float)):
                tokens.append((path, "number", value["value"]))
            elif node_type == "limit" and isinstance(value.get("count"), int):
                tokens.append((path, "number", value["count"]))
            elif node_type in {"rolling", "relative", "duration"} and isinstance(
                value.get("value"), int
            ):
                tokens.append((path, "number", value["value"]))
            elif node_type == "interval":
                tokens.append((path, "date_window", (value.get("from"), value.get("to"))))
            elif node_type == "comparison" and isinstance(value.get("operator"), str):
                tokens.append((path, "comparison_operator", value["operator"]))
            for key, child in value.items():
                if key != "evidence":
                    visit(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index))

    visit(root)
    return tokens


def _binding_target(binding: Mapping[str, Any]) -> tuple[str, Any] | None:
    kind = binding.get("kind")
    normalized = binding.get("normalized")
    if kind == "date_window" and isinstance(normalized, Mapping):
        return "date_window", (normalized.get("from"), normalized.get("to"))
    if kind == "comparison_operator" and isinstance(normalized, str):
        return "comparison_operator", normalized
    if kind == "money" and isinstance(normalized, Mapping):
        return "number", normalized.get("amount")
    if kind in {"number", "number_with_unit", "duration", "percentage"}:
        if isinstance(normalized, Mapping):
            return "number", normalized.get("value")
        return "number", normalized
    return None


def _binding_evidence(binding: Mapping[str, Any], query: str) -> dict[str, Any]:
    start, end = binding.get("start"), binding.get("end")
    if (
        isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
    ):
        return {"text": query[start:end], "start": start, "end": end}
    return {"text": query, "start": 0, "end": len(query)}


def _evidence_spans(atom: event_ir.Condition) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in _walk(atom.to_dict()):
        evidence = node.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        start, end = evidence.get("start"), evidence.get("end")
        if (
            isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and start < end
        ):
            spans.append((start, end))
    return spans


def literal_claim_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Require each application-owned literal/operator to have one IR consumer.

    Matching is evidence-scoped and one-to-one.  Thus two equal source numbers
    require two semantic occurrences; evidence offsets themselves are never
    mistaken for business numbers.
    """
    atoms = list(event_ir.iter_signed_atoms(expression))
    token_rows: list[tuple[int, tuple[Any, ...], str, Any]] = []
    for atom_index, (atom, _negated) in enumerate(atoms):
        token_rows.extend(
            (atom_index, path, kind, value)
            for path, kind, value in _semantic_tokens(atom)
        )
    consumed: set[tuple[int, tuple[Any, ...]]] = set()
    issues: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        target = _binding_target(binding)
        if target is None:
            continue
        target_kind, target_value = target
        start, end = binding.get("start"), binding.get("end")
        candidates: list[tuple[int, tuple[Any, ...]]] = []
        for atom_index, path, token_kind, token_value in token_rows:
            atom = atoms[atom_index][0]
            evidence_covers = any(
                evidence_start <= start and end <= evidence_end
                for evidence_start, evidence_end in _evidence_spans(atom)
            ) if isinstance(start, int) and isinstance(end, int) else False
            if (
                target_kind != "date_window"
                and not evidence_covers
            ):
                continue
            if token_kind == target_kind and token_value == target_value:
                candidates.append((atom_index, path))
        available = next((candidate for candidate in candidates if candidate not in consumed), None)
        if available is not None:
            consumed.add(available)
            continue
        evidence = _binding_evidence(binding, query)
        issues.append({
            "code": "validation_mismatch",
            "argument": f"literal_bindings[{index}]",
            "message": "원문에서 확정한 리터럴 또는 비교 연산자가 canonical expression에서 소비되지 않았습니다.",
            "evidence": evidence,
        })
    return issues


def _term_hits(query: str, terms: Iterable[Any]) -> list[tuple[int, int]]:
    folded = query.casefold()
    hits: set[tuple[int, int]] = set()
    for raw_term in terms:
        term = str(raw_term or "").strip()
        if not term:
            continue
        needle = term.casefold()
        cursor = 0
        while (start := folded.find(needle, cursor)) >= 0:
            before = folded[start - 1] if start else ""
            after_index = start + len(needle)
            after = folded[after_index] if after_index < len(folded) else ""
            if (
                (not needle[0].isascii() or not before.isalnum())
                and (not needle[-1].isascii() or not after.isalnum())
            ):
                hits.add((start, after_index))
            cursor = start + max(1, len(needle))
    return sorted(hits)


def _atom_field_names(atom: event_ir.Condition) -> set[str]:
    return {
        str(node["name"])
        for node in _nodes(atom.to_dict(), "field")
        if isinstance(node.get("name"), str)
    }


def catalog_claim_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate catalog-owned value domains and literal units.

    Vocabulary and physical codes stay in the catalog.  The only language
    operation here is generic polarity detection via the shared lexicon.
    """
    atoms = list(event_ir.iter_signed_atoms(expression))
    issues: list[dict[str, Any]] = []
    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}

    # A unit-bearing source literal must be consumed by an atom that references
    # a field with the same declared unit.  This prevents money from being
    # attached to age/count merely because the numeric value happens to match.
    for index, binding in enumerate(bindings):
        if binding.get("kind") != "money":
            continue
        normalized = binding.get("normalized")
        expected_unit = normalized.get("currency") if isinstance(normalized, Mapping) else None
        start, end = binding.get("start"), binding.get("end")
        covered = False
        for atom, _negated in atoms:
            evidence = atom.evidence
            if not (
                isinstance(start, int) and isinstance(end, int)
                and evidence is not None
                and evidence.start <= start and end <= evidence.end
            ):
                continue
            if any(
                isinstance(fields.get(field_name), Mapping)
                and fields[field_name].get("unit") == expected_unit
                for field_name in _atom_field_names(atom)
            ):
                covered = True
                break
        if not covered:
            issues.append({
                "code": "validation_mismatch",
                "argument": f"literal_bindings[{index}].unit",
                "message": "금액 리터럴이 같은 통화 단위를 선언한 canonical 필드에 연결되지 않았습니다.",
                "evidence": _binding_evidence(binding, query),
            })

    negative_terms = lexicon_patterns.vocabulary("generic_negation")
    seen_claims: set[tuple[str, str]] = set()
    for field_id, field_declaration in fields.items():
        if not isinstance(field_declaration, Mapping):
            continue
        domain_id = field_declaration.get("value_domain")
        domain = domains.get(domain_id) if isinstance(domain_id, str) else None
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            continue
        for canonical, value_declaration in values.items():
            aliases = (
                value_declaration.get("aliases")
                if isinstance(value_declaration, Mapping) else []
            )
            hits = _term_hits(query, [canonical, *(aliases if isinstance(aliases, list) else [])])
            if not hits or (str(field_id), str(canonical)) in seen_claims:
                continue
            seen_claims.add((str(field_id), str(canonical)))
            # Longest alias gives the most useful evidence while still keeping
            # the comparison value claim singular.
            start, end = max(hits, key=lambda hit: (hit[1] - hit[0], -hit[0]))
            local = query[max(0, start - 8):min(len(query), end + 18)].casefold()
            expected_negative = any(term.casefold() in local for term in negative_terms)
            matched = False
            for atom, negated in atoms:
                evidence = atom.evidence
                if evidence is None or not (evidence.start <= start and end <= evidence.end):
                    continue
                for comparison in _nodes(atom.to_dict(), "comparison"):
                    left, right = comparison.get("left"), comparison.get("right")
                    pairs = ((left, right), (right, left))
                    if any(
                        _field_name(field) == field_id
                        and isinstance(literal, Mapping)
                        and literal.get("type") == "literal"
                        and literal.get("value") == canonical
                        for field, literal in pairs
                    ) and comparison.get("operator") == "=" and negated == expected_negative:
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                issues.append({
                    "code": "validation_mismatch",
                    "argument": f"catalog_value.{field_id}",
                    "message": "원문의 카탈로그 값과 포함/제외 극성이 canonical expression에 보존되지 않았습니다.",
                    "evidence": {"text": query[start:end], "start": start, "end": end},
                })
    return issues


def _nodes(value: Any, node_type: str) -> list[Mapping[str, Any]]:
    return [node for node in _walk(value) if node.get("type") == node_type]


def _has_source(value: Any, source: str | None, correlation: str) -> bool:
    if not source:
        return True
    return any(
        node.get("name") == source
        and str(node.get("correlation") or "subject") == correlation
        for node in _nodes(value, "source")
    )


def _field_name(value: Any) -> str | None:
    return (
        str(value.get("name"))
        if isinstance(value, Mapping)
        and value.get("type") == "field"
        and isinstance(value.get("name"), str)
        else None
    )


def _window_matches(value: Any, expected: Mapping[str, Any] | None) -> bool:
    if not expected:
        return True
    pair = (expected.get("from"), expected.get("to"))
    return any(
        (node.get("from"), node.get("to")) == pair
        for node in _nodes(value, "interval")
    )


def _ranked_membership_matches(expression: event_ir.Condition, value: Mapping[str, Any]) -> bool:
    payload = expression.to_dict()
    expected_limit = value.get("limit")
    expected_direction = "desc" if value.get("direction") == "top" else "asc"
    expected_source = value.get("source") if isinstance(value.get("source"), str) else None
    expected_entity = (
        value.get("entity_field") if isinstance(value.get("entity_field"), str) else None
    )
    expected_function = (
        value.get("measure_function")
        if isinstance(value.get("measure_function"), str) else None
    )
    expected_measure_field = (
        value.get("measure_field") if isinstance(value.get("measure_field"), str) else None
    )
    expected_distinct = bool(value.get("measure_distinct", False))
    expected_window = value.get("time_window")
    expected_window = expected_window if isinstance(expected_window, Mapping) else None

    for join in _nodes(payload, "join"):
        if join.get("kind", "inner") != "semi":
            continue
        left, right = join.get("left"), join.get("right")
        if not _has_source(left, expected_source, "subject"):
            continue
        if not _has_source(right, expected_source, "none"):
            continue
        if expected_entity:
            on = join.get("on")
            if not (
                isinstance(on, Mapping)
                and on.get("type") == "comparison"
                and on.get("operator") == "="
                and _field_name(on.get("left")) == expected_entity
                and _field_name(on.get("right")) == expected_entity
            ):
                continue
        for limit in _nodes(right, "limit"):
            if limit.get("count") != expected_limit:
                continue
            ranked_input = limit.get("relation")
            for order in _nodes(ranked_input, "order"):
                summarized_input = order.get("relation")
                for summary in _nodes(summarized_input, "summarize"):
                    measure_name: str | None = None
                    for measure in summary.get("measures") or []:
                        if not isinstance(measure, Mapping):
                            continue
                        if expected_function and measure.get("function") != expected_function:
                            continue
                        if bool(measure.get("distinct", False)) != expected_distinct:
                            continue
                        if expected_measure_field and _field_name(measure.get("expression")) != expected_measure_field:
                            continue
                        measure_name = str(measure.get("name") or "") or None
                        break
                    if measure_name is None:
                        continue
                    if not any(
                        isinstance(key, Mapping)
                        and key.get("name") == measure_name
                        and key.get("direction", "asc") == expected_direction
                        for key in order.get("keys") or []
                    ):
                        continue
                    if expected_entity and not any(
                        isinstance(key, Mapping)
                        and _field_name(key.get("expression")) == expected_entity
                        for key in summary.get("keys") or []
                    ):
                        continue
                    if not _window_matches(summary, expected_window):
                        continue
                    return True
    return False


def semantic_obligation_issues(
    query: str,
    expression: event_ir.Condition,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for requirement in semantic_requirements.capture_source_semantic_obligations(query):
        kind = str(requirement.base.get("name") or "")
        value = requirement.value if isinstance(requirement.value, Mapping) else {}
        if kind == "ranked_entity_set" and _ranked_membership_matches(expression, value):
            continue
        span = requirement.source_span
        start = span.get("start") if isinstance(span, Mapping) else None
        end = span.get("end") if isinstance(span, Mapping) else None
        if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(query)):
            start, end = 0, len(query)
        expected = ", ".join(
            f"{key}={value.get(key)}"
            for key in (
                "source", "entity_field", "measure_function", "measure_field",
                "measure_distinct", "direction", "limit", "time_window",
            )
            if value.get(key) is not None
        )
        if kind == "ranked_entity_set":
            expected = (
                "expression=Exists(semi Join), member_source_correlation=subject"
                "(omit correlation key), rank_source_correlation=none, "
                + expected
            )
        issues.append({
            "code": "validation_mismatch",
            "argument": f"source_semantics.{kind or 'unknown'}",
            "message": (
                "원문의 조합 의미를 보존하는 canonical 연산 구조가 누락되었거나 검증되지 않았습니다."
                + (f" 기대 계약: {expected}" if expected else "")
            ),
            "evidence": {"text": query[start:end], "start": start, "end": end},
        })
    return issues


def canonical_claim_issues(
    query: str,
    expression: event_ir.Condition,
    literal_bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    bindings = list(literal_bindings)
    return [
        *literal_claim_issues(query, expression, bindings),
        *(catalog_claim_issues(query, expression, bindings, catalog) if catalog is not None else []),
        *semantic_obligation_issues(query, expression),
    ]


def ranked_obligation_is_compiled(
    expression: event_ir.Condition,
    requirement_value: Mapping[str, Any],
) -> bool:
    """Public receipt predicate used by the graph-level immutable ledger."""
    return _ranked_membership_matches(expression, requirement_value)


def refresh_canonical_unresolved(
    query: str,
    plan: dict[str, Any],
    expression: event_ir.Condition | None,
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Refresh graph-level canonical coverage and immutable receipts."""
    requirement = plan.get("audience_requirement")
    issues: list[dict[str, Any]] = []
    if expression is not None:
        semantic_requirements.discharge_source_semantic_obligations(
            plan,
            query,
            kinds={"ranked_entity_set"},
            status="compiled",
            compiler="canonical_event_ir",
            evidence=expression.to_dict(),
            value_filter=lambda _kind, value: (
                isinstance(value, Mapping)
                and ranked_obligation_is_compiled(expression, value)
            ),
        )
        bindings = plan.get("literal_bindings")
        if isinstance(bindings, list):
            issues.extend(canonical_claim_issues(query, expression, bindings, catalog))
    elif isinstance(requirement, Mapping):
        issues.extend(
            issue for issue in (requirement.get("issues") or [])
            if isinstance(issue, dict)
        )

    unresolved = [
        {
            "id": "usr_" + hashlib.sha256(
                f"{query}\0{issue.get('argument')}\0{issue.get('code')}".encode("utf-8")
            ).hexdigest()[:16],
            "path": f"source_coverage.{issue.get('argument') or 'canonical_audience'}",
            "label": str(
                (issue.get("evidence") or {}).get("text")
                if isinstance(issue.get("evidence"), dict)
                else issue.get("argument") or "canonical audience"
            ),
            "source_text": query,
            "reason": str(issue.get("message") or "원문 조건의 canonical 실행 의미가 검증되지 않았습니다."),
            "code": str(issue.get("code") or "validation_mismatch"),
            "status": "unresolved",
            "source": "canonical_audience_contract",
        }
        for issue in issues
    ]
    known = {str(item.get("id") or "") for item in unresolved}
    unresolved.extend(
        item
        for item in semantic_requirements.unresolved_semantic_obligations(plan, query)
        if str(item.get("id") or "") not in known
    )
    plan["unresolved_source_conditions"] = unresolved
    return unresolved


def discharge_legacy_ranked_obligations(
    plan: dict[str, Any], query: str, node: Mapping[str, Any]
) -> None:
    """Issue a one-way receipt for a capability-validated persisted legacy slot."""

    def matches(_kind: str, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        pairs = {
            "direction": "direction",
            "limit": "limit",
            "entity_domain": "entity",
            "measure": "measure",
            "membership_relation": "relation",
        }
        if any(
            value.get(claim_key) is not None
            and node.get(slot_key) != value.get(claim_key)
            for claim_key, slot_key in pairs.items()
        ):
            return False
        expected_window = value.get("time_window")
        actual_window = node.get("window")
        return not isinstance(expected_window, Mapping) or (
            isinstance(actual_window, Mapping)
            and actual_window.get("from") == expected_window.get("from")
            and actual_window.get("to") == expected_window.get("to")
        )

    semantic_requirements.discharge_source_semantic_obligations(
        plan,
        query,
        kinds={"ranked_entity_set"},
        status="compiled",
        compiler="legacy_entity_set_adapter",
        evidence=dict(node),
        value_filter=matches,
    )


__all__ = [
    "canonical_claim_issues",
    "catalog_claim_issues",
    "discharge_legacy_ranked_obligations",
    "literal_claim_issues",
    "ranked_obligation_is_compiled",
    "refresh_canonical_unresolved",
    "semantic_obligation_issues",
]
