"""Catalog-owned consent cardinality contracts for canonical Event IR.

The source sentence chooses consent fields through their catalog aliases and a
cardinality quantifier.  A model-proposed Boolean expression is accepted only
when its truth table is exactly the requested count predicate.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import event_ir

CONSENT_CARDINALITY_QUANTIFIER_RE = re.compile(
    r"(?:중)?(?:정확히|딱)(?:한|하나|두|둘|세|네|\d+)(?:개|곳|채널)?|"
    r"(?:중|적어도|최소)(?:하나|한개|1개)(?:이상|라도)?|"
    r"(?:한|하나|두|둘|세|네|\d+)(?:개|곳|채널)?만|"
    r"oneormore|atleastone|exactly(?:one|two|three|four|\d+)",
    re.IGNORECASE,
)

_COUNT_TOKEN = r"한|하나|두|둘|세|셋|네|넷|\d+"
_EXACT_RE = re.compile(
    rf"(?:중\s*)?(?:정확히|딱)\s*(?P<count>{_COUNT_TOKEN})"
    r"\s*(?:개|곳)?\s*(?:채널)?",
    re.IGNORECASE,
)
_AT_LEAST_RE = re.compile(
    rf"(?:중\s*)?(?:적어도\s*|최소\s*)?(?P<count>{_COUNT_TOKEN})"
    r"\s*(?:개|곳|채널)?\s*(?:이상|라도)",
    re.IGNORECASE,
)
_PREFIXED_MIN_RE = re.compile(
    rf"(?:적어도|최소)\s*(?P<count>{_COUNT_TOKEN})\s*(?:개|곳|채널)?",
    re.IGNORECASE,
)
_EN_EXACT_RE = re.compile(
    r"exactly\s*(?P<count>one|two|three|four|\d+)", re.IGNORECASE
)
_EN_AT_LEAST_RE = re.compile(
    r"(?:one\s*or\s*more|at\s*least\s*(?P<count>one|two|three|four|\d+))",
    re.IGNORECASE,
)
_COUNT_VALUES = {
    "한": 1, "하나": 1, "one": 1,
    "두": 2, "둘": 2, "two": 2,
    "세": 3, "셋": 3, "three": 3,
    "네": 4, "넷": 4, "four": 4,
}


@dataclass(frozen=True)
class ConsentCardinalityValidation:
    mode: Literal["exact", "at_least"]
    count: int
    field_ids: tuple[str, ...]
    consent_field_ids: tuple[str, ...]
    target_value: str
    domain_values: tuple[str, ...]
    quantifier_text: str
    quantifier_start: int
    quantifier_end: int
    consumed_binding_indices: frozenset[int]
    equivalent: bool
    reason: str | None = None


def _compact(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _count(raw: str | None) -> int | None:
    token = _compact(raw)
    if token.isdigit():
        return int(token)
    return _COUNT_VALUES.get(token)


def _quantifier(query: str) -> tuple[str, int, re.Match[str]] | None:
    if not CONSENT_CARDINALITY_QUANTIFIER_RE.search(_compact(query)):
        return None
    for mode, pattern in (
        ("exact", _EXACT_RE),
        ("at_least", _AT_LEAST_RE),
        ("at_least", _PREFIXED_MIN_RE),
        ("exact", _EN_EXACT_RE),
        ("at_least", _EN_AT_LEAST_RE),
    ):
        match = pattern.search(query)
        if match is None:
            continue
        raw_count = match.groupdict().get("count")
        count = 1 if mode == "at_least" and raw_count is None else _count(raw_count)
        if count is not None:
            return mode, count, match
    return None


def _catalog_contract(
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]] | None:
    fields = catalog.get("fields")
    domains = catalog.get("value_domains")
    if not isinstance(fields, Mapping) or not isinstance(domains, Mapping):
        return None
    domain = domains.get("consent_flag")
    values = domain.get("values") if isinstance(domain, Mapping) else None
    consent_fields = {
        str(field_id): declaration
        for field_id, declaration in fields.items()
        if isinstance(declaration, Mapping)
        and declaration.get("value_domain") == "consent_flag"
    }
    if not consent_fields or not isinstance(values, Mapping) or len(values) != 2:
        return None
    return consent_fields, values


def _value_terms(values: Mapping[str, Any]) -> dict[str, set[str]]:
    terms: dict[str, set[str]] = {}
    for canonical, declaration in values.items():
        aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
        terms[str(canonical)] = {
            token
            for item in (canonical, *(aliases if isinstance(aliases, list) else []))
            if (token := _compact(item))
        }
    return terms


def _target_value(query: str, values: Mapping[str, Any]) -> str | None:
    compact_query = _compact(query)
    hits = [
        (len(term), canonical)
        for canonical, terms in _value_terms(values).items()
        for term in terms
        if term in compact_query
    ]
    if not hits:
        return None
    longest = max(length for length, _canonical in hits)
    owners = {canonical for length, canonical in hits if length == longest}
    return next(iter(owners)) if len(owners) == 1 else None


def _field_tokens(
    declaration: Mapping[str, Any], value_terms: Iterable[str]
) -> set[str]:
    aliases = declaration.get("aliases")
    surfaces = [
        declaration.get("label"),
        *(aliases if isinstance(aliases, list) else []),
    ]
    tokens: set[str] = set()
    for surface in surfaces:
        token = _compact(surface)
        for value_term in sorted(value_terms, key=len, reverse=True):
            token = token.replace(value_term, "")
        if len(token) >= 2:
            tokens.add(token)
    return tokens


def _requested_fields(
    query: str,
    consent_fields: Mapping[str, Mapping[str, Any]],
    values: Mapping[str, Any],
) -> tuple[str, ...]:
    compact_query = _compact(query)
    all_value_terms = set().union(*_value_terms(values).values())
    token_owners: dict[str, set[str]] = {}
    for field_id, declaration in consent_fields.items():
        for token in _field_tokens(declaration, all_value_terms):
            token_owners.setdefault(token, set()).add(field_id)
    selected = {
        next(iter(owners))
        for token, owners in token_owners.items()
        if len(owners) == 1 and token in compact_query
    }
    return tuple(field_id for field_id in consent_fields if field_id in selected)


class _InvalidConsentExpression(ValueError):
    pass


def _evaluate(
    expression: event_ir.Condition,
    assignment: Mapping[str, bool],
    *,
    selected_fields: frozenset[str],
    target_value: str,
    domain_values: frozenset[str],
    referenced: set[str],
) -> bool:
    if isinstance(expression, event_ir.And):
        return all(_evaluate(
            operand, assignment, selected_fields=selected_fields,
            target_value=target_value, domain_values=domain_values, referenced=referenced,
        ) for operand in expression.operands)
    if isinstance(expression, event_ir.Or):
        return any(_evaluate(
            operand, assignment, selected_fields=selected_fields,
            target_value=target_value, domain_values=domain_values, referenced=referenced,
        ) for operand in expression.operands)
    if isinstance(expression, event_ir.Not):
        return not _evaluate(
            expression.operand, assignment, selected_fields=selected_fields,
            target_value=target_value, domain_values=domain_values, referenced=referenced,
        )
    if not isinstance(expression, event_ir.Comparison) or expression.operator not in {"=", "!="}:
        raise _InvalidConsentExpression("cardinality expression contains a non-Boolean consent atom")
    pairs = ((expression.left, expression.right), (expression.right, expression.left))
    pair = next((
        (field, literal)
        for field, literal in pairs
        if isinstance(field, event_ir.FieldRef) and isinstance(literal, event_ir.Literal)
    ), None)
    if pair is None:
        raise _InvalidConsentExpression("cardinality comparison needs one catalog field and one value")
    field, literal = pair
    if field.name not in selected_fields or literal.value not in domain_values:
        raise _InvalidConsentExpression("cardinality comparison uses an unrequested field or value")
    referenced.add(field.name)
    matches_target = literal.value == target_value
    result = assignment[field.name] if matches_target else not assignment[field.name]
    return result if expression.operator == "=" else not result


def validate_consent_cardinality(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> ConsentCardinalityValidation | None:
    """Return a truth-table verdict, or ``None`` when no consent count request exists."""
    quantifier = _quantifier(query)
    contract = _catalog_contract(catalog)
    if quantifier is None or contract is None:
        return None
    mode, count, match = quantifier
    consent_fields, values = contract
    fields = _requested_fields(query, consent_fields, values)
    target_value = _target_value(query, values)
    if len(fields) < 2 or target_value is None:
        return None
    binding_indices = frozenset(
        index
        for index, binding in enumerate(bindings)
        if isinstance(binding, Mapping)
        and binding.get("kind") in {"comparison_operator", "number", "number_with_unit"}
        and isinstance(binding.get("start"), int)
        and isinstance(binding.get("end"), int)
        and match.start() <= int(binding["start"]) < int(binding["end"]) <= match.end()
    )
    reason: str | None = None
    equivalent = 0 < count <= len(fields)
    if not equivalent:
        reason = "requested count is outside the number of catalog-matched consent fields"
    referenced: set[str] = set()
    if equivalent:
        try:
            for bits in itertools.product((False, True), repeat=len(fields)):
                assignment = dict(zip(fields, bits, strict=True))
                actual = _evaluate(
                    expression,
                    assignment,
                    selected_fields=frozenset(fields),
                    target_value=target_value,
                    domain_values=frozenset(str(value) for value in values),
                    referenced=referenced,
                )
                total = sum(bits)
                expected = total == count if mode == "exact" else total >= count
                if actual != expected:
                    equivalent = False
                    reason = "Boolean truth table is not equivalent to the requested consent count"
                    break
        except _InvalidConsentExpression as exc:
            equivalent = False
            reason = str(exc)
    if equivalent and referenced != set(fields):
        equivalent = False
        reason = "not every requested consent field participates in the Boolean expression"
    return ConsentCardinalityValidation(
        mode=mode,
        count=count,
        field_ids=fields,
        consent_field_ids=tuple(consent_fields),
        target_value=target_value,
        domain_values=tuple(str(value) for value in values),
        quantifier_text=match.group(0),
        quantifier_start=match.start(),
        quantifier_end=match.end(),
        consumed_binding_indices=binding_indices,
        equivalent=equivalent,
        reason=reason,
    )


def synthesize_exact_consent_cardinality(
    query: str,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> event_ir.Condition | None:
    """Build the catalog-owned Boolean normal form for an exact count request.

    This is deliberately narrower than a sentence fallback.  The query must
    name at least two unambiguous consent fields, one binary catalog value and
    an explicit ``exactly N`` quantifier.  Every extracted literal must belong
    to that quantifier, and the generated expression is accepted only after
    the same exhaustive truth-table proof used for model-authored expressions.
    """
    quantifier = _quantifier(query)
    contract = _catalog_contract(catalog)
    rows = list(bindings)
    if quantifier is None or contract is None:
        return None
    mode, count, _match = quantifier
    if mode != "exact":
        return None

    consent_fields, values = contract
    fields = _requested_fields(query, consent_fields, values)
    target_value = _target_value(query, values)
    if not (0 < count <= len(fields)) or len(fields) < 2 or target_value is None:
        return None
    complements = tuple(str(value) for value in values if str(value) != target_value)
    if len(complements) != 1:
        return None

    # Each satisfying assignment becomes one conjunction.  Keep a hard
    # complexity ceiling so an unexpectedly large catalog cannot create an
    # exponential expression during request validation.
    assignments = [
        bits
        for bits in itertools.product((False, True), repeat=len(fields))
        if sum(bits) == count
    ]
    if not assignments or len(assignments) > 64:
        return None

    evidence = event_ir.Evidence(query, 0, len(query))
    branches: list[event_ir.Condition] = []
    for bits in assignments:
        atoms = tuple(
            event_ir.Comparison(
                "=",
                event_ir.FieldRef(field_id),
                event_ir.Literal(target_value if enabled else complements[0]),
                evidence=evidence,
            )
            for field_id, enabled in zip(fields, bits, strict=True)
        )
        branches.append(event_ir.And(atoms))
    expression: event_ir.Condition = (
        branches[0] if len(branches) == 1 else event_ir.Or(tuple(branches))
    )

    validation = validate_consent_cardinality(query, expression, rows, catalog)
    if validation is None or not validation.equivalent:
        return None
    if validation.consumed_binding_indices != frozenset(range(len(rows))):
        return None
    return expression


__all__ = [
    "CONSENT_CARDINALITY_QUANTIFIER_RE",
    "ConsentCardinalityValidation",
    "synthesize_exact_consent_cardinality",
    "validate_consent_cardinality",
]
