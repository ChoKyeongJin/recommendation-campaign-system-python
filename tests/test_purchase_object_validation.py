from __future__ import annotations

import graph_rag


def _without_brand_lookup(monkeypatch) -> None:
    monkeypatch.setattr(graph_rag, "_purchase_brand_names", lambda: ())


def test_rules_parser_does_not_extract_purchase_object() -> None:
    plan = graph_rag._build_rule_query_plan(
        "기저귀를 구매한 고객",
        normalization_rules=None,
        business_policies=None,
    )

    assert plan["target_user"]["purchase_object"] is None


def test_generic_product_noun_is_cleared_to_null(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    target_user = {
        "purchase_object": "상품",
        "purchase_object_kind": "product",
        "purchase_objects": [{"value": "상품", "kind": "product"}],
    }

    graph_rag._validate_purchase_objects("상품을 구매한 고객", target_user)

    assert target_user["purchase_object"] is None
    assert "purchase_object_kind" not in target_user
    assert "purchase_objects" not in target_user


def test_query_plan_hallucinated_product_is_cleared_to_null(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {"purchase_object": "냉장고"},
    }

    coerced = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        {"intent": "find_user_segment"},
        source_query="상품을 구매한 고객",
    )

    assert coerced["target_user"]["purchase_object"] is None


def test_query_plan_unrelated_noun_present_in_source_is_cleared(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {"purchase_object": "회원"},
    }

    coerced = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        {"intent": "find_user_segment"},
        source_query="상품을 구매한 회원",
    )

    assert coerced["target_user"]["purchase_object"] is None


def test_query_plan_product_present_in_source_is_kept(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {"purchase_object": "기저귀"},
    }

    coerced = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        {"intent": "find_user_segment"},
        source_query="기저귀를 구매한 고객",
    )

    assert coerced["target_user"]["purchase_object"] == "기저귀"


def test_query_plan_product_without_source_context_is_cleared(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {"purchase_object": "기저귀"},
    }

    coerced = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        {"intent": "find_user_segment"},
    )

    assert coerced["target_user"]["purchase_object"] is None


def test_product_without_purchase_history_signal_is_cleared(monkeypatch) -> None:
    _without_brand_lookup(monkeypatch)
    target_user = {"purchase_object": "기저귀"}

    graph_rag._validate_purchase_objects("기저귀 상품을 추천해줘", target_user)

    assert target_user["purchase_object"] is None
