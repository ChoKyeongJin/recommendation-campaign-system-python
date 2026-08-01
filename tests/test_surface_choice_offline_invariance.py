"""무회귀 — 키가 없거나 스위치를 끄면 이관 전과 **글자 그대로** 같아야 한다.

이 계층은 전부 ``<결정론> or <LLM>`` 형태로 붙었고 우변이 항등원(False/None)이다. 그 성질이
실제로 성립하는지, 그리고 꺼진 경로에서 모델이 **한 번도 불리지 않는지**를 여기서 못 박는다.
골든 코퍼스가 이 경로로 돌기 때문에 이게 깨지면 측정 도구 자체가 흔들린다.
"""

from __future__ import annotations

import pytest

import analytical_intent
import graph_rag
import lexicon_llm
import surface_choices


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """모델 호출이 일어나면 즉시 실패시킨다 — '조용히 켜져 있었다'를 잡는다."""

    def explode(*args, **kwargs):
        raise AssertionError("꺼진 경로에서 LLM 이 호출됐다")

    monkeypatch.setattr(graph_rag, "_llm_choose_name", explode)
    monkeypatch.setattr(graph_rag, "_llm_extract_surface_signals", explode)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lexicon_llm.invalidate_names()
    yield
    lexicon_llm.invalidate_names()


def test_surface_signals_are_empty_without_a_key():
    assert graph_rag._resolve_surface_signals("골드 등급 회원을 찾아줘") == {}


def test_signal_choices_are_empty_outside_a_scope():
    assert lexicon_llm.signal_choices("member_grade", "골드") == ()
    assert lexicon_llm.signal_choice("member_grade", "골드") is None


def test_name_resolution_is_inert_when_disabled():
    assert graph_rag._resolve_name_choice("aggregate_metric", "듣도보도못한지표") is None
    assert graph_rag._resolve_name_choice("lifecycle_canonical", "ghost_user") is None
    assert graph_rag._resolve_name_choice("resolution_requirement", "ghost_field") is None


def test_aggregate_metric_lookup_matches_backstop_only():
    # 백스톱이 읽는 것은 그대로 읽고,
    assert graph_rag._aggregate_metric_id_for_canonical("구매 금액") == "purchase_amount"
    # 못 읽는 것은 오늘처럼 None 이다(LLM 이 메우지 않는다).
    assert graph_rag._aggregate_metric_id_for_canonical("씀씀이가 큰 정도") is None


def test_lifecycle_alias_resolution_is_unchanged():
    aliases = graph_rag._lifecycle_aliases()
    # 별칭표에 있는 값은 매핑되고,
    assert graph_rag._resolve_lifecycle_value("dormant_user", aliases) == "dormant"
    # 이미 컴파일되는 값은 손대지 않고,
    assert graph_rag._resolve_lifecycle_value("dormant", aliases) == "dormant"
    # 둘 다 아닌 값은 원값 그대로다(= 오늘 동작).
    assert graph_rag._resolve_lifecycle_value("ghost_user", aliases) == "ghost_user"


def test_lifecycle_slot_object_is_returned_unchanged_when_nothing_maps():
    slots = {"lifecycle": ["dormant"], "age": {"min": 20}}
    assert graph_rag._resolve_plan_lifecycle_aliases(slots) is slots


def test_grade_surface_fallback_is_inert():
    assert graph_rag._grade_value_from_surface("플래티넘", graph_rag.MEMBER_EQ_FILTERS) is None


def test_analytics_matchers_are_deterministic():
    registry = analytical_intent.load_analytics_registry()
    compact = analytical_intent._compact("여성 회원 중 구매하지 않은 사람")
    filters = analytical_intent._match_filters(compact, registry, "여성 회원 중 구매하지 않은 사람")
    scopes = analytical_intent._match_scopes(compact, registry)
    assert [row["id"] for row in filters] == ["female"]
    assert [(row["id"], row["negated"]) for row in scopes] == [("purchase", True)]


def test_unknown_surface_stays_unmatched_when_disabled():
    """LLM 이 꺼져 있으면 새 말투는 여전히 침묵한다 — 이관의 효과가 스위치에 달려 있다는 증거."""
    # '숙녀' 는 female 의 백스톱 낱말(여성/여자) 어디에도 없다 — 켜져 있었다면 LLM 이 읽었을 표현.
    registry = analytical_intent.load_analytics_registry()
    query = "숙녀분들에게 보낼 명단"
    assert analytical_intent._match_filters(analytical_intent._compact(query), registry, query) == []


def test_kill_switch_empties_the_derived_catalog(monkeypatch):
    monkeypatch.setenv("SURFACE_CHOICE_LLM", "off")
    surface_choices.invalidate()
    graph_rag._surface_concept_catalog.cache_clear()
    try:
        catalog = graph_rag._surface_concept_catalog()
        assert all(":" not in concept.concept_id for concept in catalog)
    finally:
        monkeypatch.delenv("SURFACE_CHOICE_LLM", raising=False)
        surface_choices.invalidate()
        graph_rag._surface_concept_catalog.cache_clear()
