"""Live #6 campaign-average claim synthesis and fail-closed boundaries."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import campaign_metric_claims  # noqa: E402
import graph_rag  # noqa: E402
import member_filters_config  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

CURRENT_DATE = "2026-08-04"
QUERY = "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"


def _literal_issues(query: str, bindings: list[dict]) -> list[dict]:
    money_index, money = next(
        (index, item)
        for index, item in enumerate(bindings)
        if item["kind"] == "money"
    )
    operator_index, operator = next(
        (index, item)
        for index, item in enumerate(bindings)
        if item["kind"] == "comparison_operator"
    )

    def issue(argument: str, binding: dict, message: str) -> dict:
        return {
            "code": "unsupported_semantics",
            "argument": argument,
            "message": message,
            "evidence": {
                "text": binding["text"],
                "start": binding["start"],
                "end": binding["end"],
            },
        }

    return [
        issue(
            f"literal_bindings[{money_index}]",
            money,
            "The amount literal could not be connected to a per-member average.",
        ),
        issue(
            f"literal_bindings[{operator_index}]",
            operator,
            "The comparison operator could not be consumed.",
        ),
        issue(
            f"literal_bindings[{money_index}].unit",
            money,
            "The money literal currency cannot be attached to a canonical field.",
        ),
    ]


def _raw(query: str) -> dict:
    bindings = extract_literal_bindings(query, current_date=CURRENT_DATE)
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
            "issues": _literal_issues(query, bindings),
        },
        "semantic_plan": {"nodes": []},
    }


def _model_expression(*, empty_filter: bool = True) -> dict:
    source: dict = {"type": "source", "name": "campaign_purchase_response"}
    relation = (
        {"type": "filter", "relation": source, "where": None}
        if empty_filter
        else source
    )
    return {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "avg",
            "relation": relation,
            "expression": {
                "type": "field",
                "name": "campaign_purchase_response.amount",
            },
            "distinct": False,
        },
        "right": {"type": "literal", "value": 100000},
        "evidence": {"text": "10만 원 이상", "start": 17, "end": 25},
    }


def _expression_raw(*, empty_filter: bool = True) -> dict:
    raw = _raw(QUERY)
    raw["audience_requirement"] = {
        "expression": _model_expression(empty_filter=empty_filter),
        "issues": [],
    }
    return raw


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


def test_exact_latest_live_empty_filter_expression_retypes_to_average_sql() -> None:
    # 194552-e70136 attempt 2.  ``where=null`` is not generic Event IR, but in
    # this one fully receipted aggregate it is a no-op relation wrapper.
    structured = attach_campaign_query_plan_v4_identity(
        _expression_raw(), QUERY, current_date=CURRENT_DATE
    )

    assert structured["audience_requirement"] == {"expression": None, "issues": []}
    assert structured["semantic_ir"]["status"] == "resolved"
    node = structured["semantic_plan"]["nodes"][0]
    assert (
        node["scope"],
        node["metric"],
        node["aggregation"],
        node["value"],
        node["unit"],
        node["operator"],
    ) == ("campaign", "campaign_buy_amount", "avg", 100000, "KRW", ">=")
    receipt = structured["decisions"][0]["value"]
    assert receipt["model_expression"] == {
        "type": "comparison.aggregate",
        "relation_wrapper": "empty_filter",
        "source": "campaign_purchase_response",
        "field": "campaign_purchase_response.amount",
        "function": "avg",
        "distinct": False,
        "operator": ">=",
        "value": 100000,
        "evidence_start": 17,
        "evidence_end": 25,
    }
    assert receipt["discharged_issues"] == []

    plan, result = _sql_result(QUERY, structured)

    assert result["is_success"] is True, result.get("failure_reason")
    sql = result["sql"]
    assert "SUM(R.BUY_AMT) * 1.0 / COUNT(DISTINCT CONCAT(R.CAMP_ID" in sql
    assert ">= 100000" in sql
    assert "R.CGRP_TYPE_CD = 'T'" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    assert "ISNULL(ZC.CANCEL_YN, 'N') = 'N'" in sql
    assert plan["unresolved_source_conditions"] == []


def test_exact_latest_live_direct_source_expression_uses_same_declared_owner() -> None:
    # The explicit fallback in the same live log omitted only the no-op Filter.
    structured = attach_campaign_query_plan_v4_identity(
        _expression_raw(empty_filter=False), QUERY, current_date=CURRENT_DATE
    )

    assert structured["audience_requirement"] == {"expression": None, "issues": []}
    assert structured["semantic_plan"]["nodes"][0]["metric"] == "campaign_buy_amount"
    assert structured["decisions"][0]["value"]["model_expression"][
        "relation_wrapper"
    ] == "source"


def test_exact_latest_live_raw_response_replays_through_average_sql() -> None:
    raw = _raw(QUERY)
    # 191526-1a0317 attempt 2: three unsupported reports own amount,
    # comparison, and currency separately.
    raw["audience_requirement"]["issues"][0]["message"] = (
        "The amount literal '10만 원' from the user query could not be connected "
        "to a canonical per-member average because campaign_purchase_response "
        "source lacks a member identifier to compute per-member aggregates."
    )
    raw["audience_requirement"]["issues"][1]["message"] = (
        "The comparison operator '이상' was not consumed because a faithful "
        "per-member average comparison cannot be constructed from available sources."
    )
    raw["audience_requirement"]["issues"][2]["message"] = (
        "The money literal's currency unit (KRW) cannot be attached to any canonical "
        "field in campaign_purchase_response for a per-member aggregation."
    )

    structured = attach_campaign_query_plan_v4_identity(
        raw, QUERY, current_date=CURRENT_DATE
    )

    assert structured["audience_requirement"] == {"expression": None, "issues": []}
    assert structured["semantic_ir"]["status"] == "resolved"
    node = structured["semantic_plan"]["nodes"][0]
    assert (
        node["scope"],
        node["metric"],
        node["aggregation"],
        node["value"],
        node["unit"],
        node["operator"],
    ) == ("campaign", "campaign_buy_amount", "avg", 100000, "KRW", ">=")

    decision = structured["decisions"][0]
    assert decision["filter"] == campaign_metric_claims.OWNER
    receipt = decision["value"]
    assert receipt["per_campaign"] == {
        "text": "캠페인별",
        "start": 0,
        "end": 4,
        "denominator_field": "campaign_purchase_response.execution_id",
        "denominator_expression": "CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)",
        "denominator_distinct": True,
    }
    assert receipt["aggregation"]["function"] == "avg"
    assert receipt["aggregation"]["base_amount_aggregation"] == "SUM"
    assert receipt["threshold"] == {
        "binding_id": "money_1",
        "amount": 100000,
        "currency": "KRW",
        "start": 17,
        "end": 22,
    }
    assert receipt["response_filters"] == {
        "target_group": "R.CGRP_TYPE_CD = 'T'",
        "purchase_response": "R.BUY_RSPN_YN = 'Y'",
        "valid_campaign": "ISNULL(ZC.CANCEL_YN, 'N') = 'N'",
    }
    assert [item["argument"] for item in receipt["discharged_issues"]] == [
        "literal_bindings[0]",
        "literal_bindings[1]",
        "literal_bindings[0].unit",
    ]

    plan, result = _sql_result(QUERY, structured)

    assert result["is_success"] is True, result.get("failure_reason")
    sql = result["sql"]
    assert "SUM(R.BUY_AMT) * 1.0 / COUNT(DISTINCT CONCAT(R.CAMP_ID" in sql
    assert ">= 100000" in sql
    assert "R.CGRP_TYPE_CD = 'T'" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    assert "ISNULL(ZC.CANCEL_YN, 'N') = 'N'" in sql
    assert "TRY_CAST(OBUY.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql
    assert plan["unresolved_source_conditions"] == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("left", "function"), "sum"),
        (("left", "expression", "name"), "campaign_purchase_response.count"),
        (("left", "distinct"), True),
        (("left", "relation", "relation", "name"), "purchase"),
        (("left", "relation", "where"), {"type": "unknown"}),
        (("right", "value"), 100001),
        (("operator",), "<="),
        (("evidence", "start"), 0),
    ],
)
def test_model_expression_retyping_rejects_any_unreceipted_axis(
    path: tuple[str, ...], value: object
) -> None:
    expression = _model_expression()
    cursor = expression
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)

    assert campaign_metric_claims.synthesize_campaign_average_amount_expression(
        QUERY, expression, bindings, {"nodes": []}
    ) is None


def test_model_expression_retyping_rejects_extra_condition_or_existing_node() -> None:
    expression = _model_expression(empty_filter=False)
    wrapped = {
        "type": "and",
        "operands": [expression, copy.deepcopy(expression)],
    }
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)

    assert campaign_metric_claims.synthesize_campaign_average_amount_expression(
        QUERY, wrapped, bindings, {"nodes": []}
    ) is None
    assert campaign_metric_claims.synthesize_campaign_average_amount_expression(
        QUERY, expression, bindings, {"nodes": [{"id": "model-node"}]}
    ) is None


def test_model_expression_retyping_rejects_nonempty_execution_envelope() -> None:
    raw = _expression_raw(empty_filter=False)
    raw["external_conditions"] = [{"name": "invented"}]

    structured = attach_campaign_query_plan_v4_identity(
        raw, QUERY, current_date=CURRENT_DATE
    )

    assert structured["semantic_plan"]["nodes"] == []
    assert structured["audience_requirement"]["expression"] == _model_expression(
        empty_filter=False
    )


def test_declared_generic_metric_issue_may_own_the_whole_closed_claim() -> None:
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)
    predicate = QUERY[: QUERY.index("인 회원")]
    synthesis = campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        QUERY,
        [
            {
                "code": "unsupported_semantics",
                "argument": "campaign_purchase_amount",
                "message": "The campaign metric is unsupported.",
                "evidence": {
                    "text": predicate,
                    "start": 0,
                    "end": len(predicate),
                },
            }
        ],
        bindings,
        {"nodes": []},
    )

    assert synthesis is not None
    assert synthesis.node["metric"] == "campaign_buy_amount"
    assert synthesis.node["aggregation"] == "avg"


@pytest.mark.parametrize(
    "query",
    [
        "구매반응 금액이 평균 10만 원 이상인 회원",
        "캠페인별 주문 금액이 평균 10만 원 이상인 회원",
        "캠페인별 구매반응 금액이 합계 10만 원 이상인 회원",
        "캠페인별 구매반응 금액이 평균 10만 원 이상이 아닌 회원",
        "캠페인별 구매반응 금액이 평균 10만 원 이상이고 VIP인 회원",
    ],
)
def test_campaign_average_synthesis_rejects_missing_axes_negation_or_remainder(
    query: str,
) -> None:
    bindings = extract_literal_bindings(query, current_date=CURRENT_DATE)
    assert campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        query,
        _literal_issues(query, bindings),
        bindings,
        {"nodes": []},
    ) is None


def test_campaign_average_synthesis_rejects_incomplete_issue_ownership() -> None:
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)
    assert campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        QUERY,
        _literal_issues(QUERY, bindings)[:2],
        bindings,
        {"nodes": []},
    ) is None


def test_campaign_average_synthesis_rejects_wrong_currency() -> None:
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)
    bindings[0] = copy.deepcopy(bindings[0])
    bindings[0]["normalized"]["currency"] = "USD"
    assert campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        QUERY,
        _literal_issues(QUERY, bindings),
        bindings,
        {"nodes": []},
    ) is None


def test_campaign_average_synthesis_requires_target_and_response_sql_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = copy.deepcopy(member_filters_config.campaign_response_targets())
    broken["boolean_metrics"].pop("purchase_response")
    monkeypatch.setattr(
        member_filters_config, "campaign_response_targets", lambda: broken
    )
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)

    assert campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        QUERY,
        _literal_issues(QUERY, bindings),
        bindings,
        {"nodes": []},
    ) is None


def test_campaign_average_synthesis_never_overwrites_a_model_node() -> None:
    bindings = extract_literal_bindings(QUERY, current_date=CURRENT_DATE)
    assert campaign_metric_claims.synthesize_campaign_average_amount_predicate(
        QUERY,
        _literal_issues(QUERY, bindings),
        bindings,
        {"nodes": [{"id": "model-node"}]},
    ) is None


@pytest.mark.parametrize(
    ("envelope_field", "value"),
    [
        ("intent", "analyze_aggregation"),
        ("campaign_objective", "purchase"),
        ("result_limit", 50),
    ],
)
def test_campaign_average_synthesis_rejects_invented_execution_envelope(
    envelope_field: str, value: object
) -> None:
    raw = _raw(QUERY)
    if envelope_field == "campaign_objective":
        raw["campaign_constraints"]["objective"] = value
    else:
        raw[envelope_field] = value

    structured = attach_campaign_query_plan_v4_identity(
        raw, QUERY, current_date=CURRENT_DATE
    )

    assert structured["audience_requirement"]["issues"]
    assert structured["semantic_plan"]["nodes"] == []
    assert structured["semantic_ir"]["status"] == "unsupported"
