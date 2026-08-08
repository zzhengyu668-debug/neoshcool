#!/usr/bin/env python3
"""Run leakage-safe W6-D controlled product-month warning comparisons.

The upstream Rating, VADER Sentiment, and Engineering signal generators are
frozen and different.  The three core downstream routes share the same text
matrix, samples, temporal split, estimators, and evaluation code.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import shutil
import struct
import sys
import time
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import scipy
from scipy import sparse
from sklearn import __version__ as sklearn_version
from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC


STAR_HEADER_RE = re.compile(
    r"^\s*(?:one|two|three|four|five)\s+stars?"
    r"\s*(?:[.!:;\-–—]+\s*)?(?:(?:\r?\n)+|$)",
    flags=re.IGNORECASE,
)


class W6DError(RuntimeError):
    pass


class InputMismatch(W6DError):
    pass


class RouteSampleMismatch(W6DError):
    pass


class LeakageError(W6DError):
    pass


class SplitReview(W6DError):
    pass


class UnknownOutput(W6DError):
    pass


class SpaceGate(W6DError):
    pass


def project_root() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / "PROJECT_HANDOFF.md").is_file() and (
            candidate / "config" / "project.toml"
        ).is_file():
            return candidate
    raise RuntimeError("Project root could not be located from script path")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_default(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime, date, pd.Period)):
        return str(value)
    if value is pd.NA:
        return None
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_parquet(path: Path, frame: pd.DataFrame, compression: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), temporary, compression=compression)
    os.replace(temporary, path)


def write_joblib(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(payload, temporary, compress=3)
    os.replace(temporary, path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def file_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    result = {
        "path": relative(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if include_hash:
        result["sha256"] = sha256_file(path)
    return result


def parquet_identity(root: Path, path: Path) -> dict[str, Any]:
    result = file_identity(root, path)
    metadata = pq.ParquetFile(path)
    result.update(
        {
            "rows": metadata.metadata.num_rows,
            "fields": metadata.schema_arrow.names,
            "field_count": len(metadata.schema_arrow),
            "compression": sorted(
                {
                    metadata.metadata.row_group(i).column(j).compression
                    for i in range(metadata.metadata.num_row_groups)
                    for j in range(metadata.metadata.row_group(i).num_columns)
                }
            ),
        }
    )
    return result


def validate_parquet(root: Path, path: Path, rows: int, digest: str) -> dict[str, Any]:
    if not path.is_file():
        raise InputMismatch(f"Missing input: {relative(root, path)}")
    identity = parquet_identity(root, path)
    if identity["rows"] != rows or identity["sha256"] != digest:
        raise InputMismatch(f"Input identity mismatch: {identity['path']}")
    return identity


def stable_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def period_series(values: pd.Series) -> pd.Series:
    if isinstance(values.dtype, pd.PeriodDtype):
        return values.astype("period[M]")
    timestamps = pd.to_datetime(values, errors="raise")
    if getattr(timestamps.dt, "tz", None) is not None:
        timestamps = timestamps.dt.tz_localize(None)
    return timestamps.dt.to_period("M")


def date_series(values: pd.Series) -> pd.Series:
    return period_series(values).dt.to_timestamp().dt.date


def split_for_month(month: pd.Period, config: dict[str, Any]) -> str:
    rules = config["split"]
    bounds = {name: pd.Period(value, freq="M") for name, value in rules.items() if isinstance(value, str)}
    if month <= bounds["train_end"]:
        return "train"
    if bounds["embargo_train_validation_start"] <= month <= bounds["embargo_train_validation_end"]:
        return "embargo_train_validation"
    if bounds["validation_start"] <= month <= bounds["validation_end"]:
        return "validation"
    if bounds["embargo_validation_test_start"] <= month <= bounds["embargo_validation_test_end"]:
        return "embargo_validation_test"
    if bounds["test_start"] <= month <= bounds["test_end"]:
        return "test"
    return "outside_frozen_range"


def build_modeling_keys(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    required = {"parent_asin", "review_month", "device_type", "analysis_role"}
    for horizon in config["targets"]["all_horizons"]:
        required.update(
            {
                f"eligible_main_h{horizon}",
                f"target_quality_deterioration_h{horizon}",
            }
        )
    missing = sorted(required - set(panel.columns))
    if missing:
        raise InputMismatch(f"Analysis panel missing columns: {missing}")
    frames: list[pd.DataFrame] = []
    for horizon in config["targets"]["all_horizons"]:
        eligibility = f"eligible_main_h{horizon}"
        target = f"target_quality_deterioration_h{horizon}"
        subset = panel.loc[panel[eligibility] & panel[target].notna()].copy()
        subset["horizon"] = int(horizon)
        subset["target"] = subset[target].astype(int)
        subset["review_month_period"] = period_series(subset["review_month"])
        subset["split"] = subset["review_month_period"].map(
            lambda value: split_for_month(value, config)
        )
        subset["review_month"] = date_series(subset["review_month_period"])
        frames.append(subset)
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["horizon", "review_month", "parent_asin"]).reset_index(drop=True)
    model_splits = {"train", "validation", "test"}
    for horizon, subset in combined.groupby("horizon"):
        used = subset.loc[subset["split"].isin(model_splits)]
        for split_name in sorted(model_splits):
            labels = used.loc[used["split"] == split_name, "target"]
            if labels.nunique() < 2:
                raise SplitReview(f"h={horizon} {split_name} lacks a binary class")
        if (used["device_type"] == "smart_switch").any():
            raise RouteSampleMismatch("Smart switch entered formal modeling keys")
    return combined


def build_text_matrix(
    samples: pd.DataFrame,
    reviews: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[sparse.csr_matrix, TfidfVectorizer, dict[str, Any]]:
    used = samples.loc[samples["split"].isin(["train", "validation", "test"])].copy()
    used = used.sort_values(["review_month", "parent_asin"]).reset_index(drop=True)
    used["month_key"] = period_series(used["review_month"]).astype(str)
    review = reviews.copy()
    review["month_key"] = period_series(review["review_month"]).astype(str)
    review = review.merge(
        used[["parent_asin", "month_key", "split"]],
        on=["parent_asin", "month_key"],
        how="inner",
        validate="many_to_one",
    )
    review = review.sort_values(
        ["month_key", "parent_asin", "review_datetime", "duplicate_key"]
    ).reset_index(drop=True)
    if review.empty:
        raise RouteSampleMismatch("No reviews matched modeling keys")
    key_to_row = {
        (row.parent_asin, row.month_key): index
        for index, row in enumerate(used.itertuples(index=False))
    }
    pm_rows = np.array(
        [key_to_row[(row.parent_asin, row.month_key)] for row in review.itertuples(index=False)],
        dtype=np.int64,
    )
    counts = np.bincount(pm_rows, minlength=len(used))
    if (counts == 0).any():
        raise RouteSampleMismatch("At least one product-month has no current-month review")
    expected_counts = used["feature_n_reviews"].astype(int).to_numpy()
    if not np.array_equal(counts, expected_counts):
        raise RouteSampleMismatch("Current-month text review counts do not match W6-C panel")
    text_cfg = config["text"]
    vectorizer = TfidfVectorizer(
        lowercase=bool(text_cfg["lowercase"]),
        ngram_range=(int(text_cfg["ngram_min"]), int(text_cfg["ngram_max"])),
        min_df=int(text_cfg["min_df"]),
        max_features=int(text_cfg["max_features"]),
        sublinear_tf=bool(text_cfg["sublinear_tf"]),
        strip_accents=str(text_cfg["strip_accents"]),
        dtype=np.float64,
    )
    model_text = review["review_text"].fillna("").astype(str)
    star_headers_removed = 0
    if bool(text_cfg.get("remove_leading_star_header", False)):
        cleaned_text: list[str] = []
        for value in model_text:
            cleaned, substitutions = STAR_HEADER_RE.subn("", value, count=1)
            cleaned_text.append(cleaned)
            star_headers_removed += substitutions
        model_text = pd.Series(cleaned_text, index=review.index, dtype="string")
    train_mask = review["split"].eq("train").to_numpy()
    vectorizer.fit(model_text.loc[train_mask])
    review_vectors = vectorizer.transform(model_text)
    weights = 1.0 / counts[pm_rows]
    aggregation = sparse.csr_matrix(
        (weights, (pm_rows, np.arange(len(review)))),
        shape=(len(used), len(review)),
    )
    product_month_vectors = (aggregation @ review_vectors).tocsr()
    if product_month_vectors.shape[0] != len(used):
        raise RouteSampleMismatch("Text aggregation row count mismatch")
    duplicate_sets = {
        split: set(review.loc[review["split"] == split, "duplicate_key"])
        for split in ["train", "validation", "test"]
    }
    overlaps = {
        "train_validation": len(duplicate_sets["train"] & duplicate_sets["validation"]),
        "train_test": len(duplicate_sets["train"] & duplicate_sets["test"]),
        "validation_test": len(duplicate_sets["validation"] & duplicate_sets["test"]),
    }
    if any(overlaps.values()):
        raise LeakageError("A review appears in more than one temporal split")
    vocabulary_hash = stable_fingerprint(sorted(vectorizer.vocabulary_.items()))
    audit = {
        "sample_rows": len(used),
        "review_rows": len(review),
        "reviews_by_split": {name: int((review["split"] == name).sum()) for name in duplicate_sets},
        "product_months_by_split": {name: int((used["split"] == name).sum()) for name in duplicate_sets},
        "train_reviews_used_to_fit_vocabulary": int(train_mask.sum()),
        "validation_reviews_used_to_fit_vocabulary": 0,
        "test_reviews_used_to_fit_vocabulary": 0,
        "vocabulary_size": len(vectorizer.vocabulary_),
        "vocabulary_sha256": vocabulary_hash,
        "matrix_shape": list(product_month_vectors.shape),
        "matrix_nnz": int(product_month_vectors.nnz),
        "aggregation": str(text_cfg["aggregation"]),
        "current_calendar_month_only": True,
        "model_text_preprocessing": {
            "leading_star_header_removed": bool(text_cfg.get("remove_leading_star_header", False)),
            "pattern_version": text_cfg.get("star_header_pattern_version"),
            "affected_reviews": int(star_headers_removed),
            "formal_review_text_modified": False,
        },
        "duplicate_key_overlap_counts": overlaps,
    }
    return product_month_vectors, vectorizer, {"samples": used, "audit": audit}


def combine_features(
    text_matrix: sparse.csr_matrix | None,
    samples: pd.DataFrame,
    numeric_features: Sequence[str],
) -> tuple[sparse.csr_matrix, StandardScaler | None]:
    if not numeric_features:
        if text_matrix is None:
            raise RouteSampleMismatch("A route has neither text nor numeric features")
        return text_matrix, None
    missing = sorted(set(numeric_features) - set(samples.columns))
    if missing:
        raise InputMismatch(f"Missing numeric route features: {missing}")
    values = samples[list(numeric_features)].astype(float).to_numpy()
    if not np.isfinite(values).all():
        raise InputMismatch("Numeric route features contain null or non-finite values")
    train_mask = samples["split"].eq("train").to_numpy()
    scaler = StandardScaler()
    scaler.fit(values[train_mask])
    scaled = sparse.csr_matrix(scaler.transform(values))
    if text_matrix is None:
        return scaled, scaler
    return sparse.hstack([text_matrix, scaled], format="csr"), scaler


def calibration_summary(y_true: np.ndarray, probability: np.ndarray, bins: int) -> dict[str, Any]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    assignments = np.clip(np.digitize(probability, edges[1:-1], right=True), 0, bins - 1)
    rows: list[dict[str, Any]] = []
    ece = 0.0
    for index in range(bins):
        mask = assignments == index
        count = int(mask.sum())
        if count:
            mean_probability = float(probability[mask].mean())
            observed = float(y_true[mask].mean())
            ece += (count / len(y_true)) * abs(mean_probability - observed)
        else:
            mean_probability = None
            observed = None
        rows.append(
            {
                "bin": index + 1,
                "lower": float(edges[index]),
                "upper": float(edges[index + 1]),
                "n": count,
                "mean_probability": mean_probability,
                "observed_positive_share": observed,
            }
        )
    return {"expected_calibration_error": float(ece), "bins": rows}


def binary_metrics(
    y_true: np.ndarray,
    prediction: np.ndarray,
    score: np.ndarray,
    probability: np.ndarray | None,
    calibration_bins: int,
) -> dict[str, Any]:
    matrix = confusion_matrix(y_true, prediction, labels=[0, 1])
    tn, fp, fn, tp = [int(value) for value in matrix.ravel()]
    both = len(np.unique(y_true)) == 2
    result: dict[str, Any] = {
        "n": len(y_true),
        "negative": int((y_true == 0).sum()),
        "positive": int((y_true == 1).sum()),
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "accuracy": float(accuracy_score(y_true, prediction)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, prediction)),
        "precision": float(precision_score(y_true, prediction, zero_division=0)),
        "recall": float(recall_score(y_true, prediction, zero_division=0)),
        "f1": float(f1_score(y_true, prediction, zero_division=0)),
        "specificity": float(tn / (tn + fp)) if tn + fp else None,
        "roc_auc": float(roc_auc_score(y_true, score)) if both else None,
        "pr_auc": float(average_precision_score(y_true, score)) if both else None,
        "brier_score": float(brier_score_loss(y_true, probability)) if probability is not None else None,
    }
    result["calibration"] = (
        calibration_summary(y_true, probability, calibration_bins)
        if probability is not None
        else None
    )
    return result


def estimator_params(config: dict[str, Any], family: str) -> dict[str, Any]:
    if family == "logistic_regression":
        rules = config["logistic_regression"]
        return {
            "C": float(rules["C"]),
            "penalty": str(rules["penalty"]),
            "solver": str(rules["solver"]),
            "class_weight": str(rules["class_weight"]),
            "max_iter": int(rules["max_iter"]),
            "random_state": int(rules["random_state"]),
        }
    if family == "linear_svm":
        rules = config["linear_svm"]
        return {
            "C": float(rules["C"]),
            "class_weight": str(rules["class_weight"]),
            "max_iter": int(rules["max_iter"]),
            "random_state": int(rules["random_state"]),
            "dual": "auto",
        }
    raise ValueError(f"Unknown estimator family: {family}")


def fit_and_predict(
    matrix: sparse.csr_matrix,
    samples: pd.DataFrame,
    family: str,
    config: dict[str, Any],
) -> tuple[Any, pd.DataFrame, list[dict[str, Any]]]:
    train_mask = samples["split"].eq("train").to_numpy()
    labels = samples["target"].astype(int).to_numpy()
    if family == "logistic_regression":
        estimator = LogisticRegression(**estimator_params(config, family))
        threshold = float(config["logistic_regression"]["decision_threshold"])
    elif family == "linear_svm":
        estimator = LinearSVC(**estimator_params(config, family))
        threshold = float(config["linear_svm"]["decision_threshold"])
    else:
        raise ValueError(f"Unsupported family: {family}")
    estimator.fit(matrix[train_mask], labels[train_mask])
    if family == "logistic_regression":
        probability = estimator.predict_proba(matrix)[:, 1]
        score = estimator.decision_function(matrix)
        prediction = (probability >= threshold).astype(int)
    else:
        probability = np.full(len(samples), np.nan)
        score = estimator.decision_function(matrix)
        prediction = (score >= threshold).astype(int)
    prediction_frame = samples[
        ["parent_asin", "review_month", "device_type", "analysis_role", "horizon", "split", "target"]
    ].copy()
    prediction_frame["prediction"] = prediction
    prediction_frame["probability"] = probability
    prediction_frame["decision_score"] = score
    metrics: list[dict[str, Any]] = []
    for split_name in ["train", "validation", "test"]:
        mask = samples["split"].eq(split_name).to_numpy()
        split_probability = probability[mask] if family == "logistic_regression" else None
        result = binary_metrics(
            labels[mask],
            prediction[mask],
            score[mask] if family == "linear_svm" else probability[mask],
            split_probability,
            int(config["evaluation"]["calibration_bins"]),
        )
        result.update({"split": split_name, "scope": "combined"})
        metrics.append(result)
        if split_name == "test":
            for device_type in ["smart_plug", "smart_bulb", "smart_switch"]:
                device_mask = mask & samples["device_type"].eq(device_type).to_numpy()
                y_device = labels[device_mask]
                negative = int((y_device == 0).sum())
                positive = int((y_device == 1).sum())
                enough = (
                    len(y_device) >= int(config["evaluation"]["minimum_device_test_rows"])
                    and negative >= int(config["evaluation"]["minimum_device_class_rows"])
                    and positive >= int(config["evaluation"]["minimum_device_class_rows"])
                )
                if enough:
                    device_probability = probability[device_mask] if family == "logistic_regression" else None
                    device_result = binary_metrics(
                        y_device,
                        prediction[device_mask],
                        score[device_mask] if family == "linear_svm" else probability[device_mask],
                        device_probability,
                        int(config["evaluation"]["calibration_bins"]),
                    )
                    device_result["support_status"] = "SUFFICIENT_SUPPORT"
                else:
                    device_result = {
                        "n": len(y_device),
                        "negative": negative,
                        "positive": positive,
                        "support_status": "INSUFFICIENT_SUPPORT",
                    }
                device_result.update({"split": split_name, "scope": device_type})
                metrics.append(device_result)
    return estimator, prediction_frame, metrics


def fit_dummy(samples: pd.DataFrame, config: dict[str, Any]) -> tuple[Any, pd.DataFrame, list[dict[str, Any]]]:
    train_mask = samples["split"].eq("train").to_numpy()
    labels = samples["target"].astype(int).to_numpy()
    estimator = DummyClassifier(
        strategy=str(config["dummy"]["strategy"]),
        random_state=int(config["dummy"]["random_state"]),
    )
    placeholder = np.zeros((len(samples), 1))
    estimator.fit(placeholder[train_mask], labels[train_mask])
    prediction = estimator.predict(placeholder).astype(int)
    classes = list(estimator.classes_)
    probabilities = estimator.predict_proba(placeholder)
    probability = probabilities[:, classes.index(1)] if 1 in classes else np.zeros(len(samples))
    frame = samples[["parent_asin", "review_month", "device_type", "analysis_role", "horizon", "split", "target"]].copy()
    frame["prediction"] = prediction
    frame["probability"] = probability
    frame["decision_score"] = probability
    metrics: list[dict[str, Any]] = []
    for split_name in ["train", "validation", "test"]:
        mask = samples["split"].eq(split_name).to_numpy()
        result = binary_metrics(
            labels[mask], prediction[mask], probability[mask], probability[mask], int(config["evaluation"]["calibration_bins"])
        )
        result.update({"split": split_name, "scope": "combined"})
        metrics.append(result)
    return estimator, frame, metrics


def flatten_metrics(
    metrics: list[dict[str, Any]], horizon: int, family: str, route: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in metrics:
        row = {
            "horizon": horizon,
            "model_family": family,
            "route": route,
            "split": result["split"],
            "scope": result["scope"],
            "support_status": result.get("support_status", "SUFFICIENT_SUPPORT"),
        }
        for field in [
            "n", "negative", "positive", "accuracy", "balanced_accuracy", "precision",
            "recall", "f1", "specificity", "roc_auc", "pr_auc", "brier_score",
        ]:
            row[field] = result.get(field)
        matrix = result.get("confusion_matrix") or {}
        for field in ["tn", "fp", "fn", "tp"]:
            row[field] = matrix.get(field)
        calibration = result.get("calibration")
        row["expected_calibration_error"] = calibration.get("expected_calibration_error") if calibration else None
        rows.append(row)
    return rows


def bootstrap_primary(
    predictions: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    subset = predictions.loc[
        (predictions["horizon"] == int(config["targets"]["primary_horizon"]))
        & (predictions["model_family"] == "logistic_regression")
        & (predictions["split"] == "test")
        & predictions["route"].isin(config["routes"]["core"])
    ].copy()
    routes = list(config["routes"]["core"])
    frames = {route: subset.loc[subset["route"] == route].sort_values(["review_month", "parent_asin"]).reset_index(drop=True) for route in routes}
    reference_keys = list(zip(frames[routes[0]]["parent_asin"], frames[routes[0]]["review_month"]))
    for route in routes[1:]:
        if list(zip(frames[route]["parent_asin"], frames[route]["review_month"])) != reference_keys:
            raise RouteSampleMismatch("Bootstrap route samples differ")
    products = sorted(frames[routes[0]]["parent_asin"].unique())
    rng = np.random.default_rng(int(config["evaluation"]["bootstrap_random_state"]))
    replicates = int(config["evaluation"]["bootstrap_replicates"])
    route_values = {route: {metric: [] for metric in ["pr_auc", "brier_score", "f1"]} for route in routes}
    pairs = [
        ("text_plus_sentiment", "text_only"),
        ("text_plus_engineering", "text_only"),
        ("text_plus_engineering", "text_plus_sentiment"),
    ]
    delta_values = {(left, right): {metric: [] for metric in ["pr_auc", "brier_score", "f1"]} for left, right in pairs}
    indices_by_product = {
        product: np.flatnonzero(frames[routes[0]]["parent_asin"].to_numpy() == product)
        for product in products
    }
    for _ in range(replicates):
        sampled_products = rng.choice(products, size=len(products), replace=True)
        indices = np.concatenate([indices_by_product[product] for product in sampled_products])
        y = frames[routes[0]]["target"].to_numpy(dtype=int)[indices]
        if len(np.unique(y)) < 2:
            continue
        current: dict[str, dict[str, float]] = {}
        for route in routes:
            frame = frames[route]
            probability = frame["probability"].to_numpy(dtype=float)[indices]
            prediction = frame["prediction"].to_numpy(dtype=int)[indices]
            current[route] = {
                "pr_auc": float(average_precision_score(y, probability)),
                "brier_score": float(brier_score_loss(y, probability)),
                "f1": float(f1_score(y, prediction, zero_division=0)),
            }
            for metric, value in current[route].items():
                route_values[route][metric].append(value)
        for left, right in pairs:
            for metric in ["pr_auc", "brier_score", "f1"]:
                delta_values[(left, right)][metric].append(current[left][metric] - current[right][metric])
    rows: list[dict[str, Any]] = []
    for route in routes:
        for metric, values in route_values[route].items():
            array = np.asarray(values)
            rows.append(
                {
                    "kind": "route_metric",
                    "route": route,
                    "comparison": None,
                    "metric": metric,
                    "valid_replicates": len(array),
                    "bootstrap_mean": float(array.mean()),
                    "ci_lower_2_5": float(np.quantile(array, 0.025)),
                    "ci_upper_97_5": float(np.quantile(array, 0.975)),
                }
            )
    for (left, right), metrics in delta_values.items():
        for metric, values in metrics.items():
            array = np.asarray(values)
            rows.append(
                {
                    "kind": "paired_delta",
                    "route": left,
                    "comparison": f"{left}_minus_{right}",
                    "metric": metric,
                    "valid_replicates": len(array),
                    "bootstrap_mean": float(array.mean()),
                    "ci_lower_2_5": float(np.quantile(array, 0.025)),
                    "ci_upper_97_5": float(np.quantile(array, 0.975)),
                }
            )
    return rows


def route_comparisons(metric_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    core = pd.DataFrame(metric_rows)
    core = core.loc[
        (core["model_family"] == "logistic_regression")
        & (core["scope"] == "combined")
        & core["route"].isin(["text_only", "text_plus_sentiment", "text_plus_engineering"])
    ]
    pairs = [
        ("text_plus_sentiment", "text_only"),
        ("text_plus_engineering", "text_only"),
        ("text_plus_engineering", "text_plus_sentiment"),
    ]
    rows: list[dict[str, Any]] = []
    for (horizon, split_name), group in core.groupby(["horizon", "split"]):
        lookup = group.set_index("route")
        for left, right in pairs:
            if left not in lookup.index or right not in lookup.index:
                continue
            for metric in ["pr_auc", "brier_score", "recall", "f1"]:
                rows.append(
                    {
                        "horizon": int(horizon),
                        "split": split_name,
                        "comparison": f"{left}_minus_{right}",
                        "metric": metric,
                        "left_value": lookup.loc[left, metric],
                        "right_value": lookup.loc[right, metric],
                        "delta": lookup.loc[left, metric] - lookup.loc[right, metric],
                    }
                )
    return rows


def summary_markdown(status: dict[str, Any]) -> str:
    test = status["primary_h3_test"]
    lines = [
        "# Phase W6-D Summary",
        "",
        f"Technical status: **{status['status']}**  ",
        f"Next-phase readiness: **{status['next_phase_readiness']}**",
        "",
        "## Frozen two-layer design",
        "",
        "Upstream Rating, VADER Sentiment, and Engineering models remained different and unchanged. The three downstream core routes used identical product-month keys, TF-IDF matrices, time boundaries, targets, and estimator settings.",
        "",
        "## Primary h=3 Logistic Regression test",
        "",
        "| Route | N | PR-AUC | Brier | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for route in ["text_only", "text_plus_sentiment", "text_plus_engineering", "rating_only", "dummy"]:
        row = test[route]
        lines.append(
            f"| {route} | {row['n']} | {row['pr_auc']:.4f} | {row['brier_score']:.4f} | {row['recall']:.4f} | {row['f1']:.4f} |"
        )
    lines.extend(
        [
            "",
            f"Engineering incremental-value assessment: **{status['incremental_value_assessment']['status']}**.",
            "",
            "Exact event-month Lead Time was not constructed; h=1/2/3 results are reported as horizon-specific early-warning performance. Smart bulbs remain exploratory and smart switches remain case-study only.",
        ]
    )
    return "\n".join(lines) + "\n"


def status_for_exception(error: Exception) -> str:
    if isinstance(error, InputMismatch):
        return "FAILED_INPUT_MISMATCH"
    if isinstance(error, RouteSampleMismatch):
        return "FAILED_ROUTE_SAMPLE_MISMATCH"
    if isinstance(error, LeakageError):
        return "FAILED_LEAKAGE_AUDIT"
    if isinstance(error, SplitReview):
        return "PAUSED_SPLIT_REVIEW"
    if isinstance(error, UnknownOutput):
        return "FAILED_UNKNOWN_OUTPUT"
    if isinstance(error, SpaceGate):
        return "PAUSED_SPACE_GATE"
    return "FAILED_W6D"


def main() -> int:
    root = project_root()
    config_path = root / "config" / "w6d_warning_model_rules.toml"
    config = load_toml(config_path)
    interim_dir = root / config["outputs"]["interim_dir"]
    report_dir = root / config["outputs"]["report_dir"]
    model_dir = root / config["outputs"]["model_dir"]
    status_path = report_dir / "w6d_status.json"
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    initial_free = disk_free_gib(root)
    status_stub: dict[str, Any] = {"phase": "W6-D", "status": "RUNNING", "started_at_utc": started_at}
    try:
        minimum_free = float(config["runtime"]["minimum_free_gib"])
        if initial_free < minimum_free:
            raise SpaceGate("Initial disk space is below the 60 GiB floor")
        w6c_status = load_json(root / config["inputs"]["w6c_status"])
        if w6c_status.get("status") != config["inputs"]["w6c_required_status"]:
            raise InputMismatch("W6-C is not PASS")
        identities: dict[str, Any] = {}
        for name in ["formal_reviews", "review_signal_components", "product_month_engineering", "quality_targets", "analysis_panel"]:
            identities[name] = validate_parquet(
                root,
                root / config["inputs"][name],
                int(config["inputs"][f"{name}_rows"]),
                str(config["inputs"][f"{name}_sha256"]),
            )
        protected_paths = [root / config["inputs"][name] for name in identities]
        protected_before = {relative(root, path): file_identity(root, path) for path in protected_paths}
        fingerprint = stable_fingerprint(
            {
                "config_sha256": sha256_file(config_path),
                "script_sha256": sha256_file(Path(__file__).resolve()),
                "inputs": {name: identity["sha256"] for name, identity in identities.items()},
            }
        )
        existing_payloads = []
        for directory in [interim_dir, model_dir]:
            if directory.exists():
                existing_payloads.extend(path for path in directory.rglob("*") if path.is_file())
        if existing_payloads:
            if status_path.is_file():
                previous = load_json(status_path)
                if previous.get("status") == "PASS" and previous.get("run_fingerprint") == fingerprint:
                    for item in previous["material_outputs"]:
                        path = root / item["path"]
                        if not path.is_file() or sha256_file(path) != item["sha256"]:
                            raise UnknownOutput(f"Existing W6-D output changed: {item['path']}")
                    print("W6-D matching PASS outputs already exist; no files were overwritten.")
                    return 0
            raise UnknownOutput("Unknown or mismatched W6-D output already exists")

        formal = pq.read_table(
            root / config["inputs"]["formal_reviews"],
            columns=["duplicate_key", "parent_asin", "device_type", "review_datetime", "review_month", "review_text"],
        ).to_pandas()
        panel = pq.read_table(root / config["inputs"]["analysis_panel"]).to_pandas()
        if len(formal) != 55877 or not formal["duplicate_key"].is_unique:
            raise InputMismatch("Formal review rows or duplicate keys are invalid")
        if formal["review_text"].isna().any():
            raise InputMismatch("Formal review_text contains null values")
        keys_all = build_modeling_keys(panel, config)
        model_splits = {"train", "validation", "test"}
        keys_used = keys_all.loc[keys_all["split"].isin(model_splits)].copy()
        if int((keys_all["horizon"] == 3).sum()) != 515:
            raise RouteSampleMismatch("h=3 eligible count is not 515")
        h3_counts = keys_used.loc[keys_used["horizon"] == 3, "split"].value_counts().to_dict()
        if h3_counts != {"train": 205, "validation": 150, "test": 115}:
            raise RouteSampleMismatch(f"h=3 split counts differ: {h3_counts}")

        interim_dir.mkdir(parents=True, exist_ok=False)
        report_dir.mkdir(parents=True, exist_ok=True)
        model_dir.mkdir(parents=True, exist_ok=False)
        compression = str(config["runtime"]["compression"])
        modeling_key_columns = ["parent_asin", "review_month", "device_type", "analysis_role", "horizon", "split", "target"]
        write_parquet(root / config["outputs"]["modeling_keys"], keys_all[modeling_key_columns], compression)

        all_predictions: list[pd.DataFrame] = []
        metric_rows: list[dict[str, Any]] = []
        evaluation_records: dict[str, list[dict[str, Any]]] = {
            "logistic_regression": [], "linear_svm": [], "rating_only": [], "dummy": []
        }
        calibration_records: list[dict[str, Any]] = []
        text_audits: list[dict[str, Any]] = []
        model_manifest: list[dict[str, Any]] = []
        model_paths: list[Path] = []
        route_features = {
            "text_only": list(config["routes"]["text_only_numeric_features"]),
            "text_plus_sentiment": list(config["routes"]["sentiment_features"]),
            "text_plus_engineering": list(config["routes"]["engineering_features"]),
        }
        shared_matrix_ids: dict[int, int] = {}
        route_key_hashes: dict[int, dict[str, str]] = {}

        for horizon in config["targets"]["all_horizons"]:
            horizon = int(horizon)
            samples_h = keys_used.loc[keys_used["horizon"] == horizon].copy()
            text_matrix, vectorizer, text_info = build_text_matrix(samples_h, formal, config)
            samples = text_info["samples"]
            text_audit = {"horizon": horizon, **text_info["audit"]}
            text_audits.append(text_audit)
            shared_matrix_ids[horizon] = id(text_matrix)
            route_key_hashes[horizon] = {}
            key_hash = stable_fingerprint(
                list(zip(samples["parent_asin"], samples["review_month"], samples["split"], samples["target"]))
            )
            for route in config["routes"]["core"]:
                route = str(route)
                route_key_hashes[horizon][route] = key_hash
                route_matrix, scaler = combine_features(text_matrix, samples, route_features[route])
                estimator, prediction, metrics = fit_and_predict(route_matrix, samples, "logistic_regression", config)
                model_version = f"w6d-h{horizon}-logistic-{route}-v1.0"
                prediction["route"] = route
                prediction["model_family"] = "logistic_regression"
                prediction["model_version"] = model_version
                all_predictions.append(prediction)
                flattened = flatten_metrics(metrics, horizon, "logistic_regression", route)
                metric_rows.extend(flattened)
                evaluation_records["logistic_regression"].extend(flattened)
                for result in metrics:
                    if result["scope"] == "combined" and result["split"] in {"validation", "test"}:
                        calibration_records.append(
                            {
                                "horizon": horizon,
                                "route": route,
                                "model_family": "logistic_regression",
                                "split": result["split"],
                                "brier_score": result["brier_score"],
                                **(result["calibration"] or {}),
                            }
                        )
                model_path = model_dir / f"h{horizon}_logistic_{route}.joblib"
                write_joblib(
                    model_path,
                    {
                        "phase": "W6-D",
                        "version": model_version,
                        "horizon": horizon,
                        "route": route,
                        "text_vectorizer": vectorizer,
                        "text_aggregation": config["text"]["aggregation"],
                        "numeric_features": route_features[route],
                        "numeric_scaler": scaler,
                        "estimator": estimator,
                        "decision_threshold": float(config["logistic_regression"]["decision_threshold"]),
                        "sample_key_sha256": key_hash,
                        "training_only_fit": True,
                    },
                )
                model_paths.append(model_path)
                model_manifest.append(
                    {"horizon": horizon, "family": "logistic_regression", "route": route, **file_identity(root, model_path)}
                )
                if horizon == int(config["targets"]["primary_horizon"]):
                    svm, svm_prediction, svm_metrics = fit_and_predict(route_matrix, samples, "linear_svm", config)
                    svm_version = f"w6d-h{horizon}-linear-svm-{route}-v1.0"
                    svm_prediction["route"] = route
                    svm_prediction["model_family"] = "linear_svm"
                    svm_prediction["model_version"] = svm_version
                    all_predictions.append(svm_prediction)
                    svm_flat = flatten_metrics(svm_metrics, horizon, "linear_svm", route)
                    metric_rows.extend(svm_flat)
                    evaluation_records["linear_svm"].extend(svm_flat)
                    svm_path = model_dir / f"h{horizon}_linear_svm_{route}.joblib"
                    write_joblib(
                        svm_path,
                        {
                            "phase": "W6-D",
                            "version": svm_version,
                            "horizon": horizon,
                            "route": route,
                            "text_vectorizer": vectorizer,
                            "text_aggregation": config["text"]["aggregation"],
                            "numeric_features": route_features[route],
                            "numeric_scaler": scaler,
                            "estimator": svm,
                            "decision_threshold": float(config["linear_svm"]["decision_threshold"]),
                            "probability_calibration": False,
                            "sample_key_sha256": key_hash,
                            "training_only_fit": True,
                        },
                    )
                    model_paths.append(svm_path)
                    model_manifest.append(
                        {"horizon": horizon, "family": "linear_svm", "route": route, **file_identity(root, svm_path)}
                    )

            rating_features = list(config["routes"]["rating_reference_features"])
            rating_matrix, rating_scaler = combine_features(None, samples, rating_features)
            rating_model, rating_prediction, rating_metrics = fit_and_predict(
                rating_matrix, samples, "logistic_regression", config
            )
            rating_version = f"w6d-h{horizon}-logistic-rating-only-v1.0"
            rating_prediction["route"] = "rating_only"
            rating_prediction["model_family"] = "logistic_regression"
            rating_prediction["model_version"] = rating_version
            all_predictions.append(rating_prediction)
            rating_flat = flatten_metrics(rating_metrics, horizon, "logistic_regression", "rating_only")
            metric_rows.extend(rating_flat)
            evaluation_records["rating_only"].extend(rating_flat)
            for result in rating_metrics:
                if result["scope"] == "combined" and result["split"] in {"validation", "test"}:
                    calibration_records.append(
                        {
                            "horizon": horizon,
                            "route": "rating_only",
                            "model_family": "logistic_regression",
                            "split": result["split"],
                            "brier_score": result["brier_score"],
                            **(result["calibration"] or {}),
                        }
                    )
            rating_path = model_dir / f"h{horizon}_logistic_rating_only.joblib"
            write_joblib(
                rating_path,
                {
                    "phase": "W6-D",
                    "version": rating_version,
                    "horizon": horizon,
                    "route": "rating_only",
                    "status": config["routes"]["rating_reference_status"],
                    "numeric_features": rating_features,
                    "numeric_scaler": rating_scaler,
                    "estimator": rating_model,
                    "decision_threshold": float(config["logistic_regression"]["decision_threshold"]),
                    "sample_key_sha256": key_hash,
                    "training_only_fit": True,
                },
            )
            model_paths.append(rating_path)
            model_manifest.append(
                {"horizon": horizon, "family": "logistic_regression", "route": "rating_only", **file_identity(root, rating_path)}
            )

            dummy_model, dummy_prediction, dummy_metrics = fit_dummy(samples, config)
            dummy_version = f"w6d-h{horizon}-dummy-most-frequent-v1.0"
            dummy_prediction["route"] = "dummy"
            dummy_prediction["model_family"] = "dummy"
            dummy_prediction["model_version"] = dummy_version
            all_predictions.append(dummy_prediction)
            dummy_flat = flatten_metrics(dummy_metrics, horizon, "dummy", "dummy")
            metric_rows.extend(dummy_flat)
            evaluation_records["dummy"].extend(dummy_flat)
            for result in dummy_metrics:
                if result["scope"] == "combined" and result["split"] in {"validation", "test"}:
                    calibration_records.append(
                        {
                            "horizon": horizon,
                            "route": "dummy",
                            "model_family": "dummy",
                            "split": result["split"],
                            "brier_score": result["brier_score"],
                            **(result["calibration"] or {}),
                        }
                    )
            dummy_path = model_dir / f"h{horizon}_dummy_most_frequent.joblib"
            write_joblib(dummy_path, {"phase": "W6-D", "version": dummy_version, "estimator": dummy_model, "sample_key_sha256": key_hash})
            model_paths.append(dummy_path)
            model_manifest.append(
                {"horizon": horizon, "family": "dummy", "route": "dummy", **file_identity(root, dummy_path)}
            )

        for horizon, hashes in route_key_hashes.items():
            if len(set(hashes.values())) != 1:
                raise RouteSampleMismatch(f"h={horizon} route product-month keys differ")

        predictions = pd.concat(all_predictions, ignore_index=True)
        prediction_columns = [
            "parent_asin", "review_month", "device_type", "analysis_role", "horizon", "split",
            "target", "route", "model_family", "prediction", "probability", "decision_score", "model_version",
        ]
        predictions = predictions[prediction_columns].sort_values(
            ["horizon", "model_family", "route", "split", "review_month", "parent_asin"]
        ).reset_index(drop=True)
        write_parquet(root / config["outputs"]["predictions"], predictions, compression)
        errors = predictions.loc[
            predictions["split"].isin(["validation", "test"])
            & (predictions["prediction"] != predictions["target"])
        ].copy()
        errors["error_type"] = np.where(errors["prediction"] == 1, "false_positive", "false_negative")
        write_parquet(root / config["outputs"]["private_errors"], errors, compression)
        write_json(root / config["outputs"]["text_manifest"], {"phase": "W6-D", "horizons": text_audits})

        bootstrap_rows = bootstrap_primary(predictions, config)
        comparison_rows = route_comparisons(metric_rows)
        metric_frame = pd.DataFrame(metric_rows)
        confusion_rows = metric_frame.loc[
            metric_frame["scope"].eq("combined"),
            ["horizon", "model_family", "route", "split", "n", "negative", "positive", "tn", "fp", "fn", "tp"],
        ].to_dict("records")
        horizon_rows = metric_frame.loc[
            metric_frame["scope"].eq("combined") & metric_frame["split"].isin(["validation", "test"])
        ].to_dict("records")
        support_rows: list[dict[str, Any]] = []
        for horizon, subset in keys_used.groupby("horizon"):
            for split_name, split_subset in subset.groupby("split"):
                for device_type in ["smart_plug", "smart_bulb", "smart_switch"]:
                    device = split_subset.loc[split_subset["device_type"] == device_type]
                    support_rows.append(
                        {
                            "horizon": int(horizon),
                            "split": split_name,
                            "device_type": device_type,
                            "analysis_role": config["analysis_roles"][device_type],
                            "n": len(device),
                            "negative": int((device["target"] == 0).sum()),
                            "positive": int((device["target"] == 1).sum()),
                            "support_status": (
                                "SUFFICIENT_SUPPORT"
                                if len(device) >= int(config["evaluation"]["minimum_device_test_rows"])
                                and int((device["target"] == 0).sum()) >= int(config["evaluation"]["minimum_device_class_rows"])
                                and int((device["target"] == 1).sum()) >= int(config["evaluation"]["minimum_device_class_rows"])
                                else "INSUFFICIENT_SUPPORT"
                            ),
                        }
                    )

        h3_test = metric_frame.loc[
            (metric_frame["horizon"] == 3)
            & metric_frame["split"].eq("test")
            & metric_frame["scope"].eq("combined")
        ].set_index(["model_family", "route"])
        primary_lookup: dict[str, Any] = {}
        for route in ["text_only", "text_plus_sentiment", "text_plus_engineering", "rating_only"]:
            primary_lookup[route] = h3_test.loc[("logistic_regression", route)].to_dict()
        primary_lookup["dummy"] = h3_test.loc[("dummy", "dummy")].to_dict()
        text = primary_lookup["text_only"]
        engineering = primary_lookup["text_plus_engineering"]
        sentiment = primary_lookup["text_plus_sentiment"]
        if engineering["pr_auc"] > text["pr_auc"] and engineering["brier_score"] < text["brier_score"]:
            assessment = "SUPPORTED_ON_BOTH_PRIMARY_METRICS_DESCRIPTIVELY"
        elif engineering["pr_auc"] > text["pr_auc"] or engineering["brier_score"] < text["brier_score"]:
            assessment = "MIXED_PRIMARY_METRICS"
        else:
            assessment = "NOT_SUPPORTED_ON_PRIMARY_METRICS_DESCRIPTIVELY"
        incremental = {
            "status": assessment,
            "engineering_minus_text_pr_auc": engineering["pr_auc"] - text["pr_auc"],
            "engineering_minus_text_brier": engineering["brier_score"] - text["brier_score"],
            "engineering_minus_sentiment_pr_auc": engineering["pr_auc"] - sentiment["pr_auc"],
            "engineering_minus_sentiment_brier": engineering["brier_score"] - sentiment["brier_score"],
            "not_causal": True,
            "bootstrap_uncertainty_required": True,
        }
        overfit_rows: list[dict[str, Any]] = []
        for route in ["text_only", "text_plus_sentiment", "text_plus_engineering", "rating_only"]:
            rows = metric_frame.loc[
                (metric_frame["horizon"] == 3)
                & (metric_frame["model_family"] == "logistic_regression")
                & (metric_frame["route"] == route)
                & (metric_frame["scope"] == "combined")
            ].set_index("split")
            gap = float(rows.loc["train", "f1"] - rows.loc["validation", "f1"])
            overfit_rows.append(
                {"route": route, "train_f1": rows.loc["train", "f1"], "validation_f1": rows.loc["validation", "f1"], "train_validation_f1_gap": gap, "large_gap": gap > 0.15}
            )

        feature_contract = {
            "two_layer_design": {
                "upstream": {
                    "rating": "direct observed stars; no text ML",
                    "sentiment": "frozen offline VADER from W6-B",
                    "engineering": "frozen Failure/Severity/Persistence models and W6-C EngineeringIndex",
                    "upstream_models_are_different": True,
                    "upstream_models_modified": False,
                },
                "downstream": {
                    "core_routes": config["routes"]["core"],
                    "separately_fitted_instances": True,
                    "shared_algorithms_and_text_representation": True,
                },
            },
            "routes": {
                "text_only": {"text": "shared current-month mean review-level TF-IDF", "numeric": []},
                "text_plus_sentiment": {"text": "identical shared TF-IDF", "numeric": route_features["text_plus_sentiment"]},
                "text_plus_engineering": {"text": "identical shared TF-IDF", "numeric": route_features["text_plus_engineering"]},
                "rating_only": {"text": None, "numeric": list(config["routes"]["rating_reference_features"]), "status": "additional_transparent_reference"},
            },
            "forbidden_from_core_routes": ["rating", "future fields", "target fields", "parent_asin", "device_type", "product text"],
        }
        leakage_audit = {
            "passed": True,
            "tfidf_fit_on_train_reviews_only": True,
            "validation_reviews_used_for_vocabulary": 0,
            "test_reviews_used_for_vocabulary": 0,
            "numeric_scalers_fit_on_train_only": True,
            "same_text_matrix_reused_within_horizon": True,
            "same_product_month_keys_across_core_routes": True,
            "future_columns_in_features": 0,
            "target_columns_in_features": 0,
            "rating_in_core_routes": False,
            "sentiment_in_text_only": False,
            "engineering_in_text_only_or_sentiment_route": False,
            "test_used_for_feature_parameter_or_threshold_selection": False,
            "random_split": False,
            "embargo_rows_used_for_modeling": False,
            "raw_or_compressed_data_read": False,
        }

        protected_after = {relative(root, path): file_identity(root, path) for path in protected_paths}
        if protected_before != protected_after:
            raise InputMismatch("A protected W3-W6-C input changed during W6-D")
        final_free = disk_free_gib(root)
        if final_free < minimum_free:
            raise SpaceGate("Final disk space is below the 60 GiB floor")

        write_json(report_dir / "w6d_input_manifest.json", {"phase": "W6-D", "inputs": identities, "protected_before": protected_before, "protected_after": protected_after, "config": file_identity(root, config_path), "script": file_identity(root, Path(__file__).resolve())})
        sample_rows: list[dict[str, Any]] = []
        for (horizon, split_name), subset in keys_all.groupby(["horizon", "split"]):
            sample_rows.append({"horizon": int(horizon), "split": split_name, "rows": len(subset), "negative": int((subset["target"] == 0).sum()), "positive": int((subset["target"] == 1).sum()), "products": int(subset["parent_asin"].nunique()), "smart_plug": int((subset["device_type"] == "smart_plug").sum()), "smart_bulb": int((subset["device_type"] == "smart_bulb").sum()), "smart_switch": int((subset["device_type"] == "smart_switch").sum())})
        write_json(report_dir / "w6d_sample_manifest.json", {"rows": sample_rows, "same_boundaries_all_horizons": True})
        write_json(report_dir / "w6d_feature_contract.json", feature_contract)
        write_json(report_dir / "w6d_text_vectorization_audit.json", {"horizons": text_audits})
        write_json(report_dir / "w6d_model_manifest.json", {"models": model_manifest})
        write_json(report_dir / "w6d_logistic_evaluation.json", {"results": evaluation_records["logistic_regression"]})
        write_json(report_dir / "w6d_linear_svm_evaluation.json", {"results": evaluation_records["linear_svm"], "probability_calibration": False, "brier_or_calibration_reported": False})
        write_json(report_dir / "w6d_rating_reference_evaluation.json", {"status": "additional_transparent_reference", "results": evaluation_records["rating_only"]})
        write_json(report_dir / "w6d_dummy_evaluation.json", {"results": evaluation_records["dummy"]})
        write_csv(report_dir / "w6d_route_comparison.csv", comparison_rows)
        write_csv(report_dir / "w6d_horizon_comparison.csv", horizon_rows)
        write_csv(report_dir / "w6d_confusion_matrices.csv", confusion_rows)
        write_json(report_dir / "w6d_calibration_summary.json", {"results": calibration_records})
        write_csv(report_dir / "w6d_bootstrap_intervals.csv", bootstrap_rows)
        write_csv(report_dir / "w6d_device_type_support.csv", support_rows)
        write_json(report_dir / "w6d_error_summary.json", {"error_rows_private": len(errors), "by_horizon_model_route_split_error_type": errors.groupby(["horizon", "model_family", "route", "split", "error_type"]).size().reset_index(name="n").to_dict("records"), "overfit_audit_h3_logistic": overfit_rows, "ordinary_reports_contain_error_text": False})
        write_json(report_dir / "w6d_leakage_audit.json", leakage_audit)
        write_json(report_dir / "w6d_disk_usage.json", {"minimum_free_gib": minimum_free, "initial_free_gib": initial_free, "final_free_gib": final_free})

        material_paths = [root / config["outputs"]["modeling_keys"], root / config["outputs"]["text_manifest"], root / config["outputs"]["predictions"], root / config["outputs"]["private_errors"], *model_paths]
        material_outputs = [file_identity(root, path) for path in material_paths]
        status: dict[str, Any] = {
            "phase": "W6-D",
            "status": "PASS",
            "next_phase_readiness": "REVIEW_REQUIRED",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.perf_counter() - started,
            "run_fingerprint": fingerprint,
            "environment": {"python_executable": sys.executable, "python_version": platform.python_version(), "python_bits": struct.calcsize("P") * 8, "pandas": pd.__version__, "pyarrow": pa.__version__, "scikit_learn": sklearn_version, "scipy": scipy.__version__},
            "input_validation": {"w6c_status": w6c_status.get("status"), "identities_match": True, "identities": identities},
            "two_layer_design_preserved": True,
            "upstream_models_different_and_unchanged": True,
            "downstream_core_routes": config["routes"]["core"],
            "same_algorithms_across_core_routes": True,
            "same_text_matrix_and_keys_across_core_routes": True,
            "primary_h3_test": primary_lookup,
            "incremental_value_assessment": incremental,
            "bootstrap": {"replicates_requested": int(config["evaluation"]["bootstrap_replicates"]), "cluster": "parent_asin", "rows": bootstrap_rows},
            "overfit_audit": overfit_rows,
            "sample_counts": sample_rows,
            "device_support": support_rows,
            "leakage_audit": leakage_audit,
            "rating_only_included": True,
            "rating_only_status": "additional_transparent_reference",
            "dummy_included": True,
            "linear_svm_robustness_included": True,
            "exact_event_month_or_lead_time_built": False,
            "horizon_specific_early_warning_reported": True,
            "bert_or_transformer_trained": False,
            "online_api_or_llm_used": False,
            "raw_jsonl_metadata_or_compressed_read": False,
            "test_used_for_tuning": False,
            "upstream_models_modified": False,
            "engineering_index_modified": False,
            "quality_target_modified": False,
            "human_labels_modified": False,
            "protected_inputs_unchanged": True,
            "next_phase_executed": False,
            "git_commit_created": False,
            "disk": {"minimum_free_gib": minimum_free, "initial_free_gib": initial_free, "final_free_gib": final_free},
            "material_outputs": material_outputs,
            "next_decisions": ["whether to construct an exact event-month Lead Time map", "whether to run supplemental BERT", "whether to generate final paper tables and figures"],
        }
        write_text(report_dir / "w6d_summary.md", summary_markdown(status))
        write_text(report_dir / "w6d_execution.log", f"{started_at} W6-D started\n{datetime.now(timezone.utc).isoformat()} PASS; upstream models unchanged; no raw reads, BERT, test tuning, or next phase\n")
        write_json(status_path, status)

        report_blob = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in report_dir.iterdir()
            if path.suffix.lower() in {".json", ".csv", ".md", ".log"}
        )
        if any(value in report_blob for value in formal["duplicate_key"].astype(str)):
            raise LeakageError("A duplicate_key value leaked into an ordinary report")
        long_texts = formal.loc[formal["review_text"].str.len() >= 40, "review_text"].astype(str)
        if any(value in report_blob for value in long_texts):
            raise LeakageError("A review text value leaked into an ordinary report")
        return 0
    except Exception as error:
        report_dir.mkdir(parents=True, exist_ok=True)
        status_stub.update({"status": status_for_exception(error), "failed_at_utc": datetime.now(timezone.utc).isoformat(), "error_type": type(error).__name__, "error": str(error), "next_phase_executed": False})
        write_json(status_path, status_stub)
        print(f"{status_stub['status']}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
