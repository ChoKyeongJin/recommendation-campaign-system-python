"""기간 리터럴이 **창의 wire 모양까지** 싣는다 — 모델이 값·단위를 옮겨 적지 않는다.

배경(실측 2026-08-07)
---------------------
구조화 안내는 절대 구간에 대해서만 "binding 의 ``normalized.event_ir_window`` 를 그대로
복사하라"고 말하고, rolling/relative 기간에 대해서는 "binding 의 값·단위를 사용"하라고 말했다.
그런데 이 추출기의 단위 표기는 복수형(``days``)이고 구조화 툴 스키마의 enum 은 단수형
(``day|week|month|year``)이다. 지시를 지키는 모델은 반드시 스키마를 어긴다 — 실측 로그에
모델이 그 ``days`` 를 그대로 복사해 교정 라운드가 실패한 응답이 있다.

같은 결함의 첫 인스턴스는 교정 지시문 쪽이었고(:mod:`targeting_policy`, 그쪽 테스트가 고정),
여기가 두 번째 인스턴스다. 고치는 방법은 모델에게 **옮겨 적을 값을 주지 않는 것**이다:
바인딩이 창 객체를 통째로 싣고, 안내는 그것을 복사하라고만 말한다.

실행: python -m pytest tests/test_duration_binding_wire_window.py -q
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

import audience_runtime
import audience_schema
import event_ir
import temporal_clause
from query_structurer.semantic_ir import extract_literal_bindings

REFERENCE_DATE = "2026-08-07"

# 스키마는 손으로 베끼지 않는다 — 검사와 계약이 갈라질 자리를 만들지 않는다.
_SCHEMA = audience_schema.audience_expression_json_schema()
_WINDOW_SCHEMA: dict[str, Any] = {"$ref": "#/$defs/window", "$defs": _SCHEMA["$defs"]}


def _durations(query: str) -> list[dict[str, Any]]:
    return [
        binding
        for binding in extract_literal_bindings(query, current_date=REFERENCE_DATE)
        if binding.get("kind") == "duration"
    ]


def _only_duration(query: str) -> dict[str, Any]:
    rows = _durations(query)
    assert len(rows) == 1, f"기간 리터럴이 하나가 아니다: {query} -> {rows}"
    return rows[0]


ROLLING_QUERIES = (
    ("최근 30일 구매한 회원", 30, "day"),
    ("최근 2주 동안 구매한 회원", 2, "week"),
    ("최근 3개월 동안 구매한 회원", 3, "month"),
    ("최근 3개월간 구매한 회원", 3, "month"),
    ("최근 1년 동안 구매한 회원", 1, "year"),
    ("최근 일주일 구매한 회원", 7, "day"),
    ("최근 반년 구매한 회원", 6, "month"),
)


@pytest.mark.parametrize(("query", "value", "unit"), ROLLING_QUERIES)
def test_a_rolling_duration_carries_its_own_schema_valid_window(
    query: str, value: int, unit: str
) -> None:
    window = _only_duration(query)["normalized"]["event_ir_window"]

    assert window == {"type": "rolling", "value": value, "unit": unit}
    jsonschema.validate(window, _WINDOW_SCHEMA)


def test_a_past_point_duration_carries_a_relative_window() -> None:
    """'3개월 전'은 길이가 아니라 **그 시점이 속한 달력 칸**이다 — 종류도 애플리케이션이 정한다."""
    window = _only_duration("3개월 전에 가입한 회원")["normalized"]["event_ir_window"]

    assert window == {"type": "relative", "value": 3, "unit": "month", "direction": "past"}
    jsonschema.validate(window, _WINDOW_SCHEMA)


@pytest.mark.parametrize(
    "query",
    [
        "최근 30일 구매한 회원",
        "최근 2주 동안 구매한 회원",
        "최근 3개월간 구매한 회원",
        "최근 일주일 구매한 회원",
        "3개월 전에 가입한 회원",
        "구매주기가 30일 이하인 회원",
    ],
)
def test_no_duration_binding_ever_shows_a_unit_outside_the_schema_enum(query: str) -> None:
    """모델이 보는 창의 단위는 **언제나** 스키마 enum 안에 있다(복수형 표기가 새지 않는다)."""
    for binding in _durations(query):
        window = binding["normalized"].get("event_ir_window")
        assert window is not None, binding
        assert window["unit"] in event_ir.WINDOW_UNITS, window
        # 내부 표기는 그대로 남는다 — 하류가 이미 읽고 있고, 복수/단수는 서로 다른 자리의 값이다.
        assert binding["normalized"]["semantic_unit"] not in event_ir.WINDOW_UNITS


def test_an_inexpressible_period_gets_no_window_instead_of_an_invented_one() -> None:
    """'24시간'은 IR 창 단위가 아니다. 지어낸 창보다 창 없음이 낫다(CLAUDE.md §11·§12)."""
    assert _durations("최근 24시간 이내 주문한 회원") == []


@pytest.mark.parametrize(
    "query",
    [
        "향후 7일 안에 구매예정일이 도래하는 회원",
        "앞으로 3개월 안에 만료되는 쿠폰을 가진 회원",
        "향후 일주일 안에 만료되는 쿠폰을 가진 회원",
    ],
)
def test_a_future_period_gets_no_window_because_the_ir_only_looks_backwards(
    query: str,
) -> None:
    """미래 기간은 창으로 옮기면 **방향이 뒤집힌다** — 그 자리에 과거 창을 놓지 않는다.

    IR 의 창은 둘 다 과거를 본다(rolling = 거슬러 센 길이, relative = 지나간 달력 칸).
    '향후 7일'을 rolling 7 day 로 제시하면 애플리케이션이 스스로 '최근 7일'을 요구하게 된다.
    """
    durations = _durations(query)

    assert durations, query
    for binding in durations:
        assert "event_ir_window" not in binding["normalized"], binding
        # 리터럴 자체는 보존된다 — 조용히 지우지 않는다(CLAUDE.md §11).
        assert binding["normalized"]["value"] > 0


def test_the_window_round_trips_through_the_ir_reader() -> None:
    """바인딩이 싣는 창은 IR 이 그대로 읽는 값이다(모델을 거치지 않아도 성립한다)."""
    for query, _value, _unit in ROLLING_QUERIES:
        window = _only_duration(query)["normalized"]["event_ir_window"]
        assert event_ir.window_from_dict(window).to_dict() == window


@pytest.mark.parametrize(("query", "_value", "_unit"), ROLLING_QUERIES)
def test_the_binding_window_and_the_clause_window_agree(
    query: str, _value: int, _unit: str
) -> None:
    """창의 wire 모양을 말하는 두 자리가 같은 값을 말한다.

    바인딩(구조화 안내가 복사시키는 값)과 시간 절(:mod:`targeting_policy` 교정 지시문이
    제시하는 값)이 어긋나면, 같은 문장에 대해 두 라운드가 서로 다른 창을 요구하게 된다.
    """
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    clauses = [
        clause
        for clause in temporal_clause.combine_temporal_clauses(query, bindings)
        if clause.is_quantified and clause.marker_bound
    ]

    assert len(clauses) == 1, clauses
    assert clauses[0].wire_window == _only_duration(query)["normalized"]["event_ir_window"]


def test_the_structuring_guidance_asks_for_a_copy_and_never_for_the_internal_unit() -> None:
    """안내가 '값·단위를 옮겨 적어라'로 되돌아가면 같은 결함이 재발한다."""
    guidance = audience_runtime.audience_catalog_guidance()
    shape_lines = [
        line for line in guidance.splitlines() if line.startswith("- ") and "window" in line
    ]

    assert shape_lines, guidance
    time_filter_line = next(line for line in shape_lines if line.startswith("- TimeFilter:"))
    assert "event_ir_window" in time_filter_line
    # 값·단위를 옮겨 적으라는 지시가 남아 있으면 같은 결함이 재발한다.
    assert "값·단위를 사용" not in guidance
    for line in shape_lines:
        assert "semantic_unit" not in line or "조립하지 않는다" in line
