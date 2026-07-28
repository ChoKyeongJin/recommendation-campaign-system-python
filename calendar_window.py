"""기간 표현(창)의 단일 소유 모듈 — 절대 달력 창과 상대 기간 창을 한 문법으로 읽는다.

배경: 창 파싱이 모듈마다 따로 살아 있었다. ``entity_set`` 은 ``(\\d{4})년`` 하나만 알아 '2019년 3월'을
2019년 전체로 뭉갰고(3월 베스트셀러가 조용히 연간 베스트셀러가 된다), ``targeting_expression`` 의 LLM
스키마에는 ``year`` 정수 필드뿐이라 월·분기·반기는 애초에 표현할 수단이 없었다. 완전한 달력 문법은
``graph_rag`` 안에만 있었지만 그쪽은 순수 모듈이 아니라 재사용이 불가능했다. 표현형이 하나 늘 때마다
세 곳을 각자 고쳐야 하는 구조 자체가 결함이고, 실제로 한쪽만 고쳐진 상태로 오래 있었다.

이 모듈이 그 문법의 유일한 소유자다. 새 표현(예: 'YYYY년 M월 상순')은 여기 한 곳에 추가하면 규칙
파서·LLM 라우트·구매일 타겟이 동시에 얻는다.

    절대 창 := {from, to, label}      # 달력상 확정된 구간. YYYYMMDD CHAR(8) 비교용.
    상대 창 := {value, unit, min_days} # 기준일로부터 거슬러 세는 구간.

한 문장에 창이 둘 이상 나오는 표현('2019년 2월과 3월', '2019년 1분기 대비 2분기')도 이 문법이 소유한다 —
parse_calendar_windows 가 등장 순서대로 전부 돌려주고, parse_calendar_window 는 그중 하나를 고르는
얇은 래퍼다. 기간 대 기간 비교(증감) 같은 다중 창 소비자는 전자를 쓴다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 도메인 게이트(구매 신호 여부 등)와 물리 매핑은
호출자가 소유한다 — 이 모듈은 '언제'만 읽고 '무엇에 대한 언제'인지는 모른다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import targeting_ir


# ── 절대 달력 창 ──────────────────────────────────────────────────────────────────

# 반기/분기 → 월 범위. 상반기=1~6월, 하반기=7~12월, N분기=(N-1)*3+1 부터 3개월.
QUARTER_MONTH_RANGES = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}

_ANY_YEAR_RE = re.compile(r"(\d{4})\s*년")
_QUARTER_RE = re.compile(r"([1-4])\s*(?:사)?분기")
# 상대 연도 어휘. 새 표현('재작년' → -2)은 이 표 한 줄이면 되고, 정규식은 표에서 파생한다 —
# 어휘와 문법이 따로 자라면 한쪽만 고쳐진 상태가 생긴다.
_RELATIVE_YEAR_OFFSETS = {
    "올해": 0,
    "금년": 0,
    "작년": -1,
    "지난해": -1,
    "전년": -1,
}
_RELATIVE_YEAR_ALTERNATION = "|".join(sorted(_RELATIVE_YEAR_OFFSETS, key=len, reverse=True))

# 나열/범위 연결어. '2018, 2019년'·'2018년 및 2019년'·'2019년 2월과 3월'처럼 창이 이어져 나올 때
# 그 사이에 오는 토큰이다. 나열형 베어 연도 문법(_CAL_TOKEN_RE 의 yb)과 '한 나열에 속하는가' 판정
# (_ENUM_LINK_RE)이 같은 어휘를 쓰도록 한 곳에서 소유한다.
_ENUM_CONNECTORS = ("및", "와", "과", "그리고", "또는", "이나", "랑", "하고")
_YEAR_ENUM_SEP = r"(?:\s*[,·/~∼]\s*|\s*[-–]\s*|\s*(?:" + "|".join(_ENUM_CONNECTORS) + r")\s*)"

# 달력 토큰 스캐너(단일 정규식, 좁은 표현 우선 순서). 파이썬 정규식은 같은 시작 위치에서 앞선 대안을
# 먼저 채택하므로, 이 열거 순서가 곧 '일 > 월 > 분기 > 반기 > 연' 구체성 우선순위다 — '2019년 3월'이
# 연 전체로 뭉개지지 않는다. 뒤쪽 세 대안(연도 생략 월/분기/반기)은 '2019년 2월과 3월'의 '3월'처럼
# 연도가 생략된 두 번째 창을 잡기 위한 것으로, 앞선 명시 연도를 상속할 때만 창이 된다.
_CAL_TOKEN_RE = re.compile(
    r"(?P<ymd>(?P<ymd_y>\d{4})\s*년\s*(?P<ymd_m>\d{1,2})\s*월\s*(?P<ymd_d>\d{1,2})\s*일)"
    r"|(?P<ymdd>(?P<ymdd_y>\d{4})[-./](?P<ymdd_m>\d{1,2})[-./](?P<ymdd_d>\d{1,2}))"
    r"|(?P<ym>(?P<ym_y>\d{4})\s*년\s*(?P<ym_m>\d{1,2})\s*월)"
    # 뒤에 일자 구분자가 없을 때만 '그 달 전체'다(2019-03-05 를 2019-03 으로 읽지 않기 위함).
    r"|(?P<ymd2>(?P<ymd2_y>\d{4})[-./](?P<ymd2_m>\d{1,2})(?![-./]?\d))"
    r"|(?P<yq>(?P<yq_y>\d{4})\s*년\s*(?P<yq_q>[1-4])\s*(?:사)?분기)"
    r"|(?P<yh>(?P<yh_y>\d{4})\s*년\s*(?P<yh_h>[상하])반기)"
    r"|(?P<y>(?P<y_y>\d{4})\s*년)"
    # 나열형 베어 연도('2018, 2019년'·'2018~2019년'의 앞쪽 '2018'). 뒤따르는 연도가 '년'을 달고 있을
    # 때만 창이 된다 — 접미어를 나열 뒤쪽에서 상속하는 문법으로, 연도 생략 월('2019년 2월과 3월'의
    # '3월')이 앞쪽 연도를 상속하는 것의 대칭이다. 앵커('년')가 없는 베어 숫자라 임의의 네 자리 수를
    # 연도로 오인하지 않도록 19/20/21 세기로 제한한다. 연결어까지 소비하되 다음 연도는 lookahead 로만
    # 본다(연쇄 나열 '2017, 2018, 2019년'도 각 토큰이 차례로 잡히도록).
    rf"|(?P<yb>(?P<yb_y>(?:19|20|21)\d{{2}}){_YEAR_ENUM_SEP}(?=(?:\d{{4}}{_YEAR_ENUM_SEP})*\d{{4}}\s*년))"
    r"|(?P<m>(?P<m_m>\d{1,2})\s*월)"
    r"|(?P<q>(?P<q_q>[1-4])\s*(?:사)?분기)"
    r"|(?P<h>(?P<h_h>[상하])반기)"
)
# 창 하나의 구체성 등급(작을수록 좁다). parse_calendar_window 가 '가장 좁은 표현' 하나를 고를 때 쓴다 —
# 여러 창이 섞인 문장에서 위치가 아니라 구체성으로 뽑던 기존 계약을 그대로 보존한다.
_GRAIN_RANK = {"ymd": 0, "ymdd": 0, "ym": 1, "ymd2": 1, "m": 1, "yq": 2, "q": 2, "yh": 3, "h": 3, "y": 4, "yb": 4}

# 창 두 개 '사이'의 문구가 나열 연결에 불과한지(= 두 창이 한 나열에 속하는지). 조사/연결어/구분자만
# 있으면 나열이고, 그 밖의 낱말(용언 등)이 끼면 서로 다른 조건의 창이다 — '2018년 및 2019년'은 나열,
# '2018년에 구매하고 2019년에 로그인한'은 나열이 아니다.
_ENUM_LINK_RE = re.compile(
    r"[\s,·/~∼\-–]*(?:" + "|".join(_ENUM_CONNECTORS) + r")?[\s,·/~∼\-–]*(?:년도|년)?[\s,·/~∼\-–]*"
)


def month_last_day(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def ymd(year: int, month: int, day: int) -> str:
    return f"{year:04d}{month:02d}{day:02d}"


def _window(start: str, end: str, label: str, suffix: str) -> dict[str, Any]:
    return {"from": start, "to": end, "label": f"{label} {suffix}".strip()}


# ── 연도 앵커(anchor) ─────────────────────────────────────────────────────────────
# 창은 '앵커 × 한정자'의 합성이다 — 앵커가 어느 해를 기준으로 삼을지 정하고(명시 4자리·상대 연도 어휘·
# 'N년 전'·생략=기준일), 한정자가 그 안을 좁힌다(월·분기·반기·일). 예전에는 앵커 종류마다 스캐너가
# 따로 살아 서로 만날 자리가 없었고('7년 전'은 relative_past 스캐너, '상반기'는 달력 토큰), 그래서
# '7년전 상반기'가 둘 중 하나만 남고 다른 하나는 조용히 사라졌다. 앵커를 한정자 앞의 공통 요소로
# 분리하면 조합이 코드가 아니라 합성에서 나온다 — 새 앵커 어휘는 표 한 줄이면 모든 한정자와 붙는다.
# '전부터/전까지/전 이후'는 시점이 아니라 그 시점을 경계로 삼는 범위다(fail-close). 과거 시점 스캐너와
# 앵커가 같은 규칙을 쓰도록 한 곳에서 소유한다 — 한쪽만 닫혀 있으면 같은 표현이 경로마다 다르게 읽힌다.
_PAST_POINT_BOUNDARY = r"(?!\s*(?:부터|까지|이후|이래|이전|보다))"
_YEAR_ANCHOR_PATTERN = (
    rf"(?:(?P<rel>{_RELATIVE_YEAR_ALTERNATION})(?:도)?|(?P<past>\d+)\s*년\s*전{_PAST_POINT_BOUNDARY})"
)
_YEAR_ANCHOR_RE = re.compile(_YEAR_ANCHOR_PATTERN)
# 한정자 바로 앞에 붙은 앵커만 본다 — 문장 반대편의 다른 조건 앵커를 끌어오지 않기 위함이다.
_ADJACENT_YEAR_ANCHOR_RE = re.compile(_YEAR_ANCHOR_PATTERN + r"\s*$")


def _anchor_year(match: "re.Match[str]", today: date) -> int:
    """앵커 표현 하나 → 절대 연도. 'N년 전'의 거슬러 세기는 _relative_past_target 이 단일 소유한다."""
    relative = match.group("rel")
    if relative is not None:
        return today.year + _RELATIVE_YEAR_OFFSETS[relative]
    return _relative_past_target(int(match.group("past")), "years", today).year


def _adjacent_anchor_year(text: str, start: int, today: date) -> tuple[int, int] | None:
    """달력 토큰 바로 앞의 연도 앵커를 (연도, 앵커 시작위치)로 읽는다('작년 상반기', '7년전 상반기').

    시작위치를 함께 주는 이유는 창의 원문 구간이 한정자만이 아니라 **앵커까지**여야 하기 때문이다 —
    구간이 한정자만 덮으면 앞의 '7년전'이 주인 없는 표현으로 남아 다른 슬롯이 다시 주워간다."""
    match = _ADJACENT_YEAR_ANCHOR_RE.search(text[:start])
    return (_anchor_year(match, today), match.start()) if match is not None else None


def _text_anchor_year(text: str, today: date) -> int | None:
    """텍스트 어딘가의 연도 앵커(첫 표현). 토큰 위치를 모르는 호출자(반기/분기 단독 파서)용."""
    match = _YEAR_ANCHOR_RE.search(text or "")
    return _anchor_year(match, today) if match is not None else None


def _is_quarter_duration(text: str, start: int, end: int) -> bool:
    """'최근/지난 2분기', '2분기 연속'의 2를 달력상 제2분기로 오인하지 않는다."""
    prefix = text[:start]
    suffix = text[end:]
    return (
        re.search(r"(?:최근|지난|향후|앞으로)\s*$", prefix) is not None
        or re.match(r"\s*(?:동안|간|연속)", suffix) is not None
    )


def parse_half_or_quarter_window(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> dict[str, Any] | None:
    """명시/상대/생략 연도의 상·하반기와 N분기를 절대 창으로 읽는다.

    연도가 없으면 현재 연도로 해석한다. '올해/금년'과 '작년/지난해/전년'은 기준일의 연도로
    확정한다. 그냥 '반기'(상/하 없음)나 숫자 없는 '분기'는 어느 반/분기인지 모호하므로 잡지 않는다."""
    reference = today or date.today()
    year_match = _ANY_YEAR_RE.search(text or "")
    y = (
        int(year_match.group(1))
        if year_match is not None
        else (_text_anchor_year(text or "", reference) or reference.year)
    )
    if "상반기" in text:
        return _window(ymd(y, 1, 1), ymd(y, 6, 30), f"{y}년 상반기", label_suffix)
    if "하반기" in text:
        return _window(ymd(y, 7, 1), ymd(y, 12, 31), f"{y}년 하반기", label_suffix)
    quarter = _QUARTER_RE.search(text)
    if quarter is not None and not _is_quarter_duration(text, quarter.start(), quarter.end()):
        q = int(quarter.group(1))
        start_month, end_month = QUARTER_MONTH_RANGES[q]
        return _window(
            ymd(y, start_month, 1),
            ymd(y, end_month, month_last_day(y, end_month)),
            f"{y}년 {q}분기",
            label_suffix,
        )
    return None


def _token_year(match: "re.Match[str]") -> int | None:
    """토큰이 스스로 명시한 연도(연도 생략 토큰이면 None)."""
    for group in ("ymd_y", "ymdd_y", "ym_y", "ymd2_y", "yq_y", "yh_y", "y_y", "yb_y"):
        value = match.group(group)
        if value is not None:
            return int(value)
    return None


def _token_window(match: "re.Match[str]", year: int | None, label_suffix: str) -> dict[str, Any] | None:
    """달력 토큰 하나 + 연도(생략 토큰은 상속받은 연도) → 절대 창. 달력상 불가능한 값이면 None."""
    if year is None:
        return None  # 연도를 끝내 못 정한 생략 토큰('3월' 단독)은 어느 해인지 모호 → 미해석
    if match.group("ymd") is not None or match.group("ymdd") is not None:
        korean = match.group("ymd") is not None
        mo = int(match.group("ymd_m") if korean else match.group("ymdd_m"))
        d = int(match.group("ymd_d") if korean else match.group("ymdd_d"))
        if not (1 <= mo <= 12 and 1 <= d <= month_last_day(year, mo)):
            return None
        label = f"{year}년 {mo}월 {d}일" if korean else f"{year}-{mo:02d}-{d:02d}"
        return _window(ymd(year, mo, d), ymd(year, mo, d), label, label_suffix)
    for month_group, label_fmt in (("ym_m", "{y}년 {m}월"), ("ymd2_m", "{y}-{m:02d}"), ("m_m", "{y}년 {m}월")):
        raw = match.group(month_group)
        if raw is not None:
            mo = int(raw)
            if not 1 <= mo <= 12:
                return None
            return _window(
                ymd(year, mo, 1), ymd(year, mo, month_last_day(year, mo)),
                label_fmt.format(y=year, m=mo), label_suffix,
            )
    quarter_raw = match.group("yq_q") or match.group("q_q")
    if quarter_raw is not None:
        q = int(quarter_raw)
        start_month, end_month = QUARTER_MONTH_RANGES[q]
        return _window(
            ymd(year, start_month, 1), ymd(year, end_month, month_last_day(year, end_month)),
            f"{year}년 {q}분기", label_suffix,
        )
    half_raw = match.group("yh_h") or match.group("h_h")
    if half_raw is not None:
        if half_raw == "상":
            return _window(ymd(year, 1, 1), ymd(year, 6, 30), f"{year}년 상반기", label_suffix)
        return _window(ymd(year, 7, 1), ymd(year, 12, 31), f"{year}년 하반기", label_suffix)
    return _window(ymd(year, 1, 1), ymd(year, 12, 31), f"{year}년", label_suffix)


def _scan_calendar_windows(
    text: str, label_suffix: str, today: date | None = None
) -> list[tuple[dict[str, Any], int, int, int]]:
    """텍스트의 모든 달력 토큰을 (창, 구체성등급, 시작위치, 끝위치) 로 스캔한다(등장 순).

    연도 생략 토큰('2019년 2월과 3월'의 '3월')은 앞서 나온 명시 연도를 상속한다. 바로 앞에
    '올해/작년' 같은 상대 연도 표지가 있으면 그 연도를 쓴다. 반기·분기는 연도가 끝내 없으면 현재
    연도로 확정한다. 월 단독은 숫자 오탐을 피하기 위해 기존처럼 연도 앵커가 있을 때만 창이 된다."""
    if not isinstance(text, str) or not text:
        return []
    matches = list(_CAL_TOKEN_RE.finditer(text))
    if not matches:
        return []
    fallback_year = next((y for y in (_token_year(m) for m in matches) if y is not None), None)
    reference = today or date.today()
    out: list[tuple[dict[str, Any], int, int, int]] = []
    running_year: int | None = None
    for match in matches:
        explicit = _token_year(match)
        if explicit is not None:
            running_year = explicit
        anchor = _adjacent_anchor_year(text, match.start(), reference)
        relative = anchor[0] if anchor is not None else None
        if relative is not None:
            running_year = relative
        quarter_duration = (
            match.group("q") is not None
            and _is_quarter_duration(text, match.start(), match.end())
        )
        inferred_current = (
            reference.year
            if (
                match.group("h") is not None
                or (match.group("q") is not None and not quarter_duration)
            )
            else None
        )
        year = explicit if explicit is not None else (relative or running_year or fallback_year or inferred_current)
        window = None if quarter_duration else _token_window(match, year, label_suffix)
        if window is not None:
            rank = next(
                (_GRAIN_RANK[name] for name in _GRAIN_RANK if match.group(name) is not None),
                len(_GRAIN_RANK),
            )
            # 앵커에서 연도를 받은 토큰의 구간은 앵커까지다('7년전 상반기' 전체가 한 창의 출처).
            start = anchor[1] if (explicit is None and anchor is not None) else match.start()
            out.append((window, rank, start, match.end()))
    return out


def parse_calendar_window_spans(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> list[tuple[dict[str, Any], int, int]]:
    """절대 달력 창 + 원문 내 (시작, 끝) 위치를 등장 순서대로 돌려준다.

    창 '사이'의 문구를 읽어야 하는 소비자용 — 예컨대 'A 대비 B'·'A보다 B' 의 어순 표지로 두 기간 중
    어느 쪽이 기준인지 판정할 때 쓴다. 위치 계산이 문법(토큰 스캔)에 딸린 정보라 이 모듈이 소유한다."""
    return [(window, start, end) for window, _rank, start, end in _scan_calendar_windows(text, label_suffix, today)]


def parse_calendar_windows(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> list[dict[str, Any]]:
    """텍스트에 나온 절대 달력 창을 등장 순서대로 전부 돌려준다(없으면 빈 리스트).

    '2019년 2월과 3월'(연도 상속), '2019년 1분기 대비 2분기', '2018년 12월과 2019년 1월'처럼 창이 둘
    이상인 표현을 소비하는 쪽(기간 대 기간 증감 비교 등)이 쓴다."""
    return [
        window
        for window, _start, _end in parse_calendar_window_spans(
            text, label_suffix=label_suffix, today=today
        )
    ]


def _calendar_group_range(scanned: list[tuple[dict[str, Any], int, int, int]], text: str) -> tuple[int, int]:
    """``parse_calendar_window_group`` 이 고르는 나열의 (첫 인덱스, 끝 인덱스). 선택 규칙의 단일 소스."""
    pivot = min(range(len(scanned)), key=lambda i: (scanned[i][1], scanned[i][2]))
    rank = scanned[pivot][1]

    def _linked(left: int, right: int) -> bool:
        """scanned[left] 와 scanned[right] 가 둘 다 같은 구체성이면서 나열로 이어져 있는지."""
        return (
            scanned[left][1] == rank
            and scanned[right][1] == rank
            and _ENUM_LINK_RE.fullmatch(text[scanned[left][3]:scanned[right][2]]) is not None
        )

    first = pivot
    while first - 1 >= 0 and _linked(first - 1, first):
        first -= 1
    last = pivot
    while last + 1 < len(scanned) and _linked(last, last + 1):
        last += 1
    return first, last


def parse_calendar_window_group_span(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> tuple[int, int] | None:
    """``parse_calendar_window_group`` 이 고른 나열 전체가 차지하는 원문 구간 (시작, 끝).

    창을 소비한 슬롯의 **출처 구간**을 기록하려는 쪽이 쓴다(slot_ownership) — 소유권 회수가
    '같은 종류'가 아니라 '같은 구간'으로 판정되게 하려면 문법 소유자가 위치도 함께 줘야 한다."""
    scanned = _scan_calendar_windows(text, label_suffix, today)
    if not scanned:
        return None
    first, last = _calendar_group_range(scanned, text)
    return scanned[first][2], scanned[last][3]


def parse_calendar_window_span(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> tuple[int, int] | None:
    """``parse_calendar_window`` 가 고르는 창 하나의 원문 구간 (시작, 끝)."""
    scanned = _scan_calendar_windows(text, label_suffix, today)
    if not scanned:
        return None
    chosen = min(scanned, key=lambda item: (item[1], item[2]))
    return chosen[2], chosen[3]


def parse_calendar_window_group(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> list[dict[str, Any]]:
    """'가장 좁은 창' + 그와 **한 나열로 이어진** 같은 구체성의 창들을 등장 순서대로 돌려준다.

    ``parse_calendar_window`` 의 일반화다. 단일 창 계약('가장 좁은 표현 하나')을 그대로 유지하되,
    그 창이 나열의 일원이면 나열 전체를 돌려준다 — '2018년 및 2019년'·'2018, 2019년'·'2019년 2월과
    3월'은 한 조건의 두 구간이지 두 조건이 아니기 때문이다. 하나만 골라 쓰면 나머지 구간이 조용히
    사라져 '2018·2019년 합계'가 '2018년 합계'가 된다.

    나열 판정은 위치로 한다 — 창 사이 문구가 연결어/조사/구분자뿐일 때만 같은 나열이다. 서로 다른
    조건이 각자 창을 가진 문장('2018년에 구매하고 2019년에 로그인한')은 나열이 아니므로 뭉치지 않는다.
    구체성이 다른 창(연 vs 월)도 섞지 않는다 — '2019년 3월 … 2018년'은 여전히 가장 좁은 3월 하나다.
    """
    scanned = _scan_calendar_windows(text, label_suffix, today)
    if not scanned:
        return []
    first, last = _calendar_group_range(scanned, text)
    return [scanned[index][0] for index in range(first, last + 1)]


def parse_calendar_window(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> dict[str, Any] | None:
    """절대 달력 표현 하나를 YYYYMMDD 창 ``{from, to, label}`` 으로 읽는다(없으면 None).

    지원: 'YYYY년 M월 D일'(하루), 'YYYY년 M월'(그 달 전체), 'YYYY년'(그 해 전체),
          'YYYY-MM-DD'/'YYYY.MM.DD'/'YYYY/MM/DD'(하루), 'YYYY-MM'(그 달 전체),
          'YYYY년/올해/작년 상반기·하반기'(6개월), 연도 생략 상·하반기(현재 연도),
          'YYYY년/올해/작년 N분기'(3개월), 연도 생략 N분기(현재 연도).

    창이 여럿이면 가장 좁은 표현을 고른다 — 일 > 월 > 분기 > 반기 > 연(동급이면 먼저 나온 것).
    순서가 뒤집히면 'YYYY년 M월'이 연 전체로 뭉개진다.
    ``label_suffix`` 는 라벨 꼬리말(예: '구매')로, 호출자의 도메인 문맥을 라벨에만 반영한다.
    """
    scanned = _scan_calendar_windows(text, label_suffix, today)
    if not scanned:
        return None
    return min(scanned, key=lambda item: (item[1], item[2]))[0]  # (구체성 등급, 등장 위치)


def calendar_window_from_parts(
    year: Any, month: Any = None, quarter: Any = None, half: Any = None
) -> dict[str, Any] | None:
    """연/월/분기/반기 숫자 조합을 절대 창으로 만든다(구조화된 입력용 — LLM 슬롯 등).

    자연어 파싱을 거치지 않는 호출자도 같은 달력 규칙(월말일·분기 범위)을 쓰게 해, 텍스트 경로와
    슬롯 경로가 서로 다른 창을 내는 일이 없게 한다."""
    if not isinstance(year, int) or isinstance(year, bool) or not 1900 < year < 3000:
        return None
    if isinstance(month, int) and not isinstance(month, bool) and 1 <= month <= 12:
        return _window(ymd(year, month, 1), ymd(year, month, month_last_day(year, month)), f"{year}년 {month}월", "")
    if isinstance(quarter, int) and not isinstance(quarter, bool) and quarter in QUARTER_MONTH_RANGES:
        start_month, end_month = QUARTER_MONTH_RANGES[quarter]
        return _window(
            ymd(year, start_month, 1),
            ymd(year, end_month, month_last_day(year, end_month)),
            f"{year}년 {quarter}분기",
            "",
        )
    if isinstance(half, int) and not isinstance(half, bool) and half in (1, 2):
        if half == 1:
            return _window(ymd(year, 1, 1), ymd(year, 6, 30), f"{year}년 상반기", "")
        return _window(ymd(year, 7, 1), ymd(year, 12, 31), f"{year}년 하반기", "")
    return _window(ymd(year, 1, 1), ymd(year, 12, 31), f"{year}년", "")


# ── 상대 기간 창 ──────────────────────────────────────────────────────────────────

# 한글 기간 단위 → 캐노니컬 영문 단위(슬롯 정규화용). 일수 환산은 targeting_ir.UNIT_DAYS 가 소유한다.
KO_UNIT_TO_CANON = {"일": "days", "주": "weeks", "주일": "weeks", "개월": "months", "달": "months", "년": "years"}
# 캐노니컬 단위 → 라벨용 한글 단위(파싱의 역방향. 단어형 '일주일'도 정규화 후 '7일'로 읽힌다).
CANON_TO_KO_UNIT = {"days": "일", "weeks": "주", "months": "개월", "years": "년"}
# 기간 표현 → 일수. 숫자형('7일', '2주')과 숫자 없는 한글 단어형('일주일', '보름', '한 달')을 모두 본다.
# 단어형은 숫자가 없어서 재작성 가드의 숫자 서명에도, 기존 '최근 N일' 파서에도 안 잡혔다.
# 한글토큰→일수는 토큰→canonical(KO_UNIT_TO_CANON)과 canonical→일수(targeting_ir.UNIT_DAYS)의 합성으로
# 파생한다 — 별도 한글 일수표를 두지 않아, 새 단위는 KO_UNIT_TO_CANON(+targeting_ir.UNIT_DAYS)만 고치면 된다.
DURATION_UNIT_DAYS = {ko: targeting_ir.UNIT_DAYS[canon] for ko, canon in KO_UNIT_TO_CANON.items()}
NUMERIC_DURATION_PATTERN = re.compile(r"(?P<num>\d+)\s*(?P<unit>주일|개월|일|주|달|년)")
WORD_DURATION_DAYS = {
    "일주일": 7, "한주일": 7, "한주": 7, "일주": 7,
    "이주일": 14, "두주일": 14, "두주": 14,
    "삼주일": 21, "세주일": 21, "세주": 21,
    "보름": 15,
    "한달": 30, "한개월": 30,
    "두달": 60, "두개월": 60,
    "석달": 90, "세달": 90, "세개월": 90,
    "반년": 180, "일년": 365, "한해": 365, "한햇": 365,
}
WORD_DURATION_PATTERN = re.compile("|".join(sorted(map(re.escape, WORD_DURATION_DAYS), key=len, reverse=True)))

# 앵커어와 기간 표현 사이 허용 간격(공백 제거 기준). '6개월동안로그인'(동안=2), '1년이내가입'(이내=2)은
# 붙은 것으로 보고, 프롬프트 반대편의 다른 조건 창은 배제한다.
DURATION_ANCHOR_GAP = 8


def duration_window_candidates(compact: str) -> list[tuple[int, int, int, str]]:
    """공백 제거 텍스트의 기간 표현을 (시작, 끝, value, canonical_unit) 목록으로(등장 순). 단어형은 unit=days."""
    out: list[tuple[int, int, int, str]] = []
    for match in NUMERIC_DURATION_PATTERN.finditer(compact):
        value = int(match.group("num"))
        # 2019년/2026년은 달력 연도이지 2019년 길이의 롤링 창이 아니다. 이를 기간으로 잡으면
        # DATEADD(DAY, -736935, ...) 같은 비정상 조건이 절대 날짜 범위와 함께 생성된다.
        if value > 0 and not (match.group("unit") == "년" and 1900 <= value <= 2199):
            out.append((match.start(), match.end(), value, KO_UNIT_TO_CANON.get(match.group("unit"), "days")))
    for match in WORD_DURATION_PATTERN.finditer(compact):
        out.append((match.start(), match.end(), WORD_DURATION_DAYS[match.group(0)], "days"))
    return sorted(out)


def parse_duration_window(
    query: str,
    *,
    require_number: bool = True,
    default_days: int | None = None,
    exclude_past: bool = False,
    anchor_terms: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """통합 기간 창 파서 — 숫자형(3개월/2주/1년)·단어형(일주일/반년/한달)을 모두 잡아 정규 shape로 돌려준다.

    반환 {value, unit(∈days/weeks/months/years), min_days}. 파편화된 슬롯별 창 파서(가입/로그인/미구매/
    미접속)가 각자 다른 단위 부분집합만 지원해 '1년 이내 가입'·'반년 미구매' 같은 표현을 놓치던 것을
    한 곳으로 모은다. 문맥 게이트(가입 신호/로그인 신호/부정어)는 호출자가 유지한다.

    anchor_terms 를 주면 그 앵커어 근처(±DURATION_ANCHOR_GAP)의 기간만 본다 — 여러 조건이 각자 창을
    가진 프롬프트('최근 1년 이내 가입 … 최근 로그인')에서 로그인 창이 가입의 '1년'을 훔쳐가는 조건 간
    창 충돌을 막는다(앵커가 하나도 없으면 전체에서 첫 창으로 폴백). exclude_past=True 면 'N개월 전'을 건너뛴다."""
    compact = query.replace(" ", "").casefold()
    candidates = duration_window_candidates(compact)
    if exclude_past:
        candidates = [c for c in candidates if compact[c[1]:c[1] + 1] != "전"]
    if anchor_terms:
        anchor_spans = [
            (match.start(), match.end())
            for term in anchor_terms
            for match in re.finditer(re.escape(term), compact)
        ]
        if anchor_spans:
            def _near(cand: tuple[int, int, int, str]) -> bool:
                start, end = cand[0], cand[1]
                return any(
                    max(start, a_start) - min(end, a_end) <= DURATION_ANCHOR_GAP
                    for a_start, a_end in anchor_spans
                )
            candidates = [c for c in candidates if _near(c)]
    if candidates:
        _s, _e, value, unit = candidates[0]
        return {"value": value, "unit": unit, "min_days": value * targeting_ir.UNIT_DAYS[unit]}
    if not require_number and default_days:
        return {"value": default_days, "unit": "days", "min_days": default_days}
    return None


def relative_window_label(window: dict[str, Any]) -> str:
    """상대 창의 한글 라벨('최근 3개월'). 단어형은 정규화된 일수로 적는다('일주일' → '최근 7일')."""
    unit = CANON_TO_KO_UNIT.get(str(window.get("unit")), "일")
    return f"최근 {window.get('value')}{unit}"


# ── 상대 과거 시점 창 ─────────────────────────────────────────────────────────────
# 'N년/개월/주/일 전'은 기간의 **길이**가 아니라 과거의 한 **시점**이다. 롤링 창 파서
# (parse_duration_window)는 이 형태를 exclude_past 로 건너뛸 뿐 아무도 읽지 않아, '7년전 구매한 고객'의
# '7년전'이 조용히 사라졌다(창 없는 전 기간 구매로 컴파일 → 조건 소실).
#
# 시점을 가리키는 단위가 곧 창의 구체성이다: '7년 전'=그 해 전체, '3개월 전'=그 달 전체, '2주 전'=그 주
# (월~일), '10일 전'=그 날 하루. 절대 창과 같은 shape({from,to,label})로 돌려주므로 소비자(구매일 술어
# 등)는 절대 창과 구분 없이 쓴다 — 기준일이 계획 수립 시점에 확정되므로 계획에 날짜가 그대로 드러난다.
#
# '전부터/전까지/전 이후'는 시점이 아니라 그 시점을 **경계로 삼는 범위**라 여기서 잡지 않는다
# (fail-close — 경계 어휘는 _PAST_POINT_BOUNDARY 가 앵커와 공유한다).
RELATIVE_PAST_PATTERN = re.compile(
    rf"(?P<num>\d+)\s*(?P<unit>주일|개월|년|달|주|일)\s*전{_PAST_POINT_BOUNDARY}"
)


def _relative_past_target(value: int, unit: str, today: "date") -> "date":
    """기준일에서 value 단위만큼 거슬러 올라간 날짜(월/년은 말일 넘침을 그 달 말일로 자른다)."""
    if unit == "years":
        year, month = today.year - value, today.month
    elif unit == "months":
        total = today.year * 12 + (today.month - 1) - value
        year, month = total // 12, total % 12 + 1
    else:
        return today - timedelta(days=value * (7 if unit == "weeks" else 1))
    return date(year, month, min(today.day, month_last_day(year, month)))


def relative_past_window(
    value: int, unit: str, *, today: "date | None" = None, label_suffix: str = ""
) -> dict[str, Any] | None:
    """'value 단위 전' 시점이 속한 달력 구간을 절대 창 {from,to,label} 으로 만든다.

    구조화된 입력(LLM 슬롯 등)도 텍스트 경로와 같은 규칙을 쓰게 하는 진입점이다
    (calendar_window_from_parts 가 절대 창에 대해 하는 역할과 같다)."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    if unit not in ("years", "months", "weeks", "days"):
        return None
    anchor = today or date.today()
    target = _relative_past_target(value, unit, anchor)
    if unit == "years":
        return _window(ymd(target.year, 1, 1), ymd(target.year, 12, 31), f"{target.year}년", label_suffix)
    if unit == "months":
        last = month_last_day(target.year, target.month)
        return _window(
            ymd(target.year, target.month, 1), ymd(target.year, target.month, last),
            f"{target.year}년 {target.month}월", label_suffix,
        )
    if unit == "weeks":
        start = target - timedelta(days=target.weekday())  # 그 주 월요일
        end = start + timedelta(days=6)
        return _window(
            ymd(start.year, start.month, start.day), ymd(end.year, end.month, end.day),
            f"{start.year}년 {start.month}월 {start.day}일~{end.month}월 {end.day}일", label_suffix,
        )
    return _window(
        ymd(target.year, target.month, target.day), ymd(target.year, target.month, target.day),
        f"{target.year}년 {target.month}월 {target.day}일", label_suffix,
    )


