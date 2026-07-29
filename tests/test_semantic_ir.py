from __future__ import annotations

import copy

import networkx as nx
import pytest

import graph_rag
from query_structurer.campaign_plan_v3 import (
    attach_campaign_query_plan_v3_identity,
    validate_campaign_query_plan_v3,
)
from query_structurer.semantic_ir import (
    extract_literal_bindings,
    materialize_semantic_operations,
)


QUERY = "2026년 2월과 3월의 구매 금액 차이가 10% 이상 증가한 고객 리스트"


def _semantic_ir(status: str = "resolved") -> dict:
    return {
        "status": status,
        "operations": [
            {
                "kind": "period_over_period_change",
                "metric_id": "purchase_amount",
                "direction": "increase",
                "bindings": [
                    {"role": "baseline", "literal_id": "date_window_1"},
                    {"role": "current", "literal_id": "date_window_2"},
                    {"role": "threshold", "literal_id": "percentage_1"},
                    {"role": "comparison", "literal_id": "comparison_operator_1"},
                ],
            }
        ],
        "missing_fields": [],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }


def _payload(semantic_ir: dict) -> dict:
    return {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {"objective": "purchase"},
        "aggregation_request": None,
        "set_expressions": [],
        "computed_metrics": [],
        "result_limit": None,
        "semantic_evidence": [
            {
                "path": "semantic_ir.operations[0]",
                "text": QUERY,
                "start": 0,
                "end": len(QUERY),
                "confidence": 0.99,
            }
        ],
        "semantic_ir": semantic_ir,
        "unresolved": [],
    }


def test_literal_extractor_owns_dates_percentage_and_operator() -> None:
    literals = extract_literal_bindings(QUERY, current_date="2026-07-29")

    assert [(item["id"], item["normalized"]) for item in literals] == [
        (
            "date_window_1",
            {"from": "20260201", "to": "20260228", "label": "2026년 2월"},
        ),
        (
            "date_window_2",
            {"from": "20260301", "to": "20260331", "label": "2026년 3월"},
        ),
        ("percentage_1", {"value": 10, "unit": "percent"}),
        ("comparison_operator_1", ">="),
    ]


def test_semantic_ir_materializes_metric_trend_without_model_values() -> None:
    literals = extract_literal_bindings(QUERY, current_date="2026-07-29")
    slots = materialize_semantic_operations(_semantic_ir(), literals)

    assert slots["metric_trend"]["baseline"]["from"] == "20260201"
    assert slots["metric_trend"]["current"]["to"] == "20260331"
    assert slots["metric_trend"]["relative_change"] == {
        "unit": "percent",
        "comparisons": [{"operator": ">=", "value": 10}],
    }


def test_resolved_v3_ir_projects_into_existing_sql_compiler() -> None:
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(
            _payload(_semantic_ir()), QUERY, current_date="2026-07-29"
        ),
        query=QUERY,
    )
    plan = graph_rag.build_query_plan(
        QUERY,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": QUERY, "channel": ""},
    )

    assert plan["parser"]["authority"] == "llm_first"
    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["target_user"]["metric_trend"]["baseline"]["from"] == "20260201"
    sql = graph_rag.build_metric_trend_targets_sql_candidate(plan)["sql"]
    assert "ORDER_DATE BETWEEN '20260201' AND '20260228'" in sql
    assert "NULLIF(M2.TREND_VALUE, 0)) >= 10" in sql


def test_unique_evidence_text_repairs_model_offset() -> None:
    payload = _payload(_semantic_ir())
    payload["semantic_evidence"] = [
        {
            "path": "semantic_ir.operations[0].metric_id",
            "text": "구매 금액",
            "start": 13,
            "end": 18,
            "confidence": 0.9,
        }
    ]
    attached = attach_campaign_query_plan_v3_identity(
        payload, QUERY, current_date="2026-07-29"
    )

    plan = validate_campaign_query_plan_v3(attached, query=QUERY)

    assert QUERY[plan["semantic_evidence"][0]["start"]:plan["semantic_evidence"][0]["end"]] == "구매 금액"


