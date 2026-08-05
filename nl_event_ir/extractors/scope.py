"""대상(TARGET)과 범위(SCOPE) 추출.

이 파일이 이 리팩터링의 핵심 하나를 담고 있다: **브랜드명·상품명은 사전으로 찾지 않는다.**

사전으로 찾을 수 없는 이유는 원리적이다. ``나이키``·``아디다스``·``뉴발란스`` 는 카탈로그
데이터이고, 카탈로그는 계속 바뀐다. 사전에 넣는 순간 그 사전은 **카탈로그의 낡은 사본**이
되어, 어제 등록된 브랜드는 영영 인식되지 않으면서도 아무 오류도 내지 않는다.

그래서 여기서는 정반대로 접근한다. 다른 축들이 읽어 간 자리를 전부 걷어내고 **남은 자리**를
후보로 내놓는다. 남은 자리가 무엇인지는 이 파일이 판정하지 않고
:mod:`nl_event_ir.resolver` 가 repository 에 물어본다. 그 결과 이 파일에는 불용어 목록이
없다 — 후보가 실제 데이터에 없으면 repository 가 못 찾을 뿐이고, 그것이 정답이다.

남는 문법 지식은 조사(particle)뿐이다. ``나이키를`` 에서 ``를`` 을 떼는 일은 어휘가 아니라
형태론이고, 조사는 닫힌 집합이라 늘어나지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from nl_event_ir.aliases import AliasRegistry, AliasSection, find_alias_matches
from nl_event_ir.enums import EntityType
from nl_event_ir.tokenizer import SemanticToken, TokenKind, uncovered_spans

__all__ = ["ScopeExtractor", "TargetExtractor"]

# 격조사·보조사. 닫힌 문법 집합이므로 코드가 소유한다(어휘가 아니라 형태론).
_PARTICLES = (
    "에서부터",
    "으로부터",
    "이라는",
    "라는",
    "에서",
    "으로",
    "부터",
    "까지",
    "하고",
    "이나",
    "에게",
    "께서",
    "을",
    "를",
    "이",
    "가",
    "은",
    "는",
    "의",
    "에",
    "와",
    "과",
    "랑",
    "도",
    "만",
    "로",
    "인",
)

_PARTICLE_SUFFIX_RE = re.compile(
    r"(?:" + "|".join(sorted(_PARTICLES, key=lambda word: (-len(word), word))) + r")$"
)

# 후보가 아직 접지되지 않았음을 나타내는 신뢰도. resolver 가 확정하면 덮어쓴다.
_UNRESOLVED_CANDIDATE_CONFIDENCE = 0.5

# 목적격 조사. 이것이 붙은 어절은 **동사의 논항**이지 잉여어가 아니다.
_OBJECT_PARTICLES = ("을", "를")

# 잔여 후보의 출처 표지. composer 가 '접지 실패를 오류로 볼 것인가'를 이 값으로 가른다.
#
# 구분이 필요한 이유: 잔여 후보에는 불용어 목록을 두지 않기로 했으므로 ``동안``·``상위`` 같은
# 잉여어도 후보로 올라온다. 그것들이 카탈로그에 없다고 오류를 내면 거의 모든 문장이 실패한다.
# 반대로 **목적격 조사가 붙은** 말은 화자가 '이것을 대상으로'라고 명시한 것이므로, 카탈로그에
# 없으면 조용히 버려서는 안 된다 — 버리면 모수가 전체로 넓어진다.
RESIDUAL_SOURCE = "residual"
RESIDUAL_OBJECT_SOURCE = "residual_object"

# 엔티티 값 후보로 인정하는 최소 길이. 조사 절삭 가드와 후보 자격 검사가 같은 값을 써야
# '떼면 후보가 못 되는' 절삭이 일어나지 않는다.
_MINIMUM_STEM_LENGTH = 2


class TargetExtractor:
    """``회원``/``고객``/``사용자`` 처럼 **조회 대상**을 가리키는 명사를 읽는다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        for start, end, surface, canonical in find_alias_matches(
            text, self._registry, AliasSection.ENTITY
        ):
            if canonical != EntityType.MEMBER.value:
                continue
            tokens.append(
                SemanticToken(
                    kind=TokenKind.TARGET,
                    value=EntityType.MEMBER,
                    start=start,
                    end=end,
                    raw_text=surface,
                )
            )
        return tokens


