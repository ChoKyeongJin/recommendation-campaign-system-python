"""시각 의미론의 계약 — 문법 → 슬롯 → SQL 이 **한 정책**을 쓰는지 고정한다.

이 영역에는 두 종류의 조용한 오답이 있었고 둘 다 '경고 없이 더 넓거나 더 좁은 집합'이었다.

1. **초를 읽지 못했다.** '2026년 7월 1일 23시 59분 59초까지'에서 파서는 '23시 59분'까지만 읽었다.
   끝이 우연히 235959 로 맞아 보였을 뿐 초는 해석된 적이 없고, '30초'였다면 그대로 틀렸다.
2. **경계 낱말을 읽지 못했다.** 같은 문장의 '까지'가 경계가 아니라 **칸**으로 읽혀, '그 시각까지
   주문한 회원'이 '그날 그 1분에 주문한 회원'이 됐다 — 요청 집합의 아주 작은 부분집합이다.

그리고 날짜 없는 시각 범위('밤 11시부터 다음 날 새벽 2시 사이')는 아예 창이 만들어지지 않아
시각 조건이 통째로 사라졌다. 여기서 재는 것은 그 셋과, 그것들이 SQL 에서 **AND 와 OR 를 정확히
가르는가**다 — 같은 두 경계로 정반대 오답(항상 거짓 / 하루 전체)을 만들 수 있는 자리다.

정책(docs/architecture/temporal_semantic_framework.md 의 '시각 경계' 절과 같은 선언):

* 시각 토큰은 **말한 정밀도의 단위 전체 구간**이다('9시'=09:00:00~09:59:59, '…59초'=그 1초).
* 내부 구간은 반개구간 ``[start, end_exclusive)`` 이고, 초 해상도 문자열 비교에서
  ``<= '185959'`` 는 ``< '190000'`` 과 같은 뜻이다 — 두 표기가 같은 집합을 가리킨다.
* 'X까지/X 이전'은 시작이 열린 구간, 'X 이후/X부터'는 끝이 열린 구간이다.
* 주 경계는 월요일 00:00 부터 다음 월요일 00:00 전까지다.
"""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

import calendar_window as cw
import event_compiler
import event_ir
import graph_rag
import targeting_ir

# 기준일은 고정한다(§44). 이 날은 수요일이라 주 경계 계산이 자명하지 않다 — 그래서 골랐다.
FIXED_TODAY = date(2026, 8, 5)

_HEADER = graph_rag._purchase_product_registry()["order_header"]
_DETAIL = graph_rag._purchase_product_registry()["order_detail"]


def _windows(text: str) -> list[dict]:
    return cw.parse_calendar_windows(text, today=FIXED_TODAY)


def _only_window(text: str) -> dict:
    windows = _windows(text)
    assert len(windows) == 1, f"창이 {len(windows)}개다: {windows}"
    return windows[0]


def _header_sql(slot: dict | None) -> str | None:
    """주문 헤더 문맥(시각 컬럼 보유)에서의 술어."""
    return graph_rag._purchase_date_predicate(
        slot, alias="O", column=_HEADER["date_column"], source_table=_HEADER["table"]
    )


def _slot(raw: dict) -> dict | None:
    return targeting_ir._coerce_purchase_date(raw)


# ── 생성된 술어를 실제로 평가하는 작은 계산기 ────────────────────────────────────────
# 경계값 테스트가 파이썬으로 같은 논리를 **다시 적으면** 그것은 테스트가 아니라 복사본이다. 술어의
# AND/OR 가 뒤집혀도 복사본은 함께 뒤집히지 않으므로 초록이 유지된다. 그래서 여기서는 컴파일러가
# 실제로 낸 문자열을 파싱해 행 하나를 넣고 평가한다 — 재는 대상이 출고물 자체가 된다.
#
# 문법은 이 컴파일러가 내는 것만 받는다(eval 금지 · CLAUDE.md §22):
#     expr := term (OR term)* / term := factor (AND factor)*
#     factor := '(' expr ')' | <컬럼> BETWEEN 'a' AND 'b' | <컬럼> (>=|<=|>|<|=) 'v'
_TOKEN_RE = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bBETWEEN\b|>=|<=|>|<|=|'[^']*'|[A-Za-z_][\w.]*")


