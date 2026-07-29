"""의미 AST(Semantic AST) — 파서 결과와 SQL 사이의 단일 정규형(순수 구조 모듈).

배경
----
자연어를 슬롯으로 뜯어 곧바로 SQL 로 컴파일하면 극성(포함/제외)·결합자(AND/OR)·소유자(owner)가
문자열 속성이나 임시 플래그로 흩어져, 어느 단계에서든 조용히 뒤집히거나 사라질 수 있다. 이 모듈은
그 셋을 **AST 노드로 보존**하는 타입 계층과, 그 위에서 도는 결정론 검사(정규화·정준화·충돌)를 소유한다.

  Predicate / Not / And / Or / Unknown

핵심 규칙
--------
  * 제외는 ``excluded=True`` 같은 필드가 아니라 ``Not`` 노드 또는 부정 연산자(not_in/neq)로 표현한다.
  * 극성 판단은 :func:`to_nnf` 에서 **한 번만** 한다. 컴파일러는 원문 표현('빼줘')을 다시 해석하지 않는다.
  * owner 가 확정되지 않은 조건은 삭제하지 않고 :class:`Unknown` 노드로 남긴다 — OR 안이라도 마찬가지다.
  * 충돌 판정은 파서 슬롯 개수가 아니라 정준화된 predicate 와 논리 경로(AND/OR 스코프)로 한다.

이 모듈은 도메인(컬럼·테이블·어휘)을 모른다. plan 투영과 SQL 대조는 ``plan_semantic_ast`` 가 맡는다.

실행: python -m pytest tests/test_semantic_ast.py -q
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal


# ── 어휘(닫힌 집합) ────────────────────────────────────────────────────────────

PredicateOperator = Literal[
    "eq", "neq", "in", "not_in", "contains", "gt", "gte", "lt", "lte", "between"
]

UnknownReason = Literal[
    "OWNER_UNRESOLVED",
    "OWNER_AMBIGUOUS",
    "DIMENSION_UNRESOLVED",
    "VALUE_UNRESOLVED",
    "UNSUPPORTED_EXPRESSION",
    "LOW_CONFIDENCE",
]

IssueCode = Literal[
    "OWNER_AMBIGUOUS",
    "OWNER_UNRESOLVED",
    "POLARITY_MISMATCH",
    "LOGICAL_OPERATOR_MISMATCH",
    "MISSING_CONDITION",
    "VALUE_MISMATCH",
    "FULL_CONFLICT",
    "PARTIAL_CONFLICT",
    "TAUTOLOGY",
    "UNSUPPORTED_EXPRESSION",
]

VerificationStatus = Literal["valid", "needs_clarification", "conflict", "unsafe"]

# 부정을 연산자로 흡수할 수 있는 쌍. 이 표가 부정 정규화(NNF)의 단일 소스다.
NEGATED_OPERATOR: dict[str, str] = {
    "eq": "neq",
    "neq": "eq",
    "in": "not_in",
    "not_in": "in",
    "gt": "lte",
    "lte": "gt",
    "gte": "lt",
    "lt": "gte",
}

# 정준 비교용 연산자 계열. 극성은 계열에 담지 않는다(SignedPredicate 가 따로 들고 있다).
OPERATOR_FAMILY: dict[str, str] = {
    "eq": "membership",
    "neq": "membership",
    "in": "membership",
    "not_in": "membership",
    "contains": "text",
    "gt": "range",
    "gte": "range",
    "lt": "range",
    "lte": "range",
    "between": "range",
}

# 연산자 자체가 음의 극성을 담은 것들(정준화 시 양의 연산자 + negative 로 분해된다).
NEGATIVE_OPERATORS = frozenset({"neq", "not_in"})

# 정준 연산자(음의 연산자를 양으로 되돌린 형태).
_POSITIVE_FORM: dict[str, str] = {"neq": "eq", "not_in": "in"}


# ── 값/스팬 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceSpan:
    """원문에서 이 조건이 유래한 구간. 진단·소유권 판정의 근거다."""

    start: int
    end: int
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True)
class Range:
    """닫힌/열린 구간 값(between 및 범위 충돌 계산용)."""

    lower: Any = None
    upper: Any = None
    include_lower: bool = True
    include_upper: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.lower,
            "to": self.upper,
            "include_lower": self.include_lower,
            "include_upper": self.include_upper,
        }


# ── AST 노드 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Predicate:
    """단일 조건. owner(누구의 조건인가)·dimension(무엇)·operator·values 를 모두 명시한다."""

    owner: str
    dimension: str
    operator: str
    values: tuple[Any, ...] = ()
    source_span: SourceSpan | None = None

    @property
    def type(self) -> str:
        return "predicate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "predicate",
            "predicate": {
                "owner": self.owner,
                "dimension": self.dimension,
                "operator": self.operator,
                "values": [_value_to_json(value) for value in self.values],
                **({"sourceSpan": self.source_span.to_dict()} if self.source_span else {}),
            },
        }


@dataclass(frozen=True)
class Not:
    """부정. 정규화 이후에는 연산자로 흡수되지 않는 노드(Unknown/contains/between) 위에만 남는다."""

    child: Any
    source_span: SourceSpan | None = None

    @property
    def type(self) -> str:
        return "not"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not", "child": to_dict(self.child)}


@dataclass(frozen=True)
class And:
    """교집합. children 이 비면 '전칭(TRUE)' — 조건 없음이지 공집합이 아니다."""

    children: tuple[Any, ...] = ()
    source_span: SourceSpan | None = None

    @property
    def type(self) -> str:
        return "and"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "and", "children": [to_dict(child) for child in self.children]}


@dataclass(frozen=True)
class Or:
    """합집합. 내부에 Unknown 이 있어도 축소하지 않는다(조건 유실 금지)."""

    children: tuple[Any, ...] = ()
    source_span: SourceSpan | None = None

    @property
    def type(self) -> str:
        return "or"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "or", "children": [to_dict(child) for child in self.children]}


@dataclass(frozen=True)
class Unknown:
    """해석하지 못한 조건. 삭제 대신 이 노드로 보존해 fail-close 근거가 되게 한다."""

    reason: str
    source_span: SourceSpan | None = None
    candidates: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def type(self) -> str:
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "unknown", "reason": self.reason}
        if self.source_span:
            payload["sourceSpan"] = self.source_span.to_dict()
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


SemanticExpr = Predicate | Not | And | Or | Unknown

TRUE = And(())


# ── 진단 타입 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerificationIssue:
    """검증 실패 항목. 코드는 닫힌 집합이며 메시지는 사용자 확인 질문으로도 쓰인다."""

    code: str
    message: str
    source_span: SourceSpan | None = None
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.source_span:
            payload["source_span"] = self.source_span.to_dict()
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class VerificationResult:
    """검증 결과. boolean 이 아니라 상태형이다 — 어느 단계도 '통과/실패' 로 뭉개지 않는다."""

    status: str
    expr: Any = None
    issues: tuple[VerificationIssue, ...] = ()
    warnings: tuple[VerificationIssue, ...] = ()

    @property
    def is_blocking(self) -> bool:
        """SQL 을 실행 가능한 상태로 내보내면 안 되는 상태인가(§fail-close)."""
        return self.status != "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "warnings": [warning.to_dict() for warning in self.warnings],
            **({"expr": to_dict(self.expr)} if self.expr is not None else {}),
        }


# ── 정준 predicate ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalPredicate:
    """비교용 정준형. 극성은 여기 없다(SignedPredicate.polarity 가 소유)."""

    owner: str
    dimension: str
    operator_family: str
    operator: str
    values: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "dimension": self.dimension,
            "operator_family": self.operator_family,
            "operator": self.operator,
            "values": [_value_to_json(value) for value in self.values],
        }


@dataclass(frozen=True)
class LogicalPathNode:
    """리프까지의 논리 경로 한 마디(어느 결합자의 몇 번째 자식인가)."""

    type: str  # and | or
    child_index: int


@dataclass(frozen=True)
class SignedPredicate:
    """리프 predicate 에 최종 적용된 극성과 논리 경로."""

    predicate: CanonicalPredicate
    polarity: str  # positive | negative
    path: tuple[LogicalPathNode, ...] = ()
    source_span: SourceSpan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate.to_dict(),
            "polarity": self.polarity,
            "path": [{"type": node.type, "child_index": node.child_index} for node in self.path],
            **({"source_span": self.source_span.to_dict()} if self.source_span else {}),
        }


# ── 직렬화 ────────────────────────────────────────────────────────────────────


def _value_to_json(value: Any) -> Any:
    if isinstance(value, Range):
        return value.to_dict()
    return value


def to_dict(expr: Any) -> Any:
    """AST 를 로그/디버그용 JSON 구조로 바꾼다."""
    if expr is None:
        return None
    if hasattr(expr, "to_dict"):
        return expr.to_dict()
    return expr


def describe(expr: Any) -> str:
    """사람이 읽는 한 줄 요약(진단 메시지·로그용)."""
    if isinstance(expr, Predicate):
        values = ", ".join(str(_value_to_json(value)) for value in expr.values)
        return f"{expr.owner}.{expr.dimension} {expr.operator}({values})"
    if isinstance(expr, Not):
        return f"NOT({describe(expr.child)})"
    if isinstance(expr, And):
        return "(" + " AND ".join(describe(child) for child in expr.children) + ")" if expr.children else "TRUE"
    if isinstance(expr, Or):
        return "(" + " OR ".join(describe(child) for child in expr.children) + ")" if expr.children else "FALSE"
    if isinstance(expr, Unknown):
        text = expr.source_span.text if expr.source_span else ""
        return f"UNKNOWN[{expr.reason}]{'(' + text + ')' if text else ''}"
    return str(expr)


# ── 정규화(NNF) ───────────────────────────────────────────────────────────────


def negate(expr: Any) -> Any:
    """한 노드의 부정을 만든다(드모르간 적용, 이중 부정 소거).

    연산자로 흡수할 수 있는 부정은 predicate 안으로 넣고, 그렇지 못한 노드(contains/between/Unknown)는
    ``Not`` 노드로 남긴다 — 부정을 흡수한 척하고 조용히 버리지 않는다.
    """
    if isinstance(expr, Not):
        return to_nnf(expr.child)
    if isinstance(expr, Predicate):
        flipped = NEGATED_OPERATOR.get(expr.operator)
        if flipped is None:
            return Not(expr, source_span=expr.source_span)
        return Predicate(expr.owner, expr.dimension, flipped, expr.values, expr.source_span)
    if isinstance(expr, And):
        return Or(tuple(negate(child) for child in expr.children), source_span=expr.source_span)
    if isinstance(expr, Or):
        return And(tuple(negate(child) for child in expr.children), source_span=expr.source_span)
    if isinstance(expr, Unknown):
        return Not(expr, source_span=expr.source_span)
    return Not(expr)


def to_nnf(expr: Any) -> Any:
    """부정 정규형으로 만든다 — ``Not`` 은 리프 바로 위에만 남고, 같은 결합자는 평탄화된다.

    극성 판단은 이 함수 하나에서만 일어난다. 이후 단계(충돌 검사·SQL 컴파일)는 노드 타입만 본다.
    """
    if isinstance(expr, Not):
        return negate(expr.child)
    if isinstance(expr, And):
        return And(_flatten(And, [to_nnf(child) for child in expr.children]), source_span=expr.source_span)
    if isinstance(expr, Or):
        return Or(_flatten(Or, [to_nnf(child) for child in expr.children]), source_span=expr.source_span)
    return expr


def _flatten(kind: type, children: list[Any]) -> tuple[Any, ...]:
    """같은 결합자의 중첩을 편다. 단항은 그대로 두어(부모가 처리) 구조 신호를 잃지 않는다."""
    flat: list[Any] = []
    for child in children:
        if isinstance(child, kind):
            flat.extend(child.children)
        else:
            flat.append(child)
    return tuple(flat)


def simplify(expr: Any) -> Any:
    """의미를 보존하는 축약만 한다 — 단항 결합자 제거와 빈 AND(전칭) 제거.

    Unknown 은 어떤 경우에도 제거하지 않는다. OR 안의 항도 제거하지 않는다.
    """
    if isinstance(expr, (And, Or)):
        children = [simplify(child) for child in expr.children]
        if isinstance(expr, And):
            children = [child for child in children if not (isinstance(child, And) and not child.children)]
        if len(children) == 1:
            return children[0]
        return type(expr)(tuple(children), source_span=expr.source_span)
    if isinstance(expr, Not):
        return Not(simplify(expr.child), source_span=expr.source_span)
    return expr


def iter_nodes(expr: Any) -> Iterator[Any]:
    """AST 의 모든 노드를 전위 순회로 내준다."""
    if expr is None:
        return
    yield expr
    if isinstance(expr, Not):
        yield from iter_nodes(expr.child)
    elif isinstance(expr, (And, Or)):
        for child in expr.children:
            yield from iter_nodes(child)


def unknown_nodes(expr: Any) -> list[Unknown]:
    """AST 에 남아 있는 미해결 조건들(fail-close 사유)."""
    return [node for node in iter_nodes(expr) if isinstance(node, Unknown)]


def has_or(expr: Any) -> bool:
    return any(isinstance(node, Or) and len(node.children) > 1 for node in iter_nodes(expr))


# ── 정준화 ────────────────────────────────────────────────────────────────────


def canonicalize_value(value: Any) -> Any:
    """값 정준화 — 문자열은 공백 정리 + casefold, 그 외는 그대로. 의미는 바꾸지 않는다."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, Range):
        return Range(
            canonicalize_value(value.lower),
            canonicalize_value(value.upper),
            value.include_lower,
            value.include_upper,
        )
    return value


