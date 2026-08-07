"""최상위 파서 — 단계를 **순서대로 흘려보내기만** 한다.

    원문
    → 텍스트 전처리·형태 정규화   (normalizer)
    → 의미 토큰 추출             (extractors)
    → 스팬 중재                  (여기)
    → 잔여 구간 엔티티 후보       (ScopeExtractor.extract_residual)
    → 엔티티 해석                (resolver)
    → 의미 토큰 조합             (composer)
    → IR 검증                    (validator)
    → semantic fallback          (fallback)

이 파일에는 한국어 표면어도, 정규식도 없다. 여기서 규칙을 하나라도 처리하기 시작하면
'문장이 왜 이렇게 읽혔는가'의 답이 두 곳으로 갈라진다.

**스팬 중재**만 이 계층의 고유 책임이다. extractor 들은 서로를 모르기 때문에 같은 글자를
각자 읽을 수 있다(``구매자`` 는 TARGET 이면서 그 안의 ``구매`` 가 EVENT 로도 잡힌다).
중재 규칙은 하나다: **긴 스팬이 이긴다.** 긴 매치가 더 구체적인 해석이기 때문이다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from nl_event_ir.aliases import AliasRegistry, load_alias_registry
from nl_event_ir.composer import EventIRComposer
from nl_event_ir.enums import IssueSeverity
from nl_event_ir.extractors import (
    ComparisonExtractor,
    CountExtractor,
    EventExtractor,
    LogicExtractor,
    PolarityExtractor,
    ScopeExtractor,
    SortExtractor,
    TargetExtractor,
    TemporalExtractor,
)
from nl_event_ir.fallback import FallbackReason, NullSemanticFallback, SemanticFallback
from nl_event_ir.models import ParseResult, ValidationIssue
from nl_event_ir.normalizer import RuleBasedKoreanNormalizer, TextNormalizer
from nl_event_ir.resolver.base import EntityRepository
from nl_event_ir.resolver.entity import EntityResolver
from nl_event_ir.tokenizer import SemanticToken, TokenKind, sort_tokens
from nl_event_ir.validator import EventIRValidator

__all__ = ["EventParser", "build_default_parser"]

logger = logging.getLogger(__name__)

# 스팬이 같을 때의 우선순위. 앞에 있을수록 이긴다. 동점 상황은 드물지만, 규칙이 없으면
# 결과가 실행마다 달라질 수 있어 명시적으로 못박는다(CLAUDE.md 63).
_KIND_PRIORITY: tuple[TokenKind, ...] = (
    TokenKind.DATE_RANGE,
    TokenKind.TIME_WINDOW,
    TokenKind.AGGREGATE,
    TokenKind.COUNT_CONDITION,
    TokenKind.TARGET,
    TokenKind.EVENT,
    TokenKind.ENTITY_TYPE,
    TokenKind.ENTITY_VALUE,
    TokenKind.LOGIC,
    TokenKind.POLARITY,
    TokenKind.COMPARISON,
    TokenKind.SORT,
    TokenKind.UNPARSED,
)


@dataclass(frozen=True, slots=True)
class _Stage:
    """진단용 단계 기록."""

    name: str
    token_count: int


class EventParser:
    """정규화 → 추출 → 해석 → 조합 → 검증을 잇는 파이프라인.

    모든 협력자는 생성자로 주입된다(CLAUDE.md 29). 전역 레지스트리나 전역 현재 시각을
    참조하지 않으므로, 테스트는 고정 기준일과 인메모리 repository 로 완전히 결정론적이다.
    """

    def __init__(
        self,
        *,
        normalizer: TextNormalizer,
        extractors: Sequence[Any],
        entity_resolver: EntityResolver | None = None,
        composer: EventIRComposer | None = None,
        validator: EventIRValidator | None = None,
        fallback: SemanticFallback | None = None,
    ) -> None:
        self._normalizer = normalizer
        self._extractors = tuple(extractors)
        # 계약을 만족하지 않는 extractor 를 조용히 건너뛰면, 오타 하나로 축 하나가 통째로
        # 빠진 채 파서가 '정상' 동작한다. 조립 시점에 막는다.
        for extractor in self._extractors:
            if not hasattr(extractor, "extract") and not hasattr(
                extractor, "extract_residual"
            ):
                raise TypeError(
                    f"{type(extractor).__name__} implements neither extract() nor "
                    "extract_residual(); it would contribute nothing to the pipeline"
                )
        self._entity_resolver = entity_resolver
        self._composer = composer if composer is not None else EventIRComposer()
        self._validator = validator if validator is not None else EventIRValidator()
        self._fallback = fallback if fallback is not None else NullSemanticFallback()

    # ── 진입점 ──────────────────────────────────────────────────────────────────

    def parse(self, text: str) -> ParseResult:
        normalized = self._normalizer.normalize_with_spans(text)
        working = normalized.normalized
        stages: list[_Stage] = []

        tokens = self._run_extractors(working)
        stages.append(_Stage("extract", len(tokens)))

        tokens = _arbitrate_spans(tokens)
        stages.append(_Stage("arbitrate", len(tokens)))

        tokens = self._run_residual(working, tokens)
        stages.append(_Stage("residual", len(tokens)))

        if self._entity_resolver is not None:
            tokens = self._entity_resolver.resolve(working, tokens)
        ordered_tokens = sort_tokens(tokens)
        stages.append(_Stage("resolve", len(ordered_tokens)))

        outcome = self._composer.compose(working, ordered_tokens)
        issues = list(outcome.issues)

        if outcome.ir is not None:
            issues.extend(self._validator.validate(outcome.ir))

        has_error = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        ir = None if (outcome.ir is None or has_error) else outcome.ir
        reason = _fallback_reason(outcome.ir, issues)

        diagnostics: dict[str, Any] = {
            "stages": [{"name": stage.name, "tokens": stage.token_count} for stage in stages],
            "unresolved_values": list(outcome.unresolved_values),
        }

        if ir is None:
            recovered = self._fallback.parse(text, ordered_tokens)
            if recovered is not None:
                # fallback 산출물도 **반드시** 같은 검증을 통과해야 한다. 검증을 건너뛰면
                # 규칙 경로보다 느슨한 뒷문이 생겨, 규칙이 막아 둔 잘못된 IR 이 fallback 을
                # 통해 그대로 실행된다.
                fallback_issues = self._validator.validate(recovered)
                diagnostics["ir_source"] = "fallback"
                if any(
                    issue.severity is IssueSeverity.ERROR for issue in fallback_issues
                ):
                    logger.debug("fallback_ir_rejected_by_validator")
                    return ParseResult(
                        original_text=text,
                        normalized_text=working,
                        tokens=ordered_tokens,
                        ir=None,
                        issues=tuple(issues) + tuple(fallback_issues),
                        fallback_required=True,
                        fallback_reason=FallbackReason.INVALID_IR.value,
                        diagnostics=diagnostics,
                        normalization=normalized,
                    )
                logger.debug("ir_recovered_by_fallback")
                return ParseResult(
                    original_text=text,
                    normalized_text=working,
                    tokens=ordered_tokens,
                    ir=recovered,
                    issues=tuple(issues) + tuple(fallback_issues),
                    fallback_required=False,
                    fallback_reason=None,
                    diagnostics=diagnostics,
                    normalization=normalized,
                )

        return ParseResult(
            original_text=text,
            normalized_text=working,
            tokens=ordered_tokens,
            ir=ir,
            issues=tuple(issues),
            fallback_required=ir is None,
            fallback_reason=reason.value if (ir is None and reason is not None) else None,
            diagnostics=diagnostics,
            normalization=normalized,
        )

    # ── 단계 ────────────────────────────────────────────────────────────────────

    def _run_extractors(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        for extractor in self._extractors:
            extract = getattr(extractor, "extract", None)
            if extract is None:
                continue
            tokens.extend(extract(text))
        return tokens

    def _run_residual(self, text: str, tokens: list[SemanticToken]) -> list[SemanticToken]:
        """잔여 구간 전용 extractor 를 돌린다. 여기서 엔티티 값 후보가 나온다."""

        collected = list(tokens)
        for extractor in self._extractors:
            extract_residual = getattr(extractor, "extract_residual", None)
            if extract_residual is None:
                continue
            collected.extend(extract_residual(text, collected))
        return collected


# ── 스팬 중재 ────────────────────────────────────────────────────────────────────


def _arbitrate_spans(tokens: Sequence[SemanticToken]) -> list[SemanticToken]:
    """겹치는 토큰 중 **긴 스팬**만 남긴다.

    긴 매치가 이기는 이유: 긴 매치는 더 많은 문맥을 설명한다. ``구매자`` 를 TARGET 으로
    읽으면 그 안의 ``구매`` 를 EVENT 로 읽을 필요가 없지만, 반대로 하면 ``자`` 가 설명되지
    않은 채 남는다.
    """

    ordered = sorted(
        tokens,
        key=lambda token: (
            -(token.end - token.start),
            _KIND_PRIORITY.index(token.kind) if token.kind in _KIND_PRIORITY else len(_KIND_PRIORITY),
            token.start,
        ),
    )
    kept: list[SemanticToken] = []
    for token in ordered:
        if any(
            token.start < other.end and other.start < token.end for other in kept
        ):
            continue
        kept.append(token)
    return list(sort_tokens(kept))


def _fallback_reason(
    ir: object | None,
    issues: Sequence[ValidationIssue],
) -> FallbackReason | None:
    codes = {issue.code for issue in issues if issue.severity is IssueSeverity.ERROR}
    if "event.missing" in codes:
        return FallbackReason.EVENT_NOT_FOUND
    if "entity.ambiguous" in codes:
        return FallbackReason.AMBIGUOUS_ENTITY
    if "metric.conflict" in codes:
        return FallbackReason.CONFLICTING_TOKENS
    if "expression.unparsed" in codes:
        return FallbackReason.UNSUPPORTED_COMPOSITION
    if codes:
        return FallbackReason.INVALID_IR
    if ir is None:
        return FallbackReason.UNSUPPORTED_COMPOSITION
    return None


# ── 기본 조립 ────────────────────────────────────────────────────────────────────


def build_default_parser(
    repository: EntityRepository,
    *,
    registry: AliasRegistry | None = None,
    reference_date: date | None = None,
    fallback: SemanticFallback | None = None,
) -> EventParser:
    """표준 구성의 파서를 만든다.

    import 부작용으로 레지스트리를 만들지 않는다(CLAUDE.md 58) — 명시적으로 이 함수를
    호출해야 사전이 로드된다. 그래야 테스트가 사전 로딩 실패를 볼 수 있다.
    """

    alias_registry = registry if registry is not None else load_alias_registry()
    return EventParser(
        normalizer=RuleBasedKoreanNormalizer(),
        extractors=[
            TemporalExtractor(alias_registry, reference_date=reference_date),
            SortExtractor(alias_registry),
            CountExtractor(alias_registry),
            ComparisonExtractor(alias_registry),
            EventExtractor(alias_registry),
            TargetExtractor(alias_registry),
            ScopeExtractor(alias_registry),
            LogicExtractor(alias_registry),
            PolarityExtractor(alias_registry),
        ],
        entity_resolver=EntityResolver(repository),
        composer=EventIRComposer(),
        validator=EventIRValidator(),
        fallback=fallback,
    )
