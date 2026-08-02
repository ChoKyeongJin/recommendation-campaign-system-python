"""이탈 위험 문형("N개월 주문은 있었지만 M일 구매가 없는")의 회귀 고정.

2026-08-02 실측: 같은 프롬프트를 5회 돌려 4회가 `semantic_ir_unsupported` 로 막혔고, 통과한 1회는
**LLM 이 노드를 더 못 만든 덕분**이었다(타입 확정 단계에서 폐기 → 결정론 합성이 통과). 즉 판정이
뒤집혀 있었다. 원인은 하나가 아니라 넷이었고, 각각을 따로 고정한다:

  C1 존재 표현의 착지 슬롯 부재 — `order_count > 0` 이 임계 슬롯의 양수 도메인에 걸렸다.
     → 카운트 대수(존재/부재/항진식/모순)와 존재 슬롯으로 환원.
  C2 결정론 구제가 게이트 뒤에 있었다 → 순서는 tests/test_deterministic_rescue_ordering.py 소관.
  C3 절대 기간이 무음 드롭(set-then-pop no-op) → 후행 창은 환산, 과거 달력 구간은 fail-close.
  C4 컨테이너 노드가 자식이 덮지 않는 구간까지 커버로 청구 → 커버리지 거짓말.

여기서 특히 **결정론 구제 없이 노드 경로만으로** 통과하는지를 따로 단언한다. 구제가 가려 주면
노드 경로의 회귀가 보이지 않고, 그러면 정규식이 못 잡는 다른 문형에서 같은 사고가 되풀이된다.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import legacy_plan_compiler  # noqa: E402
import semantic_pipeline  # noqa: E402
import semantic_plan  # noqa: E402
import semantic_retype  # noqa: E402
import semantic_plan_bridge  # noqa: E402
from semantic_normalizers import (  # noqa: E402
    COUNT_ABSENCE,
    COUNT_CONTRADICTION,
    COUNT_EXISTENCE,
    COUNT_TAUTOLOGY,
    COUNT_THRESHOLD,
    CountThresholdNormalizer,
)

_PROMPT = "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원을 추출해서 이탈방지 캠페인을 만들어줘."


def _context():
    return graph_rag._semantic_compile_context()


def _compile(nodes: list[dict], query: str = _PROMPT):
    plan = semantic_plan.plan_from_dict({"nodes": nodes}, source_query=query)
    evaluation = semantic_pipeline.evaluate(
        query, plan, context=_context(),
        compiler=legacy_plan_compiler.LegacyQueryPlanCompiler(),
        slot_catalog=legacy_plan_compiler.NODE_SLOT_MAP,
    )
    return plan, evaluation


def _aggregate_node(node_id, operator, value, *, period=None, span="", start=0, end=0, metric="order_count"):
    node = {
        "id": node_id, "type": "aggregate_predicate", "scope": "order", "metric": metric,
        "operator": operator, "value": value,
        "source_span": span, "source_start": start, "source_end": end,
    }
    if period is not None:
        node["period"] = period
    return node


# ── C1. 카운트 대수 ────────────────────────────────────────────────────────────────
def test_count_algebra_separates_presence_from_threshold() -> None:
    """'있음/없음'과 'N건 이상'은 표현이 아니라 값으로 갈린다 — 정수 카운트 위의 순수 대수."""
    classify = CountThresholdNormalizer.classify
    assert classify(">", 0) == COUNT_EXISTENCE
    assert classify(">=", 1) == COUNT_EXISTENCE
    assert classify(">", 0.5) == COUNT_EXISTENCE, "정수 카운트에서 '>0.5' 는 '>=1' 과 같다"
    assert classify("=", 0) == COUNT_ABSENCE
    assert classify("<", 1) == COUNT_ABSENCE
    assert classify("<=", 0) == COUNT_ABSENCE
    assert classify(">=", 0) == COUNT_TAUTOLOGY, "'0건 이상'은 조건이 아니라 전원이다"
    assert classify("<", 0) == COUNT_CONTRADICTION
    assert classify(">=", 2) == COUNT_THRESHOLD
    assert classify(">", 1) == COUNT_THRESHOLD


def test_existence_node_lands_in_the_membership_slot_not_a_threshold() -> None:
    """C1 핵심: `order_count > 0` 은 존재 조건이다. 임계 슬롯으로 보내면 요청 전체가 막힌다."""
    _plan, evaluation = _compile([
        _aggregate_node("req-2", ">", {"amount": 0, "currency": "KRW"},
                        period={"value": 3, "unit": "months"},
                        span="최근 3개월 주문은 있었지만", start=0, end=13),
    ])
    assert evaluation.compiled.failures == []
    target_user = evaluation.compiled.containers["target_user"]
    assert target_user["purchase_membership"] == {"operator": "exists", "window_days": 90}
    assert "aggregate_conditions" not in target_user


def test_absence_node_with_a_window_lands_in_the_inactivity_slot() -> None:
    """`order_count = 0` + 창 = '최근 N일 미구매'. 임계 비교로 컴파일하면 극성이 뒤집힌다."""
    _plan, evaluation = _compile([
        _aggregate_node("req-1", "=", 0, period={"value": 30, "unit": "days"},
                        span="최근 30일간 구매가 없는", start=14, end=27),
    ])
    assert evaluation.compiled.failures == []
    assert evaluation.compiled.containers["target_user"]["purchase_inactivity"]["min_days"] == 30


def test_both_clauses_compile_from_nodes_alone_without_the_deterministic_rescue() -> None:
    """**노드 경로만으로** 이탈 문형 전체가 컴파일된다(결정론 구제를 끄고 확인)."""
    _plan, evaluation = _compile([
        _aggregate_node("req-2", ">", 0, period={"value": 3, "unit": "months"},
                        span="최근 3개월 주문은 있었지만", start=0, end=13),
        _aggregate_node("req-1", "=", 0, period={"value": 30, "unit": "days"},
                        span="최근 30일간 구매가 없는", start=14, end=27),
    ])
    target_user = evaluation.compiled.containers["target_user"]
    assert target_user["purchase_membership"] == {"operator": "exists", "window_days": 90}
    assert target_user["purchase_inactivity"]["min_days"] == 30
    assert evaluation.compiled.failures == []


def test_threshold_path_is_unchanged_for_real_thresholds() -> None:
    """환원은 0 계열에만 붙는다 — 진짜 임계값은 기존 경로 그대로여야 한다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">=", 5, period={"value": 6, "unit": "months"},
                         span="최근 6개월 5건 이상", start=0, end=11)],
        query="최근 6개월 5건 이상 구매한 회원",
    )
    assert evaluation.compiled.containers["target_user"]["aggregate_conditions"] == [
        {"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 180}
    ]


def test_tautology_and_contradiction_are_named_not_compiled() -> None:
    """'0건 이상'은 조건이 아니다 — 조건인 척 SQL 에 들어가면 전 회원이 대상이 된다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">=", 0, span="주문 0건 이상", start=0, end=8)],
        query="주문 0건 이상인 회원",
    )
    assert evaluation.compiled.is_empty
    reasons = " ".join(failure["reason"] for failure in evaluation.compiled.failures)
    assert "항진식" in reasons, reasons


def test_sum_metrics_do_not_get_the_presence_reduction() -> None:
    """존재/부재 환원은 정수 카운트 위에서만 성립한다 — 합계 지표는 임계 경로에 남는다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">", 0, metric="purchase_amount",
                         span="구매금액 0원 초과", start=0, end=10)],
        query="구매금액 0원 초과인 회원",
    )
    assert "purchase_membership" not in evaluation.compiled.containers.get("target_user", {})


