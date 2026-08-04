"""Literal scanner/calendar lexicon ownership contracts.

The normalization lexicon owns unit aliases and comparison words.  The public
projections in ``condition_normalizers`` own the deliberately smaller literal
scanner grammar; consumers may derive from those projections but may not keep
another surface-to-meaning table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import calendar_window
import condition_normalizers
from query_structurer import semantic_ir


def test_public_literal_projections_match_json_and_code_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing configuration must not change the public scanner grammar."""

    payload = json.loads(
        condition_normalizers.DEFAULT_NORMALIZATION_LEXICON_PATH.read_text(
            encoding="utf-8"
        )
    )
    for section, fallback in condition_normalizers._CODE_FALLBACK.items():
        assert payload.get(section) == fallback

    monkeypatch.delenv("GRAPH_RAG_NORMALIZATION_LEXICON", raising=False)
    condition_normalizers.clear_lexicon_cache()
    json_comparisons = condition_normalizers.comparison_literal_operators()
    json_durations = condition_normalizers.numeric_duration_unit_semantics()

    monkeypatch.setenv(
        "GRAPH_RAG_NORMALIZATION_LEXICON",
        str(tmp_path / "missing-normalization-lexicon.json"),
    )
    condition_normalizers.clear_lexicon_cache()
    try:
        assert condition_normalizers.comparison_literal_operators() == json_comparisons
        assert condition_normalizers.numeric_duration_unit_semantics() == json_durations
    finally:
        condition_normalizers.clear_lexicon_cache()


def test_literal_projection_functions_are_part_of_the_public_contract() -> None:
    assert {
        "comparison_literal_operators",
        "numeric_duration_unit_semantics",
    } <= set(condition_normalizers.__all__)


def test_semantic_scanner_maps_are_exactly_the_shared_public_projections() -> None:
    comparisons = condition_normalizers.comparison_literal_operators()
    durations = condition_normalizers.numeric_duration_unit_semantics()

    assert dict(semantic_ir._COMPARISON_TERMS) == comparisons
    assert semantic_ir.DURATION_UNIT_SEMANTICS == durations

    for surface, canonical in comparisons.items():
        literals = semantic_ir.scan_literal_bindings(f"점수 {surface}")
        operators = [
            literal
            for literal in literals
            if literal["kind"] == "comparison_operator"
        ]
        assert [(item["text"], item["normalized"]) for item in operators] == [
            (surface, canonical)
        ]

    for surface, canonical in durations.items():
        literals = semantic_ir.scan_literal_bindings(
            f"최근 2{surface}",
            current_date="2026-08-04",
        )
        duration = next(item for item in literals if item["kind"] == "duration")
        assert duration["text"] == f"2{surface}"
        assert duration["normalized"]["surface_unit"] == surface
        assert duration["normalized"]["semantic_unit"] == canonical


def test_calendar_numeric_duration_maps_are_the_same_declared_subset() -> None:
    declared = condition_normalizers.numeric_duration_unit_semantics()
    exact_days = condition_normalizers.unit_days()

    assert calendar_window.KO_UNIT_TO_CANON == declared
    assert calendar_window.DURATION_UNIT_DAYS == {
        surface: exact_days[canonical]
        for surface, canonical in declared.items()
        if canonical in exact_days
    }


@pytest.mark.parametrize(
    ("surface", "canonical", "min_days"),
    [
        ("일간", "days", 2),
        ("년간", "years", None),
    ],
)
def test_daily_and_yearly_numeric_duration_regression(
    surface: str,
    canonical: str,
    min_days: int | None,
) -> None:
    text = f"최근 2{surface}"
    scanner_duration = next(
        item
        for item in semantic_ir.scan_literal_bindings(
            text,
            current_date="2026-08-04",
        )
        if item["kind"] == "duration"
    )
    assert scanner_duration["normalized"] == {
        "value": 2,
        "surface_unit": surface,
        "semantic_unit": canonical,
        "temporal_kind": "rolling_duration",
    }

    calendar_duration = calendar_window.parse_duration_window(text)
    assert calendar_duration is not None
    assert calendar_duration["value"] == 2
    assert calendar_duration["unit"] == canonical
    if min_days is None:
        assert "min_days" not in calendar_duration
    else:
        assert calendar_duration["min_days"] == min_days
