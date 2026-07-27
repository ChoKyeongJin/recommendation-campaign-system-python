"""자연어 의미 → SQL 근거 → 최종 출고의 fail-closed 불변식 회귀."""

from __future__ import annotations

import networkx as nx
import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _result(query: str, *, llm_model: str | None = None) -> dict:
    return g.build_sql_result(
        nx.Graph(), query, _plan(query), [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=llm_model, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )


@pytest.mark.parametrize(
    ("query", "expected_grain", "evidence"),
    [
        ("전체 회원 수", "member", "CRM_MB_BASEINFO"),
        ("정상 회원 수", "member", "MEMBER_STATE_CD"),
        ("서울에 거주하는 회원", "member", "B.SIDO"),
        ("구매한 회원", "member", "CRM_SL_ORDERHEADERMALL"),
        ("구매하지 않은 회원", "member", "NOT EXISTS"),
        ("최근 30일 이내 구매한 회원", "member", "ORDER_DATE"),
        ("장바구니에 상품을 담은 회원", "member", "ODS_MALL_OMS_CART"),
        ("캠페인에 반응한 회원", "member", "MCS_CAMP_MBR_RSPN_FT"),
        ("시군구별 회원 수", "region", "GROUP BY B.SIGUNGU"),
    ],
)
def test_supported_core_requests_keep_meaning(query: str, expected_grain: str, evidence: str):
    result = _result(query)
    assert result["is_success"] is True, (query, result.get("failure_reason"))
    assert evidence in result["sql"]
    assert result["delivery_validation"]["actual_grain"] == expected_grain
    assert result["delivery_validation"]["is_satisfied"] is True


def test_required_conditions_without_tokens_cannot_succeed(monkeypatch):
    query = "서울에 거주하는 회원"
    plan = _plan(query)
    assert g.required_sql_conditions(plan)
    monkeypatch.setattr(g, "build_verified_condition_tokens", lambda _plan: [])
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )
    assert result["is_success"] is False
    assert result["sql"] is None
    assert result["failure_reason"] == "semantic_conditions_not_extracted"
    assert result["delivery_validation"]["condition_tokens"] == 0


def test_critical_dropped_semantic_issue_cannot_succeed(monkeypatch):
    verdict = {
        "ran": True,
        "faithful": False,
        "issues": [{
            "type": "dropped", "condition": "서울 거주", "detail": "핵심 지역 조건 누락",
            "severity": "critical", "affects_result_set": True, "is_primary_condition": True,
        }],
    }
    monkeypatch.setattr(g, "_verify_sql_semantics", lambda *args, **kwargs: verdict)
    result = _result("서울에 거주하는 회원", llm_model="test-model")
    assert result["is_success"] is False
    assert result["sql"] is None and result["blocked_sql"]
    assert result["failure_reason"] == "semantic_verification_failed"
    assert result["semantic_verification"]["issues"][0]["severity"] == "critical"


def _member_contract(conditions: list[dict] | None = None) -> dict:
    return {
        "output_contract": {"expected_grain": "member", "requires_member_id": True, "whole_target": False},
        "semantic_conditions": conditions or [],
    }


def test_member_question_rejects_region_grain():
    validation = g._validate_sql_delivery_contract(
        "회원은 몇 명인가",
        _member_contract(),
        "SELECT B.SIGUNGU AS target_region, COUNT(*) AS member_count "
        "FROM CRM_MB_BASEINFO B GROUP BY B.SIGUNGU",
    )
    assert validation["is_satisfied"] is False
    assert validation["expected_grain"] == "member"
    assert validation["actual_grain"] == "region"
    assert "query_result_grain_mismatch" in validation["failure_reasons"]


def test_purchase_absence_requires_purchase_source_and_anti_join():
    condition = {"domain": "purchase", "operator": "not_exists", "is_primary_condition": True}
    validation = g._validate_sql_delivery_contract(
        "구매하지 않은 회원", _member_contract([condition]),
        "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B",
    )
    assert validation["is_satisfied"] is False
    assert validation["missing_conditions"] == [condition]
    assert validation["sql_evidence"]["1"]["actual_evidence"] == []


def test_purchase_absence_rejects_positive_exists_polarity():
    condition = {"domain": "purchase", "operator": "not_exists", "is_primary_condition": True}
    validation = g._validate_sql_delivery_contract(
        "구매하지 않은 회원", _member_contract([condition]),
        "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE EXISTS "
        "(SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)",
    )
    assert validation["is_satisfied"] is False
    assert validation["polarity_mismatches"] == [condition]
    assert validation["failure_reason"] == "semantic_condition_polarity_mismatch"


def test_targeting_contract_requires_member_id_projection():
    validation = g._validate_sql_delivery_contract(
        "회원 목록", _member_contract(),
        "SELECT B.SIGUNGU AS target_region FROM CRM_MB_BASEINFO B",
    )
    assert validation["api_contract_match"] is False
    assert "targeting_result_member_id_missing" in validation["failure_reasons"]


def test_api_does_not_treat_first_aggregate_column_as_customer_id():
    import api

    assert api._customer_id_column(["target_region", "member_count"]) is None
    assert api._customer_id_column(["target_region", "CUST_ID"]) == "CUST_ID"


@pytest.mark.parametrize(
    ("domain", "source"),
    [
        ("purchase", "CRM_SL_ORDERHEADERMALL"),
        ("cart", "ODS_MALL_OMS_CART"),
        ("campaign_response", "MCS_CAMP_MBR_RSPN_FT"),
        ("coupon", "MCS_CAMP_MBR_RSPN_FT"),
        ("visit", "VISIT"),
        ("wishlist", "WISHLIST"),
    ],
)
@pytest.mark.parametrize("operator", ["exists", "not_exists"])
def test_membership_domain_polarity_is_extensible(domain: str, source: str, operator: str):
    prefix = "NOT " if operator == "not_exists" else ""
    condition = {"domain": domain, "operator": operator, "is_primary_condition": True}
    evidence = g._condition_evidence(
        condition,
        f"SELECT M.MEMBER_NO FROM MEMBERS M WHERE {prefix}EXISTS "
        f"(SELECT 1 FROM {source} X WHERE X.MEMBER_NO = M.MEMBER_NO)",
    )
    assert evidence["satisfied"] is True, (domain, operator, evidence)
    assert evidence["polarity_match"] is True


@pytest.mark.parametrize(
    ("operator", "predicate"),
    [("exists", "B.LAST_LOGIN_DATE IS NOT NULL"), ("not_exists", "B.LAST_LOGIN_DATE IS NULL")],
)
def test_login_domain_supports_both_polarities(operator: str, predicate: str):
    evidence = g._condition_evidence(
        {"domain": "login", "operator": operator, "is_primary_condition": True},
        f"SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE {predicate}",
    )
    assert evidence["satisfied"] is True

