"""Live regressions for snapshot history depth and previous-grade ownership."""

from __future__ import annotations

import copy

import networkx as nx
import pytest

import audience_runtime
import graph_rag
import semantic_relation_ownership
import targeting_domain
import temporal_semantics
from query_structurer.campaign_plan_v4 import attach_campaign_query_plan_v4_identity


def _issue(query: str, argument: str, text: str) -> dict:
    start = query.index(text)
    return {
        "code": "unsupported_semantics",
        "argument": argument,
        "message": "model-authored hypothesis",
        "evidence": {"text": text, "start": start, "end": start + len(text)},
    }


def _node(query: str, text: str, **values: object) -> dict:
    start = query.index(text)
    return {
        "id": "req-1",
        "type": "relation_predicate",
        "source_span": text,
        "source_start": start,
        "source_end": start + len(text),
        "subject": "member",
        **values,
    }


def _attach(
    query: str,
    node: dict,
    *,
    expression: dict | None = None,
    issues: list[dict] | None = None,
) -> dict:
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": expression,
                "issues": list(issues or []),
            },
            "semantic_plan": {"nodes": [node]},
        },
        query,
        current_date="2026-08-04",
    )


def _result(query: str, payload: dict) -> tuple[dict, dict]:
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=payload)
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


@pytest.mark.parametrize(
    ("query", "attribute", "month", "issue_text"),
    [
        ("지난달 말 기준 VIP였던 회원을 찾아줘", "grade", 7, "지난달 말 기준"),
        ("이번 달 기준 골드 등급 회원", "member_month_snapshot.grade", 8, None),
    ],
)
def test_live_as_of_month_outside_declared_snapshot_coverage_is_unsupported(
    query: str, attribute: str, month: int, issue_text: str | None
) -> None:
    value = "vip" if month == 7 else "gold_grade"
    value_text = "VIP" if month == 7 else "골드 등급"
    value_start = query.index(value_text)
    expression = None
    if issue_text is None:
        expression = {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": "subject.grade"},
            "right": {"type": "literal", "value": value},
            "evidence": {
                "text": value_text,
                "start": value_start,
                "end": value_start + len(value_text),
            },
        }
    node_text = query if month == 8 else "지난달 말 기준 VIP였던"
    payload = _attach(
        query,
        _node(
            query,
            node_text,
            attribute=attribute,
            relation="as_of",
            value=value,
            value_comparison="eq",
            period={"type": "calendar_month", "year": 2026, "month": month},
        ),
        expression=expression,
        issues=(
            [_issue(query, "compiler_operation_unsupported", issue_text)]
            if issue_text is not None
            else []
        ),
    )

    assert payload["semantic_plan"]["nodes"][0]["attribute"] == "member_grade"
    assert payload["semantic_ir"]["status"] == "unsupported"
    assert payload["semantic_ir"]["unsupported_operations"][0]["kind"] == (
        "data_coverage_gap"
    )
    plan, result = _result(query, payload)
    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"
    assert plan["semantic_ir"]["status"] == "unsupported"


def test_live_this_month_interval_reprojects_coverage_when_derived_ir_is_lost() -> None:
    """Replay #16's strict raw node and the candidate-coercion loss boundary."""
    query = "이번 달 기준 골드 등급 회원"
    value_start = query.index("골드 등급")
    payload = _attach(
        query,
        _node(
            query,
            query,
            subject="member",
            attribute="member_month_snapshot.grade",
            relation="as_of",
            value="gold_grade",
            value_comparison="eq",
            from_value=None,
            to_value=None,
            period={"type": "calendar_month", "year": 2026, "month": 8},
            months=None,
            count=None,
            count_operator=None,
        ),
        expression={
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": "subject.grade"},
            "right": {"type": "literal", "value": "gold_grade"},
            "evidence": {
                "text": "골드 등급",
                "start": value_start,
                "end": value_start + len("골드 등급"),
            },
        },
    )

    assert payload["semantic_plan"]["nodes"][0]["period"] == {
        "type": "interval",
        "start": "2026-08-01",
        "end_exclusive": "2026-09-01",
    }
    # Re-emission/candidate reconstruction carries source nodes, not the
    # derived semantic_ir verdict.  SQL delivery must re-derive coverage.
    payload.pop("semantic_ir")
    plan, result = _result(query, payload)

    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"
    unsupported = plan["semantic_ir"]["unsupported_operations"]
    assert unsupported[0]["kind"] == "data_coverage_gap"
    assert "2026-08-31" in unsupported[0]["reason"]


