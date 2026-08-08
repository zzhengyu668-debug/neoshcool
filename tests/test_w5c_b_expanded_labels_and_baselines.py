from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "run_w5c_b_expanded_labels_and_baselines.py"
SPEC = importlib.util.spec_from_file_location("run_w5c_b", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def config():
    with (ROOT / "config/w5c_b_baseline_rules.toml").open("rb") as handle:
        return tomllib.load(handle)


def workbook_group(entries, columns):
    return MODULE.load_workbook_group(ROOT, entries, columns)


class W5CBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = config()
        inputs = cfg["inputs"]
        cls.cfg = cfg
        cls.r1 = workbook_group(
            inputs["reviewer_1_workbooks"], list(MODULE.w5b.MAIN_COLUMNS)
        )
        cls.r2 = workbook_group(
            inputs["reviewer_2_workbooks"], list(MODULE.w5b.REVIEWER_2_COLUMNS)
        )
        cls.adjudication = MODULE.w5b.read_xlsx_table(
            ROOT / inputs["completed_adjudication"], "Adjudication"
        )
        cls.old_main = MODULE.w5b.read_xlsx_table(
            ROOT / inputs["old_adjudicated_workbook"]
        )
        cls.old_r2 = MODULE.w5b.read_xlsx_table(
            ROOT / inputs["old_reviewer_2_workbook"]
        )
        cls.old_labels = pq.read_table(ROOT / inputs["old_labels"]).to_pandas()
        old_modeling = pq.read_table(
            ROOT / inputs["old_modeling_dataset"]
        ).to_pandas()
        old_sampling = pq.read_table(
            ROOT / inputs["old_sampling_frame"]
        ).to_pandas()
        new_sampling = pq.read_table(
            ROOT / inputs["new_sampling_frame"]
        ).to_pandas()
        new_key = pq.read_table(ROOT / inputs["new_blind_key"]).to_pandas()
        cls.new_sampling = new_sampling
        cls.validation = MODULE.validate_new_annotation_inputs(
            cls.r1, cls.r2, cls.adjudication, new_sampling
        )
        cls.labels, cls.modeling, cls.merge = MODULE.build_expanded_labels(
            cls.old_labels,
            old_modeling,
            old_sampling,
            cls.r1,
            cls.adjudication,
            new_sampling,
            new_key,
        )

    def test_project_root_is_resolved_from_script(self):
        self.assertEqual(MODULE.project_root(), ROOT)

    def test_approved_adjudication_hash(self):
        inputs = self.cfg["inputs"]
        self.assertEqual(
            MODULE.w5b.sha256_file(ROOT / inputs["completed_adjudication"]),
            inputs["completed_adjudication_sha256"],
        )

    def test_original_300_label_decisions_are_preserved(self):
        expanded = self.labels.set_index("blind_review_id")
        for old in self.old_labels.itertuples(index=False):
            for field in (
                "final_failure_binary",
                "final_failure_type",
                "final_severity",
                "final_persistence",
                "label_status",
            ):
                self.assertEqual(
                    MODULE.canonical_cell(getattr(old, field)),
                    MODULE.canonical_cell(expanded.at[old.blind_review_id, field]),
                )

    def test_new_label_priority_and_source_counts(self):
        counts = self.labels["annotation_source"].value_counts().to_dict()
        self.assertEqual(counts[MODULE.OLD_SOURCE], 300)
        self.assertEqual(counts[MODULE.NEW_DOUBLE_SOURCE], 240)
        self.assertEqual(counts[MODULE.NEW_SINGLE_SOURCE], 960)
        double_ids = set(self.adjudication["blind_review_id"])
        actual = set(
            self.labels.loc[
                self.labels["annotation_source"] == MODULE.NEW_DOUBLE_SOURCE,
                "blind_review_id",
            ]
        )
        self.assertEqual(actual, double_ids)

    def test_expanded_identifiers_and_device_quotas(self):
        self.assertEqual(len(self.labels), 1500)
        self.assertTrue(self.labels["blind_review_id"].is_unique)
        self.assertTrue(self.labels["duplicate_key"].is_unique)
        self.assertEqual(
            self.labels["device_type"].value_counts().to_dict(),
            {"smart_plug": 1137, "smart_bulb": 300, "smart_switch": 63},
        )

    def test_old_and_new_samples_do_not_overlap(self):
        old_keys = set(self.old_labels["duplicate_key"])
        new_keys = set(self.new_sampling["duplicate_key"])
        self.assertFalse(old_keys.intersection(new_keys))

    def test_all_label_combinations_are_legal(self):
        for row in self.labels.itertuples(index=False):
            MODULE.validate_label(
                row.final_failure_binary,
                row.final_failure_type,
                row.final_severity,
                row.final_persistence,
                row.blind_review_id,
            )

    def test_uncertain_is_retained_but_excluded_from_split(self):
        uncertain = self.labels.loc[self.labels["label_status"] == "uncertain"]
        self.assertGreaterEqual(len(uncertain), 15)
        definite, manifest = MODULE.w5b.chronological_split(self.modeling)
        self.assertEqual(len(definite) + len(uncertain), 1500)
        self.assertFalse(definite["final_failure_binary"].eq("uncertain").any())
        self.assertEqual(manifest["uncertain_rows_excluded"], len(uncertain))

    def test_time_split_is_chronological_and_disjoint(self):
        definite, manifest = MODULE.w5b.chronological_split(self.modeling)
        self.assertTrue(manifest["chronological_order_validated"])
        self.assertEqual(manifest["duplicate_key_cross_split_count"], 0)
        train = definite.loc[definite["split"] == "train"]
        validation = definite.loc[definite["split"] == "validation"]
        test = definite.loc[definite["split"] == "test"]
        self.assertLessEqual(train["review_datetime"].max(), validation["review_datetime"].min())
        self.assertLessEqual(validation["review_datetime"].max(), test["review_datetime"].min())
        self.assertFalse(set(train["duplicate_key"]) & set(validation["duplicate_key"]))
        self.assertFalse(set(train["duplicate_key"]) & set(test["duplicate_key"]))
        self.assertFalse(set(validation["duplicate_key"]) & set(test["duplicate_key"]))

    def test_agreement_uses_300_independent_pre_adjudication_rows(self):
        bundle = MODULE.agreement_bundle(
            self.old_main, self.old_r2, self.adjudication
        )
        self.assertEqual(bundle["w5a_original_60"]["double_review_rows"], 60)
        self.assertEqual(bundle["w5c_a_new_240"]["double_review_rows"], 240)
        self.assertEqual(bundle["combined_300"]["double_review_rows"], 300)
        self.assertTrue(
            bundle["combined_300"][
                "agreement_uses_pre_adjudication_independent_labels"
            ]
        )

    def test_tfidf_configuration_excludes_forbidden_features(self):
        self.assertEqual(self.cfg["tfidf"]["input_field"], "model_text")
        self.assertEqual(self.cfg["tfidf"]["fit_on"], "train_only")
        self.assertFalse(self.cfg["split"]["allow_random_shuffle"])
        self.assertTrue(
            {
                "rating",
                "keyword_candidate_hit",
                "device_type",
                "parent_asin",
                "review_datetime",
                "user_id_hash",
            }.issubset(MODULE.w5b.MODEL_FORBIDDEN_FEATURES)
        )

    def test_star_header_cleaning_preserves_negation_and_original(self):
        original = "Five Stars\nThis switch does not disconnect."
        model_text, count = MODULE.w5b.STAR_HEADER_RE.subn("", original, count=1)
        self.assertEqual(count, 1)
        self.assertEqual(original, "Five Stars\nThis switch does not disconnect.")
        self.assertIn("does not", model_text)

    def test_formal_and_w5b_inputs_match_approved_hashes(self):
        inputs = self.cfg["inputs"]
        for path_key, hash_key in (
            ("formal_review_parquet", "formal_review_sha256"),
            ("old_labels", "old_labels_sha256"),
            ("old_modeling_dataset", "old_modeling_sha256"),
        ):
            self.assertEqual(
                MODULE.w5b.sha256_file(ROOT / inputs[path_key]), inputs[hash_key]
            )

    def test_generated_reports_do_not_leak_text_or_identifiers(self):
        report_dir = ROOT / "data/amazon_reviews_2023/reports/w5c_b"
        if not (report_dir / "w5c_b_status.json").is_file():
            self.skipTest("W5-C-B reports not generated yet")
        combined = "\n".join(
            path.read_text(encoding="utf-8-sig", errors="replace")
            for path in report_dir.iterdir()
            if path.suffix.lower() in {".json", ".md", ".csv", ".log"}
        )
        lowered = combined.lower()
        self.assertNotIn("user_id_hash", lowered)
        self.assertNotIn("raw_user_id", lowered)
        for value in self.modeling["review_text"].head(25):
            text = str(value)
            if len(text) >= 40:
                self.assertNotIn(text, combined)


if __name__ == "__main__":
    unittest.main()
