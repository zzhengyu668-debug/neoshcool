from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    cohen_kappa_score,
    confusion_matrix,
    f1_score,
    mean_absolute_error,
    precision_score,
    recall_score,
    roc_auc_score,
)
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import run_w6a_full_failure_inference as w6a  # noqa: E402


PHASE = "W6-B"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
REVIEW_COLUMNS = [
    "duplicate_key",
    "parent_asin",
    "device_type",
    "source_domain",
    "review_datetime",
    "review_month",
    "analysis_role",
    "failure_probability",
    "failure_prediction",
    "severity_probability_ge2_given_failure",
    "severity_probability_ge3_given_failure",
    "expected_severity_given_failure",
    "expected_severity_signal",
    "persistence_probability_ge1_given_failure",
    "persistence_probability_ge2_given_failure",
    "expected_persistence_given_failure",
    "expected_persistence_signal",
    "sentiment_compound",
    "sentiment_positive",
    "sentiment_neutral",
    "sentiment_negative",
    "negative_sentiment_indicator",
    "failure_model_version",
    "severity_model_version",
    "persistence_model_version",
    "sentiment_model_version",
    "product_filter_version",
]
PRODUCT_MONTH_COLUMNS = [
    "parent_asin",
    "review_month",
    "device_type",
    "analysis_role",
    "n_reviews",
    "predicted_failure_count",
    "predicted_failure_share",
    "mean_failure_probability",
    "mean_expected_severity_signal",
    "mean_expected_persistence_signal",
    "mean_sentiment_compound",
    "negative_sentiment_count",
    "negative_sentiment_share",
    "failure_model_version",
    "severity_model_version",
    "persistence_model_version",
    "sentiment_model_version",
    "product_filter_version",
]
FORBIDDEN_FIELDS = {
    "review_text",
    "review_title",
    "review_body",
    "user_id",
    "user_id_hash",
    "product_title",
    "engineering_index",
    "future_quality_target",
    "target_next_1m",
    "target_next_3m",
}
REVIEW_SCHEMA = pa.schema(
    [
        pa.field("duplicate_key", pa.string(), False),
        pa.field("parent_asin", pa.string(), False),
        pa.field("device_type", pa.string(), False),
        pa.field("source_domain", pa.string(), False),
        pa.field("review_datetime", pa.timestamp("us", tz="UTC"), False),
        pa.field("review_month", pa.date32(), False),
        pa.field("analysis_role", pa.string(), False),
        pa.field("failure_probability", pa.float64(), False),
        pa.field("failure_prediction", pa.int8(), False),
        pa.field("severity_probability_ge2_given_failure", pa.float64(), False),
        pa.field("severity_probability_ge3_given_failure", pa.float64(), False),
        pa.field("expected_severity_given_failure", pa.float64(), False),
        pa.field("expected_severity_signal", pa.float64(), False),
        pa.field("persistence_probability_ge1_given_failure", pa.float64(), False),
        pa.field("persistence_probability_ge2_given_failure", pa.float64(), False),
        pa.field("expected_persistence_given_failure", pa.float64(), False),
        pa.field("expected_persistence_signal", pa.float64(), False),
        pa.field("sentiment_compound", pa.float64(), False),
        pa.field("sentiment_positive", pa.float64(), False),
        pa.field("sentiment_neutral", pa.float64(), False),
        pa.field("sentiment_negative", pa.float64(), False),
        pa.field("negative_sentiment_indicator", pa.int8(), False),
        pa.field("failure_model_version", pa.string(), False),
        pa.field("severity_model_version", pa.string(), False),
        pa.field("persistence_model_version", pa.string(), False),
        pa.field("sentiment_model_version", pa.string(), False),
        pa.field("product_filter_version", pa.string(), False),
    ]
)
PRODUCT_MONTH_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), False),
        pa.field("review_month", pa.date32(), False),
        pa.field("device_type", pa.string(), False),
        pa.field("analysis_role", pa.string(), False),
        pa.field("n_reviews", pa.int64(), False),
        pa.field("predicted_failure_count", pa.int64(), False),
        pa.field("predicted_failure_share", pa.float64(), False),
        pa.field("mean_failure_probability", pa.float64(), False),
        pa.field("mean_expected_severity_signal", pa.float64(), False),
        pa.field("mean_expected_persistence_signal", pa.float64(), False),
        pa.field("mean_sentiment_compound", pa.float64(), False),
        pa.field("negative_sentiment_count", pa.int64(), False),
        pa.field("negative_sentiment_share", pa.float64(), False),
        pa.field("failure_model_version", pa.string(), False),
        pa.field("severity_model_version", pa.string(), False),
        pa.field("persistence_model_version", pa.string(), False),
        pa.field("sentiment_model_version", pa.string(), False),
        pa.field("product_filter_version", pa.string(), False),
    ]
)


class W6BError(RuntimeError):
    """Controlled W6-B error."""


class InputMismatch(W6BError):
    """A frozen input is missing or differs from its approved identity."""


class TrainingFailure(W6BError):
    """An approved cumulative model cannot be trained."""


class InferenceFailure(W6BError):
    """Full signal-component inference failed."""


