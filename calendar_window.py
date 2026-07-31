"""기간 표현(창)의 단일 소유 모듈 — 절대 달력 창과 상대 기간 창을 한 문법으로 읽는다.

배경: 창 파싱이 모듈마다 따로 살아 있었다. ``entity_set`` 은 ``(\\d{4})년`` 하나만 알아 '2019년 3월'을
2019년 전체로 뭉갰고(3월 베스트셀러가 조용히 연간 베스트셀러가 된다), ``targeting_expression`` 의 LLM
스키마에는 ``year`` 정수 필드뿐이라 월·분기·반기는 애초에 표현할 수단이 없었다. 완전한 달력 문법은
``graph_rag`` 안에만 있었지만 그쪽은 순수 모듈이 아니라 재사용이 불가능했다. 표현형이 하나 늘 때마다
세 곳을 각자 고쳐야 하는 구조 자체가 결함이고, 실제로 한쪽만 고쳐진 상태로 오래 있었다.

이 모듈이 그 문법의 유일한 소유자다. 새 표현(예: 'YYYY년 M월 상순')은 여기 한 곳에 추가하면 규칙
파서·LLM 라우트·구매일 타겟이 동시에 얻는다.

    절대 창 := {from, to, label[, from_time, to_time]}  # 달력상 확정된 구간. YYYYMMDD CHAR(8) 비교용.
    상대 창 := {value, unit, min_days}                   # 기준일로부터 거슬러 세는 구간.

    시각(from_time/to_time, HHMMSS)은 일 단위 창에 시각 한정자('9시부터')가 붙었을 때만 실린다 —
    날짜만 있는 창은 기존 shape 그대로라 시각을 모르는 소비자와 호환된다.

한 문장에 창이 둘 이상 나오는 표현('2019년 2월과 3월', '2019년 1분기 대비 2분기')도 이 문법이 소유한다 —
parse_calendar_windows 가 등장 순서대로 전부 돌려주고, parse_calendar_window 는 그중 하나를 고르는
얇은 래퍼다. 기간 대 기간 비교(증감) 같은 다중 창 소비자는 전자를 쓴다.

창이 이어져 나올 때 **그 사이의 링크에는 종류가 있다**(_link_kind). 나열('2018년 및 2019년')은 서로 다른
두 구간의 합집합이지만, 범위('2019년 3월부터 2020년 5월까지')는 시작과 끝만 준 **하나의 연속 구간**이다.
범위는 스캔 단계에서 창 하나로 접어서 내보내므로 소비자는 원래 창이 몇 개였는지 알 필요가 없다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 도메인 게이트(구매 신호 여부 등)와 물리 매핑은
호출자가 소유한다 — 이 모듈은 '언제'만 읽고 '무엇에 대한 언제'인지는 모른다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, NamedTuple

import lexicon_patterns
import slot_ownership
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

# 창이 이어져 나올 때 그 사이에 오는 토큰. 낱말은 사전(parser_lexicon.json)이 소유하고 여기에는 조합
# 구조만 둔다 — 새 연결 표현은 데이터 한 줄이면 토큰 스캐너와 링크 판정이 함께 얻는다.
_ENUM_CONNECTOR_ALT = lexicon_patterns.alternation("enum_connective")
_RANGE_OPENER_ALT = lexicon_patterns.alternation("range_opener")
_RANGE_CLOSER_ALT = lexicon_patterns.alternation("range_closer")
# 낱말이 아닌 구분자. 나열('2018, 2019년')과 범위('3월~5월')는 뜻이 달라 문자 집합부터 나눈다.
_ENUM_SEP_CHARS = r",·/"
_RANGE_SEP_CHARS = r"~∼\-–"
# 나열형 베어 연도 문법(_CAL_TOKEN_RE 의 yb)이 쓰는 구분자 — 나열이든 범위든 '연도가 이어진다'는
# 신호라 둘 다 받는다. 어느 쪽인지는 링크 판정(_link_kind)이 정한다.
_YEAR_ENUM_SEP = rf"(?:\s*[{_ENUM_SEP_CHARS}{_RANGE_SEP_CHARS}]\s*|\s*(?:{_ENUM_CONNECTOR_ALT})\s*)"

def _time_suffix_pattern(prefix: str) -> str:
    """일 단위 토큰 뒤에 붙는 시각 한정자('9시', '오후 6시 30분'). 통째로 선택적이다.

    '시간'은 시각이 아니라 기간이므로 lookahead 로 배제한다('3시간 이내'의 '3시'를 시각으로 오인하면
    기간 표현이 반쪽 남는다). 시각은 일 단위 토큰에만 붙는다 — 날짜 없는 시각 단독('9시 이후 주문')은
    어느 날의 9시인지 창으로 확정할 수 없어 잡지 않는다(fail-close)."""
    return (
        rf"(?:\s*(?:(?P<{prefix}_ap>오전|오후)\s*)?(?P<{prefix}_hh>\d{{1,2}})\s*시(?!간)"
        rf"(?:\s*(?P<{prefix}_mi>\d{{1,2}})\s*분)?)?"
    )


# 달력 토큰 스캐너(단일 정규식, 좁은 표현 우선 순서). 파이썬 정규식은 같은 시작 위치에서 앞선 대안을
# 먼저 채택하므로, 이 열거 순서가 곧 '일 > 월 > 분기 > 반기 > 연' 구체성 우선순위다 — '2019년 3월'이
# 연 전체로 뭉개지지 않는다. 뒤쪽 대안들(연도 생략 월일/월/분기/반기)은 '2019년 2월과 3월'의 '3월'처럼
# 연도가 생략된 두 번째 창을 잡기 위한 것으로, 앞선 명시 연도를 상속할 때만 창이 된다.
_CAL_TOKEN_RE = re.compile(
    r"(?P<ymd>(?P<ymd_y>\d{4})\s*년\s*(?P<ymd_m>\d{1,2})\s*월\s*(?P<ymd_d>\d{1,2})\s*일"
    + _time_suffix_pattern("ymd") + r")"
    r"|(?P<ymdd>(?P<ymdd_y>\d{4})[-./](?P<ymdd_m>\d{1,2})[-./](?P<ymdd_d>\d{1,2})"
    + _time_suffix_pattern("ymdd") + r")"
    r"|(?P<ym>(?P<ym_y>\d{4})\s*년\s*(?P<ym_m>\d{1,2})\s*월)"
    # 뒤에 일자 구분자가 없을 때만 '그 달 전체'다(2019-03-05 를 2019-03 으로 읽지 않기 위함).
    r"|(?P<ymd2>(?P<ymd2_y>\d{4})[-./](?P<ymd2_m>\d{1,2})(?![-./]?\d))"
    # 연도 생략 월+일('7월 1일부터 7월 31일까지'의 '7월 31일'). 연도 생략 월('3월')이 앞선 명시 연도를
    # 상속하는 문법의 일 단위 대칭이다 — 이 대안이 없으면 '7월 31일'이 '7월'(월 전체)로 잡히고 '31일'이
    # 주인 없는 표현으로 남아 범위 접기가 실패한다. m 보다 앞에 둬야 같은 시작 위치에서 일 단위가 이긴다.
    rf"|(?P<md>(?P<md_m>\d{{1,2}})\s*월\s*(?P<md_d>\d{{1,2}})\s*일{_time_suffix_pattern('md')})"
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
_GRAIN_RANK = {"ymd": 0, "ymdd": 0, "md": 0, "ym": 1, "ymd2": 1, "m": 1, "yq": 2, "q": 2, "yh": 3, "h": 3, "y": 4, "yb": 4}
_MONTH_GRAIN_RANK = _GRAIN_RANK["ym"]

# 창 두 개 '사이'의 문구가 무슨 링크인지. 조사/연결어/구분자만 있으면 링크이고, 그 밖의 낱말(용언 등)이
# 끼면 서로 다른 조건의 창이다 — '2018년 및 2019년'은 링크, '2018년에 구매하고 2019년에 로그인한'은
# 링크가 아니다. 링크에는 **종류**가 있다: 나열은 두 구간의 합집합이고, 범위는 시작·끝만 준 하나의 연속
# 구간이다. 이 구분이 없던 동안 범위는 조용히 다른 뜻이 됐다 — '2019년 3월~5월'이 3월 OR 5월(4월 누락)로
# 컴파일되고, '2019년부터 2020년까지'는 뒤쪽 창이 주인을 못 찾아 확인 질문으로 막혔다.
_ENUM_LINK_RE = re.compile(
    rf"[\s{_ENUM_SEP_CHARS}]*(?:{_ENUM_CONNECTOR_ALT})?[\s{_ENUM_SEP_CHARS}]*"
    rf"(?:년도|년)?[\s{_ENUM_SEP_CHARS}]*"
)
# 범위 링크 두 형태: 구분자형('3월~5월', '3월-5월')과 여는 말형('3월부터 …', '3월에서 …').
_RANGE_SEP_LINK_RE = re.compile(rf"\s*[{_RANGE_SEP_CHARS}]\s*(?:{_RANGE_OPENER_ALT})?\s*")
_RANGE_OPEN_LINK_RE = re.compile(rf"\s*(?:년도|년)?\s*(?:{_RANGE_OPENER_ALT})\s*")
# 닫는 말은 오른쪽 창 **뒤**에 온다. 여는 말만 있고 닫는 말이 없으면 경계가 반쪽이라 범위로 읽지 않는다
# ('2019년부터 2020년' → fail-close). 접두 일치라 '사이에'·'까지의'도 닫는 말로 본다.
_RANGE_CLOSER_RE = re.compile(rf"\s*(?:{_RANGE_CLOSER_ALT})")


def month_last_day(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def ymd(year: int, month: int, day: int) -> str:
    return f"{year:04d}{month:02d}{day:02d}"


def _window(
    start: str, end: str, label: str, suffix: str,
    from_time: str | None = None, to_time: str | None = None,
) -> dict[str, Any]:
    """절대 창 dict. 시각(HHMMSS)은 일 단위 창에 시각 한정자가 붙었을 때만 실린다 — 키 자체가
    없으면 기존 {from,to,label} shape 그대로라, 시각을 모르는 소비자·스냅샷과 호환된다."""
    out: dict[str, Any] = {"from": start, "to": end, "label": f"{label} {suffix}".strip()}
    if from_time is not None:
        out["from_time"] = from_time
    if to_time is not None:
        out["to_time"] = to_time
    return out


# ── 연도 앵커(anchor) ─────────────────────────────────────────────────────────────
# 창은 '앵커 × 한정자'의 합성이다 — 앵커가 어느 해를 기준으로 삼을지 정하고(명시 4자리·상대 연도 어휘·
# 'N년 전'·생략=기준일), 한정자가 그 안을 좁힌다(월·분기·반기·일). 예전에는 앵커 종류마다 스캐너가
# 따로 살아 서로 만날 자리가 없었고('7년 전'은 relative_past 스캐너, '상반기'는 달력 토큰), 그래서
# '7년전 상반기'가 둘 중 하나만 남고 다른 하나는 조용히 사라졌다. 앵커를 한정자 앞의 공통 요소로
# 분리하면 조합이 코드가 아니라 합성에서 나온다 — 새 앵커 어휘는 표 한 줄이면 모든 한정자와 붙는다.
# '전부터/전까지/전 이후'는 시점이 아니라 그 시점을 경계로 삼는 범위다(fail-close). 과거 시점 스캐너와
# 앵커가 같은 규칙을 쓰도록 한 곳에서 소유한다 — 한쪽만 닫혀 있으면 같은 표현이 경로마다 다르게 읽힌다.
#
# 그 판정을 어휘 표 하나에서 파생한다: 표가 (a) 앵커·과거 시점 패턴의 lookahead 와 (b) 기간 표현의
# **의미 종류**(:data:`TEMPORAL_KINDS`) 분류를 동시에 만든다. 예전에는 같은 구분이 정규식 lookahead 와
# ``compact[end] == "전"`` 문자 검사로 두 곳에 따로 살았고, 그래서 '7년 전'이 한쪽에서는 시점, 다른
# 쪽에서는 '최근 7년' 롤링 기간으로 읽혀 한 어구가 시간 조건 두 개를 만들었다.
KIND_PAST_POINT = "past_point"            # 'N단위 전' — 과거의 한 시점(그 단위의 달력 구간)
KIND_ROLLING = "rolling_duration"         # '최근 N단위' — 기준일에서 거슬러 세는 기간
KIND_BOUNDARY_FROM = "boundary_from"      # 'N단위 전부터/이후/이래' — 그 시점을 시작 경계로 삼는 범위
KIND_BOUNDARY_UNTIL = "boundary_until"    # 'N단위 전까지/이전/보다' — 그 시점을 끝 경계로 삼는 범위
PAST_BOUNDARY_KINDS: dict[str, str] = {
    "부터": KIND_BOUNDARY_FROM,
    "까지": KIND_BOUNDARY_UNTIL,
    "이후": KIND_BOUNDARY_FROM,
    "이래": KIND_BOUNDARY_FROM,
    "이전": KIND_BOUNDARY_UNTIL,
    "보다": KIND_BOUNDARY_UNTIL,
}
TEMPORAL_KINDS = frozenset({KIND_PAST_POINT, KIND_ROLLING, KIND_BOUNDARY_FROM, KIND_BOUNDARY_UNTIL})
_PAST_BOUNDARY_ALT = "|".join(PAST_BOUNDARY_KINDS)
_PAST_POINT_BOUNDARY = rf"(?!\s*(?:{_PAST_BOUNDARY_ALT}))"
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


# 시각 한정자가 문법상 잡혔지만 달력상 불가능한 값('25시', '9시 75분')임을 알리는 표지 — 시각만 조용히
# 버리고 날짜 창을 만들면 의미가 넓어진 채 실행되므로, 창 전체를 미해석으로 남긴다(fail-close).
_INVALID_TIME = object()


def _token_time(match: "re.Match[str]", prefix: str) -> tuple[str, str, str] | None | object:
    """일 단위 토큰에 붙은 시각 한정자 → (구간 시작 HHMMSS, 구간 끝 HHMMSS, 라벨 조각).

    시각 토큰은 그 단위 전체 구간을 뜻한다 — '9시'는 09:00:00~09:59:59, '9시 30분'은 09:30:00~09:30:59.
    날짜의 '7월까지'가 7월 말일까지를 포함하는 것과 같은 단위 의미론이다. 범위 합성(_merge_range)이
    왼쪽 창의 시작 시각과 오른쪽 창의 끝 시각만 취하므로 '9시부터 18시까지'는 09:00:00~18:59:59 가 된다."""
    hh = match.group(f"{prefix}_hh")
    if hh is None:
        return None
    meridiem = match.group(f"{prefix}_ap")
    minute_raw = match.group(f"{prefix}_mi")
    hour = int(hh)
    if meridiem is not None:
        if not 1 <= hour <= 12:
            return _INVALID_TIME
        if meridiem == "오후":
            hour = 12 if hour == 12 else hour + 12
        else:
            hour = 0 if hour == 12 else hour
    elif hour > 23:
        return _INVALID_TIME
    minute = int(minute_raw) if minute_raw is not None else None
    if minute is not None and minute > 59:
        return _INVALID_TIME
    label = f"{meridiem + ' ' if meridiem else ''}{int(hh)}시" + (f" {minute}분" if minute is not None else "")
    if minute is not None:
        return (f"{hour:02d}{minute:02d}00", f"{hour:02d}{minute:02d}59", label)
    return (f"{hour:02d}0000", f"{hour:02d}5959", label)


def _token_window(match: "re.Match[str]", year: int | None, label_suffix: str) -> dict[str, Any] | None:
    """달력 토큰 하나 + 연도(생략 토큰은 상속받은 연도) → 절대 창. 달력상 불가능한 값이면 None."""
    if year is None:
        return None  # 연도를 끝내 못 정한 생략 토큰('3월' 단독)은 어느 해인지 모호 → 미해석
    day_prefix = next((p for p in ("ymd", "ymdd", "md") if match.group(p) is not None), None)
    if day_prefix is not None:
        mo = int(match.group(f"{day_prefix}_m"))
        d = int(match.group(f"{day_prefix}_d"))
        if not (1 <= mo <= 12 and 1 <= d <= month_last_day(year, mo)):
            return None
        time_parts = _token_time(match, day_prefix)
        if time_parts is _INVALID_TIME:
            return None
        label = f"{year}-{mo:02d}-{d:02d}" if day_prefix == "ymdd" else f"{year}년 {mo}월 {d}일"
        from_time = to_time = None
        if time_parts is not None:
            from_time, to_time, time_label = time_parts
            label = f"{label} {time_label}"
        return _window(ymd(year, mo, d), ymd(year, mo, d), label, label_suffix, from_time, to_time)
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


# ── 창 사이 링크(나열 vs 범위) ─────────────────────────────────────────────────────
# 스캔 결과 한 항목: (창, 구체성등급, 원문 시작, 원문 끝).
_Scanned = tuple[dict[str, Any], int, int, int]


def _link_text(text: str, left: _Scanned, right: _Scanned) -> str:
    """창 두 개 사이의 링크 문구.

    나열형 베어 연도 토큰('2019~'의 yb)은 구분자를 토큰 **안에서** 소비하므로 사이 문구가 빈다. 그때는
    그 토큰이 삼킨 구분자를 링크로 본다 — 안 그러면 '2019~2021년'의 범위 표지가 사라져 2019·2021 두
    구간의 합집합(가운데 2020년 누락)이 된다."""
    between = text[left[3]:right[2]]
    if between:
        return between
    swallowed = re.fullmatch(r"\s*\d{4}(.*)", text[left[2]:left[3]], flags=re.DOTALL)
    return swallowed.group(1) if swallowed is not None else ""


def _link_kind(text: str, left: _Scanned, right: _Scanned) -> str | None:
    """링크 종류 — ``"range"``(하나의 연속 구간) | ``"enum"``(두 구간의 합집합) | None(별개 조건).

    범위를 나열보다 **먼저** 본다. '~'·'-' 를 나열로 읽으면 '3월~5월'이 3월 OR 5월이 되어 가운데 달이
    조용히 빠진다. 여는 말('부터/에서')은 닫는 말('까지/사이')이 오른쪽 창 뒤에 실제로 있을 때만 범위이고,
    반쪽이면 나열로 강등하지 않고 미해석으로 남긴다(잘못 건 구간은 드롭보다 나쁘다)."""
    link = _link_text(text, left, right)
    if _RANGE_SEP_LINK_RE.fullmatch(link) is not None:
        return "range"
    if _RANGE_OPEN_LINK_RE.fullmatch(link) is not None:
        return "range" if _RANGE_CLOSER_RE.match(text, right[3]) is not None else None
    return "enum" if _ENUM_LINK_RE.fullmatch(link) is not None else None


def _base_label(window: dict[str, Any], label_suffix: str) -> str:
    """창 라벨에서 호출자 꼬리말('구매')을 뗀 부분 — 범위 라벨을 다시 조립할 때 쓴다."""
    label = str(window.get("label") or "")
    tail = f" {label_suffix}"
    return label[: -len(tail)] if label_suffix and label.endswith(tail) else label


def _merge_range(left: _Scanned, right: _Scanned, text: str, label_suffix: str) -> _Scanned | None:
    """범위 링크로 이어진 창 두 개 → 시작·끝만 남긴 하나의 연속 창(합성할 수 없으면 None).

    합성하지 않는(fail-close) 경우 둘: (1) 구체성이 다르면 어디가 경계인지 단정할 수 없다
    ('2019년부터 3월까지' — 연 시작과 월 끝을 이어 붙이는 것은 추측이다). (2) 합성 결과가 역전이면
    ('2020년 5월부터 2019년 3월까지') 범위가 아니다. 접지 않고 남기면 뒤쪽 창이 주인 없는 구간으로
    남아 소비자 쪽에서 미해석으로 고지된다.

    구간의 원문 출처는 닫는 말까지다 — 반쪽만 덮으면 남은 표현을 다른 슬롯이 다시 주워 간다.

    시각은 경계에서만 남는다 — 왼쪽 창의 시작 시각과 오른쪽 창의 끝 시각이 합성 구간의 경계이고,
    각 창이 홀로 뜻하던 단위 구간의 나머지 경계(왼쪽의 끝, 오른쪽의 시작)는 구간 내부라 사라진다."""
    left_window, left_rank, left_start, _left_end = left
    right_window, right_rank, _right_start, right_end = right
    if left_rank != right_rank:
        return None
    if left_window["from"] > right_window["to"]:
        return None
    from_time = left_window.get("from_time")
    to_time = right_window.get("to_time")
    if (
        left_window["from"] == right_window["to"]
        and from_time is not None and to_time is not None
        and from_time > to_time
    ):
        return None  # 같은 날 시각 역전('7월 1일 18시부터 7월 1일 9시까지')은 범위가 아니다
    closer = _RANGE_CLOSER_RE.match(text, right_end)
    label = f"{_base_label(left_window, label_suffix)}~{_base_label(right_window, label_suffix)}"
    return (
        _window(left_window["from"], right_window["to"], label, label_suffix, from_time, to_time),
        left_rank,
        left_start,
        closer.end() if closer is not None else right_end,
    )


def _fold_range_links(scanned: list[_Scanned], text: str, label_suffix: str) -> list[_Scanned]:
    """범위 링크를 스캔 단계에서 접는다 — 소비자는 창이 원래 몇 개였는지 알 필요가 없다.

    여기서 접어야 하는 이유는 뒤에서 되돌릴 수 없기 때문이다. 창 목록을 그대로 내보내면 소비하는 쪽마다
    '이 둘이 한 구간인가'를 다시 판정해야 하고, 실제로 한쪽(구매일 슬롯)은 앞 창만 쓰고 다른 쪽(드롭 고지)은
    뒤 창을 미해석으로 올려 같은 표현이 경로마다 다르게 읽혔다."""
    folded: list[_Scanned] = []
    index = 0
    while index < len(scanned):
        current = scanned[index]
        while index + 1 < len(scanned) and _link_kind(text, current, scanned[index + 1]) == "range":
            merged = _merge_range(current, scanned[index + 1], text, label_suffix)
            if merged is None:
                break
            current, index = merged, index + 1
        folded.append(current)
        index += 1
    return folded


def _shift_month(reference: date, offset: int) -> tuple[int, int]:
    """기준일의 월을 offset만큼 이동한 (연도, 월). 연말/연초도 같은 산술을 쓴다."""
    serial = reference.year * 12 + reference.month - 1 + int(offset)
    return serial // 12, serial % 12 + 1


def _scan_relative_calendar_months(
    text: str, label_suffix: str, reference: date
) -> list[_Scanned]:
    """이번 달/지난달 계열을 기준일에 고정된 절대 월 창으로 스캔한다.

    표면어는 parser_lexicon.json이 소유하고 이 함수는 의미별 월 offset만 합성한다. SQL 실행 시점의
    GETDATE에 기대지 않으므로 계획과 실행 사이에 월이 바뀌어도 요청 의미가 흔들리지 않는다.
    """
    found: list[_Scanned] = []
    occupied: list[tuple[int, int]] = []
    for vocabulary_name, offset in (
        ("calendar_current_month", 0),
        ("calendar_previous_month", -1),
    ):
        words = sorted(
            lexicon_patterns.vocabulary(vocabulary_name),
            key=lambda value: (-len(value), value),
        )
        if not words:
            continue
        pattern = re.compile("|".join(re.escape(word) for word in words))
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            year, month = _shift_month(reference, offset)
            found.append((
                _window(
                    ymd(year, month, 1),
                    ymd(year, month, month_last_day(year, month)),
                    f"{year}년 {month}월",
                    label_suffix,
                ),
                _MONTH_GRAIN_RANK,
                match.start(),
                match.end(),
            ))
            occupied.append(match.span())
    return sorted(found, key=lambda item: item[2])


def _scan_calendar_windows(
    text: str, label_suffix: str, today: date | None = None
) -> list[tuple[dict[str, Any], int, int, int]]:
    """텍스트의 모든 달력 토큰을 (창, 구체성등급, 시작위치, 끝위치) 로 스캔한다(등장 순).

    연도 생략 토큰('2019년 2월과 3월'의 '3월')은 앞서 나온 명시 연도를 상속한다. 바로 앞에
    '올해/작년' 같은 상대 연도 표지가 있으면 그 연도를 쓴다. 반기·분기는 연도가 끝내 없으면 현재
    연도로 확정한다. 월 단독은 숫자 오탐을 피하기 위해 기존처럼 연도 앵커가 있을 때만 창이 된다.

    마지막에 범위 링크를 접는다(_fold_range_links) — 축약된 오른쪽 창이 왼쪽 문맥(연도)을 상속한 **뒤**에
    합성해야 '2019년 3월부터 5월까지'의 끝이 2019년 5월로 확정된다."""
    if not isinstance(text, str) or not text:
        return []
    reference = today or date.today()
    matches = list(_CAL_TOKEN_RE.finditer(text))
    fallback_year = next((y for y in (_token_year(m) for m in matches) if y is not None), None)
    out: list[tuple[dict[str, Any], int, int, int]] = _scan_relative_calendar_months(
        text, label_suffix, reference
    )
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
    out.sort(key=lambda item: item[2])
    return _fold_range_links(out, text, label_suffix)


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
        """scanned[left] 와 scanned[right] 가 둘 다 같은 구체성이면서 나열로 이어져 있는지.

        범위 링크는 스캔 단계에서 이미 창 하나로 접혔으므로 여기 남은 범위는 합성이 거부된 것(구체성 불일치·
        역전)뿐이다 — 나열로 강등하지 않는다(그러면 '2019년부터 3월까지'가 두 구간 합집합이 된다)."""
        return (
            scanned[left][1] == rank
            and scanned[right][1] == rank
            and _link_kind(text, scanned[left], scanned[right]) == "enum"
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

    지원: 'YYYY년 M월 D일'(하루), 'M월 D일'(연도 상속), 'YYYY년 M월'(그 달 전체), 'YYYY년'(그 해 전체),
          'YYYY-MM-DD'/'YYYY.MM.DD'/'YYYY/MM/DD'(하루), 'YYYY-MM'(그 달 전체),
          'YYYY년/올해/작년 상반기·하반기'(6개월), 연도 생략 상·하반기(현재 연도),
          'YYYY년/올해/작년 N분기'(3개월), 연도 생략 N분기(현재 연도).
    일 단위 표현에는 시각 한정자('9시', '오후 6시 30분')가 붙을 수 있고, 그때만 창에
    ``from_time``/``to_time``(HHMMSS) 키가 실린다.

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


# 창 dict 에 실리는 출처 표기(내부 키 — 밑줄 접두어는 IR 스냅샷·감사 로그에서 제외되는 관례다).
# 구간은 **후보를 만든 텍스트(공백 제거)의 좌표계**다. 원문 좌표가 필요한 소비자는 그쪽에서 변환한다.
SOURCE_SPAN_KEY = "_source_span"
SOURCE_TEMPORAL_KIND_KEY = "_source_temporal_kind"
# 후보가 억제된 사유(§ 과거 시점 표현 안의 기간은 롤링 기간이 아니다). 후보를 목록에서 지우지 않고
# 표시로 남긴다 — 지우면 오작동해도 흔적이 없고, 감사 로그가 '무엇이 왜 빠졌는지'에 답할 수 없다.
SUPPRESSED_BY_PAST_POINT = "past_point"


class DurationCandidate(NamedTuple):
    """기간 표현 후보 하나 + **생성 시점에 확정된** 의미 종류.

    소비자는 ``kind``/``suppressed_by`` 만 보고 거르며 텍스트를 다시 읽지 않는다 — 소비자마다
    '이게 시점인가 기간인가'를 재해석하던 것이 같은 어구를 시간 조건 두 개로 만든 원인이었다.
    """

    start: int
    end: int
    value: int
    unit: str
    kind: str = KIND_ROLLING
    suppressed_by: str | None = None


def _raw_duration_window_candidates(compact: str) -> list[DurationCandidate]:
    """의미 종류 판정 전의 기간 표면형 후보(등장 순). 단어형은 unit=days."""
    out: list[DurationCandidate] = []
    for match in NUMERIC_DURATION_PATTERN.finditer(compact):
        value = int(match.group("num"))
        # 2019년/2026년은 달력 연도이지 2019년 길이의 롤링 창이 아니다. 이를 기간으로 잡으면
        # DATEADD(DAY, -736935, ...) 같은 비정상 조건이 절대 날짜 범위와 함께 생성된다.
        if value > 0 and not (match.group("unit") == "년" and 1900 <= value <= 2199):
            out.append(DurationCandidate(
                match.start(), match.end(), value, KO_UNIT_TO_CANON.get(match.group("unit"), "days")
            ))
    for match in WORD_DURATION_PATTERN.finditer(compact):
        out.append(DurationCandidate(match.start(), match.end(), WORD_DURATION_DAYS[match.group(0)], "days"))
    return sorted(out)


def _classified_duration_candidate(
    candidate: DurationCandidate, past_expressions: list[tuple[tuple[int, int], str]]
) -> DurationCandidate:
    """후보에 의미 종류를 붙인다 — 후보 구간을 **포함**하는 'N단위 전' 표현이 그 종류를 정한다.

    겹침이 아니라 포함으로 판정한다: '7년'(0,2)은 '7년전'(0,3) 안에 있으므로 시점의 일부지만,
    인접한 별개 표현('… 3년 전 가입한 최근 1년 …')은 한 글자 겹침만으로 남의 종류를 물려받지 않는다.
    """
    span = (candidate.start, candidate.end)
    for outer, kind in past_expressions:
        if not slot_ownership.span_contains(outer, span):
            continue
        return candidate._replace(
            kind=kind,
            suppressed_by=SUPPRESSED_BY_PAST_POINT if kind == KIND_PAST_POINT else None,
        )
    return candidate


def duration_window_candidates(compact: str) -> list[DurationCandidate]:
    """공백 제거 텍스트의 기간 표현 후보(등장 순) — 각 후보에 의미 종류와 억제 표시가 붙어 나온다.

    과거 시점('7년 전') 안의 기간 표면형('7년')은 롤링 기간이 아니므로 ``suppressed_by="past_point"``
    로 표시된다. 목록에서 지우지 않는 이유는 진단이다 — 지우면 오작동해도 흔적이 없다. 표시를 실제로
    거를지는 슬롯의 정책이 정하고(:func:`parse_duration_window` 의 ``past_point``), 판정 자체는 이
    공통 경로 한 곳에서만 한다.

    전제: 입력은 공백 제거 텍스트다(:data:`NUMERIC_DURATION_PATTERN` 이 공백을 건너뛰지 않는다).
    시점 구간도 같은 문자열에서 계산하므로 좌표계가 섞일 자리가 없다.
    """
    past_expressions = [(match.span(), kind) for match, kind in _past_expressions(compact)]
    return [
        _classified_duration_candidate(candidate, past_expressions)
        for candidate in _raw_duration_window_candidates(compact)
    ]


def duration_window_from_candidate(candidate: DurationCandidate) -> dict[str, Any]:
    """기간 후보 → 상대 창 표기 {value, unit, min_days} + 출처(구간·의미 종류).

    창 shape 의 단일 소유자다 — 예전에는 graph_rag 가 같은 dict 을 따로 조립해, 출처를 붙이려면
    두 곳을 고쳐야 했다."""
    return {
        "value": candidate.value,
        "unit": candidate.unit,
        "min_days": candidate.value * targeting_ir.UNIT_DAYS[candidate.unit],
        SOURCE_SPAN_KEY: (candidate.start, candidate.end),
        SOURCE_TEMPORAL_KIND_KEY: candidate.kind,
    }


# 과거 시점 표현을 만난 슬롯의 정책. 억제(suppress)는 **그 시점을 표현할 다른 소유자가 있을 때만**
# 옳다 — 구매 도메인에는 절대 창 슬롯(purchase_date)이 있어 시점이 그쪽으로 간다. 가입·집계·휴면처럼
# 아직 절대 창 슬롯이 없는 도메인에서 억제하면 조건이 조용히 사라지므로(그 자체가 더 큰 결함) 현행
# 해석(롤링 기간)을 유지한다. 그 도메인에 절대 창 소유자가 생기면 이 정책만 바꾼다.
PAST_POINT_AS_DURATION = "as_duration"
PAST_POINT_SUPPRESS = "suppress"


def parse_duration_window(
    query: str,
    *,
    require_number: bool = True,
    default_days: int | None = None,
    exclude_past: bool = False,
    anchor_terms: tuple[str, ...] | None = None,
    past_point: str = PAST_POINT_AS_DURATION,
) -> dict[str, Any] | None:
    """통합 기간 창 파서 — 숫자형(3개월/2주/1년)·단어형(일주일/반년/한달)을 모두 잡아 정규 shape로 돌려준다.

    반환 {value, unit(∈days/weeks/months/years), min_days}. 파편화된 슬롯별 창 파서(가입/로그인/미구매/
    미접속)가 각자 다른 단위 부분집합만 지원해 '1년 이내 가입'·'반년 미구매' 같은 표현을 놓치던 것을
    한 곳으로 모은다. 문맥 게이트(가입 신호/로그인 신호/부정어)는 호출자가 유지한다.

    anchor_terms 를 주면 그 앵커어 근처(±DURATION_ANCHOR_GAP)의 기간만 본다 — 여러 조건이 각자 창을
    가진 프롬프트('최근 1년 이내 가입 … 최근 로그인')에서 로그인 창이 가입의 '1년'을 훔쳐가는 조건 간
    창 충돌을 막는다(앵커가 하나도 없으면 전체에서 첫 창으로 폴백).

    과거 시점('7년 전')을 어떻게 볼지는 **호출자(슬롯)의 정책**이다(``past_point``) — 그 시점을 표현할
    절대 창 소유자가 있는 도메인만 :data:`PAST_POINT_SUPPRESS` 를 쓴다(:data:`PAST_POINT_AS_DURATION`
    설명 참조). ``exclude_past=True`` 는 그보다 넓은 정책이다: '전'을 낀 어떤 형태도(시점이든 '전부터/
    전까지' 경계든) 이 슬롯의 창으로 보지 않는다. 두 정책 모두 후보의 **의미 종류**로 판정하며 '전'
    문자 검사로 되돌아가지 않는다."""
    compact = query.replace(" ", "").casefold()
    candidates = list(duration_window_candidates(compact))
    if past_point == PAST_POINT_SUPPRESS:
        candidates = [c for c in candidates if c.suppressed_by is None]
    if exclude_past:
        candidates = [c for c in candidates if c.kind == KIND_ROLLING]
    if anchor_terms:
        anchor_spans = [
            (match.start(), match.end())
            for term in anchor_terms
            for match in re.finditer(re.escape(term), compact)
        ]
        if anchor_spans:
            def _near(cand: DurationCandidate) -> bool:
                return any(
                    max(cand.start, a_start) - min(cand.end, a_end) <= DURATION_ANCHOR_GAP
                    for a_start, a_end in anchor_spans
                )
            candidates = [c for c in candidates if _near(c)]
    if candidates:
        return duration_window_from_candidate(candidates[0])
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
#
# 'N단위 전' + (선택) 경계 어휘 하나. 경계 그룹이 비면 시점이고, 차 있으면 그 시점을 **경계로 삼는
# 범위**다 — 한 패턴이 두 종류를 함께 읽으므로 시점 판정 규칙이 이 파일 안에서도 두 벌이 되지 않는다
# (예전에는 lookahead 와 소비자 쪽 '전' 문자 검사로 나뉘어 있었고, 그래서 경로마다 다르게 읽혔다).
RELATIVE_PAST_PATTERN = re.compile(
    rf"(?P<num>\d+)\s*(?P<unit>주일|개월|년|달|주|일)\s*전\s*(?P<boundary>{_PAST_BOUNDARY_ALT})?"
)


def past_expression_kind(match: "re.Match[str]") -> str:
    """:data:`RELATIVE_PAST_PATTERN` 매치 하나의 의미 종류(시점/시작 경계/끝 경계)."""
    boundary = match.group("boundary")
    return PAST_BOUNDARY_KINDS[boundary] if boundary else KIND_PAST_POINT


def _past_expressions(text: str) -> list[tuple["re.Match[str]", str]]:
    """'N단위 전' 표현을 (매치, 의미 종류) 로 스캔한다 — 순수 함수(날짜 연산·창 생성 없음).

    창을 만들지 않으므로 구간만 필요한 소비자(:func:`past_point_spans`)가 부작용 없이 재사용한다."""
    return [(match, past_expression_kind(match)) for match in RELATIVE_PAST_PATTERN.finditer(text or "")]


def past_point_matches(text: str) -> list["re.Match[str]"]:
    """과거 시점 표현의 매치 목록(경계 표현 제외).

    시점인지 아닌지를 소비자가 다시 판정하지 않게 하는 공개 진입점이다 — 매치의 ``num``/``unit``
    그룹은 그대로 쓰면 된다."""
    return [match for match, kind in _past_expressions(text) if kind == KIND_PAST_POINT]


def past_point_spans(text: str) -> list[tuple[int, int]]:
    """과거 시점 표현의 구간 목록(등장 순).

    포함: '7년 전', '2주 전'  /  제외: '3개월 전부터', '3개월 전까지'(경계 표현)

    구간은 입력한 텍스트의 좌표계다 — 호출자는 후보 구간을 만든 것과 **같은 문자열**을 넘겨야 한다.
    """
    return [match.span() for match in past_point_matches(text)]


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
    (calendar_window_from_parts 가 절대 창에 대해 하는 역할과 같다).

    반환 창에는 의미 종류(``_source_temporal_kind`` = :data:`KIND_PAST_POINT`)가 실려 나간다 —
    소비자가 '이 창이 시점에서 나왔는지'를 텍스트를 다시 읽어 판단하지 않게 하는 것이 목적이다."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    if unit not in ("years", "months", "weeks", "days"):
        return None
    anchor = today or date.today()
    target = _relative_past_target(value, unit, anchor)
    if unit == "years":
        window = _window(ymd(target.year, 1, 1), ymd(target.year, 12, 31), f"{target.year}년", label_suffix)
    elif unit == "months":
        last = month_last_day(target.year, target.month)
        window = _window(
            ymd(target.year, target.month, 1), ymd(target.year, target.month, last),
            f"{target.year}년 {target.month}월", label_suffix,
        )
    elif unit == "weeks":
        start = target - timedelta(days=target.weekday())  # 그 주 월요일
        end = start + timedelta(days=6)
        window = _window(
            ymd(start.year, start.month, start.day), ymd(end.year, end.month, end.day),
            f"{start.year}년 {start.month}월 {start.day}일~{end.month}월 {end.day}일", label_suffix,
        )
    else:
        window = _window(
            ymd(target.year, target.month, target.day), ymd(target.year, target.month, target.day),
            f"{target.year}년 {target.month}월 {target.day}일", label_suffix,
        )
    window[SOURCE_TEMPORAL_KIND_KEY] = KIND_PAST_POINT
    return window


def _scan_relative_past_windows(
    text: str, today: "date | None", label_suffix: str
) -> list[tuple[dict[str, Any], int, int]]:
    """'N단위 전' 표현을 (창, 시작, 끝) 으로 등장 순서대로 스캔한다.

    표현을 찾는 일은 :func:`_past_expressions`(순수 스캔)가, 창을 만드는 일은 여기가 한다 — 구간만
    필요한 소비자가 날짜 연산까지 다시 돌지 않게 나눈 것이다."""
    out: list[tuple[dict[str, Any], int, int]] = []
    for match, kind in _past_expressions(text or ""):
        if kind != KIND_PAST_POINT:
            continue  # 경계 표현('3개월 전부터')은 시점이 아니다 — 범위 문법이 소유한다
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
