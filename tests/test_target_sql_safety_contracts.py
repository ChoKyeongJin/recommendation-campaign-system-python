"""Cross-cutting safety contracts for natural-language analytical SQL."""

from __future__ import annotations

import json

import graph_rag as g
from data_quality import analyze_execution_result, validate_metric_profile
from query_semantics import TOKEN_ROLES, classify_query_tokens, extract_extreme_semantics
from sql_guard import load_column_types, load_join_key_registry, validate_join_keys


def test_token_roles_separate_extreme_words_from_entities():
    classified = classify_query_tokens("가장 적게 구매한 회원의 구매금액을 알려줘")
    roles = {item["token"]: item["role"] for item in classified}
    assert TOKEN_ROLES >= {item["role"] for item in classified}
    assert roles["가장"] == "extreme"
    assert roles["적게"] == "extreme"
    assert extract_extreme_semantics("가장 적게 구매한 회원") == {
        "surface": "가장 적게", "extreme": "MIN", "sort_direction": "ASC", "limit": 1,
    }


def test_extreme_adverbs_cannot_be_product_candidates():
    for term in ("가장", "적게", "많이", "최소", "최고", "상위"):
        assert g._sanitize_purchase_object(term) is None
    plan = g.build_query_plan("가장 적게 구매한 회원의 구매금액을 알려줘", parser="rules")
    assert plan["target_user"].get("purchase_object") in (None, [], "")
    assert plan["detected_intent"]["comparison"]["operator"] == "argmin"


def test_unverified_try_cast_join_is_rejected_with_structured_reasons():
    types = load_column_types(g.DEFAULT_SCHEMA_PATH)
    relationships = load_join_key_registry(g.DEFAULT_SCHEMA_PATH)
    sql = (
        "SELECT B.MEMBER_NO FROM ODS_MALL_OMS_CART C JOIN CRM_MB_BASEINFO B "
        "ON TRY_CAST(C.CART_ID AS BIGINT) = B.MEMBER_NO"
    )
    result = validate_join_keys(sql, types, relationships, strict_relationships=True)
    reasons = {issue["reason_code"] for issue in result["issues"]}
    assert result["is_valid"] is False
    assert {"UNVERIFIED_JOIN_CAST", "JOIN_TYPE_MISMATCH"} <= reasons


def test_only_registered_cast_relationship_is_allowed():
    types = load_column_types(g.DEFAULT_SCHEMA_PATH)
    relationships = load_join_key_registry(g.DEFAULT_SCHEMA_PATH)
    sql = (
        "SELECT B.MEMBER_NO FROM MCS_CAMP_MBR_RSPN_FT R JOIN CRM_MB_BASEINFO B "
        "ON TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO"
    )
    assert validate_join_keys(sql, types, relationships, strict_relationships=True) == {
        "is_valid": True, "issues": [],
    }


def test_strict_mode_rejects_unregistered_known_table_relationship():
    result = validate_join_keys(
        "SELECT 1 FROM known_a A JOIN known_b B ON A.ID = B.ID",
        {"known_a": {"id": "numeric"}, "known_b": {"id": "numeric"}},
        {}, strict_relationships=True,
    )
    assert result["is_valid"] is False
    assert result["issues"][0]["reason_code"] == "UNREGISTERED_RELATIONSHIP"


def test_all_null_metric_profile_blocks_selection(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({
        "tables": {},
        "column_profiles": [{
            "table": "CRM_MB_BASEINFO", "column": "CARROT_BALANCE_AMT",
            "row_count": 69308, "non_null_count": 0, "null_ratio": 1.0,
        }],
    }), encoding="utf-8")
    intent = g.analyze_analytical_intent("적립금이 가장 많은 회원을 알려줘")
    validation = validate_metric_profile(intent, schema)
    assert validation["valid"] is False
    assert validation["status"] == "UNUSABLE_ALL_NULL"
    assert validation["reason_code"] == "UNUSABLE_METRIC_COLUMN"


def test_execution_sanity_distinguishes_valid_and_suspicious_empty_results():
    member_empty = analyze_execution_result([], [], analytical_intent=None)
    assert member_empty["valid"] is True and member_empty["status"] == "VALID_EMPTY_RESULT"

    aggregate_empty = analyze_execution_result(
        [], [], analytical_intent={"query_type": "ranking", "result_shape": "single_member"},
    )
    assert aggregate_empty["valid"] is False
    assert aggregate_empty["issues"][0]["reason_code"] == "SUSPICIOUS_EMPTY_RESULT"


def test_execution_sanity_rejects_all_null_metric_and_zero_join_rate():
    all_null = analyze_execution_result(
        [{"CUST_ID": 1, "METRIC_VALUE": None}], ["CUST_ID", "METRIC_VALUE"],
        analytical_intent={"query_type": "ranking", "result_shape": "single_member"},
    )
    assert all_null["valid"] is False
    assert any(issue["reason_code"] == "METRIC_ALL_NULL" for issue in all_null["issues"])

    zero_join = analyze_execution_result([], [], join_quality={"match_rate": 0.0})
    assert zero_join["valid"] is False
    assert zero_join["issues"][0]["reason_code"] == "ZERO_JOIN_MATCH_RATE"
