"""Promote the W3R-B human-approved product subset into a versioned W3 v1.4.0 catalog.

This script reads only small project configuration/report files and the two
approved product-level Parquet inputs. It never reads raw Metadata, Reviews,
compressed archives, or the W4 review-level Parquet.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


FORMAL_VERSION = "w3-v1.4.0"
BASE_VERSION = "w3-v1.3.2"
DRAFT_VERSION = "w3-v1.4.0-draft"
PHASE = "W3R-C"


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise RuntimeError(f"PROJECT_HANDOFF.md not found at resolved root: {root}")
    return root


def rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(root: Path, path: Path, *, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    identity: dict[str, Any] = {
        "path": rel(root, path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if include_hash:
        identity["sha256"] = sha256_file(path)
    return identity


def atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def count_by_device(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["device_type"] for row in rows)
    return {key: int(counts.get(key, 0)) for key in ("smart_plug", "smart_bulb", "smart_switch")}


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def string_or_empty(value: Any) -> str:
    return "" if value is None else str(value)


def recovered_content_fingerprint(row: dict[str, Any]) -> str:
    identity = {
        field: row.get(field)
        for field in (
            "parent_asin",
            "main_category",
            "title",
            "categories",
            "features",
            "description",
            "store",
            "details",
            "source_domains",
        )
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def recovered_record(
    row: dict[str, Any],
    decision: dict[str, Any],
    baseline_schema: pa.Schema,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    device_type = decision["final_device_type"]
    source_domains = normalize_list(row.get("source_domains"))
    if not source_domains:
        fail(f"Recovered product has no source_domains: {row.get('parent_asin')}")

    values: dict[str, Any] = {
        "parent_asin": row["parent_asin"],
        "source_domains": source_domains,
        "primary_source_domain": source_domains[0],
        "primary_source_row_number": int(defaults["primary_source_row_number"]),
        "main_category": row.get("main_category"),
        "title": row.get("title"),
        "categories": string_or_empty(row.get("categories")),
        "features": string_or_empty(row.get("features")),
        "description": string_or_empty(row.get("description")),
        "store": row.get("store"),
        "details": string_or_empty(row.get("details")),
        "price": str(defaults["price"]),
        "average_rating": None,
        "rating_number": None,
        "candidate_device_types": [device_type],
        "eligible_device_types": [device_type],
        "candidate_device_terms": list(defaults["candidate_device_terms"]),
        "candidate_smart_terms": list(defaults["candidate_smart_terms"]),
        "matched_fields": list(defaults["matched_fields"]),
        "exclusion_reasons": normalize_list(row.get("recovery_original_exclusion_reason")),
        "candidate_confidence": string_or_empty(row.get("recovery_confidence")) or "human_reviewed",
        "provisional_device_type": device_type,
        "device_type": device_type,
        "eligible_after_exclusions": True,
        "ambiguity_status": str(defaults["ambiguity_status"]),
        "candidate_reason": str(defaults["candidate_reason"]),
        "title_only": bool(defaults["title_only"]),
        "filter_version": FORMAL_VERSION,
        "candidate_source_record_count": int(defaults["candidate_source_record_count"]),
        "duplicate_resolution_rule": str(defaults["duplicate_resolution_rule"]),
        "coalesced_fields": list(defaults["coalesced_fields"]),
        "content_fingerprint": recovered_content_fingerprint(row),
        "core_nonempty_count": sum(
            bool(string_or_empty(row.get(field)).strip())
            for field in ("title", "categories", "features", "description")
        ),
        "identity_text_chars": sum(
            len(string_or_empty(row.get(field)))
            for field in ("title", "categories", "features", "description")
        ),
    }
    missing = [field.name for field in baseline_schema if field.name not in values]
    if missing:
        fail(f"Recovered record is missing baseline fields: {missing}")
    return values


def main() -> int:
    started = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    root = project_root()
    config_path = root / "config" / "product_filter_rules_w3_v1_4_0.toml"
    project_config_path = root / "config" / "project.toml"
    with config_path.open("rb") as handle:
        release_config = tomllib.load(handle)
    with project_config_path.open("rb") as handle:
        project_config = tomllib.load(handle)

    processed_dir = root / project_config["paths"]["processed"]
    reports_root = root / project_config["paths"]["reports"]
    report_dir = reports_root / "w3r_c"
    output_parquet = processed_dir / "target_products_w3_v1_4_0.parquet"
    promotion_manifest_path = root / "config" / "product_promotion_manifest_w3_v1_4_0.json"
    script_path = Path(__file__).resolve()

    output_paths = {
        "parquet": output_parquet,
        "promotion_manifest": promotion_manifest_path,
        "execution_log": report_dir / "w3r_c_execution.log",
        "input_manifest": report_dir / "w3r_c_input_manifest.json",
        "promoted_csv": report_dir / "promoted_products.csv",
        "held_excluded_csv": report_dir / "held_and_excluded_products.csv",
        "counts_csv": report_dir / "old_vs_new_product_counts.csv",
        "difference_json": report_dir / "product_set_difference.json",
        "schema_json": report_dir / "product_schema.json",
        "summary_md": report_dir / "w3r_c_summary.md",
        "status_json": report_dir / "w3r_c_status.json",
    }
    review_level_path = processed_dir / "review_level_base.parquet"
    review_level_before = (
        file_identity(root, review_level_path, include_hash=False)
        if review_level_path.exists()
        else {"path": rel(root, review_level_path), "exists": False}
    )
    existing = [rel(root, path) for path in output_paths.values() if path.exists()]
    if existing:
        fail(f"Refusing to overwrite existing W3R-C outputs: {existing}")

    free_before = shutil.disk_usage(root).free
    minimum_free = int(float(release_config["release"]["minimum_free_gib"]) * 1024**3)
    if free_before < minimum_free:
        fail("PAUSED_SPACE_GATE: free space is below the configured 60 GiB minimum")

    expected_venv = (root / ".venv" / "Scripts" / "python.exe").resolve()
    actual_python = Path(sys.executable).resolve()
    if actual_python != expected_venv:
        fail(f"Project .venv is not in use: expected {expected_venv}, got {actual_python}")

    input_paths = {
        "base_rules": root / release_config["frozen_rule_sources"]["base_rules"],
        "recovery_rules": root / release_config["frozen_rule_sources"]["recovery_rules"],
        "w3_status": root / "data/amazon_reviews_2023/reports/w3/w3_status.json",
        "w3r_a_status": root / "data/amazon_reviews_2023/reports/w3r_a/w3r_a_status.json",
        "w3r_b_status": root / "data/amazon_reviews_2023/reports/w3r_b/w3r_b_final_status.json",
        "adjudication_validation": root
        / "data/amazon_reviews_2023/reports/w3r_b/w3r_b_adjudication_validation.json",
        "decisions": root / release_config["inputs"]["final_human_decisions"],
        "baseline_products": root / release_config["inputs"]["baseline_products"],
        "draft_products": root / release_config["inputs"]["draft_products"],
    }
    for name, path in input_paths.items():
        if not path.is_file():
            fail(f"Required input is missing ({name}): {path}")

    expected_hashes = {
        "base_rules": release_config["frozen_rule_sources"]["base_rules_sha256"],
        "recovery_rules": release_config["frozen_rule_sources"]["recovery_rules_sha256"],
        "decisions": release_config["inputs"]["final_human_decisions_sha256"],
        "baseline_products": release_config["inputs"]["baseline_products_sha256"],
        "draft_products": release_config["inputs"]["draft_products_sha256"],
    }
    observed_hashes = {name: sha256_file(path) for name, path in input_paths.items()}
    for name, expected_hash in expected_hashes.items():
        if observed_hashes[name].lower() != str(expected_hash).lower():
            fail(
                f"Input hash mismatch for {name}: "
                f"expected {expected_hash}, got {observed_hashes[name]}"
            )

    w3_status = load_json(input_paths["w3_status"])
    w3r_a_status = load_json(input_paths["w3r_a_status"])
    w3r_b_status = load_json(input_paths["w3r_b_status"])
    adjudication_validation = load_json(input_paths["adjudication_validation"])
    decisions_document = load_json(input_paths["decisions"])
    if w3_status.get("status") != "PASS" or w3_status.get("filter_version") != BASE_VERSION:
        fail("W3 baseline status/version is not the expected PASS w3-v1.3.2")
    if w3r_a_status.get("status") != "PAUSED_INSUFFICIENT_RECOVERY":
        fail("Unexpected W3R-A status")
    if w3r_b_status.get("status") != "PAUSED_PROMOTION_APPROVAL":
        fail("W3R-B is not awaiting promotion approval")
    if adjudication_validation.get("status") != "PASS":
        fail("W3R-B adjudication validation did not pass")

    decisions = decisions_document.get("decisions", [])
    if len(decisions) != 23:
        fail(f"Expected 23 human decisions, got {len(decisions)}")
    decision_by_parent = {row["parent_asin"]: row for row in decisions}
    if len(decision_by_parent) != 23 or any(not key for key in decision_by_parent):
        fail("Human decisions contain duplicate or missing parent_asin")
    include_decisions = [
        row
        for row in decisions
        if row.get("final_decision") == "include"
        and row.get("final_label") == "correct_target"
        and row.get("final_device_type") in {"smart_bulb", "smart_switch"}
    ]
    held_excluded = [row for row in decisions if row not in include_decisions]
    include_counts = Counter(row["final_device_type"] for row in include_decisions)
    disposition_counts = Counter(row["final_decision"] for row in decisions)
    if len(include_decisions) != 19:
        fail(f"Expected 19 approved additions, got {len(include_decisions)}")
    if include_counts != Counter({"smart_bulb": 17, "smart_switch": 2}):
        fail(f"Approved device counts do not match: {dict(include_counts)}")
    if disposition_counts != Counter({"include": 19, "exclude": 3, "hold": 1}):
        fail(f"Decision disposition counts do not match: {dict(disposition_counts)}")

    baseline_table = pq.read_table(input_paths["baseline_products"])
    draft_table = pq.read_table(input_paths["draft_products"])
    baseline_rows = baseline_table.to_pylist()
    draft_rows = draft_table.to_pylist()
    baseline_by_parent = {row["parent_asin"]: row for row in baseline_rows}
    draft_by_parent = {row["parent_asin"]: row for row in draft_rows}
    if len(baseline_rows) != 106 or len(baseline_by_parent) != 106:
        fail("Baseline product count/uniqueness mismatch")
    if len(draft_rows) != 129 or len(draft_by_parent) != 129:
        fail("Draft product count/uniqueness mismatch")
    if count_by_device(baseline_rows) != {
        "smart_plug": 95,
        "smart_bulb": 8,
        "smart_switch": 3,
    }:
        fail("Baseline device counts mismatch")

    baseline_parents = set(baseline_by_parent)
    draft_parents = set(draft_by_parent)
    new_draft_parents = draft_parents - baseline_parents
    if len(baseline_parents & draft_parents) != 106:
        fail("Draft does not contain every baseline parent")
    if new_draft_parents != set(decision_by_parent):
        fail("The 23 W3R-B decisions do not exactly match the 23 draft additions")

    common_identity_fields = [
        "main_category",
        "title",
        "categories",
        "features",
        "description",
        "store",
        "details",
        "source_domains",
        "device_type",
    ]
    shared_conflicts: list[dict[str, Any]] = []
    for parent in sorted(baseline_parents):
        differences = [
            field
            for field in common_identity_fields
            if baseline_by_parent[parent].get(field) != draft_by_parent[parent].get(field)
        ]
        if differences:
            shared_conflicts.append({"parent_asin": parent, "fields": differences})
    if shared_conflicts:
        fail(f"Metadata conflicts found in shared baseline records: {shared_conflicts[:5]}")

    included_parents = {row["parent_asin"] for row in include_decisions}
    held_excluded_parents = {row["parent_asin"] for row in held_excluded}
    if included_parents & baseline_parents:
        fail("An approved recovery product already exists in the baseline")
    if included_parents & held_excluded_parents:
        fail("A parent_asin appears in both approved and held/excluded decisions")

    defaults = release_config["recovered_schema_defaults"]
    baseline_schema = baseline_table.schema
    base_output_rows: list[dict[str, Any]] = []
    baseline_field_changes = 0
    for original in baseline_rows:
        updated = dict(original)
        previous = str(original["filter_version"])
        updated["filter_version"] = FORMAL_VERSION
        updated["promotion_source"] = release_config["management_fields"][
            "baseline_promotion_source"
        ]
        updated["promotion_basis"] = release_config["management_fields"][
            "baseline_promotion_basis"
        ]
        updated["human_review_status"] = release_config["management_fields"][
            "baseline_human_review_status"
        ]
        updated["previous_filter_version"] = previous
        for field in baseline_schema.names:
            if field != "filter_version" and updated[field] != original[field]:
                baseline_field_changes += 1
        base_output_rows.append(updated)

    promoted_rows: list[dict[str, Any]] = []
    for decision in sorted(include_decisions, key=lambda row: row["parent_asin"]):
        parent = decision["parent_asin"]
        draft_row = draft_by_parent.get(parent)
        if draft_row is None:
            fail(f"Approved parent not found in draft Parquet: {parent}")
        if draft_row["device_type"] != decision["final_device_type"]:
            fail(f"Draft/human device-type conflict for {parent}")
        record = recovered_record(draft_row, decision, baseline_schema, defaults)
        record["promotion_source"] = release_config["management_fields"][
            "recovered_promotion_source"
        ]
        record["promotion_basis"] = release_config["management_fields"][
            "recovered_promotion_basis"
        ]
        record["human_review_status"] = release_config["management_fields"][
            "recovered_human_review_status"
        ]
        record["previous_filter_version"] = DRAFT_VERSION
        promoted_rows.append(record)

    management_fields = [
        pa.field("promotion_source", pa.string(), nullable=False),
        pa.field("promotion_basis", pa.string(), nullable=False),
        pa.field("human_review_status", pa.string(), nullable=False),
        pa.field("previous_filter_version", pa.string(), nullable=False),
    ]
    # filter_version already exists in the W3 schema; four genuinely new management
    # fields are appended while filter_version is updated to the formal release.
    output_schema = pa.schema(list(baseline_schema) + management_fields)
    final_rows = base_output_rows + promoted_rows
    final_table = pa.Table.from_pylist(final_rows, schema=output_schema)

    final_parents = final_table["parent_asin"].to_pylist()
    final_devices = final_table["device_type"].to_pylist()
    if final_table.num_rows != 125 or len(set(final_parents)) != 125:
        fail("FAILED_PROMOTION_MISMATCH: formal product rows or uniqueness are wrong")
    final_counts = Counter(final_devices)
    if final_counts != Counter({"smart_plug": 95, "smart_bulb": 25, "smart_switch": 5}):
        fail(f"FAILED_PROMOTION_MISMATCH: formal device counts are {dict(final_counts)}")
    if any(value != FORMAL_VERSION for value in final_table["filter_version"].to_pylist()):
        fail("Formal output contains an unexpected filter_version")
    if held_excluded_parents & set(final_parents):
        fail("A held or excluded product entered the formal output")
    if baseline_field_changes != 0:
        fail("Unexpected changes were made to retained baseline fields")

    report_dir.mkdir(parents=True, exist_ok=False)
    processed_dir.mkdir(parents=True, exist_ok=True)
    temp_parquet = output_parquet.with_name(
        f".{output_parquet.name}.{os.getpid()}.tmp"
    )
    pq.write_table(
        final_table,
        temp_parquet,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
    )
    check_table = pq.read_table(temp_parquet)
    if not check_table.schema.equals(output_schema, check_metadata=True):
        fail("Written Parquet schema does not match the explicit output schema")
    if check_table.num_rows != 125:
        fail("Written Parquet row count does not equal 125")
    os.replace(temp_parquet, output_parquet)

    output_identity = file_identity(root, output_parquet)
    baseline_identity_after = file_identity(root, input_paths["baseline_products"])
    if baseline_identity_after["sha256"] != observed_hashes["baseline_products"]:
        fail("Baseline target_products.parquet changed during W3R-C")

    # Stat only: the W4 review-level file is not opened or hashed in W3R-C.
    review_level_after = (
        file_identity(root, review_level_path, include_hash=False)
        if review_level_path.exists()
        else {"path": rel(root, review_level_path), "exists": False}
    )
    if review_level_after != review_level_before:
        fail("review_level_base.parquet filesystem identity changed during W3R-C")

    promoted_report_rows = [
        {
            "blind_id": decision["blind_id"],
            "parent_asin": decision["parent_asin"],
            "device_type": decision["final_device_type"],
            "product_title": decision["product_title"],
            "source_domains": decision["source_domains"],
            "final_label": decision["final_label"],
            "final_decision": decision["final_decision"],
            "decision_basis": decision["decision_basis"],
        }
        for decision in sorted(include_decisions, key=lambda row: row["blind_id"])
    ]
    held_excluded_report_rows = [
        {
            "blind_id": decision["blind_id"],
            "parent_asin": decision["parent_asin"],
            "product_title": decision["product_title"],
            "final_device_type": decision["final_device_type"],
            "final_label": decision["final_label"],
            "final_decision": decision["final_decision"],
            "decision_basis": decision["decision_basis"],
        }
        for decision in sorted(held_excluded, key=lambda row: row["blind_id"])
    ]
    counts_rows = [
        {
            "device_type": device,
            "w3_v1_3_2": count_by_device(baseline_rows)[device],
            "promoted_additions": int(include_counts.get(device, 0)),
            "w3_v1_4_0": int(final_counts.get(device, 0)),
        }
        for device in ("smart_plug", "smart_bulb", "smart_switch")
    ]
    counts_rows.append(
        {
            "device_type": "total",
            "w3_v1_3_2": 106,
            "promoted_additions": 19,
            "w3_v1_4_0": 125,
        }
    )

    product_difference = {
        "phase": PHASE,
        "formal_version": FORMAL_VERSION,
        "baseline_version": BASE_VERSION,
        "baseline_rows": 106,
        "formal_rows": 125,
        "unchanged_baseline_parents": 106,
        "duplicate_parent_asin_between_baseline_and_promoted": 0,
        "shared_baseline_metadata_conflicts": 0,
        "baseline_non_version_fields_changed": baseline_field_changes,
        "added": promoted_report_rows,
        "held_or_excluded": held_excluded_report_rows,
        "final_device_counts": {
            key: int(final_counts[key])
            for key in ("smart_plug", "smart_bulb", "smart_switch")
        },
    }
    schema_report = {
        "formal_version": FORMAL_VERSION,
        "parquet": output_identity,
        "compression": "zstd",
        "rows": final_table.num_rows,
        "fields": [
            {
                "name": field.name,
                "type": str(field.type),
                "nullable": field.nullable,
                "origin": (
                    "new_management"
                    if field.name
                    in {
                        "promotion_source",
                        "promotion_basis",
                        "human_review_status",
                        "previous_filter_version",
                    }
                    else "w3_baseline_schema"
                ),
            }
            for field in output_schema
        ],
        "baseline_schema_field_count": len(baseline_schema),
        "formal_schema_field_count": len(output_schema),
        "schema_compatibility": {
            "all_34_baseline_fields_retained": output_schema.names[: len(baseline_schema)]
            == baseline_schema.names,
            "all_baseline_field_types_retained": all(
                output_schema.field(index).type == baseline_schema.field(index).type
                for index in range(len(baseline_schema))
            ),
            "filter_version_updated_not_added": True,
            "new_management_fields": [
                "promotion_source",
                "promotion_basis",
                "human_review_status",
                "previous_filter_version",
            ],
        },
        "recovered_product_defaults": defaults,
    }

    completed = datetime.now(timezone.utc)
    free_after = shutil.disk_usage(root).free
    input_manifest = {
        "phase": PHASE,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "project_root_resolved": str(root),
        "environment": {
            "python_executable": str(actual_python),
            "python_version": platform.python_version(),
            "python_64_bit": sys.maxsize > 2**32,
            "pyarrow_version": pa.__version__,
        },
        "configuration": {
            "project_config": file_identity(root, project_config_path),
            "formal_release_config": file_identity(root, config_path),
            "script": file_identity(root, script_path),
        },
        "inputs": {
            name: file_identity(root, path)
            for name, path in input_paths.items()
        },
        "input_statuses": {
            "w3": w3_status["status"],
            "w3r_a": w3r_a_status["status"],
            "w3r_b": w3r_b_status["status"],
            "adjudication_validation": adjudication_validation["status"],
        },
        "disk": {
            "free_bytes_before": free_before,
            "free_gib_before": free_before / 1024**3,
            "free_bytes_after": free_after,
            "free_gib_after": free_after / 1024**3,
            "minimum_free_gib": release_config["release"]["minimum_free_gib"],
        },
        "review_level_base_identity_before_stat_only": review_level_before,
        "review_level_base_identity_after_stat_only": review_level_after,
        "review_level_base_opened": False,
    }

    promotion_manifest = {
        "formal_version": FORMAL_VERSION,
        "status": "frozen",
        "created_at": completed.isoformat(),
        "baseline_version": BASE_VERSION,
        "draft_version_reviewed": DRAFT_VERSION,
        "decision_source": rel(root, input_paths["decisions"]),
        "decision_source_sha256": observed_hashes["decisions"],
        "formal_product_set": output_identity,
        "promotion_policy": {
            "required_final_decision": "include",
            "required_final_label": "correct_target",
            "allowed_device_types": ["smart_bulb", "smart_switch"],
        },
        "promoted_products": [
            {
                "blind_id": row["blind_id"],
                "parent_asin": row["parent_asin"],
                "device_type": row["device_type"],
                "decision_basis": row["decision_basis"],
            }
            for row in promoted_report_rows
        ],
        "held_or_excluded_products": [
            {
                "blind_id": row["blind_id"],
                "parent_asin": row["parent_asin"],
                "final_label": row["final_label"],
                "final_decision": row["final_decision"],
            }
            for row in held_excluded_report_rows
        ],
    }

    status = {
        "phase": PHASE,
        "status": "PASS",
        "completed_at": completed.isoformat(),
        "formal_version": FORMAL_VERSION,
        "w4r_readiness": "READY_FOR_EXPLICIT_APPROVAL",
        "counts": {
            "baseline_products": 106,
            "promoted_products": 19,
            "promoted_smart_bulb": 17,
            "promoted_smart_switch": 2,
            "held": 1,
            "excluded": 3,
            "formal_products": 125,
            "formal_smart_plug": 95,
            "formal_smart_bulb": 25,
            "formal_smart_switch": 5,
            "unique_parent_asin": 125,
        },
        "integrity": {
            "w3r_b_adjudication_validation_pass": True,
            "baseline_hash_unchanged": True,
            "baseline_non_version_fields_changed": baseline_field_changes,
            "shared_metadata_conflicts": 0,
            "duplicate_parent_asin": 0,
            "held_or_excluded_in_formal_output": 0,
            "parquet_readback_pass": True,
            "schema_compatible": True,
        },
        "formal_product_set": output_identity,
        "baseline_product_set": baseline_identity_after,
        "review_level_base_protection": {
            "opened": False,
            "modified": False,
            "identity_check": "filesystem size and mtime only",
            "identity_before": review_level_before,
            "identity_after": review_level_after,
        },
        "scope_compliance": {
            "review_jsonl_read": False,
            "metadata_jsonl_read": False,
            "gzip_read": False,
            "review_level_base_read": False,
            "w4r_executed": False,
            "w5_started": False,
            "git_commit": False,
            "baseline_target_products_overwritten": False,
        },
    }

    summary = f"""# W3R-C Product Promotion Summary