# ── C0. 거부 사유의 정직성 ─────────────────────────────────────────────────────────
def test_rejection_reason_names_the_value_domain_not_the_vocabulary() -> None:
    """어휘에 **있는** 지표를 두고 '어휘에 없다'고 말하지 않는다(운영자를 엉뚱한 카탈로그로 보낸다)."""
    import targeting_ir

    allowed = graph_rag._llm_slot_allowed()["aggregate_metrics"]
    assert "order_count" in allowed, "전제: 이 지표는 실행 어휘에 있다"
    reason = targeting_ir.slot_rejection_reason(
        "aggregate_conditions", {"metric_id": "order_count", "operator": ">", "threshold": 0},
        allowed=allowed,
    )
    assert reason and "어휘" not in reason, reason
    assert "임계값" in reason, reason


# ── C3. 기간 ───────────────────────────────────────────────────────────────────────
def test_trailing_absolute_period_becomes_a_rolling_window() -> None:
    """LLM 이 상대 창을 날짜로 환산해 보내도 오늘로 끝나는 후행 창이면 정확히 되돌린다."""
    today = date.today()
    start = today - timedelta(days=89)
    _plan, evaluation = _compile([
        _aggregate_node("req-2", ">", 0,
                        period={"from": start.strftime("%Y%m%d"), "to": today.strftime("%Y%m%d")},
                        span="최근 3개월 주문은 있었지만", start=0, end=13),
    ])
    assert evaluation.compiled.containers["target_user"]["purchase_membership"]["window_days"] == 90


