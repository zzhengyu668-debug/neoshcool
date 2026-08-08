"""Prepare the approved W7-C0 public collaboration package.

This script never edits frozen inputs. It records the project owner's explicit
release decision while continuing to exclude raw and private project files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import pyarrow as pa
import pyarrow.parquet as pq
import sklearn


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "amazon_reviews_2023" / "processed"
COLLAB_DATA = ROOT / "data" / "amazon_reviews_2023" / "collaboration"
COLLAB = ROOT / "collaboration"
PUBLIC_RELEASE_APPROVED = True
PUBLIC_RELEASE_APPROVAL_DATE = "2026-08-08"

DATA_FILES: list[dict[str, Any]] = [
    {
        "path": "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet",
        "rows": 55_877,
        "sha256": "93e1aa660e81bcb89cca4d1c9661d76ed9893424fd37d5d67e44fb1c7901c553",
        "stage": "W4R",
        "purpose": "Formal cleaned review corpus approved for public collaboration",
    },
    {
        "path": "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
        "rows": 125,
        "sha256": "e9d0a7548f1568b2bff7b0e2cedd5fa2702cdda2bf507a674a0ad741e89e9a88",
        "stage": "W3R-C",
        "purpose": "Frozen target-product set",
    },
    {
        "path": "data/amazon_reviews_2023/processed/annotation_labels_w5c_b_v1_0.parquet",
        "rows": 1_500,
        "sha256": "4f52aa604c8798f236eb6401d57ab61265cdfef7b1b54b94a0b4376d141b2ec9",
        "stage": "W5-C-B",
        "purpose": "Frozen human labels without review text",
    },
    {
        "path": "data/amazon_reviews_2023/processed/review_level_failure_predictions_w6a_v1_0.parquet",
        "rows": 55_877,
        "sha256": "6a947259f9e2240b9d84e5130f657153d0c86466ee27e983a2db4e6694ddc7ab",
        "stage": "W6-A",
        "purpose": "Frozen review-level failure predictions",
    },
    {
        "path": "data/amazon_reviews_2023/processed/review_level_signal_components_w6b_v1_0.parquet",
        "rows": 55_877,
        "sha256": "816e102dda7045aadb1116429f292f9b55a56ae4da47ea09e11e34479a97ee02",
        "stage": "W6-B",
        "purpose": "Frozen review-level Failure, Severity, Persistence, and Sentiment components",
    },
    {
        "path": "data/amazon_reviews_2023/processed/product_month_signal_components_w6b_v1_0.parquet",
        "rows": 1_911,
        "sha256": "d2ebc8e4c3021031d727c74de25b0789fd6248c3dadafe029a7dac9092c25947",
        "stage": "W6-B",
        "purpose": "Frozen product-month signal components",
    },
    {
        "path": "data/amazon_reviews_2023/processed/review_level_engineering_index_w6c_v1_0.parquet",
        "rows": 55_877,
        "sha256": "4953cb02ae7293e33a9c51e32407bd9d1e08b6c02af87297c90cfde42ca96916",
        "stage": "W6-C",
        "purpose": "Frozen review-level EngineeringIndex",
    },
    {
        "path": "data/amazon_reviews_2023/processed/product_month_engineering_index_w6c_v1_0.parquet",
        "rows": 1_911,
        "sha256": "4cd7a5ce497d5eac6fbaeede39bdd87d347e40fb16b320e54dba5fc9413e5438",
        "stage": "W6-C",
        "purpose": "Frozen product-month EngineeringIndex",
    },
    {
        "path": "data/amazon_reviews_2023/processed/product_month_quality_targets_w6c_v1_0.parquet",
        "rows": 1_911,
        "sha256": "9404528b303cba738026cd3ced7f8e436becd618bc7b2d03cec9e7738e92c0dd",
        "stage": "W6-C",
        "purpose": "Frozen future quality targets and split audit",
    },
    {
        "path": "data/amazon_reviews_2023/processed/product_month_analysis_panel_w6c_v1_0.parquet",
        "rows": 1_911,
        "sha256": "c0f520268b2db674830e56d8e3f2c3fb156ee2b17bc947e1206e08c8ecbf4ac3",
        "stage": "W6-C",
        "purpose": "Frozen no-leakage product-month analysis panel",
    },
]

MODEL_FILES: list[dict[str, Any]] = [
    {
        "path": "outputs/models/w5c_b_tfidf_logistic_regression.joblib",
        "sha256": "6ddc90014da535e16c13344059315a36a22e0ed59cee3d37825a1b282920c86e",
        "role": "upstream_failure_model",
        "publication_label": "trusted_frozen_upstream_model",
    },
    {
        "path": "outputs/models/w6b_severity_cumulative_logistic.joblib",
        "sha256": "0ec77cf7ea69256f64a125dee8fd4d0717fd3a92ca341bf9186f3cf4a0a72ae3",
        "role": "upstream_severity_model",
        "publication_label": "trusted_frozen_upstream_model",
    },
    {
        "path": "outputs/models/w6b_persistence_cumulative_logistic.joblib",
        "sha256": "17cab16add57baadd7a3a86e8803e593bb425bea2fe829ceb4a82347eea08d8e",
        "role": "upstream_persistence_model",
        "publication_label": "trusted_frozen_upstream_model",
    },
    {
        "path": "outputs/models/w6d/h3_logistic_rating_only.joblib",
        "sha256": "39b8776cd5600e74a614f04ba839454d80a2976fae6e9a9f85301a965cfde763",
        "role": "rating_only_reference",
        "publication_label": "frozen_reference_model",
    },
    {
        "path": "outputs/models/w6d/h3_logistic_text_only.joblib",
        "sha256": "a031aeea6eaf5d278062c472c9e321dbf7ffec6002775d2c5f677413dc239cea",
        "role": "supplemental_text_only",
        "publication_label": "frozen_reference_model",
    },
    {
        "path": "outputs/models/w6d/h3_logistic_text_plus_sentiment.joblib",
        "sha256": "cb30dd3baa3fd4a598f517781dd6309d48918e936ac121d45e2279f2d068ff16",
        "role": "supplemental_text_plus_sentiment",
        "publication_label": "frozen_reference_model_not_sentiment_only",
    },
    {
        "path": "outputs/models/w6d/h3_logistic_text_plus_engineering.joblib",
        "sha256": "17c3eb8ef11bc4f7e10d744533d733154b9497514eb50dd44e474277dcf91abf",
        "role": "supplemental_text_plus_engineering",
        "publication_label": "frozen_reference_model_not_engineering_only",
    },
]

PUBLICATION_SAFE_FILES = [
    ".gitignore",
    "README.md",
    "DATA_PROVENANCE.md",
    "requirements-collaboration.txt",
    "config/project.toml",
    "config/w5c_b_baseline_rules.toml",
    "config/w6a_full_inference_rules.toml",
    "config/w6b_signal_component_rules.toml",
    "config/w6c_engineering_target_rules.toml",
    "config/w6d_warning_model_rules.toml",
    "docs/ENGINEERING_ONLY_HANDOFF.md",
    "scripts/check_environment.py",
    "scripts/run_w5c_b_expanded_labels_and_baselines.py",
    "scripts/run_w6a_full_failure_inference.py",
    "scripts/run_w6b_signal_components.py",
    "scripts/build_w6c_engineering_targets.py",
    "scripts/run_w6d_controlled_warning_comparison.py",
    "scripts/prepare_w7c0_collaboration_package.py",
    "scripts/verify_collaboration_package.py",
    "tests/test_w5c_b_expanded_labels_and_baselines.py",
    "tests/test_w6a_full_failure_inference.py",
    "tests/test_w6b_signal_components.py",
    "tests/test_w6c_engineering_targets.py",
    "tests/test_w6d_controlled_warning_comparison.py",
    "tests/test_w7c0_collaboration_package.py",
    "collaboration/package_manifest.json",
    "collaboration/package_manifest.csv",
    "collaboration/data_dictionary.md",
    "collaboration/model_manifest.json",
    "collaboration/reproduction_checklist.md",
    "collaboration/excluded_files_audit.txt",
    "collaboration/privacy_audit.json",
    "collaboration/package_summary.md",
    "collaboration/w7c0_status.json",
    "collaboration/publication_allowlist.txt",
    "collaboration/pending_data_allowlist.txt",
    "collaboration/fresh_environment_validation.json",
    "collaboration/github_repository_audit.json",
    "collaboration/release_approval.json",
]

PATTERNS = {
    "email": re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])"),
    "us_phone": re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-])\d{3}[\s.\-]\d{4}(?!\d)"),
    "amazon_order": re.compile(r"(?<!\d)\d{3}-\d{7}-\d{7}(?!\d)"),
    "windows_path": re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]*"),
    "wechat_marker": re.compile(r"(?i)wxid_|xwechat_files|wechat|微信"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8", newline="\n")


def create_collaboration_candidate(source: Path) -> tuple[Path, list[str], bool]:
    target = COLLAB_DATA / "review_level_base_w3_v1_4_0_collaboration_candidate.parquet"
    sidecar = COLLAB / "derived_candidate_manifest.json"
    source_hash = sha256(source)
    table = pq.read_table(source)
    removed = [name for name in ("user_id", "user_id_hash") if name in table.column_names]
    expected_names = [name for name in table.column_names if name not in removed]
    reusable = False
    if target.exists() and sidecar.exists():
        previous = json.loads(sidecar.read_text(encoding="utf-8"))
        candidate = pq.ParquetFile(target)
        reusable = (
            previous.get("source_sha256") == source_hash
            and candidate.metadata.num_rows == table.num_rows
            and candidate.schema_arrow.names == expected_names
        )
    if not reusable:
        target.parent.mkdir(parents=True, exist_ok=True)
        table = table.select(expected_names)
        pq.write_table(table, target, compression="zstd")
    manifest = {
        "source_path": source.relative_to(ROOT).as_posix(),
        "source_sha256": source_hash,
        "candidate_path": target.relative_to(ROOT).as_posix(),
        "candidate_sha256": sha256(target),
        "rows": pq.ParquetFile(target).metadata.num_rows,
        "removed_fields": removed,
        "remaining_fields": pq.ParquetFile(target).schema_arrow.names,
        "formal_input_modified": False,
        "reused_existing_candidate": reusable,
        "release_status": "EXCLUDED_DUPLICATE_NOT_REQUIRED",
    }
    write_if_changed(sidecar, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return target, removed, reusable


def scan_review_text(path: Path) -> dict[str, Any]:
    texts = pq.read_table(path, columns=["review_text"])["review_text"].to_pylist()
    counts = {name: 0 for name in PATTERNS}
    rows_any = 0
    for value in texts:
        text = "" if value is None else str(value)
        any_hit = False
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                counts[name] += 1
                any_hit = True
        rows_any += int(any_hit)
    return {
        "rows_scanned": len(texts),
        "rows_with_any_pattern": rows_any,
        "pattern_row_counts": counts,
        "actual_text_saved_to_report": False,
        "automatic_redaction_performed": False,
    }


def parquet_record(item: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / item["path"]
    parquet_file = pq.ParquetFile(path)
    names = parquet_file.schema_arrow.names
    return {
        **item,
        "kind": "parquet",
        "exists": path.exists(),
        "actual_rows": parquet_file.metadata.num_rows,
        "fields": len(names),
        "field_names": names,
        "bytes": path.stat().st_size,
        "actual_sha256": sha256(path),
        "identity_match": (
            parquet_file.metadata.num_rows == item["rows"]
            and sha256(path) == item["sha256"]
        ),
        "contains_review_text": "review_text" in names,
        "contains_future_target": any(
            name.startswith("target_") or "future_" in name for name in names
        ),
        "contains_personal_identifier_field": any(
            name in {"user_id", "user_id_hash", "reviewer_id", "annotator_id"}
            for name in names
        ),
        "release_status": "APPROVED_BY_PROJECT_OWNER",
    }


def model_record(item: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / item["path"]
    obj = joblib.load(path)
    record = {
        **item,
        "kind": "joblib",
        "bytes": path.stat().st_size,
        "actual_sha256": sha256(path),
        "identity_match": sha256(path) == item["sha256"],
        "loaded_type": f"{type(obj).__module__}.{type(obj).__name__}",
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "joblib_load_warning": "Load only from a trusted source; joblib can execute code.",
        "release_status": "APPROVED_PENDING_EXPLICIT_PUBLISH",
    }
    if isinstance(obj, dict):
        record["artifact_keys"] = sorted(str(key) for key in obj)
        record["feature_field"] = obj.get("feature_field")
        record["numeric_features"] = obj.get("numeric_features")
        record["decision_threshold"] = obj.get("decision_threshold")
        record["route"] = obj.get("route")
    return record


def data_dictionary(records: list[dict[str, Any]], candidate: Path) -> str:
    lines = [
        "# Collaboration data dictionary",
        "",
        "This document lists fields only; it never contains review text or row-level identifiers.",
        "",
    ]
    for record in records:
        path = ROOT / record["path"]
        schema = pq.ParquetFile(path).schema_arrow
        lines.extend(
            [
                f"## `{record['path']}`",
                "",
                f"- Rows: {record['actual_rows']:,}",
                f"- Fields: {record['fields']}",
                f"- Source phase: {record['stage']}",
                f"- Release status: `{record['release_status']}`",
                "",
                "| Field | Arrow type |",
                "|---|---|",
            ]
        )
        for field in schema:
            lines.append(f"| `{field.name}` | `{field.type}` |")
        lines.append("")
    candidate_schema = pq.ParquetFile(candidate).schema_arrow
    lines.extend(
        [
            f"## `{candidate.relative_to(ROOT).as_posix()}`",
            "",
            "This local derivative removes `user_id_hash` but is excluded as a duplicate because the formal frozen file was explicitly approved for release.",
            "",
            "| Field | Arrow type |",
            "|---|---|",
        ]
    )
    for field in candidate_schema:
        lines.append(f"| `{field.name}` | `{field.type}` |")
    lines.append("")
    return "\n".join(lines)


def git_status() -> dict[str, Any]:
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {"dirty": bool(status), "entry_count": len(status), "entries": status}


def main() -> int:
    COLLAB.mkdir(parents=True, exist_ok=True)
    source_review = PROCESSED / "review_level_base_w3_v1_4_0.parquet"
    source_hash_before = sha256(source_review)
    candidate, removed, reused = create_collaboration_candidate(source_review)
    privacy = scan_review_text(source_review)
    source_hash_after = sha256(source_review)
    privacy.update(
        {
            "formal_review_sha256_before": source_hash_before,
            "formal_review_sha256_after": source_hash_after,
            "formal_review_unchanged": source_hash_before == source_hash_after,
            "formal_pseudonymous_identifier_fields": ["user_id_hash"],
            "collaboration_candidate": candidate.relative_to(ROOT).as_posix(),
            "collaboration_candidate_sha256": sha256(candidate),
            "collaboration_candidate_removed_fields": removed,
            "candidate_reused": reused,
            "release_decision": "APPROVED_BY_PROJECT_OWNER_WITH_DISCLOSED_PATTERN_RISK",
            "release_approval_date": PUBLIC_RELEASE_APPROVAL_DATE,
        }
    )

    data_records = [parquet_record(item) for item in DATA_FILES]
    if not all(record["identity_match"] for record in data_records):
        raise RuntimeError("One or more frozen data identities do not match.")
    model_records = [model_record(item) for item in MODEL_FILES]
    if not all(record["identity_match"] for record in model_records):
        raise RuntimeError("One or more frozen model identities do not match.")

    candidate_record = {
        "path": candidate.relative_to(ROOT).as_posix(),
        "purpose": "Local collaboration candidate with user hash removed",
        "stage": "W7-C0",
        "kind": "parquet",
        "actual_rows": pq.ParquetFile(candidate).metadata.num_rows,
        "fields": len(pq.ParquetFile(candidate).schema_arrow.names),
        "field_names": pq.ParquetFile(candidate).schema_arrow.names,
        "bytes": candidate.stat().st_size,
        "actual_sha256": sha256(candidate),
        "contains_review_text": True,
        "contains_future_target": False,
        "contains_personal_identifier_field": False,
        "release_status": "EXCLUDED_DUPLICATE_NOT_REQUIRED",
    }

    all_candidate_paths = [record["path"] for record in data_records]
    all_candidate_paths += [record["path"] for record in model_records]
    all_candidate_paths += PUBLICATION_SAFE_FILES
    existing_candidate_paths = [
        path
        for path in all_candidate_paths
        if (ROOT / path).exists() and not path.startswith("collaboration/")
    ]
    total_bytes = sum((ROOT / path).stat().st_size for path in existing_candidate_paths)
    largest = sorted(
        (
            {"path": path, "bytes": (ROOT / path).stat().st_size}
            for path in existing_candidate_paths
        ),
        key=lambda item: item["bytes"],
        reverse=True,
    )[:20]

    existing_manifest_path = COLLAB / "package_manifest.json"
    if existing_manifest_path.exists():
        first_generated_at = json.loads(
            existing_manifest_path.read_text(encoding="utf-8")
        ).get("generated_at_utc")
    else:
        first_generated_at = None
    package_manifest = {
        "phase": "W7-C0",
        "generated_at_utc": first_generated_at or datetime.now(timezone.utc).isoformat(),
        "technical_input_identity": "PASS",
        "package_status": "READY_FOR_GITHUB_PUBLISH_APPROVAL",
        "secondary_hold": None,
        "candidate_file_count": len(existing_candidate_paths),
        "candidate_total_bytes": total_bytes,
        "single_file_over_50_mib": [
            item for item in largest if item["bytes"] > 50 * 1024 * 1024
        ],
        "git_lfs_required_by_current_file_sizes": False,
        "data": data_records,
        "models": model_records,
        "publication_safe_files": [
            path for path in PUBLICATION_SAFE_FILES if (ROOT / path).exists()
        ],
        "publication_approved_data": [record["path"] for record in data_records],
        "publication_excluded_data": [candidate_record["path"]],
        "largest_20_candidate_files": largest,
    }
    write_if_changed(
        COLLAB / "package_manifest.json",
        json.dumps(package_manifest, indent=2, ensure_ascii=False) + "\n",
    )

    csv_fields = [
        "path",
        "kind",
        "purpose",
        "stage",
        "actual_rows",
        "fields",
        "bytes",
        "actual_sha256",
        "contains_review_text",
        "contains_future_target",
        "contains_personal_identifier_field",
        "release_status",
    ]
    rows = data_records
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=csv_fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    write_if_changed(COLLAB / "package_manifest.csv", "\ufeff" + csv_buffer.getvalue())

    write_if_changed(
        COLLAB / "model_manifest.json",
        json.dumps(
            {
                "trusted_source_only": True,
                "python_version": platform.python_version(),
                "scikit_learn_version": sklearn.__version__,
                "models": model_records,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    write_if_changed(
        COLLAB / "privacy_audit.json",
        json.dumps(privacy, indent=2, ensure_ascii=False) + "\n",
    )
    write_if_changed(
        COLLAB / "data_dictionary.md",
        data_dictionary(data_records, candidate),
    )

    allowlist = [
        path for path in PUBLICATION_SAFE_FILES if (ROOT / path).exists()
    ] + [record["path"] for record in model_records] + [record["path"] for record in data_records]
    write_if_changed(
        COLLAB / "publication_allowlist.txt",
        "# W7-C0 project-owner-approved public collaboration release.\n"
        + "\n".join(sorted(allowlist))
        + "\n",
    )
    write_if_changed(
        COLLAB / "pending_data_allowlist.txt",
        "# No approved formal data are pending. The local duplicate below is intentionally excluded.\n"
        + f"{candidate_record['path']}\n",
    )

    excluded_lines = [
        "# Excluded from W7-C0 publication candidates",
        "data/amazon_reviews_2023/raw/",
        "data/amazon_reviews_2023/interim/",
        "data/amazon_reviews_2023/interim/w7b0/temporal_granularity_candidate_origins.parquet",
        "data/amazon_reviews_2023/reports/**/*.xlsx",
        "**/*blind_review_key*",
        "**/*completed*.xlsx",
        "**/*adjudicat*.xlsx",
        "*.jsonl",
        "*.jsonl.gz",
        "*.gz",
        ".venv/",
        "tmp/",
        "output/",
        "*.mp4",
        "*.docx",
        "*.pdf",
        "*.pptx",
        "private error-analysis text",
        "Git credentials and tokens",
        "",
        "Formal review source: included after explicit project-owner approval; user_id_hash and text-pattern risk are disclosed.",
        "Collaboration review candidate exclusion reason: duplicate derivative not needed for the approved release.",
        "No excluded file was deleted by W7-C0.",
    ]
    write_if_changed(COLLAB / "excluded_files_audit.txt", "\n".join(excluded_lines))

    checklist = """# Reproduction checklist

