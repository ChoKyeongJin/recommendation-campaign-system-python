"""LLM 출력 계약 + SemanticPlanV2 추출 — 원문 하나가 어떤 의미 노드가 되는가.

이 파일은 LLM 을 호출하지 않는다. LLM 이 낼 수 있는/없는 것(계약)과, 낸 payload 가
타입드 노드로 어떻게 확정되는지를 고정한다. 실제 모델 호출 채점은 골든 라이브 테스트 소관.

여기서 강제하는 계약:
  1. LLM 은 query_plan/target_user/semantic_ir(missing/unsupported/status)을 만들 수 없다.
     그런 키가 응답에 오면 **버리고 위반으로 기록**한다.
  2. 모든 노드에 원문 근거(source_span)가 있고, 근거 없는 문구는 좌표를 얻지 못한다.
  3. missing_fields 와 status 는 노드 스키마에서 **계산**된다(LLM 이 보고하지 않는다).
  4. 소유권: metric_comparison 이 두 기간을 소유하면 별도 기간 결핍이 생기지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_pipeline  # noqa: E402
import semantic_plan  # noqa: E402
import semantic_plan_llm  # noqa: E402


def _extract(query: str, nodes: list[dict]):
    return semantic_plan_llm.parse_semantic_plan_response({"nodes": nodes}, query)


# ── ① 장바구니 복합 조건 ─────────────────────────────────────────────────────────
CART_QUERY = "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원"


def test_cart_conditions_become_two_aggregate_predicates() -> None:
    plan, violations = _extract(CART_QUERY, [
        {"id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
         "scope": "cart", "metric": "line_count", "operator": "gte", "value": 2},
        {"id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
         "scope": "cart", "metric": "amount", "operator": "gte",
         "value": {"amount": 100000, "currency": "KRW"}},
    ])
    assert not violations
    assert [node.type for node in plan.nodes] == ["aggregate_predicate"] * 2
    assert plan.nodes[0].scope == "cart" and plan.nodes[0].value == 2
    assert plan.nodes[1].value == {"amount": 100000, "currency": "KRW"}
    # 두 조건이 한 노드로 뭉개지지 않는다 — 항목이 둘이어야 AND 로 컴파일된다.
    assert plan.missing_fields() == ()
    assert plan.status() == semantic_plan.STATUS_RESOLVED


# ── ② 회원 지표 랭킹 ─────────────────────────────────────────────────────────────
def test_ranking_becomes_ranked_set_with_percent_limit() -> None:
    plan, _ = _extract("구매금액 상위 10% 회원", [
        {"id": "req-1", "type": "ranked_set", "source_span": "구매금액 상위 10%",
         "entity": "member", "metric": "purchase_amount", "direction": "descending",
         "limit": {"type": "percent", "value": 10}},
    ])
    node = plan.nodes[0]
    assert node.type == "ranked_set"
    assert node.limit == {"type": "percent", "value": 10}
    assert node.source_start == 0 and node.source_end == len("구매금액 상위 10%")


# ── ③ 기간 대 기간 비교(소유권 테스트) ───────────────────────────────────────────
TREND_QUERY = "2026년 2월과 3월의 구매금액이 증가한 회원"


def test_metric_comparison_owns_both_periods_and_the_relation() -> None:
    plan, _ = _extract(TREND_QUERY, [
        {"id": "req-1", "type": "metric_comparison",
         "source_span": "2026년 2월과 3월의 구매금액이 증가한",
         "metric": "purchase_amount",
         "baseline": {"type": "calendar_month", "year": 2026, "month": 2},
         "current": {"type": "calendar_month", "year": 2026, "month": 3},
         "relation": "increase"},
    ])
    node = plan.nodes[0]
    assert node.baseline == {"type": "calendar_month", "year": 2026, "month": 2}
    assert node.current == {"type": "calendar_month", "year": 2026, "month": 3}
    assert node.relation == "increase"
    # 핵심 계약: 두 기간을 이 노드가 소유하므로 purchase_date 결핍이 생기지 않는다.
    assert plan.missing_fields() == ()
    assert "purchase_date" not in str(plan.missing_fields())
    assert semantic_pipeline.project_semantic_ir(plan)["missing_fields"] == []


def test_metric_comparison_requirements_are_schema_declared() -> None:
    """소유 필드 목록은 노드 선언에서 파생된다 — 손 목록이 아니다."""
    assert semantic_plan.NODE_REQUIREMENTS["metric_comparison"] == frozenset(
        {"metric", "baseline", "current", "relation"}
    )
    assert semantic_plan.NODE_REQUIREMENTS["aggregate_predicate"] == frozenset(
        {"scope", "metric", "operator", "value"}
    )
    assert semantic_plan.NODE_REQUIREMENTS["ranked_set"] == frozenset(
        {"entity", "metric", "direction", "limit"}
    )


# ── ④ 파생 집합(랭킹 → 회원) ─────────────────────────────────────────────────────
def test_entity_set_membership_nests_its_ranked_set() -> None:
    query = "가장 많이 팔린 상품 10개를 구매한 회원"
    plan, _ = _extract(query, [
        {"id": "req-1", "type": "entity_set_membership", "source_span": query,
         "member_entity": "member", "relation": "purchase",
         "ranked_set": [{
             "id": "req-1a", "type": "ranked_set", "source_span": "가장 많이 팔린 상품 10개",
             "entity": "product", "metric": "sales_quantity", "direction": "descending",
             "limit": {"type": "count", "value": 10},
         }]},
    ])
    root = plan.nodes[0]
    assert root.type == "entity_set_membership"
    assert [child.type for child in root.children()] == ["ranked_set"]
    assert plan.node_by_id("req-1a").limit == {"type": "count", "value": 10}
    # 대상 상품은 계산 결과이므로 리터럴 상품 조건이 결핍으로 잡히지 않는다.
    assert plan.missing_fields() == ()


# ── LLM 계약: 금지 키 ────────────────────────────────────────────────────────────
def test_llm_cannot_emit_query_plan_or_missing_fields() -> None:
    plan, violations = semantic_plan_llm.parse_semantic_plan_response(
        {
            "nodes": [],
            "query_plan": {"target_user": {"gender": "female"}},
            "target_user": {"cart_aggregate": [{"metric": "cart_amount"}]},
            "semantic_ir": {"status": "resolved", "missing_fields": []},
            "missing_fields": ["purchase_date"],
            "unsupported_operations": [{"kind": "x", "reason": "y", "evidence": "z"}],
            "status": "resolved",
            "member_metric_ranking": {"metric_id": "total_buy_amt"},
        },
        "아무 원문",
    )
    assert plan.nodes == []
    assert set(violations) == {
        "llm_emitted_forbidden_key:member_metric_ranking",
        "llm_emitted_forbidden_key:missing_fields",
        "llm_emitted_forbidden_key:query_plan",
        "llm_emitted_forbidden_key:semantic_ir",
        "llm_emitted_forbidden_key:status",
        "llm_emitted_forbidden_key:target_user",
        "llm_emitted_forbidden_key:unsupported_operations",
    }


def test_forbidden_keys_cover_every_compiler_owned_slot() -> None:
    """새 슬롯이 컴파일러 소유가 되면 LLM 금지 목록에도 자동으로 들어가야 한다."""
    import legacy_plan_compiler

    for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS:
        name = slot.rpartition(".")[2]
        assert name in semantic_plan_llm.FORBIDDEN_OUTPUT_KEYS, (
            f"컴파일러 소유 슬롯 {slot} 이 LLM 금지 목록에 없다."
        )


def test_llm_schema_is_derived_from_node_declarations() -> None:
    """손으로 쓴 두 번째 스키마 권위가 생기지 않게 — 노드를 추가하면 스키마도 따라온다."""
    schema = semantic_plan_llm.semantic_plan_tool()["function"]["parameters"]
    node_schema = schema["$defs"]["semanticNode"]
    assert set(node_schema["properties"]["type"]["enum"]) == set(semantic_plan.NODE_CLASS_BY_TYPE)
    derived_names = {
        spec.name
        for cls in semantic_plan.NODE_CLASSES
        for spec in cls.FIELDS
        if spec.derived
    }
    assert derived_names, "파생 필드가 하나도 없다 — 아래 노출 금지 계약이 공허해진다."
    for cls in semantic_plan.NODE_CLASSES:
        for spec in cls.FIELDS:
            if spec.derived:
                # 파생 필드(시간 한정어 등)는 시스템 계산값이다 — 노출하면 LLM 이 지어낸다.
                assert spec.name not in node_schema["properties"], (
                    f"{cls.TYPE}.{spec.name} 은 파생 필드인데 LLM 스키마에 노출됐다"
                )
                continue
            assert spec.name in node_schema["properties"], f"{cls.TYPE}.{spec.name} 미노출"
    # 슬롯 이름은 LLM 스키마 어디에도 없다.
    import json

    import legacy_plan_compiler

    rendered = json.dumps(schema, ensure_ascii=False)
    for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS:
        assert slot.rpartition(".")[2] not in rendered, f"LLM 스키마가 슬롯 이름을 안다: {slot}"


# ── 근거 없는 노드 ───────────────────────────────────────────────────────────────
def test_span_repair_never_invents_evidence() -> None:
    query = "장바구니 총금액이 10만 원 이상인 회원"
    plan, _ = _extract(query, [
        {"id": "req-1", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
         "source_start": 999, "source_end": 1000,
         "scope": "cart", "metric": "amount", "operator": "gte", "value": "10만 원"},
        {"id": "req-2", "type": "aggregate_predicate", "source_span": "원문에 없는 문구",
         "scope": "cart", "metric": "line_count", "operator": "gte", "value": 2},
    ])
    # 유일하게 등장하는 근거는 좌표가 복구된다.
    assert query[plan.nodes[0].source_start:plan.nodes[0].source_end] == "총금액이 10만 원 이상"
    # 원문에 없는 근거는 좌표를 얻지 못한다(지어내지 않는다).
    assert plan.nodes[1].source_start is None


def test_unknown_node_type_is_a_contract_error() -> None:
    with pytest.raises(semantic_plan_llm.SemanticPlanContractError):
        semantic_plan_llm.parse_semantic_plan_response(
            {"nodes": [{"id": "req-1", "type": "telepathy", "source_span": "x"}]}, "x"
        )


def test_duplicate_ids_are_made_unique() -> None:
    plan, _ = _extract("a b", [
        {"id": "req-1", "type": "predicate", "source_span": "a", "subject": "member"},
        {"id": "req-1", "type": "predicate", "source_span": "b", "subject": "member"},
    ])
    assert len({node.id for node in plan.nodes}) == 2
