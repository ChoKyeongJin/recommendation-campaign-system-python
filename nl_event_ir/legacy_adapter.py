"""기존 어휘 사전 어댑터 — 점진적 이행을 위한 **한시적** 보완 계층.

기존 사전을 당장 지우지 않는 이유는 위험 관리다. 새 파서가 처리하지 못하는 표현이 남아
있는 동안 사전을 없애면 그만큼이 그대로 회귀가 된다. 그렇다고 새 파서가 사전을 그대로
쓰면 이행이 영영 끝나지 않는다. 그래서 이 어댑터는 두 가지 제약 아래 동작한다.

1. **새 파서가 읽지 못한 구간에서만** 동작한다. 새 규칙과 경쟁하지 않는다.
2. 여기서 나온 토큰은 **신뢰도 0.6** 이다. 규칙 토큰(1.0)과 섞여도 출처가 구분된다.

이 두 제약 덕분에 '레거시 토큰이 얼마나 남아 있는가'가 곧 이행 진척도가 된다. 그 수치가
0 이 되면 이 파일과 원본 사전을 함께 지울 수 있다.

원본 사전은 복사하지 않고 :data:`LEGACY_POINTER_PATH` 의 선언을 따라 **live 파일**을 읽는다.
사본을 두면 원본이 바뀔 때 조용히 낡기 때문이다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from nl_event_ir.enums import EntityType, EventType, LogicOperator, Polarity
from nl_event_ir.tokenizer import SemanticToken, TokenKind, uncovered_spans

__all__ = ["LegacyPatternAdapter", "LEGACY_POINTER_PATH", "LEGACY_TOKEN_CONFIDENCE"]

logger = logging.getLogger(__name__)

_REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
LEGACY_POINTER_PATH: Final[Path] = (
    _REPOSITORY_ROOT / "resources" / "legacy_lexicon_patterns.json"
)

# 레거시 토큰의 신뢰도. 규칙 토큰과 구분되는 낮은 값이어야 이행 진척을 셀 수 있다.
LEGACY_TOKEN_CONFIDENCE: Final[float] = 0.6

# 기존 어휘 이름 → canonical 의미. 이 표가 곧 '무엇을 이미 새 파서로 옮겼는가'의 목록이다.
#
# ``purchase_verb`` 계열이 다섯 갈래인 것이 이 리팩터링의 출발점이었다. 새 파서에서는
# 정규화기 규칙 두 개가 그 다섯을 대체한다 — 여기 남은 것은 이행 기간의 안전망이다.
_EVENT_VOCABULARIES: Final[dict[str, EventType]] = {
    "event_alias_purchase": EventType.PURCHASE,
    "purchase_verb": EventType.PURCHASE,
    "purchase_membership_verb": EventType.PURCHASE,
    "purchase_verb_completed_stem": EventType.PURCHASE,
    "purchase_verb_colloquial": EventType.PURCHASE,
    "purchase_verb_colloquial_object_bound": EventType.PURCHASE,
    "event_alias_login": EventType.LOGIN,
    "event_alias_cart": EventType.CART,
    "event_alias_signup": EventType.SIGNUP,
}

_TARGET_VOCABULARIES: Final[tuple[str, ...]] = (
    "member_noun",
    "member_noun_informal",
    "member_noun_honorific",
    "member_noun_role",
    "member_noun_registrant",
)

_LOGIC_VOCABULARIES: Final[dict[str, LogicOperator]] = {
    "and_connective": LogicOperator.AND,
    "or_connective": LogicOperator.OR,
}

_POLARITY_VOCABULARIES: Final[tuple[str, ...]] = (
    "event_negation_marker",
    "generic_negation",
)

_PRODUCT_VOCABULARIES: Final[tuple[str, ...]] = ("product_noun",)


class LegacyLexiconError(Exception):
    """레거시 사전을 읽을 수 없다."""


class LegacyPatternAdapter:
    """기존 어휘 사전에서 토큰을 뽑는다. **잔여 구간 전용**이다."""

    def __init__(self, lexicon_path: Path | str | None = None) -> None:
        self._vocabularies = _load_legacy_vocabularies(lexicon_path)
        self._patterns = self._compile_patterns()

    def _compile_patterns(self) -> tuple[tuple[re.Pattern[str], TokenKind, object], ...]:
        compiled: list[tuple[re.Pattern[str], TokenKind, object]] = []
        declarations: list[tuple[tuple[str, ...], TokenKind, object]] = [
            *[((name,), TokenKind.EVENT, value) for name, value in _EVENT_VOCABULARIES.items()],
            (_TARGET_VOCABULARIES, TokenKind.TARGET, EntityType.MEMBER),
            *[((name,), TokenKind.LOGIC, value) for name, value in _LOGIC_VOCABULARIES.items()],
            (_POLARITY_VOCABULARIES, TokenKind.POLARITY, Polarity.NEGATIVE),
            (_PRODUCT_VOCABULARIES, TokenKind.ENTITY_TYPE, EntityType.PRODUCT),
        ]
        for names, kind, value in declarations:
            surfaces = sorted(
                {
                    surface
                    for name in names
                    for surface in self._vocabularies.get(name, ())
                    if surface
                },
                key=lambda word: (-len(word), word),
            )
            if not surfaces:
                continue
            pattern = re.compile("|".join(re.escape(surface) for surface in surfaces))
            compiled.append((pattern, kind, value))
        return tuple(compiled)

    def extract(self, text: str) -> list[SemanticToken]:
        """문장 전체에서 레거시 토큰을 뽑는다(잔여 구간 제한 없이)."""

        return self._extract_within(text, [(0, len(text))])

    def extract_residual(
        self,
        text: str,
        tokens: Sequence[SemanticToken],
    ) -> list[SemanticToken]:
        """새 파서가 읽지 못한 구간에서만 뽑는다. 운영 경로는 이쪽을 쓴다."""

        spans = uncovered_spans(text, tokens, minimum_length=2)
        return self._extract_within(text, list(spans))

    def _extract_within(
        self,
        text: str,
        spans: list[tuple[int, int]],
    ) -> list[SemanticToken]:
        found: list[SemanticToken] = []
        claimed: list[tuple[int, int]] = []
        for span_start, span_end in spans:
            fragment = text[span_start:span_end]
            for pattern, kind, value in self._patterns:
                for match in pattern.finditer(fragment):
                    start = span_start + match.start()
                    end = span_start + match.end()
                    if any(start < other_end and other < end for other, other_end in claimed):
                        continue
                    claimed.append((start, end))
                    found.append(
                        SemanticToken(
                            kind=kind,
                            value=value,
                            start=start,
                            end=end,
                            raw_text=match.group(0),
                            confidence=LEGACY_TOKEN_CONFIDENCE,
                            source="legacy",
                        )
                    )
        if found:
            logger.debug("legacy_tokens_used", extra={"count": len(found)})
        return found


def _load_legacy_vocabularies(
    lexicon_path: Path | str | None,
) -> dict[str, tuple[str, ...]]:
    """포인터 파일이 가리키는 **원본** 사전에서 어휘를 읽는다."""

    resolved = _resolve_lexicon_path(lexicon_path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegacyLexiconError(f"cannot read legacy lexicon: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise LegacyLexiconError(
            f"legacy lexicon is not valid JSON: {resolved} ({exc})"
        ) from exc

    vocabularies = raw.get("vocabularies")
    if not isinstance(vocabularies, dict):
        raise LegacyLexiconError(
            f"legacy lexicon has no 'vocabularies' object: {resolved}"
        )
    return {
        name: tuple(str(word) for word in words)
        for name, words in vocabularies.items()
        if isinstance(words, list)
    }


def _resolve_lexicon_path(lexicon_path: Path | str | None) -> Path:
    """명시 경로 → 기존 로더와 동일한 경로 → 환경 변수 → 포인터 파일 순으로 정한다.

    두 번째 단계가 중요하다. 기존 :mod:`lexicon_patterns` 이 경로 규칙을 바꾸면(예: 기본
    위치 이동) 이 어댑터가 **다른 파일**을 읽게 되고, 그때부터 두 시스템의 어휘가 조용히
    갈라진다. 이행 기간 내내 같은 파일을 봐야 하므로 기존 소유자의 값을 그대로 따른다.
    """

    if lexicon_path is not None:
        return Path(lexicon_path)

    try:
        import lexicon_patterns

        return Path(lexicon_patterns.DEFAULT_LEXICON_PATH)
    except ImportError:
        # 저장소 밖에서 이 패키지만 쓰는 경우. 포인터 선언으로 넘어간다.
        logger.debug("lexicon_patterns module unavailable; falling back to pointer file")

    pointer = _read_pointer()
    env_name = pointer.get("source_env_var")
    if isinstance(env_name, str):
        from_env = os.getenv(env_name)
        if from_env:
            return Path(from_env)

    source = pointer.get("source")
    if not isinstance(source, str):
        raise LegacyLexiconError(
            f"pointer file declares no 'source' path: {LEGACY_POINTER_PATH}"
        )
    candidate = Path(source)
    return candidate if candidate.is_absolute() else _REPOSITORY_ROOT / candidate


def _read_pointer() -> dict[str, Any]:
    try:
        payload = json.loads(LEGACY_POINTER_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegacyLexiconError(
            f"cannot read legacy lexicon pointer: {LEGACY_POINTER_PATH}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise LegacyLexiconError(
            f"legacy lexicon pointer is not valid JSON: {LEGACY_POINTER_PATH} ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise LegacyLexiconError(f"legacy lexicon pointer must be an object: {LEGACY_POINTER_PATH}")
    return payload
