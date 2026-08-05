"""지원 조건 표 최신성 계약(플랜 A-4) — 자동 생성 문서와 실행 자산의 일치를 강제한다.

이 표는 저장소에 처음 생긴 사용자 대면 지원 조건 문서다. 손 갱신 표는 만들자마자
드리프트하므로(supported_condition_hint 가 lapsed_buyer 미지원과 자가모순이던 전례),
문서는 tools/generate_supported_conditions.py 파생으로만 존재하고 여기서 재생성 결과와
파일 내용의 바이트 일치를 강제한다 — 슬롯/라벨/behaviors/capability 가 바뀌면 이 테스트가
빨개지고, 고치는 방법은 재생성 한 줄이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import targeting_ir  # noqa: E402

import generate_supported_conditions as gen  # noqa: E402


def _slot_row(content: str, slot: str) -> str:
    """§1 표에서 이 슬롯의 행 한 줄(없으면 실패)."""
    prefix = f"| `{slot}` |"
    rows = [line for line in content.splitlines() if line.startswith(prefix)]
    assert len(rows) == 1, f"슬롯 {slot} 행이 표에 {len(rows)}개다(1개여야 한다)."
    return rows[0]


def test_generated_doc_is_fresh() -> None:
    expected = gen.render()
    assert gen.OUTPUT_PATH.is_file(), (
        f"{gen.OUTPUT_PATH} 가 없다 — python tools/generate_supported_conditions.py 로 생성하라."
    )
    actual = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "지원 조건 표가 실행 자산과 어긋났다. 재생성하라: "
        "python tools/generate_supported_conditions.py"
    )


def test_doc_declares_conditional_supports() -> None:
    """조건부 지원·미지원 선언이 표에 실재하는지 — 공허한 표 방지.

    2026-08-05 이전에는 `metric_trend` 조건부 지원 각주의 실재만 봤다. 그 각주는 이제
    거짓이다 — 슬롯의 생산자가 폐기돼 아무도 채울 수 없다(§1 에서 미지원으로 내려갔다).
    그래서 슬롯 이름을 고정하는 대신 **도달 가능한 각주 집합**을 실행 자산에서 파생해
    §4 와 대조한다: 각주가 되살아나면 문서에도 반드시 나타나야 하고, 비었으면 비었다고
    적혀 있어야 한다(침묵한 빈 절 금지).
    """
    content = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    retired = gen.retired_slot_names()
    reachable = {
        name: note
        for name, note in targeting_ir.SLOT_SUPPORT_NOTES.items()
        if name not in retired
    }
    for name, note in reachable.items():
        assert f"(`{name}`)**: {note}." in content, f"조건부 지원 각주가 표에서 빠졌다: {name}"
    if not reachable:
        assert "조건부로 지원되는 슬롯이 현재 없다" in content, "빈 §4 가 아무 말도 하지 않는다"
    assert "lapsed_buyer" in content and "미지원" in content, "lapsed_buyer 미지원 명시 누락"


def test_retired_slots_are_never_listed_as_supported() -> None:
    """생산자가 폐기된 슬롯은 §1 에서 지원으로 표기되지 않는다.

    `graph_rag._RETIRED_COMPILER_OWNED_SLOTS` 슬롯은 LLM 후보에서 드롭되고 rules 레인도
    채우지 않으므로 생산자가 0이다. 그런데도 표가 '지원'으로 실으면 §2 의 `lapsed_buyer`
    미지원 표기와 자가모순이 된다(이 문서가 애초에 없애려던 그 모순).
    """
    content = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    retired = gen.retired_slot_names() & set(targeting_ir.SLOT_SHAPES)
    assert retired, (
        "폐기 슬롯 파생원이 비었다 — graph_rag._RETIRED_COMPILER_OWNED_SLOTS 를 확인하라."
    )
    for slot in sorted(retired):
        row = _slot_row(content, slot)
        assert "**미지원**" in row, f"생산자가 폐기된 슬롯이 지원으로 광고된다: {slot}\n{row}"
        label = targeting_ir.SLOT_KO_LABELS[slot]
        assert f"- **{label}(`{slot}`)**" not in content, (
            f"생산자가 폐기된 슬롯이 §4 조건부 지원으로 남아 있다: {slot}"
        )


def test_doc_never_advertises_a_retired_axis() -> None:
    """폐기 축을 '조건부 지원'으로 광고하지 않는다.

    2026-08-05 이전 표는 동시구매를 `same_product_same_order_quantity_v1` 서명으로 조건부
    지원한다고 적었지만, 그 IR 을 만드는 생산자는 이미 없었다(요청은 ingress 에서 막힌다).
    등급/상태 시점·이력(`relational_operation`)도 같은 이유로 표에서 사라졌다.
    """
    content = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    for retired in ("same_product_same_order_quantity_v1", "relational_operation"):
        assert retired not in content, f"폐기된 축이 지원 조건 표에 남아 있다: {retired}"
