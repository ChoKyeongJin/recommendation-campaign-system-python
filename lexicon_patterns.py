"""표면어 사전에서 만들어지는 정규식 패턴 — 어휘는 데이터, 구조는 코드.

배경: 새 표현이 하나 생길 때마다 파이썬을 고쳐야 했다. 원인은 표면어 **목록**이 코드 상수로
박혀 있었기 때문이고, 더 나쁘게는 같은 어휘가 여러 상수에 복제돼 있었다 — 예를 들어 ``동시에``는
AND 접속어로 세 곳(``_OR_OPERAND_BOUNDARY_RE`` / ``_CAMPAIGN_CLAUSE_BOUNDARY_RE`` / ``_LOGIC_AND_RE``)에
따로 적혀 있었다. 동의어 하나를 추가하려면 그 셋을 다 찾아야 하고, 하나를 놓치면 절 분리만
안 되는 식으로 조건이 조용히 새는 결함이 된다.

이 모듈이 그 어휘의 단일 소스다.

    어휘(vocabulary)  — 이름 붙은 표면어 목록. ``and_connective``, ``member_noun`` 처럼 **뜻**으로 이름한다.
    패턴(pattern)     — 어휘 몇 개를 합쳐 만든 교대 정규식. 조합 규칙만 여기 있고 낱말은 없다.

새 표현형은 :data:`DEFAULT_LEXICON_PATH` 파일의 어휘에 한 줄 추가하면 그 어휘를 쓰는 모든 패턴이
함께 얻는다. 파일이 없거나 파손되면 :data:`_CODE_FALLBACK` 으로 폴백한다(기존 렉시콘 로더와 같은 관례).

교대 순서는 **긴 낱말 우선**으로 결정론 정렬한다. ``이면서|면서`` 처럼 접두어가 겹치는 한국어 조사·
어미에서 짧은 쪽이 먼저 오면 긴 쪽이 영영 매치되지 않는다 — 데이터 작성자가 정규식 교대 순서를
알아야 하는 함정을 없앤다. 낱말은 :func:`re.escape` 로 이스케이프되므로 데이터에 정규식을 쓸 수 없다
(사전은 사전이지 코드가 아니다).
"""

from __future__ import annotations

import functools
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LEXICON_PATH = Path(
    os.getenv("PARSER_LEXICON_PATH", str(Path(__file__).resolve().parent / "docs" / "data" / "parser_lexicon.json"))
)

