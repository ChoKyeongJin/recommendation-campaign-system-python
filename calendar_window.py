"""기간 표현(창)의 단일 소유 모듈 — 절대 달력 창과 상대 기간 창을 한 문법으로 읽는다.

배경: 창 파싱이 모듈마다 따로 살아 있었다. ``entity_set`` 은 ``(\\d{4})년`` 하나만 알아 '2019년 3월'을
2019년 전체로 뭉갰고(3월 베스트셀러가 조용히 연간 베스트셀러가 된다), ``targeting_expression`` 의 LLM
스키마에는 ``year`` 정수 필드뿐이라 월·분기·반기는 애초에 표현할 수단이 없었다. 완전한 달력 문법은
``graph_rag`` 안에만 있었지만 그쪽은 순수 모듈이 아니라 재사용이 불가능했다. 표현형이 하나 늘 때마다
세 곳을 각자 고쳐야 하는 구조 자체가 결함이고, 실제로 한쪽만 고쳐진 상태로 오래 있었다.

이 모듈이 그 문법의 유일한 소유자다. 새 표현(예: 'YYYY년 M월 상순')은 여기 한 곳에 추가하면 규칙
파서·LLM 라우트·구매일 타겟이 동시에 얻는다.

    절대 창 := {from, to, label[, from_time, to_time]}  # 달력상 확정된 구간. YYYYMMDD CHAR(8) 비교용.
    상대 창 := {value, unit[, min_days]}                 # 월/년은 일수로 근사하지 않는다.

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

import condition_normalizers
import lexicon_patterns
import slot_ownership


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

class MeridiemRule(NamedTuple):
    """시각 한정어 하나의 12시간제 해석 규칙.

    ``min_hour``/``max_hour`` 는 그 말이 실제로 가리키는 시각의 범위다. 범위를 선언하는 이유는
    한국어 시각 한정어의 경계가 말마다 다르고 **바깥은 뜻이 확정되지 않기 때문**이다 — '밤 2시'는
    다음 날 새벽을 뜻할 수도 있어 어느 날의 02시인지 창으로 확정할 수 없다. 선언 밖은 추측하지
    않고 미해석으로 둔다(fail-close). ``hour12`` 는 12시가 접히는 값이고(자정 0 / 정오 12),
    12가 범위 밖인 말에는 없다.
    """

    min_hour: int
    max_hour: int
    offset: int
    hour12: int | None = None


# 시각 한정어 → 해석 규칙. 새 표현('한밤중')은 이 표 한 줄이면 정규식과 변환이 함께 얻는다 —
# 예전에는 낱말이 정규식 리터럴(`오전|오후`)에, 변환이 if 문에 따로 있어 한쪽만 늘 수 있었다.
# '낮'은 일부러 없다: '낮 2시'(14시)와 '낮 12시'(정오)가 같은 말로 쓰이지만 '낮 1시'의 경계가
# 화자마다 달라 결정론으로 확정할 수 없다(§12 — 정의 없이 구현하지 않는다).
MERIDIEM_RULES: dict[str, MeridiemRule] = {
    "오전": MeridiemRule(1, 12, 0, hour12=0),
    "오후": MeridiemRule(1, 12, 12, hour12=12),
    "새벽": MeridiemRule(1, 6, 0),
    "아침": MeridiemRule(6, 11, 0),
    "저녁": MeridiemRule(5, 9, 12),
    "밤": MeridiemRule(7, 12, 12, hour12=0),
}
_MERIDIEM_ALTERNATION = "|".join(sorted(MERIDIEM_RULES, key=len, reverse=True))


# 시각 한정자의 본체(시·분·초). 일 단위 토큰 뒤에 붙는 형태와 날짜 없는 반복 시각
# (:func:`parse_time_of_day_window`)이 **같은 문법**을 써야 한다 — 두 벌이면 '9시 30분 15초'가
# 한쪽에서만 초까지 읽히고 다른 쪽에서는 분까지만 읽혀 같은 어구가 경로마다 다른 구간이 된다.
def _time_body_pattern(prefix: str) -> str:
    """'오후 6시 30분 15초' 본체. 분·초는 각각 선택적이고 그 **정밀도가 곧 구간의 단위**다.

    '시간'은 시각이 아니라 기간이므로 lookahead 로 배제한다('3시간 이내'의 '3시'를 시각으로
    오인하면 기간 표현이 반쪽 남는다)."""
    return (
        rf"(?:(?P<{prefix}_ap>{_MERIDIEM_ALTERNATION})\s*)?(?P<{prefix}_hh>\d{{1,2}})\s*시(?!간)"
        rf"(?:\s*(?P<{prefix}_mi>\d{{1,2}})\s*분)?"
        rf"(?:\s*(?P<{prefix}_ss>\d{{1,2}})\s*초)?"
    )


def _time_suffix_pattern(prefix: str) -> str:
    """일 단위 토큰 뒤에 붙는 시각 한정자('9시', '오후 6시 30분', '23시 59분 59초'). 통째로 선택적이다.

    날짜에 붙지 않은 시각 단독('9시 이후 주문')은 어느 날의 9시인지 **하루짜리 창**으로 확정할 수
    없어 여기서는 잡지 않는다(fail-close). 다만 시각 범위('밤 11시부터 새벽 2시 사이')는 날짜가
    아니라 **매일 반복되는 시각대**라는 확정된 뜻이 있으므로 :func:`parse_time_of_day_window` 가
    따로 소유한다 — 창이 아니라 시각 조건이라 shape 부터 다르다."""
    return rf"(?:\s*{_time_body_pattern(prefix)})?"


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
# 주 단위는 달력 토큰이 아니라 상대 표현('지난주')으로만 들어오지만 등급 축에는 자리가 있어야 한다 —
# 일과 월 사이다. 등급은 서로 간의 **순서**만 뜻하므로 절대값에 의미는 없다.
_GRAIN_RANK = {"ymd": 0, "ymdd": 0, "md": 0, "ym": 2, "ymd2": 2, "m": 2, "yq": 3, "q": 3, "yh": 4, "h": 4, "y": 5, "yb": 5}
_DAY_GRAIN_RANK = _GRAIN_RANK["ymd"]
_WEEK_GRAIN_RANK = 1
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

    명시 연도는 기준일 없이 확정한다. '올해/금년'과 '작년/지난해/전년', 연도 생략 표현은 주입된
    기준일이 있을 때만 확정한다. 그냥 '반기'(상/하 없음)나 숫자 없는 '분기'는 어느 반/분기인지
    모호하므로 잡지 않는다."""
    year_match = _ANY_YEAR_RE.search(text or "")
    if year_match is not None:
        y = int(year_match.group(1))
    elif today is None:
        return None
    else:
        y = _text_anchor_year(text or "", today) or today.year
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


