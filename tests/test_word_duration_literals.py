"""명시 기간의 **표면형 공백**을 메운 계약 — 단어형('일주일')과 ``-간`` 접미형('3개월간').

사용자 요구는 '모든 명시 기간에 적용되는 일반 규칙'이다. 그런데 '원문이 기간을 말했는가'의
판정 재료(리터럴 바인딩의 ``kind="duration"``)가 표면형을 다 덮지 못해, 분명히 말한 기간인데도
되묻기로 닫히는 구멍이 남아 있었다:

  (가) 단어형 '일주일/한 달/반년/보름/석달/일년/한해' → 바인딩 자체가 안 생김
  (나) ``-간`` 접미형 '3개월간/2주간' → 안 생김(반면 '30일간/1년간'은 생김 — 표면 목록 불균일)

이 파일이 지키는 계약은 넷이다:
  1. 단어형 선언 전 항목이 바인딩으로 선다(값·단위는 :data:`calendar_window.WORD_DURATION_SPECS`
     선언과 일치하고, 모양은 숫자형과 같다).
  2. 낱말 경계 — 압축 좌표계 스캔이 만들어 낸 오탐('한해서', '일년생', '세달째', '모두 주문')이
     리터럴 근거로 새어 나가지 않는다.
  3. ``-간`` 접미형은 어간에서 **파생**한다(단위마다 표면을 손으로 적지 않는다).
  4. 종류 판정('보름 전' vs '최근 보름')은 숫자형과 같은 경로를 쓴다.

2026-08-07 기준 모양 변경: 기간 원자가 ``event_ir_window``(창의 wire 모양)를 함께 싣는다.
숫자형·단어형·``-간`` 접미형이 모두 같은 자리에 같은 모양을 받으므로 위 계약 1·3 의 기대 dict 에
그 키가 추가됐다(삭제·완화가 아니라 추가다). 이유와 단일 계약은
``tests/test_duration_binding_wire_window.py`` 가 소유한다.
"""

from __future__ import annotations

import pytest

import calendar_window
import condition_normalizers
from query_structurer.semantic_ir import extract_literal_bindings


FIXED_DATE = "2026-08-04"
# 종류 판정 어휘가 붙지 않은 자리에 넣어 값·단위만 재는 문형. 앞의 '최근'은 롤링 표지다.
_ROLLING_TEMPLATE = "최근 {surface} 구매한 회원"


def _durations(query: str) -> list[dict]:
    return [
        binding
        for binding in extract_literal_bindings(query, current_date=FIXED_DATE)
        if binding["kind"] == "duration"
    ]


# 기간 원자는 창의 **wire 모양**까지 싣는다(2026-08-07). 모델에게 값·단위를 옮겨 적게 하면
# 이 추출기의 복수형 표기('days')가 툴 스키마 enum(day|week|month|year) 밖이라 그대로 복사한
# 응답이 검증에서 떨어지기 때문이다. 계약의 소유자는 tests/test_duration_binding_wire_window.py
# 이고, 여기서는 단어형·``-간`` 접미형도 같은 모양을 받는다는 사실만 함께 고정한다.
_CANONICAL_WIRE_UNITS = {"days": "day", "weeks": "week", "months": "month", "years": "year"}


def _wire_window(value: int, semantic_unit: str) -> dict:
    return {"type": "rolling", "value": value, "unit": _CANONICAL_WIRE_UNITS[semantic_unit]}


# ── 1) 단어형 전 항목이 선언대로 바인딩된다 ──────────────────────────────────────


@pytest.mark.parametrize("surface", sorted(calendar_window.WORD_DURATION_SPECS))
def test_every_declared_word_duration_becomes_a_literal_binding(surface: str) -> None:
    """선언을 순회한다 — 표면 하나를 빠뜨리면 그 표현만 조용히 되묻기로 닫힌다."""

    value, unit = calendar_window.WORD_DURATION_SPECS[surface]
    query = _ROLLING_TEMPLATE.format(surface=surface)
    durations = _durations(query)

    assert [binding["text"] for binding in durations] == [surface], query
    binding = durations[0]
    assert query[binding["start"] : binding["end"]] == surface
    assert binding["value"] == value
    assert binding["normalized"] == {
        "value": value,
        "surface_unit": calendar_window.CANON_TO_KO_UNIT[unit],
        "semantic_unit": unit,
        "temporal_kind": calendar_window.KIND_ROLLING,
        "event_ir_window": _wire_window(value, unit),
    }


