"""확정 계층이 **실제 플랜 경로에서** 도는지 — 배선의 계약.

단위 테스트가 정책과 적용기를 각각 증명해도, 그것이 파이프라인에 꽂혀 있지 않으면 사용자에게는
아무 일도 일어나지 않는다. 이 파일은 그 연결만 본다: 같은 구조화기 산출을 넣었을 때
``plan["resolution"]`` 이 생기고, 되묻기면 SQL 이 나가지 않고, 답을 주면 SQL 경로가 열리는가.

되묻기 답변은 요청 범위 스코프(:func:`resolution.clarification_scope`)로 들어간다 — 원문에
답을 이어 붙이지 않는다는 계약이 배선 층에서도 유지되는지 함께 잰다.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import resolution  # noqa: E402
import resolution.integration  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-08"
GRAIN_QUERY = "최근 30일 동안 20만원 이상 구매한 회원을 찾아줘"
AMOUNT_TEXT = "20만원 이상 구매한"


def _evidence(query: str, text: str) -> dict[str, Any]:
    start = query.index(text)
    return {"text": text, "start": start, "end": start + len(text)}


#: 회원별 합계 임계로 굳은 표현. 원문은 grain 을 말하지 않았고 row 로도 낮출 수 있다.
SUBJECT_GRAIN_EXPRESSION: dict[str, Any] = {
    "type": "comparison",
    "operator": ">=",
    "left": {
        "type": "aggregate",
        "function": "sum",
        "distinct": False,
        "expression": {"type": "field", "name": "purchase.amount"},
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": "purchase"},
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": "purchase.occurred_at"},
                "window": {"type": "rolling", "value": 30, "unit": "day"},
            },
        },
    },
    "right": {"type": "literal", "value": 200000},
    "evidence": _evidence(GRAIN_QUERY, AMOUNT_TEXT),
}


def _plan(expression: dict[str, Any] | None, issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": copy.deepcopy(expression),
                "issues": copy.deepcopy(issues or []),
            },
        },
        GRAIN_QUERY,
        current_date=CURRENT_DATE,
    )


def test_the_plan_carries_a_resolution_block() -> None:
    plan = _plan(SUBJECT_GRAIN_EXPRESSION)

    block = plan.get("resolution")

    assert isinstance(block, dict)
    assert block["mode"] == "safe_defaults"


def test_an_unstated_grain_blocks_sql_and_asks_one_typed_question() -> None:
    plan = _plan(SUBJECT_GRAIN_EXPRESSION)

    block = plan["resolution"]

    assert block["status"] == "needs_clarification"
    assert [item["code"] for item in block["questions"]] == ["AMBIGUOUS_AMOUNT_GRAIN"]
    # 확정되지 않은 의미로 SQL 을 내보내지 않는다.
    assert "event_expression" not in plan
    assert plan["semantic_ir"]["status"] == "needs_clarification"
    # 사용자가 답하면 없어지는 결핍이다 — 구조화기·설정 실패로 분류되면 질문이 사라진다.
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


def test_answering_the_question_opens_the_sql_path_with_a_receipt() -> None:
    asked = _plan(SUBJECT_GRAIN_EXPRESSION)
    issue_id = asked["resolution"]["questions"][0]["issue_id"]

    with resolution.clarification_scope(
        [resolution.ClarificationAnswer(issue_id=issue_id, option_id="row")]
    ):
        settled = _plan(SUBJECT_GRAIN_EXPRESSION)

    assert settled["resolution"]["status"] == "resolved"
    assert settled["resolution"]["resolution"] == "assumed"
    assert [item["code"] for item in settled["resolution"]["assumptions"]] == [
        "USER_ANSWER:AMBIGUOUS_AMOUNT_GRAIN"
    ]
    assert settled["semantic_ir"]["status"] == "resolved"
    expression = settled["event_expression"]["expression"]
    # grain 만 내려갔다: 기간·금액·사건은 그대로다.
    assert expression["type"] == "exists"
    assert "aggregate" not in str(expression)
    assert '"value": 30' in __import__("json").dumps(expression)


def test_a_stated_grain_never_reaches_the_question_stage() -> None:
    """원문이 grain 을 말한 문장은 예전과 똑같이 SQL 로 간다(회귀 방지)."""

    query = "최근 30일 동안 총 구매금액이 20만원 이상인 회원을 찾아줘"
    expression = copy.deepcopy(SUBJECT_GRAIN_EXPRESSION)
    text = "총 구매금액이 20만원 이상인"
    start = query.index(text)
    expression["evidence"] = {"text": text, "start": start, "end": start + len(text)}
    plan = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {"expression": expression, "issues": []},
        },
        query,
        current_date=CURRENT_DATE,
    )

    assert plan["resolution"]["status"] == "resolved"
    assert plan["resolution"]["resolution"] == "exact"
    assert plan["semantic_ir"]["status"] == "resolved"
    assert "event_expression" in plan


def test_an_exact_request_reports_no_assumption() -> None:
    query = "최근 30일 동안 구매한 회원을 찾아줘"
    expression = {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": "purchase"},
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": "purchase.occurred_at"},
                "window": {"type": "rolling", "value": 30, "unit": "day"},
            },
        },
        "evidence": {
            "text": "최근 30일 동안 구매한",
            "start": 0,
            "end": len("최근 30일 동안 구매한"),
        },
    }
    plan = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {"expression": expression, "issues": []},
        },
        query,
        current_date=CURRENT_DATE,
    )

    assert plan["resolution"] == {
        "status": "resolved",
        "mode": "safe_defaults",
        "assumptions": [],
        "questions": [],
        "answer_count": 0,
        "rounds": 1,
        "resolution": "exact",
    }


# ── 엔터티 값 되묻기 ─────────────────────────────────────────────────────────────

ENTITY_QUERY = "특정 상품을 구매한 회원을 찾아줘"
ENTITY_REPORT = {
    "code": "missing_argument",
    "argument": "product",
    "message": "어떤 상품인지 확정하지 못했습니다.",
    "evidence": {"text": "특정 상품", "start": 0, "end": 5},
}


def _entity_plan() -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [copy.deepcopy(ENTITY_REPORT)],
            },
        },
        ENTITY_QUERY,
        current_date=CURRENT_DATE,
    )


def test_a_missing_entity_value_asks_with_a_free_text_question() -> None:
    plan = _entity_plan()

    question = plan["resolution"]["questions"][0]

    assert question["code"] == "MISSING_ENTITY_VALUE"
    assert question["allow_free_text"] is True
    assert question["entity_type"] == "product"
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


def test_an_entity_answer_becomes_a_typed_binding_without_breaking_the_plan() -> None:
    """표현이 서지 못한 자리의 답은 결속으로 남고, 그 라운드의 신고는 그대로 지켜진다.

    신고까지 함께 사라지면 '표현도 없고 결핍도 없는' 플랜이 되어 하류가 다룰 수 없다 —
    이 테스트는 그 구멍이 다시 열리는 것을 막는다.
    """

    asked = _entity_plan()
    issue_id = asked["resolution"]["questions"][0]["issue_id"]

    with resolution.clarification_scope(
        [resolution.ClarificationAnswer(issue_id=issue_id, text="로얄캐닌 사료")]
    ):
        answered = _entity_plan()

    binding = answered["resolution"]["entity_bindings"][0]
    assert binding == {
        "issue_id": issue_id,
        "entity_type": "product",
        "value": "로얄캐닌 사료",
        "evidence": {"text": "특정 상품", "start": 0, "end": 5},
    }
    # 표현이 아직 없으므로 원래 신고는 지켜진다(하류가 다룰 수 있는 상태로 남는다).
    assert answered["audience_requirement"]["issues"] == [ENTITY_REPORT]
    # 다음 구조화에 넘길 지시문은 값과 구간만 나른다 — 원문을 다시 쓰지 않는다.
    instruction = resolution.integration.entity_binding_instruction(answered)
    assert "로얄캐닌 사료" in instruction
    assert ENTITY_QUERY not in instruction


# ── 응답 계약 ────────────────────────────────────────────────────────────────────


def test_the_api_response_carries_the_resolution_block() -> None:
    """블록이 응답에서 사라지면 되묻기 UI 는 아무것도 못 그린다(조용한 회귀 방지)."""

    import graph_rag

    plan = _plan(SUBJECT_GRAIN_EXPRESSION)
    response = graph_rag.build_recommendation_api_response(
        GRAIN_QUERY,
        plan,
        {"clarification_questions": [], "confidence": None},
        {},
    )

    block = response["resolution"]
    assert block["status"] == "needs_clarification"
    assert [item["code"] for item in block["questions"]] == ["AMBIGUOUS_AMOUNT_GRAIN"]


def test_the_response_block_is_empty_when_the_layer_did_not_run() -> None:
    """오디언스 계약이 없는 레인(rules 등)에서는 키만 있고 내용이 없다."""

    import graph_rag

    response = graph_rag.build_recommendation_api_response(
        GRAIN_QUERY, {}, {"clarification_questions": [], "confidence": None}, {}
    )

    assert response["resolution"] == {}


@pytest.mark.parametrize("mode", ["strict", "best_effort"])
def test_the_mode_env_reaches_the_plan(monkeypatch: Any, mode: str) -> None:
    monkeypatch.setenv("AUDIENCE_RESOLUTION_MODE", mode)

    plan = _plan(SUBJECT_GRAIN_EXPRESSION)

    assert plan["resolution"]["mode"] == mode
    # grain 은 HIGH 위험이라 모드와 무관하게 되묻기다(§34-H).
    assert plan["resolution"]["status"] == "needs_clarification"
