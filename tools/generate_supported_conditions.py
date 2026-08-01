"""지원 조건 표 자동 생성(플랜 A-4) — docs/generated/supported_conditions.md.

손으로 쓰는 지원 조건 표는 만들자마자 드리프트한다(저장소에 표 자체가 없었고, 유일한 사용자
대면 힌트는 lapsed_buyer 미지원과 자가모순이었다). 이 도구는 표를 **실행 자산에서 파생**한다:
targeting_ir(슬롯·라벨·각주), V4 노출면, member_target_filters.json(behaviors),
requirement_capabilities.json(base×qualifier), condition_evaluation_ir(동시구매 서명).

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


def render() -> str:
    import condition_evaluation_ir
    import targeting_ir
    from query_structurer.campaign_plan_v4 import (
        CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
        _PLAN_SLOT_EXPOSURE_EXCLUSIONS,
    )

    filters = json.loads(FILTERS_PATH.read_text(encoding="utf-8"))
    capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))["capabilities"]

    exposed_target_user = set(
        CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]["target_user"]["properties"]
    )
    exposed_plan_root = set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"])

    lines: list[str] = []
    out = lines.append
    out("# 지원 타겟 조건 표")
    out("")
    out("> **자동 생성 문서 — 손으로 편집하지 마라.** 실행 자산(targeting_ir·V4 스키마·")
    out("> member_target_filters.json·requirement_capabilities.json)에서 파생되며,")
    out("> `python tools/generate_supported_conditions.py` 로 재생성한다.")
    out("> 최신성은 tests/test_supported_conditions_doc.py 가 CI 에서 강제한다.")
    out("")

    out("## 1. 구조화 슬롯 조건")
    out("")
    out("| 슬롯 | 라벨 | 컨테이너 | LLM 노출 | 조건부 지원 각주 |")
    out("|---|---|---|---|---|")
    for name, shape in targeting_ir.SLOT_SHAPES.items():
        label = targeting_ir.SLOT_KO_LABELS[name]
        if shape.container == "target_user":
            exposed = "O" if name in exposed_target_user else "X"
        else:
            exposed = "O" if name in exposed_plan_root else (
                "X(제외 선언)" if name in _PLAN_SLOT_EXPOSURE_EXCLUSIONS else "X"
            )
        note = targeting_ir.SLOT_SUPPORT_NOTES.get(name, "")
        out(f"| `{name}` | {label} | {shape.container} | {exposed} | {note} |")
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
    out(
        f"- **동시구매(condition_evaluation)**: 검증된 구성 서명 "
        f"`{condition_evaluation_ir.SAME_PRODUCT_CAPABILITY}` 만 컴파일한다 — 동일 주문 내 "
        "동일 상품 수량 집계 외의 조합(주문 횡단·상이 상품 등)은 fail-close 로 명시 차단된다."
    )
    for name, note in targeting_ir.SLOT_SUPPORT_NOTES.items():
        out(f"- **{targeting_ir.SLOT_KO_LABELS[name]}(`{name}`)**: {note}.")
    out("")
    return "\n".join(lines)


def main() -> None:
    content = render()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"generated: {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(content)} chars)")


if __name__ == "__main__":
    main()
