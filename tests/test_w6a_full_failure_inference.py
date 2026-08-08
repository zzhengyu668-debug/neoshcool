from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "run_w6a_full_failure_inference.py"
SPEC = importlib.util.spec_from_file_location("run_w6a", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def config():
    with (ROOT / "config/w6a_full_inference_rules.toml").open("rb") as handle:
        return tomllib.load(handle)


class FakeVectorizer:
    def __init__(self):
        self.vocabulary_ = {"work": 0}
        self.fit_called = False

    def fit(self, *_args, **_kwargs):
        self.fit_called = True
        raise AssertionError("fit must not be called")

    def transform(self, texts):
        return np.asarray([[len(text)] for text in texts], dtype=float)


class FakeClassifier:
    def __init__(self):
        self.classes_ = np.array([0, 1])
        self.coef_ = np.array([[1.0]])
        self.fit_called = False

    def fit(self, *_args, **_kwargs):
        self.fit_called = True
        raise AssertionError("fit must not be called")

    def predict_proba(self, matrix):
        p = np.where(matrix[:, 0] >= 5, 0.75, 0.25)
        return np.column_stack([1.0 - p, p])


class W6ATests(unittest.TestCase):
    def test_project_root_is_resolved_from_script(self):
        self.assertEqual(MODULE.project_root(), ROOT)

    def test_config_uses_relative_paths_and_fixed_threshold(self):
        cfg = config()
        for value in cfg["inputs"].values():
            if isinstance(value, str) and ("/" in value or "\\" in value):
                self.assertFalse(Path(value).is_absolute())
        self.assertEqual(cfg["model"]["decision_threshold"], 0.5)
        self.assertFalse(cfg["model"]["allow_refit"])
        self.assertFalse(cfg["model"]["allow_retraining"])

    def test_model_text_removes_only_leading_standalone_star_header(self):
        text, count = MODULE.preprocess_model_text("One Star\nStopped working")
        self.assertEqual(text, "Stopped working")
        self.assertEqual(count, 1)
        untouched, count = MODULE.preprocess_model_text(
            "I gave it one star because it stopped working"
        )
        self.assertEqual(untouched, "I gave it one star because it stopped working")
        self.assertEqual(count, 0)

    def test_score_uses_transform_only_and_fixed_threshold(self):
        vectorizer = FakeVectorizer()
        classifier = FakeClassifier()
        probabilities, predictions = MODULE.score_texts(
            ["bad", "stopped"],
            {"vectorizer": vectorizer, "classifier": classifier},
            0.5,
        )
        self.assertFalse(vectorizer.fit_called)
        self.assertFalse(classifier.fit_called)
        np.testing.assert_allclose(probabilities, [0.25, 0.75])
        np.testing.assert_array_equal(predictions, [0, 1])

    def test_reproduction_comparison_detects_exact_and_inexact_results(self):
        good = MODULE.reproduction_comparison(
            [0, 1], [0.2, 0.8], [0, 1], [0.2, 0.8], 1e-12, 1e-12
        )
        self.assertTrue(good["passed"])
        bad = MODULE.reproduction_comparison(
            [0, 1], [0.2, 0.8], [1, 1], [0.2, 0.7], 1e-12, 1e-12
        )
        self.assertFalse(bad["passed"])
        self.assertEqual(bad["prediction_mismatch_count"], 1)
        self.assertEqual(bad["probability_mismatch_count"], 1)

    def test_fingerprint_is_stable_and_configuration_sensitive(self):
        first = MODULE.stable_fingerprint({"model": "abc", "batch": 10})
        reordered = MODULE.stable_fingerprint({"batch": 10, "model": "abc"})
        changed = MODULE.stable_fingerprint({"model": "abc", "batch": 11})
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_review_output_schema_contains_no_forbidden_fields(self):
        self.assertEqual(MODULE.REVIEW_SCHEMA.names, MODULE.REVIEW_OUTPUT_COLUMNS)
        self.assertFalse(
            set(MODULE.REVIEW_SCHEMA.names) & MODULE.FORBIDDEN_REVIEW_OUTPUT_FIELDS
        )
        for forbidden in ("review_text", "user_id_hash", "severity", "persistence"):
            self.assertNotIn(forbidden, MODULE.REVIEW_SCHEMA.names)

    def test_product_month_aggregation_matches_review_totals(self):
        frame = pd.DataFrame(
            {
                "duplicate_key": ["a", "b", "c"],
                "parent_asin": ["p1", "p1", "p2"],
                "review_month": [
                    pd.Timestamp("2023-01-01").date(),
                    pd.Timestamp("2023-01-01").date(),
                    pd.Timestamp("2023-02-01").date(),
                ],
                "device_type": ["smart_plug", "smart_plug", "smart_bulb"],
                "analysis_role": ["primary", "primary", "exploratory"],
                "model_version": ["m", "m", "m"],
                "product_filter_version": ["v", "v", "v"],
                "failure_prediction": [0, 1, 1],
                "failure_probability": [0.2, 0.8, 0.9],
            }
        )
        result = MODULE.aggregate_product_month(frame)
        validation = MODULE.validate_product_month(result, frame)
        self.assertEqual(len(result), 2)
        self.assertEqual(validation["n_reviews_sum"], 3)
        p1 = result.loc[result["parent_asin"] == "p1"].iloc[0]
        self.assertEqual(p1["predicted_failure_count"], 1)
        self.assertAlmostEqual(p1["predicted_failure_share"], 0.5)
        self.assertAlmostEqual(p1["mean_failure_probability"], 0.5)

    def test_coverage_thresholds_do_not_filter_rows(self):
        signals = pd.DataFrame(
            {
                "device_type": ["smart_plug", "smart_bulb", "smart_switch"],
                "n_reviews": [1, 10, 30],
            }
        )
        rows = MODULE.coverage_rows(signals, [1, 5, 10, 20, 30])
        overall = [row for row in rows if row["scope"] == "all"]
        self.assertEqual([row["product_month_count"] for row in overall], [3, 2, 2, 1, 1])
        self.assertTrue(all(row["diagnostic_only_not_professor_requirement"] for row in rows))

    def test_probability_bins_cover_boundary_values_once(self):
        rows = MODULE.probability_distribution_rows(
            pd.Series([0.0, 0.5, 1.0]), [0.0, 0.5, 1.0]
        )
        self.assertEqual(sum(row["count"] for row in rows), 3)
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(rows[1]["count"], 2)

    def test_analysis_roles_are_non_equivalent(self):
        roles = config()["analysis_roles"]
        self.assertEqual(roles["smart_plug"], "primary")
        self.assertEqual(roles["smart_bulb"], "exploratory")
        self.assertEqual(roles["smart_switch"], "case_study")

    def test_product_month_schema_is_preliminary_only(self):
        self.assertEqual(MODULE.PRODUCT_MONTH_SCHEMA.names, MODULE.PRODUCT_MONTH_COLUMNS)
        for forbidden in (
            "severity",
            "persistence",
            "sentiment",
            "engineering_index",
            "future_quality_target",
        ):
            self.assertNotIn(forbidden, MODULE.PRODUCT_MONTH_SCHEMA.names)


if __name__ == "__main__":
    unittest.main()