def _time_bounds(
    meridiem: str | None, hh: str, minute_raw: str | None, second_raw: str | None
) -> tuple[str, str, str] | object:
    """시·분·초 조각 → (구간 시작 HHMMSS, 구간 끝 HHMMSS, 라벨 조각). 달력상 불가능하면 _INVALID_TIME.

    시각 토큰은 **말한 정밀도의 단위 전체 구간**을 뜻한다 — '9시'는 09:00:00~09:59:59, '9시 30분'은
    09:30:00~09:30:59, '23시 59분 59초'는 그 1초다. 날짜의 '7월까지'가 7월 말일까지를 포함하는 것과
    같은 단위 의미론이고, 그래서 '23시 59분 59초까지'의 끝은 정확히 235959 가 된다(초를 읽지 못하던
    동안에는 '23시 59분'까지만 읽고 끝이 우연히 같아 맞아 보였을 뿐이다).

    범위 합성(_merge_range)이 왼쪽 창의 시작 시각과 오른쪽 창의 끝 시각만 취하므로
    '9시부터 18시까지'는 09:00:00~18:59:59 가 된다.

    범위 검증은 여기서 닫는다 — 시(0~23, 한정어가 있으면 그 한정어의 선언 범위)·분(0~59)·초(0~59)를
    벗어난 값은 시각만 조용히 버리지 않고 표현 전체를 미해석으로 남긴다(fail-close)."""
    hour = int(hh)
    if meridiem is not None:
        rule = MERIDIEM_RULES[meridiem]
        if not rule.min_hour <= hour <= rule.max_hour:
            return _INVALID_TIME
        if hour == 12:
            if rule.hour12 is None:  # pragma: no cover - 범위 검사에서 이미 걸린다
                return _INVALID_TIME
            hour = rule.hour12
        else:
            hour += rule.offset
    elif hour > 23:
        return _INVALID_TIME
    minute = int(minute_raw) if minute_raw is not None else None
    second = int(second_raw) if second_raw is not None else None
    if (minute is not None and minute > 59) or (second is not None and second > 59):
        return _INVALID_TIME
    label = (
        f"{meridiem + ' ' if meridiem else ''}{int(hh)}시"
        + (f" {minute}분" if minute is not None else "")
        + (f" {second}초" if second is not None else "")
    )
    if second is not None:
        # 초까지 말했으면 구간이 아니라 그 한 초다. 분이 생략된 '9시 30초'는 09:00:30 이다.
        stamp = f"{hour:02d}{(minute or 0):02d}{second:02d}"
        return (stamp, stamp, label)
    if minute is not None:
        return (f"{hour:02d}{minute:02d}00", f"{hour:02d}{minute:02d}59", label)
    return (f"{hour:02d}0000", f"{hour:02d}5959", label)


