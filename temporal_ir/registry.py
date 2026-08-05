"""Temporal Operator Registry — 연산자별 **계약과 lowerer** 의 단일 소유자.

연산자 이름은 구조에서 파생한다
--------------------------------
:func:`resolve_operator_name` 은 (selector, quantifier, predicate) 조합을 보고 이름을 정한다.
생산자가 이름을 지어 넣지 않는다는 뜻이다 — 이름을 입력으로 받으면 ``every_bucket_as_of`` 같은
합성 이름이 늘어나고, 그 순간 §2 가 금지한 '연산자 문자열 하나에 모든 의미'로 되돌아간다.

선언 어휘와 구현의 분리
-----------------------
등록된 연산자 중 일부는 ``lower=None`` 이다. 그것은 오타가 아니라 **선언**이다:
그 의미는 정의되어 있고 이름도 있지만, 이 저장소의 실행 IR 로는 정확한 SQL 을 만들 수 없다
(예: 관측 순서에서 직전 행을 고르는 연산은 파티션·정렬·행 선택 primitive 를 요구한다).
그때는 근사하지 않고 사유와 함께 미지원으로 답한다. 오타(등록되지 않은 이름)와 미구현
(등록되었고 lowerer 가 없음)은 다른 결말이어야 한다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import event_ir
import resolved_semantic_catalog
from temporal_ir import calendar as tcal
from temporal_ir import catalog as tcat
from temporal_ir import semantic_ir as sir

# ── 연산자 이름(닫힌 집합, namespace 규약) ───────────────────────────────────────
IN_WINDOW = "temporal.in_window"
BEFORE = "temporal.before"
AFTER = "temporal.after"
AS_OF = "temporal.as_of"
PREVIOUS_BUCKET = "temporal.previous_bucket"
PREVIOUS_OBSERVATION = "temporal.previous_observation"
PREVIOUS_DISTINCT_VALUE = "temporal.previous_distinct_value"
LATEST_IN_WINDOW = "temporal.latest_in_window"
NONE_IN_WINDOW = "temporal.none"
ALL_OBSERVATIONS = "temporal.all"
EVERY_BUCKET = "temporal.every_bucket"
THROUGHOUT = "temporal.throughout"
CHANGED_BETWEEN_ENDPOINTS = "temporal.changed_between_endpoints"
CHANGED_WITHIN_WINDOW = "temporal.changed_within_window"
DIRECT_TRANSITION = "temporal.direct_transition"
CHANGE_COUNT = "temporal.change_count"
UNCHANGED_OBSERVATIONS = "temporal.unchanged_observations"
WITHIN_AFTER = "temporal.within_after"
WITHIN_BEFORE = "temporal.within_before"

# 입력 호환 별칭. 기존 대문자 qualifier(:mod:`temporal_semantics`)는 **입력에서만** 인정하고,
# 새 직렬화는 namespace 이름으로만 나간다(§9). 두 표기가 출력에 섞이면 영수증을 읽는 쪽이
# 어느 것이 정본인지 알 수 없다.
LEGACY_OPERATOR_ALIASES: dict[str, str] = {
    "AS_OF": AS_OF,
    "IMMEDIATELY_PRECEDING": PREVIOUS_OBSERVATION,
    "WITHIN_INTERVAL": IN_WINDOW,
    "AT_LEAST_ONCE_IN_INTERVAL": IN_WINDOW,
    "NEVER_IN_INTERVAL": NONE_IN_WINDOW,
    "THROUGHOUT_INTERVAL": THROUGHOUT,
    "EVERY_SUBINTERVAL": EVERY_BUCKET,
    "UNCHANGED_THROUGHOUT": UNCHANGED_OBSERVATIONS,
    "CHANGE_BETWEEN": DIRECT_TRANSITION,
    "CHANGE_COUNT": CHANGE_COUNT,
    # 같은 뜻의 축약 표기.
    "temporal.any": IN_WINDOW,
}


def canonical_operator_name(value: Any) -> str | None:
    """별칭/대문자 표기를 정본 연산자 이름으로. 모르는 이름은 ``None``(추측하지 않는다)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text in _ALL_NAMES:
        return text
    return LEGACY_OPERATOR_ALIASES.get(text) or LEGACY_OPERATOR_ALIASES.get(text.upper())


_ALL_NAMES: frozenset[str] = frozenset({
    IN_WINDOW, BEFORE, AFTER, AS_OF, PREVIOUS_BUCKET, PREVIOUS_OBSERVATION,
    PREVIOUS_DISTINCT_VALUE, LATEST_IN_WINDOW, NONE_IN_WINDOW, ALL_OBSERVATIONS, EVERY_BUCKET,
    THROUGHOUT, CHANGED_BETWEEN_ENDPOINTS, CHANGED_WITHIN_WINDOW, DIRECT_TRANSITION, CHANGE_COUNT,
    UNCHANGED_OBSERVATIONS, WITHIN_AFTER, WITHIN_BEFORE,
})

