"""형태·표현 정규화 — 활용형을 **사전이 아니라 규칙**으로 되돌리는 계층.

기존 사전이 ``purchase_verb`` → ``purchase_verb_completed_stem`` → ``purchase_verb_colloquial``
→ ``purchase_verb_colloquial_object_bound`` 로 번식한 이유는 한국어 활용형을 목록으로 셌기
때문이다. 활용은 목록이 아니라 **규칙**이라 목록으로는 끝나지 않는다.

여기서 다루는 대상은 좁다. 한자어 명사+하다 동사(``구매하다``/``로그인하다``/``가입하다``)는
정규화가 **필요 없다** — 표면형이 어떻게 활용되든 어간 ``구매``/``로그인``/``가입`` 이 문자열
안에 그대로 남으므로 alias 부분 일치로 잡힌다. 정규화가 실제로 필요한 것은 어간이 변형되는
고유어 동사, 즉 ``사다``(사/산/샀/삽니다) 계열뿐이다. 그래서 이 파일의 활용 규칙은 적다.

설계 제약(중요): **상품명·브랜드명을 훼손하지 않는다.** 그래서 모든 활용 규칙에는 한글
경계 조건이 붙는다. ``산`` 을 무조건 ``사다`` 로 바꾸면 ``부산``·``계산``·``등산`` 이 깨진다.
경계를 못 지키는 표현은 여기서 **바꾸지 않고 그대로 둔다** — 규칙이 확실하지 않으면 원문
보존이 정답이고, 그 문장은 legacy adapter 나 semantic fallback 으로 내려간다(CLAUDE.md 11).

형태소 분석기를 직접 구현하지 않는다. :class:`TextNormalizer` 프로토콜을 두었으므로 나중에
mecab/kiwi 같은 분석기를 붙일 때 이 파일을 고치는 것이 아니라 **다른 구현체를 주입**한다.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from korean_number_normalizer import NATIVE_KOREAN_NUMBER_VALUES
from nl_event_ir.models import NormalizedText, TextEdit

__all__ = [
    "NormalizationRule",
    "PassthroughNormalizer",
    "RuleBasedKoreanNormalizer",
    "TextNormalizer",
]

logger = logging.getLogger(__name__)


class TextNormalizer(Protocol):
    """정규화기 인터페이스.

    ``normalize`` 는 문자열만 필요할 때 쓰는 편의 메서드이고, 파이프라인은 위치 매핑이
    필요하므로 :meth:`normalize_with_spans` 를 쓴다.
    """

    def normalize(self, text: str) -> str: ...

    def normalize_with_spans(self, text: str) -> NormalizedText: ...


@dataclass(frozen=True, slots=True)
class NormalizationRule:
    """이름 붙은 정규화 규칙 하나.

    이름을 붙이는 이유는 진단이다. '왜 이 문장이 이렇게 바뀌었는가'를 규칙 단위로 되짚을 수
    있어야 규칙을 안전하게 늘릴 수 있다.
    """

    name: str
    pattern: re.Pattern[str]
    replacement: Callable[[re.Match[str]], str]


# ── 세그먼트 기반 치환(원문 위치 보존) ──────────────────────────────────────────────


@dataclass(slots=True)
class _Segment:
    """정규화 텍스트 조각 하나와 그것이 유래한 원문 구간."""

    text: str
    original_start: int
    original_end: int


class _SegmentedText:
    """치환을 적용하면서 원문 좌표를 잃지 않는 작업 버퍼.

    글자 하나를 세그먼트 하나로 시작해서, 규칙이 매치될 때마다 그 구간의 세그먼트들을
    **하나의 새 세그먼트**로 접는다. 접힌 세그먼트는 원문 구간 전체를 기억하므로,
    ``두 번`` → ``2번`` 처럼 길이가 달라져도 나중에 원문 위치를 되돌릴 수 있다.
    """

    def __init__(self, base: str) -> None:
        self._base = base
        self._segments: list[_Segment] = [
            _Segment(character, index, index + 1) for index, character in enumerate(base)
        ]

    def render(self) -> str:
        return "".join(segment.text for segment in self._segments)

    def _offset_index(self) -> list[int]:
        """현재 문자열의 각 글자 위치 → 세그먼트 인덱스."""

        mapping: list[int] = []
        for segment_index, segment in enumerate(self._segments):
            mapping.extend([segment_index] * len(segment.text))
        return mapping

    def apply(self, rule: NormalizationRule) -> int:
        """규칙을 전부 적용하고 치환 횟수를 반환한다."""

        applied = 0
        while True:
            current = self.render()
            mapping = self._offset_index()
            match = rule.pattern.search(current)
            if match is None:
                return applied

            start, end = match.start(), match.end()
            if start == end:  # 빈 매치는 무한 루프가 된다 — 규칙 오류로 본다.
                raise ValueError(f"normalization rule '{rule.name}' matched an empty span")

            first_segment = mapping[start]
            # ``end`` 는 반열린 경계라 mapping[end-1] 이 마지막 글자의 세그먼트다.
            last_segment = mapping[end - 1]
            replacement = rule.replacement(match)

            folded = _Segment(
                text=replacement,
                original_start=self._segments[first_segment].original_start,
                original_end=self._segments[last_segment].original_end,
            )
            self._segments[first_segment : last_segment + 1] = [folded]
            applied += 1

            if applied > _MAX_REPLACEMENTS_PER_RULE:
                raise ValueError(
                    f"normalization rule '{rule.name}' exceeded "
                    f"{_MAX_REPLACEMENTS_PER_RULE} replacements — suspected non-converging rule"
                )

    def to_normalized_text(self, original: str) -> NormalizedText:
        normalized = self.render()
        edits: list[TextEdit] = []
        cursor = 0
        for segment in self._segments:
            segment_end = cursor + len(segment.text)
            source = self._base[segment.original_start : segment.original_end]
            if source != segment.text:
                edits.append(
                    TextEdit(
                        original_start=segment.original_start,
                        original_end=segment.original_end,
                        normalized_start=cursor,
                        normalized_end=segment_end,
                    )
                )
            cursor = segment_end
        return NormalizedText(original=original, normalized=normalized, edits=tuple(edits))


_MAX_REPLACEMENTS_PER_RULE: Final[int] = 512


# ── 규칙 정의 ────────────────────────────────────────────────────────────────────

# 고유어 수사는 단독으로는 흔한 음절이라 위험하다('세'/'네'). 반드시 **선언된 셈숫자 단위**에
# 붙어 있을 때만 숫자로 바꾼다. 값 자체는 저장소의 기존 소유자를 재사용한다(중복 선언 금지).
#
# 기간 단위(달·주·개월·년)도 포함한다. ``한 달 동안`` 의 ``한`` 을 숫자로 만들지 않으면 기간
# 규칙이 ``\d+`` 를 찾지 못해 **기간 조건이 통째로 사라진다** — 조회가 조용히 전 기간으로
# 넓어지는 실패다. ``개월`` 은 ``개`` 보다 앞에 와야 한다(긴 것 우선 정렬이 보장한다).
# 그렇지 않으면 ``네 개월`` 이 ``4월``(4월이라는 달)로 바뀐다.
_COUNTER_UNITS: Final[tuple[str, ...]] = (
    "번",
    "회",
    "건",
    "개",
    "명",
    "가지",
    "종",
    "개월",
    "달",
    "주",
    "년",
)

_NATIVE_NUMBER_ALTERNATION: Final[str] = "|".join(
    sorted(NATIVE_KOREAN_NUMBER_VALUES, key=lambda word: (-len(word), word))
)
_COUNTER_ALTERNATION: Final[str] = "|".join(
    sorted(_COUNTER_UNITS, key=lambda word: (-len(word), word))
)


def _replace_native_number(match: re.Match[str]) -> str:
    value = NATIVE_KOREAN_NUMBER_VALUES[match.group("number")]
    return f"{value}{match.group('counter')}"


_DEFAULT_RULES: Final[tuple[NormalizationRule, ...]] = (
    # 영문 대문자만 소문자로. 한글에는 대소문자가 없으므로 str.lower() 를 문자열 전체에
    # 거는 것과 결과는 같지만, 규칙을 명시적으로 두어야 나중에 대상 범위를 좁힐 수 있다.
    NormalizationRule(
        name="ascii_lowercase",
        pattern=re.compile(r"[A-Z]+"),
        replacement=lambda match: match.group(0).lower(),
    ),
    # 문장 부호 정리. 마침표는 건드리지 않는다 — ``2026.07.01`` 의 구분자이기 때문이다.
    NormalizationRule(
        name="strip_sentence_punctuation",
        pattern=re.compile(r"[?!¿¡]+"),
        replacement=lambda match: "",
    ),
    NormalizationRule(
        name="comma_to_space",
        pattern=re.compile(r"[,、]+"),
        replacement=lambda match: " ",
    ),
    NormalizationRule(
        name="normalize_quotes",
        pattern=re.compile(r"[‘’“”]"),
        replacement=lambda match: "'",
    ),
    # 고유어 수사 + 셈숫자 단위 → 아라비아 숫자. '두 번' → '2번', '세 회' → '3회'.
    NormalizationRule(
        name="native_number_with_counter",
        pattern=re.compile(
            # 뒤따르는 글자로 막는 것은 ``도``(한 번**도** = 0회 관용구)와 ``째``(세 번**째**
            # = 서수)뿐이다. 예전에는 한글 전체를 막았는데, 그러면 ``한 달간``·``두 주간``
            # 처럼 꼬리말이 붙은 정상적인 기간 표현까지 숫자로 바뀌지 않아 기간이 사라졌다.
            rf"(?<![가-힣])(?P<number>{_NATIVE_NUMBER_ALTERNATION})\s*"
            rf"(?P<counter>{_COUNTER_ALTERNATION})(?!도|째)"
        ),
        replacement=_replace_native_number,
    ),
    # ``일주일`` 은 '1 + 주일'로 분해되지 않는 굳어진 낱말이라 수사 규칙이 잡지 못한다.
    # 뜻이 정확히 1주이므로 여기서 한 번에 정규형으로 바꾼다.
    NormalizationRule(
        name="lexicalized_one_week",
        pattern=re.compile(r"(?<![가-힣])일주일(?![가-힣])"),
        replacement=lambda match: "1주일",
    ),
    # 달력 표현 띄어쓰기 통일. 뜻을 바꾸지 않고 **간격만** 맞춘다(동의어 치환이 아니다).
    NormalizationRule(
        name="calendar_month_spacing",
        pattern=re.compile(r"(?P<determiner>이번|지난|저번)달(?![가-힣])"),
        replacement=lambda match: f"{match.group('determiner')} 달",
    ),
    # 고유어 동사 ``사다`` 의 활용 되돌리기. 이 두 규칙이 사전의 ``샀``/``산`` 항목을 대체한다.
    #
    # ``샀`` 계열: 뒤따르는 어미가 무엇이든(샀다/샀던/샀었다/샀습니다) 어간으로 접는다.
    # 앞에 한글이 오면 다른 낱말의 일부이므로 건드리지 않는다.
    NormalizationRule(
        name="verb_buy_past_stem",
        pattern=re.compile(r"(?<![가-힣])샀[가-힣]*"),
        replacement=lambda match: "사다",
    ),
    # ``산``(관형형): 앞뒤가 모두 한글이 아닐 때만. '부산'·'계산'·'등산'·'산에'를 지킨다.
    NormalizationRule(
        name="verb_buy_adnominal",
        pattern=re.compile(r"(?<![가-힣])산(?![가-힣])"),
        replacement=lambda match: "사다",
    ),
    # 공백 정리는 반드시 마지막이다 — 위 규칙들이 공백을 만들어 내기 때문이다.
    #
    # 패턴이 '여백 2칸 이상' 또는 '공백이 아닌 여백문자 1개'인 이유: 그냥 ``\s+`` 로 쓰면
    # 이미 정규형인 공백 한 칸도 매치되어 같은 값으로 무한히 치환된다(치환 결과가 다시
    # 매치되는 규칙은 수렴하지 않는다).
    NormalizationRule(
        name="collapse_whitespace",
        pattern=re.compile(r"\s{2,}|[^\S ]"),
        replacement=lambda match: " ",
    ),
    # 양끝 여백 제거도 규칙으로 둔다. 결과 문자열만 ``strip()`` 하면 세그먼트 좌표와 어긋나
    # 원문 위치 추적이 깨진다 — 규칙으로 접어야 스팬이 함께 따라온다.
    NormalizationRule(
        name="trim_leading_whitespace",
        pattern=re.compile(r"\A\s+"),
        replacement=lambda match: "",
    ),
    NormalizationRule(
        name="trim_trailing_whitespace",
        pattern=re.compile(r"\s+\Z"),
        replacement=lambda match: "",
    ),
)


class RuleBasedKoreanNormalizer:
    """제한된 규칙 기반 한국어 정규화기.

    '제한된'이 설계 의도다. 여기서 처리하지 못하는 활용형은 **틀리게 바꾸는 대신 그대로
    둔다**. 잘못 바꾼 문장은 조용히 다른 뜻이 되지만, 안 바꾼 문장은 아래 단계에서
    unsupported 로 드러난다.
    """

    def __init__(self, rules: tuple[NormalizationRule, ...] | None = None) -> None:
        self._rules = _DEFAULT_RULES if rules is None else rules

    @property
    def rules(self) -> tuple[NormalizationRule, ...]:
        return self._rules

    def normalize(self, text: str) -> str:
        return self.normalize_with_spans(text).normalized

    def normalize_with_spans(self, text: str) -> NormalizedText:
        """정규화 결과와 원문 위치 대응을 함께 반환한다.

        위치 매핑의 기준 좌표계는 **NFKC 정규화된 원문**이다. NFKC 는 전각/호환 문자를
        표준형으로 접으므로 길이가 달라질 수 있고, 그 경우 매핑 기준을 NFKC 형으로 두는
        편이 일관된다. 실제 한국어·ASCII 입력에서는 NFKC 가 길이를 바꾸지 않는다.
        """

        base = unicodedata.normalize("NFKC", text)
        buffer = _SegmentedText(base)
        for rule in self._rules:
            buffer.apply(rule)

        result = buffer.to_normalized_text(original=base)
        logger.debug(
            "text_normalized",
            extra={"rules": len(self._rules), "edits": len(result.edits)},
        )
        return result


class PassthroughNormalizer:
    """아무것도 바꾸지 않는 정규화기. 정규화의 기여도를 격리해 측정할 때 쓴다."""

    def normalize(self, text: str) -> str:
        return text

    def normalize_with_spans(self, text: str) -> NormalizedText:
        return NormalizedText(original=text, normalized=text, edits=())
