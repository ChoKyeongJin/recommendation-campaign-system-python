"""Regression coverage for semantic grouped aggregation contracts."""

from __future__ import annotations

import json

import networkx as nx
import pytest

import api
import graph_rag as g
from aggregation_requirements import SchemaMetadata, parse_aggregation_request, validate_aggregation_sql
from analytical_intent import validate_intent_sql_contract
from data_quality import analyze_execution_result


CASES = [
    ("성별 회원 수를 알려줘.", "GENDER_CD", "MEMBER_COUNT"),
    ("등급별 회원 수를 알려줘.", "EMART_GRADE_CD", "MEMBER_COUNT"),
    ("연령대별 회원 수를 알려줘.", "AGE_DIV1", "MEMBER_COUNT"),
    ("시도별 회원 수를 알려줘.", "SIDO", "MEMBER_COUNT"),
    ("시군구별 회원 수를 알려줘.", "SIGUNGU", "MEMBER_COUNT"),
    ("브랜드별 구매금액을 알려줘.", "BRAND_ID", "TOTAL_PURCHASE_AMOUNT"),
    ("카테고리별 구매건수를 알려줘.", "CATEGORY", "PURCHASE_COUNT"),
    ("캠페인별 구매금액을 알려줘.", "CAMP_ID", "TOTAL_PURCHASE_AMOUNT"),
    ("로그인 채널별 회원 수를 알려줘.", "LAST_LOGIN_CHANNEL", "MEMBER_COUNT"),
    ("가입채널별 회원 수를 알려줘.", "REG_CHANNEL_CD", "MEMBER_COUNT"),
]


def _result(query: str) -> tuple[dict, dict]:
    plan = g.build_query_plan(query, parser="rules")
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )
    return plan, result


@pytest.mark.parametrize(("query", "dimension", "metric_alias"), CASES)
def test_requested_grouped_aggregations_compile_from_one_contract(query, dimension, metric_alias):
    plan, result = _result(query)
    sql = result.get("sql") or ""
    assert plan["intent"] == "analyze_aggregation"
    assert result["is_success"] is True, (query, result.get("failure_reason"))
    assert result["aggregation_validation"]["valid"] is True
    assert dimension in sql and f"GROUP BY" in sql and dimension in sql.split("GROUP BY", 1)[1]
    assert metric_alias in sql


def test_registration_channel_and_shop_are_distinct_semantic_dimensions():
    channel_plan, channel_result = _result("가입채널별 회원 수를 알려줘.")
    shop_plan, shop_result = _result("가입매장별 회원 수를 알려줘.")
    assert channel_plan["analytical_intent"]["dimensions"] == ["registration_channel"]
    assert "REG_CHANNEL_CD" in channel_result["sql"]
    assert "REG_OFFSHOP_ID" not in channel_result["sql"]
    assert shop_plan["analytical_intent"]["dimensions"] == ["registration_shop"]
    assert "REG_OFFSHOP_ID" in shop_result["sql"]


def test_wrong_registration_channel_column_is_rejected_by_intent_contract():
    plan, _result_value = _result("가입채널별 회원 수를 알려줘.")
    wrong_sql = (
        "SELECT B.REG_OFFSHOP_ID, COUNT(DISTINCT B.MEMBER_NO) "
        "FROM CRM_MB_BASEINFO B GROUP BY B.REG_OFFSHOP_ID"
    )
    validation = validate_intent_sql_contract(plan["analytical_intent"], wrong_sql)
    assert validation["valid"] is False
    assert "SEMANTIC_COLUMN_MISMATCH" in {issue["code"] for issue in validation["issues"]}


