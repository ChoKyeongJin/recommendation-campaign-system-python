"""'나머지 조건만으로 조회할까요?' 안내에서 조건이 침묵 삭제되지 않는지 지킨다.

미지원 조건이 섞인 질의에서 시스템은 "이 조건은 못 만들지만 남은 조건으로 조회할까요?"를 묻는다.
그 안내는 남은 조건 목록이 정확할 때만 의미가 있다. 예전에는 라벨 사전에 없는 슬롯이 목록에서
통째로 빠져서, 살아 있는 조건을 사용자가 못 보는 상태가 됐다(장바구니 보관 기간, 캠페인 반응 횟수
등 6종). 사용자는 "그 조건은 무시됐나?"를 알 방법이 없었다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import targeting_ir  # noqa: E402


def _labelled_target_user_slots() -> set[str]:
    prefix = "target_user."
    return {
        key[len(prefix) :]
        for key in graph_rag._UNSUPPORTED_CONDITION_LABELS
        if key.startswith(prefix)
    }


def test_every_condition_slot_has_a_specific_label() -> None:
    """폴백은 안전망이지 목표가 아니다 — 조건마다 사람이 읽을 이름이 있어야 한다."""

    missing = sorted(graph_rag._LOGIC_CONDITION_SLOTS - _labelled_target_user_slots())
    assert not missing, (
        f"사용자 안내 라벨이 없는 조건 슬롯: {missing}. "
        "_UNSUPPORTED_CONDITION_LABELS 에 한국어 라벨을 추가하라."
    )


def test_every_structured_slot_has_a_label() -> None:
    structured = {
        name
        for name, shape in targeting_ir.SLOT_SHAPES.items()
        if getattr(shape, "container", None) == "target_user"
    }
    missing = sorted(structured - _labelled_target_user_slots())
    assert not missing, f"라벨 없는 구조화 슬롯: {missing}"


def test_unknown_slot_falls_back_instead_of_vanishing() -> None:
    """라벨을 깜빡한 새 슬롯이 안내에서 사라지지 않고 최소한 보이기는 해야 한다."""

    plan = {"target_user": {"gender": "female"}}
    # 게이트에는 있고 라벨에는 없는 상황을 흉내낸다.
    slot = next(iter(graph_rag._LOGIC_CONDITION_SLOTS))
    plan["target_user"][slot] = ["임의값"]

    labels = graph_rag._remaining_condition_labels(plan, [])
    assert labels, "남은 조건이 있는데 안내 목록이 비었다."


def test_labels_do_not_leak_internal_slot_names() -> None:
    """폴백 문구가 내부 이름을 그대로 노출하면 사용자에게 의미가 없다."""

    plan = {"target_user": {"cart_retention": {"min_days": 30}}}
    labels = graph_rag._remaining_condition_labels(plan, [])
    assert labels == ["장바구니 보관 기간 조건"], labels
    assert not any("_" in label for label in labels), f"내부 식별자가 샜다: {labels}"


def test_non_condition_keys_are_not_advertised() -> None:
    """조건이 아닌 키(파생 메타 등)까지 '남는 조건'으로 세면 안내가 거짓이 된다."""

    plan = {"target_user": {"purchase_membership": {"domain": "purchase"}}}
    labels = graph_rag._remaining_condition_labels(plan, [])
    assert labels == [], f"조건이 아닌 키가 안내에 실렸다: {labels}"


def test_aggregate_conditions_stay_excluded() -> None:
    """집계 조건은 별도 경로가 안내하므로 여기서 중복 노출하지 않는다(기존 계약)."""

    plan = {"target_user": {"aggregate_conditions": [{"metric_id": "order_count"}]}}
    assert graph_rag._remaining_condition_labels(plan, []) == []
