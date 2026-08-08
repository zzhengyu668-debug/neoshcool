from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
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


PHASE = "W6-A"
STAR_HEADER_RE = re.compile(
    r"^\s*(?:one|two|three|four|five)\s+stars?"
    r"\s*(?:[.!:;\-–—]+\s*)?(?:(?:\r?\n)+|$)",
    flags=re.IGNORECASE,
)
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
FORBIDDEN_REVIEW_OUTPUT_FIELDS = {
    "review_text",
    "review_title",
    "review_body",
    "user_id",
    "user_id_hash",
    "product_title",
    "severity",
    "persistence",
    "sentiment",
    "future_quality_target",
}
REVIEW_OUTPUT_COLUMNS = [
    "duplicate_key",
    "parent_asin",
    "device_type",
    "source_domain",
    "review_datetime",
    "review_month",
    "failure_probability",
    "failure_prediction",
    "model_version",
    "product_filter_version",
    "analysis_role",
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
    "median_failure_probability",
    "max_failure_probability",
    "model_version",
    "product_filter_version",
]
REVIEW_SCHEMA = pa.schema(
    [
        pa.field("duplicate_key", pa.string(), nullable=False),
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("device_type", pa.string(), nullable=False),
        pa.field("source_domain", pa.string(), nullable=False),
        pa.field("review_datetime", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("review_month", pa.date32(), nullable=False),
        pa.field("failure_probability", pa.float64(), nullable=False),
        pa.field("failure_prediction", pa.int8(), nullable=False),
        pa.field("model_version", pa.string(), nullable=False),
        pa.field("product_filter_version", pa.string(), nullable=False),
        pa.field("analysis_role", pa.string(), nullable=False),
    ]
)
PRODUCT_MONTH_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("review_month", pa.date32(), nullable=False),
        pa.field("device_type", pa.string(), nullable=False),
        pa.field("analysis_role", pa.string(), nullable=False),
        pa.field("n_reviews", pa.int64(), nullable=False),
        pa.field("predicted_failure_count", pa.int64(), nullable=False),
        pa.field("predicted_failure_share", pa.float64(), nullable=False),
        pa.field("mean_failure_probability", pa.float64(), nullable=False),
        pa.field("median_failure_probability", pa.float64(), nullable=False),
        pa.field("max_failure_probability", pa.float64(), nullable=False),
        pa.field("model_version", pa.string(), nullable=False),
        pa.field("product_filter_version", pa.string(), nullable=False),
    ]
)


class W6AError(RuntimeError):
    """Controlled W6-A failure."""


class InputMismatch(W6AError):
    """A frozen input differs from its approved identity."""


class ModelReproductionError(W6AError):
    """The frozen model does not reproduce W5-C-B predictions."""


class FullInferenceError(W6AError):
    """Full-corpus inference or review-level validation failed."""


class ProductMonthAggregationError(W6AError):
    """Product-month aggregation or validation failed."""