# ── 코드 폴백(파일 부재/파손 시). 파일과 같은 스키마이며, 이관 시점의 값을 그대로 옮긴 것이다. ──
_CODE_FALLBACK: dict[str, Any] = {
    "vocabularies": {
        # 논리 접속: AND 계열과 OR 계열. 절 경계 판정과 논리식 파싱이 공유한다.
        "and_connective": ["이면서", "면서", "이고", "이며", "그리고", "동시에"],
        "contrast_connective": ["반면", "지만", "다만"],
        "or_connective": ["또는", "혹은", "이거나", "거나"],
        # 사람(오디언스) 명사. 세 모듈이 각자 다른 범위를 쓰고 있어 층으로 나눈다.
        "member_noun": ["회원", "고객", "사용자"],
        "member_noun_informal": ["유저"],
        "member_noun_honorific": ["고객님"],
        "member_noun_role": ["사람", "구매자", "소비자"],
        # 조건 판정 IR의 의미 표지. 낱말은 데이터가 소유하고, 코드에는 조합 구조만 둔다.
        "identity_same": ["동일", "동일한", "같은", "똑같은"],
        "simultaneity": ["동시", "동시에", "함께"],
        "count_result_noun": ["수", "수량", "인원", "인원수", "명수"],
        # 크기 방향어(랭킹 정렬 방향).
        "direction_high": ["높은", "많은", "큰", "상위"],
        "direction_low": ["낮은", "적은", "작은", "하위"],
        # 비교 표지: 두 값을 견주는 말과, 변화 방향을 나타내는 말.
        "comparison_marker": ["보다", "대비"],
        "change_direction": ["증가", "감소", "늘", "줄", "커진"],
        "magnitude_adjective": ["큰", "작은", "많은", "적은", "높은", "낮은"],
        "prior_period": ["이전", "직전"],
        "exact_marker": ["정확히", "정확하게", "딱"],
        # 도메인 문맥어: 이 낱말이 있어야 그 조건/집계를 해당 도메인으로 본다.
        "purchase_verb": ["구매", "구입", "주문", "샀"],
        "product_noun": ["상품", "제품", "품목"],
        "money_noun": ["결제", "할인", "객단가", "매출", "구매액", "금액"],
        "count_noun": ["수량", "종류", "건수", "종수"],
        "campaign_contact_noun": ["쿠폰", "오퍼", "혜택", "제안", "발송", "전송", "접촉", "도달"],
        # 회원 조건을 나타내는 도메인 낱말(조건 언어 판정용 — 구매 동사와 합쳐 쓴다).
        "condition_domain_noun": [
            "재구매", "장바구니", "카트", "캠페인", "반응", "로그인", "접속", "방문", "쿠폰", "찜",
            "거주", "지역", "등급", "성별", "남성", "여성", "나이", "연령", "휴면", "탈퇴", "정상",
            "활동", "가입", "수신", "블랙리스트",
        ],
        # 오디언스 전체를 가리키는 말. 조건이 없는 것이 정상이므로 미해석 탐지에서 제외하는 데 쓴다.
        "whole_audience": ["전체", "모든", "전부", "모두", "아무나", "누구나"],
        # 낱말이 아닌 구분자(쉼표 등). 어휘로 두어 패턴 조합에서 같은 방식으로 다룬다.
        "clause_separator": [","],
    },
    # 이관 원칙: 낱말 **집합은 이관 전과 정확히 같다**. 어휘를 합치는 것(동작 변경)은 이관과 섞지
    # 않는다 — 골든 diff 가 "옮긴 것"과 "바꾼 것"을 구분할 수 없게 되기 때문이다. 그래서 역사적으로
    # 빠져 있던 낱말은 exclude 로 보존하고, 그 사유를 note 에 적어 **드러나게** 둔다.
    # exclude 가 달린 패턴은 곧 "이 누락이 의도인가?" 라는 검토 목록이다(lexicon_patterns.exclusions()).
    "patterns": {
        "or_operand_boundary": {
            "include": ["and_connective", "contrast_connective", "or_connective", "clause_separator"],
            "extra": ["중"],
            "exclude": ["다만", "혹은"],
            "note": "OR 피연산자 경계. '중'은 '회원 중'의 경계. 다만/혹은 누락은 이관 전 상태 보존 — 의도 미확인.",
        },
        "campaign_clause_boundary": {
            "include": ["and_connective", "contrast_connective", "or_connective", "clause_separator"],
            "exclude": ["이면서", "동시에", "혹은"],
            "note": "캠페인 절 경계. 이면서/동시에가 빠져 있다 — AND 접속어인데 경계로 안 치는 것이 의도인지 미확인(누락 의심).",
        },
        "logic_and": {"include": ["and_connective"]},
        "logic_or": {"include": ["or_connective"]},
        "or_connective": {
            "include": ["or_connective"], "exclude": ["혹은"],
            "note": "OR 게이트 판정용. logic_or 와 달리 '혹은'이 빠져 있다 — 이관 전 상태 보존.",
        },
        "member_noun_core": {"include": ["member_noun"]},
        "member_noun_basic": {"include": ["member_noun", "member_noun_informal"]},
        "purchase_rank_target": {
            "include": ["member_noun", "member_noun_informal", "member_noun_honorific", "member_noun_role"],
            "exclude": ["사용자"],
            "note": "구매 랭킹 대상 명사. '사용자'만 빠져 있다 — 누락 의심(다른 회원 명사 패턴에는 모두 있다).",
        },
        "direction_high": {"include": ["direction_high"]},
        "direction_low": {"include": ["direction_low"]},
        "period_compare_marker": {
            "include": ["comparison_marker", "change_direction"], "exclude": ["커진"],
            "note": "기간 비교 표지. intra_temporal_compare 와 달리 '커진'이 빠져 있다 — 이관 전 상태 보존.",
        },
        "intra_temporal_compare": {"include": ["comparison_marker", "magnitude_adjective", "change_direction"]},
        "prior_period": {"include": ["prior_period"]},
        "exact_equals_marker": {"include": ["exact_marker"]},
        "agg_domain_context": {"include": ["purchase_verb", "product_noun", "money_noun", "count_noun"]},
        "campaign_concept_anchor": {
            "include": ["purchase_verb", "campaign_contact_noun"], "exclude": ["주문", "샀"],
            "note": "캠페인 개념 앵커. 구매 동사 중 구매/구입만 쓴다(주문·샀 제외) — 이관 전 상태 보존.",
        },
        "condition_language": {
            "include": ["purchase_verb", "condition_domain_noun"], "exclude": ["샀"],
            "note": "조건 언어 판정. 구어체 '샀'만 빠져 있다 — 이관 전 상태 보존.",
        },
        "whole_audience": {"include": ["whole_audience"]},
    },
}


