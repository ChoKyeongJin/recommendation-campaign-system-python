from __future__ import annotations

import json

import pytest

from query_structurer.campaign_plan_v4 import (
    CampaignQueryPlanValidationError,
    attach_campaign_query_plan_v4_identity,
    validate_campaign_query_plan_v4,
)
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext


def test_invalid_provider_tool_schema_is_not_retried_as_model_output() -> None:
    calls = 0
    events: list[tuple[str, dict]] = []

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError(
            "Invalid schema for function 'submit_campaign_query_plan_v4': invalid_function_parameters"
        )

    result = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=5,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query="회원 추출",
        context=StructuringContext(current_date="2026-08-02", timezone="Asia/Seoul"),
    ))

    assert calls == 1
    assert result["semantic_ir"]["failure_kind"] == "system_failure"
    fallback = next(payload for name, payload in events if name == "campaign_query_plan_v4_fallback")
    assert fallback["attempts"] == 1


def test_semantically_invalid_expression_is_retried_instead_of_accepted() -> None:
    query = "남자는 제외해."
    start = query.index("남자")
    evidence = {"text": "남자", "start": start, "end": start + len("남자")}
    envelope = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
    }
    invalid = {
        **envelope,
        "audience_requirement": {
            "expression": {
                "type": "not",
                "operand": {
                    "type": "exists",
                    "relation": {"type": "source", "name": "subject"},
                    "evidence": evidence,
                },
            },
            "issues": [],
        },
    }
    corrected = {
        **envelope,
        "audience_requirement": {
            "expression": {
                "type": "not",
                "operand": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": "subject.gender"},
                    "right": {"type": "literal", "value": "male"},
                    "evidence": evidence,
                },
            },
            "issues": [],
        },
    }
    responses = iter((json.dumps(invalid), json.dumps(corrected)))
    events: list[tuple[str, dict]] = []

    result = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: next(responses),
        max_retries=1,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(current_date="2026-08-02", timezone="Asia/Seoul"),
    ))

    assert result["semantic_ir"]["status"] == "resolved"
    assert result["event_expression"]["expression"] == corrected[
        "audience_requirement"
    ]["expression"]
    failure = next(
        payload for name, payload in events
        if name == "campaign_query_plan_v4_attempt_failed"
    )
    assert "failed application validation" in failure["error"]
    assert any(name == "campaign_query_plan_v4_success" for name, _ in events)


@pytest.mark.parametrize(
    ("query", "code", "argument", "evidence_text", "expected_status"),
    [
        ("최근 구매한 회원", "missing_argument", "period", "최근", "needs_clarification"),
        ("상품을 많이 구매한 회원", "ambiguous_requirement", "threshold", "많이", "needs_clarification"),
        ("웜홀을 통과한 회원", "unsupported_semantics", "wormhole", "웜홀", "unsupported"),
    ],
)
def test_valid_reported_issue_is_accepted_without_retry(
    query: str,
    code: str,
    argument: str,
    evidence_text: str,
    expected_status: str,
) -> None:
    calls = 0
    start = query.index(evidence_text)
    response = json.dumps({
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": code,
                "argument": argument,
                "message": "조건을 확정할 수 없습니다.",
                "evidence": {
                    "text": evidence_text,
                    "start": start,
                    "end": start + len(evidence_text),
                },
            }],
        },
    }, ensure_ascii=False)

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return response

    events: list[tuple[str, dict]] = []
    result = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(current_date="2026-08-04", timezone="Asia/Seoul"),
    ))

    assert calls == 1
    assert result["audience_requirement"]["issues"][0]["code"] == code
    assert result["semantic_ir"]["status"] == expected_status
    assert [name for name, _payload in events] == ["campaign_query_plan_v4_success"]


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        (
            {
                "code": "not_a_real_issue",
                "argument": "period",
                "message": "invalid code",
                "evidence": {"text": "최근", "start": 0, "end": 2},
            },
            "unknown audience issue code",
        ),
        (
            {
                "code": "missing_argument",
                "argument": "period",
                "message": "invalid evidence",
                "evidence": {"text": "다른", "start": 0, "end": 2},
            },
            "audience issue evidence does not match original_query",
        ),
    ],
)
def test_strict_issue_validation_preserves_campaign_error_boundary(
    issue: dict,
    message: str,
) -> None:
    query = "최근 구매한 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )
    payload.pop("query_identity_digest", None)
    payload["audience_requirement"] = {"expression": None, "issues": [issue]}

    with pytest.raises(CampaignQueryPlanValidationError, match=message):
        validate_campaign_query_plan_v4(payload, query=query, require_semantic=True)