class SpaceGate(W6AError):
    """The configured free-space floor was not met."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W6AError(f"Could not resolve project root from {__file__}")
    if not (root / "config" / "project.toml").is_file():
        raise W6AError("config/project.toml is missing")
    return root


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "path": relative(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }
    if include_hash:
        result["sha256"] = sha256_file(path)
    return result


def parquet_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    result = file_identity(root, path, include_hash=include_hash)
    result.update(
        {
            "rows": parquet.metadata.num_rows,
            "fields": parquet.schema_arrow.names,
            "field_count": len(parquet.schema_arrow.names),
            "compression": sorted(
                {
                    parquet.metadata.row_group(group).column(column).compression
                    for group in range(parquet.metadata.num_row_groups)
                    for column in range(parquet.metadata.num_columns)
                }
            ),
        }
    )
    return result


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def log_message(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def check_space(root: Path, minimum_free_gib: float, stage: str) -> float:
    free = disk_free_gib(root)
    if free < minimum_free_gib:
        raise SpaceGate(
            f"{stage}: free space {free:.3f} GiB is below {minimum_free_gib:.3f} GiB"
        )
    return free


def preprocess_model_text(text: Any) -> tuple[str, int]:
    clean = "" if text is None or pd.isna(text) else str(text)
    return STAR_HEADER_RE.subn("", clean, count=1)


def score_texts(
    texts: Sequence[str], model_bundle: dict[str, Any], threshold: float
) -> tuple[np.ndarray, np.ndarray]:
    vectorizer = model_bundle["vectorizer"]
    classifier = model_bundle["classifier"]
    vocabulary_size_before = len(vectorizer.vocabulary_)
    coefficients_before = classifier.coef_.copy()
    matrix = vectorizer.transform(texts)
    classes = list(classifier.classes_)
    if 1 not in classes:
        raise FullInferenceError("Frozen classifier has no positive class 1")
    positive_class_index = classes.index(1)
    probabilities = classifier.predict_proba(matrix)[:, positive_class_index].astype(float)
    predictions = (probabilities >= threshold).astype(np.int8)
    if len(vectorizer.vocabulary_) != vocabulary_size_before:
        raise FullInferenceError("Vectorizer vocabulary changed during transform")
    if not np.array_equal(classifier.coef_, coefficients_before):
        raise FullInferenceError("Classifier coefficients changed during inference")
    return probabilities, predictions


def stable_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=json_default
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_parquet_input(
    root: Path, path: Path, expected_rows: int, expected_sha256: str
) -> dict[str, Any]:
    if not path.is_file():
        raise InputMismatch(f"Missing input: {relative(root, path)}")
    identity = parquet_identity(root, path)
    if identity["rows"] != expected_rows:
        raise InputMismatch(
            f"{identity['path']}: expected {expected_rows} rows, got {identity['rows']}"
        )
    if identity["sha256"] != expected_sha256:
        raise InputMismatch(f"{identity['path']}: SHA-256 mismatch")
    return identity


def validate_file_input(root: Path, path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise InputMismatch(f"Missing input: {relative(root, path)}")
    identity = file_identity(root, path)
    if identity["sha256"] != expected_sha256:
        raise InputMismatch(f"{identity['path']}: SHA-256 mismatch")
    return identity


def validate_model_bundle(bundle: Any, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise InputMismatch("Frozen model must be a dictionary bundle")
    required = {
        "vectorizer",
        "classifier",
        "decision_threshold",
        "feature_field",
        "label_version",
        "training_rows",
    }
    missing = sorted(required - set(bundle))
    if missing:
        raise InputMismatch(f"Frozen model bundle missing keys: {missing}")
    model_cfg = config["model"]
    checks = {
        "decision_threshold": float(bundle["decision_threshold"])
        == float(model_cfg["decision_threshold"]),
        "feature_field": bundle["feature_field"] == model_cfg["feature_field"],
        "label_version": bundle["label_version"] == model_cfg["label_version"],
        "positive_class_present": int(model_cfg["positive_class"])
        in list(bundle["classifier"].classes_),
        "vocabulary_frozen": hasattr(bundle["vectorizer"], "vocabulary_"),
        "classifier_fitted": hasattr(bundle["classifier"], "coef_"),
    }
    if not all(checks.values()):
        raise InputMismatch(f"Frozen model metadata mismatch: {checks}")
    return {
        **checks,
        "bundle_phase": bundle.get("phase"),
        "training_rows": int(bundle["training_rows"]),
        "vocabulary_size": len(bundle["vectorizer"].vocabulary_),
        "classes": [int(value) for value in bundle["classifier"].classes_],
    }


def collect_labeled_review_texts(
    formal_reviews_path: Path, labeled_duplicate_keys: set[str], batch_size: int
) -> pd.DataFrame:
    found: list[pd.DataFrame] = []
    parquet = pq.ParquetFile(formal_reviews_path)
    for batch in parquet.iter_batches(
        batch_size=batch_size, columns=["duplicate_key", "review_text"]
    ):
        frame = batch.to_pandas()
        selected = frame.loc[frame["duplicate_key"].isin(labeled_duplicate_keys)]
        if not selected.empty:
            found.append(selected.copy())
    if not found:
        return pd.DataFrame(columns=["duplicate_key", "review_text"])
    return pd.concat(found, ignore_index=True)


def reproduction_comparison(
    regenerated_prediction: Sequence[int],
    regenerated_probability: Sequence[float],
    frozen_prediction: Sequence[int],
    frozen_probability: Sequence[float],
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    left_pred = np.asarray(regenerated_prediction, dtype=int)
    right_pred = np.asarray(frozen_prediction, dtype=int)
    left_prob = np.asarray(regenerated_probability, dtype=float)
    right_prob = np.asarray(frozen_probability, dtype=float)
    if not (
        len(left_pred) == len(right_pred) == len(left_prob) == len(right_prob)
    ):
        raise ModelReproductionError("Reproduction arrays have unequal lengths")
    pred_mismatch = left_pred != right_pred
    prob_close = np.isclose(left_prob, right_prob, atol=atol, rtol=rtol)
    absolute = np.abs(left_prob - right_prob)
    return {
        "rows": int(len(left_pred)),
        "prediction_mismatch_count": int(pred_mismatch.sum()),
        "probability_mismatch_count": int((~prob_close).sum()),
        "max_probability_absolute_difference": float(absolute.max(initial=0.0)),
        "mean_probability_absolute_difference": float(absolute.mean())
        if len(absolute)
        else 0.0,
        "probability_atol": float(atol),
        "probability_rtol": float(rtol),
        "passed": bool(not pred_mismatch.any() and prob_close.all()),
    }


def reproduce_w5c_b_predictions(
    root: Path, config: dict[str, Any], model_bundle: dict[str, Any]
) -> dict[str, Any]:
    started = time.perf_counter()
    inputs = config["inputs"]
    labels = pq.read_table(
        root / inputs["frozen_labels"],
        columns=["blind_review_id", "duplicate_key", "label_status"],
    ).to_pandas()
    definite = labels.loc[labels["label_status"] == "definite"].copy()
    frozen = pq.read_table(
        root / inputs["w5c_b_baseline_predictions"],
        columns=["blind_review_id", "tfidf_prediction", "tfidf_probability"],
    ).to_pandas()
    if len(definite) != 1454 or len(frozen) != 1454:
        raise ModelReproductionError(
            f"Expected 1,454 definite and frozen prediction rows; got {len(definite)} and {len(frozen)}"
        )
    texts = collect_labeled_review_texts(
        root / inputs["formal_reviews"],
        set(definite["duplicate_key"].astype(str)),
        int(config["inference"]["batch_size"]),
    )
    if len(texts) != 1454 or not texts["duplicate_key"].is_unique:
        raise ModelReproductionError(
            f"Expected 1,454 unique labeled review texts, got {len(texts)}"
        )
    audit = definite.merge(texts, on="duplicate_key", validate="one_to_one")
    audit = audit.merge(frozen, on="blind_review_id", validate="one_to_one")
    if len(audit) != 1454:
        raise ModelReproductionError(f"Reproduction join produced {len(audit)} rows")
    model_texts: list[str] = []
    star_headers_removed = 0
    for value in audit["review_text"]:
        model_text, substitutions = preprocess_model_text(value)
        model_texts.append(model_text)
        star_headers_removed += substitutions
    probabilities, predictions = score_texts(
        model_texts, model_bundle, float(config["model"]["decision_threshold"])
    )
    comparison = reproduction_comparison(
        predictions,
        probabilities,
        audit["tfidf_prediction"],
        audit["tfidf_probability"],
        float(config["model"]["probability_atol"]),
        float(config["model"]["probability_rtol"]),
    )
    comparison.update(
        {
            "audit_source": "1,454 frozen definite labels joined to formal reviews by duplicate_key",
            "frozen_prediction_source": relative(
                root, root / inputs["w5c_b_baseline_predictions"]
            ),
            "model_text_preprocessing": config["model"]["preprocessing_version"],
            "leading_star_headers_removed": int(star_headers_removed),
            "elapsed_seconds": time.perf_counter() - started,
            "row_level_identifiers_reported": False,
            "review_text_reported": False,
        }
    )
    if not comparison["passed"]:
        raise ModelReproductionError(
            "Frozen W5-C-B predictions were not reproduced exactly within tolerance"
        )
    return comparison


def checkpoint_payload(
    fingerprint: str, source_rows: int, batch_size: int, chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "phase": PHASE,
        "fingerprint": fingerprint,
        "source_rows": source_rows,
        "batch_size": batch_size,
        "chunks": chunks,
        "completed_rows": int(sum(int(item["rows"]) for item in chunks)),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def write_parquet_atomic(
    path: Path, frame: pd.DataFrame, schema: pa.Schema, compression: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    pq.write_table(table, temporary, compression=compression)
    temporary.replace(path)


def run_chunked_inference(
    root: Path,
    config: dict[str, Any],
    model_bundle: dict[str, Any],
    fingerprint: str,
    log_path: Path,
) -> tuple[list[Path], dict[str, Any]]:
    formal_reviews_path = root / config["inputs"]["formal_reviews"]
    batch_size = int(config["inference"]["batch_size"])
    compression = str(config["inference"]["compression"])
    checkpoint_root = root / config["inference"]["checkpoint_dir"] / fingerprint[:16]
    chunks_dir = checkpoint_root / "chunks"
    manifest_path = checkpoint_root / "checkpoint.json"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        existing_manifest = load_json(manifest_path)
        if existing_manifest.get("fingerprint") != fingerprint:
            raise InputMismatch("W6-A checkpoint fingerprint mismatch")
    elif any(checkpoint_root.iterdir()):
        unexpected = [p.name for p in checkpoint_root.iterdir() if p.name != "chunks"]
        if unexpected or any(chunks_dir.iterdir()):
            raise InputMismatch(
                f"Unknown files exist in W6-A checkpoint directory: {checkpoint_root}"
            )

    existing_by_index = {
        int(item["batch_index"]): item
        for item in (existing_manifest or {}).get("chunks", [])
    }
    chunks: list[dict[str, Any]] = []
    chunk_paths: list[Path] = []
    reused_chunks = 0
    newly_scored_chunks = 0
    started = time.perf_counter()
    columns = [
        "duplicate_key",
        "parent_asin",
        "device_type",
        "source_domain",
        "review_datetime",
        "review_month",
        "review_text",
    ]
    parquet = pq.ParquetFile(formal_reviews_path)
    role_map = config["analysis_roles"]
    threshold = float(config["model"]["decision_threshold"])
    model_version = str(config["model"]["model_version"])
    product_version = str(
        config["product_version"]["dataset_product_filter_version"]
    )
    minimum_free = float(config["inference"]["minimum_free_gib"])

    for batch_index, batch in enumerate(
        parquet.iter_batches(batch_size=batch_size, columns=columns)
    ):
        row_count = batch.num_rows
        chunk_path = chunks_dir / f"chunk_{batch_index:05d}.parquet"
        old = existing_by_index.get(batch_index)
        if old and chunk_path.is_file():
            identity = parquet_identity(root, chunk_path)
            if (
                identity["rows"] == row_count
                and identity["sha256"] == old.get("sha256")
                and identity["fields"] == REVIEW_OUTPUT_COLUMNS
            ):
                chunks.append(old)
                chunk_paths.append(chunk_path)
                reused_chunks += 1
                continue
            raise InputMismatch(f"Checkpoint chunk failed validation: {chunk_path}")

        check_space(root, minimum_free, f"before inference batch {batch_index}")
        frame = batch.to_pandas()
        model_texts: list[str] = []
        star_headers_removed = 0
        for value in frame["review_text"]:
            model_text, substitutions = preprocess_model_text(value)
            model_texts.append(model_text)
            star_headers_removed += substitutions
        probabilities, predictions = score_texts(model_texts, model_bundle, threshold)
        roles = frame["device_type"].map(role_map)
        if roles.isna().any():
            bad = sorted(frame.loc[roles.isna(), "device_type"].astype(str).unique())
            raise FullInferenceError(f"Unknown device_type values: {bad}")
        output = pd.DataFrame(
            {
                "duplicate_key": frame["duplicate_key"].astype(str),
                "parent_asin": frame["parent_asin"].astype(str),
                "device_type": frame["device_type"].astype(str),
                "source_domain": frame["source_domain"].astype(str),
                "review_datetime": pd.to_datetime(frame["review_datetime"], utc=True),
                "review_month": frame["review_month"],
                "failure_probability": probabilities,
                "failure_prediction": predictions,
                "model_version": model_version,
                "product_filter_version": product_version,
                "analysis_role": roles.astype(str),
            }
        )
        write_parquet_atomic(chunk_path, output, REVIEW_SCHEMA, compression)
        identity = parquet_identity(root, chunk_path)
        item = {
            "batch_index": batch_index,
            "rows": row_count,
            "sha256": identity["sha256"],
            "size_bytes": identity["size_bytes"],
            "leading_star_headers_removed": int(star_headers_removed),
        }
        chunks.append(item)
        chunk_paths.append(chunk_path)
        newly_scored_chunks += 1
        write_json(
            manifest_path,
            checkpoint_payload(
                fingerprint, parquet.metadata.num_rows, batch_size, chunks
            ),
        )
        log_message(
            log_path,
            f"Completed inference batch {batch_index}: rows={row_count}",
        )

    if sum(int(item["rows"]) for item in chunks) != parquet.metadata.num_rows:
        raise FullInferenceError("Checkpoint row total does not match formal review rows")
    write_json(
        manifest_path,
        checkpoint_payload(fingerprint, parquet.metadata.num_rows, batch_size, chunks),
    )
    return chunk_paths, {
        "checkpoint_fingerprint": fingerprint,
        "checkpoint_path": relative(root, manifest_path),
        "chunk_count": len(chunks),
        "reused_chunks": reused_chunks,
        "newly_scored_chunks": newly_scored_chunks,
        "rows": int(sum(int(item["rows"]) for item in chunks)),
        "leading_star_headers_removed": int(
            sum(int(item.get("leading_star_headers_removed", 0)) for item in chunks)
        ),
        "elapsed_seconds": time.perf_counter() - started,
    }


def combine_chunks(
    root: Path,
    chunk_paths: Sequence[Path],
    output_path: Path,
    compression: str,
    minimum_free_gib: float,
) -> None:
    check_space(root, minimum_free_gib, "before final review prediction Parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    writer = pq.ParquetWriter(temporary, REVIEW_SCHEMA, compression=compression)
    try:
        for path in chunk_paths:
            table = pq.read_table(path)
            if table.schema != REVIEW_SCHEMA:
                table = table.cast(REVIEW_SCHEMA)
            writer.write_table(table)
    finally:
        writer.close()
    temporary.replace(output_path)


def validate_review_predictions(
    path: Path, expected_rows: int, allowed_parents: set[str]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    table = pq.read_table(path)
    if table.num_rows != expected_rows:
        raise FullInferenceError(
            f"Expected {expected_rows} prediction rows, got {table.num_rows}"
        )
    if table.schema.names != REVIEW_OUTPUT_COLUMNS:
        raise FullInferenceError("Review prediction schema/order mismatch")
    forbidden = sorted(set(table.schema.names) & FORBIDDEN_REVIEW_OUTPUT_FIELDS)
    if forbidden:
        raise FullInferenceError(f"Forbidden output fields present: {forbidden}")
    frame = table.to_pandas()
    if frame["duplicate_key"].isna().any() or not frame["duplicate_key"].is_unique:
        raise FullInferenceError("Review prediction duplicate_key must be non-null and unique")
    unknown_parents = set(frame["parent_asin"].astype(str)) - allowed_parents
    if unknown_parents:
        raise FullInferenceError(
            f"Review predictions include {len(unknown_parents)} non-target parent_asin"
        )
    if not frame["failure_probability"].between(0.0, 1.0, inclusive="both").all():
        raise FullInferenceError("failure_probability falls outside [0, 1]")
    expected_prediction = (frame["failure_probability"] >= 0.5).astype(np.int8)
    if not np.array_equal(expected_prediction, frame["failure_prediction"].astype(np.int8)):
        raise FullInferenceError("failure_prediction is inconsistent with the 0.5 threshold")
    if set(frame["device_type"]) != set(DEVICE_TYPES):
        raise FullInferenceError("Review prediction device types are incomplete")
    return frame, {
        "rows": len(frame),
        "unique_duplicate_keys": int(frame["duplicate_key"].nunique()),
        "unique_parent_asin": int(frame["parent_asin"].nunique()),
        "unknown_parent_asin": 0,
        "forbidden_fields_present": [],
        "probabilities_in_unit_interval": True,
        "fixed_threshold_validated": True,
    }


def aggregate_product_month(predictions: pd.DataFrame) -> pd.DataFrame:
    grouped = predictions.groupby(
        [
            "parent_asin",
            "review_month",
            "device_type",
            "analysis_role",
            "model_version",
            "product_filter_version",
        ],
        sort=True,
        dropna=False,
        observed=True,
    )
    result = grouped.agg(
        n_reviews=("duplicate_key", "size"),
        predicted_failure_count=("failure_prediction", "sum"),
        predicted_failure_share=("failure_prediction", "mean"),
        mean_failure_probability=("failure_probability", "mean"),
        median_failure_probability=("failure_probability", "median"),
        max_failure_probability=("failure_probability", "max"),
    ).reset_index()
    result = result[PRODUCT_MONTH_COLUMNS].sort_values(
        ["parent_asin", "review_month"], kind="stable"
    )
    result["n_reviews"] = result["n_reviews"].astype("int64")
    result["predicted_failure_count"] = result[
        "predicted_failure_count"
    ].astype("int64")
    return result.reset_index(drop=True)


def validate_product_month(
    signals: pd.DataFrame, predictions: pd.DataFrame
) -> dict[str, Any]:
    if signals.empty:
        raise ProductMonthAggregationError("Product-month signal table is empty")
    if int(signals["n_reviews"].sum()) != len(predictions):
        raise ProductMonthAggregationError(
            "Product-month n_reviews do not sum to review prediction rows"
        )
    if int(signals["predicted_failure_count"].sum()) != int(
        predictions["failure_prediction"].sum()
    ):
        raise ProductMonthAggregationError(
            "Product-month failure counts do not sum to review predictions"
        )
    if signals.duplicated(["parent_asin", "review_month"]).any():
        raise ProductMonthAggregationError("Duplicate parent_asin-review_month rows")
    if not signals["predicted_failure_share"].between(0.0, 1.0).all():
        raise ProductMonthAggregationError("Product-month failure share outside [0, 1]")
    return {
        "rows": len(signals),
        "n_reviews_sum": int(signals["n_reviews"].sum()),
        "predicted_failure_count_sum": int(
            signals["predicted_failure_count"].sum()
        ),
        "unique_parent_asin": int(signals["parent_asin"].nunique()),
        "unique_parent_month": int(
            signals[["parent_asin", "review_month"]].drop_duplicates().shape[0]
        ),
        "share_in_unit_interval": True,
    }


def probability_summary(values: pd.Series) -> dict[str, float]:
    series = values.astype(float)
    return {
        "min": float(series.min()),
        "p01": float(series.quantile(0.01)),
        "p05": float(series.quantile(0.05)),
        "p10": float(series.quantile(0.10)),
        "p25": float(series.quantile(0.25)),
        "median": float(series.median()),
        "mean": float(series.mean()),
        "p75": float(series.quantile(0.75)),
        "p90": float(series.quantile(0.90)),
        "p95": float(series.quantile(0.95)),
        "p99": float(series.quantile(0.99)),
        "max": float(series.max()),
        "std": float(series.std(ddof=0)),
    }


def device_rows(predictions: pd.DataFrame, signals: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in DEVICE_TYPES:
        subset = predictions.loc[predictions["device_type"] == device]
        monthly = signals.loc[signals["device_type"] == device]
        per_product = subset.groupby("parent_asin", observed=True).size()
        rows.append(
            {
                "device_type": device,
                "analysis_role": subset["analysis_role"].iloc[0],
                "n_products": int(subset["parent_asin"].nunique()),
                "n_reviews": len(subset),
                "predicted_failure_count": int(subset["failure_prediction"].sum()),
                "predicted_failure_share": float(subset["failure_prediction"].mean()),
                "mean_failure_probability": float(
                    subset["failure_probability"].mean()
                ),
                "median_failure_probability": float(
                    subset["failure_probability"].median()
                ),
                "min_failure_probability": float(
                    subset["failure_probability"].min()
                ),
                "max_failure_probability": float(
                    subset["failure_probability"].max()
                ),
                "product_months": len(monthly),
                "product_month_n_reviews_min": int(monthly["n_reviews"].min()),
                "product_month_n_reviews_median": float(
                    monthly["n_reviews"].median()
                ),
                "product_month_n_reviews_mean": float(
                    monthly["n_reviews"].mean()
                ),
                "product_month_n_reviews_max": int(monthly["n_reviews"].max()),
                "largest_product_review_share": float(per_product.max() / len(subset)),
            }
        )
    return rows


def year_rows(predictions: pd.DataFrame) -> list[dict[str, Any]]:
    frame = predictions.copy()
    frame["year"] = pd.to_datetime(frame["review_datetime"], utc=True).dt.year
    rows: list[dict[str, Any]] = []
    for scope, subset in [("all", frame), *[(d, frame.loc[frame["device_type"] == d]) for d in DEVICE_TYPES]]:
        for year, group in subset.groupby("year", sort=True):
            rows.append(
                {
                    "scope": scope,
                    "year": int(year),
                    "n_reviews": len(group),
                    "predicted_failure_count": int(group["failure_prediction"].sum()),
                    "predicted_failure_share": float(group["failure_prediction"].mean()),
                    "mean_failure_probability": float(group["failure_probability"].mean()),
                }
            )
    return rows


def probability_distribution_rows(
    probabilities: pd.Series, edges: Sequence[float]
) -> list[dict[str, Any]]:
    values = probabilities.astype(float).to_numpy()
    rows: list[dict[str, Any]] = []
    for index, (lower, upper) in enumerate(zip(edges[:-1], edges[1:])):
        if index == len(edges) - 2:
            mask = (values >= lower) & (values <= upper)
            interval = f"[{lower:.2f}, {upper:.2f}]"
        else:
            mask = (values >= lower) & (values < upper)
            interval = f"[{lower:.2f}, {upper:.2f})"
        count = int(mask.sum())
        rows.append(
            {
                "bin": interval,
                "lower_bound": float(lower),
                "upper_bound": float(upper),
                "count": count,
                "share": float(count / len(values)),
            }
        )
    if sum(row["count"] for row in rows) != len(values):
        raise FullInferenceError("Probability distribution bins do not cover all rows")
    return rows


def coverage_rows(
    signals: pd.DataFrame, thresholds: Sequence[int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scopes = [("all", signals)] + [
        (device, signals.loc[signals["device_type"] == device])
        for device in DEVICE_TYPES
    ]
    for scope, subset in scopes:
        for threshold in thresholds:
            count = int((subset["n_reviews"] >= threshold).sum())
            rows.append(
                {
                    "scope": scope,
                    "minimum_reviews": int(threshold),
                    "product_month_count": count,
                    "total_product_months": len(subset),
                    "share_of_product_months": float(count / len(subset)),
                    "diagnostic_only_not_professor_requirement": True,
                }
            )
    return rows


def product_month_device_rows(signals: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in DEVICE_TYPES:
        subset = signals.loc[signals["device_type"] == device]
        rows.append(
            {
                "device_type": device,
                "analysis_role": subset["analysis_role"].iloc[0],
                "n_products": int(subset["parent_asin"].nunique()),
                "product_months": len(subset),
                "n_reviews_min": int(subset["n_reviews"].min()),
                "n_reviews_median": float(subset["n_reviews"].median()),
                "n_reviews_mean": float(subset["n_reviews"].mean()),
                "n_reviews_max": int(subset["n_reviews"].max()),
                "predicted_failure_share_mean": float(
                    subset["predicted_failure_share"].mean()
                ),
                "mean_failure_probability_mean": float(
                    subset["mean_failure_probability"].mean()
                ),
            }
        )
    return rows


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
    }


def protected_paths(root: Path) -> list[Path]:
    paths = [
        root / "data/amazon_reviews_2023/processed/target_products.parquet",
        root / "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/annotation_labels_w5b_v1_0.parquet",
        root / "data/amazon_reviews_2023/processed/annotation_labels_w5c_b_v1_0.parquet",
        root / "outputs/models/w5b_tfidf_logistic_regression.joblib",
        root / "outputs/models/w5c_b_tfidf_logistic_regression.joblib",
    ]
    paths.extend(sorted((root / "data/amazon_reviews_2023/reports/w5c_b").glob("*")))
    return [path for path in paths if path.is_file()]


def identity_map(root: Path, paths: Iterable[Path]) -> dict[str, Any]:
    return {relative(root, path): file_identity(root, path) for path in paths}


def summary_markdown(
    status: dict[str, Any], device_counts: list[dict[str, Any]], coverage: list[dict[str, Any]]
) -> str:
    device_lines = "\n".join(
        f"| {row['device_type']} | {row['analysis_role']} | {row['n_products']} | "
        f"{row['n_reviews']} | {row['predicted_failure_count']} | "
        f"{row['predicted_failure_share']:.4f} | {row['product_months']} |"
        for row in device_counts
    )
    all_coverage = [row for row in coverage if row["scope"] == "all"]
    coverage_lines = "\n".join(
        f"| ≥{row['minimum_reviews']} | {row['product_month_count']} | "
        f"{row['share_of_product_months']:.4f} |"
        for row in all_coverage
    )
    probability = status["full_inference"]["failure_probability_distribution"]
    return f"""# Phase W6-A Summary

