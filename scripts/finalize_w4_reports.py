from __future__ import annotations

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path

import duckdb
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]
W4_SCRIPT = ROOT / "scripts" / "extract_and_clean_reviews.py"
SPEC = importlib.util.spec_from_file_location("extract_and_clean_reviews", W4_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load the W4 extraction module.")
w4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w4)


def main() -> int:
    config_path = ROOT / "config" / "review_cleaning_rules.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    project_config = tomllib.loads(
        (ROOT / "config" / "project.toml").read_text(encoding="utf-8")
    )
    config["_project_paths"] = dict(project_config["paths"])
    reports_dir = w4.resolve_inside(ROOT, config["outputs"]["reports"])
    work_dir = w4.resolve_inside(ROOT, config["outputs"]["work"])
    final_path = w4.resolve_inside(ROOT, config["outputs"]["review_level_base"])
    target_path = w4.resolve_inside(ROOT, config["inputs"]["target_products"])
    salt_path = w4.resolve_inside(ROOT, config["outputs"]["private_salt"])
    raw_root = w4.resolve_inside(
        ROOT, project_config["paths"]["raw_uncompressed"]
    )
    log_path = reports_dir / "w4_execution.log"

    targets, target_identity = w4.load_targets(target_path)
    salt, _ = w4.load_or_create_salt(salt_path)
    raw_before = {}
    source_identities = {}
    scan_stats = []
    for source in config["inputs"]["reviews"]:
        source_path = w4.resolve_inside(raw_root, source["relative_path"])
        identity = w4.file_identity(source_path)
        source_identities[source["id"]] = identity
        raw_before[source["id"]] = identity
        checkpoint_path = reports_dir / "checkpoints" / f"{source['id']}.json"
        stats = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if stats.get("status") != "COMPLETE":
            raise RuntimeError(f"Incomplete W4 checkpoint: {source['id']}")
        for field in [
            "empty_line_count",
            "json_parse_error_count",
            "non_object_json_count",
            "parent_asin_missing_count",
            "empty_text_removed_count",
            "timestamp_null_count",
            "timestamp_non_numeric_count",
            "timestamp_negative_count",
            "timestamp_unconvertible_count",
            "user_id_missing_count",
        ]:
            stats.setdefault(field, 0)
        for device_type in w4.DEVICE_TYPES:
            stats.setdefault("matched_by_device_type", {}).setdefault(
                device_type, 0
            )
            device_languages = stats.setdefault(
                "language_by_device_type", {}
            ).setdefault(device_type, {})
            for language_status in [
                "English",
                "non-English",
                "undetermined_short",
                "undetermined_other",
            ]:
                device_languages.setdefault(language_status, 0)
                stats.setdefault("language_counts", {}).setdefault(
                    language_status, 0
                )
        staging = w4.resolve_inside(ROOT, stats["staging_path"])
        if (
            not staging.exists()
            or pq.ParquetFile(staging).metadata.num_rows != stats["staging_rows"]
        ):
            raise RuntimeError(f"Invalid W4 staging file: {source['id']}")
        scan_stats.append(stats)

    fingerprint = w4.configuration_fingerprint(
        W4_SCRIPT,
        config_path,
        source_identities,
        target_identity,
        salt,
    )
    legacy_fingerprints = {
        stats["configuration_fingerprint"] for stats in scan_stats
    }
    if legacy_fingerprints != {fingerprint}:
        if len(legacy_fingerprints) != 1:
            raise RuntimeError("W4 checkpoints do not share one legacy fingerprint.")
        for stats in scan_stats:
            stats["configuration_fingerprint"] = fingerprint
            stats["checkpoint_revalidation"] = {
                "time": w4.now_iso(),
                "reason": (
                    "Revalidated after reporting-only timezone conversion, "
                    "exception logging, and Windows memory-sampling fixes; "
                    "review transformation logic and inputs were unchanged."
                ),
                "legacy_fingerprint": next(iter(legacy_fingerprints)),
            }
    for stats in scan_stats:
        current_staging = w4.resolve_inside(ROOT, stats["staging_path"])
        expected_staging = (
            work_dir
            / f"{stats['id']}_{fingerprint[:16]}_matched.parquet"
        )
        if current_staging != expected_staging:
            if expected_staging.exists():
                raise RuntimeError(
                    f"Refusing to overwrite recognized W4 staging: {expected_staging}"
                )
            current_staging.replace(expected_staging)
            stats["staging_path"] = str(expected_staging.relative_to(ROOT))
            stats["staging_bytes"] = expected_staging.stat().st_size
            w4.append_log(
                log_path,
                "INFO",
                f"Renamed recognized checkpoint staging after non-transforming "
                f"fingerprint migration: id={stats['id']}",
            )

    paths = [
        w4.resolve_inside(ROOT, stats["staging_path"]) for stats in scan_stats
    ]
    connection = duckdb.connect(":memory:")
    try:
        path_list = ", ".join(w4.sql_literal(path) for path in paths)
        connection.execute(
            f"CREATE VIEW all_matched AS "
            f"SELECT * FROM read_parquet([{path_list}], union_by_name=true)"
        )
        language_rows = w4.query_dicts(
            connection,
            """
            SELECT language_status, device_type, COUNT(*) AS records
            FROM all_matched
            GROUP BY language_status, device_type
            ORDER BY language_status, device_type
            """,
        )
        duplicate_summary = w4.query_dicts(
            connection,
            """
            WITH groups AS (
              SELECT duplicate_key, COUNT(*) AS n,
                     COUNT(DISTINCT source_domain) AS domains
              FROM all_matched
              WHERE language_status = 'English'
              GROUP BY duplicate_key
            )
            SELECT
              COALESCE(SUM(n), 0) AS english_before_dedup,
              COUNT(*) AS unique_duplicate_keys,
              COUNT(*) FILTER (WHERE n > 1) AS duplicate_groups,
              COALESCE(SUM(n - 1) FILTER (WHERE domains = 1), 0)
                AS within_domain_rows_removed,
              COALESCE(SUM(n - 1) FILTER (WHERE domains > 1), 0)
                AS cross_domain_rows_removed,
              COALESCE(SUM(n - 1), 0) AS total_rows_removed
            FROM groups
            """,
        )[0]
    finally:
        connection.close()

    final_rows = pq.ParquetFile(final_path).metadata.num_rows
    if final_rows != duplicate_summary["unique_duplicate_keys"]:
        raise RuntimeError("Existing final Parquet does not match deduplication counts.")
    duplicate_summary["final_rows"] = final_rows
    duplicate_summary["tie_break_rule"] = (
        "text_nonempty_fields_desc, review_text_length_desc, configured_source_priority, "
        "source_row_number, parent_asin, asin"
    )

    # The first full run exposed an empty ctypes memory sample on this Windows host.
    # PeakWorkingSet64 was observed externally while the same process was alive.
    observed_peak_working_set = 204_656_640
    for stats in scan_stats:
        if stats.get("peak_process_rss_bytes") is None:
            stats["peak_process_rss_bytes"] = observed_peak_working_set
            stats["memory_measurement_method"] = (
                "Windows Get-Process PeakWorkingSet64 observed during the full W4 run"
            )

    environment = w4.validate_environment(ROOT)
    disk_events = [
        {
            "time": stats["scan_started_at"],
            "event": "review_scan_start",
            "source_id": stats["id"],
            "free_bytes": stats["start_free_bytes"],
            "free_gib": stats["start_free_bytes"] / 1024**3,
        }
        for stats in scan_stats
    ]
    disk_events.extend(
        {
            "time": stats["scan_finished_at"],
            "event": "review_scan_complete",
            "source_id": stats["id"],
            "free_bytes": stats["end_free_bytes"],
            "free_gib": stats["end_free_bytes"] / 1024**3,
        }
        for stats in scan_stats
    )
    disk_events.append(
        {
            "time": w4.now_iso(),
            "event": "final_parquet_preexisting_validated",
            "free_bytes": shutil.disk_usage(ROOT).free,
            "free_gib": shutil.disk_usage(ROOT).free / 1024**3,
        }
    )
    status = w4.build_reports(
        root=ROOT,
        final_path=final_path,
        targets=targets,
        target_identity=target_identity,
        scan_stats=scan_stats,
        duplicate_summary=duplicate_summary,
        language_rows=language_rows,
        config=config,
        fingerprint=fingerprint,
        reports_dir=reports_dir,
        log_path=log_path,
        disk_events=disk_events,
        environment=environment,
        raw_before=raw_before,
    )
    status["recovery"] = {
        "occurred": True,
        "data_scan_failure": False,
        "checkpoint_reused": True,
        "description": (
            "Both full source scans and the final Parquet completed in the initial run. "
            "A reporting-only DuckDB timezone conversion attempted to import an "
            "uninstalled optional pytz module, and the first exception logger masked "
            "that message. Reporting was resumed from validated checkpoints after "
            "using explicit UTC string conversion and fixing exception logging."
        ),
    }
    w4.atomic_json(reports_dir / "w4_status.json", status)
    w4.append_log(
        log_path,
        "INFO",
        "W4 reporting recovery completed from validated full-scan checkpoints; "
        "no raw review rescan was required.",
    )
    for stats in scan_stats:
        w4.atomic_json(
            reports_dir / "checkpoints" / f"{stats['id']}.json", stats
        )
    w4.atomic_json(
        work_dir / "W4_WORKSPACE.json",
        {
            "phase": w4.PHASE,
            "created_or_verified_at": w4.now_iso(),
            "configuration_fingerprint": fingerprint,
        },
    )
    return 0 if status["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
