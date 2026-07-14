from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd


TARGET = "clicked"

CATEGORICAL_FEATURES = [
    "variant_code",
    "channel",
    "objective",
    "category",
    "age_group",
    "gender",
    "region",
    "lifecycle",
    "price_sensitivity",
    "predicted_ltv_segment",
    "message_length_group",
]

NUMERIC_FEATURES = [
    "send_hour",
    "send_day_of_week",
    "is_weekend",
    "is_control",
    "allocation_weight",
    "budget_krw",
    "expected_ctr",
    "expected_cvr",
    "avg_order_value_krw",
    "purchase_count_90d",
    "last_active_days",
    "is_preferred_channel",
    "is_category_interest",
    "has_recommendation_edge",
]

MODEL_FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES


def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for column in MODEL_FEATURES:
        if column not in result:
            result[column] = None

    for column in CATEGORICAL_FEATURES:
        result[column] = result[column].fillna("__MISSING__").astype("string").astype("category")

    for column in NUMERIC_FEATURES:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0)

    return result[MODEL_FEATURES]


def apply_category_metadata(features: pd.DataFrame, categories: dict[str, list[str]]) -> pd.DataFrame:
    result = features.copy()
    for column in CATEGORICAL_FEATURES:
        result[column] = result[column].cat.set_categories(categories.get(column, []))
    return result[MODEL_FEATURES]


def category_metadata(features: pd.DataFrame) -> dict[str, list[str]]:
    return {
        column: [str(value) for value in features[column].cat.categories]
        for column in CATEGORICAL_FEATURES
    }


def build_prediction_rows(
    user: dict[str, Any],
    variants: list[dict[str, Any]],
    experiment: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    interests = set(user.get("interests") or [])
    preferred_channels = set(user.get("preferred_channels") or [])
    recommendation_campaign_ids = set(user.get("recommendation_campaign_ids") or [])
    channel = str(experiment.get("channel") or "")
    category = str(experiment.get("category") or "")
    campaign_id = str(experiment.get("campaign_id") or "")
    for variant in variants:
        ai_features = variant.get("ai_features") if isinstance(variant.get("ai_features"), dict) else {}
        rows.append(
            {
                "variant_code": variant.get("variant_code"),
                "channel": channel,
                "objective": experiment.get("objective"),
                "category": category,
                "age_group": _age_group(user.get("age")),
                "gender": user.get("gender"),
                "region": user.get("region"),
                "lifecycle": user.get("lifecycle"),
                "price_sensitivity": user.get("price_sensitivity"),
                "predicted_ltv_segment": user.get("predicted_ltv_segment"),
                "message_length_group": ai_features.get("message_length_group") or _message_length_group(variant.get("message_body") or ""),
                "send_hour": pd.Timestamp.now(tz="Asia/Seoul").hour,
                "send_day_of_week": int(pd.Timestamp.now(tz="Asia/Seoul").dayofweek + 1) % 7,
                "is_weekend": 1 if pd.Timestamp.now(tz="Asia/Seoul").dayofweek >= 5 else 0,
                "is_control": _to_int(variant.get("is_control")),
                "allocation_weight": _to_float(variant.get("allocation_weight")),
                "budget_krw": _to_float(experiment.get("budget_krw")),
                "expected_ctr": _to_float(experiment.get("expected_ctr")),
                "expected_cvr": _to_float(experiment.get("expected_cvr")),
                "avg_order_value_krw": _to_float(user.get("avg_order_value_krw")),
                "purchase_count_90d": _to_float(user.get("purchase_count_90d")),
                "last_active_days": _to_float(user.get("last_active_days")),
                "is_preferred_channel": 1 if channel in preferred_channels else 0,
                "is_category_interest": 1 if category in interests else 0,
                "has_recommendation_edge": 1 if campaign_id in recommendation_campaign_ids else 0,
            }
        )
    return rows


def _age_group(age: Any) -> str | None:
    try:
        value = int(age)
    except (TypeError, ValueError):
        return None
    if value < 20:
        return "under_20"
    if value < 30:
        return "20s"
    if value < 40:
        return "30s"
    if value < 50:
        return "40s"
    if value < 60:
        return "50s"
    return "60_plus"


def _message_length_group(text: str) -> str:
    length = len(text)
    if length < 45:
        return "short"
    if length <= 90:
        return "medium"
    return "long"


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return 1 if bool(value) else 0