class AggregationFailure(W6BError):
    """Product-month component aggregation failed."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W6BError(f"Could not resolve project root from {__file__}")
    if not (root / "config/project.toml").is_file():
        raise W6BError("config/project.toml is missing")
    return root


def assign_w5c_b_split(labels: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    definite = labels.loc[labels["label_status"] == "definite"].copy()
    definite["review_datetime"] = pd.to_datetime(definite["review_datetime"], utc=True)
    definite = definite.sort_values(
        ["review_datetime", "blind_review_id"], kind="stable"
    ).reset_index(drop=True)
    split_cfg = config["split"]
    if len(definite) != int(split_cfg["definite_rows"]):
        raise InputMismatch(
            f"Expected {split_cfg['definite_rows']} definite rows, got {len(definite)}"
        )
    train_end = int(split_cfg["train_end_index_exclusive"])
    validation_end = int(split_cfg["validation_end_index_exclusive"])
    definite["split"] = "test"
    definite.loc[: train_end - 1, "split"] = "train"
    definite.loc[train_end : validation_end - 1, "split"] = "validation"
    return definite


def threshold_key(threshold: int) -> str:
    return f"ge_{threshold}"


def enforce_cumulative_monotonicity(
    probabilities: dict[str, np.ndarray], thresholds: Sequence[int]
) -> tuple[dict[str, np.ndarray], int]:
    adjusted: dict[str, np.ndarray] = {}
    previous: np.ndarray | None = None
    changed = 0
    for threshold in thresholds:
        key = threshold_key(threshold)
        current = np.asarray(probabilities[key], dtype=float).copy()
        if previous is not None:
            changed += int((current > previous).sum())
            current = np.minimum(current, previous)
        adjusted[key] = current
        previous = current
    return adjusted, changed


def expected_from_cumulative(
    probabilities: dict[str, np.ndarray], thresholds: Sequence[int], base: int
) -> np.ndarray:
    length = len(next(iter(probabilities.values())))
    expected = np.full(length, float(base))
    for threshold in thresholds:
        expected += probabilities[threshold_key(threshold)]
    return expected


def hard_class_from_cumulative(
    probabilities: dict[str, np.ndarray], thresholds: Sequence[int], base: int, cutoff: float
) -> np.ndarray:
    length = len(next(iter(probabilities.values())))
    prediction = np.full(length, int(base), dtype=np.int8)
    for threshold in thresholds:
        prediction += (probabilities[threshold_key(threshold)] >= cutoff).astype(np.int8)
    return prediction


def safe_kappa(true: Sequence[int], predicted: Sequence[int], weights: str) -> float | None:
    value = cohen_kappa_score(true, predicted, weights=weights)
    return None if np.isnan(value) else float(value)


def binary_metrics(true: Sequence[int], probability: Sequence[float], cutoff: float) -> dict[str, Any]:
    y_true = np.asarray(true, dtype=int)
    y_probability = np.asarray(probability, dtype=float)
    y_pred = (y_probability >= cutoff).astype(int)
    labels_present = sorted(int(value) for value in np.unique(y_true))
    matrix = confusion_matrix(y_true, y_pred, labels=[0, 1])
    tn, fp, fn, tp = matrix.ravel()
    both = labels_present == [0, 1]
    return {
        "n": int(len(y_true)),
        "true_0": int((y_true == 0).sum()),
        "true_1": int((y_true == 1).sum()),
        "labels_present": labels_present,
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "specificity": float(tn / (tn + fp)) if (tn + fp) else None,
        "roc_auc": float(roc_auc_score(y_true, y_probability)) if both else None,
        "pr_auc": float(average_precision_score(y_true, y_probability)) if both else None,
        "roc_auc_pr_auc_available": both,
    }


def ordinal_metrics(
    true: Sequence[int], predicted: Sequence[int], levels: Sequence[int]
) -> dict[str, Any]:
    y_true = np.asarray(true, dtype=int)
    y_pred = np.asarray(predicted, dtype=int)
    return {
        "n": int(len(y_true)),
        "true_level_counts": {
            str(level): int((y_true == level).sum()) for level in levels
        },
        "predicted_level_counts": {
            str(level): int((y_pred == level).sum()) for level in levels
        },
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "macro_f1": float(
            f1_score(y_true, y_pred, labels=list(levels), average="macro", zero_division=0)
        ),
        "linear_weighted_kappa": safe_kappa(y_true, y_pred, "linear"),
        "quadratic_weighted_kappa": safe_kappa(y_true, y_pred, "quadratic"),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=list(levels)).tolist(),
    }


def score_ordinal_bundle(
    texts: Sequence[str], bundle: dict[str, Any]
) -> dict[str, Any]:
    vectorizer = bundle["vectorizer"]
    vocabulary_size = len(vectorizer.vocabulary_)
    coefficient_copies = {
        key: model.coef_.copy() for key, model in bundle["classifiers"].items()
    }
    matrix = vectorizer.transform(texts)
    raw: dict[str, np.ndarray] = {}
    for key, classifier in bundle["classifiers"].items():
        classes = list(classifier.classes_)
        if 1 not in classes:
            raise InferenceFailure(f"{bundle['model_version']} {key} lacks positive class")
        raw[key] = classifier.predict_proba(matrix)[:, classes.index(1)]
    probabilities, changed = enforce_cumulative_monotonicity(
        raw, bundle["thresholds"]
    )
    if len(vectorizer.vocabulary_) != vocabulary_size:
        raise InferenceFailure("TF-IDF vocabulary changed during transform")
    for key, classifier in bundle["classifiers"].items():
        if not np.array_equal(classifier.coef_, coefficient_copies[key]):
            raise InferenceFailure(f"Classifier coefficients changed during {key} scoring")
    expected = expected_from_cumulative(
        probabilities, bundle["thresholds"], bundle["base"]
    )
    hard = hard_class_from_cumulative(
        probabilities,
        bundle["thresholds"],
        bundle["base"],
        bundle["decision_threshold"],
    )
    return {
        "probabilities": probabilities,
        "expected": expected,
        "hard_prediction": hard,
        "monotonic_adjustment_count": changed,
    }


def train_ordinal_bundle(
    failures: pd.DataFrame, task_name: str, task_cfg: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    train = failures.loc[failures["split"] == "train"].copy()
    tfidf_cfg = config["tfidf"]
    logistic_cfg = config["logistic_regression"]
    vectorizer = TfidfVectorizer(
        lowercase=tfidf_cfg["lowercase"],
        ngram_range=(tfidf_cfg["ngram_min"], tfidf_cfg["ngram_max"]),
        min_df=tfidf_cfg["min_df"],
        max_features=tfidf_cfg["max_features"],
        sublinear_tf=tfidf_cfg["sublinear_tf"],
        strip_accents=tfidf_cfg["strip_accents"],
    )
    train_matrix = vectorizer.fit_transform(train["model_text"].fillna(""))
    classifiers: dict[str, LogisticRegression] = {}
    training_threshold_counts: dict[str, Any] = {}
    for threshold in task_cfg["cumulative_thresholds"]:
        key = threshold_key(threshold)
        y_train = (train[task_cfg["label_field"]].astype(int) >= int(threshold)).astype(int)
        counts = y_train.value_counts().sort_index().to_dict()
        training_threshold_counts[key] = {str(int(k)): int(v) for k, v in counts.items()}
        if set(counts) != {0, 1}:
            raise TrainingFailure(f"{task_name} {key} train split lacks a binary class")
        classifier = LogisticRegression(
            C=logistic_cfg["C"],
            class_weight=logistic_cfg["class_weight"],
            max_iter=logistic_cfg["max_iter"],
            random_state=logistic_cfg["random_state"],
        )
        classifier.fit(train_matrix, y_train)
        classifiers[key] = classifier
    bundle = {
        "phase": PHASE,
        "task": task_name,
        "model_version": task_cfg["model_version"],
        "label_field": task_cfg["label_field"],
        "levels": [int(value) for value in task_cfg["levels"]],
        "thresholds": [int(value) for value in task_cfg["cumulative_thresholds"]],
        "base": int(task_cfg["conditional_base"]),
        "decision_threshold": float(logistic_cfg["decision_threshold"]),
        "vectorizer": vectorizer,
        "classifiers": classifiers,
        "training_rows": len(train),
        "training_date_min": train["review_datetime"].min().isoformat(),
        "training_date_max": train["review_datetime"].max().isoformat(),
        "feature_field": "model_text",
        "fit_vocabulary_on": "failure_train_only",
        "forbidden_features": [
            "rating", "device_type", "parent_asin", "review_datetime", "review_month"
        ],
    }
    evaluation: dict[str, Any] = {
        "task": task_name,
        "model_version": task_cfg["model_version"],
        "training_population": "definite human-labeled failure_binary=1 reviews only",
        "vocabulary_size": len(vectorizer.vocabulary_),
        "training_rows": len(train),
        "training_threshold_counts": training_threshold_counts,
        "evaluations": {},
    }
    limited = False
    insufficient = False
    rare_minimum = int(task_cfg["rare_class_minimum"])
    for split in ("train", "validation", "test"):
        subset = failures.loc[failures["split"] == split]
        scored = score_ordinal_bundle(subset["model_text"].fillna(""), bundle)
        true = subset[task_cfg["label_field"]].astype(int).to_numpy()
        split_payload: dict[str, Any] = {
            "ordinal": ordinal_metrics(true, scored["hard_prediction"], bundle["levels"]),
            "thresholds": {},
            "monotonic_adjustment_count": scored["monotonic_adjustment_count"],
        }
        for threshold in bundle["thresholds"]:
            key = threshold_key(threshold)
            binary_true = (true >= threshold).astype(int)
            metrics = binary_metrics(
                binary_true, scored["probabilities"][key], bundle["decision_threshold"]
            )
            if len(metrics["labels_present"]) < 2:
                metrics["support_status"] = "INSUFFICIENT_SUPPORT"
                insufficient = True
            elif min(metrics["true_0"], metrics["true_1"]) < rare_minimum:
                metrics["support_status"] = "LIMITED_RARE_CLASS_SUPPORT"
                limited = True
            else:
                metrics["support_status"] = "SUFFICIENT"
            split_payload["thresholds"][key] = metrics
        evaluation["evaluations"][split] = split_payload
    evaluation["model_status"] = (
        "INSUFFICIENT_SUPPORT"
        if insufficient
        else "LIMITED_RARE_CLASS_SUPPORT"
        if limited
        else "SUFFICIENT"
    )
    evaluation["test_by_device_type"] = device_test_support(
        failures, bundle, config["support"]
    )
    return bundle, evaluation


def device_test_support(
    failures: pd.DataFrame, bundle: dict[str, Any], support_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    test = failures.loc[failures["split"] == "test"]
    for device in DEVICE_TYPES:
        subset = test.loc[test["device_type"] == device]
        counts = subset[bundle["label_field"]].astype(int).value_counts().sort_index()
        status = "SUFFICIENT"
        if len(subset) < int(support_cfg["minimum_device_test_rows"]):
            status = "INSUFFICIENT_SUPPORT"
        elif counts.empty or counts.min() < int(support_cfg["minimum_rows_per_observed_class"]):
            status = "INSUFFICIENT_SUPPORT"
        row: dict[str, Any] = {
            "device_type": device,
            "rows": len(subset),
            "class_counts": {str(int(k)): int(v) for k, v in counts.items()},
            "support_status": status,
        }
        if status == "SUFFICIENT":
            scored = score_ordinal_bundle(subset["model_text"].fillna(""), bundle)
            row["metrics"] = ordinal_metrics(
                subset[bundle["label_field"]].astype(int),
                scored["hard_prediction"],
                bundle["levels"],
            )
        rows.append(row)
    return rows


def create_failure_training_frame(
    labels: pd.DataFrame, formal_reviews: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    definite = assign_w5c_b_split(labels, config)
    failures = definite.loc[
        definite["final_failure_binary"].astype(str)
        == str(config["training_population"]["required_failure_binary"])
    ].copy()
    joined = failures.merge(
        formal_reviews[["duplicate_key", "review_text"]],
        on="duplicate_key",
        how="left",
        validate="one_to_one",
    )
    if joined["review_text"].isna().any():
        raise InputMismatch("One or more failure labels could not be joined to review_text")
    joined["model_text"] = [w6a.preprocess_model_text(value)[0] for value in joined["review_text"]]
    split_counts = joined["split"].value_counts().to_dict()
    expected = config["training_population"]
    required = {
        "train": int(expected["expected_train_rows"]),
        "validation": int(expected["expected_validation_rows"]),
        "test": int(expected["expected_test_rows"]),
    }
    if len(joined) != int(expected["expected_failure_rows"]) or split_counts != required:
        raise InputMismatch(
            f"Failure training population mismatch: rows={len(joined)}, splits={split_counts}"
        )
    audit: dict[str, Any] = {
        "definite_rows": len(definite),
        "failure_rows": len(joined),
        "split_counts": split_counts,
        "strict_chronological_split_reused": True,
        "random_shuffle": False,
        "split_distributions": {},
    }
    for split in ("train", "validation", "test"):
        subset = joined.loc[joined["split"] == split]
        audit["split_distributions"][split] = {
            "rows": len(subset),
            "earliest_utc": subset["review_datetime"].min(),
            "latest_utc": subset["review_datetime"].max(),
            "severity": {
                str(int(k)): int(v)
                for k, v in subset["final_severity"].value_counts().sort_index().items()
            },
            "persistence": {
                str(int(k)): int(v)
                for k, v in subset["final_persistence"].value_counts().sort_index().items()
            },
            "device_type": {
                str(k): int(v) for k, v in subset["device_type"].value_counts().items()
            },
        }
    return joined, audit


def atomic_joblib(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(payload, temporary)
    temporary.replace(path)


def score_sentiment(texts: Sequence[str], analyzer: SentimentIntensityAnalyzer) -> pd.DataFrame:
    rows = [analyzer.polarity_scores("" if value is None else str(value)) for value in texts]
    return pd.DataFrame(
        {
            "sentiment_compound": [float(row["compound"]) for row in rows],
            "sentiment_positive": [float(row["pos"]) for row in rows],
            "sentiment_neutral": [float(row["neu"]) for row in rows],
            "sentiment_negative": [float(row["neg"]) for row in rows],
        }
    )


def validate_and_merge_full_inputs(
    formal: pd.DataFrame, failure_predictions: pd.DataFrame
) -> pd.DataFrame:
    metadata = [
        "duplicate_key", "parent_asin", "device_type", "source_domain",
        "review_datetime", "review_month",
    ]
    for frame, label in ((formal, "formal reviews"), (failure_predictions, "W6-A predictions")):
        if len(frame) != 55877 or not frame["duplicate_key"].is_unique:
            raise InputMismatch(f"{label} must contain 55,877 unique duplicate_key rows")
    joined = formal.merge(
        failure_predictions,
        on="duplicate_key",
        how="inner",
        validate="one_to_one",
        suffixes=("_formal", "_w6a"),
    )
    if len(joined) != 55877:
        raise InputMismatch("Formal review and W6-A prediction join is incomplete")
    for field in metadata[1:]:
        left = joined[f"{field}_formal"]
        right = joined[f"{field}_w6a"]
        if field == "review_datetime":
            equal = pd.to_datetime(left, utc=True).equals(pd.to_datetime(right, utc=True))
        else:
            equal = left.astype(str).equals(right.astype(str))
        if not equal:
            raise InputMismatch(f"Formal and W6-A values differ for {field}")
    return joined


def build_review_components(
    joined: pd.DataFrame,
    severity_bundle: dict[str, Any],
    persistence_bundle: dict[str, Any],
    config: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    model_text = [w6a.preprocess_model_text(value)[0] for value in joined["review_text"]]
    severity = score_ordinal_bundle(model_text, severity_bundle)
    persistence = score_ordinal_bundle(model_text, persistence_bundle)
    analyzer = SentimentIntensityAnalyzer()
    sentiment = score_sentiment(joined["review_text"].tolist(), analyzer)
    failure_probability = joined["failure_probability"].astype(float).to_numpy()
    severity_signal = failure_probability * severity["expected"]
    persistence_signal = failure_probability * persistence["expected"]
    negative_threshold = float(config["sentiment"]["compound_negative_threshold"])
    output = pd.DataFrame(
        {
            "duplicate_key": joined["duplicate_key"].astype(str),
            "parent_asin": joined["parent_asin_formal"].astype(str),
            "device_type": joined["device_type_formal"].astype(str),
            "source_domain": joined["source_domain_formal"].astype(str),
            "review_datetime": pd.to_datetime(joined["review_datetime_formal"], utc=True),
            "review_month": joined["review_month_formal"],
            "analysis_role": joined["analysis_role"].astype(str),
            "failure_probability": failure_probability,
            "failure_prediction": joined["failure_prediction"].astype(np.int8),
            "severity_probability_ge2_given_failure": severity["probabilities"]["ge_2"],
            "severity_probability_ge3_given_failure": severity["probabilities"]["ge_3"],
            "expected_severity_given_failure": severity["expected"],
            "expected_severity_signal": severity_signal,
            "persistence_probability_ge1_given_failure": persistence["probabilities"]["ge_1"],
            "persistence_probability_ge2_given_failure": persistence["probabilities"]["ge_2"],
            "expected_persistence_given_failure": persistence["expected"],
            "expected_persistence_signal": persistence_signal,
            "sentiment_compound": sentiment["sentiment_compound"],
            "sentiment_positive": sentiment["sentiment_positive"],
            "sentiment_neutral": sentiment["sentiment_neutral"],
            "sentiment_negative": sentiment["sentiment_negative"],
            "negative_sentiment_indicator": (
                sentiment["sentiment_compound"] <= negative_threshold
            ).astype(np.int8),
            "failure_model_version": joined["model_version"].astype(str),
            "severity_model_version": config["severity"]["model_version"],
            "persistence_model_version": config["persistence"]["model_version"],
            "sentiment_model_version": config["sentiment"]["model_version"],
            "product_filter_version": joined["product_filter_version"].astype(str),
        }
    )
    return output[REVIEW_COLUMNS], {
        "rows": len(output),
        "severity_monotonic_adjustment_count": severity["monotonic_adjustment_count"],
        "persistence_monotonic_adjustment_count": persistence["monotonic_adjustment_count"],
        "negative_sentiment_threshold": negative_threshold,
        "negative_sentiment_count": int(output["negative_sentiment_indicator"].sum()),
    }


def validate_review_components(frame: pd.DataFrame) -> dict[str, Any]:
    if len(frame) != 55877 or not frame["duplicate_key"].is_unique:
        raise InferenceFailure("Review components must have 55,877 unique rows")
    if set(frame.columns) != set(REVIEW_COLUMNS):
        raise InferenceFailure("Review component schema mismatch")
    if set(frame.columns) & FORBIDDEN_FIELDS:
        raise InferenceFailure("Forbidden fields exist in review components")
    numeric_bounds = [
        ("failure_probability", 0, 1),
        ("severity_probability_ge2_given_failure", 0, 1),
        ("severity_probability_ge3_given_failure", 0, 1),
        ("expected_severity_given_failure", 1, 3),
        ("expected_severity_signal", 0, 3),
        ("persistence_probability_ge1_given_failure", 0, 1),
        ("persistence_probability_ge2_given_failure", 0, 1),
        ("expected_persistence_given_failure", 0, 2),
        ("expected_persistence_signal", 0, 2),
        ("sentiment_compound", -1, 1),
        ("sentiment_positive", 0, 1),
        ("sentiment_neutral", 0, 1),
        ("sentiment_negative", 0, 1),
    ]
    for field, lower, upper in numeric_bounds:
        if not frame[field].between(lower, upper, inclusive="both").all():
            raise InferenceFailure(f"{field} falls outside [{lower}, {upper}]")
    if not (
        frame["severity_probability_ge3_given_failure"]
        <= frame["severity_probability_ge2_given_failure"] + 1e-15
    ).all():
        raise InferenceFailure("Severity cumulative probabilities are not monotonic")
    if not (
        frame["persistence_probability_ge2_given_failure"]
        <= frame["persistence_probability_ge1_given_failure"] + 1e-15
    ).all():
        raise InferenceFailure("Persistence cumulative probabilities are not monotonic")
    if not np.allclose(
        frame["expected_severity_signal"],
        frame["failure_probability"] * frame["expected_severity_given_failure"],
    ):
        raise InferenceFailure("Expected Severity signal formula mismatch")
    if not np.allclose(
        frame["expected_persistence_signal"],
        frame["failure_probability"] * frame["expected_persistence_given_failure"],
    ):
        raise InferenceFailure("Expected Persistence signal formula mismatch")
    return {
        "rows": len(frame),
        "unique_duplicate_keys": int(frame["duplicate_key"].nunique()),
        "unique_parent_asin": int(frame["parent_asin"].nunique()),
        "forbidden_fields_present": [],
        "probability_bounds_valid": True,
        "cumulative_monotonicity_valid": True,
        "signal_formulas_valid": True,
    }


def aggregate_product_month(frame: pd.DataFrame) -> pd.DataFrame:
    group_fields = [
        "parent_asin", "review_month", "device_type", "analysis_role",
        "failure_model_version", "severity_model_version",
        "persistence_model_version", "sentiment_model_version",
        "product_filter_version",
    ]
    grouped = frame.groupby(group_fields, sort=True, dropna=False, observed=True)
    result = grouped.agg(
        n_reviews=("duplicate_key", "size"),
        predicted_failure_count=("failure_prediction", "sum"),
        predicted_failure_share=("failure_prediction", "mean"),
        mean_failure_probability=("failure_probability", "mean"),
        mean_expected_severity_signal=("expected_severity_signal", "mean"),
        mean_expected_persistence_signal=("expected_persistence_signal", "mean"),
        mean_sentiment_compound=("sentiment_compound", "mean"),
        negative_sentiment_count=("negative_sentiment_indicator", "sum"),
        negative_sentiment_share=("negative_sentiment_indicator", "mean"),
    ).reset_index()
    result["n_reviews"] = result["n_reviews"].astype("int64")
    result["predicted_failure_count"] = result["predicted_failure_count"].astype("int64")
    result["negative_sentiment_count"] = result["negative_sentiment_count"].astype("int64")
    return result[PRODUCT_MONTH_COLUMNS].sort_values(
        ["parent_asin", "review_month"], kind="stable"
    ).reset_index(drop=True)


def validate_product_month(signals: pd.DataFrame, reviews: pd.DataFrame) -> dict[str, Any]:
    if len(signals) != 1911:
        raise AggregationFailure(f"Expected 1,911 product-month rows, got {len(signals)}")
    if int(signals["n_reviews"].sum()) != len(reviews):
        raise AggregationFailure("Product-month review counts do not sum to 55,877")
    if int(signals["predicted_failure_count"].sum()) != int(reviews["failure_prediction"].sum()):
        raise AggregationFailure("Failure predictions changed during aggregation")
    if int(signals["negative_sentiment_count"].sum()) != int(reviews["negative_sentiment_indicator"].sum()):
        raise AggregationFailure("Negative sentiment counts changed during aggregation")
    if signals.duplicated(["parent_asin", "review_month"]).any():
        raise AggregationFailure("Duplicate product-month keys")
    return {
        "rows": len(signals),
        "n_reviews_sum": int(signals["n_reviews"].sum()),
        "unique_parent_asin": int(signals["parent_asin"].nunique()),
        "predicted_failure_count_sum": int(signals["predicted_failure_count"].sum()),
        "negative_sentiment_count_sum": int(signals["negative_sentiment_count"].sum()),
    }


def distribution(series: pd.Series) -> dict[str, float]:
    values = series.astype(float)
    return {
        "min": float(values.min()),
        "p05": float(values.quantile(0.05)),
        "p25": float(values.quantile(0.25)),
        "median": float(values.median()),
        "mean": float(values.mean()),
        "p75": float(values.quantile(0.75)),
        "p95": float(values.quantile(0.95)),
        "max": float(values.max()),
        "std": float(values.std(ddof=0)),
    }


def device_component_rows(reviews: pd.DataFrame, signals: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in DEVICE_TYPES:
        subset = reviews.loc[reviews["device_type"] == device]
        monthly = signals.loc[signals["device_type"] == device]
        rows.append(
            {
                "device_type": device,
                "analysis_role": subset["analysis_role"].iloc[0],
                "n_products": int(subset["parent_asin"].nunique()),
                "n_reviews": len(subset),
                "product_months": len(monthly),
                "predicted_failure_share": float(subset["failure_prediction"].mean()),
                "mean_failure_probability": float(subset["failure_probability"].mean()),
                "mean_expected_severity_signal": float(subset["expected_severity_signal"].mean()),
                "mean_expected_persistence_signal": float(subset["expected_persistence_signal"].mean()),
                "mean_sentiment_compound": float(subset["sentiment_compound"].mean()),
                "negative_sentiment_share": float(subset["negative_sentiment_indicator"].mean()),
                "support_interpretation": (
                    "primary longitudinal analysis" if device == "smart_plug"
                    else "exploratory analysis" if device == "smart_bulb"
                    else "INSUFFICIENT_SUPPORT: small-sample case study only"
                ),
            }
        )
    return rows


def protected_paths(root: Path) -> list[Path]:
    paths = [
        root / "data/amazon_reviews_2023/processed/target_products.parquet",
        root / "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/annotation_labels_w5b_v1_0.parquet",
        root / "data/amazon_reviews_2023/processed/annotation_labels_w5c_b_v1_0.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_failure_predictions_w6a_v1_0.parquet",
        root / "data/amazon_reviews_2023/processed/product_month_failure_signals_w6a_v1_0.parquet",
        root / "outputs/models/w5c_b_tfidf_logistic_regression.joblib",
    ]
    paths.extend(sorted((root / "data/amazon_reviews_2023/reports/w5c_b").glob("*")))
    paths.extend(sorted((root / "data/amazon_reviews_2023/reports/w6a").glob("*")))
    return [path for path in paths if path.is_file()]


def identity_map(root: Path, paths: Iterable[Path]) -> dict[str, Any]:
    return {w6a.relative(root, path): w6a.file_identity(root, path) for path in paths}


def environment_payload() -> dict[str, Any]:
    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "pandas_version": pd.__version__,
        "pyarrow_version": pa.__version__,
        "scikit_learn_version": importlib.metadata.version("scikit-learn"),
        "joblib_version": joblib.__version__,
        "numpy_version": np.__version__,
        "vaderSentiment_version": importlib.metadata.version("vaderSentiment"),
    }


def model_evaluation_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
    result = {"model_status": evaluation["model_status"]}
    for split in ("validation", "test"):
        ordinal = evaluation["evaluations"][split]["ordinal"]
        result[split] = {
            "n": ordinal["n"],
            "mae": ordinal["mae"],
            "macro_f1": ordinal["macro_f1"],
            "linear_weighted_kappa": ordinal["linear_weighted_kappa"],
            "quadratic_weighted_kappa": ordinal["quadratic_weighted_kappa"],
        }
    return result


def summary_markdown(status: dict[str, Any]) -> str:
    sev = model_evaluation_summary(status["severity_evaluation"])
    per = model_evaluation_summary(status["persistence_evaluation"])
    device_lines = "\n".join(
        f"| {row['device_type']} | {row['analysis_role']} | {row['n_reviews']} | "
        f"{row['predicted_failure_share']:.4f} | {row['mean_expected_severity_signal']:.4f} | "
        f"{row['mean_expected_persistence_signal']:.4f} | {row['mean_sentiment_compound']:.4f} | "
        f"{row['negative_sentiment_share']:.4f} |"
        for row in status["device_component_summary"]
    )
    return f"""# Phase W6-B Summary

