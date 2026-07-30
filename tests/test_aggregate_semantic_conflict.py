"""존재/부재 의미 충돌 검증 — 모순된 SQL 은 조립되기 전에 막는다.

여기서 지키는 판정 규칙은 하나다: **기간이 겹치면 충돌이 아니라, 존재가 부재에 통째로 포함될 때만
충돌이다.** 겹침을 충돌로 보면 '최근 6개월 구매 있고 최근 1개월 구매 없는 고객'(2~6개월 전 구매자)
같은 정상 조건이 막힌다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import aggregate_parser_config as config
import aggregate_semantics as semantics
import graph_rag

ANCHOR = datetime(2026, 7, 30, 12, 0, 0)
DIMENSIONS = config.rules().semantic_domains["purchase"].event_dimensions


def _predicate(polarity: str, *, days: int | None, source_id: str, **constraints):
    window = semantics.rolling_window(ANCHOR, days) if days is not None else semantics.lifetime_window(ANCHOR)
    resolved = {dimension: None for dimension in DIMENSIONS}
    resolved.update({key: frozenset(value) if value is not None else None for key, value in constraints.items()})
    return semantics.EventPredicate(
        domain="purchase", polarity=polarity, window=window, constraints=resolved,
        source_kind="aggregate_inner_join" if polarity == semantics.PRESENCE else "purchase_inactivity",
        source_id=source_id,
    )


def _verdict(positive, negative) -> str:
    return semantics.classify_pair(positive, negative, DIMENSIONS)


# ── 1. 기간 포함 관계 ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("positive_days", "negative_days", "expected"),
    [
        (30, 180, semantics.PROVEN_CONFLICT),   # 최근 1개월 있음 + 최근 6개월 없음
        (90, 90, semantics.PROVEN_CONFLICT),    # 같은 기간
        (180, 30, semantics.PROVEN_SAFE),       # 최근 6개월 있음 + 최근 1개월 없음
        (30, 30, semantics.PROVEN_CONFLICT),
    ],
)
def test_conflict_requires_window_containment(positive_days, negative_days, expected) -> None:
    positive = _predicate(semantics.PRESENCE, days=positive_days, source_id="p")
    negative = _predicate(semantics.ABSENCE, days=negative_days, source_id="n")
    assert _verdict(positive, negative) == expected


def test_disjoint_past_range_is_safe() -> None:
    """'4~6개월 전 구매 있음' + '최근 1개월 구매 없음' — 겹치지 않으므로 정상."""
    positive = semantics.EventPredicate(
        domain="purchase", polarity=semantics.PRESENCE,
        window=semantics.NormalizedWindow(ANCHOR - timedelta(days=180), ANCHOR - timedelta(days=120)),
        constraints={d: None for d in DIMENSIONS},
        source_kind="aggregate_inner_join", source_id="p",
    )
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n")
    assert _verdict(positive, negative) == semantics.PROVEN_SAFE


def test_lifetime_presence_is_never_contained_in_a_finite_absence() -> None:
    positive = _predicate(semantics.PRESENCE, days=None, source_id="p")
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n")
    assert _verdict(positive, negative) == semantics.PROVEN_SAFE


def test_window_subset_is_half_open() -> None:
    window = semantics.NormalizedWindow(ANCHOR - timedelta(days=10), ANCHOR)
    assert semantics.window_is_subset(window, window)
    wider = semantics.NormalizedWindow(ANCHOR - timedelta(days=11), ANCHOR)
    assert semantics.window_is_subset(window, wider)
    assert not semantics.window_is_subset(wider, window)


# ── 2. 이벤트 조건 부분집합 ─────────────────────────────────────────────────────────────
def test_narrower_event_scope_inside_a_total_absence_conflicts() -> None:
    """'최근 1개월 온라인 구매 있음' + '최근 1개월 모든 구매 없음' — 온라인은 전체의 부분집합이다."""
    positive = _predicate(semantics.PRESENCE, days=30, source_id="p", channel={"online"})
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n")
    assert _verdict(positive, negative) == semantics.PROVEN_CONFLICT


def test_different_channels_do_not_conflict() -> None:
    positive = _predicate(semantics.PRESENCE, days=30, source_id="p", channel={"online"})
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n", channel={"offline"})
    assert _verdict(positive, negative) == semantics.PROVEN_SAFE


def test_completed_purchase_inside_a_total_absence_conflicts() -> None:
    positive = _predicate(semantics.PRESENCE, days=30, source_id="p", order_status={"completed"})
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n")
    assert _verdict(positive, negative) == semantics.PROVEN_CONFLICT


def test_total_presence_with_a_narrow_absence_is_not_a_proven_conflict() -> None:
    """'최근 1개월 전체 구매 있음' + '최근 1개월 취소 구매 없음' — 취소가 없어도 구매는 있을 수 있다."""
    positive = _predicate(semantics.PRESENCE, days=30, source_id="p")
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n", order_status={"cancelled"})
    assert _verdict(positive, negative) == semantics.PROVEN_SAFE


def test_constraint_subset_helper() -> None:
    assert semantics.constraints_are_subset(
        {"channel": frozenset({"online"})}, {"channel": None}, ["channel"],
    )
    assert not semantics.constraints_are_subset(
        {"channel": None}, {"channel": frozenset({"online"})}, ["channel"],
    )


# ── 3. UNKNOWN ─────────────────────────────────────────────────────────────────────────
def test_unresolved_period_is_unknown_not_a_silent_sql() -> None:
    positive = semantics.EventPredicate(
        domain="purchase", polarity=semantics.PRESENCE, window=None,
        constraints={d: None for d in DIMENSIONS},
        source_kind="aggregate_inner_join", source_id="p",
    )
    negative = _predicate(semantics.ABSENCE, days=30, source_id="n")
    assert _verdict(positive, negative) == semantics.UNKNOWN
    result = semantics.validate([positive, negative])
    assert result.verdict == semantics.UNKNOWN
    assert result.unresolved[0].code == "semantic_period_unresolved"


def test_snapshot_source_does_not_require_event_presence() -> None:
    positive = _predicate(semantics.PRESENCE, days=30, source_id="p")
    snapshot = semantics.EventPredicate(
        domain="purchase", polarity=positive.polarity, window=positive.window,
        constraints=positive.constraints, source_kind="member_summary_column",
        source_id="p", requires_event_presence=False,
    )
    negative = _predicate(semantics.ABSENCE, days=180, source_id="n")
    assert _verdict(positive, negative) == semantics.PROVEN_CONFLICT
    assert _verdict(snapshot, negative) == semantics.PROVEN_SAFE


# ── 4. 예외·로그 계약 ───────────────────────────────────────────────────────────────────
def test_typed_exception_carries_both_condition_ids() -> None:
    result = semantics.validate([
        _predicate(semantics.PRESENCE, days=30, source_id="agg[0]"),
        _predicate(semantics.ABSENCE, days=180, source_id="inactivity"),
    ])
    with pytest.raises(semantics.AggregateSemanticConflict) as raised:
        result.raise_if_conflicting()
    assert raised.value.code == "presence_absence_conflict"
    assert raised.value.positive_condition_id == "agg[0]"
    assert raised.value.negative_condition_id == "inactivity"


def test_log_fields_do_not_carry_the_user_query() -> None:
    finding = semantics.validate([
        _predicate(semantics.PRESENCE, days=30, source_id="agg[0]"),
        _predicate(semantics.ABSENCE, days=180, source_id="inactivity"),
    ]).conflicts[0]
    fields = finding.as_log_fields()
    assert set(fields) == {
        "conflict_code", "domain", "positive_condition_id", "negative_condition_id",
        "positive_window", "negative_window", "positive_source_kind", "negative_source_kind",
    }


def test_module_uses_no_assert_statements_as_a_safety_net() -> None:
    """``python -O`` 로 제거되는 문장을 입력 검증 안전장치로 쓰지 않는다."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(semantics))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]


