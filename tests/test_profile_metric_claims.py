"""Live #7 profile-metric repair and #5 bare-recency boundary regressions."""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import profile_metric_claims  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

CURRENT_DATE = "2026-08-04"
BUY_CYCLE_QUERY = "구매주기가 30일 이하인 회원"
BARE_RECENT_QUERY = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"


def _issue(query: str, span: str, *, argument: str = "purchase_cycle") -> dict:
    start = query.index(span)
    return {
        "code": "unsupported_semantics",
        "argument": argument,
        "message": (
            "The requested purchase cycle condition cannot be represented "
            "in the canonical Event IR."
        ),
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    }


def _raw_unsupported(query: str, span: str, *, argument: str = "purchase_cycle") -> dict:
    return {
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
            "issues": [_issue(query, span, argument=argument)],
        },
        "semantic_plan": {"nodes": []},
    }


def _sql_result(query: str, structured: dict) -> tuple[dict, dict]:
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=query,
    )
    return plan, result


def test_declared_buy_cycle_claim_has_metric_threshold_operator_and_snapshot_receipt() -> None:
    bindings = extract_literal_bindings(BUY_CYCLE_QUERY, current_date=CURRENT_DATE)
    synthesis = profile_metric_claims.synthesize_profile_metric_predicate(
        BUY_CYCLE_QUERY,
        _issue(BUY_CYCLE_QUERY, "구매주기"),
        bindings,
        {"nodes": []},
    )

    assert synthesis is not None
    assert synthesis.node == {
        "id": synthesis.node["id"],
        "type": "predicate",
        "source_span": "구매주기가 30일 이하",
        "source_start": 0,
        "source_end": 12,
        "subject": "member",
        "metric": "buy_cycle",
        "operator": "<=",
        "value": 30,
        "unit": "days",
        "producer": profile_metric_claims.OWNER,
    }
    receipt = synthesis.receipt
    assert receipt["source"] == {
        "table": "CRM_MB_MONTHCRMINFO",
        "column": "BUY_CYCLE",
        "snapshot_filter": "M.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)",
    }
    assert receipt["threshold"] == {
        "binding_id": "duration_1",
        "value": 30,
        "unit": "days",
        "start": 6,
        "end": 9,
    }
    assert receipt["comparison"] == {
        "binding_id": "comparison_operator_1",
        "operator": "<=",
        "start": 10,
        "end": 12,
    }
    assert receipt["consumed_literal_binding_ids"] == [
        "duration_1",
        "comparison_operator_1",
    ]


def test_exact_live_buy_cycle_raw_response_replays_through_sql() -> None:
    raw = _raw_unsupported(BUY_CYCLE_QUERY, "구매주기")
    # 185140-8777fd attempt 2 그대로: 모델 evidence end가 한 글자 넓다.
    # V4 evidence 정규화가 이를 원문 alias 0:4로 좁힌 뒤 이 receipt가 소유한다.
    raw["audience_requirement"]["issues"][0].update({
        "message": (
            "원문 '구매주기'는 Event IR로 직접 표현할 수 있는 카탈로그 관계나 "
            "메트릭으로 매핑되지 않습니다."
        ),
        "evidence": {"text": "구매주기", "start": 0, "end": 5},
    })
    structured = attach_campaign_query_plan_v4_identity(
        raw,
        BUY_CYCLE_QUERY,
        current_date=CURRENT_DATE,
    )

    assert structured["audience_requirement"] == {"expression": None, "issues": []}
    assert structured["semantic_ir"]["status"] == "resolved"
    node = structured["semantic_plan"]["nodes"][0]
    assert (node["metric"], node["operator"], node["value"], node["unit"]) == (
        "buy_cycle",
        "<=",
        30,
        "days",
    )
    decision = structured["decisions"][0]
    assert decision["filter"] == profile_metric_claims.OWNER
    assert decision["value"]["consumed_literal_binding_ids"] == [
        "duration_1",
        "comparison_operator_1",
    ]

    plan, result = _sql_result(BUY_CYCLE_QUERY, structured)

    assert result["is_success"] is True, result.get("failure_reason")
    assert "CRM_MB_MONTHCRMINFO M" in result["sql"]
    assert "M.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in result["sql"]
    assert "M.BUY_CYCLE <= 30" in result["sql"]
    assert plan["unresolved_source_conditions"] == []
    node_id = plan["semantic_plan"]["nodes"][0]["id"]
    assert plan["semantic_pipeline"]["node_slots"] == {
        node_id: "target_user.balance_conditions"
    }
    assert result["condition_tokens"] == [
        {
            "path": "target_user.balance_conditions[0]",
            "type": "member_metric",
            "operator": "<=",
            "value": 30,
            "sql_clauses": ["M.BUY_CYCLE <= 30"],
            "joins": ["CRM_MB_MONTHCRMINFO"],
            "order_by": [],
            "select_columns": [],
            "ctes": [],
            "base_joins": [],
        }
    ]


