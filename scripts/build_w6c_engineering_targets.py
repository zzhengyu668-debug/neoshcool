#!/usr/bin/env python3
"""Build frozen W6-C engineering indices and leakage-safe quality targets.

This phase reads only versioned Parquet inputs.  It does not train a model,
read raw JSONL, or execute the downstream early-warning comparison.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import sys
import time
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


class W6CError(RuntimeError):
    """Base error for W6-C."""


class InputMismatch(W6CError):
    pass


class FormulaError(W6CError):
    pass


class TargetConstructionError(W6CError):
    pass


class LeakageError(W6CError):
    pass


class UnknownOutput(W6CError):
    pass


class SpaceGate(W6CError):
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


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Period):
        return str(value)
    if value is pd.NA:
        return None
    raise TypeError(f"Unsupported JSON type: {type(value)!r}")


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
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_parquet_atomic(frame: pd.DataFrame, path: Path, compression: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, temporary, compression=compression)
    os.replace(temporary, path)


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


def parquet_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    result = file_identity(root, path, include_hash=include_hash)
    parquet = pq.ParquetFile(path)
    result.update(
        {
            "rows": parquet.metadata.num_rows,
            "fields": parquet.schema_arrow.names,
            "field_count": len(parquet.schema_arrow),
            "compression": sorted(
                {
                    parquet.metadata.row_group(i).column(j).compression
                    for i in range(parquet.metadata.num_row_groups)
                    for j in range(parquet.metadata.row_group(i).num_columns)
                }
            ),
        }
    )
    return result


def validate_parquet(
    root: Path, path: Path, expected_rows: int, expected_sha256: str
) -> dict[str, Any]:
    if not path.is_file():
        raise InputMismatch(f"Missing input: {relative(root, path)}")
    identity = parquet_identity(root, path)
    if identity["rows"] != expected_rows:
        raise InputMismatch(
            f"Row mismatch for {identity['path']}: {identity['rows']} != {expected_rows}"
        )
    if identity["sha256"] != expected_sha256:
        raise InputMismatch(f"SHA-256 mismatch for {identity['path']}")
    return identity


def validate_file(root: Path, path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise InputMismatch(f"Missing input: {relative(root, path)}")
    identity = file_identity(root, path)
    if identity["sha256"] != expected_sha256:
        raise InputMismatch(f"SHA-256 mismatch for {identity['path']}")
    return identity


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def check_space(root: Path, minimum: float) -> float:
    free = disk_free_gib(root)
    if free < minimum:
        raise SpaceGate(f"Free disk {free:.2f} GiB is below {minimum:.2f} GiB")
    return free


def stable_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, default=json_default
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def period_series(values: pd.Series) -> pd.Series:
    if isinstance(values.dtype, pd.PeriodDtype):
        return values.astype("period[M]")
    timestamps = pd.to_datetime(values, errors="raise")
    if getattr(timestamps.dt, "tz", None) is not None:
        timestamps = timestamps.dt.tz_localize(None)
    return timestamps.dt.to_period("M")


def date_series(values: pd.Series) -> pd.Series:
    if isinstance(values.dtype, pd.PeriodDtype):
        return values.dt.to_timestamp().dt.date
    timestamps = pd.to_datetime(values, errors="raise")
    if getattr(timestamps.dt, "tz", None) is not None:
        timestamps = timestamps.dt.tz_localize(None)
    return timestamps.dt.to_period("M").dt.to_timestamp().dt.date


def compute_engineering_indices(
    components: pd.DataFrame, config: dict[str, Any]
) -> pd.DataFrame:
    rules = config["engineering_index"]
    required = {
        "duplicate_key",
        "parent_asin",
        "device_type",
        "source_domain",
        "review_datetime",
        "review_month",
        "analysis_role",
        "failure_probability",
        "severity_probability_ge2_given_failure",
        "severity_probability_ge3_given_failure",
        "expected_persistence_given_failure",
        "failure_model_version",
        "severity_model_version",
        "persistence_model_version",
        "sentiment_model_version",
        "product_filter_version",
    }
    missing = sorted(required - set(components.columns))
    if missing:
        raise InputMismatch(f"Review components missing columns: {missing}")
    frame = components.copy()
    f = frame["failure_probability"].astype(float).clip(0.0, 1.0)
    s2 = frame["severity_probability_ge2_given_failure"].astype(float).clip(0.0, 1.0)
    s3 = frame["severity_probability_ge3_given_failure"].astype(float).clip(0.0, 1.0)
    persistence = (
        frame["expected_persistence_given_failure"].astype(float) / 2.0
    ).clip(0.0, 1.0)
    severity_full = ((s2 + s3) / 2.0).clip(0.0, 1.0)

    frame["normalized_persistence_given_failure"] = persistence
    frame["normalized_full_severity_exploratory"] = severity_full
    frame["engineering_index_main"] = f * (
        float(rules["failure_weight"])
        + float(rules["severity_ge2_weight"]) * s2
        + float(rules["persistence_weight"]) * persistence
    )
    frame["engineering_index_failure_only"] = f
    frame["engineering_index_equal_weight"] = f * (
        float(rules["equal_failure_weight"])
        + float(rules["equal_severity_weight"]) * s2
        + float(rules["equal_persistence_weight"]) * persistence
    )
    frame["engineering_index_failure_emphasis"] = f * (
        float(rules["emphasis_failure_weight"])
        + float(rules["emphasis_severity_weight"]) * s2
        + float(rules["emphasis_persistence_weight"]) * persistence
    )
    frame["engineering_index_full_severity_exploratory"] = f * (
        float(rules["failure_weight"])
        + float(rules["severity_ge2_weight"]) * severity_full
        + float(rules["persistence_weight"]) * persistence
    )
    frame["engineering_index_version"] = str(rules["main_version"])

    index_columns = [
        "engineering_index_main",
        "engineering_index_failure_only",
        "engineering_index_equal_weight",
        "engineering_index_failure_emphasis",
        "engineering_index_full_severity_exploratory",
    ]
    if not np.isfinite(frame[index_columns].to_numpy(dtype=float)).all():
        raise FormulaError("EngineeringIndex contains non-finite values")
    minima = frame[index_columns].min()
    maxima = frame[index_columns].max()
    if (minima < -1e-12).any() or (maxima > 1.0 + 1e-12).any():
        raise FormulaError("EngineeringIndex falls outside [0, 1]")

    output_columns = [
        "duplicate_key",
        "parent_asin",
        "device_type",
        "source_domain",
        "review_datetime",
        "review_month",
        "analysis_role",
        "failure_probability",
        "severity_probability_ge2_given_failure",
        "severity_probability_ge3_given_failure",
        "expected_persistence_given_failure",
        "normalized_persistence_given_failure",
        "normalized_full_severity_exploratory",
        *index_columns,
        "failure_model_version",
        "severity_model_version",
        "persistence_model_version",
        "sentiment_model_version",
        "product_filter_version",
        "engineering_index_version",
    ]
    output = frame[output_columns].copy()
    output["review_month"] = date_series(output["review_month"])
    return output


def aggregate_rating_product_month(formal_reviews: pd.DataFrame) -> pd.DataFrame:
    required = {
        "duplicate_key",
        "parent_asin",
        "device_type",
        "review_month",
        "rating",
    }
    missing = sorted(required - set(formal_reviews.columns))
    if missing:
        raise InputMismatch(f"Formal reviews missing columns: {missing}")
    frame = formal_reviews.copy()
    frame["review_month"] = period_series(frame["review_month"])
    if frame["rating"].isna().any():
        raise TargetConstructionError("Formal reviews contain null ratings")
    frame["low_star"] = (frame["rating"].astype(float) <= 2.0).astype("int8")
    grouped = (
        frame.groupby(["parent_asin", "review_month", "device_type"], sort=True)
        .agg(
            n_reviews=("rating", "size"),
            rating_sum=("rating", "sum"),
            low_star_count=("low_star", "sum"),
        )
        .reset_index()
    )
    grouped["mean_rating"] = grouped["rating_sum"] / grouped["n_reviews"]
    grouped["low_star_share"] = grouped["low_star_count"] / grouped["n_reviews"]
    grouped["n_reviews"] = grouped["n_reviews"].astype("int64")
    grouped["low_star_count"] = grouped["low_star_count"].astype("int64")
    return grouped


def aggregate_engineering_product_month(
    review_indices: pd.DataFrame,
    w6b_product_month: pd.DataFrame,
    rating_product_month: pd.DataFrame,
) -> pd.DataFrame:
    frame = review_indices.copy()
    frame["review_month"] = period_series(frame["review_month"])
    index_columns = [
        "engineering_index_main",
        "engineering_index_failure_only",
        "engineering_index_equal_weight",
        "engineering_index_failure_emphasis",
        "engineering_index_full_severity_exploratory",
    ]
    aggregated = (
        frame.groupby(
            ["parent_asin", "review_month", "device_type", "analysis_role"], sort=True
        )
        .agg(
            n_reviews=("duplicate_key", "size"),
            **{f"mean_{column}": (column, "mean") for column in index_columns},
        )
        .reset_index()
    )
    w6b = w6b_product_month.copy()
    w6b["review_month"] = period_series(w6b["review_month"])
    rating = rating_product_month.copy()
    key = ["parent_asin", "review_month", "device_type"]
    merged = aggregated.merge(
        w6b[
            key
            + [
                "n_reviews",
                "predicted_failure_share",
                "mean_failure_probability",
                "mean_expected_severity_signal",
                "mean_expected_persistence_signal",
                "mean_sentiment_compound",
                "negative_sentiment_share",
                "failure_model_version",
                "severity_model_version",
                "persistence_model_version",
                "sentiment_model_version",
                "product_filter_version",
            ]
        ],
        on=key,
        how="inner",
        validate="one_to_one",
        suffixes=("", "_w6b"),
    ).merge(
        rating[key + ["rating_sum", "low_star_count", "mean_rating", "low_star_share"]],
        on=key,
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(w6b) or len(merged) != len(rating):
        raise TargetConstructionError("Product-month key sets do not match across inputs")
    if not (merged["n_reviews"] == merged["n_reviews_w6b"]).all():
        raise TargetConstructionError("Product-month review counts do not reconcile")
    merged = merged.drop(columns=["n_reviews_w6b"])
    merged["engineering_index_version"] = "w6c-engineering-index-main-v1.0"
    merged["review_month"] = date_series(merged["review_month"])
    return merged.sort_values(["parent_asin", "review_month"]).reset_index(drop=True)


def complete_calendar_windows(
    rating_product_month: pd.DataFrame,
    analysis_roles: dict[str, str],
    horizons: Sequence[int],
    historical_months: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    observed = rating_product_month.copy()
    observed["review_month"] = period_series(observed["review_month"])
    global_min = observed["review_month"].min()
    global_max = observed["review_month"].max()
    calendar = pd.period_range(global_min, global_max, freq="M")
    products = (
        observed[["parent_asin", "device_type"]]
        .drop_duplicates()
        .sort_values("parent_asin")
    )
    if products["parent_asin"].duplicated().any():
        raise TargetConstructionError("A product maps to multiple device types")

    grid = pd.MultiIndex.from_product(
        [products["parent_asin"].tolist(), calendar],
        names=["parent_asin", "review_month"],
    ).to_frame(index=False)
    grid = grid.merge(products, on="parent_asin", how="left", validate="many_to_one")
    grid = grid.merge(
        observed[
            [
                "parent_asin",
                "review_month",
                "n_reviews",
                "rating_sum",
                "low_star_count",
                "mean_rating",
                "low_star_share",
            ]
        ],
        on=["parent_asin", "review_month"],
        how="left",
        validate="one_to_one",
    )
    grid["origin_has_reviews"] = grid["n_reviews"].notna()
    for column in ["n_reviews", "rating_sum", "low_star_count"]:
        grid[column] = grid[column].fillna(0)
    grid["n_reviews"] = grid["n_reviews"].astype("int64")
    grid["low_star_count"] = grid["low_star_count"].astype("int64")
    grid["analysis_role"] = grid["device_type"].map(analysis_roles)
    if grid["analysis_role"].isna().any():
        raise TargetConstructionError("Unknown device type in calendar grid")

    outputs: list[pd.DataFrame] = []
    for _, group in grid.groupby("parent_asin", sort=True):
        group = group.sort_values("review_month").reset_index(drop=True)
        n = group["n_reviews"].astype(float)
        rating_sum = group["rating_sum"].astype(float)
        low_count = group["low_star_count"].astype(float)
        hist_n = n.rolling(historical_months, min_periods=historical_months).sum()
        hist_rating_sum = rating_sum.rolling(
            historical_months, min_periods=historical_months
        ).sum()
        hist_low_count = low_count.rolling(
            historical_months, min_periods=historical_months
        ).sum()
        group["historical_window_complete"] = hist_n.notna()
        group["historical_n_reviews"] = hist_n.fillna(0).astype("int64")
        group["historical_rating_mean"] = np.where(
            hist_n > 0, hist_rating_sum / hist_n, np.nan
        )
        group["historical_low_star_share"] = np.where(
            hist_n > 0, hist_low_count / hist_n, np.nan
        )
        for horizon in horizons:
            future_n = sum(n.shift(-offset) for offset in range(1, horizon + 1))
            future_rating_sum = sum(
                rating_sum.shift(-offset) for offset in range(1, horizon + 1)
            )
            future_low_count = sum(
                low_count.shift(-offset) for offset in range(1, horizon + 1)
            )
            complete = group["review_month"].map(
                lambda month: month + horizon <= global_max
            )
            future_n = future_n.where(complete)
            group[f"future_window_complete_h{horizon}"] = complete
            group[f"future_n_reviews_h{horizon}"] = future_n.fillna(0).astype("int64")
            group[f"future_rating_mean_h{horizon}"] = np.where(
                future_n > 0, future_rating_sum / future_n, np.nan
            )
            group[f"future_low_star_share_h{horizon}"] = np.where(
                future_n > 0, future_low_count / future_n, np.nan
            )
        outputs.append(group)
    full_grid = pd.concat(outputs, ignore_index=True)
    audit = {
        "global_calendar_start": str(global_min),
        "global_calendar_end": str(global_max),
        "calendar_month_count": len(calendar),
        "product_count": int(products["parent_asin"].nunique()),
        "full_grid_rows": len(full_grid),
        "observed_product_month_rows": int(full_grid["origin_has_reviews"].sum()),
        "zero_review_calendar_rows": int((full_grid["n_reviews"] == 0).sum()),
        "uses_calendar_months": True,
        "uses_next_observed_row": False,
        "historical_offsets": list(range(-(historical_months - 1), 1)),
        "future_horizons": list(horizons),
    }
    return full_grid, audit


def nullable_binary(valid: pd.Series, condition: pd.Series) -> pd.Series:
    result = pd.Series(pd.NA, index=valid.index, dtype="Int8")
    result.loc[valid] = condition.loc[valid].astype("int8")
    return result


def threshold_tag(value: float) -> str:
    return f"{int(round(value * 100)):02d}"


def construct_targets(
    full_grid: pd.DataFrame, config: dict[str, Any]
) -> pd.DataFrame:
    target_cfg = config["quality_target"]
    support = config["support"]
    horizons = [int(value) for value in target_cfg["horizons_months"]]
    frame = full_grid.loc[full_grid["origin_has_reviews"]].copy()
    if len(frame) == 0:
        raise TargetConstructionError("No observed product-month rows")
    base_valid = (
        frame["historical_window_complete"]
        & (frame["historical_n_reviews"] > 0)
        & frame["historical_rating_mean"].notna()
        & frame["historical_low_star_share"].notna()
    )
    for horizon in horizons:
        future_valid = (
            frame[f"future_window_complete_h{horizon}"]
            & (frame[f"future_n_reviews_h{horizon}"] > 0)
            & frame[f"future_rating_mean_h{horizon}"].notna()
            & frame[f"future_low_star_share_h{horizon}"].notna()
        )
        valid = base_valid & future_valid
        rating_condition = frame[f"future_rating_mean_h{horizon}"] <= (
            frame["historical_rating_mean"]
            - float(target_cfg["primary_rating_drop"])
        )
        low_condition = frame[f"future_low_star_share_h{horizon}"] >= (
            frame["historical_low_star_share"]
            + float(target_cfg["primary_low_star_increase"])
        )
        frame[f"target_definite_h{horizon}"] = valid
        frame[f"rating_deterioration_h{horizon}"] = nullable_binary(
            valid, rating_condition
        )
        frame[f"low_star_deterioration_h{horizon}"] = nullable_binary(
            valid, low_condition
        )
        frame[f"quality_deterioration_h{horizon}"] = nullable_binary(
            valid, rating_condition | low_condition
        )
        support_counts = (
            valid
            & (frame["n_reviews"] >= int(support["main_current_month_min_reviews"]))
            & (
                frame["historical_n_reviews"]
                >= int(support["historical_window_min_reviews"])
            )
            & (
                frame[f"future_n_reviews_h{horizon}"]
                >= int(support["future_window_min_reviews"])
            )
        )
        frame[f"support_main_counts_h{horizon}"] = support_counts
        frame[f"eligible_main_h{horizon}"] = support_counts & (
            frame["analysis_role"] != "case_study"
        )
        for threshold in support["robust_current_month_min_reviews"]:
            frame[f"eligible_current_ge{int(threshold)}_h{horizon}"] = (
                valid
                & (frame["n_reviews"] >= int(threshold))
                & (
                    frame["historical_n_reviews"]
                    >= int(support["historical_window_min_reviews"])
                )
                & (
                    frame[f"future_n_reviews_h{horizon}"]
                    >= int(support["future_window_min_reviews"])
                )
                & (frame["analysis_role"] != "case_study")
            )
        for rating_drop in target_cfg["rating_drop_sensitivity"]:
            rating_sensitivity = frame[f"future_rating_mean_h{horizon}"] <= (
                frame["historical_rating_mean"] - float(rating_drop)
            )
            for low_increase in target_cfg["low_star_increase_sensitivity"]:
                low_sensitivity = frame[f"future_low_star_share_h{horizon}"] >= (
                    frame["historical_low_star_share"] + float(low_increase)
                )
                name = (
                    f"quality_deterioration_h{horizon}_"
                    f"r{threshold_tag(float(rating_drop))}_"
                    f"l{threshold_tag(float(low_increase))}"
                )
                frame[name] = nullable_binary(
                    valid, rating_sensitivity | low_sensitivity
                )
    frame["target_version"] = str(target_cfg["version"])
    frame["review_month"] = date_series(frame["review_month"])
    return frame.sort_values(["parent_asin", "review_month"]).reset_index(drop=True)


def assign_proposed_split(
    targets: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    split_cfg = config["split"]
    primary = int(split_cfg["primary_horizon_months"])
    eligible_column = f"eligible_main_h{primary}"
    target_column = f"quality_deterioration_h{primary}"
    frame = targets.copy()
    frame["review_month_period"] = period_series(frame["review_month"])
    eligible = frame[eligible_column] & frame[target_column].notna()
    months = sorted(frame.loc[eligible, "review_month_period"].unique())
    if len(months) < 5:
        raise TargetConstructionError("Too few eligible calendar months for proposed split")
    train_end = max(1, math.floor(len(months) * float(split_cfg["train_fraction"])))
    validation_end = max(
        train_end + 1,
        math.floor(
            len(months)
            * (float(split_cfg["train_fraction"]) + float(split_cfg["validation_fraction"]))
        ),
    )
    validation_end = min(validation_end, len(months) - 1)
    raw_train = set(months[:train_end])
    raw_validation = set(months[train_end:validation_end])
    raw_test = set(months[validation_end:])
    embargo = int(split_cfg["embargo_calendar_months"])
    train_max = max(raw_train)
    validation_kept = {month for month in raw_validation if month > train_max + embargo}
    embargo_tv = raw_validation - validation_kept
    if not validation_kept:
        raise TargetConstructionError("Embargo removes all validation months")
    validation_max = max(validation_kept)
    test_kept = {month for month in raw_test if month > validation_max + embargo}
    embargo_vt = raw_test - test_kept
    if not test_kept:
        raise TargetConstructionError("Embargo removes all test months")

    frame["proposed_split_h3"] = "not_eligible"
    month_values = frame["review_month_period"]
    frame.loc[eligible & month_values.isin(raw_train), "proposed_split_h3"] = "train"
    frame.loc[
        eligible & month_values.isin(embargo_tv), "proposed_split_h3"
    ] = "embargo_train_validation"
    frame.loc[
        eligible & month_values.isin(validation_kept), "proposed_split_h3"
    ] = "validation"
    frame.loc[
        eligible & month_values.isin(embargo_vt), "proposed_split_h3"
    ] = "embargo_validation_test"
    frame.loc[eligible & month_values.isin(test_kept), "proposed_split_h3"] = "test"
    frame["split_version"] = str(split_cfg["version"])

    rows: list[dict[str, Any]] = []
    for split_name in [
        "train",
        "embargo_train_validation",
        "validation",
        "embargo_validation_test",
        "test",
    ]:
        subset = frame.loc[frame["proposed_split_h3"] == split_name]
        labels = subset[target_column].dropna().astype(int)
        rows.append(
            {
                "split": split_name,
                "rows": len(subset),
                "unique_months": int(subset["review_month_period"].nunique()),
                "earliest_month": str(subset["review_month_period"].min())
                if len(subset)
                else None,
                "latest_month": str(subset["review_month_period"].max())
                if len(subset)
                else None,
                "products": int(subset["parent_asin"].nunique()),
                "negative": int((labels == 0).sum()),
                "positive": int((labels == 1).sum()),
                "smart_plug": int((subset["device_type"] == "smart_plug").sum()),
                "smart_bulb": int((subset["device_type"] == "smart_bulb").sum()),
                "smart_switch": int((subset["device_type"] == "smart_switch").sum()),
            }
        )
    final_splits = frame.loc[
        frame["proposed_split_h3"].isin(["train", "validation", "test"])
    ]
    split_month_sets = {
        name: set(
            final_splits.loc[
                final_splits["proposed_split_h3"] == name, "review_month_period"
            ]
        )
        for name in ["train", "validation", "test"]
    }
    overlaps = {
        "train_validation": len(split_month_sets["train"] & split_month_sets["validation"]),
        "train_test": len(split_month_sets["train"] & split_month_sets["test"]),
        "validation_test": len(
            split_month_sets["validation"] & split_month_sets["test"]
        ),
    }
    manifest = {
        "version": str(split_cfg["version"]),
        "method": str(split_cfg["method"]),
        "primary_target": target_column,
        "eligible_calendar_months_before_embargo": len(months),
        "raw_train_months": len(raw_train),
        "raw_validation_months": len(raw_validation),
        "raw_test_months": len(raw_test),
        "embargo_calendar_months": embargo,
        "train_validation_embargo_months_with_rows": sorted(map(str, embargo_tv)),
        "validation_test_embargo_months_with_rows": sorted(map(str, embargo_vt)),
        "embargo_rows": int(
            frame["proposed_split_h3"].str.startswith("embargo").sum()
        ),
        "calendar_month_overlap_counts": overlaps,
        "random_shuffle": False,
        "future_rows_moved_for_balance": False,
        "contains_both_classes": {
            row["split"]: bool(row["negative"] > 0 and row["positive"] > 0)
            for row in rows
            if row["split"] in {"train", "validation", "test"}
        },
    }
    frame = frame.drop(columns=["review_month_period"])
    return frame, manifest, rows


def route_contract() -> dict[str, Any]:
    return {
        "core_routes": [
            {
                "route": "text_only",
                "shared_key": ["parent_asin", "review_month"],
                "text_source": "review_level_base_w3_v1_4_0.parquet review_text",
                "information_cutoff": "review_datetime within or before origin review_month",
                "added_numeric_features": [],
                "forbidden": [
                    "sentiment components",
                    "engineering components",
                    "future rating",
                    "future low-star share",
                    "target labels",
                ],
                "materialized_tfidf_in_w6c": False,
            },
            {
                "route": "text_plus_sentiment",
                "shared_key": ["parent_asin", "review_month"],
                "text_contract": "identical to text_only",
                "added_numeric_features": [
                    "feature_mean_sentiment_compound",
                    "feature_negative_sentiment_share",
                ],
                "forbidden": ["engineering components", "future fields", "target labels"],
            },
            {
                "route": "text_plus_engineering",
                "shared_key": ["parent_asin", "review_month"],
                "text_contract": "identical to text_only",
                "added_numeric_features": [
                    "feature_mean_engineering_index_main",
                    "feature_predicted_failure_share",
                    "feature_mean_failure_probability",
                ],
                "optional_ablation_features": [
                    "feature_mean_expected_severity_signal",
                    "feature_mean_expected_persistence_signal",
                ],
                "forbidden": ["future fields", "target labels"],
            },
        ],
        "optional_reference": {
            "route": "rating_only",
            "is_core": False,
            "features": [
                "feature_mean_rating",
                "feature_low_star_share",
                "feature_historical_rating_mean",
                "feature_historical_low_star_share",
            ],
        },
        "shared_sample_requirement": (
            "All routes must use the same eligible product-month keys, target, "
            "split, and downstream learner."
        ),
        "models_trained_in_w6c": False,
    }


def build_analysis_panel(
    product_month_index: pd.DataFrame, targets_with_split: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    keys = ["parent_asin", "review_month", "device_type", "analysis_role"]
    target_frame = targets_with_split.copy()
    index_frame = product_month_index.copy()
    panel = index_frame.merge(
        target_frame,
        on=keys,
        how="inner",
        validate="one_to_one",
        suffixes=("", "_target"),
    )
    if len(panel) != len(index_frame) or len(panel) != len(target_frame):
        raise TargetConstructionError("Analysis panel does not preserve product-month keys")

    feature_mapping = {
        "n_reviews": "feature_n_reviews",
        "mean_rating": "feature_mean_rating",
        "low_star_share": "feature_low_star_share",
        "predicted_failure_share": "feature_predicted_failure_share",
        "mean_failure_probability": "feature_mean_failure_probability",
        "mean_expected_severity_signal": "feature_mean_expected_severity_signal",
        "mean_expected_persistence_signal": "feature_mean_expected_persistence_signal",
        "mean_sentiment_compound": "feature_mean_sentiment_compound",
        "negative_sentiment_share": "feature_negative_sentiment_share",
        "mean_engineering_index_main": "feature_mean_engineering_index_main",
        "mean_engineering_index_failure_only": "feature_mean_engineering_index_failure_only",
        "mean_engineering_index_equal_weight": "feature_mean_engineering_index_equal_weight",
        "mean_engineering_index_failure_emphasis": "feature_mean_engineering_index_failure_emphasis",
        "mean_engineering_index_full_severity_exploratory": (
            "feature_mean_engineering_index_full_severity_exploratory"
        ),
        "historical_n_reviews": "feature_historical_n_reviews",
        "historical_rating_mean": "feature_historical_rating_mean",
        "historical_low_star_share": "feature_historical_low_star_share",
    }
    for source, destination in feature_mapping.items():
        source_candidates = [source, f"{source}_target"]
        found = next((candidate for candidate in source_candidates if candidate in panel), None)
        if found is None:
            raise TargetConstructionError(f"Missing panel feature source: {source}")
        panel[destination] = panel[found]

    target_columns: list[str] = []
    for column in target_frame.columns:
        if column.startswith("future_") or "deterioration" in column:
            source = column if column in panel else f"{column}_target"
            destination = f"target_{column}"
            panel[destination] = panel[source]
            target_columns.append(destination)

    management_columns = [
        column
        for column in target_frame.columns
        if column.startswith("eligible_")
        or column.startswith("support_")
        or column.startswith("target_definite_")
        or column
        in {
            "proposed_split_h3",
            "split_version",
            "target_version",
            "historical_window_complete",
        }
    ]
    for column in management_columns:
        source = column if column in panel else f"{column}_target"
        panel[column] = panel[source]

    feature_columns = list(feature_mapping.values())
    if any("future" in column or "deterioration" in column for column in feature_columns):
        raise LeakageError("Future or target field leaked into feature contract")
    if set(feature_columns) & set(target_columns):
        raise LeakageError("Feature and target column sets overlap")

    selected = keys + feature_columns + target_columns + management_columns
    selected = list(dict.fromkeys(selected))
    panel = panel[selected].copy()
    contract = {
        "key_columns": keys,
        "feature_columns": feature_columns,
        "target_columns": target_columns,
        "management_columns": management_columns,
    }
    return panel.sort_values(["parent_asin", "review_month"]).reset_index(drop=True), contract


def distribution_rows(review: pd.DataFrame, product_month: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mappings = {
        "engineering_index_main": "mean_engineering_index_main",
        "engineering_index_failure_only": "mean_engineering_index_failure_only",
        "engineering_index_equal_weight": "mean_engineering_index_equal_weight",
        "engineering_index_failure_emphasis": "mean_engineering_index_failure_emphasis",
        "engineering_index_full_severity_exploratory": (
            "mean_engineering_index_full_severity_exploratory"
        ),
    }
    for review_column, product_column in mappings.items():
        for level, frame, column in [
            ("review", review, review_column),
            ("product_month", product_month, product_column),
        ]:
            values = frame[column].astype(float)
            rows.append(
                {
                    "level": level,
                    "index": review_column,
                    "n": len(values),
                    "min": values.min(),
                    "p05": values.quantile(0.05),
                    "p25": values.quantile(0.25),
                    "median": values.median(),
                    "mean": values.mean(),
                    "p75": values.quantile(0.75),
                    "p95": values.quantile(0.95),
                    "max": values.max(),
                }
            )
    return rows


def device_index_rows(product_month: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device_type, subset in product_month.groupby("device_type", sort=True):
        rows.append(
            {
                "device_type": device_type,
                "analysis_role": subset["analysis_role"].iloc[0],
                "product_months": len(subset),
                "products": int(subset["parent_asin"].nunique()),
                "reviews": int(subset["n_reviews"].sum()),
                "mean_engineering_index_main": subset[
                    "mean_engineering_index_main"
                ].mean(),
                "median_engineering_index_main": subset[
                    "mean_engineering_index_main"
                ].median(),
                "mean_sentiment_compound": np.average(
                    subset["mean_sentiment_compound"], weights=subset["n_reviews"]
                ),
            }
        )
    return rows


def target_prevalence_rows(targets: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    horizons = [int(value) for value in config["quality_target"]["horizons_months"]]
    for horizon in horizons:
        for scope, mask in [
            ("all_definite", targets[f"target_definite_h{horizon}"]),
            ("eligible_main", targets[f"eligible_main_h{horizon}"]),
        ]:
            subset = targets.loc[mask]
            for target_type in ["rating", "low_star", "quality"]:
                column = (
                    f"{target_type}_deterioration_h{horizon}"
                    if target_type != "quality"
                    else f"quality_deterioration_h{horizon}"
                )
                values = subset[column].dropna().astype(int)
                rows.append(
                    {
                        "horizon": horizon,
                        "scope": scope,
                        "target": target_type,
                        "rows": len(values),
                        "negative": int((values == 0).sum()),
                        "positive": int((values == 1).sum()),
                        "positive_share": float(values.mean()) if len(values) else None,
                    }
                )
    return rows


def target_device_rows(targets: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon in config["quality_target"]["horizons_months"]:
        horizon = int(horizon)
        for device_type, subset in targets.groupby("device_type", sort=True):
            eligible = subset.loc[subset[f"eligible_main_h{horizon}"]]
            values = eligible[f"quality_deterioration_h{horizon}"].dropna().astype(int)
            rows.append(
                {
                    "horizon": horizon,
                    "device_type": device_type,
                    "analysis_role": subset["analysis_role"].iloc[0],
                    "all_product_months": len(subset),
                    "support_main_counts": int(subset[f"support_main_counts_h{horizon}"].sum()),
                    "eligible_main": len(eligible),
                    "products_eligible": int(eligible["parent_asin"].nunique()),
                    "negative": int((values == 0).sum()),
                    "positive": int((values == 1).sum()),
                    "positive_share": float(values.mean()) if len(values) else None,
                }
            )
    return rows


def support_rows(targets: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon in config["quality_target"]["horizons_months"]:
        horizon = int(horizon)
        for device_type, subset in targets.groupby("device_type", sort=True):
            base = {
                "horizon": horizon,
                "device_type": device_type,
                "analysis_role": subset["analysis_role"].iloc[0],
                "all_product_months": len(subset),
                "target_definite": int(subset[f"target_definite_h{horizon}"].sum()),
                "support_main_counts": int(subset[f"support_main_counts_h{horizon}"].sum()),
                "eligible_main_after_role": int(subset[f"eligible_main_h{horizon}"].sum()),
            }
            for threshold in config["support"]["robust_current_month_min_reviews"]:
                base[f"eligible_current_ge{int(threshold)}"] = int(
                    subset[f"eligible_current_ge{int(threshold)}_h{horizon}"].sum()
                )
            rows.append(base)
    return rows


def summary_markdown(status: dict[str, Any]) -> str:
    target_rows = status["quality_targets"]["prevalence_eligible_main"]
    primary = next(row for row in target_rows if row["horizon"] == 3)
    return f"""# Phase W6-C Summary

