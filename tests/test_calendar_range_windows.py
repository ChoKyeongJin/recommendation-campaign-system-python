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
        # 연도 생략 일자('M월 D일')는 앞선 명시 연도를 상속한다 — 연도 생략 월('3월')의 일 단위 대칭.
        # 이 토큰이 없던 동안 '7월 31일'이 '7월'(월 전체)로 읽혀 범위가 접히지 않았고, '부터 7월
        # 31일까지'가 주인 없는 표현으로 남아 확인 질문으로 막혔다.
        ("2026년 7월 1일부터 7월 31일까지 주문한 회원", ("20260701", "20260731")),
        ("2019년 1월 5일부터 3월 10일까지 구매한 사람", ("20190105", "20190310")),
        ("2026년 6월 30일부터 7월 2일까지 주문한 회원", ("20260630", "20260702")),
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


def test_day_range_source_span_covers_the_whole_phrase() -> None:
    """연도 상속 일자 범위도 표현 전체가 한 창의 출처다 — 남는 조각이 있으면 커버리지가 막는다."""
    text = "2026년 7월 1일부터 7월 31일까지 주문한 회원"
    span = calendar_window.parse_calendar_window_span(text)

    assert text[span[0]:span[1]] == "2026년 7월 1일부터 7월 31일까지"


def test_day_tokens_without_any_year_stay_unparsed() -> None:
    """연도 필수 — 어느 해의 7월인지 단정할 수 없으면 창을 만들지 않는다(fail-close)."""
    assert _ranges("7월 1일부터 7월 31일까지 주문한 회원") == []


# ── 시각 한정자: 일 단위 창의 시각 경계(from_time/to_time) ──────────────────────────
def _time_windows(text: str) -> list[tuple[str, str, str | None, str | None]]:
    return [
        (w["from"], w["to"], w.get("from_time"), w.get("to_time"))
        for w in calendar_window.parse_calendar_windows(text)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 시각 토큰은 그 단위 전체 구간이다 — '18시까지'는 18:59:59까지('7월까지'가 7월 말일까지인 것과 같다).
        ("2026년 7월 1일 9시부터 7월 31일 18시까지 주문", ("20260701", "20260731", "090000", "185959")),
        ("2026년 7월 1일부터 7월 31일 오후 6시 30분까지 주문", ("20260701", "20260731", None, "183059")),
        ("2026년 7월 1일 오전 9시부터 7월 31일까지 주문", ("20260701", "20260731", "090000", None)),
        # 하루 안의 시각 구간과 단일 시각(그 시간대 전체).
        ("2026년 7월 1일 9시부터 7월 1일 18시까지 주문", ("20260701", "20260701", "090000", "185959")),
        ("2026년 7월 1일 9시에 주문", ("20260701", "20260701", "090000", "095959")),
        ("2026년 7월 1일 오후 3시에 주문", ("20260701", "20260701", "150000", "155959")),
    ],
)
def test_time_of_day_bounds_ride_on_day_windows(text: str, expected: tuple) -> None:
    assert _time_windows(text) == [expected]


def test_time_of_day_never_appears_on_date_only_windows() -> None:
    """시각 없는 창은 기존 {from,to,label} shape 그대로다 — 시각을 모르는 소비자·스냅샷 호환."""
    (window,) = calendar_window.parse_calendar_windows("2026년 7월 1일부터 7월 31일까지 주문")
    assert "from_time" not in window and "to_time" not in window


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("2026년 7월 1일 25시부터 7월 2일까지 주문", "불가능한 시각은 창 전체를 미해석으로(시각만 조용히 버리지 않는다)"),
        ("2026년 7월 1일 18시부터 7월 1일 9시까지 주문", "같은 날 시각 역전은 범위가 아니다"),
    ],
)
def test_invalid_or_reversed_times_fail_close(text: str, reason: str) -> None:
    folded = _time_windows(text)
    assert len(folded) != 1 or folded[0][0] != folded[0][1] or folded[0][2] is None, reason


def test_duration_hours_are_not_read_as_time_of_day() -> None:
    """'3시간'은 기간이지 시각이 아니다 — 시각으로 오인하면 기간 표현이 반쪽 남는다."""
    assert _time_windows("2026년 7월 1일부터 3시간 이내 구매") == [("20260701", "20260701", None, None)]


def test_time_slot_compiles_time_bounds_into_order_time_predicate() -> None:
    """시각 창 → 술어: 날짜 BETWEEN(색인 활용) 위에 경계일 시각 조건을 얹는다. 시각 컬럼은 주문
    헤더에만 있으므로 상세(D) 기반 쿼리는 헤더 상관 EXISTS, 헤더 직접 쿼리는 인라인 비교다."""
    slot = graph_rag._parse_purchase_date_period("2026년 7월 1일 9시부터 7월 31일 18시까지 주문한 회원")

    assert (slot["from"], slot["to"]) == ("20260701", "20260731")
    assert (slot["from_time"], slot["to_time"]) == ("090000", "185959")

    detail = graph_rag._purchase_date_predicate(slot)
    assert "D.ORDER_DATE BETWEEN '20260701' AND '20260731'" in detail
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL OT WHERE OT.ORDER_ID = D.ORDER_ID" in detail
    assert "OT.ORDER_TIME >= '090000'" in detail and "OT.ORDER_TIME <= '185959'" in detail

    header = graph_rag._purchase_date_predicate(slot, alias=None, source_table="CRM_SL_ORDERHEADERMALL")
    assert "ORDER_TIME >= '090000'" in header and "EXISTS" not in header


def test_time_slot_without_expressible_column_fails_close() -> None:
    """시각 컬럼이 없는 문맥(별칭 없는 비주문 테이블)이면 술어 전체를 만들지 않는다 — 날짜만 걸어
    조건을 조용히 넓히지 않기 위함이다. 출고는 결정론 불변식(time_window_dropped)이 막는다."""
    slot = graph_rag._parse_purchase_date_period("2026년 7월 1일 9시부터 7월 31일 18시까지 주문한 회원")

    assert graph_rag._purchase_date_predicate(slot, alias=None, source_table="CRM_MB_MONTHCRMINFO") is None
    verdict = graph_rag._verify_sql_semantic_invariants(
        "q", {"target_user": {"purchase_date": slot}}, "SELECT 1 FROM T WHERE ORDER_DATE = '20260701'", [],
    )
    assert any(issue["type"] == "time_window_dropped" for issue in verdict["issues"])


def test_date_only_predicates_are_unchanged_by_the_time_layer() -> None:
    """시각이 없으면 술어는 기존 그대로다(BETWEEN 하나·인접 병합) — 시각 층의 무영향 회귀 가드."""
    slot = graph_rag._parse_purchase_date_period("2018, 2019년에 기저귀를 구매한 사람")
    assert graph_rag._purchase_date_predicate(slot) == "D.ORDER_DATE BETWEEN '20180101' AND '20191231'"
