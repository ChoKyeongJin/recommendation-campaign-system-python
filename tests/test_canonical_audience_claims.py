from __future__ import annotations

from datetime import date

import pytest

import audience_runtime
import canonical_audience_claims
import event_compiler
import event_ir
from query_structurer import audience_execution
from query_structurer.semantic_ir import extract_literal_bindings

AMOUNT_QUERY = "2019년에 이십만원 이상을 구매한고객에서 남자는 제외해."
RANK_QUERY = "2019년 가장 많이 팔린 상품 10개를 구매한고객만 추출해"
LOGIN_CHANNEL_QUERY = "앱으로 로그인하지 않은 회원을 찾아줘."
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


def test_app_login_exclusion_keeps_the_channel_and_null_in_the_complement() -> None:
    evidence_text = "앱으로 로그인하지 않은"
    start = LOGIN_CHANNEL_QUERY.index(evidence_text)
    expression = event_ir.Not(event_ir.Comparison(
        "=",
        event_ir.FieldRef("subject.last_login_channel"),
        event_ir.Literal("app_user"),
        evidence=_evidence(
            LOGIN_CHANNEL_QUERY, start, start + len(evidence_text)
        ),
    ))

    assert _issues(LOGIN_CHANNEL_QUERY, expression) == []
    sql = event_compiler.compile_expression(
        expression,
        context=audience_runtime.resolve_audience_catalog().compile_context(
            literals=True
        ),
    ).sql

    assert sql == (
        "NOT (CASE WHEN (B.LAST_LOGIN_CHANNEL = 'DEVICE_TYPE_CD.APP') "
        "THEN 1 ELSE 0 END = 1)"
    )


def _login_channel_comparison(query: str, operator: str) -> event_ir.Comparison:
    evidence_text = "앱으로 로그인하지 않은" if "하지" in query else "앱으로 로그인한"
    start = query.index(evidence_text)
    return event_ir.Comparison(
        operator,
        event_ir.FieldRef("subject.last_login_channel"),
        event_ir.Literal("app_user"),
        evidence=_evidence(query, start, start + len(evidence_text)),
    )


def test_app_login_exclusion_accepts_direct_not_equal_and_compiles_the_channel() -> None:
    expression = _login_channel_comparison(LOGIN_CHANNEL_QUERY, "!=")

    assert _issues(LOGIN_CHANNEL_QUERY, expression) == []
    sql = event_compiler.compile_expression(
        expression,
        context=audience_runtime.resolve_audience_catalog().compile_context(literals=True),
    ).sql
    assert sql == "B.LAST_LOGIN_CHANNEL != 'DEVICE_TYPE_CD.APP'"


@pytest.mark.parametrize(
    ("query", "operator"),
    [
        (LOGIN_CHANNEL_QUERY, "="),
        ("앱으로 로그인한 회원을 찾아줘.", "!="),
    ],
)
def test_login_channel_comparison_still_fails_closed_on_real_polarity_mismatch(
    query: str, operator: str,
) -> None:
    issues = _issues(query, _login_channel_comparison(query, operator))

    assert any(
        issue["argument"] == "catalog_value.subject.last_login_channel"
        for issue in issues
    )


def test_current_vip_satisfies_the_shared_grade_domain_once() -> None:
    query = "VIP 등급 회원을 찾아줘"
    span = "VIP 등급"
    start = query.index(span)
    expression = event_ir.Comparison(
        "=",
        event_ir.FieldRef("subject.grade"),
        event_ir.Literal("vip"),
        evidence=_evidence(query, start, start + len(span)),
    )

    assert _issues(query, expression) == []


def test_specific_worth_grade_alias_cannot_be_claimed_by_member_grade() -> None:
    query = "가치등급 VIP 회원을 찾아줘"
    span = "가치등급 VIP"
    start = query.index(span)
    wrong_axis = event_ir.Comparison(
        "=",
        event_ir.FieldRef("subject.grade"),
        event_ir.Literal("vip"),
        evidence=_evidence(query, start, start + len(span)),
    )
    correct_axis = event_ir.Comparison(
        "=",
        event_ir.FieldRef("member_month_snapshot.worth_grade"),
        event_ir.Literal("vip_worth_grade"),
        evidence=_evidence(query, start, start + len(span)),
    )

    issues = _issues(query, wrong_axis)
    assert [issue["argument"] for issue in issues] == [
        "catalog_value.member_month_snapshot.worth_grade"
    ]
    assert issues[0]["evidence"]["text"] == span
    assert _issues(query, correct_axis) == []


