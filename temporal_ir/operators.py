"""연산자 정의와 lowerer — 시간 의미 → **기존 실행 IR 조합**.

여기서 만드는 것은 전부 :mod:`event_ir` 의 기존 노드 조합이다. ``GradeTransitionNode`` 도
``LastMonthVipNode`` 도 만들지 않는다 — 그런 노드는 문장 하나에 노드 하나를 붙이는 설계이고,
실행 IR 이 이미 금지한 것이다.

낮추는 다섯 가지 모양
---------------------
::

    시점 상태      Exists(Filter(Source, And(TimeFilter(칸), 값비교)))
    구간 존재      Exists(Filter(Source, And(TimeFilter(구간), 술어…)))
    구간 부재      Not(Exists(...))
    관측 전칭      And(Exists(구간), Not(Exists(구간 ∧ ¬술어)))
    칸 전칭        Comparison(Aggregate(count distinct 시각), '=', 기대 칸 수)

컬럼 이름도 값 코드도 없다
--------------------------
필드는 binding 이 선언한 **심볼**(``member_month_snapshot.grade``)이고, 물리 컬럼과 값 코드로의
변환은 :mod:`event_compiler` 의 필드 선언이 한다. 그래서 이 파일에는 ``ZTS_GRADE`` 도 ``VIP`` 도
없고, 새 월별 속성은 카탈로그 한 항목으로 늘어난다.
"""

from __future__ import annotations

from collections.abc import Sequence

import event_ir
from temporal_ir import catalog as tcat
from temporal_ir import registry as treg
from temporal_ir import semantic_ir as sir

Issue = treg.ValidationIssue
Representation = tcat.Representation


# ── 공통 조립 ─────────────────────────────────────────────────────────────────────


def _evidence(data: treg.LoweringInput) -> sir.Evidence:
    evidence = data.condition.evidence
    if evidence is None:  # pragma: no cover - orchestrator 가 먼저 막는다
        raise treg.TemporalRegistryError("근거 없는 조건은 낮추지 않습니다")
    return evidence


def _time_filter(data: treg.LoweringInput) -> event_ir.TimeFilter:
    if data.interval is None or data.binding.time_field is None:  # pragma: no cover
        raise treg.TemporalRegistryError("시간 조건에 확정된 구간이 없습니다")
    start, end = data.interval.dates()
    return event_ir.TimeFilter(
        field=event_ir.FieldRef(name=data.binding.time_field),
        window=event_ir.AbsoluteInterval(start=start, end_exclusive=end),
    )


def _comparison(
    data: treg.LoweringInput, field_id: str, operator: str, value: str
) -> event_ir.Comparison:
    return event_ir.Comparison(
        operator=operator,
        left=event_ir.FieldRef(name=field_id),
        right=event_ir.Literal(value=value),
        evidence=_evidence(data),
    )


def _predicate_conditions(data: treg.LoweringInput) -> tuple[event_ir.Condition, ...]:
    """술어 → 관측 행에 걸리는 조건들(값 조건이 없는 술어는 빈 튜플)."""
    predicate = data.condition.predicate
    binding = data.binding
    if isinstance(predicate, sir.OccurrencePredicate):
        return ()
    if isinstance(predicate, sir.StatePredicate):
        return (
            _comparison(
                data, str(binding.value_field), predicate.comparison.operator, predicate.comparison.value
            ),
        )
    if isinstance(predicate, sir.TransitionPredicate):
        # 두 비교가 **한 Filter 안**에 있는 것이 전이의 존재 이유다. 나누면 서로 다른 행에서
        # 만족되어도 통과한다(1월에 골드→실버, 3월에 X→VIP 인 회원이 '골드에서 VIP로'에 걸린다).
        return (
            _comparison(data, str(binding.value_field), "=", predicate.to_value),
            _comparison(data, str(binding.prev_value_field), "=", predicate.from_value),
        )
    raise treg.TemporalRegistryError(  # pragma: no cover - 계약 검증이 먼저 막는다
        f"낮출 수 없는 술어입니다: {predicate.kind}"
    )


