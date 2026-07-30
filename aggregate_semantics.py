"""SQL 합성 **전**에 실적 존재/부재 조건의 의미 충돌을 잡는 typed 검증.

파서가 틀릴 수 있다는 전제 위에 두는 마지막 방어선이다. '최근 90일 구매 수량 50만개 이상'(존재)과
'최근 90일 무주문'(부재)이 한 SQL 에 AND 로 들어가면 그 SQL 은 정의상 공집합인데, 문법적으로는
멀쩡해서 어떤 SQL 검증기도 잡지 못한다. 그래서 SQL 문자열이 아니라 **의미 IR** 위에서 판정한다.

판정 규칙(중요): 기간이 조금이라도 겹치면 충돌, 이 아니다. 그 규칙은 정상 조건을 막는다 —
'최근 6개월 구매 있고 최근 1개월 구매 없는' 고객은 2~6개월 전에 산 사람들로 공집합이 아니다.
확정 충돌은 **존재가 요구하는 이벤트 집합이 부재가 금지하는 집합에 통째로 들어갈 때**뿐이다:

    P의 기간 ⊆ N의 기간   그리고   P의 이벤트 조건 ⊆ N이 금지하는 이벤트 조건

셋 중 하나로 답한다: PROVEN_CONFLICT / PROVEN_SAFE / UNKNOWN. UNKNOWN(기간을 확정할 수 없음)은
모순 SQL 을 내보내는 대신 기간을 나눠 달라는 clarification 으로 보낸다.

파이썬 ``assert`` 는 쓰지 않는다 — ``-O`` 로 제거되는 문장은 사용자 입력 검증 안전장치가 될 수 없다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aggregate_parser_config
import event_ir
import event_semantic_registry

PRESENCE = "presence"
ABSENCE = "absence"

PROVEN_CONFLICT = "PROVEN_CONFLICT"
PROVEN_SAFE = "PROVEN_SAFE"
UNKNOWN = "UNKNOWN"

# Boolean-expression level semantic status.  These values intentionally do not
# encode compiler support: a semantically consistent expression can still be
# unsupported by the physical compiler.
CONSISTENT = "consistent"
CONTRADICTORY = "contradictory"
SEMANTIC_UNKNOWN = "unknown"

# 기간을 '평생'으로 표현할 때 쓰는 하한. 실제 데이터 시작보다 이르기만 하면 되고, 포함 관계 판정에만 쓴다.
BEGINNING_OF_TIME = datetime.min  # noqa: DTZ901 - sentinel, never a wall-clock instant


class AggregateSemanticConflict(Exception):
    """존재/부재 조건이 확정 충돌이다. 사용자에게는 이 예외가 아니라 메시지 키로 번역해 전달한다."""

    def __init__(self, code: str, positive_condition_id: str, negative_condition_id: str) -> None:
        super().__init__(f"{code}: {positive_condition_id} vs {negative_condition_id}")
        self.code = code
        self.positive_condition_id = positive_condition_id
        self.negative_condition_id = negative_condition_id


@dataclass(frozen=True)
class NormalizedWindow:
    """반개방 구간 ``[start, end)``. 롤링 창은 호출부가 이미 일수로 정규화해 넘긴다 —
    개월을 여기서 30일로 다시 환산하지 않는다(기존 기간 계산기와 기준이 갈리면 판정이 틀린다)."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class EventPredicate:
    """한 조건이 요구/금지하는 이벤트 집합.

    ``constraints`` 의 각 차원은 허용 집합이며 ``None`` 은 그 차원 전체(제한 없음)를 뜻한다.
    ``requires_event_presence`` 는 이 노드가 실제로 이벤트가 하나 이상 있어야 성립하는지다 —
    스냅샷 컬럼이나 left join 은 이벤트가 없어도 유지되므로 존재를 요구하지 않는다."""

    domain: str
    polarity: str
    window: NormalizedWindow | None
    constraints: Mapping[str, frozenset[str] | None]
    source_kind: str
    source_id: str
    requires_event_presence: bool = True