def _open_text_expression(
    query: str,
    literal: str,
    *,
    source: str = "purchase_line",
    field: str = "purchase_line.product_text",
    negated: bool = False,
    evidence_text: str | None = None,
) -> event_ir.Condition:
    evidence_text = query if evidence_text is None else evidence_text
    start = query.index(evidence_text)
    evidence = _evidence(query, start, start + len(evidence_text))
    exists: event_ir.Condition = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source(source),
            event_ir.Comparison(
                "=",
                event_ir.FieldRef(field),
                event_ir.Literal(literal),
                evidence=evidence,
            ),
        ),
        evidence=evidence,
    )
    return event_ir.Not(exists) if negated else exists


def _resolve_open_text(query: str, expression: event_ir.Condition):
    return audience_execution.run_audience_resolver(
        {
            "audience_requirement": {
                "expression": expression.to_dict(),
                "issues": [],
            },
            "literal_bindings": extract_literal_bindings(
                query, current_date="2026-08-04"
            ),
        },
        query,
        current_date="2026-08-04",
    )


def test_run_audience_resolver_blocks_ungrounded_product_text_literal() -> None:
    query = "사료를 구매한 회원"
    resolution = _resolve_open_text(
        query, _open_text_expression(query, "장난감")
    )

    assert resolution is not None
    assert any(
        issue["code"] == "validation_mismatch"
        and issue["argument"] == "catalog_literal.purchase_line.product_text"
        for issue in resolution.issues
    )


def test_run_audience_resolver_accepts_grounded_product_text_literal() -> None:
    query = "사료를 구매한 회원"
    resolution = _resolve_open_text(query, _open_text_expression(query, "사료"))

    assert resolution is not None
    assert resolution.issues == []


@pytest.mark.parametrize(
    ("query", "literal", "source", "field", "negated"),
    [
        (
            "사료를 구매하지 않은 회원",
            "사료",
            "purchase_line",
            "purchase_line.product_name",
            True,
        ),
        (
            "장바구니에 사료를 담은 회원",
            "사료",
            "cart",
            "cart.product_category",
            False,
        ),
        (
            "DOGFOOD를 구매한 회원",
            "dogfood",
            "purchase_line",
            "purchase_line.product_text",
            False,
        ),
    ],
)
def test_open_text_grounding_preserves_negation_cart_and_casefold(
    query: str,
    literal: str,
    source: str,
    field: str,
    negated: bool,
) -> None:
    resolution = _resolve_open_text(
        query,
        _open_text_expression(
            query,
            literal,
            source=source,
            field=field,
            negated=negated,
        ),
    )

    assert resolution is not None
    assert resolution.issues == []


def test_open_text_literal_must_be_inside_its_own_evidence_span() -> None:
    query = "사료를 구매한 회원에게 장난감 광고를 보내줘"
    expression = _open_text_expression(
        query,
        "장난감",
        evidence_text="사료를 구매한 회원",
    )

    issues = _issues(query, expression)

    assert any(
        issue["argument"] == "catalog_literal.purchase_line.product_text"
        for issue in issues
    )


def test_contains_grounding_is_driven_by_the_catalog_declaration() -> None:
    query = "원문에 있는 검색어"
    expression = event_ir.Comparison(
        "=",
        event_ir.FieldRef("custom.open_text"),
        event_ir.Literal("모델이 만든 값"),
        evidence=_evidence(query),
    )
    issues = canonical_audience_claims.catalog_claim_issues(
        query,
        expression,
        [],
        {"fields": {"custom.open_text": {"match_mode": "contains"}}},
    )

    assert [issue["argument"] for issue in issues] == [
        "catalog_literal.custom.open_text"
    ]
