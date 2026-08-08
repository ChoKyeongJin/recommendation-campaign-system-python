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
_ALL_RE = re.compile(r"(?:모두|전부|전체)\s*(?:동의|수신|허용)?|\ball\b", re.IGNORECASE)
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


def _quantifier(query: str) -> tuple[str, int | None, re.Match[str]] | None:
    all_match = _ALL_RE.search(query)
    if not CONSENT_CARDINALITY_QUANTIFIER_RE.search(_compact(query)) and all_match is None:
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
    if all_match is not None:
        # The numeric value is catalog-dependent: "all" means exactly the
        # number of consent fields named by this clause, not every consent field
        # that happens to exist in the registry.
        return "all", None, all_match
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


@dataclass(frozen=True)
class CardinalityClaim:
    """집합 수준 카디널리티 주장 하나 — ``이메일, 문자, 앱푸시 중 정확히 두 개``.

    이 타입이 있는 이유(감사 #47)
    ----------------------------
    이 축의 구제 진입이 모델이 ``"consent_count"`` 라는 **문자열을 정확히 썼는가**에 달려
    있었다. 그 문자열은 모델 산문이고 회차마다 달라지므로, 같은 요청이 어휘 운에 따라 열리거나
    닫혔다. 진입 조건은 문자열이 아니라 **의미의 종류**여야 한다: 이것이 카디널리티 주장인가,
    그 도메인이 동의 채널인가.

    ``evidence_span`` 이 **하나**인 것도 계약이다. 카디널리티는 개별 필드가 아니라 필드
    **집합**에 걸리는 술어이므로, 세 멤버가 각자 독립된 근거 스팬을 가질 필요가 없다 —
    그 어구 전체가 하나의 공유 근거다.
    """

    domain: str
    members: tuple[str, ...]
    operator: Literal["exact", "at_least"]
    count: int
    target_value: str
    # 수량자 어구의 원문 구간(공유 근거).
    quantifier_span: tuple[int, int]
    # 이 주장이 원문에서 덮는 전체 구간(수량자 + 멤버 표면어). 리터럴 정산의 사정거리다.
    footprint: tuple[int, int]

    def owns(self, start: int, end: int) -> bool:
        """원문 구간이 이 주장의 사정거리 안인가."""
        return self.footprint[0] <= start and end <= self.footprint[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "members": list(self.members),
            "operator": self.operator,
            "count": self.count,
            "target_value": self.target_value,
            "quantifier_span": list(self.quantifier_span),
            "footprint": list(self.footprint),
        }


# 이 주장이 사는 값 도메인. 카탈로그 선언 이름이므로 코드에 낱말을 적는 것이 아니다.
CONSENT_CHANNEL_DOMAIN = "consent_flag"


def _member_spans(
    query: str,
    field_ids: Iterable[str],
    consent_fields: Mapping[str, Mapping[str, Any]],
    values: Mapping[str, Any],
) -> list[tuple[int, int]]:
    """요청된 동의 필드들의 표면어가 원문에서 나타난 구간.

    표면어는 압축 좌표(공백 제거)로 찾으므로 원문 좌표로 되돌린다 — 근거 구간은 원문 좌표계에서만
    뜻이 있다.
    """
    offsets = [index for index, char in enumerate(query) if not char.isspace()]
    compact = _compact(query)
    all_value_terms = set().union(*_value_terms(values).values())
    spans: list[tuple[int, int]] = []
    for field_id in field_ids:
        declaration = consent_fields.get(field_id)
        if not isinstance(declaration, Mapping):
            continue
        for token in _field_tokens(declaration, all_value_terms):
            start = compact.find(token)
            while start >= 0:
                end = start + len(token)
                if end <= len(offsets):
                    spans.append((offsets[start], offsets[end - 1] + 1))
                start = compact.find(token, start + 1)
    return spans


def detect_cardinality_claim(
    query: str, catalog: Mapping[str, Any]
) -> CardinalityClaim | None:
    """원문의 집합 수준 카디널리티 주장. 없으면 ``None``.

    **표현을 보지 않는다.** 이것이 :func:`validate_consent_cardinality` 와 다른 점이고, 구제
    진입을 문자열이 아니라 의미로 판정할 수 있게 하는 이유다 — 모델이 표현을 아예 내지 못했을
    때도 이 주장은 원문에서 그대로 읽힌다.
    """
    quantifier = _quantifier(query)
    contract = _catalog_contract(catalog)
    if quantifier is None or contract is None:
        return None
    mode, count, match = quantifier
    consent_fields, values = contract
    fields = _requested_fields(query, consent_fields, values)
    target_value = _target_value(query, values)
    if mode == "all":
        mode, count = "exact", len(fields)
    if len(fields) < 2 or target_value is None or not isinstance(count, int):
        return None
    if not (0 < count <= len(fields)):
        return None
    spans = [(match.start(), match.end())]
    spans.extend(_member_spans(query, fields, consent_fields, values))
    return CardinalityClaim(
        domain=CONSENT_CHANNEL_DOMAIN,
        members=fields,
        operator=mode,  # type: ignore[arg-type]
        count=count,
        target_value=target_value,
        quantifier_span=(match.start(), match.end()),
        footprint=(min(item[0] for item in spans), max(item[1] for item in spans)),
    )


