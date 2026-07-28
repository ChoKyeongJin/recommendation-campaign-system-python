"""Regression coverage for derived entity-set conditions.

``2019년 가장 많이 팔린 상품 10개를 구매한 고객`` 류의 요청은 피연산자가 리터럴이 아니라 다른
질의의 결과다. 이 계층의 목적은 문장 하나를 지원하는 것이 아니라 (엔터티 × 지표 × 방향 × 개수 ×
기간 × 관계 × 부정) 조합을 **코드 추가 없이** 지원하는 것이므로, 테스트도 조합축을 따라 간다.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

import graph_rag as g
from entity_set import (
    compile_entity_set_predicate,
    entity_set_capability,
    entity_set_label,
    parse_entity_set_condition,
)


@pytest.fixture(scope="module")
def config() -> dict:
    payload = json.loads(Path("docs/data/member_target_filters.json").read_text(encoding="utf-8"))
    return payload["entity_set_targets"]


def _sql_result(query: str) -> tuple[dict, dict]:
    plan = g.build_query_plan(query, parser="rules")
    result = g.build_sql_result(
        nx.Graph(), query, plan, [], g.DEFAULT_SCHEMA_PATH, None,
        llm_model=None, original_query=query, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )
    return plan, result


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "2019년 가장 많이 팔린 상품 10개를 구매한 고객만 추출해",
            {"entity": "product", "measure": "sales_quantity", "direction": "top", "limit": 10,
             "relation": "purchase", "rankRelation": "purchase", "negated": False},
        ),
        # 엔터티만 바꾼 문장 — 코드가 아니라 레지스트리 한 줄이 담당한다.
        (
            "가장 많이 팔린 브랜드 3개를 구매한 회원 추출해줘",
            {"entity": "brand", "limit": 3, "direction": "top", "negated": False},
        ),
        # 지표·어순(개수가 엔터티 앞)·기간 표현이 바뀐 문장.
        (
            "최근 90일 매출 상위 5개 카테고리 상품을 구매한 고객",
            {"entity": "category", "measure": "sales_amount", "limit": 5, "direction": "top"},
        ),
        # 방향 반전.
        (
            "가장 적게 팔린 상품 20개를 구매한 회원",
            {"entity": "product", "direction": "bottom", "limit": 20},
        ),
        # 부정: 집합은 그대로, 관계만 부재로.
        (
            "가장 많이 팔린 상품 10개를 구매하지 않은 회원 추출해줘",
            {"entity": "product", "negated": True, "relation": "purchase"},
        ),
        # 순위를 매기는 관계와 회원을 잇는 관계가 다른 조합.
        (
            "가장 많이 장바구니에 담은 상품 5개를 구매한 고객 추출해줘",
            {"entity": "product", "rankRelation": "cart", "relation": "purchase", "limit": 5},
        ),
    ],
)
def test_one_node_covers_the_combination_axes(query, expected, config):
    node = parse_entity_set_condition(query, config)
    assert node is not None, query
    assert node.get("unsupported_reason") is None, node
    for key, value in expected.items():
        assert node[key] == value, (query, key, node[key])


def test_default_period_scope_belongs_to_the_ranking_clause(config):
    """'2019년 가장 많이 팔린 상품을 구매한 고객'의 2019년은 판매 순위의 창이지 구매 시점이 아니다."""
    node = parse_entity_set_condition("2019년 가장 많이 팔린 상품 10개를 구매한 고객", config)
    assert node["window"] == {"from": "20190101", "to": "20191231", "label": "2019년"}
    predicate = compile_entity_set_predicate(node, config)
    inner = predicate.split("IN (", 1)[1]
    assert "ORDER_DATE BETWEEN '20190101' AND '20191231'" in inner
    # 바깥(구매 시점)에는 기간 조건이 붙지 않는다.
    assert predicate.split("IN (", 1)[0].count("ORDER_DATE") == 0


def test_month_scoped_ranking_window_keeps_the_month(config):
    """'2019년 3월 구매에서 가장 많이 팔린 상품 5개' — 월이 살아 있어야 그 달의 순위다.

    창 파서가 연도만 알던 동안 3월이 통째로 사라져 2019년 연간 베스트셀러를 뽑았고, 의미 검증이
    이를 잡아내 SQL 이 차단됐다(사용자는 결과를 못 받는다).
    """
    query = "2019년 3월 구매에서 가장 많이 팔린상품 5개를 구매한 고객 리스트"
    node = parse_entity_set_condition(query, config)
    assert node["window"] == {"from": "20190301", "to": "20190331", "label": "2019년 3월"}
    assert node["limit"] == 5
    predicate = compile_entity_set_predicate(node, config)
    inner = predicate.split("IN (", 1)[1]
    assert "ORDER_DATE BETWEEN '20190301' AND '20190331'" in inner


def test_unsupported_combination_names_the_offending_element(config):
    """장바구니 테이블에는 브랜드 키가 없다 — 조용히 무시하지 않고 어느 요소가 문제인지 알린다."""
    node = parse_entity_set_condition("가장 많이 팔린 브랜드 3개를 장바구니에 담은 회원", config)
    assert node["unsupported_reason"] == "unsupported_entity_set_entity"
    assert compile_entity_set_predicate(node, config) is None

    unknown_measure = {**node, "entity": "product", "measure": "존재하지_않는_지표"}
    assert entity_set_capability(unknown_measure, config) == "unsupported_entity_set_measure"


def test_unrelated_queries_are_left_to_the_existing_parsers(config):
    for query in ["여성 회원 추출해줘", "최근 30일 구매한 회원", "구매금액이 가장 많은 회원 100명"]:
        assert parse_entity_set_condition(query, config) is None, query


@pytest.mark.parametrize(
    ("query", "expected_fragments"),
    [
        (
            "2019년 가장 많이 팔린 상품 10개를 구매한 고객만 추출해",
            ["EXISTS (", "SELECT TOP 10 D.PRODUCT_ID", "ORDER BY SUM(D.ORDER_QTY) DESC",
             "OD.MEMBER_NO = B.MEMBER_NO", "B.MEMBER_STATE_CD"],
        ),
        (
            "가장 많이 팔린 상품 10개를 구매하지 않은 회원 추출해줘",
            ["NOT EXISTS ("],
        ),
        (
            "최근 90일 매출 상위 5개 카테고리 상품을 구매한 VIP 회원",
            ["SELECT TOP 5 CP.CATEGORY", "SUM(D.PAYMENT_AMT)", "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'"],
        ),
        # 회원 속성과의 결합 — 순위 집합 조건은 다른 조건과 같은 SQL 에 AND 로 붙는다.
        (
            "판매량 상위 20개 상품을 구매한 30대 여성 회원 추출해줘",
            ["SELECT TOP 20 D.PRODUCT_ID", "B.GENDER_CD = 'GENDER_CD.FEMALE'", "B.AGE >= 30", "B.AGE <= 39"],
        ),
    ],
)
def test_end_to_end_sql_projects_members_not_the_ranked_entities(query, expected_fragments):
    _plan, result = _sql_result(query)
    assert result["is_success"] is True, (query, result.get("failure_reason"))
    sql = result["sql"]
    # 이 계층의 존재 이유: 전제 조건(순위 집합)만 계산하고 대상(회원)을 잊는 SQL 이 나올 수 없다.
    assert sql.startswith("SELECT DISTINCT B.MEMBER_NO AS CUST_ID")
    for fragment in expected_fragments:
        assert fragment in sql, (query, fragment)


def test_ranking_clause_owns_the_slots_it_consumes():
    """같은 어구가 상품 조건·구매 시점·회원 랭킹으로 이중 해석되면 서로 모순되는 SQL 이 된다."""
    plan, result = _sql_result("2019년 가장 많이 팔린 상품 10개를 구매한 고객만 추출해")
    target_user = plan["target_user"]
    assert isinstance(target_user.get("entity_set_condition"), dict)
    assert target_user.get("purchase_object") is None
    assert target_user.get("purchase_date") is None
    assert target_user.get("purchase_membership") is None
    assert plan.get("member_metric_selection") is None
    assert result["sql"].count("SELECT") == 3  # 회원 + 관계 EXISTS + 순위 서브쿼리


def test_entity_count_is_not_applied_as_a_member_row_limit():
    plan, result = _sql_result("최근 90일 매출 상위 5개 카테고리 상품을 구매한 VIP 회원")
    assert plan.get("result_limit") in (None, 0)
    assert "TOP 5" in result["sql"].split("IN (", 1)[1]
    assert "TOP 5" not in result["sql"].split("IN (", 1)[0]


def test_label_describes_the_composition(config):
    node = parse_entity_set_condition("2019년 가장 많이 팔린 상품 10개를 구매한 고객", config)
    label = entity_set_label(node, config)
    assert "2019년" in label and "상위" in label and "10개" in label and "상품" in label
