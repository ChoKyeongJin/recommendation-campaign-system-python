"""레거시 호환 테스트 — 이행 중 회귀가 나지 않는지 본다.

여기서 확인하는 계약은 세 가지다.

1. 레거시 어댑터는 **새 파서가 읽지 못한 자리에서만** 동작한다(경쟁하지 않는다).
2. 레거시 토큰은 **낮은 신뢰도**를 가져 출처가 구분된다.
3. 기존 사전을 **복사하지 않고** 원본을 읽는다(사본은 조용히 낡는다).

3번이 특히 중요하다. 사본을 만들면 원본이 바뀌어도 테스트는 계속 통과하면서 운영만 달라진다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lexicon_patterns
from nl_event_ir import EntityCandidate, EntityType, EventType, InMemoryEntityRepository
from nl_event_ir.legacy_adapter import (
    LEGACY_POINTER_PATH,
    LEGACY_TOKEN_CONFIDENCE,
    LegacyLexiconError,
    LegacyPatternAdapter,
)
from nl_event_ir.parser import build_default_parser
from nl_event_ir.tokenizer import SemanticToken, TokenKind

CATALOG = [EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0)]


@pytest.fixture(scope="module")
def adapter() -> LegacyPatternAdapter:
    return LegacyPatternAdapter()


# ── 원본을 읽는가 ────────────────────────────────────────────────────────────────


def test_pointer_file_does_not_duplicate_the_lexicon() -> None:
    """포인터 파일은 어휘를 담지 않는다 — 담는 순간 두 번째 진실이 생긴다."""

    payload = json.loads(LEGACY_POINTER_PATH.read_text(encoding="utf-8"))
    assert "vocabularies" not in payload
    assert "source" in payload


def test_pointer_targets_the_live_lexicon_used_by_the_existing_system() -> None:
    """기존 :mod:`lexicon_patterns` 가 읽는 바로 그 파일을 가리켜야 한다."""

    payload = json.loads(LEGACY_POINTER_PATH.read_text(encoding="utf-8"))
    pointed = (LEGACY_POINTER_PATH.parent.parent / payload["source"]).resolve()
    assert pointed == lexicon_patterns.DEFAULT_LEXICON_PATH.resolve()


def test_adapter_reads_the_existing_vocabularies(adapter: LegacyPatternAdapter) -> None:
    tokens = adapter.extract("구매한 고객")
    assert EventType.PURCHASE in [
        token.value for token in tokens if token.kind is TokenKind.EVENT
    ]


def test_missing_lexicon_raises_a_clear_error(tmp_path: Path) -> None:
    """사전이 없으면 조용히 비어 돌지 않는다 — 그 상태가 가장 찾기 어려운 고장이다."""

    with pytest.raises(LegacyLexiconError, match="cannot read legacy lexicon"):
        LegacyPatternAdapter(lexicon_path=tmp_path / "does-not-exist.json")


def test_malformed_lexicon_raises_a_clear_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(LegacyLexiconError, match="not valid JSON"):
        LegacyPatternAdapter(lexicon_path=broken)


def test_lexicon_without_vocabularies_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"patterns": {}}), encoding="utf-8")
    with pytest.raises(LegacyLexiconError, match="no 'vocabularies'"):
        LegacyPatternAdapter(lexicon_path=empty)


# ── 낮은 신뢰도 ──────────────────────────────────────────────────────────────────


def test_legacy_tokens_are_marked_with_low_confidence(
    adapter: LegacyPatternAdapter,
) -> None:
    for token in adapter.extract("구매한 고객"):
        assert token.confidence == LEGACY_TOKEN_CONFIDENCE
        assert token.source == "legacy"


# ── 경쟁하지 않는다 ──────────────────────────────────────────────────────────────


def test_adapter_only_fills_uncovered_spans(adapter: LegacyPatternAdapter) -> None:
    text = "구매한 고객"
    already_read = [
        SemanticToken(
            kind=TokenKind.EVENT,
            value=EventType.PURCHASE,
            start=0,
            end=2,
            raw_text="구매",
        )
    ]
    recovered = adapter.extract_residual(text, already_read)
    assert all(token.start >= 2 for token in recovered)


def test_adapter_adds_nothing_when_everything_is_already_read(
    adapter: LegacyPatternAdapter,
) -> None:
    text = "구매"
    covered = [
        SemanticToken(
            kind=TokenKind.EVENT,
            value=EventType.PURCHASE,
            start=0,
            end=len(text),
            raw_text=text,
        )
    ]
    assert adapter.extract_residual(text, covered) == []


# ── 파이프라인에 꽂았을 때 ────────────────────────────────────────────────────────


def test_pipeline_result_is_unchanged_for_sentences_the_new_parser_handles() -> None:
    """새 파서가 이미 처리하는 문장은 어댑터를 켜도 결과가 같아야 한다."""

    from datetime import date

    reference = date(2026, 8, 5)
    without = build_default_parser(
        InMemoryEntityRepository(CATALOG), reference_date=reference
    )
    with_legacy = build_default_parser(
        InMemoryEntityRepository(CATALOG),
        reference_date=reference,
        use_legacy_adapter=True,
    )
    sentence = "최근 3개월 동안 나이키를 두 번 이상 산 고객"
    assert without.parse(sentence).ir == with_legacy.parse(sentence).ir


def test_legacy_usage_is_visible_in_diagnostics() -> None:
    """레거시 토큰 수가 곧 이행 진척도다. 세지 못하면 이행이 끝났는지 알 수 없다."""

    from datetime import date

    parser = build_default_parser(
        InMemoryEntityRepository(CATALOG),
        reference_date=date(2026, 8, 5),
        use_legacy_adapter=True,
    )
    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    assert result.diagnostics["legacy_token_count"] == 0


def test_legacy_adapter_actually_recovers_an_expression_the_rules_miss() -> None:
    """어댑터가 **실제로 기여하는** 경로를 고정한다.

    이 테스트가 없으면 어댑터가 아무것도 반환하지 않게 망가져도 스위트는 초록으로 남는다
    (다른 레거시 테스트는 전부 '기여하지 않음'을 확인하기 때문이다).

    ``사서`` 는 새 정규화기가 의도적으로 다루지 않는 형태다(``사고``와 함께 동음이의 위험이
    있어 규칙으로 바꾸지 않는다). 기존 사전에는
    ``purchase_verb_colloquial_object_bound`` 로 들어 있으므로 어댑터가 메울 수 있다.
    """

    from datetime import date

    sentence = "상품을 사서 받은 고객"
    reference = date(2026, 8, 5)

    without = build_default_parser(
        InMemoryEntityRepository(CATALOG), reference_date=reference
    )
    with_legacy = build_default_parser(
        InMemoryEntityRepository(CATALOG),
        reference_date=reference,
        use_legacy_adapter=True,
    )

    # 새 규칙만으로는 사건을 찾지 못한다.
    assert without.parse(sentence).ir is None

    recovered = with_legacy.parse(sentence)
    assert recovered.ir is not None
    assert recovered.ir.condition.event_type is EventType.PURCHASE
    assert recovered.diagnostics["legacy_token_count"] >= 1

    # 그리고 그 토큰은 낮은 신뢰도로 표시되어 출처가 구분되어야 한다.
    legacy_tokens = [token for token in recovered.tokens if token.source == "legacy"]
    assert legacy_tokens and all(
        token.confidence == LEGACY_TOKEN_CONFIDENCE for token in legacy_tokens
    )


def test_adapter_reads_the_same_file_as_the_existing_loader() -> None:
    """두 시스템이 다른 파일을 보면 어휘가 조용히 갈라진다."""

    adapter = LegacyPatternAdapter()
    from_existing = json.loads(
        lexicon_patterns.DEFAULT_LEXICON_PATH.read_text(encoding="utf-8")
    )["vocabularies"]
    assert tuple(from_existing["purchase_verb"]) == adapter._vocabularies["purchase_verb"]


# ── 기존 시스템을 건드리지 않았는가 ───────────────────────────────────────────────


def test_existing_lexicon_module_still_loads() -> None:
    """이 리팩터링은 기존 파서를 지우지 않는다. 기존 사전 로더가 그대로 살아 있어야 한다."""

    assert lexicon_patterns.vocabulary("purchase_verb")


def test_existing_event_ir_module_is_not_shadowed() -> None:
    """새 패키지 이름이 기존 ``event_ir`` 모듈을 가리면 저장소 절반이 깨진다."""

    import event_ir

    assert Path(event_ir.__file__).name == "event_ir.py"
