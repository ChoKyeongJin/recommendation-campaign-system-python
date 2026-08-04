"""Evidence-scoped coverage for open-text product purchase claims.

The semantic catalog declares which fields are compiled as ``contains``
searches.  This module prevents a structuring model from acknowledging a
concrete product phrase in evidence while emitting only a bare event source.
It never invents a product vocabulary or a SQL column: product values come
from exact query spans and searchable fields come from the catalog.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import audience_frame
import event_ir
import event_semantic_registry
import lexicon_patterns
import query_semantics

_VALUE = r"[0-9A-Za-z가-힣_+&.\-]{1,40}"
# Product and category names commonly contain spaces (``반려견 사료``).  Keeping
# ``_VALUE`` as the lexical atom preserves the existing separator grammar,
# while the phrase form prevents the validator from silently reducing a name
# to only the final token before the object particle.
_VALUE_PHRASE = rf"{_VALUE}(?:\s+{_VALUE}){{0,5}}"
_ENUM_SEPARATOR_RE = re.compile(
    r"(?:(?<=[가-힣])(?:와|과|랑|이랑)\s+|\s*(?:및|그리고)\s+|\s*[,、]\s*)"
)
_QUANTITY_OR_MONEY_RE = re.compile(
    r"^(?:\d[\d,.]*|[일이삼사오육칠팔구십백천만억]+)"
    r"(?:개|회|번|건|명|종|가지|원|만원|천원|억원|일|주|개월|달|년)?$"
)


@dataclass(frozen=True)
class OpenTextScopeClaim:
    value: str
    start: int
    end: int
    complemented: bool
    group: str | None = None

    def evidence(self, query: str) -> dict[str, Any]:
        return {"text": query[self.start:self.end], "start": self.start, "end": self.end}


@dataclass(frozen=True)
class _SearchComparison:
    field: str
    value: str
    complemented: bool


@dataclass(frozen=True)
class _ConcreteValue:
    value: str
    start: int
    end: int


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _product_dimension() -> event_semantic_registry.ScopeDimension | None:
    return event_semantic_registry.registry().dimensions.get("product_scope")


def _generic_product_terms() -> frozenset[str]:
    dimension = _product_dimension()
    declared = set(dimension.qualifier_aliases if dimension is not None else ())
    declared.update(dimension.unrestricted_aliases if dimension is not None else ())
    declared.update(lexicon_patterns.vocabulary("event_scope_value_stopword"))
    declared.update({"다른", "해당", "특정", "외", "외의"})
    return frozenset(_normalized(term) for term in declared if term)


_PHRASE_BOUNDARY_TERMS = frozenset({
    "중", "중에서", "그리고", "또는", "혹은", "및", "이며", "이고", "이면서",
    "거주", "거주지", "지역", "성별", "연령", "등급",
})
_AUDIENCE_BOUNDARY_RE = re.compile(
    r"^(?:회원|고객|사용자|사람|대상)(?:이|가|은|는|을|를|과|와|이며|이고|이면서)?$"
)
_COMPACT_TEMPORAL_RE = re.compile(
    r"^(?:최근|지난|이번)(?:\d+)?(?:일|주|개월|달|분기|년)?$"
)
_CLAUSE_CONNECTOR_RE = re.compile(r".+(?:이면서|하면서|이며|하며|이고)$")
_AGE_DEMOGRAPHIC_RE = re.compile(r"^\d{1,3}대(?:인)?$")
_GENDER_TERMS = frozenset({"여성", "남성", "여자", "남자"})


def _phrase_boundary(value: str) -> bool:
    normalized = _normalized(value).strip(" \t\r\n\"'“”‘’「」")
    return bool(
        not normalized
        or normalized in _generic_product_terms()
        or normalized in _PHRASE_BOUNDARY_TERMS
        or _AUDIENCE_BOUNDARY_RE.fullmatch(normalized)
        or _COMPACT_TEMPORAL_RE.fullmatch(normalized)
        or _CLAUSE_CONNECTOR_RE.fullmatch(normalized)
        or _AGE_DEMOGRAPHIC_RE.fullmatch(normalized)
        or _QUANTITY_OR_MONEY_RE.fullmatch(normalized)
        or query_semantics.is_non_entity_candidate(normalized)
    )


def _concrete_value(value: str) -> _ConcreteValue | None:
    tokens = list(re.finditer(_VALUE, value))
    if not tokens:
        return None
    last_boundary = -1
    demographic_prefix = False
    for index, token in enumerate(tokens):
        normalized_token = _normalized(token.group(0))
        is_age_prefix = bool(_AGE_DEMOGRAPHIC_RE.fullmatch(normalized_token))
        is_boundary = _phrase_boundary(normalized_token) or (
            demographic_prefix and normalized_token in _GENDER_TERMS
        )
        if is_boundary:
            last_boundary = index
        demographic_prefix = demographic_prefix or is_age_prefix
    selected = tokens[last_boundary + 1:]
    if not selected:
        return None
    start, end = selected[0].start(), selected[-1].end()
    normalized = _normalized(value[start:end]).strip(" \t\r\n\"'“”‘’「」")
    if (
        not normalized
        or len(normalized) > 80
        or normalized in _generic_product_terms()
        or query_semantics.is_non_entity_candidate(normalized)
        or _QUANTITY_OR_MONEY_RE.fullmatch(normalized)
    ):
        return None
    return _ConcreteValue(normalized, start, end)


def _patterns() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    purchase_alt = lexicon_patterns.alternation("event_alias_purchase")
    dimension = _product_dimension()
    qualifiers = tuple(dimension.qualifier_aliases if dimension is not None else ())
    qualifier_alt = "|".join(re.escape(item) for item in sorted(qualifiers, key=len, reverse=True))
    all_alt = lexicon_patterns.alternation("source_all_quantifier") or r"모두|전부|각각|빠짐없이"
    separator = (
        r"(?:(?<=[가-힣])(?:와|과|랑|이랑)\s+|\s*(?:및|그리고)\s+|\s*[,、]\s*)"
    )
    enumeration = re.compile(
        rf"(?P<chain>{_VALUE_PHRASE}(?:{separator}{_VALUE_PHRASE}){{1,4}})\s*(?:을|를)\s*"
        rf"(?:{all_alt})\s*(?:{purchase_alt})",
        re.IGNORECASE,
    )
    excluded = re.compile(
        rf"(?P<value>{_VALUE_PHRASE})\s*외(?:의)?\s*(?:{qualifier_alt})"
        rf"(?:을|를|은|는)?\s*(?:{purchase_alt})",
        re.IGNORECASE,
    )
    other = re.compile(
        rf"다른\s*(?:{qualifier_alt})(?:을|를|은|는)?\s*(?:{purchase_alt})",
        re.IGNORECASE,
    )
    direct = re.compile(
        rf"(?P<value>{_VALUE_PHRASE})\s*(?:을|를)\s*(?:{purchase_alt})",
        re.IGNORECASE,
    )
    return enumeration, excluded, other, direct


def _enumerated_claims(query: str, pattern: re.Pattern[str]) -> list[OpenTextScopeClaim]:
    claims: list[OpenTextScopeClaim] = []
    for match in pattern.finditer(query):
        chain = match.group("chain")
        chain_start = match.start("chain")
        group = f"all:{match.start()}:{match.end()}"
        cursor = 0
        pieces: list[tuple[int, int]] = []
        for separator in _ENUM_SEPARATOR_RE.finditer(chain):
            pieces.append((cursor, separator.start()))
            cursor = separator.end()
        pieces.append((cursor, len(chain)))
        for local_start, local_end in pieces:
            while local_start < local_end and chain[local_start].isspace():
                local_start += 1
            while local_end > local_start and chain[local_end - 1].isspace():
                local_end -= 1
            concrete = _concrete_value(chain[local_start:local_end])
            if concrete is None:
                continue
            claims.append(OpenTextScopeClaim(
                value=concrete.value,
                start=chain_start + local_start + concrete.start,
                end=chain_start + local_start + concrete.end,
                complemented=False,
                group=group,
            ))
    return claims


def extract_purchase_product_claims(query: str) -> tuple[OpenTextScopeClaim, ...]:
    """Return only product values anchored to an explicit purchase construction."""

    enumeration, excluded, other, direct = _patterns()
    claims = _enumerated_claims(query, enumeration)
    enumerated_spans = {(claim.start, claim.end) for claim in claims}

    for match in excluded.finditer(query):
        concrete = _concrete_value(match.group("value"))
        if concrete is not None:
            claims.append(OpenTextScopeClaim(
                value=concrete.value,
                start=match.start("value") + concrete.start,
                end=match.end(),
                complemented=True,
            ))

    for match in direct.finditer(query):
        concrete = _concrete_value(match.group("value"))
        if concrete is None:
            continue
        value_span = (
            match.start("value") + concrete.start,
            match.start("value") + concrete.end,
        )
        if value_span in enumerated_spans:
            continue
        claims.append(OpenTextScopeClaim(
            value=concrete.value,
            start=value_span[0],
            end=match.end(),
            complemented=False,
        ))

    explicit_claims = list(claims)
    for match in other.finditer(query):
        prior = [claim for claim in explicit_claims if claim.end <= match.start()]
        if not prior:
            continue
        referenced = max(prior, key=lambda claim: claim.end)
        claims.append(OpenTextScopeClaim(
            value=referenced.value,
            start=match.start(),
            end=match.end(),
            complemented=True,
        ))

    unique: dict[tuple[str, int, int, bool, str | None], OpenTextScopeClaim] = {}
    for claim in claims:
        unique[(claim.value, claim.start, claim.end, claim.complemented, claim.group)] = claim
    return tuple(sorted(unique.values(), key=lambda item: (item.start, item.end, item.value)))


def synthesize_single_product_complement_purchase(
    query: str,
) -> event_ir.Exists | None:
    """Synthesize only the request that is one ``X 외 상품 구매`` claim and nothing else.

    This is deliberately not a general Korean parser.  Exactly one concrete
    complemented product claim must be present, and everything the claim does
    not own must be frame — audience nouns, particles, verb endings, request
    verbs (:mod:`audience_frame`).  Prefix conditions, conjunctions, multiple
    product claims, periods, thresholds, and arbitrary trailing text all fail
    closed.  The judgement is by clause structure rather than by one sentence
    template, so ``찾아줘`` / ``추출해 주세요`` / ``했던`` are the same answer.

    The resulting ``!=`` remains a row-level predicate inside positive
    ``Exists``; it can never become member-level ``Not(Exists(...))``.
    """

    claims = extract_purchase_product_claims(query)
    if len(claims) != 1:
        return None
    claim = claims[0]
    if not claim.complemented or claim.group is not None:
        return None
    if not audience_frame.is_frame_only(query, [(claim.start, claim.end)]):
        return None

    evidence = event_ir.Evidence(
        text=query[claim.start:claim.end], start=claim.start, end=claim.end
    )
    comparison = event_ir.Comparison(
        "!=",
        event_ir.FieldRef("purchase_line.product_text"),
        event_ir.Literal(claim.value),
        evidence=evidence,
    )
    return event_ir.Exists(
        event_ir.Filter(event_ir.Source("purchase_line"), comparison),
        evidence=evidence,
    )


def _contains_fields_by_source(fields: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for field_id, declaration in fields.items():
        if not (
            isinstance(field_id, str)
            and isinstance(declaration, Mapping)
            and str(declaration.get("match_mode") or "").casefold() == "contains"
            and isinstance(declaration.get("source"), str)
        ):
            continue
        result.setdefault(str(declaration["source"]), set()).add(field_id)
    return result


def _field_literal(comparison: Mapping[str, Any]) -> tuple[str, str] | None:
    left, right = comparison.get("left"), comparison.get("right")
    for field, literal in ((left, right), (right, left)):
        if not (
            isinstance(field, Mapping)
            and field.get("type") == "field"
            and isinstance(field.get("name"), str)
            and isinstance(literal, Mapping)
            and literal.get("type") == "literal"
            and isinstance(literal.get("value"), str)
        ):
            continue
        return str(field["name"]), _normalized(str(literal["value"]))
    return None


def _search_comparisons(
    value: Any,
    searchable_fields: set[str],
    *,
    negated: bool = False,
) -> Iterable[_SearchComparison]:
    if isinstance(value, Mapping):
        node_type = value.get("type")
        if node_type == "not":
            yield from _search_comparisons(
                value.get("operand"), searchable_fields, negated=not negated
            )
            return
        if node_type == "comparison":
            operands = _field_literal(value)
            if operands is not None and operands[0] in searchable_fields:
                operator = str(value.get("operator") or "")
                if operator in {"=", "!="}:
                    yield _SearchComparison(
                        field=operands[0],
                        value=operands[1],
                        complemented=negated != (operator == "!="),
                    )
            return
        for key, child in value.items():
            if key != "evidence":
                yield from _search_comparisons(child, searchable_fields, negated=negated)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _search_comparisons(child, searchable_fields, negated=negated)


def _source_names(atom: event_ir.Condition) -> set[str]:
    return {
        node.name for node in event_ir.walk(atom) if isinstance(node, event_ir.Source)
    }


def _overlaps(evidence: event_ir.Evidence | None, claim: OpenTextScopeClaim) -> bool:
    return bool(
        evidence is not None
        and max(evidence.start, claim.start) < min(evidence.end, claim.end)
    )


def _maximum_group_matching(
    claim_indices: list[int], candidates: Mapping[int, list[int]]
) -> set[int]:
    owner: dict[int, int] = {}

    def assign(claim_index: int, seen: set[int]) -> bool:
        for atom_index in candidates.get(claim_index, []):
            if atom_index in seen:
                continue
            seen.add(atom_index)
            previous = owner.get(atom_index)
            if previous is None or assign(previous, seen):
                owner[atom_index] = claim_index
                return True
        return False

    matched: set[int] = set()
    for claim_index in claim_indices:
        if assign(claim_index, set()):
            matched.add(claim_index)
    return matched


def omitted_open_text_scope_issues(
    query: str,
    expression: event_ir.Condition,
    fields: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reject product purchase claims not consumed by catalog contains filters.

    ``X 외 상품`` and ``다른 상품`` require the complement of the exact X
    predicate inside a positive Exists.  ``X를 구매하지 않은`` keeps the X
    predicate positive and lets the outer Not(Exists(...)) own absence.  An
    explicit all-list additionally requires one independent Exists atom per
    item, preventing a single OR-filtered rowset from meaning "any item".
    """

    claims = list(extract_purchase_product_claims(query))
    if not claims:
        return []
    searchable_by_source = _contains_fields_by_source(fields)
    purchase_fields = searchable_by_source.get("purchase_line")
    if not purchase_fields:
        return []

    atoms = list(event_ir.iter_signed_atoms(expression))
    atom_comparisons: dict[int, tuple[_SearchComparison, ...]] = {}
    eligible: dict[int, list[int]] = {}
    matching: dict[int, list[int]] = {}
    for claim_index, claim in enumerate(claims):
        eligible[claim_index] = []
        matching[claim_index] = []
        for atom_index, (atom, _outer_negated) in enumerate(atoms):
            if "purchase_line" not in _source_names(atom) or not _overlaps(atom.evidence, claim):
                continue
            eligible[claim_index].append(atom_index)
            comparisons = atom_comparisons.setdefault(
                atom_index,
                tuple(_search_comparisons(atom.to_dict(), purchase_fields)),
            )
            if any(
                comparison.value == claim.value
                and comparison.complemented == claim.complemented
                for comparison in comparisons
            ):
                matching[claim_index].append(atom_index)

    consumed: set[int] = set()
    grouped: dict[str, list[int]] = {}
    for index, claim in enumerate(claims):
        if claim.group is None:
            if matching[index]:
                consumed.add(index)
        else:
            grouped.setdefault(claim.group, []).append(index)
    for indices in grouped.values():
        consumed.update(_maximum_group_matching(indices, matching))

    issues: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        if index in consumed:
            continue
        shape = (
            "Filter(Source('purchase_line'), Not(Comparison('=', "
            "purchase_line.product_text, literal)))"
            if claim.complemented
            else "Filter(Source('purchase_line'), Comparison('=', "
            "purchase_line.product_text, literal))"
        )
        independent = (
            " '모두'로 나열한 각 상품은 서로 다른 Exists에 두어야 합니다."
            if claim.group is not None else ""
        )
        issues.append({
            "code": "validation_mismatch",
            "argument": "catalog_scope.purchase_line.product_text",
            "message": (
                f"원문의 상품 범위 '{claim.value}'가 상품명/카테고리 contains 필터로 "
                f"소비되지 않았습니다. 필요한 Event IR 형태: {shape}."
                + independent
            ),
            "evidence": claim.evidence(query),
        })
    return issues


__all__ = [
    "OpenTextScopeClaim",
    "extract_purchase_product_claims",
    "omitted_open_text_scope_issues",
    "synthesize_single_product_complement_purchase",
]