def test_v3_rejects_model_literal_not_extracted_from_source() -> None:
    semantic_ir = _semantic_ir()
    semantic_ir["operations"][0]["bindings"][2]["literal_id"] = "percentage_99"
    payload = attach_campaign_query_plan_v3_identity(
        _payload(semantic_ir), QUERY, current_date="2026-07-29"
    )

    with pytest.raises(ValueError, match="unknown literal"):
        validate_campaign_query_plan_v3(payload, query=QUERY)


def test_two_unspecified_months_require_clarification_and_never_compile() -> None:
    query = "두 월의 구매 금액을 비교한 고객 리스트"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["comparison_month_1", "comparison_month_2"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "비교할 두 월을 지정해 주세요.",
    }
    payload = attach_campaign_query_plan_v3_identity(
        _payload(semantic_ir), query, current_date="2026-07-29"
    )
    # Replace evidence because _payload uses the resolved example source.
    payload["semantic_evidence"] = []
    plan = validate_campaign_query_plan_v3(payload, query=query)

    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert result is not None
    assert result["sql"] is None
    assert result["interpretation_status"] == "needs_clarification"
    assert result["clarification_questions"] == ["비교할 두 월을 지정해 주세요."]


def test_entity_ranking_window_resolves_stale_purchase_date_missing_field() -> None:
    query = "2019년 5월에 가장 잘 팔린 제품 5개를 산 고객 추출해줘"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["purchase_date"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }
    payload = _payload(semantic_ir)
    payload["target_user"] = {
        "entity_set_condition": {
            "derived_set_ast": {
                "type": "member_set",
                "relation": "top",
                "exists": True,
                "source": {
                    "type": "ranking",
                    "direction": "top",
                    "limit": 5,
                    "source": {
                        "type": "aggregation",
                        "relation": "purchase",
                        "group_by": "product_id",
                        "measure": "count",
                        "window": {},
                        "filters": [
                            {
                                "type": "dimension_filter",
                                "dimension": "purchase_date",
                                "operator": "contains",
                                "value": "2019년 5월",
                            }
                        ],
                    },
                },
            }
        }
    }
    payload["semantic_evidence"] = []
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(
            payload, query, current_date="2026-07-29"
        ),
        query=query,
    )

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": query, "channel": ""},
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert plan["semantic_ir_reconciliation"]["resolved_fields"] == [
        {
            "field": "purchase_date",
            "resolved_by": "target_user.entity_set_condition.window",
            "reason": "ranking_window_owns_purchase_date",
        }
    ]
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is True
    assert "SELECT TOP 5 D.PRODUCT_ID" in result["sql"]
    assert "D.ORDER_DATE BETWEEN '20190501' AND '20190531'" in result["sql"]


def test_compilable_entity_ranking_overrides_stale_purchase_object_missing_field() -> None:
    query = "2019년 하반기 가장 잘 팔린 제품 11개를 산 고객 추출하고 남성을 빼줘"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["target_user.purchase_object"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }
    payload = _payload(semantic_ir)
    # Reproduce the LLM's two false inferences: requiring a concrete product and
    # rewriting the explicit male exclusion as a positive female condition.
    payload["target_user"] = {"gender": "female"}
    payload["exclude"] = {"gender": ["male"], "interests": [], "lifecycle": []}
    payload["semantic_evidence"] = []
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(
            payload, query, current_date="2026-07-29"
        ),
        query=query,
    )

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": query, "channel": ""},
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert plan["target_user"]["gender"] is None
    assert plan["exclude"]["gender"] == ["male"]
    assert plan["set_expressions"] == []
    assert plan["semantic_ir_reconciliation"]["resolved_fields"] == [
        {
            "field": "target_user.purchase_object",
            "resolved_by": "target_user.entity_set_condition.derived_set_ast",
            "reason": "entity_ranking_owns_purchase_object",
        }
    ]
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is True
    assert "SELECT TOP 11 D.PRODUCT_ID" in result["sql"]
    assert "D.ORDER_DATE BETWEEN '20190701' AND '20191231'" in result["sql"]
    assert "B.GENDER_CD <> 'GENDER_CD.MALE'" in result["sql"]


