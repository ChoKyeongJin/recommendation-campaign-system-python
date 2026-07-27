"""Regression coverage for the registry-derived analytical contract.

The analytical layer must (1) compile the aggregate corpus end to end, (2) carry
behavioural population scopes into the SQL, (3) name the offending element when a
request cannot be served, and (4) refuse to answer at all when a condition of the
question was not compiled — a plausible number over the wrong population is worse
than an explicit "cannot answer".
"""

from __future__ import annotations

import networkx as nx
import pytest

import graph_rag as g
from aggregation_requirements import parse_aggregation_request, validate_aggregation_sql
from analytical_intent import (
    analyze_analytical_intent,
    build_aggregation_request,
    compile_aggregation_ast,
    load_analytics_registry,
    member_condition_filter,
    resolve_dimension_mapping,
    validate_intent_sql_contract,
)
from sql_ast import render_select_ast


AGGREGATE_CORPUS = [
    "회원 수를 알려줘.",
    "최근 90일 로그인한 회원 수를 알려줘.",
    "구매한 회원 수를 알려줘.",
    "구매하지 않은 회원 수를 알려줘.",
    "캠페인에 반응한 회원 수를 알려줘.",
    "앱으로 로그인한 회원 수를 알려줘.",
    "구매한 고객 수를 알려줘.",
    "상품을 구매한 회원 수를 알려줘.",
    "최근 30일 구매 고객 수를 알려줘.",
    "브랜드별 구매 고객 수를 알려줘.",
    "카테고리별 구매 회원 수를 알려줘.",
    "시도별 회원 수를 알려줘.",
    "시군구별 회원 수를 알려줘.",
    "등급별 회원 수를 알려줘.",
    "캠페인별 반응 회원 수를 알려줘.",
    "중복 없이 구매 회원 수를 알려줘.",
    "전체 구매 금액을 알려줘.",
    "최근 30일 총 구매금액을 알려줘.",
    "VIP 회원의 구매금액 합계를 알려줘.",
    "여성 회원 구매금액 합계를 알려줘.",
    "브랜드별 구매금액 합계를 알려줘.",
    "카테고리별 구매금액을 알려줘.",
    "시도별 구매금액을 알려줘.",
    "회원등급별 구매금액 합계를 알려줘.",
    "캠페인 구매금액 합계를 알려줘.",
    "쿠폰 사용으로 발생한 구매금액 합계를 알려줘.",
    "회원 평균 구매금액을 알려줘.",
    "평균 주문금액을 알려줘.",
    "회원당 평균 구매횟수를 알려줘.",
    "회원 평균 로그인 횟수를 알려줘.",
    "평균 적립금 잔액을 알려줘.",
    "평균 예치금 잔액을 알려줘.",
    "지역별 평균 구매금액을 알려줘.",
    "등급별 평균 구매금액을 알려줘.",
    "성별 평균 구매금액을 알려줘.",
    "연령대별 평균 주문금액을 알려줘.",
    "가장 많이 구매한 회원의 구매금액을 알려줘.",
    "가장 적게 구매한 회원의 구매금액을 알려줘.",
    "최고 구매금액을 알려줘.",
    "최소 구매금액을 알려줘.",
    "가장 최근 구매일을 알려줘.",
    "가장 오래된 구매일을 알려줘.",
    "로그인을 가장 많이 한 회원의 로그인 횟수를 알려줘.",
    "적립금이 가장 많은 회원을 알려줘.",
    "예치금이 가장 많은 회원을 알려줘.",
    "장바구니 상품이 가장 많은 회원을 알려줘.",
    "성별 회원 수를 알려줘.",
    "연령대별 회원 수를 알려줘.",
    "브랜드별 구매금액을 알려줘.",
    "카테고리별 구매건수를 알려줘.",
    "캠페인별 구매금액을 알려줘.",
    "로그인 채널별 회원 수를 알려줘.",
    "가입채널별 회원 수를 알려줘.",
]


def _sql_result(query: str) -> tuple[dict, dict]:
    plan = g.build_query_plan(query, parser="rules")
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )
    return plan, result


def _compiled_sql(query: str) -> str:
    intent = analyze_analytical_intent(query)
    assert intent is not None and not intent.get("unsupported_reason"), (query, intent)
    request = build_aggregation_request(intent)
    return render_select_ast(compile_aggregation_ast(intent, request))