def test_live_this_month_snapshot_comparison_is_promoted_before_retry() -> None:
    """Replay #16's first model attempt, including its repairable bad offsets."""
    query = "이번 달 기준 골드 등급 회원"
    payload = attach_campaign_query_plan_v4_identity(
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
                "expression": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {
                        "type": "field",
                        "name": "member_month_snapshot.grade",
                    },
                    "right": {"type": "literal", "value": "gold_grade"},
                    "evidence": {
                        "text": "골드 등급 회원",
                        "start": 5,
                        "end": 10,
                    },
                },
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["audience_requirement"] == {"expression": None, "issues": []}
    node = payload["semantic_plan"]["nodes"][0]
    assert (node["attribute"], node["relation"], node["value"]) == (
        "member_grade",
        "as_of",
        "gold_grade",
    )
    assert node["period"] == {
        "type": "interval",
        "start": "2026-08-01",
        "end_exclusive": "2026-09-01",
    }
    assert payload["semantic_ir"]["status"] == "unsupported"
    assert payload["semantic_ir"]["unsupported_operations"][0]["kind"] == (
        "data_coverage_gap"
    )
    assert payload["decisions"][0]["filter"] == (
        "semantic_relation_ownership.snapshot_as_of"
    )
    _, result = _result(query, payload)
    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"


def test_live_snapshot_value_only_evidence_is_promoted_with_verified_member_tail() -> None:
    """Replay the second valid #16 evidence boundary seen in live logs."""
    query = "이번 달 기준 골드 등급 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {
                        "type": "field",
                        "name": "member_month_snapshot.grade",
                    },
                    "right": {"type": "literal", "value": "gold_grade"},
                    "evidence": {"text": "골드 등급", "start": 8, "end": 13},
                },
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    node = payload["semantic_plan"]["nodes"][0]
    assert node["source_span"] == query
    assert node["attribute"] == "member_grade"
    assert payload["semantic_ir"]["unsupported_operations"][0]["kind"] == (
        "data_coverage_gap"
    )


def test_this_month_as_of_bridge_is_not_added_to_shared_history_lexicon() -> None:
    query = "이번 달 기준 구매금액이 10만원 이상인 VIP 상품 구매 회원"

    assert not any(
        marker.operator == temporal_semantics.AS_OF
        for marker in targeting_domain.temporal_lexicon().detect(query)
    )


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("이번 달 골드 등급 회원", "member_month_snapshot.grade"),
        ("이번 달 기준 골드 등급 여성 회원", "member_month_snapshot.grade"),
        ("이번 달과 지난달 기준 골드 등급 회원", "member_month_snapshot.grade"),
        ("이번 달 기준 골드 등급 회원", "subject.grade"),
    ],
)
def test_snapshot_comparison_without_closed_as_of_receipt_is_not_promoted(
    query: str, field: str
) -> None:
    evidence_text = query[query.index("골드"):]
    evidence_start = query.index(evidence_text)
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": field},
                    "right": {"type": "literal", "value": "gold_grade"},
                    "evidence": {
                        "text": evidence_text,
                        "start": evidence_start,
                        "end": len(query),
                    },
                },
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["semantic_plan"]["nodes"] == []
    assert not any(
        decision.get("filter") == "semantic_relation_ownership.snapshot_as_of"
        for decision in payload.get("decisions") or []
    )


def test_snapshot_comparison_with_another_executable_claim_is_not_promoted() -> None:
    query = "2017년 1월 기준 골드 등급 회원"
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "target_user": {},
            "exclude": {"gender": ["male"]},
            "campaign_constraints": {},
            "result_limit": None,
            "audience_requirement": {
                "expression": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {
                        "type": "field",
                        "name": "member_month_snapshot.grade",
                    },
                    "right": {"type": "literal", "value": "gold_grade"},
                    "evidence": {"text": "골드 등급 회원", "start": 12, "end": 20},
                },
                "issues": [],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date="2026-08-04",
    )

    assert payload["semantic_plan"]["nodes"] == []
    assert payload["exclude"]["gender"] == ["male"]
    assert not any(
        decision.get("filter") == "semantic_relation_ownership.snapshot_as_of"
        for decision in payload.get("decisions") or []
    )