def _evaluate_predicate(sql: str, row: dict[str, str]) -> bool:
    """술어 문자열 + 행 한 줄 → 참/거짓. 알 수 없는 토큰이 나오면 즉시 실패한다."""
    tokens = _TOKEN_RE.findall(sql)
    assert "".join(tokens) == re.sub(r"\s+", "", sql), f"파싱하지 못한 술어: {sql}"
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def value(token: str) -> str:
        if token.startswith("'"):
            return token[1:-1]
        assert token in row, f"행에 없는 컬럼: {token} (행 {row})"
        return row[token]

    def factor() -> bool:
        if peek() == "(":
            take()
            result = expression()
            assert take() == ")"
            return result
        left = value(take())
        operator = take()
        if operator == "BETWEEN":
            low = value(take())
            assert take() == "AND"
            high = value(take())  # 연쇄 비교로 쓰면 단락 평가가 토큰을 남긴다
            return low <= left <= high
        right = value(take())
        return {
            ">=": left >= right, "<=": left <= right,
            ">": left > right, "<": left < right, "=": left == right,
        }[operator]

    def term() -> bool:
        result = factor()
        while peek() == "AND":
            take()
            result = factor() and result
        return result

    def expression() -> bool:
        result = term()
        while peek() == "OR":
            take()
            result = term() or result
        return result

    outcome = expression()
    assert position == len(tokens), f"남은 토큰: {tokens[position:]}"
    return outcome


# ── 시·분·초 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "bounds"),
    [
        ("2026년 7월 1일 9시", ("090000", "095959")),
        ("2026년 7월 1일 9시 30분", ("093000", "093059")),
        ("2026년 7월 1일 23시 59분 59초", ("235959", "235959")),
        ("2026년 7월 1일 23시 59분 30초", ("235930", "235930")),
        # 분을 건너뛴 초는 정시 기준이다.
        ("2026년 7월 1일 9시 30초", ("090030", "090030")),
        # 한정어는 12시간제를 접는다 — 초까지 말해도 같은 규칙이다.
        ("2026년 7월 1일 오후 6시 30분 15초", ("183015", "183015")),
    ],
)
def test_time_token_covers_the_unit_it_actually_named(text: str, bounds: tuple[str, str]) -> None:
    """말한 정밀도가 곧 구간의 단위다. 초를 말했으면 구간은 그 1초다.

    초를 읽지 못하던 동안 '23시 59분 59초'는 '23시 59분'(=235900~235959)으로 읽혔다. 끝이 같아
    보이는 것은 우연이고, '23시 59분 30초'에서는 그 우연이 깨져 30초 전체가 딸려 들어온다.
    """
    window = _only_window(text)
    assert (window["from_time"], window["to_time"]) == bounds


@pytest.mark.parametrize(
    "text",
    [
        "2026년 7월 1일 25시",           # 시 범위 밖
        "2026년 7월 1일 9시 75분",       # 분 범위 밖
        "2026년 7월 1일 9시 30분 61초",  # 초 범위 밖
        "2026년 7월 1일 밤 2시",         # 한정어가 선언한 범위 밖(어느 날인지 확정 불가)
    ],
)
def test_impossible_clock_values_leave_the_whole_expression_uninterpreted(text: str) -> None:
    """시각만 조용히 버리고 날짜 창을 내면 조건이 '그날 하루 전체'로 넓어진 채 실행된다."""
    assert _windows(text) == []


# ── 방향성 열린 구간 ────────────────────────────────────────────────────────────────


def test_until_opens_the_start_and_keeps_the_named_instant_as_the_end() -> None:
    """'X까지'는 X 라는 칸이 아니라 **끝 경계**다(#69 의 결함).

    경계를 읽지 못하면 '2026-07-01 23:59:59 까지 주문한 회원'이 '그날 그 시각에 주문한 회원'이
    되어 요청 집합의 극히 일부만 나온다 — 0건에 가까운 결과라 오답인 줄 알아채기도 어렵다.
    """
    window = _only_window("2026년 7월 1일 23시 59분 59초까지 주문한 회원을 찾아줘.")
    assert window["from"] == cw.OPEN_WINDOW_MIN_DATE
    assert window["to"] == "20260701"
    assert window["to_time"] == "235959"
    assert window[cw.OPEN_START_KEY] is True
    assert "from_time" not in window  # 열린 쪽에는 시각 경계가 없다


