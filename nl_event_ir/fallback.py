"""Semantic fallback — 규칙이 감당하지 못한 문장만 넘어오는 자리.

이 계층의 존재 이유는 '규칙을 무한히 늘리지 않기 위해서'다. 규칙 기반 파서는 커버리지를
100% 로 올리려는 순간 문장 유형별 정규식 더미로 되돌아간다. 그러니 규칙은 **확실한 것만**
처리하고, 나머지는 여기로 내려보낸다.

내려보내는 조건이 흐릿하면 이 분리가 무의미해지므로 :class:`FallbackReason` 으로 못박는다.
'왜 fallback 이 필요했는가'는 규칙을 어디에 더 넣을지 정하는 데 그대로 쓰이는 정보다.

기본 구현은 아무것도 하지 않는다. LLM 호출을 여기 기본값으로 두면 테스트가 네트워크에
의존하게 되고, 규칙의 실제 커버리지도 가려진다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from nl_event_ir.models import EventIR
from nl_event_ir.tokenizer import SemanticToken

__all__ = ["FallbackReason", "NullSemanticFallback", "SemanticFallback"]

logger = logging.getLogger(__name__)


class FallbackReason(StrEnum):
    """규칙 파서가 손을 든 이유. 진단과 개선 우선순위 판단에 쓴다."""

    EVENT_NOT_FOUND = "event_not_found"
    AMBIGUOUS_ENTITY = "ambiguous_entity"
    CONFLICTING_TOKENS = "conflicting_tokens"
    INVALID_IR = "invalid_ir"
    UNSUPPORTED_COMPOSITION = "unsupported_composition"


class SemanticFallback(Protocol):
    """규칙으로 만들지 못한 IR 을 다른 수단으로 만들어 본다.

    구현체는 **같은 :class:`EventIR` 스키마**를 돌려주어야 한다. 자유 형식 결과를 허용하면
    검증·직렬화 계약이 이 지점에서 무너진다.
    """

    def parse(
        self,
        text: str,
        partial_tokens: Sequence[SemanticToken],
    ) -> EventIR | None: ...


class NullSemanticFallback:
    """아무것도 하지 않는 기본 구현.

    규칙이 실패했다는 사실을 **그대로 드러내는** 것이 기본 동작이어야 한다. 여기서 무엇이든
    지어내면 규칙의 빈틈이 영영 보이지 않는다.
    """

    def parse(
        self,
        text: str,
        partial_tokens: Sequence[SemanticToken],
    ) -> EventIR | None:
        logger.debug(
            "fallback_skipped",
            extra={"token_count": len(partial_tokens), "text_length": len(text)},
        )
        return None
