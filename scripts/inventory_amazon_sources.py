from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import orjson


PHASE = "W2"
MINIMUM_FREE_BYTES = 60 * 1024**3
PROGRESS_BYTES = 1024**3
PROGRESS_RECORDS = 5_000_000
MEMORY_SAMPLE_RECORDS = 1_000_000

METADATA_CORE_FIELDS = [
    "parent_asin",
    "title",
    "main_category",
    "categories",
    "features",
    "description",
    "store",
    "details",
    "price",
    "average_rating",
    "rating_number",
]

REVIEW_CORE_FIELDS = [
    "parent_asin",
    "rating",
    "title",
    "text",
    "timestamp",
    "verified_purchase",
    "helpful_vote",
    "asin",
    "user_id",
    "images",
]

FILE_ATTRIBUTE_READONLY = 0x1
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
MIN_DATETIME_MILLISECONDS = -62_135_596_800_000
MAX_DATETIME_MILLISECONDS = 253_402_300_799_999


class EnvironmentBlocked(RuntimeError):
    pass


class SpaceGate(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def resolve_inside(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError(f"Expected project-relative path: {relative}")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path escapes project root: {relative}")
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
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def atomic_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def get_windows_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    attributes = int(get_attributes(str(path)))
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
    process = get_current_process()
    success = get_process_memory_info(
        process,
        ctypes.byref(counters),
        counters.cb,
    )
    if not success:
        return None
    return int(counters.WorkingSetSize)


class W2Logger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, message: str) -> None:
        safe = " ".join(str(message).splitlines())
        line = f"[{now_iso()}] [{level}] {safe}\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()


def load_context() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    project_toml = project_root / "config" / "project.toml"
    dataset_config = project_root / "config" / "amazon_w1_files.json"

    with project_toml.open("rb") as handle:
        config = tomllib.load(handle)
    datasets = json.loads(dataset_config.read_text(encoding="utf-8"))
    if len(datasets) != 4:
        raise ValueError(f"Expected four configured sources, found {len(datasets)}")

    raw_uncompressed = resolve_inside(
        project_root,
        config["paths"]["raw_uncompressed"],
    )
    reports_root = resolve_inside(project_root, config["paths"]["reports"])
    reports_w2 = reports_root / "w2"
    checkpoints = reports_w2 / "checkpoints"
    fingerprint = hashlib.sha256()
    for fingerprint_path in (project_toml, dataset_config, Path(__file__).resolve()):
        fingerprint.update(str(fingerprint_path.relative_to(project_root)).encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(fingerprint_path.read_bytes())
        fingerprint.update(b"\0")
    fingerprint.update(orjson.__version__.encode("ascii"))

    sources: list[dict[str, Any]] = []
    for item in datasets:
        relative = item["uncompressed_relative"]
        source_path = raw_uncompressed / Path(relative)
        if source_path.suffix.lower() != ".jsonl" or source_path.name.lower().endswith(
            ".jsonl.gz"
        ):
            raise ValueError(f"Refusing non-JSONL W2 source path: {relative}")
        sources.append(
            {
                "id": item["id"],
                "record_type": item["record_type"],
                "domain": item["domain"],
                "relative_path": relative.replace("\\", "/"),
                "path": source_path,
                "core_fields": (
                    METADATA_CORE_FIELDS
                    if item["record_type"] == "metadata"
                    else REVIEW_CORE_FIELDS
                ),
            }
        )

    return {
        "project_root": project_root,
        "project_toml": project_toml,
        "dataset_config": dataset_config,
        "raw_uncompressed": raw_uncompressed,
        "reports_w2": reports_w2,
        "checkpoints": checkpoints,
        "configuration_fingerprint": fingerprint.hexdigest(),
        "sources": sources,
    }


def environment_summary(context: dict[str, Any]) -> dict[str, Any]:
    expected_python = (
        context["project_root"] / ".venv" / "Scripts" / "python.exe"
    ).resolve()
    actual_python = Path(sys.executable).resolve()
    summary = {
        "python_executable": str(actual_python),
        "expected_python_executable": str(expected_python),
        "project_venv_in_use": actual_python == expected_python,
        "python_version": platform.python_version(),
        "python_64_bit": struct.calcsize("P") * 8 == 64,
        "architecture": platform.architecture()[0],
        "orjson_version": orjson.__version__,
    }
    if not summary["project_venv_in_use"]:
        raise EnvironmentBlocked(
            f"W2 must run under the project .venv; actual={actual_python}"
        )
    if not summary["python_64_bit"]:
        raise EnvironmentBlocked("W2 requires a 64-bit Python interpreter.")
    return summary


def free_bytes(project_root: Path) -> int:
    return int(shutil.disk_usage(project_root).free)


def input_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime,
            timezone.utc,
        ).isoformat(),
        "readonly": is_readonly(path),
    }


