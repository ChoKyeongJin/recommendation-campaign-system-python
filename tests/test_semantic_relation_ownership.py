from __future__ import annotations

import networkx as nx
import pytest

import audience_runtime
import canonical_audience_claims
import event_ir
import graph_rag
import semantic_relation_ownership
from query_structurer.campaign_plan_v4 import attach_campaign_query_plan_v4_identity
from query_structurer.semantic_ir import extract_literal_bindings


def _evidence(query: str, start: int, end: int) -> event_ir.Evidence:
    return event_ir.Evidence(query[start:end], start, end)


def _female(query: str) -> event_ir.Condition:
    return event_ir.Comparison(
        "=", event_ir.FieldRef("subject.gender"), event_ir.Literal("female"),
        _evidence(query, 0, 2),
    )


def _node(query: str, start: int, end: int, **values: object) -> dict[str, object]:
    return {
        "id": "history",
        "type": "relation_predicate",
        "source_span": query[start:end],
        "source_start": start,
        "source_end": end,
        "subject": "member",
        **values,
    }


def _attach(query: str, expression: event_ir.Condition, node: dict[str, object]):
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {"expression": expression.to_dict(), "issues": []},
            "semantic_plan": {"nodes": [node]},
        },
        query,
        current_date="2026-08-04",
    )


def _sql_result(query: str, payload: dict):
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=payload)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    return plan, result


def test_unique_relation_span_repairs_one_character_model_offset() -> None:
    query = "여성이면서 골드에서 VIP로 바뀐 회원"
    node = _node(
        query, 6, 18, attribute="member_grade", relation="transition",
        from_value="gold_grade", to_value="vip",
    )
    node["source_end"] = 17

    payload = _attach(query, _female(query), node)

    assert payload["semantic_plan"]["nodes"][0]["source_end"] == 18
    assert payload["audience_requirement"]["issues"] == []
    _, result = _sql_result(query, payload)
    assert result["is_success"]
    assert "GENDER_CD" in result["sql"] and "PREV_ZTS_GRADE" in result["sql"]


def test_ambiguous_invalid_relation_span_fails_closed() -> None:
    query = "VIP였고 다시 VIP였던 회원"
    payload = {
        "semantic_plan": {"nodes": [{
            "id": "history", "type": "relation_predicate", "subject": "member",
            "relation": "as_of", "attribute": "member_grade", "value": "vip",
            "source_span": "VIP", "source_start": 1, "source_end": 4,
        }]}
    }
    with pytest.raises(semantic_relation_ownership.RelationOwnershipError):
        semantic_relation_ownership.normalize_relation_node_spans(payload, query)


def test_worth_grade_particle_scope_routes_both_transition_values() -> None:
    query = "여성이면서 가치등급이 골드에서 VIP로 바뀐 회원"
    payload = _attach(
        query,
        _female(query),
        _node(
            query, 6, 24, attribute="member_worth_grade_transition",
            relation="transition", from_value="gold_worth_grade",
            to_value="vip_worth_grade",
        ),
    )

    assert payload["audience_requirement"]["issues"] == []
    _, result = _sql_result(query, payload)
    assert result["is_success"]
    assert "GENDER_CD" in result["sql"] and "PREV_WORTH_GRADE" in result["sql"]