def _relation(
    data: treg.LoweringInput, *, extra: Sequence[event_ir.Condition] = (), with_time: bool = True
) -> event_ir.Relation:
    conditions: list[event_ir.Condition] = []
    if with_time and data.binding.time_field is not None and data.interval is not None:
        conditions.append(_time_filter(data))
    conditions.extend(extra)
    relation: event_ir.Relation = event_ir.Source(name=data.binding.source)
    if not conditions:
        return relation
    where = conditions[0] if len(conditions) == 1 else event_ir.And(operands=tuple(conditions))
    return event_ir.Filter(relation=relation, where=where)


def _exists(data: treg.LoweringInput, relation: event_ir.Relation) -> event_ir.Condition:
    return event_ir.Exists(relation=relation, evidence=_evidence(data))


# ── 공통 검증 ─────────────────────────────────────────────────────────────────────


def _point_state_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    """시점 상태 질의의 공통 계약: 현재값 전용 표현 + (요청한 시점이 있으면) 시점 정밀도."""
    issues: list[Issue] = []
    anchor = sir.anchor_of(condition)
    if binding.representation is Representation.CURRENT_ONLY and not isinstance(
        anchor, sir.ReferenceAnchor
    ):
        issues.append(Issue(
            "temporal_past_state_unsupported",
            f"binding {binding.id!r} 은 현재 상태만 보관합니다(current_only). 과거 시점의 상태는 "
            "이 관측 방식으로 답할 수 없습니다 — 이력 관측 방식이 필요합니다.",
            "selector.anchor",
        ))
    if isinstance(condition.selector, sir.AsOfSelector):
        # 시점 정밀도는 **요청한 그 시점**을 묻는 selector 에서만 뜻이 있다. ``PreviousSelector`` 의
        # anchor 는 어느 칸의 직전인지를 정할 뿐이고 답은 칸 하나이므로 정밀도 문제가 없다.
        issues.extend(_anchor_precision_issues(anchor, binding))
    return tuple(issues)