Technical status: **{status['status']}**
W6-B readiness: **{status['w6b_readiness']}**

## Frozen-model reproduction

- Definite labeled rows re-scored: {status['model_reproduction']['rows']}
- Prediction mismatches: {status['model_reproduction']['prediction_mismatch_count']}
- Probability mismatches: {status['model_reproduction']['probability_mismatch_count']}
- Maximum absolute probability difference: {status['model_reproduction']['max_probability_absolute_difference']:.3g}

## Full-corpus inference

- Formal reviews scored: {status['full_inference']['rows']}
- Predicted failures: {status['full_inference']['predicted_failure_count']}
- Predicted failure share: {status['full_inference']['predicted_failure_share']:.4f}
- Failure probability median/mean: {probability['median']:.4f} / {probability['mean']:.4f}
- Unique products: {status['full_inference']['unique_parent_asin']}
- Product-month rows: {status['product_month_aggregation']['rows']}

| Device type | Analysis role | Products | Reviews | Predicted failures | Failure share | Product-months |
|---|---|---:|---:|---:|---:|---:|
{device_lines}

The three device types do not have equal statistical support. Smart plug is the primary longitudinal analysis, smart bulb is exploratory, and smart switch remains a small-sample case study.

## Product-month volume coverage

These thresholds are diagnostic only and are not professor-confirmed requirements.

