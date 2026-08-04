"""Precision regressions for active non-Event-IR semantic value paths."""

from __future__ import annotations

import json
from decimal import Decimal

import conceptual_targeting
import condition_normalizers
import targeting_ir
from semantic_normalizers import (
    COUNT_CONTRADICTION,
    COUNT_EXISTENCE,
    AmountNormalizer,
    CountThresholdNormalizer,
    Quantity,
)


def test_targeting_aggregate_threshold_preserves_integer_beyond_float_precision() -> None:
    exact = "9007199254740993"

    coerced = targeting_ir.SLOT_SHAPES["aggregate_conditions"].coerce(
        [{"metric_id": "purchase_amount", "operator": ">=", "threshold": exact}],
        allowed={"purchase_amount"},
    )

    assert coerced is not None
    assert coerced[0]["threshold"] == 9_007_199_254_740_993
    assert type(coerced[0]["threshold"]) is int
    assert targeting_ir.SLOT_SHAPES["aggregate_conditions"].coerce(
        coerced, allowed={"purchase_amount"}
    ) == coerced


def test_targeting_profile_and_ranking_fractional_values_use_exact_wire_text() -> None:
    exact = "12.123456789012345678901"
    profile_allowed = {
        "balance": {"column": "BALANCE", "label": "balance", "operators": [">="]}
    }

    profile = targeting_ir.SLOT_SHAPES["balance_conditions"].coerce(
        [{"metric_id": "balance", "operator": ">=", "threshold": exact}],
        allowed=profile_allowed,
    )
    ranking = targeting_ir.SLOT_SHAPES["member_metric_ranking"].coerce(
        {
            "metric_id": "total_buy_amt",
            "direction": "high",
            "limit_type": "percent",
            "percent": exact,
        },
        allowed={"total_buy_amt"},
    )

    assert profile is not None and profile[0]["threshold"] == exact
    assert ranking is not None and ranking["percent"] == exact
    assert json.loads(json.dumps({"profile": profile, "ranking": ranking})) == {
        "profile": profile,
        "ranking": ranking,
    }


def test_condition_comparison_uses_decimal_before_canonical_projection() -> None:
    integer = condition_normalizers.normalize_comparison(
        {"operator": ">=", "value": "9007199254740993"}
    )
    fractional = condition_normalizers.normalize_comparison(
        {"operator": ">=", "value": "9007199254740993.0000000000000001"}
    )

    assert integer.ok and integer.value["value"] == 9_007_199_254_740_993
    assert type(integer.value["value"]) is int
    assert fractional.ok
    assert fractional.value["value"] == Decimal("9007199254740993.0000000000000001")


def test_quantity_and_count_semantics_do_not_round_through_float() -> None:
    exact = "9007199254740993.0000000000000001"
    quantity = AmountNormalizer.normalize({"value": exact, "unit": "item_quantity"})

    assert isinstance(quantity, Quantity)
    assert quantity.value == Decimal(exact)
    assert quantity.to_dict()["value"] == exact
    assert AmountNormalizer.normalize(quantity.to_dict()) == quantity

    assert CountThresholdNormalizer.classify("=", exact) == COUNT_CONTRADICTION
    tiny_positive = "0." + ("0" * 400) + "1"
    assert CountThresholdNormalizer.classify(">=", tiny_positive) == COUNT_EXISTENCE


def test_conceptual_numeric_json_preserves_exact_threshold_through_materialization() -> None:
    exact = "9007199254740993.1250000000000000001"
    capability = conceptual_targeting.Capability(
        capability_id="cap_score",
        kind="numeric",
        logical_name="score",
        description="score",
        table="MEMBER_PROFILE",
        column="SCORE",
        join_column=None,
        materializer="numeric_condition",
        aliases=("score",),
        inference_mode="explicit_only",
        number_type="number",
    )
    catalog = conceptual_targeting.CapabilityCatalog(
        capabilities=(capability,), digest="decimal-test"
    )
    service = conceptual_targeting.ConceptualTargetingService(
        catalog=catalog,
        complete=lambda _messages, _schema: {},
        model="test-model",
    )
    raw = (
        '{"interpretations":[{"evidence":"score",'
        '"capability_id":"cap_score","operator":">",'
        '"value_ids":[],"lower_bound":null,"upper_bound":null,'
        f'"threshold":{exact},"confidence":0.9,'
        '"rationale":"score threshold"}],'
        '"unsupported":[],"ignored":[],"coverage_complete":true}'
    )

    accepted, unsupported, rejected, ignored = service._validate(raw, "score", {})

    assert not unsupported and not rejected and not ignored
    assert accepted[0]["threshold"] == exact
    assert json.loads(json.dumps(accepted))[0]["threshold"] == exact
    dimension_filter, native = conceptual_targeting.materialize_resolution(
        accepted[0], catalog
    )
    assert dimension_filter is None
    assert native is not None
    assert native["values"][0]["threshold"] == exact

