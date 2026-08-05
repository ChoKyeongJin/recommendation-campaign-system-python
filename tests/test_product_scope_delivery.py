from __future__ import annotations

import json
from pathlib import Path

import pytest

import audience_runtime
import canonical_audience_claims
import event_ir
import graph_rag
import open_text_scope_claims
import sql_guard
from query_structurer.semantic_ir import extract_literal_bindings
from query_structurer.structurer import (
    LLMCampaignQueryPlanV4Structurer,
    _decode_campaign_query_plan_v4_response,
)
from query_structurer.types import QueryStructuringInput, StructuringContext
from sql_ast import SelectAst, validate_select_ast


def _evidence(query: str, text: str, *, after: int = 0) -> event_ir.Evidence:
    start = query.index(text, after)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _bare_purchase(query: str, evidence_text: str) -> event_ir.Exists:
    return event_ir.Exists(
        event_ir.Source("purchase_line"),
        evidence=_evidence(query, evidence_text),
    )


def _filtered_purchase(
    query: str,
    term: str,
    evidence_text: str,
    *,
    complement: bool = False,
) -> event_ir.Exists:
    comparison: event_ir.Condition = event_ir.Comparison(
        "=",
        event_ir.FieldRef("purchase_line.product_text"),
        event_ir.Literal(term),
        evidence=_evidence(query, term),
    )
    if complement:
        comparison = event_ir.Not(comparison)
    return event_ir.Exists(
        event_ir.Filter(event_ir.Source("purchase_line"), comparison),
        evidence=_evidence(query, evidence_text),
    )


def _issues(query: str, expression: event_ir.Condition) -> list[dict]:
    return canonical_audience_claims.canonical_claim_issues(
        query,
        expression,
        extract_literal_bindings(query, current_date="2026-08-04"),
        audience_runtime.catalog_snapshot(),
    )


def _scope_issues(query: str, expression: event_ir.Condition) -> list[dict]:
    return [
        issue
        for issue in _issues(query, expression)
        if issue.get("argument") == "catalog_scope.purchase_line.product_text"
    ]


def _candidate(query: str, expression: event_ir.Condition) -> dict:
    plan = {
        "event_expression": {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
            "receipts": [],
        },
        "audience_authority": "event_ir",
        "campaign_constraints": {},
        "original_query": query,
        "intent": "find_user_segment",
    }
    candidate = graph_rag.build_event_expression_sql_candidate(plan)
    assert candidate is not None, plan
    guarded = sql_guard.validate_sql(
        candidate["sql"],
        allowed_tables=set(candidate["tables"]),
        default_limit=None,
        dialect="tsql",
        schema_columns=sql_guard.load_schema_columns(),
    )
    assert guarded["is_valid"], guarded["issues"]
    return candidate


def test_product_absence_rejects_a_bare_source_and_accepts_filtered_not_exists() -> None:
    query = "사료를 구매하지 않은 회원을 찾아줘."
    phrase = "사료를 구매하지 않은"

    assert len(_scope_issues(query, event_ir.Not(_bare_purchase(query, phrase)))) == 1

    expression = event_ir.Not(_filtered_purchase(query, "사료", phrase))
    assert _scope_issues(query, expression) == []
    candidate = _candidate(query, expression)
    sql = candidate["sql"]
    assert candidate["validation"]["issues"] == []
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL OD" in sql
    assert "LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT" in sql
    assert "OD_PRODUCT.PRODUCT_NAME LIKE N'%사료%'" in sql
    assert "OD_PRODUCT.CATEGORYS_NAME LIKE N'%사료%'" in sql


def test_other_product_requires_the_inner_product_complement() -> None:
    query = "사료 외 상품을 구매한 회원을 찾아줘."
    phrase = "사료 외 상품을 구매한"

    assert len(_scope_issues(query, _bare_purchase(query, phrase))) == 1
    assert len(_scope_issues(query, _filtered_purchase(query, "사료", phrase))) == 1

    expression = _filtered_purchase(query, "사료", phrase, complement=True)
    assert _scope_issues(query, expression) == []
    candidate = _candidate(query, expression)
    sql = candidate["sql"]
    assert candidate["validation"]["issues"] == []
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL OD" in sql
    assert "OD_PRODUCT.PRODUCT_NAME IS NOT NULL" in sql
    assert "NOT (CASE WHEN" in sql
    assert "LIKE N'%사료%'" in sql


