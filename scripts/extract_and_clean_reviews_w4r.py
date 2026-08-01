"""Phase W4R: incrementally extract reviews for the 19 W3 v1.4.0 additions.

The script deliberately reuses the frozen W4 cleaning implementation and scans
only the two approved uncompressed Reviews JSONL files. It never reads Metadata
JSONL or compressed archives and never overwrites a W3/W4 baseline artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import statistics
import sys
import time
import tomllib
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

try:
    from scripts import extract_and_clean_reviews as w4
except ImportError:
    import extract_and_clean_reviews as w4


PHASE = "W4R"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
FORBIDDEN_FIELDS = {
    "user_id",
    "images",
    "failure_binary",
    "failure_type",
    "severity",
    "persistence",
    "sentiment_score",
    "keyword_hit",
    "split",
}


class BaselineMergeFailed(RuntimeError):
    pass


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise w4.EnvironmentBlocked(f"Could not resolve project root from {__file__}")
    return root


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")


def sha256_file(path: Path) -> str:
    return w4.sha256_file(path)


def hashed_identity(root: Path, path: Path) -> dict[str, Any]:
    identity = w4.file_identity(path)
    identity["path"] = relative(root, path)
    identity["sha256"] = sha256_file(path)
    return identity


def parquet_identity(root: Path, path: Path) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    identity = hashed_identity(root, path)
    identity.update(
        {
            "rows": parquet.metadata.num_rows,
            "row_groups": parquet.metadata.num_row_groups,
            "fields": parquet.schema_arrow.names,
            "schema": str(parquet.schema_arrow),
            "compression_by_column": sorted(
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
    return identity


def check_frozen_rule_sections(
    base_config: dict[str, Any], w4r_config: dict[str, Any]
) -> None:
    for section in ("text", "language", "deduplication"):
        if base_config[section] != w4r_config[section]:
            raise w4.SourceMismatch(
                f"W4R section [{section}] differs from the frozen W4 configuration."
            )
    for key in (
        "diagnostic_product_thresholds",
        "diagnostic_product_month_thresholds",
        "minimum_products_warning",
        "maximum_top_product_share",
        "maximum_device_product_imbalance_ratio",
    ):
        if base_config["readiness"][key] != w4r_config["readiness"][key]:
            raise w4.SourceMismatch(f"W4R readiness setting changed: {key}")
    for key in (
        "minimum_free_gib",
        "progress_records",
        "progress_bytes_gib",
        "parquet_batch_rows",
        "parquet_compression",
        "duckdb_memory_limit",
    ):
        if base_config["phase"][key] != w4r_config["phase"][key]:
            raise w4.SourceMismatch(f"W4R phase setting changed from W4: {key}")


def load_product_sets(
    baseline_path: Path,
    formal_path: Path,
    *,
    expected_formal_hash: str,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    columns = [
        "parent_asin",
        "source_domains",
        "main_category",
        "title",
        "device_type",
        "filter_version",
    ]
    baseline_table = pq.read_table(baseline_path, columns=columns)
    formal_table = pq.read_table(formal_path, columns=columns)
    if baseline_table.num_rows != 106 or formal_table.num_rows != 125:
        raise w4.SourceMismatch(
            "Product-set row counts are not the expected 106 and 125."
        )

    def map_rows(table: pa.Table) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in table.to_pylist():
            parent = row["parent_asin"]
            if not isinstance(parent, str) or not parent or parent in result:
                raise w4.SourceMismatch(
                    "Product-set parent_asin values must be nonempty and unique."
                )
            result[parent] = {
                "source_domains": list(row["source_domains"] or []),
                "main_category": row["main_category"],
                "product_title": row["title"],
                "device_type": row["device_type"],
                "filter_version": row["filter_version"],
            }
        return result

    baseline = map_rows(baseline_table)
    formal = map_rows(formal_table)
    if not set(baseline).issubset(formal):
        raise w4.SourceMismatch("The formal product set does not contain all W3 products.")
    incremental_parents = set(formal) - set(baseline)
    if len(incremental_parents) != 19:
        raise w4.SourceMismatch(
            f"Expected 19 incremental products, got {len(incremental_parents)}."
        )
    incremental = {parent: formal[parent] for parent in sorted(incremental_parents)}
    counts = Counter(row["device_type"] for row in incremental.values())
    if counts != Counter({"smart_bulb": 17, "smart_switch": 2}):
        raise w4.SourceMismatch(
            f"Incremental device counts differ from 17 bulbs and 2 switches: {dict(counts)}"
        )
    if any(row["filter_version"] != "w3-v1.4.0" for row in incremental.values()):
        raise w4.SourceMismatch("Incremental products do not use filter version w3-v1.4.0.")
    formal_hash = sha256_file(formal_path)
    if formal_hash != expected_formal_hash:
        raise w4.SourceMismatch(
            f"Formal product SHA-256 mismatch: {formal_hash}"
        )
    baseline_identity = parquet_identity(project_root(), baseline_path)
    formal_identity = parquet_identity(project_root(), formal_path)
    return incremental, formal, baseline_identity, formal_identity


def compare_schema(left: pa.Schema, right: pa.Schema) -> bool:
    return left.names == right.names and all(
        left.field(index).type == right.field(index).type
        and left.field(index).nullable == right.field(index).nullable
        for index in range(len(left))
    )


def materialize_combined(
    baseline_path: Path,
    incremental_path: Path,
    combined_path: Path,
    work_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    if combined_path.exists():
        raise BaselineMergeFailed(
            f"Combined output already exists and will not be overwritten: {combined_path}"
        )
    baseline = pq.read_table(baseline_path)
    incremental = pq.read_table(incremental_path)
    if baseline.num_rows != 54870:
        raise BaselineMergeFailed(
            f"W4 baseline has {baseline.num_rows} rows, expected 54870."
        )
    if not compare_schema(baseline.schema, incremental.schema):
        raise BaselineMergeFailed("Incremental schema differs from the W4 baseline schema.")
    if baseline.schema.names != w4.FINAL_FIELDS:
        raise BaselineMergeFailed("The W4 baseline field order is unexpected.")

    baseline_keys = baseline["duplicate_key"].to_pylist()
    incremental_keys = incremental["duplicate_key"].to_pylist()
    if len(set(baseline_keys)) != len(baseline_keys):
        raise BaselineMergeFailed("The W4 baseline duplicate_key is not unique.")
    if len(set(incremental_keys)) != len(incremental_keys):
        raise BaselineMergeFailed("The incremental duplicate_key is not unique.")
    cross_collisions = set(baseline_keys) & set(incremental_keys)
    if cross_collisions:
        raise BaselineMergeFailed(
            f"Unexpected duplicate_key collisions across baseline and increment: "
            f"{len(cross_collisions)}"
        )
    baseline_parents = set(baseline["parent_asin"].to_pylist())
    incremental_parents = set(incremental["parent_asin"].to_pylist())
    if baseline_parents & incremental_parents:
        raise BaselineMergeFailed(
            "Incremental review parents overlap W4 baseline review parents."
        )

    combined = pa.concat_tables([baseline, incremental])
    temporary = work_dir / f"review_level_base_w3_v1_4_0.{os.getpid()}.part.parquet"
    pq.write_table(
        combined,
        temporary,
        compression=config["phase"]["parquet_compression"],
        use_dictionary=True,
        write_statistics=True,
        row_group_size=50000,
    )
    readback = pq.read_table(temporary)
    if not compare_schema(baseline.schema, readback.schema):
        temporary.unlink(missing_ok=True)
        raise BaselineMergeFailed("Combined readback schema differs from W4.")
    if readback.num_rows != baseline.num_rows + incremental.num_rows:
        temporary.unlink(missing_ok=True)
        raise BaselineMergeFailed("Combined readback row count is wrong.")
    if not readback.slice(0, baseline.num_rows).equals(baseline):
        temporary.unlink(missing_ok=True)
        raise BaselineMergeFailed(
            "The first 54,870 combined rows do not exactly equal the W4 baseline."
        )
    if len(set(readback["duplicate_key"].to_pylist())) != readback.num_rows:
        temporary.unlink(missing_ok=True)
        raise BaselineMergeFailed("Combined duplicate_key values are not unique.")
    os.replace(temporary, combined_path)
    return {
        "baseline_rows_preserved": baseline.num_rows,
        "incremental_rows_appended": incremental.num_rows,
        "combined_rows": readback.num_rows,
        "baseline_rows_value_equal_after_readback": True,
        "baseline_duplicate_keys_preserved": len(baseline_keys),
        "incremental_duplicate_keys_unique": True,
        "cross_duplicate_key_collisions": 0,
        "combined_duplicate_keys_unique": True,
        "schema_matches_w4": True,
    }


def csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    w4.atomic_csv(path, rows)


def clean_scan_stats(stats: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "phase",
        "status",
        "configuration_fingerprint",
        "id",
        "domain",
        "relative_path",
        "input_identity",
        "scan_started_at",
        "scan_finished_at",
        "duration_seconds",
        "start_free_bytes",
        "end_free_bytes",
        "peak_process_rss_bytes",
        "physical_line_count",
        "empty_line_count",
        "nonempty_record_count",
        "json_parse_success_count",
        "json_parse_error_count",
        "json_object_count",
        "non_object_json_count",
        "parent_asin_missing_count",
        "matched_target_count",
        "non_target_product_count",
        "title_cleaned",
        "body_cleaned",
        "html_changed",
        "unicode_changed",
        "whitespace_changed",
        "empty_text_removed_count",
        "timestamp_null_count",
        "timestamp_non_numeric_count",
        "timestamp_negative_count",
        "timestamp_unconvertible_count",
        "user_id_missing_count",
        "cleaned_candidate_count",
        "matched_by_device_type",
        "language_counts",
        "language_by_device_type",
        "timestamp_min_ms",
        "timestamp_max_ms",
        "timestamp_min_utc",
        "timestamp_max_utc",
        "parse_error_categories",
        "parse_error_details",
        "staging_path",
        "staging_rows",
        "staging_bytes",
    }
    return {key: value for key, value in stats.items() if key in allowed}


def build_coverage(
    combined_path: Path,
    incremental_path: Path,
    formal_targets: dict[str, dict[str, Any]],
    incremental_targets: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    combined = pq.read_table(combined_path)
    incremental = pq.read_table(incremental_path)
    rows = combined.to_pylist()
    incremental_rows = incremental.to_pylist()

    product_counts = Counter(row["parent_asin"] for row in rows)
    incremental_product_counts = Counter(
        row["parent_asin"] for row in incremental_rows
    )
    product_dates: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        product_dates[row["parent_asin"]].append(row["review_datetime"])

    product_rows: list[dict[str, Any]] = []
    for parent, product in sorted(
        formal_targets.items(), key=lambda item: (item[1]["device_type"], item[0])
    ):
        dates = product_dates.get(parent, [])
        product_rows.append(
            {
                "parent_asin": parent,
                "device_type": product["device_type"],
                "n_reviews": int(product_counts.get(parent, 0)),
                "has_reviews": bool(dates),
                "earliest_review_utc": min(dates).isoformat() if dates else None,
                "latest_review_utc": max(dates).isoformat() if dates else None,
            }
        )

    thresholds = [
        int(value) for value in config["readiness"]["diagnostic_product_thresholds"]
    ]
    device_rows: list[dict[str, Any]] = []
    concentration: dict[str, Any] = {}
    for device in (*DEVICE_TYPES, "ALL"):
        selected = [
            row
            for row in product_rows
            if device == "ALL" or row["device_type"] == device
        ]
        counts = [int(row["n_reviews"]) for row in selected]
        positive = [value for value in counts if value > 0]
        total = sum(counts)
        ordered = sorted(counts, reverse=True)
        dates = [
            value
            for row in selected
            for value in (row["earliest_review_utc"], row["latest_review_utc"])
            if value
        ]
        output: dict[str, Any] = {
            "device_type": device,
            "target_products": len(selected),
            "products_with_reviews": len(positive),
            "products_without_reviews": len(selected) - len(positive),
            "final_reviews": total,
            "reviews_per_target_product_min": min(counts) if counts else None,
            "reviews_per_target_product_median": (
                statistics.median(counts) if counts else None
            ),
            "reviews_per_target_product_mean": (
                statistics.mean(counts) if counts else None
            ),
            "reviews_per_target_product_max": max(counts) if counts else None,
            "earliest_review_utc": min(dates) if dates else None,
            "latest_review_utc": max(dates) if dates else None,
        }
        for threshold in thresholds:
            output[f"products_with_at_least_{threshold}_reviews"] = sum(
                value >= threshold for value in counts
            )
        device_rows.append(output)
        concentration[device] = {
            "total_reviews": total,
            "top_1_product_share": ordered[0] / total if total and ordered else None,
            "top_5_product_share": sum(ordered[:5]) / total if total else None,
            "top_10_product_share": sum(ordered[:10]) / total if total else None,
        }

    product_month_counts = Counter(
        (row["parent_asin"], row["device_type"], row["review_month"]) for row in rows
    )
    month_thresholds = [
        int(value)
        for value in config["readiness"]["diagnostic_product_month_thresholds"]
    ]
    monthly_rows: list[dict[str, Any]] = []
    for device in (*DEVICE_TYPES, "ALL"):
        values = [
            count
            for (_, row_device, _), count in product_month_counts.items()
            if device == "ALL" or row_device == device
        ]
        output = {
            "device_type": device,
            "product_month_rows": len(values),
            "monthly_reviews_mean": statistics.mean(values) if values else None,
            "monthly_reviews_median": statistics.median(values) if values else None,
            "monthly_reviews_p25": w4.quantile(values, 0.25),
            "monthly_reviews_p75": w4.quantile(values, 0.75),
            "monthly_reviews_p90": w4.quantile(values, 0.90),
            "monthly_reviews_max": max(values) if values else None,
        }
        for threshold in month_thresholds:
            output[f"product_months_at_least_{threshold}"] = sum(
                value >= threshold for value in values
            )
        monthly_rows.append(output)

    year_counts = Counter(
        (row["device_type"], row["review_datetime"].year) for row in rows
    )
    rating_counts = Counter((row["device_type"], row["rating"]) for row in rows)
    verified_counts = Counter(
        (row["device_type"], row["verified_purchase"]) for row in rows
    )
    incremental_device_counts = Counter(
        row["device_type"] for row in incremental_rows
    )
    incremental_with_reviews = set(incremental_product_counts)

    warnings: list[dict[str, Any]] = []
    minimum_products = int(config["readiness"]["minimum_products_warning"])
    for row in device_rows:
        device = row["device_type"]
        if device == "ALL":
            continue
        if row["products_with_reviews"] == 0:
            warnings.append(
                {"code": "DEVICE_TYPE_HAS_NO_USABLE_REVIEWS", "device_type": device}
            )
        if row["products_with_reviews"] < minimum_products:
            warnings.append(
                {
                    "code": "DEVICE_TYPE_FEWER_THAN_DIAGNOSTIC_PRODUCT_COUNT",
                    "device_type": device,
                    "value": row["products_with_reviews"],
                    "diagnostic_threshold": minimum_products,
                    "professor_requirement": False,
                }
            )
        top_share = concentration[device]["top_1_product_share"]
        if (
            top_share is not None
            and top_share
            > float(config["readiness"]["maximum_top_product_share"])
        ):
            warnings.append(
                {
                    "code": "REVIEWS_CONCENTRATED_IN_TOP_PRODUCT",
                    "device_type": device,
                    "value": top_share,
                }
            )
        monthly = next(item for item in monthly_rows if item["device_type"] == device)
        if monthly.get("product_months_at_least_10", 0) == 0:
            warnings.append(
                {
                    "code": "NO_PRODUCT_MONTH_AT_10_REVIEWS",
                    "device_type": device,
                    "professor_requirement": False,
                }
            )

    positive_product_counts = [
        row["products_with_reviews"]
        for row in device_rows
        if row["device_type"] != "ALL" and row["products_with_reviews"] > 0
    ]
    if positive_product_counts:
        ratio = max(positive_product_counts) / min(positive_product_counts)
        if ratio > float(
            config["readiness"]["maximum_device_product_imbalance_ratio"]
        ):
            warnings.append(
                {
                    "code": "SEVERE_DEVICE_PRODUCT_IMBALANCE",
                    "value": ratio,
                    "professor_requirement": False,
                }
            )
    positive_review_counts = [
        row["final_reviews"]
        for row in device_rows
        if row["device_type"] != "ALL" and row["final_reviews"] > 0
    ]
    if positive_review_counts:
        ratio = max(positive_review_counts) / min(positive_review_counts)
        if ratio > float(
            config["readiness"]["maximum_device_review_imbalance_ratio"]
        ):
            warnings.append(
                {
                    "code": "SEVERE_DEVICE_REVIEW_IMBALANCE",
                    "value": ratio,
                    "professor_requirement": False,
                }
            )

    return {
        "combined_table": combined,
        "incremental_table": incremental,
        "product_rows": product_rows,
        "device_rows": device_rows,
        "monthly_rows": monthly_rows,
        "concentration": concentration,
        "year_rows": [
            {"device_type": device, "year": year, "reviews": count}
            for (device, year), count in sorted(year_counts.items())
        ],
        "rating_rows": [
            {"device_type": device, "rating": rating, "reviews": count}
            for (device, rating), count in sorted(
                rating_counts.items(), key=lambda item: (item[0][0], item[0][1] or -1)
            )
        ],
        "verified_rows": [
            {
                "device_type": device,
                "verified_purchase": verified,
                "reviews": count,
            }
            for (device, verified), count in sorted(
                verified_counts.items(), key=lambda item: (item[0][0], str(item[0][1]))
            )
        ],
        "incremental": {
            "target_products": len(incremental_targets),
            "products_with_reviews": len(incremental_with_reviews),
            "products_without_reviews": len(incremental_targets)
            - len(incremental_with_reviews),
            "final_reviews": incremental.num_rows,
            "reviews_by_device_type": {
                device: int(incremental_device_counts.get(device, 0))
                for device in DEVICE_TYPES
            },
            "products_with_reviews_by_device_type": {
                device: sum(
                    parent in incremental_with_reviews
                    and product["device_type"] == device
                    for parent, product in incremental_targets.items()
                )
                for device in DEVICE_TYPES
            },
        },
        "time": {
            "earliest_review_utc": min(
                row["review_datetime"] for row in rows
            ).isoformat()
            if rows
            else None,
            "latest_review_utc": max(
                row["review_datetime"] for row in rows
            ).isoformat()
            if rows
            else None,
        },
        "warnings": warnings,
        "w5_readiness": "REVIEW_REQUIRED" if warnings else "READY",
    }


def configuration_fingerprint(
    script_path: Path,
    config_path: Path,
    raw_identities: dict[str, Any],
    product_identity: dict[str, Any],
    baseline_review_identity: dict[str, Any],
    salt: bytes,
) -> str:
    digest = hashlib.sha256()
    digest.update(script_path.read_bytes())
    digest.update(config_path.read_bytes())
    digest.update(
        json.dumps(raw_identities, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    digest.update(
        json.dumps(product_identity, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
    )
    digest.update(
        json.dumps(
            baseline_review_identity, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    )
    digest.update(hashlib.sha256(salt).digest())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase W4R incremental review extraction")
    parser.add_argument(
        "--config",
        default="config/review_cleaning_rules_w4r.toml",
        help="Project-relative W4R configuration",
    )
    args = parser.parse_args()
    root = project_root()
    script_path = Path(__file__).resolve()
    config_path = w4.resolve_inside(root, args.config)
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    project_config_path = root / "config" / "project.toml"
    project_config = tomllib.loads(project_config_path.read_text(encoding="utf-8"))
    config["_project_paths"] = dict(project_config["paths"])

    work_dir = w4.resolve_inside(root, config["outputs"]["work"])
    reports_dir = w4.resolve_inside(root, config["outputs"]["reports"])
    incremental_path = w4.resolve_inside(
        root, config["outputs"]["incremental_reviews"]
    )
    combined_path = w4.resolve_inside(root, config["outputs"]["combined_reviews"])
    salt_path = w4.resolve_inside(root, config["outputs"]["private_salt"])
    raw_root = w4.resolve_inside(
        root, project_config["paths"]["raw_uncompressed"]
    )
    baseline_products_path = w4.resolve_inside(
        root, config["inputs"]["baseline_products"]
    )
    formal_products_path = w4.resolve_inside(
        root, config["inputs"]["formal_products"]
    )
    baseline_reviews_path = w4.resolve_inside(
        root, config["inputs"]["baseline_reviews"]
    )

    started_at = w4.now_iso()
    initial_free = shutil.disk_usage(root).free
    minimum_free = int(config["phase"]["minimum_free_gib"]) * 1024**3
    if initial_free < minimum_free:
        reports_dir.mkdir(parents=True, exist_ok=True)
        w4.atomic_json(
            reports_dir / "w4r_status.json",
            {
                "phase": PHASE,
                "status": "PAUSED_SPACE_GATE",
                "reason": "Free space is below 60 GiB.",
                "updated_at": w4.now_iso(),
            },
        )
        return 2

    log_path = reports_dir / "w4r_execution.log"
    disk_events: list[dict[str, Any]] = [
        {
            "time": started_at,
            "event": "w4r_start",
            "free_bytes": initial_free,
            "free_gib": initial_free / 1024**3,
        }
    ]
    try:
        environment = w4.validate_environment(root)
        base_config_path = w4.resolve_inside(
            root, config["rule_freeze"]["base_cleaning_config"]
        )
        base_script_path = w4.resolve_inside(
            root, config["rule_freeze"]["base_w4_script"]
        )
        if sha256_file(base_config_path) != config["rule_freeze"][
            "base_cleaning_config_sha256"
        ]:
            raise w4.SourceMismatch("Frozen W4 cleaning-config hash changed.")
        if sha256_file(base_script_path) != config["rule_freeze"][
            "base_w4_script_sha256"
        ]:
            raise w4.SourceMismatch("Frozen W4 script hash changed.")
        base_config = tomllib.loads(base_config_path.read_text(encoding="utf-8"))
        check_frozen_rule_sections(base_config, config)

        statuses = {
            "w2": json.loads(
                w4.resolve_inside(root, config["inputs"]["w2_status"]).read_text(
                    encoding="utf-8"
                )
            ),
            "w4": json.loads(
                w4.resolve_inside(root, config["inputs"]["w4_status"]).read_text(
                    encoding="utf-8"
                )
            ),
            "w3r_c": json.loads(
                w4.resolve_inside(root, config["inputs"]["w3r_c_status"]).read_text(
                    encoding="utf-8"
                )
            ),
        }
        if any(document.get("status") != "PASS" for document in statuses.values()):
            raise w4.SourceMismatch("W2, W4, and W3R-C must all be PASS.")

        expected_hashes = {
            baseline_products_path: config["inputs"]["baseline_products_sha256"],
            formal_products_path: config["inputs"]["formal_products_sha256"],
            baseline_reviews_path: config["inputs"]["baseline_reviews_sha256"],
        }
        for path, expected_hash in expected_hashes.items():
            observed = sha256_file(path)
            if observed != expected_hash:
                raise w4.SourceMismatch(
                    f"Input SHA-256 mismatch for {relative(root, path)}: {observed}"
                )

        incremental_targets, formal_targets, baseline_product_identity, formal_product_identity = (
            load_product_sets(
                baseline_products_path,
                formal_products_path,
                expected_formal_hash=config["inputs"]["formal_products_sha256"],
            )
        )
        promotion_manifest_path = w4.resolve_inside(
            root, config["inputs"]["promotion_manifest"]
        )
        promotion_manifest = json.loads(
            promotion_manifest_path.read_text(encoding="utf-8")
        )
        held_excluded = {
            row["parent_asin"]
            for row in promotion_manifest["held_or_excluded_products"]
        }
        if held_excluded & set(formal_targets):
            raise w4.SourceMismatch(
                "A held or excluded recovery product is present in the formal target set."
            )

        baseline_review_before = parquet_identity(root, baseline_reviews_path)
        if baseline_review_before["rows"] != 54870:
            raise w4.SourceMismatch("W4 baseline review count is not 54,870.")
        if baseline_review_before["fields"] != w4.FINAL_FIELDS:
            raise w4.SourceMismatch("W4 baseline review fields differ from the frozen schema.")

        raw_before: dict[str, dict[str, Any]] = {}
        for source in config["inputs"]["reviews"]:
            source_path = w4.resolve_inside(raw_root, source["relative_path"])
            identity = w4.file_identity(source_path)
            if (
                identity["size_bytes"] != int(source["expected_bytes"])
                or not identity["readonly"]
            ):
                raise w4.SourceMismatch(f"Raw review identity mismatch: {source['id']}")
            raw_before[source["id"]] = identity

        salt, salt_created = w4.load_or_create_salt(salt_path)
        if salt_created:
            raise w4.EnvironmentBlocked(
                "W4R requires the existing W4 private salt; a new salt was not allowed."
            )
        fingerprint = configuration_fingerprint(
            script_path,
            config_path,
            raw_before,
            formal_product_identity,
            baseline_review_before,
            salt,
        )

        if reports_dir.exists():
            marker = reports_dir / "W4R_REPORTS.json"
            if not marker.exists() and any(reports_dir.iterdir()):
                raise w4.ReviewExtractionFailed(
                    "W4R reports directory contains unknown files."
                )
        reports_dir.mkdir(parents=True, exist_ok=True)
        w4.atomic_json(
            reports_dir / "W4R_REPORTS.json",
            {
                "phase": PHASE,
                "configuration_fingerprint": fingerprint,
                "created_or_verified_at": w4.now_iso(),
            },
        )
        if work_dir.exists():
            marker = work_dir / "W4R_WORKSPACE.json"
            if not marker.exists() and any(work_dir.iterdir()):
                raise w4.ReviewExtractionFailed(
                    "W4R work directory contains unknown files."
                )
        work_dir.mkdir(parents=True, exist_ok=True)
        w4.atomic_json(
            work_dir / "W4R_WORKSPACE.json",
            {
                "phase": PHASE,
                "configuration_fingerprint": fingerprint,
                "created_or_verified_at": w4.now_iso(),
            },
        )

        existing_status = reports_dir / "w4r_status.json"
        if existing_status.exists() and incremental_path.exists() and combined_path.exists():
            status = json.loads(existing_status.read_text(encoding="utf-8"))
            if (
                status.get("status") == "PASS"
                and status.get("configuration_fingerprint") == fingerprint
                and status.get("combined_reviews", {}).get("sha256")
                == sha256_file(combined_path)
            ):
                w4.append_log(
                    log_path,
                    "INFO",
                    "Recognized completed W4R output; repeat execution skipped.",
                )
                return 0
            raise w4.ReviewExtractionFailed(
                "Existing W4R final outputs are not a recognized completed run."
            )
        if combined_path.exists():
            raise w4.ReviewExtractionFailed(
                "Combined W4R output already exists without a completed status."
            )

        w4.PHASE = PHASE
        w4.append_log(
            log_path,
            "INFO",
            f"W4R started; python={environment['python_version']}; "
            f"pyarrow={environment['pyarrow_version']}; "
            f"orjson={environment['orjson_version']}; "
            f"lingua={environment['lingua_version']}; "
            f"incremental_products={len(incremental_targets)}; "
            f"free_bytes={initial_free}; salt_created={salt_created}",
        )
        detector = w4.build_detector(config["language"])
        scan_stats: list[dict[str, Any]] = []
        for source in config["inputs"]["reviews"]:
            scan_stats.append(
                w4.scan_source(
                    source,
                    root=root,
                    raw_uncompressed=raw_root,
                    targets=incremental_targets,
                    salt=salt,
                    detector=detector,
                    config=config,
                    fingerprint=fingerprint,
                    work_dir=work_dir,
                    reports_dir=reports_dir,
                    log_path=log_path,
                    disk_events=disk_events,
                )
            )

        if incremental_path.exists():
            raise w4.ReviewExtractionFailed(
                "Incremental output exists without a recognized completed W4R status."
            )
        duplicate_summary, language_rows = w4.materialize_final(
            scan_stats,
            root=root,
            work_dir=work_dir,
            final_path=incremental_path,
            config=config,
            log_path=log_path,
            disk_events=disk_events,
        )
        incremental_identity = parquet_identity(root, incremental_path)
        if incremental_identity["fields"] != w4.FINAL_FIELDS:
            raise w4.ReviewExtractionFailed("Incremental output fields differ from W4.")

        merge_audit = materialize_combined(
            baseline_reviews_path,
            incremental_path,
            combined_path,
            work_dir,
            config,
        )
        combined_identity = parquet_identity(root, combined_path)
        coverage = build_coverage(
            combined_path,
            incremental_path,
            formal_targets,
            incremental_targets,
            config,
        )

        scan_stats_safe = [clean_scan_stats(stats) for stats in scan_stats]
        extraction_totals = {
            name: sum(int(stats.get(name, 0)) for stats in scan_stats)
            for name in (
                "physical_line_count",
                "empty_line_count",
                "nonempty_record_count",
                "json_parse_success_count",
                "json_parse_error_count",
                "json_object_count",
                "non_object_json_count",
                "parent_asin_missing_count",
                "matched_target_count",
                "non_target_product_count",
                "cleaned_candidate_count",
                "empty_text_removed_count",
            )
        }
        language_counts = Counter()
        language_by_device: dict[str, Counter[str]] = defaultdict(Counter)
        for stats in scan_stats:
            language_counts.update(stats["language_counts"])
            for device, counts in stats["language_by_device_type"].items():
                language_by_device[device].update(counts)
        undetermined = language_counts["undetermined_short"] + language_counts[
            "undetermined_other"
        ]
        language_denominator = sum(language_counts.values())
        undetermined_share = (
            undetermined / language_denominator if language_denominator else 0.0
        )
        if undetermined_share > float(
            config["language"]["undetermined_warning_share"]
        ):
            coverage["warnings"].append(
                {
                    "code": "LANGUAGE_UNDETERMINED_SHARE_HIGH",
                    "value": undetermined_share,
                    "diagnostic_threshold": config["language"][
                        "undetermined_warning_share"
                    ],
                }
            )
            coverage["w5_readiness"] = "REVIEW_REQUIRED"

        raw_after = {
            source["id"]: w4.file_identity(
                w4.resolve_inside(raw_root, source["relative_path"])
            )
            for source in config["inputs"]["reviews"]
        }
        baseline_review_after = parquet_identity(root, baseline_reviews_path)
        baseline_product_after = parquet_identity(root, baseline_products_path)
        formal_product_after = parquet_identity(root, formal_products_path)
        if raw_after != raw_before:
            raise w4.SourceMismatch("Raw Reviews identity changed during W4R.")
        if baseline_review_after != baseline_review_before:
            raise BaselineMergeFailed("The W4 review baseline changed during W4R.")
        if baseline_product_after["sha256"] != baseline_product_identity["sha256"]:
            raise w4.SourceMismatch("The W3 baseline product file changed.")
        if formal_product_after["sha256"] != formal_product_identity["sha256"]:
            raise w4.SourceMismatch("The W3 v1.4.0 product file changed.")

        incremental_parents_with_reviews = set(
            coverage["incremental_table"]["parent_asin"].to_pylist()
        )
        if not incremental_parents_with_reviews.issubset(incremental_targets):
            raise w4.ReviewExtractionFailed(
                "Incremental output contains a non-incremental parent_asin."
            )
        if held_excluded & incremental_parents_with_reviews:
            raise w4.ReviewExtractionFailed(
                "Incremental output contains held or excluded products."
            )
        combined_parents = set(coverage["combined_table"]["parent_asin"].to_pylist())
        if not combined_parents.issubset(formal_targets):
            raise BaselineMergeFailed(
                "Combined output contains a parent outside the 125 formal products."
            )
        if FORBIDDEN_FIELDS & set(combined_identity["fields"]):
            raise BaselineMergeFailed(
                f"Combined output contains forbidden fields: "
                f"{sorted(FORBIDDEN_FIELDS & set(combined_identity['fields']))}"
            )

        extraction_csv = [
            {
                "source_id": stats["id"],
                "source_domain": stats["domain"],
                "physical_records": stats["physical_line_count"],
                "json_errors": stats["json_parse_error_count"],
                "empty_lines": stats["empty_line_count"],
                "non_object": stats["non_object_json_count"],
                "parent_asin_missing": stats["parent_asin_missing_count"],
                "matched_incremental_reviews": stats["matched_target_count"],
                "non_incremental_reviews": stats["non_target_product_count"],
                "match_rate": stats["matched_target_count"]
                / stats["physical_line_count"],
                "cleaned_candidates": stats["cleaned_candidate_count"],
                "seconds": stats["duration_seconds"],
                "peak_rss_bytes": stats["peak_process_rss_bytes"],
            }
            for stats in scan_stats
        ]
        cleaning_counts = {
            name: sum(int(stats.get(name, 0)) for stats in scan_stats)
            for name in (
                "matched_target_count",
                "title_cleaned",
                "body_cleaned",
                "html_changed",
                "unicode_changed",
                "whitespace_changed",
                "empty_text_removed_count",
                "timestamp_null_count",
                "timestamp_non_numeric_count",
                "timestamp_negative_count",
                "timestamp_unconvertible_count",
                "cleaned_candidate_count",
            )
        }
        incremental_product_report = {
            "phase": PHASE,
            "formal_product_version": "w3-v1.4.0",
            "derivation": "target_products_w3_v1_4_0 minus target_products",
            "counts": {
                "total": len(incremental_targets),
                "smart_plug": 0,
                "smart_bulb": 17,
                "smart_switch": 2,
            },
            "parent_asin_unique": True,
            "held_or_excluded_overlap": 0,
            "products": [
                {
                    "parent_asin": parent,
                    "device_type": product["device_type"],
                    "source_domains": product["source_domains"],
                    "filter_version": product["filter_version"],
                }
                for parent, product in incremental_targets.items()
            ],
        }
        extraction_report = {
            "phase": PHASE,
            "generated_at": w4.now_iso(),
            "configuration_fingerprint": fingerprint,
            "incremental_products": incremental_product_report["counts"],
            "source_scans": scan_stats_safe,
            "totals": extraction_totals,
            "incremental_parquet": incremental_identity,
        }
        cleaning_report = {
            "phase": PHASE,
            "generated_at": w4.now_iso(),
            "frozen_w4_rules": True,
            "counts": cleaning_counts,
            "language_before_filter": dict(sorted(language_counts.items())),
            "duplicate_audit": duplicate_summary,
            "incremental_final_rows": incremental_identity["rows"],
            "prohibited_fields_created": [],
        }
        language_report = {
            "phase": PHASE,
            "generated_at": w4.now_iso(),
            "package": config["language"]["package"],
            "package_version": environment["lingua_version"],
            "configuration": config["language"],
            "counts": dict(sorted(language_counts.items())),
            "counts_by_device_type": {
                device: dict(sorted(counts.items()))
                for device, counts in sorted(language_by_device.items())
            },
            "denominator": language_denominator,
            "undetermined_count": undetermined,
            "undetermined_share": undetermined_share,
            "online_service_used": False,
        }
        merge_report = {
            "phase": PHASE,
            "generated_at": w4.now_iso(),
            **merge_audit,
            "baseline_identity_before": baseline_review_before,
            "baseline_identity_after": baseline_review_after,
            "incremental_identity": incremental_identity,
            "combined_identity": combined_identity,
            "record_filter_version_policy": {
                "baseline_rows": "retain original w3-v1.3.2",
                "incremental_rows": "w3-v1.4.0",
                "mixed_versions_intentional": True,
                "dataset_level_product_version": "w3-v1.4.0",
            },
        }
        schema_report = {
            "phase": PHASE,
            "generated_at": w4.now_iso(),
            "baseline": baseline_review_before,
            "incremental": incremental_identity,
            "combined": combined_identity,
            "schema_matches_w4": True,
            "fields": combined_identity["fields"],
            "excluded_fields": sorted(FORBIDDEN_FIELDS),
        }
        w5_readiness = {
            "phase": "W5_READINESS_AFTER_W4R",
            "generated_at": w4.now_iso(),
            "status": coverage["w5_readiness"],
            "warnings": coverage["warnings"],
            "device_type_coverage": coverage["device_rows"],
            "concentration": coverage["concentration"],
            "monthly_coverage_diagnostics": coverage["monthly_rows"],
            "diagnostic_thresholds_are_professor_requirements": False,
            "w5_started": False,
            "w3_rules_modified": False,
        }

        free_final = shutil.disk_usage(root).free
        disk_events.append(
            {
                "time": w4.now_iso(),
                "event": "w4r_reporting_start",
                "free_bytes": free_final,
                "free_gib": free_final / 1024**3,
            }
        )
        input_manifest = {
            "phase": PHASE,
            "started_at": started_at,
            "generated_at": w4.now_iso(),
            "configuration_fingerprint": fingerprint,
            "environment": environment,
            "configuration": {
                "w4r_config": hashed_identity(root, config_path),
                "w4_base_config": hashed_identity(root, base_config_path),
                "w4_base_script": hashed_identity(root, base_script_path),
                "w4r_script": hashed_identity(root, script_path),
            },
            "statuses": {
                name: document["status"] for name, document in statuses.items()
            },
            "products": {
                "baseline": baseline_product_identity,
                "formal": formal_product_identity,
                "incremental_count": len(incremental_targets),
            },
            "baseline_reviews": baseline_review_before,
            "raw_reviews_before": raw_before,
            "private_salt": {
                "path": relative(root, salt_path),
                "existed_before_w4r": True,
                "reused": True,
                "value_or_hash_recorded": False,
            },
        }

        csv_rows(
            reports_dir / "incremental_review_extraction_flow.csv",
            extraction_csv,
        )
        w4.atomic_json(
            reports_dir / "incremental_review_extraction_flow.json",
            extraction_report,
        )
        csv_rows(
            reports_dir / "incremental_cleaning_flow.csv",
            [{"reason": key, "count": value} for key, value in cleaning_counts.items()],
        )
        w4.atomic_json(
            reports_dir / "incremental_cleaning_flow.json", cleaning_report
        )
        w4.atomic_json(
            reports_dir / "incremental_product_set.json",
            incremental_product_report,
        )
        w4.atomic_json(
            reports_dir / "incremental_language_audit.json", language_report
        )
        w4.atomic_json(
            reports_dir / "incremental_duplicate_audit.json", duplicate_summary
        )
        w4.atomic_json(reports_dir / "baseline_merge_audit.json", merge_report)
        w4.atomic_json(reports_dir / "combined_review_schema.json", schema_report)
        csv_rows(
            reports_dir / "combined_review_count_by_device_type.csv",
            coverage["device_rows"],
        )
        csv_rows(
            reports_dir / "combined_review_count_by_product.csv",
            coverage["product_rows"],
        )
        csv_rows(
            reports_dir / "combined_review_count_by_year.csv",
            coverage["year_rows"],
        )
        csv_rows(
            reports_dir / "combined_review_count_by_rating.csv",
            coverage["rating_rows"],
        )
        csv_rows(
            reports_dir / "combined_review_count_by_verified_purchase.csv",
            coverage["verified_rows"],
        )
        csv_rows(
            reports_dir / "combined_monthly_coverage_diagnostics.csv",
            coverage["monthly_rows"],
        )
        w4.atomic_json(
            reports_dir / "combined_timestamp_audit.json",
            {
                "phase": PHASE,
                "unit": "Unix milliseconds",
                "earliest_review_utc": coverage["time"]["earliest_review_utc"],
                "latest_review_utc": coverage["time"]["latest_review_utc"],
                "review_month_type": "date32: first UTC calendar day of month",
            },
        )
        w4.atomic_json(reports_dir / "w4r_input_manifest.json", input_manifest)
        w4.atomic_json(reports_dir / "w5_readiness.json", w5_readiness)
        disk_events.append(
            {
                "time": w4.now_iso(),
                "event": "w4r_reporting_complete",
                "free_bytes": shutil.disk_usage(root).free,
                "free_gib": shutil.disk_usage(root).free / 1024**3,
            }
        )
        w4.atomic_json(
            reports_dir / "w4r_disk_usage.json",
            {
                "phase": PHASE,
                "minimum_free_bytes": minimum_free,
                "events": disk_events,
            },
        )

        summary_lines = [
            "# Phase W4R Incremental Review Extraction Summary",
            "",
            "- Technical status: `PASS`",
            f"- W5 readiness: `{coverage['w5_readiness']}`",
            "- Formal product version: `w3-v1.4.0`",
            f"- Incremental target products: {len(incremental_targets):,}",
            f"- Incremental matched reviews before language filtering: "
            f"{extraction_totals['matched_target_count']:,}",
            f"- Incremental English reviews before deduplication: "
            f"{int(duplicate_summary['english_before_dedup']):,}",
            f"- Incremental duplicates removed: "
            f"{int(duplicate_summary['total_rows_removed']):,}",
            f"- Incremental final reviews: {incremental_identity['rows']:,}",
            f"- Combined final reviews: {combined_identity['rows']:,}",
            "",
            "| Device type | Formal products | Products with reviews | Combined reviews | "
            "Median reviews/product |",
            "|---|---:|---:|---:|---:|",
        ]
        for row in coverage["device_rows"]:
            if row["device_type"] == "ALL":
                continue
            summary_lines.append(
                f"| {row['device_type']} | {row['target_products']:,} | "
                f"{row['products_with_reviews']:,} | {row['final_reviews']:,} | "
                f"{row['reviews_per_target_product_median']:,} |"
            )
        summary_lines.extend(
            [
                "",
                "The 10/20/30 review and product-month thresholds are project diagnostics, "
                "not professor-confirmed hard requirements.",
                "",
                "The original W4 rows and their record-level filter versions were preserved "
                "exactly. No Metadata JSONL, gzip, annotation, baseline, product-month "
                "feature table, W5, or Git commit was used.",
            ]
        )
        w4.atomic_text(
            reports_dir / "w4r_summary.md", "\n".join(summary_lines) + "\n"
        )

        required_reports = [
            "w4r_execution.log",
            "w4r_input_manifest.json",
            "incremental_product_set.json",
            "incremental_review_extraction_flow.json",
            "incremental_review_extraction_flow.csv",
            "incremental_cleaning_flow.json",
            "incremental_cleaning_flow.csv",
            "incremental_language_audit.json",
            "incremental_duplicate_audit.json",
            "baseline_merge_audit.json",
            "combined_review_schema.json",
            "combined_review_count_by_device_type.csv",
            "combined_review_count_by_product.csv",
            "combined_review_count_by_year.csv",
            "combined_review_count_by_rating.csv",
            "combined_monthly_coverage_diagnostics.csv",
            "w4r_disk_usage.json",
            "w4r_summary.md",
            "w5_readiness.json",
        ]
        report_presence = {
            name: (reports_dir / name).is_file()
            and (reports_dir / name).stat().st_size > 0
            for name in required_reports
        }
        reconciled = all(
            stats["physical_line_count"]
            == stats["empty_line_count"] + stats["nonempty_record_count"]
            and stats["nonempty_record_count"]
            == stats["json_parse_success_count"] + stats["json_parse_error_count"]
            and stats["json_parse_success_count"]
            == stats["json_object_count"] + stats["non_object_json_count"]
            for stats in scan_stats
        )
        final_free = shutil.disk_usage(root).free
        criteria = {
            "project_venv_environment_valid": environment["project_venv_in_use"],
            "w4_status_pass": statuses["w4"]["status"] == "PASS",
            "w3r_c_status_pass": statuses["w3r_c"]["status"] == "PASS",
            "formal_product_identity_valid": formal_product_identity["sha256"]
            == config["inputs"]["formal_products_sha256"],
            "incremental_product_difference_is_19": len(incremental_targets) == 19,
            "two_review_sources_fully_scanned": len(scan_stats) == 2,
            "record_counts_match_w2": all(
                stats["physical_line_count"] == int(source["expected_records"])
                for stats, source in zip(scan_stats, config["inputs"]["reviews"])
            ),
            "record_count_reconciliation_passed": reconciled,
            "only_incremental_parents_extracted": incremental_parents_with_reviews.issubset(
                incremental_targets
            ),
            "w4_cleaning_rules_frozen": True,
            "incremental_parquet_valid": incremental_identity["rows"]
            == duplicate_summary["unique_duplicate_keys"],
            "combined_parquet_valid": combined_identity["rows"]
            == 54870 + incremental_identity["rows"],
            "baseline_rows_preserved_exactly": merge_audit[
                "baseline_rows_value_equal_after_readback"
            ],
            "combined_duplicate_keys_unique": merge_audit[
                "combined_duplicate_keys_unique"
            ],
            "held_excluded_absent": not bool(
                held_excluded & incremental_parents_with_reviews
            ),
            "raw_reviews_unchanged_and_readonly": raw_after == raw_before
            and all(item["readonly"] for item in raw_after.values()),
            "w4_baseline_unchanged": baseline_review_after
            == baseline_review_before,
            "final_space_at_least_60_gib": final_free >= minimum_free,
            "metadata_jsonl_not_read": True,
            "compressed_archives_not_read": True,
            "w5_not_started": True,
            "required_reports_present": all(report_presence.values()),
        }
        technical_status = "PASS" if all(criteria.values()) else "FAILED_BASELINE_MERGE"
        status = {
            "phase": PHASE,
            "status": technical_status,
            "updated_at": w4.now_iso(),
            "configuration_fingerprint": fingerprint,
            "w5_readiness": coverage["w5_readiness"],
            "environment": environment,
            "criteria": criteria,
            "report_presence": report_presence,
            "incremental_products": incremental_product_report["counts"],
            "source_scans": [
                {
                    "id": stats["id"],
                    "physical_line_count": stats["physical_line_count"],
                    "matched_target_count": stats["matched_target_count"],
                    "duration_seconds": stats["duration_seconds"],
                    "peak_process_rss_bytes": stats["peak_process_rss_bytes"],
                }
                for stats in scan_stats
            ],
            "incremental_reviews": incremental_identity,
            "combined_reviews": combined_identity,
            "baseline_reviews_before": baseline_review_before,
            "baseline_reviews_after": baseline_review_after,
            "raw_before": raw_before,
            "raw_after": raw_after,
            "coverage": {
                "incremental": coverage["incremental"],
                "combined_device_types": coverage["device_rows"],
                "time": coverage["time"],
            },
            "final_free_bytes": final_free,
            "final_free_gib": final_free / 1024**3,
            "recovery": {
                "checkpoint_reused": any(
                    bool(stats.get("checkpoint_reused", False)) for stats in scan_stats
                ),
                "data_scan_failure": False,
            },
            "policy_attestation": {
                "review_jsonl_opened": True,
                "metadata_jsonl_opened": False,
                "compressed_archive_opened": False,
                "raw_user_id_written_to_output_or_report": False,
                "online_language_service_used": False,
                "annotation_or_baseline_performed": False,
                "product_month_feature_table_created": False,
                "w5_started": False,
                "w3_or_w4_baseline_overwritten": False,
                "git_commit": False,
            },
        }
        w4.atomic_json(reports_dir / "w4r_status.json", status)
        w4.append_log(
            log_path,
            "INFO",
            f"W4R finished with status={technical_status}; "
            f"w5_readiness={coverage['w5_readiness']}; "
            f"incremental_rows={incremental_identity['rows']}; "
            f"combined_rows={combined_identity['rows']}; free_bytes={final_free}",
        )
        return 0 if technical_status == "PASS" else 1
    except w4.SpaceGate as caught:
        status_name = "PAUSED_SPACE_GATE"
        code = 2
        message = f"{type(caught).__name__}: {caught}"
    except w4.EnvironmentBlocked as caught:
        status_name = "BLOCKED_ENVIRONMENT"
        code = 3
        message = f"{type(caught).__name__}: {caught}"
    except w4.SourceMismatch as caught:
        status_name = "FAILED_SOURCE_MISMATCH"
        code = 4
        message = f"{type(caught).__name__}: {caught}"
    except BaselineMergeFailed as caught:
        status_name = "FAILED_BASELINE_MERGE"
        code = 5
        message = f"{type(caught).__name__}: {caught}"
    except BaseException as caught:
        status_name = "FAILED_INCREMENTAL_EXTRACTION"
        code = 6
        message = f"{type(caught).__name__}: {caught}"

    reports_dir.mkdir(parents=True, exist_ok=True)
    w4.append_log(
        log_path, "ERROR", f"W4R stopped with status={status_name}; error={message}"
    )
    w4.atomic_json(
        reports_dir / "w4r_status.json",
        {
            "phase": PHASE,
            "status": status_name,
            "reason": message,
            "updated_at": w4.now_iso(),
            "final_free_bytes": shutil.disk_usage(root).free,
            "w5_started": False,
        },
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
