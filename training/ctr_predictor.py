from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from training.ctr_features import (
    CATEGORICAL_FEATURES,
    MODEL_FEATURES,
    apply_category_metadata,
    build_prediction_rows,
    prepare_features,
)


DEFAULT_MODEL_PATH = Path("artifacts/ctr_model.joblib")
DEFAULT_METADATA_PATH = Path("artifacts/ctr_metadata.json")


class CtrPredictor:
    def __init__(self, model_path: Path | None = None, metadata_path: Path | None = None) -> None:
        self.model_path = model_path or Path(os.getenv("CTR_MODEL_PATH", DEFAULT_MODEL_PATH))
        self.metadata_path = metadata_path or Path(os.getenv("CTR_METADATA_PATH", DEFAULT_METADATA_PATH))
        if not self.model_path.exists():
            raise FileNotFoundError(f"CTR model file not found: {self.model_path}")
        if not self.metadata_path.exists():
            raise FileNotFoundError(f"CTR metadata file not found: {self.metadata_path}")
        self.model = joblib.load(self.model_path)
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.model_version = str(self.metadata.get("model_version") or self.model_path.stem)
        self.categories: dict[str, list[str]] = self.metadata.get("categories", {})
        missing_categories = [column for column in CATEGORICAL_FEATURES if column not in self.categories]
        if missing_categories:
            raise ValueError(f"CTR metadata missing categories: {missing_categories}")

    def predict_rows(self, rows: list[dict[str, Any]]) -> list[float]:
        frame = pd.DataFrame(rows)
        features = prepare_features(frame)
        features = apply_category_metadata(features, self.categories)
        probabilities = self.model.predict_proba(
            features[MODEL_FEATURES],
            num_iteration=self.metadata.get("best_iteration"),
        )[:, 1]
        return [round(float(value), 7) for value in probabilities]

    def predict_variant_scores(
        self,
        user: dict[str, Any],
        variants: list[dict[str, Any]],
        experiment: dict[str, Any],
    ) -> dict[str, float]:
        rows = build_prediction_rows(user, variants, experiment)
        probabilities = self.predict_rows(rows)
        return {
            str(variant["variant_code"]): probability
            for variant, probability in zip(variants, probabilities, strict=True)
        }


_CACHED_PREDICTOR: CtrPredictor | None = None
_CACHED_SIGNATURE: tuple[str, str, float, float] | None = None


def get_ctr_predictor() -> CtrPredictor:
    global _CACHED_PREDICTOR, _CACHED_SIGNATURE
    model_path = Path(os.getenv("CTR_MODEL_PATH", DEFAULT_MODEL_PATH))
    metadata_path = Path(os.getenv("CTR_METADATA_PATH", DEFAULT_METADATA_PATH))
    signature = (
        str(model_path),
        str(metadata_path),
        model_path.stat().st_mtime,
        metadata_path.stat().st_mtime,
    )
    if _CACHED_PREDICTOR is None or _CACHED_SIGNATURE != signature:
        _CACHED_PREDICTOR = CtrPredictor(model_path, metadata_path)
        _CACHED_SIGNATURE = signature
    return _CACHED_PREDICTOR


def predict_variant_scores(
    user: dict[str, Any],
    variants: list[dict[str, Any]],
    experiment: dict[str, Any],
    requested_model_version: str,
) -> dict[str, float]:
    predictor = get_ctr_predictor()
    if requested_model_version not in {"latest", predictor.model_version}:
        raise ValueError(
            f"requested CTR model version {requested_model_version!r} does not match loaded model {predictor.model_version!r}"
        )
    return predictor.predict_variant_scores(user, variants, experiment)
