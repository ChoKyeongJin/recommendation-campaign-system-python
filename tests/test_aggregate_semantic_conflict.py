"""존재/부재 의미 충돌 검증 — 모순된 SQL 은 조립되기 전에 막는다.

여기서 지키는 판정 규칙은 하나다: **기간이 겹치면 충돌이 아니라, 존재가 부재에 통째로 포함될 때만
충돌이다.** 겹침을 충돌로 보면 '최근 6개월 구매 있고 최근 1개월 구매 없는 고객'(2~6개월 전 구매자)
같은 정상 조건이 막힌다.
"""

from __future__ import annotations

import json
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


# ── 6. 사건 논리식 트랙(EXISTS/NOT EXISTS 쌍)도 같은 판정기를 통과한다 ─────────────────────
# 이 문장들은 집계 임계값이 없어 event_expression 빌더가 소유한다 — 극성별 창을 그대로 보존하는
# 빌더라서, 판정이 없으면 정의상 공집합인 EXISTS/NOT EXISTS 쌍이 문법적으로 멀쩡한 SQL 로 나간다.
CONTRADICTORY_QUERIES = [
    "최근 1개월 구매 있고 최근 6개월 구매 없는 고객",
    "최근 3개월 구매 있고 최근 3개월 구매 없는 고객",
    "최근 1개월 온라인 구매 있고 최근 1개월 모든 구매 없는 고객",
]

SATISFIABLE_QUERIES = [
    "최근 6개월 구매 있고 최근 1개월 구매 없는 고객",
    "4~6개월 전 구매 있고 최근 1개월 구매 없는 고객",
    "최근 1개월 전체 구매 있고 최근 1개월 취소 구매 없는 고객",
]


@pytest.mark.parametrize("query", CONTRADICTORY_QUERIES)
def test_contradictory_event_expressions_produce_no_sql(query: str) -> None:
    plan = graph_rag.build_query_plan(query, parser="rules")
    assert graph_rag.build_sql_template_candidate(plan) is None, query
    assert plan["unsupported"]["reason"] == "presence_absence_conflict"


@pytest.mark.parametrize("query", SATISFIABLE_QUERIES)
def test_satisfiable_event_expressions_still_compile(query: str) -> None:
    plan = graph_rag.build_query_plan(query, parser="rules")
    assert graph_rag.build_sql_template_candidate(plan) is not None, query
    assert plan.get("unsupported") is None, query


def test_channel_words_are_not_yet_part_of_the_purchase_event_ir() -> None:
    """알려진 한계(이 작업 범위 밖): 구매 사건 IR 에 채널 차원이 없다.

    '온라인 구매 있고 오프라인 구매 없는' 은 파서가 두 원자를 **같은** purchase/30일로 만든다 —
    즉 IR 자체가 '같은 구매 있음 AND 없음'이다. 검증기는 받은 IR 대로 판정하므로 차단이 맞다
    (예전엔 정의상 빈 결과 SQL 이 조용히 나갔다). 채널 구분은 사건 소스에 채널 차원을 도입해야
    풀리는 별개 과제이고, 부분집합 판정 자체는 위 constraints 단위 테스트가 이미 보장한다."""
    plan = graph_rag.build_query_plan(
        "최근 1개월 온라인 구매 있고 최근 1개월 오프라인 구매 없는 고객", parser="rules",
    )
    atoms = plan["event_expression"]["expression"]["operands"]
    windows = {
        json.dumps(atom.get("relation", atom.get("operand", {}).get("relation", {})), sort_keys=True)
        for atom in atoms
    }
    assert len(windows) == 1, "채널이 IR 에 반영됐다면 이 한계 테스트를 실제 통과 케이스로 옮겨야 한다"
    assert graph_rag.build_sql_template_candidate(plan) is None
    assert plan["unsupported"]["reason"] == "presence_absence_conflict"


def test_the_reported_query_never_emits_both_an_aggregate_join_and_an_anti_join() -> None:
    """원본 재현 쿼리: 집계 INNER JOIN 과 미구매 NOT EXISTS 가 한 SQL 에 함께 나오지 않는다."""
    plan = graph_rag.build_query_plan(
        "인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는 사람 뽑아줘", parser="rules",
    )
    candidate = graph_rag.build_sql_template_candidate(plan)
    assert candidate is None
    assert plan["unsupported"]["reason"] == "unsupported_threshold_attribute"