@pytest.mark.parametrize("query", AGGREGATE_CORPUS)
def test_aggregate_corpus_compiles_and_passes_every_validation_layer(query):
    plan, result = _sql_result(query)
    assert plan["intent"] == "analyze_aggregation", query
    assert result["is_success"] is True, (query, result.get("failure_reason"))
    assert result["aggregation_validation"]["valid"] is True, (query, result["aggregation_validation"].get("errors"))
    assert result["intent_sql_contract"]["valid"] is True, (query, result["intent_sql_contract"].get("issues"))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # 존재 스코프는 회원 모집단 위 EXISTS 로 컴파일되고 회원 상태 정책을 유지한다.
        ("구매한 회원 수를 알려줘.", "EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL OH WHERE OH.MEMBER_NO = B.MEMBER_NO)"),
        # 부재는 같은 술어의 NOT EXISTS 다 — 별도 정의를 두지 않는다.
        ("구매하지 않은 회원 수를 알려줘.", "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL OH"),
        # 기간은 스코프의 날짜 컬럼에 붙는다(지표 소스에 날짜가 없어도 성립).
        ("최근 30일 구매 고객 수를 알려줘.", "OH.ORDER_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)"),
        # 회원 행 자체가 증거인 스코프는 서브쿼리 없이 컬럼 술어로 컴파일된다.
        ("최근 90일 로그인한 회원 수를 알려줘.", "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -90, GETDATE()), 112)"),
        # 캠페인 반응 정의는 스코프 한 곳에 있고 EXISTS 로 주입된다.
        ("캠페인에 반응한 회원 수를 알려줘.", "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')"),
    ],
)
def test_population_scopes_reach_the_sql(query, expected):
    assert expected in _compiled_sql(query)


def test_inherently_scoped_source_keeps_the_scope_definition():
    """주문 상세에서 회원을 세면 구매자 모집단이지만, 스코프가 요구하는 술어는 남아야 한다."""
    sql = _compiled_sql("캠페인별 반응 회원 수를 알려줘.")
    assert "FROM MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.CGRP_TYPE_CD = 'T'" in sql
    assert "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')" in sql
    assert "GROUP BY R.CAMP_ID" in sql


def test_dimension_support_is_derived_from_source_joins():
    """차원×소스 행렬을 손으로 유지하지 않는다 — 소스가 닿는 테이블이면 그룹 축이 된다."""
    registry = load_analytics_registry()
    metric = next(item for item in registry["metrics"] if item["id"] == "purchase_amount")
    order_source = next(item for item in metric["sources"] if item["id"] == "purchase_header")
    mapping = resolve_dimension_mapping(registry, order_source, "gender")
    assert mapping is not None
    assert mapping["expression"] == "B.GENDER_CD"
    assert mapping["dependencies"] == ["member"]
    # 닿지 않는 테이블은 여전히 미지원이다(추측 금지).
    assert resolve_dimension_mapping(registry, order_source, "brand") is None

    sql = _compiled_sql("성별 평균 구매금액을 알려줘.")
    assert "INNER JOIN CRM_MB_BASEINFO B ON B.MEMBER_NO = O.MEMBER_NO" in sql
    assert "GROUP BY B.GENDER_CD" in sql


def test_per_member_average_uses_a_two_level_aggregate():
    """'회원당 평균 구매횟수'는 주문 행 평균이 아니라 회원별 건수의 평균이다."""
    sql = " ".join(_compiled_sql("회원당 평균 구매횟수를 알려줘.").split())
    assert sql.startswith("SELECT AVG(M.PURCHASE_COUNT)")
    assert "COUNT(DISTINCT D.ORDER_ID) AS PURCHASE_COUNT" in sql
    assert "GROUP BY D.MEMBER_NO) M" in sql

    plan, result = _sql_result("회원당 평균 구매횟수를 알려줘.")
    # 파생 테이블 별칭(M.PURCHASE_COUNT)을 물리 컬럼으로 오인해 탈락시키지 않는다.
    assert result["is_success"] is True, result.get("failure_reason")
    assert result["aggregation_validation"]["valid"] is True
    assert plan["analytical_intent"]["result_shape"] == "scalar"


