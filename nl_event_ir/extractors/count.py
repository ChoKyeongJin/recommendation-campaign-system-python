"""횟수 조건 추출 — ``얼마나 자주``만 읽는다.

셈숫자 단위(``회``/``번``/``건``/``개``)는 alias 파일에 넣지 않았다. 이 낱말들은 canonical
값으로 정규화되는 **동의어**가 아니라, 숫자에 붙어 '이것이 횟수다'를 표시하는 **문법 표지**다.
alias 파일의 계약은 '표면어 → canonical enum 값'인데 셈숫자 단위에 대응되는 enum 이 없다 —
계약에 맞지 않는 것을 그 파일에 넣기 시작하면 그 파일이 다시 잡동사니가 된다.

``한 번도`` 는 정규화기가 숫자로 바꾸지 않는다(셈숫자 단위 뒤에 한글 조사 ``도`` 가 붙으면
숫자 변환 규칙의 경계 조건에 걸린다). 그래서 여기서 관용구 하나로 읽어 '0회'로 만든다.
"""

from __future__ import annotations

import re

from nl_event_ir.aliases import AliasRegistry, AliasSection
from nl_event_ir.enums import ComparisonOperator, MetricType
from nl_event_ir.models import MetricCondition
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["CountExtractor"]

# 셈숫자 단위(counter). 문법 표지이므로 코드가 소유한다 — 위 docstring 참조.
_COUNT_UNITS = ("회", "번", "건", "개")

# '한 번도 ~ 않다' 계열. 0 회를 뜻하는 관용구이며 뒤따르는 부정어와 짝을 이룬다.
_ZERO_IDIOMS = ("한 번도", "한번도", "단 한 번도", "단 한번도", "전혀")


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(word) for word in sorted(words, key=lambda w: (-len(w), w)))


class CountExtractor:
    """``2회 이상``·``3건 이하``·``정확히 1번``·``한 번도`` 를 :class:`MetricCondition` 으로 만든다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry
        comparison_alternation = registry.alternation(AliasSection.COMPARISON)
        unit_alternation = _alternation(_COUNT_UNITS)

        # 선행 비교어('정확히 1번')와 후행 비교어('2회 이상')를 한 패턴으로 읽는다.
        # 둘 다 없으면 매치되지 않는다 — 비교 없는 맨 숫자는 횟수 조건이 아니다
        # ('상품 3개를 담은' 의 3개는 조건이 아니라 수량이다).
        self._count_re = re.compile(
            rf"(?:(?P<leading>{comparison_alternation})\s*)?"
            rf"(?P<value>\d+)\s*(?P<unit>{unit_alternation})"
            rf"(?:\s*(?P<trailing>{comparison_alternation}))?"
        )
        self._zero_re = re.compile(
            rf"(?<![가-힣])(?P<idiom>{_alternation(_ZERO_IDIOMS)})(?![가-힣])"
        )

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        claimed: list[tuple[int, int]] = []

        for match in self._zero_re.finditer(text):
            tokens.append(
                SemanticToken(
                    kind=TokenKind.COUNT_CONDITION,
                    value=MetricCondition(
                        metric_type=MetricType.EVENT_COUNT,
                        operator=ComparisonOperator.EQ,
                        value=0,
                    ),
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                )
            )
            claimed.append(match.span())

        for match in self._count_re.finditer(text):
            if any(
                match.start() < end and start < match.end() for start, end in claimed
            ):
                continue
            token = self._build_count_token(match)
            if token is not None:
                tokens.append(token)
        return tokens

    def _build_count_token(self, match: re.Match[str]) -> SemanticToken | None:
        leading = match.group("leading")
        trailing = match.group("trailing")
        if leading is None and trailing is None:
            # 비교어가 없는 '3회' 는 조건이 아니라 값이다. 기본 연산자를 지어내지 않는다.
            return None

        # 후행 비교어가 한국어의 기본 어순('2회 이상')이라 우선한다. 둘 다 있으면
        # ('정확히 2회 이상') 모순이므로 검증기가 볼 수 있게 토큰은 만들되 선행을 버리지 않고
        # 후행을 택한 사실이 raw_text 에 남는다.
        surface = trailing if trailing is not None else leading
        canonical = self._registry.lookup(AliasSection.COMPARISON, surface)
        if canonical is None:  # pragma: no cover - 교대가 사전에서 나왔으므로 도달 불가
            return None

        return SemanticToken(
            kind=TokenKind.COUNT_CONDITION,
            value=MetricCondition(
                metric_type=MetricType.EVENT_COUNT,
                operator=ComparisonOperator(canonical),
                value=int(match.group("value")),
            ),
            start=match.start(),
            end=match.end(),
            raw_text=match.group(0),
        )
