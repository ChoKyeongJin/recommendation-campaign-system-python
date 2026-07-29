from __future__ import annotations

import graph_rag as g


def test_auto_parser_defaults_to_rules_first_authority(monkeypatch) -> None:
    monkeypatch.delenv("QUERY_PLAN_AUTHORITY", raising=False)

    assert g._query_plan_authority("auto") == "rules_first"


def test_prompt_normalization_defaults_to_unresolved_only(monkeypatch) -> None:
    """The default path must not rewrite clear conditions in the full prompt."""
    monkeypatch.delenv("PROMPT_REWRITE_STYLE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    query = "최근 90일 구매금액 3만원 이하 고객 중 서울 거주 고객"
    result = g.normalize_prompt(query)

    assert result["normalized"] == query
    assert result["mode"] == "rules_unresolved_only"


def test_full_targeting_rewrite_remains_explicit_opt_in(monkeypatch) -> None:
    """An operator can still explicitly opt in to the legacy rewrite mode."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = g.normalize_prompt("돈 없어 보이는 고객", style="targeting")

    assert result["mode"] == "rules"