def _scan_relative_past_windows(
    text: str, today: "date | None", label_suffix: str
) -> list[tuple[dict[str, Any], int, int]]:
    """'N단위 전' 표현을 (창, 시작, 끝) 으로 등장 순서대로 스캔한다."""
    out: list[tuple[dict[str, Any], int, int]] = []
    for match in RELATIVE_PAST_PATTERN.finditer(text or ""):
        unit = KO_UNIT_TO_CANON.get(match.group("unit"))
        window = relative_past_window(int(match.group("num")), unit or "", today=today, label_suffix=label_suffix)
        if window is not None:
            out.append((window, match.start(), match.end()))
    return out


def parse_relative_past_window(
    text: str, *, today: "date | None" = None, label_suffix: str = ""
) -> dict[str, Any] | None:
    """텍스트의 'N단위 전'(첫 표현)을 절대 창으로 읽는다(없으면 None).

    도메인 게이트('무엇에 대한 언제'인가)는 호출자가 소유한다 — 이 모듈은 '언제'만 읽는다."""
    scanned = _scan_relative_past_windows(text, today, label_suffix)
    return scanned[0][0] if scanned else None


def parse_relative_past_window_span(text: str) -> tuple[int, int] | None:
    """``parse_relative_past_window`` 가 읽은 표현의 원문 구간 (시작, 끝)."""
    scanned = _scan_relative_past_windows(text, None, "")
    return (scanned[0][1], scanned[0][2]) if scanned else None


