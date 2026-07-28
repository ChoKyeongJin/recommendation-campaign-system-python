"""집계 → 랭킹 → 회원 집합 파생 AST 계약."""

from __future__ import annotations

import copy
from datetime import date

import graph_rag as g
from entity_set import (
    build_derived_set_ast,
    compile_entity_set_predicate,
    derived_set_ast_error,
    entity_set_capability,
    entity_set_node_from_ast,
    parse_entity_set_condition,
)
from graph_rag import _entity_set_config
from targeting_expression import compile_targeting_expression


def test_parser_builds_explicit_aggregation_ranking_member_set_chain() -> None:
    node = parse_entity_set_condition("2019년 가장 많이 팔린 상품 10개를 구매한 고객", _entity_set_config())

    assert node is not None
    member_set = node["derived_set_ast"]
    assert member_set["type"] == "member_set"
    assert member_set["relation"] == "purchase"
    assert member_set["exists"] is True

    ranking = member_set["source"]
    assert (ranking["type"], ranking["direction"], ranking["limit"]) == ("ranking", "top", 10)

    aggregation = ranking["source"]
    assert aggregation == {
        "type": "aggregation",
        "relation": "purchase",
        "group_by": "product",
        "measure": "sales_quantity",
        "window": {"from": "20190101", "to": "20191231", "label": "2019년"},
    }


def test_each_relation_is_owned_by_the_correct_ast_stage() -> None:
    node = parse_entity_set_condition(
        "가장 많이 장바구니에 담은 상품 10개를 구매한 고객", _entity_set_config()
    )

    assert node is not None
    ast = node["derived_set_ast"]
    assert ast["relation"] == "purchase"
    assert ast["source"]["source"]["relation"] == "cart"


def test_ast_only_payload_compiles_to_ranked_member_predicate() -> None:
    ast = build_derived_set_ast(
        member_relation="purchase",
        rank_relation="purchase",
        entity="product",
        measure="sales_amount",
        direction="bottom",
        limit=5,
        window=None,
    )

    sql = compile_entity_set_predicate({"derived_set_ast": ast}, _entity_set_config())

    assert sql is not None
    assert "SELECT TOP 5 D.PRODUCT_ID" in sql
    assert "ORDER BY SUM(D.PAYMENT_AMT) ASC, D.PRODUCT_ID ASC" in sql
    assert sql.startswith("EXISTS")


def test_ast_is_authoritative_over_legacy_projection() -> None:
    node = parse_entity_set_condition("가장 많이 팔린 상품 10개를 구매한 고객", _entity_set_config())
    assert node is not None
    conflicting = copy.deepcopy(node)
    conflicting.update({"entity": "brand", "measure": "sales_amount", "limit": 99})

    normalized = entity_set_node_from_ast(conflicting["derived_set_ast"])
    sql = compile_entity_set_predicate(conflicting, _entity_set_config())

    assert normalized is not None
    assert (normalized["entity"], normalized["measure"], normalized["limit"]) == (
        "product", "sales_quantity", 10,
    )
    assert sql is not None and "SELECT TOP 10 D.PRODUCT_ID" in sql


def test_malformed_ast_fails_closed_in_validation_and_compilation() -> None:
    ast = build_derived_set_ast(
        member_relation="purchase",
        rank_relation="purchase",
        entity="product",
        measure="sales_quantity",
        direction="top",
        limit=10,
    )
    del ast["source"]["source"]
    node = {"derived_set_ast": ast}

    assert derived_set_ast_error(ast) == "invalid_derived_set_aggregation"
    assert entity_set_capability(node, _entity_set_config()) == "invalid_derived_set_aggregation"
    assert compile_entity_set_predicate(node, _entity_set_config()) is None


def test_targeting_expression_preserves_negative_member_set_in_ast() -> None:
    expression = {
        "relation": {
            "name": "purchase",
            "exists": False,
            "entitySet": {
                "entity": "product",
                "measure": "sales_quantity",
                "direction": "top",
                "limit": 10,
            },
        }
    }

    sql = compile_targeting_expression(
        expression,
        _entity_set_config(),
        member_predicate=lambda _canonical: None,
    )

    assert sql.startswith("NOT EXISTS")


def test_targeting_expression_compiles_registered_aggregation_scope_filter() -> None:
    expression = {
        "relation": {
            "name": "purchase",
            "exists": True,
            "entitySet": {
                "entity": "product",
                "measure": "sales_quantity",
                "direction": "top",
                "limit": 5,
                "filters": [{
                    "type": "dimension_filter",
                    "dimension": "category",
                    "operator": "contains",
                    "value": "어린이건강",
                }],
            },
        }
    }

    sql = compile_targeting_expression(
        expression,
        _entity_set_config(),
        member_predicate=lambda _canonical: None,
    )

    assert "SELECT TOP 5 D.PRODUCT_ID" in sql
    assert "INNER JOIN CRM_CM_PRODUCT CP" in sql
    assert "CP.CATEGORY LIKE N'%어린이건강%'" in sql


