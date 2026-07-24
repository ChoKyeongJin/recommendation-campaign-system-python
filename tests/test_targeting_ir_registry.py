"""타겟 조건 도메인 IR + 중앙 레지스트리 회귀 (targeting_ir).

배경: 조건 유형 추가마다 신호 감지 OR 목록·빌더 defer 나열·confidence 수집 분기를 손배선하다
하나만 빠지면 조건이 조용히 다른 빌더로 새거나(캠페인 반응 횟수→주문 집계 오배정) 근거 없이
감점됐다. 이제 도메인 선언은 targeting_ir.CONDITION_SPECS 단일 소스이고, 신호/defer/confidence 가
전부 여기서 파생된다. 이 테스트는 (1) '모든 fact_join kind 는 정확히 하나의 빌더가 소유한다'
불변식과 (2) 파생 결과가 기존 수작업 배선과 동일함(파리티)을 고정한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_targeting_ir_registry.py -q
"""

import pytest

import graph_rag as g
import targeting_ir as ir


# ── 불변식: 소유권/유일성 ─────────────────────────────────────────────────────────
def test_every_fact_join_kind_has_exactly_one_owner():
    ownership: dict[str, list[str]] = {}
    for builder, owned in g._sql_target_builder_registry():
        for kind in owned:
            ownership.setdefault(kind, []).append(builder.__name__)
    for kind in ir.fact_join_kinds():
        owners = ownership.get(kind, [])
        assert len(owners) == 1, f"fact_join 조건 {kind!r} 소유 빌더가 {owners} — 정확히 1개여야 한다"


def test_owned_kinds_exist_in_condition_specs():
    known = {spec.kind for spec in ir.CONDITION_SPECS}
    for builder, owned in g._sql_target_builder_registry():
        unknown = owned - known
        assert not unknown, f"{builder.__name__} 가 레지스트리에 없는 kind 소유 선언: {unknown}"


def test_spec_kinds_unique():
    kinds = [spec.kind for spec in ir.CONDITION_SPECS]
    assert len(kinds) == len(set(kinds))


def test_builder_order_preserved():
    # 순서 정책 회귀: 반응 '횟수' 빌더는 EXISTS 캠페인 빌더보다, purchase_count_ranking 은
    # purchase_history 보다 먼저다(날짜만 있는 랭킹이 구매 이력으로 새지 않게).
    builders = g._sql_target_builders()
    assert builders.index(g.build_campaign_response_frequency_targets_sql_candidate) < builders.index(
        g.build_campaign_response_targets_sql_candidate
    )
    assert builders.index(g.build_purchase_count_ranking_sql_candidate) < builders.index(
        g.build_purchase_history_targets_sql_candidate
    )
    assert builders[-1] is g.build_member_targets_sql_candidate


# ── 파리티: 신호 감지(_has_member_target_signal ← spec.signals_target) ─────────────
@pytest.mark.parametrize("plan,expected", [
    ({"target_user": {}}, False),
    ({"target_user": {"campaign_responses": [{"canonical": "offer_response", "predicate": "R.OFFR_RSPN_YN = 'Y'"}]}}, True),
    ({"target_user": {"campaign_response_frequency": {"operator": ">=", "count": 2, "window_days": 90}}}, True),
    ({"target_user": {"behaviors": ["no_purchase"]}}, True),
    ({"target_user": {"behaviors": ["cart_abandoner"]}}, True),
    # 지원 집합 밖 행동만으로는 신호가 아니다(현행 intent 미승격 보존 — unclassified 는 signals_target=False).
    ({"target_user": {"behaviors": ["office_worker"]}}, False),
    ({"target_user": {"purchase_inactivity": {"min_days": 90}}}, True),
    ({"target_user": {"birthday_target": {"granularity": "month"}}}, True),
    ({"target_user": {"aggregate_conditions": [{"metric_id": "purchase_amount"}]}}, True),
    ({"target_user": {"purchase_object": "기저귀"}}, True),
    ({"target_user": {}, "region_density_target": {"metric_id": None}}, True),
    ({"target_user": {}, "purchase_count_ranking": {"top_n": 10}}, True),
])
def test_signal_parity(plan, expected):
    assert g._has_member_target_signal(plan) is expected


# ── 파리티: 캠페인 EXISTS 빌더 defer(← spec.fact_join, purchase_* 예외 유지) ───────
def _exists_plan(**target_user_extra):
    tu = {"campaign_responses": [{"canonical": "campaign_contact", "predicate": "R.CNCT_SCS_YN = 'Y'"}]}
    tu.update(target_user_extra)
    return {"intent": "find_user_segment", "target_user": tu}


def test_exists_builder_defers_to_fact_join_conditions():
    assert g.build_campaign_response_targets_sql_candidate(_exists_plan(behaviors=["no_purchase"])) is None
    assert g.build_campaign_response_targets_sql_candidate(_exists_plan(purchase_inactivity={"min_days": 30})) is None
    assert g.build_campaign_response_targets_sql_candidate(
        _exists_plan(campaign_response_frequency={"operator": ">=", "count": 2, "window_days": None})
    ) is None


def test_exists_builder_keeps_purchase_object_exception():
    # '캠페인 보고 구매'의 '보고' 오추출 상품이 구매이력으로 새지 않게 purchase_object/purchase_date 는
    # defer 하지 않고 여기서 계속 처리한다(문서화된 예외 정책 보존).
    candidate = g.build_campaign_response_targets_sql_candidate(_exists_plan(purchase_object="보고"))
    assert candidate is not None
    assert "MCS_CAMP_MBR_RSPN_FT" in candidate["sql"]


# ── 파리티: confidence 조건 수집(← spec.confidence 메타) ──────────────────────────
def test_confidence_conditions_derive_from_registry():
    import confidence as c

    plan = {
        "target_user": {
            "gender": "female",
            "campaign_response_frequency": {"operator": ">=", "count": 2, "window_days": 90, "label": "캠페인 2회 이상 반응"},
            "purchase_inactivity": {"min_days": 30},
            "behaviors": ["repeat_buyer", "cart_abandoner"],
            "cart_retention": {"min_days": 7},
            "purchase_object": "기저귀",
        },
    }
    conditions = c._extract_conditions(plan, {})
    keys = {cond["key"] for cond in conditions}
    assert {"gender", "campaign_response_frequency", "purchase_inactivity", "repeat_buyer",
            "cart_abandoner", "cart_retention", "purchase_object"} <= keys
    by_key = {cond["key"]: cond for cond in conditions}
    assert by_key["repeat_buyer"]["ko"] == "재구매(2회 이상)"
    assert by_key["campaign_response_frequency"]["kind"] == "campaign_response"
    assert by_key["cart_retention"]["ko"] == "장바구니 보관 7일 이상"


def test_ir_extraction_respects_order_count_config():
    # 설정 소유 행동 집합을 주입 — 집합 밖 행동은 unclassified 로 분류된다.
    plan = {"target_user": {"behaviors": ["no_purchase", "office_worker"]}}
    kinds = [c.kind for c in ir.extract_target_conditions(plan)]
    assert "order_count_behavior" in kinds
    assert "unclassified_behavior" in kinds
