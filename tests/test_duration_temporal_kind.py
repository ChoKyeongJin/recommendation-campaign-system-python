"""기간 표현의 **종류**(rolling/past_point)는 애플리케이션이 소유한다.

'최근 30일'과 '30일 전'은 리터럴 원자가 완전히 같다 — value=30, unit=days. 종류를 정하는
단어('최근'/'전')는 그 원자 **밖**에 있어서, 값만 넘기고 창 타입을 모델이 고르게 두면 두 뜻이
조용히 뒤바뀐다. 실측(2026-08-03 라이브): `최근 30일 장바구니에 담아두고 결제하지 않은 회원`
이 relative 창으로 와서

    EC.UPD_DT >= '2026-07-04' AND EC.UPD_DT < '2026-07-05'

즉 30일 전 **하루**만 보는 조건이 됐고, 값 검증·근거 구간 검증·SQL 가드가 전부 통과해
**성공 응답**으로 나갔다. 달력 모듈은 그때도 정답을 알고 있었다(rolling_duration) — 그 판정이
결정권자에게 전달되지 않았을 뿐이다.

이 파일이 지키는 계약은 둘이다:
  1. 판정이 literal binding 에 실려 나간다(semantic_ir).
  2. 실려 온 판정과 어긋난 창 타입은 반려된다(canonical_audience_claims).
"""

from __future__ import annotations

from datetime import date

import canonical_audience_claims
import event_ir
from query_structurer.semantic_ir import extract_literal_bindings


ROLLING_QUERY = "최근 30일 장바구니에 담아두고 결제하지 않은 회원"
PAST_POINT_QUERY = "30일 전 장바구니에 담은 회원"
TODAY = date(2026, 8, 3)


def _duration_bindings(query: str) -> list[dict]:
    return [
        binding
        for binding in extract_literal_bindings(query, current_date=TODAY)
        if binding.get("kind") == "duration"
    ]


def _window_expression(query: str, window: event_ir.TimeWindow) -> event_ir.Condition:
    return event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("cart"),
            event_ir.TimeFilter(event_ir.FieldRef("cart.occurred_at"), window),
        ),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )


# ── 1) 판정이 전달되는가 ──────────────────────────────────────────────────────────


def test_rolling_and_past_point_are_distinguished_in_the_binding() -> None:
    """같은 원자('30일')라도 종류는 다르게 실려야 한다 — 이게 없으면 대조할 것이 없다."""
    rolling = _duration_bindings(ROLLING_QUERY)
    past = _duration_bindings(PAST_POINT_QUERY)
    assert [binding["normalized"]["temporal_kind"] for binding in rolling] == ["rolling_duration"]
    assert [binding["normalized"]["temporal_kind"] for binding in past] == ["past_point"]


def test_the_value_atom_is_unchanged() -> None:
    """구간·값은 그대로다 — 다른 소비자가 이 좌표로 소유권을 계산한다(회귀 방지)."""
    binding = _duration_bindings(ROLLING_QUERY)[0]
    assert ROLLING_QUERY[binding["start"]:binding["end"]] == "30일"
    assert binding["value"] == 30
    assert binding["normalized"]["semantic_unit"] == "days"


def test_kind_maps_to_exactly_one_window_type() -> None:
    """판정 어휘 ↔ IR 창 타입 매핑이 살아 있는지(둘 중 하나가 이름을 바꾸면 대조가 공허해진다)."""
    for query, expected in ((ROLLING_QUERY, "rolling"), (PAST_POINT_QUERY, "relative")):
        kind = _duration_bindings(query)[0]["normalized"]["temporal_kind"]
        assert event_ir.CALENDAR_KIND_WINDOW_TYPES[kind] == expected


# ── 2) 어긋난 창 타입이 반려되는가 ────────────────────────────────────────────────


def test_relative_window_for_a_rolling_phrase_is_rejected() -> None:
    """이번 사고 그 자체 — '최근 30일'에 relative 창이 오면 반려한다."""
    issues = canonical_audience_claims.window_kind_issues(
        ROLLING_QUERY,
        _window_expression(ROLLING_QUERY, event_ir.RelativeWindow(30, "day")),
        _duration_bindings(ROLLING_QUERY),
    )
    assert [issue["code"] for issue in issues] == ["validation_mismatch"]
    assert issues[0]["argument"] == "period"
    assert "rolling" in issues[0]["message"]
    # 근거는 원문 구간이어야 재방출 지시가 어디를 고칠지 가리킬 수 있다.
    evidence = issues[0]["evidence"]
    assert ROLLING_QUERY[evidence["start"]:evidence["end"]] == evidence["text"]


