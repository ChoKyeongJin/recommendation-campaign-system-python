from __future__ import annotations

import json

import pytest

import event_ir
import event_semantic_registry
import graph_rag
import open_text_scope_claims
import sql_guard
from query_structurer.campaign_plan_v4 import (
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _product_exists(
    query: str, *, operator: str = "=", negated_exists: bool = False
) -> event_ir.Condition:
    phrase = "사료를 구매하지 않은" if negated_exists else "사료 외 상품을 구매"
    predicate = event_ir.Comparison(
        operator,
        event_ir.FieldRef("purchase_line.product_text"),
        event_ir.Literal("사료"),
        evidence=_evidence(query, phrase),
    )
    exists: event_ir.Condition = event_ir.Exists(
        event_ir.Filter(event_ir.Source("purchase_line"), predicate),
        evidence=_evidence(query, phrase),
    )
    return event_ir.Not(exists) if negated_exists else exists


def _event_plan(query: str, expression: event_ir.Condition) -> dict:
    return {
        "intent": "find_user_segment",
        "target_user": {},
        "campaign_constraints": {},
        "audience_authority": "event_ir",
        "original_query": query,
        "event_expression": {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
            "receipts": [],
        },
    }


def _compiled_candidate(query: str, expression: event_ir.Condition) -> dict:
    candidate = graph_rag.build_event_expression_sql_candidate(
        _event_plan(query, expression)
    )
    assert candidate is not None
    guarded = sql_guard.validate_sql(
        candidate["sql"],
        allowed_tables=set(candidate["tables"]),
        default_limit=None,
        dialect="tsql",
        schema_columns=sql_guard.load_schema_columns(),
    )
    assert guarded["is_valid"], guarded["issues"]
    return candidate


def _clarification_response(query: str, *, argument: str = "product_scope") -> str:
    evidence_text = "사료 외 상품"
    start = query.index(evidence_text)
    return json.dumps(
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
                "expression": None,
                "issues": [
                    {
                        "code": "ambiguous_requirement",
                        "argument": argument,
                        "message": "대상 상품 범위가 불분명합니다.",
                        "evidence": {
                            "text": evidence_text,
                            "start": start,
                            "end": start + len(evidence_text),
                        },
                    }
                ],
            },
            "semantic_plan": {"nodes": []},
        },
        ensure_ascii=False,
    )


def test_purchase_line_is_a_declarative_purchase_coverage_source() -> None:
    registry = event_semantic_registry.registry()

    assert registry.coverage_family("purchase") == "purchase"
    assert registry.coverage_family("purchase_line") == "purchase"
    assert registry.coverage_family("cart") == "cart"


def test_live_59_product_absence_receipt_survives_final_invariants() -> None:
    query = "사료를 구매하지 않은 회원을 찾아줘."
    expression = _product_exists(query, negated_exists=True)
    plan = _event_plan(query, expression)
    candidate = _compiled_candidate(query, expression)

    assert graph_rag._event_expression_covers(plan, "purchase", "not_exists")
    assert graph_rag._deterministic_dropped_conditions(query, plan) == []
    assert graph_rag._verify_sql_semantic_invariants(
        query, plan, candidate["sql"], []
    ) == {"ran": True, "ok": True, "issues": []}
    assert "NOT EXISTS (" in candidate["sql"]
    assert "LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT" in candidate["sql"]


def test_purchase_coverage_family_does_not_relax_source_or_polarity() -> None:
    positive_query = "사료 외 상품을 구매한 회원을 찾아줘."
    positive = _event_plan(
        positive_query, _product_exists(positive_query, operator="!=")
    )
    cart_absence = _event_plan(
        "장바구니가 비어 있는 회원",
        event_ir.Not(
            event_ir.Exists(
                event_ir.Source("cart"),
                evidence=event_ir.Evidence(
                    text="장바구니가 비어 있는", start=0, end=12
                ),
            )
        ),
    )

    assert not graph_rag._event_expression_covers(
        positive, "purchase", "not_exists"
    )
    assert not graph_rag._event_expression_covers(
        cart_absence, "purchase", "not_exists"
    )


def test_live_60_product_scope_clarification_is_synthesized_as_row_complement() -> None:
    query = "사료 외 상품을 구매한 회원을 찾아줘."
    result = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: _clarification_response(query), max_retries=0
    ).structure(
        QueryStructuringInput(
            query=query,
            context=StructuringContext(
                current_date="2026-08-04", timezone="Asia/Seoul"
            ),
        )
    )

    expression_raw = result["event_expression"]["expression"]
    assert expression_raw["type"] == "exists"
    assert expression_raw["relation"]["type"] == "filter"
    comparison = expression_raw["relation"]["where"]
    assert comparison == {
        "type": "comparison",
        "operator": "!=",
        "left": {"type": "field", "name": "purchase_line.product_text"},
        "right": {"type": "literal", "value": "사료"},
        "evidence": {"text": "사료 외 상품을 구매", "start": 0, "end": 11},
    }
    assert result["semantic_ir"]["status"] == "resolved"
    assert any(
        item.get("filter") == "open_text_scope_claims.single_product_complement"
        for item in result.get("decisions", [])
    )

    expression = event_ir.condition_from_dict(expression_raw)
    candidate = _compiled_candidate(query, expression)
    sql = candidate["sql"]
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL OD" in sql
    assert "NOT EXISTS (" not in sql
    assert "LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT" in sql
    assert "NOT (CASE WHEN" in sql
    for column in (
        "PRODUCT_NAME",
        "CATEGORY",
        "CATEGORYL_NAME",
        "CATEGORYM_NAME",
        "CATEGORYS_NAME",
    ):
        assert f"OD_PRODUCT.{column} LIKE N'%사료%'" in sql
        assert f"OD_PRODUCT.{column} IS NOT NULL" in sql


@pytest.mark.parametrize(
    "query",
    [
        "서울 거주 사료 외 상품을 구매한 회원을 찾아줘.",
        "사료 외 상품을 구매한 여성 회원을 찾아줘.",
        "사료 외 상품을 구매한 회원 또는 골드 회원",
        "사료 외 상품을 구매한 회원 중 최근 30일 로그인한 회원",
        "사료를 구매하지 않은 회원을 찾아줘.",
    ],
)
def test_product_complement_synthesis_refuses_extra_or_absence_meaning(
    query: str,
) -> None:
    assert (
        open_text_scope_claims.synthesize_single_product_complement_purchase(query)
        is None
    )


def test_product_complement_synthesis_does_not_override_an_unrelated_issue() -> None:
    query = "사료 외 상품을 구매한 회원을 찾아줘."
    enriched = attach_campaign_query_plan_v4_identity(
        json.loads(_clarification_response(query, argument="audience.gender")),
        query,
        current_date="2026-08-04",
    )

    assert enriched["audience_requirement"]["expression"] is None
    assert enriched.get("event_expression") is None
    assert not any(
        item.get("filter") == "open_text_scope_claims.single_product_complement"
        for item in enriched.get("decisions", [])
    )