- [ ] Clone the repository into a path without hard-coded user names.
- [ ] Install 64-bit Python 3.11.
- [ ] Create `.venv` and install `requirements-collaboration.txt`.
    - [ ] Confirm the approved data files were downloaded with the repository.
- [ ] Run `scripts/verify_collaboration_package.py`.
- [ ] Confirm all SHA-256 values match.
- [ ] Confirm 55,877 cleaned reviews and 125 target products.
- [ ] Confirm 1,911 unique product-month rows.
- [ ] Confirm 515 h=3 eligible rows and split counts 205/28/150/17/115.
- [ ] Read `docs/ENGINEERING_ONLY_HANDOFF.md`.
- [ ] Keep Rating, Sentiment, text, identity, and future fields out of Engineering-only.
- [ ] Fit preprocessing on Train only.
- [ ] Develop on Validation only.
- [ ] Freeze code, features, environment, parameters, and threshold before requesting Test approval.
- [ ] Compare the two independent Engineering-only implementations.
- [ ] Preserve negative and uncertain results.
"""
    write_if_changed(COLLAB / "reproduction_checklist.md", checklist)

    status = {
        "phase": "W7-C0",
        "status": "READY_FOR_GITHUB_PUBLISH_APPROVAL",
        "secondary_status": None,
        "technical_identity_checks": "PASS",
        "engineering_only_trained": False,
        "test_target_performance_evaluated": False,
        "test_target_used_for_development": False,
        "manual_precheck_note": "An aggregate all-eligible target count was loaded during precheck; no Test-specific target metric, prediction, or model selection was computed.",
        "git_publication_authorized_by_current_request": True,
        "publication_allowlist_contains_data": True,
        "public_release_approved_by_project_owner": PUBLIC_RELEASE_APPROVED,
        "public_release_approval_date": PUBLIC_RELEASE_APPROVAL_DATE,
        "privacy_pattern_rows": privacy["rows_with_any_pattern"],
        "formal_inputs_modified": False,
    }
    write_if_changed(
        COLLAB / "w7c0_status.json",
        json.dumps(status, indent=2, ensure_ascii=False) + "\n",
    )

    summary = f"""# W7-C0 collaboration package summary

