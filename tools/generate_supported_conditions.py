"""지원 조건 표 자동 생성(플랜 A-4) — docs/generated/supported_conditions.md.

손으로 쓰는 지원 조건 표는 만들자마자 드리프트한다(저장소에 표 자체가 없었고, 유일한 사용자
대면 힌트는 lapsed_buyer 미지원과 자가모순이었다). 이 도구는 표를 **실행 자산에서 파생**한다:
targeting_ir(호환 슬롯·라벨·각주), V4 단일 audience_requirement 노출면, member_target_filters.json(behaviors),
requirement_capabilities.json(base×qualifier).

재생성:  python tools/generate_supported_conditions.py
최신성:  tests/test_supported_conditions_doc.py 가 재생성 결과와 파일의 일치를 CI 에서 강제한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUTPUT_PATH = REPO_ROOT / "docs" / "generated" / "supported_conditions.md"
FILTERS_PATH = REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json"
CAPABILITIES_PATH = (
    REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "requirement_capabilities.json"
)


def retired_slot_names() -> frozenset[str]:
    """생산자가 폐기돼 **아무도 채울 수 없는** 슬롯 이름.

    이 목록의 단일 소유자는 `graph_rag._RETIRED_COMPILER_OWNED_SLOTS` 다(사설 이름이지만
    tests/test_single_interpretation_path.py 도 같은 경로로 읽는다). 여기에 목록을 두 벌로
    적으면 그 순간 갈라지고, 실행이 드롭하는 슬롯을 문서가 지원으로 광고하게 된다 —
    이 표가 막으려던 바로 그 사고다. 컨테이너 접두어(`target_user.`)는 떼고 슬롯 이름만 쓴다.
    """
    import graph_rag

    return frozenset(
        slot.rpartition(".")[2] for slot in graph_rag._RETIRED_COMPILER_OWNED_SLOTS
    )


# 생산자가 폐기된 슬롯의 미지원 사유(§1 비고). 표기 관례는 `lapsed_buyer`(§2) 와 같다 —
# 행을 지우지 않고 **미지원**으로 낮춘다. 슬롯 선언(SLOT_SHAPES)은 코드에 그대로 살아 있어서
# 행을 지우면 "표에 없다 = 아직 안 훑어봤다"로 읽히지만, 실제로는 "선언은 있는데 채우는
# 경로가 없다"이기 때문이다(RETIRED_SLOT_REASON 이 그 차이를 그대로 적는다).
RETIRED_SLOT_REASON = "선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다"


def render() -> str:
    import targeting_ir
    from query_structurer.campaign_plan_v4 import (
        CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
        _PLAN_SLOT_EXPOSURE_EXCLUSIONS,
    )

    retired = retired_slot_names()

    filters = json.loads(FILTERS_PATH.read_text(encoding="utf-8"))
    capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))["capabilities"]

    exposed_plan_root = set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"])
    assert "audience_requirement" in exposed_plan_root
    exposed_target_user: set[str] = set()

    lines: list[str] = []
    out = lines.append
    out("# 지원 타겟 조건 표")
    out("")
    out("> **자동 생성 문서 — 손으로 편집하지 마라.** LLM은 고정 Event IR 대수인")
    out("> `audience_requirement`만 만들며 아래 호환 슬롯을 직접 만들지 않는다.")
    out("> 표는 실행 자산(targeting_ir·V4 스키마·폐기 슬롯 목록·")
    out("> member_target_filters.json·requirement_capabilities.json)에서 파생되며,")
    out("> `python tools/generate_supported_conditions.py` 로 재생성한다.")
    out("> 최신성은 tests/test_supported_conditions_doc.py 가 CI 에서 강제한다.")
    out("")

    out("## 1. 호환 실행 슬롯 조건")
    out("")
    out("| 슬롯 | 라벨 | 컨테이너 | 지원 | LLM 직접 노출 | 비고 |")
    out("|---|---|---|---|---|---|")
    for name, shape in targeting_ir.SLOT_SHAPES.items():
        label = targeting_ir.SLOT_KO_LABELS[name]
        if name in retired:
            # 생산자가 없으므로 노출 여부와 무관하게 미지원이다. LLM 후보에 실려 와도
            # `_coerce_llm_structured_conditions` 가 드롭한다.
            out(f"| `{name}` | {label} | {shape.container} | **미지원** | X(후보 드롭) | "
                f"{RETIRED_SLOT_REASON}. |")
            continue
        if shape.container == "target_user":
            exposed = "O" if name in exposed_target_user else "X"
        else:
            exposed = "O" if name in exposed_plan_root else (
                "X(제외 선언)" if name in _PLAN_SLOT_EXPOSURE_EXCLUSIONS else "X"
            )
        note = targeting_ir.SLOT_SUPPORT_NOTES.get(name, "")
        out(f"| `{name}` | {label} | {shape.container} | 지원 | {exposed} | {note} |")
    out("")

    out("## 2. 주문 행동(behaviors)")
    out("")
    out("| 행동 | 지원 | 비고 |")
    out("|---|---|---|")
    behaviors = filters["order_count_targets"]["behaviors"]
    for name, spec in behaviors.items():
        supported = spec.get("_supported", True)
        if supported:
            note = ", ".join(spec.get("synonyms", []))
            out(f"| `{name}` | 지원 | {note} |")
        else:
            reason = str(spec.get("_unsupported_reason", "")).split(".")[0]
            out(f"| `{name}` | **미지원** | {reason}. |")
    out("")

    out("## 3. 조건×수식어(base×qualifier)")
    out("")
    out("| base | qualifier | 지원 | 안내 |")
    out("|---|---|---|---|")
    for base, base_spec in capabilities.items():
        label = base_spec.get("label", base)
        for qualifier, spec in base_spec.get("qualifiers", {}).items():
            supported = "지원" if spec.get("supported") else "**미지원**"
            message = spec.get("message", "")
            out(f"| {label}(`{base}`) | {qualifier} | {supported} | {message} |")
    out("")

    out("## 4. 특수 조건부 지원")
    out("")
    # 동시구매(condition_evaluation) 문단은 2026-08-05 삭제됐다 — 그 IR 을 만드는 생산자가
    # 축1 폐기와 함께 사라져 "조건부로 지원한다"가 거짓 광고였다(요청은 ingress 에서 막힌다).
    # 같은 이유로 생산자가 폐기된 슬롯의 각주도 여기 싣지 않는다 — "조건부로는 된다"는 말은
    # 채우는 경로가 있을 때만 참이다(해당 슬롯은 §1 에서 미지원으로 내려간다).
    reachable_notes = [
        (name, note)
        for name, note in targeting_ir.SLOT_SUPPORT_NOTES.items()
        if name not in retired
    ]
    if reachable_notes:
        for name, note in reachable_notes:
            out(f"- **{targeting_ir.SLOT_KO_LABELS[name]}(`{name}`)**: {note}.")
    else:
        out("- 조건부로 지원되는 슬롯이 현재 없다 — 선언된 각주는 모두 생산자가 폐기된 "
            "슬롯의 것이라 §1 에서 미지원으로 내렸다.")
    out("")
    return "\n".join(lines)


def main() -> None:
    content = render()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"generated: {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(content)} chars)")


if __name__ == "__main__":
    main()