- Status: **PASS**
- Formal version: `{FORMAL_VERSION}`
- W4R readiness: **READY_FOR_EXPLICIT_APPROVAL**

## Promotion result

| Device type | W3 v1.3.2 | Promoted additions | W3 v1.4.0 |
|---|---:|---:|---:|
| Smart plug | 95 | 0 | 95 |
| Smart bulb | 8 | 17 | 25 |
| Smart switch | 3 | 2 | 5 |
| **Total** | **106** | **19** | **125** |

- The 19 additions are exactly the W3R-B records with `final_decision = include`,
  `final_label = correct_target`, and an approved bulb/switch device type.
- One ambiguous product remains on hold.
- Three products adjudicated as accessories remain excluded.
- All 125 `parent_asin` values are non-empty and unique.
- The 106 baseline products were retained without Metadata changes; only their
  version-management fields were updated for the new catalog release.
- The 106 shared baseline rows have no Metadata conflicts between the W3 and
  W3R-A product files.

## Output

`{rel(root, output_parquet)}`

- Rows: 125
- Fields: {len(output_schema)}
- Compression: ZSTD
- Bytes: {output_identity['bytes']:,}
- SHA-256: `{output_identity['sha256']}`

The original `target_products.parquet` remains unchanged. The W4
`review_level_base.parquet` was not opened or modified. No raw JSONL, gzip,
W4R, W5, or Git commit was used.

