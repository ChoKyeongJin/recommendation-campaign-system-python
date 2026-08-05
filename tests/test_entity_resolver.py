"""엔티티 해석 테스트.

여기서 지켜야 하는 성질은 하나다: **근거가 없으면 고르지 않는다.** 그럴듯한 후보를 조용히
확정하는 것이 이 계층에서 가장 위험한 실패다 — 결과가 정상처럼 보이고 아무 신호도 남지
않기 때문이다.
"""

from __future__ import annotations

import pytest

from nl_event_ir.enums import EntityType
from nl_event_ir.resolver import (
    EntityCandidate,
    EntityResolution,
    EntityResolver,
    InMemoryEntityRepository,
)
from nl_event_ir.tokenizer import SemanticToken, TokenKind

NIKE = EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0)
ADIDAS = EntityCandidate(EntityType.BRAND, "brand:adidas", "아디다스", 1.0)
APPLE_BRAND = EntityCandidate(EntityType.BRAND, "brand:apple", "애플", 1.0)
APPLE_PRODUCT = EntityCandidate(EntityType.PRODUCT, "product:apple", "애플", 1.0)


def _candidate_token(value: str, start: int = 0) -> SemanticToken:
    return SemanticToken(
        kind=TokenKind.ENTITY_VALUE,
        value=value,
        start=start,
        end=start + len(value),
        raw_text=value,
        confidence=0.5,
        source="residual",
    )


def _type_token(entity_type: EntityType, surface: str, start: int) -> SemanticToken:
    return SemanticToken(
        kind=TokenKind.ENTITY_TYPE,
        value=entity_type,
        start=start,
        end=start + len(surface),
        raw_text=surface,
    )


def _resolution(tokens: list[SemanticToken]) -> EntityResolution:
    resolved = [token for token in tokens if token.kind is TokenKind.ENTITY_VALUE]
    assert len(resolved) == 1
    value = resolved[0].value
    assert isinstance(value, EntityResolution)
    return value


# ── repository ───────────────────────────────────────────────────────────────────


def test_repository_exact_match_scores_one() -> None:
    repository = InMemoryEntityRepository([NIKE])
    assert repository.search("나이키")[0].score == 1.0


def test_repository_partial_match_is_penalised() -> None:
    repository = InMemoryEntityRepository([NIKE])
    assert repository.search("나이")[0].score < 1.0


def test_repository_can_filter_by_type() -> None:
    repository = InMemoryEntityRepository([APPLE_BRAND, APPLE_PRODUCT])
    found = repository.search("애플", entity_types=(EntityType.PRODUCT,))
    assert [candidate.entity_id for candidate in found] == ["product:apple"]


def test_repository_ordering_is_deterministic() -> None:
    """동점 후보의 순서가 흔들리면 선택 결과가 실행마다 달라진다."""

    repository = InMemoryEntityRepository([APPLE_PRODUCT, APPLE_BRAND])
    first = [candidate.entity_id for candidate in repository.search("애플")]
    second = [candidate.entity_id for candidate in repository.search("애플")]
    assert first == second == ["brand:apple", "product:apple"]


def test_repository_returns_nothing_for_unknown_value() -> None:
    assert InMemoryEntityRepository([NIKE]).search("존재하지않는브랜드") == []


# ── resolver ─────────────────────────────────────────────────────────────────────


def test_resolves_single_candidate() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([NIKE]))
    resolution = _resolution(resolver.resolve("나이키", [_candidate_token("나이키")]))

    assert resolution.selected == NIKE
    assert resolution.selected.entity_id == "brand:nike"
    assert not resolution.is_ambiguous


def test_ambiguous_candidates_are_not_silently_chosen() -> None:
    """``애플`` 이 브랜드이자 상품명이면 문장만으로는 정할 수 없다."""

    resolver = EntityResolver(InMemoryEntityRepository([APPLE_BRAND, APPLE_PRODUCT]))
    resolution = _resolution(resolver.resolve("애플", [_candidate_token("애플")]))

    assert resolution.selected is None
    assert resolution.is_ambiguous
    assert len(resolution.candidates) == 2


def test_unknown_value_is_distinguished_from_ambiguous() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([NIKE]))
    resolution = _resolution(resolver.resolve("동안", [_candidate_token("동안")]))

    assert resolution.is_unknown
    assert not resolution.is_ambiguous


def test_type_hint_disambiguates() -> None:
    """``애플 브랜드`` 에서는 종류 표지가 모호성을 없앤다."""

    resolver = EntityResolver(InMemoryEntityRepository([APPLE_BRAND, APPLE_PRODUCT]))
    tokens = [
        _candidate_token("애플", start=0),
        _type_token(EntityType.BRAND, "브랜드", start=3),
    ]
    resolution = _resolution(resolver.resolve("애플 브랜드", tokens))

    assert resolution.selected == APPLE_BRAND


def test_type_hint_is_preference_not_filter() -> None:
    """``나이키 상품`` 의 ``상품`` 은 '나이키가 상품'이 아니라 '나이키 브랜드의 상품'이다.

    하드 필터로 쓰면 브랜드 후보가 조용히 사라진다.
    """

    resolver = EntityResolver(InMemoryEntityRepository([NIKE]))
    tokens = [
        _candidate_token("나이키", start=0),
        _type_token(EntityType.PRODUCT, "상품", start=4),
    ]
    resolution = _resolution(resolver.resolve("나이키 상품", tokens))

    assert resolution.selected == NIKE


def test_distant_type_hint_is_ignored() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([APPLE_BRAND, APPLE_PRODUCT]))
    tokens = [
        _candidate_token("애플", start=0),
        _type_token(EntityType.BRAND, "브랜드", start=40),
    ]
    resolution = _resolution(resolver.resolve("애플" + " " * 38 + "브랜드", tokens))

    assert resolution.selected is None


def test_low_score_candidates_are_dropped() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([NIKE]), minimum_score=0.9)
    resolution = _resolution(resolver.resolve("나이", [_candidate_token("나이")]))

    assert resolution.is_unknown


def test_clear_winner_is_selected_despite_multiple_candidates() -> None:
    repository = InMemoryEntityRepository(
        [NIKE, EntityCandidate(EntityType.PRODUCT, "product:nike-air", "나이키 에어맥스", 1.0)]
    )
    resolver = EntityResolver(repository)
    resolution = _resolution(resolver.resolve("나이키", [_candidate_token("나이키")]))

    # 정확 일치(1.0)와 부분 일치(0.375)의 차이가 충분하므로 확정한다.
    assert resolution.selected == NIKE


def test_non_entity_tokens_pass_through_untouched() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([NIKE]))
    other = _type_token(EntityType.BRAND, "브랜드", start=0)
    assert resolver.resolve("브랜드", [other]) == [other]


def test_resolver_does_not_mutate_input_tokens() -> None:
    resolver = EntityResolver(InMemoryEntityRepository([NIKE]))
    token = _candidate_token("나이키")
    resolver.resolve("나이키", [token])
    assert token.value == "나이키"


def test_entity_candidate_rejects_out_of_range_score() -> None:
    with pytest.raises(ValueError, match="score"):
        EntityCandidate(EntityType.BRAND, "brand:x", "x", 1.5)
