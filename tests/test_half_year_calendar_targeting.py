"""연도 생략/상대 연도 반기·분기 표현이 구매 조건에서 소실되지 않는지 검증한다."""

from datetime import date

import pytest

import calendar_window as cw
import graph_rag as g


TODAY = date(2026, 7, 28)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("상반기에 구매한 고객", ("20260101", "20260630")),
        ("하반기에 구매한 고객", ("20260701", "20261231")),
        ("올해 상반기에 구매한 고객", ("20260101", "20260630")),
        ("작년 하반기에 구매한 고객", ("20250701", "20251231")),
        ("지난해 2분기에 구매한 고객", ("20250401", "20250630")),
        ("3분기에 구매한 고객", ("20260701", "20260930")),
    ],
)
def test_half_and_quarter_windows_infer_relative_or_current_year(text, expected):
    window = cw.parse_calendar_window(text, today=TODAY)
    assert window is not None
    assert (window["from"], window["to"]) == expected


def test_relative_year_is_inherited_by_following_half_in_same_enumeration():
    windows = cw.parse_calendar_windows("작년 상반기와 하반기", today=TODAY)
    assert [(window["from"], window["to"]) for window in windows] == [
        ("20250101", "20250630"),
        ("20250701", "20251231"),
    ]


@pytest.mark.parametrize("half", ["상반기", "하반기"])
def test_bare_half_year_reaches_purchase_plan_and_sql(half):
    current_year = date.today().year
    plan = g.build_query_plan(f"{half}에 구매한 고객", parser="rules")
    purchase_date = plan["target_user"].get("purchase_date")

    assert purchase_date is not None
    assert purchase_date["from"].startswith(str(current_year))

    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    assert f"D.ORDER_DATE BETWEEN '{purchase_date['from']}' AND '{purchase_date['to']}'" in candidate["sql"]
    assert "target_user.purchase_date" not in candidate.get("dropped_conditions", [])


def test_generic_half_and_quarter_words_remain_ambiguous():
    assert cw.parse_calendar_window("반기 실적") is None
    assert cw.parse_calendar_window("분기 실적") is None


@pytest.mark.parametrize("text", ["최근 2분기 구매", "지난 2분기 동안 구매", "2분기 연속 구매"])
def test_quarter_duration_is_not_misread_as_calendar_second_quarter(text):
    assert cw.parse_calendar_window(text, today=TODAY) is None
