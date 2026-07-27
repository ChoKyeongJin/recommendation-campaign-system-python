"""Regression coverage for the closed targeting IR (LLM 폴백의 출력 공간).

자유 SQL 폴백에서 반복된 사고는 '전제 조건만 계산하고 대상을 잊은 SQL'과 '없는 컬럼/값 생성'이었다.
이 계층의 목적은 그 두 가지를 **문법적으로 불가능**하게 만드는 것이므로, 테스트도 (i) 회원 투영이
컴파일러 소유인지 (ii) 어휘 밖 값이 반드시 거부되는지를 본다. LLM 호출 없이 IR 만으로 검증한다.
"""

from __future__ import annotations

import json

import pytest

import graph_rag as g
from member_policy import member_condition_canonicals
from targeting_expression import (
    TargetingExpressionError,
    compile_targeting_expression,
    describe_targeting_expression,
    targeting_expression_json_schema,
    validate_targeting_expression,
)


@pytest.fixture(scope="module")
def config() -> dict:
    return g._entity_set_config()


@pytest.fixture(scope="module")
def canonicals() -> dict:
    return member_condition_canonicals()


def _compile(node: dict) -> str:
    return compile_targeting_expression(
        node,
        g._entity_set_config(),
        member_predicate=g._member_condition_predicate,
        member_alias=g._member_alias(),
        member_key=g._member_key_column(),
        relative_date=g._member_dialect().char8_cutoff,
    )


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"member_filter": "vip"}, "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'"),
        ({"member_filter": "employee"}, "B.EMPLOYEE_YN = 'Y'"),
        ({"age": {"min": 30, "max": 39}}, "(B.AGE >= 30 AND B.AGE <= 39)"),
        ({"not": {"member_filter": "employee"}}, "NOT (B.EMPLOYEE_YN = 'Y')"),
    ],
)
def test_leaves_compile_to_the_same_predicates_as_the_slot_path(node, expected):
    assert _compile(node) == expected


def test_relations_compile_to_existence_predicates_not_joins():
    """1:N 관계를 조인으로 붙이면 회원 행이 증폭된다 — IR 은 EXISTS 로만 표현된다."""
    sql = _compile({"relation": {"name": "purchase", "exists": True, "windowDays": 90}})
    assert sql.startswith("EXISTS (")
    assert "OD.MEMBER_NO = B.MEMBER_NO" in sql
    assert "OD.ORDER_DATE >=" in sql
    assert "JOIN" not in sql

    absent = _compile({"relation": {"name": "cart", "exists": False}})
    assert absent.startswith("NOT EXISTS (")
    assert "C.CART_ID = B.MEMBER_ID" in absent


def test_entity_set_operand_reuses_the_step_one_compiler():
    sql = _compile({
        "relation": {
            "name": "purchase", "exists": True,
            "entitySet": {"entity": "product", "measure": "sales_quantity",
                          "direction": "top", "limit": 10, "year": 2019},
        }
    })
    assert "SELECT TOP 10 D.PRODUCT_ID" in sql
    assert "D.ORDER_DATE BETWEEN '20190101' AND '20191231'" in sql


def test_boolean_composition_preserves_grouping():
    sql = _compile({"and": [
        {"or": [{"member_filter": "sns_registered"}, {"member_filter": "membership_member"}]},
        {"not": {"member_filter": "employee"}},
    ]})
    assert sql == "((B.SNS_REG_YN = 'Y' OR B.MEMBERSHIP_YN = 'Y') AND NOT (B.EMPLOYEE_YN = 'Y'))"


@pytest.mark.parametrize(
    "node",
    [
        {"member_filter": "존재하지_않는_조건"},          # 어휘 밖 회원 조건
        {"relation": {"name": "없는관계", "exists": True}},  # 어휘 밖 관계
        {"relation": {"name": "cart", "exists": True, "windowDays": 30}},  # 기간 컬럼 없는 관계
        {"relation": {"name": "purchase", "exists": True,
                      "entitySet": {"entity": "brand", "measure": "sales_quantity",
                                    "direction": "top", "limit": 3, "rankRelation": "cart"}}},  # 장바구니엔 브랜드 키 없음
        {"and": [{"all": True}]},                          # 피연산자 부족
        {"all": True, "member_filter": "vip"},             # 키 중복
        {},                                                 # 빈 노드
        "문자열",                                            # 타입 오류
    ],
)
def test_vocabulary_and_grammar_violations_are_rejected(node, config, canonicals):
    with pytest.raises(TargetingExpressionError):
        validate_targeting_expression(node, config, canonicals)


def test_recursion_is_bounded(config, canonicals):
    node: dict = {"member_filter": "vip"}
    for _ in range(10):
        node = {"not": node}
    with pytest.raises(TargetingExpressionError):
        validate_targeting_expression(node, config, canonicals)


def test_tool_schema_enumerates_only_registered_vocabulary(config, canonicals):
    schema = targeting_expression_json_schema(config, canonicals)
    node = schema["$defs"]["node"]["properties"]
    assert set(node["member_filter"]["enum"]) == set(canonicals)
    assert set(node["relation"]["properties"]["name"]["enum"]) == set(config["relations"])
    entity_set = node["relation"]["properties"]["entitySet"]["properties"]
    assert set(entity_set["entity"]["enum"]) == set(config["entities"])
    assert set(entity_set["measure"]["enum"]) == set(config["measures"])
    # 스키마는 프롬프트에 그대로 실린다 — 직렬화 가능해야 한다.
    assert json.loads(json.dumps(schema, ensure_ascii=False))


def test_condition_labels_are_derived_for_coverage_and_trace():
    node = {"and": [
        {"member_filter": "vip"},
        {"relation": {"name": "purchase", "exists": True,
                      "entitySet": {"entity": "product", "measure": "sales_quantity",
                                    "direction": "top", "limit": 10}}},
    ]}
    assert describe_targeting_expression(node) == ["vip", "purchase", "top_10_product"]


def test_negated_labels_match_the_slot_path_vocabulary():
    """커버리지 검증이 두 경로를 같은 어휘로 대조해야 IR 후보만 탈락하지 않는다."""
    node = {"and": [
        {"member_filter": "sns_registered"},
        {"not": {"member_filter": "employee"}},
    ]}
    assert describe_targeting_expression(node) == ["sns_registered", "non_employee"]
    assert describe_targeting_expression({"not": {"relation": {"name": "cart", "exists": True}}}) == ["cart_absent"]


def test_member_projection_is_owned_by_the_compiler_not_the_model(monkeypatch):
    """모델이 무엇을 내놓든 결과는 회원 집합이다 — 이 계층의 존재 이유."""
    expression = {"relation": {
        "name": "purchase", "exists": True,
        "entitySet": {"entity": "product", "measure": "sales_quantity", "direction": "top", "limit": 10, "year": 2019},
    }}
    monkeypatch.setattr(g, "_llm_targeting_ir_payload", lambda *_a, **_k: {"expression": expression}, raising=False)
    candidate = g._compile_targeting_ir_candidate(expression)
    assert candidate is not None
    sql = candidate["sql"]
    assert sql.startswith("SELECT DISTINCT B.MEMBER_NO AS CUST_ID")
    assert "SELECT TOP 10 D.PRODUCT_ID" in sql
    assert "B.MEMBER_STATE_CD" in sql  # 회원 상태 기본 정책은 컴파일러가 붙인다
