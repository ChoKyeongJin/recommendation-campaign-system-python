from __future__ import annotations

import json
from decimal import Decimal

import pytest

from semantic_normalizers import AmountNormalizer, Money
from query_structurer.semantic_ir import (
    bind_counter_literals,
    extract_literal_bindings,
    scan_literal_bindings,
)


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
        "unit": "count",
    }


def test_counter_scanner_does_not_guess_a_business_metric() -> None:
    scanned = scan_literal_bindings("최근 3회 활동한 회원", current_date="2026-08-04")
    counter = next(item for item in scanned if item["kind"] == "number_with_unit")

    assert counter["normalized"] == {
        "value": 3,
        "surface_unit": "회",
        "unit": "count",
    }
    assert "order_count" not in repr(scanned)


def test_counter_binder_uses_injected_domain_context() -> None:
    scanned = scan_literal_bindings("최근 3회 로그인한 회원", current_date="2026-08-04")

    bound = bind_counter_literals(scanned, counter_units={"회": "login_count"})
    counter = next(item for item in bound if item["kind"] == "number_with_unit")
    assert counter["normalized"] == {
        "value": 3,
        "surface_unit": "회",
        "semantic_unit": "login_count",
    }
    original = next(item for item in scanned if item["kind"] == "number_with_unit")
    assert original["normalized"]["unit"] == "count"


@pytest.mark.parametrize(
    ("query", "expected_unit"),
    [
        ("최근 3회 로그인한 회원", "login_count"),
        ("최근 주문 3회 이상인 회원", "order_count"),
        ("캠페인 메시지를 3회 발송한 회원", "campaign_contact_count"),
        ("최근 3회 활동한 회원", None),
    ],
)
def test_default_counter_binding_requires_nearby_domain_evidence(
    query: str,
    expected_unit: str | None,
) -> None:
    scanned = scan_literal_bindings(query, current_date="2026-08-04")
    counter = next(
        item
        for item in bind_counter_literals(scanned, query=query)
        if item["kind"] == "number_with_unit"
    )

    if expected_unit is None:
        assert counter["normalized"] == {
            "value": 3,
            "surface_unit": "회",
            "unit": "count",
        }
    else:
        assert counter["normalized"]["semantic_unit"] == expected_unit
        assert "unit" not in counter["normalized"]


def test_fractional_money_is_exact_internally_and_json_round_trips_without_float() -> None:
    money = AmountNormalizer.normalize("12,345.67원")

    assert money == Money(Decimal("12345.67"), "KRW")
    assert money.amount == Decimal("12345.67")
    payload = money.to_dict()
    assert payload == {"amount": "12345.67", "currency": "KRW"}
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_fractional_money_literal_uses_the_compatible_exact_json_projection() -> None:
    binding = extract_literal_bindings("12,345.67원 이상 구매한 고객")[0]

    assert binding["kind"] == "money"
    assert binding["value"] == "12345.67"
    assert binding["normalized"] == {"amount": "12345.67", "currency": "KRW"}


def test_money_exact_json_projection_parses_back_without_precision_loss() -> None:
    original = Money(Decimal("12345.670000000000000000000001"), "KRW")

    restored = AmountNormalizer.normalize(original.to_dict())
    assert restored == original
    assert restored.amount.as_tuple() == original.amount.as_tuple()
