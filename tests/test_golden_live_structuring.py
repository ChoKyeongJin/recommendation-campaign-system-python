"""골든 코퍼스 라이브 구조화 — canonical audience contract 품질을 확인하는 옵트인 테스트.

LLM 입력면은 더 이상 ``target_user`` 같은 실행 슬롯을 노출하지 않는다. 각 프롬프트의 오디언스
의미는 하나의 ``audience_requirement`` 로 제출되고, 모델은 의미를 온전히 Event IR 로 표현하거나
표현할 수 없는 이유를 ``issues`` 로 명시해야 한다.

실행: ``GOLDEN_LIVE_LLM=1`` 과 ``OPENAI_API_KEY`` 가 모두 필요하다.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_ir  # noqa: E402
import ir_snapshot  # noqa: E402
from golden_support import load_cases  # noqa: E402

_LIVE = os.getenv("GOLDEN_LIVE_LLM") == "1" and bool(os.getenv("OPENAI_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _LIVE, reason="라이브 LLM 옵트인: GOLDEN_LIVE_LLM=1 + OPENAI_API_KEY 필요"
)

_CASES = load_cases()
_IDS = [case["id"] for case in _CASES]


@pytest.fixture(scope="module")
def live_plans() -> dict[str, dict]:
    """케이스당 한 번만 구조화한다(재시도는 structurer 내부 정책을 따른다)."""
    import graph_rag
    from query_structurer.types import StructuringContext

    context = StructuringContext(
        current_date=date.today().isoformat(),
        timezone=os.getenv("GRAPH_RAG_TIMEZONE"),
    )
    plans: dict[str, dict] = {}
    for case in _CASES:
        structured = graph_rag._structure_campaign_query_plan_v4(
            case["prompt"], context, graph_rag.DEFAULT_LLM_MODEL
        )
        payload = structured.to_dict() if hasattr(structured, "to_dict") else dict(structured)
        if payload.get("intent") == "unknown":
            plans[case["id"]] = {"_fallback": True}
            continue
        plans[case["id"]] = graph_rag.build_query_plan(
            case["prompt"], parser="llm", query_plan_v4=structured
        )
    return plans


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_live_structuring_returns_meaning_or_explicit_issue(
    case: dict, live_plans: dict[str, dict]
) -> None:
    plan = live_plans[case["id"]]
    if plan.get("_fallback"):
        pytest.skip("구조화가 폴백(intent=unknown)으로 떨어짐 — 키/네트워크를 확인하라")

    requirement = plan.get("audience_requirement")
    assert isinstance(requirement, dict), (
        f"[{case['id']}] audience_requirement 가 없다: {case['prompt']}"
    )
    issues = requirement.get("issues")
    assert isinstance(issues, list), (
        f"[{case['id']}] audience_requirement.issues 는 배열이어야 한다"
    )

    expression = requirement.get("expression")
    assert expression is not None or issues, (
        f"[{case['id']}] 의미도 issue도 없는 빈 오디언스 계약이다: {case['prompt']}"
    )
    if expression is not None:
        # 런타임과 LLM 스키마가 같은 고정 대수를 읽는지 라이브 응답으로도 확인한다.
        event_ir.condition_from_dict(expression)

    # 내부 호환 슬롯은 실행 권위가 아니지만, 금지한 오해가 결정론 단계에서 되살아나는지는 계속 감시한다.
    produced = ir_snapshot.condition_slots(plan)
    leaked = set(case.get("forbid_slots", [])) & produced
    assert not leaked, (
        f"[{case['id']}] 금지된 호환 슬롯이 생성됐다: {sorted(leaked)}\n"
        f"  실제 생성된 슬롯: {sorted(produced)}\n"
        f"  프롬프트: {case['prompt']}"
    )