# ── 창 파싱 단일 진입점 ────────────────────────────────────────────────────────────
# 창의 **종류 간 우선순위**(절대 달력 → 상대 과거 시점 → 롤링 기간)도 문법의 일부다. 이 순서가
# 소비자마다 복제돼 있던 동안, 새 창 종류가 생길 때마다 각 체인의 순서를 따로 정해야 했고 실제로
# 한쪽만 고쳐진 채로 결과가 뒤집혔다('7년전 상반기'가 어느 체인을 타느냐에 따라 2019년 전체 또는
# 올해 상반기). 순서는 여기 한 곳에 있고, 호출자는 '무엇에 대한 언제인가'라는 도메인 정책만 플래그로
# 넘긴다 — 순수 모듈 불변식(도메인 게이트는 호출자 소유)은 그대로다.


def parse_time_windows(
    text: str,
    *,
    label_suffix: str = "",
    today: "date | None" = None,
    allow_relative_past: bool = True,
    include_duration: bool = False,
    duration_anchor_terms: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """텍스트의 기간 표현을 창 목록으로 읽는다(없으면 빈 목록).

    절대 달력 창이 있으면 그 나열 전체를 돌려준다(구간이 조용히 사라지지 않게). 없을 때만 상대 과거
    시점('7년 전')을, 그것도 없을 때만 롤링 기간('최근 3개월')을 본다.

    ``allow_relative_past`` 는 호출자의 도메인 판단이다 — 문장에 다른 도메인의 날짜 앵커(가입·로그인
    …)가 있어 그 시점이 이 조건의 것이라고 단정할 수 없으면 False 로 닫는다.
    ``include_duration=True`` 인 소비자만 롤링 기간을 받는다(반환 shape 이 {days,label} 로 다르다).
    """
    group = parse_calendar_window_group(text, label_suffix=label_suffix, today=today)
    if group:
        return group
    if allow_relative_past:
        past = parse_relative_past_window(text, today=today, label_suffix=label_suffix)
        if past is not None:
            return [past]
    if include_duration:
        relative = parse_duration_window(text, anchor_terms=duration_anchor_terms)
        if relative is not None:
            return [{"days": relative["min_days"], "label": relative_window_label(relative)}]
    return []


def parse_time_window(
    text: str,
    *,
    label_suffix: str = "",
    today: "date | None" = None,
    allow_relative_past: bool = True,
    include_duration: bool = False,
    duration_anchor_terms: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """``parse_time_windows`` 의 단일 창 버전(나열이면 첫 구간)."""
    windows = parse_time_windows(
        text,
        label_suffix=label_suffix,
        today=today,
        allow_relative_past=allow_relative_past,
        include_duration=include_duration,
        duration_anchor_terms=duration_anchor_terms,
    )
    return windows[0] if windows else None


def parse_time_window_span(
    text: str, *, today: "date | None" = None, allow_relative_past: bool = True
) -> tuple[int, int] | None:
    """``parse_time_window`` 가 읽은 표현의 원문 구간. 롤링 기간은 위치를 단정하지 않는다(None).

    절대 창의 구간은 앵커까지 포함한다 — '7년전 상반기'는 '상반기'가 아니라 표현 전체가 출처다."""
    span = parse_calendar_window_span(text, today=today)
    if span is not None:
        return span
    return parse_relative_past_window_span(text) if allow_relative_past else None


def parse_time_window_group_span(
    text: str, *, today: "date | None" = None, allow_relative_past: bool = True
) -> tuple[int, int] | None:
    """``parse_time_windows`` 가 읽은 **나열 전체**의 원문 구간(창 목록을 통째로 소비하는 슬롯용)."""
    span = parse_calendar_window_group_span(text, today=today)
    if span is not None:
        return span
    return parse_relative_past_window_span(text) if allow_relative_past else None
