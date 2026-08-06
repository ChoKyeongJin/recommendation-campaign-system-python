"""반박 → 재방출의 결정론 통합 계약(라이브 LLM 없음).

라이브 호출은 회귀 게이트가 될 수 없다 — 같은 코드로 두 번 돌려도 귀결이 달라진다. 그래서
여기서는 **순서가 정해진 stub 모델**로 실제 루프를 돌린다:

    1차  unsupported_semantics(ranked_set_cardinality)   ← 실측된 거짓 신고
    2차  올바른 canonical ranked-set cardinality IR

지키는 것:
  · 애플리케이션이 계산한 의무 구간과 신고 구간이 겹친다 → 1차가 반박된다.
  · 재시도는 정확히 한 번(기존 제한 유지, 무한 루프 없음).
  · 재시도 프롬프트가 랭킹 카디널리티 레시피를 들고 간다.
  · 2차 출력이 영수증 검증을 통과하고 canonical 경로로 SQL 이 된다.
  · 별도 entity-set 폴백은 열리지 않는다.

두 번 다 틀린 시나리오는 **미지원으로 거짓 종결되지 않고** semantic_emission_failure 로 끝난다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import test_ranked_set_cardinality as ranked  # noqa: E402

import audience_authority  # noqa: E402
import canonical_audience_claims  # noqa: E402
import graph_rag  # noqa: E402
import semantic_requirements  # noqa: E402
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer  # noqa: E402
from query_structurer.types import QueryStructuringInput, StructuringContext  # noqa: E402

QUERY = ranked.QUERY
CURRENT_DATE = ranked.CURRENT_DATE
UNSUPPORTED_SPAN = "가장 많이 팔린 상품 5개 중 2개 이상 구매"


def _unsupported_response() -> str:
    start = QUERY.index(UNSUPPORTED_SPAN)
    return json.dumps({
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None, "offer_type": None, "channels": None, "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "unsupported_semantics",
                "argument": "ranked_set_cardinality",
                "message": "랭킹 파생 집합과 회원별 교집합 개수 임계는 지원하지 않습니다.",
                "evidence": {
                    "text": UNSUPPORTED_SPAN,
                    "start": start,
                    "end": start + len(UNSUPPORTED_SPAN),
                },
            }],
        },
    }, ensure_ascii=False)


def _correct_response() -> str:
    return json.dumps({
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None, "offer_type": None, "channels": None, "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": ranked.ranked_set_cardinality_expression(),
            "issues": [],
        },
    }, ensure_ascii=False)


class _ScriptedModel:
    """정해진 순서로 응답을 돌려주고, 받은 메시지를 기록한다."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append([dict(message) for message in messages])
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


def _structure(responses: list[str]) -> tuple[dict, _ScriptedModel, list[tuple[str, dict]]]:
    model = _ScriptedModel(responses)
    events: list[tuple[str, dict]] = []
    structurer = LLMCampaignQueryPlanV4Structurer(
        model, on_event=lambda name, payload: events.append((name, payload))
    )
    plan = structurer.structure(
        QueryStructuringInput(
            query=QUERY, context=StructuringContext(current_date=CURRENT_DATE)
        )
    )
    return dict(plan), model, events


def test_the_unsupported_claim_overlaps_an_application_owned_obligation() -> None:
    """이 시나리오의 전제 — 겹치지 않으면 반박 축 자체가 발동하지 않는다."""
    obligations = canonical_audience_claims.supported_obligations_for_query(QUERY)
    assert obligations

    start = QUERY.index(UNSUPPORTED_SPAN)
    claim = {"evidence": {
        "text": UNSUPPORTED_SPAN, "start": start, "end": start + len(UNSUPPORTED_SPAN),
    }}
    conflict = canonical_audience_claims.obligation_conflicting_with_claim(
        claim, obligations
    )
    assert conflict is not None
    assert semantic_requirements.obligation_kind(conflict) == "ranked_entity_set"


def test_a_refuted_unsupported_claim_is_retried_once_and_then_compiles() -> None:
    plan, model, events = _structure([_unsupported_response(), _correct_response()])

    assert len(model.calls) == 2, "정확히 한 번만 재시도해야 한다"
    assert [name for name, _ in events][-1] == "campaign_query_plan_v4_success"

    # 재시도 요청은 반박 사유와 랭킹 레시피를 함께 들고 간다.
    retry_messages = "\n".join(message["content"] for message in model.calls[1])
    assert "ranked_entity_set obligation" in retry_messages
    assert "[Ranked Entity Set Recipe]" in retry_messages
    assert "distinct=true" in retry_messages

    # 두 번째 출력이 애플리케이션 검증을 통과하고 canonical 권위로 실행된다.
    assert plan["semantic_ir"]["status"] == "resolved"
    assert audience_authority.executes_event_ir(plan)
    assert not plan.get("unresolved_source_conditions")

    candidate = graph_rag.compile_executable_plan(plan)
    assert candidate is not None
    assert "COUNT(DISTINCT" in candidate["sql"]

    # 별도 해석 경로(legacy entity-set)는 열리지 않는다.
    assert not plan["target_user"].get("entity_set_condition")
    assert any(
        decision.get("filter") == "builder:build_event_expression_sql_candidate"
        for decision in plan.get("decisions", [])
    )


def test_a_persistently_unemitted_meaning_ends_as_emission_failure() -> None:
    """끝까지 방출에 실패해도 '표현할 수 없다'로 종결하지 않는다."""
    plan, model, events = _structure([_unsupported_response()])

    # 기존 재시도 제한(max_retries=2)을 그대로 지킨다 — 무한 루프가 아니다.
    assert len(model.calls) == 3
    assert [name for name, _ in events][-1] == "campaign_query_plan_v4_emission_failure"

    semantic_ir = plan["semantic_ir"]
    assert semantic_ir["status"] != "unsupported"
    assert semantic_ir["failure_kind"] == "system_failure"
    assert semantic_ir["failure_reason"] == "semantic_emission_failure"
    assert plan["audience_emission_failures"][0]["obligation_kind"] == "ranked_entity_set"

    # 표현이 없으므로 SQL 은 여전히 나가지 않는다(fail-close 는 그대로다).
    assert plan.get("event_expression") is None
    assert graph_rag.compile_executable_plan(plan) is None
