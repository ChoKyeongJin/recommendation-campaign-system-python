"""SemanticPlanV2 → 기존 query_plan 컴파일 계약.

삭제된 결정론 백필들이 검증하던 **사용자 요구와 기대 의미**를 이 계층으로 이전한 파일이다.
이전 대응(과거 테스트 → 지금 테스트):

  numeric_condition_backfill 카트/트렌드   → AggregatePredicate(scope=cart) / MetricComparison
  numeric_condition_backfill 구매금액 임계 → AggregatePredicate(scope=order)
  numeric_condition_backfill 회원 랭킹     → RankedSet(entity=member)
  campaign_condition_backfill 횟수/금액    → AggregatePredicate(scope=campaign)
  metric_registry 프로필 백필              → Predicate(프로필 지표/날짜 상태)
  entity_set 백필                          → EntitySetMembership
  condition_evaluation_ir 동시구매 백필    → RelationPredicate(relation=co_purchase)

차이는 **입력**이다: 예전 테스트는 원문 문자열을 넣었고, 지금은 그 원문의 의미 노드를 넣는다.
원문 → 노드는 LLM 이 하고(tests/test_semantic_plan_extraction.py 가 계약을 고정),
노드 → 슬롯은 여기 컴파일러가 결정론으로 한다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import legacy_plan_compiler  # noqa: E402
import semantic_capability  # noqa: E402
import semantic_plan  # noqa: E402
from legacy_plan_compiler import LegacyQueryPlanCompiler  # noqa: E402
from semantic_plan import SemanticPlanV2  # noqa: E402


@pytest.fixture(scope="module")
def context():
    return graph_rag._semantic_compile_context()


@pytest.fixture(scope="module")
def registry():
    return semantic_capability.registry()


def _plan(*nodes) -> SemanticPlanV2:
    return SemanticPlanV2(nodes=list(nodes))


def _compile(plan: SemanticPlanV2, context, registry=None):
    return LegacyQueryPlanCompiler().compile(plan, registry, context)


def _node(payload: dict) -> semantic_plan.SemanticNode:
    return semantic_plan.node_from_dict(payload)


# ── 장바구니 집계(과거: numeric_condition_backfill ①) ────────────────────────────
def test_cart_aggregate_predicates_compile_to_cart_slot(context) -> None:
    """'장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상' — 항목 둘이 모두 남는다."""
    plan = _plan(
        _node({
            "id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
            "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개",
        }),
        _node({
            "id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
            "scope": "cart", "metric": "cart_amount", "operator": "이상",
            "value": {"amount": 100000, "currency": "KRW"},
        }),
    )
    result = _compile(plan, context)
    cart = result.target_user["cart_aggregate"]
    assert {"metric": "cart_line_count", "operator": ">=", "threshold": 2} in cart
    assert {"metric": "cart_amount", "operator": ">=", "threshold": 100000} in cart
    assert result.node_slots == {
        "req-1": "target_user.cart_aggregate",
        "req-2": "target_user.cart_aggregate",
    }


# ── 구매금액 임계(과거: numeric_condition_backfill ①-β) ─────────────────────────
def test_order_aggregate_predicate_compiles_to_aggregate_conditions(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "aggregate_predicate", "source_span": "이십만원 이상을 구매한",
        "scope": "order", "metric": "purchase_amount", "operator": ">=", "value": "이십만원",
    }))
    result = _compile(plan, context)
    condition = result.target_user["aggregate_conditions"][0]
    assert condition["metric_id"] == "purchase_amount"
    assert condition["operator"] == ">="
    assert condition["threshold"] == 200000


def test_cart_and_order_scopes_never_claim_the_same_slot(context) -> None:
    """예전 백필은 '총금액' 문맥을 두 번 청구하지 않으려 원문 앞뒤를 다시 읽어야 했다.
    이제 scope 가 노드 필드라 이중 청구가 구조적으로 불가능하다."""
    plan = _plan(_node({
        "id": "req-1", "type": "aggregate_predicate", "source_span": "장바구니 총금액이 10만 원 이상",
        "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원",
    }))
    result = _compile(plan, context)
    assert "cart_aggregate" in result.target_user
    assert "aggregate_conditions" not in result.target_user


# ── 캠페인 집계 기간 ──────────────────────────────────────────────────────────────
def test_campaign_aggregate_period_fails_closed_instead_of_being_dropped(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "aggregate_predicate",
        "source_span": "2026년 3월 캠페인 발송 성공 횟수가 3회 이상",
        "scope": "campaign", "metric": "campaign_contact",
        "operator": ">=", "value": 3,
        "period": {"type": "absolute", "from": "2026-03-01", "to": "2026-03-31"},
    }))

    result = _compile(plan, context)

    assert result.is_empty
    assert result.failures[0]["failure_code"] == semantic_plan.UNSUPPORTED_SEMANTICS
    assert "캠페인 집계 기간" in result.failures[0]["reason"]
    assert "보존하지 못하므로" in result.failures[0]["reason"]


# ── 기간 대 기간 증감(과거: numeric_condition_backfill ②) ────────────────────────
def test_metric_comparison_owns_both_periods_and_compiles_to_metric_trend(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "metric_comparison",
        "source_span": "2026년 2월과 3월의 구매금액이 증가한",
        "metric": "purchase_amount",
        "baseline": {"type": "calendar_month", "year": 2026, "month": 2},
        "current": {"type": "calendar_month", "year": 2026, "month": 3},
        "relation": "increase",
    }))
    result = _compile(plan, context)
    trend = result.target_user["metric_trend"]
    assert trend["metric_id"] == "purchase_amount"
    assert trend["direction"] == "increase"
    assert (trend["baseline"]["from"], trend["baseline"]["to"]) == ("20260201", "20260228")
    assert (trend["current"]["from"], trend["current"]["to"]) == ("20260301", "20260331")
    assert "relative_change" not in trend


def test_metric_comparison_threshold_becomes_relative_change(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "metric_comparison",
        "source_span": "2026년 2월과 3월의 구매금액 차이가 10% 이상 증가한",
        "metric": "purchase_amount",
        "baseline": {"type": "calendar_month", "year": 2026, "month": 2},
        "current": {"type": "calendar_month", "year": 2026, "month": 3},
        "relation": "increase", "threshold": 10, "threshold_operator": "이상",
    }))
    trend = _compile(plan, context).target_user["metric_trend"]
    assert trend["relative_change"]["comparisons"] == [{"operator": ">=", "value": 10}]


def test_metric_comparison_fractional_ratio_reaches_sql_exactly(context) -> None:
    exact = "10.123456789012345678901"
    plan = _plan(_node({
        "id": "req-1", "type": "metric_comparison",
        "source_span": "두 기간의 구매금액이 정확한 비율 이상 증가한",
        "metric": "purchase_amount",
        "baseline": {"type": "calendar_month", "year": 2026, "month": 2},
        "current": {"type": "calendar_month", "year": 2026, "month": 3},
        "relation": "increase",
        "threshold": {"value": exact, "unit": "percent"},
        "threshold_operator": "이상",
    }))

    trend = _compile(plan, context).target_user["metric_trend"]
    assert trend["relative_change"]["comparisons"] == [
        {"operator": ">=", "value": exact}
    ]
    candidate = graph_rag.build_metric_trend_targets_sql_candidate(
        {"target_user": {"metric_trend": trend}}
    )
    assert candidate is not None
    assert exact in candidate["sql"]
    assert "10.123456789012346" not in candidate["sql"]


# ── 회원 지표 랭킹(과거: numeric_condition_backfill ②-α) ─────────────────────────
def test_ranked_set_compiles_to_member_metric_ranking(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "ranked_set", "source_span": "누적 구매금액 상위 10%",
        "entity": "member", "metric": "누적 구매금액", "direction": "descending",
        "limit": {"type": "percent", "value": 10},
    }))
    result = _compile(plan, context)
    ranking = result.plan["member_metric_ranking"]
    assert ranking["metric_id"] == "total_buy_amt"
    assert ranking["limit_type"] == "percent"
    assert ranking["percent"] == 10
    assert result.node_slots["req-1"] == "member_metric_ranking"


def test_ranked_set_count_limit_becomes_top_n(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "ranked_set", "source_span": "누적 구매금액 상위 100명",
        "entity": "member", "metric": "total_buy_amt", "direction": "descending",
        "limit": {"type": "count", "value": 100},
    }))
    ranking = _compile(plan, context).plan["member_metric_ranking"]
    assert ranking["top_n"] == 100 and "percent" not in ranking


def test_member_ranked_set_period_fails_closed_instead_of_being_dropped(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "ranked_set",
        "source_span": "2026년 3월 누적 구매금액 상위 10%",
        "entity": "member", "metric": "total_buy_amt", "direction": "descending",
        "limit": {"type": "percent", "value": 10},
        "period": {"type": "absolute", "from": "2026-03-01", "to": "2026-03-31"},
    }))

    result = _compile(plan, context)

    assert result.is_empty
    assert result.failures[0]["failure_code"] == semantic_plan.UNSUPPORTED_SEMANTICS
    assert "회원 랭킹 기간" in result.failures[0]["reason"]
    assert "보존하지 못하므로" in result.failures[0]["reason"]


# ── 프로필 지표(과거: metric_registry.apply_profile_condition_backfill) ──────────
def test_profile_predicates_compile_to_balance_and_date_slots(context) -> None:
    plan = _plan(
        _node({
            "id": "req-1", "type": "predicate", "source_span": "평균 구매주기가 30일 이내",
            "subject": "member", "metric": "buy_cycle", "operator": "이내", "value": "30일",
        }),
        _node({
            "id": "req-2", "type": "predicate", "source_span": "다음 구매예정일이 지난",
            "subject": "member", "metric": "next_purchase_due_date",
            "operator": "=", "value": "지남", "state": "past",
        }),
    )
    result = _compile(plan, context)
    balance = result.target_user["balance_conditions"][0]
    assert balance["metric_id"] == "buy_cycle"
    assert balance["operator"] == "<="
    assert result.target_user["profile_date_conditions"][0]["metric_id"] == "next_purchase_due_date"


# ── 파생 엔터티 집합(과거: graph_rag._apply_entity_set_backfill) ─────────────────
def test_entity_set_membership_compiles_to_entity_set_slot(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "entity_set_membership",
        "source_span": "가장 많이 팔린 상품 10개를 구매한 고객",
        "member_entity": "member", "relation": "purchase",
        "ranked_set": [{
            "id": "req-1a", "type": "ranked_set", "source_span": "가장 많이 팔린 상품 10개",
            "entity": "product", "metric": "sales_quantity", "direction": "descending",
            "limit": {"type": "count", "value": 10},
        }],
    }))
    node = _compile(plan, context).target_user["entity_set_condition"]
    assert node["entity"] == "product"
    assert node["relation"] == "purchase"
    assert node["limit"] == 10
    assert node["direction"] == "top"


# ── 동시구매(과거: condition_evaluation_ir.apply_same_product_co_purchase_backfill) ──
def test_co_purchase_relation_compiles_to_condition_evaluations(context) -> None:
    query = "2026년 3월 같은 상품을 동시 구매한 고객수"
    span = "같은 상품을 동시 구매한 고객수"
    plan = _plan(_node({
        "id": "req-1", "type": "relation_predicate", "source_span": span,
        "source_start": query.index(span), "source_end": query.index(span) + len(span),
        "subject": "member", "attribute": "product", "relation": "co_purchase",
        "period": {"type": "absolute", "from": "2026-03-01", "to": "2026-03-31"},
    }))
    result = _compile(plan, context)
    evaluations = result.plan["condition_evaluations"]
    assert len(evaluations) == 1
    assert evaluations[0]["capability"] == "same_product_same_order_quantity_v1"
    assert evaluations[0]["evaluation_scope"]["time_range"]["from"] == "20260301"


# ── 결핍 노드는 컴파일하지 않는다(가짜 성공 차단) ────────────────────────────────
def test_incomplete_node_is_not_compiled(context) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "aggregate_predicate", "source_span": "장바구니 상품이 많은",
        "scope": "cart", "metric": "cart_line_count",  # operator/value 결핍
    }))
    result = _compile(plan, context)
    assert result.is_empty
    assert plan.missing_fields() == ("req-1.operator", "req-1.value")


# ── capability 가 막은 노드는 컴파일되지 않는다 ─────────────────────────────────
def test_capability_gate_blocks_unsupported_entity_ranking(context, registry) -> None:
    plan = _plan(_node({
        "id": "req-1", "type": "ranked_set", "source_span": "매출 상위 브랜드 5개",
        "entity": "brand", "metric": "purchase_amount", "direction": "descending",
        "limit": {"type": "count", "value": 5},
    }))
    result = _compile(plan, context, registry)
    assert result.is_empty
    verdict = registry.judge(plan.nodes[0])
    assert verdict.failure_code == semantic_plan.UNSUPPORTED_SEMANTICS
    assert verdict.compiler_supported is False


# ── 결정론성: 같은 SemanticPlan → 항상 같은 query plan ──────────────────────────
def test_compilation_is_deterministic(context) -> None:
    payload = {
        "id": "req-1", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
        "scope": "cart", "metric": "cart_amount", "operator": "이상", "value": "10만 원",
    }
    first = _compile(_plan(_node(payload)), context).to_dict()
    second = _compile(_plan(_node(payload)), context).to_dict()
    assert first == second


# ── 슬롯 소유권: 컴파일러 소유 슬롯은 LLM 노출면에 없다 ─────────────────────────
def test_compiler_owned_slots_are_not_exposed_to_the_llm() -> None:
    import json

    from query_structurer.campaign_plan_v4 import CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA

    properties = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]
    assert "audience_requirement" in properties
    assert "target_user" not in properties and "exclude" not in properties
    rendered = json.dumps(properties, ensure_ascii=False)
    for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS:
        assert f'"{slot.rpartition(".")[2]}"' not in rendered, slot


def test_every_node_type_has_a_declared_slot_mapping() -> None:
    """새 노드를 추가하면 매핑 선언도 함께 — 조용한 '컴파일러 없음'을 막는다."""
    assert set(legacy_plan_compiler.NODE_SLOT_MAP) == set(semantic_plan.NODE_CLASS_BY_TYPE)


def test_relative_window_resolves_against_injected_today(context) -> None:
    """상대 창은 정규화기가 기준일로 확정한다 — 컴파일러가 날짜를 만들지 않는다."""
    ctx = legacy_plan_compiler.CompileContext(
        slot_shapes=context.slot_shapes, allowed=context.allowed,
        today=date(2026, 3, 31), metric_resolvers=context.metric_resolvers,
    )
    plan = _plan(_node({
        "id": "req-1", "type": "entity_set_membership",
        "source_span": "최근 30일 가장 많이 팔린 상품 5개를 구매한 고객",
        "member_entity": "member", "relation": "purchase",
        "ranked_set": [{
            "id": "req-1a", "type": "ranked_set", "source_span": "최근 30일 가장 많이 팔린 상품 5개",
            "entity": "product", "metric": "sales_quantity", "direction": "descending",
            "limit": {"type": "count", "value": 5},
            "period": {"type": "relative", "value": 30, "unit": "days"},
        }],
    }))
    node = LegacyQueryPlanCompiler().compile(plan, None, ctx).target_user["entity_set_condition"]
    assert node["window"] == {
        "type": "relative",
        "value": 30,
        "unit": "days",
        "from": "20260302",
        "to": "20260331",
    }


@pytest.mark.parametrize(
    ("value", "unit", "expected_from"),
    [(1, "months", "20260301"), (6, "months", "20251001"), (1, "years", "20250401")],
)
def test_calendar_relative_windows_compile_from_the_injected_date(
    context, value: int, unit: str, expected_from: str
) -> None:
    ctx = legacy_plan_compiler.CompileContext(
        slot_shapes=context.slot_shapes,
        allowed=context.allowed,
        node_vocabularies=context.node_vocabularies,
        today=date(2026, 3, 31),
        metric_resolvers=context.metric_resolvers,
        scope_slots=context.scope_slots,
        count_metrics=context.count_metrics,
    )
    result = _compile(
        _plan(_node({
            "id": "req-1",
            "type": "aggregate_predicate",
            "source_span": "달력 기준 주문",
            "scope": "order",
            "metric": "order_count",
            "operator": ">=",
            "value": 5,
            "period": {"type": "relative", "value": value, "unit": unit},
        })),
        ctx,
    )

    assert result.failures == []
    assert result.target_user["aggregate_conditions"][0]["window"] == {
        "type": "relative",
        "value": value,
        "unit": unit,
        "from": expected_from,
        "to": "20260331",
    }


def test_absolute_window_ending_after_injected_today_fails_closed(context) -> None:
    ctx = legacy_plan_compiler.CompileContext(
        slot_shapes=context.slot_shapes,
        allowed=context.allowed,
        node_vocabularies=context.node_vocabularies,
        today=date(2026, 3, 31),
        metric_resolvers=context.metric_resolvers,
        scope_slots=context.scope_slots,
        count_metrics=context.count_metrics,
    )
    result = _compile(
        _plan(_node({
            "id": "req-1",
            "type": "aggregate_predicate",
            "source_span": "2026년 3월부터 4월까지 주문",
            "scope": "order",
            "metric": "order_count",
            "operator": ">=",
            "value": 5,
            "period": {"type": "absolute", "from": "2026-03-01", "to": "2026-04-30"},
        })),
        ctx,
    )

    assert result.is_empty
    assert result.failures[0]["failure_code"] == semantic_plan.UNSUPPORTED_SEMANTICS
    assert "기준일 이후에 끝나는" in result.failures[0]["reason"]


def test_calendar_relative_window_without_reference_date_fails_closed(context) -> None:
    result = _compile(
        _plan(_node({
            "id": "req-1",
            "type": "aggregate_predicate",
            "source_span": "최근 한 달 주문",
            "scope": "order",
            "metric": "order_count",
            "operator": ">=",
            "value": 5,
            "period": {"type": "relative", "value": 1, "unit": "months"},
        })),
        graph_rag._semantic_compile_context(),
    )

    assert result.is_empty
    assert "기준일이 주입되지 않아" in result.failures[0]["reason"]
