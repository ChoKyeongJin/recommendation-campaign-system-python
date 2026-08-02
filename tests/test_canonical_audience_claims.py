from __future__ import annotations

from datetime import date

import pytest

import audience_runtime
import canonical_audience_claims
import event_compiler
import event_ir
from query_structurer.semantic_ir import extract_literal_bindings


AMOUNT_QUERY = "2019년에 이십만원 이상을 구매한고객에서 남자는 제외해."
RANK_QUERY = "2019년 가장 많이 팔린 상품 10개를 구매한고객만 추출해"
YEAR_2019 = event_ir.AbsoluteInterval(date(2019, 1, 1), date(2020, 1, 1))


def _evidence(query: str, start: int = 0, end: int | None = None) -> event_ir.Evidence:
    end = len(query) if end is None else end
    return event_ir.Evidence(query[start:end], start, end)


def _amount_expression() -> event_ir.Condition:
    gender_start = AMOUNT_QUERY.index("남자")
    purchase = event_ir.Filter(
        event_ir.Source("purchase"),
        event_ir.TimeFilter(event_ir.FieldRef("purchase.occurred_at"), YEAR_2019),
    )
    return event_ir.And((
        event_ir.Comparison(
            ">=",
            event_ir.Aggregate("sum", purchase, event_ir.FieldRef("purchase.amount")),
            event_ir.Literal(200000),
            evidence=_evidence(AMOUNT_QUERY, 0, gender_start),
        ),
        event_ir.Not(event_ir.Comparison(
            "=",
            event_ir.FieldRef("subject.gender"),
            event_ir.Literal("male"),
            evidence=_evidence(AMOUNT_QUERY, gender_start),
        )),
    ))


def _rank_expression(*, window_on_rank: bool = True) -> event_ir.Condition:
    global_rows: event_ir.Relation = event_ir.Source("purchase_line", correlation="none")
    member_rows: event_ir.Relation = event_ir.Source("purchase_line")
    timed = event_ir.TimeFilter(event_ir.FieldRef("purchase_line.occurred_at"), YEAR_2019)
    if window_on_rank:
        global_rows = event_ir.Filter(global_rows, timed)
    else:
        member_rows = event_ir.Filter(member_rows, timed)
    summary = event_ir.Summarize(
        global_rows,
        keys=(event_ir.NamedExpression(
            "product_id", event_ir.FieldRef("purchase_line.product_id")
        ),),
        measures=(event_ir.NamedMeasure(
            "quantity", "sum", event_ir.FieldRef("purchase_line.quantity")
        ),),
    )
    ranked = event_ir.Limit(
        event_ir.Order(summary, (
            event_ir.SortKey("quantity", "desc"),
            event_ir.SortKey("product_id", "asc"),
        )),
        10,
    )
    membership = event_ir.Join(
        member_rows,
        ranked,
        event_ir.Comparison(
            "=",
            event_ir.FieldRef("purchase_line.product_id"),
            event_ir.FieldRef("purchase_line.product_id"),
        ),
        kind="semi",
    )
    return event_ir.Exists(membership, evidence=_evidence(RANK_QUERY))


def _issues(query: str, expression: event_ir.Condition) -> list[dict]:
    return canonical_audience_claims.canonical_claim_issues(
        query,
        expression,
        extract_literal_bindings(query, current_date="2026-08-02"),
        audience_runtime.catalog_snapshot(),
    )


def test_amount_window_and_negative_catalog_value_are_fully_covered() -> None:
    assert _issues(AMOUNT_QUERY, _amount_expression()) == []


def test_nested_date_window_is_owned_structurally_when_atom_evidence_is_narrow() -> None:
    expression = _amount_expression()
    amount = expression.operands[0]
    narrow = event_ir.Comparison(
        amount.operator,
        amount.left,
        amount.right,
        evidence=_evidence(AMOUNT_QUERY, 7, 14),
    )

    assert _issues(AMOUNT_QUERY, event_ir.And((narrow, expression.operands[1]))) == []


def test_omitted_gender_exclusion_is_blocked_even_when_other_literals_match() -> None:
    expression = _amount_expression().operands[0]

    issues = _issues(AMOUNT_QUERY, expression)

    assert any(issue["argument"] == "catalog_value.subject.gender" for issue in issues)


