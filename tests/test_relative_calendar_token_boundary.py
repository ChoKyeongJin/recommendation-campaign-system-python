"""상대 달력 토큰의 **matcher 불변식** — 어휘 한 낱말이 아니라 매칭 규칙을 고정한다.

배경(실측 2026-08-08). '지지난달 구매한 회원'이 경고 하나 없이 **2026년 7월** 창으로 컴파일됐다.
한글에는 ``\\b`` 가 성립하지 않아 어휘 alternation 의 finditer 가 (1,4) 의 '지난달'을 잡았기
때문이다. 같은 결함이 전전월→전월, 지지난주→지난주, 그그제→그제에서 재현됐다.

0건이 나오는 것은 이 프로젝트에서 허용되는 실패지만 **틀린 모집단**은 아니다. 그래서 여기서
재는 것은 특정 낱말의 지원 여부가 아니라 매칭 규칙 셋이다.

    1. 같은 시작점이면 가장 긴 유효 매치가 이긴다.
    2. 더 긴 한글 낱말의 부분 매치는 토큰이 아니다(왼쪽 경계).
    3. 겹치는 후보의 선택은 결정론적이다 — 스캐너 등록 순서에 좌우되지 않는다.

'지지난달'을 **지원**할지는 별개 결정이다(어휘에 offset −2 를 선언하면 된다). 이 테스트가
고정하는 것은 "모르는 것을 아는 척하지 않는다"이지 "지원하지 않는다"가 아니다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import calendar_window  # noqa: E402

TODAY = date(2026, 8, 8)

# 접두어가 붙은 형태 → 그 접두어를 못 읽으면 **창이 없어야** 한다. 짧은 쪽으로 읽으면 한 달/한 주
# 어긋난 모집단이 조용히 나간다.
PREFIXED_FORMS = (
    ("지지난달", "지난달"),
    ("전전월", "전월"),
    ("지지난주", "지난주"),
    ("그그제", "그제"),
)

# 정상 형태 — 가드가 이들을 잡아먹으면 안 된다(조사가 붙은 형태 포함).
STANDALONE_FORMS = (
    "오늘", "금일", "어제", "어저께", "그제", "그저께",
    "이번 주", "이번주", "금주", "지난 주", "지난주", "저번 주", "저번주", "전주",
    "이번 달", "이번달", "이달", "금월", "당월",
    "지난 달", "지난달", "저번 달", "저번달", "전월",
)


def _windows(text: str) -> list[tuple[dict, int, int]]:
    return calendar_window.parse_calendar_window_spans(text, today=TODAY)


@pytest.mark.parametrize(("prefixed", "shorter"), PREFIXED_FORMS)
def test_prefixed_form_does_not_match_its_shorter_suffix(prefixed: str, shorter: str) -> None:
    """불변식 2 — 더 긴 한글 낱말의 꼬리는 토큰이 아니다."""
    assert _windows(f"{prefixed} 구매한 회원") == []
    # 대조: 짧은 형태 단독은 그대로 읽힌다(가드가 어휘를 죽이지 않았다).
    assert len(_windows(f"{shorter} 구매한 회원")) == 1


@pytest.mark.parametrize("word", STANDALONE_FORMS)
def test_standalone_relative_words_still_resolve(word: str) -> None:
    """회귀 — 정상 형태와 조사가 붙은 형태는 계속 창이 된다."""
    assert len(_windows(f"{word} 구매한 회원")) == 1
    assert len(_windows(f"{word}에 구매한 회원")) == 1


def test_prefixed_form_yields_no_comparison_obligation() -> None:
    """문장 수준 — 못 읽은 창으로 기간 대 기간 비교를 세우지 않는다.

    수정 전에는 '지지난달 대비 이번달'이 7월 vs 8월 비교 SQL 로 나갔다(둘 다 틀린 창).
    """
    import lowering_planner

    query = "지지난달 대비 이번달 구매금액이 증가한 회원"
    assert len(_windows(query)) == 1  # '이번달' 만 읽힌다
    assert lowering_planner.detect_comparison_obligations(query, today=TODAY) == ()


def test_overlapping_candidates_resolve_deterministically() -> None:
    """불변식 1·3 — 같은 시작점이면 최장, 겹침 해소는 등록 순서와 무관하다."""
    resolve = calendar_window._resolve_overlapping_candidates
    marker = {"from": "20260801", "to": "20260831", "label": "x"}
    # 같은 시작점, 길이만 다름 → 긴 쪽이 이긴다.
    longer, shorter = (marker, 3, 0, 4), (marker, 3, 0, 2)
    assert resolve([shorter, longer]) == [longer]
    assert resolve([longer, shorter]) == [longer]
    # 완전히 포함된 후보는 버린다.
    outer, inner = (marker, 3, 0, 6), (marker, 3, 2, 4)
    assert resolve([inner, outer]) == [outer]
    assert resolve([outer, inner]) == [outer]
    # 겹치지 않는 후보는 둘 다 남고 등장 순으로 정렬된다.
    left, right = (marker, 3, 0, 3), (marker, 3, 5, 8)
    assert resolve([right, left]) == [left, right]


def test_guard_matches_the_standalone_year_precedent() -> None:
    """같은 파일의 상대 연도 가드와 같은 규칙임을 고정한다(드리프트 방지).

    '재작년'이 '작년'으로 읽히지 않는 것은 이미 :data:`_STANDALONE_YEAR_RE` 가 보장했다.
    상대 일/주/월만 빠져 있었고, 이제 세 축이 같은 규칙을 쓴다.
    """
    assert _windows("재작년 구매한 회원") == []
    assert len(_windows("작년 구매한 회원")) == 1
