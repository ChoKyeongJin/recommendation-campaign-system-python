"""구매 의미의 **형태 판정** 계층 — 활용형·명사형·존재/부재를 한곳에서 읽는다.

위치: 구매 의미의 1순위 판정은 :mod:`semantic_signal` 의 구조화 추출이 갖는다(뜻을 문맥으로 읽고
실제 발생·의향·가정·단순 언급·부정을 상태로 구분한다). 이 모듈은 그 아래 **2순위 폴백**이자, 창
귀속에 필요한 **스팬 공급자**다 — 구조화 추출은 뜻을 주지 위치를 주지 않기 때문이다.
:func:`rule_signal` 이 이 계층을 구조화 신호로 올려 주는 어댑터이며, 표면형 목록으로 발생을
판정하는 유일한 정당한 자리는 여기(장애 대응 폴백)뿐이다.

배경: '구매 신호'를 읽는 규칙이 파서·재작성 게이트·상품 추출에 각자 흩어져 있었다. 그래서 같은
뜻의 표현형 하나가 한 곳에서만 새는 결함이 반복됐다.

  * ``구매/구입/주문`` 만 아는 곳(``purchase_membership_verb``)과 ``샀`` 까지 아는 곳(``purchase_verb``)이
    갈라져 있어 "노트북을 샀다"가 구매 **존재 조건**으로 승격되지 않았다.
  * ``구매 이력`` 처럼 용언 없이 명사형으로만 남은 표현("… 구매 이력")은 어느 극성에도 안 잡혔다.
    재작성/절 분리가 동사를 명사구로 바꾸면 구매 조건이 통째로 사라진다.
  * ``산`` 은 ``사다`` 의 관형형이면서 동시에 명사 ``山`` 이라, 낱말 목록에 그냥 넣으면 '부산 고객'이
    구매 조건이 된다. 그래서 아무도 넣지 못했고 "기저귀 산 사람들"은 영영 안 읽혔다.

이 모듈이 그 셋을 한 계층으로 합친다.

    어휘(vocabulary)  — :mod:`lexicon_patterns` 가 소유한다(데이터). 여기서는 조합만 한다.
    형태(morphology)  — 활용형·명사형 결합 규칙. 구조라 코드가 소유한다.
    중의성(ambiguity) — 동음이의 표면형의 **문맥 조건**. 구조라 코드가 소유한다.

좌표계 주의: 파서의 구매 판정은 조사·어미 결합을 보기 위해 **공백을 지운(compact)** 텍스트에서
돈다. 그런데 ``산`` 의 중의성은 띄어쓰기가 있어야만 풀린다("기저귀 산 사람" vs "부산 고객").
그래서 중의 표면형만 원문(공백 보존)에서 읽고 :class:`SurfaceSpan` 으로 compact 좌표를 돌려준다 —
문자열을 고쳐 쓰지 않으므로 다른 파서가 보는 원문·스팬은 그대로다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import lexicon_patterns
import semantic_signal

# ── 어휘 조합(낱말은 lexicon_patterns 가 소유) ────────────────────────────────────────────
# 구매 존재/부재가 공유하는 동사. 두 극성이 같은 목록을 봐야 표현형 하나가 한쪽에서만 새지 않는다.
VERB_ALT = lexicon_patterns.alternation("purchase_membership_verb")
# 어간만으로 완료인 형태('샀다'·'샀던'). 뒤에 완료 어미를 요구하지 않는 갈래다.
COMPLETED_STEM_ALT = lexicon_patterns.alternation("purchase_verb_completed_stem")
# 사건 동사 뒤에 붙어 '그 사건의 기록'을 뜻하는 명사(구매 이력/내역/기록/경험/실적).
RECORD_ALT = lexicon_patterns.alternation("event_record_noun")
# 완료·존재 어미. '구매했다'와 '구매 이력이 있는'이 같은 뜻으로 묶이는 근거다.
COMPLETION_ALT = lexicon_patterns.alternation("event_completion_ending")
# 부정 표지. 명사형 단독 승격이 '구매 이력이 없는'을 긍정으로 뒤집지 않게 하는 유일한 안전장치다.
NEGATION_ALT = lexicon_patterns.alternation(
    "generic_negation", extra=["하지않", "안함", "안한", "안했", "안하"]
)
# 용언 앞 부정 부사('안 샀'·'못 샀'). 완료 어간 단독 갈래를 부재 표현으로부터 지킨다.
NEGATION_ADVERB_ALT = lexicon_patterns.alternation("negation_adverb")
# 사건 명사 앞 부정 접두사('미구매'). 부정형이 '미 + 동사'로 읽는 것과 대칭으로 긍정형은 배제한다.
NEGATION_PREFIX_ALT = lexicon_patterns.alternation("negation_prefix")
MEMBER_ALT = lexicon_patterns.alternation("member_noun", "member_noun_informal", "member_noun_role")
# 구매의 목적어 자리에 오는 일반 상품 명사('상품 구매'처럼 동사가 명사구의 머리인 형태를 읽는다).
PRODUCT_NOUN_ALT = lexicon_patterns.alternation("product_noun")
# 조사. 목적격은 타동사의 목적어 앵커라 중의 표면형 판정에도 쓰인다.
OBJECT_PARTICLE_ALT = lexicon_patterns.alternation("object_particle")
BOUND_PARTICLE_ALT = lexicon_patterns.alternation("bound_particle")

_HANGUL = r"가-힣"


def _no_negation_ahead_alt() -> str:
    """뒤따르는 조사 몇 개를 지나 부정 표지가 오면 긍정이 아니다.

    단일 lookahead 로 쓴다 — 조사를 소비한 뒤 부정을 검사하면 백트래킹으로 조사 길이를 줄여 가며
    ``구매 이력이 없는`` 이 결국 긍정으로 통과한다.
    """
    return rf"(?!(?:{BOUND_PARTICLE_ALT})*(?:{NEGATION_ALT}))"


def positive_membership_alt() -> str:
    """구매 **존재** 표면형(compact 텍스트용 정규식 소스).

    다섯 갈래를 한 목록으로 둔다 — 새 표현형은 갈래를 고르는 문제이지 새 정규식을 만드는 문제가 아니다.

      1. 동사 + (기록 명사)? + 조사* + 완료 어미      '구매했다', '구매 이력이 있는'
      2. 동사 + 기록 명사 (부정 문맥 아님)            '구매 이력', '구입 경험'  ← 재작성이 만드는 명사형
      3. 상품 명사 + 동사 (부정 문맥 아님)            '상품 구매'               ← 동사가 명사구의 머리
      4. 완료 어간 단독 (부정 부사·부정 문맥 아님)    '샀다', '샀던'
      5. 동사 + 회원 명사                             '구매 고객'

    모든 갈래에 부정 접두사 배제(``미``)를 건다 — 부정형이 '미 + 동사'를 부재로 읽으므로, 긍정형이
    같은 자리를 존재로도 읽으면 '미구매 고객'이 두 극성을 동시에 갖는다.
    """
    guard = _no_negation_ahead_alt()
    unprefixed = rf"(?<!(?:{NEGATION_PREFIX_ALT}))"
    return "|".join((
        rf"{unprefixed}(?:{VERB_ALT})(?:{RECORD_ALT})?(?:{BOUND_PARTICLE_ALT})*(?:{COMPLETION_ALT})",
        rf"{unprefixed}(?:{VERB_ALT})(?:{RECORD_ALT}){guard}",
        rf"(?:{PRODUCT_NOUN_ALT})(?:{VERB_ALT}){guard}",
        rf"(?<!(?:{NEGATION_ADVERB_ALT}))(?:{COMPLETED_STEM_ALT}){guard}",
        rf"{unprefixed}(?:{VERB_ALT})(?=(?:{MEMBER_ALT}))",
    ))


def negative_membership_alt() -> str:
    """구매 **부재** 표면형(compact 텍스트용 정규식 소스). 긍정형과 같은 동사·기록 명사 어휘를 쓴다."""
    return "|".join((
        rf"(?:{VERB_ALT})(?:{RECORD_ALT})?(?:{BOUND_PARTICLE_ALT})*(?:{NEGATION_ALT})",
        rf"미(?:{VERB_ALT})",
    ))


POSITIVE_MEMBERSHIP_RE = re.compile(positive_membership_alt())
NEGATIVE_MEMBERSHIP_RE = re.compile(negative_membership_alt())


# ── 중의 표면형(공백이 있어야 풀리는 것) ──────────────────────────────────────────────────
# '산'(사다의 관형형 ↔ 山), '사다'(↔ 사다리), '사서'(↔ 司書), '사고'(↔ 事故).
# 낱말은 사전이 소유하고, "언제 구매 동사인가"라는 문맥 조건만 여기 둔다.
#
#   공통: 원문에서 **한 낱말로 서 있어야 한다**(앞뒤가 한글이 아님). '부산 고객'·'한라산에'·'등산을'
#         은 이 조건에서 탈락하고, '높은 산을'은 뒤에 조사가 붙어 낱말 경계가 깨지므로 탈락한다.
#   추가: 목적격 조사로 끝나는 앞말이 있거나('노트북을 산'), 뒤에 사람 명사가 온다('기저귀 산 사람').
#         '산 정상'처럼 둘 다 없으면 구매로 보지 않는다.
# 연결형('사서'·'사고')은 사람 명사가 뒤에 와도 사고(事故)·사서(司書) 오탐이 남으므로 목적격 조사만 앵커로 쓴다.
_COLLOQUIAL_VOCABULARIES = (
    ("purchase_verb_colloquial", False),
    ("purchase_verb_colloquial_object_bound", True),
)

# 앞말이 목적격 조사로 끝나는가('노트북을 ', '기저귀를'). 중의 표면형 바로 앞만 본다.
_OBJECT_ANCHOR_RE = re.compile(rf"(?:{OBJECT_PARTICLE_ALT})\s*$")
# 관형형이 수식하는 사람 명사가 바로 뒤에 오는가('산 사람들', '산 고객').
_MEMBER_AFTER_RE = re.compile(rf"^\s*(?:{MEMBER_ALT})")


def verb_surface_alt() -> str:
    """구매 동사 교대(**공백이 살아 있는 원문**용).

    상품 추출 패턴들이 "이 목적어가 구매의 목적어인가"를 확인할 때 쓴다. 중의 활용형은 낱말 경계
    조건을 함께 걸어 '산책'·'부산' 같은 표면형을 배제한다 — 여기서는 앞말(목적격 조사)이 패턴 자체에
    이미 앵커로 들어 있으므로 경계 조건만으로 충분하다.
    """
    colloquial = sorted(
        {
            word
            for vocabulary, _bound in _COLLOQUIAL_VOCABULARIES
            for word in lexicon_patterns.vocabulary(vocabulary)
            if word
        },
        key=lambda item: (-len(item), item),
    )
    alternatives = [VERB_ALT]
    if colloquial:
        joined = "|".join(re.escape(word) for word in colloquial)
        alternatives.append(rf"(?<![{_HANGUL}])(?:{joined})(?![{_HANGUL}])")
    return "|".join(alternatives)


@dataclass(frozen=True)
class SurfaceSpan:
    """compact 좌표의 구간. ``re.Match`` 대신 쓰이므로 ``span()`` 만 있으면 된다."""

    start: int
    end: int

    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


def _standalone_form_re(vocabulary: str) -> re.Pattern[str] | None:
    """어휘의 낱말들이 **한 낱말로 서 있을 때만** 잡는 패턴(앞뒤가 한글이 아니어야 한다)."""
    words: Iterable[str] = lexicon_patterns.vocabulary(vocabulary)
    unique = tuple(sorted({word for word in words if word}, key=lambda item: (-len(item), item)))
    if not unique:
        return None
    alternation = "|".join(re.escape(word) for word in unique)
    return re.compile(rf"(?<![{_HANGUL}])(?:{alternation})(?![{_HANGUL}])")


def _colloquial_matches(text: str) -> list[tuple[int, int]]:
    """중의 표면형이 실제로 '사다'인 원문 구간들. 문맥 조건을 통과한 것만 남는다."""
    source = text if isinstance(text, str) else ""
    found: list[tuple[int, int]] = []
    for vocabulary, needs_object_anchor in _COLLOQUIAL_VOCABULARIES:
        regex = _standalone_form_re(vocabulary)
        if regex is None:
            continue
        for match in regex.finditer(source):
            before, after = source[: match.start()], source[match.end():]
            has_object = bool(_OBJECT_ANCHOR_RE.search(before))
            if has_object or (not needs_object_anchor and _MEMBER_AFTER_RE.search(after)):
                found.append(match.span())
    return sorted(set(found))


def _compact_index_map(text: str) -> list[int]:
    """'원문 index → compact index' 표. 파서가 쓰는 compact 좌표계와 스팬을 맞추기 위한 것이다."""
    mapping: list[int] = []
    seen = 0
    for char in text:
        mapping.append(seen)
        if not char.isspace():
            seen += 1
    mapping.append(seen)
    return mapping


def colloquial_spans(text: str) -> tuple[SurfaceSpan, ...]:
    """중의 활용형('기저귀 산 사람')을 원문에서 읽어 compact 좌표 구간으로 돌려준다."""
    source = text if isinstance(text, str) else ""
    mapping = _compact_index_map(source)
    return tuple(
        SurfaceSpan(mapping[start], mapping[end]) for start, end in _colloquial_matches(source)
    )


# ── 종합 판정 ─────────────────────────────────────────────────────────────────────────────
EXISTS = "purchase:exists"
ABSENT = "purchase:absent"

SIGNAL = "purchase"


def membership_signals(text: str) -> frozenset[str]:
    """텍스트가 담은 구매 의미(존재/부재) 집합 — **형태 판정 계층**의 답이다.

    이 함수는 더 이상 구매 의미의 1순위 판정기가 아니다. 파이프라인의 1순위는
    :func:`semantic_signal.resolve` 의 구조화 추출이고, 여기 형태 판정은 그 위가 침묵하거나
    실패했을 때의 2순위 폴백이다(:func:`rule_signal` 이 그 어댑터다).

    그래도 이 계층이 남아 있는 이유는 둘이다. (i) 키가 없는 오프라인·테스트 실행에서 기존 결정론
    동작을 그대로 재현해야 한다. (ii) 창 귀속에 필요한 **스팬**은 형태에서만 나온다 — 구조화
    추출은 뜻을 주지 위치를 주지 않는다.
    """
    source = text if isinstance(text, str) else ""
    compact = source.replace(" ", "").casefold()
    signals: set[str] = set()
    if NEGATIVE_MEMBERSHIP_RE.search(compact):
        signals.add(ABSENT)
    if POSITIVE_MEMBERSHIP_RE.search(compact) or _colloquial_matches(source):
        signals.add(EXISTS)
    return frozenset(signals)


def has_purchase_expression(text: str) -> bool:
    """구매(존재/부재 무관) 표현이 있는가 — 형태 판정만으로 답하는 저수준 질문."""
    return bool(membership_signals(text))


def rule_signal(text: str) -> semantic_signal.SemanticSignal:
    """형태 판정 → 구조화 신호. 폴백 사슬 2순위(:func:`semantic_signal.resolve`)의 어댑터다.

    형태가 줄 수 있는 것은 '관계를 주장했는가'와 그 극성뿐이다. 의향·가정·단순 언급은 활용형으로
    구분되지 않으므로 여기서는 만들지 않는다 — 그 구분은 1순위 구조화 추출의 몫이고, 폴백이
    없는 뜻을 지어내면 폴백이 조용한 오탐원이 된다.

    극성이 둘 다 잡히면("A는 샀고 B는 안 샀다") 둘을 **각각의 주장으로** 남긴다. 대상 이름은
    형태로 알 수 없어 대상 미지정 주장이 되고, 문장 단위 롤업 정책이 발생으로 접는다.
    """
    signals = membership_signals(text)
    claims = []
    if EXISTS in signals:
        claims.append(semantic_signal.claim(SIGNAL, semantic_signal.COMPLETED))
    if ABSENT in signals:
        claims.append(semantic_signal.claim(SIGNAL, semantic_signal.DENIED))
    return semantic_signal.build(SIGNAL, claims, source=semantic_signal.SOURCE_RULES)