def test_money_cannot_be_consumed_by_a_non_currency_field() -> None:
    gender_start = AMOUNT_QUERY.index("남자")
    expression = event_ir.And((
        event_ir.Exists(
            event_ir.Filter(
                event_ir.Source("purchase"),
                event_ir.TimeFilter(event_ir.FieldRef("purchase.occurred_at"), YEAR_2019),
            ),
            evidence=_evidence(AMOUNT_QUERY),
        ),
        event_ir.Comparison(
            ">=", event_ir.FieldRef("subject.age"), event_ir.Literal(200000),
            evidence=_evidence(AMOUNT_QUERY),
        ),
        event_ir.Not(event_ir.Comparison(
            "=", event_ir.FieldRef("subject.gender"), event_ir.Literal("male"),
            evidence=_evidence(AMOUNT_QUERY, gender_start),
        )),
    ))

    issues = _issues(AMOUNT_QUERY, expression)

    assert any(issue["argument"].endswith(".unit") for issue in issues)


def test_rank_summarize_order_limit_and_semi_join_cover_the_source_obligation() -> None:
    assert _issues(RANK_QUERY, _rank_expression()) == []


def test_rank_limit_can_use_nested_join_evidence_with_narrow_exists_evidence() -> None:
    expression = _rank_expression()
    join = expression.relation
    rank_start = RANK_QUERY.index("가장")
    rank_end = RANK_QUERY.index("를 구매")
    purchase_start = RANK_QUERY.index("구매한")
    on = event_ir.Comparison(
        join.on.operator,
        join.on.left,
        join.on.right,
        evidence=_evidence(RANK_QUERY, rank_start, rank_end),
    )
    scoped = event_ir.Exists(
        event_ir.Join(join.left, join.right, on, kind=join.kind),
        evidence=_evidence(RANK_QUERY, purchase_start),
    )

    assert _issues(RANK_QUERY, scoped) == []


def test_rank_period_must_belong_to_the_global_ranking_relation() -> None:
    issues = _issues(RANK_QUERY, _rank_expression(window_on_rank=False))

    assert any(issue["argument"] == "source_semantics.ranked_entity_set" for issue in issues)


def test_rank_membership_must_join_the_entity_key_on_both_scopes() -> None:
    expression = _rank_expression()
    join = expression.relation
    wrong_on = event_ir.Comparison(
        "=",
        event_ir.FieldRef("purchase_line.member_id"),
        event_ir.FieldRef("purchase_line.product_id"),
        evidence=_evidence(RANK_QUERY),
    )
    wrong = event_ir.Exists(
        event_ir.Join(join.left, join.right, wrong_on, kind=join.kind),
        evidence=expression.evidence,
    )

    issues = _issues(RANK_QUERY, wrong)

    assert any(issue["argument"] == "source_semantics.ranked_entity_set" for issue in issues)


def test_plain_purchase_exists_cannot_silently_replace_ranked_membership() -> None:
    incomplete = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("purchase_line"),
            event_ir.TimeFilter(event_ir.FieldRef("purchase_line.occurred_at"), YEAR_2019),
        ),
        evidence=_evidence(RANK_QUERY),
    )

    issues = _issues(RANK_QUERY, incomplete)

    assert any(issue["argument"] == "source_semantics.ranked_entity_set" for issue in issues)
    assert any(issue["argument"].startswith("literal_bindings[") for issue in issues)


def test_catalog_value_is_lowered_and_unknown_canonical_value_fails_closed() -> None:
    catalog = audience_runtime.resolve_audience_catalog()
    context = catalog.compile_context(literals=True)
    known = event_ir.Comparison(
        "=", event_ir.FieldRef("subject.gender"), event_ir.Literal("male")
    )

    assert "GENDER_CD.MALE" in event_compiler.compile_expression(known, context=context).sql

    unknown = event_ir.Comparison(
        "=", event_ir.FieldRef("subject.gender"), event_ir.Literal("unregistered")
    )
    with pytest.raises(event_compiler.SqlCompileError):
        event_compiler.compile_expression(unknown, context=catalog.compile_context(literals=True))


def test_negative_attribute_is_a_two_valued_audience_complement() -> None:
    sql = event_compiler.compile_expression(
        event_ir.Not(event_ir.Comparison(
            "=", event_ir.FieldRef("subject.gender"), event_ir.Literal("male")
        )),
        context=audience_runtime.resolve_audience_catalog().compile_context(literals=True),
    ).sql

    assert "CASE WHEN" in sql
    assert "GENDER_CD.MALE" in sql
