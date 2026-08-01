from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "prepare_w5a_annotation_and_baselines.py"
SPEC = importlib.util.spec_from_file_location("prepare_w5a", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def keyword_rules():
    with (ROOT / "config" / "failure_keyword_rules_w5a.toml").open("rb") as handle:
        return MODULE.KeywordRules(tomllib.load(handle))


class W5ATests(unittest.TestCase):
    def test_project_root_resolves_from_script(self):
        self.assertEqual(MODULE.project_root(), ROOT)

    def test_keyword_rule_preserves_failure_negation(self):
        hit, codes = keyword_rules().classify(
            "The switch does not work with Alexa and keeps disconnecting from Wi-Fi."
        )
        self.assertTrue(hit)
        self.assertIn("F2", codes)
        self.assertIn("F5", codes)

    def test_keyword_rule_detects_safety_failure(self):
        hit, codes = keyword_rules().classify(
            "The smart plug overheated, started smoking, and melted."
        )
        self.assertTrue(hit)
        self.assertIn("F7", codes)

    def test_keyword_rule_does_not_treat_shipping_as_failure(self):
        hit, codes = keyword_rules().classify(
            "Fast shipping, a nice package, and a fair price."
        )
        self.assertFalse(hit)
        self.assertEqual(codes, ())

    def test_rating_strata(self):
        self.assertEqual(MODULE.rating_stratum(1), "low_1_2")
        self.assertEqual(MODULE.rating_stratum(2), "low_1_2")
        self.assertEqual(MODULE.rating_stratum(3), "middle_3")
        self.assertEqual(MODULE.rating_stratum(4), "high_4_5")
        self.assertEqual(MODULE.rating_stratum(5), "high_4_5")

    def test_time_strata(self):
        self.assertEqual(
            MODULE.time_stratum("2015-01-01T00:00:00Z"), "early_2011_2017"
        )
        self.assertEqual(
            MODULE.time_stratum("2019-01-01T00:00:00Z"), "middle_2018_2020"
        )
        self.assertEqual(
            MODULE.time_stratum("2022-01-01T00:00:00Z"), "recent_2021_2023"
        )

    def test_balanced_selection_is_deterministic_and_unique(self):
        frame = pd.DataFrame(
            [
                {
                    "duplicate_key": f"k{index}",
                    "parent_asin": f"p{index % 4}",
                    "keyword_candidate_hit": index % 2 == 0,
                    "rating_stratum": [
                        "low_1_2",
                        "middle_3",
                        "high_4_5",
                    ][index % 3],
                    "time_stratum": [
                        "early_2011_2017",
                        "middle_2018_2020",
                        "recent_2021_2023",
                    ][index % 3],
                }
                for index in range(20)
            ]
        )
        first = MODULE.balanced_select(frame, 10, seed=20260731, purpose="test")
        second = MODULE.balanced_select(frame, 10, seed=20260731, purpose="test")
        self.assertEqual(
            first["duplicate_key"].tolist(), second["duplicate_key"].tolist()
        )
        self.assertTrue(first["duplicate_key"].is_unique)
        self.assertEqual(first["parent_asin"].nunique(), 4)

    def test_blind_schema_rejects_rating_and_keyword_fields(self):
        bad = pd.DataFrame(columns=MODULE.MAIN_BLIND_COLUMNS + ["rating"])
        with self.assertRaises(MODULE.W5AError):
            MODULE.validate_blind_columns(bad, MODULE.MAIN_BLIND_COLUMNS)

    def test_human_label_columns_are_empty(self):
        frame = pd.DataFrame(
            [
                {
                    column: (
                        "W5A-001"
                        if column == "blind_review_id"
                        else "smart_plug"
                        if column == "device_type"
                        else "Review text"
                        if column == "review_text"
                        else ""
                    )
                    for column in MODULE.MAIN_BLIND_COLUMNS
                }
            ]
        )
        MODULE.validate_blind_columns(frame, MODULE.MAIN_BLIND_COLUMNS)
        frame.loc[0, "reviewer_1_failure_binary"] = "1"
        with self.assertRaises(MODULE.W5AError):
            MODULE.validate_blind_columns(frame, MODULE.MAIN_BLIND_COLUMNS)

    def test_private_mapping_schema_excludes_user_identifiers(self):
        approved = {
            "blind_review_id",
            "duplicate_key",
            "parent_asin",
            "rating",
            "review_datetime",
            "source_domain",
            "keyword_candidate_hit",
            "sampling_stratum",
        }
        self.assertNotIn("user_id", approved)
        self.assertNotIn("user_id_hash", approved)

    def test_descriptive_outputs_forbid_future_targets_and_labels(self):
        self.assertIn("future_target", MODULE.FORBIDDEN_MODEL_COLUMNS)
        self.assertIn("split", MODULE.FORBIDDEN_MODEL_COLUMNS)
        self.assertIn("failure_binary", MODULE.FORBIDDEN_MODEL_COLUMNS)
        self.assertIn("severity", MODULE.FORBIDDEN_MODEL_COLUMNS)
        self.assertIn("persistence", MODULE.FORBIDDEN_MODEL_COLUMNS)

    def test_no_machine_learning_training_imports(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "LogisticRegression(",
            "TfidfVectorizer(",
            "torch.",
            "transformers.",
            ".fit(",
            ".fit_transform(",
        )
        self.assertTrue(all(token not in source for token in forbidden))


if __name__ == "__main__":
    unittest.main()
