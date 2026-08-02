from __future__ import annotations

import json

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