@dataclass(frozen=True)
class ConflictFinding:
    verdict: str
    code: str
    domain: str
    positive: EventPredicate
    negative: EventPredicate

    def as_log_fields(self) -> dict[str, object]:
        """구조화 로그용 필드. 사용자 원문이나 개인정보가 될 수 있는 문자열은 담지 않는다."""
        return {
            "conflict_code": self.code,
            "domain": self.domain,
            "positive_condition_id": self.positive.source_id,
            "negative_condition_id": self.negative.source_id,
            "positive_window": _window_repr(self.positive.window),
            "negative_window": _window_repr(self.negative.window),
            "positive_source_kind": self.positive.source_kind,
            "negative_source_kind": self.negative.source_kind,
        }


@dataclass(frozen=True)
class SemanticValidationIssue:
    """A machine-readable semantic issue; presentation is a later concern."""

    code: str
    branch_id: str | None = None
    domain: str | None = None
    positive_condition_id: str | None = None
    negative_condition_id: str | None = None


@dataclass(frozen=True)
class BranchValidationResult:
    branch_id: str
    status: str
    predicates: tuple[EventPredicate, ...]
    issues: tuple[SemanticValidationIssue, ...] = ()


@dataclass(frozen=True)
class BooleanValidationResult:
    status: str
    issues: tuple[SemanticValidationIssue, ...]
    branches: tuple[BranchValidationResult, ...]

    @property
    def verdict(self) -> str:
        """Compatibility projection for callers that still use the old tri-state."""

        return {
            CONSISTENT: PROVEN_SAFE,
            CONTRADICTORY: PROVEN_CONFLICT,
            SEMANTIC_UNKNOWN: UNKNOWN,
        }[self.status]


@dataclass
class ValidationResult:
    conflicts: list[ConflictFinding] = field(default_factory=list)
    unresolved: list[ConflictFinding] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.conflicts:
            return PROVEN_CONFLICT
        if self.unresolved:
            return UNKNOWN
        return PROVEN_SAFE

    def raise_if_conflicting(self) -> None:
        """확정 충돌을 typed 예외로 올린다(``assert`` 대신)."""
        if not self.conflicts:
            return
        finding = self.conflicts[0]
        raise AggregateSemanticConflict(
            finding.code, finding.positive.source_id, finding.negative.source_id
        )


def _window_repr(window: NormalizedWindow | None) -> str | None:
    if window is None:
        return None
    start = "-inf" if window.start == BEGINNING_OF_TIME else window.start.isoformat()
    return f"[{start}, {window.end.isoformat()})"


def rolling_window(anchor: datetime, days: int) -> NormalizedWindow:
    """'최근 N일' 창. 일수는 호출부(기존 기간 파서)가 확정한 값을 그대로 쓴다."""
    from datetime import timedelta

    return NormalizedWindow(start=anchor - timedelta(days=days), end=anchor)


def lifetime_window(anchor: datetime) -> NormalizedWindow:
    """'전 기간'. 어떤 유한 창의 부분집합도 아니므로 부재 조건과 충돌하지 않는다."""
    return NormalizedWindow(start=BEGINNING_OF_TIME, end=anchor)


def intersect(left: NormalizedWindow, right: NormalizedWindow) -> NormalizedWindow | None:
    start, end = max(left.start, right.start), min(left.end, right.end)
    return NormalizedWindow(start, end) if start < end else None


def window_is_subset(positive: NormalizedWindow, negative: NormalizedWindow) -> bool:
    """존재 기간이 부재 기간에 통째로 들어가는가(반개방 구간 기준)."""
    return negative.start <= positive.start and positive.end <= negative.end


def constraints_are_subset(
    positive: Mapping[str, frozenset[str] | None],
    negative: Mapping[str, frozenset[str] | None],
    dimensions: Iterable[str],
) -> bool:
    """모든 차원에서 존재가 허용하는 값이 부재가 금지하는 값에 포함되는가.

    ``None`` 은 그 차원 전체다 — 부재가 None(전체 금지)이면 어떤 존재 조건도 포함되고,
    존재가 None(전체 허용)인데 부재가 일부만 금지하면 포함되지 않는다."""
    for dimension in dimensions:
        allowed = positive.get(dimension)
        forbidden = negative.get(dimension)
        if forbidden is None:
            continue          # 부재가 이 차원 전체를 금지 → 어떤 존재 값도 그 안이다
        if allowed is None:
            return False      # 존재는 전체인데 부재는 일부만 → 금지 밖의 이벤트로 만족 가능
        if not allowed <= forbidden:
            return False
    return True