class ScopeExtractor:
    """엔티티 **종류** 표지(ENTITY_TYPE)와 엔티티 **값** 후보(ENTITY_VALUE)를 읽는다.

    값 후보는 다른 extractor 가 끝난 뒤에야 계산할 수 있으므로 :meth:`extract_residual`
    로 분리했다. 이 2단계 구조가 '사전 없이 데이터 값 찾기'를 가능하게 한다.
    """

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        for start, end, surface, canonical in find_alias_matches(
            text, self._registry, AliasSection.ENTITY
        ):
            if canonical == EntityType.MEMBER.value:
                continue  # 대상 명사는 TargetExtractor 의 몫이다.
            if not _starts_a_word(text, start):
                # 어절 한가운데서 매치된 종류 표지는 실제 표지가 아니라 값의 일부다.
                # ``언노운브랜드를`` 의 ``브랜드`` 를 표지로 읽으면 브랜드명이 ``언노운`` 으로
                # 잘려 엉뚱한 값을 찾게 된다.
                continue
            tokens.append(
                SemanticToken(
                    kind=TokenKind.ENTITY_TYPE,
                    value=EntityType(canonical),
                    start=start,
                    end=end,
                    raw_text=surface,
                )
            )
        return tokens

    def extract_residual(
        self,
        text: str,
        tokens: Sequence[SemanticToken],
    ) -> list[SemanticToken]:
        """아무도 읽지 않은 자리를 엔티티 값 **후보**로 내놓는다.

        여기서 후보를 걸러 내지 않는 것이 의도다. 불용어 목록으로 거르기 시작하면 그 목록이
        곧 예전 ``exclude`` 사전이 된다. 후보의 진위는 repository 조회가 판정한다.
        """

        candidates: list[SemanticToken] = []
        for span_start, span_end in uncovered_spans(text, tokens):
            fragment = text[span_start:span_end]
            for word_start, word in _iter_words(fragment):
                absolute_start = span_start + word_start
                if not _starts_a_word(text, absolute_start):
                    # 앞 글자가 이미 다른 토큰에 읽힌 **같은 어절**의 일부다. 즉 이 조각은
                    # 값이 아니라 어미다('로그인하지' 의 '하지'). 값 후보는 어절 첫머리에서만
                    # 시작한다 — 불용어 목록이 아니라 형태 위치로 거른다.
                    continue
                stem = _strip_particles(word)
                if not _is_candidate(stem):
                    continue
                candidates.append(
                    SemanticToken(
                        kind=TokenKind.ENTITY_VALUE,
                        value=stem,
                        start=absolute_start,
                        end=absolute_start + len(stem),
                        raw_text=stem,
                        confidence=_UNRESOLVED_CANDIDATE_CONFIDENCE,
                        source=(
                            RESIDUAL_OBJECT_SOURCE
                            if _is_object_marked(word, stem)
                            else RESIDUAL_SOURCE
                        ),
                    )
                )
        return candidates


def _is_object_marked(word: str, stem: str) -> bool:
    """그 어절이 목적격 조사로 끝났는가 — 즉 화자가 명시한 **대상**인가.

    ``나이키를`` 은 대상이고 ``동안`` 은 아니다. 이 구분이 '카탈로그에 없으면 오류'와
    '없으면 그냥 잉여어'를 가르는 유일한 근거다. 형태론적 신호만 쓰므로 낱말 목록이 필요 없다.
    """

    return word != stem and word[len(stem) :] in _OBJECT_PARTICLES


def _starts_a_word(text: str, index: int) -> bool:
    """그 위치가 어절의 첫머리인가(문장 처음이거나 앞이 공백)."""

    return index == 0 or text[index - 1].isspace()


def _iter_words(fragment: str) -> list[tuple[int, str]]:
    """공백으로 나눈 어절과 그 시작 위치."""

    return [(match.start(), match.group(0)) for match in re.finditer(r"\S+", fragment)]


def _strip_particles(word: str) -> str:
    """어절 끝의 조사를 한 번 떼어 낸다.

    한 번만 떼는 이유는 과잉 절삭을 막기 위해서다. 반복해서 떼면 ``도미``·``가나`` 같은
    실제 값이 조사 연쇄로 오인되어 사라진다.

    절삭 결과가 너무 짧아지면 **절삭하지 않는다.** 조사와 같은 음절로 끝나는 두 글자
    브랜드가 실제로 많기 때문이다 — ``진로``·``새로``·``제로``·``이도`` 에서 끝 음절을 조사로
    보면 남는 것은 한 글자뿐이고, 그 한 글자는 후보 자격을 잃어 값이 통째로 사라진다.
    조사가 실제로 붙은 ``진로를`` 은 ``진로`` 가 남으므로 이 가드에 걸리지 않는다.
    """

    stripped = _PARTICLE_SUFFIX_RE.sub("", word)
    if len(stripped) < _MINIMUM_STEM_LENGTH:
        return word
    return stripped


def _is_candidate(stem: str) -> bool:
    """엔티티 값 후보가 될 수 있는 최소 조건.

    의미 판단은 하지 않는다. 숫자·기호만으로 이루어졌거나 한 글자인 조각만 제외한다 —
    한 글자짜리 브랜드·상품명이 없지는 않으나, 조사 잔여물과 구분할 근거가 문장 안에 없다.
    """

    if len(stem) < _MINIMUM_STEM_LENGTH:
        return False
    return any(character.isalnum() for character in stem) and not stem.isdigit()