def _token_time(match: "re.Match[str]", prefix: str) -> tuple[str, str, str] | None | object:
    """일 단위 토큰에 붙은 시각 한정자 → :func:`_time_bounds` 결과(한정자가 없으면 None)."""
    hh = match.group(f"{prefix}_hh")
    if hh is None:
        return None
    return _time_bounds(
        match.group(f"{prefix}_ap"),
        hh,
        match.group(f"{prefix}_mi"),
        match.group(f"{prefix}_ss"),
    )


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


# ── 방향성 열린 구간('X까지' · 'X 이후') ────────────────────────────────────────────
# 'X까지 주문한 회원'의 X 는 사건이 일어난 **칸**이 아니라 **끝 경계**다. 그런데 경계 낱말을 읽지
# 않으면 남는 것은 X 라는 칸 하나뿐이고, 그 순간 '2026년 7월 1일 23시 59분 59초까지'가 '그날 그 1초에
# 주문한 회원'이 된다 — 요청한 집합의 아주 작은 부분집합이라 결과가 조용히 비는 종류의 오답이다.
#
# 열린 쪽은 '모든 과거/모든 미래'를 뜻하는 센티널 날짜로 닫는다. 창 shape 을 바꾸지 않는 것이 목적이다:
# 모든 소비자(BETWEEN 술어·구간 병합·plan 감사)가 이미 {from,to} 를 읽으므로, 센티널로 닫으면 새 배선
# 없이 곧바로 옳은 SQL 이 된다. 방향은 ``open_start``/``open_end`` 표지로 함께 남긴다 — 센티널을 모르는
# 소비자가 '이 경계는 원문이 말한 것이 아니다'를 판정할 수 있어야 한다.
#
# 값은 **표현 가능한 최대 구간**이어야 한다. 끝을 9999-12-31 로 두면, 이 구간을 반개구간으로 바꾸는
# 소비자(event_ir.AbsoluteInterval, 구간 인접 판정 _next_day8)가 계산하는 '끝 + 하루'가 연도 10000 이라
# 표현되지 않는다(실측: 라이브 '… 이후' 프롬프트가 OverflowError 로 500). 9999-12-30 을 포함 끝으로
# 두면 그 다음 날이 date.max 라 어느 소비자도 넘치지 않는다 — 소비자마다 방어 코드를 다는 대신
# 우리가 고르는 값을 한계 안에 둔다.
OPEN_WINDOW_MIN_DATE = "19000101"
OPEN_WINDOW_MAX_DATE = "99991230"
OPEN_START_KEY = "open_start"
OPEN_END_KEY = "open_end"

# 경계 낱말 → 열리는 쪽. 'N단위 전부터/까지'의 경계 어휘(:data:`PAST_BOUNDARY_KINDS`)와 같은 구분이며,
# 그쪽은 상대 시점에, 이쪽은 절대 달력 표현에 붙는다.
OPEN_BOUNDARY_WORDS: dict[str, str] = {
    "까지": OPEN_START_KEY,
    "이전": OPEN_START_KEY,
    "부터": OPEN_END_KEY,
    "이후": OPEN_END_KEY,
    "이래": OPEN_END_KEY,
}
_OPEN_BOUNDARY_RE = re.compile(
    r"\s*(?:에)?\s*(?P<word>"
    + "|".join(sorted(OPEN_BOUNDARY_WORDS, key=len, reverse=True))
    + r")"
)


def _linked_indexes(scanned: list[_Scanned], text: str) -> set[int]:
    """이웃 창과 링크(나열·범위)로 이어진 창의 인덱스.

    링크에 낀 창에는 열린 경계를 적용하지 않는다 — 'A부터 B까지'의 '까지'는 범위를 닫는 말이지
    B 를 열린 구간으로 만드는 말이 아니고, 범위 합성이 거부된 자리(구체성 불일치 등)에서 한쪽만
    열어 주면 fail-close 하기로 한 판단이 추측으로 바뀐다."""
    linked: set[int] = set()
    for index in range(len(scanned) - 1):
        if _link_kind(text, scanned[index], scanned[index + 1]) is not None:
            linked.update((index, index + 1))
    return linked