def test_after_opens_the_end_and_keeps_the_named_instant_as_the_start() -> None:
    window = _only_window("2026년 7월 1일 23시 59분 30초 이후 주문한 회원")
    assert (window["from"], window["from_time"]) == ("20260701", "235930")
    assert window["to"] == cw.OPEN_WINDOW_MAX_DATE
    assert window[cw.OPEN_END_KEY] is True
    assert "to_time" not in window


@pytest.mark.parametrize(
    ("text", "opened"),
    [
        ("2026년 7월 1일 이전 주문", cw.OPEN_START_KEY),
        ("2026년 7월 1일부터 주문", cw.OPEN_END_KEY),
        ("2026년 7월 1일 이래 주문", cw.OPEN_END_KEY),
        # 날짜 단위와 무관하게 같은 규칙이다 — 월·연도도 경계가 될 수 있다.
        ("2019년까지 주문", cw.OPEN_START_KEY),
        ("2026년 7월 이후 주문", cw.OPEN_END_KEY),
    ],
)
def test_every_boundary_word_opens_the_declared_side(text: str, opened: str) -> None:
    window = _only_window(text)
    assert window.get(opened) is True
    closed = cw.OPEN_END_KEY if opened == cw.OPEN_START_KEY else cw.OPEN_START_KEY
    assert closed not in window


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 범위는 접히면서 닫는 말까지 삼킨다 — 그 자리에 경계 낱말이 남지 않는다.
        ("2026년 7월 1일부터 2026년 7월 31일까지 주문", ("20260701", "20260731")),
        ("2026년 7월부터 12월까지 구매가 없는 고객", ("20260701", "20261231")),
        ("2026-07-01부터 2026-07-31까지 구매한 고객", ("20260701", "20260731")),
    ],
)
def test_a_closed_range_is_never_reopened_by_its_own_boundary_words(
    text: str, expected: tuple[str, str]
) -> None:
    """'A부터 B까지'의 '까지'는 범위를 닫는 말이지 B 를 열린 구간으로 만드는 말이 아니다.

    이 구분이 없으면 '7월부터 12월까지'가 '태초부터 12월까지'가 되어 앞 경계가 사라진다.
    """
    window = _only_window(text)
    assert (window["from"], window["to"]) == expected
    assert cw.OPEN_START_KEY not in window and cw.OPEN_END_KEY not in window


def test_an_enumeration_is_not_reopened_either() -> None:
    """나열('2월과 3월')의 각 구간도 열린 경계로 바뀌지 않는다 — 링크에 낀 창은 건드리지 않는다."""
    windows = _windows("2026년 2월과 3월의 구매금액이 증가한 회원")
    assert [(w["from"], w["to"]) for w in windows] == [
        ("20260201", "20260228"), ("20260301", "20260331"),
    ]
    assert not any(cw.OPEN_START_KEY in w or cw.OPEN_END_KEY in w for w in windows)


# ── 반복 시각대(날짜 없는 시각 범위) ────────────────────────────────────────────────


def test_a_time_range_without_a_date_is_a_recurring_daily_band() -> None:
    """'밤 11시부터 다음 날 새벽 2시 사이'는 어느 하루가 아니라 **매일** 되풀이되는 시각대다."""
    text = "밤 11시부터 다음 날 새벽 2시 사이에 주문한 회원을 알려줘."
    assert _windows(text) == [], "날짜가 없으므로 달력 창은 만들지 않는다"

    band = cw.parse_time_of_day_window(text)
    assert band[cw.TIME_OF_DAY_FROM_KEY] == "230000"
    assert band[cw.TIME_OF_DAY_TO_KEY] == "025959"
    assert cw.time_of_day_crosses_midnight(band) is True


def test_a_same_day_band_does_not_claim_to_cross_midnight() -> None:
    """#70 의 대조군. 같은 두 경계로 AND/OR 를 뒤집는 실수를 여기서 잡는다."""
    band = cw.parse_time_of_day_window("오전 9시부터 오후 6시 사이에 주문한 회원")
    assert (band[cw.TIME_OF_DAY_FROM_KEY], band[cw.TIME_OF_DAY_TO_KEY]) == ("090000", "185959")
    assert cw.time_of_day_crosses_midnight(band) is False


