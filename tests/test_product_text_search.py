from __future__ import annotations

from collections.abc import Callable

import pytest

import audience_runtime
import event_compiler
import event_ir
import query_pipeline

EVIDENCE = event_ir.Evidence(text="상품 조건", start=0, end=5)


def _match(term: object, *, field: str = "purchase_line.product_text") -> event_ir.Comparison:
    return event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef(field),
        right=event_ir.Literal(term),
        evidence=EVIDENCE,
    )


def _purchases(where: event_ir.Condition) -> event_ir.Exists:
    return event_ir.Exists(
        relation=event_ir.Filter(event_ir.Source("purchase_line"), where),
        evidence=EVIDENCE,
    )


def _cart_items(where: event_ir.Condition) -> event_ir.Exists:
    return event_ir.Exists(
        relation=event_ir.Filter(event_ir.Source("cart"), where),
        evidence=EVIDENCE,
    )


def _compile(expression: event_ir.Condition) -> str:
    catalog = audience_runtime.resolve_audience_catalog()
    return event_compiler.compile_expression(
        expression,
        context=catalog.compile_context(literals=True),
    ).sql


def _compile_bound(expression: event_ir.Condition) -> event_compiler.CompiledCondition:
    catalog = audience_runtime.resolve_audience_catalog()
    return event_compiler.compile_expression(
        expression,
        context=catalog.compile_context(literals=False),
    )


def test_product_text_uses_product_master_name_and_category_like_search() -> None:
    sql = _compile(_purchases(_match("사료")))

    assert (
        "CRM_SL_ORDERDETAILMALL OD LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT "
        "ON OD.PRODUCT_ID = OD_PRODUCT.PRODUCT_ID"
    ) in sql
    for column in (
        "PRODUCT_NAME",
        "CATEGORY",
        "CATEGORYL_NAME",
        "CATEGORYM_NAME",
        "CATEGORYS_NAME",
    ):
        assert f"OD_PRODUCT.{column} LIKE N'%사료%' ESCAPE N'~'" in sql
    assert "OD.PRODUCT_ID = '사료'" not in sql


@pytest.mark.parametrize(
    ("field", "relation"),
    [
        ("purchase_line.product_id", _purchases),
        ("cart.product_id", _cart_items),
    ],
)
@pytest.mark.parametrize("natural_name", ["사료", "DOGFOOD"])
def test_product_id_rejects_korean_and_ascii_natural_names(
    field: str,
    relation: Callable[[event_ir.Condition], event_ir.Exists],
    natural_name: str,
) -> None:
    with pytest.raises(event_compiler.SqlCompileError, match="식별자 형식"):
        _compile(relation(_match(natural_name, field=field)))


@pytest.mark.parametrize(
    ("field", "relation", "column"),
    [
        ("purchase_line.product_id", _purchases, "OD.PRODUCT_ID"),
        ("cart.product_id", _cart_items, "EC.PRODUCT_ID"),
    ],
)
def test_product_id_accepts_an_identifier(
    field: str,
    relation: Callable[[event_ir.Condition], event_ir.Exists],
    column: str,
) -> None:
    sql = _compile(relation(_match("P0001", field=field)))
    assert f"{column} = 'P0001'" in sql
    assert "LIKE" not in sql


@pytest.mark.parametrize(
    ("field", "relation"),
    [
        ("purchase_line.product_id", _purchases),
        ("cart.product_id", _cart_items),
    ],
)
def test_product_id_rejects_non_string_literals_declared_by_catalog(
    field: str,
    relation: Callable[[event_ir.Condition], event_ir.Exists],
) -> None:
    expression = relation(_match(123, field=field))

    with pytest.raises(event_compiler.SqlCompileError, match="식별자 형식"):
        _compile(expression)

    catalog = audience_runtime.resolve_audience_catalog()
    capability = event_compiler.validate_compiler_capability(
        expression,
        context=catalog.compile_context(literals=False),
    )
    assert capability.status == event_compiler.CAPABILITY_UNSUPPORTED


def test_cart_product_text_uses_product_master_without_changing_cart_correlation() -> None:
    sql = _compile(_cart_items(_match("사료", field="cart.product_text")))

    assert (
        "ODS_MALL_OMS_CART EC LEFT JOIN CRM_CM_PRODUCT EC_PRODUCT "
        "ON EC.PRODUCT_ID = EC_PRODUCT.PRODUCT_ID"
    ) in sql
    assert "EC.CART_ID = B.MEMBER_ID" in sql
    assert "EC_PRODUCT.PRODUCT_NAME LIKE N'%사료%' ESCAPE N'~'" in sql
    assert "EC_PRODUCT.CATEGORYS_NAME LIKE N'%사료%' ESCAPE N'~'" in sql


def test_product_like_escapes_quotes_and_pattern_metacharacters() -> None:
    sql = _compile(_purchases(_match("100%_[' OR 1=1 --", field="purchase_line.product_name")))

    assert "LIKE N'%100~%~_~['' OR 1=1 --%' ESCAPE N'~'" in sql
    assert "OD.PRODUCT_ID = '100" not in sql


