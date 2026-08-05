"""순위·정렬 표현 추출 — ``상위 100명`` 의 '어느 쪽 끝에서 몇 명'을 읽는다.

이 축이 없으면 순위 표현이 잔여물로 흘러가 조용히 사라진다. 그 결과는 단순한 미지원이
아니라 **모수의 확대**다 — ``구매 금액 상위 100명`` 이 ``구매한 전원`` 이 되어 대상이
수백 배로 늘어난다. 조용히 넓어지는 실패는 결과가 정상처럼 보이므로 가장 위험하다.

정렬 방향을 표면어에서 추측하지 않는다(CLAUDE.md 64). ``상위`` 가 항상 내림차순인 것은
아니다 — 응답 시간·이탈률처럼 낮은 값이 좋은 지표에서는 반대다. 여기서는 표면어가 선언한
방향(alias 파일의 ``sort`` 절)을 그대로 옮기고, 그 방향이 지표에 맞는지는 상위 계층이 정한다.

숫자가 없는 ``상위 고객`` 은 개수를 만들 수 없다. 이때 임의의 기본값(10? 100?)을 넣지 않고
:data:`~nl_event_ir.tokenizer.TokenKind.UNPARSED` 로 남겨 상위 단계가 명시적으로 실패하게 한다.
"""

from __future__ import annotations

import re

from nl_event_ir.aliases import AliasRegistry, AliasSection
from nl_event_ir.enums import SortDirection
from nl_event_ir.models import RankSpec
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["SortExtractor"]

# 사람·건을 세는 단위. 순위 표현의 꼬리에 붙는 문법 표지다.
_RANK_UNITS = ("명", "개", "건", "곳", "위")


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(word) for word in sorted(words, key=lambda w: (-len(w), w)))


class SortExtractor:
    """``상위/하위 N명`` 을 :class:`RankSpec` 토큰으로 만든다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry
        sort_alternation = registry.alternation(AliasSection.SORT)
        unit_alternation = _alternation(_RANK_UNITS)

        self._ranked_re = re.compile(
            rf"(?<![가-힣])(?P<direction>{sort_alternation})\s*"
            rf"(?P<limit>\d+)\s*(?:{unit_alternation})?"
        )
        # 방향어만 있고 개수가 없는 형태. 개수를 지어내지 않기 위해 따로 잡는다.
        self._bare_direction_re = re.compile(
            rf"(?<![가-힣])(?P<direction>{sort_alternation})(?![가-힣\d\s]*\d)"
        )

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        claimed: list[tuple[int, int]] = []

        for match in self._ranked_re.finditer(text):
            canonical = self._registry.lookup(AliasSection.SORT, match.group("direction"))
            if canonical is None:  # pragma: no cover - 교대가 사전에서 나왔으므로 도달 불가
                continue
            limit = int(match.group("limit"))
            tokens.append(
                SemanticToken(
                    kind=TokenKind.SORT,
                    value=RankSpec(limit=limit, direction=SortDirection(canonical)),
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                )
            )
            claimed.append(match.span())

        for match in self._bare_direction_re.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in claimed):
                continue
            tokens.append(
                SemanticToken(
                    kind=TokenKind.UNPARSED,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                )
            )
        return tokens
