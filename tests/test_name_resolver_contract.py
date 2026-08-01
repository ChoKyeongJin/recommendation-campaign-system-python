"""Tier N(이름 리졸버) 규약.

Tier Q 는 haystack 이 사용자 원문이지만, 여기는 haystack 이 **식별자**다. 같은 검증기를 쓰되
근거가 원문이 아니라 그 이름 안에 있어야 하고, 구조 접두어는 근거로 치지 않는다.
"""

from __future__ import annotations

import pytest

import lexicon_llm

CANDIDATES = (
    ("target_user.location", "거주지"),
    ("metric_trend.baseline", "기준 기간"),
    ("metric_trend.current", "비교 기간"),
)


@pytest.fixture(autouse=True)
def _enable_and_clear(monkeypatch):
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "true")
    lexicon_llm.invalidate_names()
    yield
    lexicon_llm.invalidate_names()


def _extract(signals):
    """고정 응답 추출기 + 호출 횟수 기록."""
    calls: list[str] = []

    def extract(value, candidates):
        calls.append(value)
        return {"signals": signals}

    return extract, calls


def test_ids_outside_the_closed_set_are_dropped():
    extract, _ = _extract([{"concept_id": "target_user.invented", "evidence": "invented"}])
    assert lexicon_llm.resolve_name("invented", CANDIDATES, extract) is None


def test_evidence_must_appear_in_the_name_itself():
    # 근거가 이름 안에 없다 — 질의에 있었더라도 여기서는 인정되지 않는다.
    extract, _ = _extract([{"concept_id": "target_user.location", "evidence": "서울에 사는"}])
    assert lexicon_llm.resolve_name("residential_area", CANDIDATES, extract) is None


def test_structural_prefix_alone_cannot_justify_a_choice():
    """'customer' 한 조각이 customer_* 후보 전부를 정당화하는 것을 막는다."""
    extract, _ = _extract([{"concept_id": "target_user.location", "evidence": "customer"}])
    picked = lexicon_llm.resolve_name(
        "customer_value", CANDIDATES, extract, reject_evidence=frozenset({"customer"})
    )
    assert picked is None


def test_real_evidence_still_wins_with_reject_set():
    extract, _ = _extract([{"concept_id": "target_user.location", "evidence": "location"}])
    picked = lexicon_llm.resolve_name(
        "customer_location", CANDIDATES, extract, reject_evidence=frozenset({"customer"})
    )
    assert picked == "target_user.location"


def test_tie_on_longest_evidence_fails_closed():
    """애매하면 버린다 — 임의 tie-break 는 그대로 SQL 이 된다."""
    extract, _ = _extract(
        [
            {"concept_id": "metric_trend.baseline", "evidence": "period"},
            {"concept_id": "metric_trend.current", "evidence": "period"},
        ]
    )
    assert lexicon_llm.resolve_name("period_x", CANDIDATES, extract) is None


def test_longest_evidence_wins_when_unique():
    extract, _ = _extract(
        [
            {"concept_id": "metric_trend.baseline", "evidence": "baseline"},
            {"concept_id": "metric_trend.current", "evidence": "e"},
        ]
    )
    assert lexicon_llm.resolve_name("baseline_period", CANDIDATES, extract) == "metric_trend.baseline"


def test_negative_results_are_cached():
    """캐시하지 않으면 '매칭 안 되는 이름'이 요청마다 다시 호출된다 = 실패 경로가 상시 경로."""
    extract, calls = _extract([])
    assert lexicon_llm.resolve_name("unknown_field", CANDIDATES, extract) is None
    assert lexicon_llm.resolve_name("unknown_field", CANDIDATES, extract) is None
    assert len(calls) == 1


def test_disabled_never_calls_the_model():
    extract, calls = _extract([{"concept_id": "target_user.location", "evidence": "location"}])
    import os

    os.environ["SURFACE_LEXICON_LLM"] = "off"
    try:
        assert lexicon_llm.resolve_name("location", CANDIDATES, extract) is None
    finally:
        os.environ["SURFACE_LEXICON_LLM"] = "true"
    assert calls == []


def test_extract_failure_is_not_fatal():
    def boom(value, candidates):
        raise RuntimeError("model down")

    assert lexicon_llm.resolve_name("location", CANDIDATES, boom) is None


def test_empty_candidates_short_circuits():
    extract, calls = _extract([{"concept_id": "x", "evidence": "x"}])
    assert lexicon_llm.resolve_name("location", (), extract) is None
    assert calls == []


def test_candidate_change_invalidates_the_cache():
    """후보가 캐시 키에 들어가므로 레지스트리가 바뀌면 별도 버전 키 없이도 갈린다."""
    extract_a, _ = _extract([])
    assert lexicon_llm.resolve_name("location", CANDIDATES, extract_a) is None
    extract_b, calls_b = _extract([{"concept_id": "other.location", "evidence": "location"}])
    grown = (*CANDIDATES, ("other.location", "다른 거주지"))
    assert lexicon_llm.resolve_name("location", grown, extract_b) == "other.location"
    assert calls_b == ["location"]
