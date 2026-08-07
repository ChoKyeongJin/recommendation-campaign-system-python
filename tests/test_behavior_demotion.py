"""잉여 행동 라벨 강등 — 집계 조건이 함의하는 behaviors 가 소유권 이동으로 회수됨을 고정.

배경(2026-08-01 'VIP 전환' 실사고): LLM 이 '최근 6개월 주문 5건 이상 + 총결제 50만'을 올바른
aggregate_conditions 2건으로 실으면서 잉여 behaviors=['repeat_buyer'] 를 덧붙였다. 잉여 슬롯이
(a) order_count 빌더의 라우팅 가로채기 → aggregate 드롭 → fail-close reject, (b) 폴백 후보의
behaviors 파생 커버리지 조건(리터럴 문자열 매칭) 사망을 연쇄 격발시켰다.

여기서 강제하는 계약:
  1. 같은 물리 집계를 함의하는 잉여 행동은 강등된다(소유권 이동 — superseded_conditions 기록).
  2. 함의가 성립하지 않으면 절대 강등하지 않는다(fail-close): 다른 지표·낮은 임계·회원 외 grain·
     부재 조건(no_purchase)·창이 있는 정확값(first_purchase).
  3. order_count 빌더는 컴파일 불가 선언(_supported:false)·불완전 규칙을 선택하지 않는다 —
     lapsed_buyer 가 폴백('=1')으로 조용히 '첫 구매'가 되던 잠복 결함의 가드.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import behavior_demotion  # noqa: E402
import member_filters_config  # noqa: E402

_QUERY = "최근 6개월 주문 건수가 5건 이상이고 총결제금액이 50만 원 이상인 회원"


def _plan(behaviors: list[str], aggregates: list[dict]) -> dict:
    return {"target_user": {"behaviors": list(behaviors), "aggregate_conditions": list(aggregates)}}


def test_config_declares_order_count_equivalents() -> None:
    """등가 선언의 단일 소스는 설정이다 — 코드에 매핑을 박으면 이중 소유가 재발한다."""
    equivalents = member_filters_config.behavior_aggregate_equivalents()
    assert equivalents.get("repeat_buyer") == {"metric_id": "order_count", "operator": ">=", "count": 2}
    assert equivalents.get("first_purchase") == {"metric_id": "order_count", "operator": "=", "count": 1}
    assert "no_purchase" not in equivalents, "부재 조건은 집계 임계값과 등가가 없다 — 강등하면 극성이 반전된다."
    assert "lapsed_buyer" not in equivalents


def test_equivalent_metric_ids_exist_in_aggregate_registry() -> None:
    """등가 선언의 metric_id 가 aggregate_targets.metrics 실재 지표와 결속돼야 한다 —
    어느 한쪽이 개명되면 강등이 무증상 no-op 이 되어 라우팅 가로채기 사고가 재발한다."""
    equivalents = member_filters_config.behavior_aggregate_equivalents()
    metrics = set(member_filters_config.aggregate_metrics())
    dangling = {
        behavior: spec["metric_id"]
        for behavior, spec in equivalents.items()
        if spec["metric_id"] not in metrics
    }
    assert not dangling, (
        f"behaviors.*.metric_id 가 aggregate_targets.metrics 에 없는 지표를 가리킨다: {dangling}"
    )


def test_repeat_buyer_is_demoted_by_covering_windowed_aggregate() -> None:
    """'N일 내 5건 이상'은 평생 2건 이상을 반드시 함의한다 — 창이 좁아도 하한 함의는 성립."""
    plan = _plan(["repeat_buyer"], [{"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 180}])
    demoted = behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY)
    assert demoted == ["repeat_buyer"]
    assert plan["target_user"]["behaviors"] == []
    records = plan.get("superseded_conditions") or []
    assert any(
        entry.get("slot") == "target_user.behaviors:repeat_buyer"
        and entry.get("owner") == "aggregate_conditions:order_count"
        and entry.get("outcome") == "removed"
        for entry in records
    ), f"강등은 소유권 이동으로 기록돼야 한다: {records}"


def test_lower_threshold_does_not_demote() -> None:
    """order_count>=1 은 repeat_buyer(>=2)를 함의하지 않는다 — 조건이 다르므로 둘 다 남는다."""
    plan = _plan(["repeat_buyer"], [{"metric_id": "order_count", "operator": ">=", "threshold": 1}])
    assert behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY) == []
    assert plan["target_user"]["behaviors"] == ["repeat_buyer"]


def test_different_metric_does_not_demote() -> None:
    plan = _plan(["repeat_buyer"], [{"metric_id": "purchase_amount", "operator": ">=", "threshold": 500000}])
    assert behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY) == []


def test_non_member_grain_does_not_demote() -> None:
    """per_brand 임계값은 회원 총량을 함의하지 않는다(브랜드당 5건 ≠ 총 5건)."""
    plan = _plan(
        ["repeat_buyer"],
        [{"metric_id": "order_count", "operator": ">=", "threshold": 5, "aggregation_scope": "per_brand"}],
    )
    assert behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY) == []


def test_windowed_exact_count_does_not_demote_first_purchase() -> None:
    """'창 안에서 정확히 1건'은 평생 1건(첫 구매)을 보장하지 않는다."""
    plan = _plan(["first_purchase"], [{"metric_id": "order_count", "operator": "=", "threshold": 1, "window_days": 90}])
    assert behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY) == []
    calendar_window = _plan(["first_purchase"], [{
        "metric_id": "order_count",
        "operator": "=",
        "threshold": 1,
        "window": {
            "type": "relative",
            "value": 3,
            "unit": "months",
            "from": "20260101",
            "to": "20260331",
        },
    }])
    assert behavior_demotion.demote_aggregate_covered_behaviors(
        calendar_window,
        source_text=_QUERY,
    ) == []
    windowless = _plan(["first_purchase"], [{"metric_id": "order_count", "operator": "=", "threshold": 1}])
    assert behavior_demotion.demote_aggregate_covered_behaviors(windowless, source_text=_QUERY) == ["first_purchase"]


def test_no_purchase_is_never_demoted() -> None:
    plan = _plan(["no_purchase"], [{"metric_id": "order_count", "operator": ">=", "threshold": 5}])
    assert behavior_demotion.demote_aggregate_covered_behaviors(plan, source_text=_QUERY) == []
    assert plan["target_user"]["behaviors"] == ["no_purchase"]


# ── order_count 빌더의 미지원 규칙 가드 ─────────────────────────────────────────────


def test_builder_skips_unsupported_lapsed_buyer() -> None:
    """lapsed_buyer(_supported:false)는 선택되지 않는다 — 폴백 '=1' 컴파일이 아니라 미선택(None)."""
    import graph_rag

    candidate = graph_rag.build_order_count_targets_sql_candidate(
        {"target_user": {"behaviors": ["lapsed_buyer"]}}
    )
    assert candidate is None


def test_builder_still_compiles_supported_repeat_buyer(member_slot_gate_lifted: None) -> None:
    import graph_rag

    candidate = graph_rag.build_order_count_targets_sql_candidate(
        {"target_user": {"behaviors": ["repeat_buyer"]}}
    )
    assert candidate is not None
    assert "HAVING COUNT(DISTINCT ORDER_ID) >= 2" in candidate["sql"]