def test_snapshot_comparison_with_ambiguous_metric_owner_is_not_promoted() -> None:
    query = "이번 달 기준 골드 등급 회원"
    catalog = copy.deepcopy(audience_runtime.catalog_snapshot())
    catalog["metrics"]["duplicate_member_grade"] = copy.deepcopy(
        catalog["metrics"]["member_grade"]
    )
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {},
        "result_limit": None,
        "literal_bindings": [{
            "kind": "date_window",
            "text": "이번 달",
            "start": 0,
            "end": 4,
            "normalized": {
                "event_ir_window": {
                    "type": "interval",
                    "start": "2026-08-01",
                    "end_exclusive": "2026-09-01",
                },
            },
        }],
        "audience_requirement": {
            "expression": {
                "type": "comparison",
                "operator": "=",
                "left": {
                    "type": "field",
                    "name": "member_month_snapshot.grade",
                },
                "right": {"type": "literal", "value": "gold_grade"},
                "evidence": {"text": "골드 등급 회원", "start": 8, "end": 16},
            },
            "issues": [],
        },
        "semantic_plan": {"nodes": []},
    }

    assert semantic_relation_ownership.promote_snapshot_as_of_expression(
        payload, query, catalog
    ) is None
    assert payload["semantic_plan"]["nodes"] == []


def test_snapshot_comparison_requires_explicit_empty_issue_list() -> None:
    query = "이번 달 기준 골드 등급 회원"
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {},
        "result_limit": None,
        "literal_bindings": [{
            "kind": "date_window",
            "text": "이번 달",
            "start": 0,
            "end": 4,
            "normalized": {
                "event_ir_window": {
                    "type": "interval",
                    "start": "2026-08-01",
                    "end_exclusive": "2026-09-01",
                },
            },
        }],
        "audience_requirement": {
            "expression": {
                "type": "comparison",
                "operator": "=",
                "left": {
                    "type": "field",
                    "name": "member_month_snapshot.grade",
                },
                "right": {"type": "literal", "value": "gold_grade"},
                "evidence": {"text": "골드 등급", "start": 8, "end": 13},
            },
        },
        "semantic_plan": {"nodes": []},
    }

    assert semantic_relation_ownership.promote_snapshot_as_of_expression(
        payload, query, audience_runtime.catalog_snapshot()
    ) is None
    assert payload["semantic_plan"]["nodes"] == []


def test_rebuilt_normalized_period_reprojects_coverage_before_history_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-emitted typed plan cannot discard its candidate coverage verdict."""
    query = "이번 달 기준 골드 등급 회원"
    raw_node = _node(
        query,
        query,
        attribute="member_grade",
        relation="as_of",
        value="gold_grade",
        value_comparison="eq",
        period=None,
    )
    rebuilt_node = copy.deepcopy(raw_node)
    rebuilt_node["period"] = {"from": "20260801", "to": "20260831"}
    plan = {
        "target_user": {"gender": "female"},
        "audience_requirement": {"expression": None, "issues": []},
        "semantic_plan": {"nodes": [raw_node]},
        "semantic_ir": {"status": "resolved", "unsupported_operations": []},
    }

    def rebuild(query_plan: dict, *_args: object, **_kwargs: object) -> None:
        query_plan["semantic_plan"] = {"nodes": [rebuilt_node]}
        query_plan["semantic_ir"] = {
            "status": "resolved",
            "unsupported_operations": [],
        }

    monkeypatch.setattr(graph_rag.semantic_plan_bridge, "apply", rebuild)
    monkeypatch.setattr(
        graph_rag.member_attribute_history,
        "apply",
        lambda *_args, **_kwargs: pytest.fail(
            "history compiler must not run after a rebuilt coverage gap"
        ),
    )

    graph_rag._apply_semantic_plan_pipeline(plan, query)

    assert plan["semantic_ir"]["status"] == "unsupported"
    unsupported = plan["semantic_ir"]["unsupported_operations"]
    assert unsupported[0]["kind"] == "data_coverage_gap"
    assert "2026-08-31" in unsupported[0]["reason"]


def test_live_previous_grade_field_reaches_prev_snapshot_sql() -> None:
    query = "직전 등급이 골드였던 회원"
    payload = _attach(
        query,
        _node(
            query,
            "직전 등급",
            attribute="member_month_snapshot.prev_grade",
            relation="as_of",
            value="gold_grade",
            value_comparison="eq",
            period=None,
        ),
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["attribute"] == "member_grade"
    assert relation["relation"] == "transition"
    assert relation["from_value"] == "gold_grade"
    assert relation["source_span"] == query
    assert payload["audience_requirement"]["issues"] == []
    _, result = _result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in result["sql"]
    assert "ZTS_GRADE != MS.PREV_ZTS_GRADE" in result["sql"]


def test_live_previous_grade_short_leaf_is_canonicalized_by_unique_source() -> None:
    """Replay #19's exact short-field wire shape."""
    query = "직전 등급이 골드였던 회원"
    payload = _attach(
        query,
        _node(
            query,
            "직전 등급이 골드였던",
            subject="member_month_snapshot",
            attribute="prev_grade",
            relation="as_of",
            value="gold_grade",
            value_comparison="eq",
            period=None,
            from_value=None,
            to_value=None,
            months=None,
            count=None,
            count_operator=None,
        ),
    )

    relation = payload["semantic_plan"]["nodes"][0]
    assert relation["attribute"] == "member_grade"
    assert relation["relation"] == "transition"
    assert relation["from_value"] == "gold_grade"
    assert relation["source_span"] == query
    assert payload["audience_requirement"]["issues"] == []
    _, result = _result(query, payload)
    assert result["is_success"], result.get("clarification_questions")
    assert "PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in result["sql"]
    assert "ZTS_GRADE != MS.PREV_ZTS_GRADE" in result["sql"]


