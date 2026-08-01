"""Prepare the W5-A blind annotation package and descriptive B0/B3 baselines.

This script reads only the formal W3 v1.4.0 product and W4R review Parquets.
It never reads raw JSONL or compressed sources, never trains a classifier, and
never populates a human annotation label.
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PHASE = "W5-A"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
ANNOTATION_VERSION = "w5a-annotation-v1.0-draft"
KEYWORD_VERSION = "w5a-keyword-v1.0-draft"
ANALYSIS_ROLE = {
    "smart_plug": "primary",
    "smart_bulb": "exploratory",
    "smart_switch": "case_study",
}
FORBIDDEN_BLIND_COLUMNS = {
    "rating",
    "low_star",
    "keyword_hit",
    "keyword_candidate_hit",
    "parent_asin",
    "asin",
    "source_domain",
    "product_title",
    "review_datetime",
    "user_id_hash",
    "duplicate_key",
}
FORBIDDEN_MODEL_COLUMNS = {
    "future_target",
    "quality_deterioration",
    "split",
    "failure_binary",
    "failure_type",
    "severity",
    "persistence",
    "sentiment_score",
}
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
DOUBLE_BLIND_COLUMNS = [
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
HUMAN_LABEL_COLUMNS = [
    column
    for column in MAIN_BLIND_COLUMNS + DOUBLE_BLIND_COLUMNS
    if column.startswith(("reviewer_", "adjudicated_", "adjudication_"))
]


class W5AError(RuntimeError):
    """Controlled W5-A failure."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W5AError(f"Could not resolve project root from {__file__}")
    if not (root / "config" / "project.toml").is_file():
        raise W5AError("config/project.toml is missing.")
    return root


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash_int(seed: int, purpose: str, value: str) -> int:
    payload = f"{seed}|{purpose}|{value}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def file_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "path": relative(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
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
            "schema": str(parquet.schema_arrow),
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
    return result


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
    if hasattr(value, "item"):
        return value.item()
    if pd.isna(value) if not isinstance(value, (list, dict, tuple, set)) else False:
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_rows_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def normalize_for_rules(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).lower()
    return re.sub(r"\s+", " ", text).strip()


class KeywordRules:
    """Compiled transparent keyword/rule draft."""

    def __init__(self, raw: dict[str, Any]):
        if raw["rules"]["version"] != KEYWORD_VERSION:
            raise W5AError("Unexpected keyword rule version.")
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
        self.categories: list[dict[str, Any]] = []
        for category in raw["category"]:
            self.categories.append(
                {
                    "code": category["code"],
                    "name": category["name"],
                    "patterns": [
                        re.compile(pattern, flags) for pattern in category["patterns"]
                    ],
                }
            )

    def classify(self, value: Any) -> tuple[bool, tuple[str, ...]]:
        text = normalize_for_rules(value)
        if not text:
            return False, ()
        codes = [
            category["code"]
            for category in self.categories
            if any(pattern.search(text) for pattern in category["patterns"])
        ]
        if codes:
            return True, tuple(codes)
        has_context = any(pattern.search(text) for pattern in self.device_context)
        has_general_failure = any(
            pattern.search(text) for pattern in self.general_failure
        )
        only_non_engineering = any(
            pattern.search(text) for pattern in self.non_engineering
        )
        if has_context and has_general_failure and not only_non_engineering:
            return True, ("F1",)
        return False, ()


def rating_stratum(value: Any) -> str:
    rating = float(value)
    if rating <= 2:
        return "low_1_2"
    if rating == 3:
        return "middle_3"
    return "high_4_5"