@pytest.mark.parametrize(
    "text",
    [
        "9시 이후에 주문한 회원",            # 단독 시각 — 그날의 시점인지 매일의 시각대인지 확정 불가
        "오전 9시부터 주문한 회원",          # 여는 말만 있고 닫는 말이 없다(반쪽 경계)
        "밤 11시부터 다음 날 밤 2시 사이",   # 한정어 선언 범위 밖의 시각이 섞였다
    ],
)
def test_unconfirmable_time_expressions_stay_uninterpreted(text: str) -> None:
    """확정할 수 없는 시각은 근사하지 않는다 — 잘못 건 시각대는 드롭보다 나쁘다."""
    assert cw.parse_time_of_day_window(text) is None


def test_a_dated_time_qualifier_is_not_also_read_as_a_recurring_band() -> None:
    """날짜 토큰이 삼킨 시각은 반복 시각대가 아니다(같은 어구가 조건 두 개가 되면 안 된다)."""
    text = "2026년 7월 1일 오전 9시부터 7월 2일 오후 6시 30분까지 주문한 회원"
    assert cw.parse_time_of_day_window(text) is None
    window = _only_window(text)
    assert (window["from"], window["to"]) == ("20260701", "20260702")
    assert (window["from_time"], window["to_time"]) == ("090000", "183059")


def test_the_recurring_band_reports_its_source_span() -> None:
    """슬롯 소유권은 '같은 종류'가 아니라 '같은 구간'으로 판정된다 — 문법이 위치도 준다."""
    text = "밤 11시부터 다음 날 새벽 2시 사이에 주문한 회원을 알려줘."
    start, end = cw.parse_time_of_day_window_span(text)
    assert text[start:end] == "밤 11시부터 다음 날 새벽 2시 사이"


# ── 슬롯 정규화 ─────────────────────────────────────────────────────────────────────


def test_the_slot_survives_a_recurring_band_without_any_date() -> None:
    """날짜가 없어도 시각대만으로 조건이 성립한다.

    예전에는 from/to 가 없는 슬롯이 통째로 버려졌다 — 시각만 말한 요청은 조건 자체가 사라졌다.
    """
    slot = _slot({"time_of_day": {"from_time": "230000", "to_time": "025959"}})
    assert slot == {"time_of_day": {"from_time": "230000", "to_time": "025959"}}


@pytest.mark.parametrize(
    "raw",
    [
        {"time_of_day": {"from_time": "230000"}},           # 한쪽만 있는 시각대
        {"time_of_day": {"from_time": "230000", "to_time": "2560"}},  # 형식 위반
        {"time_of_day": {"from_time": "240000", "to_time": "025959"}},  # 시 범위 밖
    ],
)
def test_a_half_or_malformed_band_does_not_become_a_condition(raw: dict) -> None:
    assert _slot(raw) is None


def test_open_boundary_markers_survive_normalization() -> None:
    """센티널 날짜만으로도 SQL 은 옳지만, '이 경계는 원문이 말한 것이 아니다'가 감사에 남아야 한다."""
    slot = _slot(_only_window("2026년 7월 1일 23시 59분 59초까지 주문한 회원을 찾아줘."))
    assert slot[cw.OPEN_START_KEY] is True
    assert (slot["from"], slot["to"], slot["to_time"]) == (cw.OPEN_WINDOW_MIN_DATE, "20260701", "235959")


# ── SQL ─────────────────────────────────────────────────────────────────────────────


def test_until_compiles_to_an_open_start_range_with_the_instant_on_the_boundary_day() -> None:
    sql = _header_sql(_slot(_only_window("2026년 7월 1일 23시 59분 59초까지 주문한 회원을 찾아줘.")))
    assert sql == (
        "(O.ORDER_DATE BETWEEN '19000101' AND '20260701'"
        " AND (O.ORDER_DATE < '20260701' OR O.ORDER_TIME <= '235959'))"
    )


def test_after_compiles_to_an_open_end_range_with_the_instant_on_the_boundary_day() -> None:
    sql = _header_sql(_slot(_only_window("2026년 7월 1일 23시 59분 30초 이후 주문한 회원")))
    assert sql == (
        "(O.ORDER_DATE BETWEEN '20260701' AND '99991230'"
        " AND (O.ORDER_DATE > '20260701' OR O.ORDER_TIME >= '235930'))"
    )


