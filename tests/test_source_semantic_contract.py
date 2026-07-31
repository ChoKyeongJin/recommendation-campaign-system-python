from __future__ import annotations

import networkx as nx

import graph_rag
import semantic_requirements


QUERY = (
    "최근 6개월 동안 매월 한 번 이상 구매했고 지정한 세 개 브랜드를 모두 구매했으며, "
    "회원별 최신 월 CRM 스냅샷에서 골드 이상인 회원을 찾아줘."
)


def test_lossy_operators_are_captured_before_execution_slots() -> None:
    requirements = semantic_requirements.capture_source_semantic_obligations(QUERY)

    by_kind = {
        requirement.base["name"]: requirement
        for requirement in requirements
    }

    assert set(by_kind) == {
        "temporal_recurrence",
        "referenced_entity_set",
        "snapshot_selector",
    }
    assert by_kind["temporal_recurrence"].value == {
        "bucket": "month",
        "quantifier": "every",
        "surface": "매월",
    }
    assert by_kind["referenced_entity_set"].value == {
        "cardinality": 3,
        "entity_domain": "brand",
        "reference": "deictic",
        "set_quantifier": "all",
    }
    assert by_kind["snapshot_selector"].value == {
        "grain": "month",
        "partition_by": "member",
        "selector": "latest",
    }
    assert len(
        semantic_requirements.unresolved_semantic_obligations({}, QUERY)
    ) == 3


def test_plain_rolling_total_does_not_create_recurrence_obligation() -> None:
    requirements = semantic_requirements.capture_source_semantic_obligations(
        "최근 6개월 동안 총 1회 이상 구매한 회원"
    )

    assert requirements == ()
    assert semantic_requirements.capture_source_semantic_obligations(
        "최신 상품 이미지 스냅샷을 보여줘"
    ) == ()


def test_compiler_receipts_are_required_and_do_not_mutate_source_ledger() -> None:
    requirements = semantic_requirements.capture_source_semantic_obligations(QUERY)
    plan: dict = {}
    semantic_requirements.attach_source_requirements(plan, requirements)
    original_digest = plan["source_requirements_digest"]

    assert len(semantic_requirements.unresolved_semantic_obligations(plan)) == 3

    for requirement in requirements:
        semantic_requirements.record_source_requirement_receipt(
            plan,
            requirement_id=requirement.id,
            status="compiled",
            compiler="test_compiler",
            evidence={"node": requirement.base["name"]},
        )

    assert semantic_requirements.unresolved_semantic_obligations(plan) == []
    assert plan["source_requirements_digest"] == original_digest
    assert semantic_requirements.verify_source_requirements(plan) is True


def test_query_plan_blocks_before_sql_candidate_generation(
    monkeypatch,
) -> None:
    # This test exercises the deterministic path. LLM availability must not be
    # required for the source contract to fail closed.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plan = graph_rag.build_query_plan(QUERY, parser="rules")

    obligation_kinds = {
        item.get("semantic_kind")
        for item in plan["unresolved_source_conditions"]
        if item.get("source") == "source_semantic_contract"
    }
    assert obligation_kinds == {
        "temporal_recurrence",
        "referenced_entity_set",
        "snapshot_selector",
    }

    result = graph_rag.build_sql_result(
        nx.Graph(),
        QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        llm_model=None,
        original_query=QUERY,
    )

    assert result["sql"] is None
    assert result["candidate_count"] == 0
    assert result["failure_reason"] == "query_plan_required_conditions_missing"


def test_llm_member_flag_cannot_relabel_explicit_grade_as_premium(
    monkeypatch,
) -> None:
    query = "골드 이상인 회원"
    raw = [
        {
            "canonical": "premium_member",
            "polarity": "include",
            "evidence": query,
        }
    ]

    assert graph_rag._validated_member_flags(
        raw,
        graph_rag._attribute_token_canonicals(),
        query,
        {"matched_terms": []},
    ) == []

    plan = {
        "target_user": {"lifecycle": ["gold_grade", "vip"]},
        "exclude": {"lifecycle": []},
        "matched_terms": [],
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(graph_rag, "_condition_slot_llm_enabled", lambda: True)
    monkeypatch.setattr(
        graph_rag,
        "_llm_extract_condition_slots",
        lambda *_args, **_kwargs: {
            "member_flags": raw,
            "coupon_usage": None,
        },
    )

    graph_rag._apply_llm_condition_slot_fallback(query, plan)

    assert plan["target_user"]["lifecycle"] == ["gold_grade", "vip"]
