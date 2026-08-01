"""Prepare W5-C-A expanded blind annotation batches.

The approved W5-B pilot model is used only to compute private sampling scores
for the formal 55,877-review Parquet.  No prediction is treated as a label, no
model is fit, and no raw JSONL or gzip source is read.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import sys
import time
import tomllib
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PHASE = "W5-C-A"
SAMPLING_VERSION = "w5c-a-sampling-v1.0"
ANNOTATION_VERSION = "w5a-annotation-v1.0-draft"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
BUCKETS = (
    "high_uncertainty",
    "rating_keyword_disagreement",
    "diversity_control",
)
MAIN_BLIND_COLUMNS = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_1_failure_binary",
    "reviewer_1_failure_type",
    "reviewer_1_severity",
    "reviewer_1_persistence",
    "reviewer_1_confidence",
    "reviewer_1_notes",
    "adjudicated_failure_binary",
    "adjudicated_failure_type",
    "adjudicated_severity",
    "adjudicated_persistence",
    "adjudication_notes",
]
REVIEWER_2_COLUMNS = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_2_failure_binary",
    "reviewer_2_failure_type",
    "reviewer_2_severity",
    "reviewer_2_persistence",
    "reviewer_2_confidence",
    "reviewer_2_notes",
]
HIDDEN_COLUMNS = {
    "rating",
    "low_star_indicator",
    "keyword_candidate_hit",
    "model_failure_probability",
    "model_uncertainty_distance",
    "sampling_bucket",
    "rating_stratum",
    "time_stratum",
    "parent_asin",
    "duplicate_key",
    "source_domain",
    "review_datetime",
}
STAR_HEADER_RE = re.compile(
    r"^\s*(?:one|two|three|four|five)\s+stars?"
    r"\s*(?:[.!:;\-–—]+\s*)?(?:(?:\r?\n)+|$)",
    flags=re.IGNORECASE,
)


class W5CAError(RuntimeError):
    """Controlled W5-C-A error."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W5CAError(f"Could not resolve project root from {__file__}")
    if not (root / "config" / "project.toml").is_file():
        raise W5CAError("config/project.toml is missing")
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


def stable_hash(seed: int, purpose: str, value: str) -> int:
    payload = f"{seed}|{purpose}|{value}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def file_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    payload: dict[str, Any] = {
        "path": relative(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }
    if include_hash:
        payload["sha256"] = sha256_file(path)
    return payload


def parquet_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    payload = file_identity(root, path, include_hash=include_hash)
    payload.update(
        {
            "rows": parquet.metadata.num_rows,
            "fields": parquet.schema_arrow.names,
            "field_count": len(parquet.schema_arrow.names),
            "compression": sorted(
                {
                    parquet.metadata.row_group(group).column(column).compression
                    for group in range(parquet.metadata.num_row_groups)
                    for column in range(
                        parquet.metadata.row_group(group).num_columns
                    )
                }
            ),
        }
    )
    return payload


def assert_hash(path: Path, expected: str, label: str) -> None:
    observed = sha256_file(path)
    if observed != expected:
        raise W5CAError(
            f"{label} SHA-256 mismatch: expected {expected}, observed {observed}"
        )


def assert_parquet(
    path: Path, expected_rows: int, expected_hash: str, label: str
) -> None:
    if not path.is_file():
        raise W5CAError(f"{label} is missing: {path}")
    assert_hash(path, expected_hash, label)
    rows = pq.ParquetFile(path).metadata.num_rows
    if rows != expected_rows:
        raise W5CAError(f"{label} row mismatch: expected {expected_rows}, got {rows}")


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def process_peak_working_set_bytes() -> int | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    )
    return int(counters.PeakWorkingSetSize) if ok else None


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: (
                        ""
                        if row.get(field) is None or row.get(field) is pd.NA
                        else row.get(field, "")
                    )
                    for field in fields
                }
            )
    temporary.replace(path)


def write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(
        pa.Table.from_pandas(frame, preserve_index=False),
        temporary,
        compression="zstd",
    )
    temporary.replace(path)


def log_message(path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp}\t{message}\n")


def normalize_for_rules(value: Any) -> str:
    if value is None or value is pd.NA:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).lower()
    return re.sub(r"\s+", " ", text).strip()


