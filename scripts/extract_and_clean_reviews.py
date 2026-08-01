from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import hmac
import html
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shutil
import statistics
import sys
import time
import tomllib
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from lingua import Language, LanguageDetectorBuilder


PHASE = "W4"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
FILE_ATTRIBUTE_READONLY = 0x1
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
WHITESPACE_RE = re.compile(r"\s+")

STAGING_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("asin", pa.string()),
        pa.field("source_domain", pa.string(), nullable=False),
        pa.field("source_domains", pa.list_(pa.string()), nullable=False),
        pa.field("device_type", pa.string(), nullable=False),
        pa.field("main_category", pa.string()),
        pa.field("product_title", pa.string()),
        pa.field("timestamp_ms", pa.int64(), nullable=False),
        pa.field("review_datetime", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("review_month", pa.date32(), nullable=False),
        pa.field("rating", pa.float64()),
        pa.field("verified_purchase", pa.bool_()),
        pa.field("helpful_vote", pa.int64()),
        pa.field("review_title", pa.string(), nullable=False),
        pa.field("review_body", pa.string(), nullable=False),
        pa.field("review_text", pa.string(), nullable=False),
        pa.field("language_status", pa.string(), nullable=False),
        pa.field("language_detected_iso", pa.string()),
        pa.field("language_confidence", pa.float32()),
        pa.field("user_id_hash", pa.string()),
        pa.field("duplicate_key", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("filter_version", pa.string(), nullable=False),
        pa.field("_normalized_text_for_dedup", pa.string(), nullable=False),
        pa.field("_text_nonempty_fields", pa.int8(), nullable=False),
    ]
)

FINAL_FIELDS = [
    "parent_asin",
    "asin",
    "source_domain",
    "source_domains",
    "device_type",
    "main_category",
    "product_title",
    "timestamp_ms",
    "review_datetime",
    "review_month",
    "rating",
    "verified_purchase",
    "helpful_vote",
    "review_title",
    "review_body",
    "review_text",
    "language",
    "language_detected_iso",
    "language_confidence",
    "user_id_hash",
    "duplicate_key",
    "source_row_number",
    "filter_version",
]


class EnvironmentBlocked(RuntimeError):
    pass


class SourceMismatch(RuntimeError):
    pass


class SpaceGate(RuntimeError):
    pass


class ReviewExtractionFailed(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def resolve_inside(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Configured path escapes project root: {relative}") from exc
    return candidate


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    )


def atomic_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_log(path: Path, level: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] [{level}] {message}\n")


def get_windows_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    function = ctypes.windll.kernel32.GetFileAttributesW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint32
    attributes = int(function(str(path)))
    if attributes == INVALID_FILE_ATTRIBUTES:
        raise ctypes.WinError()
    return attributes


def is_readonly(path: Path) -> bool:
    if os.name == "nt":
        return bool(get_windows_attributes(path) & FILE_ATTRIBUTE_READONLY)
    return not bool(path.stat().st_mode & 0o222)


def process_rss_bytes() -> int | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCountersEx),
        ctypes.c_uint32,
    ]
    get_process_memory_info.restype = ctypes.c_int
    current_process = get_current_process()
    ok = get_process_memory_info(
        current_process, ctypes.byref(counters), counters.cb
    )
    return int(counters.WorkingSetSize) if ok else None


def update_peak(current_peak: int | None) -> int | None:
    current = process_rss_bytes()
    if current is None:
        return current_peak
    return current if current_peak is None else max(current_peak, current)


def file_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "readonly": is_readonly(path),
    }


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def clean_text(
    value: Any,
    *,
    unicode_form: str = "NFKC",
    strip_html: bool = True,
    collapse_whitespace: bool = True,
) -> tuple[str, dict[str, bool]]:
    text = value if isinstance(value, str) else ""
    original = text
    decoded = html.unescape(text)
    html_changed = decoded != text
    text = decoded
    if strip_html:
        stripped = HTML_TAG_RE.sub(" ", text)
        html_changed = html_changed or stripped != text
        text = stripped
    normalized = unicodedata.normalize(unicode_form, text)
    unicode_changed = normalized != text
    text = normalized
    if collapse_whitespace:
        collapsed = WHITESPACE_RE.sub(" ", text).strip()
        whitespace_changed = collapsed != text
        text = collapsed
    else:
        whitespace_changed = text != text.strip()
        text = text.strip()
    return text, {
        "changed": text != original,
        "html_changed": html_changed,
        "unicode_changed": unicode_changed,
        "whitespace_changed": whitespace_changed,
    }


def normalize_for_dedup(text: str, unicode_form: str, casefold: bool) -> str:
    normalized = unicodedata.normalize(unicode_form, text)
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.casefold() if casefold else normalized


def timestamp_fields(value: Any) -> tuple[int, datetime, date] | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not float(value).is_integer():
        return None
    milliseconds = int(value)
    if milliseconds < 0:
        return None
    try:
        converted = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return milliseconds, converted, date(converted.year, converted.month, 1)