def test_past_calendar_period_fails_closed_instead_of_silently_becoming_lifetime() -> None:
    """C3: 창이 사라진 집계는 '평생'이 되어 원문과 다른 대상을 낸다 — 조용히 넘기지 않는다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">=", 5,
                         period={"from": "20200501", "to": "20200531", "label": "2020년 5월"},
                         span="2020년 5월 5건 이상", start=0, end=13)],
        query="2020년 5월 5건 이상 구매한 회원",
    )
    assert evaluation.compiled.is_empty, "창이 확정되지 않았는데 조건이 만들어졌다"
    reasons = " ".join(failure["reason"] for failure in evaluation.compiled.failures)
    assert "2020년 5월" in reasons and "지원하지 않는다" in reasons, reasons


# ── C4. 컨테이너 커버리지 ───────────────────────────────────────────────────────────
def test_container_node_cannot_claim_coverage_its_children_do_not_provide() -> None:
    """자식 하나짜리 logical_expression 이 두 절 전체를 청구하면 나머지 절은 **미커버**여야 한다."""
    import semantic_coverage

    child = _aggregate_node("req-1-1", ">", 0, period={"value": 3, "unit": "months"},
                            span="최근 3개월 주문은 있었지만", start=0, end=13)
    container = {
        "id": "req-1", "type": "logical_expression", "operator": "and", "children": [child],
        "source_span": "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원",
        "source_start": 0, "source_end": 32,
    }
    plan = semantic_plan.plan_from_dict({"nodes": [container]}, source_query=_PROMPT)
    report = semantic_coverage.verify_coverage(_PROMPT, plan)
    uncovered = " ".join(item["source_span"] for item in report.uncovered_requirements)
    assert "30일" in uncovered, (
        "컨테이너가 자식이 덮지 않는 구간까지 커버로 청구했다 — 표현되지 않은 절이 "
        f"조용히 사라진다. uncovered={report.uncovered_requirements}"
    )


# ── C4. 타입 재확정 ────────────────────────────────────────────────────────────────
def test_retype_recovers_the_discriminator_that_is_also_a_required_field() -> None:
    """metric 하나로 scope 가 확정되면 코드가 정한다 — 사람에게 되묻지 않는다."""
    context = _context()
    nodes, outcomes = semantic_retype.normalize_payloads(
        [{
            "id": "req-2", "type": "predicate", "metric": "order_count", "operator": ">",
            "value": {"value": 1, "unit": "count"},
            "period": {"type": "relative", "value": 3, "unit": "months"},
            "source_span": "최근 3개월 주문은 있었지만", "source_start": 0, "source_end": 13,
        }],
        vocabularies=context.node_vocabularies,
        resolve=semantic_plan_bridge._canonical_forms(context),
    )
    assert [outcome.resolved_type for outcome in outcomes] == ["aggregate_predicate"]
    assert nodes[0]["scope"] == "order"


# ── C2. 소유권 소거 ────────────────────────────────────────────────────────────────
def test_slot_owned_span_supersedes_a_compile_failure() -> None:
    """같은 구간의 의미가 이미 실행 플랜에 있으면, 그것을 다시 만든 노드는 실패가 아니라 중복이다."""
    query = "최근 30일간 구매가 없는 회원"
    plan = semantic_plan.plan_from_dict(
        {"nodes": [_aggregate_node("n", ">=", 5, span="최근 30일간 구매가 없는", start=0, end=13)]},
        source_query=query,
    )
    plan.capability_verdicts.append(
        {"node_id": "n", "node_type": "compiler", "failure_code": "unsupported_semantics",
         "message": "집계 지표 'order_count' 는 실행 어휘에 없다"}
    )
    assert plan.status() == semantic_plan.STATUS_UNSUPPORTED
    superseded = semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=query, claimed=[(0, 13)]
    )
    assert superseded and superseded[0]["superseded_by"] == "slot_claim"
    assert plan.capability_verdicts == []
    assert plan.status() != semantic_plan.STATUS_UNSUPPORTED


def test_unclaimed_span_keeps_its_compile_failure() -> None:
    """반대 방향: 슬롯이 안 덮은 구간의 실패는 그대로 실패다(소거가 만능 우회로가 되면 안 된다)."""
    query = "최근 30일간 구매가 없는 회원"
    plan = semantic_plan.plan_from_dict(
        {"nodes": [_aggregate_node("n", ">=", 5, span="최근 30일간 구매가 없는", start=0, end=13)]},
        source_query=query,
    )
    verdict = {"node_id": "n", "node_type": "compiler", "failure_code": "unsupported_semantics",
               "message": "무언가 안 된다"}
    plan.capability_verdicts.append(verdict)
    assert semantic_plan_bridge.supersede_slot_owned_failures(plan, query=query, claimed=[]) == []
    assert plan.capability_verdicts == [verdict]


def test_only_metrics_that_are_zero_when_absent_get_the_presence_reduction() -> None:
    """부분 차원 카운트는 환원 대상이 아니다 — 브랜드가 빈 주문만 가진 회원은 구매를 **했는데**
    distinct_brand_count 는 0 이다. 환원했다면 틀린 대상이 나간다."""
    reducible = _context().count_metrics.get("aggregate") or frozenset()
    assert "order_count" in reducible
    for metric in ("distinct_brand_count", "distinct_category_count", "distinct_product_count"):
        assert metric not in reducible, f"{metric} 는 행 부재 시 0 이 아니므로 환원하면 안 된다"

    _plan, evaluation = _compile(
        [_aggregate_node("n", "=", 0, metric="distinct_brand_count",
                         period={"value": 30, "unit": "days"},
                         span="최근 30일간 브랜드 0종", start=0, end=13)],
        query="최근 30일간 브랜드 0종 구매한 회원",
    )
    assert "purchase_inactivity" not in evaluation.compiled.containers.get("target_user", {})


def test_atomless_span_cannot_be_proven_duplicate() -> None:
    """값 원자가 없는 구간은 '중복'으로 증명되지 않는다 — all([]) 이 참이라고 소거하면
    '이 연산은 지원하지 않는다'가 근거 없이 침묵으로 바뀐다(fail-open)."""
    query = "브랜드 재구매 캠페인 대상 회원"
    plan = semantic_plan.plan_from_dict(
        {"nodes": [{
            "id": "n", "type": "aggregate_predicate", "scope": "order", "metric": "order_count",
            "operator": ">=", "value": 2,
            "source_span": "브랜드 재구매", "source_start": 0, "source_end": 6,
        }]},
        source_query=query,
    )
    verdict = {"node_id": "n", "node_type": "compiler", "failure_code": "unsupported_semantics",
               "message": "브랜드 한정 집계는 지원하지 않는다"}
    plan.capability_verdicts.append(verdict)
    assert semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=query, claimed=[(0, 6)]
    ) == []
    assert plan.capability_verdicts == [verdict]


def test_rescue_claim_flows_through_the_bridge_and_clears_the_gate() -> None:
    """C2 종단: 결정론 구제가 채운 슬롯의 구간 청구가 파이프라인 소유 판정의 입력이 되어,
    같은 어구를 다시 방출한 노드의 컴파일 실패가 요청을 막지 못한다.

    직접 호출(supersede_slot_owned_failures) 이 아니라 실제 진입점(semantic_plan_bridge.apply)으로
    확인한다 — 이 경로의 결함은 순서와 배선에 있었지 함수 본문에 있지 않았다.
    """
    import behavior_demotion

    query = _PROMPT
    query_plan: dict = {
        "target_user": {"behaviors": ["no_purchase"]},
        # 구조화기가 존재 절을 '평균 구매주기' 로 환각한 실측 모양(컴파일 실패로 귀결된다).
        "semantic_plan": {"nodes": [{
            "id": "req-1", "type": "predicate", "metric": "buy_cycle", "subject": "member",
            "operator": "<", "value": {"amount": 0, "currency": "KRW"},
            "source_span": "최근 3개월 주문은 있었지만", "source_start": 0, "source_end": 13,
        }]},
    }
    behavior_demotion.normalize_lapsed_purchase_pattern(query_plan, source_text=query)
    assert query_plan["target_user"]["purchase_membership"]["window_days"] == 90

    result = semantic_plan_bridge.apply(query_plan, query, context=_context())

    assert query_plan["semantic_ir"]["status"] != "unsupported", (
        "구제가 채운 슬롯이 같은 구간의 컴파일 실패를 소거하지 못했다: "
        f"{query_plan['semantic_ir'].get('message')}"
    )
    assert result.status != semantic_plan.STATUS_UNSUPPORTED
    claimed = query_plan[semantic_plan_bridge.PIPELINE_KEY]["claimed_spans"]
    assert claimed, "결정론 구제의 슬롯 구간이 청구 목록에 없다"