Technical status: **{status['status']}**
W6-D readiness: **{status['w6d_readiness']}**

## Frozen construction

- Review rows with EngineeringIndex: {status['engineering_index']['review_rows']}
- Observed product-month rows retained: {status['engineering_index']['product_month_rows']}
- Main EngineeringIndex: `F * (0.50 + 0.25 * P(Severity>=2|Failure) + 0.25 * E(Persistence|Failure)/2)`
- Main target: future rating decline >= 0.30 OR low-star-share increase >= 0.10
- Historical window: calendar months m-2, m-1, m
- Primary future horizon: 3 calendar months; h=1 and h=2 are secondary
- Main current-month support: >=5 reviews; historical/future windows: >=10 reviews each

## Primary h=3 support

- Eligible product-month rows: {primary['rows']}
- Positive deterioration targets: {primary['positive']}
- Positive share: {primary['positive_share']:.4f}
- Proposed train/validation/test rows after embargo: {status['split']['train_rows']} / {status['split']['validation_rows']} / {status['split']['test_rows']}
- Embargo rows: {status['split']['embargo_rows']}

Smart plugs remain primary, smart bulbs exploratory, and smart switches case-study only. No downstream warning model, final route comparison, online model, or raw-data scan was executed.
"""


def status_for_exception(error: Exception) -> str:
    if isinstance(error, InputMismatch):
        return "FAILED_INPUT_MISMATCH"
    if isinstance(error, FormulaError):
        return "FAILED_ENGINEERING_INDEX"
    if isinstance(error, TargetConstructionError):
        return "FAILED_TARGET_CONSTRUCTION"
    if isinstance(error, LeakageError):
        return "FAILED_LEAKAGE_AUDIT"
    if isinstance(error, SpaceGate):
        return "PAUSED_SPACE_GATE"
    if isinstance(error, UnknownOutput):
        return "FAILED_UNKNOWN_OUTPUT"
    return "FAILED_W6C"


def protected_paths(root: Path, config: dict[str, Any]) -> list[Path]:
    paths = [
        root / config["inputs"]["formal_reviews"],
        root / config["inputs"]["review_components"],
        root / config["inputs"]["product_month_components"],
        root / config["inputs"]["frozen_failure_model"],
        root / config["inputs"]["w6a_status"],
        root / config["inputs"]["w6b_status"],
    ]
    return paths


def identity_map(root: Path, paths: Iterable[Path]) -> dict[str, Any]:
    return {relative(root, path): file_identity(root, path) for path in paths}


def main() -> int:
    root = project_root()
    config_path = root / "config" / "w6c_engineering_target_rules.toml"
    config = load_toml(config_path)
    outputs = {key: root / value for key, value in config["outputs"].items()}
    report_dir = outputs.pop("report_dir")
    report_dir.mkdir(parents=True, exist_ok=True)
    status_path = report_dir / "w6c_status.json"
    start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    initial_free = disk_free_gib(root)
    status_stub: dict[str, Any] = {
        "phase": "W6-C",
        "status": "RUNNING",
        "started_at_utc": started_at,
    }
    try:
        check_space(root, float(config["runtime"]["minimum_free_gib"]))
        input_cfg = config["inputs"]
        w6a = load_json(root / input_cfg["w6a_status"])
        w6b = load_json(root / input_cfg["w6b_status"])
        if w6a.get("status") != input_cfg["w6a_required_status"]:
            raise InputMismatch("W6-A is not PASS")
        if w6b.get("status") != input_cfg["w6b_required_status"]:
            raise InputMismatch("W6-B is not PASS")

        input_identities = {
            "formal_reviews": validate_parquet(
                root,
                root / input_cfg["formal_reviews"],
                int(input_cfg["formal_reviews_rows"]),
                str(input_cfg["formal_reviews_sha256"]),
            ),
            "review_components": validate_parquet(
                root,
                root / input_cfg["review_components"],
                int(input_cfg["review_components_rows"]),
                str(input_cfg["review_components_sha256"]),
            ),
            "product_month_components": validate_parquet(
                root,
                root / input_cfg["product_month_components"],
                int(input_cfg["product_month_components_rows"]),
                str(input_cfg["product_month_components_sha256"]),
            ),
            "frozen_failure_model": validate_file(
                root,
                root / input_cfg["frozen_failure_model"],
                str(input_cfg["frozen_failure_model_sha256"]),
            ),
        }
        protected_before = identity_map(root, protected_paths(root, config))
        fingerprint_payload = {
            "config_sha256": sha256_file(config_path),
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "inputs": {
                name: identity["sha256"] for name, identity in input_identities.items()
            },
        }
        run_fingerprint = stable_fingerprint(fingerprint_payload)
        existing = [path for path in outputs.values() if path.exists()]
        if existing:
            if not status_path.is_file():
                raise UnknownOutput("W6-C outputs exist without a status report")
            old_status = load_json(status_path)
            if (
                old_status.get("status") == "PASS"
                and old_status.get("run_fingerprint") == run_fingerprint
                and len(existing) == len(outputs)
            ):
                for name, path in outputs.items():
                    expected = old_status["outputs"][name]
                    current = parquet_identity(root, path)
                    if current["sha256"] != expected["sha256"]:
                        raise UnknownOutput(f"Existing output hash changed: {relative(root, path)}")
                print("W6-C matching PASS outputs already exist; no files were overwritten.")
                return 0
            raise UnknownOutput("Unknown or mismatched W6-C outputs already exist")

        formal = pq.read_table(
            root / input_cfg["formal_reviews"],
            columns=[
                "duplicate_key",
                "parent_asin",
                "device_type",
                "review_month",
                "rating",
            ],
        ).to_pandas()
        components = pq.read_table(root / input_cfg["review_components"]).to_pandas()
        w6b_pm = pq.read_table(root / input_cfg["product_month_components"]).to_pandas()
        if not formal["duplicate_key"].is_unique or not components["duplicate_key"].is_unique:
            raise InputMismatch("Review duplicate_key is not unique")
        if set(formal["duplicate_key"]) != set(components["duplicate_key"]):
            raise InputMismatch("Formal-review and W6-B review key sets differ")

        review_indices = compute_engineering_indices(components, config)
        if len(review_indices) != int(input_cfg["review_components_rows"]):
            raise FormulaError("Review EngineeringIndex row count mismatch")
        rating_pm = aggregate_rating_product_month(formal)
        if len(rating_pm) != int(input_cfg["product_month_components_rows"]):
            raise TargetConstructionError("Rating product-month count is not 1,911")
        product_month_indices = aggregate_engineering_product_month(
            review_indices, w6b_pm, rating_pm
        )

        calendar_grid, calendar_audit = complete_calendar_windows(
            rating_pm,
            {str(key): str(value) for key, value in config["analysis_roles"].items()},
            [int(value) for value in config["quality_target"]["horizons_months"]],
            int(config["quality_target"]["historical_window_months"]),
        )
        targets = construct_targets(calendar_grid, config)
        if len(targets) != int(input_cfg["product_month_components_rows"]):
            raise TargetConstructionError("Quality-target output did not retain 1,911 rows")
        targets_with_split, split_manifest, split_rows = assign_proposed_split(
            targets, config
        )
        panel, panel_contract = build_analysis_panel(
            product_month_indices, targets_with_split
        )
        contract = route_contract()
        contract["panel_column_contract"] = panel_contract
        contract["core_routes_fixed"] = True
        contract["rating_only_first_route_question_resolved"] = True

        target_columns = panel_contract["target_columns"]
        feature_columns = panel_contract["feature_columns"]
        leakage_audit = {
            "passed": True,
            "feature_target_overlap": sorted(set(feature_columns) & set(target_columns)),
            "future_or_target_named_feature_columns": [
                column
                for column in feature_columns
                if "future" in column or "deterioration" in column or "target" in column
            ],
            "features_use_month_m_or_earlier_only": True,
            "future_values_restricted_to_target_columns": True,
            "calendar_months_used": True,
            "random_split_used": False,
            "three_month_embargo_used": True,
            "downstream_models_trained": False,
        }
        if leakage_audit["feature_target_overlap"] or leakage_audit[
            "future_or_target_named_feature_columns"
        ]:
            raise LeakageError("Panel feature/target contract failed")

        compression = str(config["runtime"]["compression"])
        write_parquet_atomic(review_indices, outputs["review_engineering_index"], compression)
        write_parquet_atomic(
            product_month_indices, outputs["product_month_engineering_index"], compression
        )
        write_parquet_atomic(
            targets_with_split, outputs["product_month_quality_targets"], compression
        )
        write_parquet_atomic(panel, outputs["product_month_analysis_panel"], compression)

        output_identities = {
            name: parquet_identity(root, path) for name, path in outputs.items()
        }
        expected_rows = {
            "review_engineering_index": 55877,
            "product_month_engineering_index": 1911,
            "product_month_quality_targets": 1911,
            "product_month_analysis_panel": 1911,
        }
        for name, rows in expected_rows.items():
            if output_identities[name]["rows"] != rows:
                raise TargetConstructionError(f"Output row mismatch: {name}")

        index_distribution = distribution_rows(review_indices, product_month_indices)
        device_indices = device_index_rows(product_month_indices)
        prevalence = target_prevalence_rows(targets_with_split, config)
        device_targets = target_device_rows(targets_with_split, config)
        supports = support_rows(targets_with_split, config)
        primary_eligible = [
            row
            for row in prevalence
            if row["scope"] == "eligible_main" and row["target"] == "quality"
        ]
        split_lookup = {row["split"]: row for row in split_rows}
        class_support = split_manifest["contains_both_classes"]
        w6d_readiness = (
            str(config["phase"]["w6d_readiness_default"])
            if all(class_support.values())
            else "REVIEW_REQUIRED"
        )

        protected_after = identity_map(root, protected_paths(root, config))
        protected_unchanged = protected_before == protected_after
        if not protected_unchanged:
            raise InputMismatch("A protected W3-W6-B input changed during W6-C")
        final_free = disk_free_gib(root)
        if final_free < float(config["runtime"]["minimum_free_gib"]):
            raise SpaceGate("Final disk space fell below the hard floor")

        status: dict[str, Any] = {
            "phase": "W6-C",
            "status": "PASS",
            "w6d_readiness": w6d_readiness,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.perf_counter() - start,
            "run_fingerprint": run_fingerprint,
            "environment": {
                "python_executable": sys.executable,
                "python_version": platform.python_version(),
                "python_bits": struct.calcsize("P") * 8,
                "pandas_version": pd.__version__,
                "pyarrow_version": pa.__version__,
            },
            "input_validation": {
                "w6a_status": w6a.get("status"),
                "w6b_status": w6b.get("status"),
                "all_rows_and_hashes_match": True,
                "identities": input_identities,
            },
            "engineering_index": {
                "review_rows": len(review_indices),
                "product_month_rows": len(product_month_indices),
                "main_formula": "F*(0.50+0.25*S_ge2+0.25*E_persistence/2)",
                "main_uses_severity_ge3": False,
                "sensitivity_versions": [
                    "failure_only",
                    "equal_weight",
                    "failure_emphasis",
                    "full_severity_exploratory",
                ],
                "distributions": index_distribution,
                "by_device_type": device_indices,
            },
            "calendar_windows": calendar_audit,
            "quality_targets": {
                "rows": len(targets_with_split),
                "primary_horizon": 3,
                "primary_rating_drop": 0.30,
                "primary_low_star_increase": 0.10,
                "combine_rule": "OR",
                "prevalence_all": prevalence,
                "prevalence_eligible_main": primary_eligible,
                "by_device_type": device_targets,
            },
            "support": {
                "main_current_month_min_reviews": 5,
                "historical_window_min_reviews": 10,
                "future_window_min_reviews": 10,
                "robust_current_month_thresholds": [10, 20],
                "counts": supports,
                "ineligible_rows_retained": True,
                "smart_switch_formal_modeling": False,
            },
            "split": {
                **split_manifest,
                "counts": split_rows,
                "train_rows": split_lookup["train"]["rows"],
                "validation_rows": split_lookup["validation"]["rows"],
                "test_rows": split_lookup["test"]["rows"],
            },
            "route_contract": contract,
            "leakage_audit": leakage_audit,
            "protected_inputs_unchanged": protected_unchanged,
            "downstream_warning_model_trained": False,
            "final_route_comparison_executed": False,
            "failure_model_modified": False,
            "severity_model_modified": False,
            "persistence_model_modified": False,
            "human_labels_modified": False,
            "raw_jsonl_read": False,
            "metadata_jsonl_read": False,
            "compressed_sources_read": False,
            "online_api_or_llm_used": False,
            "bert_transformer_lstm_pytorch_embedding_trained": False,
            "next_phase_executed": False,
            "git_commit_created": False,
            "disk": {
                "minimum_free_gib": float(config["runtime"]["minimum_free_gib"]),
                "initial_free_gib": initial_free,
                "final_free_gib": final_free,
            },
            "outputs": output_identities,
            "w6d_remaining_decisions": {
                "core_routes": [
                    "text_only",
                    "text_plus_sentiment",
                    "text_plus_engineering",
                ],
                "core_routes_are_frozen": True,
                "rating_only_is_optional_reference": True,
                "remaining": [
                    "common downstream machine-learning methods",
                    "whether to include optional Rating-only reference",
                    "whether BERT is supplemental only",
                ],
            },
        }

        engineering_definition = {
            "version": config["engineering_index"]["main_version"],
            "main": status["engineering_index"]["main_formula"],
            "main_weights": {"failure": 0.50, "severity_ge2": 0.25, "persistence": 0.25},
            "severity_ge3_in_main": False,
            "sensitivity": {
                "failure_only": "F",
                "equal_weight": "F*(1/3+1/3*S_ge2+1/3*P_norm)",
                "failure_emphasis": "F*(0.60+0.20*S_ge2+0.20*P_norm)",
                "full_severity_exploratory": (
                    "F*(0.50+0.25*((P_ge2+P_ge3)/2)+0.25*P_norm)"
                ),
            },
            "not_human_truth": True,
            "not_real_failure_rate": True,
        }
        quality_definition = {
            "version": config["quality_target"]["version"],
            "historical_calendar_offsets": [-2, -1, 0],
            "future_horizons": [1, 2, 3],
            "primary_horizon": 3,
            "rating_drop": 0.30,
            "low_star_increase": 0.10,
            "low_star_definition": "rating <= 2",
            "combine_rule": "OR",
            "weighted_by_review_counts": True,
            "operational_consumer_rating_outcome": True,
            "not_maintenance_confirmed_hardware_truth": True,
        }
        rating_summary = {
            "formal_review_rows": len(formal),
            "observed_product_month_rows": len(rating_pm),
            "products": int(rating_pm["parent_asin"].nunique()),
            "rating_min": float(formal["rating"].min()),
            "rating_max": float(formal["rating"].max()),
            "mean_rating": float(formal["rating"].mean()),
            "low_star_count": int((formal["rating"] <= 2).sum()),
            "low_star_share": float((formal["rating"] <= 2).mean()),
            "all_formal_reviews_used": True,
        }
        support_audit = {
            "rules": status["support"],
            "all_1911_rows_retained": len(targets_with_split) == 1911,
            "eligibility_is_a_flag_not_a_deletion": True,
            "same_eligibility_required_for_all_routes": True,
        }
        write_json(report_dir / "engineering_index_definition.json", engineering_definition)
        write_csv(report_dir / "engineering_index_distribution.csv", index_distribution)
        write_csv(report_dir / "engineering_index_by_device_type.csv", device_indices)
        write_json(report_dir / "rating_product_month_summary.json", rating_summary)
        write_json(report_dir / "quality_target_definition.json", quality_definition)
        write_csv(report_dir / "quality_target_prevalence.csv", prevalence)
        write_csv(report_dir / "quality_target_by_device_type.csv", device_targets)
        write_json(report_dir / "support_rule_audit.json", support_audit)
        write_csv(report_dir / "support_rule_counts.csv", supports)
        write_json(report_dir / "calendar_window_audit.json", calendar_audit)
        write_json(report_dir / "leakage_audit.json", leakage_audit)
        write_json(report_dir / "route_feature_contract.json", contract)
        write_json(report_dir / "proposed_time_split_manifest.json", split_manifest)
        write_csv(report_dir / "proposed_time_split_counts.csv", split_rows)
        write_json(
            report_dir / "w6c_disk_usage.json",
            status["disk"],
        )
        write_json(
            report_dir / "w6c_input_manifest.json",
            {
                "phase": "W6-C",
                "run_fingerprint": run_fingerprint,
                "config": file_identity(root, config_path),
                "script": file_identity(root, Path(__file__).resolve()),
                "inputs": input_identities,
                "protected_before": protected_before,
                "protected_after": protected_after,
            },
        )
        write_text(report_dir / "w6c_summary.md", summary_markdown(status))
        write_text(
            report_dir / "w6c_execution.log",
            (
                f"{started_at} W6-C started\n"
                f"{datetime.now(timezone.utc).isoformat()} PASS; no model training, "
                "raw reads, or W6-D execution\n"
            ),
        )
        write_json(status_path, status)
        return 0
    except Exception as error:
        status_stub.update(
            {
                "status": status_for_exception(error),
                "failed_at_utc": datetime.now(timezone.utc).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "downstream_warning_model_trained": False,
                "next_phase_executed": False,
            }
        )
        write_json(status_path, status_stub)
        print(f"{status_stub['status']}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