def test_absent_one_product_but_bought_another_requires_both_scopes() -> None:
    query = "사료를 구매한 적은 없지만 다른 상품은 구매한 회원을 찾아줘."
    absent_phrase = "사료를 구매한 적은 없지만"
    other_phrase = "다른 상품은 구매한"
    bare = event_ir.And((
        event_ir.Not(_bare_purchase(query, absent_phrase)),
        _bare_purchase(query, other_phrase),
    ))

    assert len(_scope_issues(query, bare)) == 2

    expression = event_ir.And((
        event_ir.Not(_filtered_purchase(query, "사료", absent_phrase)),
        _filtered_purchase(query, "사료", other_phrase, complement=True),
    ))
    assert _scope_issues(query, expression) == []
    candidate = _candidate(query, expression)
    sql = candidate["sql"]
    assert candidate["validation"]["issues"] == []
    assert sql.count("LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT") == 2
    assert sql.count("LIKE N'%사료%'") == 10
    assert "NOT EXISTS (" in sql
    assert ") AND (EXISTS (" in sql
    assert "NOT (CASE WHEN" in sql


def test_all_product_list_needs_one_independent_filtered_exists_per_item() -> None:
    query = "사료, 간식, 장난감을 모두 구매한 회원을 찾아줘."
    terms = ("사료", "간식", "장난감")
    bare = event_ir.And(tuple(_bare_purchase(query, term) for term in terms))

    assert len(_scope_issues(query, bare)) == 3

    one_any_row = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("purchase_line"),
            event_ir.Or(tuple(
                event_ir.Comparison(
                    "=",
                    event_ir.FieldRef("purchase_line.product_text"),
                    event_ir.Literal(term),
                    evidence=_evidence(query, term),
                )
                for term in terms
            )),
        ),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )
    assert len(_scope_issues(query, one_any_row)) == 2

    expression = event_ir.And(tuple(
        _filtered_purchase(query, term, term) for term in terms
    ))
    assert _scope_issues(query, expression) == []
    candidate = _candidate(query, expression)
    sql = candidate["sql"]
    assert candidate["validation"]["issues"] == []
    assert sql.count("EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL OD") == 3
    assert sql.count("LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT") == 3
    for term in terms:
        assert f"OD_PRODUCT.PRODUCT_NAME LIKE N'%{term}%'" in sql
        assert f"OD_PRODUCT.CATEGORYS_NAME LIKE N'%{term}%'" in sql


def test_generic_purchase_and_later_ad_copy_do_not_invent_a_product_scope() -> None:
    generic = "구매한 회원을 찾아줘."
    assert _scope_issues(generic, _bare_purchase(generic, "구매한")) == []

    ad_copy = "구매한 회원에게 사료 광고를 보내줘."
    assert _scope_issues(ad_copy, _bare_purchase(ad_copy, "구매한")) == []


def test_multiword_product_phrase_is_preserved_and_cannot_be_silently_reduced() -> None:
    query = "반려견 사료를 구매한 회원을 찾아줘."
    phrase = "반려견 사료를 구매한"

    exact = _filtered_purchase(query, "반려견 사료", phrase)
    assert _scope_issues(query, exact) == []
    candidate = _candidate(query, exact)
    assert "OD_PRODUCT.PRODUCT_NAME LIKE N'%반려견 사료%'" in candidate["sql"]

    reduced = _filtered_purchase(query, "사료", phrase)
    issues = _scope_issues(query, reduced)
    assert len(issues) == 1
    assert issues[0]["evidence"]["text"] == "반려견 사료를 구매"


def test_multiword_all_product_list_keeps_each_complete_phrase() -> None:
    query = "반려견 사료, 고양이 간식, 지능형 장난감을 모두 구매한 회원을 찾아줘."
    claims = open_text_scope_claims.extract_purchase_product_claims(query)

    assert [claim.value for claim in claims] == [
        "반려견 사료",
        "고양이 간식",
        "지능형 장난감",
    ]
    assert len({claim.group for claim in claims}) == 1


