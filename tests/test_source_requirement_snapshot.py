"""최초 원문 요구 스냅샷의 불변성·출처·충돌 보존 계약."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

import graph_rag as g
import semantic_requirements as sr
from query_structurer import build_campaign_query_plan_v2_fallback


def test_capture_records_stable_id_exact_span_polarity_and_source() -> None:
    query = "여성 회원 중 무구매 회원, 블랙리스트는 제외"
    plan = {
        "target_user": {"gender": "female", "behaviors": ["no_purchase"]},
        "exclude": {"lifecycle": ["blacklisted"]},
        "campaign_constraints": {"marketing_opt_in": False},
        "matched_terms": [
            {"matched_text": "여성", "canonical": "female"},
            {"matched_text": "무구매", "canonical": "no_purchase"},
            {"matched_text": "블랙리스트", "canonical": "blacklisted"},
        ],
    }

    first = sr.capture_plan_source_requirements(query, plan, source="rules")
    second = sr.capture_plan_source_requirements(query, plan, source="rules")

    assert [item.id for item in first] == [item.id for item in second]
    by_path = {item.path: item for item in first}
    assert by_path["target_user.gender"].source_text == "여성"
    assert by_path["target_user.gender"].polarity == "positive"
    assert by_path["target_user.behaviors[0]"].polarity == "negative"
    assert by_path["exclude.lifecycle[0]"].polarity == "negative"
    assert by_path["campaign_constraints.marketing_opt_in"].polarity == "negative"
    assert all(item.source == "rules" for item in first)


def test_source_requirement_objects_and_nested_values_are_frozen() -> None:
    requirement = sr.capture_plan_source_requirements(
        "최근 구매금액 10만원 이상",
        {
            "target_user": {
                "aggregate_conditions": [
                    {
                        "metric_id": "purchase_amount",
                        "operator": ">=",
                        "threshold": 100000,
                        "source": "orders",
                    }
                ]
            },
            "exclude": {},
            "campaign_constraints": {},
        },
    )[0]

    with pytest.raises(FrozenInstanceError):
        requirement.polarity = "negative"  # type: ignore[misc]
    assert isinstance(requirement.value, Mapping)
    assert requirement.value == {
        "metric_id": "purchase_amount",
        "operator": ">=",
        "source": "orders",
        "threshold": 100000,
    }
    assert requirement.source == "rules"
    with pytest.raises(TypeError):
        requirement.value["threshold"] = 100


def test_attached_snapshot_detects_any_later_mutation() -> None:
    requirements = sr.capture_plan_source_requirements(
        "여성 회원", {"target_user": {"gender": "female"}, "exclude": {}, "campaign_constraints": {}}
    )
    plan: dict = {}
    sr.attach_source_requirements(plan, requirements)
    assert sr.verify_source_requirements(plan) is True

    plan["source_requirements"][0]["polarity"] = "negative"
    with pytest.raises(sr.SourceRequirementIntegrityError):
        sr.verify_source_requirements(plan)


def test_rules_and_llm_conflict_are_both_preserved_as_source_requirements() -> None:
    query = "여성 VIP 회원을 추출해줘"
    llm_plan = build_campaign_query_plan_v2_fallback(query)
    llm_plan["intent"] = "find_user_segment"
    llm_plan["target_user"] = {"gender": "male", "lifecycle": ["dormant"]}

    plan = g.build_query_plan(query, parser="rules", query_plan_v2=llm_plan)

    gender_requirements = [
        item for item in plan["source_requirements"] if item.get("path") == "target_user.gender"
    ]
    assert {(item["source"], item["value"]) for item in gender_requirements} == {
        ("rules", "female"),
        ("llm_query_structurer", "male"),
    }
    assert sr.verify_source_requirements(plan) is True


def test_llm_object_fallback_has_its_own_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fallback(query: str, plan: dict, **_: object) -> None:
        plan["target_user"]["purchase_object"] = "테스트상품"

    monkeypatch.setattr(g, "_apply_llm_object_fallback", fake_fallback)

    plan = g.build_query_plan("구매 고객", parser="rules")

    requirement = next(
        item
        for item in plan["source_requirements"]
        if item.get("path") == "target_user.purchase_object"
    )
    assert requirement["source"] == "llm_object_fallback"
    assert sr.verify_source_requirements(plan) is True


def test_entity_owner_can_remove_execution_slot_without_erasing_source_requirement() -> None:
    query = "2019년 가장 많이 팔린 상품 10개를 구매한 고객"

    plan = g.build_query_plan(query, parser="rules")

    assert not plan["target_user"].get("purchase_date")
    source_paths = {item.get("path") for item in plan["source_requirements"]}
    assert "target_user.purchase_date" in source_paths
    assert sr.verify_source_requirements(plan) is True