Smart plug remains the primary longitudinal class. Smart bulb and smart switch
remain exploratory until an explicitly approved W4R measures their review and
product-month coverage.
"""
    log_lines = [
        f"{started.isoformat()} START {PHASE}",
        f"{datetime.now(timezone.utc).isoformat()} PASS environment and input hashes",
        f"{datetime.now(timezone.utc).isoformat()} PASS W3/W3R status gates",
        f"{datetime.now(timezone.utc).isoformat()} PASS 23 decisions: include=19 hold=1 exclude=3",
        f"{datetime.now(timezone.utc).isoformat()} PASS baseline/draft shared Metadata conflicts=0",
        f"{datetime.now(timezone.utc).isoformat()} PASS formal catalog rows=125 unique_parent_asin=125",
        f"{datetime.now(timezone.utc).isoformat()} PASS Parquet readback and schema validation",
        f"{completed.isoformat()} COMPLETE status=PASS w4r_readiness=READY_FOR_EXPLICIT_APPROVAL",
    ]

    atomic_write_csv(
        output_paths["promoted_csv"],
        promoted_report_rows,
        [
            "blind_id",
            "parent_asin",
            "device_type",
            "product_title",
            "source_domains",
            "final_label",
            "final_decision",
            "decision_basis",
        ],
    )
    atomic_write_csv(
        output_paths["held_excluded_csv"],
        held_excluded_report_rows,
        [
            "blind_id",
            "parent_asin",
            "product_title",
            "final_device_type",
            "final_label",
            "final_decision",
            "decision_basis",
        ],
    )
    atomic_write_csv(
        output_paths["counts_csv"],
        counts_rows,
        ["device_type", "w3_v1_3_2", "promoted_additions", "w3_v1_4_0"],
    )
    atomic_write_json(output_paths["difference_json"], product_difference)
    atomic_write_json(output_paths["schema_json"], schema_report)
    atomic_write_json(output_paths["input_manifest"], input_manifest)
    atomic_write_json(promotion_manifest_path, promotion_manifest)
    atomic_write_text(output_paths["summary_md"], summary)
    atomic_write_json(output_paths["status_json"], status)
    atomic_write_text(output_paths["execution_log"], "\n".join(log_lines) + "\n")

    required_outputs = list(output_paths.values())
    for output in required_outputs:
        if not output.is_file() or output.stat().st_size <= 0:
            fail(f"Required output is missing or empty: {output}")

    print(
        json.dumps(
            {
                "phase": PHASE,
                "status": "PASS",
                "formal_version": FORMAL_VERSION,
                "promoted": 19,
                "promoted_smart_bulb": 17,
                "promoted_smart_switch": 2,
                "held": 1,
                "excluded": 3,
                "formal_counts": {
                    "smart_plug": 95,
                    "smart_bulb": 25,
                    "smart_switch": 5,
                    "total": 125,
                },
                "output_parquet": output_identity,
                "w4r_readiness": "READY_FOR_EXPLICIT_APPROVAL",
                "duration_seconds": round(time.perf_counter() - started_perf, 3),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
