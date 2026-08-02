"""광고 프롬프트 14종 슬롯 커버리지 감사(2026-08-01)에서 확정된 gap 6종의 회귀 가드.

공통 형태는 '컴파일러·물리 컬럼은 완비인데 생산 경로(스키마 노출→coerce→plan)가 끊김'이었다.
여기서는 그 생산 경로가 열려 있고 의미(AND 다중 조건·합계 vs 평균·퍼센트 랭킹·프로필 지표·
동시구매 카운트)가 보존되는지 고정한다.
  - P5: cart_aggregate 다중 조건 리스트('종류 2개 이상이고 총금액 10만 원 이상')
  - P7: campaign_buy_amount agg=AVG('캠페인별 구매반응 금액이 평균 10만 원 이상')
  - P4: member_metric_ranking percent('누적 구매금액 상위 10%')
  - P8: balance/profile_date 프로필 지표 슬롯('평균 구매주기 30일 이내', '다음 구매예정일이 지난')
  - P12: condition_evaluations 결정론 백필('같은 상품을 동시 구매한 고객수')
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import metric_registry  # noqa: E402
import targeting_ir  # noqa: E402


# ── P5: cart_aggregate 다중 조건 ─────────────────────────────────────────────────────────


def test_cart_aggregate_list_coerces_and_compiles_both_conditions() -> None:
    allowed = graph_rag._llm_slot_allowed()["cart_aggregate_metrics"]
    coerced = targeting_ir.SLOT_SHAPES["cart_aggregate"].coerce(
        [
            {"metric": "cart_line_count", "operator": "이상", "threshold": 2},
            {"metric": "cart_amount", "operator": ">=", "threshold": 100000},
        ],
        allowed=allowed,
    )
    assert isinstance(coerced, list) and len(coerced) == 2

    candidate = graph_rag.build_cart_aggregate_targets_sql_candidate(
        {"target_user": {"cart_aggregate": coerced}}
    )
    assert candidate is not None
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 2" in candidate["sql"]
    assert "SUM(TOTAL_SALE_PRICE) >= 100000" in candidate["sql"]


def test_cart_aggregate_single_dict_shape_is_preserved() -> None:
    allowed = graph_rag._llm_slot_allowed()["cart_aggregate_metrics"]
    coerced = targeting_ir.SLOT_SHAPES["cart_aggregate"].coerce(
        {"metric": "cart_quantity", "operator": ">=", "threshold": 10}, allowed=allowed
    )
    assert coerced == {"metric": "cart_quantity", "operator": ">=", "threshold": 10}


def test_cart_aggregate_list_extracts_one_ir_node_per_condition() -> None:
    plan = {
        "target_user": {
            "cart_aggregate": [
                {"metric": "cart_line_count", "operator": ">=", "threshold": 2},
                {"metric": "cart_amount", "operator": ">=", "threshold": 100000},
            ]
        }
    }
    kinds = [condition.kind for condition in targeting_ir.extract_target_conditions(plan)]
    assert kinds.count("cart_aggregate") == 2


# ── P7: campaign_buy_amount 집계함수(캠페인당 평균) ──────────────────────────────────────


def test_campaign_buy_amount_avg_compiles_per_campaign_average() -> None:
    coerced = targeting_ir.SLOT_SHAPES["campaign_buy_amount"].coerce(
        {"operator": "이상", "amount": 100000, "agg": "avg"}
    )
    assert coerced is not None and coerced["agg"] == "AVG"

    candidate = graph_rag.build_campaign_response_frequency_targets_sql_candidate(
        {"target_user": {"campaign_buy_amount": coerced}}
    )
    assert candidate is not None
    assert "* 1.0 / COUNT(DISTINCT" in candidate["sql"]
    assert "100000" in candidate["sql"]


def test_campaign_buy_amount_without_agg_stays_config_sum() -> None:
    candidate = graph_rag.build_campaign_response_frequency_targets_sql_candidate(
        {"target_user": {"campaign_buy_amount": {"operator": ">=", "amount": 100000}}}
    )
    assert candidate is not None
    assert "* 1.0 /" not in candidate["sql"]
    assert "SUM(" in candidate["sql"]


# ── P4: member_metric_ranking(상위 N%) ──────────────────────────────────────────────────


def test_member_metric_ranking_percent_produces_top_percent_sql() -> None:
    allowed = graph_rag._llm_slot_allowed()["member_metrics"]
    assert "total_buy_amt" in allowed

    coerced = targeting_ir.SLOT_SHAPES["member_metric_ranking"].coerce(
        {"metric_id": "total_buy_amt", "direction": "high", "limit_type": "percent", "percent": 10},
        allowed=allowed,
    )
    assert coerced == {
        "metric_id": "total_buy_amt",
        "direction": "high",
        "limit_type": "percent",
        "percent": 10,
    }

    candidate = graph_rag.build_member_metric_ranking_sql_candidate(
        {"member_metric_ranking": coerced, "target_user": {}}
    )
    assert candidate is not None
    assert "TOP 10 PERCENT" in candidate["sql"]
    assert "TOTAL_BUY_AMT" in candidate["sql"]


def test_member_metric_ranking_fails_closed_instead_of_degrading() -> None:
    allowed = graph_rag._llm_slot_allowed()["member_metrics"]
    shape = targeting_ir.SLOT_SHAPES["member_metric_ranking"]
    # 퍼센트 경계 밖 → 상위 100명으로 강등하지 않고 슬롯 미설정.
    assert shape.coerce({"metric_id": "total_buy_amt", "limit_type": "percent", "percent": 0}, allowed=allowed) is None
    assert shape.coerce({"metric_id": "total_buy_amt", "limit_type": "percent", "percent": 100}, allowed=allowed) is None
    # 레지스트리 밖 지표 → 환각 차단.
    assert shape.coerce({"metric_id": "fabricated_metric", "top_n": 10}, allowed=allowed) is None


def test_llm_candidate_cannot_smuggle_compiler_owned_plan_slots() -> None:
    """plan 컨테이너 슬롯을 LLM 후보가 실어 보내도 받지 않는다(2026-08-02 소유권 이행).

    과거에는 후보 최상위의 member_metric_ranking 을 그대로 읽어 플랜에 넣었다 — 지금 이
    슬롯의 생산자는 SemanticPlan RankedSet → LegacyQueryPlanCompiler 하나뿐이고,
    후보 경로는 방어선으로 한 번 더 막는다."""
    out = graph_rag._coerce_llm_structured_conditions(
        {
            "target_user": {},
            "member_metric_ranking": {"metric_id": "total_buy_amt", "limit_type": "percent", "percent": 10},
        }
    )
    assert "member_metric_ranking" not in out
    # plan 컨테이너 슬롯을 읽는 배선 자체는 살아 있다(컴파일러 소유가 아닌 슬롯으로 확인).
    passthrough = graph_rag._coerce_llm_structured_conditions(
        {"target_user": {}, "purchase_count_ranking": {"direction": "top", "top_n": 5}}
    )
    assert passthrough.get("purchase_count_ranking")


# ── P8: 회원 프로필 지표(구매주기/구매예정일) ───────────────────────────────────────────


def test_profile_vocab_derives_from_metric_spec_registry() -> None:
    metrics, date_states = metric_registry.profile_slot_vocab()
    assert "buy_cycle" in metrics
    source = metrics["buy_cycle"]["profile_source"]
    assert source["table"] == "CRM_MB_MONTHCRMINFO"
    assert source["column"] == "BUY_CYCLE"
    assert "grain_filter" in source

    assert "next_purchase_due_date" in date_states
    states = date_states["next_purchase_due_date"]["states"]
    assert states["past"]["operator"] == "<"
    assert states["not_due"]["operator"] == ">="


def test_buy_cycle_and_due_date_share_one_snapshot_exists() -> None:
    allowed = graph_rag._llm_slot_allowed()
    balance = targeting_ir.SLOT_SHAPES["balance_conditions"].coerce(
        [{"metric_id": "buy_cycle", "operator": "이하", "threshold": 30}],
        allowed=allowed["profile_metrics"],
    )
    dates = targeting_ir.SLOT_SHAPES["profile_date_conditions"].coerce(
        [{"metric_id": "next_purchase_due_date", "state": "past"}],
        allowed=allowed["profile_date_states"],
    )
    assert balance and dates

    compiled = graph_rag.compile_member_target_conditions(
        {"target_user": {"balance_conditions": balance, "profile_date_conditions": dates}}
    )
    snapshot_exists = [p for p in compiled["predicates"] if "CRM_MB_MONTHCRMINFO" in p]
    # BUY_CYCLE(수치)과 BUY_DUE_DATE(날짜 상태)는 같은 최신월 스냅샷 행에서 동시에 만족해야
    # 하므로 하나의 EXISTS 로 결합된다(graph_rag 프로필 source key 공유 설계).
    assert len(snapshot_exists) == 1
    predicate = snapshot_exists[0]
    assert "M.BUY_CYCLE <= 30" in predicate
    assert "M.BUY_DUE_DATE < CONVERT(char(8), GETDATE(), 112)" in predicate
    assert "MAX(YYYYMM)" in predicate


def test_profile_slots_reject_unregistered_metric_and_state() -> None:
    allowed = graph_rag._llm_slot_allowed()
    assert targeting_ir.SLOT_SHAPES["balance_conditions"].coerce(
        [{"metric_id": "fabricated", "operator": "<=", "threshold": 30}],
        allowed=allowed["profile_metrics"],
    ) is None
    assert targeting_ir.SLOT_SHAPES["profile_date_conditions"].coerce(
        [{"metric_id": "next_purchase_due_date", "state": "fabricated_state"}],
        allowed=allowed["profile_date_states"],
    ) is None


def test_allowed_canonical_values_advertise_profile_and_ranking_vocab() -> None:
    values = graph_rag._allowed_canonical_values()
    assert "buy_cycle" in values["profile_metric_id"]
    assert "next_purchase_due_date:past" in values["profile_date_state"]
    assert "total_buy_amt" in values["member_metric_id"]


# ── P12: 동시구매(고객수) 조건 판정 IR 백필 ─────────────────────────────────────────────


def _co_purchase_plan(query: str, span: str, period: dict | None = None) -> dict:
    """동시구매 RelationPredicate 노드를 실은 플랜(파이프라인 입력 형태)."""
    start = query.index(span)
    node: dict = {
        "id": "req-1", "type": "relation_predicate", "source_span": span,
        "source_start": start, "source_end": start + len(span),
        "subject": "member", "attribute": "product", "relation": "co_purchase",
    }
    if period is not None:
        node["period"] = period
    return {"target_user": {}, "semantic_plan": {"nodes": [node]}}


def test_co_purchase_node_compiles_to_valid_ir_with_window() -> None:
    """'2026년 3월 같은 상품을 동시 구매한 고객수'

    과거에는 `apply_same_product_co_purchase_backfill` 이 원문을 정규식으로 감지해
    condition_evaluations 를 채웠다. 이제 그 IR 은 SemanticPlan 노드의 컴파일 산출물이다 —
    빌더(검증된 capability 서명)와 검증 체인은 그대로다."""
    import semantic_plan_bridge

    query = "2026년 3월 같은 상품을 동시 구매한 고객수"
    plan = _co_purchase_plan(
        query, "같은 상품을 동시 구매한 고객수",
        {"type": "absolute", "from": "2026-03-01", "to": "2026-03-31"},
    )
    semantic_plan_bridge.apply(plan, query, context=graph_rag._semantic_compile_context())

    evaluations = plan.get(graph_rag.CONDITION_EVALUATIONS_KEY)
    assert isinstance(evaluations, list) and len(evaluations) == 1
    assert graph_rag.validate_condition_evaluations(evaluations) == []
    time_range = evaluations[0]["evaluation_scope"]["time_range"]
    assert (time_range["from"], time_range["to"]) == ("20260301", "20260331")


def test_co_purchase_ir_is_not_produced_without_a_node() -> None:
    """노드가 없으면 IR 도 없다 — 원문에 동시구매 어구가 있어도 마찬가지(fail-close).

    과거 백필의 '카운트 출력이 아니면 만들지 않는다' 가드가 여기로 이전됐다: 출력 형태를
    포함한 요구 해석 전체가 LLM 의 노드 방출에 달려 있고, 결정론 감지 경로는 없다."""
    import semantic_plan_bridge

    query = "2026년 3월 같은 상품을 동시 구매한 고객 리스트"
    plan: dict = {"target_user": {}, "semantic_plan": {"nodes": []}}
    semantic_plan_bridge.apply(plan, query, context=graph_rag._semantic_compile_context())
    assert graph_rag.CONDITION_EVALUATIONS_KEY not in plan


def test_co_purchase_compile_does_not_overwrite_existing_ir() -> None:
    import semantic_plan_bridge

    query = "같은 상품을 동시 구매한 고객수"
    sentinel = [{"id": "existing"}]
    plan = _co_purchase_plan(query, query)
    plan[graph_rag.CONDITION_EVALUATIONS_KEY] = sentinel
    semantic_plan_bridge.apply(plan, query, context=graph_rag._semantic_compile_context())
    assert plan[graph_rag.CONDITION_EVALUATIONS_KEY] is sentinel