def test_the_word_form_normalized_shape_matches_the_numeric_one() -> None:
    """숫자형과 **같은 모양**이어야 한다 — 하류는 기간 원자를 한 형태로만 읽는다."""

    word = _durations("최근 일주일 구매한 회원")[0]["normalized"]
    numeric = _durations("최근 7일 구매한 회원")[0]["normalized"]

    assert set(word) == set(numeric)
    # ``surface_unit`` 은 canonical 한국어 단위다 — 단어형은 값과 단위가 한 낱말에 붙어 있어
    # 떼어낼 단위 표면이 없다. 그래서 숫자형 표면 목록의 키로 투영한다(원문 표면은 text 가 보존).
    assert word["surface_unit"] in condition_normalizers.numeric_duration_unit_semantics()
    assert word == numeric


@pytest.mark.parametrize(
    ("query", "surface"),
    [("최근 한 달 구매한 회원", "한 달"), ("최근 한달 구매한 회원", "한달")],
)
def test_the_source_span_survives_the_internal_space(query: str, surface: str) -> None:
    """'한 달'과 '한달'은 같은 원자다 — 스캔은 압축 좌표계에서 돌지만 구간은 원문 좌표다."""

    binding = _durations(query)[0]
    assert binding["text"] == surface
    assert query[binding["start"] : binding["end"]] == surface
    assert binding["normalized"]["value"] == 1
    assert binding["normalized"]["semantic_unit"] == "months"


# ── 2) 낱말 경계 ─────────────────────────────────────────────────────────────────
# 기대값의 근거: 아래 표면은 모두 **더 긴 낱말의 조각**이거나 서로 다른 낱말이 압축 좌표계에서
# 이어 붙은 것이다. 그 자리에 기간 원자를 세우면 사용자가 말하지 않은 시간 조건이 생기고, 그
# 조건은 근거 구간까지 갖춘 채로 SQL 로 나간다(되묻기보다 나쁘다). '세달째 미구매'는 사람이
# 읽으면 3개월로 보이지만 '째'가 세는 방식(현재 달 포함 여부·기산점)을 정하지 않으므로 창을
# 확정할 수 없다 — 추측하지 않고 남긴다.


@pytest.mark.parametrize(
    "query",
    [
        "VIP에 한해서 발송",                       # '한해'(1년)가 '한해서'의 앞부분
        "일년생 상품 구매한 회원",                  # '일년'이 '일년생'의 앞부분
        "세달째 미구매한 회원",                     # '세달'이 '세달째'의 앞부분
        "앱과 PC 양쪽 채널에서 모두 주문한 회원",     # '모두 주문' → '두주'(2주)
        "보름달 이벤트에 참여한 회원",               # '보름'이 '보름달'의 앞부분
        "최근 6개월 중 적어도 한 달은 골드 이상이었던 회원",  # 조사가 붙은 '한 달'은 세는 수다
    ],
)
def test_word_fragments_do_not_become_period_literals(query: str) -> None:
    """낱말 조각은 기간이 아니다 — 이 가드가 없으면 오탐이 그대로 근거가 된다."""

    leaked = [
        binding["text"]
        for binding in _durations(query)
        if calendar_window.is_word_duration_surface(binding["text"])
    ]
    assert not leaked, f"{query} → 단어형 기간 오탐: {leaked}"


