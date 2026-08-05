"""정규화기 단독 테스트.

이 단계에서 검증할 것은 두 가지다. (1) 사전에서 걷어낸 활용형이 규칙으로 복원되는가,
(2) 그 규칙이 **상품명·브랜드명을 훼손하지 않는가**. 두 번째가 더 중요하다 — 정규화가
과하면 조용히 다른 뜻이 되고, 그 실패는 아래 단계에서 드러나지 않는다.
"""

from __future__ import annotations

import pytest

from nl_event_ir.normalizer import (
    NormalizationRule,
    PassthroughNormalizer,
    RuleBasedKoreanNormalizer,
)


@pytest.fixture
def normalizer() -> RuleBasedKoreanNormalizer:
    return RuleBasedKoreanNormalizer()


# ── 요구 사양에 명시된 변환 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("두 번", "2번"),
        ("세 회", "3회"),
        ("한 건", "1건"),
        ("네 개", "4개"),
        ("다섯 번", "5번"),
        ("이번달", "이번 달"),
        ("지난달", "지난 달"),
        ("샀다", "사다"),
        ("샀던", "사다"),
        ("산 고객", "사다 고객"),
    ],
)
def test_required_normalizations(
    normalizer: RuleBasedKoreanNormalizer, source: str, expected: str
) -> None:
    assert normalizer.normalize(source) == expected


def test_golden_sentence_normalization(normalizer: RuleBasedKoreanNormalizer) -> None:
    assert (
        normalizer.normalize("최근 3개월 동안 나이키를 두 번 이상 산 고객")
        == "최근 3개월 동안 나이키를 2번 이상 사다 고객"
    )


# ── 훼손 방지: 여기가 무너지면 정규화가 의미를 바꾼다 ────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "부산에 사는 고객",
        "계산이 끝난 고객",
        "등산 용품을 구매한 고객",
        "산에 간 고객",
        "안양시 고객",  # '안'(부정 부사)이 지명 첫 음절을 건드리면 안 된다
        "출산 준비물",
    ],
)
def test_does_not_damage_words_containing_verb_syllables(
    normalizer: RuleBasedKoreanNormalizer, text: str
) -> None:
    assert "사다" not in normalizer.normalize(text)


def test_standalone_mountain_is_a_known_false_positive(
    normalizer: RuleBasedKoreanNormalizer,
) -> None:
    """알려진 트레이드오프를 **숨기지 않고 고정**한다.

    요구 사양이 ``산 고객 → 사다 고객`` 을 명시적으로 요구하므로 띄어쓴 단독 ``산`` 은
    구매 동사로 읽는다. 그 대가로 ``산 정상``(山)도 구매로 읽힌다 — 문맥 없이 둘을 가를
    방법이 없기 때문이다. 이 테스트는 그 대가가 **의도된 것**임을 기록하며, 정책을 바꾸려면
    여기부터 바꾸면 된다.
    """

    assert normalizer.normalize("상품을 산 고객") == "상품을 사다 고객"
    # 같은 규칙의 대가 — 아래가 '사다 정상'이 되는 것은 알려진 오탐이다.
    assert normalizer.normalize("산 정상") == "사다 정상"
    # 다만 붙어 있는 형태는 반드시 보호된다.
    assert normalizer.normalize("산악 지역") == "산악 지역"
    assert normalizer.normalize("산에 간 고객") == "산에 간 고객"


def test_native_number_not_converted_when_bound_to_particle(
    normalizer: RuleBasedKoreanNormalizer,
) -> None:
    """``한 번도`` 는 '1번도'가 아니다 — 관용구이므로 숫자로 바꾸지 않는다."""

    assert normalizer.normalize("한 번도 구매하지 않은") == "한 번도 구매하지 않은"


def test_brand_name_with_number_survives(normalizer: RuleBasedKoreanNormalizer) -> None:
    assert "나이키" in normalizer.normalize("나이키 에어맥스 두 번 구매")


# ── 텍스트 전처리 ────────────────────────────────────────────────────────────────


def test_collapses_whitespace_and_trims(normalizer: RuleBasedKoreanNormalizer) -> None:
    assert normalizer.normalize("  공백   정리   테스트  ") == "공백 정리 테스트"


def test_lowercases_ascii_only(normalizer: RuleBasedKoreanNormalizer) -> None:
    assert normalizer.normalize("NIKE 상품") == "nike 상품"


def test_strips_sentence_punctuation(normalizer: RuleBasedKoreanNormalizer) -> None:
    assert normalizer.normalize("구매한 고객?!") == "구매한 고객"


def test_keeps_date_separators(normalizer: RuleBasedKoreanNormalizer) -> None:
    """마침표는 지운다면 ``2026.07.01`` 이 깨진다."""

    assert normalizer.normalize("2026.07.01 구매") == "2026.07.01 구매"


def test_normalization_is_idempotent(normalizer: RuleBasedKoreanNormalizer) -> None:
    once = normalizer.normalize("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    assert normalizer.normalize(once) == once


# ── 위치 매핑 ────────────────────────────────────────────────────────────────────


def test_span_maps_back_to_original_after_length_change() -> None:
    """``두 번`` → ``2번`` 으로 길이가 줄어도 뒤쪽 토큰의 원문 위치를 되찾을 수 있어야 한다."""

    normalizer = RuleBasedKoreanNormalizer()
    source = "최근 3개월 동안 나이키를 두 번 이상 산 고객"
    result = normalizer.normalize_with_spans(source)

    verb_index = result.normalized.index("사다")
    start, end = result.to_original_span(verb_index, verb_index + len("사다"))
    assert source[start:end] == "산"


def test_span_is_identity_when_nothing_changes() -> None:
    normalizer = RuleBasedKoreanNormalizer()
    source = "나이키를 구매한 고객"
    result = normalizer.normalize_with_spans(source)

    assert result.normalized == source
    index = source.index("나이키")
    assert result.to_original_span(index, index + 3) == (index, index + 3)


def test_leading_whitespace_shifts_spans() -> None:
    normalizer = RuleBasedKoreanNormalizer()
    source = "   나이키 구매"
    result = normalizer.normalize_with_spans(source)

    index = result.normalized.index("나이키")
    start, end = result.to_original_span(index, index + 3)
    assert source[start:end] == "나이키"


# ── 규칙 계약 ────────────────────────────────────────────────────────────────────


def test_non_converging_rule_is_rejected() -> None:
    """치환 결과가 다시 매치되는 규칙은 무한 루프다. 조용히 매달리지 않고 실패해야 한다."""

    looping = NormalizationRule(
        name="loop",
        pattern=__import__("re").compile("a"),
        replacement=lambda match: "a",
    )
    normalizer = RuleBasedKoreanNormalizer(rules=(looping,))
    with pytest.raises(ValueError, match="non-converging"):
        normalizer.normalize("a" * 3)


def test_passthrough_normalizer_changes_nothing() -> None:
    source = "  샀다  두 번  "
    assert PassthroughNormalizer().normalize(source) == source
