"""타입 정규화 계층의 불변식 — LLM 이 타입을 잘못 제안해도 오염이 IR 에 들어가지 않는다.

2026-08-02 실측에서 이 파일이 고정하는 실패가 전 요청을 막았다: LLM 이
`entity_set_membership` 을 제안하고 집계 술어의 payload 를 채웠고, 그 결과 필수 필드 결핍이
그대로 사용자 확인 요청(`req-1.member_entity 를 확정해 주세요`)이 됐다.

여기서 강제하는 불변식:
  1. 타입별 스키마가 다른 타입의 필드를 받지 않는다(스키마 단계 차단).
  2. 호환 타입이 정확히 하나면 **재호출 없이** 재분류된다.
  3. discriminator 는 값에서 되찾는다(선언의 역방향 — 새 지식이 아니다).
  4. 의미가 사라지는 재분류는 하지 않는다(부정 flag·identity 필드 손실).
  5. 모호하면 조용히 고르지 않는다.
  6. 내부 타입 오류는 사용자 확인 요청이 아니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_plan  # noqa: E402
import semantic_retype  # noqa: E402


VOCABULARIES = {
    "aggregate_metric_id": ["order_count", "purchase_amount"],
    "cart_aggregate_metric": ["cart_amount", "cart_line_count"],
    "profile_metric_id": ["buy_cycle"],
    "campaign_frequency_event": ["campaign_response"],
    "member_metric_id": ["total_buy_amt"],
}

BINDINGS = {
    "aggregate_predicate.metric": {
        "discriminator": "scope",
        "by_value": {
            "cart": "cart_aggregate_metric",
            "order": "aggregate_metric_id",
            "campaign": "campaign_frequency_event",
            "profile": "profile_metric_id",
        },
    },
    "predicate.metric": {"default": "profile_metric_id"},
    "ranked_set.metric": {"default": "member_metric_id"},
}


def _normalize(payload):
    return semantic_retype.normalize_payloads(
        [payload], vocabularies=VOCABULARIES, bindings=BINDINGS
    )


# ── ① 스키마가 타입-필드 정합을 강제한다 ────────────────────────────────────────
def test_variant_schema_only_exposes_its_own_fields() -> None:
    schema = semantic_plan.semantic_node_json_schema()
    variants = {
        variant["properties"]["type"]["enum"][0]: variant for variant in schema["anyOf"]
    }
    assert set(variants) == set(semantic_plan.NODE_CLASS_BY_TYPE)
    membership = variants["entity_set_membership"]["properties"]
    # 집계 술어의 payload 필드가 소속 노드 스키마에 존재하면 안 된다 — 실측 오염의 경로다.
    for foreign in ("scope", "operator", "value", "aggregation"):
        assert foreign not in membership, foreign


def test_variant_requires_its_own_required_fields() -> None:
    schema = semantic_plan.semantic_node_json_schema()
    for variant in schema["anyOf"]:
        node_type = variant["properties"]["type"]["enum"][0]
        expected = semantic_plan.NODE_REQUIREMENTS[node_type]
        assert expected <= set(variant["required"]), node_type


def test_llm_tool_schema_keeps_required_fields_non_nullable() -> None:
    """strict 변환 후에도 필수 필드가 null 을 받으면 '전부 null 인 노드'가 다시 통과한다."""
    from query_structurer.campaign_plan_v4 import CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA

    node = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["$defs"]["semanticNode"]
    for variant in node["anyOf"]:
        node_type = variant["properties"]["type"]["enum"][0]
        for name in semantic_plan.NODE_REQUIREMENTS[node_type]:
            schema = variant["properties"][name]
            types = schema.get("type")
            listed = types if isinstance(types, list) else [types]
            assert "null" not in listed, f"{node_type}.{name} 이 null 을 허용한다"


# ── ② 결정론 재분류 ─────────────────────────────────────────────────────────────
def test_retypes_when_exactly_one_type_is_complete() -> None:
    nodes, outcomes = _normalize({
        "id": "req-1",
        "type": "entity_set_membership",
        "source_span": "2회 이상 구매한 회원",
        "subject": "회원", "metric": "order_count", "operator": ">=",
        "value": {"value": 2, "unit": "회"}, "scope": "order",
        "member_entity": None, "ranked_set": None, "relation": None,
    })
    assert [node["type"] for node in nodes] == ["aggregate_predicate"]
    assert outcomes[0].action == semantic_retype.ACTION_RETYPED
    assert outcomes[0].compatible_types == ("aggregate_predicate",)


# ── ③ discriminator 역인덱스 ────────────────────────────────────────────────────
def test_infers_discriminator_from_metric_value() -> None:
    nodes, outcomes = _normalize({
        "id": "req-1", "type": "aggregate_predicate", "source_span": "2회 이상 구매",
        "metric": "order_count", "operator": ">=", "value": 2, "scope": "profile",
    })
    assert nodes[0]["scope"] == "order"
    assert outcomes[0].repairs == {"scope": "order"}


def test_rejects_metric_outside_every_vocabulary() -> None:
    nodes, outcomes = _normalize({
        "id": "req-1", "type": "aggregate_predicate", "source_span": "총 구매건수 2회 이상",
        "metric": "total_buy_cnt", "operator": ">=", "value": 2, "scope": "profile",
    })
    assert nodes == []
    assert outcomes[0].action == semantic_retype.ACTION_UNRESOLVED
    assert "total_buy_cnt" in outcomes[0].reason


# ── ④ 의미 손실 차단 ────────────────────────────────────────────────────────────
def test_does_not_retype_when_negation_would_be_dropped() -> None:
    """부정을 버리면 조건이 뒤집힌다 — 재분류보다 재방출이 낫다."""
    nodes, outcomes = _normalize({
        "id": "req-1", "type": "entity_set_membership",
        "source_span": "2회 이상 구매하지 않은 회원",
        "metric": "order_count", "operator": ">=", "value": 2, "scope": "order",
        "negated": True,
    })
    assert nodes == []
    assert outcomes[0].action == semantic_retype.ACTION_UNRESOLVED


# ── ⑤ 모호성은 조용히 고르지 않는다 ─────────────────────────────────────────────
def test_ambiguous_when_two_types_tie() -> None:
    outcome = semantic_retype.normalize_node_payload(
        {"id": "req-1", "type": "unknown", "source_span": "x", "a": 1},
        bindings={},
        vocabularies={},
        all_declared=frozenset(),
    )[1]
    assert outcome.action in {
        semantic_retype.ACTION_UNRESOLVED, semantic_retype.ACTION_AMBIGUOUS
    }


# ── ⑥ 내부 오류는 사용자 확인 요청이 아니다 ─────────────────────────────────────
def test_type_mismatch_is_not_a_user_clarification() -> None:
    plan = semantic_plan.SemanticPlanV2(source_query="x")
    plan.structurer_issues = [{
        "node_id": "req-1", "source_span": "무언가", "proposed_type": "predicate",
        "reason": "어떤 타입도 완성되지 않았다",
    }]
    assert plan.failure_kind() == semantic_plan.FAILURE_KIND_STRUCTURER
    causes = {record["cause"] for record in plan.missing_field_records()}
    assert causes == {semantic_plan.CAUSE_TYPE_MISMATCH}


def test_empty_runtime_vocabulary_is_a_registry_gap_not_a_question() -> None:
    """어휘가 선언됐는데 실행 값이 하나도 없으면 생산자를 탓할 수 없다."""
    spec = semantic_plan.FieldSpec("scope", "scope", required=True, vocabulary="__missing__")

    class _Probe(semantic_plan.SemanticNode):
        TYPE = "probe"
        FIELDS = (spec,)

    node = _Probe(id="req-1", source_span="x")
    assert node.missing_cause("scope") == semantic_plan.CAUSE_REGISTRY_GAP


# ── 어휘 결속 드리프트 가드 ─────────────────────────────────────────────────────
def test_every_node_field_binding_points_at_a_live_vocabulary() -> None:
    """선언이 가리키는 어휘 키가 실행 레지스트리에 없으면 그 필드는 검증되지 않는다."""
    import graph_rag  # noqa: PLC0415 — 실행 레지스트리는 조립 지점에만 있다
    import targeting_domain  # noqa: PLC0415

    available = graph_rag._allowed_canonical_values()
    dangling: list[str] = []
    for path, spec in targeting_domain.node_field_vocabularies().items():
        if not isinstance(spec, dict):
            continue
        keys = [spec.get("default")] if spec.get("default") else list(
            (spec.get("by_value") or {}).values()
        )
        dangling += [f"{path} → {key}" for key in keys if key and key not in available]
    assert not dangling, f"실행 어휘에 없는 결속: {dangling}"


@pytest.mark.parametrize("node_type", sorted(semantic_plan.NODE_CLASS_BY_TYPE))
def test_node_type_guidance_documents_every_type(node_type: str) -> None:
    """프롬프트 안내는 선언에서 생성된다 — 새 노드 타입이 조용히 빠질 수 없다."""
    guidance = semantic_plan.node_type_guidance()
    assert node_type in guidance
    for required in semantic_plan.NODE_REQUIREMENTS[node_type]:
        assert required in guidance
