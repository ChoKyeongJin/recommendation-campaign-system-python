from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from training.ctr_features import (
    CATEGORICAL_FEATURES,
    MODEL_FEATURES,
    TARGET,
    apply_category_metadata,
    category_metadata,
    prepare_features,
)


DEFAULT_SQL_PATH = Path("training/ctr_training_dataset.sql")
DEFAULT_ARTIFACT_DIR = Path("artifacts")
DEFAULT_MODEL_PATH = DEFAULT_ARTIFACT_DIR / "ctr_model.joblib"
DEFAULT_METADATA_PATH = DEFAULT_ARTIFACT_DIR / "ctr_metadata.json"


def postgres_conninfo() -> str:
    return " ".join(
        [
            f"host={os.getenv('POSTGRES_HOST', 'postgres')}",
            f"port={os.getenv('POSTGRES_PORT', '5432')}",
            f"dbname={os.getenv('POSTGRES_DB', 'campaign_db')}",
            f"user={os.getenv('POSTGRES_USER', 'postgres')}",
            f"password={os.getenv('POSTGRES_PASSWORD', '1234')}",
        ]
    )


def load_training_data(sql_path: Path, include_unobserved: bool) -> pd.DataFrame:
    query = sql_path.read_text(encoding="utf-8")
    if include_unobserved:
        query = query.replace(
            "      AND d.sent_at < NOW() - INTERVAL '24 hours'\n",
            "",
        )
    with psycopg.connect(postgres_conninfo()) as connection:
        return pd.read_sql_query(query, connection)


def filter_data_range(df: pd.DataFrame, start_at: str | None, end_at: str | None) -> pd.DataFrame:
    data = df.copy()
    data["sent_at"] = pd.to_datetime(data["sent_at"], utc=True)
    if start_at:
        data = data[data["sent_at"] >= pd.Timestamp(start_at, tz="UTC")]
    if end_at:
        data = data[data["sent_at"] < pd.Timestamp(end_at, tz="UTC")]
    return data.sort_values("sent_at").reset_index(drop=True)