Technical status: **{status['status']}**
W6-C readiness: **{status['w6c_readiness']}**

## Conditional ordinal models

- Human-labeled failure reviews: {status['training_population']['failure_rows']} (train/validation/test: 374/107/122)
- Severity status: {sev['model_status']}
- Severity validation MAE/Macro-F1: {sev['validation']['mae']:.4f} / {sev['validation']['macro_f1']:.4f}
- Severity test MAE/Macro-F1: {sev['test']['mae']:.4f} / {sev['test']['macro_f1']:.4f}
- Persistence status: {per['model_status']}
- Persistence validation MAE/Macro-F1: {per['validation']['mae']:.4f} / {per['validation']['macro_f1']:.4f}
- Persistence test MAE/Macro-F1: {per['test']['mae']:.4f} / {per['test']['macro_f1']:.4f}

Severity level 3 has limited support (20 total; train/validation/test = 13/2/5). Its metrics must not be described as stable.

## Full signal components

- Reviews scored: {status['review_component_validation']['rows']}
- Product-month rows: {status['product_month_validation']['rows']}
- Negative sentiment threshold: VADER compound <= -0.05
- Negative-sentiment reviews: {status['sentiment_summary']['negative_sentiment_count']}

| Device type | Role | Reviews | Failure share | Mean severity signal | Mean persistence signal | Mean sentiment | Negative sentiment share |
|---|---|---:|---:|---:|---:|---:|---:|
{device_lines}