@pytest.mark.parametrize(
    "query",
    [
        "최근 30일 반려견 사료를 구매한 회원을 찾아줘.",
        "여성이면서 유기농 반려견 사료를 구매한 회원을 찾아줘.",
        "30대 여성 유기농 반려견 사료를 구매한 회원을 찾아줘.",
        "VIP이고 유기농 반려견 사료를 구매한 회원을 찾아줘.",
        "서울에 거주하며 유기농 반려견 사료를 구매한 회원을 찾아줘.",
        "서울 거주 유기농 반려견 사료를 구매한 회원을 찾아줘.",
    ],
)
def test_context_before_multiword_product_is_not_absorbed_into_the_search_term(
    query: str,
) -> None:
    claims = open_text_scope_claims.extract_purchase_product_claims(query)

    assert len(claims) == 1
    expected = "유기농 반려견 사료" if "유기농" in query else "반려견 사료"
    assert claims[0].value == expected
    assert query[claims[0].start:].startswith(expected)


def test_gender_word_inside_a_product_phrase_is_not_unconditionally_trimmed() -> None:
    query = "여성 유산균을 구매한 회원을 찾아줘."
    claims = open_text_scope_claims.extract_purchase_product_claims(query)

    assert len(claims) == 1
    assert claims[0].value == "여성 유산균"


def test_compiler_generated_product_join_aliases_are_allowed_but_unknown_aliases_fail() -> None:
    config_path = Path("docs/data/runtime/sql/member_target_filters.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))["validation"]

    for alias in ("OD_PRODUCT", "EC_PRODUCT"):
        ast = SelectAst(columns=["1"], from_lines=[f"FROM CRM_CM_PRODUCT {alias}"])
        assert validate_select_ast(ast, config) == []

    rogue = SelectAst(columns=["1"], from_lines=["FROM CRM_CM_PRODUCT UNTRUSTED_PRODUCT"])
    assert any(
        "UNTRUSTED_PRODUCT" in issue for issue in validate_select_ast(rogue, config)
    )


def test_missing_product_filters_trigger_structurer_retry_and_accept_the_repair() -> None:
    query = "사료를 구매한 적은 없지만 다른 상품은 구매한 회원을 찾아줘."
    absent_phrase = "사료를 구매한 적은 없지만"
    other_phrase = "다른 상품은 구매한"
    bare = event_ir.And((
        event_ir.Not(_bare_purchase(query, absent_phrase)),
        _bare_purchase(query, other_phrase),
    ))
    corrected = event_ir.And((
        event_ir.Not(_filtered_purchase(query, "사료", absent_phrase)),
        _filtered_purchase(query, "사료", other_phrase, complement=True),
    ))
    envelope = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
    }
    responses = iter((
        json.dumps({
            **envelope,
            "audience_requirement": {"expression": bare.to_dict(), "issues": []},
        }, ensure_ascii=False),
        json.dumps({
            **envelope,
            "audience_requirement": {"expression": corrected.to_dict(), "issues": []},
        }, ensure_ascii=False),
    ))
    calls: list[list[dict[str, str]]] = []
    events: list[tuple[str, dict]] = []

    def complete(messages: list[dict[str, str]]) -> str:
        calls.append(list(messages))
        return next(responses)

    result = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=1,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert len(calls) == 2
    failure = next(
        payload for name, payload in events
        if name == "campaign_query_plan_v4_attempt_failed"
    )
    assert "catalog_scope.purchase_line.product_text" in failure["error"]
    assert "상품명/카테고리 contains 필터" in calls[1][-1]["content"]
    assert result["semantic_ir"]["status"] == "resolved"
    assert result["event_expression"]["expression"] == corrected.to_dict()


