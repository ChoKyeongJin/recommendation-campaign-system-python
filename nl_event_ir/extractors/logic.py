"""논리 결합과 극성 추출.

두 축을 한 파일에 둔 이유는 둘 다 **다른 조건에 대한 조건**이기 때문이다. 접속어는 조건들
사이의 관계를, 부정어는 조건 하나의 참/거짓 방향을 바꾼다. 둘 다 스스로는 조건이 아니다.

부정 판정에서 이 파일이 지키는 규칙: **부정어는 사건에 붙어 있을 때만 그 사건을 부정한다.**
문장 어딘가에 ``없`` 이 있다는 이유로 전체를 부정으로 만들면 ``재고가 없는 상품을 구매한
고객`` 같은 문장이 뒤집힌다. 여기서는 부정 토큰의 **위치**만 정확히 기록하고, 어느 조건에
붙일지는 스팬 거리로 composer 가 정한다.

``미구매``·``미접속`` 처럼 접두사로 부정하는 형태는 사전에 낱말을 늘려 처리하지 않는다.
``미`` 바로 뒤에 **사건 표면어가 오는지**를 구조로 확인한다 — 그래서 새 사건이 추가되면
부정형도 자동으로 따라온다.
"""

from __future__ import annotations

import re

from nl_event_ir.aliases import AliasRegistry, AliasSection, find_alias_matches
from nl_event_ir.enums import LogicOperator, Polarity
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["LogicExtractor", "PolarityExtractor"]

# 부정 접두사. 뒤에 사건 표면어가 올 때만 부정으로 읽는다(구조 조건).
_NEGATION_PREFIX = "미"

# 부정 부사. 뒤에 동사가 오는 자리 표지라 단독으로는 조건이 아니다.
# ``안`` 은 ``안양``·``안내`` 같은 지명·명사의 첫 음절이라 **단독 어절일 때만** 인정한다.
_NEGATION_ADVERBS = ("못", "안")


class LogicExtractor:
    """``또는``/``그리고``/``제외`` 같은 접속·배제 표현을 :class:`LogicOperator` 로 만든다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        for start, end, surface, canonical in find_alias_matches(
            text, self._registry, AliasSection.LOGIC
        ):
            tokens.append(
                SemanticToken(
                    kind=TokenKind.LOGIC,
                    value=LogicOperator(canonical),
                    start=start,
                    end=end,
                    raw_text=surface,
                )
            )
        return tokens


class PolarityExtractor:
    """부정 표현을 :class:`Polarity` 토큰으로 만든다. 결합 대상은 composer 가 정한다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry
        event_alternation = registry.alternation(AliasSection.EVENT)
        # '미' + 사건 표면어. 사건 어휘를 참조하므로 새 사건이 부정형을 자동으로 얻는다.
        self._prefix_re = re.compile(
            rf"(?<![가-힣]){re.escape(_NEGATION_PREFIX)}(?={event_alternation})"
        )
        self._adverb_re = re.compile(
            rf"(?<![가-힣])(?:{'|'.join(re.escape(word) for word in _NEGATION_ADVERBS)})"
            rf"(?![가-힣])"
        )

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []

        for start, end, surface, canonical in find_alias_matches(
            text, self._registry, AliasSection.POLARITY
        ):
            tokens.append(
                SemanticToken(
                    kind=TokenKind.POLARITY,
                    value=Polarity(canonical),
                    start=start,
                    end=end,
                    raw_text=surface,
                )
            )

        for pattern in (self._prefix_re, self._adverb_re):
            for match in pattern.finditer(text):
                tokens.append(
                    SemanticToken(
                        kind=TokenKind.POLARITY,
                        value=Polarity.NEGATIVE,
                        start=match.start(),
                        end=match.end(),
                        raw_text=match.group(0),
                    )
                )
        return tokens
