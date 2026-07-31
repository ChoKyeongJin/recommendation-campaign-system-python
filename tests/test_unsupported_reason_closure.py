"""미지원 사유 식별자를 닫힌 집합으로 유지한다.

`{"reason": "..."}` 문자열 중 일부는 단순 라벨이 아니라 **다른 모듈이 되받아 동작을 바꾸는 계약**이다:
targeting_ir 의 SlotShape 가 특정 사유를 '해소됨'으로 선언하고, 쿠폰 오버라이드 목록이 같은 문자열을
별도 상수로 재나열한다. 같은 사실이 세 곳에 손배선돼 있어 이름을 하나 바꾸면 게이트가 예외도 경고도
없이 동작을 멈춘다.

여기서는 세 방향을 함께 본다:
  (1) 소스가 내는 사유 ⊆ 선언 집합 (미선언 사유가 게이트를 지나치지 않게)
  (2) 소비자 집합 ⊆ 선언 집합 (오타가 죽은 배선이 되지 않게)
  (3) 선언 집합 ⊆ 소스가 내는 사유 (이름만 남은 죽은 선언이 쌓이지 않게)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import targeting_ir  # noqa: E402
import unsupported_reasons  # noqa: E402


def _produced_reasons() -> set[str]:
    """소스에서 {"reason": "<식별자>"} 로 생산되는 사유(문장형 메시지는 제외)."""

    tree = ast.parse((REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "reason"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                text = value.value
                if text.replace("_", "").isalnum() and text.islower():
                    found.add(text)
    return found


def _consumer_reasons() -> dict[str, set[str]]:
    slot_shape_reasons: set[str] = set()
    for shape in targeting_ir.SLOT_SHAPES.values():
        slot_shape_reasons |= set(getattr(shape, "resolves_unsupported", ()) or ())
    return {
        "targeting_ir.SlotShape.resolves_unsupported": slot_shape_reasons,
        "graph_rag._COUPON_OVERRIDABLE_REASONS": set(
            getattr(graph_rag, "_COUPON_OVERRIDABLE_REASONS", ()) or ()
        ),
    }


def test_declaration_is_not_empty() -> None:
    assert len(unsupported_reasons.ALL) > 20, "선언 집합이 비정상적으로 작다."


def test_every_produced_reason_is_declared() -> None:
    """새 사유를 만들면서 선언을 빠뜨리면, 그 사유를 보는 게이트가 아무 반응도 하지 않는다."""

    undeclared = sorted(_produced_reasons() - unsupported_reasons.ALL)
    assert not undeclared, (
        f"소스가 내는데 선언되지 않은 사유: {undeclared}. "
        "unsupported_reasons.ALL 에 추가하라."
    )


def test_no_dead_reason_declaration() -> None:
    """아무도 내지 않는 사유가 선언에 남아 있으면, 그걸 기다리는 게이트는 영영 안 켜진다."""

    dead = sorted(unsupported_reasons.ALL - _produced_reasons())
    assert not dead, f"선언돼 있으나 소스가 내지 않는 사유: {dead}"


def test_consumer_sets_are_subsets_of_the_declaration() -> None:
    """소비자가 오타를 들고 있으면 그 배선은 조용히 죽는다."""

    problems: list[str] = []
    for name, values in _consumer_reasons().items():
        unknown = sorted(values - unsupported_reasons.ALL)
        if unknown:
            problems.append(f"{name}: {unknown}")
    assert not problems, "선언에 없는 사유를 참조하는 소비자:\n  " + "\n  ".join(problems)


def test_consumer_sets_are_not_empty() -> None:
    """소비자 집합이 비면 이 계약이 아무것도 지키지 않는다(퇴화 방지)."""

    for name, values in _consumer_reasons().items():
        assert values, f"{name} 이 비었다."


def test_the_two_coupon_consumers_agree() -> None:
    """같은 사실을 두 상수가 들고 있다 — 갈라지면 한쪽 게이트만 동작한다."""

    consumers = _consumer_reasons()
    assert (
        consumers["targeting_ir.SlotShape.resolves_unsupported"]
        == consumers["graph_rag._COUPON_OVERRIDABLE_REASONS"]
    ), (
        "SlotShape 가 해소한다고 선언한 사유와 쿠폰 오버라이드 목록이 갈라졌다: "
        f"{sorted(consumers['targeting_ir.SlotShape.resolves_unsupported'])} vs "
        f"{sorted(consumers['graph_rag._COUPON_OVERRIDABLE_REASONS'])}"
    )