def test_live_worth_grade_endpoint_is_normalized_to_its_selected_axis() -> None:
    query = "여성이면서 가치등급이 골드에서 VIP로 바뀐 회원"
    start = query.index("가치등급")
    end = query.index(" 회원")
    payload = _attach(
        query,
        _female(query),
        _node(
            query,
            start,
            end,
            subject="member_month_snapshot",
            attribute="worth_grade",
            relation="transition",
            from_value="gold_worth_grade",
            to_value="vip",
        ),
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["to_value"] == "vip_worth_grade"
    _, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "PREV_WORTH_GRADE = 'GOLD'" in result["sql"]
    assert "WORTH_GRADE = 'VIP'" in result["sql"]


def test_live_mixed_worth_snapshot_pair_is_moved_to_one_relation_owner() -> None:
    """Replay live prompt #75 attempt 1, including both wrong evidence offsets."""
    query = "여성이면서 가치등급이 골드에서 VIP로 바뀐 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": {
                    "type": "and",
                    "operands": [
                        _female(query).to_dict(),
                        {
                            "type": "comparison",
                            "operator": "=",
                            "left": {
                                "type": "field",
                                "name": "member_month_snapshot.prev_worth_grade",
                            },
                            "right": {
                                "type": "literal",
                                "value": "gold_worth_grade",
                            },
                            "evidence": {
                                "text": "가치등급이 골드에서",
                                "start": 6,
                                "end": 14,
                            },
                        },
                        {
                            "type": "comparison",
                            "operator": "=",
                            "left": {
                                "type": "field",
                                "name": "member_month_snapshot.worth_grade",
                            },
                            "right": {
                                "type": "literal",
                                "value": "vip_worth_grade",
                            },
                            "evidence": {
                                "text": "VIP로 바뀐",
                                "start": 15,
                                "end": 22,
                            },
                        },
                    ],
                },
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["audience_requirement"]["expression"] == _female(query).to_dict()
    assert payload["audience_requirement"]["issues"] == []
    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["attribute"] == "member_worth_grade_transition"
    assert relation["from_value"] == "gold_worth_grade"
    assert relation["to_value"] == "vip_worth_grade"
    assert relation["source_span"] == "가치등급이 골드에서 VIP로 바뀐"
    assert any(
        decision.get("filter") == "semantic_relation_ownership.snapshot_transition"
        for decision in payload["decisions"]
    )
    _, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "GENDER_CD" in result["sql"]
    assert "PREV_WORTH_GRADE" in result["sql"]


def test_snapshot_pair_without_its_declared_axis_is_not_promoted() -> None:
    query = "여성이면서 골드에서 VIP로 바뀐 회원"
    previous_start = query.index("골드")
    current_start = query.index("VIP")
    payload = {
        "audience_requirement": {
            "expression": {
                "type": "and",
                "operands": [
                    _female(query).to_dict(),
                    {
                        "type": "comparison",
                        "operator": "=",
                        "left": {
                            "type": "field",
                            "name": "member_month_snapshot.prev_worth_grade",
                        },
                        "right": {"type": "literal", "value": "gold_worth_grade"},
                        "evidence": {
                            "text": "골드에서",
                            "start": previous_start,
                            "end": previous_start + len("골드에서"),
                        },
                    },
                    {
                        "type": "comparison",
                        "operator": "=",
                        "left": {
                            "type": "field",
                            "name": "member_month_snapshot.worth_grade",
                        },
                        "right": {"type": "literal", "value": "vip_worth_grade"},
                        "evidence": {
                            "text": "VIP로 바뀐",
                            "start": current_start,
                            "end": current_start + len("VIP로 바뀐"),
                        },
                    },
                ],
            },
            "issues": [],
        },
        "semantic_plan": {"nodes": []},
    }

    original = payload["audience_requirement"]["expression"]
    promotions = semantic_relation_ownership.promote_snapshot_transition_expression(
        payload, query, audience_runtime.catalog_snapshot()
    )

    assert promotions == []
    assert payload["audience_requirement"]["expression"] is original
    assert payload["semantic_plan"]["nodes"] == []


def test_live_purchase_duration_and_narrow_transition_span_are_owned() -> None:
    query = "최근 90일 동안 3회 이상 구매하고 골드에서 VIP로 승급한 회원"
    count_text = "3회 이상"
    count_start = query.index(count_text)
    transition_text = "승급"
    transition_start = query.index(transition_text)
    purchase = event_ir.Filter(
        event_ir.Source("purchase"),
        event_ir.TimeFilter(
            event_ir.FieldRef("purchase.occurred_at"),
            event_ir.RollingWindow(90, "day"),
        ),
    )
    expression = event_ir.And((
        event_ir.Comparison(
            ">=",
            event_ir.Aggregate(
                "count", purchase, event_ir.FieldRef("purchase.order_id"), True
            ),
            event_ir.Literal(3),
            _evidence(query, count_start, count_start + len(count_text)),
        ),
        event_ir.Exists(
            event_ir.Source("member_month_snapshot"),
            _evidence(
                query, transition_start, transition_start + len(transition_text)
            ),
        ),
    ))
    payload = _attach(
        query,
        expression,
        _node(
            query,
            transition_start,
            transition_start + len(transition_text),
            attribute="member_month_snapshot.grade",
            relation="transition",
            from_value="gold_grade",
            to_value="vip",
        ),
    )

    assert payload["audience_requirement"]["issues"] == []
    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["source_span"] == "골드에서 VIP로 승급"
    assert relation["attribute"] == "member_grade"
    plan, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "DATEADD(DAY, -90" in result["sql"]
    assert "PREV_ZTS_GRADE" in result["sql"]
    assert not plan.get("unresolved_source_conditions")


def test_live_physical_grade_transition_with_event_placeholder_is_owned() -> None:
    """Replay final5 prompt #74's physical-field + Exists placeholder wire."""
    query = "여성이면서 골드에서 VIP로 바뀐 회원"
    transition_start = query.index("골드")
    transition_end = query.index(" 회원")
    expression = event_ir.And((
        _female(query),
        event_ir.Exists(
            event_ir.Source("member_month_snapshot"),
            _evidence(query, transition_start, transition_end),
        ),
    ))
    payload = _attach(
        query,
        expression,
        _node(
            query,
            transition_start,
            transition_end,
            attribute="member_month_snapshot.grade",
            relation="transition",
            from_value="gold_grade",
            to_value="vip",
        ),
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["attribute"] == "member_grade"
    plan, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "GENDER_CD" in result["sql"]
    assert "PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in result["sql"]
    assert "ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in result["sql"]
    assert not plan.get("unresolved_source_conditions")


def test_unscoped_duration_stays_unowned_when_two_windows_match() -> None:
    query = "최근 90일 동안 구매와 로그인을 한 회원"
    binding = next(
        item
        for item in extract_literal_bindings(query, current_date="2026-08-04")
        if item["kind"] == "duration"
    )
    evidence_start = query.index("구매")
    evidence = _evidence(query, evidence_start, evidence_start + len("구매"))

    def timed_exists(source: str) -> event_ir.Exists:
        return event_ir.Exists(
            event_ir.Filter(
                event_ir.Source(source),
                event_ir.TimeFilter(
                    event_ir.FieldRef(f"{source}.occurred_at"),
                    event_ir.RollingWindow(90, "day"),
                ),
            ),
            evidence,
        )

    issues = canonical_audience_claims.literal_claim_issues(
        query,
        event_ir.And((timed_exists("purchase"), timed_exists("login"))),
        [binding],
    )

    assert [issue["argument"] for issue in issues] == ["literal_bindings[0]"]


def test_lowered_history_receipt_discharges_only_its_source_obligation() -> None:
    query = "최근 90일 동안 3회 이상 구매하고 골드에서 VIP로 승급한 회원"
    purchase = event_ir.Filter(
        event_ir.Source("purchase"),
        event_ir.TimeFilter(
            event_ir.FieldRef("purchase.occurred_at"), event_ir.RollingWindow(90, "day")
        ),
    )
    expression = event_ir.Comparison(
        ">=",
        event_ir.Aggregate("count", purchase, event_ir.FieldRef("purchase.order_id"), True),
        event_ir.Literal(3),
        _evidence(query, 0, 20),
    )
    payload = _attach(
        query, expression,
        _node(
            query, 21, 33, attribute="member_grade", relation="transition",
            from_value="gold_grade", to_value="vip",
        ),
    )

    assert payload["audience_requirement"]["issues"] == []
    plan, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "CRM_SL_ORDERHEADERMALL" in result["sql"] and "PREV_ZTS_GRADE" in result["sql"]
    assert not plan.get("unresolved_source_conditions")


def test_as_of_literal_is_deferred_then_coverage_gap_is_unsupported() -> None:
    query = "여성이면서 지난달 말 기준 VIP였던 회원"
    payload = _attach(
        query,
        _female(query),
        _node(
            query, 6, 20, attribute="member_grade", relation="as_of", value="vip",
            value_comparison="eq", period={"type": "calendar_month", "year": 2026, "month": 7},
        ),
    )

    assert payload["audience_requirement"]["issues"] == []
    assert payload["semantic_plan"]["nodes"][0]["period"]["type"] == "interval"
    plan, result = _sql_result(query, payload)
    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert plan["semantic_ir"]["unsupported_operations"][0]["kind"] == "data_coverage_gap"


def test_empty_as_of_period_does_not_promote_physical_snapshot_field() -> None:
    query = "여성이면서 기준 VIP였던 회원"
    start = query.index("기준")
    end = query.index(" 회원")
    payload = _attach(
        query,
        _female(query),
        _node(
            query,
            start,
            end,
            attribute="member_month_snapshot.grade",
            relation="as_of",
            value="vip",
            value_comparison="eq",
            period={},
        ),
    )

    assert payload["semantic_plan"]["nodes"][0]["attribute"] == (
        "member_month_snapshot.grade"
    )
    _, result = _sql_result(query, payload)
    assert not result["is_success"]
    assert result.get("sql") is None


def test_live_physical_grade_field_uses_declared_as_of_coverage_owner() -> None:
    """Replay live prompt #76, including its singleton And and raw period shape."""
    query = "여성이면서 지난달 말 기준 VIP였던 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": {
                    "type": "and",
                    "operands": [{
                        "type": "comparison",
                        "operator": "=",
                        "left": {"type": "field", "name": "subject.gender"},
                        "right": {"type": "literal", "value": "female"},
                        "evidence": {"text": "여성이면서", "start": 0, "end": 5},
                    }],
                },
                "issues": [],
            },
            "semantic_plan": {
                "nodes": [{
                    "id": "req-1",
                    "type": "relation_predicate",
                    "source_span": "지난달 말 기준 VIP였던",
                    "source_start": 6,
                    "source_end": 20,
                    "confidence": 0.9,
                    "subject": "member",
                    "attribute": "member_month_snapshot.grade",
                    "relation": "as_of",
                    "value": "vip",
                    "value_comparison": "eq",
                    "from_value": None,
                    "to_value": None,
                    "period": {
                        "type": "absolute",
                        "year": None,
                        "month": None,
                        "from": "2026-07-01",
                        "to": "2026-08-01",
                        "value": None,
                        "unit": None,
                        "label": "2026년 7월",
                    },
                    "months": None,
                    "count": None,
                    "count_operator": None,
                }],
            },
        },
        query,
        current_date="2026-08-04",
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["attribute"] == "member_grade"
    assert relation["period"] == {
        "type": "interval",
        "start": "2026-07-01",
        "end_exclusive": "2026-08-01",
    }
    plan, result = _sql_result(query, payload)
    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"
    unsupported = plan["semantic_ir"]["unsupported_operations"]
    assert unsupported[0]["kind"] == "data_coverage_gap"
    assert unsupported[0]["evidence"] == "지난달 말 기준 VIP였던"
    assert "2026-07-31" in unsupported[0]["reason"]
    assert "2017-01-31" in unsupported[0]["reason"]


