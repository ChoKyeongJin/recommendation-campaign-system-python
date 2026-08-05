"""엔티티 해석기 — 값 후보를 카탈로그에 물어 **접지**한다.

여기서 하는 결정은 하나뿐이다: *이 후보를 확정할 근거가 충분한가.* 충분하지 않으면
확정하지 않는다. '가장 그럴듯한 것'을 조용히 고르는 것이 가장 위험한 실패 방식이다 —
결과는 그럴듯해 보이지만 근거가 없고, 아무 신호도 남지 않는다.

근접한 엔티티 **종류** 표지(``브랜드``/``상품``)는 **선호**로만 쓰고 필터로 쓰지 않는다.
``애플 브랜드`` 에서는 종류 표지가 모호성을 없애 주지만, ``나이키 상품`` 의 ``상품`` 은
'나이키가 상품이다'가 아니라 '나이키 브랜드의 상품'을 뜻하기 때문이다. 하드 필터로 쓰면
후자가 조용히 사라진다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from nl_event_ir.enums import EntityType
from nl_event_ir.resolver.base import EntityCandidate, EntityRepository, EntityResolution
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["EntityResolver"]

logger = logging.getLogger(__name__)

# 이 점수 미만은 후보로 치지 않는다.
_DEFAULT_MINIMUM_SCORE = 0.6
# 1등과 2등의 점수 차가 이보다 작으면 확정하지 않는다(모호).
_DEFAULT_AMBIGUITY_MARGIN = 0.15
# 종류 표지를 '근처'로 볼 글자 거리.
_DEFAULT_TYPE_HINT_DISTANCE = 6


class EntityResolver:
    """ENTITY_VALUE 후보 토큰을 :class:`EntityResolution` 을 담은 토큰으로 바꾼다."""

    def __init__(
        self,
        repository: EntityRepository,
        *,
        minimum_score: float = _DEFAULT_MINIMUM_SCORE,
        ambiguity_margin: float = _DEFAULT_AMBIGUITY_MARGIN,
        type_hint_distance: int = _DEFAULT_TYPE_HINT_DISTANCE,
    ) -> None:
        self._repository = repository
        self._minimum_score = minimum_score
        self._ambiguity_margin = ambiguity_margin
        self._type_hint_distance = type_hint_distance

    def resolve(
        self,
        text: str,
        tokens: Sequence[SemanticToken],
    ) -> list[SemanticToken]:
        """ENTITY_VALUE 토큰만 해석해 돌려주고, 나머지 토큰은 그대로 통과시킨다."""

        type_hints = _collect_type_hints(tokens)
        resolved: list[SemanticToken] = []
        for token in tokens:
            if token.kind is not TokenKind.ENTITY_VALUE:
                resolved.append(token)
                continue
            resolved.append(self._resolve_token(token, type_hints))
        return resolved

    def _resolve_token(
        self,
        token: SemanticToken,
        type_hints: tuple[tuple[int, int, EntityType], ...],
    ) -> SemanticToken:
        raw_value = token.value if isinstance(token.value, str) else token.raw_text
        candidates = tuple(
            candidate
            for candidate in self._repository.search(raw_value)
            if candidate.score >= self._minimum_score
        )
        hint = _nearest_type_hint(token, type_hints, self._type_hint_distance)
        selected = self._select(candidates, hint)

        resolution = EntityResolution(
            raw_value=raw_value,
            candidates=candidates,
            selected=selected,
        )
        logger.debug(
            "entity_resolved",
            extra={
                "candidate_count": len(candidates),
                "selected": selected.entity_id if selected is not None else None,
                "type_hint": hint.value if hint is not None else None,
            },
        )
        return SemanticToken(
            kind=TokenKind.ENTITY_VALUE,
            value=resolution,
            start=token.start,
            end=token.end,
            raw_text=token.raw_text,
            confidence=selected.score if selected is not None else token.confidence,
            source=token.source,
        )

    def _select(
        self,
        candidates: tuple[EntityCandidate, ...],
        hint: EntityType | None,
    ) -> EntityCandidate | None:
        if not candidates:
            return None

        # 종류 표지가 실제로 후보를 좁혀 줄 때만 적용한다(빈 결과가 되면 무시).
        pool = candidates
        if hint is not None:
            narrowed = tuple(item for item in candidates if item.entity_type is hint)
            if narrowed:
                pool = narrowed

        if len(pool) == 1:
            return pool[0]

        best, runner_up = pool[0], pool[1]
        if best.score - runner_up.score < self._ambiguity_margin:
            # 1등과 2등을 가를 근거가 없다. 지어내지 않고 모호 상태로 남긴다.
            return None
        return best


def _collect_type_hints(
    tokens: Sequence[SemanticToken],
) -> tuple[tuple[int, int, EntityType], ...]:
    return tuple(
        (token.start, token.end, token.value)
        for token in tokens
        if token.kind is TokenKind.ENTITY_TYPE and isinstance(token.value, EntityType)
    )


def _nearest_type_hint(
    token: SemanticToken,
    type_hints: tuple[tuple[int, int, EntityType], ...],
    maximum_distance: int,
) -> EntityType | None:
    """값 토큰에서 가장 가까운 종류 표지. 거리가 멀면 관계없는 표지로 본다."""

    best: tuple[int, EntityType] | None = None
    for hint_start, hint_end, entity_type in type_hints:
        if hint_start >= token.end:
            distance = hint_start - token.end
        elif hint_end <= token.start:
            distance = token.start - hint_end
        else:  # pragma: no cover - 스팬 중재가 겹침을 미리 제거한다
            distance = 0
        if distance > maximum_distance:
            continue
        if best is None or distance < best[0]:
            best = (distance, entity_type)
    return best[1] if best is not None else None
