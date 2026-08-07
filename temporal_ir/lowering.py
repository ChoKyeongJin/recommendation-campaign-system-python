"""Temporal lowering 오케스트레이터 — 의미 IR → 실행 IR + **영수증**.

절차는 고정이다::

    근거 확인 → 연산자 결정 → binding 해석 → 계약 검증 → 구간 확정 →
    적재 판정 → lowering → 합성 검증 → 영수증 발급

각 단계는 다른 종류의 실패를 낸다. 그 구분이 이 모듈의 존재 이유다 — "안 된다"가 전부 같은
문장으로 나오면 사용자는 무엇을 고쳐야 하는지 알 수 없고, 운영자는 데이터가 없는 것인지
표현이 없는 것인지 구분할 수 없다.

    invalid                요청 자체가 성립하지 않는다(선언되지 않은 값, 모호한 관측 방식).
    unsupported            데이터 표현이 그 질문에 답할 수 없다(§14: 정책과 무관하게 SQL 금지).
    blocked_by_coverage    의미는 표현되지만 적재가 없다 + 정책이 block 이다.
    compiled               실행 IR 과 영수증을 낸다(적재 경고는 붙을 수 있다).

부분 합성 차단
--------------
영수증은 lowering 이 "했다고 말한 것"이 아니라 **낮춘 트리를 되읽어** 확인한 뒤 발급한다
(:func:`_composition_gaps`). 시간 조건만 있고 값 비교가 사라진 SQL, 값 비교만 있고 구간이
사라진 SQL 은 둘 다 성공처럼 보이지만 다른 집합이다. 여러 조건을 함께 낼 때도 같은 규칙이
적용된다 — :func:`compose_audience` 는 하나라도 실패하면 아무것도 내지 않는다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import event_ir
import resolved_semantic_catalog
from temporal_ir import calendar as tcal
from temporal_ir import catalog as tcat
from temporal_ir import registry as treg
from temporal_ir import semantic_ir as sir

# 데이터 표현이 답할 수 없다는 사유(정책과 무관하게 SQL 을 만들지 않는다). 나머지 검증 실패는
# 요청 자체의 문제(invalid)다 — 두 결말은 사용자에게 다른 안내가 나가야 한다.
UNSUPPORTED_CODES: frozenset[str] = frozenset({
    "temporal_representation_unsupported",
    "temporal_capability_missing",
    "temporal_operator_unsupported_by_binding",
    "temporal_occurrence_history_unavailable",
    "temporal_point_state_unavailable",
    "temporal_past_state_unsupported",
    "temporal_anchor_grain_too_fine",
    "temporal_bucket_semantics_mismatch",
    "temporal_strategy_not_lowerable",
    "temporal_strategy_unsupported",
    "temporal_strategy_unsupported_by_binding",
    "temporal_continuous_validity_unavailable",
    "temporal_transition_pair_unavailable",
    "temporal_row_identity_ambiguous",
    "temporal_window_not_bucket_aligned",
    "temporal_open_window_unbounded",
    "temporal_null_policy_unsupported",
    "temporal_missing_policy_unsupported",
    "temporal_empty_window_policy_unsupported",
    "temporal_operator_not_lowerable",
    "temporal_relation_window_unsupported",
    "temporal_relation_grain_unsupported",
    "temporal_relation_duration_unit_unsupported",
    "temporal_unbounded_bucket_count",
    "temporal_value_field_unavailable",
})


@dataclass(frozen=True, slots=True)
class TemporalLoweringReceipt:
    """무엇을, 어떤 관측 방식으로, 어느 구간에서, 어떤 값으로 낮췄는지의 구조화된 근거."""

    evidence_id: str
    evidence: dict[str, Any]
    metric: str
    binding: str
    operator: str
    selector: str
    quantifier: str
    predicate_type: str
    representation: str
    selection_strategy: str | None
    normalized_window: dict[str, str] | None
    anchor_instant: str | None
    timezone: str
    semantic_grain: str
    value_domain: str | None
    canonical_values: tuple[str, ...]
    comparison_operator: str | None
    subject_correlation: dict[str, str]
    missing_policy: str
    null_policy: str
    expected_buckets: int | None
    operator_version: int
    binding_version: int
    lowered_ir_hash: str
    coverage_status: str
    missing_buckets: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence": dict(self.evidence),
            "metric": self.metric,
            "binding": self.binding,
            "operator": self.operator,
            "selector": self.selector,
            "quantifier": self.quantifier,
            "predicate_type": self.predicate_type,
            "representation": self.representation,
            "selection_strategy": self.selection_strategy,
            "normalized_window": dict(self.normalized_window) if self.normalized_window else None,
            "anchor_instant": self.anchor_instant,
            "timezone": self.timezone,
            "semantic_grain": self.semantic_grain,
            "value_domain": self.value_domain,
            "canonical_values": list(self.canonical_values),
            "comparison_operator": self.comparison_operator,
            "subject_correlation": dict(self.subject_correlation),
            "missing_policy": self.missing_policy,
            "null_policy": self.null_policy,
            "expected_buckets": self.expected_buckets,
            "operator_version": self.operator_version,
            "binding_version": self.binding_version,
            "lowered_ir_hash": self.lowered_ir_hash,
            "coverage_status": self.coverage_status,
            "missing_buckets": list(self.missing_buckets),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TemporalLoweringReceipt":
        """저장된 영수증을 되읽는다. 감사에는 발급만큼 **복원**도 필요하다."""
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"알 수 없는 영수증 필드: {unknown}")
        payload = dict(raw)
        for name in ("canonical_values", "missing_buckets", "warnings"):
            payload[name] = tuple(payload.get(name) or ())
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LoweringSuccess:
    expression: event_ir.Condition
    receipt: TemporalLoweringReceipt
    coverage: tcat.CoverageAssessment
    status: Literal["compiled"] = "compiled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expression": self.expression.to_dict(),
            "receipt": self.receipt.to_dict(),
            "coverage": self.coverage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LoweringUnsupported:
    condition: sir.TemporalCondition
    code: str
    message: str
    issues: tuple[treg.ValidationIssue, ...] = ()
    status: Literal["unsupported"] = "unsupported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "issues": [issue.to_dict() for issue in self.issues],
            "preserved_condition": self.condition.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LoweringInvalid:
    condition: sir.TemporalCondition
    code: str
    message: str
    issues: tuple[treg.ValidationIssue, ...] = ()
    status: Literal["invalid"] = "invalid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "issues": [issue.to_dict() for issue in self.issues],
            "preserved_condition": self.condition.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LoweringBlocked:
    condition: sir.TemporalCondition
    coverage: tcat.CoverageAssessment
    message: str
    status: Literal["blocked_by_coverage"] = "blocked_by_coverage"

    @property
    def code(self) -> str:
        return str(self.coverage.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "coverage": self.coverage.to_dict(),
            "preserved_condition": self.condition.to_dict(),
        }


LoweringOutcome = LoweringSuccess | LoweringUnsupported | LoweringInvalid | LoweringBlocked


def lower_temporal_condition(
    condition: sir.TemporalCondition,
    *,
    temporal_catalog: tcat.TemporalCatalog,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    context: sir.TemporalRequestContext,
    registry: treg.TemporalOperatorRegistry,
) -> LoweringOutcome:
    """시간 의미 하나를 실행 IR 로 낮추거나, 어느 단계에서 왜 멈췄는지 답한다."""

    if condition.evidence is None or not condition.evidence.text.strip():
        return LoweringInvalid(
            condition,
            "temporal_evidence_missing",
            "시간 조건에는 원문 근거가 필요합니다 — 근거 없는 조건에는 영수증을 발급할 수 없습니다.",
        )

    operator = treg.resolve_operator_name(condition)
    try:
        definition = registry.get(operator)
    except treg.TemporalRegistryError as exc:  # pragma: no cover - 이름은 구조에서 나온다
        return LoweringInvalid(condition, "temporal_operator_unregistered", str(exc))

    try:
        metric = temporal_catalog.metric(condition.metric)
        candidates = _binding_candidates(condition, metric, temporal_catalog, operator)
    except tcat.BindingResolutionError as exc:
        if exc.code in {
            "temporal_operator_unsupported_by_metric",
            "temporal_operator_unsupported_by_binding",
        }:
            return LoweringUnsupported(condition, exc.code, str(exc))
        return LoweringInvalid(condition, exc.code, str(exc))
    except tcat.TemporalCatalogError as exc:
        return LoweringInvalid(condition, exc.code, str(exc))

    binding, issues = _select_binding(condition, metric, candidates, definition, semantic_catalog)
    if issues:
        return _issue_outcome(condition, issues)

    lower = definition.lower
    if lower is None:
        return LoweringUnsupported(
            condition,
            "temporal_operator_not_lowerable",
            f"{operator}: {definition.unsupported_reason}",
        )

    warnings: list[str] = []
    if binding.timezone != context.timezone:
        warnings.append("binding_timezone_differs_from_request")

    try:
        anchor_instant, interval = _resolve_interval(condition, binding, context)
    except tcal.CalendarError as exc:
        return LoweringInvalid(condition, "temporal_window_unresolved", str(exc))
    except _OpenWindowUnbounded as exc:
        return LoweringUnsupported(condition, "temporal_open_window_unbounded", str(exc))

    if interval is not None:
        aligned = _align_to_grain(interval, binding, warnings)
        if aligned is None:
            return LoweringUnsupported(
                condition,
                "temporal_window_not_bucket_aligned",
                f"요청 구간({interval.to_dict()})의 경계가 {binding.semantic_grain} 단위 적재의 "
                "칸 경계와 맞지 않습니다. 칸보다 잘게 나눈 구간을 칸으로 접으면 요청하지 않은 "
                "대상이 나옵니다.",
            )
        interval = aligned

    expected_buckets: int | None = None
    if isinstance(condition.quantifier, sir.EveryBucketQuantifier) and interval is not None:
        expected_buckets = tcal.expected_bucket_count(
            interval, binding.semantic_grain, binding.timezone
        )

    coverage = (
        binding.coverage.assess(interval, binding.semantic_grain, binding.timezone)
        if interval is not None
        else tcat.CoverageAssessment(tcat.CoverageStatus.EVALUABLE)
    )
    missing_policy = getattr(condition.selector, "missing_policy", sir.MissingPolicy.UNKNOWN)
    if coverage.status is not tcat.CoverageStatus.EVALUABLE:
        if context.coverage_policy is sir.CoveragePolicy.BLOCK:
            return LoweringBlocked(
                condition,
                coverage,
                f"적재 판정이 {coverage.status} 이고 coverage 정책이 block 입니다"
                f"(결측 칸: {list(coverage.missing_buckets)}).",
            )
        if missing_policy is sir.MissingPolicy.ERROR:
            # 요청이 "관측 행이 없으면 평가하지 말라"고 말했고, 적재 선언이 그 공백을 증명한다.
            # 그때도 SQL 을 내면 '없음'이 '조건 불성립'으로 조용히 바뀐다.
            return LoweringBlocked(
                condition,
                coverage,
                f"요청이 missing_policy=error 이고 적재 판정이 {coverage.status} 입니다"
                f"(결측 칸: {list(coverage.missing_buckets)}). 관측이 없는 구간을 조건 불성립으로 "
                "바꾸지 않습니다.",
            )

    right_binding: tcat.TemporalBindingSpec | None = None
    if isinstance(condition.predicate, sir.TemporalRelationPredicate):
        try:
            right_binding = temporal_catalog.binding(condition.predicate.right_binding)
        except tcat.TemporalCatalogError as exc:
            return LoweringInvalid(condition, exc.code, str(exc))
        relation_issues = _anchor_binding_issues(condition.predicate, right_binding)
        if relation_issues:
            return _issue_outcome(condition, relation_issues)

    data = treg.LoweringInput(
        condition=condition,
        metric=metric,
        binding=binding,
        semantic_catalog=semantic_catalog,
        context=context,
        operator=operator,
        interval=interval,
        anchor_instant=anchor_instant,
        expected_buckets=expected_buckets,
        right_binding=right_binding,
    )
    expression = lower(data)

    gaps = _composition_gaps(condition, binding, expression, interval)
    if gaps:
        # lowerer 가 만들었다고 말한 것과 트리에 실제로 있는 것이 다르다. 이것은 사용자 오류가
        # 아니라 구현 계약 위반이므로 SQL 을 내지 않고 이름을 대며 멈춘다.
        return LoweringInvalid(
            condition,
            "temporal_partial_composition",
            "낮춘 실행 IR 에 요청 의미의 일부가 없습니다: " + ", ".join(gaps),
        )
    try:
        event_ir.validate_evidence(expression)
    except event_ir.SemanticLossError as exc:  # pragma: no cover - 조립이 근거를 항상 붙인다
        return LoweringInvalid(condition, "temporal_evidence_missing", str(exc))

    receipt = _build_receipt(
        condition=condition,
        metric=metric,
        binding=binding,
        definition=definition,
        semantic_catalog=semantic_catalog,
        expression=expression,
        interval=interval,
        anchor_instant=anchor_instant,
        expected_buckets=expected_buckets,
        coverage=coverage,
        warnings=warnings,
    )
    return LoweringSuccess(expression=expression, receipt=receipt, coverage=coverage)


# ── 합성(부분 SQL 차단) ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AudienceComposition:
    """여러 조건을 하나의 실행 IR 로 합성한 결과. 하나라도 실패하면 아무것도 내지 않는다."""

    status: Literal["compiled", "blocked"]
    expression: event_ir.Condition | None
    receipts: tuple[TemporalLoweringReceipt, ...] = ()
    failures: tuple[LoweringOutcome, ...] = ()
    coverage_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expression": self.expression.to_dict() if self.expression else None,
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "failures": [failure.to_dict() for failure in self.failures],
            "coverage_warnings": list(self.coverage_warnings),
        }


def compose_audience(
    conditions: Sequence[sir.TemporalCondition],
    *,
    temporal_catalog: tcat.TemporalCatalog,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    context: sir.TemporalRequestContext,
    registry: treg.TemporalOperatorRegistry,
    additional: Sequence[event_ir.Condition] = (),
) -> AudienceComposition:
    """시간 조건들 + 이미 낮춰진 비시간 조건들을 **전부 또는 아무것도**로 합성한다.

    ``additional`` 은 다른 컴파일러가 낸 조건(성별·지역 등)이다. 시간 조건 하나가 실패했는데
    나머지로 SQL 을 내면 사용자가 요청하지 않은 더 넓은 집합이 나간다 — 그 부분 SQL 이 이
    함수가 막는 것이다.
    """
    outcomes = [
        lower_temporal_condition(
            condition,
            temporal_catalog=temporal_catalog,
            semantic_catalog=semantic_catalog,
            context=context,
            registry=registry,
        )
        for condition in conditions
    ]
    failures = tuple(item for item in outcomes if not isinstance(item, LoweringSuccess))
    if failures:
        return AudienceComposition(status="blocked", expression=None, failures=failures)

    successes = [item for item in outcomes if isinstance(item, LoweringSuccess)]
    operands = [*additional, *(item.expression for item in successes)]
    if not operands:
        return AudienceComposition(status="blocked", expression=None)
    expression = operands[0] if len(operands) == 1 else event_ir.And(operands=tuple(operands))
    try:
        # 합성 트리 **전체**에 근거를 요구한다. 다른 컴파일러가 준 조건이라고 해서 근거 없이
        # 실려 나가면, 그 절이 어디서 왔는지 아무도 답할 수 없는 SQL 이 성공으로 나간다.
        event_ir.validate_evidence(expression)
    except event_ir.SemanticLossError as exc:
        return AudienceComposition(
            status="blocked",
            expression=None,
            failures=tuple(
                LoweringInvalid(condition, "temporal_evidence_missing", str(exc))
                for condition in conditions[:1]
            ),
        )
    warnings = tuple(
        dict.fromkeys(
            warning for item in successes for warning in item.coverage.warnings
        )
    )
    return AudienceComposition(
        status="compiled",
        expression=expression,
        receipts=tuple(item.receipt for item in successes),
        coverage_warnings=warnings,
    )


# ── 내부 ─────────────────────────────────────────────────────────────────────────


class _OpenWindowUnbounded(ValueError):
    """열린 구간을 닫을 선언된 경계가 없다."""


def _binding_candidates(
    condition: sir.TemporalCondition,
    metric: tcat.MetricSpec,
    temporal_catalog: tcat.TemporalCatalog,
    operator: str,
) -> tuple[tcat.TemporalBindingSpec, ...]:
    """명시된 binding 하나, 또는 그 연산을 선언한 후보 전부(우선순위 순).

    명시된 binding 은 연산 선언 여부로 여기서 걸러내지 않는다 — 계약 검증을 그대로 통과시키면
    "선언하지 않았다"보다 **왜 답할 수 없는지**(관측 능력·표현)가 먼저 나온다.
    """
    if condition.binding is not None:
        binding = temporal_catalog.binding(condition.binding)
        if binding.metric_id != metric.id:
            raise tcat.BindingResolutionError(
                "temporal_binding_metric_mismatch",
                f"binding {binding.id!r} 은 metric {binding.metric_id!r} 의 것입니다"
                f"(요청: {metric.id!r})",
                symbol=binding.id,
            )
        return (binding,)
    candidates = temporal_catalog.candidate_bindings(metric.id, operator=operator)
    if not candidates:
        raise tcat.BindingResolutionError(
            "temporal_operator_unsupported_by_metric",
            f"metric {metric.id!r} 의 어떤 관측 방식도 {operator} 를 지원하지 않습니다"
            f"(선언된 관측: {list(metric.temporal_bindings)})",
            symbol=metric.id,
        )
    return candidates


def _select_binding(
    condition: sir.TemporalCondition,
    metric: tcat.MetricSpec,
    candidates: Sequence[tcat.TemporalBindingSpec],
    definition: treg.TemporalOperatorDefinition,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> tuple[tcat.TemporalBindingSpec, tuple[treg.ValidationIssue, ...]]:
    """이 **요청에** 답할 수 있는 관측 방식을 고른다.

    '연산자를 지원한다'와 '이 요청에 답할 수 있다'는 다르다 — 현재값만 아는 관측도
    ``temporal.as_of`` 를 지원하지만 지난달을 물으면 답할 수 없다. 그래서 후보를 계약 검증에
    통과시켜 걸러내고, 그러고도 둘 이상이면 선언된 우선순위(첫 후보)를 따른다. 임의 선택은
    하지 않는다 — 후보 순서는 카탈로그 선언이 정한다.
    """
    viable: list[tcat.TemporalBindingSpec] = []
    blocked: list[tuple[tcat.TemporalBindingSpec, tuple[treg.ValidationIssue, ...]]] = []
    for binding in candidates:
        issues = (
            *definition.check(condition, binding),
            *_value_issues(condition, metric, binding, semantic_catalog),
        )
        if issues:
            blocked.append((binding, issues))
        else:
            viable.append(binding)

    if len(viable) == 1:
        return viable[0], ()
    if len(viable) > 1:
        # 둘 다 답할 수 있다면 어느 쪽을 고를지는 **선언**이 정해야 한다. 선언이 없으면 고르지
        # 않는다 — id 알파벳 순서가 결과 집합을 정하게 두면 그 오답은 실행 결과로만 드러난다.
        priority = [name for name in metric.binding_priority if name in {item.id for item in viable}]
        if priority:
            return next(item for item in viable if item.id == priority[0]), ()
        return viable[0], (treg.ValidationIssue(
            "temporal_binding_ambiguous",
            f"metric {metric.id!r} 에 이 요청을 답할 수 있는 관측 방식이 둘 이상입니다"
            f"({sorted(item.id for item in viable)}). 우선순위 선언이나 명시적 binding 이 "
            "없으면 어느 관측으로 답했는지가 이름 순서에 좌우됩니다.",
            "binding",
        ),)
    # 아무도 답할 수 없다면 **가장 적게 막힌** 후보의 사유를 낸다(첫 후보가 아니라).
    binding, issues = min(blocked, key=lambda item: len(item[1]))
    return binding, issues


def _anchor_binding_issues(
    predicate: sir.TemporalRelationPredicate, right_binding: tcat.TemporalBindingSpec
) -> tuple[treg.ValidationIssue, ...]:
    """기준 사건 쪽 관측의 계약. 여러 번 발생할 수 있으면 **어느 발생인지** 밝혀야 한다."""
    issues: list[treg.ValidationIssue] = []
    if not right_binding.observation_capabilities.supports_all_occurrences:
        issues.append(treg.ValidationIssue(
            "temporal_occurrence_history_unavailable",
            f"기준 사건 관측 {right_binding.id!r} 은 발생을 전부 볼 수 없다고 선언했습니다.",
            "predicate.right_binding",
        ))
    single_occurrence = right_binding.representation in {
        tcat.Representation.SINGLETON,
        tcat.Representation.LATEST_ONLY,
        tcat.Representation.CURRENT_ONLY,
    }
    if predicate.anchor_occurrence is None and not single_occurrence:
        issues.append(treg.ValidationIssue(
            "temporal_anchor_occurrence_missing",
            f"기준 사건 {right_binding.id!r} 은 여러 번 발생할 수 있습니다. '첫 발생 후'인지 "
            "'마지막 발생 후'인지 밝히지 않으면 서로 다른 집합 중 하나를 임의로 고르게 됩니다.",
            "predicate.anchor_occurrence",
        ))
    if right_binding.semantic_grain is not sir.TimeUnit.DAY:
        issues.append(treg.ValidationIssue(
            "temporal_relation_grain_unsupported",
            f"기준 사건 관측 {right_binding.id!r} 은 {right_binding.semantic_grain} 단위로 "
            "적재되어 일 단위 시점 관계를 표현할 수 없습니다.",
            "predicate.right_binding",
        ))
    return tuple(issues)


def _issue_outcome(
    condition: sir.TemporalCondition, issues: Sequence[treg.ValidationIssue]
) -> LoweringOutcome:
    unsupported = [issue for issue in issues if issue.code in UNSUPPORTED_CODES]
    primary = unsupported[0] if unsupported else issues[0]
    payload = tuple(issues)
    if unsupported:
        return LoweringUnsupported(condition, primary.code, primary.message, payload)
    return LoweringInvalid(condition, primary.code, primary.message, payload)


def _value_issues(
    condition: sir.TemporalCondition,
    metric: tcat.MetricSpec,
    binding: tcat.TemporalBindingSpec,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> tuple[treg.ValidationIssue, ...]:
    """요청한 값과 비교가 **선언된 값 도메인** 안에 있는가.

    컴파일러도 결국 fail-close 하지만, 그때는 SQL 을 만들다 터지는 예외이지 답이 아니다.
    선언을 짚어 답할 수 있는 오류는 여기서 낸다.
    """
    predicate = condition.predicate
    issues: list[treg.ValidationIssue] = []
    values: list[str] = []
    operator: str | None = None
    if isinstance(predicate, sir.StatePredicate):
        values.append(predicate.comparison.value)
        operator = predicate.comparison.operator
    elif isinstance(predicate, sir.TransitionPredicate):
        values.extend((predicate.from_value, predicate.to_value))
        operator = "="
    elif isinstance(predicate, sir.DirectionalTransitionPredicate):
        # 방향 전이는 값을 이름으로 말하지 않지만 **서열 비교**다. 그 사실을 여기서 연산자로
        # 드러내야 두 선언(지표의 supported_comparisons, 도메인의 서열)이 그대로 게이트가 된다 —
        # 서열 없는 축('정상→휴면')에 '승급'이 붙으면 낮추기 전에 이름을 대며 막힌다.
        operator = (
            "<" if predicate.direction is sir.TransitionDirection.ASCENDING else ">"
        )
    if operator is not None and operator not in metric.supported_comparisons:
        issues.append(treg.ValidationIssue(
            "temporal_comparison_unsupported",
            f"metric {metric.id!r} 은 비교 연산자 {operator!r} 를 선언하지 않았습니다"
            f"(선언된 연산자: {sorted(metric.supported_comparisons)})",
            "predicate.comparison.operator",
        ))
    # 방향 전이는 이름 붙은 값이 없어도 값 필드와 서열을 읽어야 한다 — 값 목록이 비었다는
    # 이유로 검사를 건너뛰면 서열 없는 축이 검증을 통과해 컴파일러 예외로 터진다.
    needs_value_field = bool(values) or isinstance(
        predicate, sir.DirectionalTransitionPredicate
    )
    if needs_value_field and binding.value_field is None:
        # 값 필드가 없는 관측(사건 로그 등)에 값 조건이 오면 낮출 대상이 없다. 예전에는 그대로
        # 조립을 시도해 event_ir 스키마 예외가 합성 전체를 죽였다(리뷰 실측).
        issues.append(treg.ValidationIssue(
            "temporal_value_field_unavailable",
            f"binding {binding.id!r} 은 읽을 값 필드를 선언하지 않았습니다. 이 관측 방식에는 "
            "값 조건을 걸 수 없습니다(사건의 존재만 판정할 수 있습니다).",
            "predicate",
        ))
    if not needs_value_field or binding.value_field is None:
        return tuple(issues)
    field_spec = semantic_catalog.field(binding.value_field)
    value_map = dict(field_spec.compiler_field.value_map)
    if not value_map:
        return tuple(issues)
    for value in values:
        if value not in value_map:
            issues.append(treg.ValidationIssue(
                "temporal_value_unknown",
                f"{value!r} 은 값 도메인 {field_spec.value_domain!r} 에 선언된 값이 아닙니다"
                f"(선언된 값: {sorted(value_map)})",
                "predicate",
            ))
    if operator in {">", ">=", "<", "<="} and not field_spec.compiler_field.value_order:
        issues.append(treg.ValidationIssue(
            "temporal_value_order_undeclared",
            f"값 도메인 {field_spec.value_domain!r} 에는 서열이 선언되지 않아 부등호 비교를 "
            "표현할 수 없습니다(저장 문자열을 부등호로 비교하면 조용히 틀립니다).",
            "predicate.comparison.operator",
        ))
    return tuple(issues)


def _resolve_interval(
    condition: sir.TemporalCondition,
    binding: tcat.TemporalBindingSpec,
    context: sir.TemporalRequestContext,
) -> tuple[datetime | None, tcal.TimeInterval | None]:
    """selector → (기준 순간, 확정된 구간). 시간 필드가 없는 관측 방식은 구간도 없다."""
    selector = condition.selector
    if binding.time_field is None:
        anchor = sir.anchor_of(condition)
        return (tcal.resolve_anchor(anchor, context) if anchor else None), None

    if isinstance(selector, sir.AsOfSelector):
        instant = tcal.resolve_anchor(selector.anchor, context)
        return instant, tcal.bucket_containing(instant, binding.semantic_grain, binding.timezone)
    if isinstance(selector, sir.PreviousSelector):
        instant = tcal.resolve_anchor(selector.anchor, context)
        current = tcal.bucket_containing(instant, binding.semantic_grain, binding.timezone)
        return instant, tcal.shift_bucket(current, binding.semantic_grain, -1, binding.timezone)

    instant = tcal.resolve_anchor(selector.anchor, context)
    if isinstance(selector.window, sir.LifetimeWindow):
        # '평생'은 구간의 부재가 아니라 구간 제한이 없다는 뜻이다 — 시간 필터를 만들지 않는다.
        return instant, None
    open_bound: Any = None
    if isinstance(selector.window, sir.OpenEndedWindow):
        open_bound = _open_bound(selector.window, binding)
    return instant, tcal.resolve_window(
        selector.window, context, anchor=selector.anchor, open_bound=open_bound
    )


def _open_bound(window: sir.OpenEndedWindow, binding: tcat.TemporalBindingSpec) -> date:
    """열린 쪽을 닫는 **선언된 적재 경계**. 선언이 없으면 닫지 않는다."""
    coverage = binding.coverage
    if window.direction is sir.Direction.BEFORE:
        if coverage.available_from is None:
            raise _OpenWindowUnbounded(
                f"binding {binding.id!r} 에 적재 시작이 선언되지 않아 '이전' 구간을 닫을 수 "
                "없습니다. 열린 구간을 임의의 과거로 채우지 않습니다."
            )
        return coverage.available_from
    if coverage.available_through is None:
        raise _OpenWindowUnbounded(
            f"binding {binding.id!r} 에 적재 끝이 선언되지 않아 '이후' 구간을 닫을 수 없습니다."
        )
    return coverage.available_through + timedelta(days=1)


def _align_to_grain(
    interval: tcal.TimeInterval, binding: tcat.TemporalBindingSpec, warnings: list[str]
) -> tcal.TimeInterval | None:
    """구간을 적재 칸에 맞춘다. 맞출 수 없으면 ``None``(근사하지 않는다).

    일 단위 적재에서만 확장을 허용하는 이유는 저장 정밀도다 — 날짜 컬럼에는 시:분이 없으므로
    하루의 일부를 요청해도 표현할 수 있는 최소 단위가 하루다. 그 확장은 영수증에 남긴다.
    월 이상의 칸에서 같은 확장을 하면 한 달이 통째로 늘어나므로 허용하지 않는다.
    """
    grain = binding.semantic_grain
    if tcal.is_bucket_aligned(interval, grain, binding.timezone):
        return interval
    if grain is not sir.TimeUnit.DAY:
        return None
    start = tcal.bucket_containing(interval.start, grain, binding.timezone).start
    end = tcal.bucket_containing(
        interval.end_exclusive - timedelta(microseconds=1), grain, binding.timezone
    ).end_exclusive
    warnings.append("window_expanded_to_day_grain")
    return tcal.TimeInterval(start, end)


def _composition_gaps(
    condition: sir.TemporalCondition,
    binding: tcat.TemporalBindingSpec,
    expression: event_ir.Condition,
    interval: tcal.TimeInterval | None,
) -> tuple[str, ...]:
    """낮춘 트리를 되읽어 **요청 의미가 실제로 들어 있는지** 확인한다."""
    nodes = list(event_ir.walk(expression))
    gaps: list[str] = []

    predicate = condition.predicate
    if isinstance(predicate, sir.TemporalRelationPredicate):
        # 시간 관계는 두 사건의 **발생 시점**을 잇는 노드 하나로 낮아진다. 소스는 그 노드의
        # EventReference 가 들고 있으므로 Source/TimeFilter 를 찾는 것이 아니라 그 짝을 확인한다.
        if not any(
            isinstance(node, event_ir.TemporalRelation)
            and node.operator == str(predicate.relation)
            # 실행 IR 은 left=기준 사건이므로 의미 계층과 반대다(lower_temporal_relation 참고).
            and node.right.source == binding.source
            and node.duration.value == predicate.duration.amount
            and node.duration.unit == str(predicate.duration.unit)
            for node in nodes
        ):
            gaps.append(f"시간 관계({predicate.relation} {predicate.duration.amount}{predicate.duration.unit})")
        return tuple(gaps)

    if binding.representation is tcat.Representation.CURRENT_ONLY:
        if not any(
            isinstance(node, event_ir.FieldRef) and node.source == binding.source
            for node in nodes
        ):
            gaps.append(f"주체 필드({binding.source})")
    elif not any(
        isinstance(node, event_ir.Source) and node.name == binding.source for node in nodes
    ):
        gaps.append(f"관측 소스({binding.source})")

    if interval is not None and binding.time_field is not None:
        start, end = interval.dates()
        if not any(
            isinstance(node, event_ir.TimeFilter)
            and node.field.name == binding.time_field
            and isinstance(node.window, event_ir.AbsoluteInterval)
            and node.window.start == start
            and node.window.end_exclusive == end
            for node in nodes
        ):
            gaps.append(f"시간 조건({binding.time_field} [{start}, {end}))")

    if isinstance(predicate, sir.DirectionalTransitionPredicate) and binding.value_field:
        # 방향 전이에는 확정된 값 쌍이 없으므로 값 하나하나가 아니라 **모양**을 확인한다:
        # 현재값 등호 비교와 직전값 부등호 비교가 둘 다 있어야 한다. 부등호가 빠지면 그냥
        # '그 값이 된 회원'이 되고, 부등호가 뒤집히면 승급이 강등이 된다.
        ordinal = "<" if predicate.direction is sir.TransitionDirection.ASCENDING else ">"
        for field_id, operator in (
            (binding.value_field, "="),
            (str(binding.prev_value_field), ordinal),
        ):
            if not any(
                isinstance(node, event_ir.Comparison)
                and isinstance(node.left, event_ir.FieldRef)
                and node.left.name == field_id
                and node.operator == operator
                for node in nodes
            ):
                gaps.append(f"방향 전이 비교({field_id} {operator} …)")

    for field_id, value in _expected_value_bindings(condition, binding):
        if not any(
            isinstance(node, event_ir.Comparison)
            and isinstance(node.left, event_ir.FieldRef)
            and node.left.name == field_id
            and isinstance(node.right, event_ir.Literal)
            and node.right.value == value
            for node in nodes
        ):
            gaps.append(f"값 비교({field_id} = {value})")

    return tuple(gaps)


def _expected_value_bindings(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[tuple[str, str], ...]:
    predicate = condition.predicate
    if isinstance(predicate, sir.StatePredicate) and binding.value_field:
        return ((binding.value_field, predicate.comparison.value),)
    if isinstance(predicate, sir.TransitionPredicate) and binding.value_field:
        return (
            (binding.value_field, predicate.to_value),
            (str(binding.prev_value_field), predicate.from_value),
        )
    return ()


def _build_receipt(
    *,
    condition: sir.TemporalCondition,
    metric: tcat.MetricSpec,
    binding: tcat.TemporalBindingSpec,
    definition: treg.TemporalOperatorDefinition,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    expression: event_ir.Condition,
    interval: tcal.TimeInterval | None,
    anchor_instant: datetime | None,
    expected_buckets: int | None,
    coverage: tcat.CoverageAssessment,
    warnings: Sequence[str],
) -> TemporalLoweringReceipt:
    evidence = condition.evidence
    assert evidence is not None  # 진입에서 이미 확인했다.
    selector = condition.selector
    strategy = (
        selector.strategy.kind if isinstance(selector, sir.AsOfSelector) else None
    )
    values = tuple(value for _field, value in _expected_value_bindings(condition, binding))
    comparison_operator = None
    if isinstance(condition.predicate, sir.StatePredicate):
        comparison_operator = condition.predicate.comparison.operator
    elif isinstance(condition.predicate, sir.TransitionPredicate):
        comparison_operator = "="
    elif isinstance(condition.predicate, sir.DirectionalTransitionPredicate):
        # 영수증에 남는 것은 '어느 쪽으로'를 결정한 서열 비교다. '='로 적으면 승급과 강등이
        # 영수증에서 구별되지 않는다.
        comparison_operator = (
            "<"
            if condition.predicate.direction is sir.TransitionDirection.ASCENDING
            else ">"
        )
    source = semantic_catalog.source(binding.source)
    return TemporalLoweringReceipt(
        evidence_id=_evidence_id(metric.id, definition.name, evidence),
        evidence=evidence.to_dict(),
        metric=metric.id,
        binding=binding.id,
        operator=definition.name,
        selector=selector.kind,
        quantifier=condition.quantifier.kind,
        predicate_type=condition.predicate.kind,
        representation=str(binding.representation),
        selection_strategy=strategy,
        normalized_window=interval.to_dict() if interval is not None else None,
        anchor_instant=anchor_instant.isoformat() if anchor_instant is not None else None,
        timezone=binding.timezone,
        semantic_grain=str(binding.semantic_grain),
        value_domain=metric.value_domain,
        canonical_values=values,
        comparison_operator=comparison_operator,
        subject_correlation={
            "subject_key": source.event.subject_key,
            "source_key": source.event.event_subject_key,
        },
        missing_policy=str(getattr(selector, "missing_policy", sir.MissingPolicy.UNKNOWN)),
        null_policy=str(
            getattr(condition.predicate, "null_policy", binding.null_policy)
        ),
        expected_buckets=expected_buckets,
        operator_version=definition.version,
        binding_version=binding.version,
        lowered_ir_hash=lowered_ir_hash(expression),
        coverage_status=str(coverage.status),
        missing_buckets=coverage.missing_buckets,
        warnings=tuple(dict.fromkeys((*warnings, *coverage.warnings))),
    )


def lowered_ir_hash(expression: event_ir.Condition) -> str:
    """낮춘 트리의 안정적인 지문(영수증과 SQL 이 같은 트리에서 나왔음을 대조하는 값)."""
    payload = json.dumps(expression.to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_id(metric_id: str, operator: str, evidence: sir.Evidence) -> str:
    seed = f"{metric_id}\0{operator}\0{evidence.start}\0{evidence.end}\0{evidence.text}"
    return "tmp_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "AudienceComposition",
    "LoweringBlocked",
    "LoweringInvalid",
    "LoweringOutcome",
    "LoweringSuccess",
    "LoweringUnsupported",
    "TemporalLoweringReceipt",
    "UNSUPPORTED_CODES",
    "compose_audience",
    "lower_temporal_condition",
    "lowered_ir_hash",
]
