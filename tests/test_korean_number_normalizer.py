"""Single-source contract for the shared Korean numeral grammar."""

from __future__ import annotations

import re

import pytest

import aggregate_parser_config
import korean_number_normalizer as korean_numbers
import lexicon_patterns
import semantic_normalizers
import semantic_requirements


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("일", 1),
        ("십", 10),
        ("이십", 20),
        ("백이십삼", 123),
        ("천", 1_000),
        ("영", None),
        ("이십만", None),
        ("", None),
        (None, None),
    ],
)
def test_sino_korean_number_compatibility(surface: object, expected: int | None) -> None:
    assert korean_numbers.parse_sino_korean_number(surface) == expected
    assert korean_numbers.normalize_korean_number(surface, form="sino") == expected


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("한", 1),
        ("두", 2),
        ("세", 3),
        ("네", 4),
        ("다섯", 5),
        ("열", None),
        ("두세", None),
        (1, None),
    ],
)
def test_native_korean_number_compatibility(surface: object, expected: int | None) -> None:
    assert korean_numbers.parse_native_korean_number(surface) == expected
    assert korean_numbers.normalize_korean_number(surface, form="native") == expected


def test_public_api_is_domain_neutral_and_reusable_by_graph_parsers() -> None:
    pattern = re.compile(
        rf"^(?P<number>{korean_numbers.native_korean_number_alternation()})\s*(?:개|번)$"
    )

    match = pattern.fullmatch("다섯 개")

    assert match is not None
    assert korean_numbers.normalize_korean_number(match.group("number")) == 5
    assert korean_numbers.native_korean_number_surfaces() == (
        "한",
        "두",
        "세",
        "네",
        "다섯",
    )
    with pytest.raises(ValueError, match="unknown Korean number form"):
        korean_numbers.normalize_korean_number("한", form="guess")  # type: ignore[arg-type]


def test_existing_consumers_derive_from_the_shared_declaration(monkeypatch) -> None:
    monkeypatch.setattr(
        korean_numbers,
        "parse_sino_korean_number",
        lambda surface: 7 if surface == "이십" else None,
    )
    assert semantic_normalizers.AmountNormalizer.normalize("이십만원").amount == 70_000

    monkeypatch.setattr(
        korean_numbers,
        "parse_native_korean_number",
        lambda surface: 7 if surface == "세" else None,
    )

    obligations = semantic_requirements.capture_source_semantic_obligations(
        "지정한 세 브랜드를 모두 구매한 회원"
    )
    referenced_set = next(
        item for item in obligations if item.base.get("name") == "referenced_entity_set"
    )
    assert referenced_set.value["cardinality"] == 7


def test_runtime_language_mirrors_match_the_shared_native_values() -> None:
    """Configuration mirrors cannot silently acquire a word without a value."""

    declared = dict(korean_numbers.NATIVE_KOREAN_NUMBER_VALUES)
    assert dict(aggregate_parser_config.rules().number_words) == declared
    assert set(lexicon_patterns.vocabulary("source_korean_count")) == set(declared)
