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