class _InvalidConsentExpression(ValueError):
    pass


def _comparison_field(
    expression: event_ir.Condition,
) -> str | None:
    if not isinstance(expression, event_ir.Comparison):
        return None
    for field, literal in (
        (expression.left, expression.right),
        (expression.right, expression.left),
    ):
        if isinstance(field, event_ir.FieldRef) and isinstance(literal, event_ir.Literal):
            return field.name
    return None


def _project_consent_expression(
    expression: event_ir.Condition,
    *,
    consent_fields: frozenset[str],
) -> event_ir.Condition | None:
    """Remove unrelated audience atoms while preserving consent topology.

    A full audience is normally ``AND(other filters, consent formula)``.  Those
    unrelated conjuncts are neutral for the consent truth table.  An unrelated
    disjunct is not neutral—it would let a row bypass consent—so a mixed OR is
    rejected instead of being projected optimistically.
    """

    if isinstance(expression, event_ir.Comparison):
        return expression if _comparison_field(expression) in consent_fields else None
    if isinstance(expression, event_ir.Not):
        operand = _project_consent_expression(
            expression.operand, consent_fields=consent_fields
        )
        return event_ir.Not(operand) if operand is not None else None
    if isinstance(expression, (event_ir.And, event_ir.Or)):
        projected = [
            _project_consent_expression(operand, consent_fields=consent_fields)
            for operand in expression.operands
        ]
        present = [operand for operand in projected if operand is not None]
        if isinstance(expression, event_ir.Or) and present and len(present) != len(projected):
            raise _InvalidConsentExpression(
                "consent condition is mixed with an unrelated OR branch"
            )
        if not present:
            return None
        if len(present) == 1:
            return present[0]
        return type(expression)(tuple(present))
    return None


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
    if mode == "all":
        mode, count = "exact", len(fields)
    if not isinstance(count, int):
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
            projected_expression = _project_consent_expression(
                expression,
                consent_fields=frozenset(consent_fields),
            )
            if projected_expression is None:
                raise _InvalidConsentExpression(
                    "requested consent fields are absent from the Boolean expression"
                )
            for bits in itertools.product((False, True), repeat=len(fields)):
                assignment = dict(zip(fields, bits, strict=True))
                actual = _evaluate(
                    projected_expression,
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
    consent_fields, values = contract
    fields = _requested_fields(query, consent_fields, values)
    target_value = _target_value(query, values)
    mode, count, _match = quantifier
    if mode == "all":
        mode, count = "exact", len(fields)
    if mode != "exact" or not isinstance(count, int):
        return None
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
    # **이 주장이 소유한 리터럴만 정산한다.** 예전 조건은 *문장의 모든* 리터럴이 수량자 매치
    # 안에 있을 것을 요구했고, 그래서 무관한 리터럴 하나(``최근 30일``)가 구제를 죽였다
    # (감사 #47). 카디널리티는 집합 술어이므로 그 사정거리는 수량자 어구와 멤버 표면어이고,
    # 그 밖의 리터럴은 **다른 절의 것**이다 — 다른 절을 이 주장이 책임질 수는 없다.
    #
    # 이 완화가 절을 잃지 않는 이유: 사정거리 밖 리터럴은 여전히 리터럴 정산
    # (:func:`canonical_audience_claims.literal_claim_issues`)이 요구하므로, 그 절을 아무도
    # 컴파일하지 않으면 문장은 그대로 막힌다. 여기서 푸는 것은 **엉뚱한 절의 책임**뿐이다.
    claim = detect_cardinality_claim(query, catalog)
    if claim is None:
        return None
    owned = frozenset(
        index
        for index, binding in enumerate(rows)
        if isinstance(binding, Mapping)
        and isinstance(binding.get("start"), int)
        and isinstance(binding.get("end"), int)
        and claim.owns(int(binding["start"]), int(binding["end"]))
    )
    # 빈 집합은 정상이다 — ``정확히 두 개`` 처럼 수량자가 한국어 수사면 리터럴 원자가 아예
    # 없다. 정산할 것이 없다는 뜻이고, 이 축의 실제 안전장치는 진리표 증명이다.
    if not owned <= validation.consumed_binding_indices:
        return None
    return expression


__all__ = [
    "CONSENT_CARDINALITY_QUANTIFIER_RE",
    "CONSENT_CHANNEL_DOMAIN",
    "CardinalityClaim",
    "ConsentCardinalityValidation",
    "detect_cardinality_claim",
    "synthesize_exact_consent_cardinality",
    "validate_consent_cardinality",
]
