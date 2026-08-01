from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "run_w5b_annotation_and_baselines.py"
SPEC = importlib.util.spec_from_file_location("run_w5b", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def load_config():
    with (ROOT / "config" / "w5b_baseline_rules.toml").open("rb") as handle:
        return tomllib.load(handle)


def load_actual_inputs():
    config = load_config()
    main = MODULE.read_xlsx_table(
        ROOT / config["inputs"]["adjudicated_workbook"]
    )
    reviewer_2 = MODULE.read_xlsx_table(
        ROOT / config["inputs"]["reviewer_2_workbook"]
    )
    blind_key = MODULE.pq.read_table(
        ROOT / config["inputs"]["blind_review_key"]
    ).to_pandas()
    sampling = MODULE.pq.read_table(
        ROOT / config["inputs"]["annotation_sampling_frame"]
    ).to_pandas()
    return main, reviewer_2, blind_key, sampling


class W5BTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main, reviewer_2, blind_key, sampling = load_actual_inputs()
        cls.main = main
        cls.reviewer_2 = reviewer_2
        cls.blind_key = blind_key
        cls.sampling = sampling
        (
            cls.labels,
            cls.modeling,
            cls.workbook_validation,
        ) = MODULE.build_final_labels(main, reviewer_2, blind_key, sampling)

    def test_project_root_resolves_from_script(self):
        self.assertEqual(MODULE.project_root(), ROOT)

    def test_workbook_hashes_match_approved_values(self):
        config = load_config()
        self.assertEqual(
            MODULE.sha256_file(ROOT / config["inputs"]["adjudicated_workbook"]),
            config["inputs"]["adjudicated_workbook_sha256"],
        )
        self.assertEqual(
            MODULE.sha256_file(ROOT / config["inputs"]["reviewer_2_workbook"]),
            config["inputs"]["reviewer_2_workbook_sha256"],
        )

    def test_double_rows_use_adjudication_and_single_rows_use_reviewer1(self):
        counts = self.labels["annotation_source"].value_counts().to_dict()
        self.assertEqual(counts[MODULE.DOUBLE_SOURCE], 60)
        self.assertEqual(counts[MODULE.SINGLE_SOURCE], 240)
        double_ids = set(self.reviewer_2["blind_review_id"])
        double_labels = self.labels.loc[
            self.labels["blind_review_id"].isin(double_ids)
        ]
        single_labels = self.labels.loc[
            ~self.labels["blind_review_id"].isin(double_ids)
        ]
        self.assertTrue(
            (double_labels["annotation_source"] == MODULE.DOUBLE_SOURCE).all()
        )
        self.assertTrue(
            (single_labels["annotation_source"] == MODULE.SINGLE_SOURCE).all()
        )

    def test_uncertain_is_preserved_and_excluded_from_model_split(self):
        uncertain = self.labels.loc[self.labels["label_status"] == "uncertain"]
        self.assertEqual(len(uncertain), 4)
        self.assertTrue(
            (uncertain["final_failure_binary"] == "uncertain").all()
        )
        definite, _ = MODULE.chronological_split(self.modeling)
        self.assertEqual(len(definite), 296)
        self.assertFalse(
            definite["final_failure_binary"].eq("uncertain").any()
        )

    def test_final_label_combinations_are_legal(self):
        for row in self.labels.itertuples(index=False):
            MODULE.validate_label_combination(
                row.final_failure_binary,
                "" if pd.isna(row.final_failure_type) else row.final_failure_type,
                None if pd.isna(row.final_severity) else int(row.final_severity),
                (
                    None
                    if pd.isna(row.final_persistence)
                    else int(row.final_persistence)
                ),
                row.blind_review_id,
            )

    def test_blind_review_and_duplicate_keys_are_one_to_one(self):
        self.assertTrue(self.labels["blind_review_id"].is_unique)
        self.assertTrue(self.labels["duplicate_key"].is_unique)
        self.assertEqual(len(self.labels), 300)

    def test_time_split_is_strictly_chronological_and_disjoint(self):
        definite, manifest = MODULE.chronological_split(self.modeling)
        self.assertTrue(manifest["chronological_order_validated"])
        self.assertEqual(manifest["duplicate_key_cross_split_count"], 0)
        train_max = definite.loc[
            definite["split"] == "train", "review_datetime"
        ].max()
        validation_min = definite.loc[
            definite["split"] == "validation", "review_datetime"
        ].min()
        validation_max = definite.loc[
            definite["split"] == "validation", "review_datetime"
        ].max()
        test_min = definite.loc[
            definite["split"] == "test", "review_datetime"
        ].min()
        self.assertLessEqual(train_max, validation_min)
        self.assertLessEqual(validation_max, test_min)
        for left, right in (
            ("train", "validation"),
            ("train", "test"),
            ("validation", "test"),
        ):
            left_keys = set(
                definite.loc[definite["split"] == left, "duplicate_key"]
            )
            right_keys = set(
                definite.loc[definite["split"] == right, "duplicate_key"]
            )
            self.assertFalse(left_keys.intersection(right_keys))

    def test_tfidf_vocabulary_is_fit_on_train_only(self):
        rows = []
        dates = pd.date_range("2020-01-01", periods=30, freq="D", tz="UTC")
        for index, date in enumerate(dates):
            split = (
                "train"
                if index < 18
                else "validation"
                if index < 24
                else "test"
            )
            label = index % 2
            token = (
                "traincommon failureword"
                if split == "train" and label == 1
                else "traincommon normalword"
                if split == "train"
                else "validationonlytoken failureword"
                if split == "validation" and label == 1
                else "validationonlytoken normalword"
                if split == "validation"
                else "testonlytoken failureword"
                if label == 1
                else "testonlytoken normalword"
            )
            rows.append(
                {
                    "blind_review_id": f"T-{index:03d}",
                    "duplicate_key": f"K-{index:03d}",
                    "parent_asin": f"P-{index % 5}",
                    "device_type": "smart_plug",
                    "review_datetime": date,
                    "final_failure_binary": str(label),
                    "final_failure_type": "F1" if label else "N0",
                    "final_severity": 1 if label else 0,
                    "final_persistence": 0,
                    "annotation_source": MODULE.SINGLE_SOURCE,
                    "label_status": "definite",
                    "annotation_version": MODULE.LABEL_VERSION,
                    "keyword_candidate_hit": bool(label),
                    "review_text": token,
                    "model_text": token,
                    "rating": 1.0 if label else 5.0,
                    "low_star_indicator": label,
                    "split": split,
                }
            )
        synthetic = pd.DataFrame(rows)
        results = MODULE.model_and_evaluate(synthetic, load_config())
        model_bundle = results[-1]["model_bundle"]
        vocabulary = model_bundle["vectorizer"].vocabulary_
        self.assertNotIn("validationonlytoken", vocabulary)
        self.assertNotIn("testonlytoken", vocabulary)
        self.assertIn("traincommon", vocabulary)

    def test_model_feature_rules_exclude_rating_keyword_and_identifiers(self):
        config = load_config()
        self.assertEqual(config["tfidf"]["input_field"], "model_text")
        self.assertTrue(
            {
                "rating",
                "keyword_candidate_hit",
                "device_type",
                "parent_asin",
                "review_datetime",
                "user_id_hash",
            }.issubset(MODULE.MODEL_FORBIDDEN_FEATURES)
        )

    def test_star_header_cleaning_does_not_modify_review_text(self):
        review_text = "Five Stars\n\nThis plug works and does not disconnect."
        model_text, count = MODULE.STAR_HEADER_RE.subn("", review_text, count=1)
        self.assertEqual(count, 1)
        self.assertEqual(
            review_text, "Five Stars\n\nThis plug works and does not disconnect."
        )
        self.assertEqual(model_text, "This plug works and does not disconnect.")
        self.assertIn("does not", model_text)

    def test_binary_metrics_match_manual_small_example(self):
        metrics = MODULE.binary_metrics([0, 0, 1, 1], [0, 1, 0, 1])
        self.assertEqual(metrics["confusion_matrix"]["matrix"], [[1, 1], [1, 1]])
        self.assertAlmostEqual(metrics["accuracy"], 0.5)
        self.assertAlmostEqual(metrics["balanced_accuracy"], 0.5)
        self.assertAlmostEqual(metrics["precision"], 0.5)
        self.assertAlmostEqual(metrics["recall"], 0.5)
        self.assertAlmostEqual(metrics["f1"], 0.5)
        self.assertAlmostEqual(metrics["specificity"], 0.5)

    def test_test_set_is_not_used_for_parameter_selection(self):
        config = load_config()
        self.assertEqual(config["tfidf"]["fit_on"], "train_only")
        self.assertEqual(config["logistic_regression"]["C"], 1.0)
        self.assertEqual(
            config["logistic_regression"]["decision_threshold"], 0.5
        )
        self.assertFalse(config["split"]["allow_random_shuffle"])

    def test_reports_do_not_contain_private_text_or_user_identifiers(self):
        report_dir = ROOT / "data/amazon_reviews_2023/reports/w5b"
        if not report_dir.exists():
            self.skipTest("W5-B reports are not generated yet")
        for path in report_dir.iterdir():
            if path.suffix.lower() in {".json", ".md", ".csv", ".log"}:
                text = path.read_text(
                    encoding="utf-8-sig", errors="replace"
                ).lower()
                self.assertNotIn("user_id_hash", text)
                self.assertNotIn("raw_user_id", text)
        for review_text in self.main["review_text"].head(10):
            if len(str(review_text)) >= 40:
                for path in report_dir.iterdir():
                    if path.suffix.lower() in {".json", ".md", ".csv", ".log"}:
                        text = path.read_text(
                            encoding="utf-8-sig", errors="replace"
                        )
                        self.assertNotIn(str(review_text), text)

    def test_formal_w3_w4_w4r_files_match_approved_hashes(self):
        config = load_config()
        self.assertEqual(
            MODULE.sha256_file(ROOT / config["inputs"]["formal_review_parquet"]),
            config["inputs"]["formal_review_sha256"],
        )
        self.assertEqual(
            MODULE.sha256_file(ROOT / config["inputs"]["formal_product_parquet"]),
            config["inputs"]["formal_product_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