def temporal_split(
    df: pd.DataFrame,
    validation_start: str | None,
    test_start: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    data = df.sort_values("sent_at").reset_index(drop=True)
    if len(data) < 10:
        raise ValueError("At least 10 training rows are required for temporal train/validation/test split.")

    if validation_start and test_start:
        validation_start_ts = pd.Timestamp(validation_start, tz="UTC")
        test_start_ts = pd.Timestamp(test_start, tz="UTC")
        train_df = data[data["sent_at"] < validation_start_ts].copy()
        valid_df = data[(data["sent_at"] >= validation_start_ts) & (data["sent_at"] < test_start_ts)].copy()
        test_df = data[data["sent_at"] >= test_start_ts].copy()
        split_metadata = {
            "validation_start": validation_start,
            "test_start": test_start,
            "split_mode": "explicit_time",
        }
    else:
        train_end = max(1, int(len(data) * 0.70))
        valid_end = max(train_end + 1, int(len(data) * 0.85))
        valid_end = min(valid_end, len(data) - 1)
        train_df = data.iloc[:train_end].copy()
        valid_df = data.iloc[train_end:valid_end].copy()
        test_df = data.iloc[valid_end:].copy()
        split_metadata = {
            "validation_start": valid_df["sent_at"].min().isoformat() if not valid_df.empty else None,
            "test_start": test_df["sent_at"].min().isoformat() if not test_df.empty else None,
            "split_mode": "row_order_time",
        }

    if train_df.empty or valid_df.empty or test_df.empty:
        raise ValueError("Train, validation, and test splits must all contain rows.")
    return train_df, valid_df, test_df, split_metadata


def scale_pos_weight(y: pd.Series) -> float:
    positives = int(y.sum())
    negatives = int(len(y) - positives)
    if positives == 0 or negatives == 0:
        raise ValueError("Training data must contain both clicked=1 and clicked=0 rows.")
    return negatives / positives


def evaluate(y_true: pd.Series, probabilities: np.ndarray) -> dict[str, float | None]:
    clipped = np.clip(probabilities, 1e-7, 1 - 1e-7)
    metrics: dict[str, float | None] = {
        "actual_ctr": float(y_true.mean()),
        "predicted_ctr_mean": float(clipped.mean()),
        "brier_score": float(brier_score_loss(y_true, clipped)),
        "lift_at_10_pct": lift_at_k(y_true, clipped, 0.10),
    }
    if y_true.nunique() > 1:
        metrics.update(
            {
                "roc_auc": float(roc_auc_score(y_true, clipped)),
                "pr_auc": float(average_precision_score(y_true, clipped)),
                "log_loss": float(log_loss(y_true, clipped)),
            }
        )
    else:
        metrics.update({"roc_auc": None, "pr_auc": None, "log_loss": None})
    return metrics


def lift_at_k(y_true: pd.Series, probabilities: np.ndarray, fraction: float) -> float | None:
    if y_true.empty or float(y_true.mean()) == 0:
        return None
    frame = pd.DataFrame({"clicked": y_true.astype(int), "probability": probabilities})
    top_n = max(1, int(len(frame) * fraction))
    top_ctr = frame.sort_values("probability", ascending=False).head(top_n)["clicked"].mean()
    return float(top_ctr / y_true.mean())


def main(args: argparse.Namespace) -> None:
    df = load_training_data(args.sql_path, args.include_unobserved)
    df = filter_data_range(df, args.start_at, args.end_at)
    if df.empty:
        raise ValueError("No training rows were returned. Check delivery/event data and the observation window.")
    if TARGET not in df:
        raise ValueError(f"Training data is missing target column: {TARGET}")

    train_df, valid_df, test_df, split_metadata = temporal_split(df, args.validation_start, args.test_start)
    x_train = prepare_features(train_df)
    y_train = train_df[TARGET].astype(int)
    x_valid = apply_category_metadata(prepare_features(valid_df), category_metadata(x_train))
    y_valid = valid_df[TARGET].astype(int)
    x_test = apply_category_metadata(prepare_features(test_df), category_metadata(x_train))
    y_test = test_df[TARGET].astype(int)

    weight = scale_pos_weight(y_train)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        min_child_samples=args.min_child_samples,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=1.0,
        scale_pos_weight=weight,
        random_state=args.random_state,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(
        x_train,
        y_train,
        eval_set=[(x_valid, y_valid)],
        eval_metric=["binary_logloss", "auc"],
        categorical_feature=CATEGORICAL_FEATURES,
        callbacks=[lgb.early_stopping(stopping_rounds=args.early_stopping_rounds), lgb.log_evaluation(period=50)],
    )

    best_iteration = model.best_iteration_ or args.n_estimators
    valid_probabilities = model.predict_proba(x_valid, num_iteration=best_iteration)[:, 1]
    test_probabilities = model.predict_proba(x_test, num_iteration=best_iteration)[:, 1]

    model_version = args.model_version or datetime.now(ZoneInfo("Asia/Seoul")).strftime("ctr-lgbm-%Y%m%d-%H%M%S")
    metadata = {
        "model_version": model_version,
        "created_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "features": MODEL_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "categories": category_metadata(x_train),
        "best_iteration": int(best_iteration),
        "scale_pos_weight": float(weight),
        "train_rows": int(len(train_df)),
        "validation_rows": int(len(valid_df)),
        "test_rows": int(len(test_df)),
        "train_ctr": float(y_train.mean()),
        "validation_metrics": evaluate(y_valid, valid_probabilities),
        "test_metrics": evaluate(y_test, test_probabilities),
        "data_range": {
            "start_at": args.start_at,
            "end_at": args.end_at,
            **split_metadata,
        },
    }
    args.model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.model_path)
    args.metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train the campaign CTR LightGBM model.")
    parser.add_argument("--sql-path", type=Path, default=DEFAULT_SQL_PATH)
    parser.add_argument("--model-path", type=Path, default=Path(os.getenv("CTR_MODEL_PATH", DEFAULT_MODEL_PATH)))
    parser.add_argument("--metadata-path", type=Path, default=Path(os.getenv("CTR_METADATA_PATH", DEFAULT_METADATA_PATH)))
    parser.add_argument("--model-version")
    parser.add_argument("--start-at")
    parser.add_argument("--end-at")
    parser.add_argument("--validation-start")
    parser.add_argument("--test-start")
    parser.add_argument("--n-estimators", type=int, default=1000)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--num-leaves", type=int, default=31)
    parser.add_argument("--min-child-samples", type=int, default=50)
    parser.add_argument("--early-stopping-rounds", type=int, default=100)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--include-unobserved",
        action="store_true",
        help="Development-only: include recent deliveries whose click observation window has not ended.",
    )
    main(parser.parse_args())