def test_bound_product_like_reuses_one_escaped_named_parameter() -> None:
    term = "100%_[' OR 1=1 --"
    compiled = _compile_bound(
        _purchases(_match(term, field="purchase_line.product_text"))
    )

    assert term not in compiled.sql
    assert len(compiled.params) == 1
    parameter_name, parameter_value = next(iter(compiled.params.items()))
    placeholder = f":{parameter_name}"
    assert parameter_value == "%100~%~_~[' OR 1=1 --%"
    assert compiled.sql.count(placeholder) == 5
    for column in (
        "PRODUCT_NAME",
        "CATEGORY",
        "CATEGORYL_NAME",
        "CATEGORYM_NAME",
        "CATEGORYS_NAME",
    ):
        assert (
            f"OD_PRODUCT.{column} LIKE {placeholder} ESCAPE N'~'"
            in compiled.sql
        )


def test_query_pipeline_named_style_keeps_product_term_out_of_sql() -> None:
    expression = _purchases(_match("A_B%", field="purchase_line.product_text"))
    catalog = audience_runtime.resolve_audience_catalog()

    compiled = query_pipeline.compile_audience_predicate(
        {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
            "receipts": [],
        },
        compile_context_factory=catalog.compile_context,
        parameter_style=query_pipeline.ParameterStyle.NAMED,
    )

    assert "A_B%" not in compiled.sql
    assert compiled.parameter_map == {"text_search_0": "%A~_B~%%"}
    assert compiled.sql.count(":text_search_0") == 5


def test_all_products_are_independent_exists_not_impossible_same_row_filters() -> None:
    expression = event_ir.And(
        tuple(_purchases(_match(term)) for term in ("사료", "간식", "장난감"))
    )
    sql = _compile(expression)

    assert sql.count("EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL") == 3
    assert sql.count("LEFT JOIN CRM_CM_PRODUCT") == 3
    assert ") AND (EXISTS" in sql


def test_all_products_rejects_impossible_same_row_and_shape() -> None:
    expression = _purchases(
        event_ir.And(tuple(_match(term) for term in ("사료", "간식", "장난감")))
    )

    with pytest.raises(event_compiler.SqlCompileError, match="독립된 Exists"):
        _compile(expression)

    catalog = audience_runtime.resolve_audience_catalog()
    capability = event_compiler.validate_compiler_capability(
        expression, context=catalog.compile_context()
    )
    assert capability.status == event_compiler.CAPABILITY_UNSUPPORTED


def test_any_product_can_share_one_exists_with_or_search_terms() -> None:
    expression = _purchases(
        event_ir.Or(tuple(_match(term) for term in ("사료", "간식", "장난감")))
    )
    sql = _compile(expression)

    assert sql.count("EXISTS (SELECT 1 FROM CRM_SL_ORDERDETAILMALL") == 1
    assert ") OR (" in sql
    for term in ("사료", "간식", "장난감"):
        assert f"LIKE N'%{term}%' ESCAPE N'~'" in sql


def test_same_row_and_preserves_distinct_product_text_fields() -> None:
    expression = _purchases(
        event_ir.And(
            (
                _match("반려견 사료", field="purchase_line.product_name"),
                _match("사료", field="purchase_line.product_category"),
            )
        )
    )

    sql = _compile(expression)
    assert "OD_PRODUCT.PRODUCT_NAME LIKE N'%반려견 사료%'" in sql
    assert "OD_PRODUCT.CATEGORY LIKE N'%사료%'" in sql


def test_absence_other_product_and_combination_keep_distinct_boolean_meanings() -> None:
    feed = _match("사료")
    never_feed = event_ir.Not(_purchases(feed))
    bought_other = _purchases(event_ir.Not(feed))

    never_feed_sql = _compile(never_feed)
    bought_other_sql = _compile(bought_other)
    combined_sql = _compile(event_ir.And((never_feed, bought_other)))

    assert never_feed_sql.startswith("NOT EXISTS (")
    assert bought_other_sql.startswith("EXISTS (")
    assert "OD_PRODUCT.PRODUCT_NAME IS NOT NULL" in bought_other_sql
    assert "OD_PRODUCT.CATEGORYS_NAME IS NOT NULL" in bought_other_sql
    assert "NOT (CASE WHEN" in bought_other_sql
    assert "NOT EXISTS (" in combined_sql
    assert ") AND (EXISTS (" in combined_sql
    assert combined_sql.count("LEFT JOIN CRM_CM_PRODUCT") == 2


def test_compound_other_product_negation_requires_a_searchable_product_row() -> None:
    expression = _purchases(
        event_ir.Not(event_ir.Or((_match("사료"), _match("간식"))))
    )

    sql = _compile(expression)
    assert "OD_PRODUCT.PRODUCT_NAME IS NOT NULL" in sql
    assert "OD_PRODUCT.CATEGORYS_NAME IS NOT NULL" in sql
    assert "AND NOT (CASE WHEN" in sql


def test_product_absence_not_exists_does_not_require_a_searchable_product_row() -> None:
    sql = _compile(event_ir.Not(_purchases(_match("사료"))))

    assert sql.startswith("NOT EXISTS (")
    assert "OD_PRODUCT.PRODUCT_NAME IS NOT NULL" not in sql