def classify_scope_relation(
    positive: Mapping[str, frozenset[str] | None],
    negative: Mapping[str, frozenset[str] | None],
    dimensions: Iterable[str],
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
) -> str:
    """Classify whether a positive event scope is covered by an absence scope.

    Different strings are never assumed disjoint.  Only an explicit registry
    relation can prove that.  ``None`` is the dimension universe: a constrained
    positive is a subset of an unconstrained absence, while an unconstrained
    positive has known room outside a constrained absence.
    """

    registry = semantic_registry or event_semantic_registry.registry()
    dimension_relations: list[str] = []
    for dimension in dimensions:
        allowed = positive.get(dimension)
        forbidden = negative.get(dimension)
        if forbidden is None:
            dimension_relations.append(
                event_semantic_registry.EQUAL
                if allowed is None
                else event_semantic_registry.SUBSET
            )
            continue
        if allowed is None:
            # The positive scope is the full dimension and the negative scope
            # names a proper registered subset.
            dimension_spec = registry.dimensions.get(dimension)
            if dimension_spec is not None and all(value in dimension_spec.values for value in forbidden):
                dimension_relations.append(event_semantic_registry.OVERLAP_POSSIBLE)
            else:
                dimension_relations.append(event_semantic_registry.UNKNOWN)
            continue

        all_covered = True
        all_disjoint = True
        has_known_outside = False
        for positive_value in allowed:
            relations = [
                registry.relation(dimension, positive_value, negative_value)
                for negative_value in forbidden
            ]
            covered = any(
                relation in (event_semantic_registry.EQUAL, event_semantic_registry.SUBSET)
                for relation in relations
            )
            all_covered = all_covered and covered
            value_disjoint = bool(relations) and all(
                relation == event_semantic_registry.DISJOINT for relation in relations
            )
            all_disjoint = all_disjoint and value_disjoint
            reverse_overlap = any(
                registry.relation(dimension, negative_value, positive_value)
                in (event_semantic_registry.SUBSET, event_semantic_registry.OVERLAP_POSSIBLE)
                for negative_value in forbidden
            )
            has_known_outside = has_known_outside or value_disjoint or reverse_overlap or any(
                relation == event_semantic_registry.OVERLAP_POSSIBLE for relation in relations
            )

        if all_covered:
            dimension_relations.append(
                event_semantic_registry.EQUAL
                if allowed == forbidden
                else event_semantic_registry.SUBSET
            )
        elif all_disjoint:
            dimension_relations.append(event_semantic_registry.DISJOINT)
        elif has_known_outside:
            dimension_relations.append(event_semantic_registry.OVERLAP_POSSIBLE)
        else:
            dimension_relations.append(event_semantic_registry.UNKNOWN)

    # Event dimensions are conjunctive.  One proven-disjoint dimension makes
    # the complete scopes disjoint; one proven outside portion is enough to
    # show the positive scope is not contained by the absence scope.  Unknown
    # only dominates when neither fact has been established.
    if event_semantic_registry.DISJOINT in dimension_relations:
        return event_semantic_registry.DISJOINT
    if event_semantic_registry.OVERLAP_POSSIBLE in dimension_relations:
        return event_semantic_registry.OVERLAP_POSSIBLE
    if event_semantic_registry.UNKNOWN in dimension_relations:
        return event_semantic_registry.UNKNOWN
    if event_semantic_registry.SUBSET in dimension_relations:
        return event_semantic_registry.SUBSET
    return event_semantic_registry.EQUAL


def classify_pair_detail(
    positive: EventPredicate,
    negative: EventPredicate,
    dimensions: Iterable[str],
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
) -> tuple[str, str | None]:
    if not positive.requires_event_presence:
        return PROVEN_SAFE, None
    if positive.window is None or negative.window is None:
        return UNKNOWN, "semantic_period_unresolved"
    scope_relation = classify_scope_relation(
        positive.constraints, negative.constraints, dimensions, semantic_registry
    )
    if scope_relation == event_semantic_registry.UNKNOWN:
        return UNKNOWN, "semantic_scope_unknown"
    if scope_relation in (event_semantic_registry.DISJOINT, event_semantic_registry.OVERLAP_POSSIBLE):
        return PROVEN_SAFE, None
    verdict = PROVEN_CONFLICT if window_is_subset(positive.window, negative.window) else PROVEN_SAFE
    return verdict, "presence_absence_conflict" if verdict == PROVEN_CONFLICT else None


