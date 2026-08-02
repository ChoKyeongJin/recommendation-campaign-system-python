"""골든 코퍼스와 canonical LLM 계약의 오프라인 드리프트 가드.

LLM 계약은 실행 슬롯 목록에서 파생되지 않는다. 루트는 항상 캠페인 메타데이터 세 필드와 단일
``audience_requirement`` 뿐이고, 실제 의미는 고정 Event IR 대수로 표현한다. 기존 ``target_user``
슬롯은 저장 플랜을 읽기 위한 내부 호환 스키마에만 남는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import plan_schema  # noqa: E402
import targeting_ir  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA,
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
)

from golden_support import load_cases  # noqa: E402

_LLM_ROOT = frozenset(
    {"intent", "campaign_constraints", "result_limit", "audience_requirement"}
)
_EXECUTION_OR_COMPATIBILITY_ROOT = frozenset(
    {
        "target_user",
        "exclude",
        "aggregation_request",
        "set_expressions",
        "computed_metrics",
        "external_conditions",
        "condition_evaluations",
        "member_metric_ranking",
        "semantic_evidence",
        "unresolved",
        "semantic_plan",
        "semantic_ir",
        "event_expression",
        "literal_bindings",
        "source_requirements",
    }
)


def _split_slot(slot: str) -> tuple[str, str]:
    container, _, name = slot.partition(".")
    return container, name


def _corpus_slots() -> set[str]:
    return {
        slot
        for case in load_cases()
        for key in ("expect_slots", "forbid_slots")
        for slot in case.get(key, [])
    }


def test_corpus_keeps_minimum_size() -> None:
    """표본이 줄면 계약이 공허해진다 — 케이스 삭제는 의도적 결정이어야 한다."""
    assert len(load_cases()) >= 19, (
        "골든 코퍼스가 19케이스 밑으로 줄었다. 삭제가 의도라면 하한도 함께 조정하라."
    )


def test_llm_root_is_the_fixed_canonical_envelope() -> None:
    schema = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA
    exposed = set(schema.get("properties") or {})
    assert exposed == _LLM_ROOT
    assert set(schema.get("required") or []) == _LLM_ROOT
    assert schema.get("additionalProperties") is False
    assert not (_EXECUTION_OR_COMPATIBILITY_ROOT & exposed)


def test_audience_requirement_is_the_only_llm_meaning_container() -> None:
    requirement = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"][
        "audience_requirement"
    ]
    assert requirement.get("type") == "object"
    assert requirement.get("additionalProperties") is False
    assert set(requirement.get("properties") or {}) == {"expression", "issues"}
    assert set(requirement.get("required") or []) == {"expression", "issues"}

    expression = requirement["properties"]["expression"]
    branches = expression.get("anyOf") or []
    assert any(branch.get("type") == "null" for branch in branches)
    assert any("anyOf" in branch for branch in branches), (
        "audience_requirement.expression 에 Event IR 조건 대수가 배선되지 않았다"
    )


def test_corpus_slot_names_remain_known_to_internal_compatibility_schema() -> None:
    """골든의 옛 슬롯 표기는 LLM 계약이 아니라 저장 플랜 호환 이름의 오타 가드로만 쓴다."""
    internal = CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA["properties"]
    target_user = set(internal["target_user"].get("properties") or {})
    plan_names = set(internal)
    for kind in plan_schema.KINDS:
        plan_names.update(plan_schema.names(kind))

    unknown: list[str] = []
    for slot in sorted(_corpus_slots()):
        container, name = _split_slot(slot)
        if container == "target_user" and name not in target_user:
            unknown.append(slot)
        elif container == "plan" and name not in plan_names:
            unknown.append(slot)
        elif container not in {"target_user", "plan"}:
            unknown.append(slot)
    assert not unknown, f"골든 코퍼스에 알려지지 않은 호환 슬롯 이름이 있다: {unknown}"


def test_internal_schema_retains_all_registered_legacy_target_user_slots() -> None:
    """저장된 구 플랜을 읽는 내부 스키마는 SLOT_SHAPES 호환면을 계속 보존한다."""
    internal_target_user = set(
        CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA["properties"]["target_user"].get(
            "properties"
        )
        or {}
    )
    registered = {
        shape.name
        for shape in targeting_ir.structured_slot_shapes()
        if shape.container == "target_user"
    }
    assert registered <= internal_target_user
    assert "target_user" not in CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]


def test_legacy_target_user_object_fragments_declare_properties() -> None:
    """내부 strict 변환에서도 빈 닫힌 객체가 되지 않도록 기존 조각의 구조를 검증한다."""
    undeclared = [
        shape.name
        for shape in targeting_ir.structured_slot_shapes()
        if shape.container == "target_user"
        and shape.schema.get("type") == "object"
        and not shape.schema.get("properties")
    ]
    assert not undeclared, (
        f"properties 없는 legacy object 슬롯 조각이 있다: {undeclared}"
    )