OPERATOR_NAMES: frozenset[str] = _ALL_NAMES


class TemporalRegistryError(ValueError):
    """레지스트리 계약 위반(중복 등록, 등록되지 않은 연산자)."""


class UnsupportedTemporalOperatorError(TemporalRegistryError):
    """이름은 있는데 이 저장소가 낮출 수 없는 연산이다(오타와 구분되는 결말)."""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "path": self.path}


# ── 구조 → 연산자 이름 ───────────────────────────────────────────────────────────

_WINDOW_QUANTIFIER_NAMES: dict[type, str] = {
    sir.ExistsQuantifier: IN_WINDOW,
    sir.NoneQuantifier: NONE_IN_WINDOW,
    sir.AllObservationsQuantifier: ALL_OBSERVATIONS,
    sir.EveryBucketQuantifier: EVERY_BUCKET,
    sir.ThroughoutQuantifier: THROUGHOUT,
    sir.LatestObservationQuantifier: LATEST_IN_WINDOW,
}

_PREVIOUS_NAMES: dict[sir.PreviousKind, str] = {
    sir.PreviousKind.BUCKET: PREVIOUS_BUCKET,
    sir.PreviousKind.OBSERVATION: PREVIOUS_OBSERVATION,
    sir.PreviousKind.DISTINCT_VALUE: PREVIOUS_DISTINCT_VALUE,
}

_TRANSITION_NAMES: dict[sir.TransitionMode, str] = {
    sir.TransitionMode.DIRECT_OBSERVED_TRANSITION: DIRECT_TRANSITION,
    sir.TransitionMode.ENDPOINT_CHANGE: CHANGED_BETWEEN_ENDPOINTS,
    sir.TransitionMode.ANY_CHANGE: CHANGED_WITHIN_WINDOW,
}

_RELATION_NAMES: dict[sir.RelationKind, str] = {
    sir.RelationKind.WITHIN_AFTER: WITHIN_AFTER,
    sir.RelationKind.WITHIN_BEFORE: WITHIN_BEFORE,
}


def resolve_operator_name(condition: sir.TemporalCondition) -> str:
    """조합 → 정본 연산자 이름. 이 함수가 **유일한** 이름 생산자다."""
    predicate = condition.predicate
    if isinstance(predicate, sir.TemporalRelationPredicate):
        return _RELATION_NAMES[predicate.relation]
    if isinstance(predicate, sir.TransitionPredicate):
        return _TRANSITION_NAMES[predicate.transition_mode]
    if isinstance(predicate, sir.ChangeCountPredicate):
        return CHANGE_COUNT
    if isinstance(predicate, sir.UnchangedPredicate):
        return UNCHANGED_OBSERVATIONS

    selector = condition.selector
    if isinstance(selector, sir.AsOfSelector):
        return AS_OF
    if isinstance(selector, sir.PreviousSelector):
        return _PREVIOUS_NAMES[selector.previous_kind]
    if isinstance(selector.window, sir.OpenEndedWindow):
        return BEFORE if selector.window.direction is sir.Direction.BEFORE else AFTER
    name = _WINDOW_QUANTIFIER_NAMES.get(type(condition.quantifier))
    if name is None:  # pragma: no cover - quantifier 어휘가 닫혀 있어 도달 불가
        raise TemporalRegistryError(f"연산자 이름을 정할 수 없는 조합입니다: {condition.to_dict()}")
    return name


# ── lowering 입력 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoweringInput:
    """lowerer 가 필요로 하는 **이미 확정된** 사실만 담는다.

    lowerer 안에서 시계를 읽거나 달력을 다시 계산하지 않는다 — 구간은 여기 들어오기 전에
    요청 문맥으로 확정되고, 영수증에 적히는 값과 SQL 에 들어가는 값이 같은 하나가 된다.
    """

    condition: sir.TemporalCondition
    metric: tcat.MetricSpec
    binding: tcat.TemporalBindingSpec
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog
    context: sir.TemporalRequestContext
    operator: str
    interval: tcal.TimeInterval | None = None
    anchor_instant: datetime | None = None
    expected_buckets: int | None = None
    # 시간 관계 연산자의 오른쪽 피연산자. 두 관측을 잇는 연산에서만 채워진다.
    right_binding: tcat.TemporalBindingSpec | None = None

    @property
    def evidence(self) -> sir.Evidence | None:
        return self.condition.evidence


