"""`semantic_ir` literal scanner의 현행 표면·좌표·결정성 계약."""

from __future__ import annotations

import json

import pytest

from query_structurer.semantic_ir import extract_literal_bindings


FIXED_DATE = "2026-08-04"
GOLDEN_QUERY = "2019년에 200,000원 이상 구매하고 상위 10.5%이며 5건 주문, 최근 2개월"


def test_literal_kinds_values_and_exact_spans_are_characterized() -> None:
    bindings = extract_literal_bindings(GOLDEN_QUERY, current_date=FIXED_DATE)

    assert [item["kind"] for item in bindings] == [
        "date_window",
        "money",
        "comparison_operator",
        "percentage",
        "number_with_unit",
        "duration",
    ]
    assert [item["text"] for item in bindings] == [
        "2019년",
        "200,000원",
        "이상",
        "10.5%",
        "5건",
        "2개월",
    ]
    assert [GOLDEN_QUERY[item["start"] : item["end"]] for item in bindings] == [
        item["text"] for item in bindings
    ]
    assert [item["normalized"] for item in bindings] == [
        {
            "from": "20190101",
            "to": "20191231",
            "label": "2019년",
            "event_ir_window": {
                "type": "interval",
                "start": "2019-01-01",
                "end_exclusive": "2020-01-01",
            },
        },
        {"amount": 200000, "currency": "KRW"},
        ">=",
        {"value": "10.5", "unit": "percent"},
        {"value": 5, "surface_unit": "건", "unit": "count"},
        {
            "value": 2,
            "surface_unit": "개월",
            "semantic_unit": "months",
            "temporal_kind": "rolling_duration",
        },
    ]

    spans = [(item["start"], item["end"]) for item in bindings]
    assert all(
        left_end <= right_start
        for (_, left_end), (right_start, _) in zip(spans, spans[1:])
    )


def test_same_input_and_context_are_byte_for_byte_deterministic() -> None:
    def encoded() -> bytes:
        return json.dumps(
            extract_literal_bindings(GOLDEN_QUERY, current_date=FIXED_DATE),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    assert encoded() == encoded()


def test_relative_date_uses_the_injected_reference_date() -> None:
    bindings = extract_literal_bindings("지난달 구매한 고객", current_date=FIXED_DATE)

    assert bindings[0]["kind"] == "date_window"
    assert bindings[0]["normalized"]["from"] == "20260701"
    assert bindings[0]["normalized"]["to"] == "20260731"


@pytest.mark.parametrize("current_date", [None, "not-an-iso-date"])
def test_relative_date_never_falls_back_to_the_system_date(current_date: str | None) -> None:
    bindings = extract_literal_bindings("지난달 구매한 고객", current_date=current_date)

    assert all(item["kind"] != "date_window" for item in bindings)


def test_absolute_date_remains_available_without_a_reference_date() -> None:
    bindings = extract_literal_bindings("2019년 구매한 고객")

    window = next(item for item in bindings if item["kind"] == "date_window")
    assert window["normalized"]["from"] == "20190101"
    assert window["normalized"]["to"] == "20191231"


def test_fractional_percentage_preserves_exact_json_round_trip() -> None:
    literal = extract_literal_bindings("상위 10.123456789012345678901% 고객")[0]

    assert literal["kind"] == "percentage"
    assert literal["value"] == "10.123456789012345678901"
    assert literal["normalized"] == {
        "value": "10.123456789012345678901",
        "unit": "percent",
    }
    assert json.loads(json.dumps(literal, ensure_ascii=False)) == literal


def test_fractional_generic_number_never_rounds_through_float() -> None:
    exact = "12345678901234567890.1234567890123456789"
    literal = extract_literal_bindings(f"점수 {exact} 기준")[-1]

    assert literal["kind"] == "number"
    assert literal["value"] == exact
    assert literal["normalized"] == exact
    assert not isinstance(literal["value"], float)