def _open_window(
    window: dict[str, Any], direction: str, word: str, label_suffix: str
) -> dict[str, Any]:
    """닫힌 창 하나 + 경계 낱말 → 한쪽이 센티널로 열린 창."""
    # 조사형('까지'·'부터')은 붙여 쓰고 부사형('이전'·'이후'·'이래')은 띄어 쓴다 — 라벨은 사람이 읽는다.
    separator = "" if word in ("까지", "부터") else " "
    base = f"{_base_label(window, label_suffix)}{separator}{word}".strip()
    if direction == OPEN_START_KEY:
        opened = _window(
            OPEN_WINDOW_MIN_DATE, window["to"], base, label_suffix,
            None, window.get("to_time"),
        )
    else:
        opened = _window(
            window["from"], OPEN_WINDOW_MAX_DATE, base, label_suffix,
            window.get("from_time"), None,
        )
    opened[direction] = True
    return opened


def _apply_open_boundaries(
    scanned: list[_Scanned], text: str, label_suffix: str
) -> list[_Scanned]:
    """범위 접기 뒤 남은 단독 창에 방향성 경계 낱말을 적용한다.

    접기 **뒤**에 하는 이유는 'A부터 B까지'가 이미 한 창으로 접히면서 닫는 말까지 원문 구간에
    삼켰기 때문이다 — 그 자리에는 경계 낱말이 남아 있지 않으므로 범위와 열린 구간이 서로를
    가로채지 않는다."""
    linked = _linked_indexes(scanned, text)
    out: list[_Scanned] = []
    for index, item in enumerate(scanned):
        window, rank, start, end = item
        match = None if index in linked else _OPEN_BOUNDARY_RE.match(text, end)
        if match is None:
            out.append(item)
            continue
        word = match.group("word")
        out.append(
            (_open_window(window, OPEN_BOUNDARY_WORDS[word], word, label_suffix), rank, start, match.end())
        )
    return out


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


