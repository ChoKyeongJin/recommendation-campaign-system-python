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
    # 2026-08-07: 기간 원자는 창의 wire 모양(``event_ir_window``)도 함께 싣는다 — 모델이
    # 값·단위를 옮겨 적다 스키마 enum 밖의 복수형을 복사하던 결함을 없앤다. 계약의 소유자는
    # tests/test_duration_binding_wire_window.py 이고 여기서는 키가 붙는다는 사실만 반영한다.
    assert scanner_duration["normalized"] == {
        "value": 2,
        "surface_unit": surface,
        "semantic_unit": canonical,
        "temporal_kind": "rolling_duration",
        "event_ir_window": {
            "type": "rolling",
            "value": 2,
            "unit": {"days": "day", "years": "year"}[canonical],
        },
    }

    calendar_duration = calendar_window.parse_duration_window(text)
    assert calendar_duration is not None
    assert calendar_duration["value"] == 2
    assert calendar_duration["unit"] == canonical
    if min_days is None:
        assert "min_days" not in calendar_duration
    else:
        assert calendar_duration["min_days"] == min_days


# ── 세 번째 표면 목록을 두지 않는다 ─────────────────────────────────────────────
# 같은 문법(숫자 + 기간 단위)을 읽는 자리가 늘 때마다 손 목록을 하나씩 더 두면, 그 목록만 낡아서
# **같은 문장이 어느 경로로 오느냐에 따라** 기간이 보이기도 하고 안 보이기도 한다. 실측:
# ``-간`` 접미형이 리터럴 추출기에는 파생으로 들어갔는데 기간 정규화기에는 '일간/년간'만 손으로
# 적혀 있어 '최근 3개월간'이 그쪽에서만 읽히지 않았다.


@pytest.mark.parametrize(
    "surface", sorted(condition_normalizers.numeric_duration_unit_semantics())
)
def test_the_period_normalizer_reads_every_declared_numeric_duration_surface(
    surface: str,
) -> None:
    from semantic_normalizers import PeriodNormalizer

    canonical = condition_normalizers.numeric_duration_unit_semantics()[surface]
    window = PeriodNormalizer.normalize(f"최근 3{surface}")

    assert (window.value, window.unit) == (3, canonical), surface


def test_the_period_normalizer_never_guesses_a_unit_it_could_not_read() -> None:
    """읽지 못한 단위를 'days' 로 물러서면 '3개월'이 조용히 3일이 된다(§20)."""
    from semantic_normalizers import NormalizationError, PeriodNormalizer

    with pytest.raises(NormalizationError):
        PeriodNormalizer.normalize("최근 24시간")


def test_the_rewrite_duration_signature_uses_the_guarded_parser() -> None:
    """재작성 가드의 기간 신호도 낱말 경계 가드를 받는다(압축 텍스트만 보지 않는다).

    ``'모두 주문'`` 의 압축 표면 ``'두주'`` 가 14일 기간 신호로 읽히면, 그 유령 신호가 재작성본
    에서 사라졌다는 이유로 **정상 재작성이 폐기된다**(실측: 라이브 코퍼스 53번).
    """
    import graph_rag

    assert graph_rag._duration_days_signals("앱과 PC 양쪽 채널에서 모두 주문한 회원") == set()
    assert graph_rag._duration_days_signals("VIP에 한해서 발송한 회원") == set()
    # 정상 표현은 그대로 잡힌다 — 가드가 기능을 잃지 않았다.
    assert graph_rag._duration_days_signals("일주일 이상 장바구니에 담아둔 회원") == {7}
    assert graph_rag._duration_days_signals("최근 7일 구매한 회원") == {7}
    assert graph_rag._duration_days_signals("3주간 유지한 회원") == {21}
