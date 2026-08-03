"""End-to-end regressions for live prompts rejected after valid semantic parsing."""

from __future__ import annotations

import json

import event_ir
import graph_rag
from query_structurer.campaign_plan_v4 import (
    attach_campaign_query_plan_v4_identity,
    build_campaign_query_plan_v4_fallback,
    validate_campaign_query_plan_v4,
)
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text, start, start + len(text))


def _latest_overbroad_rolling_absence_expression() -> dict:
    broad_evidence = "6개월 이상 접속하지 않은"
    return {
        "type": "not",
        "operand": {
            "type": "exists",
            "relation": {
                "type": "filter",
                "relation": {"type": "source", "name": "login"},
                "where": {
                    "type": "time_filter",
                    "field": {"type": "field", "name": "login.occurred_at"},
                    "window": {
                        "type": "relative",
                        "value": 6,
                        "unit": "months",
                        "label": None,
                    },
                },
            },
            "evidence": {
                "text": broad_evidence,
                "start": 0,
                "end": len(broad_evidence),
            },
        },
    }


def _validated_plan(query: str, expression: event_ir.Condition) -> dict:
    enriched = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": expression.to_dict(),
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )
    return validate_campaign_query_plan_v4(
        enriched,
        query=query,
        raw_query=query,
        require_semantic=True,
    )


def _validated_null_expression_plan(query: str, issue: dict) -> dict:
    enriched = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {"expression": None, "issues": [issue]},
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )
    return validate_campaign_query_plan_v4(
        enriched,
        query=query,
        raw_query=query,
        require_semantic=True,
    )


def _validated_raw_expression_plan(query: str, expression: dict) -> dict:
    enriched = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {"expression": expression, "issues": []},
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )
    return validate_campaign_query_plan_v4(
        enriched,
        query=query,
        raw_query=query,
        require_semantic=True,
    )


def test_direct_login_channel_negation_survives_v4_validation_and_sql_lowering() -> None:
    query = "앱으로 로그인하지 않은 회원을 찾아줘"
    evidence_text = "앱으로 로그인하지 않은"
    expression = event_ir.Comparison(
        "!=",
        event_ir.FieldRef("subject.last_login_channel"),
        event_ir.Literal("app_user"),
        evidence=_evidence(query, evidence_text),
    )

    plan = _validated_plan(query, expression)
    assert plan["semantic_ir"]["status"] == "resolved"

    candidate = graph_rag.build_event_expression_sql_candidate(dict(plan))
    assert candidate is not None
    assert "B.LAST_LOGIN_CHANNEL != 'DEVICE_TYPE_CD.APP'" in candidate["sql"]


def test_six_month_login_absence_survives_v4_validation_and_sql_lowering() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    evidence_text = "접속하지 않은"
    expression = event_ir.Not(
        event_ir.Exists(
            event_ir.Filter(
                event_ir.Source("login"),
                event_ir.TimeFilter(
                    event_ir.FieldRef("login.occurred_at"),
                    event_ir.RollingWindow(6, "month"),
                ),
            ),
            evidence=_evidence(query, evidence_text),
        )
    )

    plan = _validated_plan(query, expression)
    assert plan["semantic_ir"]["status"] == "resolved"

    candidate = graph_rag.build_event_expression_sql_candidate(dict(plan))
    assert candidate is not None
    assert "B.LAST_LOGIN_DATE" in candidate["sql"]
    assert "DATEADD(DAY, -180" in candidate["sql"]
    assert "AND NOT (CASE WHEN" in candidate["sql"]


def test_latest_live_rolling_absence_payload_normalizes_owned_evidence_and_compiles() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    plan = _validated_raw_expression_plan(
        query,
        _latest_overbroad_rolling_absence_expression(),
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    evidence = plan["event_expression"]["expression"]["operand"]["evidence"]
    assert evidence == {
        "text": "접속하지 않은",
        "start": query.index("접속하지 않은"),
        "end": query.index("접속하지 않은") + len("접속하지 않은"),
    }
    assert {
        "canonical_audience_claims.window_kind",
        "rolling_absence.evidence_span",
    }.issubset({decision["filter"] for decision in plan["decisions"]})

    candidate = graph_rag.build_event_expression_sql_candidate(dict(plan))
    assert candidate is not None
    assert "B.LAST_LOGIN_DATE" in candidate["sql"]
    assert "DATEADD(DAY, -180" in candidate["sql"]
    assert "AND NOT (CASE WHEN" in candidate["sql"]


def test_latest_live_payload_is_accepted_without_structurer_fallback() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    response = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": _latest_overbroad_rolling_absence_expression(),
            "issues": [],
        },
        "semantic_plan": {"nodes": []},
    }
    events: list[tuple[str, dict]] = []
    calls = 0

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(response, ensure_ascii=False)

    plan = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert calls == 1
    assert plan["semantic_ir"]["status"] == "resolved"
    assert any(name == "campaign_query_plan_v4_success" for name, _ in events)
    assert not any(name == "campaign_query_plan_v4_fallback" for name, _ in events)