def time_stratum(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    year = timestamp.year
    if year <= 2017:
        return "early_2011_2017"
    if year <= 2020:
        return "middle_2018_2020"
    return "recent_2021_2023"


def proportional_targets(total: int, shares: dict[str, float]) -> dict[str, int]:
    raw = {key: total * share for key, share in shares.items()}
    targets = {key: math.floor(value) for key, value in raw.items()}
    remaining = total - sum(targets.values())
    order = sorted(
        shares,
        key=lambda key: (raw[key] - targets[key], key),
        reverse=True,
    )
    for key in order[:remaining]:
        targets[key] += 1
    return targets


def balanced_select(
    pool: pd.DataFrame,
    quota: int,
    *,
    seed: int,
    purpose: str,
) -> pd.DataFrame:
    """Deterministically select rows while prioritizing parent coverage and balance."""
    if quota > len(pool):
        raise W5AError(f"{purpose}: quota {quota} exceeds available {len(pool)}.")
    records = pool.to_dict("records")
    for row in records:
        row["_stable_order"] = stable_hash_int(
            seed, purpose, str(row["duplicate_key"])
        )

    keyword_targets = proportional_targets(
        quota, {"hit": 0.50, "non_hit": 0.50}
    )
    rating_targets = proportional_targets(
        quota, {"low_1_2": 0.35, "middle_3": 0.20, "high_4_5": 0.45}
    )
    time_targets = proportional_targets(
        quota,
        {
            "early_2011_2017": 1 / 3,
            "middle_2018_2020": 1 / 3,
            "recent_2021_2023": 1 / 3,
        },
    )
    selected: list[dict[str, Any]] = []
    parent_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    rating_counts: Counter[str] = Counter()
    time_counts: Counter[str] = Counter()

    while len(selected) < quota:
        best_index = -1
        best_score: tuple[Any, ...] | None = None
        for index, row in enumerate(records):
            keyword_key = "hit" if row["keyword_candidate_hit"] else "non_hit"
            keyword_pressure = keyword_counts[keyword_key] / max(
                keyword_targets[keyword_key], 1
            )
            rating_pressure = rating_counts[row["rating_stratum"]] / max(
                rating_targets[row["rating_stratum"]], 1
            )
            period_pressure = time_counts[row["time_stratum"]] / max(
                time_targets[row["time_stratum"]], 1
            )
            score = (
                parent_counts[str(row["parent_asin"])],
                keyword_pressure,
                rating_pressure,
                period_pressure,
                row["_stable_order"],
            )
            if best_score is None or score < best_score:
                best_score = score
                best_index = index
        chosen = records.pop(best_index)
        selected.append(chosen)
        parent_counts[str(chosen["parent_asin"])] += 1
        keyword_counts["hit" if chosen["keyword_candidate_hit"] else "non_hit"] += 1
        rating_counts[chosen["rating_stratum"]] += 1
        time_counts[chosen["time_stratum"]] += 1

    frame = pd.DataFrame(selected)
    return frame.drop(columns=["_stable_order"], errors="ignore")


def validate_human_columns_empty(frame: pd.DataFrame) -> None:
    relevant = [column for column in frame.columns if column in HUMAN_LABEL_COLUMNS]
    for column in relevant:
        nonempty = frame[column].fillna("").astype(str).str.strip().ne("")
        if bool(nonempty.any()):
            raise W5AError(f"Human annotation column is not empty: {column}")


def validate_blind_columns(frame: pd.DataFrame, expected: list[str]) -> None:
    if list(frame.columns) != expected:
        raise W5AError(
            f"Blind file fields differ from the approved schema: {frame.columns.tolist()}"
        )
    forbidden = FORBIDDEN_BLIND_COLUMNS.intersection(frame.columns)
    if forbidden:
        raise W5AError(f"Blind file exposes forbidden fields: {sorted(forbidden)}")
    validate_human_columns_empty(frame)


def safe_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    table = pa.Table.from_pandas(frame, preserve_index=False)
    forbidden = FORBIDDEN_MODEL_COLUMNS.intersection(table.schema.names)
    if forbidden and path.name.endswith("_descriptive.parquet"):
        raise W5AError(f"Descriptive output contains forbidden fields: {forbidden}")
    pq.write_table(table, path, compression="zstd")
    test = pq.read_table(path)
    if test.num_rows != len(frame):
        raise W5AError(f"Parquet row count mismatch after writing {path.name}.")


def create_annotation_guide(path: Path) -> None:
    content = """# W5-A Manual Annotation Guide

Version: `w5a-annotation-v1.0-draft`

## Purpose

Label what the review text explicitly says. Do not infer from star rating, product popularity, or a desire to increase the number of failures. The annotation sample is stratified for boundary coverage and is not representative of the population failure rate.

## Failure binary

- `1`: The text clearly describes failure of a core intended function or abnormal technical behavior.
- `0`: No engineering failure is described, or the issue concerns only price, delivery, packaging, appearance, customer service, or another non-technical matter.
- `uncertain`: The text does not provide enough evidence; leave the final decision for adjudication.

A low rating is never sufficient evidence of an engineering failure.

## Failure type

Use one or more codes separated by semicolons when multiple mechanisms are explicitly present.

- `F1`: Power supply, charging, relay, or hardware failure.
- `F2`: Connectivity or network failure.
- `F3`: Installation, setup, or pairing failure.
- `F4`: Firmware, software, or app failure.
- `F5`: Automation, voice-assistant, ecosystem, or compatibility failure.
- `F6`: Intermittent behavior, instability, latency, or random restart.
- `F7`: Safety, overheating, smoke, spark, shock, or electrical hazard.
- `F8`: Durability, premature wear, repeated breakage, or shortened service life.
- `N0`: No engineering failure. Use only with `failure_binary = 0`.

## Severity

- `0`: No engineering failure.
- `1`: Minor or temporary issue recoverable with one retry, reset, or simple action.
- `2`: Core-function loss, repeated failure, or a problem requiring return or replacement.
- `3`: Overheating, electrical or safety risk, permanent damage, or property risk.

## Persistence (review-level text evidence only)

- `0`: Single incident, unknown recurrence, or no explicit repetition evidence.
- `1`: Intermittent, repeated, or recurring behavior is explicitly described.
- `2`: Continuous failure or failure remaining after reset, upgrade, reinstallation, or another attempted remedy.

Do not use other reviews or future months to assign Persistence. Product-level cross-month Persistence is outside W5-A.

## Confidence and notes

Use `low`, `medium`, or `high` confidence. Notes should briefly identify the textual evidence or the source of uncertainty. Do not consult the hidden rating, keyword rule, product identifier, or another reviewer’s decisions.

## Double review and adjudication

Reviewer 2 independently labels the separate 60-row workbook without seeing Reviewer 1 results. After both independent passes are complete, disagreements are resolved in the adjudication columns of the 300-row workbook.
"""
    path.write_text(content, encoding="utf-8")


def create_sampling_outputs(
    reviews: pd.DataFrame,
    annotation_config: dict[str, Any],
    rules: KeywordRules,
    interim_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    reviews = reviews.copy()
    if reviews["duplicate_key"].isna().any() or not reviews["duplicate_key"].is_unique:
        raise W5AError("Formal review duplicate_key values must be nonempty and unique.")
    if reviews["parent_asin"].isna().any():
        raise W5AError("Formal review parent_asin contains null values.")
    reviews["review_text"] = reviews["review_text"].fillna("").astype(str)
    if reviews["review_text"].str.strip().eq("").any():
        raise W5AError("Formal input contains an empty review_text.")

    classifications = reviews["review_text"].map(rules.classify)
    reviews["keyword_candidate_hit"] = classifications.map(lambda item: item[0])
    reviews["keyword_failure_types"] = classifications.map(
        lambda item: ";".join(item[1])
    )
    reviews["rating_stratum"] = reviews["rating"].map(rating_stratum)
    reviews["time_stratum"] = reviews["review_datetime"].map(time_stratum)
    reviews["sampling_stratum"] = (
        reviews["device_type"].astype(str)
        + "|"
        + reviews["keyword_candidate_hit"].map(
            {True: "keyword_hit", False: "keyword_non_hit"}
        )
        + "|"
        + reviews["rating_stratum"]
        + "|"
        + reviews["time_stratum"]
    )

    seed = int(annotation_config["phase"]["random_seed"])
    quotas = annotation_config["sampling"]["device_quotas"]
    samples: list[pd.DataFrame] = []
    for device_type in DEVICE_TYPES:
        pool = reviews[reviews["device_type"] == device_type].copy()
        selected = balanced_select(
            pool,
            int(quotas[device_type]),
            seed=seed,
            purpose=f"annotation-{device_type}",
        )
        samples.append(selected)
    sample = pd.concat(samples, ignore_index=True)
    if len(sample) != 300 or not sample["duplicate_key"].is_unique:
        raise W5AError("The annotation sample is not 300 unique reviews.")

    sample["_blind_order"] = sample["duplicate_key"].map(
        lambda key: stable_hash_int(seed, "blind-order", str(key))
    )
    sample = sample.sort_values("_blind_order", kind="mergesort").reset_index(drop=True)
    sample["blind_review_id"] = [
        f"W5A-{index:03d}" for index in range(1, len(sample) + 1)
    ]
    sample = sample.drop(columns=["_blind_order"])

    double_quotas = annotation_config["sampling"]["double_review_quotas"]
    double_samples: list[pd.DataFrame] = []
    for device_type in DEVICE_TYPES:
        pool = sample[sample["device_type"] == device_type].copy()
        selected = balanced_select(
            pool,
            int(double_quotas[device_type]),
            seed=seed,
            purpose=f"double-review-{device_type}",
        )
        double_samples.append(selected)
    double_sample = pd.concat(double_samples, ignore_index=True)
    double_ids = set(double_sample["blind_review_id"].tolist())
    if len(double_sample) != 60 or len(double_ids) != 60:
        raise W5AError("The double-review subset is not 60 unique reviews.")
    sample["selected_for_double_review"] = sample["blind_review_id"].isin(double_ids)

    sampling_columns = [
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
        "rating_stratum",
        "time_stratum",
        "sampling_stratum",
        "selected_for_double_review",
    ]
    sampling_frame = sample[sampling_columns].copy()
    safe_write_parquet(
        sampling_frame, interim_dir / "annotation_sampling_frame.parquet"
    )

    private_key = sampling_frame[
        [
            "blind_review_id",
            "duplicate_key",
            "parent_asin",
            "rating",
            "review_datetime",
            "source_domain",
            "keyword_candidate_hit",
            "sampling_stratum",
        ]
    ].copy()
    safe_write_parquet(private_key, interim_dir / "blind_review_key.parquet")

    main_blind = sample[["blind_review_id", "device_type", "review_text"]].copy()
    for column in MAIN_BLIND_COLUMNS[3:]:
        main_blind[column] = ""
    main_blind = main_blind[MAIN_BLIND_COLUMNS]
    validate_blind_columns(main_blind, MAIN_BLIND_COLUMNS)
    main_blind.to_csv(
        interim_dir / "annotation_batch_300_blind.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )

    double_blind = double_sample[
        ["blind_review_id", "device_type", "review_text"]
    ].copy()
    for column in DOUBLE_BLIND_COLUMNS[3:]:
        double_blind[column] = ""
    double_blind = double_blind[DOUBLE_BLIND_COLUMNS]
    double_blind["_order"] = double_blind["blind_review_id"].map(
        lambda value: int(str(value).split("-")[-1])
    )
    double_blind = double_blind.sort_values("_order").drop(columns=["_order"])
    validate_blind_columns(double_blind, DOUBLE_BLIND_COLUMNS)
    double_blind.to_csv(
        interim_dir / "annotation_double_review_60_blind.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )

    balances: dict[str, list[dict[str, Any]]] = {}
    balances["device"] = (
        sample.groupby("device_type", observed=True)
        .agg(
            sampled_reviews=("blind_review_id", "size"),
            independent_products=("parent_asin", "nunique"),
            keyword_hits=("keyword_candidate_hit", "sum"),
            keyword_non_hits=(
                "keyword_candidate_hit",
                lambda values: int((~values).sum()),
            ),
        )
        .reset_index()
        .to_dict("records")
    )
    balances["rating"] = (
        sample.groupby(["device_type", "rating_stratum"], observed=True)
        .size()
        .rename("sampled_reviews")
        .reset_index()
        .to_dict("records")
    )
    balances["time"] = (
        sample.groupby(["device_type", "time_stratum"], observed=True)
        .size()
        .rename("sampled_reviews")
        .reset_index()
        .to_dict("records")
    )
    write_rows_csv(
        report_dir / "annotation_balance_by_device_type.csv",
        balances["device"],
        [
            "device_type",
            "sampled_reviews",
            "independent_products",
            "keyword_hits",
            "keyword_non_hits",
        ],
    )
    write_rows_csv(
        report_dir / "annotation_balance_by_rating_stratum.csv",
        balances["rating"],
        ["device_type", "rating_stratum", "sampled_reviews"],
    )
    write_rows_csv(
        report_dir / "annotation_balance_by_time_stratum.csv",
        balances["time"],
        ["device_type", "time_stratum", "sampled_reviews"],
    )

    sampling_flow = {
        "phase": PHASE,
        "annotation_version": ANNOTATION_VERSION,
        "random_seed": seed,
        "formal_review_rows": len(reviews),
        "sample_rows": len(sample),
        "unique_duplicate_keys": int(sample["duplicate_key"].nunique()),
        "unique_parent_asin": int(sample["parent_asin"].nunique()),
        "device_quotas": {
            key: int(value) for key, value in quotas.items()
        },
        "double_review_rows": len(double_sample),
        "double_review_quotas": {
            key: int(value) for key, value in double_quotas.items()
        },
        "keyword_hit_rows": int(sample["keyword_candidate_hit"].sum()),
        "keyword_non_hit_rows": int((~sample["keyword_candidate_hit"]).sum()),
        "blind_review_ids_unique": bool(sample["blind_review_id"].is_unique),
        "labels_prefilled": False,
        "rating_exposed_to_annotators": False,
        "keyword_exposed_to_annotators": False,
        "sampling_is_population_representative": False,
        "balance_by_device_type": balances["device"],
        "balance_by_rating_stratum": balances["rating"],
        "balance_by_time_stratum": balances["time"],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    write_json(report_dir / "annotation_sampling_flow.json", sampling_flow)
    flow_rows = [
        {"stage": "formal_review_rows", "count": len(reviews)},
        {"stage": "annotation_sample", "count": len(sample)},
        {
            "stage": "sample_unique_parent_asin",
            "count": sample["parent_asin"].nunique(),
        },
        {"stage": "sample_keyword_hits", "count": sample["keyword_candidate_hit"].sum()},
        {
            "stage": "sample_keyword_non_hits",
            "count": (~sample["keyword_candidate_hit"]).sum(),
        },
        {"stage": "double_review_subset", "count": len(double_sample)},
    ]
    write_rows_csv(
        report_dir / "annotation_sampling_flow.csv", flow_rows, ["stage", "count"]
    )
    return {
        "sample": sample,
        "classifications": reviews[
            [
                "parent_asin",
                "review_month",
                "device_type",
                "keyword_candidate_hit",
                "keyword_failure_types",
            ]
        ].copy(),
        "sampling_flow": sampling_flow,
    }


def create_rating_baseline(
    reviews: pd.DataFrame, interim_dir: Path, report_dir: Path
) -> dict[str, Any]:
    frame = reviews[
        ["parent_asin", "review_month", "device_type", "rating"]
    ].copy()
    frame["analysis_role"] = frame["device_type"].map(ANALYSIS_ROLE)
    frame["low_star"] = frame["rating"].astype(float).le(2.0)
    monthly = (
        frame.groupby(
            ["parent_asin", "review_month", "device_type", "analysis_role"],
            observed=True,
            sort=True,
        )
        .agg(
            n_reviews=("rating", "size"),
            mean_rating=("rating", "mean"),
            low_star_count=("low_star", "sum"),
        )
        .reset_index()
    )
    monthly["n_reviews"] = monthly["n_reviews"].astype("int64")
    monthly["low_star_count"] = monthly["low_star_count"].astype("int64")
    monthly["low_star_share"] = monthly["low_star_count"] / monthly["n_reviews"]
    monthly = monthly[
        [
            "parent_asin",
            "review_month",
            "device_type",
            "analysis_role",
            "n_reviews",
            "mean_rating",
            "low_star_count",
            "low_star_share",
        ]
    ]
    safe_write_parquet(
        monthly, interim_dir / "rating_product_month_descriptive.parquet"
    )
    summary_rows = []
    for device_type, group in frame.groupby("device_type", observed=True):
        summary_rows.append(
            {
                "device_type": device_type,
                "analysis_role": ANALYSIS_ROLE[device_type],
                "reviews": len(group),
                "products": group["parent_asin"].nunique(),
                "product_months": int(
                    monthly[monthly["device_type"] == device_type].shape[0]
                ),
                "mean_rating": float(group["rating"].mean()),
                "low_star_count": int(group["low_star"].sum()),
                "low_star_share": float(group["low_star"].mean()),
            }
        )
    summary = {
        "baseline": "B0 Rating",
        "description": "Descriptive rating-only product-month baseline",
        "low_star_definition": "rating <= 2",
        "low_star_is_engineering_failure_label": False,
        "future_target_created": False,
        "split_created": False,
        "early_warning_comparison_performed": False,
        "product_month_rows": len(monthly),
        "by_device_type": summary_rows,
    }
    write_json(report_dir / "rating_baseline_summary.json", summary)
    return summary


def create_keyword_baseline(
    reviews: pd.DataFrame,
    classifications: pd.DataFrame,
    interim_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    frame = classifications.copy()
    frame["analysis_role"] = frame["device_type"].map(ANALYSIS_ROLE)
    monthly = (
        frame.groupby(
            ["parent_asin", "review_month", "device_type", "analysis_role"],
            observed=True,
            sort=True,
        )
        .agg(
            n_reviews=("keyword_candidate_hit", "size"),
            keyword_hit_count=("keyword_candidate_hit", "sum"),
        )
        .reset_index()
    )
    monthly["n_reviews"] = monthly["n_reviews"].astype("int64")
    monthly["keyword_hit_count"] = monthly["keyword_hit_count"].astype("int64")
    monthly["keyword_hit_share"] = (
        monthly["keyword_hit_count"] / monthly["n_reviews"]
    )
    monthly["keyword_version"] = KEYWORD_VERSION
    monthly = monthly[
        [
            "parent_asin",
            "review_month",
            "device_type",
            "analysis_role",
            "n_reviews",
            "keyword_hit_count",
            "keyword_hit_share",
            "keyword_version",
        ]
    ]
    safe_write_parquet(
        monthly, interim_dir / "keyword_product_month_descriptive.parquet"
    )

    category_counts: Counter[str] = Counter()
    for value in frame["keyword_failure_types"]:
        for code in str(value).split(";"):
            if code:
                category_counts[code] += 1
    by_device = []
    for device_type, group in frame.groupby("device_type", observed=True):
        hits = int(group["keyword_candidate_hit"].sum())
        by_device.append(
            {
                "device_type": device_type,
                "analysis_role": ANALYSIS_ROLE[device_type],
                "reviews": len(group),
                "keyword_hits": hits,
                "keyword_hit_share": hits / len(group),
                "product_months": int(
                    monthly[monthly["device_type"] == device_type].shape[0]
                ),
            }
        )
    total_hits = int(frame["keyword_candidate_hit"].sum())
    summary = {
        "baseline": "B3 Keyword/rule draft",
        "keyword_version": KEYWORD_VERSION,
        "ratings_used_for_keyword_decision": False,
        "human_labels_used": False,
        "precision_reported": False,
        "recall_reported": False,
        "f1_reported": False,
        "future_target_created": False,
        "split_created": False,
        "review_rows": len(reviews),
        "keyword_hit_rows": total_hits,
        "keyword_non_hit_rows": len(reviews) - total_hits,
        "keyword_hit_share": total_hits / len(reviews),
        "product_month_rows": len(monthly),
        "category_hit_counts_overlapping": dict(sorted(category_counts.items())),
        "by_device_type": by_device,
    }
    write_json(report_dir / "keyword_baseline_summary.json", summary)
    return summary


def verify_input_status(root: Path) -> dict[str, Any]:
    w4r_status_path = (
        root / "data/amazon_reviews_2023/reports/w4r/w4r_status.json"
    )
    readiness_path = (
        root / "data/amazon_reviews_2023/reports/w4r/w5_readiness.json"
    )
    status = json.loads(w4r_status_path.read_text(encoding="utf-8"))
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    if status.get("status") != "PASS":
        raise W5AError("W4R status is not PASS.")
    readiness_value = readiness.get("w5_readiness") or readiness.get("status")
    if readiness_value != "REVIEW_REQUIRED":
        raise W5AError(
            f"W4R W5 readiness must be REVIEW_REQUIRED, got {readiness_value!r}."
        )
    return {
        "w4r_status": status.get("status"),
        "w5_readiness": readiness_value,
        "w4r_status_path": relative(root, w4r_status_path),
        "w5_readiness_path": relative(root, readiness_path),
    }


def package_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "python_bits": platform.architecture()[0],
        "pandas": importlib.metadata.version("pandas"),
        "pyarrow": importlib.metadata.version("pyarrow"),
        "scikit-learn": importlib.metadata.version("scikit-learn"),
        "orjson": importlib.metadata.version("orjson"),
    }


def prepare() -> int:
    root = project_root()
    annotation_path = root / "config" / "annotation_rules_w5a.toml"
    keyword_path = root / "config" / "failure_keyword_rules_w5a.toml"
    annotation_config = load_toml(annotation_path)
    keyword_config = load_toml(keyword_path)
    if annotation_config["phase"]["annotation_version"] != ANNOTATION_VERSION:
        raise W5AError("Unexpected annotation rule version.")
    _ = load_toml(root / "config" / "project.toml")
    rules = KeywordRules(keyword_config)

    interim_dir = root / annotation_config["outputs"]["interim_dir"]
    report_dir = root / annotation_config["outputs"]["report_dir"]
    known_outputs = [
        interim_dir / "annotation_sampling_frame.parquet",
        interim_dir / "blind_review_key.parquet",
        interim_dir / "annotation_batch_300_blind.csv",
        interim_dir / "annotation_batch_300_blind.xlsx",
        interim_dir / "annotation_double_review_60_blind.csv",
        interim_dir / "annotation_double_review_60_blind.xlsx",
        interim_dir / "rating_product_month_descriptive.parquet",
        interim_dir / "keyword_product_month_descriptive.parquet",
    ]
    if interim_dir.exists() or report_dir.exists():
        existing = [path for path in known_outputs if path.exists()]
        if existing:
            raise W5AError(
                "Existing W5-A outputs found; refusing to overwrite: "
                + ", ".join(relative(root, path) for path in existing)
            )
    interim_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w5a_execution.log"
    started_utc = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()
    start_free = disk_free_gib(root)
    if start_free < 60:
        raise W5AError(f"PAUSED_SPACE_GATE: only {start_free:.3f} GiB free.")

    def log(message: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")

    log("W5-A preparation started.")
    status_inputs = verify_input_status(root)
    review_path = root / annotation_config["inputs"]["review_parquet"]
    product_path = root / annotation_config["inputs"]["product_parquet"]
    review_before = parquet_identity(root, review_path)
    product_before = parquet_identity(root, product_path)
    if (
        review_before["rows"] != annotation_config["inputs"]["review_rows"]
        or review_before["sha256"] != annotation_config["inputs"]["review_sha256"]
    ):
        raise W5AError("FAILED_INPUT_MISMATCH: formal review input mismatch.")
    if (
        product_before["rows"] != annotation_config["inputs"]["product_rows"]
        or product_before["sha256"] != annotation_config["inputs"]["product_sha256"]
    ):
        raise W5AError("FAILED_INPUT_MISMATCH: formal product input mismatch.")
    log("Formal input identities verified.")

    product_counts = (
        pq.read_table(product_path, columns=["parent_asin", "device_type"])
        .to_pandas()
        .groupby("device_type", observed=True)["parent_asin"]
        .nunique()
        .to_dict()
    )
    if product_counts != {
        "smart_plug": 95,
        "smart_bulb": 25,
        "smart_switch": 5,
    }:
        raise W5AError(f"Formal product counts mismatch: {product_counts}")

    review_columns = [
        "duplicate_key",
        "parent_asin",
        "rating",
        "review_datetime",
        "review_month",
        "source_domain",
        "device_type",
        "review_text",
    ]
    reviews = pq.read_table(review_path, columns=review_columns).to_pandas()
    if len(reviews) != 55_877:
        raise W5AError("Formal review row count changed during read.")
    log("Formal review columns loaded from the approved Parquet.")

    sampling = create_sampling_outputs(
        reviews, annotation_config, rules, interim_dir, report_dir
    )
    log("Generated 300-row blind sample and 60-row double-review subset.")
    rating_summary = create_rating_baseline(reviews, interim_dir, report_dir)
    log("Generated B0 Rating product-month descriptive output.")
    keyword_summary = create_keyword_baseline(
        reviews,
        sampling["classifications"],
        interim_dir,
        report_dir,
    )
    log("Generated B3 Keyword/rule draft product-month descriptive output.")
    create_annotation_guide(report_dir / "manual_annotation_guide.md")

    review_after = parquet_identity(root, review_path)
    product_after = parquet_identity(root, product_path)
    if review_before != review_after or product_before != product_after:
        raise W5AError("A formal input changed during W5-A.")

    end_free = disk_free_gib(root)
    peak_bytes = process_peak_working_set_bytes()
    manifest = {
        "phase": PHASE,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": package_versions(),
        "status_inputs": status_inputs,
        "formal_inputs_before": {
            "reviews": review_before,
            "products": product_before,
        },
        "formal_inputs_after": {
            "reviews": review_after,
            "products": product_after,
        },
        "input_files_unchanged": True,
        "configs": {
            "annotation": {
                **file_identity(root, annotation_path),
                "version": ANNOTATION_VERSION,
            },
            "keyword": {
                **file_identity(root, keyword_path),
                "version": KEYWORD_VERSION,
            },
        },
        "product_counts": product_counts,
        "prohibited_sources_read": [],
        "raw_jsonl_read": False,
        "compressed_files_read": False,
        "old_review_parquet_used_as_formal_input": False,
        "models_trained": [],
        "future_target_created": False,
        "split_created": False,
    }
    write_json(report_dir / "w5a_input_manifest.json", manifest)
    disk_report = {
        "phase": PHASE,
        "started_at_utc": started_utc,
        "completed_preparation_at_utc": datetime.now(timezone.utc).isoformat(),
        "free_gib_before": start_free,
        "free_gib_after_preparation": end_free,
        "minimum_free_gib": 60,
        "space_gate_passed": end_free >= 60,
        "peak_process_working_set_mib": (
            peak_bytes / (1024**2) if peak_bytes is not None else None
        ),
    }
    write_json(report_dir / "w5a_disk_usage.json", disk_report)
    provisional = {
        "phase": PHASE,
        "status": "PREPARED_AWAITING_WORKBOOK_VALIDATION",
        "w5b_readiness": "NOT_READY",
        "annotation_version": ANNOTATION_VERSION,
        "keyword_version": KEYWORD_VERSION,
        "sample_rows": 300,
        "double_review_rows": 60,
        "rating_product_month_rows": rating_summary["product_month_rows"],
        "keyword_hit_rows": keyword_summary["keyword_hit_rows"],
        "keyword_product_month_rows": keyword_summary["product_month_rows"],
        "all_human_labels_empty": True,
        "blind_fields_validated": True,
        "formal_inputs_unchanged": True,
        "workbook_validation_pending": True,
        "elapsed_seconds": round(time.perf_counter() - started_perf, 3),
    }
    write_json(report_dir / "w5a_status.json", provisional)
    log("Preparation complete; workbook generation and validation remain.")
    return 0


def finalize() -> int:
    root = project_root()
    annotation_config = load_toml(root / "config" / "annotation_rules_w5a.toml")
    interim_dir = root / annotation_config["outputs"]["interim_dir"]
    report_dir = root / annotation_config["outputs"]["report_dir"]
    validation_path = report_dir / "workbook_validation.json"
    if not validation_path.is_file():
        raise W5AError("Workbook validation report is missing.")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "PASS":
        raise W5AError("Workbook validation did not pass.")
    if validation.get("workbookCount") != 2:
        raise W5AError("Workbook validation does not cover both workbooks.")
    expected_workbooks = {(300, 14, 1), (60, 9, 2)}
    observed_workbooks = {
        (
            int(item.get("rowCount", -1)),
            int(item.get("columnCount", -1)),
            int(item.get("reviewerNumber", -1)),
        )
        for item in validation.get("workbooks", [])
    }
    if observed_workbooks != expected_workbooks:
        raise W5AError(
            f"Workbook structural validation mismatch: {observed_workbooks}"
        )
    validation["visualInspectionPendingByCodex"] = False
    validation["visualInspectionByCodex"] = (
        "PASS: rendered top, middle, bottom, and instructions views were "
        "visually inspected for both workbooks."
    )
    validation["visualInspectionCompletedAtUtc"] = datetime.now(
        timezone.utc
    ).isoformat()
    write_json(validation_path, validation)

    main_csv = pd.read_csv(
        interim_dir / "annotation_batch_300_blind.csv",
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    double_csv = pd.read_csv(
        interim_dir / "annotation_double_review_60_blind.csv",
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    validate_blind_columns(main_csv, MAIN_BLIND_COLUMNS)
    validate_blind_columns(double_csv, DOUBLE_BLIND_COLUMNS)
    if len(main_csv) != 300 or len(double_csv) != 60:
        raise W5AError("Blind CSV row counts changed before finalization.")
    private_key = pq.read_table(
        interim_dir / "blind_review_key.parquet",
        columns=["blind_review_id", "duplicate_key"],
    ).to_pandas()
    if len(private_key) != 300:
        raise W5AError("Private blind key does not contain 300 rows.")
    if not private_key["blind_review_id"].is_unique:
        raise W5AError("Private blind key contains repeated blind_review_id.")
    if not private_key["duplicate_key"].is_unique:
        raise W5AError("Private blind key contains repeated duplicate_key.")
    if set(private_key["blind_review_id"]) != set(main_csv["blind_review_id"]):
        raise W5AError("Private blind key is not one-to-one with the 300-row CSV.")
    if not set(double_csv["blind_review_id"]).issubset(
        set(main_csv["blind_review_id"])
    ):
        raise W5AError("Reviewer 2 blind IDs are not a subset of Reviewer 1 IDs.")
    for name in (
        "annotation_batch_300_blind.xlsx",
        "annotation_double_review_60_blind.xlsx",
    ):
        if not (interim_dir / name).is_file():
            raise W5AError(f"Missing workbook: {name}")

    existing_status_path = report_dir / "w5a_status.json"
    previously_cleaned: list[str] = []
    if existing_status_path.is_file():
        previous_status = json.loads(
            existing_status_path.read_text(encoding="utf-8")
        )
        previously_cleaned = list(
            previous_status.get("cleaned_current_run_inspection_sidecars", [])
        )
    cleaned_sidecars: list[str] = []
    for sidecar in interim_dir.glob("*.xlsx.inspect.ndjson"):
        resolved = sidecar.resolve()
        if resolved.parent != interim_dir.resolve():
            raise W5AError(f"Refusing to clean unsafe sidecar path: {sidecar}")
        sidecar.unlink()
        cleaned_sidecars.append(relative(root, sidecar))
    newly_cleaned_sidecars = list(cleaned_sidecars)
    cleaned_sidecars = list(dict.fromkeys(previously_cleaned + cleaned_sidecars))

    review_path = root / annotation_config["inputs"]["review_parquet"]
    product_path = root / annotation_config["inputs"]["product_parquet"]
    review_identity = parquet_identity(root, review_path)
    product_identity = parquet_identity(root, product_path)
    if (
        review_identity["sha256"] != annotation_config["inputs"]["review_sha256"]
        or product_identity["sha256"] != annotation_config["inputs"]["product_sha256"]
    ):
        raise W5AError("Formal input changed before finalization.")
    if disk_free_gib(root) < 60:
        raise W5AError("PAUSED_SPACE_GATE during finalization.")
    disk_path = report_dir / "w5a_disk_usage.json"
    disk_report = json.loads(disk_path.read_text(encoding="utf-8"))
    disk_report["finalized_at_utc"] = datetime.now(timezone.utc).isoformat()
    disk_report["final_free_gib"] = disk_free_gib(root)
    disk_report["final_space_gate_passed"] = disk_report["final_free_gib"] >= 60
    write_json(disk_path, disk_report)

    sampling_flow = json.loads(
        (report_dir / "annotation_sampling_flow.json").read_text(encoding="utf-8")
    )
    rating_summary = json.loads(
        (report_dir / "rating_baseline_summary.json").read_text(encoding="utf-8")
    )
    keyword_summary = json.loads(
        (report_dir / "keyword_baseline_summary.json").read_text(encoding="utf-8")
    )
    outputs = {}
    for path in sorted(interim_dir.iterdir()):
        if path.is_file():
            identity = file_identity(root, path)
            if path.suffix == ".parquet":
                identity["rows"] = pq.ParquetFile(path).metadata.num_rows
                identity["fields"] = pq.ParquetFile(path).schema_arrow.names
            outputs[path.name] = identity
    status = {
        "phase": PHASE,
        "status": "PAUSED_HUMAN_ANNOTATION",
        "w5b_readiness": "WAITING_FOR_COMPLETED_ANNOTATION",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "annotation_version": ANNOTATION_VERSION,
        "keyword_version": KEYWORD_VERSION,
        "formal_review_rows": review_identity["rows"],
        "formal_product_rows": product_identity["rows"],
        "sample_rows": len(main_csv),
        "sample_device_quotas": sampling_flow["device_quotas"],
        "sample_unique_duplicate_keys": sampling_flow["unique_duplicate_keys"],
        "sample_unique_parent_asin": sampling_flow["unique_parent_asin"],
        "double_review_rows": len(double_csv),
        "double_review_quotas": sampling_flow["double_review_quotas"],
        "all_human_labels_empty": True,
        "rating_hidden_from_annotators": True,
        "keyword_hidden_from_annotators": True,
        "blind_key_one_to_one_validated": True,
        "double_review_ids_subset_validated": True,
        "rating_product_month_rows": rating_summary["product_month_rows"],
        "keyword_hit_rows": keyword_summary["keyword_hit_rows"],
        "keyword_hit_share": keyword_summary["keyword_hit_share"],
        "keyword_product_month_rows": keyword_summary["product_month_rows"],
        "precision_recall_f1_reported": False,
        "future_target_created": False,
        "split_created": False,
        "models_trained": [],
        "formal_inputs_unchanged": True,
        "raw_or_compressed_sources_read": False,
        "w5b_executed": False,
        "git_commit_created": False,
        "workbook_validation": validation,
        "cleaned_current_run_inspection_sidecars": cleaned_sidecars,
        "outputs": outputs,
        "final_free_gib": disk_free_gib(root),
    }
    write_json(report_dir / "w5a_status.json", status)
    with (report_dir / "w5a_execution.log").open("a", encoding="utf-8") as handle:
        if newly_cleaned_sidecars:
            handle.write(
                f"{datetime.now(timezone.utc).isoformat()} "
                "Removed current-run artifact-tool inspection sidecars after "
                "validation; no formal or requested output was deleted: "
                + ", ".join(newly_cleaned_sidecars)
                + "\n"
            )
        handle.write(
            f"{datetime.now(timezone.utc).isoformat()} "
            "Workbook validation passed; status PAUSED_HUMAN_ANNOTATION.\n"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--finalize-workbooks",
        action="store_true",
        help="Finalize status after artifact-tool workbook generation and validation.",
    )
    args = parser.parse_args()
    try:
        return finalize() if args.finalize_workbooks else prepare()
    except W5AError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - last-resort controlled exit
        print(f"[ERROR] Unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