def classify_pair(
    positive: EventPredicate,
    negative: EventPredicate,
    dimensions: Iterable[str],
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
) -> str:
    """존재/부재 한 쌍의 판정. 기간을 모르면 UNKNOWN(모순 SQL 대신 clarification)."""
    return classify_pair_detail(positive, negative, dimensions, semantic_registry)[0]


def validate(
    predicates: Sequence[EventPredicate],
    domains: Mapping[str, aggregate_parser_config.SemanticDomain] | None = None,
    *,
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
) -> ValidationResult:
    """같은 도메인의 존재×부재 쌍을 전부 판정한다. 도메인 정의(이벤트 차원)는 설정이 소유한다."""
    resolved_domains = domains if domains is not None else aggregate_parser_config.rules().semantic_domains
    registry = semantic_registry or event_semantic_registry.registry()
    result = ValidationResult()
    known_domains = set(resolved_domains)
    for predicate in predicates:
        if predicate.domain in known_domains:
            continue
        result.unresolved.append(ConflictFinding(
            UNKNOWN,
            "semantic_domain_unknown",
            predicate.domain,
            predicate,
            predicate,
        ))
    for domain_name, domain in resolved_domains.items():
        positives = [
            p for p in predicates if p.domain == domain_name and p.polarity == PRESENCE
        ]
        negatives = [
            p for p in predicates if p.domain == domain_name and p.polarity == ABSENCE
        ]
        for positive in positives:
            for negative in negatives:
                verdict, code = classify_pair_detail(
                    positive, negative, domain.event_dimensions, registry
                )
                if verdict == PROVEN_CONFLICT:
                    result.conflicts.append(ConflictFinding(
                        verdict, code or "presence_absence_conflict", domain_name, positive, negative,
                    ))
                elif verdict == UNKNOWN:
                    result.unresolved.append(ConflictFinding(
                        verdict, code or "semantic_scope_unknown", domain_name, positive, negative,
                    ))
    return result


PredicateFactory = Callable[[event_ir.Condition, bool], EventPredicate | None]


class _BranchExpansionLimit(RuntimeError):
    pass


def _predicate_signature(predicate: EventPredicate) -> tuple[Any, ...]:
    return (
        predicate.domain,
        predicate.polarity,
        _window_repr(predicate.window),
        tuple(
            sorted(
                (dimension, None if values is None else tuple(sorted(values)))
                for dimension, values in predicate.constraints.items()
            )
        ),
        predicate.source_kind,
        predicate.source_id,
        predicate.requires_event_presence,
    )


def _deduplicate_predicates(predicates: Iterable[EventPredicate]) -> tuple[EventPredicate, ...]:
    unique: dict[tuple[Any, ...], EventPredicate] = {}
    for predicate in predicates:
        unique.setdefault(_predicate_signature(predicate), predicate)
    return tuple(unique[key] for key in sorted(unique, key=repr))


def _branch_id(predicates: Sequence[EventPredicate]) -> str:
    encoded = repr(tuple(_predicate_signature(predicate) for predicate in predicates)).encode("utf-8")
    return "branch_" + hashlib.sha256(encoded).hexdigest()[:16]


def _expand_boolean_branches(
    expression: event_ir.Condition,
    predicate_factory: PredicateFactory,
    *,
    negated: bool,
    max_branches: int,
) -> list[tuple[EventPredicate, ...]]:
    """Accumulate conjunction-local constraints without unbounded DNF expansion."""

    if isinstance(expression, event_ir.Not):
        return _expand_boolean_branches(
            expression.operand,
            predicate_factory,
            negated=not negated,
            max_branches=max_branches,
        )

    if isinstance(expression, (event_ir.And, event_ir.Or)):
        effective_and = isinstance(expression, event_ir.And) != negated
        child_branches = [
            _expand_boolean_branches(
                child,
                predicate_factory,
                negated=negated,
                max_branches=max_branches,
            )
            for child in expression.operands
        ]
        if effective_and:
            branches: list[tuple[EventPredicate, ...]] = [()]
            for alternatives in child_branches:
                if len(branches) * len(alternatives) > max_branches:
                    raise _BranchExpansionLimit
                branches = [
                    _deduplicate_predicates((*left, *right))
                    for left in branches
                    for right in alternatives
                ]
            return branches
        count = sum(len(alternatives) for alternatives in child_branches)
        if count > max_branches:
            raise _BranchExpansionLimit
        return [branch for alternatives in child_branches for branch in alternatives]

    predicate = predicate_factory(expression, negated)
    if predicate is None:
        return [()]
    if not isinstance(predicate, EventPredicate):
        raise TypeError("predicate_factory must return EventPredicate or None")
    return [(predicate,)]


