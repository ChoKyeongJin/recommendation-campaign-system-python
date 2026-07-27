"""집합식 엔진만으로 실행 가능한 한국어 제외 활용형 회귀 테스트."""

from set_expression_engine import parse_set_expressions_from_query


def test_attributive_exclusion_does_not_leave_unknown_operand():
    assert parse_set_expressions_from_query("남성을 제외한 고객") == []


def test_aggregate_cohort_difference_consumes_jeoehan_ending():
    expressions = parse_set_expressions_from_query("2019년에 20만원 이상 구매한 고객 중 남성을 제외한 고객")
    assert len(expressions) == 1
    expression = expressions[0]
    assert expression["detection"] == "natural"
    assert expression["set_ast"]["op"] == "-"
    assert expression["set_ast"]["right"]["canonical"] == "male"
    assert expression["set_ast"]["right"].get("text") != "한 고객"


def test_rewrite_style_jeoehae_ending_is_consumed():
    expressions = parse_set_expressions_from_query("2019년에 이십만원 이상을 구매한 고객에서 남자는 제외해.")
    assert len(expressions) == 1
    assert expressions[0]["detection"] == "natural"
    assert expressions[0]["set_ast"]["right"]["canonical"] == "male"
    assert "제외해" not in (expressions[0]["clarification_question"] or "")
