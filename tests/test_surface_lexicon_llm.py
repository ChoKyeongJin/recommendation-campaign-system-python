"""표면어 LLM 소유 계층의 계약 — 닫힌 집합·원문 근거·빈칸 보완·소유권 경계.

이 계층이 지키기로 한 것은 넷이다. 하나라도 깨지면 "LLM 이 개념을 지어내지 않는다"가 무너진다.

  1. **닫힌 집합** — surface_concepts.json 에 없는 concept_id 는 버린다.
  2. **원문 근거** — 근거가 질의에 글자 그대로 없으면 버린다(번역·요약·유추 금지).
  3. **빈칸 보완** — 백스톱이 이미 읽은 신호는 LLM 없이도 참이고, LLM 은 침묵한 자리만 메운다.
  4. **소유권 경계** — LLM 이 소유한 그룹의 낱말은 데이터 파일에 다시 쌓이지 않는다(래칫).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import graph_rag
import lexicon_llm

CONCEPTS = lexicon_llm.load_concepts()
CONCEPT_IDS = set(lexicon_llm.concept_ids(CONCEPTS))


# ── 1. 개념 선언(닫힌 집합)이 소비 지점과 맞는가 ──────────────────────────────────────────
def test_every_llm_owned_group_has_a_concept() -> None:
    """LLM 이 소유한 그룹은 전부 개념 선언이 있어야 한다 — 없으면 그 그룹은 백스톱만 남아 조용히 침묵한다."""
    missing = sorted(graph_rag._LLM_OWNED_LEXICON_GROUPS - CONCEPT_IDS)
    assert not missing, f"surface_concepts.json 에 개념 선언이 없는 LLM 소유 그룹: {missing}"


def test_every_objective_has_a_concept() -> None:
    """목적(objective) 분류도 LLM 이 읽는다 — 규칙 키워드가 침묵할 때 쓸 개념이 선언돼 있어야 한다."""
    missing = [
        objective
        for objective, _keywords in graph_rag._lexicon_objective_rules()
        if f"objective_{objective}" not in CONCEPT_IDS
    ]
    assert not missing, f"개념 선언이 없는 캠페인 목적: {missing}"


def test_concepts_declare_meaning_not_words() -> None:
    """개념 선언은 '뜻'이지 낱말 목록이 아니다 — 설명이 비면 LLM 이 경계를 알 수 없다."""
    assert CONCEPTS, "개념 선언을 못 읽었다(파일 부재/파손이면 LLM 계층 전체가 비활성)"
    for concept in CONCEPTS:
        assert len(concept.description) >= 10, f"{concept.concept_id}: 설명이 너무 짧다"


# ── 2. 응답 검증(닫힌 집합 + 원문 근거) ──────────────────────────────────────────────────
QUERY = "3개월 뒤에 재입고되는 물건을 알려드리고 싶은 고객"


def test_validate_keeps_in_set_signal_with_verbatim_evidence() -> None:
    payload = {"signals": [{"concept_id": "channel_signal_words", "evidence": "알려드리고 싶은"}]}
    assert lexicon_llm.validate(payload, CONCEPT_IDS, QUERY) == {
        "channel_signal_words": ("알려드리고싶은",)
    }


def test_validate_drops_concept_outside_closed_set() -> None:
    payload = {"signals": [{"concept_id": "made_up_concept", "evidence": "알려드리고 싶은"}]}
    assert lexicon_llm.validate(payload, CONCEPT_IDS, QUERY) == {}


def test_validate_drops_evidence_not_in_query() -> None:
    """LLM 이 '뜻은 맞는데 원문에 없는 말'로 근거를 대면 채택하지 않는다."""
    payload = {"signals": [{"concept_id": "channel_signal_words", "evidence": "홍보하고 싶은"}]}
    assert lexicon_llm.validate(payload, CONCEPT_IDS, QUERY) == {}


def test_validate_drops_too_short_evidence() -> None:
    """한 글자 근거는 어떤 문장에도 걸려 스팬 검사를 무력화한다."""
    payload = {"signals": [{"concept_id": "channel_signal_words", "evidence": "고"}]}
    assert lexicon_llm.validate(payload, CONCEPT_IDS, QUERY) == {}


def test_validate_survives_malformed_payload() -> None:
    for payload in (None, {}, {"signals": "nope"}, {"signals": [None, 3]}):
        assert lexicon_llm.validate(payload, CONCEPT_IDS, QUERY) == {}


def test_resolve_returns_empty_when_extractor_fails() -> None:
    """추출 실패가 파싱을 막으면 안 된다 — 빈 결과로 백스톱만 쓰게 둔다."""

    def boom(_text, _concepts):
        raise RuntimeError("network down")

    assert lexicon_llm.resolve(QUERY, boom, CONCEPTS) == {}


# ── 3. 스팬 재사용: 전체 질의로 해석한 신호를 절 단위 질문에 그대로 쓴다 ────────────────────
def test_hit_is_scoped_to_the_span() -> None:
    signals = {"channel_signal_words": ("알려드리고싶은",)}
    assert lexicon_llm.hit(signals, "channel_signal_words", "알려드리고 싶은 고객")
    assert not lexicon_llm.hit(signals, "channel_signal_words", "3개월 뒤에 재입고되는 물건")
    assert not lexicon_llm.hit(signals, "repurchase_terms", "알려드리고 싶은 고객")


# ── 4. 빈칸 보완: 백스톱이 침묵한 말투만 LLM 이 메운다 ──────────────────────────────────────
def test_backstop_wins_without_any_llm_signal() -> None:
    """사전에 있는 말은 LLM 스코프가 없어도 그대로 참이다(오프라인·키 없음 경로)."""
    assert graph_rag._lexicon_signal("channel_signal_words", "쿠폰을 발송하고 싶어요")


def test_llm_fills_what_the_backstop_cannot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """동결 백스톱에 없는 말투('알려드리고 싶은')를 LLM 해석이 메운다."""
    assert not graph_rag._lexicon_signal("channel_signal_words", QUERY)
    monkeypatch.setattr(
        graph_rag,
        "_resolve_surface_signals",
        lambda *_args, **_kwargs: {"channel_signal_words": ("알려드리고싶은",)},
    )
    with graph_rag._surface_signal_scope(QUERY):
        assert graph_rag._lexicon_signal("channel_signal_words", QUERY)
    # 스코프를 벗어나면 다시 백스톱만 — 해석이 전역 상태로 새지 않는다.
    assert not graph_rag._lexicon_signal("channel_signal_words", QUERY)


def test_scope_is_reused_for_a_sub_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """파이프라인은 전체 프롬프트로 열고 그 안에서 타겟팅 절만 다시 계획한다 — 두 번 부르지 않는다."""
    calls: list[str] = []

    def _resolve(text, *_args, **_kwargs):
        calls.append(text)
        return {"channel_signal_words": ("알려드리고싶은",)}

    monkeypatch.setattr(graph_rag, "_resolve_surface_signals", _resolve)
    with graph_rag._surface_signal_scope(QUERY):
        with graph_rag._surface_signal_scope("알려드리고 싶은 고객"):
            assert graph_rag._lexicon_signal("channel_signal_words", "알려드리고 싶은 고객")
    assert calls == [QUERY]


# ── 5. 소유권 경계 래칫 ────────────────────────────────────────────────────────────────
LEXICON_PATH = Path(__file__).resolve().parents[1] / "docs" / "data" / "targeting_lexicon.json"

# 동결 백스톱의 낱말 수 상한. 이관 시점 값이며 **올릴 수 없다** — 새 말투는 LLM 이 읽는다.
# 내려가는 것(백스톱 축소)은 언제든 허용한다.
_BACKSTOP_TERM_CEILING = 130


def _backstop_term_count() -> int:
    total = 0
    for group in graph_rag._LLM_OWNED_LEXICON_GROUPS:
        total += len(graph_rag._DEFAULT_TARGETING_LEXICON[group])
    total += sum(
        len(rule["keywords"]) for rule in graph_rag._DEFAULT_TARGETING_LEXICON["objective_rules"]
    )
    return total


def test_data_file_no_longer_owns_llm_owned_surface_words() -> None:
    """이관한 신호어가 데이터 파일로 되돌아오면 소유권이 둘로 갈라진다 — 그 순간 실패시킨다."""
    payload = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    strays = sorted(graph_rag._LLM_OWNED_LEXICON_GROUPS & set(payload)) + (
        ["objective_rules"] if "objective_rules" in payload else []
    )
    assert not strays, (
        f"targeting_lexicon.json 에 LLM 소유 그룹이 다시 들어왔다: {strays}. "
        "새 말투는 이 파일이 아니라 LLM 이 읽는다 — 개념이 정말 새로 필요하면 "
        "docs/data/surface_concepts.json 에 항목을 추가하라."
    )


def test_backstop_does_not_grow() -> None:
    count = _backstop_term_count()
    assert count <= _BACKSTOP_TERM_CEILING, (
        f"동결 백스톱 낱말이 늘었다: {count} > 상한 {_BACKSTOP_TERM_CEILING}. "
        "백스톱은 키 없는 환경의 재현용이지 표현형을 받는 곳이 아니다."
    )


ANALYTICS_PATH = Path(__file__).resolve().parents[1] / "docs" / "data" / "analytics_registry.json"


def test_analytics_registry_no_longer_owns_aggregate_surface_words() -> None:
    """집계 함수어·미지원 한정어는 위치를 안 쓰는 순수 판정이라 LLM 이 소유한다."""
    payload = json.loads(ANALYTICS_PATH.read_text(encoding="utf-8"))
    strays = [k for k in ("aggregateFunctions", "unsupportedMetricQualifiers") if k in payload]
    assert not strays, (
        f"analytics_registry.json 에 표면어가 다시 들어왔다: {strays}. "
        "지표·디멘션·필터의 물리 매핑(접지)만 이 파일이 소유한다."
    )


def test_every_aggregate_function_has_a_concept() -> None:
    import analytical_intent

    missing = [
        function
        for function in analytical_intent._AGGREGATE_FUNCTION_BACKSTOP
        if f"aggregate_{function.casefold()}" not in CONCEPT_IDS
    ]
    assert not missing, f"개념 선언이 없는 집계 함수: {missing}"
    assert "unsupported_metric_qualifier" in CONCEPT_IDS


def test_aggregate_function_falls_back_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """백스톱이 못 읽은 집계 표현을 LLM 이 메운다 — 레지스트리에 표면어가 없어도 동작한다."""
    import analytical_intent

    query = "회원들이 그동안 지출한 것을 죄다 모으면"
    compact = analytical_intent._compact(query)
    assert analytical_intent._match_aggregate_function(compact, {}) is None
    monkeypatch.setattr(
        graph_rag, "_resolve_surface_signals", lambda *_a, **_k: {"aggregate_sum": ("죄다모으면",)}
    )
    with graph_rag._surface_signal_scope(query):
        assert analytical_intent._match_aggregate_function(compact, {}) == "SUM"


def test_span_local_groups_stay_in_the_data_file() -> None:
    """위치로 판정하는 어휘(분리 지점·인접성)는 LLM 이 대체할 수 없으므로 파일이 계속 소유한다."""
    payload = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    for group in ("audience_direction_markers", "audience_direction_marker_exceptions", "cart_terms"):
        assert payload.get(group), f"{group} 은 스팬 지역 어휘라 데이터 파일이 소유해야 한다"
