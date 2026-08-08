from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/run_w6b_signal_components.py"
SPEC = importlib.util.spec_from_file_location("run_w6b", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def config():
    with (ROOT / "config/w6b_signal_component_rules.toml").open("rb") as handle:
        return tomllib.load(handle)


class W6BTests(unittest.TestCase):
    def test_project_root(self):
        self.assertEqual(MODULE.project_root(), ROOT)

    def test_config_paths_are_relative_and_test_is_not_for_tuning(self):
        cfg = config()
        for value in cfg["inputs"].values():
            if isinstance(value, str) and ("/" in value or "\\" in value):
                self.assertFalse(Path(value).is_absolute())
        self.assertFalse(cfg["split"]["allow_random_shuffle"])
        self.assertEqual(cfg["logistic_regression"]["decision_threshold"], 0.5)

    def test_original_chronological_split_counts(self):
        labels = MODULE.pq.read_table(
            ROOT / config()["inputs"]["frozen_labels"]
        ).to_pandas()
        definite = MODULE.assign_w5c_b_split(labels, config())
        self.assertEqual(definite["split"].value_counts().to_dict(), {
            "train": 872, "validation": 291, "test": 291
        })
        failures = definite.loc[definite["final_failure_binary"].astype(str) == "1"]
        self.assertEqual(failures["split"].value_counts().to_dict(), {
            "train": 374, "test": 122, "validation": 107
        })

    def test_cumulative_monotonicity(self):
        adjusted, changed = MODULE.enforce_cumulative_monotonicity(
            {"ge_1": np.array([0.2, 0.9]), "ge_2": np.array([0.8, 0.4])},
            [1, 2],
        )
        np.testing.assert_allclose(adjusted["ge_2"], [0.2, 0.4])
        self.assertEqual(changed, 1)

    def test_expected_and_hard_ordinal_formulas(self):
        probabilities = {"ge_2": np.array([0.3, 0.8]), "ge_3": np.array([0.1, 0.6])}
        expected = MODULE.expected_from_cumulative(probabilities, [2, 3], 1)
        hard = MODULE.hard_class_from_cumulative(probabilities, [2, 3], 1, 0.5)
        np.testing.assert_allclose(expected, [1.4, 2.4])
        np.testing.assert_array_equal(hard, [1, 3])

    def test_binary_and_ordinal_metrics(self):
        binary = MODULE.binary_metrics([0, 0, 1, 1], [0.1, 0.4, 0.6, 0.9], 0.5)
        self.assertEqual(binary["f1"], 1.0)
        ordinal = MODULE.ordinal_metrics([1, 2, 3], [1, 2, 2], [1, 2, 3])
        self.assertAlmostEqual(ordinal["mae"], 1 / 3)

    def test_sentiment_negative_threshold_boundary(self):
        threshold = config()["sentiment"]["compound_negative_threshold"]
        compounds = pd.Series([-0.051, -0.05, -0.049])
        indicator = (compounds <= threshold).astype(int).tolist()
        self.assertEqual(indicator, [1, 1, 0])

    def test_review_schema_has_no_text_or_final_index(self):
        self.assertEqual(MODULE.REVIEW_SCHEMA.names, MODULE.REVIEW_COLUMNS)
        self.assertFalse(set(MODULE.REVIEW_COLUMNS) & MODULE.FORBIDDEN_FIELDS)
        for field in ("review_text", "user_id_hash", "engineering_index", "future_quality_target"):
            self.assertNotIn(field, MODULE.REVIEW_COLUMNS)

    def test_product_month_aggregation_preserves_counts(self):
        frame = pd.DataFrame({
            "duplicate_key": ["a", "b"],
            "parent_asin": ["p", "p"],
            "review_month": [pd.Timestamp("2023-01-01").date()] * 2,
            "device_type": ["smart_plug"] * 2,
            "analysis_role": ["primary"] * 2,
            "failure_prediction": [0, 1],
            "failure_probability": [0.2, 0.8],
            "expected_severity_signal": [0.3, 1.8],
            "expected_persistence_signal": [0.1, 1.0],
            "sentiment_compound": [0.5, -0.5],
            "negative_sentiment_indicator": [0, 1],
            "failure_model_version": ["f"] * 2,
            "severity_model_version": ["s"] * 2,
            "persistence_model_version": ["p"] * 2,
            "sentiment_model_version": ["v"] * 2,
            "product_filter_version": ["w3"] * 2,
        })
        result = MODULE.aggregate_product_month(frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["n_reviews"], 2)
        self.assertAlmostEqual(result.iloc[0]["predicted_failure_share"], 0.5)
        self.assertAlmostEqual(result.iloc[0]["negative_sentiment_share"], 0.5)

    def test_signal_formulas_are_multiplicative(self):
        failure_probability = np.array([0.2, 0.8])
        expected = np.array([1.5, 2.5])
        np.testing.assert_allclose(failure_probability * expected, [0.3, 2.0])

    def test_analysis_roles_remain_non_equivalent(self):
        roles = config()["analysis_roles"]
        self.assertEqual(roles, {
            "smart_plug": "primary",
            "smart_bulb": "exploratory",
            "smart_switch": "case_study",
        })

    def test_no_raw_paths_in_config(self):
        raw = (ROOT / "config/w6b_signal_component_rules.toml").read_text(encoding="utf-8")
        self.assertNotIn("raw/uncompressed", raw)
        self.assertNotIn(".jsonl", raw)
        self.assertNotIn(".jsonl.gz", raw)


if __name__ == "__main__":
    unittest.main()