def test_explicit_positive_gender_is_not_removed_by_exclusion_reconciliation() -> None:
    plan = {
        "target_user": {"gender": "female"},
        "exclude": {"gender": ["male"]},
    }

    graph_rag._reconcile_deterministic_member_exclusions(
        "여성 고객을 대상으로 하되 남성은 제외해줘", plan
    )

    assert plan["target_user"]["gender"] == "female"
    assert plan["exclude"]["gender"] == ["male"]


def test_purchase_object_missing_without_compilable_entity_ranking_still_blocks() -> None:
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["target_user.purchase_object"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "구매 상품을 지정해 주세요.",
    }
    plan = {"semantic_ir": semantic_ir, "target_user": {}}

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)
    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert result is not None
    assert result["clarification_questions"] == ["구매 상품을 지정해 주세요."]


def test_unowned_purchase_date_missing_field_still_blocks() -> None:
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["purchase_date"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "구매 기간을 지정해 주세요.",
    }
    plan = {"semantic_ir": semantic_ir, "target_user": {}}

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)
    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert result is not None
    assert result["clarification_questions"] == ["구매 기간을 지정해 주세요."]


def test_unsupported_operation_is_distinct_from_clarification() -> None:
    semantic_ir = {
        "status": "unsupported",
        "operations": [],
        "missing_fields": [],
        "policy_applications": [],
        "unsupported_operations": [
            {
                "kind": "forecast_purchase_growth",
                "reason": "forecast_not_supported",
                "evidence": "예측",
            }
        ],
        "message": "미래 구매금액 예측은 지원하지 않습니다.",
    }
    result = graph_rag._semantic_ir_blocking_sql_result(
        {"semantic_ir": copy.deepcopy(semantic_ir)}
    )

    assert result is not None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"


def _rules_plan_without_slot_llm(monkeypatch: pytest.MonkeyPatch, query: str) -> dict:
    monkeypatch.setattr(graph_rag, "_apply_llm_condition_slot_fallback", lambda *_a, **_kw: None)
    return graph_rag.build_query_plan(query, parser="rules")


def _dimension_filter(plan: dict, code: str) -> dict:
    return next(
        item
        for item in plan.get("dimension_filters", [])
        if code in item.get("codes", [])
    )


def test_value_polarity_survives_entity_ir_merge_and_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "2019년 하반기 가장 잘 팔린 제품 11개를 산 고객 추출하고 남성도 빼주고 서울사는 고객도 빼줘"
    plan = _rules_plan_without_slot_llm(monkeypatch, query)

    assert plan["target_user"]["entity_set_condition"]["negated"] is False
    assert plan["target_user"]["gender"] is None
    assert plan["exclude"]["gender"] == ["male"]
    seoul = _dimension_filter(plan, "서울")
    assert seoul["operator"] == "NOT_IN"
    assert seoul["polarity"] == "exclude"
    assert seoul["evidence"] == "서울사는 고객도 빼줘"

    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is True
    assert "B.GENDER_CD <> 'GENDER_CD.MALE'" in result["sql"]
    assert "B.SIDO NOT IN ('서울')" in result["sql"]


def test_member_value_filters_split_include_and_exclude_for_same_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _rules_plan_without_slot_llm(monkeypatch, "경기는 포함하고 서울은 빼줘")

    assert _dimension_filter(plan, "경기")["operator"] == "IN"
    assert _dimension_filter(plan, "서울")["operator"] == "NOT_IN"
    compiled = graph_rag.compile_member_target_conditions(plan)
    assert "B.SIDO IN ('경기')" in compiled["predicates"]
    assert "B.SIDO NOT IN ('서울')" in compiled["predicates"]


