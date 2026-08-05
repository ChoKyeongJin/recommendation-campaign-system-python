"""plan 키 분류 레지스트리가 '13번째 사본'이 되지 않게 지킨다.

plan_schema 는 분류의 단일 소유자로 신설됐다. 위험은 명백하다 — 소비자를 전환하지 않으면
레지스트리 자체가 또 하나의 목록이 될 뿐이고, 그러면 드리프트 지점이 하나 늘어난다.
그래서 여기서는 (1) 선언 자체의 무결성과 (2) 소비자가 실제로 파생하고 있는지를 함께 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import ir_snapshot  # noqa: E402
import plan_schema  # noqa: E402
import semantic_requirements  # noqa: E402


def test_registry_is_pure() -> None:
    """graph_rag 를 끌어오면 순수 모듈 규약이 깨지고 테스트 비용이 모놀리스에 묶인다."""

    source = (REPO_ROOT / "plan_schema.py").read_text(encoding="utf-8")
    assert "import graph_rag" not in source


def test_names_are_unique_across_kinds() -> None:
    """같은 키가 두 분류에 있으면 어느 쪽이 맞는지 코드가 답할 수 없다."""

    condition = plan_schema.names(plan_schema.CONDITION)
    derived = plan_schema.names(plan_schema.DERIVED)
    assert not (condition & derived), f"두 분류에 동시에 선언된 키: {sorted(condition & derived)}"


def test_every_declaration_has_a_note() -> None:
    """분류만 있고 설명이 없으면 다음 사람이 경계 판단을 못 한다."""

    missing = [key.name for key in plan_schema.ALL if not key.note.strip()]
    assert not missing, f"설명 없는 선언: {missing}"


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_schema.PlanKey(name="x", kind="something_else")

    with pytest.raises(ValueError):
        plan_schema.names("something_else")


def test_ir_snapshot_derives_from_the_registry() -> None:
    """소비자가 리터럴 사본으로 되돌아가면 이 테스트가 잡는다."""

    assert ir_snapshot.KNOWN_CONDITION_PLAN_KEYS == plan_schema.names(plan_schema.CONDITION)
    assert ir_snapshot.DERIVED_PLAN_KEYS == plan_schema.names(plan_schema.DERIVED)

    source = (REPO_ROOT / "ir_snapshot.py").read_text(encoding="utf-8")
    assert "plan_schema.names(" in source, "ir_snapshot 이 다시 리터럴 목록을 들고 있다."


def test_requirement_slots_are_declared_conditions() -> None:
    """요구 원장이 담는 슬롯은 '사용자가 말한 것'이어야 한다 — 파생을 담으면 원장이 거짓이 된다."""

    derived = plan_schema.names(plan_schema.DERIVED)
    contradictions = sorted(set(semantic_requirements._PLAN_REQUIREMENT_SLOTS) & derived)
    assert not contradictions, f"요구 원장이 파생 키를 담고 있다: {contradictions}"


# ── 오디언스 표면(Phase 3-1) ────────────────────────────────────────────────────────
#
# "무엇이 두 번째 오디언스 언어인가"의 답이 저장소에 하나여야 한다. 이 절이 없던 동안 같은
# 목록이 canonical_event_ir_grounding 과 plan_validation 에 각각 리터럴로 있었다.


def test_condition_keys_must_declare_whether_they_are_audience() -> None:
    """CONDITION 키에서 audience 를 빠뜨리면 그 키가 표면에서 조용히 빠진다."""

    with pytest.raises(ValueError, match="audience"):
        plan_schema.PlanKey(name="x", kind=plan_schema.CONDITION, note="n")


def test_derived_keys_cannot_claim_to_be_audience() -> None:
    """파생물은 사용자가 말한 조건이 아니므로 오디언스 언어일 수 없다."""

    with pytest.raises(ValueError, match="CONDITION"):
        plan_schema.PlanKey(
            name="x", kind=plan_schema.DERIVED, note="n", audience=False
        )


def test_audience_surface_is_exactly_todays_gate_surface() -> None:
    """표면 확대는 **결정**이지 리팩터링이 아니다(docs/plans_event_ir_only.md §6-1).

    넓히면 그 키를 가진 canonical 요청이 fail-close 되는데, 그 축은 Event IR 로 표현할 수도
    없어서 기능이 그대로 줄어든다. 그래서 이 집합은 얼려 둔다 — 바꾸려면 이 테스트를 함께
    고쳐야 하고, 그 편집이 곧 결정의 기록이다.
    """

    assert plan_schema.AUDIENCE_CONTAINERS == ("target_user", "exclude")
    assert plan_schema.audience_keys() == frozenset({
        "set_expressions",
        "computed_metrics",
        "external_conditions",
        "compound_dimension_filters",
        "condition_evaluations",
    })


@pytest.mark.parametrize(
    ("name", "why"),
    [
        (
            "unresolved_source_conditions",
            "Event IR 빌더 자신이 쓰는 좌표다 — True 면 canonical 실패가 자기참조 충돌이 된다.",
        ),
    ],
)
def test_self_referential_keys_are_never_audience(name: str, why: str) -> None:
    key = next(item for item in plan_schema.ALL if item.name == name)
    assert key.audience is False, f"{name} 은 audience=False 여야 한다: {why}"


@pytest.mark.parametrize(
    "name",
    [
        # 2026-08-05 폐기. 되살아나면 "두 번째 오디언스 해석 계층"이 계약에 다시 선언된다.
        "semantic_plan",
        # 그 계층의 컴파일 산출물이었다 — 생산자가 없는데 파생 키로 남아 있으면 스냅샷·감사가
        # "있다가 비었다"를 계속 구분하려 든다.
        "semantic_pipeline",
        "requirements",
    ],
)
def test_retired_semantic_plan_keys_are_not_declared(name: str) -> None:
    assert name not in {key.name for key in plan_schema.ALL}, (
        f"{name} 이 plan 계약에 되살아났다 — 그 키의 생산자는 2026-08-05 폐기됐다."
    )


def test_grounding_predicate_derives_its_surface_from_the_registry() -> None:
    """소비자가 리터럴 사본으로 되돌아가면 이 테스트가 잡는다."""

    import canonical_event_ir_grounding as grounding

    source = (REPO_ROOT / "canonical_event_ir_grounding.py").read_text(encoding="utf-8")
    assert "plan_schema.audience_keys()" in source, (
        "canonical_event_ir_grounding 이 다시 표면 목록을 리터럴로 들고 있다."
    )

    # 선언된 표면이 실제로 술어를 움직인다(공허한 파생 방지).
    for name in (*plan_schema.AUDIENCE_CONTAINERS, *plan_schema.audience_keys()):
        assert grounding.has_empty_legacy_audience_surface({}) is True
        assert grounding.has_empty_legacy_audience_surface({name: {"any": "value"}}) is False, (
            f"{name} 이 채워졌는데 표면이 비었다고 답한다."
        )