def serialize_canonical_value(value: Any) -> str:
    return json.dumps(_value_to_json(canonicalize_value(value)), ensure_ascii=False, sort_keys=True)


def _sort_key(value: Any) -> str:
    return serialize_canonical_value(value)


def canonicalize_predicate(predicate: Predicate) -> tuple[CanonicalPredicate, str]:
    """정준 predicate 와 극성을 분리해 돌려준다(not_in → in + negative)."""
    operator = predicate.operator
    polarity = "negative" if operator in NEGATIVE_OPERATORS else "positive"
    operator = _POSITIVE_FORM.get(operator, operator)
    values = tuple(sorted((canonicalize_value(value) for value in predicate.values), key=_sort_key))
    canonical = CanonicalPredicate(
        owner=str(predicate.owner or "").strip().casefold(),
        dimension=str(predicate.dimension or "").strip().casefold(),
        operator_family=OPERATOR_FAMILY.get(operator, operator),
        operator=operator,
        values=values,
    )
    return canonical, polarity


def predicate_key(predicate: CanonicalPredicate) -> str:
    """동일 조건 판정을 위한 정준 키."""
    return json.dumps(
        [
            predicate.owner,
            predicate.dimension,
            predicate.operator_family,
            [_value_to_json(value) for value in predicate.values],
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def scope_key(predicate: CanonicalPredicate) -> str:
    """충돌 비교 단위(같은 owner·dimension·연산 계열)."""
    return json.dumps(
        [predicate.owner, predicate.dimension, predicate.operator_family], ensure_ascii=False
    )


# ── 부호 있는 predicate 추출 ──────────────────────────────────────────────────


def signed_predicates(expr: Any) -> list[SignedPredicate]:
    """리프 predicate 마다 최종 극성과 논리 경로를 뽑는다(입력은 NNF 가 아니어도 된다)."""
    normalized = to_nnf(expr)
    collected: list[SignedPredicate] = []
    _collect_signed(normalized, (), False, collected)
    return collected


def _collect_signed(
    expr: Any, path: tuple[LogicalPathNode, ...], negated: bool, out: list[SignedPredicate]
) -> None:
    if isinstance(expr, Predicate):
        canonical, polarity = canonicalize_predicate(expr)
        if negated:
            polarity = "negative" if polarity == "positive" else "positive"
        out.append(SignedPredicate(canonical, polarity, path, expr.source_span))
        return
    if isinstance(expr, Not):
        _collect_signed(expr.child, path, not negated, out)
        return
    if isinstance(expr, (And, Or)):
        kind = "and" if isinstance(expr, And) else "or"
        for index, child in enumerate(expr.children):
            _collect_signed(child, path + (LogicalPathNode(kind, index),), negated, out)


def _divergence(left: SignedPredicate, right: SignedPredicate) -> tuple[str, bool]:
    """두 리프가 갈라지는 지점의 결합자 종류와 '직계 형제인가' 를 돌려준다.

    루트는 AND 스코프로 본다(플랜 최상위는 조건들의 교집합이다).
    """
    index = 0
    while index < min(len(left.path), len(right.path)) and left.path[index] == right.path[index]:
        index += 1
    if index >= len(left.path) or index >= len(right.path):
        return "and", False
    node_type = left.path[index].type
    siblings = index == len(left.path) - 1 and index == len(right.path) - 1
    return node_type, siblings


# ── 집합/범위 충돌 ────────────────────────────────────────────────────────────


def detect_set_conflict(included: list[Any], excluded: list[Any]) -> dict[str, Any]:
    """IN(A) AND NOT IN(B) 의 판정: 교집합 없음 / A⊆B(전체) / 일부 겹침(부분)."""
    included_map = {serialize_canonical_value(value): value for value in included}
    excluded_keys = {serialize_canonical_value(value) for value in excluded}
    overlap = [value for key, value in included_map.items() if key in excluded_keys]
    if not overlap:
        return {"type": "none", "overlap": []}
    if len(overlap) == len(included_map) and included_map:
        return {"type": "full", "overlap": overlap}
    return {"type": "partial", "overlap": overlap}


def normalize_range_constraint(operator: str, values: tuple[Any, ...]) -> Range | None:
    """비교 연산자 하나를 구간으로 바꾼다. 표현할 수 없으면 None(판정 보류)."""
    if operator == "between":
        if len(values) == 1 and isinstance(values[0], Range):
            return values[0]
        if len(values) == 2:
            return Range(values[0], values[1])
        return None
    if not values:
        return None
    value = values[0]
    if operator == "gte":
        return Range(lower=value, upper=None, include_lower=True)
    if operator == "gt":
        return Range(lower=value, upper=None, include_lower=False)
    if operator == "lte":
        return Range(lower=None, upper=value, include_upper=True)
    if operator == "lt":
        return Range(lower=None, upper=value, include_upper=False)
    return None


def intersect_ranges(left: Range, right: Range) -> Range | None:
    """두 구간의 교집합. 비교 불가한 값 타입이 섞이면 None(판정 보류)."""
    lower, include_lower = left.lower, left.include_lower
    if right.lower is not None:
        if lower is None:
            lower, include_lower = right.lower, right.include_lower
        else:
            try:
                if right.lower > lower:
                    lower, include_lower = right.lower, right.include_lower
                elif right.lower == lower:
                    include_lower = include_lower and right.include_lower
            except TypeError:
                return None
    upper, include_upper = left.upper, left.include_upper
    if right.upper is not None:
        if upper is None:
            upper, include_upper = right.upper, right.include_upper
        else:
            try:
                if right.upper < upper:
                    upper, include_upper = right.upper, right.include_upper
                elif right.upper == upper:
                    include_upper = include_upper and right.include_upper
            except TypeError:
                return None
    return Range(lower, upper, include_lower, include_upper)


def is_empty_range(value: Range) -> bool:
    """구간이 공집합인가(비교 불가 타입은 False — 근거 없이 충돌로 몰지 않는다)."""
    if value.lower is None or value.upper is None:
        return False
    try:
        if value.lower > value.upper:
            return True
        if value.lower == value.upper:
            return not (value.include_lower and value.include_upper)
    except TypeError:
        return False
    return False


def detect_range_conflict(constraints: list[tuple[str, tuple[Any, ...]]]) -> dict[str, Any]:
    """같은 dimension 의 범위 조건들을 교집합으로 접어 공집합 여부를 본다."""
    current: Range | None = None
    for operator, values in constraints:
        span = normalize_range_constraint(operator, values)
        if span is None:
            return {"type": "none", "range": None}
        current = span if current is None else intersect_ranges(current, span)
        if current is None:
            return {"type": "none", "range": None}
    if current is not None and is_empty_range(current):
        return {"type": "full", "range": current}
    return {"type": "none", "range": current}


# ── 충돌 검사 ────────────────────────────────────────────────────────────────


def detect_conflicts(expr: Any) -> list[VerificationIssue]:
    """포함/제외 충돌과 항진식을 논리 스코프까지 반영해 찾는다.

    * 같은 AND 스코프의 ``P``/``NOT P`` → FULL_CONFLICT (겹침이 일부면 PARTIAL_CONFLICT)
    * 같은 OR 의 직계 형제 ``P``/``NOT P`` → TAUTOLOGY
    * 서로 다른 OR 분기에 흩어져 있으면 정상 조건이므로 판정하지 않는다.
    * owner 나 dimension 이 다르면 충돌이 아니다.
    """
    signed = signed_predicates(expr)
    issues: list[VerificationIssue] = []
    by_scope: dict[str, list[SignedPredicate]] = {}
    for item in signed:
        by_scope.setdefault(scope_key(item.predicate), []).append(item)

    for members in by_scope.values():
        issues.extend(_membership_conflicts(members))
        issues.extend(_range_conflicts(members))
    return issues


def _membership_conflicts(members: list[SignedPredicate]) -> list[VerificationIssue]:
    positives = [item for item in members if item.polarity == "positive" and item.predicate.operator_family == "membership"]
    negatives = [item for item in members if item.polarity == "negative" and item.predicate.operator_family == "membership"]
    issues: list[VerificationIssue] = []
    for positive in positives:
        for negative in negatives:
            conflict = detect_set_conflict(list(positive.predicate.values), list(negative.predicate.values))
            if conflict["type"] == "none":
                continue
            node_type, siblings = _divergence(positive, negative)
            overlap = [_value_to_json(value) for value in conflict["overlap"]]
            metadata = {
                "owner": positive.predicate.owner,
                "dimension": positive.predicate.dimension,
                "included": [_value_to_json(value) for value in positive.predicate.values],
                "excluded": [_value_to_json(value) for value in negative.predicate.values],
                "overlap": overlap,
            }
            label = f"{positive.predicate.owner}.{positive.predicate.dimension}"
            if node_type == "or":
                if not siblings:
                    continue  # 서로 다른 OR 분기 — 정상 조건이다
                issues.append(
                    VerificationIssue(
                        "TAUTOLOGY",
                        f"'{label}' 의 포함 조건과 제외 조건이 OR 로 묶여 모든 대상이 선택됩니다: {', '.join(map(str, overlap))}",
                        positive.source_span,
                        metadata,
                    )
                )
                continue
            if conflict["type"] == "full":
                issues.append(
                    VerificationIssue(
                        "FULL_CONFLICT",
                        f"'{label}' 을(를) 포함하면서 동시에 제외하고 있습니다: {', '.join(map(str, overlap))}",
                        positive.source_span,
                        metadata,
                    )
                )
            else:
                issues.append(
                    VerificationIssue(
                        "PARTIAL_CONFLICT",
                        f"'{label}' 의 포함 조건 일부가 제외 조건과 겹칩니다: {', '.join(map(str, overlap))}",
                        positive.source_span,
                        metadata,
                    )
                )
    return issues


def _range_conflicts(members: list[SignedPredicate]) -> list[VerificationIssue]:
    ranges = [
        item
        for item in members
        if item.predicate.operator_family == "range" and item.polarity == "positive"
    ]
    if len(ranges) < 2:
        return []
    # AND 스코프에 함께 있는 것들만 교집합으로 접는다(OR 분기는 서로 좁히지 않는다).
    conjunctive: list[SignedPredicate] = []
    for item in ranges:
        if all(_divergence(item, other)[0] == "and" for other in ranges if other is not item):
            conjunctive.append(item)
    if len(conjunctive) < 2:
        return []
    conflict = detect_range_conflict([(item.predicate.operator, item.predicate.values) for item in conjunctive])
    if conflict["type"] != "full":
        return []
    label = f"{conjunctive[0].predicate.owner}.{conjunctive[0].predicate.dimension}"
    return [
        VerificationIssue(
            "FULL_CONFLICT",
            f"'{label}' 범위 조건들이 서로 배타적이라 대상이 존재할 수 없습니다.",
            conjunctive[0].source_span,
            {
                "owner": conjunctive[0].predicate.owner,
                "dimension": conjunctive[0].predicate.dimension,
                "constraints": [
                    {"operator": item.predicate.operator, "values": [_value_to_json(v) for v in item.predicate.values]}
                    for item in conjunctive
                ],
            },
        )
    ]


# ── 표현식 검증 ───────────────────────────────────────────────────────────────


_UNKNOWN_ISSUE_CODE: dict[str, str] = {
    "OWNER_AMBIGUOUS": "OWNER_AMBIGUOUS",
    "OWNER_UNRESOLVED": "OWNER_UNRESOLVED",
    "DIMENSION_UNRESOLVED": "UNSUPPORTED_EXPRESSION",
    "VALUE_UNRESOLVED": "UNSUPPORTED_EXPRESSION",
    "UNSUPPORTED_EXPRESSION": "UNSUPPORTED_EXPRESSION",
    "LOW_CONFIDENCE": "UNSUPPORTED_EXPRESSION",
}


def verify_expression(expr: Any) -> VerificationResult:
    """정규화된 표현식의 의미 보존 상태를 판정한다(충돌 > 미해결 > 정상).

    부분 충돌·항진식·미해결은 모두 SQL 을 내보내지 않는 상태로 귀결된다 — 어느 쪽을 임의로 고르지 않는다.
    """
    normalized = to_nnf(expr)
    issues = list(detect_conflicts(normalized))
    unknowns = unknown_nodes(normalized)
    for node in unknowns:
        issues.append(
            VerificationIssue(
                _UNKNOWN_ISSUE_CODE.get(node.reason, "UNSUPPORTED_EXPRESSION"),
                _unknown_message(node),
                node.source_span,
                {"reason": node.reason, "candidates": list(node.candidates), **dict(node.metadata)},
            )
        )
    if any(issue.code == "FULL_CONFLICT" for issue in issues):
        return VerificationResult("conflict", normalized, tuple(issues))
    if issues:
        return VerificationResult("needs_clarification", normalized, tuple(issues))
    return VerificationResult("valid", normalized)


def _unknown_message(node: Unknown) -> str:
    text = node.source_span.text.strip() if node.source_span and node.source_span.text else ""
    if node.reason in ("OWNER_AMBIGUOUS", "OWNER_UNRESOLVED"):
        target = f"'{text}'" if text else "일부 조건"
        candidates = f" (후보: {', '.join(node.candidates)})" if node.candidates else ""
        return f"{target}이(가) 어느 대상의 조건인지 확인이 필요합니다.{candidates}"
    target = f"'{text}'" if text else "일부 조건"
    return f"{target}을(를) 실행 가능한 조건으로 해석하지 못했습니다."