def test_a_multi_day_range_puts_the_clock_only_on_the_boundary_days() -> None:
    """가운데 날은 전일이 포함된다.

    양 경계에 시각을 무조건 AND 로 걸면 '7월 1일 09:00 ~ 7월 2일 18:30' 이 '매일 09:00~18:30'
    이 되어 7월 1일 18:30 이후 주문이 조용히 사라진다.
    """
    sql = _header_sql(_slot(_only_window(
        "2026년 7월 1일 오전 9시부터 7월 2일 오후 6시 30분까지 주문한 회원"
    )))
    assert sql == (
        "(O.ORDER_DATE BETWEEN '20260701' AND '20260702'"
        " AND (O.ORDER_DATE > '20260701' OR O.ORDER_TIME >= '090000')"
        " AND (O.ORDER_DATE < '20260702' OR O.ORDER_TIME <= '183059'))"
    )


def test_a_same_day_band_uses_and_and_a_midnight_crossing_band_uses_or() -> None:
    """이 한 줄이 이 파일의 존재 이유다. 자정 횡단을 AND 로 쓰면 **항상 거짓**이고,
    같은 날 범위를 OR 로 쓰면 **하루 전체**가 된다 — 같은 두 경계로 정반대 오답이 나온다."""
    same_day = _header_sql(_slot({"time_of_day": cw.parse_time_of_day_window(
        "오전 9시부터 오후 6시 사이에 주문한 회원"
    )}))
    crossing = _header_sql(_slot({"time_of_day": cw.parse_time_of_day_window(
        "밤 11시부터 다음 날 새벽 2시 사이에 주문한 회원을 알려줘."
    )}))
    assert same_day == "(O.ORDER_TIME >= '090000' AND O.ORDER_TIME <= '185959')"
    assert crossing == "(O.ORDER_TIME >= '230000' OR O.ORDER_TIME <= '025959')"


def test_a_band_is_anded_onto_the_date_range_not_folded_into_it() -> None:
    """반복 시각대는 날짜 구간과 **직교**한다 — 날짜 OR 나열 전체에 AND 로 얹힌다."""
    sql = _header_sql(_slot({
        "from": "20260701", "to": "20260731",
        "time_of_day": {"from_time": "230000", "to_time": "025959"},
    }))
    assert sql == (
        "(O.ORDER_DATE BETWEEN '20260701' AND '20260731'"
        " AND (O.ORDER_TIME >= '230000' OR O.ORDER_TIME <= '025959'))"
    )


def test_a_context_without_the_clock_column_correlates_to_the_header() -> None:
    """시각은 주문 헤더에만 있다. 상세 문맥에서는 헤더 상관 EXISTS 로 표현한다."""
    sql = graph_rag._purchase_date_predicate(
        _slot({"time_of_day": {"from_time": "230000", "to_time": "025959"}}),
        alias="D", column=_DETAIL["date_column"], source_table=_DETAIL["table"],
    )
    assert sql == (
        f"EXISTS (SELECT 1 FROM {_HEADER['table']} OT"
        f" WHERE OT.{_HEADER['order_id_column']} = D.{_HEADER['order_id_column']}"
        f" AND (OT.{_HEADER['time_column']} >= '230000'"
        f" OR OT.{_HEADER['time_column']} <= '025959'))"
    )


def test_a_context_that_cannot_reach_the_clock_column_emits_no_predicate_at_all() -> None:
    """시각을 표현할 수 없으면 날짜만 걸어 내보내지 않는다 — 조건이 넓어진 채 실행되는 것보다
    술어를 아예 만들지 않는 편이 낫다(호출부가 미생성으로 처리하고 불변식이 출고를 막는다)."""
    slot = _slot({"time_of_day": {"from_time": "230000", "to_time": "025959"}})
    assert graph_rag._purchase_date_predicate(
        slot, alias=None, column="ORDER_DATE", source_table="SOME_OTHER_TABLE"
    ) is None


