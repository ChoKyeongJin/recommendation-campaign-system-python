from __future__ import annotations

import pytest

from query_structurer.semantic_ir import extract_literal_bindings


@pytest.mark.parametrize(
    "surface",
    ("이십만원", "이십만 원", "20만원", "200,000원"),
)
def test_money_surfaces_are_normalized_with_exact_evidence_span(surface: str) -> None:
    query = f"2019년에 {surface} 이상을 구매한 고객"

    literals = extract_literal_bindings(query)

    money = [literal for literal in literals if literal["kind"] == "money"]
    assert money == [
        {
            "id": "money_1",
            "kind": "money",
            "text": surface,
            "start": query.index(surface),
            "end": query.index(surface) + len(surface),
            "value": 200000,
            "normalized": {"amount": 200000, "currency": "KRW"},
        }
    ]
    assert query[money[0]["start"]:money[0]["end"]] == surface
    assert set(money[0]) == {"id", "kind", "text", "start", "end", "value", "normalized"}


def test_money_span_is_not_reemitted_as_number_and_does_not_consume_other_literals() -> None:
    query = "2019년에 200,000원 이상 구매하고 5건 주문한 고객"

    literals = extract_literal_bindings(query)

    assert [(literal["kind"], literal["text"]) for literal in literals] == [
        ("date_window", "2019년"),
        ("money", "200,000원"),
        ("comparison_operator", "이상"),
        ("number_with_unit", "5건"),
    ]
    spans = [(literal["start"], literal["end"]) for literal in literals]
    assert all(
        left_end <= right_start or right_end <= left_start
        for index, (left_start, left_end) in enumerate(spans)
        for right_start, right_end in spans[index + 1:]
    )


def test_korean_word_ending_in_won_is_not_a_currency_prefix() -> None:
    literals = extract_literal_bindings("지원 20만 명을 선정해")

    assert all(literal["kind"] != "money" for literal in literals)


def test_counter_literal_keeps_particle_outside_exact_span() -> None:
    query = "상위 상품 10개를 구매한 고객"

    bindings = extract_literal_bindings(query)

    counter = next(item for item in bindings if item["kind"] == "number_with_unit")
    assert counter["text"] == "10개"
    assert query[counter["start"]:counter["end"]] == "10개"
    assert counter["normalized"] == {
        "value": 10,
        "surface_unit": "개",
        "semantic_unit": "item_quantity",
    }
