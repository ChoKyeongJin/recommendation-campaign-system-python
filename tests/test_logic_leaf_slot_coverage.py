"""OR 논리식 Leaf 게이트가 모든 조건 슬롯을 보는지 지킨다.

`_build_logical_leaf` 는 컴파일하지 못한 조건이 남아 있으면 전체 논리식을 실패시킨다(fail-close).
그런데 그 검사가 `_LOGIC_CONDITION_SLOTS` **집합 안의 슬롯만** 훑기 때문에, 집합에 없는 슬롯은
값이 채워져 있어도 leftover 로 잡히지 않는다. 즉 게이트 누락은 곧 fail-OPEN 이고, 증상은
"조건이 SQL 에 반영되지 않은 채 조용히 사라짐"이다 — 이 저장소가 가장 피하려는 실패 형태다.

실제로 cart_absence 와 metric_trend 가 그 상태였다(SLOT_SHAPES 에는 있는데 게이트에는 없음).
그래서 게이트를 손 나열이 아니라 레지스트리 파생으로 바꿨고, 이 파일이 그 파생을 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import targeting_ir  # noqa: E402


def _structured_target_user_slots() -> set[str]:
    return {
        name
        for name, shape in targeting_ir.SLOT_SHAPES.items()
        if getattr(shape, "container", None) == "target_user"
    }


def test_gate_covers_every_structured_target_user_slot() -> None:
    """SLOT_SHAPES 에 슬롯을 추가하면 게이트에 자동 편입돼야 한다."""

    missing = sorted(_structured_target_user_slots() - graph_rag._LOGIC_CONDITION_SLOTS)
    assert not missing, (
        f"논리식 게이트가 모르는 구조화 슬롯: {missing}. "
        "이 슬롯이 채워진 OR 질의는 fail-close 를 그냥 통과해 조건이 조용히 사라진다."
    )


def test_gate_covers_every_slot_the_rule_parser_initialises() -> None:
    """규칙 파서가 만드는 target_user 슬롯 중 게이트 밖에 있는 것을 드러낸다.

    값 슬롯이 아닌 것(파생 메타 등)은 아래 화이트리스트로 명시 제외한다 — 조용히 빠지는 것과
    '이건 조건이 아니다'라고 선언하는 것은 다르다.
    """

    # 조건이 아닌 target_user 키(파생·메타·해석 부산물). 여기 추가는 곧 '조건 아님' 선언이다.
    NON_CONDITION_KEYS = {
        "purchase_membership",  # purchase_date/purchase_object 에서 파생되는 멤버십 표현
        "entity_set_condition",  # 집합식 파서 소유(별도 경로에서 clarification 판정)
    }

    plan = graph_rag.build_query_plan("20대 여성 고객", parser="rules")
    initialised = set(plan.get("target_user") or {})
    uncovered = sorted(initialised - graph_rag._LOGIC_CONDITION_SLOTS - NON_CONDITION_KEYS)
    assert not uncovered, (
        f"규칙 파서가 만드는데 게이트가 모르는 슬롯: {uncovered}. "
        "조건이면 게이트에 넣고, 조건이 아니면 NON_CONDITION_KEYS 에 사유와 함께 선언하라."
    )


def test_rule_only_list_holds_no_structured_slot() -> None:
    """손 나열 목록이 레지스트리와 겹치기 시작하면 파생의 의미가 사라진다(이중 소유 재발)."""

    overlap = sorted(graph_rag._LOGIC_RULE_ONLY_CONDITION_SLOTS & _structured_target_user_slots())
    assert not overlap, (
        f"규칙 전용 목록에 구조화 슬롯이 들어 있다: {overlap}. "
        "SLOT_SHAPES 파생에 맡기고 목록에서 지워라."
    )


def test_handled_slots_are_a_subset_of_the_gate() -> None:
    """'컴파일할 줄 아는 슬롯'이 게이트 밖에 있으면 covered 계산이 어긋난다."""

    stray = sorted(graph_rag._LOGIC_HANDLED_SLOTS - graph_rag._LOGIC_CONDITION_SLOTS)
    assert not stray, f"게이트에 없는 처리 슬롯: {stray}"


def test_gate_did_not_shrink() -> None:
    """게이트가 줄어드는 방향의 변경은 곧 조건 소실 창구를 여는 것이다."""

    assert len(graph_rag._LOGIC_CONDITION_SLOTS) >= 29, (
        f"게이트 슬롯이 {len(graph_rag._LOGIC_CONDITION_SLOTS)}개로 줄었다. "
        "슬롯을 뺐다면 그 조건이 다른 곳에서 확실히 컴파일되는지 확인하고 이 하한을 조정하라."
    )