def hash_user_id(salt: bytes, user_id: Any) -> str | None:
    if not isinstance(user_id, str) or not user_id:
        return None
    return hmac.new(salt, user_id.encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_rating(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def make_duplicate_key(
    user_id_hash: str | None,
    parent_asin: str,
    timestamp_ms: int,
    rating: float | None,
    normalized_text: str,
) -> str:
    payload = orjson.dumps(
        [
            user_id_hash or "<missing-user>",
            parent_asin,
            timestamp_ms,
            rating,
            normalized_text,
        ]
    )
    return hashlib.sha256(payload).hexdigest()


def classify_language(
    text: str,
    detector: Any,
    *,
    minimum_alphabetic_characters: int,
    minimum_total_characters: int,
    minimum_english_confidence: float,
) -> tuple[str, str | None, float | None]:
    alphabetic = sum(character.isalpha() for character in text)
    if len(text) < minimum_total_characters or alphabetic < minimum_alphabetic_characters:
        return "undetermined_short", None, None
    detected = detector.detect_language_of(text)
    if detected is None:
        return "undetermined_other", None, None
    confidence = float(detector.compute_language_confidence(text, Language.ENGLISH))
    iso_code = detected.iso_code_639_1.name
    if detected == Language.ENGLISH and confidence >= minimum_english_confidence:
        return "English", iso_code, confidence
    return "non-English", iso_code, confidence


def load_or_create_salt(path: Path) -> tuple[bytes, bool]:
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        if len(value) != 64:
            raise EnvironmentBlocked("Existing private salt is not 32-byte hexadecimal.")
        try:
            return bytes.fromhex(value), False
        except ValueError as exc:
            raise EnvironmentBlocked("Existing private salt is invalid.") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    atomic_text(path, salt.hex() + "\n")
    return salt, True


def load_targets(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    required = [
        "parent_asin",
        "source_domains",
        "main_category",
        "title",
        "device_type",
        "filter_version",
    ]
    table = pq.read_table(path, columns=required)
    if table.num_rows != 106:
        raise SourceMismatch(
            f"target_products.parquet has {table.num_rows} rows, expected 106."
        )
    rows = table.to_pylist()
    targets: dict[str, dict[str, Any]] = {}
    counts = Counter()
    versions = set()
    for row in rows:
        parent = row["parent_asin"]
        if not isinstance(parent, str) or not parent or parent in targets:
            raise SourceMismatch("Target parent_asin values must be nonempty and unique.")
        device_type = row["device_type"]
        counts[device_type] += 1
        versions.add(row["filter_version"])
        targets[parent] = {
            "source_domains": list(row["source_domains"] or []),
            "main_category": row["main_category"],
            "product_title": row["title"],
            "device_type": device_type,
            "filter_version": row["filter_version"],
        }
    expected = {"smart_plug": 95, "smart_bulb": 8, "smart_switch": 3}
    if dict(counts) != expected:
        raise SourceMismatch(f"Target device counts differ: {dict(counts)}")
    if versions != {"w3-v1.3.2"}:
        raise SourceMismatch(f"Unexpected target filter versions: {sorted(versions)}")
    identity = file_identity(path)
    identity["sha256"] = sha256_file(path)
    identity["rows"] = table.num_rows
    identity["device_type_counts"] = dict(sorted(counts.items()))
    identity["filter_version"] = next(iter(versions))
    return targets, identity


def build_detector(config: dict[str, Any]) -> Any:
    builder = LanguageDetectorBuilder.from_all_languages().with_low_accuracy_mode()
    distance = float(config["minimum_relative_distance"])
    if distance > 0:
        builder = builder.with_minimum_relative_distance(distance)
    return builder.build()


def safe_remove_current_run(path: Path, work_root: Path, log_path: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(work_root.resolve())
    except ValueError as exc:
        raise ReviewExtractionFailed(
            f"Refusing cleanup outside W4 work directory: {path}"
        ) from exc
    if resolved.exists():
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        append_log(log_path, "INFO", f"Cleaned current-run temporary path: {resolved}")


def write_batch(writer: pq.ParquetWriter, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    writer.write_table(pa.Table.from_pylist(rows, schema=STAGING_SCHEMA))
    rows.clear()


def scan_source(
    source: dict[str, Any],
    *,
    root: Path,
    raw_uncompressed: Path,
    targets: dict[str, dict[str, Any]],
    salt: bytes,
    detector: Any,
    config: dict[str, Any],
    fingerprint: str,
    work_dir: Path,
    reports_dir: Path,
    log_path: Path,
    disk_events: list[dict[str, Any]],
) -> dict[str, Any]:
    source_path = resolve_inside(raw_uncompressed, source["relative_path"])
    if source_path.suffix.lower() != ".jsonl" or source_path.name.endswith(".jsonl.gz"):
        raise SourceMismatch(f"W4 source is not an ordinary JSONL: {source_path}")
    identity = file_identity(source_path)
    expected_identity = {
        "size_bytes": int(source["expected_bytes"]),
        "readonly": True,
    }
    if identity["size_bytes"] != expected_identity["size_bytes"] or not identity["readonly"]:
        raise SourceMismatch(f"Review source identity mismatch: {source['id']}")

    checkpoint_dir = reports_dir / "checkpoints"
    checkpoint_path = checkpoint_dir / f"{source['id']}.json"
    staging_path = work_dir / f"{source['id']}_{fingerprint[:16]}_matched.parquet"
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("status") == "COMPLETE"
            and checkpoint.get("configuration_fingerprint") == fingerprint
            and checkpoint.get("input_identity") == identity
            and checkpoint.get("staging_path") == str(staging_path.relative_to(root))
            and staging_path.exists()
            and staging_path.stat().st_size == checkpoint.get("staging_bytes")
        ):
            append_log(
                log_path,
                "INFO",
                f"Checkpoint reused; review scan skipped: id={source['id']}",
            )
            return checkpoint

    free_start = shutil.disk_usage(root).free
    minimum_free = int(config["phase"]["minimum_free_gib"]) * 1024**3
    if free_start < minimum_free:
        raise SpaceGate(f"Free space below gate before {source['id']}.")
    scan_started = now_iso()
    disk_events.append(
        {
            "time": scan_started,
            "event": "review_scan_start",
            "source_id": source["id"],
            "free_bytes": free_start,
            "free_gib": free_start / 1024**3,
        }
    )
    append_log(
        log_path,
        "INFO",
        f"Review scan started: id={source['id']}; bytes={identity['size_bytes']}; "
        f"free_bytes={free_start}",
    )

    counts = Counter()
    language_counts = Counter()
    language_by_device: dict[str, Counter[str]] = defaultdict(Counter)
    matched_by_device = Counter()
    parse_error_categories = Counter()
    parse_error_details: list[dict[str, Any]] = []
    timestamp_min: int | None = None
    timestamp_max: int | None = None
    batch: list[dict[str, Any]] = []
    batch_rows = int(config["phase"]["parquet_batch_rows"])
    progress_records = int(config["phase"]["progress_records"])
    progress_bytes = int(config["phase"]["progress_bytes_gib"]) * 1024**3
    next_progress_records = progress_records
    next_progress_bytes = progress_bytes
    byte_position = 0
    peak_rss = update_peak(None)
    started_monotonic = time.perf_counter()
    run_id = f"{int(time.time())}-{os.getpid()}"
    temporary_path = work_dir / f".{source['id']}.{run_id}.part.parquet"
    writer: pq.ParquetWriter | None = None

    text_config = config["text"]
    language_config = config["language"]
    try:
        writer = pq.ParquetWriter(
            temporary_path,
            STAGING_SCHEMA,
            compression=config["phase"]["parquet_compression"],
            use_dictionary=True,
            write_statistics=True,
        )
        with source_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line_offset = byte_position
                byte_position += len(raw_line)
                counts["physical_line_count"] += 1
                if not raw_line.strip():
                    counts["empty_line_count"] += 1
                    continue
                counts["nonempty_record_count"] += 1
                try:
                    record = orjson.loads(raw_line)
                except orjson.JSONDecodeError as exc:
                    counts["json_parse_error_count"] += 1
                    category = type(exc).__name__
                    parse_error_categories[category] += 1
                    if len(parse_error_details) < 1000:
                        parse_error_details.append(
                            {
                                "source_id": source["id"],
                                "line_number": line_number,
                                "byte_position": line_offset,
                                "error_category": category,
                            }
                        )
                    continue
                counts["json_parse_success_count"] += 1
                if not isinstance(record, dict):
                    counts["non_object_json_count"] += 1
                    continue
                counts["json_object_count"] += 1
                parent = record.get("parent_asin")
                if not isinstance(parent, str) or not parent:
                    counts["parent_asin_missing_count"] += 1
                    continue
                product = targets.get(parent)
                if product is None:
                    counts["non_target_product_count"] += 1
                    continue
                counts["matched_target_count"] += 1
                matched_by_device[product["device_type"]] += 1

                review_title, title_changes = clean_text(
                    record.get("title"),
                    unicode_form=text_config["unicode_form"],
                    strip_html=bool(text_config["strip_html"]),
                    collapse_whitespace=bool(text_config["collapse_whitespace"]),
                )
                review_body, body_changes = clean_text(
                    record.get("text"),
                    unicode_form=text_config["unicode_form"],
                    strip_html=bool(text_config["strip_html"]),
                    collapse_whitespace=bool(text_config["collapse_whitespace"]),
                )
                for name, changed in (
                    ("title_cleaned", title_changes["changed"]),
                    ("body_cleaned", body_changes["changed"]),
                    (
                        "html_changed",
                        title_changes["html_changed"] or body_changes["html_changed"],
                    ),
                    (
                        "unicode_changed",
                        title_changes["unicode_changed"]
                        or body_changes["unicode_changed"],
                    ),
                    (
                        "whitespace_changed",
                        title_changes["whitespace_changed"]
                        or body_changes["whitespace_changed"],
                    ),
                ):
                    if changed:
                        counts[name] += 1
                pieces = [piece for piece in (review_title, review_body) if piece]
                if not pieces:
                    counts["empty_text_removed_count"] += 1
                    continue
                review_text = text_config["review_text_separator"].join(pieces)
                timestamp_result = timestamp_fields(record.get("timestamp"))
                if timestamp_result is None:
                    value = record.get("timestamp")
                    if value is None:
                        counts["timestamp_null_count"] += 1
                    elif isinstance(value, bool) or not isinstance(value, (int, float)):
                        counts["timestamp_non_numeric_count"] += 1
                    elif value < 0:
                        counts["timestamp_negative_count"] += 1
                    else:
                        counts["timestamp_unconvertible_count"] += 1
                    continue
                timestamp_ms, review_datetime, review_month = timestamp_result
                timestamp_min = (
                    timestamp_ms
                    if timestamp_min is None
                    else min(timestamp_min, timestamp_ms)
                )
                timestamp_max = (
                    timestamp_ms
                    if timestamp_max is None
                    else max(timestamp_max, timestamp_ms)
                )
                normalized_text = normalize_for_dedup(
                    review_text,
                    text_config["unicode_form"],
                    bool(text_config["dedup_casefold"]),
                )
                user_hash = hash_user_id(salt, record.get("user_id"))
                if user_hash is None:
                    counts["user_id_missing_count"] += 1
                rating = canonical_rating(record.get("rating"))
                language_status, language_iso, language_confidence = classify_language(
                    review_text,
                    detector,
                    minimum_alphabetic_characters=int(
                        language_config["minimum_alphabetic_characters"]
                    ),
                    minimum_total_characters=int(
                        language_config["minimum_total_characters"]
                    ),
                    minimum_english_confidence=float(
                        language_config["minimum_english_confidence"]
                    ),
                )
                language_counts[language_status] += 1
                language_by_device[product["device_type"]][language_status] += 1
                duplicate_key = make_duplicate_key(
                    user_hash, parent, timestamp_ms, rating, normalized_text
                )
                helpful = record.get("helpful_vote")
                if isinstance(helpful, bool) or not isinstance(helpful, int):
                    helpful = None
                verified = record.get("verified_purchase")
                if not isinstance(verified, bool):
                    verified = None
                asin = record.get("asin")
                if not isinstance(asin, str) or not asin:
                    asin = None
                batch.append(
                    {
                        "parent_asin": parent,
                        "asin": asin,
                        "source_domain": source["domain"],
                        "source_domains": product["source_domains"],
                        "device_type": product["device_type"],
                        "main_category": product["main_category"],
                        "product_title": product["product_title"],
                        "timestamp_ms": timestamp_ms,
                        "review_datetime": review_datetime,
                        "review_month": review_month,
                        "rating": rating,
                        "verified_purchase": verified,
                        "helpful_vote": helpful,
                        "review_title": review_title,
                        "review_body": review_body,
                        "review_text": review_text,
                        "language_status": language_status,
                        "language_detected_iso": language_iso,
                        "language_confidence": language_confidence,
                        "user_id_hash": user_hash,
                        "duplicate_key": duplicate_key,
                        "source_row_number": line_number,
                        "filter_version": product["filter_version"],
                        "_normalized_text_for_dedup": normalized_text,
                        "_text_nonempty_fields": len(pieces),
                    }
                )
                counts["cleaned_candidate_count"] += 1
                if len(batch) >= batch_rows:
                    write_batch(writer, batch)

                if (
                    counts["physical_line_count"] >= next_progress_records
                    or byte_position >= next_progress_bytes
                ):
                    write_batch(writer, batch)
                    peak_rss = update_peak(peak_rss)
                    free_now = shutil.disk_usage(root).free
                    if free_now < minimum_free:
                        raise SpaceGate(f"Free space fell below gate during {source['id']}.")
                    elapsed = max(time.perf_counter() - started_monotonic, 0.001)
                    append_log(
                        log_path,
                        "INFO",
                        f"Review progress: id={source['id']}; "
                        f"records={counts['physical_line_count']}; "
                        f"matched={counts['matched_target_count']}; "
                        f"bytes={byte_position}; bytes_per_second={int(byte_position / elapsed)}; "
                        f"free_bytes={free_now}",
                    )
                    while counts["physical_line_count"] >= next_progress_records:
                        next_progress_records += progress_records
                    while byte_position >= next_progress_bytes:
                        next_progress_bytes += progress_bytes
        write_batch(writer, batch)
        writer.close()
        writer = None
    except BaseException:
        if writer is not None:
            writer.close()
        if temporary_path.exists():
            safe_remove_current_run(temporary_path, work_dir, log_path)
        raise

    duration = time.perf_counter() - started_monotonic
    peak_rss = update_peak(peak_rss)
    for field in [
        "physical_line_count",
        "empty_line_count",
        "nonempty_record_count",
        "json_parse_success_count",
        "json_parse_error_count",
        "json_object_count",
        "non_object_json_count",
        "parent_asin_missing_count",
        "matched_target_count",
        "non_target_product_count",
        "title_cleaned",
        "body_cleaned",
        "html_changed",
        "unicode_changed",
        "whitespace_changed",
        "empty_text_removed_count",
        "timestamp_null_count",
        "timestamp_non_numeric_count",
        "timestamp_negative_count",
        "timestamp_unconvertible_count",
        "user_id_missing_count",
        "cleaned_candidate_count",
    ]:
        counts.setdefault(field, 0)
    for status in [
        "English",
        "non-English",
        "undetermined_short",
        "undetermined_other",
    ]:
        language_counts.setdefault(status, 0)
    for device_type in DEVICE_TYPES:
        matched_by_device.setdefault(device_type, 0)
        for status in [
            "English",
            "non-English",
            "undetermined_short",
            "undetermined_other",
        ]:
            language_by_device[device_type].setdefault(status, 0)
    if counts["physical_line_count"] != int(source["expected_records"]):
        safe_remove_current_run(temporary_path, work_dir, log_path)
        raise SourceMismatch(
            f"{source['id']} records={counts['physical_line_count']}, "
            f"expected={source['expected_records']}"
        )
    if byte_position != int(source["expected_bytes"]):
        safe_remove_current_run(temporary_path, work_dir, log_path)
        raise SourceMismatch(
            f"{source['id']} scanned bytes={byte_position}, "
            f"expected={source['expected_bytes']}"
        )
    if staging_path.exists():
        raise ReviewExtractionFailed(f"Recognized staging destination already exists: {staging_path}")
    os.replace(temporary_path, staging_path)
    free_end = shutil.disk_usage(root).free
    scan_finished = now_iso()
    disk_events.append(
        {
            "time": scan_finished,
            "event": "review_scan_complete",
            "source_id": source["id"],
            "free_bytes": free_end,
            "free_gib": free_end / 1024**3,
        }
    )
    stats = {
        "phase": PHASE,
        "status": "COMPLETE",
        "configuration_fingerprint": fingerprint,
        "id": source["id"],
        "domain": source["domain"],
        "relative_path": source["relative_path"],
        "input_identity": identity,
        "scan_started_at": scan_started,
        "scan_finished_at": scan_finished,
        "duration_seconds": duration,
        "start_free_bytes": free_start,
        "end_free_bytes": free_end,
        "peak_process_rss_bytes": peak_rss,
        **dict(counts),
        "matched_by_device_type": dict(sorted(matched_by_device.items())),
        "language_counts": dict(sorted(language_counts.items())),
        "language_by_device_type": {
            device: dict(sorted(values.items()))
            for device, values in sorted(language_by_device.items())
        },
        "timestamp_min_ms": timestamp_min,
        "timestamp_max_ms": timestamp_max,
        "timestamp_min_utc": (
            datetime.fromtimestamp(timestamp_min / 1000, timezone.utc).isoformat()
            if timestamp_min is not None
            else None
        ),
        "timestamp_max_utc": (
            datetime.fromtimestamp(timestamp_max / 1000, timezone.utc).isoformat()
            if timestamp_max is not None
            else None
        ),
        "parse_error_categories": dict(parse_error_categories),
        "parse_error_details": parse_error_details,
        "staging_path": str(staging_path.relative_to(root)),
        "staging_rows": pq.ParquetFile(staging_path).metadata.num_rows,
        "staging_bytes": staging_path.stat().st_size,
    }
    atomic_json(checkpoint_path, stats)
    append_log(
        log_path,
        "INFO",
        f"Review scan completed: id={source['id']}; "
        f"records={counts['physical_line_count']}; "
        f"matched={counts['matched_target_count']}; "
        f"cleaned={counts['cleaned_candidate_count']}; seconds={duration:.3f}; "
        f"peak_rss_bytes={peak_rss}",
    )
    return stats


def query_dicts(connection: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def materialize_final(
    scan_stats: list[dict[str, Any]],
    *,
    root: Path,
    work_dir: Path,
    final_path: Path,
    config: dict[str, Any],
    log_path: Path,
    disk_events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_paths = [resolve_inside(root, stats["staging_path"]) for stats in scan_stats]
    if final_path.exists():
        raise ReviewExtractionFailed(
            f"Final output already exists without recognized completed W4 status: {final_path}"
        )
    free_start = shutil.disk_usage(root).free
    minimum_free = int(config["phase"]["minimum_free_gib"]) * 1024**3
    if free_start < minimum_free:
        raise SpaceGate("Free space below gate before final deduplication.")
    disk_events.append(
        {
            "time": now_iso(),
            "event": "deduplication_start",
            "free_bytes": free_start,
            "free_gib": free_start / 1024**3,
        }
    )
    temporary_output = work_dir / f"review_level_base.{os.getpid()}.part.parquet"
    duck_temp = work_dir / f"duckdb_temp_{os.getpid()}"
    duck_temp.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET memory_limit={sql_literal(config['phase']['duckdb_memory_limit'])}")
        connection.execute(f"SET temp_directory={sql_literal(duck_temp)}")
        path_list = ", ".join(sql_literal(path) for path in source_paths)
        connection.execute(
            f"CREATE VIEW all_matched AS SELECT * FROM read_parquet([{path_list}], union_by_name=true)"
        )
        language_rows = query_dicts(
            connection,
            """
            SELECT language_status, device_type, COUNT(*) AS records
            FROM all_matched
            GROUP BY language_status, device_type
            ORDER BY language_status, device_type
            """,
        )
        duplicate_summary = query_dicts(
            connection,
            """
            WITH groups AS (
              SELECT duplicate_key, COUNT(*) AS n,
                     COUNT(DISTINCT source_domain) AS domains
              FROM all_matched
              WHERE language_status = 'English'
              GROUP BY duplicate_key
            )
            SELECT
              COALESCE(SUM(n), 0) AS english_before_dedup,
              COUNT(*) AS unique_duplicate_keys,
              COUNT(*) FILTER (WHERE n > 1) AS duplicate_groups,
              COALESCE(SUM(n - 1) FILTER (WHERE domains = 1), 0) AS within_domain_rows_removed,
              COALESCE(SUM(n - 1) FILTER (WHERE domains > 1), 0) AS cross_domain_rows_removed,
              COALESCE(SUM(n - 1), 0) AS total_rows_removed
            FROM groups
            """,
        )[0]
        selected_fields = [
            "parent_asin",
            "asin",
            "source_domain",
            "source_domains",
            "device_type",
            "main_category",
            "product_title",
            "timestamp_ms",
            "review_datetime",
            "review_month",
            "rating",
            "verified_purchase",
            "helpful_vote",
            "review_title",
            "review_body",
            "review_text",
            "language_status AS language",
            "language_detected_iso",
            "language_confidence",
            "user_id_hash",
            "duplicate_key",
            "source_row_number",
            "filter_version",
        ]
        select_sql = ",\n".join(selected_fields)
        priority_cases = " ".join(
            f"WHEN {sql_literal(domain)} THEN {index}"
            for index, domain in enumerate(
                config["deduplication"]["source_domain_priority"], start=1
            )
        )
        query = f"""
            SELECT {select_sql}
            FROM (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY duplicate_key
                       ORDER BY
                         _text_nonempty_fields DESC,
                         LENGTH(review_text) DESC,
                         CASE source_domain {priority_cases} ELSE 999 END,
                         source_row_number,
                         parent_asin,
                         COALESCE(asin, '')
                     ) AS _dedup_rank
              FROM all_matched
              WHERE language_status = 'English'
            )
            WHERE _dedup_rank = 1
        """
        connection.execute(
            f"COPY ({query}) TO {sql_literal(temporary_output)} "
            f"(FORMAT PARQUET, COMPRESSION {str(config['phase']['parquet_compression']).upper()}, "
            "ROW_GROUP_SIZE 50000)"
        )
    except BaseException:
        connection.close()
        if temporary_output.exists():
            safe_remove_current_run(temporary_output, work_dir, log_path)
        if duck_temp.exists():
            safe_remove_current_run(duck_temp, work_dir, log_path)
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if duck_temp.exists():
        safe_remove_current_run(duck_temp, work_dir, log_path)
    parquet_file = pq.ParquetFile(temporary_output)
    final_rows = parquet_file.metadata.num_rows
    final_schema_names = parquet_file.schema_arrow.names
    del parquet_file
    if final_rows != int(duplicate_summary["unique_duplicate_keys"]):
        safe_remove_current_run(temporary_output, work_dir, log_path)
        raise ReviewExtractionFailed(
            f"Dedup row mismatch: {final_rows} != "
            f"{duplicate_summary['unique_duplicate_keys']}"
        )
    if final_schema_names != FINAL_FIELDS:
        safe_remove_current_run(temporary_output, work_dir, log_path)
        raise ReviewExtractionFailed("Final Parquet schema names differ from W4 schema.")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary_output, final_path)
    free_end = shutil.disk_usage(root).free
    disk_events.append(
        {
            "time": now_iso(),
            "event": "final_parquet_complete",
            "free_bytes": free_end,
            "free_gib": free_end / 1024**3,
        }
    )
    duplicate_summary["final_rows"] = final_rows
    duplicate_summary["tie_break_rule"] = (
        "text_nonempty_fields_desc, review_text_length_desc, configured_source_priority, "
        "source_row_number, parent_asin, asin"
    )
    return duplicate_summary, language_rows


def quantile(values: list[int], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def build_reports(
    *,
    root: Path,
    final_path: Path,
    targets: dict[str, dict[str, Any]],
    target_identity: dict[str, Any],
    scan_stats: list[dict[str, Any]],
    duplicate_summary: dict[str, Any],
    language_rows: list[dict[str, Any]],
    config: dict[str, Any],
    fingerprint: str,
    reports_dir: Path,
    log_path: Path,
    disk_events: list[dict[str, Any]],
    environment: dict[str, Any],
    raw_before: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    connection = duckdb.connect(":memory:")
    final_literal = sql_literal(final_path)
    try:
        connection.execute(
            f"CREATE VIEW reviews AS SELECT * FROM read_parquet({final_literal})"
        )
        product_counts_raw = query_dicts(
            connection,
            """
            SELECT parent_asin, device_type, COUNT(*) AS n_reviews,
                   STRFTIME(MIN(TIMEZONE('UTC', review_datetime)),
                            '%Y-%m-%dT%H:%M:%S.%gZ') AS earliest_review_utc,
                   STRFTIME(MAX(TIMEZONE('UTC', review_datetime)),
                            '%Y-%m-%dT%H:%M:%S.%gZ') AS latest_review_utc
            FROM reviews
            GROUP BY parent_asin, device_type
            ORDER BY device_type, parent_asin
            """,
        )
        count_by_year = query_dicts(
            connection,
            """
            SELECT device_type, YEAR(review_datetime) AS year, COUNT(*) AS reviews
            FROM reviews GROUP BY device_type, year ORDER BY device_type, year
            """,
        )
        count_by_rating = query_dicts(
            connection,
            """
            SELECT device_type, rating, COUNT(*) AS reviews
            FROM reviews GROUP BY device_type, rating ORDER BY device_type, rating
            """,
        )
        count_by_verified = query_dicts(
            connection,
            """
            SELECT device_type, verified_purchase, COUNT(*) AS reviews
            FROM reviews GROUP BY device_type, verified_purchase
            ORDER BY device_type, verified_purchase
            """,
        )
        overall_time = query_dicts(
            connection,
            """
            SELECT COUNT(*) AS reviews, COUNT(DISTINCT parent_asin) AS products,
                   STRFTIME(MIN(TIMEZONE('UTC', review_datetime)),
                            '%Y-%m-%dT%H:%M:%S.%gZ') AS earliest_review_utc,
                   STRFTIME(MAX(TIMEZONE('UTC', review_datetime)),
                            '%Y-%m-%dT%H:%M:%S.%gZ') AS latest_review_utc
            FROM reviews
            """,
        )[0]
        product_month_rows = query_dicts(
            connection,
            """
            SELECT parent_asin, device_type, review_month, COUNT(*) AS n_reviews
            FROM reviews
            GROUP BY parent_asin, device_type, review_month
            """,
        )
    finally:
        connection.close()

    product_counts_lookup = {
        row["parent_asin"]: int(row["n_reviews"]) for row in product_counts_raw
    }
    product_report_rows: list[dict[str, Any]] = []
    for parent, product in sorted(
        targets.items(), key=lambda item: (item[1]["device_type"], item[0])
    ):
        source = next(
            (row for row in product_counts_raw if row["parent_asin"] == parent), None
        )
        product_report_rows.append(
            {
                "parent_asin": parent,
                "device_type": product["device_type"],
                "n_reviews": product_counts_lookup.get(parent, 0),
                "has_reviews": parent in product_counts_lookup,
                "earliest_review_utc": source["earliest_review_utc"] if source else None,
                "latest_review_utc": source["latest_review_utc"] if source else None,
            }
        )

    thresholds = [int(value) for value in config["readiness"]["diagnostic_product_thresholds"]]
    device_rows: list[dict[str, Any]] = []
    concentration: dict[str, Any] = {}
    for device in (*DEVICE_TYPES, "ALL"):
        selected = [
            row
            for row in product_report_rows
            if device == "ALL" or row["device_type"] == device
        ]
        counts = [int(row["n_reviews"]) for row in selected]
        positive = [value for value in counts if value > 0]
        earliest_values = [
            str(row["earliest_review_utc"])
            for row in selected
            if row["earliest_review_utc"]
        ]
        latest_values = [
            str(row["latest_review_utc"])
            for row in selected
            if row["latest_review_utc"]
        ]
        total = sum(counts)
        ordered = sorted(counts, reverse=True)
        row: dict[str, Any] = {
            "device_type": device,
            "target_products": len(selected),
            "products_with_reviews": len(positive),
            "products_without_reviews": len(selected) - len(positive),
            "final_reviews": total,
            "reviews_per_target_product_min": min(counts) if counts else None,
            "reviews_per_target_product_median": statistics.median(counts)
            if counts
            else None,
            "reviews_per_target_product_mean": statistics.mean(counts)
            if counts
            else None,
            "reviews_per_target_product_max": max(counts) if counts else None,
            "earliest_review_utc": min(earliest_values)
            if earliest_values
            else None,
            "latest_review_utc": max(latest_values) if latest_values else None,
        }
        for threshold in thresholds:
            row[f"products_with_at_least_{threshold}_reviews"] = sum(
                value >= threshold for value in counts
            )
        device_rows.append(row)
        concentration[device] = {
            "total_reviews": total,
            "top_1_product_share": ordered[0] / total if total and ordered else None,
            "top_5_product_share": sum(ordered[:5]) / total if total else None,
            "top_10_product_share": sum(ordered[:10]) / total if total else None,
        }

    monthly_rows: list[dict[str, Any]] = []
    month_thresholds = [
        int(value) for value in config["readiness"]["diagnostic_product_month_thresholds"]
    ]
    for device in (*DEVICE_TYPES, "ALL"):
        values = [
            int(row["n_reviews"])
            for row in product_month_rows
            if device == "ALL" or row["device_type"] == device
        ]
        row = {
            "device_type": device,
            "product_month_rows": len(values),
            "monthly_reviews_mean": statistics.mean(values) if values else None,
            "monthly_reviews_median": statistics.median(values) if values else None,
            "monthly_reviews_p25": quantile(values, 0.25),
            "monthly_reviews_p75": quantile(values, 0.75),
            "monthly_reviews_p90": quantile(values, 0.90),
            "monthly_reviews_max": max(values) if values else None,
        }
        for threshold in month_thresholds:
            row[f"product_months_at_least_{threshold}"] = sum(
                value >= threshold for value in values
            )
        monthly_rows.append(row)

    language_totals = Counter()
    language_by_device: dict[str, Counter[str]] = defaultdict(Counter)
    for row in language_rows:
        status = row["language_status"]
        device = row["device_type"]
        count = int(row["records"])
        language_totals[status] += count
        language_by_device[device][status] += count
    language_denominator = sum(language_totals.values())
    undetermined = sum(
        language_totals[name]
        for name in ("undetermined_short", "undetermined_other")
    )

    readiness_warnings: list[dict[str, Any]] = []
    minimum_products = int(config["readiness"]["minimum_products_warning"])
    for row in device_rows:
        if row["device_type"] == "ALL":
            continue
        if row["products_with_reviews"] == 0 or row["final_reviews"] == 0:
            readiness_warnings.append(
                {
                    "code": "DEVICE_TYPE_HAS_NO_USABLE_REVIEWS",
                    "device_type": row["device_type"],
                    "value": row["final_reviews"],
                }
            )
        if row["products_with_reviews"] < minimum_products:
            readiness_warnings.append(
                {
                    "code": "DEVICE_TYPE_FEWER_THAN_DIAGNOSTIC_PRODUCT_COUNT",
                    "device_type": row["device_type"],
                    "value": row["products_with_reviews"],
                    "diagnostic_threshold": minimum_products,
                    "professor_requirement": False,
                }
            )
        top_share = concentration[row["device_type"]]["top_1_product_share"]
        if (
            top_share is not None
            and top_share > float(config["readiness"]["maximum_top_product_share"])
        ):
            readiness_warnings.append(
                {
                    "code": "REVIEWS_CONCENTRATED_IN_TOP_PRODUCT",
                    "device_type": row["device_type"],
                    "value": top_share,
                }
            )
    positive_product_counts = [
        row["products_with_reviews"]
        for row in device_rows
        if row["device_type"] != "ALL" and row["products_with_reviews"] > 0
    ]
    if positive_product_counts:
        imbalance = max(positive_product_counts) / min(positive_product_counts)
        if imbalance > float(
            config["readiness"]["maximum_device_product_imbalance_ratio"]
        ):
            readiness_warnings.append(
                {
                    "code": "SEVERE_DEVICE_PRODUCT_IMBALANCE",
                    "value": imbalance,
                    "diagnostic_threshold": config["readiness"][
                        "maximum_device_product_imbalance_ratio"
                    ],
                    "professor_requirement": False,
                }
            )
    undetermined_share = (
        undetermined / language_denominator if language_denominator else 0.0
    )
    if undetermined_share > float(config["language"]["undetermined_warning_share"]):
        readiness_warnings.append(
            {
                "code": "LANGUAGE_UNDETERMINED_SHARE_HIGH",
                "value": undetermined_share,
                "diagnostic_threshold": config["language"][
                    "undetermined_warning_share"
                ],
            }
        )
    w5_readiness = "REVIEW_REQUIRED" if readiness_warnings else "READY"

    raw_after: dict[str, dict[str, Any]] = {}
    raw_unchanged = True
    for stats in scan_stats:
        source_path = resolve_inside(
            resolve_inside(root, config["_project_paths"]["raw_uncompressed"]),
            stats["relative_path"],
        )
        current = file_identity(source_path)
        raw_after[stats["id"]] = current
        if current != raw_before[stats["id"]] or not current["readonly"]:
            raw_unchanged = False

    final_parquet = pq.ParquetFile(final_path)
    final_identity = {
        "path": str(final_path.relative_to(root)),
        "rows": final_parquet.metadata.num_rows,
        "bytes": final_path.stat().st_size,
        "compression_by_column": sorted(
            {
                final_parquet.metadata.row_group(group)
                .column(column)
                .compression
                for group in range(final_parquet.metadata.num_row_groups)
                for column in range(final_parquet.metadata.row_group(group).num_columns)
            }
        ),
        "fields": final_parquet.schema_arrow.names,
        "schema": str(final_parquet.schema_arrow),
    }

    extraction_flow = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "configuration_fingerprint": fingerprint,
        "filter_version": target_identity["filter_version"],
        "target_products": target_identity,
        "source_scans": scan_stats,
        "totals": {
            name: sum(int(stats.get(name, 0)) for stats in scan_stats)
            for name in [
                "physical_line_count",
                "empty_line_count",
                "nonempty_record_count",
                "json_parse_success_count",
                "json_parse_error_count",
                "json_object_count",
                "non_object_json_count",
                "parent_asin_missing_count",
                "matched_target_count",
                "non_target_product_count",
                "cleaned_candidate_count",
                "empty_text_removed_count",
            ]
        },
        "final": final_identity,
    }
    extraction_csv = []
    for stats in scan_stats:
        extraction_csv.append(
            {
                "source_id": stats["id"],
                "source_domain": stats["domain"],
                "physical_records": stats["physical_line_count"],
                "json_errors": stats.get("json_parse_error_count", 0),
                "parent_asin_missing": stats.get("parent_asin_missing_count", 0),
                "matched_target_reviews": stats.get("matched_target_count", 0),
                "non_target_reviews": stats.get("non_target_product_count", 0),
                "match_rate": stats.get("matched_target_count", 0)
                / stats["physical_line_count"],
                "cleaned_candidates": stats.get("cleaned_candidate_count", 0),
                "seconds": stats["duration_seconds"],
                "peak_rss_bytes": stats["peak_process_rss_bytes"],
            }
        )

    cleaning_reasons = {
        name: sum(int(stats.get(name, 0)) for stats in scan_stats)
        for name in [
            "matched_target_count",
            "title_cleaned",
            "body_cleaned",
            "html_changed",
            "unicode_changed",
            "whitespace_changed",
            "empty_text_removed_count",
            "timestamp_null_count",
            "timestamp_non_numeric_count",
            "timestamp_negative_count",
            "timestamp_unconvertible_count",
            "cleaned_candidate_count",
        ]
    }
    cleaning_flow = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "configuration_fingerprint": fingerprint,
        "counts": cleaning_reasons,
        "language_before_filter": dict(sorted(language_totals.items())),
        "duplicate_audit": duplicate_summary,
        "final_rows": final_identity["rows"],
        "prohibited_fields_created": [],
    }
    language_audit = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "package": config["language"]["package"],
        "package_version": environment["lingua_version"],
        "configuration": config["language"],
        "counts": dict(sorted(language_totals.items())),
        "counts_by_device_type": {
            device: dict(sorted(counts.items()))
            for device, counts in sorted(language_by_device.items())
        },
        "denominator": language_denominator,
        "undetermined_count": undetermined,
        "undetermined_share": undetermined_share,
        "online_service_used": False,
    }
    timestamp_audit = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "unit": "Unix milliseconds",
        "source_scans": [
            {
                "source_id": stats["id"],
                "null": stats.get("timestamp_null_count", 0),
                "non_numeric": stats.get("timestamp_non_numeric_count", 0),
                "negative": stats.get("timestamp_negative_count", 0),
                "unconvertible": stats.get("timestamp_unconvertible_count", 0),
                "matched_cleaned_min_utc": stats.get("timestamp_min_utc"),
                "matched_cleaned_max_utc": stats.get("timestamp_max_utc"),
            }
            for stats in scan_stats
        ],
        "final_earliest_utc": overall_time["earliest_review_utc"],
        "final_latest_utc": overall_time["latest_review_utc"],
        "review_month_type": "date32: first calendar day of UTC month",
    }
    readiness = {
        "phase": "W5_READINESS",
        "generated_at": now_iso(),
        "status": w5_readiness,
        "warnings": readiness_warnings,
        "device_type_coverage": device_rows,
        "concentration": concentration,
        "monthly_coverage_diagnostics": monthly_rows,
        "diagnostic_thresholds_are_professor_requirements": False,
        "w5_started": False,
        "w3_rules_modified": False,
    }

    atomic_json(reports_dir / "review_extraction_flow.json", extraction_flow)
    atomic_csv(reports_dir / "review_extraction_flow.csv", extraction_csv)
    atomic_json(reports_dir / "review_cleaning_flow.json", cleaning_flow)
    atomic_csv(
        reports_dir / "review_cleaning_flow.csv",
        [{"reason": key, "count": value} for key, value in cleaning_reasons.items()],
    )
    atomic_json(
        reports_dir / "review_schema.json",
        {
            "phase": PHASE,
            "generated_at": now_iso(),
            "parquet": final_identity,
            "excluded_fields": [
                "images",
                "raw user_id",
                "normalized_text_for_dedup",
                "failure_binary",
                "failure_type",
                "severity",
                "persistence",
                "sentiment_score",
                "keyword_hit",
                "split",
            ],
        },
    )
    atomic_json(reports_dir / "duplicate_review_audit.json", duplicate_summary)
    atomic_json(reports_dir / "language_audit.json", language_audit)
    atomic_json(reports_dir / "timestamp_audit.json", timestamp_audit)
    atomic_csv(reports_dir / "review_count_by_device_type.csv", device_rows)
    atomic_csv(reports_dir / "review_count_by_product.csv", product_report_rows)
    atomic_csv(
        reports_dir / "review_count_by_year.csv",
        [
            {
                **row,
                "year": int(row["year"]) if row["year"] is not None else None,
            }
            for row in count_by_year
        ],
    )
    atomic_csv(reports_dir / "review_count_by_rating.csv", count_by_rating)
    atomic_csv(
        reports_dir / "monthly_coverage_diagnostics.csv", monthly_rows
    )
    atomic_csv(
        reports_dir / "review_count_by_verified_purchase.csv", count_by_verified
    )
    atomic_json(reports_dir / "w5_readiness.json", readiness)

    summary_lines = [
        "# Phase W4 Review Extraction and Cleaning Summary",
        "",
        f"- Final W4 status: `PASS`",
        f"- W5 readiness: `{w5_readiness}`",
        f"- Target products: {len(targets):,}",
        f"- Target-matched reviews before cleaning: "
        f"{extraction_flow['totals']['matched_target_count']:,}",
        f"- English reviews before deduplication: "
        f"{int(duplicate_summary['english_before_dedup']):,}",
        f"- Duplicate reviews removed: "
        f"{int(duplicate_summary['total_rows_removed']):,}",
        f"- Final reviews: {final_identity['rows']:,}",
        f"- Final UTC coverage: {timestamp_audit['final_earliest_utc']} — "
        f"{timestamp_audit['final_latest_utc']}",
        "",
        "| Device type | Target products | Products with reviews | Final reviews | "
        "Median reviews/product |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in device_rows:
        if row["device_type"] == "ALL":
            continue
        summary_lines.append(
            f"| {row['device_type']} | {row['target_products']:,} | "
            f"{row['products_with_reviews']:,} | {row['final_reviews']:,} | "
            f"{row['reviews_per_target_product_median']:,} |"
        )
    summary_lines.extend(
        [
            "",
            "The product and product-month thresholds in the diagnostics are provisional "
            "coverage checks, not professor-mandated minimum sample sizes.",
            "",
            "W5 was not started. No failure, severity, persistence, sentiment, keyword, "
            "rating-baseline, split, or model fields were created.",
        ]
    )
    atomic_text(
        reports_dir / "review_sample_summary.md", "\n".join(summary_lines) + "\n"
    )

    free_final = shutil.disk_usage(root).free
    disk_events.append(
        {
            "time": now_iso(),
            "event": "w4_reporting_complete",
            "free_bytes": free_final,
            "free_gib": free_final / 1024**3,
        }
    )
    atomic_json(
        reports_dir / "w4_disk_usage.json",
        {
            "phase": PHASE,
            "generated_at": now_iso(),
            "minimum_free_bytes": int(config["phase"]["minimum_free_gib"]) * 1024**3,
            "events": disk_events,
        },
    )

    required_reports = [
        "w4_execution.log",
        "review_extraction_flow.json",
        "review_extraction_flow.csv",
        "review_cleaning_flow.json",
        "review_cleaning_flow.csv",
        "review_schema.json",
        "review_sample_summary.md",
        "duplicate_review_audit.json",
        "language_audit.json",
        "timestamp_audit.json",
        "review_count_by_device_type.csv",
        "review_count_by_product.csv",
        "review_count_by_year.csv",
        "review_count_by_rating.csv",
        "monthly_coverage_diagnostics.csv",
        "w4_disk_usage.json",
        "w5_readiness.json",
    ]
    report_presence = {
        name: (reports_dir / name).exists() for name in required_reports
    }
    reconciled = all(
        stats["physical_line_count"]
        == stats.get("empty_line_count", 0) + stats["nonempty_record_count"]
        and stats["nonempty_record_count"]
        == stats["json_parse_success_count"]
        + stats.get("json_parse_error_count", 0)
        and stats["json_parse_success_count"]
        == stats["json_object_count"] + stats.get("non_object_json_count", 0)
        for stats in scan_stats
    )
    criteria = {
        "project_venv_environment_valid": environment["project_venv_in_use"],
        "w2_status_pass": True,
        "w3_status_pass": True,
        "target_products_identity_valid": True,
        "two_reviews_full_scans_complete": len(scan_stats) == 2,
        "input_record_counts_match_w2": all(
            stats["physical_line_count"]
            == next(
                int(item["expected_records"])
                for item in config["inputs"]["reviews"]
                if item["id"] == stats["id"]
            )
            for stats in scan_stats
        ),
        "record_count_reconciliation_passed": reconciled,
        "target_parent_filter_applied": True,
        "timestamp_conversion_complete": True,
        "text_cleaning_complete": True,
        "language_rule_complete": True,
        "duplicate_review_processing_complete": True,
        "review_level_base_valid": final_identity["rows"]
        == duplicate_summary["unique_duplicate_keys"],
        "exact_counts_and_date_coverage_available": True,
        "raw_reviews_unchanged_and_readonly": raw_unchanged,
        "final_free_space_at_least_60_gib": free_final
        >= int(config["phase"]["minimum_free_gib"]) * 1024**3,
        "compressed_archives_not_read": True,
        "metadata_jsonl_not_read": True,
        "w5_not_started": True,
        "required_reports_present": all(report_presence.values()),
    }
    status = "PASS" if all(criteria.values()) else "FAILED_REVIEW_EXTRACTION"
    w4_status = {
        "phase": PHASE,
        "status": status,
        "reason": (
            "All W4 technical acceptance criteria passed."
            if status == "PASS"
            else "One or more W4 technical acceptance criteria failed."
        ),
        "updated_at": now_iso(),
        "configuration_fingerprint": fingerprint,
        "environment": environment,
        "criteria": criteria,
        "report_presence": report_presence,
        "target_products": target_identity,
        "source_scans": [
            {
                "id": stats["id"],
                "physical_line_count": stats["physical_line_count"],
                "matched_target_count": stats["matched_target_count"],
                "duration_seconds": stats["duration_seconds"],
                "peak_process_rss_bytes": stats["peak_process_rss_bytes"],
            }
            for stats in scan_stats
        ],
        "review_level_base": final_identity,
        "w5_readiness": w5_readiness,
        "final_free_bytes": free_final,
        "final_free_gib": free_final / 1024**3,
        "raw_before": raw_before,
        "raw_after": raw_after,
        "policy_attestation": {
            "review_jsonl_opened": True,
            "metadata_jsonl_opened": False,
            "compressed_archive_opened": False,
            "raw_user_id_written_to_output_or_report": False,
            "online_language_service_used": False,
            "rating_used_as_failure_label": False,
            "annotation_or_baseline_performed": False,
            "product_month_feature_table_created": False,
            "chronological_split_created": False,
            "w3_rules_modified": False,
            "w5_started": False,
        },
    }
    atomic_json(reports_dir / "w4_status.json", w4_status)
    append_log(
        log_path,
        "INFO",
        f"W4 finished with status={status}; w5_readiness={w5_readiness}; "
        f"final_rows={final_identity['rows']}; free_bytes={free_final}",
    )
    return w4_status


def configuration_fingerprint(
    script_path: Path,
    config_path: Path,
    source_identities: dict[str, dict[str, Any]],
    target_identity: dict[str, Any],
    salt: bytes,
) -> str:
    digest = hashlib.sha256()
    digest.update(script_path.read_bytes())
    digest.update(config_path.read_bytes())
    digest.update(
        json.dumps(source_identities, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    digest.update(
        json.dumps(target_identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    digest.update(hashlib.sha256(salt).digest())
    return digest.hexdigest()


def validate_environment(root: Path) -> dict[str, Any]:
    expected = (root / ".venv" / "Scripts" / "python.exe").resolve()
    actual = Path(sys.executable).resolve()
    if actual != expected:
        raise EnvironmentBlocked(f"W4 requires project venv: {expected}; got {actual}")
    if platform.architecture()[0] != "64bit":
        raise EnvironmentBlocked("W4 requires 64-bit Python.")
    versions = {
        name: importlib.metadata.version(name)
        for name in [
            "duckdb",
            "pyarrow",
            "polars",
            "orjson",
            "lingua-language-detector",
        ]
    }
    return {
        "python_executable": str(actual),
        "python_version": platform.python_version(),
        "python_architecture": platform.architecture()[0],
        "project_venv_in_use": True,
        "duckdb_version": versions["duckdb"],
        "pyarrow_version": versions["pyarrow"],
        "polars_version": versions["polars"],
        "orjson_version": versions["orjson"],
        "lingua_version": versions["lingua-language-detector"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase W4 review extraction and cleaning")
    parser.add_argument(
        "--config", default="config/review_cleaning_rules.toml", help="Project-relative config"
    )
    args = parser.parse_args()
    script_path = Path(__file__).resolve()
    root = script_path.parents[1]
    config_path = resolve_inside(root, args.config)
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    project_config = tomllib.loads(
        (root / "config" / "project.toml").read_text(encoding="utf-8")
    )
    config["_project_paths"] = dict(project_config["paths"])
    work_dir = resolve_inside(root, config["outputs"]["work"])
    reports_dir = resolve_inside(root, config["outputs"]["reports"])
    final_path = resolve_inside(root, config["outputs"]["review_level_base"])
    salt_path = resolve_inside(root, config["outputs"]["private_salt"])
    raw_uncompressed = resolve_inside(
        root, project_config["paths"]["raw_uncompressed"]
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_dir = reports_dir / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    log_path = reports_dir / "w4_execution.log"
    disk_events: list[dict[str, Any]] = []
    initial_free = shutil.disk_usage(root).free
    disk_events.append(
        {
            "time": now_iso(),
            "event": "w4_start",
            "free_bytes": initial_free,
            "free_gib": initial_free / 1024**3,
        }
    )
    minimum_free = int(config["phase"]["minimum_free_gib"]) * 1024**3
    if initial_free < minimum_free:
        atomic_json(
            reports_dir / "w4_status.json",
            {
                "phase": PHASE,
                "status": "PAUSED_SPACE_GATE",
                "reason": "Free space was below 60 GiB before W4 scanning.",
                "updated_at": now_iso(),
                "free_bytes": initial_free,
            },
        )
        return 2

    try:
        environment = validate_environment(root)
        w2_status = json.loads(
            resolve_inside(root, config["inputs"]["w2_status"]).read_text(
                encoding="utf-8"
            )
        )
        w3_status = json.loads(
            resolve_inside(root, config["inputs"]["w3_status"]).read_text(
                encoding="utf-8"
            )
        )
        if w2_status.get("status") != "PASS" or w3_status.get("status") != "PASS":
            raise SourceMismatch("W2 and W3 must both be PASS.")
        targets, target_identity = load_targets(
            resolve_inside(root, config["inputs"]["target_products"])
        )
        source_identities: dict[str, dict[str, Any]] = {}
        raw_before: dict[str, dict[str, Any]] = {}
        for source in config["inputs"]["reviews"]:
            source_path = resolve_inside(raw_uncompressed, source["relative_path"])
            identity = file_identity(source_path)
            source_identities[source["id"]] = identity
            raw_before[source["id"]] = identity
            if (
                identity["size_bytes"] != int(source["expected_bytes"])
                or not identity["readonly"]
            ):
                raise SourceMismatch(f"Raw review identity mismatch: {source['id']}")
        salt, salt_created = load_or_create_salt(salt_path)
        fingerprint = configuration_fingerprint(
            script_path,
            config_path,
            source_identities,
            target_identity,
            salt,
        )
        existing_status_path = reports_dir / "w4_status.json"
        if existing_status_path.exists() and final_path.exists():
            existing = json.loads(existing_status_path.read_text(encoding="utf-8"))
            if (
                existing.get("status") == "PASS"
                and existing.get("configuration_fingerprint") == fingerprint
                and existing.get("review_level_base", {}).get("bytes")
                == final_path.stat().st_size
            ):
                append_log(
                    log_path,
                    "INFO",
                    "Recognized completed W4 output; safe repeat execution skipped.",
                )
                return 0
            raise ReviewExtractionFailed(
                "Existing final output is not a recognized completed output for this configuration."
            )
        marker_path = work_dir / "W4_WORKSPACE.json"
        if work_dir.exists() and not marker_path.exists():
            unknown = list(work_dir.iterdir())
            if unknown:
                raise ReviewExtractionFailed(
                    "W4 work directory contains unknown files and no workspace marker."
                )
        work_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(
            marker_path,
            {
                "phase": PHASE,
                "created_or_verified_at": now_iso(),
                "configuration_fingerprint": fingerprint,
            },
        )
        append_log(
            log_path,
            "INFO",
            f"W4 started; python={environment['python_version']}; "
            f"duckdb={environment['duckdb_version']}; "
            f"pyarrow={environment['pyarrow_version']}; "
            f"lingua={environment['lingua_version']}; free_bytes={initial_free}; "
            f"salt_created={salt_created}; config_version={config['phase']['version']}",
        )
        detector = build_detector(config["language"])
        scan_stats = []
        for source in config["inputs"]["reviews"]:
            scan_stats.append(
                scan_source(
                    source,
                    root=root,
                    raw_uncompressed=raw_uncompressed,
                    targets=targets,
                    salt=salt,
                    detector=detector,
                    config=config,
                    fingerprint=fingerprint,
                    work_dir=work_dir,
                    reports_dir=reports_dir,
                    log_path=log_path,
                    disk_events=disk_events,
                )
            )
        duplicate_summary, language_rows = materialize_final(
            scan_stats,
            root=root,
            work_dir=work_dir,
            final_path=final_path,
            config=config,
            log_path=log_path,
            disk_events=disk_events,
        )
        status = build_reports(
            root=root,
            final_path=final_path,
            targets=targets,
            target_identity=target_identity,
            scan_stats=scan_stats,
            duplicate_summary=duplicate_summary,
            language_rows=language_rows,
            config=config,
            fingerprint=fingerprint,
            reports_dir=reports_dir,
            log_path=log_path,
            disk_events=disk_events,
            environment=environment,
            raw_before=raw_before,
        )
        return 0 if status["status"] == "PASS" else 1
    except SpaceGate as caught:
        status = "PAUSED_SPACE_GATE"
        exit_code = 2
        error_message = f"{type(caught).__name__}: {caught}"
    except EnvironmentBlocked as caught:
        status = "BLOCKED_ENVIRONMENT"
        exit_code = 3
        error_message = f"{type(caught).__name__}: {caught}"
    except SourceMismatch as caught:
        status = "FAILED_SOURCE_MISMATCH"
        exit_code = 4
        error_message = f"{type(caught).__name__}: {caught}"
    except BaseException as caught:
        status = "FAILED_REVIEW_EXTRACTION"
        exit_code = 5
        error_message = f"{type(caught).__name__}: {caught}"
    append_log(log_path, "ERROR", f"W4 stopped with status={status}; error={error_message}")
    atomic_json(
        reports_dir / "w4_status.json",
        {
            "phase": PHASE,
            "status": status,
            "reason": error_message,
            "updated_at": now_iso(),
            "final_free_bytes": shutil.disk_usage(root).free,
            "w5_started": False,
        },
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