def checkpoint_matches(
    checkpoint: dict[str, Any],
    identity: dict[str, Any],
    configuration_fingerprint: str,
) -> bool:
    recorded = checkpoint.get("input_identity", {})
    return (
        checkpoint.get("status") == "COMPLETE"
        and checkpoint.get("configuration_fingerprint")
        == configuration_fingerprint
        and recorded.get("path") == identity["path"]
        and recorded.get("size_bytes") == identity["size_bytes"]
        and recorded.get("mtime_ns") == identity["mtime_ns"]
        and identity["readonly"]
    )


def new_field_stat() -> dict[str, Any]:
    return {
        "occurrence_count": 0,
        "type_counts": Counter(),
        "empty_string_count": 0,
    }


def update_peak_memory(current_peak: int | None) -> int | None:
    current = process_rss_bytes()
    if current is None:
        return current_peak
    if current_peak is None:
        return current
    return max(current_peak, current)


def timestamp_state() -> dict[str, Any]:
    return {
        "field_present_count": 0,
        "type_counts": Counter(),
        "null_count": 0,
        "non_numeric_count": 0,
        "numeric_count": 0,
        "negative_count": 0,
        "numeric_min": None,
        "numeric_max": None,
        "magnitude_counts": Counter(),
        "milliseconds_unconvertible_count": 0,
    }


def update_timestamp(state: dict[str, Any], value: Any) -> None:
    state["field_present_count"] += 1
    value_type = json_type(value)
    state["type_counts"][value_type] += 1

    if value is None:
        state["null_count"] += 1
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        state["non_numeric_count"] += 1
        return
    if isinstance(value, float) and not math.isfinite(value):
        state["non_numeric_count"] += 1
        return

    numeric = float(value) if isinstance(value, float) else int(value)
    state["numeric_count"] += 1
    if (
        numeric < MIN_DATETIME_MILLISECONDS
        or numeric > MAX_DATETIME_MILLISECONDS
    ):
        state["milliseconds_unconvertible_count"] += 1
    if state["numeric_min"] is None or numeric < state["numeric_min"]:
        state["numeric_min"] = numeric
    if state["numeric_max"] is None or numeric > state["numeric_max"]:
        state["numeric_max"] = numeric

    if numeric < 0:
        state["negative_count"] += 1
        state["magnitude_counts"]["negative"] += 1
    elif 100_000_000 <= numeric < 100_000_000_000:
        state["magnitude_counts"]["seconds"] += 1
    elif 100_000_000_000 <= numeric < 100_000_000_000_000:
        state["magnitude_counts"]["milliseconds"] += 1
    elif 100_000_000_000_000 <= numeric < 100_000_000_000_000_000:
        state["magnitude_counts"]["microseconds"] += 1
    else:
        state["magnitude_counts"]["other"] += 1