@functools.lru_cache(maxsize=4)
def _load(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _section(name: str) -> dict[str, Any]:
    """파일의 섹션(어휘/패턴). 없거나 비정형이면 코드 폴백."""
    section = _load(str(DEFAULT_LEXICON_PATH)).get(name)
    if isinstance(section, dict) and section:
        return section
    return _CODE_FALLBACK[name]


def vocabulary(name: str) -> tuple[str, ...]:
    """이름 붙은 표면어 목록. 파일에 없으면 코드 폴백(둘 다 없으면 빈 튜플)."""
    values = _section("vocabularies").get(name)
    if not isinstance(values, list):
        values = _CODE_FALLBACK["vocabularies"].get(name, [])
    return tuple(value for value in values if isinstance(value, str) and value)


def _terms_for(spec: dict[str, Any]) -> tuple[str, ...]:
    """패턴 정의(include/extra/exclude) → 낱말 집합. 순서는 긴 것 우선으로 결정론 정렬."""
    collected: list[str] = []
    for group in spec.get("include", []) or []:
        collected.extend(vocabulary(str(group)))
    collected.extend(str(item) for item in (spec.get("extra") or []) if isinstance(item, str))
    excluded = {str(item) for item in (spec.get("exclude") or [])}
    unique = {term for term in collected if term and term not in excluded}
    # 긴 낱말 우선(접두어 겹침 방지) → 같은 길이는 사전순(재현 가능성).
    return tuple(sorted(unique, key=lambda term: (-len(term), term)))


def terms(pattern_name: str) -> tuple[str, ...]:
    """패턴이 실제로 쓰는 낱말 목록(진단·테스트용)."""
    spec = _section("patterns").get(pattern_name)
    if not isinstance(spec, dict):
        spec = _CODE_FALLBACK["patterns"][pattern_name]
    return _terms_for(spec)


@functools.lru_cache(maxsize=64)
def _compiled(pattern_name: str, flags: int) -> re.Pattern[str]:
    words = terms(pattern_name)
    if not words:
        raise ValueError(f"lexicon pattern '{pattern_name}' has no terms")
    return re.compile("|".join(re.escape(word) for word in words), flags)


class LexiconPattern:
    """지연 컴파일되는 사전 기반 패턴. ``re.Pattern`` 처럼 쓰도록 위임한다.

    모듈 최상위 상수를 그대로 대체하기 위한 얇은 프록시다 — 호출부(``_X_RE.search(...)``)를
    고치지 않고 어휘만 데이터로 옮길 수 있다. 컴파일은 첫 사용 시점이라 import 순서에 영향받지 않는다.
    """

    __slots__ = ("_name", "_flags")

    def __init__(self, name: str, flags: int = 0) -> None:
        self._name = name
        self._flags = flags

    @property
    def name(self) -> str:
        return self._name

    @property
    def compiled(self) -> re.Pattern[str]:
        return _compiled(self._name, self._flags)

    @property
    def pattern(self) -> str:
        return self.compiled.pattern

    @property
    def terms(self) -> tuple[str, ...]:
        return terms(self._name)

    def search(self, *args: Any, **kwargs: Any): return self.compiled.search(*args, **kwargs)
    def match(self, *args: Any, **kwargs: Any): return self.compiled.match(*args, **kwargs)
    def fullmatch(self, *args: Any, **kwargs: Any): return self.compiled.fullmatch(*args, **kwargs)
    def finditer(self, *args: Any, **kwargs: Any): return self.compiled.finditer(*args, **kwargs)
    def findall(self, *args: Any, **kwargs: Any): return self.compiled.findall(*args, **kwargs)
    def split(self, *args: Any, **kwargs: Any): return self.compiled.split(*args, **kwargs)
    def sub(self, *args: Any, **kwargs: Any): return self.compiled.sub(*args, **kwargs)
    def subn(self, *args: Any, **kwargs: Any): return self.compiled.subn(*args, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 진단용
        return f"LexiconPattern({self._name!r}, terms={len(self.terms)})"


def pattern(name: str, flags: int = 0) -> LexiconPattern:
    """사전 기반 패턴 하나. 모듈 최상위에서 ``_X_RE = pattern('x')`` 로 쓴다."""
    return LexiconPattern(name, flags)


def alternation(*vocabularies: str, extra: Iterable[str] = ()) -> str:
    """어휘들을 합친 교대 **문자열**(다른 정규식에 끼워 넣을 때). 이스케이프·정렬 규칙은 동일하다."""
    return "|".join(re.escape(word) for word in _terms_for({"include": list(vocabularies), "extra": list(extra)}))


def exclusions() -> dict[str, tuple[str, ...]]:
    """자기 어휘의 일부를 빼고 있는 패턴들 — "이 누락이 의도인가?" 검토 목록.

    이관은 낱말 집합을 그대로 옮기는 것이 원칙이라, 역사적 누락은 exclude 로 보존된다. 이 함수는
    그 누락을 숨기지 않고 세어 보여 준다. 하나씩 검토해 없애는 것이 이관 이후의 후속 작업이다.
    """
    specs = _section("patterns")
    out: dict[str, tuple[str, ...]] = {}
    for name in pattern_names():
        spec = specs.get(name) or _CODE_FALLBACK["patterns"].get(name) or {}
        excluded = tuple(str(item) for item in (spec.get("exclude") or []))
        if excluded:
            out[name] = excluded
    return out


def note(pattern_name: str) -> str | None:
    """패턴에 적힌 사유(있으면)."""
    spec = _section("patterns").get(pattern_name) or _CODE_FALLBACK["patterns"].get(pattern_name) or {}
    value = spec.get("note")
    return value if isinstance(value, str) and value else None


def pattern_names() -> tuple[str, ...]:
    """선언된 패턴 이름들(계약 테스트가 전수 검증에 쓴다)."""
    names = set(_CODE_FALLBACK["patterns"]) | {
        name for name in _section("patterns") if isinstance(name, str)
    }
    return tuple(sorted(names))
