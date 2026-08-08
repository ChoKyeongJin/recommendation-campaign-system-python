"""Temporal lowering 의 고정 계약 — 의미 IR → 실행 IR → SQL, 그리고 **무엇이 막히는가**.

재는 것은 선언에서 SQL 이 나오는가이지 실데이터가 무엇을 돌려주는가가 아니다. 데이터베이스에
연결하지 않고 회원 행도 스냅샷 행도 만들지 않는다.

이 파일의 절반은 **성공하지 않아야 하는 것들**이다. 시간 표현에서 가장 비싼 오답은 실패가
아니라 '표면상 성공한 축소'이기 때문이다 — 지난달 조건이 현재값 비교로 접히거나, 관측된 값이
같다는 사실이 '한 번도 바뀌지 않았다'로 승격되면 잘못된 대상이 발송된 뒤에야 드러난다.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import temporal_ir  # noqa: E402
from temporal_ir import catalog as tcat  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=SEOUL)
TODAY = date(2026, 8, 5)
SNAPSHOT = "member.grade.monthly_snapshot"
EVIDENCE = sir.Evidence(text="지난달 말 기준 VIP", start=6, end=18)


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def runtime(semantic_catalog) -> temporal_ir.TemporalRuntime:
    return temporal_ir.create_temporal_runtime(semantic_catalog)


@pytest.fixture()
def context() -> sir.TemporalRequestContext:
    return sir.TemporalRequestContext(now=NOW)


def _sql(semantic_catalog, expression: event_ir.Condition) -> str:
    return event_compiler.compile_condition(
        expression, semantic_catalog.compile_context(today=TODAY, literals=True)
    ).sql


def _state(value: str = "vip", **kwargs) -> sir.StatePredicate:
    return sir.StatePredicate(comparison=sir.Comparison(operator="=", value=value), **kwargs)


def _as_of(anchor: sir.Anchor, **overrides) -> sir.TemporalCondition:
    payload = {
        "metric": "member.grade",
        "binding": SNAPSHOT,
        "selector": sir.AsOfSelector(anchor=anchor, strategy=sir.ExactBucket()),
        "quantifier": sir.ExistsQuantifier(),
        "predicate": _state(),
        "evidence": EVIDENCE,
    }
    payload.update(overrides)
    return sir.TemporalCondition(**payload)


def _previous_month_end() -> sir.RelativeAnchor:
    return sir.RelativeAnchor(offset=-1, unit="month", boundary="end")


def _months(amount: int, *, include_current: bool = False) -> sir.RelativeWindow:
    return sir.RelativeWindow(
        amount=amount, unit="month", mode="calendar", include_current=include_current
    )


# ── 단순 상태 조회 ────────────────────────────────────────────────────────────────


def test_female_and_vip_last_month_end_keeps_both_conditions(runtime, semantic_catalog, context) -> None:
    """§16 의 문제 문장. 성별 조건과 스냅샷 조건이 **둘 다** SQL 에 있어야 한다."""
    gender = event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef("subject.gender"),
        right=event_ir.Literal("female"),
        evidence=event_ir.Evidence(text="여성", start=0, end=2),
    )
    composition = runtime.compose([_as_of(_previous_month_end())], context, additional=[gender])

    assert composition.status == "compiled"
    sql = _sql(semantic_catalog, composition.expression)
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "CRM_MB_MONTHCRMINFO" in sql
    assert "MS.MEMBER_NO = B.MEMBER_NO" in sql
    assert "MS.YYYYMM >= '202607'" in sql and "MS.YYYYMM < '202608'" in sql
    assert "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql


def test_current_grade_uses_the_current_binding(runtime, semantic_catalog, context) -> None:
    """현재 시점은 두 관측 모두 답할 수 있고, 그때 선언된 우선순위가 현재값 컬럼을 고른다."""
    outcome = runtime.lower(
        _as_of(sir.ReferenceAnchor(), binding=None), context
    )
    assert outcome.status == "compiled"
    assert outcome.receipt.binding == "member.grade.current"
    assert _sql(semantic_catalog, outcome.expression) == "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'"


def test_past_grade_falls_back_to_the_history_binding(runtime, context) -> None:
    """과거 시점을 물으면 현재값 관측은 후보에서 탈락하고 이력 관측이 남는다."""
    outcome = runtime.lower(_as_of(_previous_month_end(), binding=None), context)
    assert outcome.status == "compiled"
    assert outcome.receipt.binding == SNAPSHOT


def test_previous_bucket_and_as_of_agree_on_the_same_month(runtime, context) -> None:
    as_of = runtime.lower(_as_of(_previous_month_end()), context)
    previous = runtime.lower(
        _as_of(
            sir.ReferenceAnchor(),
            selector=sir.PreviousSelector(
                anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.BUCKET
            ),
        ),
        context,
    )
    assert previous.status == "compiled"
    assert previous.receipt.normalized_window == as_of.receipt.normalized_window
    assert previous.receipt.operator == "temporal.previous_bucket"


# ── 데이터 표현 제약 ──────────────────────────────────────────────────────────────


def test_current_only_binding_cannot_answer_a_past_point(runtime, context) -> None:
    outcome = runtime.lower(_as_of(_previous_month_end(), binding="member.grade.current"), context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_past_state_unsupported"


def test_day_precision_is_not_approximated_by_a_monthly_snapshot(runtime, context) -> None:
    outcome = runtime.lower(
        _as_of(sir.RelativeAnchor(offset=-1, unit="day", boundary="end")), context
    )
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_anchor_grain_too_fine"


def test_rolling_day_window_is_not_folded_into_month_buckets(runtime, context) -> None:
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.WindowSelector(
            window=sir.RelativeWindow(amount=30, unit="day", mode="rolling", include_current=True)
        ),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_window_not_bucket_aligned"


def test_latest_only_binding_refuses_arbitrary_occurrence_questions(runtime, context) -> None:
    """'구간에 로그인이 있었는가'와 '마지막 로그인이 그 구간인가'는 다른 계약이다(§13)."""
    window = sir.WindowSelector(
        window=sir.RelativeWindow(amount=30, unit="day", mode="rolling", include_current=True)
    )
    any_occurrence = sir.TemporalCondition(
        metric="member.login",
        binding="member.login.latest",
        selector=window,
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.OccurrencePredicate(),
        evidence=EVIDENCE,
    )
    latest = sir.TemporalCondition(
        metric="member.login",
        binding="member.login.latest",
        selector=window,
        quantifier=sir.LatestObservationQuantifier(),
        predicate=sir.OccurrencePredicate(),
        evidence=EVIDENCE,
    )
    assert runtime.lower(any_occurrence, context).status == "unsupported"
    assert runtime.lower(latest, context).status == "compiled"


def test_snapshot_change_count_measures_observed_changes(runtime, context) -> None:
    """스냅샷의 변경 횟수는 **관측된 값 변화**를 센다 — 그 사실은 영수증이 말하고, SQL 은 낸다.

    예전 계약은 같은 요청을 ``supports_intra_bucket_changes`` 미선언으로 닫았다. 그 플래그는
    스키마 모양이 아니라 **적재 밀도**에 대한 진술이라(칸 안에서 몇 번 바뀌었는지 아는가),
    답할 수 있는 질문("관측 사이에 값이 몇 번 달라졌는가")까지 함께 막혔다. 측정 의미의
    한계는 결과를 읽을 때 필요한 사실이므로 ``measurement`` 로 고지하고 컴파일은 한다.
    """
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.ChangeCountPredicate(
            transition=sir.AnyValueChange(),
            comparison=sir.NumericComparison(operator=">=", value=Decimal("2")),
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled", outcome
    assert outcome.receipt.measurement == "observed_value_changes"
    assert outcome.receipt.operator == "temporal.change_count"


def test_change_count_needs_a_way_to_read_the_previous_value(runtime, context) -> None:
    """직전 값을 읽을 방법이 **선언에** 없으면 그때가 미지원이다(데이터 양과 무관하다).

    구매 이벤트에는 값 필드도 직전 값도 없다 — 그래서 이 관측은 애초에 이 연산자를 선언하지
    않았고, 판정은 그 선언에서 나온다. 이것이 유지되어야 할 fail-close 다(§14).
    """
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.ChangeCountPredicate(
            transition=sir.AnyValueChange(),
            comparison=sir.NumericComparison(operator=">=", value=Decimal("2")),
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported", outcome
    assert outcome.code == "temporal_operator_unsupported_by_binding"


def test_observed_values_equal_is_not_promoted_to_never_changed(runtime, semantic_catalog, context) -> None:
    def _unchanged(semantics: str) -> sir.TemporalCondition:
        return sir.TemporalCondition(
            metric="member.grade",
            binding=SNAPSHOT,
            selector=sir.WindowSelector(window=_months(3)),
            quantifier=sir.AllObservationsQuantifier(),
            predicate=sir.UnchangedPredicate(observation_semantics=semantics),
            evidence=EVIDENCE,
        )

    observed = runtime.lower(_unchanged("observed_values_equal"), context)
    continuous = runtime.lower(_unchanged("never_changed"), context)
    assert observed.status == "compiled"
    assert "COUNT(DISTINCT MS.ZTS_GRADE) = 1" in _sql(semantic_catalog, observed.expression)
    assert continuous.status == "unsupported"
    assert continuous.code == "temporal_continuous_validity_unavailable"


# ── previous 의 세 가지 의미 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_operator"),
    [
        (sir.PreviousKind.BUCKET, "compiled", "temporal.previous_bucket"),
        # 2026-08-08 변경: 직전 **관측**은 이 선언(한 행에 PREV_* 컬럼)으로 낮출 수 있다.
        # 예전에는 '주체별 정렬이 필요하다'를 이유로 미지원이었는데, 그 이유는 정렬로만
        # 직전 행을 고를 수 있는 선언에만 해당한다 — 판정 근거를 capability 문자열에서
        # 스키마 모양(prev_value_field)으로 옮겼다. 정렬이 필요한 선언은
        # test_previous_observation_without_prev_column_is_unsupported 가 고정한다.
        (sir.PreviousKind.OBSERVATION, "compiled", "temporal.previous_observation"),
        (sir.PreviousKind.DISTINCT_VALUE, "unsupported", "temporal.previous_distinct_value"),
    ],
)
def test_previous_kinds_are_three_different_questions(
    runtime, semantic_catalog, context, kind, expected_status, expected_operator
) -> None:
    """누락된 달과 값 반복에서 세 의미는 서로 다른 답을 낸다 — 하나로 합치지 않는다."""
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.PreviousSelector(anchor=sir.ReferenceAnchor(), previous_kind=kind),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == expected_status
    if expected_status == "unsupported":
        assert expected_operator in outcome.message


def test_previous_bucket_and_observation_ask_different_questions(
    runtime, semantic_catalog, context
) -> None:
    """둘 다 낮춰지더라도 **같은 SQL 이 되면 안 된다** — 이 시험이 그 합쳐짐을 막는다.

    직전 칸은 '앞 칸 행의 현재값'이고 직전 관측은 '이 행이 들고 있는 직전값'이다. 적재가
    한 칸뿐인 배포에서 두 뜻은 서로 다른 답을 낸다(앞 칸은 아예 없다).
    """

    def sql(kind: sir.PreviousKind) -> str:
        outcome = runtime.lower(
            _as_of(
                sir.ReferenceAnchor(),
                selector=sir.PreviousSelector(
                    anchor=sir.ReferenceAnchor(), previous_kind=kind
                ),
            ),
            context,
        )
        assert outcome.status == "compiled"
        return _sql(semantic_catalog, outcome.expression)

    bucket = sql(sir.PreviousKind.BUCKET)
    observation = sql(sir.PreviousKind.OBSERVATION)
    assert bucket != observation
    # 칸은 현재값 컬럼을 앞 칸에서, 관측은 직전값 컬럼을 같은 칸에서 읽는다.
    assert "PREV_ZTS_GRADE" not in bucket
    assert "PREV_ZTS_GRADE" in observation


# ── 전칭과 전이 ───────────────────────────────────────────────────────────────────


def test_every_bucket_counts_the_expected_number_of_buckets(runtime, semantic_catalog, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3), bucket=sir.TimeUnit.MONTH),
        quantifier=sir.EveryBucketQuantifier(),
        predicate=_state(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert outcome.receipt.expected_buckets == 3
    assert "COUNT(DISTINCT MS.YYYYMM) = 3" in _sql(semantic_catalog, outcome.expression)


def test_every_bucket_rejects_a_bucket_unit_the_data_does_not_have(runtime, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3), bucket=sir.TimeUnit.DAY),
        quantifier=sir.EveryBucketQuantifier(),
        predicate=_state(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_bucket_grain_mismatch"


def test_all_observations_never_becomes_vacuously_true(runtime, semantic_catalog, context) -> None:
    """관측이 없는 구간을 참으로 만들지 않는다 — 존재 조건이 함께 걸린다(§12)."""
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.AllObservationsQuantifier(),
        predicate=_state(null_policy=sir.NullPolicy.TREAT_AS_MISMATCH),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    sql = _sql(semantic_catalog, outcome.expression)
    assert sql.count("EXISTS") == 2 and "NOT EXISTS" in sql
    # 극성을 **값 수준으로** 고정한다. EXISTS 개수만 세면 위반 조건의 부정이 사라져 의미가
    # 정반대가 된 구현(VIP 였던 달이 하나도 없음)도 그대로 통과한다(리뷰 실측).
    negated = [
        node
        for node in event_ir.walk(outcome.expression)
        if isinstance(node, event_ir.Not)
        and isinstance(node.operand, event_ir.Comparison)
        and isinstance(node.operand.right, event_ir.Literal)
        and node.operand.right.value == "vip"
    ]
    assert len(negated) == 1, outcome.expression.to_dict()
    assert "NOT (CASE WHEN (MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP')" in sql


def test_all_observations_rejects_a_null_policy_it_cannot_deliver(runtime, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.AllObservationsQuantifier(),
        predicate=_state(null_policy=sir.NullPolicy.EXCLUDE),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_null_policy_unsupported"


def test_direct_transition_keeps_both_values_in_one_observation(runtime, semantic_catalog, context) -> None:
    condition = _as_of(
        _previous_month_end(),
        predicate=sir.TransitionPredicate(
            from_value="gold_grade",
            to_value="vip",
            transition_mode=sir.TransitionMode.DIRECT_OBSERVED_TRANSITION,
        ),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    sql = _sql(semantic_catalog, outcome.expression)
    # 두 비교가 같은 EXISTS 안에 있어야 서로 다른 달에서 만족되는 오답이 막힌다.
    assert sql.count("EXISTS") == 1
    assert "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql
    assert "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in sql


def test_endpoint_change_is_a_different_operator_and_stays_unsupported(runtime, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TransitionPredicate(
            from_value="gold_grade", to_value="vip", transition_mode=sir.TransitionMode.ENDPOINT_CHANGE
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert "temporal.changed_between_endpoints" in outcome.message


# ── 이벤트와 시간 관계 ────────────────────────────────────────────────────────────


def test_absence_in_an_absolute_window_is_an_anti_join(runtime, semantic_catalog, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(
            window=sir.AbsoluteWindow(
                start=datetime(2026, 1, 1, tzinfo=SEOUL),
                end_exclusive=datetime(2026, 7, 1, tzinfo=SEOUL),
            )
        ),
        quantifier=sir.NoneQuantifier(),
        predicate=sir.OccurrencePredicate(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    sql = _sql(semantic_catalog, outcome.expression)
    assert sql.startswith("NOT EXISTS")
    assert "EO.ORDER_DATE >= '20260101'" in sql and "EO.ORDER_DATE < '20260701'" in sql


def test_lifetime_window_means_no_time_filter(runtime, semantic_catalog, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.OccurrencePredicate(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert outcome.receipt.normalized_window is None
    assert "ORDER_DATE" not in _sql(semantic_catalog, outcome.expression)


def test_within_after_uses_the_declared_anchor_event(runtime, semantic_catalog, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.signup.event",
            relation=sir.RelationKind.WITHIN_AFTER,
            duration=sir.Duration(amount=7, unit="day"),
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    sql = _sql(semantic_catalog, outcome.expression)
    assert "B.REG_DT" in sql and "ORDER_DATE" in sql


def test_repeat_purchase_requires_an_explicit_anchor_occurrence(runtime, semantic_catalog, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.purchase.events",
            relation=sir.RelationKind.WITHIN_AFTER,
            duration=sir.Duration(amount=30, unit="day"),
            anchor_occurrence=sir.OccurrenceSelector.FIRST,
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert "MIN(EO1.ORDER_DATE)" in _sql(semantic_catalog, outcome.expression)


def test_bounded_window_on_a_relation_is_refused_instead_of_approximated(runtime, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=_months(3)),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.signup.event",
            relation=sir.RelationKind.WITHIN_AFTER,
            duration=sir.Duration(amount=7, unit="day"),
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_relation_window_unsupported"


def test_open_ended_window_is_closed_by_declared_coverage(runtime, semantic_catalog, context) -> None:
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.WindowSelector(
            window=sir.OpenEndedWindow(
                anchor=sir.RelativeAnchor(offset=0, unit="month", boundary="start"),
                direction=sir.Direction.BEFORE,
            )
        ),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert outcome.receipt.normalized_window["start"].startswith("2017-01-01")
    assert "MS.YYYYMM >= '201701'" in _sql(semantic_catalog, outcome.expression)


def test_open_ended_window_without_declared_coverage_is_unsupported(
    semantic_catalog, context
) -> None:
    """열린 쪽을 임의의 과거로 채우지 않는다 — 닫을 근거가 선언에 없으면 답하지 않는다."""
    payload = json.loads(tcat.DEFAULT_TEMPORAL_CATALOG_PATH.read_text(encoding="utf-8"))
    payload["bindings"][SNAPSHOT].pop("coverage")
    unbounded = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.WindowSelector(
            window=sir.OpenEndedWindow(
                anchor=sir.RelativeAnchor(offset=0, unit="month", boundary="start"),
                direction=sir.Direction.BEFORE,
            )
        ),
    )
    outcome = unbounded.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_open_window_unbounded"


# ── 부분 합성 차단 ────────────────────────────────────────────────────────────────


def test_composition_blocks_everything_when_one_condition_fails(runtime, context) -> None:
    """여성 조건만 남고 VIP 조건이 사라지는 부분 SQL 은 나가지 않는다(§15)."""
    gender = event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef("subject.gender"),
        right=event_ir.Literal("female"),
        evidence=event_ir.Evidence(text="여성", start=0, end=2),
    )
    impossible = _as_of(_previous_month_end(), binding="member.grade.current")
    composition = runtime.compose([impossible], context, additional=[gender])

    assert composition.status == "blocked"
    assert composition.expression is None
    assert composition.receipts == ()
    assert composition.failures[0].code == "temporal_past_state_unsupported"


def test_a_value_outside_the_declared_domain_never_reaches_sql(runtime, context) -> None:
    outcome = runtime.lower(_as_of(_previous_month_end(), predicate=_state("platinum")), context)
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_value_unknown"


def test_ordered_comparison_needs_a_declared_ranking(runtime, context, semantic_catalog) -> None:
    condition = sir.TemporalCondition(
        metric="member.newproduct_favor",
        binding="member.newproduct_favor.monthly_snapshot",
        selector=sir.AsOfSelector(anchor=_previous_month_end(), strategy=sir.ExactBucket()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.StatePredicate(
            comparison=sir.Comparison(operator=">=", value="favors_new_product")
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_comparison_unsupported"


def test_conditions_without_evidence_get_no_receipt(runtime, context) -> None:
    outcome = runtime.lower(_as_of(_previous_month_end(), evidence=None), context)
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_evidence_missing"


def test_lowering_that_drops_a_requested_value_is_caught_by_the_receipt_gate(
    runtime, context
) -> None:
    """영수증은 lowerer 의 주장이 아니라 **낮춘 트리**를 확인한 뒤 발급한다.

    시간 조건만 남고 값 비교가 사라진 SQL 은 표면상 성공하지만 훨씬 넓은 집합이다.
    """
    import dataclasses

    from temporal_ir import lowering as tlow
    from temporal_ir import operators as tops
    from temporal_ir import registry as treg

    def _drops_the_value(data):
        return event_ir.Exists(
            relation=event_ir.Filter(
                relation=event_ir.Source(name=data.binding.source),
                where=tops._time_filter(data),
            ),
            evidence=data.condition.evidence,
        )

    broken = treg.TemporalOperatorRegistry([
        dataclasses.replace(definition, lower=_drops_the_value)
        if definition.name == "temporal.as_of"
        else definition
        for definition in tops.operator_definitions()
    ])
    outcome = tlow.lower_temporal_condition(
        _as_of(_previous_month_end()),
        temporal_catalog=runtime.temporal_catalog,
        semantic_catalog=runtime.semantic_catalog,
        context=context,
        registry=broken,
    )
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_partial_composition"


# ── coverage 정책 ────────────────────────────────────────────────────────────────


def test_out_of_coverage_still_compiles_under_the_advise_policy(runtime, context) -> None:
    """의미 지원 여부와 적재 여부는 다른 축이다 — 데이터가 아직 없다고 표현을 거절하지 않는다."""
    outcome = runtime.lower(_as_of(_previous_month_end()), context)
    assert outcome.status == "compiled"
    assert outcome.receipt.coverage_status == "supported_but_out_of_coverage"
    assert "out_of_coverage" in outcome.receipt.warnings


def test_block_policy_refuses_to_evaluate_outside_coverage(runtime) -> None:
    blocking = sir.TemporalRequestContext(now=NOW, coverage_policy=sir.CoveragePolicy.BLOCK)
    outcome = runtime.lower(_as_of(_previous_month_end()), blocking)
    assert outcome.status == "blocked_by_coverage"
    assert outcome.code == "supported_but_out_of_coverage"


def test_inside_coverage_is_evaluable(runtime) -> None:
    context = sir.TemporalRequestContext(now=datetime(2017, 2, 5, tzinfo=SEOUL))
    outcome = runtime.lower(_as_of(_previous_month_end()), context)
    assert outcome.status == "compiled"
    assert outcome.receipt.coverage_status == "supported_and_evaluable"
    assert outcome.receipt.normalized_window["start"].startswith("2017-01-01")


def test_partial_coverage_reports_the_missing_buckets(runtime) -> None:
    context = sir.TemporalRequestContext(now=datetime(2017, 4, 5, tzinfo=SEOUL))
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=SNAPSHOT,
        selector=sir.WindowSelector(window=_months(3), bucket=sir.TimeUnit.MONTH),
        quantifier=sir.EveryBucketQuantifier(),
        predicate=_state(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert outcome.receipt.coverage_status == "supported_but_incomplete"
    assert outcome.receipt.missing_buckets == ("201702", "201703")


# ── 영수증 ────────────────────────────────────────────────────────────────────────


def test_receipt_records_every_decision_that_shaped_the_sql(runtime, context) -> None:
    outcome = runtime.lower(_as_of(_previous_month_end()), context)
    receipt = outcome.receipt.to_dict()
    assert receipt["metric"] == "member.grade"
    assert receipt["binding"] == SNAPSHOT
    assert receipt["operator"] == "temporal.as_of"
    assert receipt["selector"] == "as_of" and receipt["quantifier"] == "exists"
    assert receipt["predicate_type"] == "state"
    assert receipt["selection_strategy"] == "exact_bucket"
    assert receipt["normalized_window"] == {
        "start": "2026-07-01T00:00:00+09:00",
        "end_exclusive": "2026-08-01T00:00:00+09:00",
    }
    assert receipt["timezone"] == "Asia/Seoul"
    assert receipt["value_domain"] == "grade"
    assert receipt["canonical_values"] == ["vip"]
    assert receipt["comparison_operator"] == "="
    assert receipt["subject_correlation"] == {"subject_key": "MEMBER_NO", "source_key": "MEMBER_NO"}
    assert receipt["missing_policy"] == "unknown" and receipt["null_policy"] == "exclude"
    assert receipt["evidence"]["text"] == EVIDENCE.text
    assert receipt["lowered_ir_hash"] and receipt["evidence_id"]
    assert json.loads(json.dumps(receipt, ensure_ascii=False)) == receipt


def test_receipt_round_trips_through_its_wire_form(runtime, context) -> None:
    from temporal_ir import lowering as tlow

    receipt = runtime.lower(_as_of(_previous_month_end()), context).receipt
    wire = json.loads(json.dumps(receipt.to_dict(), ensure_ascii=False))
    assert tlow.TemporalLoweringReceipt.from_dict(wire) == receipt


def test_receipt_hash_follows_the_lowered_tree(runtime, context) -> None:
    first = runtime.lower(_as_of(_previous_month_end()), context)
    same = runtime.lower(_as_of(_previous_month_end()), context)
    other = runtime.lower(_as_of(_previous_month_end(), predicate=_state("gold_grade")), context)
    assert first.receipt.lowered_ir_hash == same.receipt.lowered_ir_hash
    assert first.receipt.lowered_ir_hash != other.receipt.lowered_ir_hash


def test_same_request_and_clock_give_the_same_sql(runtime, semantic_catalog, context) -> None:
    """같은 입력·같은 기준 시각이면 같은 결과여야 한다(재현성)."""
    condition = _as_of(_previous_month_end())
    first = runtime.lower(condition, context)
    second = runtime.lower(sir.condition_from_dict(condition.to_dict()), context)
    assert _sql(semantic_catalog, first.expression) == _sql(semantic_catalog, second.expression)
    assert first.receipt.lowered_ir_hash == second.receipt.lowered_ir_hash


def test_failures_preserve_the_original_condition(runtime, context) -> None:
    """지원하지 않는 표현을 삭제하지 않는다 — 원본을 보존해 되돌려 준다."""
    condition = _as_of(_previous_month_end(), binding="member.grade.current")
    outcome = runtime.lower(condition, context)
    assert outcome.to_dict()["preserved_condition"] == condition.to_dict()


# ── 카탈로그 확장성(§19-1) ───────────────────────────────────────────────────────


def test_a_new_monthly_attribute_needs_only_catalog_declarations(semantic_catalog, context) -> None:
    """새 월별 속성은 카탈로그 두 항목이면 된다 — Python 분기는 하나도 늘지 않는다."""
    payload = json.loads(tcat.DEFAULT_TEMPORAL_CATALOG_PATH.read_text(encoding="utf-8"))
    snapshot = copy.deepcopy(payload["bindings"][SNAPSHOT])
    snapshot.update({
        "metric_id": "member.value_grade_axis",
        "value_field": "member_month_snapshot.worth_grade",
        "prev_value_field": "member_month_snapshot.prev_worth_grade",
        "label": "새로 추가한 월별 축",
    })
    payload["metrics"]["member.value_grade_axis"] = {
        "label": "새 축",
        "value_type": "string",
        "value_domain": "worth_grade",
        "supported_comparisons": ["=", "!="],
        "temporal_bindings": ["member.value_grade_axis.monthly_snapshot"],
    }
    payload["bindings"]["member.value_grade_axis.monthly_snapshot"] = snapshot

    runtime = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)
    condition = sir.TemporalCondition(
        metric="member.value_grade_axis",
        selector=sir.AsOfSelector(anchor=_previous_month_end(), strategy=sir.ExactBucket()),
        quantifier=sir.ExistsQuantifier(),
        predicate=_state(next(iter(sorted(
            dict(semantic_catalog.field("member_month_snapshot.worth_grade").compiler_field.value_map)
        )))),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "compiled"
    assert outcome.receipt.binding == "member.value_grade_axis.monthly_snapshot"
    assert "WORTH_GRADE" in _sql(semantic_catalog, outcome.expression)


# ── 적대적 검토(2026-08-05)에서 확인된 결함의 회귀 방어 ──────────────────────────
# 아래는 전부 "표면상 성공한 축소" 계열이라 status 만 보는 단정으로는 다시 새어 나간다.


@pytest.mark.parametrize(
    "anchor",
    [
        sir.AbsoluteAnchor(instant=datetime(2017, 1, 15, 14, tzinfo=SEOUL)),
        sir.ReferenceAnchor(),
    ],
    ids=["absolute_instant", "reference_now"],
)
def test_instant_anchors_are_not_folded_into_month_buckets(runtime, anchor) -> None:
    """정밀도 판정은 anchor 의 **종류**가 아니라 정밀도로 한다.

    예전에는 RelativeAnchor 만 검사해서, 같은 뜻을 절대 시각이나 '지금'으로 적으면 한 달 칸으로
    조용히 접혔다 — 같은 의미가 표기에 따라 다른 답을 냈다.
    """
    context = sir.TemporalRequestContext(now=datetime(2017, 3, 15, 9, tzinfo=SEOUL))
    condition = sir.TemporalCondition(
        metric="member.worth_grade",
        binding="member.worth_grade.monthly_snapshot",
        selector=sir.AsOfSelector(anchor=anchor, strategy=sir.ExactBucket()),
        quantifier=sir.ExistsQuantifier(),
        predicate=_state("vip_worth_grade"),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_anchor_grain_too_fine"


def test_previous_bucket_still_accepts_a_reference_anchor(runtime, context) -> None:
    """'직전 칸'의 anchor 는 어느 칸의 직전인지를 정할 뿐 — 답은 칸 하나라 정밀도 문제가 없다."""
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.PreviousSelector(
            anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.BUCKET
        ),
    )
    assert runtime.lower(condition, context).status == "compiled"


def test_transition_obeys_the_same_point_contract_as_a_state_query(runtime) -> None:
    """같은 anchor 가 상태 술어에서는 막히고 전이 술어에서는 통과하면 안 된다."""
    context = sir.TemporalRequestContext(now=datetime(2017, 3, 15, tzinfo=SEOUL))
    condition = _as_of(
        sir.RelativeAnchor(offset=-1, unit="day", boundary="end"),
        predicate=sir.TransitionPredicate(
            from_value="gold_grade",
            to_value="vip",
            transition_mode=sir.TransitionMode.DIRECT_OBSERVED_TRANSITION,
        ),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_anchor_grain_too_fine"


def test_null_error_policy_is_refused_rather_than_ignored(runtime, context) -> None:
    """'값이 NULL 이면 평가하지 말라'를 기본값과 같은 SQL 로 조용히 낮추지 않는다."""
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.WindowSelector(
            window=sir.AbsoluteWindow(
                start=datetime(2017, 1, 1, tzinfo=SEOUL),
                end_exclusive=datetime(2017, 2, 1, tzinfo=SEOUL),
            )
        ),
        predicate=_state(null_policy=sir.NullPolicy.ERROR),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_null_policy_unsupported"


def test_missing_error_policy_blocks_when_coverage_proves_the_gap(runtime, context) -> None:
    """적재 공백이 선언으로 증명되면 '없음'을 '조건 불성립'으로 바꾸지 않는다."""
    condition = _as_of(
        _previous_month_end(),
        selector=sir.AsOfSelector(
            anchor=_previous_month_end(),
            strategy=sir.ExactBucket(),
            missing_policy=sir.MissingPolicy.ERROR,
        ),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "blocked_by_coverage"
    assert "missing_policy=error" in outcome.message


def test_every_bucket_over_a_lifetime_window_returns_a_result_not_an_exception(
    runtime, context
) -> None:
    """기대 칸 수가 없는 구간은 결과 타입으로 답한다 — 예외가 합성 전체를 죽이지 않는다."""
    condition = _as_of(
        sir.ReferenceAnchor(),
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow(), bucket=sir.TimeUnit.MONTH),
        quantifier=sir.EveryBucketQuantifier(),
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_unbounded_bucket_count"


def test_value_predicate_on_a_binding_without_a_value_field_is_a_result(runtime, context) -> None:
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        quantifier=sir.ExistsQuantifier(),
        predicate=_state(),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_value_field_unavailable"


def test_relation_duration_must_convert_to_days_exactly(runtime, context) -> None:
    """월을 30일로 환산하는 근사는 창 계산이 금지한 것이다 — 관계에서만 되살리지 않는다."""
    condition = sir.TemporalCondition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.signup.event",
            relation=sir.RelationKind.WITHIN_AFTER,
            duration=sir.Duration(amount=1, unit="month"),
        ),
        evidence=EVIDENCE,
    )
    outcome = runtime.lower(condition, context)
    assert outcome.status == "unsupported"
    assert outcome.code == "temporal_relation_duration_unit_unsupported"


def _twin_binding_payload() -> dict:
    payload = json.loads(tcat.DEFAULT_TEMPORAL_CATALOG_PATH.read_text(encoding="utf-8"))
    twin = copy.deepcopy(payload["bindings"][SNAPSHOT])
    twin["label"] = "같은 질문에 답할 수 있는 두 번째 관측"
    payload["bindings"]["member.grade.twin_snapshot"] = twin
    payload["metrics"]["member.grade"]["temporal_bindings"].append("member.grade.twin_snapshot")
    payload["metrics"]["member.grade"].pop("binding_priority")
    return payload


def test_two_viable_bindings_without_priority_are_refused(semantic_catalog, context) -> None:
    """임의 선택 금지는 **실행 경로에서** 집행되어야 한다(이름 순서가 집합을 정하면 안 된다)."""
    runtime = temporal_ir.create_temporal_runtime(
        semantic_catalog, payload=_twin_binding_payload()
    )
    outcome = runtime.lower(_as_of(_previous_month_end(), binding=None), context)
    assert outcome.status == "invalid"
    assert outcome.code == "temporal_binding_ambiguous"


def test_declared_priority_resolves_two_viable_bindings(semantic_catalog, context) -> None:
    payload = _twin_binding_payload()
    payload["metrics"]["member.grade"]["binding_priority"] = ["member.grade.twin_snapshot"]
    runtime = temporal_ir.create_temporal_runtime(semantic_catalog, payload=payload)
    outcome = runtime.lower(_as_of(_previous_month_end(), binding=None), context)
    assert outcome.status == "compiled"
    assert outcome.receipt.binding == "member.grade.twin_snapshot"


def test_open_window_keeps_its_polarity(runtime, semantic_catalog, context) -> None:
    """열린 구간의 이름은 방향이, 극성은 quantifier 가 정한다.

    둘을 한 곳에서 함께 읽지 않으면 '그 이전에 한 번도 없음'이 표현 불가가 되거나, 더 나쁘게는
    부정이 사라진 채 긍정 SQL 로 나간다.
    """
    def _open(quantifier: sir.Quantifier) -> sir.TemporalCondition:
        return _as_of(
            sir.ReferenceAnchor(),
            selector=sir.WindowSelector(
                window=sir.OpenEndedWindow(
                    anchor=sir.RelativeAnchor(offset=0, unit="month", boundary="start"),
                    direction=sir.Direction.BEFORE,
                )
            ),
            quantifier=quantifier,
        )

    positive = runtime.lower(_open(sir.ExistsQuantifier()), context)
    negative = runtime.lower(_open(sir.NoneQuantifier()), context)
    assert positive.status == negative.status == "compiled"
    assert positive.receipt.operator == negative.receipt.operator == "temporal.before"
    assert not _sql(semantic_catalog, positive.expression).startswith("NOT EXISTS")
    assert _sql(semantic_catalog, negative.expression).startswith("NOT EXISTS")


def test_composition_requires_evidence_from_every_operand(runtime, context) -> None:
    """다른 컴파일러가 준 조건이라고 근거 없이 실려 나가지 않는다."""
    evidenceless = event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef("subject.gender"),
        right=event_ir.Literal("female"),
    )
    composition = runtime.compose(
        [_as_of(_previous_month_end())], context, additional=[evidenceless]
    )
    assert composition.status == "blocked"
    assert composition.expression is None
    assert composition.failures[0].code == "temporal_evidence_missing"