def finish_timestamp(
    state: dict[str, Any],
    object_count: int,
) -> dict[str, Any]:
    result = {
        "field_present_count": state["field_present_count"],
        "field_missing_count": object_count - state["field_present_count"],
        "type_counts": dict(sorted(state["type_counts"].items())),
        "null_count": state["null_count"],
        "non_numeric_count": state["non_numeric_count"],
        "numeric_count": state["numeric_count"],
        "negative_count": state["negative_count"],
        "numeric_min": state["numeric_min"],
        "numeric_max": state["numeric_max"],
        "magnitude_counts": dict(sorted(state["magnitude_counts"].items())),
        "unit": None,
        "unit_status": "TIMESTAMP_UNIT_UNCERTAIN",
        "unconvertible_count": None,
        "milliseconds_unconvertible_count": state[
            "milliseconds_unconvertible_count"
        ],
        "min_utc": None,
        "max_utc": None,
    }

    nonnegative_count = (
        state["numeric_count"] - state["negative_count"]
    )
    milliseconds_count = state["magnitude_counts"].get("milliseconds", 0)
    if nonnegative_count > 0 and milliseconds_count == nonnegative_count:
        result["unit"] = "milliseconds"
        result["unit_status"] = "CONFIRMED_MILLISECONDS"
        unconvertible = state["milliseconds_unconvertible_count"]
        for key, destination in (
            ("numeric_min", "min_utc"),
            ("numeric_max", "max_utc"),
        ):
            value = state[key]
            if value is None:
                continue
            try:
                result[destination] = datetime.fromtimestamp(
                    float(value) / 1000.0,
                    timezone.utc,
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                result[destination] = None
        result["unconvertible_count"] = unconvertible
    return result


def write_error_location(
    handle: BinaryIO,
    *,
    file_id: str,
    line_number: int,
    byte_offset: int,
    category: str,
) -> None:
    payload = {
        "file_id": file_id,
        "line_number": line_number,
        "byte_offset": byte_offset,
        "error_category": category,
    }
    handle.write(orjson.dumps(payload))
    handle.write(b"\n")


def scan_source(
    source: dict[str, Any],
    context: dict[str, Any],
    logger: W2Logger,
) -> dict[str, Any]:
    path: Path = source["path"]
    identity = input_identity(path)
    if not identity["readonly"]:
        raise RuntimeError(f"Raw source is not read-only: {source['relative_path']}")
    if free_bytes(context["project_root"]) < MINIMUM_FREE_BYTES:
        raise SpaceGate("Free space is below the 60 GiB W2 floor.")

    start_time = time.perf_counter()
    started_at = now_iso()
    start_free = free_bytes(context["project_root"])
    field_stats: dict[str, dict[str, Any]] = {}
    error_categories: Counter[str] = Counter()
    timestamp = timestamp_state() if source["record_type"] == "reviews" else None

    physical_line_count = 0
    nonempty_record_count = 0
    empty_line_count = 0
    parse_success_count = 0
    parse_error_count = 0
    object_count = 0
    non_object_count = 0
    object_field_count_min: int | None = None
    object_field_count_max: int | None = None
    object_field_count_sum = 0
    offset = 0
    bytes_processed = 0
    peak_rss = update_peak_memory(None)
    next_progress_bytes = PROGRESS_BYTES
    next_progress_records = PROGRESS_RECORDS
    next_memory_records = MEMORY_SAMPLE_RECORDS

    error_details = (
        context["checkpoints"] / f"{source['id']}_parse_errors.jsonl"
    )
    error_details.parent.mkdir(parents=True, exist_ok=True)
    logger.write(
        "INFO",
        (
            f"Full serial scan started: id={source['id']}; "
            f"bytes={identity['size_bytes']}; free_bytes={start_free}"
        ),
    )

    with path.open("rb", buffering=1024 * 1024) as raw_handle, error_details.open(
        "wb"
    ) as error_handle:
        for line in raw_handle:
            physical_line_count += 1
            line_offset = offset
            line_length = len(line)
            offset += line_length
            bytes_processed += line_length

            if not line or line.isspace():
                empty_line_count += 1
                continue

            nonempty_record_count += 1
            try:
                value = orjson.loads(line)
            except orjson.JSONDecodeError:
                parse_error_count += 1
                category = "JSONDecodeError"
                error_categories[category] += 1
                write_error_location(
                    error_handle,
                    file_id=source["id"],
                    line_number=physical_line_count,
                    byte_offset=line_offset,
                    category=category,
                )
                continue
            except Exception as exc:
                parse_error_count += 1
                category = type(exc).__name__
                error_categories[category] += 1
                write_error_location(
                    error_handle,
                    file_id=source["id"],
                    line_number=physical_line_count,
                    byte_offset=line_offset,
                    category=category,
                )
                continue

            parse_success_count += 1
            if not isinstance(value, dict):
                non_object_count += 1
                continue

            object_count += 1
            field_count = len(value)
            object_field_count_sum += field_count
            if object_field_count_min is None or field_count < object_field_count_min:
                object_field_count_min = field_count
            if object_field_count_max is None or field_count > object_field_count_max:
                object_field_count_max = field_count

            for key, field_value in value.items():
                stat = field_stats.get(key)
                if stat is None:
                    stat = new_field_stat()
                    field_stats[key] = stat
                stat["occurrence_count"] += 1
                stat["type_counts"][json_type(field_value)] += 1
                if isinstance(field_value, str) and field_value == "":
                    stat["empty_string_count"] += 1

            if timestamp is not None and "timestamp" in value:
                update_timestamp(timestamp, value["timestamp"])

            if nonempty_record_count >= next_memory_records:
                peak_rss = update_peak_memory(peak_rss)
                next_memory_records += MEMORY_SAMPLE_RECORDS

            if (
                bytes_processed >= next_progress_bytes
                or nonempty_record_count >= next_progress_records
            ):
                current_free = free_bytes(context["project_root"])
                if current_free < MINIMUM_FREE_BYTES:
                    raise SpaceGate(
                        f"Free space fell below 60 GiB during {source['id']}."
                    )
                elapsed = max(time.perf_counter() - start_time, 0.001)
                logger.write(
                    "INFO",
                    (
                        f"Scan progress: id={source['id']}; "
                        f"records={nonempty_record_count}; "
                        f"bytes={bytes_processed}; "
                        f"bytes_per_second={int(bytes_processed / elapsed)}; "
                        f"free_bytes={current_free}"
                    ),
                )
                while bytes_processed >= next_progress_bytes:
                    next_progress_bytes += PROGRESS_BYTES
                while nonempty_record_count >= next_progress_records:
                    next_progress_records += PROGRESS_RECORDS

        error_handle.flush()
        os.fsync(error_handle.fileno())

    peak_rss = update_peak_memory(peak_rss)
    finished_at = now_iso()
    duration_seconds = time.perf_counter() - start_time
    end_free = free_bytes(context["project_root"])

    completed_field_stats: dict[str, Any] = {}
    observed_fields = sorted(field_stats)
    all_reported_fields = sorted(set(observed_fields).union(source["core_fields"]))
    for field in all_reported_fields:
        stat = field_stats.get(field, new_field_stat())
        occurrence = int(stat["occurrence_count"])
        key_absent = object_count - occurrence
        null_count = int(stat["type_counts"].get("null", 0))
        empty_string_count = int(stat["empty_string_count"])
        effective_missing = key_absent + null_count + empty_string_count
        completed_field_stats[field] = {
            "observed": field in field_stats,
            "is_core_field": field in source["core_fields"],
            "occurrence_count": occurrence,
            "key_absent_count": key_absent,
            "null_count": null_count,
            "empty_string_count": empty_string_count,
            "effective_missing_count": effective_missing,
            "key_absent_rate": (
                key_absent / object_count if object_count else None
            ),
            "effective_missing_rate": (
                effective_missing / object_count if object_count else None
            ),
            "type_counts": dict(sorted(stat["type_counts"].items())),
        }

    reconciliation = {
        "physical_equals_empty_plus_nonempty": (
            physical_line_count == empty_line_count + nonempty_record_count
        ),
        "nonempty_equals_success_plus_errors": (
            nonempty_record_count == parse_success_count + parse_error_count
        ),
        "success_equals_objects_plus_nonobjects": (
            parse_success_count == object_count + non_object_count
        ),
    }

    result = {
        "phase": PHASE,
        "status": "COMPLETE",
        "configuration_fingerprint": context["configuration_fingerprint"],
        "id": source["id"],
        "record_type": source["record_type"],
        "domain": source["domain"],
        "relative_path": source["relative_path"],
        "input_identity": identity,
        "scan_started_at": started_at,
        "scan_finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "start_free_bytes": start_free,
        "end_free_bytes": end_free,
        "peak_process_rss_bytes": peak_rss,
        "physical_line_count": physical_line_count,
        "exact_nonempty_record_count": nonempty_record_count,
        "empty_line_count": empty_line_count,
        "json_parse_success_count": parse_success_count,
        "json_parse_error_count": parse_error_count,
        "json_object_count": object_count,
        "non_object_json_count": non_object_count,
        "reconciliation": reconciliation,
        "observed_field_count": len(observed_fields),
        "observed_fields": observed_fields,
        "object_field_count_min": object_field_count_min,
        "object_field_count_max": object_field_count_max,
        "object_field_count_average": (
            object_field_count_sum / object_count if object_count else None
        ),
        "field_statistics": completed_field_stats,
        "parse_error_categories": dict(sorted(error_categories.items())),
        "parse_error_details_relative_path": str(
            error_details.relative_to(context["project_root"])
        ).replace("\\", "/"),
        "timestamp_audit": (
            finish_timestamp(timestamp, object_count)
            if timestamp is not None
            else None
        ),
    }

    logger.write(
        "INFO",
        (
            f"Full serial scan completed: id={source['id']}; "
            f"records={nonempty_record_count}; errors={parse_error_count}; "
            f"objects={object_count}; seconds={duration_seconds:.3f}; "
            f"peak_rss_bytes={peak_rss}; free_bytes={end_free}"
        ),
    )
    return result


def source_inventory_rows(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summaries:
        parent = item["field_statistics"].get("parent_asin", {})
        timestamp = item.get("timestamp_audit") or {}
        rows.append(
            {
                "id": item["id"],
                "file": item["relative_path"],
                "record_type": item["record_type"],
                "domain": item["domain"],
                "file_bytes": item["input_identity"]["size_bytes"],
                "exact_nonempty_records": item["exact_nonempty_record_count"],
                "json_parse_success": item["json_parse_success_count"],
                "json_parse_errors": item["json_parse_error_count"],
                "empty_lines": item["empty_line_count"],
                "non_object_json": item["non_object_json_count"],
                "observed_fields": item["observed_field_count"],
                "parent_asin_key_absent": parent.get("key_absent_count"),
                "parent_asin_effective_missing": parent.get(
                    "effective_missing_count"
                ),
                "timestamp_unit": timestamp.get("unit"),
                "timestamp_min_utc": timestamp.get("min_utc"),
                "timestamp_max_utc": timestamp.get("max_utc"),
                "duration_seconds": round(item["duration_seconds"], 3),
                "peak_process_rss_bytes": item["peak_process_rss_bytes"],
                "readonly": item["input_identity"]["readonly"],
            }
        )
    return rows


def field_statistic_rows(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summaries:
        denominator = item["json_object_count"]
        for field, stat in item["field_statistics"].items():
            rows.append(
                {
                    "file_id": item["id"],
                    "record_type": item["record_type"],
                    "domain": item["domain"],
                    "field": field,
                    "observed": stat["observed"],
                    "is_core_field": stat["is_core_field"],
                    "object_denominator": denominator,
                    "occurrence_count": stat["occurrence_count"],
                    "key_absent_count": stat["key_absent_count"],
                    "null_count": stat["null_count"],
                    "empty_string_count": stat["empty_string_count"],
                    "effective_missing_count": stat["effective_missing_count"],
                    "key_absent_rate": stat["key_absent_rate"],
                    "effective_missing_rate": stat["effective_missing_rate"],
                    "type_counts_json": json.dumps(
                        stat["type_counts"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
    return rows


def compare_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    core_fields: list[str],
) -> dict[str, Any]:
    left_fields = set(left["observed_fields"])
    right_fields = set(right["observed_fields"])
    shared = sorted(left_fields.intersection(right_fields))
    type_differences = []
    for field in shared:
        left_types = sorted(
            left["field_statistics"][field]["type_counts"]
        )
        right_types = sorted(
            right["field_statistics"][field]["type_counts"]
        )
        if left_types != right_types:
            type_differences.append(
                {
                    "field": field,
                    left["domain"]: left_types,
                    right["domain"]: right_types,
                }
            )

    missing_comparison = []
    for field in core_fields:
        left_stat = left["field_statistics"][field]
        right_stat = right["field_statistics"][field]
        missing_comparison.append(
            {
                "field": field,
                left["domain"]: {
                    "effective_missing_count": left_stat[
                        "effective_missing_count"
                    ],
                    "effective_missing_rate": left_stat[
                        "effective_missing_rate"
                    ],
                },
                right["domain"]: {
                    "effective_missing_count": right_stat[
                        "effective_missing_count"
                    ],
                    "effective_missing_rate": right_stat[
                        "effective_missing_rate"
                    ],
                },
            }
        )

    return {
        "record_type": left["record_type"],
        "shared_fields": shared,
        f"only_{left['domain']}": sorted(left_fields - right_fields),
        f"only_{right['domain']}": sorted(right_fields - left_fields),
        "type_differences": type_differences,
        "core_field_missingness": missing_comparison,
    }


def schema_comparison(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {item["id"]: item for item in summaries}
    return {
        "phase": PHASE,
        "generated_at": now_iso(),
        "metadata": compare_pair(
            by_id["meta_electronics"],
            by_id["meta_home_and_kitchen"],
            METADATA_CORE_FIELDS,
        ),
        "reviews": compare_pair(
            by_id["reviews_electronics"],
            by_id["reviews_home_and_kitchen"],
            REVIEW_CORE_FIELDS,
        ),
    }


def inventory_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Amazon Reviews 2023 Source Inventory — Phase W2",
        "",
        "This report contains aggregate source statistics only. It does not "
        "contain review text, user IDs, product text, or other raw field values.",
        "",
        "| File | Type | Exact non-empty records | Parsed | Errors | Empty | "
        "Non-object | Bytes | Fields | parent_asin effective missing | "
        "Timestamp range (UTC) | Seconds |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for row in rows:
        timestamp_range = "N/A"
        if row["timestamp_min_utc"] or row["timestamp_max_utc"]:
            timestamp_range = (
                f"{row['timestamp_min_utc']} — {row['timestamp_max_utc']}"
            )
        lines.append(
            "| {file} | {record_type} | {exact_nonempty_records:,} | "
            "{json_parse_success:,} | {json_parse_errors:,} | "
            "{empty_lines:,} | {non_object_json:,} | {file_bytes:,} | "
            "{observed_fields:,} | {parent_missing:,} | {timestamp_range} | "
            "{duration_seconds:,.3f} |".format(
                file=row["file"],
                record_type=row["record_type"],
                exact_nonempty_records=row["exact_nonempty_records"],
                json_parse_success=row["json_parse_success"],
                json_parse_errors=row["json_parse_errors"],
                empty_lines=row["empty_lines"],
                non_object_json=row["non_object_json"],
                file_bytes=row["file_bytes"],
                observed_fields=row["observed_fields"],
                parent_missing=(
                    row["parent_asin_effective_missing"]
                    if row["parent_asin_effective_missing"] is not None
                    else 0
                ),
                timestamp_range=timestamp_range,
                duration_seconds=row["duration_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "Record counts are exact for non-empty physical JSONL lines. "
            "Missingness uses parsed top-level JSON objects as the denominator.",
            "",
        ]
    )
    return "\n".join(lines)


def prohibited_outputs(project_root: Path) -> list[str]:
    data_root = project_root / "data" / "amazon_reviews_2023"
    hits = []
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if lower.endswith(".parquet") or lower.endswith(".duckdb") or lower.endswith(
            ".duckdb.wal"
        ):
            hits.append(str(path.relative_to(project_root)).replace("\\", "/"))
    return sorted(hits)


def generate_reports(
    context: dict[str, Any],
    summaries: list[dict[str, Any]],
    environment: dict[str, Any],
    disk_events: list[dict[str, Any]],
    logger: W2Logger,
) -> dict[str, Any]:
    reports = context["reports_w2"]
    rows = source_inventory_rows(summaries)
    fields = field_statistic_rows(summaries)
    comparison = schema_comparison(summaries)
    timestamp_report = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "files": [
            {
                "id": item["id"],
                "domain": item["domain"],
                **item["timestamp_audit"],
            }
            for item in summaries
            if item["record_type"] == "reviews"
        ],
    }
    parse_report = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "total_parse_errors": sum(
            item["json_parse_error_count"] for item in summaries
        ),
        "files": [
            {
                "id": item["id"],
                "parse_error_count": item["json_parse_error_count"],
                "error_categories": item["parse_error_categories"],
                "error_details_relative_path": item[
                    "parse_error_details_relative_path"
                ],
            }
            for item in summaries
        ],
    }
    inventory = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "environment": environment,
        "privacy": (
            "Aggregate schema, count, missingness, error-location, and timestamp "
            "statistics only; no raw text or identifier values are retained."
        ),
        "files": summaries,
    }

    atomic_json(reports / "source_inventory.json", inventory)
    atomic_csv(reports / "source_inventory.csv", rows)
    atomic_text(reports / "source_inventory.md", inventory_markdown(rows))
    atomic_json(reports / "schema_comparison.json", comparison)
    atomic_csv(reports / "field_statistics.csv", fields)
    atomic_json(reports / "parse_errors_summary.json", parse_report)
    atomic_json(reports / "timestamp_audit.json", timestamp_report)

    final_free = free_bytes(context["project_root"])
    disk_events.append(
        {
            "time": now_iso(),
            "event": "w2_complete",
            "free_bytes": final_free,
            "free_gib": final_free / 1024**3,
        }
    )
    disk_report = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "minimum_free_bytes": MINIMUM_FREE_BYTES,
        "events": disk_events,
    }
    atomic_json(reports / "w2_disk_usage.json", disk_report)

    expected_reports = [
        "w2_execution.log",
        "source_inventory.json",
        "source_inventory.csv",
        "source_inventory.md",
        "schema_comparison.json",
        "field_statistics.csv",
        "parse_errors_summary.json",
        "timestamp_audit.json",
        "w2_disk_usage.json",
    ]
    report_presence = {
        name: (reports / name).is_file() for name in expected_reports
    }
    raw_readonly = all(
        is_readonly(source["path"]) for source in context["sources"]
    )
    prohibited = prohibited_outputs(context["project_root"])
    all_reconciled = all(
        all(item["reconciliation"].values()) for item in summaries
    )
    timestamp_confirmed = all(
        item["timestamp_audit"]["unit_status"] == "CONFIRMED_MILLISECONDS"
        for item in summaries
        if item["record_type"] == "reviews"
    )
    criteria = {
        "project_venv_environment_valid": (
            environment["project_venv_in_use"]
            and environment["python_64_bit"]
        ),
        "four_jsonl_full_scans_complete": len(summaries) == 4
        and all(item["status"] == "COMPLETE" for item in summaries),
        "four_exact_record_counts_available": len(summaries) == 4
        and all(
            isinstance(item["exact_nonempty_record_count"], int)
            for item in summaries
        ),
        "record_count_reconciliation_passed": all_reconciled,
        "field_and_type_statistics_generated": len(fields) > 0,
        "core_field_missingness_generated": all(
            all(
                field in item["field_statistics"]
                for field in (
                    METADATA_CORE_FIELDS
                    if item["record_type"] == "metadata"
                    else REVIEW_CORE_FIELDS
                )
            )
            for item in summaries
        ),
        "review_timestamp_units_confirmed": timestamp_confirmed,
        "schema_comparison_generated": (reports / "schema_comparison.json").is_file(),
        "all_raw_jsonl_remain_readonly": raw_readonly,
        "final_free_space_at_least_60_gib": final_free >= MINIMUM_FREE_BYTES,
        "no_compressed_json_scan_performed": True,
        "no_parquet_or_duckdb_created": not prohibited,
        "w3_not_executed": True,
        "required_reports_present": all(report_presence.values()),
    }
    passed = all(criteria.values())
    status = {
        "phase": PHASE,
        "status": "PASS" if passed else "FAILED_SOURCE_INVENTORY",
        "reason": (
            "All W2 acceptance criteria passed."
            if passed
            else "One or more W2 acceptance criteria did not pass."
        ),
        "updated_at": now_iso(),
        "environment": environment,
        "criteria": criteria,
        "report_presence": report_presence,
        "final_free_bytes": final_free,
        "final_free_gib": final_free / 1024**3,
        "prohibited_outputs_found": prohibited,
        "policy_attestation": {
            "gzip_open_used": False,
            "compressed_archives_opened": False,
            "pandas_full_load_used": False,
            "parquet_created": False,
            "duckdb_database_created": False,
            "product_screening_performed": False,
            "metadata_review_join_performed": False,
            "cleaning_or_deduplication_performed": False,
            "annotation_or_modeling_performed": False,
            "w3_started": False,
        },
    }
    atomic_json(reports / "w2_status.json", status)
    logger.write("INFO", f"W2 finished with status={status['status']}.")
    return status


def write_failure_status(
    context: dict[str, Any],
    *,
    status: str,
    reason: str,
    environment: dict[str, Any] | None,
    completed_ids: list[str],
) -> None:
    document = {
        "phase": PHASE,
        "status": status,
        "reason": reason,
        "updated_at": now_iso(),
        "environment": environment,
        "completed_file_ids": completed_ids,
        "free_bytes": free_bytes(context["project_root"]),
        "w3_started": False,
    }
    atomic_json(context["reports_w2"] / "w2_status.json", document)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Full read-only, serial W2 inventory of uncompressed JSONL."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate environment, configured paths, read-only state, and disk only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    context = load_context()
    context["reports_w2"].mkdir(parents=True, exist_ok=True)
    context["checkpoints"].mkdir(parents=True, exist_ok=True)
    logger = W2Logger(context["reports_w2"] / "w2_execution.log")
    environment: dict[str, Any] | None = None
    completed: list[dict[str, Any]] = []
    disk_events: list[dict[str, Any]] = []

    try:
        environment = environment_summary(context)
        initial_free = free_bytes(context["project_root"])
        if initial_free < MINIMUM_FREE_BYTES:
            raise SpaceGate("W2 start free space is below 60 GiB.")

        identities = []
        for source in context["sources"]:
            if not source["path"].is_file():
                raise FileNotFoundError(f"Missing W2 source: {source['relative_path']}")
            identity = input_identity(source["path"])
            if not identity["readonly"]:
                raise RuntimeError(
                    f"W2 source is not read-only: {source['relative_path']}"
                )
            identities.append(
                {
                    "id": source["id"],
                    "relative_path": source["relative_path"],
                    **identity,
                }
            )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "phase": PHASE,
                        "dry_run": "PASS",
                        "environment": environment,
                        "free_bytes": initial_free,
                        "sources": identities,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        logger.write(
            "INFO",
            (
                f"W2 started; python={environment['python_version']}; "
                f"orjson={environment['orjson_version']}; "
                f"free_bytes={initial_free}"
            ),
        )
        disk_events.append(
            {
                "time": now_iso(),
                "event": "w2_start",
                "free_bytes": initial_free,
                "free_gib": initial_free / 1024**3,
            }
        )

        for source in context["sources"]:
            checkpoint_path = context["checkpoints"] / f"{source['id']}.json"
            identity = input_identity(source["path"])
            summary = None
            if checkpoint_path.is_file():
                checkpoint = json.loads(
                    checkpoint_path.read_text(encoding="utf-8-sig")
                )
                if checkpoint_matches(
                    checkpoint,
                    identity,
                    context["configuration_fingerprint"],
                ):
                    summary = checkpoint
                    logger.write(
                        "INFO",
                        f"Checkpoint reused; full scan skipped: id={source['id']}",
                    )
                    disk_events.append(
                        {
                            "time": now_iso(),
                            "event": "scan_skipped_checkpoint",
                            "file_id": source["id"],
                            "free_bytes": free_bytes(context["project_root"]),
                        }
                    )

            if summary is None:
                before = free_bytes(context["project_root"])
                disk_events.append(
                    {
                        "time": now_iso(),
                        "event": "scan_start",
                        "file_id": source["id"],
                        "free_bytes": before,
                        "free_gib": before / 1024**3,
                    }
                )
                summary = scan_source(source, context, logger)
                atomic_json(checkpoint_path, summary)
                disk_events.append(
                    {
                        "time": now_iso(),
                        "event": "scan_complete",
                        "file_id": source["id"],
                        "free_bytes": free_bytes(context["project_root"]),
                        "free_gib": free_bytes(context["project_root"]) / 1024**3,
                        "records": summary["exact_nonempty_record_count"],
                        "parse_errors": summary["json_parse_error_count"],
                        "duration_seconds": summary["duration_seconds"],
                    }
                )
            completed.append(summary)

        status = generate_reports(
            context,
            completed,
            environment,
            disk_events,
            logger,
        )
        return 0 if status["status"] == "PASS" else 1
    except EnvironmentBlocked as exc:
        write_failure_status(
            context,
            status="BLOCKED_ENVIRONMENT",
            reason=str(exc),
            environment=environment,
            completed_ids=[item["id"] for item in completed],
        )
        logger.write("ERROR", str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 10
    except SpaceGate as exc:
        write_failure_status(
            context,
            status="PAUSED_SPACE_GATE",
            reason=str(exc),
            environment=environment,
            completed_ids=[item["id"] for item in completed],
        )
        logger.write("WARN", str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 20
    except Exception as exc:
        write_failure_status(
            context,
            status="FAILED_SOURCE_INVENTORY",
            reason=f"{type(exc).__name__}: {exc}",
            environment=environment,
            completed_ids=[item["id"] for item in completed],
        )
        logger.write("ERROR", f"{type(exc).__name__}: {exc}")
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
