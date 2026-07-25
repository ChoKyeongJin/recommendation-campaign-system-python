"""기간 창(duration window) 파싱 특성화 — B-1(기간단위 맵 3벌 통합) 리팩터 전 안전망.

배경: 한글 기간단위 → 일수 환산표가 최소 3벌 존재한다 —
  * _WINDOW_UNIT_DAYS(집계창용 2차 파서 _parse_recent_window_days 소유; '주일'·단어형 미지원)
  * _DURATION_UNIT_DAYS(통합 파서 _parse_duration_window 소유; 숫자형·단어형 모두 지원)
  * targeting_ir.UNIT_DAYS(영문 canonical 단위; 순수 모듈 소유)
새 기간 단위(분기/반기 등)를 넣으려면 이 표들과 정규식을 동시에 고쳐야 한다. B-1 은 2차 파서
(_parse_recent_window_days)를 통합 파서로 흡수해 한글 단위 맵을 1벌로 줄이는 것이 목표다.

이 파일은 리팩터가 '동작 불변'임을 증명하도록 현재 파서들의 출력을 코퍼스로 고정한다. 값은 현재 코드를
실행해 얻은 실측치를 리터럴로 박았다(공식 재유도가 아니라 스냅샷 — 순환 검증 방지). 통합 후에도
_parse_recent_window_days 는 동일한 int(일수)를, _parse_duration_window 는 동일한 shape 를 돌려줘야 한다.

실행: python -m pytest tests/test_duration_window_characterization.py -q
"""

import pytest

import graph_rag as g
import targeting_ir


# ── 2차 파서 _parse_recent_window_days: '최근 N<단위>' → 일수(int) 또는 None ──────────────
# 현재 커버: 일/주/개월/달/년 (숫자형만). '주일'·단어형·부정경계 미지원(= None). B-1 흡수 후에도 동일.
_RECENT_WINDOW_DAYS_CASES = [
    ("최근 90일", 90),
    ("최근 3개월", 90),
    ("최근 2주", 14),
    ("최근 7일 로그인", 7),
    ("최근 1년", 365),
    ("일주일 이상", None),   # 단어형 미지원
    ("반년 미구매", None),   # 단어형 미지원
    ("1년 이내 가입", None), # '최근' 표지 없음
    ("3주일", None),         # '주일' 단위 + '최근' 없음
    ("최근 로그인", None),   # 숫자 없음
]


@pytest.mark.parametrize("query, expected_days", _RECENT_WINDOW_DAYS_CASES)
def test_parse_recent_window_days_characterization(query, expected_days):
    assert g._parse_recent_window_days(query) == expected_days


# ── 통합 파서 _parse_duration_window: {value, unit, min_days} 또는 None ────────────────────
_DURATION_WINDOW_CASES = [
    ("최근 90일", {"value": 90, "unit": "days", "min_days": 90}),
    ("최근 3개월", {"value": 3, "unit": "months", "min_days": 90}),
    ("최근 2주", {"value": 2, "unit": "weeks", "min_days": 14}),
    ("일주일 이상", {"value": 7, "unit": "days", "min_days": 7}),
    ("반년 미구매", {"value": 180, "unit": "days", "min_days": 180}),
    ("1년 이내 가입", {"value": 1, "unit": "years", "min_days": 365}),
    ("한달", {"value": 30, "unit": "days", "min_days": 30}),
    ("두달", {"value": 60, "unit": "days", "min_days": 60}),
    ("석달", {"value": 90, "unit": "days", "min_days": 90}),
    ("보름", {"value": 15, "unit": "days", "min_days": 15}),
    ("3주일", {"value": 3, "unit": "weeks", "min_days": 21}),
    ("6개월 동안", {"value": 6, "unit": "months", "min_days": 180}),
    ("2년", {"value": 2, "unit": "years", "min_days": 730}),
    ("최근 로그인", None),  # 숫자·단어형 기간 표현 없음(require_number 기본 True)
]


@pytest.mark.parametrize("query, expected", _DURATION_WINDOW_CASES)
def test_parse_duration_window_characterization(query, expected):
    assert g._parse_duration_window(query) == expected


def test_duration_window_candidates_positions():
    # (시작, 끝, value, canonical_unit) — 위치까지 고정(앵커 근접 필터 로직이 이 위치에 의존).
    assert g._duration_window_candidates("최근3개월") == [(2, 5, 3, "months")]
    assert g._duration_window_candidates("일주일이상") == [(0, 3, 7, "days")]
    assert g._duration_window_candidates("3주일") == [(0, 3, 3, "weeks")]


# ── 단위 맵 3벌의 현재 내용 고정 — 통합 시 '값이 바뀌지 않았음'을 증명 ──────────────────────
def test_window_unit_days_map_snapshot():
    # 2차 파서 소유 맵('주일' 없음이 특징 — B-1 이 흡수하면 이 맵은 사라지거나 통합 맵의 부분집합이 됨).
    assert g._WINDOW_UNIT_DAYS == {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}


def test_duration_unit_days_map_snapshot():
    assert g._DURATION_UNIT_DAYS == {"일": 1, "주": 7, "주일": 7, "개월": 30, "달": 30, "년": 365}


def test_ko_unit_to_canon_map_snapshot():
    assert g._KO_UNIT_TO_CANON == {
        "일": "days", "주": "weeks", "주일": "weeks", "개월": "months", "달": "months", "년": "years",
    }


def test_ir_unit_days_map_snapshot():
    # 영문 canonical → 일수(순수 모듈 소유). _parse_duration_window 의 min_days 계산 근거.
    assert targeting_ir.UNIT_DAYS == {"days": 1, "weeks": 7, "months": 30, "years": 365}


def test_recent_window_and_duration_agree_on_supported_units():
    # 통합 가능성의 핵심 계약: 2차 파서가 지원하는 단위에 대해 두 파서의 '일수'가 일치한다.
    # (B-1 이 _parse_recent_window_days 를 _parse_duration_window(...).min_days 로 대체해도 안전함을 보장.)
    for query in ("최근 90일", "최근 3개월", "최근 2주", "최근 1년"):
        legacy_days = g._parse_recent_window_days(query)
        unified = g._parse_duration_window(query)
        assert unified is not None and unified["min_days"] == legacy_days