def test_connected_values_and_mixed_clauses_keep_independent_polarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = _rules_plan_without_slot_llm(monkeypatch, "남성과 서울 고객은 빼줘")
    assert connected["exclude"]["gender"] == ["male"]
    assert _dimension_filter(connected, "서울")["operator"] == "NOT_IN"

    mixed = _rules_plan_without_slot_llm(monkeypatch, "남성은 빼고 서울은 포함해줘")
    assert mixed["target_user"]["gender"] is None
    assert mixed["exclude"]["gender"] == ["male"]
    assert _dimension_filter(mixed, "서울")["operator"] == "IN"


@pytest.mark.parametrize(
    "query",
    [
        "서울 고객은 빼지 말아줘",
        "서울을 제외할 필요는 없어",
    ],
)
def test_negated_exclusion_does_not_create_not_in(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    plan = _rules_plan_without_slot_llm(monkeypatch, query)

    assert _dimension_filter(plan, "서울")["operator"] == "IN"
    assert not any(item.get("operator") == "NOT_IN" for item in plan["dimension_filters"])


@pytest.mark.parametrize(
    "query",
    [
        "남성을 빼달라는 뜻은 아니야",
        "남성은 제외라고 했지만 이번에는 포함해줘",
    ],
)
def test_gender_exclusion_cancellation_does_not_infer_opposite_gender(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    plan = _rules_plan_without_slot_llm(monkeypatch, query)

    assert plan["target_user"]["gender"] == "male"
    assert plan["exclude"]["gender"] == []


def test_conflicting_dimension_polarity_is_blocked_before_sql() -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "dimension_filters": [
            {
                "dimension_id": "member_value:SIDO", "table": "CRM_MB_BASEINFO",
                "column": "CRM_MB_BASEINFO.SIDO", "operator": "IN", "codes": ["서울"],
            },
            {
                "dimension_id": "member_value:SIDO", "table": "CRM_MB_BASEINFO",
                "column": "CRM_MB_BASEINFO.SIDO", "operator": "NOT_IN", "codes": ["서울"],
            },
        ],
    }

    errors = graph_rag._validate_dimension_filters(plan)
    assert [error["code"] for error in errors] == ["DIMENSION_POLARITY_CONFLICT"]
    result = graph_rag.build_sql_result(
        nx.Graph(), "서울 포함과 제외", plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
    )
    assert result["is_success"] is False
    assert result["failure_reason"] == "invalid_dimension_filters"
    assert result["sql"] is None


def test_dimension_operator_is_whitelisted_before_rendering() -> None:
    malicious = "NOT_IN); DROP TABLE USERS; --"
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "dimension_filters": [
            {
                "dimension_id": "member_value:SIDO", "table": "CRM_MB_BASEINFO",
                "column": "CRM_MB_BASEINFO.SIDO", "operator": malicious, "codes": ["서울"],
            }
        ],
    }

    errors = graph_rag._validate_dimension_filters(plan)
    assert [error["code"] for error in errors] == ["DIMENSION_OPERATOR_UNSUPPORTED"]
    compiled = graph_rag.compile_member_target_conditions(plan)
    assert all("DROP TABLE" not in predicate for predicate in compiled["predicates"])
    result = graph_rag.build_sql_result(
        nx.Graph(), "서울 회원", plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
    )
    assert result["is_success"] is False
    assert result["failure_reason"] == "invalid_dimension_filters"
    assert result["sql"] is None


def test_deterministic_plan_clears_grounded_semantic_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "2019년 하반기 가장 잘 팔린 제품 11개를 산 고객 추출하고 남성도 빼주고 서울사는 고객도 빼줘"
    plan = _rules_plan_without_slot_llm(monkeypatch, query)
    plan["semantic_ir"] = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": [
            "customer_location",
            "customer_gender",
            "ranking_limit",
            "target_user.purchase_object",
            "purchase_date",
        ],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "필수 조건을 확인해 주세요.",
    }

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert [item["reason"] for item in plan["semantic_ir_reconciliation"]["resolved_fields"]] == [
        "grounded_dimension_filter",
        "grounded_plan_slot",
        "entity_ranking_owns_limit",
        "entity_ranking_owns_purchase_object",
        "ranking_window_owns_purchase_date",
    ]
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    assert result["is_success"] is True
    assert "B.GENDER_CD <> 'GENDER_CD.MALE'" in result["sql"]
    assert "B.SIDO NOT IN ('서울')" in result["sql"]


