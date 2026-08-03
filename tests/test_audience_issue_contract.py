"""Model-authored period issues must be semantically grounded."""

from __future__ import annotations

import json

import pytest

import audience_runtime
import event_compiler
import event_ir
from query_structurer import audience_execution
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext


APP_QUERY = "앱으로 로그인하지 않은 회원을 찾아줘"


def _period_issue(query: str, evidence_text: str | None = None) -> dict:
    text = query if evidence_text is None else evidence_text
    start = query.index(text)
    return {
        "code": "missing_argument",
        "argument": "period",
        "message": "기간이 필요합니다.",
        "evidence": {"text": text, "start": start, "end": start + len(text)},
    }


def _correct_app_expression() -> event_ir.Not:
    span = "앱으로 로그인하지 않은"
    start = APP_QUERY.index(span)
    return event_ir.Not(event_ir.Comparison(
        "=",
        event_ir.FieldRef("subject.last_login_channel"),
        event_ir.Literal("app_user"),
        evidence=event_ir.Evidence(span, start, start + len(span)),
    ))


def test_catalog_grounded_app_channel_rejects_fabricated_period_issue() -> None:
    with pytest.raises(
        audience_execution.AudienceValidationError,
        match=r"missing_argument\(period\).*catalog-grounded",
    ):
        audience_execution.validate_audience_issue(
            _period_issue(APP_QUERY), APP_QUERY
        )


@pytest.mark.parametrize(
    ("query", "evidence"),
    [
        ("최근 구매한 회원", "최근"),
        ("최근 로그인한 회원", "최근"),
        ("미접속 회원", None),
        ("로그인하지 않은 회원", None),
        ("장기 미접속 회원", None),
        ("오랫동안 앱으로 로그인하지 않은 회원", None),
    ],
)
def test_real_temporal_or_unscoped_inactivity_period_issue_remains_valid(
    query: str, evidence: str | None
) -> None:
    issue = _period_issue(query, evidence)

    assert audience_execution.validate_audience_issue(issue, query) == issue


def test_correct_app_channel_complement_resolves_and_compiles() -> None:
    expression = _correct_app_expression()
    payload = {
        "audience_requirement": {"expression": expression.to_dict(), "issues": []},
        "literal_bindings": [],
    }

    resolution = audience_execution.run_audience_resolver(
        payload, APP_QUERY, current_date="2026-08-04"
    )

    assert resolution is not None and resolution.issues == []
    assert resolution.expression is not None
    sql = event_compiler.compile_expression(
        resolution.expression,
        context=audience_runtime.resolve_audience_catalog().compile_context(
            literals=True
        ),
    ).sql
    assert sql == (
        "NOT (CASE WHEN (B.LAST_LOGIN_CHANNEL = 'DEVICE_TYPE_CD.APP') "
        "THEN 1 ELSE 0 END = 1)"
    )


def test_fabricated_period_issue_is_retried_and_correct_expression_is_accepted() -> None:
    bad = {
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
            "issues": [_period_issue(APP_QUERY)],
        },
    }
    good = {
        **bad,
        "audience_requirement": {
            "expression": _correct_app_expression().to_dict(),
            "issues": [],
        },
    }
    responses = iter((
        json.dumps(bad, ensure_ascii=False),
        json.dumps(good, ensure_ascii=False),
    ))
    calls = 0

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return next(responses)

    result = LLMCampaignQueryPlanV4Structurer(
        complete, max_retries=2
    ).structure(QueryStructuringInput(
        query=APP_QUERY,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert calls == 2
    assert result["semantic_ir"]["status"] == "resolved"
    assert result["event_expression"]["expression"] == good[
        "audience_requirement"
    ]["expression"]
