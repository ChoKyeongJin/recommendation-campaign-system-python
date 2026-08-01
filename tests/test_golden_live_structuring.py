"""골든 코퍼스 라이브 구조화 — 실제 LLM V4 구조화 결과가 expect/forbid 슬롯을 만족하는지(옵트인).

배경: 실패 프롬프트 14종 역추적(2026-08-01)에서 최대 군집은 '스키마·빌더·어휘가 전부 지원하는데
LLM 구조화기가 슬롯을 미방출/오배선/환각'이었다(cart_aggregate 통째 누락, behaviors 중복 방출,
cart_abandoner 환각). CI 는 OPENAI_API_KEY 를 주입하지 않으므로 이 회귀는 오프라인 테스트로 잡을
수 없다 — 이 파일은 키가 있는 개발자 셸에서 명시적 옵트인으로만 돈다.

실행: GOLDEN_LIVE_LLM=1 + OPENAI_API_KEY 필요.
    GOLDEN_LIVE_LLM=1 python -m pytest tests/test_golden_live_structuring.py -q

판정 규약:
  - expect_slots 중 **LLM 노출 슬롯과 앱 소유(결정론 백필) 슬롯**만 필수로 단언한다.
    컴파일러 파생 슬롯(event_expression 등)은 plan 생성 시점 산물이 아니라 여기서 묻지 않는다
    (그 계층의 실재는 test_golden_slot_schema_contract 가 오프라인으로 가드한다).
  - forbid_slots 는 전 계층에서 단언한다 — 누출은 어느 계층이든 실패다.
  - 구조화가 폴백(intent=unknown)으로 떨어지면 채점하지 않고 스킵한다 — 폴백 플랜을 채점하면
    네트워크 장애가 의미 회귀로 위장된다.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import ir_snapshot  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    _APPLICATION_OWNED_PLAN_FIELDS,
)

from golden_support import load_cases  # noqa: E402

_LIVE = os.getenv("GOLDEN_LIVE_LLM") == "1" and bool(os.getenv("OPENAI_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _LIVE, reason="라이브 LLM 옵트인: GOLDEN_LIVE_LLM=1 + OPENAI_API_KEY 필요"
)

_EXPOSED_TARGET_USER = frozenset(
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]["target_user"]["properties"]
)
_EXPOSED_PLAN_ROOT = frozenset(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"])
_APP_OWNED_PLAN = frozenset(_APPLICATION_OWNED_PLAN_FIELDS)

_CASES = load_cases()
_IDS = [case["id"] for case in _CASES]


def _required_expects(case: dict) -> set[str]:
    required: set[str] = set()
    for slot in case.get("expect_slots", []):
        container, _, name = slot.partition(".")
        if container == "target_user" and name in _EXPOSED_TARGET_USER:
            required.add(slot)
        elif container == "plan" and (name in _EXPOSED_PLAN_ROOT or name in _APP_OWNED_PLAN):
            required.add(slot)
    return required


@pytest.fixture(scope="module")
def live_plans() -> dict[str, dict]:
    """케이스당 1회만 구조화한다(재시도 포함 최악 3호출/케이스)."""
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
        plan = graph_rag.build_query_plan(
            case["prompt"], parser="llm", query_plan_v4=structured
        )
        # 앱 소유 백필(동시구매 IR)은 프로덕션에서 build_sql_result 단계에 배선돼 있다 —
        # 여기서도 같은 결정론 생산자를 태워야 plan.condition_evaluations 기대가 공허하지 않다.
        graph_rag.apply_same_product_co_purchase_backfill(plan, case["prompt"])
        plans[case["id"]] = plan
    return plans


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_live_structuring_covers_expected_slots(case: dict, live_plans: dict[str, dict]) -> None:
    plan = live_plans[case["id"]]
    if plan.get("_fallback"):
        pytest.skip("구조화가 폴백(intent=unknown)으로 떨어짐 — 키/네트워크를 확인하라")
    produced = ir_snapshot.condition_slots(plan)
    missing = _required_expects(case) - produced
    leaked = set(case.get("forbid_slots", [])) & produced
    assert not missing and not leaked, (
        f"[{case['id']}] 라이브 구조화 결과가 골든 계약을 어겼다.\n"
        f"  누락(필수): {sorted(missing)}\n"
        f"  누출(금지): {sorted(leaked)}\n"
        f"  실제 생성된 슬롯: {sorted(produced)}\n"
        f"  프롬프트: {case['prompt']}"
    )