def test_latest_live_rolling_absence_full_build_passes_compiler_aligned_coverage() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    structured = _validated_raw_expression_plan(
        query, _latest_overbroad_rolling_absence_expression()
    )
    plan = graph_rag.build_query_plan(
        query,
        parser="llm",
        query_plan_v4=structured,
    )

    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=query,
    )

    assert result["is_success"]
    assert result["failure_reason"] is None
    assert result["selected"]["id"] == "sql_template:event_expression"
    assert result["selected"]["coverage"]["is_satisfied"]
    assert result["required_conditions"][0]["none_terms"] == []
    assert "not exists" not in result["required_conditions"][0]["all_terms"]
    assert "B.LAST_LOGIN_DATE" in result["sql"]
    assert "DATEADD(DAY, -180" in result["sql"]
    assert "AND NOT (CASE WHEN" in result["sql"]


def test_total_structurer_failure_recovers_only_closed_rolling_login_absence() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    calls = 0
    events: list[tuple[str, dict]] = []

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return "{"

    structured = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert calls == 3
    assert structured["intent"] == "find_user_segment"
    assert structured["semantic_ir"]["status"] == "resolved"
    assert any(
        decision["filter"] == "rolling_absence.closed_structurer_fallback"
        for decision in structured["decisions"]
    )
    assert any(name == "campaign_query_plan_v4_fallback" for name, _ in events)

    plan = graph_rag.build_query_plan(
        query,
        parser="llm",
        query_plan_v4_factory=lambda _base: structured,
    )
    assert plan["parser"]["type"] == "llm"
    assert plan["parser"]["fallback_used"] is False
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=query,
    )

    assert result["is_success"]
    assert "B.LAST_LOGIN_DATE" in result["sql"]
    assert "DATEADD(DAY, -180" in result["sql"]
    assert "AND NOT (CASE WHEN" in result["sql"]


def test_structurer_fallback_keeps_nearby_login_phrase_fail_closed() -> None:
    plan = build_campaign_query_plan_v4_fallback(
        "6개월 이상 접속하지 않은 휴면 고객 중 여성",
        current_date="2026-08-04",
    )

    assert plan["intent"] == "unknown"
    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["failure_kind"] == "system_failure"
    assert "event_expression" not in plan


def test_live_exact_two_model_clarification_is_replaced_by_proven_dnf() -> None:
    query = "이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원을 찾아줘."
    evidence = query.removesuffix(".")
    plan = _validated_null_expression_plan(
        query,
        {
            "code": "ambiguous_requirement",
            "argument": "consent_count",
            "message": "exact comparison evidence offsets are missing",
            "evidence": {"text": evidence, "start": 0, "end": len(evidence)},
        },
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["audience_requirement"]["issues"] == []
    assert any(
        decision["filter"] == "consent_cardinality.exact_truth_table"
        for decision in plan["decisions"]
    )
    candidate = graph_rag.build_event_expression_sql_candidate(dict(plan))
    assert candidate is not None
    assert all(
        column in candidate["sql"]
        for column in ("B.EMAIL_YN", "B.SMS_YN", "B.APP_PUSH_YN")
    )
    assert candidate["sql"].count(" OR ") >= 2


def test_live_rolling_absence_model_unsupported_is_replaced_by_proven_not_exists() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    operator = "이상"
    start = query.index(operator)
    plan = _validated_null_expression_plan(
        query,
        {
            "code": "unsupported_semantics",
            "argument": "comparison_operator",
            "message": "rolling absence has no standalone comparison node",
            "evidence": {
                "text": operator,
                "start": start,
                "end": start + len(operator),
            },
        },
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["audience_requirement"]["issues"] == []
    assert any(
        decision["filter"] == "rolling_absence.not_exists_window"
        for decision in plan["decisions"]
    )
    candidate = graph_rag.build_event_expression_sql_candidate(dict(plan))
    assert candidate is not None
    assert "B.LAST_LOGIN_DATE" in candidate["sql"]
    assert "DATEADD(DAY, -180" in candidate["sql"]
    assert "AND NOT (CASE WHEN" in candidate["sql"]


def test_rolling_absence_issue_is_not_suppressed_without_operator_evidence() -> None:
    query = "6개월 이상 접속하지 않은 휴면 고객"
    evidence = "접속하지 않은"
    start = query.index(evidence)
    plan = _validated_null_expression_plan(
        query,
        {
            "code": "unsupported_semantics",
            "argument": "comparison_operator",
            "message": "operator unsupported",
            "evidence": {
                "text": evidence,
                "start": start,
                "end": start + len(evidence),
            },
        },
    )

    assert plan["semantic_ir"]["status"] != "resolved"
    assert plan["audience_requirement"]["expression"] is None