def _scan_relative_calendar_days(
    text: str, label_suffix: str, reference: date
) -> list[_Scanned]:
    """오늘/어제 계열을 기준일에 고정된 **하루짜리** 절대 창으로 스캔한다.

    월 계열(:func:`_scan_relative_calendar_months`)과 같은 합성이고 단위만 일이다. 이 스캐너가
    없던 동안 '오늘 주문한 회원'의 '오늘'은 어떤 창도 만들지 못했고, 애플리케이션이 소유해야 할
    기간 바인딩이 비어 있으니 구조화 모델은 규칙대로 ``missing_argument(period)`` 를 냈다. 그
    결과가 확인 질문이거나 — 더 나쁘게는 — 기간이 빠진 '구매 있음' SQL 이었다(전수 EXISTS).

    실행 시점 ``GETDATE`` 로 미루지 않고 기준일에 고정하는 이유는 월 계열과 같다: 계획과 실행
    사이에 날짜가 바뀌어도 요청 의미가 흔들리면 안 된다.

    '오늘 기준'처럼 **앵커 표지**가 뒤따르면 창이 아니다 — 그때 이 낱말은 사건이 일어난 구간이
    아니라 다른 조건을 읽을 기준 시점을 가리킨다('오늘 기준 휴면 회원'은 현재 상태 조건이지 오늘
    하루의 사건이 아니다). 앵커를 창으로 만들면 아무도 소비하지 않는 기간 리터럴이 생기고, 그
    미소비가 곧 확인 질문이 된다(실측 회귀).
    """
    found: list[_Scanned] = []
    occupied: list[tuple[int, int]] = []
    anchor_markers = sorted(
        lexicon_patterns.vocabulary("as_of_anchor_marker"),
        key=lambda value: (-len(value), value),
    )
    as_of_suffix = (
        re.compile(r"\s*(?:은|는|이|가|으로|로)?\s*(?:" + "|".join(re.escape(word) for word in anchor_markers) + r")")
        if anchor_markers
        else None
    )
    for vocabulary_name, offset in (
        ("calendar_today", 0),
        ("calendar_yesterday", -1),
        ("calendar_day_before_yesterday", -2),
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
            if as_of_suffix is not None and as_of_suffix.match(text, match.end()):
                continue
            day = reference + timedelta(days=offset)
            stamp = ymd(day.year, day.month, day.day)
            found.append((
                _window(stamp, stamp, f"{day.year}년 {day.month}월 {day.day}일", label_suffix),
                _DAY_GRAIN_RANK,
                match.start(),
                match.end(),
            ))
            occupied.append(match.span())
    return sorted(found, key=lambda item: item[2])


def _scan_relative_calendar_weeks(
    text: str, label_suffix: str, reference: date
) -> list[_Scanned]:
    """이번 주/지난주 계열을 기준일에 고정된 절대 주 창으로 스캔한다.

    주의 경계는 **월요일 00:00 부터 다음 월요일 00:00 전까지**다(ISO 관례이고, 'N주 전'이 이미
    같은 경계를 쓴다 — :func:`relative_past_window` 의 weeks 분기). 정책을 두 곳이 각자 고르면
    '지난주'와 '1주 전'이 다른 이레를 가리킨다.

    이 스캐너가 없던 동안 '지난 주에 주문한 회원'의 '지난 주'는 어떤 창도 만들지 못했고, 기간이
    빠진 채 '구매 있음'으로 컴파일될 수 있었다(전수 EXISTS) — 오늘/어제 계열이 같은 이유로 생긴
    스캐너의 주 단위 대칭이다.
    """
    found: list[_Scanned] = []
    occupied: list[tuple[int, int]] = []
    for vocabulary_name, offset in (
        ("calendar_current_week", 0),
        ("calendar_previous_week", -1),
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
            monday = reference - timedelta(days=reference.weekday()) + timedelta(weeks=offset)
            sunday = monday + timedelta(days=6)
            found.append((
                _window(
                    ymd(monday.year, monday.month, monday.day),
                    ymd(sunday.year, sunday.month, sunday.day),
                    f"{monday.year}년 {monday.month}월 {monday.day}일~{sunday.month}월 {sunday.day}일",
                    label_suffix,
                ),
                _WEEK_GRAIN_RANK,
                match.start(),
                match.end(),
            ))
            occupied.append(match.span())
    return sorted(found, key=lambda item: item[2])


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
    '올해/작년' 같은 상대 연도 표지가 있으면 주입된 기준일에서 그 연도를 구한다. 반기·분기는 연도가
    끝내 없고 기준일도 없으면 호스트 날짜를 추측하지 않고 미검출로 닫는다. 월 단독은 숫자 오탐을
    피하기 위해 기존처럼 연도 앵커가 있을 때만 창이 된다.

    마지막에 범위 링크를 접는다(_fold_range_links) — 축약된 오른쪽 창이 왼쪽 문맥(연도)을 상속한 **뒤**에
    합성해야 '2019년 3월부터 5월까지'의 끝이 2019년 5월로 확정된다."""
    if not isinstance(text, str) or not text:
        return []
    matches = list(_CAL_TOKEN_RE.finditer(text))
    fallback_year = next((y for y in (_token_year(m) for m in matches) if y is not None), None)
    out: list[tuple[dict[str, Any], int, int, int]] = (
        [
            *_scan_relative_calendar_days(text, label_suffix, today),
            *_scan_relative_calendar_weeks(text, label_suffix, today),
            *_scan_relative_calendar_months(text, label_suffix, today),
        ]
        if today is not None
        else []
    )
    running_year: int | None = None
    for match in matches:
        explicit = _token_year(match)
        if explicit is not None:
            running_year = explicit
        anchor = (
            _adjacent_anchor_year(text, match.start(), today)
            if today is not None
            else None
        )
        relative = anchor[0] if anchor is not None else None
        if relative is not None:
            running_year = relative
        quarter_duration = (
            match.group("q") is not None
            and _is_quarter_duration(text, match.start(), match.end())
        )
        inferred_current = (
            today.year
            if (
                today is not None
                and (
                    match.group("h") is not None
                    or (match.group("q") is not None and not quarter_duration)
                )
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
    return _apply_open_boundaries(_fold_range_links(out, text, label_suffix), text, label_suffix)


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
          'YYYY년/올해/작년 상반기·하반기'(6개월), 연도 생략 상·하반기(기준일 필요),
          'YYYY년/올해/작년 N분기'(3개월), 연도 생략 N분기(기준일 필요).
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


# ── 반복 시각대(날짜 없는 시각 범위) ────────────────────────────────────────────────
# '밤 11시부터 다음 날 새벽 2시 사이에 주문한 회원'의 시각은 **어느 하루의** 23시가 아니라 매일
# 되풀이되는 시각대다. 날짜가 없으므로 절대 창(YYYYMMDD)으로는 표현할 수 없고, 그렇다고 미해석으로
# 두면 시각 조건이 통째로 사라진 '전 기간 구매'가 된다 — 창이 아니라 **시각 조건**이라는 별도 shape 이
# 있어야 하는 이유다. 날짜 창과 직교하므로 둘은 AND 로 함께 쓸 수 있다('7월에 밤 11시~새벽 2시').
#
# 단독 시각('9시 이후 주문')은 여전히 잡지 않는다 — 하루의 시각대인지 특정일의 시점인지 확정할 수
# 없다(fail-close). 확정할 수 있는 것은 **두 시각이 범위로 묶인** 형태뿐이다.
TIME_OF_DAY_FROM_KEY = "from_time"
TIME_OF_DAY_TO_KEY = "to_time"

_TIME_OF_DAY_TOKEN_RE = re.compile(_time_body_pattern("tod"))
# 자정을 넘긴다는 표지. 표지가 없어도 시작 > 끝이면 자정 횡단이지만, 표지가 있는 표현을 링크로
# 인정하지 않으면 '부터 다음 날 …'이 통째로 미해석이 된다.
_NEXT_DAY_ALT = r"다음\s*날|다음날|익일|이튿날"
_TOD_SEP_LINK_RE = re.compile(
    rf"\s*[{_RANGE_SEP_CHARS}]\s*(?:{_RANGE_OPENER_ALT})?\s*(?:(?:{_NEXT_DAY_ALT})\s*)?"
)
_TOD_OPEN_LINK_RE = re.compile(
    rf"\s*(?:{_RANGE_OPENER_ALT})\s*(?:(?:{_NEXT_DAY_ALT})\s*)?"
)


def time_of_day_crosses_midnight(time_of_day: dict[str, Any]) -> bool:
    """시작이 끝보다 늦으면 자정을 넘는 시각대다('23시~02시'). 저장하지 않고 파생한다 —
    같은 사실을 두 곳에 두면 한쪽만 갱신된 상태가 생긴다."""
    start = str(time_of_day.get(TIME_OF_DAY_FROM_KEY) or "")
    end = str(time_of_day.get(TIME_OF_DAY_TO_KEY) or "")
    return bool(start and end and start > end)


def _scan_time_of_day_tokens(text: str) -> list[tuple[tuple[str, str, str], int, int]] | None:
    """날짜 토큰에 딸리지 않은 시각 토큰 목록. 달력상 불가능한 시각이 섞이면 None(전체 미해석)."""
    occupied = [match.span() for match in _CAL_TOKEN_RE.finditer(text)]
    found: list[tuple[tuple[str, str, str], int, int]] = []
    for match in _TIME_OF_DAY_TOKEN_RE.finditer(text):
        if any(match.start() < end and start < match.end() for start, end in occupied):
            continue
        bounds = _time_bounds(
            match.group("tod_ap"), match.group("tod_hh"), match.group("tod_mi"), match.group("tod_ss")
        )
        if bounds is _INVALID_TIME:
            return None
        if isinstance(bounds, tuple):
            found.append((bounds, match.start(), match.end()))
    return found


def _time_of_day_pairs(text: str) -> list[tuple[dict[str, Any], int, int]]:
    """범위로 묶인 시각 토큰 쌍 → (시각대, 시작위치, 끝위치) 목록(등장 순).

    링크 판정은 날짜 범위와 같은 규칙이다: 구분자형('9시~18시')은 그대로 범위이고, 여는 말형
    ('9시부터 …')은 닫는 말('까지'/'사이')이 뒤에 실제로 있을 때만 범위다(반쪽이면 미해석)."""
    tokens = _scan_time_of_day_tokens(text)
    if not tokens:
        return []
    out: list[tuple[dict[str, Any], int, int]] = []
    index = 0
    while index + 1 < len(tokens):
        (left, _left_start, left_end), (right, right_start, right_end) = tokens[index], tokens[index + 1]
        link = text[left_end:right_start]
        if _TOD_SEP_LINK_RE.fullmatch(link) is not None:
            end = right_end
        elif _TOD_OPEN_LINK_RE.fullmatch(link) is not None:
            closer = _RANGE_CLOSER_RE.match(text, right_end)
            if closer is None:
                index += 1
                continue
            end = closer.end()
        else:
            index += 1
            continue
        out.append((
            {
                TIME_OF_DAY_FROM_KEY: left[0],
                TIME_OF_DAY_TO_KEY: right[1],
                "label": f"{left[2]}~{right[2]}",
            },
            tokens[index][1],
            end,
        ))
        index += 2
    return out


def parse_time_of_day_window(text: str) -> dict[str, Any] | None:
    """날짜 없는 반복 시각대 하나를 ``{from_time, to_time, label}``(HHMMSS)로 읽는다(없으면 None).

    '오전 9시부터 오후 6시 사이'는 090000~185959, '밤 11시부터 다음 날 새벽 2시 사이'는
    230000~025959 다(끝 경계는 날짜 창의 '18시까지'와 같은 단위 의미론 — 말한 정밀도의 단위
    전체를 포함한다). 뒤쪽이 앞쪽보다 이르면 자정을 넘는 시각대이며
    (:func:`time_of_day_crosses_midnight`), 그 구분은 SQL 에서 AND 와 OR 를 가른다."""
    if not isinstance(text, str) or not text:
        return None
    pairs = _time_of_day_pairs(text)
    return dict(pairs[0][0]) if pairs else None


def parse_time_of_day_window_span(text: str) -> tuple[int, int] | None:
    """:func:`parse_time_of_day_window` 가 읽은 표현의 원문 구간(슬롯 소유권 기록용)."""
    if not isinstance(text, str) or not text:
        return None
    pairs = _time_of_day_pairs(text)
    return (pairs[0][1], pairs[0][2]) if pairs else None


_RELATIVE_YEAR_ONLY_RE = re.compile(_YEAR_ANCHOR_PATTERN + r"\s*$")


def parse_relative_year_window(
    text: str, *, label_suffix: str = "", today: date | None = None
) -> dict[str, Any] | None:
    """'올해'/'작년'처럼 **연도 앵커 하나뿐인** 표현을 그 해 전체 창으로 읽는다(없으면 None).

    :func:`parse_calendar_window` 는 앵커를 창으로 만들지 않는다 — '작년 상반기'에서 앵커는
    한정자의 연도를 정할 뿐이다. 그런데 앵커만 있는 표현('작년')은 그 해 전체가 유일한 해석이고,
    그 해석을 호출자가 각자 적으면 상대 연도 어휘(:data:`_RELATIVE_YEAR_OFFSETS`)의 두 번째
    소유자가 생긴다. 그래서 앵커 → 창 변환도 여기서 한다.

    앵커 **전체**가 표현이어야 한다('작년 매출'은 창이 아니다). 'N년 전'은 받지 않는다 —
    그것은 창이 아니라 시점이고, 시점의 소유자는 :func:`relative_past_window` 다.
    """
    if not isinstance(text, str) or today is None:
        return None
    match = _RELATIVE_YEAR_ONLY_RE.fullmatch(text.strip())
    if match is None or match.group("rel") is None:
        return None
    window = calendar_window_from_parts(_anchor_year(match, today))
    if window is not None and label_suffix:
        window["label"] = _base_label(window, label_suffix)
    return window


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

# 한글 기간 단위 → 캐노니컬 영문 단위(슬롯 정규화용). 표면 별칭의 소유자는
# normalization lexicon이며 달력 파서는 숫자 기간 부분집합만 소비한다.
KO_UNIT_TO_CANON = condition_normalizers.numeric_duration_unit_semantics()
# 캐노니컬 단위 → 라벨용 한글 단위(파싱의 역방향. 단어형 '일주일'도 정규화 후 '7일'로 읽힌다).
CANON_TO_KO_UNIT = {"days": "일", "weeks": "주", "months": "개월", "years": "년"}
# 기간 표현. 숫자형('7일', '2주')과 숫자 없는 한글 단어형('일주일', '보름', '한 달')을 모두 본다.
# 단어형은 숫자가 없어서 재작성 가드의 숫자 서명에도, 기존 '최근 N일' 파서에도 안 잡혔다.
# ``*_DAYS`` 와 공개 패턴은 아직 이를 import하는 레거시 신호 비교기의 호환 표면이다. 정확히 일수로
# 환산되는 일/주만 노출한다. canonical 기간 파서는 별도 전체 패턴으로 월/년까지 읽고 단위를 보존한다.
DURATION_UNIT_DAYS = {
    surface: condition_normalizers.unit_days()[canonical]
    for surface, canonical in KO_UNIT_TO_CANON.items()
    if canonical in condition_normalizers.unit_days()
}
_FIXED_DURATION_UNIT_PATTERN = "|".join(
    re.escape(unit)
    for unit in sorted(DURATION_UNIT_DAYS, key=lambda item: (-len(item), item))
)
_CANONICAL_DURATION_UNIT_PATTERN = "|".join(
    re.escape(unit)
    for unit in sorted(KO_UNIT_TO_CANON, key=lambda item: (-len(item), item))
)
NUMERIC_DURATION_PATTERN = re.compile(
    rf"(?P<num>\d+)\s*(?P<unit>{_FIXED_DURATION_UNIT_PATTERN})"
)
_CANONICAL_NUMERIC_DURATION_PATTERN = re.compile(
    rf"(?P<num>\d+)\s*(?P<unit>{_CANONICAL_DURATION_UNIT_PATTERN})"
)
WORD_DURATION_SPECS: dict[str, tuple[int, str]] = {
    "일주일": (7, "days"), "한주일": (7, "days"), "한주": (7, "days"), "일주": (7, "days"),
    "이주일": (14, "days"), "두주일": (14, "days"), "두주": (14, "days"),
    "삼주일": (21, "days"), "세주일": (21, "days"), "세주": (21, "days"),
    "보름": (15, "days"),
    "한달": (1, "months"), "한개월": (1, "months"),
    "두달": (2, "months"), "두개월": (2, "months"),
    "석달": (3, "months"), "세달": (3, "months"), "세개월": (3, "months"),
    "반년": (6, "months"), "일년": (1, "years"), "한해": (1, "years"), "한햇": (1, "years"),
}
WORD_DURATION_DAYS = {
    surface: value * ({"days": 1, "weeks": 7}[unit])
    for surface, (value, unit) in WORD_DURATION_SPECS.items()
    if unit in {"days", "weeks"}
}
WORD_DURATION_PATTERN = re.compile(
    "|".join(sorted(map(re.escape, WORD_DURATION_DAYS), key=len, reverse=True))
)
_CANONICAL_WORD_DURATION_PATTERN = re.compile(
    "|".join(sorted(map(re.escape, WORD_DURATION_SPECS), key=len, reverse=True))
)

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
    """의미 종류 판정 전의 기간 표면형 후보(등장 순)."""
    out: list[DurationCandidate] = []
    for match in _CANONICAL_NUMERIC_DURATION_PATTERN.finditer(compact):
        value = int(match.group("num"))
        # 2019년/2026년은 달력 연도이지 2019년 길이의 롤링 창이 아니다. 이를 기간으로 잡으면
        # DATEADD(DAY, -736935, ...) 같은 비정상 조건이 절대 날짜 범위와 함께 생성된다.
        if value > 0 and not (match.group("unit") == "년" and 1900 <= value <= 2199):
            unit = KO_UNIT_TO_CANON.get(match.group("unit"))
            if unit is not None:
                out.append(DurationCandidate(match.start(), match.end(), value, unit))
    for match in _CANONICAL_WORD_DURATION_PATTERN.finditer(compact):
        value, unit = WORD_DURATION_SPECS[match.group(0)]
        out.append(DurationCandidate(match.start(), match.end(), value, unit))
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
    """기간 후보 → 상대 창 표기 {value, unit[, min_days]} + 출처(구간·의미 종류).

    창 shape 의 단일 소유자다 — 예전에는 graph_rag 가 같은 dict 을 따로 조립해, 출처를 붙이려면
    두 곳을 고쳐야 했다. ``min_days`` 는 일/주만 정확히 파생하며 월/년은 원 단위를 보존한다."""
    window: dict[str, Any] = {
        "value": candidate.value,
        "unit": candidate.unit,
        SOURCE_SPAN_KEY: (candidate.start, candidate.end),
        SOURCE_TEMPORAL_KIND_KEY: candidate.kind,
    }
    fixed_unit_days = {"days": 1, "weeks": 7}.get(candidate.unit)
    if fixed_unit_days is not None:
        window["min_days"] = candidate.value * fixed_unit_days
    return window


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

    반환 {value, unit(∈days/weeks/months/years)[, min_days]}. ``min_days`` 는 일/주에만 붙는다.
    파편화된 슬롯별 창 파서(가입/로그인/미구매/
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
    raw_unit = str(window.get("unit") or "")
    unit = CANON_TO_KO_UNIT.get(raw_unit, raw_unit)
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
    if unit not in ("years", "months", "weeks", "days") or today is None:
        return None
    target = _relative_past_target(value, unit, today)
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
    """과거 시점 표현의 원문 구간(시작, 끝)을 날짜 계산 없이 찾는다.

    구간 탐지는 실행 창 확정이 아니다. 실제 창은 :func:`parse_relative_past_window` 가
    주입된 ``today`` 를 받았을 때만 만든다.
    """
    match = next(iter(past_point_matches(text)), None)
    return match.span() if match is not None else None


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
    ``include_duration=True`` 인 소비자만 롤링 기간을 받는다. 기간은 ``{value, unit, label}`` 로
    보존하고, 일/주처럼 정확히 환산되는 경우에만 호환용 ``days`` 를 함께 싣는다.
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
            duration = {
                "value": relative["value"],
                "unit": relative["unit"],
                "label": relative_window_label(relative),
            }
            if "min_days" in relative:
                duration["days"] = relative["min_days"]
            return [duration]
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

    절대 창의 구간은 앵커까지 포함한다 — '7년전 상반기'는 '상반기'가 아니라 표현 전체가 출처다.
    상대 과거 시점은 창과 마찬가지로 ``today`` 가 주입됐을 때만 소비된 구간으로 보고한다."""
    span = parse_calendar_window_span(text, today=today)
    if span is not None:
        return span
    return (
        parse_relative_past_window_span(text)
        if allow_relative_past and today is not None
        else None
    )


def parse_time_window_group_span(
    text: str, *, today: "date | None" = None, allow_relative_past: bool = True
) -> tuple[int, int] | None:
    """``parse_time_windows`` 가 읽은 **나열 전체**의 원문 구간(창 목록을 통째로 소비하는 슬롯용)."""
    span = parse_calendar_window_group_span(text, today=today)
    if span is not None:
        return span
    return (
        parse_relative_past_window_span(text)
        if allow_relative_past and today is not None
        else None
    )