Lowerer = Callable[[LoweringInput], event_ir.Condition]
Validator = Callable[[sir.TemporalCondition, tcat.TemporalBindingSpec], Sequence[ValidationIssue]]


def _no_extra_validation(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> Sequence[ValidationIssue]:
    return ()


@dataclass(frozen=True, slots=True)
class TemporalOperatorDefinition:
    """연산자 하나의 계약. 무엇을 받을 수 있는지가 **선언으로** 여기 다 있다."""

    name: str
    selector_types: frozenset[type]
    quantifier_types: frozenset[type]
    predicate_types: frozenset[type]
    accepted_representations: frozenset[tcat.Representation]
    required_capabilities: frozenset[str] = frozenset()
    accepted_strategies: frozenset[str] = frozenset(tcat.SELECTION_STRATEGIES)
    accepted_null_policies: frozenset[sir.NullPolicy] = frozenset(sir.NullPolicy)
    accepted_missing_policies: frozenset[sir.MissingPolicy] = frozenset(sir.MissingPolicy)
    accepted_empty_window_policies: frozenset[sir.EmptyWindowPolicy] = frozenset(
        sir.EmptyWindowPolicy
    )
    validate: Validator = _no_extra_validation
    lower: Lowerer | None = None
    unsupported_reason: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if self.name not in _ALL_NAMES:
            raise TemporalRegistryError(f"등록되지 않은 연산자 이름입니다: {self.name!r}")
        unknown = sorted(set(self.required_capabilities) - set(tcat.FLAG_NAMES))
        if unknown:
            raise TemporalRegistryError(
                f"{self.name}: 알 수 없는 관측 능력을 요구합니다: {unknown}"
            )
        if self.lower is None and not self.unsupported_reason:
            raise TemporalRegistryError(
                f"{self.name}: lowerer 가 없으면 사유(unsupported_reason)를 선언해야 합니다"
            )

    @property
    def lowerable(self) -> bool:
        return self.lower is not None

    def check(
        self, condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
    ) -> tuple[ValidationIssue, ...]:
        """계약 검증 — 조합·표현·능력·정책. 여기서 막힌 것은 SQL 을 만들다 터지지 않는다."""
        issues: list[ValidationIssue] = []
        if type(condition.selector) not in self.selector_types:
            issues.append(ValidationIssue(
                "temporal_selector_unsupported",
                f"{self.name} 은 {condition.selector.kind} selector 를 받지 않습니다",
                "selector",
            ))
        if type(condition.quantifier) not in self.quantifier_types:
            issues.append(ValidationIssue(
                "temporal_quantifier_unsupported",
                f"{self.name} 은 {condition.quantifier.kind} quantifier 를 받지 않습니다",
                "quantifier",
            ))
        if type(condition.predicate) not in self.predicate_types:
            issues.append(ValidationIssue(
                "temporal_predicate_unsupported",
                f"{self.name} 은 {condition.predicate.kind} predicate 를 받지 않습니다",
                "predicate",
            ))
        if binding.representation not in self.accepted_representations:
            issues.append(ValidationIssue(
                "temporal_representation_unsupported",
                f"{self.name} 은 {binding.representation} 표현으로 답할 수 없습니다"
                f"(가능한 표현: {sorted(str(item) for item in self.accepted_representations)})",
                "binding.representation",
            ))
        missing_capabilities = sorted(
            name for name in self.required_capabilities
            if not binding.observation_capabilities.has(name)
        )
        if missing_capabilities:
            issues.append(ValidationIssue(
                "temporal_capability_missing",
                f"binding {binding.id!r} 이 {self.name} 에 필요한 관측 능력을 선언하지 않았습니다: "
                f"{missing_capabilities}",
                "binding.observation_capabilities",
            ))
        if self.name not in binding.supported_operators:
            issues.append(ValidationIssue(
                "temporal_operator_unsupported_by_binding",
                f"binding {binding.id!r} 은 {self.name} 를 지원한다고 선언하지 않았습니다",
                "binding.supported_operators",
            ))
        issues.extend(self._policy_issues(condition, binding))
        issues.extend(self.validate(condition, binding))
        return tuple(issues)

    def _policy_issues(
        self, condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        selector = condition.selector
        if isinstance(selector, sir.AsOfSelector):
            strategy = selector.strategy.kind
            if strategy not in self.accepted_strategies:
                issues.append(ValidationIssue(
                    "temporal_strategy_unsupported",
                    f"{self.name} 은 선택 전략 {strategy!r} 을 받지 않습니다",
                    "selector.strategy",
                ))
            elif strategy not in binding.selection_strategies:
                issues.append(ValidationIssue(
                    "temporal_strategy_unsupported_by_binding",
                    f"binding {binding.id!r} 은 선택 전략 {strategy!r} 을 선언하지 않았습니다"
                    f"(선언된 전략: {sorted(binding.selection_strategies)})",
                    "binding.selection_strategies",
                ))
        missing_policy = getattr(selector, "missing_policy", None)
        if missing_policy is not None and missing_policy not in self.accepted_missing_policies:
            issues.append(ValidationIssue(
                "temporal_missing_policy_unsupported",
                f"{self.name} 은 missing 정책 {missing_policy} 를 표현할 수 없습니다",
                "selector.missing_policy",
            ))
        if isinstance(selector, sir.WindowSelector) and isinstance(
            condition.quantifier,
            (sir.AllObservationsQuantifier, sir.EveryBucketQuantifier, sir.ThroughoutQuantifier),
        ):
            if selector.empty_window_policy not in self.accepted_empty_window_policies:
                issues.append(ValidationIssue(
                    "temporal_empty_window_policy_unsupported",
                    f"{self.name} 은 빈 구간 정책 {selector.empty_window_policy} 를 표현할 수 "
                    "없습니다 — 관측이 없는 구간을 조용히 참으로 만들지 않습니다",
                    "selector.empty_window_policy",
                ))
        null_policy = getattr(condition.predicate, "null_policy", None)
        if null_policy is not None and null_policy not in self.accepted_null_policies:
            issues.append(ValidationIssue(
                "temporal_null_policy_unsupported",
                f"{self.name} 은 null 정책 {null_policy} 를 표현할 수 없습니다"
                f"(가능한 정책: {sorted(str(item) for item in self.accepted_null_policies)})",
                "predicate.null_policy",
            ))
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "lowerable": self.lowerable,
            "unsupported_reason": self.unsupported_reason,
            "selector_types": sorted(item.__name__ for item in self.selector_types),
            "quantifier_types": sorted(item.__name__ for item in self.quantifier_types),
            "predicate_types": sorted(item.__name__ for item in self.predicate_types),
            "accepted_representations": sorted(str(item) for item in self.accepted_representations),
            "required_capabilities": sorted(self.required_capabilities),
            "accepted_strategies": sorted(self.accepted_strategies),
        }


class TemporalOperatorRegistry:
    """등록된 연산자 정의의 소유자. 전역 가변 상태를 두지 않는다 — 구성 시점에 만든다."""

    def __init__(self, definitions: Sequence[TemporalOperatorDefinition] = ()) -> None:
        self._definitions: dict[str, TemporalOperatorDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: TemporalOperatorDefinition) -> None:
        if definition.name in self._definitions:
            raise TemporalRegistryError(f"이미 등록된 연산자입니다: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> TemporalOperatorDefinition:
        canonical = canonical_operator_name(name)
        definition = self._definitions.get(canonical or name)
        if definition is None:
            raise TemporalRegistryError(f"등록되지 않은 시간 연산자입니다: {name!r}")
        return definition

    def contains(self, name: str) -> bool:
        canonical = canonical_operator_name(name)
        return (canonical or name) in self._definitions

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def lowerable_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, item in self._definitions.items() if item.lowerable))

    def documentation(self) -> dict[str, dict[str, Any]]:
        """문서·테스트가 읽는 파생 표(손 목록을 두지 않는다)."""
        return {name: item.to_dict() for name, item in sorted(self._definitions.items())}


__all__ = [
    "AFTER",
    "ALL_OBSERVATIONS",
    "AS_OF",
    "BEFORE",
    "CHANGED_BETWEEN_ENDPOINTS",
    "CHANGED_WITHIN_WINDOW",
    "CHANGE_COUNT",
    "DIRECT_TRANSITION",
    "EVERY_BUCKET",
    "IN_WINDOW",
    "LATEST_IN_WINDOW",
    "LEGACY_OPERATOR_ALIASES",
    "LoweringInput",
    "NONE_IN_WINDOW",
    "OPERATOR_NAMES",
    "PREVIOUS_BUCKET",
    "PREVIOUS_DISTINCT_VALUE",
    "PREVIOUS_OBSERVATION",
    "THROUGHOUT",
    "TemporalOperatorDefinition",
    "TemporalOperatorRegistry",
    "TemporalRegistryError",
    "UNCHANGED_OBSERVATIONS",
    "UnsupportedTemporalOperatorError",
    "ValidationIssue",
    "WITHIN_AFTER",
    "WITHIN_BEFORE",
    "canonical_operator_name",
    "resolve_operator_name",
]
