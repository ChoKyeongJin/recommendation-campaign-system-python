"""타겟팅 IR — LLM 이 SQL 대신 내놓는 닫힌 표현식.

배경: 템플릿 빌더가 못 만드는 요청은 LLM 이 SQL 을 직접 써 왔다. 출력 공간이 자유 SQL 이면
'2019년 top10 상품'만 계산하고 정작 그 상품을 산 **고객**을 빼먹는 식의 오류가 계속 나오고,
사후 의미검증이 그걸 잡아내면 사용자는 결과를 못 받는다. 규칙을 프롬프트에 더 적는 방식으로는
표현 형태가 늘어날수록 같은 사고가 반복된다.

이 모듈은 LLM 의 출력 공간을 다음 문법으로 닫는다:

    member_set := all | member_filter(canonical) | age(min,max)
                | relation(name, exists, windowDays?, entitySet?)
                | aggregate_threshold(relation, metric_id, aggregation_scope,
                                      operator, threshold, period|windowDays?)
                | and[...] | or[...] | not(...)

효과는 두 가지다.
  1) 회원 투영(SELECT DISTINCT 회원키)은 컴파일러가 항상 붙인다 — '대상을 잊은 SQL' 이 문법적으로
     표현 불가능해진다.
  2) 모든 리프가 레지스트리(member_target_filters.json)에 대해 검증된다 — 없는 컬럼·값·테이블을
     지어낼 수 없다. 검증은 LLM 판단이 아니라 사전 대조다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 물리 매핑과 방언 렌더러는 호출자가 주입한다.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import lexicon_patterns
from calendar_window import calendar_window_from_parts, parse_calendar_window
from entity_set import (
    build_derived_set_ast,
    compile_entity_set_predicate,
    entity_set_capability,
)

# ---------------------------------------------------------------------------
# Canonical, typed targeting expression and condition-claim contracts.
#
# The module predates these types and also owns the dict-based LLM schema and
# SQL compiler below.  The two APIs intentionally remain separate: callers may
# adopt the typed tree as a planning/ownership contract without changing the
# legacy compiler payload in the same release.
# ---------------------------------------------------------------------------

SourceSpan = tuple[int, int]

_OR_ALIASES = frozenset(
    {
        "or",
        "any",
        "any_of",
        "union",
        "||",
        "|",
        "+",
        *lexicon_patterns.vocabulary("or_connective"),
    }
)
_AND_ALIASES = frozenset(lexicon_patterns.vocabulary("targeting_and_alias"))
_NOT_ALIASES = frozenset(lexicon_patterns.vocabulary("targeting_not_alias"))

# These fields describe where a predicate came from, not what it means.  They
# are retained by ``to_dict`` but excluded recursively from semantic hashes.
_PROVENANCE_PAYLOAD_KEYS = frozenset(
    {
        "evidence",
        "evidence_text",
        "evidenceText",
        "evidence_span",
        "evidenceSpan",
        "source_text",
        "sourceText",
        "sourceSpan",
        "raw_text",
        "rawText",
        "source_span",
        "source_spans",
        "_source_span",
    }
)

CLAIM_STATUSES = frozenset({"resolved", "unresolved"})
CLAIM_DISPOSITIONS = frozenset(
    {
        "owned",
        "suppressed_duplicate",
        "rejected_conflict",
        "unresolved",
        "unsupported",
    }
)


class TargetingExpressionInvariantError(ValueError):
    """Raised when serialized typed IR disagrees with its content hash."""


class ConditionClaimInvariantError(ValueError):
    """Raised when condition claims violate ownership invariants."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(str(issue) for issue in issues)
        super().__init__("; ".join(self.issues))