class KeywordRules:
    """Frozen W5-A transparent keyword/rule baseline."""

    def __init__(self, raw: dict[str, Any]):
        if raw["rules"]["version"] != "w5a-keyword-v1.0-draft":
            raise W5CAError("Unexpected keyword rule version")
        flags = 0 if raw["rules"].get("case_sensitive") else re.IGNORECASE
        self.device_context = [
            re.compile(pattern, flags)
            for pattern in raw["context"]["device_function_patterns"]
        ]
        self.general_failure = [
            re.compile(pattern, flags)
            for pattern in raw["context"]["general_failure_patterns"]
        ]
        self.non_engineering = [
            re.compile(pattern, flags)
            for pattern in raw["context"]["non_engineering_only_patterns"]
        ]
        self.categories = [
            (
                category["code"],
                [re.compile(pattern, flags) for pattern in category["patterns"]],
            )
            for category in raw.get("category", [])
        ]

    def classify(self, text: Any) -> tuple[bool, tuple[str, ...]]:
        normalized = normalize_for_rules(text)
        if not normalized:
            return False, ()
        category_hits = tuple(
            code
            for code, patterns in self.categories
            if any(pattern.search(normalized) for pattern in patterns)
        )
        device_context = any(
            pattern.search(normalized) for pattern in self.device_context
        )
        general_failure = any(
            pattern.search(normalized) for pattern in self.general_failure
        )
        non_engineering_only = (
            any(pattern.search(normalized) for pattern in self.non_engineering)
            and not category_hits
            and not general_failure
        )
        hit = bool(
            not non_engineering_only
            and (category_hits or (device_context and general_failure))
        )
        return hit, category_hits


def rating_stratum(value: Any) -> str:
    rating = float(value)
    if rating <= 2:
        return "low_1_2"
    if rating == 3:
        return "middle_3"
    return "high_4_5"


def time_stratum(value: Any) -> str:
    year = pd.to_datetime(value, utc=True).year
    if year <= 2017:
        return "early_2011_2017"
    if year <= 2020:
        return "middle_2018_2020"
    return "recent_2021_2023"


def bucket_targets(total: int, shares: dict[str, float]) -> dict[str, int]:
    high = math.floor(total * shares["high_uncertainty"])
    disagreement = math.floor(total * shares["rating_keyword_disagreement"])
    return {
        "high_uncertainty": high,
        "rating_keyword_disagreement": disagreement,
        "diversity_control": total - high - disagreement,
    }