def test_latest_live_buy_cycle_literal_only_issue_replays_through_sql() -> None:
    raw = _raw_unsupported(BUY_CYCLE_QUERY, "30일 이하")
    # 191548-045e60 attempt 3 exactly: the metric is named by the declared
    # issue argument while evidence owns only the amount/operator literals.
    raw["audience_requirement"]["issues"][0]["message"] = (
        "'구매주기' (purchase cycle) 집계는 현재 Canonical Event IR로 "
        "표현할 수 없거나 compiler가 지원하지 않습니다."
    )
    structured = attach_campaign_query_plan_v4_identity(
        raw,
        BUY_CYCLE_QUERY,
        current_date=CURRENT_DATE,
    )

    assert structured["audience_requirement"] == {"expression": None, "issues": []}
    receipt = structured["decisions"][0]["value"]
    assert receipt["issue"] == {
        "code": "unsupported_semantics",
        "argument": "purchase_cycle",
        "start": 6,
        "end": 12,
        "ownership": "threshold_and_operator",
    }

    _plan, result = _sql_result(BUY_CYCLE_QUERY, structured)
    assert result["is_success"] is True, result.get("failure_reason")
    assert "M.BUY_CYCLE <= 30" in result["sql"]


def test_buy_cycle_comparison_polarity_is_preserved() -> None:
    query = "구매주기가 30일 이상인 회원"
    structured = attach_campaign_query_plan_v4_identity(
        _raw_unsupported(query, "구매주기"), query, current_date=CURRENT_DATE
    )
    plan, result = _sql_result(query, structured)

    assert result["is_success"] is True, result.get("failure_reason")
    assert plan["target_user"]["balance_conditions"][0]["operator"] == ">="
    assert "M.BUY_CYCLE >= 30" in result["sql"]
    assert "M.BUY_CYCLE <= 30" not in result["sql"]


@pytest.mark.parametrize(
    ("query", "span", "argument"),
    [
        ("구매수량이 30일 이하인 회원", "구매수량", "purchase_cycle"),
        ("구매주기가 30회 이하인 회원", "구매주기", "purchase_cycle"),
        ("구매주기가 30일 이하가 아닌 회원", "구매주기", "purchase_cycle"),
        ("최근 구매주기가 30일 이하인 회원", "구매주기", "purchase_cycle"),
        ("VIP이면서 구매주기가 30일 이하인 회원", "구매주기", "purchase_cycle"),
    ],
)
def test_profile_metric_synthesis_rejects_wrong_field_unit_negation_or_remainder(
    query: str, span: str, argument: str
) -> None:
    assert profile_metric_claims.synthesize_profile_metric_predicate(
        query,
        _issue(query, span, argument=argument),
        extract_literal_bindings(query, current_date=CURRENT_DATE),
        {"nodes": []},
    ) is None


def test_profile_metric_literal_only_issue_requires_declared_argument_mapping() -> None:
    assert profile_metric_claims.synthesize_profile_metric_predicate(
        BUY_CYCLE_QUERY,
        _issue(BUY_CYCLE_QUERY, "30일 이하", argument="arbitrary_metric"),
        extract_literal_bindings(BUY_CYCLE_QUERY, current_date=CURRENT_DATE),
        {"nodes": []},
    ) is None


def test_bare_recent_campaign_count_remains_a_user_period_omission() -> None:
    raw = {
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
            "issues": [
                {
                    "code": "missing_argument",
                    "argument": "period",
                    "message": (
                        "The term '최근' requires a specific period to define the time frame "
                        "for the audience segment and no duration was provided."
                    ),
                    "evidence": {"text": "최근", "start": 0, "end": 2},
                }
            ],
        },
        "semantic_plan": {"nodes": []},
    }
    structured = attach_campaign_query_plan_v4_identity(
        raw, BARE_RECENT_QUERY, current_date=CURRENT_DATE
    )

    assert [binding["kind"] for binding in structured["literal_bindings"]] == [
        "number_with_unit",
        "comparison_operator",
    ]
    assert structured["semantic_plan"]["nodes"] == []
    assert structured.get("event_expression") is None
    assert structured["semantic_ir"]["status"] == "needs_clarification"
    assert structured["semantic_ir"]["failure_kind"] == "user_clarification"
    assert structured["semantic_ir"]["missing_field_causes"][0]["cause"] == "user_omission"

    _plan, result = _sql_result(BARE_RECENT_QUERY, structured)
    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "semantic_ir_needs_clarification"


@pytest.mark.parametrize(
    ("envelope_field", "value"),
    [
        ("intent", "analyze_aggregation"),
        ("campaign_objective", "purchase"),
        ("result_limit", 50),
    ],
)
def test_profile_metric_synthesis_rejects_invented_execution_envelope(
    envelope_field: str, value: object
) -> None:
    raw = _raw_unsupported(BUY_CYCLE_QUERY, "30일 이하")
    if envelope_field == "campaign_objective":
        raw["campaign_constraints"]["objective"] = value
    else:
        raw[envelope_field] = value

    structured = attach_campaign_query_plan_v4_identity(
        raw, BUY_CYCLE_QUERY, current_date=CURRENT_DATE
    )

    assert structured["audience_requirement"]["issues"]
    assert structured["semantic_plan"]["nodes"] == []
    assert structured["semantic_ir"]["status"] == "unsupported"