@pytest.mark.parametrize(
    "query",
    [
        "최근 일주일 구매한 회원",
        "최근 한 달 구매한 회원",
        "반년 이상 미구매한 회원",
        "보름 전 가입한 회원",
        "일주일간 장바구니를 유지한 회원",
        "한해 동안 구매한 회원",
        "일주일 이상 유지한 장바구니",
        "최근일주일 구매한 회원",
    ],
)
def test_the_boundary_guard_keeps_the_ordinary_expressions(query: str) -> None:
    """가드가 정상 표현까지 지우면 그 순간 이 변경은 되묻기를 늘리는 회귀다."""

    assert len(_durations(query)) == 1, query


def test_the_boundary_guard_is_owned_by_the_grammar_module() -> None:
    """판정의 소유자는 문법 모듈 하나다 — 스캐너가 자기 목록으로 다시 읽지 않는다."""

    text = "VIP에 한해서 발송"
    assert calendar_window.is_word_duration_surface("한해")
    assert not calendar_window.is_standalone_word_duration(text, 4, 6)
    assert calendar_window.is_standalone_word_duration("최근 한해 구매", 3, 5)


# ── 3) ``-간`` 접미형은 파생이다 ────────────────────────────────────────────────


def test_the_span_suffix_surfaces_are_derived_for_every_unit() -> None:
    """'일간/년간'만 있고 '개월간/주간'은 없던 불균일이 재발하지 않는지."""

    semantics = condition_normalizers.numeric_duration_unit_semantics()
    base = {
        surface: canonical
        for surface, canonical in semantics.items()
        if not surface.endswith("간") or surface + "간" in semantics
    }
    missing = {
        surface + "간": canonical
        for surface, canonical in base.items()
        if semantics.get(surface + "간") != canonical
    }
    assert not missing, f"어간은 있는데 -간 접미형이 없는 단위: {sorted(missing)}"


@pytest.mark.parametrize(
    "surface",
    sorted(
        surface
        for surface in condition_normalizers.numeric_duration_unit_semantics()
        if surface.endswith("간")
    ),
)
def test_every_span_suffix_surface_binds_a_period(surface: str) -> None:
    canonical = condition_normalizers.numeric_duration_unit_semantics()[surface]
    query = _ROLLING_TEMPLATE.format(surface=f"3{surface}")
    durations = _durations(query)

    assert [binding["text"] for binding in durations] == [f"3{surface}"], query
    assert durations[0]["normalized"] == {
        "value": 3,
        "surface_unit": surface,
        "semantic_unit": canonical,
        "temporal_kind": calendar_window.KIND_ROLLING,
        "event_ir_window": _wire_window(3, canonical),
    }


def test_a_bare_weekly_noun_is_not_a_period() -> None:
    """'주간 리포트'의 '주간'은 기간이 아니다 — 표면 목록은 숫자가 앞에 붙은 자리에서만 쓰인다."""

    assert _durations("주간 리포트를 받는 회원") == []
    assert _durations("주간 단위로 발송") == []


# ── 4) 종류 판정은 숫자형과 같은 경로다 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("최근 보름 구매한 회원", calendar_window.KIND_ROLLING),
        ("보름 전 구매한 회원", calendar_window.KIND_PAST_POINT),
        ("최근 석달 구매한 회원", calendar_window.KIND_ROLLING),
        ("석달 전 구매한 회원", calendar_window.KIND_PAST_POINT),
    ],
)
def test_word_forms_get_the_same_temporal_kind_rule(query: str, expected: str) -> None:
    """'최근 보름'과 '보름 전'은 값이 같고 뜻이 다르다 — 원자에 종류가 실려야 구분된다."""

    assert [binding["normalized"]["temporal_kind"] for binding in _durations(query)] == [
        expected
    ], query


def test_the_word_form_kind_matches_the_numeric_one_for_the_same_meaning() -> None:
    """단어형이 숫자형과 다른 종류로 읽히면 같은 뜻이 경로마다 다른 창이 된다."""

    word = _durations("보름 전 구매한 회원")[0]["normalized"]
    numeric = _durations("15일 전 구매한 회원")[0]["normalized"]
    assert word["temporal_kind"] == numeric["temporal_kind"]
    assert (word["value"], word["semantic_unit"]) == (numeric["value"], numeric["semantic_unit"])