def test_rolling_window_for_a_past_point_phrase_is_rejected() -> None:
    """반대 방향도 막는다 — '30일 전'을 롤링 30일로 넓히면 없던 대상이 들어온다."""
    issues = canonical_audience_claims.window_kind_issues(
        PAST_POINT_QUERY,
        _window_expression(PAST_POINT_QUERY, event_ir.RollingWindow(30, "day")),
        _duration_bindings(PAST_POINT_QUERY),
    )
    assert [issue["argument"] for issue in issues] == ["period"]
    assert "relative" in issues[0]["message"]


def test_matching_window_type_is_not_reported() -> None:
    """가드 공허·오탐 방지 — 맞게 온 창은 통과해야 한다."""
    for query, window in (
        (ROLLING_QUERY, event_ir.RollingWindow(30, "day")),
        (PAST_POINT_QUERY, event_ir.RelativeWindow(30, "day")),
    ):
        assert not canonical_audience_claims.window_kind_issues(
            query, _window_expression(query, window), _duration_bindings(query)
        )


def test_absolute_interval_is_not_claimed_by_this_rule() -> None:
    """절대 구간은 종류 대조 대상이 아니다(달력 창 소유자가 따로 있다)."""
    query = "2017년 1월 장바구니에 담은 회원"
    window = event_ir.AbsoluteInterval(date(2017, 1, 1), date(2017, 2, 1))
    assert not canonical_audience_claims.window_kind_issues(
        query, _window_expression(query, window), _duration_bindings(query)
    )


# ── 3) 고칠 수 있으면 고친다(반려는 고칠 수 없을 때만) ────────────────────────────


def test_the_window_type_is_corrected_before_validation() -> None:
    """앱이 소유한 값이면 되묻지 않고 넣는다 — 값·단위를 다루는 방식과 같다."""
    raw = {
        "type": "time_filter",
        "field": {"type": "field", "name": "cart.occurred_at"},
        "window": {"type": "relative", "value": 30, "unit": "days", "direction": "past"},
    }
    corrections = canonical_audience_claims.apply_window_kinds(
        raw, _duration_bindings(ROLLING_QUERY)
    )
    assert raw["window"]["type"] == "rolling"
    assert corrections == [{"value": 30, "unit": "day", "from": "relative", "to": "rolling"}]


def test_a_correct_window_type_is_left_alone() -> None:
    """맞게 온 창은 건드리지 않는다(무의미한 감사 기록을 만들지 않는다)."""
    raw = {"type": "rolling", "value": 30, "unit": "days"}
    assert canonical_audience_claims.apply_window_kinds(raw, _duration_bindings(ROLLING_QUERY)) == []
    assert raw["type"] == "rolling"


def test_unattributable_windows_are_not_corrected() -> None:
    """귀속할 수 없으면 고치지 않는다 — 그 자리는 반려 규칙이 지킨다."""
    query = "30일 전 가입하고 최근 30일 구매한 회원"
    raw = {"type": "relative", "value": 30, "unit": "days"}
    assert canonical_audience_claims.apply_window_kinds(raw, _duration_bindings(query)) == []
    assert raw["type"] == "relative"


def test_conflicting_phrases_of_the_same_length_are_not_forced() -> None:
    """같은 값의 기간 표현이 종류까지 갈리면 귀속할 수 없다 — 억지로 반려하지 않는다.

    오탐이 나면 사람이 규칙을 끈다. 확정할 수 있을 때만 말하는 것이 규칙의 수명이다.
    """
    query = "30일 전 가입하고 최근 30일 구매한 회원"
    bindings = _duration_bindings(query)
    assert {binding["normalized"]["temporal_kind"] for binding in bindings} == {
        "past_point",
        "rolling_duration",
    }
    assert not canonical_audience_claims.window_kind_issues(
        query, _window_expression(query, event_ir.RelativeWindow(30, "day")), bindings
    )
