from __future__ import annotations

import hashlib
import tomllib
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from scripts import extract_and_clean_reviews as w4
from scripts import extract_and_clean_reviews_w4r as w4r


ROOT = Path(__file__).resolve().parents[1]


class W4RIncrementalTests(unittest.TestCase):
    def test_frozen_cleaning_sections_match_w4(self) -> None:
        with (ROOT / "config/review_cleaning_rules.toml").open("rb") as handle:
            base = tomllib.load(handle)
        with (ROOT / "config/review_cleaning_rules_w4r.toml").open("rb") as handle:
            incremental = tomllib.load(handle)
        w4r.check_frozen_rule_sections(base, incremental)

    def test_product_difference_is_exactly_19(self) -> None:
        baseline = pq.read_table(
            ROOT / "data/amazon_reviews_2023/processed/target_products.parquet",
            columns=["parent_asin", "device_type"],
        ).to_pylist()
        formal = pq.read_table(
            ROOT
            / "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
            columns=["parent_asin", "device_type"],
        ).to_pylist()
        baseline_parents = {row["parent_asin"] for row in baseline}
        additions = [
            row for row in formal if row["parent_asin"] not in baseline_parents
        ]
        self.assertEqual(len(additions), 19)
        self.assertEqual(
            sum(row["device_type"] == "smart_bulb" for row in additions), 17
        )
        self.assertEqual(
            sum(row["device_type"] == "smart_switch" for row in additions), 2
        )

    def test_w4_review_schema_and_forbidden_fields(self) -> None:
        schema = pq.ParquetFile(
            ROOT / "data/amazon_reviews_2023/processed/review_level_base.parquet"
        ).schema_arrow
        self.assertEqual(schema.names, w4.FINAL_FIELDS)
        self.assertFalse(w4r.FORBIDDEN_FIELDS & set(schema.names))

    def test_text_timestamp_hash_and_duplicate_rules_remain_deterministic(self) -> None:
        cleaned, _ = w4.clean_text("<p>not   working</p>")
        self.assertEqual(cleaned, "not working")
        timestamp = w4.timestamp_fields(1_700_000_000_000)
        self.assertIsNotNone(timestamp)
        salt = bytes(range(32))
        user_hash_1 = w4.hash_user_id(salt, "test-user")
        user_hash_2 = w4.hash_user_id(salt, "test-user")
        self.assertEqual(user_hash_1, user_hash_2)
        key_1 = w4.make_duplicate_key(
            user_hash_1, "PARENT", 1_700_000_000_000, 2.0, "not working"
        )
        key_2 = w4.make_duplicate_key(
            user_hash_2, "PARENT", 1_700_000_000_000, 2.0, "not working"
        )
        self.assertEqual(key_1, key_2)
        self.assertEqual(len(key_1), hashlib.sha256().digest_size * 2)


if __name__ == "__main__":
    unittest.main()