## Status

- Primary status: `READY_FOR_GITHUB_PUBLISH_APPROVAL`
- Secondary hold: none
- Frozen input identities: PASS
- Engineering-only trained: no
- Test performance evaluated: no
- Test target used for development: no
- Process note: an aggregate all-eligible target count was loaded during precheck; no Test-specific metric, prediction, or model-selection calculation was performed.
- Git publication: explicitly authorized; the resulting branch, commit, push, and PR are recorded by Git/GitHub rather than this preparation snapshot

## Package inventory

- Candidate files currently present: {len(existing_candidate_paths)}
- Candidate total size: {total_bytes:,} bytes
- Single files over 50 MiB: {len(package_manifest['single_file_over_50_mib'])}
- Git LFS required by current sizes: no

## Privacy decision

The formal 55,877-row review file contains the pseudonymous `user_id_hash`. The project owner explicitly approved publishing it unchanged. Automated review-text scanning found {privacy['rows_with_any_pattern']} rows with email- or phone-shaped strings. No text was copied into reports and no automatic redaction occurred. The local hash-free derivative is excluded as an unnecessary duplicate.

## Redistribution decision

The project owner confirmed public release of this cleaned research subset on {PUBLIC_RELEASE_APPROVAL_DATE}. Raw source JSONL/GZ files remain excluded, and downstream users are directed to the upstream citation and terms.

## Next decision

Publish only the exact allowlist, then have both Engineering-only implementers run the verifier before development.
"""
    write_if_changed(COLLAB / "package_summary.md", summary)
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