def test_extra_brace_before_audience_issues_is_repaired_then_fully_validated() -> None:
    query = "사료를 구매하지 않은 회원을 찾아줘."
    phrase = "사료를 구매하지 않은"
    expression = event_ir.Not(_filtered_purchase(query, "사료", phrase))
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": expression.to_dict(),
            "issues": [],
        },
    }
    valid = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    marker = ',"issues":[]'
    marker_index = valid.index(marker)
    malformed = valid[:marker_index] + "}" + valid[marker_index:]
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed)

    events: list[tuple[str, dict]] = []
    result = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: malformed,
        max_retries=0,
        on_event=lambda name, item: events.append((name, item)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert result["semantic_ir"]["status"] == "resolved"
    assert result["event_expression"]["expression"] == expression.to_dict()
    assert any(
        name == "campaign_query_plan_v4_syntax_repair" for name, _item in events
    )
    candidate = _candidate(query, expression)
    assert candidate["validation"]["issues"] == []
    assert "NOT EXISTS (" in candidate["sql"]
    assert "OD_PRODUCT.PRODUCT_NAME LIKE N'%사료%'" in candidate["sql"]


def test_extra_brace_before_an_exists_evidence_is_repaired_by_the_same_rule() -> None:
    """같은 결함(잉여 닫는 중괄호)의 다른 자리 — 수리 규칙이 이웃 키에 매이면 안 된다.

    실측(2026-08-05, '오늘 주문한 회원'): 모델이 Exists 를 한 괄호 일찍 닫아 evidence 가 밖으로
    나갔고, 그때 유일하게 스키마를 통과하는 수리는 evidence 를 Exists 안으로 되돌리는 것이었다.
    수리 후보를 ``,"issues"`` 앞 중괄호로 한정했던 동안 이 표본은 파싱 실패로 버려졌고, 기간이
    올바르게 들어 있던 표현이 통째로 사라져 확인 질문이나 기간 없는 SQL 로 귀결됐다.
    """
    query = "오늘 주문한 회원을 알려줘"
    evidence = _evidence(query, "오늘 주문한")
    # 모델이 실제로 내보내는 wire 형태 그대로 쓴다(앱이 덧붙이는 파생 키 없이).
    expression = {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": "purchase"},
            "where": {
                "type": "time_filter",
                "field": {
                    "type": "field",
                    "name": f"purchase.{event_ir.TIME_FIELD_SUFFIX}",
                },
                "window": {
                    "type": "interval",
                    "start": "2026-08-05",
                    "end_exclusive": "2026-08-06",
                },
            },
        },
        "evidence": {
            "text": evidence.text,
            "start": evidence.start,
            "end": evidence.end,
        },
    }
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": expression, "issues": []},
    }
    valid = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    marker = ',"evidence":'
    marker_index = valid.index(marker)
    malformed = valid[:marker_index] + "}" + valid[marker_index:]
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed)

    repaired, repair = _decode_campaign_query_plan_v4_response(malformed)

    assert repair is not None and repair["kind"] == "remove_extra_closing_brace"
    assert repaired["audience_requirement"]["expression"] == expression


def test_json_repair_refuses_an_arbitrary_trailing_object() -> None:
    with pytest.raises(json.JSONDecodeError):
        _decode_campaign_query_plan_v4_response('{"intent":"x"}{}')


def test_misplaced_exists_where_and_singleton_not_operands_are_normalized() -> None:
    query = "사료를 구매한 적은 없지만 다른 상품은 구매한 회원을 찾아줘."
    absent_phrase = "사료를 구매한 적은 없지만"
    other_phrase = "다른 상품은 구매한"
    first = _filtered_purchase(query, "사료", absent_phrase).to_dict()
    second = _filtered_purchase(
        query, "사료", other_phrase, complement=True
    ).to_dict()
    first_filter = first["relation"]
    second_filter = second["relation"]
    second_not = second_filter["where"]
    misplaced = {
        "type": "and",
        "operands": [
            {
                "type": "not",
                "operand": {
                    "type": "exists",
                    "relation": first_filter["relation"],
                    "where": first_filter["where"],
                    "evidence": first["evidence"],
                },
            },
            {
                "type": "exists",
                "relation": second_filter["relation"],
                "where": {
                    "type": "not", "operands": [second_not["operand"]],
                },
                "evidence": second["evidence"],
            },
        ],
    }
    expected = event_ir.And((
        event_ir.Not(_filtered_purchase(query, "사료", absent_phrase)),
        _filtered_purchase(query, "사료", other_phrase, complement=True),
    ))
    response = json.dumps({
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": misplaced, "issues": []},
    }, ensure_ascii=False)

    result = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: response, max_retries=0
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert result["semantic_ir"]["status"] == "resolved"
    assert result["event_expression"]["expression"] == expected.to_dict()
    candidate = _candidate(query, expected)
    assert candidate["validation"]["issues"] == []
    assert candidate["sql"].count("LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT") == 2
    assert "NOT EXISTS (" in candidate["sql"]
    assert ") AND (EXISTS (" in candidate["sql"]