# ── 경계값 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("clock", "inside"),
    [
        ("225959", False),  # 직전 1초
        ("230000", True),   # 정확한 시작 경계
        ("230001", True),
        ("015959", True),   # 끝 단위(01시)의 마지막 순간
        ("020000", False),  # 직후 — 02시는 시각대 밖이다
        ("120000", False),  # 한낮
    ],
)
def test_midnight_crossing_band_boundary_rows(clock: str, inside: bool) -> None:
    """자정 횡단 시각대의 경계 행 — **생성된 술어를 평가해서** 포함 여부를 잰다.

    AND 로 컴파일되면 모든 행이 거짓이 되어 이 표 전체가 깨진다. 파이썬으로 같은 논리를 다시
    적으면 그 뒤집힘을 함께 따라가므로 아무것도 잡지 못한다.
    """
    sql = _header_sql(_slot({"time_of_day": {"from_time": "230000", "to_time": "015959"}}))
    assert _evaluate_predicate(sql, {"O.ORDER_TIME": clock}) is inside


@pytest.mark.parametrize(
    ("clock", "inside"),
    [("085959", False), ("090000", True), ("185959", True), ("190000", False)],
)
def test_same_day_band_boundary_rows(clock: str, inside: bool) -> None:
    """같은 날 시각대가 OR 로 컴파일되면 모든 행이 참이 되어 이 표가 깨진다."""
    sql = _header_sql(_slot({"time_of_day": {"from_time": "090000", "to_time": "185959"}}))
    assert _evaluate_predicate(sql, {"O.ORDER_TIME": clock}) is inside


@pytest.mark.parametrize(
    ("order_date", "clock", "inside"),
    [
        ("20260630", "235959", False),  # 하루 전
        ("20260701", "085959", False),  # 시작 직전 1초
        ("20260701", "090000", True),   # 정확한 시작 경계
        ("20260701", "183100", True),   # 첫날 오후 — 가운데가 아니라 첫날이지만 종일 유효하다
        ("20260701", "235959", True),
        ("20260702", "000000", True),   # 마지막 날 자정
        ("20260702", "183059", True),   # 끝 단위(18시 30분)의 마지막 순간
        ("20260702", "183100", False),  # 직후 1초
        ("20260703", "000000", False),  # 하루 뒤
    ],
)
def test_multi_day_range_boundary_rows(order_date: str, clock: str, inside: bool) -> None:
    """날짜를 넘는 절대 시각 범위의 경계 행.

    ('20260701', '183100') 이 참인 것이 이 표의 핵심이다 — 양 경계 시각을 무조건 AND 로 걸면
    이 행이 거짓이 되어 '7월 1일 09:00 ~ 7월 2일 18:30' 이 '매일 09:00~18:30' 으로 바뀐다.
    """
    sql = _header_sql(_slot(_only_window(
        "2026년 7월 1일 오전 9시부터 7월 2일 오후 6시 30분까지 주문한 회원"
    )))
    row = {"O.ORDER_DATE": order_date, "O.ORDER_TIME": clock}
    assert _evaluate_predicate(sql, row) is inside


@pytest.mark.parametrize(
    ("order_date", "clock", "inside"),
    [
        ("20200101", "120000", True),   # 열린 과거 — 아주 오래된 주문도 포함된다
        ("20260701", "235958", True),
        ("20260701", "235959", True),   # 말한 그 초는 포함('까지' 정책)
        ("20260702", "000000", False),  # 직후 1초
    ],
)
def test_open_start_range_boundary_rows(order_date: str, clock: str, inside: bool) -> None:
    """'…까지'가 칸으로 읽히면 ('20200101','120000') 행이 거짓이 되어 이 표가 깨진다."""
    sql = _header_sql(_slot(_only_window(
        "2026년 7월 1일 23시 59분 59초까지 주문한 회원을 찾아줘."
    )))
    row = {"O.ORDER_DATE": order_date, "O.ORDER_TIME": clock}
    assert _evaluate_predicate(sql, row) is inside


@pytest.mark.parametrize(
    ("order_date", "clock", "inside"),
    [
        ("20260701", "235929", False),  # 직전 1초
        ("20260701", "235930", True),   # 말한 그 초부터
        ("20260702", "000000", True),
        ("20270101", "000000", True),   # 열린 미래
    ],
)
def test_open_end_range_boundary_rows(order_date: str, clock: str, inside: bool) -> None:
    sql = _header_sql(_slot(_only_window("2026년 7월 1일 23시 59분 30초 이후 주문한 회원")))
    row = {"O.ORDER_DATE": order_date, "O.ORDER_TIME": clock}
    assert _evaluate_predicate(sql, row) is inside


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 윤일
        ("2024년 2월 29일 23시 59분 59초까지 주문", ("19000101", "20240229", "235959")),
        # 평년 2월 29일은 존재하지 않는다 — 창을 만들지 않는다(아래 별도 테스트)
        # 월말
        ("2026년 7월 31일 이후 주문", ("20260731", "99991230", None)),
        # 연말
        ("2026년 12월 31일 23시 59분 59초까지 주문", ("19000101", "20261231", "235959")),
    ],
)
def test_calendar_edges_keep_the_clock(text: str, expected: tuple[str, str, str | None]) -> None:
    window = _only_window(text)
    start, end, clock = expected
    assert (window["from"], window["to"]) == (start, end)
    if clock is None:
        assert "to_time" not in window
    else:
        assert window["to_time"] == clock


