"""오디언스 문장의 **프레임**(잉여어)과 **절 구조**를 읽는 공용 원자.

배경: 닫힌 문형(sentence template) 정규식이 모듈마다 하나씩 자라고 있었다 — 장바구니 미결제
한 문형, 휴면 로그인 한 문형, 단일 보완 상품 한 문형, 지표 두 문형. 다섯 자리가 전부 같은 일을
한다: "이 요청이 **이 조건 하나뿐인가**"를 판정한다. 그런데 판정 단위가 **문장**이라 어미 하나
('찾아줘'→'추출해줘'), 요청어 유무, 어순이 달라지면 같은 뜻인데도 그냥 무효가 되고, 확장은
케이스마다 정규식을 한 줄 더 쓰는 선형 작업이었다.

이 모듈은 그 판정을 **문장에서 절 구조로** 옮긴다. 문형이 실제로 확인하던 것은 셋이다.

    1. 같은 절인가   두 표면(소스 별칭 · 부정 표지)이 한 절 안에 있는가
    2. 프레임뿐인가  그 밖의 잔여물이 조건을 담지 않는 잉여어(회원·찾아줘·조사)뿐인가
    3. 원장 전량 소비 추출된 리터럴이 남김없이 소유되는가 — 원장은 호출자가 갖고 있으므로
       여기서는 "소유 스팬"으로만 받는다

어휘는 데이터(:mod:`lexicon_patterns`)가, 구조만 여기 코드가 갖는다 — 저장소의 같은 계약이다.
그래서 '고객님'을 더 받아들이는 일은 이 파일이 아니라 사전 한 줄이다.

**같은 낱말, 다른 자리, 다른 뜻.** 회원 명사는 잔여물에서는 프레임이지만(조건이 아니다) 두 스팬
**사이**에 있으면 절 경계다('… 담은 회원 중 구매 이력이 없는 회원'의 '회원'). 그래서 프레임 어휘와
절 경계 어휘를 따로 선언한다. ``member_noun_role`` 을 통째로 쓰지 않는 이유도 같다 — 그 어휘에는
'구매자'가 들어 있어서, 프레임으로 인정하면 구매 조건이 잉여어로 위장해 통과한다.

좌표계: 모든 스팬은 **원문 좌표**다. ``graph_rag`` 의 부정 감지기만 공백을 지운 compact 문자열
위에서 도는데, 그 좌표를 원문으로 되돌리는 것이 :func:`compact_to_source_span` 이고 길이가
달라지는 입력(casefold 확장)에서는 ``None`` 으로 fail-close 한다.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence

import lexicon_patterns

Span = tuple[int, int]

# 프레임 어휘 — 조건을 담지 않는 잉여어. 오디언스 명사·요청 지시어·조사·용언 어미.
# ``member_noun_role``('사람·구매자·소비자')은 쓰지 않는다: '구매자'가 조건을 프레임으로 위장시킨다.
FRAME_VOCABULARIES: tuple[str, ...] = (
    "audience_frame_noun",
    "request_directive",
    "frame_particle",
    "bound_particle",
    "object_particle",
    "verb_ending_hada",
    "event_completion_ending",
)

# 절 경계 어휘 — 두 스팬 **사이**에 오면 그 둘은 다른 절이다. 프레임 명사가 여기에도 있는 것은
# 오타가 아니라 계약이다(위 docstring 의 '같은 낱말, 다른 자리, 다른 뜻').
CLAUSE_BOUNDARY_VOCABULARIES: tuple[str, ...] = (
    "audience_frame_noun",
    "clause_scope_marker",
    "and_connective",
    "or_connective",
    "enum_connective",
)

# 절을 가르는 구두점. 마침표(.!?)는 문장 끝 장식이라 잔여물에서 무시하지만, 쉼표는 나열이므로
# 절 경계다 — 둘을 한 집합에 두면 'A, B 회원'이 조용히 한 절이 된다.
CLAUSE_BOUNDARY_PUNCTUATION = frozenset(",、，;；")

# 잔여물에서 지우는 문장 장식. 낱말이 아니므로 프레임 어휘에 넣지 않는다.
_IGNORABLE = frozenset(" \t\r\n.!?…~\"'“”‘’「」『』")

# 명사형 어미. 별칭이 이것으로 끝나면 그 별칭은 용언에서 온 것이고 어간을 파생할 수 있다('담기'→'담').
_NOMINALIZER = "기"

_HANGUL = r"가-힣"

# 별칭과 부정 표지 사이의 최대 거리(글자). 이 값을 넘으면 국소 부정이 아니라 다른 절의 부정이다.
DEFAULT_NEGATION_GAP = 8

# 근거 스팬을 어절 끝까지 늘릴 때 멈추는 문자('않' → '않은'은 붙이되 다음 낱말은 삼키지 않는다).
_EVIDENCE_STOP_CHARS = ",.;:!?()[]{}"


def _compact(value: object) -> str:
    """공백을 지우고 대소문자를 접은 비교용 문자열."""
    return "".join(str(value or "").split()).casefold()


@functools.lru_cache(maxsize=64)
def _terms_of(vocabularies: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    """어휘 이름들 → compact 낱말 목록(긴 낱말 우선)."""
    collected = {
        _compact(term)
        for name in vocabularies
        for term in lexicon_patterns.vocabulary(name)
    }
    collected.update(_compact(term) for term in extra)
    return tuple(
        sorted((term for term in collected if term), key=lambda item: (-len(item), item))
    )


def frame_terms(*, extra_terms: Iterable[str] = ()) -> tuple[str, ...]:
    """프레임으로 인정하는 낱말 전체(진단·테스트용).

    ``extra_terms`` 는 호출자가 **이미 다른 근거로 소유한** 낱말이다. 예를 들어 원장이
    ``temporal_kind=rolling_duration`` 을 증명했으면 '최근'은 그 리터럴의 동어반복이고,
    카탈로그가 ``absence_restatement_terms`` 로 '휴면'을 선언했으면 그것은 부재 조건의
    동어반복이다. 삼키는 근거를 데이터로 드러내야 드리프트 가드가 볼 수 있다.
    """
    return _terms_of(FRAME_VOCABULARIES, tuple(sorted({str(term) for term in extra_terms})))


def _is_segmentable(text: str, terms: Sequence[str]) -> bool:
    """문자열이 낱말들로 **빈틈없이** 쪼개지는가(탐욕 매칭의 함정을 피한 DP)."""
    if not text:
        return True
    reachable = [False] * (len(text) + 1)
    reachable[0] = True
    for index in range(len(text)):
        if not reachable[index]:
            continue
        for term in terms:
            if text.startswith(term, index):
                reachable[index + len(term)] = True
    return reachable[len(text)]


def _normalized_spans(query: str, spans: Iterable[Span]) -> list[Span]:
    """스팬 목록을 원문 범위로 자르고 정렬·병합한다(중복 소유는 오류가 아니다)."""
    cleaned: list[Span] = []
    for item in spans or ():
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        start, end = max(0, start), min(len(query), end)
        if start < end:
            cleaned.append((start, end))
    cleaned.sort()
    merged: list[Span] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def residue_pieces(query: str, owned_spans: Iterable[Span]) -> tuple[str, ...]:
    """소유 스팬 사이에 남은 조각들.

    조각을 이어 붙이지 않고 **각각** 돌려준다 — 이어 붙이면 서로 다른 자리의 글자가 우연히 한
    프레임 낱말을 이뤄 잔여물 검사가 느슨해진다.
    """
    if not isinstance(query, str):
        return ()
    pieces: list[str] = []
    cursor = 0
    for start, end in _normalized_spans(query, owned_spans):
        if cursor < start:
            pieces.append(query[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(query):
        pieces.append(query[cursor:])
    return tuple(pieces)


def is_frame_only(
    query: str,
    owned_spans: Iterable[Span],
    *,
    extra_terms: Iterable[str] = (),
) -> bool:
    """소유 스팬 밖 잔여물이 **잉여어(프레임)뿐**인가.

    닫힌 문형 정규식의 접미 패턴(``_MEMBER_SUFFIX`` 계열)을 대체한다. 문형은 "회원+요청어로
    끝나는가"를 물었지만 실제로 알아야 하는 것은 "**조건이 더 있는가**"이므로, 어순·어미·요청어
    유무와 무관하게 같은 답을 낸다. 조건어가 하나라도 남아 있으면 거짓이다.
    """
    if not isinstance(query, str):
        return False
    terms = frame_terms(extra_terms=extra_terms)
    for piece in residue_pieces(query, owned_spans):
        text = _compact("".join(char for char in piece if char not in _IGNORABLE))
        if text and not _is_segmentable(text, terms):
            return False
    return True


def alias_stems(aliases: Iterable[str]) -> tuple[str, ...]:
    """별칭에서 용언 어간을 파생한다('담기'→'담').

    카탈로그의 별칭 목록은 이미 '담기'를 갖고 있다. 그래서 '담아두고'·'담은' 같은 활용형을
    코드에 적을 필요가 없다 — 명사형 어미로 끝나는 별칭만 용언으로 보고 어간을 떼면 된다.
    """
    stems: set[str] = set()
    for alias in aliases or ():
        text = str(alias or "").strip()
        if (
            len(text) >= 2
            and text.endswith(_NOMINALIZER)
            and re.fullmatch(rf"[{_HANGUL}]+", text) is not None
        ):
            stems.add(text[:-1])
    return tuple(sorted(stems))


def stem_inflection_spans(query: str, stems: Iterable[str]) -> tuple[Span, ...]:
    """어간으로 **시작하는 어절** 구간들('담'→'담아두고'·'담은').

    어절 첫머리로 제한해 '부담'·'분담' 같은 낱말 안쪽 일치를 배제한다. 활용 어미 목록을 만들지
    않는 이유는 그것이 곧 새 두더지잡기이기 때문이다 — 어간 뒤 한글은 활용으로 본다.
    """
    if not isinstance(query, str) or not query:
        return ()
    found: set[Span] = set()
    for stem in stems or ():
        text = str(stem or "")
        if not text:
            continue
        pattern = rf"(?<![{_HANGUL}]){re.escape(text)}[{_HANGUL}]*"
        for match in re.finditer(pattern, query):
            found.add(match.span())
    return tuple(sorted(found))


def _blank(text: str, spans: Iterable[Span]) -> str:
    """구간을 공백으로 덮는다(길이 보존 — 좌표가 어긋나지 않는다)."""
    result = text
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + " " * (end - start) + result[end:]
    return result


def in_same_clause(
    query: str,
    left: Span,
    right: Span,
    *,
    stems: Iterable[str] = (),
    content_terms: Iterable[str] = (),
) -> bool:
    """두 스팬이 **한 절** 안에 있는가.

    "증거 스팬이 상대 표면을 덮는가"로는 부족하다. 모델이 문장 전체를 증거로 적으면 그 검사는
    언제나 참이 되고, ``장바구니에 담은 회원 중 구매 이력이 없는`` 처럼 **조건이 둘인 문장**이
    하나로 접힌다. 그래서 덮는지가 아니라 **사이에 무엇이 있는가**로 판정한다.

    거절 근거는 둘이다 — 절 경계어(프레임 명사·범위 표지·접속어·쉼표)와 내용어(호출자가 주는
    도메인 명사). 소유 소스의 어간 활용형('담아두고')은 그 절의 서술어이므로 먼저 비운다.
    """
    if not isinstance(query, str):
        return False
    # 두 스팬이 **각각** 유효해야 한다. 하나가 무효인데 병합 결과만 세면, 스팬을 못 읽은 입력이
    # '겹쳤다=같은 절'로 통과한다(fail-open).
    if len(_normalized_spans(query, (left,))) != 1 or len(
        _normalized_spans(query, (right,))
    ) != 1:
        return False
    bounds = _normalized_spans(query, (left, right))
    if len(bounds) < 2:
        # 겹치거나 맞닿은 두 스팬은 사이가 없다 — 같은 절이다.
        return True
    (_left_start, left_end), (right_start, _right_end) = bounds[0], bounds[-1]
    between = query[left_end:right_start]
    if not between:
        return True
    if any(char in CLAUSE_BOUNDARY_PUNCTUATION for char in between):
        return False
    between = _blank(between, stem_inflection_spans(between, stems))
    haystack = _compact(between)
    if not haystack:
        return True
    boundary = _terms_of(CLAUSE_BOUNDARY_VOCABULARIES, ())
    if any(term in haystack for term in boundary):
        return False
    return not any(
        (compacted := _compact(term)) and compacted in haystack for term in content_terms or ()
    )


def local_negation_spans(
    query: str,
    aliases: Iterable[str],
    *,
    max_gap: int = DEFAULT_NEGATION_GAP,
) -> tuple[Span, ...]:
    """별칭이 **국소 부정**된 원문 구간들('결제하지 않은'·'미구매').

    부정이 별칭을 접두하거나(``미구매``) 별칭 바로 뒤 ``max_gap`` 글자 안에서 따라올 때만 그
    별칭의 부정으로 본다. 문장 어디든 있는 부정어를 끌어오면 다른 절의 부정을 이 사건의 것으로
    읽는다. 구간 끝은 어절 끝까지 늘려 짧은 부정 어간에 붙은 활용('않'→'않은')을 포함하되,
    다음 낱말은 삼키지 않는다.
    """
    if not isinstance(query, str) or not query:
        return ()
    negations = tuple(
        str(term) for term in lexicon_patterns.vocabulary("generic_negation") if term
    )
    alias_spans = {
        match.span()
        for term in aliases or ()
        if isinstance(term, str) and term
        for match in re.finditer(re.escape(term), query, flags=re.IGNORECASE)
    }
    found: set[Span] = set()
    for alias_start, alias_end in alias_spans:
        for negation in negations:
            for match in re.finditer(re.escape(negation), query, flags=re.IGNORECASE):
                negation_start, negation_end = match.span()
                prefixes_alias = negation_start <= alias_start and negation_end >= alias_end
                follows_alias = (
                    alias_end <= negation_start and negation_start - alias_end <= max_gap
                )
                if not (prefixes_alias or follows_alias):
                    continue
                start = min(alias_start, negation_start)
                end = max(alias_end, negation_end)
                while (
                    end < len(query)
                    and not query[end].isspace()
                    and query[end] not in _EVIDENCE_STOP_CHARS
                ):
                    end += 1
                found.add((start, end))
    return tuple(sorted(found))


def surface_spans(query: str, aliases: Iterable[str]) -> tuple[Span, ...]:
    """별칭 표면과 그 어간 활용형이 나타난 구간 전부('장바구니'·'담아두고')."""
    if not isinstance(query, str) or not query:
        return ()
    found: set[Span] = {
        match.span()
        for term in aliases or ()
        if isinstance(term, str) and term
        for match in re.finditer(re.escape(term), query, flags=re.IGNORECASE)
    }
    found.update(stem_inflection_spans(query, alias_stems(aliases)))
    return tuple(sorted(found))


def _compact_offsets(query: str) -> list[int] | None:
    """compact 인덱스 → 원문 인덱스 표. 접기로 길이가 달라지면 ``None``."""
    offsets: list[int] = []
    for index, char in enumerate(query):
        if char == " ":
            continue
        if len(char.casefold()) != 1:
            return None
        offsets.append(index)
    return offsets


def compact_to_source_span(query: str, start: int, end: int) -> Span | None:
    """compact(``replace(" ", "").casefold()``) 좌표 → 원문 좌표.

    ``graph_rag`` 의 표면 감지기들은 조사·어미 결합을 보려고 공백을 지운 문자열에서 돈다. 그
    좌표로 원문 스팬 소유권을 따지려면 되돌려야 하는데, casefold 가 글자 수를 늘리는 입력
    (독일어 ``ß`` 등)에서는 대응이 1:1이 아니다. 그때는 추측하지 않고 ``None`` 이다.
    """
    if not isinstance(query, str):
        return None
    offsets = _compact_offsets(query)
    if offsets is None:
        return None
    if not (
        isinstance(start, int)
        and isinstance(end, int)
        and not isinstance(start, bool)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(offsets)
    ):
        return None
    return offsets[start], offsets[end - 1] + 1


__all__ = [
    "CLAUSE_BOUNDARY_PUNCTUATION",
    "CLAUSE_BOUNDARY_VOCABULARIES",
    "FRAME_VOCABULARIES",
    "Span",
    "alias_stems",
    "compact_to_source_span",
    "frame_terms",
    "in_same_clause",
    "is_frame_only",
    "local_negation_spans",
    "residue_pieces",
    "stem_inflection_spans",
    "surface_spans",
]
