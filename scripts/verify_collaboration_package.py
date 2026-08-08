"""Verify W7-C0 collaboration data, models, package rules, and frozen splits."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import Any

import joblib
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]

DATA = {
    "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet": (
        55_877,
        "93e1aa660e81bcb89cca4d1c9661d76ed9893424fd37d5d67e44fb1c7901c553",
    ),
    "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet": (
        125,
        "e9d0a7548f1568b2bff7b0e2cedd5fa2702cdda2bf507a674a0ad741e89e9a88",
    ),
    "data/amazon_reviews_2023/processed/annotation_labels_w5c_b_v1_0.parquet": (
        1_500,
        "4f52aa604c8798f236eb6401d57ab61265cdfef7b1b54b94a0b4376d141b2ec9",
    ),
    "data/amazon_reviews_2023/processed/review_level_failure_predictions_w6a_v1_0.parquet": (
        55_877,
        "6a947259f9e2240b9d84e5130f657153d0c86466ee27e983a2db4e6694ddc7ab",
    ),
    "data/amazon_reviews_2023/processed/review_level_signal_components_w6b_v1_0.parquet": (
        55_877,
        "816e102dda7045aadb1116429f292f9b55a56ae4da47ea09e11e34479a97ee02",
    ),
    "data/amazon_reviews_2023/processed/product_month_signal_components_w6b_v1_0.parquet": (
        1_911,
        "d2ebc8e4c3021031d727c74de25b0789fd6248c3dadafe029a7dac9092c25947",
    ),
    "data/amazon_reviews_2023/processed/review_level_engineering_index_w6c_v1_0.parquet": (
        55_877,
        "4953cb02ae7293e33a9c51e32407bd9d1e08b6c02af87297c90cfde42ca96916",
    ),
    "data/amazon_reviews_2023/processed/product_month_engineering_index_w6c_v1_0.parquet": (
        1_911,
        "4cd7a5ce497d5eac6fbaeede39bdd87d347e40fb16b320e54dba5fc9413e5438",
    ),
    "data/amazon_reviews_2023/processed/product_month_quality_targets_w6c_v1_0.parquet": (
        1_911,
        "9404528b303cba738026cd3ced7f8e436becd618bc7b2d03cec9e7738e92c0dd",
    ),
    "data/amazon_reviews_2023/processed/product_month_analysis_panel_w6c_v1_0.parquet": (
        1_911,
        "c0f520268b2db674830e56d8e3f2c3fb156ee2b17bc947e1206e08c8ecbf4ac3",
    ),
}

MODELS = {
    "outputs/models/w5c_b_tfidf_logistic_regression.joblib": "6ddc90014da535e16c13344059315a36a22e0ed59cee3d37825a1b282920c86e",
    "outputs/models/w6b_severity_cumulative_logistic.joblib": "0ec77cf7ea69256f64a125dee8fd4d0717fd3a92ca341bf9186f3cf4a0a72ae3",
    "outputs/models/w6b_persistence_cumulative_logistic.joblib": "17cab16add57baadd7a3a86e8803e593bb425bea2fe829ceb4a82347eea08d8e",
}

REQUIRED_VERSIONS = {
    "numpy": "2.4.6",
    "pandas": "3.0.5",
    "pyarrow": "25.0.0",
    "scipy": "1.17.1",
    "scikit-learn": "1.9.0",
    "joblib": "1.5.3",
    "vaderSentiment": "3.3.2",
    "pytest": "9.1.1",
}

ENGINEERING_MAIN_FEATURES = [
    "feature_mean_engineering_index_main",
    "feature_predicted_failure_share",
    "feature_mean_failure_probability",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record(checks: list[dict[str, Any]], name: str, passed: bool, detail: Any) -> None:
    checks.append({"check": name, "passed": bool(passed), "detail": detail})


def verify(require_release_ready: bool = False) -> tuple[int, dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    record(checks, "python_3_11", sys.version_info[:2] == (3, 11), platform.python_version())
    record(checks, "python_64_bit", platform.architecture()[0] == "64bit", platform.architecture()[0])

    versions = {}
    for package, expected in REQUIRED_VERSIONS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        versions[package] = actual
        record(checks, f"version_{package}", actual == expected, {"expected": expected, "actual": actual})

    for rel, (expected_rows, expected_hash) in DATA.items():
        path = ROOT / rel
        exists = path.exists()
        record(checks, f"exists_{rel}", exists, rel)
        if not exists:
            continue
        parquet_file = pq.ParquetFile(path)
        record(checks, f"rows_{rel}", parquet_file.metadata.num_rows == expected_rows, parquet_file.metadata.num_rows)
        actual_hash = sha256(path)
        record(checks, f"sha256_{rel}", actual_hash == expected_hash, actual_hash)

    for rel, expected_hash in MODELS.items():
        path = ROOT / rel
        exists = path.exists()
        record(checks, f"exists_{rel}", exists, rel)
        if not exists:
            continue
        actual_hash = sha256(path)
        record(checks, f"sha256_{rel}", actual_hash == expected_hash, actual_hash)
        try:
            loaded = joblib.load(path)
            record(checks, f"load_{rel}", isinstance(loaded, dict), type(loaded).__name__)
        except Exception as exc:  # pragma: no cover - diagnostic detail
            record(checks, f"load_{rel}", False, type(exc).__name__)

    review_path = ROOT / next(key for key in DATA if "review_level_base" in key)
    product_path = ROOT / next(key for key in DATA if "target_products" in key)
    panel_path = ROOT / next(key for key in DATA if "analysis_panel" in key)
    if review_path.exists() and product_path.exists():
        reviews = pq.read_table(review_path, columns=["duplicate_key", "parent_asin"])
        duplicate_values = reviews["duplicate_key"].to_pylist()
        record(checks, "review_duplicate_key_unique", len(duplicate_values) == len(set(duplicate_values)), len(duplicate_values))
        product_values = set(pq.read_table(product_path, columns=["parent_asin"])["parent_asin"].to_pylist())
        review_products = set(reviews["parent_asin"].to_pylist())
        record(checks, "review_products_in_target_set", review_products.issubset(product_values), len(review_products))

    if panel_path.exists():
        panel = pq.read_table(
            panel_path,
            columns=["parent_asin", "review_month", "eligible_main_h3", "proposed_split_h3"],
        ).to_pydict()
        keys = list(zip(panel["parent_asin"], map(str, panel["review_month"])))
        record(checks, "product_month_keys_unique", len(keys) == len(set(keys)), len(keys))
        eligible_indices = [index for index, value in enumerate(panel["eligible_main_h3"]) if bool(value)]
        record(checks, "eligible_main_h3_515", len(eligible_indices) == 515, len(eligible_indices))
        split_counts: dict[str, int] = {}
        for index in eligible_indices:
            split = str(panel["proposed_split_h3"][index])
            split_counts[split] = split_counts.get(split, 0) + 1
        expected_splits = {
            "train": 205,
            "embargo_train_validation": 28,
            "validation": 150,
            "embargo_validation_test": 17,
            "test": 115,
        }
        record(checks, "frozen_split_counts", split_counts == expected_splits, split_counts)

    if review_path.exists():
        review_schema = pq.ParquetFile(review_path).schema_arrow.names
        record(checks, "formal_review_has_no_source_user_id", "user_id" not in review_schema, review_schema)
        record(checks, "formal_review_has_approved_user_id_hash", "user_id_hash" in review_schema, review_schema)

    allowlist_path = ROOT / "collaboration/publication_allowlist.txt"
    if allowlist_path.exists():
        entries = [
            line.strip()
            for line in allowlist_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        forbidden = [
            entry
            for entry in entries
            if entry.lower().endswith((".jsonl", ".jsonl.gz", ".gz", ".xlsx", ".mp4"))
            or "/raw/" in f"/{entry.lower()}/"
            or "blind_review_key" in entry.lower()
        ]
        record(checks, "publication_allowlist_has_no_forbidden_files", not forbidden, forbidden)
        record(checks, "publication_allowlist_contains_all_formal_data", set(DATA).issubset(entries), len(entries))

    status_path = ROOT / "collaboration/w7c0_status.json"
    release_status = None
    if status_path.exists():
        release_status = json.loads(status_path.read_text(encoding="utf-8")).get("status")
    technical_pass = all(item["passed"] for item in checks)
    release_ready = release_status == "READY_FOR_GITHUB_PUBLISH_APPROVAL"
    result = {
        "technical_status": "PASS" if technical_pass else "FAIL",
        "publication_status": release_status,
        "release_ready": release_ready,
        "engineering_main_features": ENGINEERING_MAIN_FEATURES,
        "checks": checks,
    }
    if not technical_pass:
        return 1, result
    if require_release_ready and not release_ready:
        return 2, result
    return 0, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args()
    code, result = verify(require_release_ready=args.require_release_ready)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
