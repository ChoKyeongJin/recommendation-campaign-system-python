"""Live regressions for directional grade and unsupported state transitions."""

from __future__ import annotations

import copy

import networkx as nx

import compositional_targeting
import execution_assets
import graph_rag
import targeting_domain
from query_structurer.campaign_plan_v4 import attach_campaign_query_plan_v4_identity


def _issue(query: str, code: str, argument: str, text: str) -> dict:
    start = query.index(text)
    return {
        "code": code,
        "argument": argument,
        "message": "model-authored issue",
        "evidence": {"text": text, "start": start, "end": start + len(text)},
    }


def _direction_node(query: str, text: str, value: str = "승급") -> dict:
    start = query.index(text)
    return {
        "id": "req-1",
        "type": "relation_predicate",
        "source_span": text,
        "source_start": start,
        "source_end": start + len(text),
        "subject": "member",
        "attribute": "member_month_snapshot.grade",
        "relation": "transition",
        "value": value,
        "value_comparison": None,
        "from_value": None,
        "to_value": None,
        "period": None,
        "months": None,
        "count": None,
        "count_operator": None,
    }


def _sql_result(query: str, payload: dict) -> dict:
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=payload)
    return graph_rag.build_sql_result(
        nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=query,
    )


def test_recent_bare_promotion_uses_latest_snapshot_and_complete_grade_order() -> None:
    query = "최근에 등급이 승급한 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [_issue(query, "missing_argument", "period", "최근")],
            },
            "semantic_plan": {
                "nodes": [_direction_node(query, "등급이 승급")]
            },
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["audience_requirement"]["issues"] == []
    assert payload["semantic_ir"]["status"] == "resolved"
    result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    sql = result["sql"]
    assert "S.YYYYMM = (SELECT MAX(YYYYMM)" in sql
    assert "S.PREV_ZTS_GRADE" in sql
    assert (
        "S.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD' "
        "AND S.ZTS_GRADE = 'MEM_GRADE_CD.VIP'"
    ) in sql
    assert (
        "S.PREV_ZTS_GRADE = 'MEM_GRADE_CD.VIP' "
        "AND S.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"
    ) not in sql
    assert "S.PREV_ZTS_GRADE <> S.ZTS_GRADE" not in sql


def test_live_bare_promotion_cue_is_grounded_to_its_adjacent_grade_axis() -> None:
    """Replay the sparse relation node emitted in live prompt #17."""
    query = "최근에 등급이 승급한 회원"
    node = _direction_node(query, "승급")
    node.update({
        "subject": "member_month_snapshot",
        "attribute": "grade",
        "value": None,
    })

    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [_issue(query, "missing_argument", "period", "최근")],
            },
            "semantic_plan": {"nodes": [node]},
        },
        query,
        current_date="2026-08-04",
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["source_span"] == "등급이 승급"
    assert relation["source_start"] == query.index("등급")
    assert relation["source_end"] == query.index("승급") + len("승급")
    assert relation["value"] == "승급"
    assert payload["audience_requirement"]["issues"] == []
    result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "PREV_ZTS_GRADE" in result["sql"]


def test_recent_issue_is_not_reassigned_across_an_intervening_purchase_phrase() -> None:
    query = "최근 구매하고 등급이 승급한 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [_issue(query, "missing_argument", "period", "최근")],
            },
            "semantic_plan": {
                "nodes": [_direction_node(query, "등급이 승급")]
            },
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["semantic_ir"]["status"] == "needs_clarification"
    assert payload["semantic_ir"]["missing_fields"] == ["audience.period"]


def test_directional_transition_fails_closed_without_a_complete_unique_order() -> None:
    catalog = targeting_domain.attribute_catalog()
    broken = copy.deepcopy(catalog)
    values = broken["attributes"]["member_grade"]["values"]
    values["vip"].pop("rank")

    operation = compositional_targeting.resolve_operation(
        {
            "operator": "transition",
            "attribute_id": "member_grade",
            "transition_direction": "ascending",
        },
        broken,
    )

    assert operation["status"] == "unsupported"
    assert "완전한 값 서열" in operation["message"]


def test_current_dormant_asset_does_not_rebut_unsupported_state_history() -> None:
    query = "여성이면서 정상에서 휴면으로 바뀐 회원"
    span = "정상에서 휴면으로 바뀐"
    issue = _issue(query, "unsupported_semantics", "grade_transition_values", span)

    assert any(
        asset.symbol in {"dormant", "inactivity_period"}
        for asset in execution_assets.non_canonical_assets_for_text(span)
    )
    assert execution_assets.non_canonical_assets_for_issue(issue) == ()

    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {"expression": None, "issues": [issue]},
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    semantic_ir = payload["semantic_ir"]
    assert semantic_ir["status"] == "unsupported"
    assert semantic_ir["failure_kind"] == "unsupported"
    assert semantic_ir["unsupported_operations"]
    assert payload.get("audience_execution_assets") is None
    result = _sql_result(query, payload)
    assert not result["is_success"]
    assert not result.get("sql")


def test_declared_grade_history_can_still_rebut_a_transition_claim() -> None:
    query = "골드에서 VIP로 바뀐 회원"
    issue = _issue(
        query,
        "unsupported_semantics",
        "grade_transition_values",
        "골드에서 VIP로 바뀐",
    )

    assets = execution_assets.non_canonical_assets_for_issue(issue)
    assert any(
        asset.layer == execution_assets.HISTORY and asset.symbol == "member_grade"
        for asset in assets
    )