def test_a_day_that_does_not_exist_never_becomes_a_window() -> None:
    assert _windows("2026년 2월 29일 23시 59분 59초까지 주문") == []


# ── 주 경계 ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 기준일 2026-08-05 는 수요일이다.
        ("지난 주에 주문한 회원", ("20260727", "20260802")),
        ("지난주 주문한 회원", ("20260727", "20260802")),
        ("이번 주 주문한 회원", ("20260803", "20260809")),
        ("금주 주문한 회원", ("20260803", "20260809")),
    ],
)
def test_week_windows_run_monday_to_sunday(text: str, expected: tuple[str, str]) -> None:
    """주 경계는 월요일 00:00 ~ 다음 월요일 00:00 전이다.

    이 스캐너가 없던 동안 '지난 주'는 어떤 창도 만들지 못했고, 기간이 빠진 '구매 있음'(전수
    EXISTS)으로 컴파일될 수 있었다 — 오늘/어제 계열이 같은 이유로 생긴 스캐너의 주 단위 대칭이다.
    """
    window = _only_window(text)
    assert (window["from"], window["to"]) == expected


def test_a_week_window_and_the_relative_past_week_share_one_boundary_policy() -> None:
    """'지난주'와 '1주 전'이 서로 다른 이레를 가리키면 정책이 두 곳에 있는 것이다."""
    scanned = _only_window("지난주 주문")
    relative = cw.relative_past_window(1, "weeks", today=FIXED_TODAY)
    assert (scanned["from"], scanned["to"]) == (relative["from"], relative["to"])


# ── Canonical Event IR 왕복 ─────────────────────────────────────────────────────────


def test_the_interval_ir_carries_the_clock_through_a_json_round_trip() -> None:
    """직렬화 → 복원에서 시각이 사라지면 그 조건은 다음 계층에서 날짜로 넓어진다."""
    window = _only_window("2026년 7월 1일 오전 9시부터 7월 2일 오후 6시 30분까지 주문한 회원")
    interval = event_ir.AbsoluteInterval.from_calendar_window(window)

    assert interval.has_time_bounds is True
    assert (interval.start_time, interval.end_time) == ("090000", "183059")

    restored = event_ir.AbsoluteInterval.from_dict(json.loads(json.dumps(interval.to_dict())))
    assert restored == interval
    assert restored.to_calendar_window() == {
        "from": "20260701", "to": "20260702", "from_time": "090000", "to_time": "183059",
    }


def test_a_clockless_interval_serializes_byte_identically_to_before() -> None:
    """시각이 없는 창의 표기는 바뀌지 않는다 — 기존 스냅샷·소비자와의 호환이 그 조건이다."""
    interval = event_ir.AbsoluteInterval.from_calendar_window({"from": "20260701", "to": "20260731"})
    assert interval.to_dict() == {
        "type": "interval",
        "start": "2026-07-01",
        "end_exclusive": "2026-08-01",
        "from": "20260701",
        "to": "20260731",
    }
    assert interval.to_calendar_window() == {"from": "20260701", "to": "20260731"}
    assert interval.has_time_bounds is False


