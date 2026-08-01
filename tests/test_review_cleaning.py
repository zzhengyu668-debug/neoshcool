from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from datetime import date, timezone
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from lingua import Language


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extract_and_clean_reviews.py"
SPEC = importlib.util.spec_from_file_location("extract_and_clean_reviews", SCRIPT)
assert SPEC and SPEC.loader
w4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w4)


class FakeEnglishDetector:
    def detect_language_of(self, text: str):
        return Language.ENGLISH

    def compute_language_confidence(self, text: str, language: Language) -> float:
        return 0.99


class FakeSpanishDetector:
    def detect_language_of(self, text: str):
        return Language.SPANISH

    def compute_language_confidence(self, text: str, language: Language) -> float:
        return 0.01


class ReviewCleaningTests(unittest.TestCase):
    def test_timestamp_milliseconds_to_utc(self) -> None:
        result = w4.timestamp_fields(1_700_000_000_123)
        self.assertIsNotNone(result)
        milliseconds, converted, month = result
        self.assertEqual(milliseconds, 1_700_000_000_123)
        self.assertEqual(converted.tzinfo, timezone.utc)
        self.assertEqual(month, date(converted.year, converted.month, 1))

    def test_timestamp_rejects_invalid_values(self) -> None:
        for value in (None, True, "1700000000000", -1, 1.5):
            self.assertIsNone(w4.timestamp_fields(value))

    def test_html_unicode_and_whitespace_cleaning(self) -> None:
        text, changes = w4.clean_text("  <b>Ａ smart&nbsp; plug</b>\n works  ")
        self.assertEqual(text, "A smart plug works")
        self.assertTrue(changes["html_changed"])
        self.assertTrue(changes["unicode_changed"])
        self.assertTrue(changes["whitespace_changed"])

    def test_negations_are_preserved(self) -> None:
        text, _ = w4.clean_text("It does not work, never connects, without Wi-Fi.")
        self.assertIn("not", text)
        self.assertIn("never", text)
        self.assertIn("without", text)

    def test_non_string_text_is_empty(self) -> None:
        self.assertEqual(w4.clean_text(None)[0], "")
        self.assertEqual(w4.clean_text(["not", "text"])[0], "")

    def test_dedup_normalization_is_separate_and_casefolded(self) -> None:
        normalized = w4.normalize_for_dedup("  NOT   Working  ", "NFKC", True)
        self.assertEqual(normalized, "not working")

    def test_hmac_user_hash_is_deterministic_and_irreversible_output(self) -> None:
        salt = bytes.fromhex("11" * 32)
        raw = "RAW-USER-123"
        first = w4.hash_user_id(salt, raw)
        second = w4.hash_user_id(salt, raw)
        self.assertEqual(first, second)
        self.assertNotIn(raw, first)
        self.assertEqual(len(first), 64)

    def test_missing_user_hash_is_null(self) -> None:
        salt = bytes.fromhex("22" * 32)
        self.assertIsNone(w4.hash_user_id(salt, None))
        self.assertIsNone(w4.hash_user_id(salt, ""))

    def test_duplicate_key_is_stable_and_text_sensitive(self) -> None:
        first = w4.make_duplicate_key("u", "p", 1, 5.0, "not working")
        second = w4.make_duplicate_key("u", "p", 1, 5.0, "not working")
        changed = w4.make_duplicate_key("u", "p", 1, 5.0, "working")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_short_language_is_undetermined(self) -> None:
        result = w4.classify_language(
            "Good",
            FakeEnglishDetector(),
            minimum_alphabetic_characters=10,
            minimum_total_characters=12,
            minimum_english_confidence=0.70,
        )
        self.assertEqual(result, ("undetermined_short", None, None))

    def test_english_language_rule(self) -> None:
        status, iso_code, confidence = w4.classify_language(
            "This smart plug does not connect to Wi-Fi.",
            FakeEnglishDetector(),
            minimum_alphabetic_characters=10,
            minimum_total_characters=12,
            minimum_english_confidence=0.70,
        )
        self.assertEqual(status, "English")
        self.assertEqual(iso_code, "EN")
        self.assertGreater(confidence, 0.70)

    def test_non_english_language_rule(self) -> None:
        status, iso_code, _ = w4.classify_language(
            "Este enchufe inteligente no funciona correctamente.",
            FakeSpanishDetector(),
            minimum_alphabetic_characters=10,
            minimum_total_characters=12,
            minimum_english_confidence=0.70,
        )
        self.assertEqual(status, "non-English")
        self.assertEqual(iso_code, "ES")

    def test_configuration_fingerprint_changes_with_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            script = base / "script.py"
            config = base / "config.toml"
            script.write_text("print('x')", encoding="utf-8")
            config.write_text("version='1'", encoding="utf-8")
            inputs = {"source": {"size": 1, "mtime": 2}}
            target = {"sha256": "abc"}
            first = w4.configuration_fingerprint(
                script, config, inputs, target, b"a" * 32
            )
            config.write_text("version='2'", encoding="utf-8")
            second = w4.configuration_fingerprint(
                script, config, inputs, target, b"a" * 32
            )
            self.assertNotEqual(first, second)

    def test_source_scan_filters_non_target_and_never_logs_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            reviews = raw / "review_categories" / "tiny.jsonl"
            reports = root / "reports"
            work = root / "work"
            reviews.parent.mkdir(parents=True)
            reports.mkdir()
            work.mkdir()
            records = [
                {
                    "parent_asin": "TARGET",
                    "asin": "A1",
                    "rating": 2.0,
                    "title": "Does not work",
                    "text": "This smart plug never connects to my Wi-Fi network.",
                    "timestamp": 1_700_000_000_123,
                    "verified_purchase": True,
                    "helpful_vote": 1,
                    "user_id": "SECRET-USER-ID",
                },
                {
                    "parent_asin": "OTHER",
                    "asin": "A2",
                    "rating": 5.0,
                    "title": "Other",
                    "text": "This record must not be retained.",
                    "timestamp": 1_700_000_000_124,
                    "verified_purchase": True,
                    "helpful_vote": 0,
                    "user_id": "OTHER-SECRET-ID",
                },
            ]
            with reviews.open("wb") as handle:
                for record in records:
                    handle.write(orjson.dumps(record) + b"\n")
            os.chmod(reviews, stat.S_IREAD)
            source = {
                "id": "tiny",
                "domain": "Electronics",
                "relative_path": "review_categories/tiny.jsonl",
                "expected_records": 2,
                "expected_bytes": reviews.stat().st_size,
            }
            config = {
                "phase": {
                    "minimum_free_gib": 0,
                    "parquet_batch_rows": 2,
                    "parquet_compression": "zstd",
                    "progress_records": 100,
                    "progress_bytes_gib": 1,
                },
                "text": {
                    "unicode_form": "NFKC",
                    "strip_html": True,
                    "collapse_whitespace": True,
                    "review_text_separator": "\n\n",
                    "dedup_casefold": True,
                },
                "language": {
                    "minimum_alphabetic_characters": 10,
                    "minimum_total_characters": 12,
                    "minimum_english_confidence": 0.70,
                },
            }
            targets = {
                "TARGET": {
                    "source_domains": ["Electronics"],
                    "main_category": "Test",
                    "product_title": "Target product",
                    "device_type": "smart_plug",
                    "filter_version": "w3-v1.3.2",
                }
            }
            log_path = reports / "execution.log"
            try:
                stats = w4.scan_source(
                    source,
                    root=root,
                    raw_uncompressed=raw,
                    targets=targets,
                    salt=bytes.fromhex("33" * 32),
                    detector=FakeEnglishDetector(),
                    config=config,
                    fingerprint="f" * 64,
                    work_dir=work,
                    reports_dir=reports,
                    log_path=log_path,
                    disk_events=[],
                )
                self.assertEqual(stats["physical_line_count"], 2)
                self.assertEqual(stats["matched_target_count"], 1)
                self.assertEqual(stats["non_target_product_count"], 1)
                table = pq.read_table(root / stats["staging_path"])
                self.assertEqual(table.num_rows, 1)
                self.assertNotIn("user_id", table.schema.names)
                self.assertNotIn("SECRET-USER-ID", log_path.read_text(encoding="utf-8"))
                self.assertNotIn("OTHER-SECRET-ID", log_path.read_text(encoding="utf-8"))
            finally:
                os.chmod(reviews, stat.S_IWRITE | stat.S_IREAD)

    def test_deduplication_prefers_richer_text_and_removes_private_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            duplicate_key = "d" * 64
            common = {
                "parent_asin": "P",
                "asin": "A",
                "source_domains": ["Electronics", "Home_and_Kitchen"],
                "device_type": "smart_plug",
                "main_category": "Test",
                "product_title": "Product",
                "timestamp_ms": 1_700_000_000_000,
                "review_datetime": w4.timestamp_fields(1_700_000_000_000)[1],
                "review_month": w4.timestamp_fields(1_700_000_000_000)[2],
                "rating": 1.0,
                "verified_purchase": True,
                "helpful_vote": 0,
                "language_status": "English",
                "language_detected_iso": "EN",
                "language_confidence": 0.99,
                "user_id_hash": "u" * 64,
                "duplicate_key": duplicate_key,
                "filter_version": "w3-v1.3.2",
                "_normalized_text_for_dedup": "same normalized text",
            }
            rows = [
                {
                    **common,
                    "source_domain": "Home_and_Kitchen",
                    "review_title": "",
                    "review_body": "Short duplicate text.",
                    "review_text": "Short duplicate text.",
                    "source_row_number": 2,
                    "_text_nonempty_fields": 1,
                },
                {
                    **common,
                    "source_domain": "Electronics",
                    "review_title": "Title",
                    "review_body": "Longer duplicate text with more evidence.",
                    "review_text": "Title\n\nLonger duplicate text with more evidence.",
                    "source_row_number": 1,
                    "_text_nonempty_fields": 2,
                },
            ]
            staging = work / "input.parquet"
            pq.write_table(pa.Table.from_pylist(rows, schema=w4.STAGING_SCHEMA), staging)
            final = root / "review_level_base.parquet"
            summary, _ = w4.materialize_final(
                [
                    {
                        "staging_path": str(staging.relative_to(root)),
                    }
                ],
                root=root,
                work_dir=work,
                final_path=final,
                config={
                    "phase": {
                        "minimum_free_gib": 0,
                        "duckdb_memory_limit": "256MB",
                        "parquet_compression": "zstd",
                    },
                    "deduplication": {
                        "source_domain_priority": [
                            "Electronics",
                            "Home_and_Kitchen",
                        ]
                    },
                },
                log_path=work / "test.log",
                disk_events=[],
            )
            result = pq.read_table(final).to_pylist()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["source_domain"], "Electronics")
            self.assertEqual(summary["cross_domain_rows_removed"], 1)
            self.assertNotIn("_normalized_text_for_dedup", result[0])
            self.assertNotIn("user_id", result[0])


if __name__ == "__main__":
    unittest.main()