@pytest.mark.parametrize(
    ("query", "reason", "item"),
    [
        ("브랜드별 회원 평균 로그인 횟수를 알려줘.", "unsupported_group_dimension", "brand"),
        ("카테고리별 예치금 잔액 합계를 알려줘.", "unsupported_group_dimension", "category"),
        ("회원 수 합계를 알려줘.", "unsupported_aggregate_function", "SUM"),
    ],
)
def test_unsupported_requests_name_the_offending_element(query, reason, item):
    intent = analyze_analytical_intent(query)
    assert intent is not None, query
    assert intent["unsupported_reason"] == reason, (query, intent)
    assert item in intent["unsupported_detail"]["items"]
    assert intent["unsupported_message"] and intent["clarification"]


@pytest.mark.parametrize(
    ("query", "label"),
    [
        # 생일/상품 조건은 집계 계약이 컴파일하지 못한다. 조건을 무시한 '전체 회원 수'를 내보내는 대신
        # 무엇이 빠졌는지 밝히고 확인을 요청해야 한다.
        ("생일인 회원 수를 알려줘.", "생일 조건"),
        ("기저귀를 구매한 회원 수를 알려줘.", "구매 상품 조건"),
    ],
)
def test_dropped_audience_condition_fails_closed_instead_of_counting_everyone(query, label):
    plan, result = _sql_result(query)
    assert plan["intent"] == "analyze_aggregation"
    assert plan["unsupported"]["reason"] == "analytical_signal_dropped"
    assert label in plan["unsupported"]["dropped_conditions"]
    assert result["is_success"] is False
    assert result["sql"] is None


def test_registered_member_canonicals_are_reusable_by_any_metric():
    """오디언스 정의(휴면/임직원/등급)는 집계에서도 같은 물리 정의로 재사용된다."""
    assert member_condition_filter("employee")["memberFilter"] == "employee"
    activity = member_condition_filter("inactive_90d")
    assert activity["operator"] == "lt" and activity["value"] == "P90D" and activity["includeNull"] is True
    assert member_condition_filter("존재하지_않는_조건") is None

    _plan, result = _sql_result("임직원 회원 수를 알려줘.")
    assert result["is_success"] is True, result.get("failure_reason")
    assert "B.EMPLOYEE_YN = 'Y'" in result["sql"]

    _dormant_plan, dormant = _sql_result("휴면 회원의 구매금액 합계를 알려줘.")
    assert dormant["is_success"] is True, dormant.get("failure_reason")
    assert "B.LAST_LOGIN_DATE < CONVERT(CHAR(8), DATEADD(DAY, -90, GETDATE()), 112)" in dormant["sql"]


def test_a_dropped_scope_predicate_is_caught_by_both_validation_layers():
    """스코프가 SQL 에서 사라지면 '전체 회원 수'가 되지만 숫자는 그럴듯하다 — 두 계층 모두 잡는다."""
    query = "구매한 회원 수를 알려줘."
    intent = analyze_analytical_intent(query)
    request = build_aggregation_request(intent)
    unscoped = (
        "SELECT COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT FROM CRM_MB_BASEINFO B "
        "WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"
    )

    contract = validate_intent_sql_contract(intent, unscoped)
    assert contract["valid"] is False
    assert "MISSING_SCOPE_PREDICATE" in {issue["code"] for issue in contract["issues"]}

    parsed, errors = parse_aggregation_request(request, g.DEFAULT_SCHEMA_PATH)
    assert parsed is not None and not errors
    validation = validate_aggregation_sql(parsed, unscoped, g.DEFAULT_SCHEMA_PATH)
    assert validation["valid"] is False
    assert validation["error_code"] == "MISSING_RELATION_JOIN"


def test_registry_contract_is_not_rewritten_by_the_llm_planner_normalizer():
    """LLM 플래너 교정 후처리가 결정론 계약의 회원키/테이블을 덮어쓰면 검증이 순환한다."""
    plan, _result = _sql_result("구매한 회원 수를 알려줘.")
    aggregation = plan["aggregation_request"]
    assert aggregation["businessRules"]["contractSource"] == "analytics_registry"
    metric = aggregation["aggregations"][0]
    assert metric["table"].casefold() == "crm_mb_baseinfo"
    assert metric["column"] == "MEMBER_NO" and metric["distinct"] is True