# ── 5. 빌더 통합(SQL 조립 전 차단) ──────────────────────────────────────────────────────
def _plan_with(aggregate_window_days: int | None, inactivity_days: int | None) -> dict:
    return {
        "intent": "find_user_segment",
        "target_user": {
            "aggregate_conditions": [{
                "metric_id": "total_item_quantity", "operator": ">=", "threshold": 3.0,
                "window_days": aggregate_window_days, "label": "상품 수량",
            }],
            "purchase_inactivity": (
                {"min_days": inactivity_days} if inactivity_days is not None else None
            ),
        },
        "campaign_constraints": {},
    }


def test_builder_blocks_a_contained_presence_and_absence() -> None:
    plan = _plan_with(30, 180)
    assert graph_rag.build_aggregate_targets_sql_candidate(plan) is None
    assert plan["unsupported"]["reason"] == "presence_absence_conflict"
    for term in ("AGG", "NOT EXISTS", "semantic conflict"):
        assert term not in plan["unsupported"]["clarification"]


def test_builder_allows_a_partially_overlapping_pair() -> None:
    plan = _plan_with(180, 30)
    candidate = graph_rag.build_aggregate_targets_sql_candidate(plan)
    assert candidate is not None
    assert plan.get("unsupported") is None


def test_builder_blocks_the_reported_repro_shape() -> None:
    """원본 재현 쿼리가 파서를 뚫고 들어와도 SQL 로는 나가지 않는다."""
    plan = _plan_with(90, 90)
    assert graph_rag.build_aggregate_targets_sql_candidate(plan) is None
    assert plan["unsupported"]["reason"] == "presence_absence_conflict"


def test_builder_asks_for_periods_when_they_cannot_be_resolved() -> None:
    plan = _plan_with(None, 90)
    plan["target_user"]["aggregate_conditions"][0]["calendar_period"] = "last_month"
    assert graph_rag.build_aggregate_targets_sql_candidate(plan) is None
    assert plan["unsupported"]["reason"] == "semantic_period_unresolved"


def test_builder_does_not_validate_when_there_is_no_absence() -> None:
    plan = _plan_with(30, None)
    plan["target_user"]["aggregate_conditions"][0]["calendar_period"] = "last_month"
    assert graph_rag.build_aggregate_targets_sql_candidate(plan) is not None