def test_category_scoped_reverse_order_ranking_becomes_filtered_ast() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'

    plan = g.build_query_plan(query, parser="rules")

    node = plan["target_user"]["entity_set_condition"]
    aggregation = node["derived_set_ast"]["source"]["source"]
    expected_year = date.today().year - 7
    assert node["derived_set_ast"]["source"]["limit"] == 5
    assert aggregation["group_by"] == "product"
    assert aggregation["measure"] == "sales_quantity"
    assert aggregation["window"] == {
        "from": f"{expected_year}0101",
        "to": f"{expected_year}1231",
        "label": f"{expected_year}년",
    }
    assert aggregation["filters"] == [{
        "type": "dimension_filter",
        "dimension": "category",
        "operator": "contains",
        "value": "어린이건강",
    }]
    assert plan["target_user"].get("purchase_object") is None
    assert plan["target_user"].get("purchase_date") is None


def test_category_scoped_ranking_uses_entity_set_sql_not_purchase_history() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'
    plan = g.build_query_plan(query, parser="rules")

    candidate = g.build_sql_template_candidate(plan)

    assert candidate is not None
    assert candidate["id"] == "sql_template:entity_set_targets"
    sql = candidate["sql"]
    assert "SELECT TOP 5 D.PRODUCT_ID" in sql
    assert "INNER JOIN CRM_CM_PRODUCT CP ON CP.PRODUCT_ID = D.PRODUCT_ID" in sql
    assert "CP.CATEGORY LIKE N'%어린이건강%'" in sql
    assert "GROUP BY D.PRODUCT_ID" in sql
    assert "ORDER BY SUM(D.ORDER_QTY) DESC, D.PRODUCT_ID ASC" in sql
    assert f"D.ORDER_DATE BETWEEN '{date.today().year - 7}0101' AND '{date.today().year - 7}1231'" in sql
    coverage = g.validate_sql_condition_coverage(sql, g.required_sql_conditions(plan))
    assert coverage["is_satisfied"] is True
    assert coverage["matched_count"] == 1


def test_category_scoped_ranking_emits_one_verified_entity_set_token() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'
    plan = g.build_query_plan(query, parser="rules")

    tokens = g.build_verified_condition_tokens(plan)

    entity_tokens = [
        token for token in tokens
        if token["path"] == "target_user.entity_set_condition"
    ]
    assert len(entity_tokens) == 1
    token = entity_tokens[0]
    assert token["type"] == "entity_set"
    assert token["operator"] == "exists"
    predicate = token["sql_clauses"][0]
    assert "SELECT TOP 5 D.PRODUCT_ID" in predicate
    assert "CP.CATEGORY LIKE N'%어린이건강%'" in predicate
    assert "GROUP BY D.PRODUCT_ID" in predicate
    assert "ORDER BY SUM(D.ORDER_QTY) DESC, D.PRODUCT_ID ASC" in predicate


def test_deterministic_entity_set_evidence_downgrades_llm_dropped_false_positive() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'
    plan = g.build_query_plan(query, parser="rules")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    verdict = {
        "ran": True,
        "faithful": False,
        "issues": [{
            "type": "dropped",
            "condition": '카테고리가 "어린이건강"인 상품중 많이팔린 5개',
            "detail": "카테고리와 TOP 5 기간이 SQL에 없다고 판단했습니다.",
        }],
    }

    validation = g._validate_sql_delivery_contract(
        query,
        plan,
        candidate["sql"],
        dialect="tsql",
        semantic_verification=verdict,
    )

    assert validation["is_satisfied"] is True
    assert validation["semantic_issues"][0]["severity"] == "warning"
    assert validation["semantic_issues"][0]["exempt_reason"] == "entity_set_predicate_present"


def test_deterministic_entity_set_and_policy_evidence_override_contradictory_llm_issues() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'
    plan = g.build_query_plan(query, parser="rules")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    verdict = {
        "ran": True,
        "faithful": False,
        "issues": [
            {
                "type": "wrong_value",
                "condition": "7년전",
                "detail": "2019년으로 고정되어 상대 기간이 반영되지 않았습니다.",
            },
            {
                "type": "spurious",
                "condition": "회원 상태 필터",
                "detail": "MEMBER_STATE_CD.NORMAL이 원문에 없는 조건입니다.",
            },
        ],
    }

    validation = g._validate_sql_delivery_contract(
        query,
        plan,
        candidate["sql"],
        dialect="tsql",
        semantic_verification=verdict,
    )

    assert validation["is_satisfied"] is True
    assert [issue["exempt_reason"] for issue in validation["semantic_issues"]] == [
        "entity_set_predicate_present",
        "contracted_service_policy_present",
    ]


def test_category_scope_survives_repeated_entity_set_reconciliation() -> None:
    query = '7년전 카테고리가 "어린이건강"인 상품중 많이팔린 5개 구매한 고객 추출해줘'
    plan = g.build_query_plan(query, parser="rules")

    g._apply_entity_set_condition(query, plan)

    filters = plan["target_user"]["entity_set_condition"]["derived_set_ast"]["source"]["source"]["filters"]
    assert filters[0]["value"] == "어린이건강"


def test_unparsed_entity_ranking_fails_closed_and_is_audited() -> None:
    query = "상품 가운데에서 특별히 많이 팔린 5개를 구매한 고객"

    plan = g.build_query_plan(query, parser="rules")

    assert plan["unsupported"]["reason"] == "entity_ranking_not_structured"
    assert plan["unmatched_source_conditions"] == [{
        "type": "entity_ranking",
        "source_text": query,
        "status": "unsupported",
        "reason": "entity_ranking_not_structured",
    }]
    assert g.build_sql_template_candidate(plan) is None
    assert any(
        item["action"] == "unsupported" and item["slot"] == "target_user.entity_set_condition"
        for item in plan["decisions"]
    )
