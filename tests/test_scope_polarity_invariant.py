"""극성 반전 전용 가드 — 이 이관의 최대 위험.

memberScope 판정은 불리언 문맥 판정이 아니라 EXISTS/NOT EXISTS **술어**가 되어 모집단을 통째로
바꾼다. 그리고 위험한 형태가 하나 있다: "구매 경험이 전무한" 에서 LLM 이 근거 '구매'로 긍정
스코프를 켜면 모집단이 뒤집히는데, 그 근거는 원문에 글자 그대로 있으므로 근거 검사를 **통과한다**.

그래서 긍정형만 이관하는 것은 안전하지 않다. 부정 개념을 짝으로 두고 부정 우선 순서를 지키는 것이
안전 조건이고, 이 파일이 그 조건을 못 박는다.
"""

from __future__ import annotations

import pytest

import analytical_intent
import lexicon_llm

REGISTRY = analytical_intent.load_analytics_registry()


def _scopes_for(query: str, signals: dict[str, tuple[str, ...]]):
    compact = analytical_intent._compact(query)
    with lexicon_llm.signal_scope(query, lambda _text: signals):
        return {row["id"]: row for row in analytical_intent._match_scopes(compact, REGISTRY)}


def _positive(scope_id: str, evidence: str) -> dict[str, tuple[str, ...]]:
    return {lexicon_llm.namespaced("analytics_scope", scope_id): (lexicon_llm.compact(evidence),)}


def _negative(scope_id: str, evidence: str) -> dict[str, tuple[str, ...]]:
    return {lexicon_llm.namespaced("analytics_scope_negated", scope_id): (lexicon_llm.compact(evidence),)}


def test_registry_negation_beats_an_llm_positive():
    """등록된 부정형이 걸리는 문장에 가짜 긍정을 주입해도 부정이 이긴다."""
    scopes = _scopes_for("구매하지 않은 고객", _positive("purchase", "구매"))
    assert scopes["purchase"]["negated"] is True


def test_unregistered_negation_is_absorbed_by_the_paired_concept():
    """사전에 없는 부정형('구매 경험이 전무한') — 짝 개념이 없으면 여기서 모집단이 뒤집힌다."""
    query = "구매 경험이 전무한 회원"
    signals = {**_positive("purchase", "구매"), **_negative("purchase", "구매 경험이 전무한")}
    scopes = _scopes_for(query, signals)
    assert scopes["purchase"]["negated"] is True


def test_llm_positive_alone_still_creates_a_positive_scope():
    """짝 방어가 긍정 보완 자체를 막아서는 안 된다(그러면 이관이 무의미하다)."""
    scopes = _scopes_for("물건을 사 간 사람", _positive("purchase", "사 간"))
    assert scopes["purchase"]["negated"] is False


def test_no_signals_means_todays_behaviour():
    """스코프 밖·신호 없음 = 백스톱만. 결정론 경로가 그대로다."""
    compact = analytical_intent._compact("구매한 고객")
    baseline = analytical_intent._match_scopes(compact, REGISTRY)
    assert [row["id"] for row in baseline] == ["purchase"]
    assert baseline[0]["negated"] is False


@pytest.mark.parametrize("scope_id", ["purchase", "login", "campaign_response", "cart"])
def test_every_scope_has_a_paired_negated_concept(scope_id):
    """짝이 빠진 축이 하나라도 있으면 그 축에서 극성 반전이 다시 열린다."""
    import surface_choices

    positive = {choice.choice_id for choice in surface_choices.choice_set("analytics_scope")}
    negated = {choice.choice_id for choice in surface_choices.choice_set("analytics_scope_negated")}
    assert scope_id in positive
    assert scope_id in negated
