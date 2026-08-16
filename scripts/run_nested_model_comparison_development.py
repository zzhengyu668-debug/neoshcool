"""Develop the frozen M0-M3 nested signal comparison on Train/Validation only.

This script intentionally never materializes the Test target and never emits Test
predictions.  It is the shared implementation used by both collaborators so that
feature additions, rather than different code paths, drive route comparisons.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from sklearn.impute import SimpleImputer
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
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "nested_model_comparison_rules.toml"


@dataclass(frozen=True)
class DevelopmentData:
    train: pd.DataFrame
    validation: pd.DataFrame
    split_counts: dict[str, int]
    test_key_count: int


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=json_default) + "\n",
        encoding="utf-8",
    )


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def route_features(config: dict[str, Any]) -> dict[str, list[str]]:
    return {name: list(features) for name, features in config["routes"].items()}


def build_pipeline(config: dict[str, Any]) -> Pipeline:
    prep = config["preprocessing"]
    model = config["model"]
    return Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(
                    strategy=str(prep["imputer_strategy"]),
                    add_indicator=bool(prep["imputer_add_indicator"]),
                ),
            ),
            ("scaler", StandardScaler()),
            (
                "logistic_regression",
                LogisticRegression(
                    C=float(model["C"]),
                    penalty=str(model["penalty"]),
                    solver=str(model["solver"]),
                    class_weight=str(model["class_weight"]),
                    max_iter=int(model["max_iter"]),
                    random_state=int(model["random_state"]),
                ),
            ),
        ]
    )


def validate_feature_contract(config: dict[str, Any]) -> dict[str, Any]:
    routes = route_features(config)
    rating = list(config["features"]["rating"])
    sentiment = list(config["features"]["sentiment"])
    engineering = list(config["features"]["engineering"])
    expected = {
        "m0_rating_only": rating,
        "m1_rating_sentiment": rating + sentiment,
        "m2_rating_engineering": rating + engineering,
        "m3_rating_sentiment_engineering": rating + sentiment + engineering,
    }
    forbidden_exact = {"parent_asin", "review_month", "device_type", "review_text"}
    violations: list[str] = []
    if routes != expected:
        violations.append("route feature lists do not match the frozen nested contract")
    for route, features in routes.items():
        if len(features) != len(set(features)):
            violations.append(f"{route}: duplicate feature")
        for feature in features:
            if feature in forbidden_exact or feature.startswith(("target_", "future_", "target_future_")):
                violations.append(f"{route}: forbidden feature {feature}")
    return {"passed": not violations, "violations": violations, "routes": routes}


def load_development_data(config: dict[str, Any]) -> DevelopmentData:
    input_cfg = config["input"]
    sample_cfg = config["sample"]
    panel_path = ROOT / str(input_cfg["analysis_panel"])
    if not panel_path.exists():
        raise FileNotFoundError(panel_path)
    if pq.ParquetFile(panel_path).metadata.num_rows != int(input_cfg["analysis_panel_rows"]):
        raise RuntimeError("analysis panel row count mismatch")
    actual_hash = sha256(panel_path)
    if actual_hash != str(input_cfg["analysis_panel_sha256"]):
        raise RuntimeError(f"analysis panel SHA-256 mismatch: {actual_hash}")

    eligibility = str(input_cfg["eligibility_field"])
    split = str(input_cfg["split_field"])
    target = str(input_cfg["target_field"])
    keys = list(input_cfg["key_fields"])
    routes = route_features(config)
    all_features = list(dict.fromkeys(feature for features in routes.values() for feature in features))
    dataset = ds.dataset(panel_path, format="parquet")

    # Target is deliberately absent from this table.  It is used for the 515-row
    # split audit and to count the 115 sealed Test keys without label access.
    audit = dataset.to_table(columns=keys + [eligibility, split]).to_pandas()
    eligible_audit = audit.loc[audit[eligibility].astype(bool)].copy()
    split_counts = eligible_audit[split].astype(str).value_counts().to_dict()
    expected_counts = {
        "train": int(sample_cfg["train_rows"]),
        "embargo_train_validation": int(sample_cfg["embargo_train_validation_rows"]),
        "validation": int(sample_cfg["validation_rows"]),
        "embargo_validation_test": int(sample_cfg["embargo_validation_test_rows"]),
        "test": int(sample_cfg["sealed_test_rows"]),
    }
    if len(eligible_audit) != int(sample_cfg["eligible_rows"]) or split_counts != expected_counts:
        raise RuntimeError(f"frozen eligible/split counts mismatch: {split_counts}")
    if eligible_audit.duplicated(keys).any():
        raise RuntimeError("eligible product-month keys are not unique")

    # Only Train and Validation rows can materialize the target column.
    development_filter = ds.field(eligibility) & ds.field(split).isin(["train", "validation"])
    columns = keys + [split, target] + all_features
    development = dataset.to_table(columns=columns, filter=development_filter).to_pandas()
    development[split] = development[split].astype(str)
    development = development.sort_values(["review_month", "parent_asin"], kind="stable").reset_index(drop=True)
    train = development.loc[development[split] == "train"].copy()
    validation = development.loc[development[split] == "validation"].copy()

    expected_class_counts = {
        "train": (int(sample_cfg["train_rows"]), int(sample_cfg["train_positive"])),
        "validation": (int(sample_cfg["validation_rows"]), int(sample_cfg["validation_positive"])),
    }
    for name, frame in (("train", train), ("validation", validation)):
        expected_n, expected_positive = expected_class_counts[name]
        if len(frame) != expected_n or int(frame[target].sum()) != expected_positive:
            raise RuntimeError(
                f"{name} sample/class mismatch: n={len(frame)}, positive={int(frame[target].sum())}"
            )
        if frame.duplicated(keys).any():
            raise RuntimeError(f"{name} product-month keys are not unique")
    if set(map(tuple, train[keys].to_numpy())).intersection(set(map(tuple, validation[keys].to_numpy()))):
        raise RuntimeError("Train and Validation keys overlap")
    return DevelopmentData(
        train=train,
        validation=validation,
        split_counts={str(k): int(v) for k, v in split_counts.items()},
        test_key_count=int((eligible_audit[split].astype(str) == "test").sum()),
    )


def calibration_bins(y_true: np.ndarray, probabilities: np.ndarray, n_bins: int) -> list[dict[str, Any]]:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    assignments = np.minimum(np.digitize(probabilities, edges[1:-1], right=False), n_bins - 1)
    rows: list[dict[str, Any]] = []
    for bin_id in range(n_bins):
        mask = assignments == bin_id
        count = int(mask.sum())
        rows.append(
            {
                "bin_id": bin_id,
                "lower": float(edges[bin_id]),
                "upper": float(edges[bin_id + 1]),
                "count": count,
                "mean_probability": float(probabilities[mask].mean()) if count else None,
                "event_rate": float(y_true[mask].mean()) if count else None,
            }
        )
    return rows


def evaluate(y_true: np.ndarray, probabilities: np.ndarray, threshold: float, n_bins: int) -> dict[str, Any]:
    predictions = (probabilities >= threshold).astype(np.int8)
    tn, fp, fn, tp = confusion_matrix(y_true, predictions, labels=[0, 1]).ravel()
    recall = recall_score(y_true, predictions, zero_division=0)
    specificity = tn / (tn + fp) if (tn + fp) else math.nan
    bins = calibration_bins(y_true, probabilities, n_bins)
    ece = sum(
        row["count"] / len(y_true) * abs(row["event_rate"] - row["mean_probability"])
        for row in bins
        if row["count"]
    )
    result = {
        "n": int(len(y_true)),
        "positive_count": int(y_true.sum()),
        "negative_count": int(len(y_true) - y_true.sum()),
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "tp": int(tp),
        "accuracy": float(accuracy_score(y_true, predictions)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, predictions)),
        "precision": float(precision_score(y_true, predictions, zero_division=0)),
        "recall": float(recall),
        "f1": float(f1_score(y_true, predictions, zero_division=0)),
        "specificity": float(specificity),
        "roc_auc": float(roc_auc_score(y_true, probabilities)),
        "pr_auc_average_precision": float(average_precision_score(y_true, probabilities)),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "ece_10_uniform": float(ece),
        "decision_threshold": float(threshold),
        "calibration_bins": bins,
    }
    # Independent arithmetic audit of the threshold metrics.
    manual = {
        "accuracy": (tp + tn) / len(y_true),
        "precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "specificity": specificity,
    }
    manual["balanced_accuracy"] = (manual["recall"] + manual["specificity"]) / 2
    denom = manual["precision"] + manual["recall"]
    manual["f1"] = 2 * manual["precision"] * manual["recall"] / denom if denom else 0.0
    max_error = max(abs(float(result[key]) - float(value)) for key, value in manual.items())
    if max_error > 1e-12:
        raise RuntimeError(f"manual metric audit failed: {max_error}")
    result["manual_metric_max_abs_error"] = float(max_error)
    return result


def pairwise_bootstrap(
    validation: pd.DataFrame,
    probabilities: dict[str, np.ndarray],
    target: str,
    comparisons: list[tuple[str, str]],
    replicates: int,
    random_state: int,
    threshold: float,
) -> pd.DataFrame:
    products = np.array(sorted(validation["parent_asin"].astype(str).unique()))
    product_indices = {
        product: np.flatnonzero(validation["parent_asin"].astype(str).to_numpy() == product)
        for product in products
    }
    y = validation[target].astype(int).to_numpy()
    rng = np.random.default_rng(random_state)
    samples: dict[tuple[str, str, str], list[float]] = {}
    metrics = ("pr_auc_average_precision", "brier_score", "recall", "f1")
    for _ in range(replicates):
        chosen = rng.choice(products, size=len(products), replace=True)
        indices = np.concatenate([product_indices[str(product)] for product in chosen])
        y_sample = y[indices]
        if len(np.unique(y_sample)) < 2:
            continue
        route_values: dict[str, dict[str, float]] = {}
        for route, route_probability in probabilities.items():
            probability = route_probability[indices]
            prediction = (probability >= threshold).astype(np.int8)
            route_values[route] = {
                "pr_auc_average_precision": float(average_precision_score(y_sample, probability)),
                "brier_score": float(brier_score_loss(y_sample, probability)),
                "recall": float(recall_score(y_sample, prediction, zero_division=0)),
                "f1": float(f1_score(y_sample, prediction, zero_division=0)),
            }
        for baseline, augmented in comparisons:
            for metric in metrics:
                key = (baseline, augmented, metric)
                samples.setdefault(key, []).append(route_values[augmented][metric] - route_values[baseline][metric])
    rows: list[dict[str, Any]] = []
    for (baseline, augmented, metric), values in samples.items():
        array = np.asarray(values, dtype=float)
        rows.append(
            {
                "comparison": f"{augmented}-minus-{baseline}",
                "baseline": baseline,
                "augmented": augmented,
                "metric": metric,
                "delta_definition": "augmented_minus_baseline",
                "favorable_direction": "negative" if metric == "brier_score" else "positive",
                "valid_replicates": int(len(array)),
                "bootstrap_mean_delta": float(array.mean()),
                "ci95_lower": float(np.quantile(array, 0.025)),
                "ci95_upper": float(np.quantile(array, 0.975)),
                "interval_crosses_zero": bool(np.quantile(array, 0.025) <= 0 <= np.quantile(array, 0.975)),
            }
        )
    return pd.DataFrame(rows)


def package_versions() -> dict[str, str | None]:
    names = ["numpy", "pandas", "pyarrow", "scikit-learn", "joblib"]
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def run(config_path: Path, executor: str, output_dir: Path | None, bootstrap_replicates: int | None) -> Path:
    config = load_config(config_path)
    contract = validate_feature_contract(config)
    if not contract["passed"]:
        raise RuntimeError(f"feature contract failed: {contract['violations']}")
    data = load_development_data(config)
    input_cfg = config["input"]
    target = str(input_cfg["target_field"])
    split = str(input_cfg["split_field"])
    keys = list(input_cfg["key_fields"])
    threshold = float(config["model"]["decision_threshold"])
    n_bins = int(config["evaluation"]["calibration_bins"])
    replicates = int(bootstrap_replicates or config["evaluation"]["bootstrap_replicates"])
    executor_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", executor).strip("._")
    if not executor_slug:
        raise ValueError("executor must contain at least one safe character")
    out = output_dir or ROOT / "outputs" / "nested_model_comparison" / executor_slug / "development"
    out.mkdir(parents=True, exist_ok=True)
    model_dir = out / "models"
    model_dir.mkdir(exist_ok=True)

    metrics: dict[str, dict[str, Any]] = {}
    probabilities: dict[str, np.ndarray] = {}
    prediction_frames: list[pd.DataFrame] = []
    model_manifest: list[dict[str, Any]] = []
    y_train = data.train[target].astype(int).to_numpy()
    y_validation = data.validation[target].astype(int).to_numpy()
    for route, features in contract["routes"].items():
        pipeline = build_pipeline(config)
        pipeline.fit(data.train[features], y_train)
        probability = pipeline.predict_proba(data.validation[features])[:, 1]
        prediction = (probability >= threshold).astype(np.int8)
        probabilities[route] = probability
        metrics[route] = evaluate(y_validation, probability, threshold, n_bins)
        prediction_frame = data.validation[keys + [split, target]].copy()
        prediction_frame = prediction_frame.rename(columns={target: "y_true", split: "split"})
        prediction_frame.insert(0, "route", route)
        prediction_frame["y_probability"] = probability
        prediction_frame["y_pred"] = prediction
        prediction_frames.append(prediction_frame)
        model_path = model_dir / f"{route}_train_fitted.joblib"
        joblib.dump(
            {
                "pipeline": pipeline,
                "route": route,
                "features": features,
                "target": target,
                "decision_threshold": threshold,
                "training_scope": "train_only",
                "status": config["phase"]["status_label"],
            },
            model_path,
        )
        model_manifest.append(
            {
                "route": route,
                "path": model_path.relative_to(ROOT).as_posix() if model_path.is_relative_to(ROOT) else str(model_path),
                "features": features,
                "bytes": model_path.stat().st_size,
                "sha256": sha256(model_path),
            }
        )

    predictions = pd.concat(prediction_frames, ignore_index=True)
    predictions.to_parquet(out / "validation_predictions.parquet", index=False, compression="zstd")
    metric_rows = [
        {key: value for key, value in {"route": route, **route_metrics}.items() if key != "calibration_bins"}
        for route, route_metrics in metrics.items()
    ]
    pd.DataFrame(metric_rows).to_csv(out / "validation_metrics.csv", index=False)
    write_json(out / "validation_metrics.json", metrics)

    comparisons = [
        ("m0_rating_only", "m1_rating_sentiment"),
        ("m0_rating_only", "m2_rating_engineering"),
        ("m1_rating_sentiment", "m3_rating_sentiment_engineering"),
        ("m2_rating_engineering", "m3_rating_sentiment_engineering"),
    ]
    point_rows: list[dict[str, Any]] = []
    for baseline, augmented in comparisons:
        for metric in ("pr_auc_average_precision", "brier_score", "recall", "f1"):
            point_rows.append(
                {
                    "comparison": f"{augmented}-minus-{baseline}",
                    "metric": metric,
                    "delta": metrics[augmented][metric] - metrics[baseline][metric],
                    "favorable_direction": "negative" if metric == "brier_score" else "positive",
                }
            )
    pd.DataFrame(point_rows).to_csv(out / "validation_pairwise_differences.csv", index=False)
    bootstrap = pairwise_bootstrap(
        data.validation,
        probabilities,
        target,
        comparisons,
        replicates,
        int(config["evaluation"]["bootstrap_random_state"]),
        threshold,
    )
    bootstrap.to_csv(out / "validation_bootstrap_intervals.csv", index=False)

    config_hash = sha256(config_path)
    code_hash = sha256(Path(__file__).resolve())
    input_path = ROOT / str(input_cfg["analysis_panel"])
    now = datetime.now(timezone.utc).isoformat()
    write_json(
        out / "feature_contract.json",
        {
            "status": config["phase"]["status_label"],
            "routes": contract["routes"],
            "forbidden_prefixes": ["target_", "future_", "target_future_"],
            "common_sample_required": True,
            "route_specific_hyperparameters_allowed": False,
        },
    )
    write_json(
        out / "model_manifest.json",
        {
            "generated_at_utc": now,
            "executor": executor_slug,
            "models": model_manifest,
            "pipeline": {
                "steps": ["median_imputer_with_indicator", "standard_scaler", "balanced_logistic_regression"],
                "parameters": config["model"],
            },
        },
    )
    write_json(
        out / "input_manifest.json",
        {
            "analysis_panel": str(input_cfg["analysis_panel"]),
            "rows": int(input_cfg["analysis_panel_rows"]),
            "sha256": sha256(input_path),
            "config": config_path.relative_to(ROOT).as_posix() if config_path.is_relative_to(ROOT) else str(config_path),
            "config_sha256": config_hash,
            "code_sha256": code_hash,
            "python": platform.python_version(),
            "packages": package_versions(),
        },
    )
    write_json(
        out / "common_sample_audit.json",
        {
            "eligible_rows": sum(data.split_counts.values()),
            "split_counts": data.split_counts,
            "train_rows": len(data.train),
            "validation_rows": len(data.validation),
            "all_routes_use_same_train_and_validation_keys": True,
        },
    )
    write_json(
        out / "leakage_audit.json",
        {
            "passed": True,
            "feature_contract_violations": contract["violations"],
            "target_in_features": False,
            "future_fields_in_features": False,
            "product_identity_in_features": False,
            "preprocessing_fit_scope": "train_only",
            "validation_fit_access": False,
        },
    )
    write_json(
        out / "sealed_test_audit.json",
        {
            "passed": True,
            "test_key_count": data.test_key_count,
            "test_target_materialized": False,
            "test_target_distribution_computed": False,
            "test_predictions_generated": False,
            "test_metrics_computed": False,
            "test_used_for_feature_or_parameter_selection": False,
            "test_used_for_preprocessing_or_model_fit": False,
            "next_step": "explicit group approval required after Validation routes are frozen",
        },
    )
    status = {
        "status": "PASS_TRAIN_VALIDATION_DEVELOPMENT",
        "experiment_label": config["phase"]["status_label"],
        "executor": executor_slug,
        "generated_at_utc": now,
        "test_evaluation_status": "SEALED_NOT_EVALUATED",
        "technical_success_does_not_require_engineering_improvement": True,
        "bootstrap_replicates": replicates,
    }
    write_json(out / "development_status.json", status)
    summary_lines = [
        "# Nested M0-M3 development summary",
        "",
        f"- Status: `{status['status']}`",
        f"- Experiment label: `{status['experiment_label']}`",
        f"- Executor: `{executor_slug}`",
        f"- Train/Validation: {len(data.train)}/{len(data.validation)}",
        f"- Sealed Test keys: {data.test_key_count}; Test target and predictions were not accessed.",
        "- All routes use the same rows, estimator parameters, threshold, and metric implementation.",
        "- Improvement is a research hypothesis, not a technical acceptance criterion.",
        "",
        "## Validation metrics",
        "",
    ]
    for route, route_metrics in metrics.items():
        summary_lines.append(
            f"- {route}: PR-AUC={route_metrics['pr_auc_average_precision']:.4f}, "
            f"Brier={route_metrics['brier_score']:.4f}, Recall={route_metrics['recall']:.4f}, "
            f"F1={route_metrics['f1']:.4f}"
        )
    (out / "execution_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", required=True, help="Name used only for the output directory and manifest")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bootstrap-replicates", type=int, help="Testing override; publication default is 1000")
    args = parser.parse_args()
    try:
        output = run(args.config.resolve(), args.executor, args.output_dir.resolve() if args.output_dir else None, args.bootstrap_replicates)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: outputs written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