@pytest.mark.parametrize(
    ("query", "column"),
    [
        ("최초 로그인 채널별 회원 수", "FIRST_LOGIN_CHANNEL"),
        ("최근 로그인 채널별 회원 수", "LAST_LOGIN_CHANNEL"),
        ("로그인 채널별 회원 수", "LAST_LOGIN_CHANNEL"),
    ],
)
def test_login_channel_specificity_selects_existing_physical_column(query, column):
    _plan, result = _result(query)
    assert result["is_success"] is True
    assert column in result["sql"]


def test_campaign_group_by_omission_has_specific_error_code():
    plan, _result_value = _result("캠페인별 구매금액을 알려줘.")
    request, errors = parse_aggregation_request(plan["aggregation_request"], g.DEFAULT_SCHEMA_PATH)
    assert request is not None and not errors
    validation = validate_aggregation_sql(
        request,
        "SELECT SUM(R.BUY_AMT) FROM MCS_CAMP_MBR_RSPN_FT R "
        "WHERE R.CGRP_TYPE_CD='T' AND R.BUY_RSPN_YN='Y'",
        g.DEFAULT_SCHEMA_PATH,
    )
    assert validation["valid"] is False
    assert validation["error_code"] == "MISSING_GROUP_BY_DIMENSION"


def test_schema_loader_recovers_columns_from_full_catalog(tmp_path):
    compact = tmp_path / "aggregation_schema.json"
    compact.write_text(json.dumps({
        "tables": {"CRM_MB_BASEINFO": {"columns": [{"name": "MEMBER_NO", "type": "bigint"}]}}
    }), encoding="utf-8")
    catalog = SchemaMetadata.load(compact)
    assert catalog.column("CRM_MB_BASEINFO", "FIRST_LOGIN_CHANNEL") is not None
    assert catalog.column("CRM_MB_BASEINFO", "LAST_LOGIN_CHANNEL") is not None


def test_default_member_policy_is_structured_and_can_be_overridden():
    default_plan, default_result = _result("성별 회원 수")
    assert "MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'" in default_result["sql"]
    assert default_plan["aggregation_request"]["businessRules"]["appliedPolicyFilters"] == [{
        "id": "policy_active_member", "column": "MEMBER_STATE_CD", "operator": "eq",
        "value": "MEMBER_STATE_CD.NORMAL", "mode": "default",
    }]

    _all_plan, all_result = _result("전체 회원의 성별 회원 수")
    assert "MEMBER_STATE_CD" not in all_result["sql"]

    _sleep_plan, sleep_result = _result("휴면 포함 성별 회원 수")
    assert "MEMBER_STATE_CD IN" in sleep_result["sql"]
    assert "MEMBER_STATE_CD.SLEEP" in sleep_result["sql"]


def test_dimension_value_profile_rejects_shop_codes_as_signup_channels():
    plan, _result_value = _result("가입채널별 회원 수")
    sanity = analyze_execution_result(
        [{"REG_CHANNEL_CD": "CAB4", "MEMBER_COUNT": 10}],
        ["REG_CHANNEL_CD", "MEMBER_COUNT"],
        analytical_intent=plan["analytical_intent"],
    )
    assert sanity["valid"] is False
    assert sanity["issues"][-1]["reason_code"] == "SEMANTIC_DIMENSION_VALUE_MISMATCH"


def test_aggregation_response_is_not_a_zero_sized_target_audience(monkeypatch):
    import db_connections

    monkeypatch.setattr(
        db_connections,
        "run_read_query",
        lambda _connection, _sql: [{"GENDER_CD": "GENDER_CD.FEMALE", "MEMBER_COUNT": 100}],
    )
    execution = api._execute_external_target_sql(
        "SELECT B.GENDER_CD, COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT "
        "FROM CRM_MB_BASEINFO B GROUP BY B.GENDER_CD",
        "CRMDW",
        100,
        query_plan={"intent": "analyze_aggregation", "analytical_intent": {}},
    )
    assert execution["result_type"] == "aggregation"
    assert execution["targeting_result"]["result_type"] == "aggregation"
    assert execution["targeting_result"]["target_customer_count"] is None

