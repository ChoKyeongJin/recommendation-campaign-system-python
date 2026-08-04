from __future__ import annotations

import ast
from pathlib import Path

import graph_rag
import targeting_domain
from rag.message import MESSAGE_CHANNEL_TERMS


REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_BEHAVIORS = frozenset({
    "no_purchase",
    "first_purchase",
    "cart_abandoner",
    "repeat_buyer",
    "review_likely",
    "office_worker",
    "student",
    "gift_buyer",
})
EXPECTED_CATEGORIES = frozenset({
    "fashion",
    "beauty",
    "electronics",
    "food",
    "home_living",
    "travel",
    "sports",
    "outdoor",
    "eco",
    "health_food",
    "digital_content",
    "global_shopping",
})
EXPECTED_INTERESTS = EXPECTED_CATEGORIES | {"parent", "pet_owner"}
EXPECTED_CHANNELS = frozenset({
    "app_push",
    "kakao",
    "email",
    "sms",
    "instagram",
    "lms",
    "rcs",
})
EXPECTED_OFFERS = frozenset({"coupon", "free_shipping", "subscription"})
EXPECTED_OBJECTIVES = frozenset({
    "purchase",
    "repurchase",
    "retention",
    "reactivation",
    "subscription",
    "awareness",
})


def test_closed_targeting_facets_preserve_the_current_exact_contract() -> None:
    """카탈로그 이동은 기존 wire canonical 집합을 늘리거나 줄이지 않는다."""

    assert graph_rag.BEHAVIOR_TERMS == EXPECTED_BEHAVIORS
    assert graph_rag.CATEGORY_TERMS == EXPECTED_CATEGORIES
    assert graph_rag.INTEREST_TERMS == EXPECTED_INTERESTS
    assert graph_rag.CHANNEL_TERMS == EXPECTED_CHANNELS
    assert graph_rag.OFFER_TERMS == EXPECTED_OFFERS
    assert graph_rag.CAMPAIGN_OBJECTIVES == EXPECTED_OBJECTIVES


def test_graph_rag_closed_targeting_terms_come_from_domain_facets() -> None:
    assert graph_rag.BEHAVIOR_TERMS == set(targeting_domain.vocabulary("behavior"))
    assert graph_rag.CATEGORY_TERMS == set(targeting_domain.vocabulary("category"))
    assert graph_rag.INTEREST_TERMS == (
        graph_rag.CATEGORY_TERMS
        | set(targeting_domain.vocabulary("interest_extension"))
    )
    assert graph_rag.CHANNEL_TERMS == (
        set(targeting_domain.vocabulary("channel")) | MESSAGE_CHANNEL_TERMS
    )
    assert graph_rag.OFFER_TERMS == set(targeting_domain.vocabulary("offer"))
    assert graph_rag.CAMPAIGN_OBJECTIVES == set(
        targeting_domain.vocabulary("campaign_objective")
    )


def test_runtime_facets_are_non_empty_and_disjoint_where_required() -> None:
    assert graph_rag.BEHAVIOR_TERMS
    assert graph_rag.CATEGORY_TERMS
    assert graph_rag.CHANNEL_TERMS
    assert graph_rag.OFFER_TERMS
    assert graph_rag.CAMPAIGN_OBJECTIVES
    assert not (
        set(targeting_domain.vocabulary("interest_extension"))
        & graph_rag.CATEGORY_TERMS
    )


def test_interest_and_channel_facets_preserve_their_declared_composition() -> None:
    interest_extension = set(targeting_domain.vocabulary("interest_extension"))
    targeting_channels = set(targeting_domain.vocabulary("channel"))

    assert interest_extension == {"parent", "pet_owner"}
    assert graph_rag.INTEREST_TERMS == graph_rag.CATEGORY_TERMS | interest_extension
    assert targeting_channels.isdisjoint(MESSAGE_CHANNEL_TERMS)
    assert graph_rag.CHANNEL_TERMS == targeting_channels | MESSAGE_CHANNEL_TERMS
    assert MESSAGE_CHANNEL_TERMS <= graph_rag.CHANNEL_TERMS


def test_graph_rag_does_not_redeclare_closed_facets_as_literal_collections() -> None:
    """facet 값은 graph_rag 수기 컨테이너가 아니라 targeting_domain 호출에서만 온다."""

    guarded = {
        "BEHAVIOR_TERMS",
        "CATEGORY_TERMS",
        "INTEREST_TERMS",
        "CHANNEL_TERMS",
        "OFFER_TERMS",
        "CAMPAIGN_OBJECTIVES",
    }
    tree = ast.parse((REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8"))
    assignments: dict[str, ast.expr] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in guarded and node.value is not None:
                assignments[target.id] = node.value

    assert set(assignments) == guarded
    for name, value in assignments.items():
        literal_collections = [
            node
            for node in ast.walk(value)
            if isinstance(node, (ast.Set, ast.List, ast.Tuple, ast.Dict))
        ]
        assert not literal_collections, (
            f"{name} 이 graph_rag 안에서 수기 컨테이너로 재선언됐다 — "
            "targeting_domain vocabulary에서 파생하라."
        )

    vocabulary_keys = {
        constant.value
        for value in assignments.values()
        for constant in ast.walk(value)
        if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
    }
    assert vocabulary_keys == {
        "behavior",
        "category",
        "interest_extension",
        "channel",
        "offer",
        "campaign_objective",
    }