def validate_boolean_expression(
    expression: event_ir.Condition,
    predicate_factory: PredicateFactory,
    *,
    semantic_registry: event_semantic_registry.EventSemanticRegistry | None = None,
    max_branches: int = 64,
) -> BooleanValidationResult:
    """Validate presence/absence constraints inside Boolean branches.

    A conflict is considered only inside one conjunction branch.  An OR is
    contradictory only when every branch is contradictory.  Any surviving
    unknown branch keeps the whole result unknown.  Expansion is bounded; the
    tree is never simplified or flattened after the limit is reached.
    """

    if not isinstance(max_branches, int) or isinstance(max_branches, bool) or max_branches < 1:
        raise ValueError("max_branches must be a positive integer")
    if not isinstance(expression, event_ir.Condition.__args__):
        raise TypeError("expression must be an event_ir.Condition")

    try:
        raw_branches = _expand_boolean_branches(
            expression,
            predicate_factory,
            negated=False,
            max_branches=max_branches,
        )
    except _BranchExpansionLimit:
        issue = SemanticValidationIssue(code="semantic_complexity_limit")
        return BooleanValidationResult(SEMANTIC_UNKNOWN, (issue,), ())

    registry = semantic_registry or event_semantic_registry.registry()
    configured_domains = aggregate_parser_config.rules().semantic_domains
    branches: list[BranchValidationResult] = []
    all_issues: list[SemanticValidationIssue] = []
    for predicates in raw_branches:
        branch_id = _branch_id(predicates)
        validation = validate(
            predicates,
            configured_domains,
            semantic_registry=registry,
        )
        if validation.conflicts:
            status = CONTRADICTORY
            findings = validation.conflicts
        elif validation.unresolved:
            status = SEMANTIC_UNKNOWN
            findings = validation.unresolved
        else:
            status = CONSISTENT
            findings = []
        branch_issues = tuple(
            SemanticValidationIssue(
                code=finding.code,
                branch_id=branch_id,
                domain=finding.domain,
                positive_condition_id=finding.positive.source_id,
                negative_condition_id=finding.negative.source_id,
            )
            for finding in findings
        )
        all_issues.extend(branch_issues)
        branches.append(BranchValidationResult(branch_id, status, tuple(predicates), branch_issues))

    if branches and all(branch.status == CONTRADICTORY for branch in branches):
        status = CONTRADICTORY
    elif any(branch.status == SEMANTIC_UNKNOWN for branch in branches):
        status = SEMANTIC_UNKNOWN
    else:
        status = CONSISTENT
    unique_issues = tuple(dict.fromkeys(all_issues))
    return BooleanValidationResult(status, unique_issues, tuple(branches))


__all__ = [
    "ABSENCE",
    "BEGINNING_OF_TIME",
    "CONSISTENT",
    "CONTRADICTORY",
    "PRESENCE",
    "PROVEN_CONFLICT",
    "PROVEN_SAFE",
    "SEMANTIC_UNKNOWN",
    "UNKNOWN",
    "AggregateSemanticConflict",
    "BooleanValidationResult",
    "BranchValidationResult",
    "ConflictFinding",
    "EventPredicate",
    "NormalizedWindow",
    "SemanticValidationIssue",
    "ValidationResult",
    "classify_pair",
    "classify_pair_detail",
    "classify_scope_relation",
    "constraints_are_subset",
    "intersect",
    "lifetime_window",
    "rolling_window",
    "validate",
    "validate_boolean_expression",
    "window_is_subset",
]
