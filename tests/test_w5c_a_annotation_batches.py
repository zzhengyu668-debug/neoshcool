from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_w5c_a_annotation_batches.py"
INTERIM = ROOT / "data" / "amazon_reviews_2023" / "interim" / "w5c_a"
REPORTS = ROOT / "data" / "amazon_reviews_2023" / "reports" / "w5c_a"


def load_module():
    spec = importlib.util.spec_from_file_location("w5c_a", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_bucket_targets_are_complete_and_deterministic():
    module = load_module()
    shares = {
        "high_uncertainty": 0.40,
        "rating_keyword_disagreement": 0.30,
        "diversity_control": 0.30,
    }
    assert module.bucket_targets(232, shares) == {
        "high_uncertainty": 92,
        "rating_keyword_disagreement": 69,
        "diversity_control": 71,
    }
    assert sum(module.bucket_targets(231, shares).values()) == 231


def test_private_scoring_uses_transform_only():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "vectorizer.transform(model_text)" in source
    assert "classifier.predict_proba(matrix)" in source
    assert ".fit(" not in source
    assert ".fit_transform(" not in source
    assert "predictions_used_as_labels" in source


def test_expanded_sample_has_exact_quota_and_no_old_overlap():
    expanded = pd.read_parquet(
        INTERIM / "expanded_annotation_sampling_frame.parquet",
        columns=[
            "duplicate_key",
            "device_type",
            "batch_id",
            "selected_for_double_review",
        ],
    )
    old = pd.read_parquet(
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "interim"
        / "w5a"
        / "annotation_sampling_frame.parquet",
        columns=["duplicate_key"],
    )
    assert len(expanded) == 1200
    assert expanded["duplicate_key"].is_unique
    assert not set(expanded["duplicate_key"]).intersection(old["duplicate_key"])
    assert Counter(expanded["device_type"]) == {
        "smart_plug": 927,
        "smart_bulb": 240,
        "smart_switch": 33,
    }
    assert int(expanded["selected_for_double_review"].sum()) == 240


def test_batch_and_double_review_quotas():
    expanded = pd.read_parquet(
        INTERIM / "expanded_annotation_sampling_frame.parquet",
        columns=["batch_id", "device_type", "selected_for_double_review"],
    )
    expected = {
        "batch_1": {
            "all": {"smart_plug": 232, "smart_bulb": 60, "smart_switch": 8},
            "double": {"smart_plug": 46, "smart_bulb": 12, "smart_switch": 2},
        },
        "batch_2": {
            "all": {"smart_plug": 232, "smart_bulb": 60, "smart_switch": 8},
            "double": {"smart_plug": 46, "smart_bulb": 12, "smart_switch": 2},
        },
        "batch_3": {
            "all": {"smart_plug": 232, "smart_bulb": 60, "smart_switch": 8},
            "double": {"smart_plug": 46, "smart_bulb": 12, "smart_switch": 2},
        },
        "batch_4": {
            "all": {"smart_plug": 231, "smart_bulb": 60, "smart_switch": 9},
            "double": {"smart_plug": 47, "smart_bulb": 12, "smart_switch": 1},
        },
    }
    for batch_id, quotas in expected.items():
        batch = expanded.loc[expanded["batch_id"] == batch_id]
        assert Counter(batch["device_type"]) == quotas["all"]
        double = batch.loc[batch["selected_for_double_review"]]
        assert Counter(double["device_type"]) == quotas["double"]
        assert len(batch) == 300
        assert len(double) == 60


def test_all_remaining_switch_reviews_were_selected():
    formal = pd.read_parquet(
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "processed"
        / "review_level_base_w3_v1_4_0.parquet",
        columns=["duplicate_key", "device_type"],
    )
    old = pd.read_parquet(
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "interim"
        / "w5a"
        / "annotation_sampling_frame.parquet",
        columns=["duplicate_key"],
    )
    expanded = pd.read_parquet(
        INTERIM / "expanded_annotation_sampling_frame.parquet",
        columns=["duplicate_key", "device_type"],
    )
    expected = set(
        formal.loc[
            (formal["device_type"] == "smart_switch")
            & ~formal["duplicate_key"].isin(old["duplicate_key"]),
            "duplicate_key",
        ]
    )
    actual = set(
        expanded.loc[
            expanded["device_type"] == "smart_switch", "duplicate_key"
        ]
    )
    assert len(expected) == 33
    assert actual == expected


def test_private_files_and_blind_csvs_do_not_leak():
    private_scores_path = INTERIM / "private_sampling_scores_w5c_a.parquet"
    assert pq.ParquetFile(private_scores_path).metadata.num_rows == 55577
    private_fields = set(pq.read_schema(private_scores_path).names)
    assert "review_text" not in private_fields
    assert "user_id" not in private_fields
    assert "user_id_hash" not in private_fields

    hidden = {
        "rating",
        "keyword_candidate_hit",
        "model_failure_probability",
        "model_uncertainty_distance",
        "parent_asin",
        "duplicate_key",
        "review_datetime",
        "source_domain",
    }
    for path in sorted(INTERIM.glob("annotation_batch_*_blind.csv")):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        assert not hidden.intersection(frame.columns)
        label_columns = [
            column
            for column in frame.columns
            if column.startswith(
                ("reviewer_", "adjudicated_", "adjudication_")
            )
        ]
        assert (
            frame[label_columns]
            .apply(lambda column: column.str.strip().eq("").all())
            .all()
        )


def test_input_identities_and_prepared_status():
    expected = {
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "processed"
        / "review_level_base_w3_v1_4_0.parquet":
            "93e1aa660e81bcb89cca4d1c9661d76ed9893424fd37d5d67e44fb1c7901c553",
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "interim"
        / "w5a"
        / "annotation_sampling_frame.parquet":
            "68a46fa0cb83ff38c43f48b6cb2bb8a7f85c8f6360b06a23829f85d2c8f70a06",
        ROOT
        / "data"
        / "amazon_reviews_2023"
        / "processed"
        / "annotation_labels_w5b_v1_0.parquet":
            "1a9ec1a078895ed5fe45f07195fdd2055e59e10d7eeaed0eafb8975ffad13d23",
        ROOT / "outputs" / "models" / "w5b_tfidf_logistic_regression.joblib":
            "376b3c367b3abaf107b0f0fd9ea7ea800f1e1ac478bc8e805101801fe0e34712",
    }
    for path, expected_hash in expected.items():
        assert sha256(path) == expected_hash
    status = json.loads(
        (REPORTS / "w5c_a_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] in {
        "PREPARED_FOR_WORKBOOK_BUILD",
        "PAUSED_HUMAN_ANNOTATION",
    }
    assert status.get("model_refit", status.get("pilot_model_refit")) is False
    assert (
        status.get(
            "predictions_used_as_labels",
            status.get("pilot_predictions_used_as_labels"),
        )
        is False
    )


if __name__ == "__main__":
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test_name, test_function in tests:
        test_function()
        print(f"PASS {test_name}")
    print(f"{len(tests)} W5-C-A tests passed")