def test_physical_grade_field_as_of_inside_declared_coverage_still_compiles() -> None:
    query = "여성이면서 2017년 1월 말 기준 VIP였던 회원"
    start = query.index("2017년")
    end = query.index(" 회원")
    payload = _attach(
        query,
        _female(query),
        _node(
            query,
            start,
            end,
            attribute="member_month_snapshot.grade",
            relation="as_of",
            value="vip",
            value_comparison="eq",
            period={"type": "calendar_month", "year": 2017, "month": 1},
        ),
    )

    assert payload["semantic_plan"]["nodes"][0]["attribute"] == "member_grade"
    _, result = _sql_result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "GENDER_CD" in result["sql"]
    assert "YYYYMM" in result["sql"] and "201701" in result["sql"]


def test_history_node_cannot_hide_unrelated_login_catalog_issue() -> None:
    query = "앱으로 로그인하지 않았고 골드에서 VIP로 바뀐 회원"
    node = _node(
        query, 14, 26, attribute="member_grade", relation="transition",
        from_value="gold_grade", to_value="vip",
    )
    issue = {
        "code": "validation_mismatch",
        "argument": "catalog_value.subject.last_login_channel",
        "message": "unrelated",
        "evidence": {"text": query[0:3], "start": 0, "end": 3},
    }
    assert not semantic_relation_ownership.relation_node_owns_issue(
        issue, node, query, audience_runtime.catalog_snapshot()
    )
