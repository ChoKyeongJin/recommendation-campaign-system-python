"""기간 범위 문법 — '…부터 …까지'는 두 창이 아니라 하나의 연속 구간이다.

이 파일이 못 박는 것은 링크의 **뜻**이다. 창이 이어져 나오는 표현에는 종류가 둘 있다.

    나열: '2019년과 2020년'      → 두 구간의 합집합(OR)
    범위: '2019년 3월부터 5월까지' → 시작과 끝만 준 하나의 연속 구간(BETWEEN)

구분이 없던 동안 두 가지 결함이 같은 뿌리에서 나왔다. '2019년 3월~5월'은 3월 OR 5월로 컴파일돼 **4월이
조용히 빠진** SQL 이 그대로 출고됐고, '2019년부터 2020년까지'는 뒤쪽 창이 어느 슬롯에도 안 잡혀
'원문 조건 해석' 단계에서 확인 질문으로 막혔다(fail-close 는 정상 동작이었고 원인은 문법 미지원).

경계를 단정할 수 없는 표현은 **여전히 막는다** — 접어서 틀린 구간을 거는 것은 드롭보다 나쁘다.
관련: :mod:`calendar_window` (기간 문법의 단일 소유자).
"""

from __future__ import annotations

import pytest

import calendar_window
import graph_rag


def _ranges(text: str) -> list[tuple[str, str]]:
    return [(window["from"], window["to"]) for window in calendar_window.parse_calendar_windows(text)]


# ── 범위: 하나의 연속 구간으로 접힌다 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2019년부터 2020년까지 구매한 사람", ("20190101", "20201231")),
        ("2019년 3월~5월에 구매한 사람", ("20190301", "20190531")),
        ("2019년 3월-5월에 구매한 사람", ("20190301", "20190531")),
        ("2019년 3월부터 2020년 5월까지 구매한 사람", ("20190301", "20200531")),
        ("2019년 3월부터 5월까지 구매한 사람", ("20190301", "20190531")),
        ("2019년 3월부터 5월 사이에 구매한 사람", ("20190301", "20190531")),
        ("2019년 3월에서 5월까지 구매한 사람", ("20190301", "20190531")),
        # 나열형 베어 연도는 구분자를 토큰 안에서 삼킨다 — 그래도 범위로 읽혀야 가운데 해가 안 빠진다.
        ("2019~2021년 구매한 사람", ("20190101", "20211231")),
        ("2019년 1분기부터 3분기까지 구매한 사람", ("20190101", "20190930")),
        ("2019년 1월 5일부터 2020년 3월 10일까지 구매한 사람", ("20190105", "20200310")),
    ],
)
def test_range_link_folds_into_one_continuous_window(text: str, expected: tuple[str, str]) -> None:
    assert _ranges(text) == [expected]


def test_range_covers_the_months_between_its_ends() -> None:
    """범위의 핵심 — 가운데가 빠지지 않는다. '3월~5월'을 나열로 읽으면 4월이 사라진다."""
    (start, end), = _ranges("2019년 3월~5월에 구매한 사람")
    assert start <= "20190415" <= end


# ── 나열: 예전 그대로 두 구간의 합집합 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "2019년과 2020년에 구매한 사람",
        "2019년 및 2020년에 구매한 사람",
        "2019년, 2020년에 구매한 사람",
        "2019년 또는 2020년에 구매한 사람",
    ],
)
def test_enum_link_keeps_two_windows(text: str) -> None:
    assert _ranges(text) == [("20190101", "20191231"), ("20200101", "20201231")]


def test_enum_of_months_stays_disjoint() -> None:
    """나열은 합치지 않는다 — '3월, 5월'은 3월 OR 5월이고 4월은 포함하지 않는다."""
    assert _ranges("2019년 3월, 5월에 구매한 사람") == [
        ("20190301", "20190331"), ("20190501", "20190531"),
    ]


# ── fail-close: 경계를 단정할 수 없으면 접지 않는다 ─────────────────────────────────
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("2020년 5월부터 2019년 3월까지 구매한 사람", "역전 범위(from > to)"),
        ("2019년부터 3월까지 구매한 사람", "구체성 불일치(연 시작 + 월 끝은 추측)"),
        ("2019년부터 2020년 구매한 사람", "닫는 말이 없는 반쪽 경계"),
        ("2018년에 구매하고 2019년에 로그인한 회원", "링크가 아니라 서로 다른 조건의 창"),
        # 토큰 문법에 '연도 생략 M월 D일'이 없어 오른쪽이 '3월'(월 전체)로 읽힌다 → 구체성 불일치로
        # 접히지 않는다. 지금은 확인 질문으로 막히는 것이 맞다(틀린 구간을 거는 것보다 낫다).
        ("2019년 1월 5일부터 3월 10일까지 구매한 사람", "연도 생략 일자 토큰 미지원(알려진 한계)"),
    ],
)
def test_ambiguous_or_reversed_boundaries_are_not_folded(text: str, reason: str) -> None:
    assert len(_ranges(text)) == 2, reason


def test_unfoldable_range_is_reported_as_unresolved_not_silently_ored() -> None:
    """접지 못한 범위는 나열로 강등되지 않고 미해석으로 고지된다(조용한 드롭 금지)."""
    query = "2020년 5월부터 2019년 3월까지 기저귀를 구매한 사람"
    slot = graph_rag._parse_purchase_date_period(query)

    assert "windows" not in slot, "역전 범위를 두 구간 합집합으로 읽으면 안 된다"
    assert graph_rag._deterministic_dropped_conditions(query, {"target_user": {"purchase_date": slot}})


# ── 소비자(구매일 슬롯·드롭 고지)까지 한 뜻으로 도달하는지 ──────────────────────────
def test_purchase_date_slot_compiles_a_range_to_one_between() -> None:
    query = "2019년부터 2020년까지 기저귀를 구매한 사람을 추출해줘"
    slot = graph_rag._parse_purchase_date_period(query)

    assert (slot["from"], slot["to"]) == ("20190101", "20201231")
    assert "windows" not in slot, "범위는 구간 목록이 아니라 하나의 창이다"
    assert graph_rag._purchase_date_predicate(slot) == "D.ORDER_DATE BETWEEN '20190101' AND '20201231'"
    # 범위 전체가 슬롯에 담겼으므로 주인 없는 창이 남지 않는다(확인 질문으로 막히던 원인).
    assert graph_rag._deterministic_dropped_conditions(query, {"target_user": {"purchase_date": slot}}) == []


def test_enum_slot_still_compiles_to_or_of_windows() -> None:
    slot = graph_rag._parse_purchase_date_period("2019년 3월과 5월에 기저귀를 구매한 사람")

    assert [(window["from"], window["to"]) for window in slot["windows"]] == [
        ("20190301", "20190331"), ("20190501", "20190531"),
    ]
    assert graph_rag._purchase_date_predicate(slot) == (
        "(D.ORDER_DATE BETWEEN '20190301' AND '20190331'"
        " OR D.ORDER_DATE BETWEEN '20190501' AND '20190531')"
    )


def test_range_source_span_includes_the_closing_word() -> None:
    """창의 출처 구간은 닫는 말까지다 — 반쪽만 덮으면 남은 '까지'를 다른 슬롯이 다시 주워 간다."""
    text = "2019년 3월부터 5월까지 구매한 사람"
    span = calendar_window.parse_calendar_window_span(text)

    assert text[span[0]:span[1]] == "2019년 3월부터 5월까지"