These are separate signal components. No final EngineeringIndex, future quality-deterioration target, product-level temporal Persistence label, or final early-warning comparison was created.
"""


def status_for_exception(error: Exception) -> str:
    if isinstance(error, w6a.SpaceGate):
        return "PAUSED_SPACE_GATE"
    if isinstance(error, InputMismatch):
        return "FAILED_INPUT_MISMATCH"
    if isinstance(error, TrainingFailure):
        return "FAILED_ORDINAL_TRAINING"
    if isinstance(error, AggregationFailure):
        return "FAILED_PRODUCT_MONTH_AGGREGATION"
    return "FAILED_SIGNAL_COMPONENT_INFERENCE"


def main() -> int:
    root = project_root()
    config_path = root / "config/w6b_signal_component_rules.toml"
    config = w6a.load_toml(config_path)
    report_dir = root / config["outputs"]["report_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w6b_execution.log"
    started = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    initial_free = w6a.disk_free_gib(root)
    protected = protected_paths(root)
    protected_before = identity_map(root, protected)
    w6a.log_message(log_path, "W6-B started")
    try:
        w6a.check_space(root, float(config["inference"]["minimum_free_gib"]), "W6-B start")
        inputs = config["inputs"]
        w5_status = w6a.load_json(root / inputs["w5c_b_status"])
        w6a_status = w6a.load_json(root / inputs["w6a_status"])
        if w5_status.get("status") != "PASS" or w6a_status.get("status") != "PASS":
            raise InputMismatch("W5-C-B and W6-A must both be PASS")
        parquet_specs = [
            ("formal_reviews", "formal_reviews_rows", "formal_reviews_sha256"),
            ("frozen_labels", "frozen_labels_rows", "frozen_labels_sha256"),
            ("w6a_review_predictions", "w6a_review_predictions_rows", "w6a_review_predictions_sha256"),
            ("w6a_product_month", "w6a_product_month_rows", "w6a_product_month_sha256"),
        ]
        identities: dict[str, Any] = {}
        for path_key, rows_key, hash_key in parquet_specs:
            identities[path_key] = w6a.validate_parquet_input(
                root, root / inputs[path_key], int(inputs[rows_key]), str(inputs[hash_key])
            )
        identities["frozen_failure_model"] = w6a.validate_file_input(
            root,
            root / inputs["frozen_failure_model"],
            str(inputs["frozen_failure_model_sha256"]),
        )
        identities["w5c_b_status"] = w6a.file_identity(root, root / inputs["w5c_b_status"])
        identities["w6a_status"] = w6a.file_identity(root, root / inputs["w6a_status"])
        identities["config"] = w6a.file_identity(root, config_path)
        package_version = importlib.metadata.version("vaderSentiment")
        if package_version != str(config["sentiment"]["package_version"]):
            raise InputMismatch(
                f"Expected vaderSentiment {config['sentiment']['package_version']}, got {package_version}"
            )
        fingerprint = w6a.stable_fingerprint(
            {
                "phase": config["phase"]["version"],
                "inputs": {key: item["sha256"] for key, item in identities.items()},
                "vaderSentiment": package_version,
            }
        )
        output_paths = [
            root / config["outputs"]["review_components"],
            root / config["outputs"]["product_month_components"],
            root / config["outputs"]["severity_model"],
            root / config["outputs"]["persistence_model"],
        ]
        existing = [path for path in output_paths if path.exists()]
        if existing:
            status_path = report_dir / "w6b_status.json"
            if len(existing) != len(output_paths) or not status_path.is_file():
                raise InputMismatch("Incomplete or unknown W6-B output set already exists")
            old = w6a.load_json(status_path)
            if old.get("status") != "PASS" or old.get("run_fingerprint") != fingerprint:
                raise InputMismatch("Existing W6-B outputs do not match this approved fingerprint")
            for name, path in zip(
                ("review_components", "product_month_components", "severity_model", "persistence_model"),
                output_paths,
            ):
                if w6a.sha256_file(path) != old["outputs"][name]["sha256"]:
                    raise InputMismatch(f"Existing W6-B output hash mismatch: {name}")
            print("W6-B matching PASS outputs already exist; no files were overwritten.")
            return 0

        manifest = {
            "phase": PHASE,
            "started_at_utc": started,
            "project_root": str(root),
            "environment": environment_payload(),
            "inputs": identities,
            "w5c_b_status": w5_status.get("status"),
            "w6a_status": w6a_status.get("status"),
            "run_fingerprint": fingerprint,
            "protected_files_before": protected_before,
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_source_read": False,
            "online_service_used": False,
        }
        w6a.write_json(report_dir / "w6b_input_manifest.json", manifest)
        formal = pq.read_table(
            root / inputs["formal_reviews"],
            columns=[
                "duplicate_key", "parent_asin", "device_type", "source_domain",
                "review_datetime", "review_month", "review_text",
            ],
        ).to_pandas()
        labels = pq.read_table(root / inputs["frozen_labels"]).to_pandas()
        w6a_predictions = pq.read_table(root / inputs["w6a_review_predictions"]).to_pandas()
        joined = validate_and_merge_full_inputs(formal, w6a_predictions)
        failures, training_audit = create_failure_training_frame(labels, formal, config)
        w6a.write_json(report_dir / "training_population_audit.json", training_audit)
        severity_bundle, severity_evaluation = train_ordinal_bundle(
            failures, "severity", config["severity"], config
        )
        persistence_bundle, persistence_evaluation = train_ordinal_bundle(
            failures, "persistence", config["persistence"], config
        )
        severity_model_path = root / config["outputs"]["severity_model"]
        persistence_model_path = root / config["outputs"]["persistence_model"]
        atomic_joblib(severity_model_path, severity_bundle)
        atomic_joblib(persistence_model_path, persistence_bundle)
        reloaded_severity = joblib.load(severity_model_path)
        reloaded_persistence = joblib.load(persistence_model_path)
        original_sev = score_ordinal_bundle(failures["model_text"], severity_bundle)
        reload_sev = score_ordinal_bundle(failures["model_text"], reloaded_severity)
        original_per = score_ordinal_bundle(failures["model_text"], persistence_bundle)
        reload_per = score_ordinal_bundle(failures["model_text"], reloaded_persistence)
        model_reload_audit = {
            "severity_expected_max_abs_diff": float(
                np.max(np.abs(original_sev["expected"] - reload_sev["expected"]))
            ),
            "persistence_expected_max_abs_diff": float(
                np.max(np.abs(original_per["expected"] - reload_per["expected"]))
            ),
            "passed": bool(
                np.array_equal(original_sev["hard_prediction"], reload_sev["hard_prediction"])
                and np.array_equal(original_per["hard_prediction"], reload_per["hard_prediction"])
                and np.allclose(original_sev["expected"], reload_sev["expected"], atol=0, rtol=0)
                and np.allclose(original_per["expected"], reload_per["expected"], atol=0, rtol=0)
            ),
        }
        if not model_reload_audit["passed"]:
            raise TrainingFailure("Reloaded ordinal models do not reproduce in-memory predictions")
        w6a.write_json(report_dir / "severity_model_evaluation.json", severity_evaluation)
        w6a.write_json(report_dir / "persistence_model_evaluation.json", persistence_evaluation)
        w6a.write_json(report_dir / "model_reload_audit.json", model_reload_audit)
        w6a.log_message(log_path, "Conditional Severity and Persistence models trained")
        review_components, inference_audit = build_review_components(
            joined, reloaded_severity, reloaded_persistence, config
        )
        review_validation = validate_review_components(review_components)
        review_output = root / config["outputs"]["review_components"]
        product_output = root / config["outputs"]["product_month_components"]
        w6a.write_parquet_atomic(
            review_output, review_components, REVIEW_SCHEMA, config["inference"]["compression"]
        )
        signals = aggregate_product_month(review_components)
        product_validation = validate_product_month(signals, review_components)
        w6a.write_parquet_atomic(
            product_output, signals, PRODUCT_MONTH_SCHEMA, config["inference"]["compression"]
        )
        if pq.ParquetFile(review_output).metadata.num_rows != 55877:
            raise InferenceFailure("Reloaded review component Parquet row mismatch")
        if pq.ParquetFile(product_output).metadata.num_rows != 1911:
            raise AggregationFailure("Reloaded product-month component Parquet row mismatch")
        sentiment_summary = {
            "method": "VADER",
            "package_version": package_version,
            "offline": True,
            "compound_negative_threshold": float(config["sentiment"]["compound_negative_threshold"]),
            "negative_sentiment_count": int(review_components["negative_sentiment_indicator"].sum()),
            "negative_sentiment_share": float(review_components["negative_sentiment_indicator"].mean()),
            "compound_distribution": distribution(review_components["sentiment_compound"]),
            "sentiment_is_not_engineering_failure_truth": True,
        }
        device_summary = device_component_rows(review_components, signals)
        w6a.write_json(report_dir / "sentiment_baseline_summary.json", sentiment_summary)
        w6a.write_json(
            report_dir / "full_signal_component_summary.json",
            {
                "inference_audit": inference_audit,
                "review_validation": review_validation,
                "product_month_validation": product_validation,
                "severity_signal_distribution": distribution(review_components["expected_severity_signal"]),
                "persistence_signal_distribution": distribution(review_components["expected_persistence_signal"]),
                "sentiment_compound_distribution": sentiment_summary["compound_distribution"],
                "components_are_not_a_final_engineering_index": True,
            },
        )
        w6a.write_csv(
            report_dir / "signal_component_by_device_type.csv",
            device_summary,
            list(device_summary[0]),
        )
        product_rows = []
        for device in DEVICE_TYPES:
            subset = signals.loc[signals["device_type"] == device]
            product_rows.append(
                {
                    "device_type": device,
                    "analysis_role": subset["analysis_role"].iloc[0],
                    "product_months": len(subset),
                    "n_reviews_sum": int(subset["n_reviews"].sum()),
                    "n_reviews_min": int(subset["n_reviews"].min()),
                    "n_reviews_median": float(subset["n_reviews"].median()),
                    "n_reviews_mean": float(subset["n_reviews"].mean()),
                    "n_reviews_max": int(subset["n_reviews"].max()),
                    "mean_failure_probability": float(subset["mean_failure_probability"].mean()),
                    "mean_severity_signal": float(subset["mean_expected_severity_signal"].mean()),
                    "mean_persistence_signal": float(subset["mean_expected_persistence_signal"].mean()),
                    "mean_sentiment_compound": float(subset["mean_sentiment_compound"].mean()),
                    "mean_negative_sentiment_share": float(subset["negative_sentiment_share"].mean()),
                }
            )
        w6a.write_csv(
            report_dir / "product_month_component_summary.csv",
            product_rows,
            list(product_rows[0]),
        )
        final_free = w6a.check_space(
            root, float(config["inference"]["minimum_free_gib"]), "W6-B completion"
        )
        disk_payload = {
            "minimum_free_gib": float(config["inference"]["minimum_free_gib"]),
            "initial_free_gib": initial_free,
            "final_free_gib": final_free,
        }
        w6a.write_json(report_dir / "w6b_disk_usage.json", disk_payload)
        protected_after = identity_map(root, protected)
        if protected_before != protected_after:
            raise InferenceFailure("A protected W3-W6-A input or output changed")
        outputs = {
            "review_components": w6a.parquet_identity(root, review_output),
            "product_month_components": w6a.parquet_identity(root, product_output),
            "severity_model": w6a.file_identity(root, severity_model_path),
            "persistence_model": w6a.file_identity(root, persistence_model_path),
        }
        required_reports = [
            "w6b_execution.log", "w6b_input_manifest.json",
            "training_population_audit.json", "severity_model_evaluation.json",
            "persistence_model_evaluation.json", "model_reload_audit.json",
            "sentiment_baseline_summary.json", "full_signal_component_summary.json",
            "signal_component_by_device_type.csv", "product_month_component_summary.csv",
            "w6b_disk_usage.json", "w6b_summary.md", "w6b_status.json",
        ]
        status = {
            "phase": PHASE,
            "status": "PASS",
            "w6c_readiness": config["phase"]["w6c_readiness"],
            "completed_at_utc": datetime.now(timezone.utc),
            "elapsed_seconds": time.perf_counter() - started_perf,
            "run_fingerprint": fingerprint,
            "environment": environment_payload(),
            "input_validation": {
                "w5c_b_status": w5_status.get("status"),
                "w6a_status": w6a_status.get("status"),
                "all_rows_and_hashes_match": True,
            },
            "training_population": training_audit,
            "severity_evaluation": severity_evaluation,
            "persistence_evaluation": persistence_evaluation,
            "model_reload_audit": model_reload_audit,
            "full_inference_audit": inference_audit,
            "review_component_validation": review_validation,
            "product_month_validation": product_validation,
            "sentiment_summary": sentiment_summary,
            "device_component_summary": device_summary,
            "protected_inputs_unchanged": True,
            "protected_files_after": protected_after,
            "failure_binary_model_modified": False,
            "failure_binary_threshold_modified": False,
            "human_labels_modified": False,
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_sources_read": False,
            "online_api_or_llm_used": False,
            "bert_transformer_lstm_pytorch_embedding_trained": False,
            "product_level_temporal_persistence_label_created": False,
            "final_engineering_index_created": False,
            "future_quality_target_created": False,
            "final_early_warning_comparison_executed": False,
            "next_phase_executed": False,
            "git_commit_created": False,
            "outputs": outputs,
            "required_reports": [w6a.relative(root, report_dir / name) for name in required_reports],
            "remaining_decisions": [
                "EngineeringIndex weights",
                "Future quality-deterioration definition",
                "Prediction horizon",
                "Minimum product-month support rule",
            ],
            "limitations": [
                "Severity level 3 has only 20 labeled examples and is not stably estimated.",
                "Smart plugs are primary, smart bulbs exploratory, and smart switches a case study.",
                "The outputs are separate signal components, not a final EngineeringIndex.",
            ],
        }
        (report_dir / "w6b_summary.md").write_text(
            summary_markdown(status), encoding="utf-8"
        )
        w6a.write_json(report_dir / "w6b_status.json", status)
        w6a.log_message(log_path, "W6-B PASS")
        return 0
    except Exception as error:
        status_name = status_for_exception(error)
        w6a.log_message(log_path, f"{status_name}: {type(error).__name__}: {error}")
        w6a.write_json(
            report_dir / "w6b_status.json",
            {
                "phase": PHASE,
                "status": status_name,
                "w6c_readiness": "NOT_READY",
                "failed_at_utc": datetime.now(timezone.utc),
                "error_type": type(error).__name__,
                "error_message": str(error),
                "raw_jsonl_read": False,
                "metadata_jsonl_read": False,
                "compressed_sources_read": False,
                "online_api_or_llm_used": False,
                "final_engineering_index_created": False,
                "future_quality_target_created": False,
                "next_phase_executed": False,
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