| Minimum reviews | Product-months | Share |
|---:|---:|---:|
{coverage_lines}

The product-month output contains preliminary failure-binary engineering signals only. It is not a final EngineeringIndex and contains no Severity, Persistence, Sentiment, future quality target, or final early-warning comparison.
"""


def status_for_exception(error: Exception) -> str:
    if isinstance(error, SpaceGate):
        return "PAUSED_SPACE_GATE"
    if isinstance(error, InputMismatch):
        return "FAILED_INPUT_MISMATCH"
    if isinstance(error, ModelReproductionError):
        return "FAILED_MODEL_REPRODUCTION"
    if isinstance(error, ProductMonthAggregationError):
        return "FAILED_PRODUCT_MONTH_AGGREGATION"
    return "FAILED_FULL_INFERENCE"


def main() -> int:
    root = project_root()
    config_path = root / "config" / "w6a_full_inference_rules.toml"
    config = load_toml(config_path)
    project_config = load_toml(root / "config" / "project.toml")
    report_dir = root / config["outputs"]["report_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w6a_execution.log"
    start_utc = datetime.now(timezone.utc)
    start_perf = time.perf_counter()
    initial_free = disk_free_gib(root)
    disk_stages: list[dict[str, Any]] = [
        {"stage": "start", "free_gib": initial_free, "recorded_at_utc": start_utc}
    ]
    protected = protected_paths(root)
    protected_before = identity_map(root, protected)
    log_message(log_path, f"W6-A started at project root {root}")

    try:
        check_space(root, float(config["inference"]["minimum_free_gib"]), "W6-A start")
        status_input = load_json(root / config["inputs"]["w5c_b_status"])
        if status_input.get("status") != config["inputs"]["w5c_b_required_status"]:
            raise InputMismatch(
                f"W5-C-B status is {status_input.get('status')}, expected PASS"
            )

        inputs = config["inputs"]
        input_identities = {
            "formal_reviews": validate_parquet_input(
                root,
                root / inputs["formal_reviews"],
                int(inputs["formal_reviews_rows"]),
                str(inputs["formal_reviews_sha256"]),
            ),
            "formal_products": validate_parquet_input(
                root,
                root / inputs["formal_products"],
                int(inputs["formal_products_rows"]),
                str(inputs["formal_products_sha256"]),
            ),
            "frozen_labels": validate_parquet_input(
                root,
                root / inputs["frozen_labels"],
                int(inputs["frozen_labels_rows"]),
                str(inputs["frozen_labels_sha256"]),
            ),
            "frozen_model": validate_file_input(
                root,
                root / inputs["frozen_model"],
                str(inputs["frozen_model_sha256"]),
            ),
            "w5c_b_baseline_predictions": validate_parquet_input(
                root,
                root / inputs["w5c_b_baseline_predictions"],
                int(inputs["w5c_b_baseline_predictions_rows"]),
                str(inputs["w5c_b_baseline_predictions_sha256"]),
            ),
            "w5c_b_status": file_identity(root, root / inputs["w5c_b_status"]),
            "config": file_identity(root, config_path),
            "project_config": file_identity(root, root / "config" / "project.toml"),
        }
        products = pq.read_table(
            root / inputs["formal_products"], columns=["parent_asin", "device_type"]
        ).to_pandas()
        if (
            len(products) != 125
            or products["parent_asin"].isna().any()
            or not products["parent_asin"].is_unique
        ):
            raise InputMismatch("Formal product identity or uniqueness validation failed")
        product_counts = products["device_type"].value_counts().to_dict()
        if product_counts != {"smart_plug": 95, "smart_bulb": 25, "smart_switch": 5}:
            raise InputMismatch(f"Unexpected formal product device counts: {product_counts}")

        model_bundle = joblib.load(root / inputs["frozen_model"])
        model_validation = validate_model_bundle(model_bundle, config)
        log_message(log_path, "Frozen input identities and model metadata verified")
        manifest = {
            "phase": PHASE,
            "started_at_utc": start_utc,
            "project_root": str(root),
            "project_configuration": project_config,
            "environment": environment_payload(),
            "initial_free_gib": initial_free,
            "inputs": input_identities,
            "formal_product_device_counts": product_counts,
            "model_validation": model_validation,
            "protected_files_before": protected_before,
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_source_read": False,
        }
        write_json(report_dir / "w6a_input_manifest.json", manifest)

        reproduction = reproduce_w5c_b_predictions(root, config, model_bundle)
        write_json(report_dir / "model_reproduction_audit.json", reproduction)
        log_message(log_path, "Frozen model reproduction passed with zero mismatches")

        fingerprint_payload = {
            "phase_version": config["phase"]["version"],
            "formal_reviews_sha256": input_identities["formal_reviews"]["sha256"],
            "formal_products_sha256": input_identities["formal_products"]["sha256"],
            "model_sha256": input_identities["frozen_model"]["sha256"],
            "config_sha256": input_identities["config"]["sha256"],
            "batch_size": int(config["inference"]["batch_size"]),
            "decision_threshold": float(config["model"]["decision_threshold"]),
            "preprocessing_version": config["model"]["preprocessing_version"],
        }
        fingerprint = stable_fingerprint(fingerprint_payload)
        chunk_paths, checkpoint = run_chunked_inference(
            root, config, model_bundle, fingerprint, log_path
        )
        disk_stages.append(
            {
                "stage": "after_chunked_inference",
                "free_gib": disk_free_gib(root),
                "recorded_at_utc": datetime.now(timezone.utc),
            }
        )
        review_output = root / config["outputs"]["review_predictions"]
        signal_output = root / config["outputs"]["product_month_signals"]
        existing_status_path = report_dir / "w6a_status.json"
        if review_output.exists() or signal_output.exists():
            safely_reusable = False
            if existing_status_path.is_file():
                old_status = load_json(existing_status_path)
                safely_reusable = (
                    old_status.get("status") == "PASS"
                    and old_status.get("checkpoint", {}).get("checkpoint_fingerprint")
                    == fingerprint
                )
            if not safely_reusable:
                raise InputMismatch(
                    "Existing W6-A processed output lacks a matching successful fingerprint"
                )
        else:
            combine_chunks(
                root,
                chunk_paths,
                review_output,
                str(config["inference"]["compression"]),
                float(config["inference"]["minimum_free_gib"]),
            )
        disk_stages.append(
            {
                "stage": "after_review_predictions",
                "free_gib": disk_free_gib(root),
                "recorded_at_utc": datetime.now(timezone.utc),
            }
        )

        predictions, review_validation = validate_review_predictions(
            review_output,
            int(inputs["formal_reviews_rows"]),
            set(products["parent_asin"].astype(str)),
        )
        signals = aggregate_product_month(predictions)
        aggregation_validation = validate_product_month(signals, predictions)
        if not signal_output.exists():
            check_space(
                root,
                float(config["inference"]["minimum_free_gib"]),
                "before product-month signal Parquet",
            )
            write_parquet_atomic(
                signal_output,
                signals,
                PRODUCT_MONTH_SCHEMA,
                str(config["inference"]["compression"]),
            )
        reloaded_signals = pq.read_table(signal_output)
        if (
            reloaded_signals.num_rows != len(signals)
            or reloaded_signals.schema.names != PRODUCT_MONTH_COLUMNS
        ):
            raise ProductMonthAggregationError(
                "Reloaded product-month Parquet failed row/schema validation"
            )
        disk_stages.append(
            {
                "stage": "after_product_month_signals",
                "free_gib": disk_free_gib(root),
                "recorded_at_utc": datetime.now(timezone.utc),
            }
        )

        by_device = device_rows(predictions, signals)
        by_year = year_rows(predictions)
        probability_bins = probability_distribution_rows(
            predictions["failure_probability"],
            [float(value) for value in config["coverage"]["probability_bin_edges"]],
        )
        coverage = coverage_rows(
            signals,
            [int(value) for value in config["coverage"]["product_month_review_thresholds"]],
        )
        product_month_by_device = product_month_device_rows(signals)
        probability = probability_summary(predictions["failure_probability"])
        full_summary = {
            "rows": len(predictions),
            "unique_duplicate_keys": int(predictions["duplicate_key"].nunique()),
            "unique_parent_asin": int(predictions["parent_asin"].nunique()),
            "device_type_review_counts": {
                row["device_type"]: row["n_reviews"] for row in by_device
            },
            "predicted_failure_count": int(predictions["failure_prediction"].sum()),
            "predicted_non_failure_count": int(
                (predictions["failure_prediction"] == 0).sum()
            ),
            "predicted_failure_share": float(predictions["failure_prediction"].mean()),
            "failure_probability_distribution": probability,
            "model_version": config["model"]["model_version"],
            "decision_threshold": float(config["model"]["decision_threshold"]),
            "all_reviews_scored": len(predictions) == int(inputs["formal_reviews_rows"]),
            "predictions_are_model_outputs_not_human_truth": True,
            "inference_checkpoint": checkpoint,
        }
        write_json(report_dir / "full_inference_summary.json", full_summary)
        write_csv(
            report_dir / "full_inference_count_by_device_type.csv",
            by_device,
            list(by_device[0]),
        )
        write_csv(
            report_dir / "full_inference_count_by_year.csv",
            by_year,
            list(by_year[0]),
        )
        write_csv(
            report_dir / "failure_probability_distribution.csv",
            probability_bins,
            list(probability_bins[0]),
        )
        write_csv(
            report_dir / "product_month_coverage.csv",
            coverage,
            list(coverage[0]),
        )
        write_csv(
            report_dir / "product_month_count_by_device_type.csv",
            product_month_by_device,
            list(product_month_by_device[0]),
        )
        final_free = check_space(
            root, float(config["inference"]["minimum_free_gib"]), "W6-A completion"
        )
        disk_stages.append(
            {
                "stage": "completion",
                "free_gib": final_free,
                "recorded_at_utc": datetime.now(timezone.utc),
            }
        )
        disk_payload = {
            "minimum_free_gib": float(config["inference"]["minimum_free_gib"]),
            "stages": disk_stages,
            "minimum_observed_free_gib": min(item["free_gib"] for item in disk_stages),
            "final_free_gib": final_free,
        }
        write_json(report_dir / "w6a_disk_usage.json", disk_payload)

        protected_after = identity_map(root, protected)
        protected_unchanged = protected_before == protected_after
        if not protected_unchanged:
            raise FullInferenceError("One or more protected input/baseline files changed")
        review_identity = parquet_identity(root, review_output)
        signal_identity = parquet_identity(root, signal_output)
        required_reports = [
            "w6a_execution.log",
            "w6a_input_manifest.json",
            "model_reproduction_audit.json",
            "full_inference_summary.json",
            "full_inference_count_by_device_type.csv",
            "full_inference_count_by_year.csv",
            "failure_probability_distribution.csv",
            "product_month_coverage.csv",
            "product_month_count_by_device_type.csv",
            "w6a_disk_usage.json",
            "w6a_summary.md",
            "w6a_status.json",
        ]
        status = {
            "phase": PHASE,
            "status": "PASS",
            "w6b_readiness": config["phase"]["w6b_readiness"],
            "completed_at_utc": datetime.now(timezone.utc),
            "elapsed_seconds": time.perf_counter() - start_perf,
            "environment": environment_payload(),
            "input_validation": {
                "w5c_b_status": status_input.get("status"),
                "formal_review_rows": input_identities["formal_reviews"]["rows"],
                "formal_product_rows": input_identities["formal_products"]["rows"],
                "frozen_label_rows": input_identities["frozen_labels"]["rows"],
                "all_hashes_match": True,
                "formal_product_device_counts": product_counts,
                "model_validation": model_validation,
            },
            "model_reproduction": reproduction,
            "checkpoint": checkpoint,
            "full_inference": full_summary,
            "review_prediction_validation": review_validation,
            "product_month_aggregation": aggregation_validation,
            "device_type_summary": by_device,
            "product_month_coverage": coverage,
            "disk_usage": disk_payload,
            "protected_inputs_unchanged": protected_unchanged,
            "protected_files_after": protected_after,
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_sources_read": False,
            "tfidf_refit": False,
            "model_retrained_or_modified": False,
            "human_labels_modified": False,
            "severity_model_or_signal_created": False,
            "persistence_model_or_signal_created": False,
            "sentiment_model_or_signal_created": False,
            "final_engineering_index_created": False,
            "future_quality_target_created": False,
            "final_early_warning_comparison_executed": False,
            "w6b_executed": False,
            "git_commit_created": False,
            "outputs": {
                "review_predictions": review_identity,
                "product_month_signals": signal_identity,
            },
            "required_reports": [
                relative(root, report_dir / name) for name in required_reports
            ],
            "remaining_decisions": [
                "Severity modeling method",
                "Persistence modeling method",
                "Sentiment comparison method",
                "Future quality-deterioration definition",
                "Prediction horizon",
                "EngineeringIndex weights",
            ],
            "limitations": [
                "The review-level predictions are frozen-model outputs, not human ground truth.",
                "Smart plugs are primary, smart bulbs exploratory, and smart switches a small-sample case study.",
                "The product-month fields are preliminary failure-binary signals, not a final EngineeringIndex.",
            ],
        }
        (report_dir / "w6a_summary.md").write_text(
            summary_markdown(status, by_device, coverage), encoding="utf-8"
        )
        write_json(report_dir / "w6a_status.json", status)
        log_message(
            log_path,
            f"W6-A PASS: reviews={len(predictions)} product_months={len(signals)}",
        )
        return 0
    except Exception as error:
        failure_status = status_for_exception(error)
        log_message(log_path, f"{failure_status}: {type(error).__name__}: {error}")
        failure_payload = {
            "phase": PHASE,
            "status": failure_status,
            "w6b_readiness": "NOT_READY",
            "failed_at_utc": datetime.now(timezone.utc),
            "elapsed_seconds": time.perf_counter() - start_perf,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_sources_read": False,
            "tfidf_refit": False,
            "model_retrained_or_modified": False,
            "w6b_executed": False,
        }
        write_json(report_dir / "w6a_status.json", failure_payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