def _canonical_json(value: Any) -> str:
    """Return a deterministic JSON encoding and reject non-JSON-safe values."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-safe") from exc


def _content_hash(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _normalize_token(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_source_spans(value: Any) -> tuple[SourceSpan, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping) or (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
    ):
        values = [value]
    elif isinstance(value, (tuple, list)):
        values = list(value)
    else:
        raise ValueError("source_spans must be a sequence of spans")

    normalized: list[SourceSpan] = []
    for span in values:
        if isinstance(span, Mapping):
            start, end = span.get("start"), span.get("end")
        elif isinstance(span, (tuple, list)) and len(span) == 2:
            start, end = span
        else:
            raise ValueError("each source span must contain start and end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            raise ValueError("source spans require integer bounds with 0 <= start < end")
        normalized.append((start, end))
    # A claim treats evidence locations as a set.  Sorting and de-duplicating
    # makes IDs stable when parsers discover the same spans in a different order.
    return tuple(sorted(set(normalized)))


def _serialize_source_spans(spans: tuple[SourceSpan, ...]) -> list[dict[str, int]]:
    return [{"start": start, "end": end} for start, end in spans]


def _strip_semantic_provenance(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_semantic_provenance(child)
            for key, child in value.items()
            if str(key) not in _PROVENANCE_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [_strip_semantic_provenance(child) for child in value]
    if isinstance(value, tuple):
        return [_strip_semantic_provenance(child) for child in value]
    return value


def _normalize_operator(value: Any, expected: str) -> str:
    token = _normalize_token(value, field_name="operator").casefold().replace("-", "_").replace(" ", "_")
    aliases = _AND_ALIASES if expected == "and" else _OR_ALIASES
    if token not in aliases:
        raise ValueError(f"operator {value!r} is not an alias for {expected}")
    return expected


@dataclass(frozen=True)
class TargetingExpression:
    """Base class for the canonical Boolean targeting tree.

    Node IDs are derived from semantic content.  Evidence positions and raw
    evidence text therefore do not split the same predicate into parser-specific
    node identities.
    """

    _canonical_fingerprint: str = field(init=False, repr=False, compare=False)
    _expression_node_id: str = field(init=False, repr=False, compare=False)

    TYPE: ClassVar[str] = "expression"

    @property
    def canonical_fingerprint(self) -> str:
        return self._canonical_fingerprint

    @property
    def fingerprint(self) -> str:
        """Short alias used by ownership and comparison code."""

        return self._canonical_fingerprint

    @property
    def expression_node_id(self) -> str:
        return self._expression_node_id

    @property
    def node_id(self) -> str:
        return self._expression_node_id

    def _set_identity(self, semantic_content: Any) -> None:
        fingerprint = hashlib.sha256(_canonical_json(semantic_content).encode("utf-8")).hexdigest()
        object.__setattr__(self, "_canonical_fingerprint", fingerprint)
        object.__setattr__(self, "_expression_node_id", f"expr_{fingerprint}")

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TargetingExpression":
        return targeting_expression_from_dict(payload)


@dataclass(frozen=True)
class PredicateRef(TargetingExpression):
    predicate_kind: str
    semantic_key: str
    source_spans: tuple[SourceSpan, ...] = ()
    payload: Any = field(default_factory=dict, compare=False, hash=False)
    _payload_canonical: str = field(init=False, repr=False)

    TYPE: ClassVar[str] = "predicate_ref"

    def __post_init__(self) -> None:
        predicate_kind = _normalize_token(self.predicate_kind, field_name="predicate_kind")
        semantic_key = _normalize_token(self.semantic_key, field_name="semantic_key")
        spans = _normalize_source_spans(self.source_spans)
        payload_copy = copy.deepcopy(self.payload)
        payload_canonical = _canonical_json(payload_copy)
        object.__setattr__(self, "predicate_kind", predicate_kind)
        object.__setattr__(self, "semantic_key", semantic_key)
        object.__setattr__(self, "source_spans", spans)
        object.__setattr__(self, "payload", payload_copy)
        object.__setattr__(self, "_payload_canonical", payload_canonical)
        self._set_identity(
            {
                "type": self.TYPE,
                "predicate_kind": predicate_kind,
                "semantic_key": semantic_key,
                "payload": _strip_semantic_provenance(json.loads(payload_canonical)),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "expression_node_id": self.expression_node_id,
            "canonical_fingerprint": self.canonical_fingerprint,
            "predicate_kind": self.predicate_kind,
            "semantic_key": self.semantic_key,
            "source_spans": _serialize_source_spans(self.source_spans),
            "payload": json.loads(self._payload_canonical),
        }


@dataclass(frozen=True)
class And(TargetingExpression):
    children: tuple[TargetingExpression, ...]
    operator: str = "and"

    TYPE: ClassVar[str] = "and"

    def __post_init__(self) -> None:
        children = tuple(self.children)
        if len(children) < 2 or not all(isinstance(child, TargetingExpression) for child in children):
            raise ValueError("And requires at least two TargetingExpression children")
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "operator", _normalize_operator(self.operator, self.TYPE))
        self._set_identity(
            {"type": self.TYPE, "children": sorted(child.canonical_fingerprint for child in children)}
        )

    @property
    def operands(self) -> tuple[TargetingExpression, ...]:
        return self.children

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "expression_node_id": self.expression_node_id,
            "canonical_fingerprint": self.canonical_fingerprint,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class Or(TargetingExpression):
    children: tuple[TargetingExpression, ...]
    operator: str = "or"

    TYPE: ClassVar[str] = "or"

    def __post_init__(self) -> None:
        children = tuple(self.children)
        if len(children) < 2 or not all(isinstance(child, TargetingExpression) for child in children):
            raise ValueError("Or requires at least two TargetingExpression children")
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "operator", _normalize_operator(self.operator, self.TYPE))
        self._set_identity(
            {"type": self.TYPE, "children": sorted(child.canonical_fingerprint for child in children)}
        )

    @property
    def operands(self) -> tuple[TargetingExpression, ...]:
        return self.children

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "expression_node_id": self.expression_node_id,
            "canonical_fingerprint": self.canonical_fingerprint,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class Not(TargetingExpression):
    operand: TargetingExpression

    TYPE: ClassVar[str] = "not"

    def __post_init__(self) -> None:
        if not isinstance(self.operand, TargetingExpression):
            raise ValueError("Not requires a TargetingExpression operand")
        self._set_identity({"type": self.TYPE, "operand": self.operand.canonical_fingerprint})

    @property
    def child(self) -> TargetingExpression:
        return self.operand

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "expression_node_id": self.expression_node_id,
            "canonical_fingerprint": self.canonical_fingerprint,
            "operand": self.operand.to_dict(),
        }


def _serialized_expression_type(payload: Mapping[str, Any]) -> tuple[str, Any]:
    raw_type = payload.get("type", payload.get("operator"))
    if raw_type is not None:
        token = str(raw_type).strip().casefold().replace("-", "_").replace(" ", "_")
        if token in _AND_ALIASES:
            return "and", payload.get("children", payload.get("operands"))
        if token in _OR_ALIASES:
            return "or", payload.get("children", payload.get("operands"))
        if token in _NOT_ALIASES:
            return "not", payload.get("operand", payload.get("child"))
        if token in {"predicate", "predicate_ref", "predicateref"}:
            return "predicate_ref", None
    for alias in (*_AND_ALIASES, *_OR_ALIASES, *_NOT_ALIASES):
        if alias in payload:
            canonical = "and" if alias in _AND_ALIASES else "or" if alias in _OR_ALIASES else "not"
            return canonical, payload[alias]
    if "predicate_kind" in payload and "semantic_key" in payload:
        return "predicate_ref", None
    raise ValueError("serialized targeting expression has an unknown node type")


def _verify_serialized_expression_identity(node: TargetingExpression, payload: Mapping[str, Any]) -> None:
    expected_id = payload.get("expression_node_id")
    if expected_id is not None and expected_id != node.expression_node_id:
        raise TargetingExpressionInvariantError("serialized expression_node_id does not match node content")
    expected_fingerprint = payload.get("canonical_fingerprint")
    if expected_fingerprint is not None and expected_fingerprint != node.canonical_fingerprint:
        raise TargetingExpressionInvariantError("serialized canonical_fingerprint does not match node content")


def targeting_expression_from_dict(payload: Mapping[str, Any]) -> TargetingExpression:
    """Deserialize a canonical tree while accepting common Boolean aliases."""

    if not isinstance(payload, Mapping):
        raise ValueError("serialized targeting expression must be an object")
    node_type, child_payload = _serialized_expression_type(payload)
    if node_type == "predicate_ref":
        node: TargetingExpression = PredicateRef(
            predicate_kind=payload.get("predicate_kind"),
            semantic_key=payload.get("semantic_key"),
            source_spans=payload.get("source_spans", ()),
            payload=payload.get("payload", {}),
        )
    elif node_type == "not":
        if not isinstance(child_payload, Mapping):
            raise ValueError("Not.operand must be an expression object")
        node = Not(targeting_expression_from_dict(child_payload))
    else:
        if not isinstance(child_payload, (tuple, list)):
            raise ValueError(f"{node_type}.children must be an array")
        children = tuple(targeting_expression_from_dict(child) for child in child_payload)
        node = And(children, operator=str(payload.get("operator") or node_type)) if node_type == "and" else Or(
            children, operator=str(payload.get("operator") or node_type)
        )
    _verify_serialized_expression_identity(node, payload)
    return node


def targeting_expression_to_dict(expression: TargetingExpression) -> dict[str, Any]:
    if not isinstance(expression, TargetingExpression):
        raise TypeError("expression must be a TargetingExpression")
    return expression.to_dict()


def expression_fingerprint(expression: TargetingExpression) -> str:
    return expression.canonical_fingerprint


def expression_node_id(expression: TargetingExpression) -> str:
    return expression.expression_node_id


@dataclass(frozen=True)
class ConditionClaim:
    """A parser's immutable ownership claim for one canonical predicate."""

    source_spans: tuple[SourceSpan, ...]
    expression_node_id: str
    parent_expression_node_id: str | None
    predicate_kind: str
    semantic_key: str
    owner: str | None
    status: str
    disposition: str
    origin_parser: str
    issues: tuple[Any, ...] = field(default_factory=tuple, compare=False, hash=False)
    claim_id: str = ""
    _issues_canonical: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        spans = _normalize_source_spans(self.source_spans)
        expression_id = _normalize_token(self.expression_node_id, field_name="expression_node_id")
        parent_id = self.parent_expression_node_id
        if parent_id is not None:
            parent_id = _normalize_token(parent_id, field_name="parent_expression_node_id")
            if parent_id == expression_id:
                raise ValueError("a condition claim cannot be its own parent")
        predicate_kind = _normalize_token(self.predicate_kind, field_name="predicate_kind")
        semantic_key = _normalize_token(self.semantic_key, field_name="semantic_key")
        owner = self.owner
        if owner is not None:
            owner = _normalize_token(owner, field_name="owner")
        status = _normalize_token(self.status, field_name="status")
        if status not in CLAIM_STATUSES:
            raise ValueError(f"status must be one of {sorted(CLAIM_STATUSES)}")
        disposition = _normalize_token(self.disposition, field_name="disposition")
        if disposition not in CLAIM_DISPOSITIONS:
            raise ValueError(f"disposition must be one of {sorted(CLAIM_DISPOSITIONS)}")
        origin_parser = _normalize_token(self.origin_parser, field_name="origin_parser")
        if not isinstance(self.issues, (tuple, list)):
            raise ValueError("issues must be an array")
        issues_copy = copy.deepcopy(list(self.issues))
        issues_canonical = _canonical_json(issues_copy)

        object.__setattr__(self, "source_spans", spans)
        object.__setattr__(self, "expression_node_id", expression_id)
        object.__setattr__(self, "parent_expression_node_id", parent_id)
        object.__setattr__(self, "predicate_kind", predicate_kind)
        object.__setattr__(self, "semantic_key", semantic_key)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "origin_parser", origin_parser)
        object.__setattr__(self, "issues", tuple(issues_copy))
        object.__setattr__(self, "_issues_canonical", issues_canonical)

        content = {
            "source_spans": _serialize_source_spans(spans),
            "expression_node_id": expression_id,
            "parent_expression_node_id": parent_id,
            "predicate_kind": predicate_kind,
            "semantic_key": semantic_key,
            "owner": owner,
            "status": status,
            "disposition": disposition,
            "origin_parser": origin_parser,
            "issues": json.loads(issues_canonical),
        }
        derived_id = _content_hash("claim", content)
        if self.claim_id and self.claim_id != derived_id:
            raise ConditionClaimInvariantError(("serialized claim_id does not match claim content",))
        object.__setattr__(self, "claim_id", derived_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "source_spans": _serialize_source_spans(self.source_spans),
            "expression_node_id": self.expression_node_id,
            "parent_expression_node_id": self.parent_expression_node_id,
            "predicate_kind": self.predicate_kind,
            "semantic_key": self.semantic_key,
            "owner": self.owner,
            "status": self.status,
            "disposition": self.disposition,
            "origin_parser": self.origin_parser,
            "issues": json.loads(self._issues_canonical),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConditionClaim":
        if not isinstance(payload, Mapping):
            raise ValueError("serialized condition claim must be an object")
        return cls(
            source_spans=payload.get("source_spans", ()),
            expression_node_id=payload.get("expression_node_id"),
            parent_expression_node_id=payload.get("parent_expression_node_id"),
            predicate_kind=payload.get("predicate_kind"),
            semantic_key=payload.get("semantic_key"),
            owner=payload.get("owner"),
            status=payload.get("status"),
            disposition=payload.get("disposition"),
            origin_parser=payload.get("origin_parser"),
            issues=payload.get("issues", ()),
            claim_id=str(payload.get("claim_id") or ""),
        )


def condition_claim_invariant_issues(claims: Iterable[ConditionClaim]) -> tuple[str, ...]:
    """Return deterministic ownership errors without mutating claims.

    Two parsers may report the same predicate, but only one claim may own a
    semantic node/source span.  Other equivalent claims must be explicitly
    marked ``suppressed_duplicate`` (or carry another non-owning disposition).
    """

    materialized = tuple(claims)
    issues: list[str] = []
    seen_claim_ids: set[str] = set()
    node_semantics: dict[str, tuple[str, str]] = {}
    owned_nodes: dict[tuple[str, str, str], ConditionClaim] = {}
    owned_spans: dict[SourceSpan, ConditionClaim] = {}
    suppressed_claims: list[ConditionClaim] = []

    for index, claim in enumerate(materialized):
        if not isinstance(claim, ConditionClaim):
            issues.append(f"claims[{index}] is not a ConditionClaim")
            continue
        if claim.claim_id in seen_claim_ids:
            issues.append(f"duplicate claim_id: {claim.claim_id}")
        seen_claim_ids.add(claim.claim_id)

        semantic_identity = (claim.predicate_kind, claim.semantic_key)
        previous_semantics = node_semantics.setdefault(claim.expression_node_id, semantic_identity)
        if previous_semantics != semantic_identity:
            issues.append(
                f"expression node {claim.expression_node_id} has multiple semantic identities"
            )

        if claim.disposition in {"owned", "suppressed_duplicate"} and claim.owner is None:
            issues.append(f"{claim.disposition} claim {claim.claim_id} requires an owner")

        if claim.disposition in {"owned", "suppressed_duplicate"} and claim.status != "resolved":
            issues.append(f"{claim.disposition} claim {claim.claim_id} must be resolved")
        if claim.disposition in {"unresolved", "unsupported"} and claim.status != "unresolved":
            issues.append(f"{claim.disposition} claim {claim.claim_id} must be unresolved")

        if claim.disposition == "suppressed_duplicate":
            suppressed_claims.append(claim)

        if claim.disposition != "owned":
            continue

        node_key = (claim.expression_node_id, claim.predicate_kind, claim.semantic_key)
        previous_node_owner = owned_nodes.get(node_key)
        if previous_node_owner is not None:
            issues.append(
                "duplicate owned expression node: "
                f"{claim.expression_node_id} ({previous_node_owner.owner!r}, {claim.owner!r})"
            )
        else:
            owned_nodes[node_key] = claim

        for span in claim.source_spans:
            previous_span_owner = owned_spans.get(span)
            if previous_span_owner is not None:
                issues.append(
                    "duplicate owned source span: "
                    f"{span[0]}:{span[1]} "
                    f"({previous_span_owner.semantic_key!r}, {claim.semantic_key!r}) "
                    f"({previous_span_owner.owner!r}, {claim.owner!r})"
                )
            else:
                owned_spans[span] = claim

    for claim in suppressed_claims:
        node_key = (claim.expression_node_id, claim.predicate_kind, claim.semantic_key)
        if node_key not in owned_nodes:
            issues.append(f"suppressed duplicate {claim.claim_id} has no owned canonical claim")

    return tuple(sorted(set(issues)))


def validate_condition_claims(claims: Iterable[ConditionClaim]) -> tuple[ConditionClaim, ...]:
    materialized = tuple(claims)
    issues = condition_claim_invariant_issues(materialized)
    if issues:
        raise ConditionClaimInvariantError(issues)
    return materialized


# Explicit alias for callers that name the operation after the contract rather
# than the collection being validated.
validate_condition_claim_invariants = validate_condition_claims


MAX_DEPTH = 6
MAX_NODES = 40
_COMBINATORS = ("and", "or", "not")
_LEAVES = ("all", "member_filter", "age", "relation", "aggregate_threshold")


class TargetingExpressionError(ValueError):
    """IR 이 문법·어휘·물리 매핑 중 하나를 위반했다(어느 것인지 메시지에 남긴다)."""


def _relative_date(days: int) -> str:
    return f"CONVERT(CHAR(8), DATEADD(DAY, -{int(days)}, GETDATE()), 112)"


# 기간의 자유 표현 슬롯. 연/월 이외의 달력 표현(분기·반기·특정일)과 시점 앵커('작년'·'7년전')까지 한
# 필드로 받고 해석은 calendar_window 가 한다 — 표현형이 늘어도 스키마를 늘리지 않는다(LLM 이 문장의
# 기간을 그대로 옮긴다). 앵커 해석을 LLM 에 맡기면 연도 산술을 틀린다('7년전'을 2016년으로 계산한
# 사례) — 날짜 산술은 결정론이 갖고, LLM 은 어느 표현이 기간인지만 고른다.
_PERIOD_FIELD = {
    "type": ["string", "null"],
    "description": (
        "기간 표현을 원문 그대로(예: '2019년 3월', '2019년 2분기', '2019년 상반기', '2019-03-05', "
        "'2019년', '작년 하반기', '7년전 상반기'). 연도를 직접 계산해 바꾸지 말고 문장에 쓰인 대로 옮겨라. "
        "상대 기간(최근 N일)은 windowDays 를 쓴다."
    ),
}


def targeting_expression_json_schema(
    entity_set_config: dict[str, Any],
    canonicals: dict[str, dict[str, Any]],
    aggregate_capabilities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LLM tool 스키마. 어휘(canonical/관계/엔터티/지표)는 레지스트리에서 그대로 열거한다."""
    relations = sorted(str(name) for name in (entity_set_config.get("relations") or {}))
    entities = sorted(str(name) for name in (entity_set_config.get("entities") or {}))
    measures = sorted(str(name) for name in (entity_set_config.get("measures") or {}))
    filter_dimensions = sorted(str(name) for name in (entity_set_config.get("filters") or {}))
    aggregate_capabilities = aggregate_capabilities or {}
    aggregate_metrics = sorted(str(name) for name in aggregate_capabilities)
    aggregate_relations = sorted({
        str(spec.get("relation"))
        for spec in aggregate_capabilities.values()
        if isinstance(spec, dict) and spec.get("relation")
    })
    aggregate_scopes = sorted({
        str(scope)
        for spec in aggregate_capabilities.values()
        if isinstance(spec, dict)
        for scope in (spec.get("scopes") or {})
    })
    entity_set = {
        "type": "object",
        "description": "순위로 정의되는 엔터티 집합(예: 2019년 판매수량 상위 10개 상품).",
        "properties": {
            "entity": {"type": "string", "enum": entities},
            "measure": {"type": "string", "enum": measures},
            "direction": {"type": "string", "enum": ["top", "bottom"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "rankRelation": {"type": "string", "enum": relations, "description": "순위를 계산할 관계(기본: 구매)."},
            "windowDays": {"type": ["integer", "null"], "description": "상대 기간(일). 절대 기간은 period/year 로 준다."},
            "year": {"type": ["integer", "null"], "description": "절대 연도(예: 2019). month 와 함께 주면 그 달."},
            "month": {"type": ["integer", "null"], "minimum": 1, "maximum": 12, "description": "절대 월(1~12). year 필요."},
            "period": _PERIOD_FIELD,
            "cardinality": {
                "type": ["object", "null"],
                "description": (
                    "이 랭킹 집합과 회원 행동 집합의 서로 다른 엔터티 교집합 개수 조건."
                ),
                "properties": {
                    "operator": {
                        "type": "string",
                        "enum": ["=", ">", ">=", "<", "<="],
                    },
                    "value": {"type": "integer", "minimum": 0, "maximum": 1000},
                },
                "required": ["operator", "value"],
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"const": "dimension_filter"},
                        "dimension": {"type": "string", "enum": filter_dimensions},
                        "operator": {"type": "string", "enum": ["equals", "contains"]},
                        "value": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "dimension", "operator", "value"],
                },
            },
        },
        "required": ["entity", "measure", "direction", "limit"],
    }
    node: dict[str, Any] = {
        "type": "object",
        "description": "회원 집합 표현식. 반드시 아래 키 중 정확히 하나만 사용한다.",
        "properties": {
            "all": {"type": "boolean", "description": "조건 없는 전체 회원."},
            "member_filter": {
                "type": "string",
                "enum": sorted(canonicals),
                "description": "회원 속성 조건. 이 목록 밖의 값은 사용할 수 없다.",
            },
            "age": {
                "type": "object",
                "properties": {"min": {"type": ["integer", "null"]}, "max": {"type": ["integer", "null"]}},
            },
            "relation": {
                "type": "object",
                "description": "행동 관계의 존재/부재. entitySet 을 주면 그 집합에 한정한다.",
                "properties": {
                    "name": {"type": "string", "enum": relations},
                    "exists": {"type": "boolean"},
                    "windowDays": {"type": ["integer", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "month": {"type": ["integer", "null"], "minimum": 1, "maximum": 12},
                    "period": _PERIOD_FIELD,
                    "entitySet": entity_set,
                },
                "required": ["name", "exists"],
            },
            "aggregate_threshold": {
                "type": "object",
                "description": (
                    "회원별 행동 지표 임계 조건. 컬럼명은 쓰지 않고 metric_id만 고른다. "
                    "예: 같은 브랜드에서 2개 이상 구매 = relation purchase, "
                    "metric_id total_item_quantity, aggregation_scope per_brand, operator >=, threshold 2."
                ),
                "properties": {
                    "relation": {"type": "string", "enum": aggregate_relations},
                    "metric_id": {"type": "string", "enum": aggregate_metrics},
                    "aggregation_scope": {"type": "string", "enum": aggregate_scopes},
                    "operator": {"type": "string", "enum": ["=", ">", ">=", "<", "<="]},
                    "threshold": {"type": "number"},
                    "windowDays": {"type": ["integer", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "month": {"type": ["integer", "null"], "minimum": 1, "maximum": 12},
                    "period": _PERIOD_FIELD,
                },
                "required": ["relation", "metric_id", "aggregation_scope", "operator", "threshold"],
            },
            "and": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "or": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "not": {"$ref": "#/$defs/node"},
        },
    }
    return {
        "type": "object",
        "$defs": {"node": node},
        "properties": {
            "expression": {"$ref": "#/$defs/node"},
            "unsupported": {
                "type": ["string", "null"],
                "description": "이 문법으로 표현할 수 없으면 사유를 적고 expression 은 생략한다.",
            },
        },
        "required": ["expression"],
    }


def _window(payload: dict[str, Any]) -> dict[str, Any] | None:
    """LLM 이 준 기간 슬롯을 창으로 해석한다. 달력 규칙은 calendar_window 가 단일 소유한다.

    period(자유 표현) → year/month(구조화) → windowDays(상대) 순. 예전에는 year 정수 하나뿐이라
    '2019년 3월'을 LLM 이 표현할 수단 자체가 없어 연 단위로 뭉개졌다."""
    period = payload.get("period")
    if isinstance(period, str) and period.strip():
        window = parse_calendar_window(period)
        if window is not None:
            return window
    absolute = calendar_window_from_parts(payload.get("year"), payload.get("month"))
    if absolute is not None:
        return absolute
    days = payload.get("windowDays")
    if isinstance(days, int) and days > 0:
        return {"days": days, "label": f"최근 {days}일"}
    return None


def validate_targeting_expression(
    node: Any,
    entity_set_config: dict[str, Any],
    canonicals: dict[str, dict[str, Any]],
    aggregate_capabilities: dict[str, dict[str, Any]] | None = None,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    """문법·어휘·물리 매핑을 사전 대조한다. 위반은 예외로 즉시 드러낸다(조용한 축소 금지)."""
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > MAX_DEPTH or counter[0] > MAX_NODES:
        raise TargetingExpressionError("표현식이 너무 깊거나 큽니다.")
    if not isinstance(node, dict):
        raise TargetingExpressionError("표현식 노드는 객체여야 합니다.")
    keys = [key for key in (*_LEAVES, *_COMBINATORS) if key in node]
    if len(keys) != 1:
        raise TargetingExpressionError(f"노드에는 정확히 하나의 키가 필요합니다: {sorted(node)}")
    key = keys[0]

    if key == "all":
        return
    if key == "member_filter":
        if str(node["member_filter"]) not in canonicals:
            raise TargetingExpressionError(f"등록되지 않은 회원 조건입니다: {node['member_filter']}")
        return
    if key == "age":
        age = node["age"]
        if not isinstance(age, dict) or not any(isinstance(age.get(bound), int) for bound in ("min", "max")):
            raise TargetingExpressionError("age 에는 min 또는 max 가 필요합니다.")
        return
    if key == "relation":
        relation = node["relation"]
        if not isinstance(relation, dict):
            raise TargetingExpressionError("relation 은 객체여야 합니다.")
        spec = (entity_set_config.get("relations") or {}).get(str(relation.get("name")))
        if not isinstance(spec, dict):
            raise TargetingExpressionError(f"등록되지 않은 관계입니다: {relation.get('name')}")
        if _window(relation) and not spec.get("dateColumn"):
            raise TargetingExpressionError(f"{relation.get('name')} 관계에는 기간 기준 컬럼이 없습니다.")
        entity_set = relation.get("entitySet")
        if entity_set is not None:
            reason = entity_set_capability(
                _entity_set_node(entity_set, str(relation.get("name")), entity_set_config), entity_set_config
            )
            if reason:
                raise TargetingExpressionError(f"엔터티 집합을 컴파일할 수 없습니다: {reason}")
        return
    if key == "aggregate_threshold":
        aggregate = node["aggregate_threshold"]
        if not isinstance(aggregate, dict):
            raise TargetingExpressionError("aggregate_threshold 는 객체여야 합니다.")
        capabilities = aggregate_capabilities or {}
        metric_id = str(aggregate.get("metric_id") or "")
        spec = capabilities.get(metric_id)
        if not isinstance(spec, dict):
            raise TargetingExpressionError(f"등록·검증되지 않은 집계 지표입니다: {metric_id}")
        relation = str(aggregate.get("relation") or "")
        if relation != str(spec.get("relation") or ""):
            raise TargetingExpressionError(
                f"집계 지표 {metric_id}는 {relation!r} 관계에서 사용할 수 없습니다."
            )
        scope = str(aggregate.get("aggregation_scope") or "")
        scope_spec = (spec.get("scopes") or {}).get(scope)
        if not isinstance(scope_spec, dict):
            raise TargetingExpressionError(
                f"집계 지표 {metric_id}는 {scope!r} 그룹 단위를 지원하지 않습니다."
            )
        if aggregate.get("operator") not in {"=", ">", ">=", "<", "<="}:
            raise TargetingExpressionError("aggregate_threshold 비교 연산자가 유효하지 않습니다.")
        threshold = aggregate.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise TargetingExpressionError("aggregate_threshold 임계값은 숫자여야 합니다.")
        if _window(aggregate) and not scope_spec.get("date_column"):
            raise TargetingExpressionError(f"집계 지표 {metric_id}에는 기간 기준 컬럼이 없습니다.")
        return
    if key == "not":
        validate_targeting_expression(
            node["not"], entity_set_config, canonicals, aggregate_capabilities,
            depth=depth + 1, counter=counter,
        )
        return
    operands = node[key]
    if not isinstance(operands, list) or len(operands) < 2:
        raise TargetingExpressionError(f"{key} 에는 2개 이상의 피연산자가 필요합니다.")
    for operand in operands:
        validate_targeting_expression(
            operand, entity_set_config, canonicals, aggregate_capabilities,
            depth=depth + 1, counter=counter,
        )


def _entity_set_node(
    payload: dict[str, Any],
    member_relation: str,
    config: dict[str, Any],
    *,
    negated: bool = False,
) -> dict[str, Any]:
    """LLM 이 준 엔터티 집합 조각을 1단계 노드 형태로 정규화한다(같은 컴파일러를 그대로 쓴다)."""
    if not isinstance(payload, dict):
        return {}
    node = {
        "relation": member_relation,
        "rankRelation": str(payload.get("rankRelation") or config.get("defaultRankRelation") or member_relation),
        "entity": str(payload.get("entity") or ""),
        "measure": str(payload.get("measure") or config.get("defaultMeasure") or ""),
        "direction": "bottom" if str(payload.get("direction")) == "bottom" else "top",
        "limit": int(payload.get("limit") or 10),
        "window": _window(payload),
        "cardinality": (
            dict(payload.get("cardinality"))
            if isinstance(payload.get("cardinality"), dict)
            else None
        ),
        "negated": negated,
    }
    node["derived_set_ast"] = build_derived_set_ast(
        member_relation=node["relation"],
        rank_relation=node["rankRelation"],
        entity=node["entity"],
        measure=node["measure"],
        direction=node["direction"],
        limit=node["limit"],
        window=node["window"],
        filters=payload.get("filters") if isinstance(payload.get("filters"), list) else None,
        cardinality=node["cardinality"],
        negated=negated,
    )
    return node


def compile_targeting_expression(
    node: dict[str, Any],
    entity_set_config: dict[str, Any],
    *,
    member_predicate: Callable[[str], str | None],
    aggregate_predicate: Callable[[dict[str, Any]], str | None] | None = None,
    member_alias: str = "B",
    member_key: str = "MEMBER_NO",
    age_column: str = "AGE",
    relative_date: Callable[[int], str] = _relative_date,
) -> str:
    """IR 을 회원(B) 기준 단일 술어로 컴파일한다.

    회원 투영은 호출자가 소유하므로 여기서는 술어만 만든다 — 어떤 표현식도 '회원이 아닌 것'을
    결과로 낼 수 없다.
    """
    keys = [key for key in (*_LEAVES, *_COMBINATORS) if key in node]
    key = keys[0]
    if key == "all":
        return "1 = 1"
    if key == "member_filter":
        predicate = member_predicate(str(node["member_filter"]))
        if not predicate:
            raise TargetingExpressionError(f"회원 조건을 컴파일할 수 없습니다: {node['member_filter']}")
        return predicate
    if key == "age":
        age = node["age"]
        bounds = []
        if isinstance(age.get("min"), int):
            bounds.append(f"{member_alias}.{age_column} >= {int(age['min'])}")
        if isinstance(age.get("max"), int):
            bounds.append(f"{member_alias}.{age_column} <= {int(age['max'])}")
        return " AND ".join(bounds) if len(bounds) == 1 else "(" + " AND ".join(bounds) + ")"
    if key == "aggregate_threshold":
        if aggregate_predicate is None:
            raise TargetingExpressionError("집계 임계 조건 컴파일러가 제공되지 않았습니다.")
        predicate = aggregate_predicate(node["aggregate_threshold"])
        if not predicate:
            raise TargetingExpressionError("집계 임계 조건을 컴파일할 수 없습니다.")
        return predicate
    if key == "not":
        return "NOT (" + compile_targeting_expression(
            node["not"], entity_set_config, member_predicate=member_predicate,
            aggregate_predicate=aggregate_predicate,
            member_alias=member_alias, member_key=member_key, age_column=age_column, relative_date=relative_date,
        ) + ")"
    if key in {"and", "or"}:
        joiner = " AND " if key == "and" else " OR "
        parts = [
            compile_targeting_expression(
                operand, entity_set_config, member_predicate=member_predicate,
                aggregate_predicate=aggregate_predicate,
                member_alias=member_alias, member_key=member_key, age_column=age_column, relative_date=relative_date,
            )
            for operand in node[key]
        ]
        return "(" + joiner.join(parts) + ")"
    return _compile_relation(
        node["relation"], entity_set_config,
        member_alias=member_alias, member_key=member_key, relative_date=relative_date,
    )


def _compile_relation(
    relation: dict[str, Any],
    config: dict[str, Any],
    *,
    member_alias: str,
    member_key: str,
    relative_date: Callable[[int], str],
) -> str:
    name = str(relation.get("name"))
    spec = config["relations"][name]
    exists = bool(relation.get("exists", True))
    entity_set = relation.get("entitySet")
    if isinstance(entity_set, dict) and entity_set:
        node = _entity_set_node(entity_set, name, config, negated=not exists)
        predicate = compile_entity_set_predicate(node, config, member_alias=member_alias, member_key=member_key)
        if predicate is None:
            raise TargetingExpressionError("엔터티 집합 술어를 컴파일하지 못했습니다.")
        return predicate

    alias = str(spec["outerAlias"])
    where = [str(spec["memberJoin"]).format(alias=alias, member=f"{member_alias}.{member_key}")]
    where.extend(str(condition).format(alias=alias) for condition in spec.get("conditions", []) or [])
    window = _window(relation)
    date_column = str(spec.get("dateColumn", "")).format(alias=alias)
    if window and date_column:
        if window.get("from"):
            where.append(f"{date_column} BETWEEN '{window['from']}' AND '{window['to']}'")
        else:
            where.append(f"{date_column} >= {relative_date(int(window['days']))}")
    keyword = "EXISTS" if exists else "NOT EXISTS"
    body = "\n      AND ".join(where)
    return f"{keyword} (\n    SELECT 1\n    FROM {spec['table']} {alias}\n    WHERE {body}\n  )"


def describe_targeting_expression(node: dict[str, Any], negated: bool = False) -> list[str]:
    """표현식이 담은 조건들의 canonical 요약(커버리지·트레이스 표시용).

    부정 라벨은 슬롯 경로의 표기(``non_<canonical>``)를 그대로 따른다 — 조건 커버리지 검증이
    두 경로를 같은 어휘로 대조해야 IR 후보만 '조건 미반영'으로 탈락하는 일이 없다.
    """
    labels: list[str] = []
    prefix = "non_" if negated else ""
    key = next((key for key in (*_LEAVES, *_COMBINATORS) if key in node), None)
    if key == "member_filter":
        labels.append(prefix + str(node["member_filter"]))
    elif key == "age":
        age = node["age"]
        labels.append(f"{prefix}age_{age.get('min') or ''}_{age.get('max') or ''}")
    elif key == "relation":
        relation = node["relation"]
        suffix = "" if relation.get("exists", True) and not negated else "_absent"
        labels.append(f"{relation.get('name')}{suffix}")
        if isinstance(relation.get("entitySet"), dict):
            entity_set = relation["entitySet"]
            labels.append(f"{entity_set.get('direction')}_{entity_set.get('limit')}_{entity_set.get('entity')}")
    elif key == "aggregate_threshold":
        aggregate = node["aggregate_threshold"]
        labels.append(prefix + str(aggregate.get("metric_id") or "aggregate_threshold"))
    elif key == "not":
        labels.extend(describe_targeting_expression(node["not"], not negated))
    elif key in {"and", "or"}:
        for operand in node[key]:
            labels.extend(describe_targeting_expression(operand, negated))
    return labels
