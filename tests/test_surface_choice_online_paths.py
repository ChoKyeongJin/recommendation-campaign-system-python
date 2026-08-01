"""켜진 경로 — 백스톱이 침묵한 표현을 LLM 이 실제로 메우는가, 그리고 그 결과가 검증을 받는가.

무회귀 테스트(test_surface_choice_offline_invariance)가 "꺼져 있으면 오늘과 같다"를 못 박는다면,
여기는 "켜져 있으면 무엇이 달라지는가"를 못 박는다. 둘이 짝이다 — 앞의 것만 있으면 아무 일도 하지
않는 코드가 통과하고, 뒤의 것만 있으면 결정론 경로가 깨져도 통과한다.

모델은 부르지 않는다. contextvar 에 신호를 직접 주입해서 **소비 지점의 채택 규약**만 검증한다.
"""

from __future__ import annotations

import pytest

import graph_rag
import lexicon_llm


@pytest.fixture(autouse=True)
def _clear_names():
    lexicon_llm.invalidate_names()
    yield
    lexicon_llm.invalidate_names()


def _grade_signal(canonical: str, evidence: str) -> dict[str, tuple[str, ...]]:
    return {lexicon_llm.namespaced("member_grade", canonical): (lexicon_llm.compact(evidence),)}


# ── Tier Q: 등급 ──────────────────────────────────────────────────────────────────────────
def test_llm_fills_a_grade_phrase_the_backstop_cannot_read():
    """'최고 등급' 은 _GRADE_SURFACE_TO_VALUE 어디에도 없다 — 오늘이면 조용히 None 이다."""
    phrase = "최고 등급"
    assert graph_rag._grade_value_from_surface(phrase, graph_rag.MEMBER_EQ_FILTERS) is None
    with lexicon_llm.signal_scope(phrase, lambda _t: _grade_signal("vip", phrase)):
        assert graph_rag._grade_value_from_surface(phrase, graph_rag.MEMBER_EQ_FILTERS) == "vip"


def test_grade_pick_must_survive_the_existing_allowlist():
    """LLM 이 무엇을 고르든 호출자의 허용 집합을 통과해야 한다 — 새 컴파일 경로가 열리지 않는다."""
    phrase = "듣도 보도 못한 등급"
    with lexicon_llm.signal_scope(phrase, lambda _t: _grade_signal("ghost_grade", phrase)):
        assert graph_rag._grade_value_from_surface(phrase, graph_rag.MEMBER_EQ_FILTERS) is None


def test_grade_evidence_must_be_present_in_the_operand_surface():
    """근거가 이 조각 안에 없으면 채택하지 않는다(다른 절의 근거를 빌려 쓸 수 없다)."""
    with lexicon_llm.signal_scope("최고 등급", lambda _t: _grade_signal("vip", "최고 등급")):
        assert graph_rag._grade_value_from_surface("서울 사는 사람", graph_rag.MEMBER_EQ_FILTERS) is None


def test_ambiguous_grade_choice_fails_closed():
    """근거 길이가 같은 둘이 오면 버린다 — 임의로 고르면 그 임의성이 그대로 SQL 이 된다."""
    phrase = "골드실버"
    signals = {
        lexicon_llm.namespaced("member_grade", "gold_grade"): ("골드",),
        lexicon_llm.namespaced("member_grade", "silver_grade"): ("실버",),
    }
    with lexicon_llm.signal_scope(phrase, lambda _t: signals):
        assert lexicon_llm.signal_choice("member_grade", phrase) is None


# ── Tier N: 지표 / lifecycle ───────────────────────────────────────────────────────────────
def _name_extract(concept_id: str, evidence: str):
    return lambda value, candidates: {"signals": [{"concept_id": concept_id, "evidence": evidence}]}


def test_llm_fills_a_metric_phrase_the_synonyms_cannot_read(monkeypatch):
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "true")
    monkeypatch.setattr(graph_rag, "_llm_choose_name", _name_extract("purchase_amount", "amount"))
    assert graph_rag._aggregate_metric_id_for_canonical("total_spend_amount") == "purchase_amount"


def test_metric_pick_outside_the_registry_is_dropped(monkeypatch):
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "true")
    monkeypatch.setattr(graph_rag, "_llm_choose_name", _name_extract("invented_metric", "metric"))
    assert graph_rag._aggregate_metric_id_for_canonical("some_metric") is None


def test_lifecycle_fallback_only_fires_for_uncompilable_values(monkeypatch):
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "true")
    called: list[str] = []

    def spy(value, candidates):
        called.append(value)
        return {"signals": [{"concept_id": "dormant", "evidence": value}]}

    monkeypatch.setattr(graph_rag, "_llm_choose_name", spy)
    aliases = graph_rag._lifecycle_aliases()
    # 별칭표에 있는 값과 이미 컴파일되는 값은 모델을 부르지 않는다.
    graph_rag._resolve_lifecycle_value("dormant_user", aliases)
    graph_rag._resolve_lifecycle_value("dormant", aliases)
    assert called == []
    # 둘 다 아닌 값에서만 부른다.
    assert graph_rag._resolve_lifecycle_value("dormant", aliases) == "dormant"
    assert graph_rag._resolve_lifecycle_value("sleeping_customer", aliases) == "dormant"
    assert called == ["sleeping_customer"]


def test_lifecycle_pick_must_be_compilable(monkeypatch):
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "true")
    monkeypatch.setattr(graph_rag, "_llm_choose_name", _name_extract("ghost_state", "ghost"))
    aliases = graph_rag._lifecycle_aliases()
    assert graph_rag._resolve_lifecycle_value("ghost_state", aliases) == "ghost_state"
