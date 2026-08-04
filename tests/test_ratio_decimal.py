from __future__ import annotations

import json
from decimal import Decimal

import graph_rag
import targeting_ir
from semantic_normalizers import Ratio, RatioNormalizer


def test_ratio_normalizer_preserves_fractional_percent_round_trip() -> None:
    exact = "12.345678901234567890123"

    ratio = RatioNormalizer.normalize(f"{exact}%")

    assert ratio == Ratio(Decimal(exact), "percent")
    assert ratio.to_dict() == {"value": exact, "unit": "percent"}
    wire = json.loads(json.dumps(ratio.to_dict()))
    assert RatioNormalizer.normalize(wire) == ratio


def test_cell_rate_fraction_reaches_sql_without_float_rounding() -> None:
    exact = "12.345678901234567890123"
    shape = targeting_ir.SLOT_SHAPES["cell_rate_target"]

    coerced = shape.coerce(
        {
            "success_rate": {"operator": ">=", "value": exact},
            "buy_rate": None,
        }
    )

    assert coerced is not None
    assert coerced["success_rate"]["value"] == exact
    candidate = graph_rag.build_cell_rate_targets_sql_candidate(
        {"target_user": {"cell_rate_target": coerced}}
    )
    assert candidate is not None
    assert exact in candidate["sql"]
    assert "12.345678901234568" not in candidate["sql"]