def _anchor_precision_issues(
    anchor: sir.Anchor | None, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    """요청한 시점을 이 관측 방식이 **그 정밀도로** 답할 수 있는가.

    칸 단위(월 이상)로 적재된 관측은 칸 안의 어느 순간도 대표하지 못한다. 그래서 판정은
    anchor 의 종류가 아니라 **정밀도**로 한다 — 예전에는 ``RelativeAnchor`` 만 검사해서
    같은 뜻을 절대 시각이나 '지금'으로 적으면 그대로 한 달 칸으로 접혔다(리뷰 실측).
    """
    if anchor is None:  # pragma: no cover - selector 어휘가 닫혀 있어 도달 불가
        return ()
    bucket_shaped = sir.UNIT_RANK[binding.semantic_grain] > sir.UNIT_RANK[sir.TimeUnit.DAY]
    if not bucket_shaped:
        return ()

    if isinstance(anchor, sir.RelativeAnchor):
        if sir.UNIT_RANK[anchor.unit] < sir.UNIT_RANK[binding.semantic_grain]:
            return (Issue(
                "temporal_anchor_grain_too_fine",
                f"{anchor.unit} 단위 시점은 {binding.semantic_grain} 단위로 적재된 관측"
                f"({binding.id!r})으로 답할 수 없습니다 — 칸보다 잘게 나눈 시점을 칸으로 "
                "근사하면 요청하지 않은 대상이 나옵니다.",
                "selector.anchor",
            ),)
        if anchor.unit == binding.semantic_grain:
            expected = {
                sir.Boundary.START: tcat.BucketSemantics.PERIOD_START_STATE,
                sir.Boundary.END: tcat.BucketSemantics.PERIOD_END_STATE,
            }[anchor.boundary]
            if binding.bucket_semantics is not expected:
                return (Issue(
                    "temporal_bucket_semantics_mismatch",
                    f"'{anchor.boundary}' 시점을 요구했지만 binding {binding.id!r} 은 한 행이 "
                    f"{binding.bucket_semantics} 를 대표한다고 선언했습니다. 그 계약 없이 답하면 "
                    "그 칸의 아무 시점 상태가 되고, 요청과 다른 집합입니다.",
                    "binding.bucket_semantics",
                ),)
        return ()

    # 절대 시각과 '지금'은 칸 안의 한 순간이다. 칸 단위 적재는 그 순간의 상태를 모른다.
    return (Issue(
        "temporal_anchor_grain_too_fine",
        f"순간 시점({anchor.kind})은 {binding.semantic_grain} 단위로 적재된 관측"
        f"({binding.id!r})으로 답할 수 없습니다. 이 관측은 칸 하나가 "
        f"{binding.bucket_semantics} 를 대표할 뿐이므로, 칸 안의 임의 시점을 물으면 답이 "
        "요청과 다른 집합이 됩니다 — 달력 시점(예: 지난달 말)으로 요청해야 합니다.",
        "selector.anchor",
    ),)


def _as_of_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    issues = list(_point_state_issues(condition, binding))
    selector = condition.selector
    if isinstance(selector, sir.AsOfSelector) and isinstance(selector.strategy, sir.LatestAtOrBefore):
        issues.append(Issue(
            "temporal_strategy_not_lowerable",
            "latest_at_or_before 는 주체별로 정렬해 한 행을 고르는 실행 primitive"
            "(PartitionBy/OrderBy/LimitPerEntity)를 요구하는데 실행 IR 에 그 노드가 없습니다. "
            "빈 칸을 이전 값으로 채우는 근사 대신 미지원으로 답합니다.",
            "selector.strategy",
        ))
    if binding.representation is not Representation.CURRENT_ONLY and not binding.unique_per_time_point:
        issues.append(Issue(
            "temporal_row_identity_ambiguous",
            f"binding {binding.id!r} 은 한 시점에 회원당 여러 행이 가능하다고 선언했습니다"
            f"(row_identity={list(binding.row_identity)}). 어느 행의 상태인지 정해지지 않으면 "
            "as_of 의 답이 적재에 따라 달라집니다.",
            "binding.row_identity",
        ))
    return tuple(issues)


def _occurrence_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    """구간 질의의 공통 계약(§13).

    시점 상태 능력(``supports_point_state``)은 여기서 보지 않는다 — 그것을 요구하는 연산자
    (as_of·previous_bucket·latest_in_window)가 자기 선언으로 요구한다. 구간 질의가 요구하는 것은
    '이 구간의 관측 행을 전부 볼 수 있는가'뿐이다.
    """
    predicate = condition.predicate
    capabilities = binding.observation_capabilities
    if isinstance(predicate, sir.OccurrencePredicate) and not capabilities.supports_all_occurrences:
        return (Issue(
            "temporal_occurrence_history_unavailable",
            f"binding {binding.id!r} 은 구간 안의 관측 행을 전부 볼 수 없다고 선언했습니다"
            "(supports_all_occurrences=False). 마지막 발생만 아는 표현에서 '구간에 발생이 "
            "있었는가'를 답하면 이력이 없는 것을 있는 것처럼 말하게 됩니다.",
            "binding.observation_capabilities",
        ),)
    return ()


def _every_bucket_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    issues = list(_occurrence_issues(condition, binding))
    selector = condition.selector
    if not isinstance(selector, sir.WindowSelector):  # pragma: no cover - 타입 검증이 먼저 막는다
        return tuple(issues)
    if isinstance(selector.window, sir.LifetimeWindow):
        # 기대 칸 수가 정의되지 않는 구간에서는 '모든 칸'이 셀 수 있는 수가 아니다.
        # 예전에는 여기서 걸리지 않아 lowerer 가 예외를 던져 결과 타입 계약을 깼다(리뷰 실측).
        issues.append(Issue(
            "temporal_unbounded_bucket_count",
            "시간 제한 없는 구간에는 기대 칸 수가 없습니다. 칸 전칭 판정에는 경계가 확정된 "
            "구간이 필요합니다.",
            "selector.window",
        ))
    if selector.bucket is None:
        issues.append(Issue(
            "temporal_bucket_missing",
            "칸 전칭 판정에는 '몇 칸으로 나눠 보는가'(selector.bucket) 선언이 필요합니다.",
            "selector.bucket",
        ))
    elif selector.bucket != binding.semantic_grain:
        issues.append(Issue(
            "temporal_bucket_grain_mismatch",
            f"요청한 칸 단위({selector.bucket})가 적재 칸({binding.semantic_grain})과 달라 "
            "칸 수를 셀 수 없습니다.",
            "selector.bucket",
        ))
    return tuple(issues)


def _unchanged_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    predicate = condition.predicate
    if (
        isinstance(predicate, sir.UnchangedPredicate)
        and predicate.observation_semantics is sir.ObservationSemantics.NEVER_CHANGED
        and not binding.observation_capabilities.supports_continuous_validity
    ):
        return (Issue(
            "temporal_continuous_validity_unavailable",
            f"binding {binding.id!r} 은 관측과 관측 사이의 상태를 모릅니다. 관측값이 모두 같다는 "
            "사실은 '한 번도 바뀌지 않았다'를 보장하지 않습니다 — 관측 기준 의미"
            "(observed_values_equal)로 요청하거나 연속 유효 표현이 필요합니다.",
            "predicate.observation_semantics",
        ),)
    return ()


def _transition_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    # 전이도 '한 시점의 관측'을 고르는 질의다 — as_of 와 같은 시점 계약을 건다.
    # 이 합성이 없으면 같은 anchor 가 상태 술어에서는 막히고 전이 술어에서는 통과했다(리뷰 실측).
    issues: list[Issue] = list(_point_state_issues(condition, binding))
    if binding.prev_value_field is None:
        issues.append(Issue(
            "temporal_transition_pair_unavailable",
            f"binding {binding.id!r} 은 한 관측 행에서 직전 값을 함께 읽을 수 있다고 선언하지 "
            "않았습니다(prev_value_field 없음). 관측 순서에서 직전 행을 고르는 것은 별도의 "
            "실행 primitive 를 요구합니다.",
            "binding.prev_value_field",
        ))
    predicate = condition.predicate
    if isinstance(predicate, sir.TransitionPredicate) and predicate.from_value == predicate.to_value:
        issues.append(Issue(  # pragma: no cover - 술어 생성에서 이미 막힌다
            "temporal_transition_values_identical",
            "전이는 서로 다른 두 값이 필요합니다.",
            "predicate",
        ))
    return tuple(issues)


def _relation_issues(
    condition: sir.TemporalCondition, binding: tcat.TemporalBindingSpec
) -> tuple[Issue, ...]:
    predicate = condition.predicate
    if not isinstance(predicate, sir.TemporalRelationPredicate):  # pragma: no cover
        return ()
    if predicate.left_binding != binding.id:
        return (Issue(
            "temporal_relation_binding_mismatch",
            f"시간 관계의 왼쪽 관측({predicate.left_binding!r})이 이 조건의 관측"
            f"({binding.id!r})과 다릅니다.",
            "predicate.left_binding",
        ),)
    if predicate.duration.unit not in {sir.TimeUnit.DAY, sir.TimeUnit.WEEK}:
        # 실행 IR 의 Duration 은 월을 30일, 연을 365일로 환산한다. 창 계산이 명시적으로 금지한
        # 근사이므로 관계 연산에서만 되살아나게 두지 않는다.
        return (Issue(
            "temporal_relation_duration_unit_unsupported",
            f"{predicate.duration.unit} 단위 기간은 일 수로 정확히 환산되지 않습니다"
            "(월=30일·연=365일 근사). 시간 관계의 폭은 일 또는 주 단위로 표현해야 합니다.",
            "predicate.duration",
        ),)
    selector = condition.selector
    if isinstance(selector, sir.WindowSelector) and not isinstance(
        selector.window, sir.LifetimeWindow
    ):
        # 관계는 두 사건 시각의 차이로 낮아지고, 그 노드에는 바깥 구간을 걸 자리가 없다.
        # 구간을 따로 EXISTS 로 덧붙이면 '같은 사건 쌍'이라는 보장이 사라져 다른 집합이 된다.
        return (Issue(
            "temporal_relation_window_unsupported",
            "기간이 붙은 시간 관계는 아직 낮출 수 없습니다. 관계 노드는 두 사건 시각의 차이만 "
            "표현하며, 바깥 구간을 따로 붙이면 같은 사건 쌍이라는 보장이 사라집니다.",
            "selector.window",
        ),)
    return ()


# ── lowerer ──────────────────────────────────────────────────────────────────────


def lower_point_state(data: treg.LoweringInput) -> event_ir.Condition:
    """한 시점의 상태. 현재값 전용 표현은 시간 조건 없이 주체 행을 직접 비교한다."""
    if data.binding.representation is Representation.CURRENT_ONLY:
        conditions = _predicate_conditions(data)
        return conditions[0] if len(conditions) == 1 else event_ir.And(operands=conditions)
    return _exists(data, _relation(data, extra=_predicate_conditions(data)))


def lower_in_window(data: treg.LoweringInput) -> event_ir.Condition:
    return _exists(data, _relation(data, extra=_predicate_conditions(data)))


def lower_none_in_window(data: treg.LoweringInput) -> event_ir.Condition:
    return event_ir.Not(operand=lower_in_window(data))


def lower_open_window(data: treg.LoweringInput) -> event_ir.Condition:
    """열린 구간(이전/이후)의 존재·부재.

    이름은 방향이 정하고(:func:`registry.resolve_operator_name`) 극성은 quantifier 가 정한다.
    둘을 한 lowerer 에서 함께 읽지 않으면 '이전에 한 번도 없음'이 표현 자체가 불가능해지거나,
    더 나쁘게는 부정이 사라진 채 긍정 SQL 로 나간다.
    """
    if isinstance(data.condition.quantifier, sir.NoneQuantifier):
        return lower_none_in_window(data)
    return lower_in_window(data)


def lower_all_observations(data: treg.LoweringInput) -> event_ir.Condition:
    """관측된 모든 시점에서 성립.

    관측이 하나도 없는 구간을 공허참으로 만들지 않으려고 존재 조건을 함께 건다(§12).
    """
    violations = tuple(
        event_ir.Not(operand=condition) for condition in _predicate_conditions(data)
    )
    return event_ir.And(operands=(
        _exists(data, _relation(data)),
        event_ir.Not(operand=_exists(data, _relation(data, extra=violations))),
    ))


def lower_every_bucket(data: treg.LoweringInput) -> event_ir.Condition:
    """구간의 모든 칸에서 관측되고 성립 = 성립한 **서로 다른 칸의 수** == 기대 칸 수."""
    if data.expected_buckets is None:  # pragma: no cover - orchestrator 가 채운다
        raise treg.TemporalRegistryError("칸 전칭 판정에 기대 칸 수가 없습니다")
    return event_ir.Comparison(
        operator="=",
        left=event_ir.Aggregate(
            function="count",
            relation=_relation(data, extra=_predicate_conditions(data)),
            expression=event_ir.FieldRef(name=str(data.binding.time_field)),
            distinct=True,
        ),
        right=event_ir.Literal(value=data.expected_buckets),
        evidence=_evidence(data),
    )


def lower_unchanged_observations(data: treg.LoweringInput) -> event_ir.Condition:
    """관측된 값이 모두 같다 = 서로 다른 값의 수 == 1(관측이 없으면 0 이므로 불성립)."""
    return event_ir.Comparison(
        operator="=",
        left=event_ir.Aggregate(
            function="count",
            relation=_relation(data),
            expression=event_ir.FieldRef(name=str(data.binding.value_field)),
            distinct=True,
        ),
        right=event_ir.Literal(value=1),
        evidence=_evidence(data),
    )


def lower_temporal_relation(data: treg.LoweringInput) -> event_ir.Condition:
    predicate = data.condition.predicate
    if not isinstance(predicate, sir.TemporalRelationPredicate):  # pragma: no cover
        raise treg.TemporalRegistryError("시간 관계 술어가 아닙니다")
    if data.right_binding is None:  # pragma: no cover - orchestrator 가 먼저 채운다
        raise treg.TemporalRegistryError("시간 관계에 오른쪽 관측이 없습니다")
    # 실행 IR 의 TemporalRelation 은 left 가 **기준 사건**이고 right 가 그 창 안에서 찾는 사건이다.
    # 의미 계층의 읽기 방향(left 가 right 이후)과 반대이므로 여기서 한 번만 뒤집는다.
    return event_ir.TemporalRelation(
        operator=str(predicate.relation),
        left=event_ir.EventReference(
            source=data.right_binding.source,
            selector=str(predicate.anchor_occurrence) if predicate.anchor_occurrence else "any",
        ),
        right=event_ir.EventReference(source=data.binding.source, selector="any"),
        duration=event_ir.Duration(value=predicate.duration.amount, unit=str(predicate.duration.unit)),
        evidence=_evidence(data),
    )


# ── 정의 ─────────────────────────────────────────────────────────────────────────

_STATE_LIKE = frozenset({sir.StatePredicate})
_OBSERVABLE = frozenset({sir.StatePredicate, sir.OccurrencePredicate})
_HISTORY_REPRESENTATIONS = frozenset({
    Representation.EVENT_LOG,
    Representation.PERIODIC_SNAPSHOT,
    Representation.VALIDITY_INTERVAL,
    Representation.SINGLETON,
})


def operator_definitions() -> tuple[treg.TemporalOperatorDefinition, ...]:
    """등록할 연산자 전부. ``lower=None`` 은 오타가 아니라 **선언된 미지원**이다."""
    return (
        treg.TemporalOperatorDefinition(
            name=treg.AS_OF,
            selector_types=frozenset({sir.AsOfSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=frozenset({
                Representation.PERIODIC_SNAPSHOT,
                Representation.VALIDITY_INTERVAL,
                Representation.CURRENT_ONLY,
            }),
            required_capabilities=frozenset({"supports_point_state"}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            validate=_as_of_issues,
            lower=lower_point_state,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.PREVIOUS_BUCKET,
            selector_types=frozenset({sir.PreviousSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=frozenset({
                Representation.PERIODIC_SNAPSHOT,
                Representation.VALIDITY_INTERVAL,
            }),
            required_capabilities=frozenset({"supports_point_state"}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            validate=_point_state_issues,
            lower=lower_point_state,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.PREVIOUS_OBSERVATION,
            selector_types=frozenset({sir.PreviousSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            required_capabilities=frozenset({"supports_ordered_observations"}),
            unsupported_reason=(
                "실제로 존재하는 이전 관측 행을 고르려면 주체별 정렬과 행 선택"
                "(PartitionBy/OrderBy/LimitPerEntity)이 필요한데 실행 IR 에 그 primitive 가 "
                "없습니다. 직전 **칸**(temporal.previous_bucket)은 다른 의미이며 그것은 낮출 수 "
                "있습니다."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.PREVIOUS_DISTINCT_VALUE,
            selector_types=frozenset({sir.PreviousSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            required_capabilities=frozenset({"supports_ordered_observations"}),
            unsupported_reason=(
                "'현재값과 다른 마지막 값'은 값이 반복된 구간을 건너뛰며 훑어야 하므로 "
                "Lag/윈도 함수가 필요합니다. 실행 IR 에 그 primitive 가 없습니다."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.IN_WINDOW,
            selector_types=frozenset({sir.WindowSelector}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=_OBSERVABLE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            validate=_occurrence_issues,
            lower=lower_in_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.BEFORE,
            selector_types=frozenset({sir.WindowSelector}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            quantifier_types=frozenset({sir.ExistsQuantifier, sir.NoneQuantifier}),
            predicate_types=_OBSERVABLE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            validate=_occurrence_issues,
            lower=lower_open_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.AFTER,
            selector_types=frozenset({sir.WindowSelector}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            quantifier_types=frozenset({sir.ExistsQuantifier, sir.NoneQuantifier}),
            predicate_types=_OBSERVABLE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            validate=_occurrence_issues,
            lower=lower_open_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.LATEST_IN_WINDOW,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.LatestObservationQuantifier}),
            predicate_types=frozenset({sir.OccurrencePredicate}),
            # 마지막 발생이 물리적으로 이미 한 행인 표현에서만 낮춘다. 이벤트 로그에서 같은
            # 질문을 답하려면 MAX(시각) 집계가 필요하고, 그것은 다른 모양이다.
            accepted_representations=frozenset({
                Representation.LATEST_ONLY,
                Representation.SINGLETON,
            }),
            required_capabilities=frozenset({"supports_point_state"}),
            lower=lower_in_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.NONE_IN_WINDOW,
            selector_types=frozenset({sir.WindowSelector}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            quantifier_types=frozenset({sir.NoneQuantifier}),
            predicate_types=_OBSERVABLE,
            accepted_representations=_HISTORY_REPRESENTATIONS,
            validate=_occurrence_issues,
            lower=lower_none_in_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.ALL_OBSERVATIONS,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.AllObservationsQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=frozenset({
                Representation.EVENT_LOG,
                Representation.PERIODIC_SNAPSHOT,
                Representation.VALIDITY_INTERVAL,
            }),
            required_capabilities=frozenset({"supports_ordered_observations"}),
            # 값이 NULL 인 관측을 '제외'하려면 IS NULL 술어가 필요한데 실행 IR 에 없다.
            # 이 모양이 실제로 주는 의미는 '위반으로 센다'이므로 그것만 받는다.
            accepted_null_policies=frozenset({sir.NullPolicy.TREAT_AS_MISMATCH}),
            accepted_empty_window_policies=frozenset({sir.EmptyWindowPolicy.REQUIRE_OBSERVATION}),
            validate=_occurrence_issues,
            lower=lower_all_observations,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.EVERY_BUCKET,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.EveryBucketQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=frozenset({
                Representation.PERIODIC_SNAPSHOT,
                Representation.VALIDITY_INTERVAL,
            }),
            required_capabilities=frozenset({"supports_complete_bucket_enumeration"}),
            accepted_null_policies=frozenset({sir.NullPolicy.EXCLUDE}),
            accepted_empty_window_policies=frozenset({sir.EmptyWindowPolicy.REQUIRE_OBSERVATION}),
            validate=_every_bucket_issues,
            lower=lower_every_bucket,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.THROUGHOUT,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ThroughoutQuantifier}),
            predicate_types=_STATE_LIKE,
            accepted_representations=frozenset({Representation.VALIDITY_INTERVAL}),
            required_capabilities=frozenset({"supports_continuous_validity"}),
            unsupported_reason=(
                "구간 '내내'는 관측과 관측 사이의 상태까지 요구합니다. 관측 기준 전칭"
                "(temporal.all)과 뜻이 다르므로 그것으로 대신 답하지 않습니다."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.UNCHANGED_OBSERVATIONS,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.AllObservationsQuantifier}),
            predicate_types=frozenset({sir.UnchangedPredicate}),
            accepted_representations=frozenset({
                Representation.PERIODIC_SNAPSHOT,
                Representation.EVENT_LOG,
                Representation.VALIDITY_INTERVAL,
            }),
            required_capabilities=frozenset({"supports_ordered_observations"}),
            accepted_empty_window_policies=frozenset({sir.EmptyWindowPolicy.REQUIRE_OBSERVATION}),
            validate=_unchanged_issues,
            lower=lower_unchanged_observations,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.DIRECT_TRANSITION,
            selector_types=frozenset({sir.AsOfSelector, sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.TransitionPredicate}),
            accepted_representations=frozenset({Representation.PERIODIC_SNAPSHOT}),
            required_capabilities=frozenset({"supports_ordered_observations"}),
            accepted_strategies=frozenset({"exact_bucket"}),
            validate=_transition_issues,
            lower=lower_in_window,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.CHANGED_BETWEEN_ENDPOINTS,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.TransitionPredicate}),
            accepted_representations=_HISTORY_REPRESENTATIONS,
            required_capabilities=frozenset({"supports_ordered_observations"}),
            unsupported_reason=(
                "구간의 첫 관측과 마지막 관측을 골라 비교하려면 주체별 정렬과 행 선택이 "
                "필요합니다. 실행 IR 에 그 primitive 가 없습니다."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.CHANGED_WITHIN_WINDOW,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.TransitionPredicate}),
            accepted_representations=_HISTORY_REPRESENTATIONS,
            required_capabilities=frozenset({"supports_ordered_observations"}),
            unsupported_reason=(
                "구간 안 어딘가의 값 변화는 관측 쌍을 훑어야 하므로 Lag 가 필요합니다. "
                "한 행에 직전 값이 함께 있는 표현이라면 temporal.direct_transition 이 "
                "그 의미를 정확히 낮춥니다."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.CHANGE_COUNT,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.ChangeCountPredicate}),
            accepted_representations=_HISTORY_REPRESENTATIONS,
            required_capabilities=frozenset({"supports_intra_bucket_changes"}),
            unsupported_reason=(
                "값 변경 **횟수**는 관측 사이의 변화를 세는 것이라 Lag 와 집계가 필요합니다. "
                "덧붙여 주기적 스냅샷에서 센 변화는 실제 업무 변경 횟수가 아니라 "
                "'관측된 변화 수'이며, 두 수를 같은 이름으로 부르지 않습니다(§8)."
            ),
        ),
        treg.TemporalOperatorDefinition(
            name=treg.WITHIN_AFTER,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.TemporalRelationPredicate}),
            accepted_representations=frozenset({
                Representation.EVENT_LOG,
                Representation.SINGLETON,
            }),
            required_capabilities=frozenset({"supports_all_occurrences"}),
            validate=_relation_issues,
            lower=lower_temporal_relation,
        ),
        treg.TemporalOperatorDefinition(
            name=treg.WITHIN_BEFORE,
            selector_types=frozenset({sir.WindowSelector}),
            quantifier_types=frozenset({sir.ExistsQuantifier}),
            predicate_types=frozenset({sir.TemporalRelationPredicate}),
            accepted_representations=frozenset({
                Representation.EVENT_LOG,
                Representation.SINGLETON,
            }),
            required_capabilities=frozenset({"supports_all_occurrences"}),
            validate=_relation_issues,
            unsupported_reason=(
                "실행 IR 에는 노드가 있으나 SQL 컴파일러가 within_after 만 렌더합니다. "
                "방향을 뒤집어 within_after 로 표현하면 같은 뜻을 정확히 낼 수 있습니다."
            ),
        ),
    )


def create_default_temporal_operator_registry() -> treg.TemporalOperatorRegistry:
    """명시적 초기화 — import 부작용으로 레지스트리를 만들지 않는다."""
    return treg.TemporalOperatorRegistry(operator_definitions())


__all__ = [
    "create_default_temporal_operator_registry",
    "lower_all_observations",
    "lower_every_bucket",
    "lower_in_window",
    "lower_none_in_window",
    "lower_open_window",
    "lower_point_state",
    "lower_temporal_relation",
    "lower_unchanged_observations",
    "operator_definitions",
]