@pytest.mark.parametrize("subject", ["member", "purchase"])
def test_previous_grade_short_leaf_without_matching_catalog_source_stays_unowned(
    subject: str,
) -> None:
    query = "직전 등급이 골드였던 회원"
    payload = {
        "semantic_plan": {"nodes": [_node(
            query,
            "직전 등급이 골드였던",
            subject=subject,
            attribute="prev_grade",
            relation="as_of",
            value="gold_grade",
            period=None,
        )]},
    }

    semantic_relation_ownership.normalize_relation_node_claims(
        payload, query, audience_runtime.catalog_snapshot()
    )

    assert payload["semantic_plan"]["nodes"][0]["attribute"] == "prev_grade"


def test_previous_grade_short_leaf_with_duplicate_source_candidates_stays_unowned() -> None:
    query = "직전 등급이 골드였던 회원"
    catalog = copy.deepcopy(audience_runtime.catalog_snapshot())
    catalog["fields"]["shadow.prev_grade"] = copy.deepcopy(
        catalog["fields"]["member_month_snapshot.prev_grade"]
    )
    payload = {
        "semantic_plan": {"nodes": [_node(
            query,
            "직전 등급이 골드였던",
            subject="member_month_snapshot",
            attribute="prev_grade",
            relation="as_of",
            value="gold_grade",
            period=None,
        )]},
    }

    semantic_relation_ownership.normalize_relation_node_claims(payload, query, catalog)

    assert payload["semantic_plan"]["nodes"][0]["attribute"] == "prev_grade"


@pytest.mark.parametrize(
    ("query", "node", "expression", "issues", "required_months"),
    [
        (
            "3개월 내내 VIP를 유지한 회원",
            {
                "attribute": "member_month_snapshot.grade",
                "relation": "held_throughout",
                "value": "vip",
                "value_comparison": "eq",
                "period": {"type": "relative", "value": 3, "unit": "months"},
                "months": 3,
            },
            None,
            ("expression", "3개월"),
            3,
        ),
        (
            "등급이 2회 이상 변경된 회원",
            {
                "attribute": "grade",
                "relation": "changed_n_times",
                "count": 2,
                "count_operator": ">=",
            },
            {
                "type": "exists",
                "relation": {"type": "source", "name": "member_month_snapshot"},
                "evidence": {"text": "2회 이상 변경된 회원", "start": 4, "end": 16},
            },
            None,
            3,
        ),
        (
            "최근 6개월 매월 존재한 회원",
            {
                "attribute": "member_month_snapshot",
                "relation": "exists_every_month",
                "period": {"type": "relative", "value": 6, "unit": "months"},
                "months": 6,
            },
            None,
            ("attribute", "최근 6개월 매월 존재한 회원"),
            6,
        ),
    ],
)
def test_live_multi_month_history_is_blocked_before_empty_sql(
    query: str,
    node: dict,
    expression: dict | None,
    issues: tuple[str, str] | None,
    required_months: int,
) -> None:
    payload = _attach(
        query,
        _node(query, query if expression is None else expression["evidence"]["text"], **node),
        expression=expression,
        issues=[_issue(query, *issues)] if issues is not None else [],
    )

    unsupported = payload["semantic_ir"]["unsupported_operations"]
    assert payload["semantic_ir"]["status"] == "unsupported"
    assert unsupported[0]["kind"] == "data_coverage_gap"
    assert f"requires {required_months} distinct monthly snapshots" in unsupported[0]["reason"]
    _, result = _result(query, payload)
    assert not result["is_success"] and result.get("sql") is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"


@pytest.mark.parametrize("operator", ["<", "<=", "lt", "lte"])
def test_upper_bounded_change_count_does_not_invent_minimum_months(
    operator: str,
) -> None:
    query = "등급 변경 횟수가 2회 이하인 회원"
    payload = {
        "semantic_plan": {
            "nodes": [_node(
                query,
                query,
                attribute="grade",
                relation="changed_n_times",
                count=2,
                count_operator=operator,
            )],
        },
    }

    assert semantic_relation_ownership.relation_data_coverage_gaps(
        payload, query, audience_runtime.catalog_snapshot()
    ) == []
