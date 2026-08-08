"""Focused tests for W6-D route parity, temporal isolation, and leakage controls."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import sparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_w6d_controlled_warning_comparison.py"
SPEC = importlib.util.spec_from_file_location("w6d", SCRIPT)
assert SPEC and SPEC.loader
w6d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w6d)


def rules() -> dict:
    return w6d.load_toml(ROOT / "config" / "w6d_warning_model_rules.toml")


def sample_rows() -> pd.DataFrame:
    rows = []
    months = [
        ("2019-06", "train"),
        ("2019-07", "train"),
        ("2020-01", "validation"),
        ("2020-02", "validation"),
        ("2022-01", "test"),
        ("2022-02", "test"),
    ]
    for index, (month, split) in enumerate(months):
        rows.append(
            {
                "parent_asin": f"P{index}",
                "review_month": pd.Period(month, freq="M").to_timestamp().date(),
                "device_type": "smart_plug" if index % 2 == 0 else "smart_bulb",
                "analysis_role": "primary" if index % 2 == 0 else "exploratory",
                "horizon": 3,
                "split": split,
                "target": index % 2,
                "feature_n_reviews": 2,
                "feature_mean_sentiment_compound": 0.1 * index,
                "feature_negative_sentiment_share": 0.05 * index,
                "feature_mean_engineering_index_main": 0.15 * index,
                "feature_predicted_failure_share": 0.1 * index,
                "feature_mean_failure_probability": 0.12 * index,
                "feature_mean_rating": 4.5 - 0.2 * index,
                "feature_low_star_share": 0.05 * index,
                "feature_historical_rating_mean": 4.6 - 0.1 * index,
                "feature_historical_low_star_share": 0.03 * index,
                "feature_historical_n_reviews": 10 + index,
            }
        )
    return pd.DataFrame(rows)


def review_rows(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for row in samples.itertuples(index=False):
        month = pd.Period(row.review_month, freq="M").to_timestamp()
        split_word = {
            "train": "shared training",
            "validation": "validationonly",
            "test": "testonly",
        }[row.split]
        for offset in range(2):
            prefix = "One Star\n" if offset == 0 else ""
            rows.append(
                {
                    "duplicate_key": f"{row.parent_asin}-{offset}",
                    "parent_asin": row.parent_asin,
                    "device_type": row.device_type,
                    "review_datetime": pd.Timestamp(month) + pd.Timedelta(days=offset),
                    "review_month": month,
                    "review_text": f"{prefix}{split_word} device text",
                }
            )
    return pd.DataFrame(rows)


def test_project_root_and_paths_are_portable() -> None:
    assert w6d.project_root() == ROOT
    config = rules()
    for section in ["inputs", "outputs"]:
        for value in config[section].values():
            if isinstance(value, str) and ("/" in value or "\\" in value):
                assert not Path(value).is_absolute()


def test_frozen_split_boundaries_and_embargo() -> None:
    config = rules()
    assert w6d.split_for_month(pd.Period("2019-08", freq="M"), config) == "train"
    assert w6d.split_for_month(pd.Period("2019-09", freq="M"), config) == "embargo_train_validation"
    assert w6d.split_for_month(pd.Period("2019-11", freq="M"), config) == "embargo_train_validation"
    assert w6d.split_for_month(pd.Period("2019-12", freq="M"), config) == "validation"
    assert w6d.split_for_month(pd.Period("2021-08", freq="M"), config) == "embargo_validation_test"
    assert w6d.split_for_month(pd.Period("2021-10", freq="M"), config) == "embargo_validation_test"
    assert w6d.split_for_month(pd.Period("2021-11", freq="M"), config) == "test"


def test_real_h3_keys_match_frozen_counts_and_exclude_switch() -> None:
    config = rules()
    panel = pq.read_table(ROOT / config["inputs"]["analysis_panel"]).to_pandas()
    keys = w6d.build_modeling_keys(panel, config)
    h3 = keys.loc[(keys["horizon"] == 3) & keys["split"].isin(["train", "validation", "test"])]
    assert h3["split"].value_counts().to_dict() == {
        "train": 205,
        "validation": 150,
        "test": 115,
    }
    assert set(h3["target"].unique()) == {0, 1}
    assert "smart_switch" not in set(h3["device_type"])


def test_tfidf_fits_train_only_and_aggregates_review_vectors() -> None:
    samples = sample_rows()
    reviews = review_rows(samples)
    matrix, vectorizer, info = w6d.build_text_matrix(samples, reviews, rules())
    assert matrix.shape[0] == len(samples)
    assert info["audit"]["train_reviews_used_to_fit_vocabulary"] == 4
    assert info["audit"]["validation_reviews_used_to_fit_vocabulary"] == 0
    assert info["audit"]["test_reviews_used_to_fit_vocabulary"] == 0
    assert "validationonly" not in vectorizer.vocabulary_
    assert "testonly" not in vectorizer.vocabulary_
    assert info["audit"]["model_text_preprocessing"]["affected_reviews"] == 6
    assert info["audit"]["model_text_preprocessing"]["formal_review_text_modified"] is False
    assert info["audit"]["duplicate_key_overlap_counts"] == {
        "train_validation": 0,
        "train_test": 0,
        "validation_test": 0,
    }


def test_star_header_removal_does_not_remove_normal_negation() -> None:
    text = "One Star\nNot working after reset"
    cleaned, count = w6d.STAR_HEADER_RE.subn("", text, count=1)
    assert count == 1
    assert cleaned == "Not working after reset"
    untouched, count = w6d.STAR_HEADER_RE.subn("", "Not working", count=1)
    assert count == 0
    assert untouched == "Not working"


def test_route_contracts_are_distinct_and_exclude_rating_from_core() -> None:
    config = rules()
    assert config["routes"]["core"] == [
        "text_only",
        "text_plus_sentiment",
        "text_plus_engineering",
    ]
    assert config["routes"]["text_only_numeric_features"] == []
    sentiment = set(config["routes"]["sentiment_features"])
    engineering = set(config["routes"]["engineering_features"])
    rating = set(config["routes"]["rating_reference_features"])
    assert sentiment.isdisjoint(engineering)
    assert rating.isdisjoint(sentiment | engineering)
    assert config["routes"]["rating_reference_status"] == "additional_transparent_reference"


def test_numeric_scaler_is_fit_on_train_only() -> None:
    samples = sample_rows()
    feature = ["feature_mean_sentiment_compound"]
    matrix, scaler = w6d.combine_features(None, samples, feature)
    assert scaler is not None
    train_values = samples.loc[samples["split"] == "train", feature].to_numpy()
    assert np.allclose(scaler.mean_, train_values.mean(axis=0))
    assert matrix.shape == (len(samples), 1)


def test_same_text_matrix_is_reused_and_numeric_features_only_append() -> None:
    samples = sample_rows()
    text_matrix = sparse.csr_matrix(np.arange(len(samples) * 3).reshape(len(samples), 3))
    text_only, text_scaler = w6d.combine_features(text_matrix, samples, [])
    sentiment, sentiment_scaler = w6d.combine_features(
        text_matrix, samples, rules()["routes"]["sentiment_features"]
    )
    engineering, engineering_scaler = w6d.combine_features(
        text_matrix, samples, rules()["routes"]["engineering_features"]
    )
    assert text_scaler is None
    assert text_only is text_matrix
    assert sentiment.shape[1] == text_matrix.shape[1] + 2
    assert engineering.shape[1] == text_matrix.shape[1] + 3
    assert sentiment_scaler is not None and engineering_scaler is not None
    assert np.array_equal(sentiment[:, :3].toarray(), text_matrix.toarray())
    assert np.array_equal(engineering[:, :3].toarray(), text_matrix.toarray())


def test_logistic_and_svm_use_fixed_thresholds_without_svm_probabilities() -> None:
    samples = sample_rows()
    matrix = sparse.csr_matrix(
        np.array(
            [
                [1, 0], [0, 1], [1, 0.2], [0.2, 1], [1, 0.1], [0.1, 1]
            ],
            dtype=float,
        )
    )
    logistic, logistic_pred, logistic_metrics = w6d.fit_and_predict(
        matrix, samples, "logistic_regression", rules()
    )
    svm, svm_pred, svm_metrics = w6d.fit_and_predict(matrix, samples, "linear_svm", rules())
    assert logistic.get_params()["C"] == 1.0
    assert svm.get_params()["C"] == 1.0
    assert np.isfinite(logistic_pred["probability"]).all()
    assert svm_pred["probability"].isna().all()
    assert all(row.get("calibration") is None for row in svm_metrics if row.get("n"))
    assert all(row.get("brier_score") is None for row in svm_metrics if row.get("n"))
    assert any(row.get("calibration") is not None for row in logistic_metrics)


def test_calibration_curve_has_complete_bins_and_valid_ece() -> None:
    result = w6d.calibration_summary(
        np.array([0, 0, 1, 1]), np.array([0.1, 0.4, 0.6, 0.9]), 4
    )
    assert len(result["bins"]) == 4
    assert sum(row["n"] for row in result["bins"]) == 4
    assert 0 <= result["expected_calibration_error"] <= 1


def test_formal_input_hashes_match_frozen_manifest() -> None:
    config = rules()
    for name in [
        "formal_reviews",
        "review_signal_components",
        "product_month_engineering",
        "quality_targets",
        "analysis_panel",
    ]:
        path = ROOT / config["inputs"][name]
        assert w6d.sha256_file(path) == config["inputs"][f"{name}_sha256"]
        assert pq.ParquetFile(path).metadata.num_rows == config["inputs"][f"{name}_rows"]


def test_runtime_forbids_bert_online_and_raw_reads() -> None:
    config = rules()
    assert config["runtime"]["bert_allowed"] is False
    assert config["runtime"]["online_api_allowed"] is False
    assert config["runtime"]["raw_reads_allowed"] is False
    assert all("raw/" not in str(value) for value in config["inputs"].values())