def diversity_order(
    frame: pd.DataFrame, seed: int, purpose: str
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    ordered = frame.copy()
    ordered["_stable_hash"] = [
        stable_hash(seed, purpose, value)
        for value in ordered["duplicate_key"].astype(str)
    ]
    ordered["_stratum"] = (
        ordered["rating_stratum"].astype(str)
        + "|"
        + ordered["time_stratum"].astype(str)
        + "|kw="
        + ordered["keyword_candidate_hit"].astype(int).astype(str)
    )
    ordered["_stratum_hash"] = [
        stable_hash(seed, f"{purpose}|stratum", value)
        for value in ordered["_stratum"]
    ]
    ordered = ordered.sort_values(
        ["parent_asin", "_stable_hash"], kind="mergesort"
    )
    ordered["_parent_round"] = ordered.groupby("parent_asin").cumcount()
    return ordered.sort_values(
        ["_parent_round", "_stratum_hash", "_stable_hash", "duplicate_key"],
        kind="mergesort",
    )


def select_bucket_mix(
    pool: pd.DataFrame,
    total: int,
    seed: int,
    purpose: str,
    shares: dict[str, float],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    targets = bucket_targets(total, shares)
    selected_indices: list[int] = []
    bucket_results: dict[str, Any] = {}
    for bucket in BUCKETS:
        candidates = pool.loc[
            (pool["sampling_bucket"] == bucket)
            & ~pool.index.isin(selected_indices)
        ]
        ordered = diversity_order(
            candidates, seed, f"{purpose}|bucket={bucket}"
        )
        take = min(targets[bucket], len(ordered))
        chosen = ordered.head(take)
        selected_indices.extend(chosen.index.tolist())
        bucket_results[bucket] = {
            "target": targets[bucket],
            "available": len(candidates),
            "selected_before_fallback": take,
        }
    shortfall = total - len(selected_indices)
    if shortfall:
        remaining = pool.loc[~pool.index.isin(selected_indices)]
        ordered = diversity_order(remaining, seed, f"{purpose}|fallback")
        if len(ordered) < shortfall:
            raise W5CAError(
                f"{purpose} cannot fill quota {total}; only "
                f"{len(selected_indices) + len(ordered)} available"
            )
        selected_indices.extend(ordered.head(shortfall).index.tolist())
    selected = pool.loc[selected_indices].copy()
    if len(selected) != total or not selected["duplicate_key"].is_unique:
        raise W5CAError(f"{purpose} selection is incomplete or duplicated")
    actual = Counter(selected["sampling_bucket"])
    for bucket in BUCKETS:
        bucket_results[bucket]["selected_final"] = int(actual.get(bucket, 0))
    return selected, {
        "purpose": purpose,
        "requested": total,
        "selected": len(selected),
        "fallback_rows": shortfall,
        "bucket_results": bucket_results,
        "unique_parent_asin": int(selected["parent_asin"].nunique()),
    }


def score_sampling_frame(
    reviews: pd.DataFrame,
    model_bundle: dict[str, Any],
    keyword_rules: KeywordRules,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    vectorizer = model_bundle["vectorizer"]
    classifier = model_bundle["classifier"]
    before_vocab = dict(vectorizer.vocabulary_)
    before_coefficients = classifier.coef_.copy()
    model_text = reviews["review_text"].fillna("").astype(str).map(
        lambda value: STAR_HEADER_RE.sub("", value, count=1)
    )
    matrix = vectorizer.transform(model_text)
    classes = list(classifier.classes_)
    if 1 not in classes:
        raise W5CAError("Pilot model does not expose positive class 1")
    probability = classifier.predict_proba(matrix)[:, classes.index(1)]
    if before_vocab != vectorizer.vocabulary_ or not np.array_equal(
        before_coefficients, classifier.coef_
    ):
        raise W5CAError("Pilot model changed during private scoring")

    keyword_results = reviews["review_text"].map(keyword_rules.classify)
    scored = reviews.copy()
    scored["model_failure_probability"] = probability
    scored["model_uncertainty_distance"] = np.abs(probability - 0.5)
    scored["keyword_candidate_hit"] = keyword_results.map(lambda value: value[0])
    scored["keyword_failure_types"] = keyword_results.map(
        lambda value: ";".join(value[1])
    )
    scored["low_star_indicator"] = scored["rating"].astype(float).le(2)
    scored["rating_keyword_disagreement"] = (
        scored["low_star_indicator"] != scored["keyword_candidate_hit"]
    )
    scored["rating_stratum"] = scored["rating"].map(rating_stratum)
    scored["time_stratum"] = scored["review_datetime"].map(time_stratum)
    lower = config["sampling"]["uncertainty"]["probability_lower"]
    upper = config["sampling"]["uncertainty"]["probability_upper"]
    high_uncertainty = scored["model_failure_probability"].between(
        lower, upper, inclusive="both"
    )
    scored["sampling_bucket"] = "diversity_control"
    scored.loc[
        scored["rating_keyword_disagreement"],
        "sampling_bucket",
    ] = "rating_keyword_disagreement"
    scored.loc[high_uncertainty, "sampling_bucket"] = "high_uncertainty"
    return scored, {
        "rows_scored": len(scored),
        "model_refit": False,
        "predictions_used_as_labels": False,
        "probability_min": float(scored["model_failure_probability"].min()),
        "probability_max": float(scored["model_failure_probability"].max()),
        "probability_mean": float(scored["model_failure_probability"].mean()),
        "high_uncertainty_rows": int(high_uncertainty.sum()),
        "rating_keyword_disagreement_rows": int(
            scored["rating_keyword_disagreement"].sum()
        ),
        "keyword_hit_rows": int(scored["keyword_candidate_hit"].sum()),
        "low_star_rows": int(scored["low_star_indicator"].sum()),
        "bucket_counts": dict(
            sorted(Counter(scored["sampling_bucket"]).items())
        ),
    }


def make_blind_rows(
    selected: pd.DataFrame, reviewer: int
) -> tuple[list[dict[str, Any]], list[str]]:
    if reviewer == 1:
        fields = MAIN_BLIND_COLUMNS
    else:
        fields = REVIEWER_2_COLUMNS
    rows: list[dict[str, Any]] = []
    for row in selected.to_dict(orient="records"):
        output = {field: "" for field in fields}
        output["blind_review_id"] = row["blind_review_id"]
        output["device_type"] = row["device_type"]
        output["review_text"] = row["review_text"]
        rows.append(output)
    return rows, fields


def validate_blind_csv(path: Path, expected_rows: int, expected_fields: list[str]) -> None:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if list(frame.columns) != expected_fields:
        raise W5CAError(f"{path.name}: blind schema mismatch")
    if len(frame) != expected_rows:
        raise W5CAError(
            f"{path.name}: expected {expected_rows} rows, got {len(frame)}"
        )
    if not frame["blind_review_id"].is_unique:
        raise W5CAError(f"{path.name}: blind_review_id is not unique")
    hidden = set(frame.columns).intersection(HIDDEN_COLUMNS)
    if hidden:
        raise W5CAError(f"{path.name}: hidden fields exposed: {sorted(hidden)}")
    label_columns = [
        column
        for column in frame.columns
        if column.startswith(("reviewer_", "adjudicated_", "adjudication_"))
    ]
    nonempty = int(
        frame[label_columns].apply(
            lambda column: column.astype(str).str.strip().ne("").sum()
        ).sum()
    )
    if nonempty:
        raise W5CAError(f"{path.name}: {nonempty} human label cells are prefilled")


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


def protected_inputs(root: Path, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    paths = {
        "formal_review_parquet": root
        / config["inputs"]["formal_review_parquet"],
        "existing_sampling_frame": root
        / config["inputs"]["existing_sampling_frame"],
        "existing_labels": root / config["inputs"]["existing_labels"],
        "pilot_model": root / config["inputs"]["pilot_model"],
    }
    return {label: file_identity(root, path) for label, path in paths.items()}


def workbook_paths(interim_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for batch in range(1, 5):
        for reviewer in (1, 2):
            paths.append(
                interim_dir
                / f"annotation_batch_{batch}_reviewer{reviewer}_blind.xlsx"
            )
    return paths


def csv_paths(interim_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for batch in range(1, 5):
        for reviewer in (1, 2):
            paths.append(
                interim_dir
                / f"annotation_batch_{batch}_reviewer{reviewer}_blind.csv"
            )
    return paths


def prepare() -> int:
    root = project_root()
    config_path = root / "config" / "w5c_a_sampling_rules.toml"
    config = load_toml(config_path)
    if config["phase"]["name"] != PHASE:
        raise W5CAError("Unexpected W5-C-A phase in config")
    if config["phase"]["sampling_version"] != SAMPLING_VERSION:
        raise W5CAError("Unexpected W5-C-A sampling version")
    interim_dir = root / config["outputs"]["interim_dir"]
    report_dir = root / config["outputs"]["report_dir"]
    interim_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w5c_a_execution.log"
    if log_path.exists():
        log_path.unlink()
    started = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()
    initial_free = disk_free_gib(root)
    if initial_free < 60:
        raise W5CAError(f"PAUSED_SPACE_GATE: only {initial_free:.3f} GiB free")
    log_message(log_path, "W5-C-A preparation start")

    w5b_status = load_json(root / config["inputs"]["w5b_status"])
    if w5b_status.get("status") != "PASS":
        raise W5CAError("W5-B status is not PASS")
    if w5b_status.get("w6_readiness") != "REVIEW_REQUIRED":
        raise W5CAError("Unexpected W5-B W6 readiness")

    review_path = root / config["inputs"]["formal_review_parquet"]
    old_sampling_path = root / config["inputs"]["existing_sampling_frame"]
    old_labels_path = root / config["inputs"]["existing_labels"]
    model_path = root / config["inputs"]["pilot_model"]
    assert_parquet(
        review_path,
        config["inputs"]["formal_review_rows"],
        config["inputs"]["formal_review_sha256"],
        "formal review parquet",
    )
    assert_parquet(
        old_sampling_path,
        config["inputs"]["existing_sampling_rows"],
        config["inputs"]["existing_sampling_sha256"],
        "existing sampling frame",
    )
    assert_parquet(
        old_labels_path,
        config["inputs"]["existing_labels_rows"],
        config["inputs"]["existing_labels_sha256"],
        "existing labels",
    )
    assert_hash(
        model_path,
        config["inputs"]["pilot_model_sha256"],
        "pilot model",
    )
    protected_before = protected_inputs(root, config)
    input_manifest = {
        "phase": PHASE,
        "started_at_utc": started.isoformat(),
        "environment": environment_payload(),
        "initial_free_gib": initial_free,
        "inputs": {
            "formal_review_parquet": {
                **file_identity(root, review_path),
                "rows": pq.ParquetFile(review_path).metadata.num_rows,
            },
            "existing_sampling_frame": parquet_identity(
                root, old_sampling_path
            ),
            "existing_labels": parquet_identity(root, old_labels_path),
            "pilot_model": file_identity(root, model_path),
            "annotation_rules": file_identity(
                root, root / config["inputs"]["annotation_rules"]
            ),
            "keyword_rules": file_identity(
                root, root / config["inputs"]["keyword_rules"]
            ),
            "w5b_status": file_identity(
                root, root / config["inputs"]["w5b_status"]
            ),
        },
        "raw_jsonl_read": False,
        "compressed_source_read": False,
        "model_refit": False,
    }
    write_json(report_dir / "w5c_a_input_manifest.json", input_manifest)

    approved_columns = [
        "duplicate_key",
        "parent_asin",
        "device_type",
        "review_datetime",
        "review_text",
        "rating",
        "source_domain",
    ]
    reviews = pq.read_table(review_path, columns=approved_columns).to_pandas()
    if len(reviews) != 55877 or not reviews["duplicate_key"].is_unique:
        raise W5CAError("Formal review identity mismatch")
    old_sampling = pq.read_table(
        old_sampling_path, columns=["duplicate_key", "device_type"]
    ).to_pandas()
    old_keys = set(old_sampling["duplicate_key"].astype(str))
    eligible = reviews.loc[~reviews["duplicate_key"].isin(old_keys)].copy()
    if len(eligible) != 55577 or not eligible["duplicate_key"].is_unique:
        raise W5CAError("Existing 300 rows were not excluded exactly")
    remaining_counts = Counter(eligible["device_type"])
    expected_remaining = {
        "smart_plug": 54397,
        "smart_bulb": 1147,
        "smart_switch": 33,
    }
    if dict(remaining_counts) != expected_remaining:
        raise W5CAError(
            f"Unexpected remaining device counts: {dict(remaining_counts)}"
        )
    log_message(log_path, "Formal Parquet loaded; existing 300 rows excluded")

    model_bundle = joblib.load(model_path)
    keyword_rules = KeywordRules(
        load_toml(root / config["inputs"]["keyword_rules"])
    )
    scored, scoring_summary = score_sampling_frame(
        eligible, model_bundle, keyword_rules, config
    )
    if sha256_file(model_path) != config["inputs"]["pilot_model_sha256"]:
        raise W5CAError("Pilot model changed after scoring")
    log_message(log_path, "Private pilot-model and keyword scores computed")

    shares = config["sampling"]["bucket_shares"]
    seed = config["phase"]["random_seed"]
    selected_parts: list[pd.DataFrame] = []
    selection_audit: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    for batch_number in range(1, 5):
        batch_key = f"batch_{batch_number}"
        batch_config = config["batches"][batch_key]
        for device_type in DEVICE_TYPES:
            quota = int(batch_config[device_type])
            pool = scored.loc[
                (scored["device_type"] == device_type)
                & ~scored.index.isin(used_indices)
            ]
            selected, audit = select_bucket_mix(
                pool,
                quota,
                seed,
                f"{batch_key}|{device_type}",
                shares,
            )
            selected["batch_id"] = batch_key
            selected_parts.append(selected)
            used_indices.update(selected.index.tolist())
            audit.update(
                {
                    "batch_id": batch_key,
                    "device_type": device_type,
                }
            )
            selection_audit.append(audit)
    selected_all = pd.concat(selected_parts, ignore_index=False)
    if len(selected_all) != 1200 or not selected_all["duplicate_key"].is_unique:
        raise W5CAError("Expanded selected sample is not 1,200 unique reviews")
    if set(selected_all["duplicate_key"]).intersection(old_keys):
        raise W5CAError("Expanded selected sample overlaps the original 300")
    selected_counts = Counter(selected_all["device_type"])
    expected_selected = {
        key: int(value)
        for key, value in config["sampling"]["new_device_quotas"].items()
    }
    if dict(selected_counts) != expected_selected:
        raise W5CAError(
            f"Expanded device quotas mismatch: {dict(selected_counts)}"
        )
    switch_selected = selected_all.loc[
        selected_all["device_type"] == "smart_switch"
    ]
    if set(switch_selected["duplicate_key"]) != set(
        eligible.loc[
            eligible["device_type"] == "smart_switch", "duplicate_key"
        ]
    ):
        raise W5CAError("The remaining 33 smart-switch reviews were not all selected")

    selected_all = selected_all.copy()
    selected_all["_blind_order"] = [
        stable_hash(seed, f"{row.batch_id}|blind-order", str(row.duplicate_key))
        for row in selected_all.itertuples()
    ]
    selected_all = selected_all.sort_values(
        ["batch_id", "_blind_order", "duplicate_key"], kind="mergesort"
    )
    blind_ids: list[str] = []
    for batch_id, group in selected_all.groupby("batch_id", sort=True):
        batch_number = int(batch_id.split("_")[-1])
        blind_ids.extend(
            [
                f"W5C-A-B{batch_number}-{position:03d}"
                for position in range(1, len(group) + 1)
            ]
        )
    selected_all["blind_review_id"] = blind_ids
    selected_all["selected_for_double_review"] = False

    double_audit: list[dict[str, Any]] = []
    for batch_number in range(1, 5):
        batch_key = f"batch_{batch_number}"
        batch_config = config["batches"][batch_key]
        for device_type in DEVICE_TYPES:
            quota = int(batch_config[f"double_{device_type}"])
            pool = selected_all.loc[
                (selected_all["batch_id"] == batch_key)
                & (selected_all["device_type"] == device_type)
            ]
            chosen, audit = select_bucket_mix(
                pool,
                quota,
                seed,
                f"{batch_key}|double|{device_type}",
                shares,
            )
            selected_all.loc[
                selected_all.index.isin(chosen.index),
                "selected_for_double_review",
            ] = True
            audit.update(
                {
                    "batch_id": batch_key,
                    "device_type": device_type,
                }
            )
            double_audit.append(audit)
    if int(selected_all["selected_for_double_review"].sum()) != 240:
        raise W5CAError("Double-review selection must contain exactly 240 rows")

    selected_all = selected_all.sort_values(
        ["batch_id", "blind_review_id"], kind="mergesort"
    ).reset_index(drop=True)
    sampling_frame_columns = [
        "blind_review_id",
        "duplicate_key",
        "parent_asin",
        "device_type",
        "review_text",
        "rating",
        "review_datetime",
        "source_domain",
        "keyword_candidate_hit",
        "keyword_failure_types",
        "model_failure_probability",
        "model_uncertainty_distance",
        "low_star_indicator",
        "rating_keyword_disagreement",
        "rating_stratum",
        "time_stratum",
        "sampling_bucket",
        "batch_id",
        "selected_for_double_review",
    ]
    sampling_frame = selected_all[sampling_frame_columns].copy()
    blind_key_columns = [
        "blind_review_id",
        "duplicate_key",
        "parent_asin",
        "rating",
        "review_datetime",
        "source_domain",
        "keyword_candidate_hit",
        "model_failure_probability",
        "model_uncertainty_distance",
        "sampling_bucket",
        "rating_stratum",
        "time_stratum",
        "batch_id",
        "selected_for_double_review",
    ]
    blind_key = selected_all[blind_key_columns].copy()
    score_columns = [
        "duplicate_key",
        "parent_asin",
        "device_type",
        "review_datetime",
        "rating",
        "source_domain",
        "model_failure_probability",
        "model_uncertainty_distance",
        "keyword_candidate_hit",
        "keyword_failure_types",
        "low_star_indicator",
        "rating_keyword_disagreement",
        "rating_stratum",
        "time_stratum",
        "sampling_bucket",
    ]
    private_scores = scored[score_columns].copy()
    selected_lookup = selected_all.set_index("duplicate_key")
    private_scores["selected_for_w5c_a"] = private_scores["duplicate_key"].isin(
        selected_lookup.index
    )
    private_scores["blind_review_id"] = private_scores["duplicate_key"].map(
        selected_lookup["blind_review_id"]
    )
    private_scores["batch_id"] = private_scores["duplicate_key"].map(
        selected_lookup["batch_id"]
    )
    private_scores["selected_for_double_review"] = (
        private_scores["duplicate_key"]
        .map(selected_lookup["selected_for_double_review"])
        .fillna(False)
        .astype(bool)
    )
    write_parquet(root / config["outputs"]["sampling_frame"], sampling_frame)
    write_parquet(root / config["outputs"]["blind_review_key"], blind_key)
    write_parquet(
        root / config["outputs"]["private_sampling_scores"], private_scores
    )
    log_message(log_path, "Private W5-C-A Parquet outputs written")

    batch_manifest_rows: list[dict[str, Any]] = []
    for batch_number in range(1, 5):
        batch_key = f"batch_{batch_number}"
        batch = selected_all.loc[selected_all["batch_id"] == batch_key]
        for reviewer in (1, 2):
            source = (
                batch
                if reviewer == 1
                else batch.loc[batch["selected_for_double_review"]]
            )
            blind_rows, fields = make_blind_rows(source, reviewer)
            csv_path = (
                interim_dir
                / f"annotation_batch_{batch_number}_reviewer{reviewer}_blind.csv"
            )
            write_csv(csv_path, blind_rows, fields)
            validate_blind_csv(csv_path, 300 if reviewer == 1 else 60, fields)
            batch_manifest_rows.append(
                {
                    "batch_id": batch_key,
                    "reviewer": reviewer,
                    "rows": len(source),
                    "smart_plug": int(
                        (source["device_type"] == "smart_plug").sum()
                    ),
                    "smart_bulb": int(
                        (source["device_type"] == "smart_bulb").sum()
                    ),
                    "smart_switch": int(
                        (source["device_type"] == "smart_switch").sum()
                    ),
                    "unique_parent_asin": int(source["parent_asin"].nunique()),
                    "csv_path": relative(root, csv_path),
                    "xlsx_path": relative(
                        root,
                        csv_path.with_suffix(".xlsx"),
                    ),
                }
            )
    log_message(log_path, "Eight blind CSV annotation files written")

    def grouped_rows(columns: list[str], report_name: str) -> list[dict[str, Any]]:
        grouped = (
            selected_all.groupby(columns, dropna=False)
            .agg(
                sampled_reviews=("duplicate_key", "size"),
                unique_parent_asin=("parent_asin", "nunique"),
                double_review_rows=("selected_for_double_review", "sum"),
            )
            .reset_index()
        )
        rows = grouped.to_dict(orient="records")
        fields = (
            columns
            + ["sampled_reviews", "unique_parent_asin", "double_review_rows"]
        )
        write_csv(report_dir / report_name, rows, fields)
        return rows

    balance_device = grouped_rows(
        ["batch_id", "device_type"], "sampling_balance_by_device_type.csv"
    )
    balance_bucket = grouped_rows(
        ["batch_id", "device_type", "sampling_bucket"],
        "sampling_balance_by_bucket.csv",
    )
    balance_rating = grouped_rows(
        ["batch_id", "device_type", "rating_stratum"],
        "sampling_balance_by_rating_stratum.csv",
    )
    balance_time = grouped_rows(
        ["batch_id", "device_type", "time_stratum"],
        "sampling_balance_by_time_stratum.csv",
    )
    write_csv(
        report_dir / "batch_manifest.csv",
        batch_manifest_rows,
        [
            "batch_id",
            "reviewer",
            "rows",
            "smart_plug",
            "smart_bulb",
            "smart_switch",
            "unique_parent_asin",
            "csv_path",
            "xlsx_path",
        ],
    )
    write_json(
        report_dir / "batch_manifest.json",
        {"batches": batch_manifest_rows},
    )
    sampling_flow = {
        "phase": PHASE,
        "sampling_version": SAMPLING_VERSION,
        "formal_reviews": len(reviews),
        "existing_annotated_reviews_excluded": len(old_sampling),
        "eligible_unannotated_reviews": len(eligible),
        "new_sample_rows": len(selected_all),
        "new_sample_unique_duplicate_keys": int(
            selected_all["duplicate_key"].nunique()
        ),
        "new_sample_unique_parent_asin": int(
            selected_all["parent_asin"].nunique()
        ),
        "new_device_counts": dict(sorted(selected_counts.items())),
        "remaining_device_counts_before_sampling": dict(
            sorted(remaining_counts.items())
        ),
        "all_remaining_smart_switch_selected": True,
        "existing_overlap_count": 0,
        "double_review_rows": int(
            selected_all["selected_for_double_review"].sum()
        ),
        "cumulative_annotation_rows": 1500,
        "cumulative_double_review_rows": 300,
        "cumulative_double_review_share": 0.20,
        "scoring_summary": scoring_summary,
        "selection_audit": selection_audit,
        "double_selection_audit": double_audit,
        "balance_by_device_type": balance_device,
        "balance_by_bucket": balance_bucket,
        "balance_by_rating": balance_rating,
        "balance_by_time": balance_time,
        "sampling_is_population_representative": False,
        "model_predictions_are_labels": False,
    }
    write_json(report_dir / "sampling_flow.json", sampling_flow)
    write_json(
        report_dir / "privacy_blinding_audit.json",
        {
            "blind_csv_files": [
                relative(root, path) for path in csv_paths(interim_dir)
            ],
            "rating_exposed": False,
            "keyword_exposed": False,
            "model_probability_exposed": False,
            "parent_asin_exposed": False,
            "duplicate_key_exposed": False,
            "review_datetime_exposed": False,
            "source_domain_exposed": False,
            "human_labels_prefilled": False,
            "raw_user_id_present": False,
            "user_id_hash_present": False,
            "review_text_present_only_in_blind_annotation_files_and_private_sampling_frame": True,
        },
    )
    instructions = """# W5-C-A Expanded Annotation Instructions

Use only the visible `review_text`. Do not seek or infer the hidden rating,
keyword hit, model prediction, product identifier, date, or another reviewer's
decision. The expanded sample is selected for boundary coverage and is not
representative of population failure prevalence.

Use the same W5-A definitions:

- `failure_binary = 1`: explicit core-function failure or abnormal technical behavior.
- `failure_binary = 0`: no engineering failure, or only price, delivery, packaging, appearance, service, or another non-technical issue.
- `failure_binary = uncertain`: insufficient textual evidence.
- Failure type: `F1`–`F8`, with multiple codes separated by semicolons; `N0` only for non-failure.
- Severity: `0` no failure; `1` minor/recoverable; `2` core loss/repeated/return; `3` safety, permanent damage, or property risk.
- Persistence: `0` single/unknown; `1` intermittent/repeated; `2` continuous or unresolved after an attempted remedy.
- Confidence: `low`, `medium`, or `high`.

Reviewer 2 must complete the separate 60-row workbook independently. Do not
compare reviewer decisions before both independent passes are complete.
"""
    (report_dir / "expanded_annotation_instructions.md").write_text(
        instructions, encoding="utf-8"
    )
    write_json(
        report_dir / "w5c_a_disk_usage.json",
        {
            "initial_free_gib": initial_free,
            "after_preparation_free_gib": disk_free_gib(root),
            "minimum_required_free_gib": 60,
            "process_peak_working_set_bytes": process_peak_working_set_bytes(),
        },
    )
    prepared_status = {
        "phase": PHASE,
        "status": "PREPARED_FOR_WORKBOOK_BUILD",
        "sampling_version": SAMPLING_VERSION,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - start_monotonic,
        "new_sample_rows": len(selected_all),
        "new_device_counts": dict(sorted(selected_counts.items())),
        "double_review_rows": int(
            selected_all["selected_for_double_review"].sum()
        ),
        "blind_csv_files": [
            file_identity(root, path) for path in csv_paths(interim_dir)
        ],
        "protected_inputs_before": protected_before,
        "model_refit": False,
        "predictions_used_as_labels": False,
        "raw_jsonl_read": False,
        "compressed_source_read": False,
        "w6_executed": False,
        "git_commit_created": False,
    }
    write_json(report_dir / "w5c_a_status.json", prepared_status)
    log_message(log_path, "W5-C-A prepared for artifact-tool workbook build")
    return 0


def finalize() -> int:
    root = project_root()
    config = load_toml(root / "config" / "w5c_a_sampling_rules.toml")
    interim_dir = root / config["outputs"]["interim_dir"]
    report_dir = root / config["outputs"]["report_dir"]
    log_path = report_dir / "w5c_a_execution.log"
    validation_path = root / config["outputs"]["workbook_validation"]
    if not validation_path.is_file():
        raise W5CAError("artifact-tool workbook validation report is missing")
    validation = load_json(validation_path)
    if validation.get("status") != "PASS" or validation.get("workbook_count") != 8:
        raise W5CAError("artifact-tool workbook validation did not pass")
    status = load_json(report_dir / "w5c_a_status.json")
    if status.get("status") not in {
        "PREPARED_FOR_WORKBOOK_BUILD",
        "PAUSED_HUMAN_ANNOTATION",
    }:
        raise W5CAError("W5-C-A is not ready for finalization")
    all_workbooks = workbook_paths(interim_dir)
    if any(not path.is_file() for path in all_workbooks):
        raise W5CAError("One or more required W5-C-A workbooks are missing")
    for batch in range(1, 5):
        reviewer_1_csv = (
            interim_dir / f"annotation_batch_{batch}_reviewer1_blind.csv"
        )
        reviewer_2_csv = (
            interim_dir / f"annotation_batch_{batch}_reviewer2_blind.csv"
        )
        validate_blind_csv(reviewer_1_csv, 300, MAIN_BLIND_COLUMNS)
        validate_blind_csv(reviewer_2_csv, 60, REVIEWER_2_COLUMNS)
    sampling = pq.read_table(
        root / config["outputs"]["sampling_frame"]
    ).to_pandas()
    old = pq.read_table(
        root / config["inputs"]["existing_sampling_frame"],
        columns=["duplicate_key"],
    ).to_pandas()
    if len(sampling) != 1200 or not sampling["duplicate_key"].is_unique:
        raise W5CAError("Final sampling frame identity mismatch")
    if set(sampling["duplicate_key"]).intersection(set(old["duplicate_key"])):
        raise W5CAError("Final sampling frame overlaps original 300")
    if int(sampling["selected_for_double_review"].sum()) != 240:
        raise W5CAError("Final double-review count differs from 240")

    protected_after = protected_inputs(root, config)
    protected_before = status.get(
        "protected_inputs_before",
        status.get("protected_inputs_after"),
    )
    if protected_before is None:
        raise W5CAError("Protected-input baseline is missing from W5-C-A status")
    if protected_after != protected_before:
        raise W5CAError("A protected W5-B or formal input changed")
    final_free = disk_free_gib(root)
    if final_free < 60:
        raise W5CAError(f"PAUSED_SPACE_GATE: final free space {final_free:.3f} GiB")
    output_files = {
        "sampling_frame": parquet_identity(
            root, root / config["outputs"]["sampling_frame"]
        ),
        "blind_review_key": parquet_identity(
            root, root / config["outputs"]["blind_review_key"]
        ),
        "private_sampling_scores": parquet_identity(
            root, root / config["outputs"]["private_sampling_scores"]
        ),
        "workbooks": [
            file_identity(root, path) for path in all_workbooks
        ],
        "csv_files": [
            file_identity(root, path) for path in csv_paths(interim_dir)
        ],
    }
    final_status = {
        "phase": PHASE,
        "status": "PAUSED_HUMAN_ANNOTATION",
        "w5c_b_readiness": "WAITING_FOR_EXPANDED_ANNOTATION",
        "sampling_version": SAMPLING_VERSION,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "new_sample_rows": 1200,
        "new_device_counts": {
            "smart_plug": 927,
            "smart_bulb": 240,
            "smart_switch": 33,
        },
        "cumulative_annotation_rows_after_completion": 1500,
        "double_review_rows_new": 240,
        "cumulative_double_review_rows": 300,
        "cumulative_double_review_share": 0.20,
        "all_human_label_fields_empty": True,
        "workbook_validation": validation,
        "protected_inputs_unchanged": True,
        "protected_inputs_after": protected_after,
        "pilot_model_refit": False,
        "pilot_predictions_used_as_labels": False,
        "formal_reviews_available": 55877,
        "eligible_unannotated_reviews_scored_for_private_sampling_only": 55577,
        "raw_jsonl_read": False,
        "compressed_source_read": False,
        "product_month_failure_signals_created": False,
        "future_quality_target_created": False,
        "w6_executed": False,
        "git_commit_created": False,
        "final_free_gib": final_free,
        "outputs": output_files,
    }
    write_json(report_dir / "w5c_a_status.json", final_status)
    disk_report = load_json(report_dir / "w5c_a_disk_usage.json")
    disk_report["final_free_gib"] = final_free
    disk_report["space_gate_passed"] = True
    write_json(report_dir / "w5c_a_disk_usage.json", disk_report)
    log_message(
        log_path,
        "W5-C-A finalized: PAUSED_HUMAN_ANNOTATION; "
        "w5c_b_readiness=WAITING_FOR_EXPANDED_ANNOTATION",
    )
    return 0


def write_failure_status(error: Exception, status_name: str) -> None:
    try:
        root = project_root()
        report_dir = root / "data/amazon_reviews_2023/reports/w5c_a"
        report_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            report_dir / "w5c_a_status.json",
            {
                "phase": PHASE,
                "status": status_name,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "w6_executed": False,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Finalize after artifact-tool created and validated all workbooks.",
    )
    args = parser.parse_args()
    try:
        return finalize() if args.finalize else prepare()
    except W5CAError as error:
        status = (
            "PAUSED_SPACE_GATE"
            if str(error).startswith("PAUSED_SPACE_GATE")
            else "FAILED_ANNOTATION_SAMPLE"
        )
        write_failure_status(error, status)
        print(f"[{status}] {error}", file=sys.stderr)
        return 2
    except Exception as error:
        write_failure_status(error, "FAILED_ANNOTATION_SAMPLE")
        print(
            f"[FAILED_ANNOTATION_SAMPLE] {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