def test_the_open_boundary_sentinels_survive_every_downstream_date_arithmetic() -> None:
    """열린 구간 센티널은 **표현 가능한 최대 구간**이어야 한다.

    실측 결함: 끝 센티널을 9999-12-31 로 두자 반개구간 변환(`끝 + 하루`)이 연도 10000 을 만들어
    라이브 '… 이후' 프롬프트가 `OverflowError` 로 500 을 냈다. 센티널은 우리가 고르는 값이므로
    소비자마다 방어 코드를 다는 대신 **넘치지 않는 값**을 고른다.
    """
    opened = _only_window("2026년 7월 1일 23시 59분 30초 이후 주문한 회원")

    assert graph_rag._next_day8(opened["to"]) == "99991231"  # date.max — 여전히 표현된다
    interval = event_ir.AbsoluteInterval.from_calendar_window(opened)
    assert interval is not None and interval.end_exclusive == date.max
    assert interval.to_calendar_window()["to"] == cw.OPEN_WINDOW_MAX_DATE  # 왕복 대칭

    # 열린 시작 쪽도 같은 방식으로 확인한다(이쪽은 뺄셈이 없어 넘칠 자리가 없다).
    until = _only_window("2026년 7월 1일 23시 59분 59초까지 주문한 회원을 찾아줘.")
    assert event_ir.AbsoluteInterval.from_calendar_window(until) is not None
    assert _header_sql(_slot(until)) is not None


def test_an_unrepresentable_interval_is_refused_rather_than_raised() -> None:
    """손으로 적은 plan 이 date.max 를 끝으로 주면 예외가 아니라 미해석으로 답한다."""
    assert event_ir.AbsoluteInterval.from_calendar_window(
        {"from": "20260701", "to": "99991231"}
    ) is None


@pytest.mark.parametrize("clock", ["24000", "240000", "236000", "235961", "9시"])
def test_the_interval_ir_refuses_malformed_clock_values(clock: str) -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.AbsoluteInterval(
            start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2), start_time=clock
        )


def test_a_reversed_clock_on_a_single_day_is_not_an_interval() -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.AbsoluteInterval(
            start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2),
            start_time="180000", end_time="090000",
        )


def test_a_date_only_time_binding_refuses_a_clock_bounded_interval() -> None:
    """날짜 단위 컬럼에 시각 구간을 걸면 '그날 하루 전체'가 된다 — 근사하지 않고 닫는다.

    이 fail-close 가 없으면 canonical 레인이 시각을 조용히 버린 SQL 을 성공으로 내보내고,
    plan 에 시각 흔적이 남지 않아 silent-drop 가드조차 볼 것이 없다.
    """
    context = event_compiler.CompileContext(today=FIXED_TODAY)
    interval = event_ir.AbsoluteInterval(
        start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2), end_time="235959"
    )
    with pytest.raises(event_compiler.SqlCompileError):
        event_compiler.compile_time_window(
            "EO.ORDER_DATE", interval, "w0", data_type="char8", context=context
        )


# ── 조용한 드롭 금지 ────────────────────────────────────────────────────────────────


def test_the_guard_sees_a_recurring_band_the_same_way_it_sees_a_dated_clock() -> None:
    """시각 조건이 어떤 모양으로 실리든 SQL 에 시각 컬럼이 없으면 그것은 조용한 확대다."""
    dated = {"target_user": {"purchase_date": {
        "from": "20260701", "to": "20260701", "to_time": "235959", "label": "7월 1일 23:59:59까지",
    }}}
    recurring = {"target_user": {"purchase_date": {
        "time_of_day": {"from_time": "230000", "to_time": "025959"}, "label": "밤 11시~새벽 2시",
    }}}

    assert graph_rag._plan_time_bounded_window_labels(dated) == ["7월 1일 23:59:59까지"]
    assert graph_rag._plan_time_bounded_window_labels(recurring) == ["밤 11시~새벽 2시"]
    assert graph_rag._plan_time_of_day_bounds(recurring) == [
        {"from_time": "230000", "to_time": "025959"}
    ]
    assert graph_rag._plan_time_of_day_bounds(dated) == []


def test_a_dropped_recurring_band_is_announced_by_name() -> None:
    """plan 이 시각대를 못 받았으면 침묵하지 않는다 — 그 침묵이 '아무 때나 주문'이다."""
    text = "밤 11시부터 다음 날 새벽 2시 사이에 주문한 회원을 알려줘."
    dropped = graph_rag._deterministic_dropped_conditions(text, {"target_user": {}}, today=FIXED_TODAY)
    assert any("시각대" in warning for warning in dropped), dropped

    claimed = graph_rag._deterministic_dropped_conditions(
        text,
        {"target_user": {"purchase_date": {"time_of_day": {"from_time": "230000", "to_time": "025959"}}}},
        today=FIXED_TODAY,
    )
    assert not any("시각대" in warning for warning in claimed), claimed
