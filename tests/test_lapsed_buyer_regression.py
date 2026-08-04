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
_FIXED_TODAY = date(2026, 3, 31)


def _context():
    return graph_rag._semantic_compile_context(_FIXED_TODAY)


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
    assert target_user["purchase_membership"] == {
        "operator": "exists",
        "window": {
            "type": "relative",
            "value": 3,
            "unit": "months",
            "from": "20260101",
            "to": "20260331",
        },
    }
    assert "aggregate_conditions" not in target_user


def test_absence_node_with_a_window_lands_in_the_inactivity_slot() -> None:
    """`order_count = 0` + 창 = '최근 N일 미구매'. 임계 비교로 컴파일하면 극성이 뒤집힌다."""
    _plan, evaluation = _compile([
        _aggregate_node("req-1", "=", 0, period={"value": 30, "unit": "days"},
                        span="최근 30일간 구매가 없는", start=14, end=27),
    ])
    assert evaluation.compiled.failures == []
    assert evaluation.compiled.containers["target_user"]["purchase_inactivity"] == {
        "window": {
            "type": "relative",
            "value": 30,
            "unit": "days",
            "from": "20260302",
            "to": "20260331",
        }
    }


def test_both_clauses_compile_from_nodes_alone_without_the_deterministic_rescue() -> None:
    """**노드 경로만으로** 이탈 문형 전체가 컴파일된다(결정론 구제를 끄고 확인)."""
    _plan, evaluation = _compile([
        _aggregate_node("req-2", ">", 0, period={"value": 3, "unit": "months"},
                        span="최근 3개월 주문은 있었지만", start=0, end=13),
        _aggregate_node("req-1", "=", 0, period={"value": 30, "unit": "days"},
                        span="최근 30일간 구매가 없는", start=14, end=27),
    ])
    target_user = evaluation.compiled.containers["target_user"]
    assert target_user["purchase_membership"]["window"]["from"] == "20260101"
    assert target_user["purchase_membership"]["window"]["to"] == "20260331"
    assert target_user["purchase_inactivity"]["window"]["from"] == "20260302"
    assert target_user["purchase_inactivity"]["window"]["to"] == "20260331"
    assert evaluation.compiled.failures == []