def test_llm_first_pipeline_clears_stale_customer_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "2019년 하반기 가장 잘 팔린 제품 11개를 산 고객 추출하고 남성도 빼주고 서울사는 고객도 빼줘"
    semantic_ir = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["customer_location"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }
    payload = _payload(semantic_ir)
    payload["target_user"] = {"gender": "female"}
    payload["exclude"] = {"gender": ["male"], "interests": [], "lifecycle": []}
    payload["semantic_evidence"] = []
    semantic_plan = validate_campaign_query_plan_v3(
        attach_campaign_query_plan_v3_identity(payload, query, current_date="2026-07-29"),
        query=query,
    )
    monkeypatch.setattr(graph_rag, "_apply_llm_object_fallback", lambda *_a, **_kw: None)
    monkeypatch.setattr(graph_rag, "_apply_llm_condition_slot_fallback", lambda *_a, **_kw: None)

    plan = graph_rag.build_query_plan(
        query,
        parser="auto",
        query_plan_v2=semantic_plan,
        precomputed_scopes={"mode": "llm", "targeting": query, "channel": ""},
    )

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert plan["semantic_ir_reconciliation"]["resolved_fields"] == [
        {
            "field": "customer_location",
            "resolved_by": "dimension_filters.member_value:SIDO",
            "reason": "grounded_dimension_filter",
        }
    ]
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    assert result["is_success"] is True
    assert "B.SIDO NOT IN ('서울')" in result["sql"]


def test_metric_trend_clears_operation_role_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "2026년 2월과 3월의 구매 금액 차이가 10% 이상 증가한 고객 리스트"
    plan = _rules_plan_without_slot_llm(monkeypatch, query)
    plan["semantic_ir"] = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": [
            "comparison_month_1",
            "comparison_month_2",
            "threshold",
            "comparison_metric",
            "direction",
            "target_user.purchase_date",
        ],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)

    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan["semantic_ir"]["missing_fields"] == []
    assert {
        item["resolved_by"] for item in plan["semantic_ir_reconciliation"]["resolved_fields"]
    } == {
        "target_user.metric_trend.baseline",
        "target_user.metric_trend.current",
        "target_user.metric_trend.relative_change",
        "target_user.metric_trend.metric_id",
        "target_user.metric_trend.direction",
        "target_user.metric_trend.baseline+current",
    }
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )
    assert result["is_success"] is True


def test_unproved_semantic_aliases_remain_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "두 월의 구매 금액을 비교한 고객 리스트"
    plan = _rules_plan_without_slot_llm(monkeypatch, query)
    plan["semantic_ir"] = {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": ["comparison_month_1", "comparison_month_2", "customer_location"],
        "policy_applications": [],
        "unsupported_operations": [],
        "message": "비교할 두 월과 지역을 지정해 주세요.",
    }

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)
    result = graph_rag._semantic_ir_blocking_sql_result(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["missing_fields"] == [
        "comparison_month_1", "comparison_month_2", "customer_location",
    ]
    assert result is not None
    assert result["failure_reason"] == "semantic_ir_needs_clarification"


def test_dynamic_dimension_alias_requires_a_compilable_matching_filter() -> None:
    plan = {
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["customer_grade", "customer_location"],
            "policy_applications": [],
            "unsupported_operations": [],
            "message": None,
        },
        "dimension_filters": [
            {
                "dimension_id": "member_value:CUSTOMER_GRADE",
                "prompt_label": "customer_grade",
                "table": "CRM_MB_BASEINFO",
                "column": "CRM_MB_BASEINFO.EMART_GRADE_CD",
                "operator": "IN",
                "codes": ["MEM_GRADE_CD.VIP"],
            }
        ],
    }

    graph_rag._reconcile_semantic_ir_with_execution_plan(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["missing_fields"] == ["customer_location"]
    assert plan["semantic_ir_reconciliation"]["resolved_fields"][0]["field"] == "customer_grade"
