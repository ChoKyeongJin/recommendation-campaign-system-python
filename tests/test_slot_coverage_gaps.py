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


def test_llm_candidate_reads_plan_container_slot_from_top_level() -> None:
    out = graph_rag._coerce_llm_structured_conditions(
        {
            "target_user": {},
            "member_metric_ranking": {"metric_id": "total_buy_amt", "limit_type": "percent", "percent": 10},
        }
    )
    assert out["member_metric_ranking"]["percent"] == 10


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


def test_condition_evaluation_backfill_produces_valid_ir_with_window() -> None:
    plan = {"target_user": {"purchase_date": {"from": "20260301", "to": "20260331"}}}
    # graph_rag 가 SQL 생성 fail-close 판정 전에 호출하는 그 이름을 그대로 사용한다(배선 가드 겸용).
    graph_rag.apply_same_product_co_purchase_backfill(plan, "2026년 3월 같은 상품을 동시 구매한 고객수")

    evaluations = plan.get(graph_rag.CONDITION_EVALUATIONS_KEY)
    assert isinstance(evaluations, list) and len(evaluations) == 1
    assert graph_rag.validate_condition_evaluations(evaluations) == []
    time_range = evaluations[0]["evaluation_scope"]["time_range"]
    assert (time_range["from"], time_range["to"]) == ("20260301", "20260331")


def test_condition_evaluation_backfill_stays_fail_close_without_count_output() -> None:
    plan = {"target_user": {}}
    graph_rag.apply_same_product_co_purchase_backfill(plan, "2026년 3월 같은 상품을 동시 구매한 고객 리스트")
    assert graph_rag.CONDITION_EVALUATIONS_KEY not in plan


def test_condition_evaluation_backfill_does_not_overwrite_existing_ir() -> None:
    sentinel = [{"id": "existing"}]
    plan = {graph_rag.CONDITION_EVALUATIONS_KEY: sentinel, "target_user": {}}
    graph_rag.apply_same_product_co_purchase_backfill(plan, "같은 상품을 동시 구매한 고객수")
    assert plan[graph_rag.CONDITION_EVALUATIONS_KEY] is sentinel
