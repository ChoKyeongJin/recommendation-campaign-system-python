"""슬롯 라벨 facet(플랜 A-2) 계약 — 라벨의 단일 소유와 파생 완전성을 고정한다.

배경: '남는 조건' 안내 라벨이 graph_rag 손 목록(42개)이던 시절, 슬롯 6종의 라벨이 빠져
해당 조건이 안내에서 침묵 삭제됐다(W2-2 가 수리). 손 목록은 언제든 다시 벌어질 수 있으므로
슬롯 라벨을 targeting_ir.SLOT_KO_LABELS 로 옮겨 SLOT_SHAPES 와 키 동등을 강제하고,
graph_rag 는 파생 병합만 한다. 이 파일은 그 구조가 되돌아가지 않게 지킨다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import targeting_ir  # noqa: E402


def test_every_slot_has_a_nonempty_ko_label() -> None:
    """새 슬롯 = SLOT_SHAPES 한 항목 + 라벨 한 줄. 라벨 누락은 임포트 assert 이전에 여기서도 잡는다."""
    missing = {
        name for name in targeting_ir.SLOT_SHAPES
        if not str(targeting_ir.SLOT_KO_LABELS.get(name, "")).strip()
    }
    assert not missing, f"ko_label 없는 슬롯: {sorted(missing)}"


def test_label_keys_match_slot_shapes_exactly() -> None:
    """스테일 라벨(슬롯이 사라졌는데 라벨만 남음)도 드리프트다 — 양방향 동등."""
    assert set(targeting_ir.SLOT_KO_LABELS) == set(targeting_ir.SLOT_SHAPES)


def test_support_notes_reference_real_slots() -> None:
    assert set(targeting_ir.SLOT_SUPPORT_NOTES) <= set(targeting_ir.SLOT_SHAPES)


def test_graph_rag_labels_are_derived_not_duplicated() -> None:
    """graph_rag 안내 라벨: 슬롯 경로는 전부 SLOT_KO_LABELS 파생값이고, 잔여 목록이 슬롯을 그림자 소유하지 않는다."""
    import graph_rag  # noqa: PLC0415

    for name, shape in targeting_ir.SLOT_SHAPES.items():
        if shape.container != "target_user":
            continue
        path = f"target_user.{name}"
        assert graph_rag._UNSUPPORTED_CONDITION_LABELS.get(path) == targeting_ir.SLOT_KO_LABELS[name], (
            f"{path} 라벨이 SLOT_KO_LABELS 파생값과 다르다 — 이중 소유가 재발했다."
        )
        assert path not in graph_rag._RESIDUAL_CONDITION_LABELS, (
            f"{path} 가 잔여 목록에 있다 — 슬롯 라벨은 targeting_ir 단일 소유다."
        )


def test_hint_fallback_is_derived_and_nonempty() -> None:
    """힌트 폴백은 라벨 파생이라 스테일 불가 — 손 목록 시대(7항목 고정 문자열) 재발 방지."""
    import graph_rag  # noqa: PLC0415

    derived = graph_rag._derived_supported_condition_hint()
    assert derived.strip(), "파생 폴백이 비어 있다."
    assert "장바구니 보관 기간" in derived, "슬롯 라벨이 폴백 힌트에 반영되지 않는다."
    assert graph_rag._supported_condition_hint().strip(), "런타임 힌트가 비어 있다."