def test_threshold_path_is_unchanged_for_real_thresholds() -> None:
    """환원은 0 계열에만 붙는다 — 진짜 임계값은 기존 경로 그대로여야 한다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">=", 5, period={"value": 6, "unit": "months"},
                         span="최근 6개월 5건 이상", start=0, end=11)],
        query="최근 6개월 5건 이상 구매한 회원",
    )
    assert evaluation.compiled.containers["target_user"]["aggregate_conditions"] == [{
        "metric_id": "order_count",
        "operator": ">=",
        "threshold": 5,
        "window": {
            "type": "relative",
            "value": 6,
            "unit": "months",
            "from": "20251001",
            "to": "20260331",
        },
    }]


def test_calendar_window_reaches_sql_without_database_clock_reanchoring() -> None:
    _plan, evaluation = _compile(
        [_aggregate_node(
            "n",
            ">=",
            5,
            period={"value": 1, "unit": "months"},
            span="최근 1개월 5건 이상",
            start=0,
            end=12,
        )],
        query="최근 1개월 5건 이상 구매한 회원",
    )
    query_plan = {
        "intent": "find_user_segment",
        "target_user": evaluation.compiled.containers["target_user"],
    }

    candidate = graph_rag.build_aggregate_targets_sql_candidate(query_plan)

    assert candidate is not None
    assert "ORDER_DATE BETWEEN '20260301' AND '20260331'" in candidate["sql"]
    assert "GETDATE" not in candidate["sql"].upper()


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
    today = _FIXED_TODAY
    start = today - timedelta(days=89)
    _plan, evaluation = _compile([
        _aggregate_node("req-2", ">", 0,
                        period={"from": start.strftime("%Y%m%d"), "to": today.strftime("%Y%m%d")},
                        span="최근 3개월 주문은 있었지만", start=0, end=13),
    ])
    assert evaluation.compiled.containers["target_user"]["purchase_membership"]["window"] == {
        "type": "absolute",
        "from": start.strftime("%Y%m%d"),
        "to": today.strftime("%Y%m%d"),
    }


def test_past_calendar_period_is_preserved_instead_of_becoming_lifetime() -> None:
    """C3: 닫힌 과거 구간은 평생 조건으로 축소되지 않고 그대로 보존된다."""
    _plan, evaluation = _compile(
        [_aggregate_node("n", ">=", 5,
                         period={"from": "20200501", "to": "20200531", "label": "2020년 5월"},
                         span="2020년 5월 5건 이상", start=0, end=13)],
        query="2020년 5월 5건 이상 구매한 회원",
    )
    assert evaluation.compiled.failures == []
    assert evaluation.compiled.containers["target_user"]["aggregate_conditions"] == [{
        "metric_id": "order_count",
        "operator": ">=",
        "threshold": 5,
        "window": {
            "type": "absolute",
            "from": "20200501",
            "to": "20200531",
        },
    }]


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
    behavior_demotion.normalize_lapsed_purchase_pattern(
        query_plan, source_text=query, reference_date=_FIXED_TODAY
    )
    assert query_plan["target_user"]["purchase_membership"]["window"] == {
        "type": "relative",
        "value": 3,
        "unit": "months",
        "from": "20260101",
        "to": "20260331",
    }

    result = semantic_plan_bridge.apply(query_plan, query, context=_context())

    assert query_plan["semantic_ir"]["status"] != "unsupported", (
        "구제가 채운 슬롯이 같은 구간의 컴파일 실패를 소거하지 못했다: "
        f"{query_plan['semantic_ir'].get('message')}"
    )
    assert result.status != semantic_plan.STATUS_UNSUPPORTED
    claimed = query_plan[semantic_plan_bridge.PIPELINE_KEY]["claimed_spans"]
    assert claimed, "결정론 구제의 슬롯 구간이 청구 목록에 없다"


# ── 적대적 리뷰에서 확인된 결함의 회귀 고정(2026-08-02) ──────────────────────────────


def test_classify_rejects_fuzzy_operators_instead_of_inverting_polarity() -> None:
    """'!=' 안에서 별칭 '=' 를 찾아내던 관대함이 `order_count != 0`(주문 있음)을 부재로 뒤집었다.

    분류가 곧 슬롯 선택이므로, 모르는 표기는 관대하게 넘기지 말고 **환원하지 않는다**.
    """
    for surface in ("!=", "<>", "≠", "not", ""):
        assert CountThresholdNormalizer.classify(surface, 0) == COUNT_THRESHOLD, surface
    # 정상 표기는 그대로 판정된다.
    assert CountThresholdNormalizer.classify("=", 0) == COUNT_ABSENCE
    assert CountThresholdNormalizer.classify("eq", 0) == COUNT_ABSENCE
    assert CountThresholdNormalizer.classify("이상", 1) == COUNT_EXISTENCE


def test_classify_survives_non_finite_thresholds() -> None:
    """inf 는 floor/ceil 에서 OverflowError 를 던지는데, 그건 노드별 예외 처리를 빠져나가
    파이프라인 전체를 죽이고 형제 노드가 만든 슬롯까지 함께 버린다."""
    for value in (float("inf"), float("-inf"), float("nan")):
        assert CountThresholdNormalizer.classify(">", value) == COUNT_THRESHOLD


def test_existence_reduction_never_drops_a_narrowing_qualifier() -> None:
    """'나이키를 1번 이상 구매' 를 존재로 환원하면 브랜드가 사라져 **대상이 조용히 넓어진다**.
    한정어가 붙은 노드는 임계 경로에 남아야 한다(>=1 은 양수라 그대로 컴파일된다)."""
    query = "최근 3개월 동안 나이키 브랜드를 1번 이상 구매한 회원"
    _plan, evaluation = _compile(
        [{
            "id": "n", "type": "aggregate_predicate", "scope": "order", "metric": "order_count",
            "operator": ">=", "value": 1, "period": {"value": 3, "unit": "months"},
            "qualifier": "나이키",
            "source_span": "나이키 브랜드를 1번 이상 구매", "source_start": 9, "source_end": 26,
        }],
        query=query,
    )
    target_user = evaluation.compiled.containers["target_user"]
    assert "purchase_membership" not in target_user, "브랜드 한정어가 사라졌다 — 대상이 넓어진다"
    assert target_user["aggregate_conditions"] == [
        {"metric_id": "order_count", "operator": ">=", "threshold": 1,
         "window": {
             "type": "relative", "value": 3, "unit": "months",
             "from": "20260101", "to": "20260331",
         }, "scope": {"brand": "나이키"}}
    ]


def test_non_member_grain_is_not_reduced_either() -> None:
    """per_brand grain 도 존재 슬롯이 담을 수 없다 — '브랜드마다 1번 이상' ≠ '구매한 적 있음'."""
    _plan, evaluation = _compile(
        [{
            "id": "n", "type": "aggregate_predicate", "scope": "order", "metric": "order_count",
            "operator": ">=", "value": 1, "grain": "per_brand",
            "source_span": "브랜드마다 1번 이상", "source_start": 0, "source_end": 10,
        }],
        query="브랜드마다 1번 이상 구매한 회원",
    )
    assert "purchase_membership" not in evaluation.compiled.containers.get("target_user", {})


def test_two_existence_windows_conflict_instead_of_silently_overwriting() -> None:
    """존재 슬롯은 값이 하나뿐이다. 덮어쓰면 **노드 순서가 곧 대상**이 되고 근거가 남지 않는다."""
    query = "최근 3개월 이내 구매 이력이 있고 최근 7일 이내에도 주문한 회원"
    _plan, evaluation = _compile(
        [
            _aggregate_node("a", ">=", 1, period={"value": 3, "unit": "months"},
                            span="최근 3개월 이내 구매 이력이 있고", start=0, end=17),
            _aggregate_node("b", ">=", 1, period={"value": 7, "unit": "days"},
                            span="최근 7일 이내에도 주문한", start=18, end=31),
        ],
        query=query,
    )
    reasons = " ".join(failure["reason"] for failure in evaluation.compiled.failures)
    assert "둘 있다" in reasons, f"충돌이 보고되지 않았다: {evaluation.compiled.to_dict()}"


def test_lapsed_claim_does_not_swallow_an_interposed_condition() -> None:
    """정규식의 `.{0,24}?` 사이에 무관한 조건이 낄 수 있다 — 매치 전체를 청구하면 그 조건이
    '이미 표현됨'으로 처리돼 조용히 사라진다. 청구는 **각 절**의 구간이어야 한다."""
    import behavior_demotion
    import slot_ownership

    query = "최근 3개월 주문은 있었지만 서울에 사는 30대 중 최근 30일간 구매가 없는 회원을 추출해줘"
    plan: dict = {}
    assert behavior_demotion.normalize_lapsed_purchase_pattern(
        plan, source_text=query, reference_date=_FIXED_TODAY
    )
    for slot in ("purchase_membership", "purchase_inactivity"):
        text = slot_ownership.slot_span(plan, slot)["text"]
        assert "서울" not in text and "30대" not in text, f"{slot} 청구가 남의 절을 삼켰다: {text!r}"


def test_a_node_claims_as_many_period_anchors_as_it_owns() -> None:
    """기간을 둘 소유하는 노드(기간 대 기간 비교의 baseline·current)가 자기 기간 하나를
    남의 것처럼 미커버로 흘리면 안 된다 — 1:1 고정은 그 노드를 통째로 막았다."""
    import semantic_coverage

    query = "2019년 2월 대비 2019년 3월 구매금액이 증가한 회원"
    start = query.find("구매금액이 증가한")
    plan = semantic_plan.plan_from_dict(
        {"nodes": [{
            "id": "n", "type": "metric_comparison", "metric": "purchase_amount", "relation": "increase",
            "baseline": {"from": "20190201", "to": "20190228"},
            "current": {"from": "20190301", "to": "20190331"},
            "source_span": "구매금액이 증가한", "source_start": start, "source_end": start + 9,
        }]},
        source_query=query,
    )
    assert semantic_coverage.verify_coverage(query, plan).uncovered_requirements == []


def test_a_single_period_node_still_claims_only_one_anchor() -> None:
    """반대 방향: 기간 하나짜리 노드가 절 안의 다른 기간까지 덮으면 표현되지 않은 절이 사라진다."""
    import semantic_coverage

    plan = semantic_plan.plan_from_dict(
        {"nodes": [_aggregate_node("a", ">", 0, period={"value": 3, "unit": "months"},
                                   span="최근 3개월 주문은 있었지만", start=0, end=13)]},
        source_query=_PROMPT,
    )
    uncovered = " ".join(
        item["source_span"] for item in semantic_coverage.verify_coverage(_PROMPT, plan).uncovered_requirements
    )
    assert "30일" in uncovered, "표현되지 않은 두 번째 기간이 조용히 커버로 처리됐다"
