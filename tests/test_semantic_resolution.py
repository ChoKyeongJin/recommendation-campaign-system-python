from __future__ import annotations

import json

import pytest

import semantic_resolution


def _registry(requirements: list[dict]) -> dict:
    return {
        "version": 1,
        "structural_prefixes": ["target_user", "customer", "member", "plan"],
        "structural_suffixes": ["field", "value"],
        "dimension_operators": ["IN", "NOT_IN"],
        "top_level_plan_fields": [],
        "requirements": requirements,
    }


def test_default_semantic_resolution_registry_is_valid() -> None:
    registry = semantic_resolution.load_registry()

    assert registry["version"] == 1
    assert {item["id"] for item in registry["requirements"]} >= {
        "target_user.location",
        "metric_trend.baseline",
        "entity_ranking.purchase_date",
    }


def test_registry_extension_adds_alias_without_pipeline_code_change(tmp_path) -> None:
    registry_path = tmp_path / "semantic_resolution.json"
    registry_path.write_text(
        json.dumps(_registry([
            {
                "id": "target_user.location",
                "aliases": ["market_area"],
                "evidence": {"kind": "dimension_group", "group": "member_region"},
                "reason": "grounded_dimension_filter",
            }
        ])),
        encoding="utf-8",
    )
    plan = {
        "dimension_filters": [
            {
                "dimension_id": "member_value:SIDO",
                "prompt_label": "SIDO",
                "table": "CRM_MB_BASEINFO",
                "column": "CRM_MB_BASEINFO.SIDO",
                "operator": "NOT_IN",
                "codes": ["서울"],
            }
        ]
    }

    evidence = semantic_resolution.build_resolution_evidence(
        plan,
        compiled_entity_set=None,
        member_table="CRM_MB_BASEINFO",
        region_columns={"B.SIDO", "B.SIGUNGU"},
        registry_path=registry_path,
    )
    result = semantic_resolution.resolve_missing_field(
        "market_area", evidence, registry_path=registry_path,
    )

    assert result == {
        "field": "market_area",
        "resolved_by": "dimension_filters.member_value:SIDO",
        "reason": "grounded_dimension_filter",
    }


def test_comparison_period_aliases_do_not_collapse_to_numeric_suffixes() -> None:
    plan = {
        "target_user": {
            "metric_trend": {
                "baseline": {"from": "20260201", "to": "20260228"},
                "current": {"from": "20260301", "to": "20260331"},
            }
        }
    }
    evidence = semantic_resolution.build_resolution_evidence(
        plan,
        compiled_entity_set=None,
        member_table="CRM_MB_BASEINFO",
        region_columns={"B.SIDO", "B.SIGUNGU"},
    )

    first = semantic_resolution.resolve_missing_field("comparison_month_1", evidence)
    second = semantic_resolution.resolve_missing_field("comparison_month_2", evidence)

    assert first is not None and first["resolved_by"] == "target_user.metric_trend.baseline"
    assert second is not None and second["resolved_by"] == "target_user.metric_trend.current"


def test_unknown_requirement_remains_unresolved() -> None:
    evidence = semantic_resolution.build_resolution_evidence(
        {"target_user": {"gender": "female"}},
        compiled_entity_set=None,
        member_table="CRM_MB_BASEINFO",
        region_columns={"B.SIDO", "B.SIGUNGU"},
    )

    assert semantic_resolution.resolve_missing_field("unknown_required_input", evidence) is None


def test_duplicate_requirement_id_is_rejected(tmp_path) -> None:
    requirement = {
        "id": "target_user.location",
        "aliases": ["location"],
        "evidence": {"kind": "dimension_group", "group": "member_region"},
        "reason": "grounded_dimension_filter",
    }
    registry_path = tmp_path / "invalid_semantic_resolution.json"
    registry_path.write_text(
        json.dumps(_registry([requirement, requirement])), encoding="utf-8",
    )

    with pytest.raises(semantic_resolution.SemanticResolutionRegistryError, match="duplicated"):
        semantic_resolution.load_registry(registry_path)
