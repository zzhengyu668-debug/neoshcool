from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import struct
import sys
import time
import tomllib
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import orjson
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


PHASE = "W3"
MINIMUM_FREE_BYTES = 60 * 1024**3
PROGRESS_BYTES = 1024**3
PROGRESS_RECORDS = 500_000
FILE_ATTRIBUTE_READONLY = 0x1
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

CORE_METADATA_FIELDS = [
    "parent_asin",
    "main_category",
    "title",
    "categories",
    "features",
    "description",
]
AUXILIARY_METADATA_FIELDS = ["store", "details", "price"]
MANAGEMENT_METADATA_FIELDS = ["average_rating", "rating_number"]
APPROVED_METADATA_FIELDS = (
    CORE_METADATA_FIELDS + AUXILIARY_METADATA_FIELDS + MANAGEMENT_METADATA_FIELDS
)
IDENTITY_FIELDS = ["title", "categories", "features", "description"]
DEVICE_TYPES = ["smart_plug", "smart_bulb", "smart_switch"]


class SpaceGate(RuntimeError):
    pass


class SourceMismatch(RuntimeError):
    pass


class EnvironmentBlocked(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


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


def resolve_inside(root: Path, relative: str | Path) -> Path:
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Configured path escapes the project root: {relative}") from exc
    return candidate


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
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        return None
    return int(counters.WorkingSetSize)


def update_peak_memory(current_peak: int | None) -> int | None:
    current = process_rss_bytes()
    if current is None:
        return current_peak
    if current_peak is None:
        return current
    return max(current_peak, current)


class W3Logger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, message: str) -> None:
        safe = " ".join(str(message).splitlines())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] [{level}] {safe}\n")
            handle.flush()


def free_bytes(project_root: Path) -> int:
    return int(shutil.disk_usage(project_root).free)


def input_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "readonly": is_readonly(path),
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
        "duckdb_version": duckdb.__version__,
        "pyarrow_version": pa.__version__,
        "polars_version": pl.__version__,
        "orjson_version": orjson.__version__,
    }
    if not summary["project_venv_in_use"]:
        raise EnvironmentBlocked(
            f"W3 must use the project .venv; actual={actual_python}"
        )
    if not summary["python_64_bit"]:
        raise EnvironmentBlocked("W3 requires a 64-bit Python interpreter.")
    return summary


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    return str(value)


def json_string(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode("utf-8")


def has_term(text: str, term: str) -> bool:
    return f" {term} " in f" {text} "


def matching_terms(text: str, terms: Iterable[str]) -> list[str]:
    return sorted({term for term in terms if has_term(text, term)})


def is_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def canonical_content_fingerprint(record: dict[str, Any]) -> str:
    selected = {field: record.get(field) for field in APPROVED_METADATA_FIELDS}
    payload = orjson.dumps(selected, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(payload).hexdigest()


def compile_rules(raw_rules: dict[str, Any]) -> dict[str, Any]:
    compiled = json.loads(json.dumps(raw_rules))
    compiled["smart_functions"]["terms"] = [
        normalize_text(term) for term in raw_rules["smart_functions"]["terms"]
    ]
    for device_type in DEVICE_TYPES:
        device = compiled["devices"][device_type]
        for key in (
            "terms",
            "strong_identity_terms",
            "eligibility_identity_terms",
            "wrong_product_terms",
        ):
            device[key] = [normalize_text(term) for term in device[key]]
    for key in ("accessory_terms", "accessory_relation_terms", "non_smart_terms"):
        compiled["exclusions"][key] = [
            normalize_text(term) for term in raw_rules["exclusions"][key]
        ]
    compiled["matching"]["conditional_device_terms"] = [
        normalize_text(term)
        for term in raw_rules["matching"]["conditional_device_terms"]
    ]
    compiled["matching"]["identity_gate_fields"] = list(
        raw_rules["matching"]["identity_gate_fields"]
    )
    for device_type in DEVICE_TYPES:
        required_context_terms = raw_rules["devices"][device_type].get(
            "required_context_terms",
            [],
        )
        compiled["devices"][device_type]["required_context_terms"] = [
            normalize_text(term) for term in required_context_terms
        ]
    return compiled


def analyze_candidate(
    record: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any] | None:
    field_texts = {
        field: normalize_text(flatten_text(record.get(field)))
        for field in IDENTITY_FIELDS
    }
    smart_by_field = {
        field: matching_terms(text, rules["smart_functions"]["terms"])
        for field, text in field_texts.items()
    }
    all_smart_terms = sorted(
        {term for terms in smart_by_field.values() for term in terms}
    )
    if not all_smart_terms:
        return None

    device_terms_by_type: dict[str, dict[str, list[str]]] = {}
    candidate_types: list[str] = []
    generic_switch_fields = set(rules["matching"]["generic_switch_fields"])
    for device_type in DEVICE_TYPES:
        terms = rules["devices"][device_type]["terms"]
        matches: dict[str, list[str]] = {}
        for field, text in field_texts.items():
            field_terms = matching_terms(text, terms)
            if device_type == "smart_switch" and field not in generic_switch_fields:
                field_terms = [term for term in field_terms if term != "switch"]
            matches[field] = field_terms
        device_terms_by_type[device_type] = matches
        if any(matches.values()):
            candidate_types.append(device_type)
    if not candidate_types:
        return None

    title_text = field_texts["title"]
    category_text = field_texts["categories"]
    title_category = f"{title_text} {category_text}".strip()
    primary_fields = set(rules["matching"]["primary_evidence_fields"])
    identity_gate_fields = set(rules["matching"]["identity_gate_fields"])
    primary_smart_terms = sorted(
        {
            term
            for field, terms in smart_by_field.items()
            if field in primary_fields
            for term in terms
        }
    )
    conditional_terms = set(rules["matching"]["conditional_device_terms"])
    global_exclusions: list[str] = []

    non_smart_hits = matching_terms(
        title_text,
        rules["exclusions"]["non_smart_terms"],
    )
    for term in non_smart_hits:
        global_exclusions.append(f"global:explicit_non_smart:{term}")

    accessory_hits = matching_terms(
        title_text,
        rules["exclusions"]["accessory_terms"],
    )
    relation_hits = matching_terms(
        title_text,
        rules["exclusions"]["accessory_relation_terms"],
    )
    strong_title_hits = []
    for device_type in candidate_types:
        strong_title_hits.extend(
            matching_terms(
                title_text,
                rules["devices"][device_type]["strong_identity_terms"],
            )
        )
    if accessory_hits and (relation_hits or not strong_title_hits):
        for term in accessory_hits:
            global_exclusions.append(f"global:accessory_primary_identity:{term}")

    type_exclusions: dict[str, list[str]] = {}
    all_device_terms: set[str] = set()
    matched_fields: set[str] = set()
    for device_type in candidate_types:
        matches = device_terms_by_type[device_type]
        observed_terms = {
            term for field_terms in matches.values() for term in field_terms
        }
        all_device_terms.update(observed_terms)
        matched_fields.update(field for field, terms in matches.items() if terms)
        exclusions = list(global_exclusions)
        wrong_hits = matching_terms(
            title_category,
            rules["devices"][device_type]["wrong_product_terms"],
        )
        exclusions.extend(
            f"{device_type}:wrong_product:{term}" for term in wrong_hits
        )
        gate_device_terms = sorted(
            {
                term
                for field, field_terms in matches.items()
                if field in identity_gate_fields
                for term in field_terms
            }
        )
        gate_smart_terms = sorted(
            {
                term
                for field, field_terms in smart_by_field.items()
                if field in identity_gate_fields
                for term in field_terms
            }
        )
        strong_gate_hits = matching_terms(
            title_category,
            rules["devices"][device_type]["strong_identity_terms"],
        )
        eligibility_identity_hits = matching_terms(
            title_category,
            rules["devices"][device_type]["eligibility_identity_terms"],
        )
        if not eligibility_identity_hits:
            exclusions.append(
                f"{device_type}:approved_identity_phrase_absent"
            )
        if not gate_device_terms:
            exclusions.append(
                f"{device_type}:identity_not_in_title_or_categories"
            )
        elif not strong_gate_hits and not gate_smart_terms:
            exclusions.append(
                f"{device_type}:smart_evidence_not_in_title_or_categories"
            )
        required_context_terms = rules["devices"][device_type].get(
            "required_context_terms",
            [],
        )
        if required_context_terms and not matching_terms(
            title_category,
            required_context_terms,
        ):
            exclusions.append(f"{device_type}:required_target_context_absent")
        conditional_identity_hits = matching_terms(
            title_category,
            conditional_terms,
        )
        specific_smart_terms = [
            term for term in all_smart_terms if term != "smart"
        ]
        if conditional_identity_hits and not specific_smart_terms:
            exclusions.append(
                f"{device_type}:conditional_without_specific_smart_control_evidence"
            )
        type_exclusions[device_type] = sorted(set(exclusions))

    matched_fields.update(
        field for field, terms in smart_by_field.items() if terms
    )
    eligible_types = sorted(
        device_type
        for device_type in candidate_types
        if not type_exclusions[device_type]
    )
    exclusion_reasons = sorted(
        {
            reason
            for reasons in type_exclusions.values()
            for reason in reasons
        }
    )

    if len(candidate_types) > 1 and len(eligible_types) == 1:
        ambiguity_status = "resolved_by_explicit_exclusion"
    elif len(candidate_types) > 1:
        ambiguity_status = "unresolved_multiple_device_types"
    else:
        ambiguity_status = "single_device_type"

    provisional_device_type = (
        eligible_types[0]
        if len(eligible_types) == 1
        else ("ambiguous" if len(eligible_types) > 1 else "excluded")
    )
    if not eligible_types:
        confidence = "excluded"
    elif len(eligible_types) > 1:
        confidence = "ambiguous"
    elif matching_terms(
        title_text,
        rules["devices"][eligible_types[0]]["strong_identity_terms"],
    ):
        confidence = "high"
    elif device_terms_by_type[eligible_types[0]]["title"] and primary_smart_terms:
        confidence = "medium"
    else:
        confidence = "borderline"

    smart_matched_fields = {
        field for field, terms in smart_by_field.items() if terms
    }
    title_only = matched_fields.issubset({"title"}) and smart_matched_fields.issubset(
        {"title"}
    )
    reason = {
        "policy": rules["filter"]["candidate_term_policy"],
        "device_types": candidate_types,
        "eligible_device_types": eligible_types,
        "matched_fields": sorted(matched_fields),
        "smart_matched_fields": sorted(smart_matched_fields),
        "title_only": title_only,
        "ambiguity_status": ambiguity_status,
    }
    return {
        "candidate_device_types": sorted(candidate_types),
        "eligible_device_types": eligible_types,
        "candidate_device_terms": sorted(all_device_terms),
        "candidate_smart_terms": all_smart_terms,
        "matched_fields": sorted(matched_fields),
        "exclusion_reasons": exclusion_reasons,
        "candidate_confidence": confidence,
        "provisional_device_type": provisional_device_type,
        "eligible_after_exclusions": len(eligible_types) == 1,
        "ambiguity_status": ambiguity_status,
        "candidate_reason": json.dumps(
            reason,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "title_only": title_only,
    }


PARENT_INDEX_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("source_domain", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("content_fingerprint", pa.string(), nullable=False),
        pa.field("core_nonempty_count", pa.int16(), nullable=False),
        pa.field("identity_text_chars", pa.int32(), nullable=False),
    ]
)

CANDIDATE_SOURCE_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("source_domain", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("main_category", pa.string()),
        pa.field("title", pa.string()),
        pa.field("categories", pa.string(), nullable=False),
        pa.field("features", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("store", pa.string()),
        pa.field("details", pa.string(), nullable=False),
        pa.field("price", pa.string(), nullable=False),
        pa.field("average_rating", pa.float64()),
        pa.field("rating_number", pa.int64()),
        pa.field("candidate_device_types", pa.list_(pa.string()), nullable=False),
        pa.field("eligible_device_types", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_device_terms", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_smart_terms", pa.list_(pa.string()), nullable=False),
        pa.field("matched_fields", pa.list_(pa.string()), nullable=False),
        pa.field("exclusion_reasons", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_confidence", pa.string(), nullable=False),
        pa.field("provisional_device_type", pa.string(), nullable=False),
        pa.field("eligible_after_exclusions", pa.bool_(), nullable=False),
        pa.field("ambiguity_status", pa.string(), nullable=False),
        pa.field("candidate_reason", pa.string(), nullable=False),
        pa.field("title_only", pa.bool_(), nullable=False),
        pa.field("filter_version", pa.string(), nullable=False),
        pa.field("content_fingerprint", pa.string(), nullable=False),
        pa.field("core_nonempty_count", pa.int16(), nullable=False),
        pa.field("identity_text_chars", pa.int32(), nullable=False),
    ]
)

MERGED_CANDIDATE_SCHEMA = pa.schema(
    [
        pa.field("parent_asin", pa.string(), nullable=False),
        pa.field("source_domains", pa.list_(pa.string()), nullable=False),
        pa.field("primary_source_domain", pa.string(), nullable=False),
        pa.field("primary_source_row_number", pa.int64(), nullable=False),
        pa.field("main_category", pa.string()),
        pa.field("title", pa.string()),
        pa.field("categories", pa.string(), nullable=False),
        pa.field("features", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("store", pa.string()),
        pa.field("details", pa.string(), nullable=False),
        pa.field("price", pa.string(), nullable=False),
        pa.field("average_rating", pa.float64()),
        pa.field("rating_number", pa.int64()),
        pa.field("candidate_device_types", pa.list_(pa.string()), nullable=False),
        pa.field("eligible_device_types", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_device_terms", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_smart_terms", pa.list_(pa.string()), nullable=False),
        pa.field("matched_fields", pa.list_(pa.string()), nullable=False),
        pa.field("exclusion_reasons", pa.list_(pa.string()), nullable=False),
        pa.field("candidate_confidence", pa.string(), nullable=False),
        pa.field("provisional_device_type", pa.string(), nullable=False),
        pa.field("device_type", pa.string()),
        pa.field("eligible_after_exclusions", pa.bool_(), nullable=False),
        pa.field("ambiguity_status", pa.string(), nullable=False),
        pa.field("candidate_reason", pa.string(), nullable=False),
        pa.field("title_only", pa.bool_(), nullable=False),
        pa.field("filter_version", pa.string(), nullable=False),
        pa.field("candidate_source_record_count", pa.int32(), nullable=False),
        pa.field("duplicate_resolution_rule", pa.string(), nullable=False),
        pa.field("coalesced_fields", pa.list_(pa.string()), nullable=False),
        pa.field("content_fingerprint", pa.string(), nullable=False),
        pa.field("core_nonempty_count", pa.int16(), nullable=False),
        pa.field("identity_text_chars", pa.int32(), nullable=False),
    ]
)


class ParquetSink:
    def __init__(
        self,
        final_path: Path,
        schema: pa.Schema,
        *,
        chunk_size: int,
    ) -> None:
        self.final_path = final_path
        self.temporary_path = final_path.with_name(
            f"{final_path.name}.tmp-{os.getpid()}"
        )
        self.schema = schema
        self.chunk_size = chunk_size
        self.rows: list[dict[str, Any]] = []
        self.writer: pq.ParquetWriter | None = None
        self.row_count = 0
        final_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        if len(self.rows) >= self.chunk_size:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        table = pa.Table.from_pylist(self.rows, schema=self.schema)
        if self.writer is None:
            self.writer = pq.ParquetWriter(
                self.temporary_path,
                self.schema,
                compression="zstd",
                use_dictionary=True,
            )
        self.writer.write_table(table)
        self.row_count += table.num_rows
        self.rows.clear()

    def finish(self) -> int:
        self.flush()
        if self.writer is None:
            pq.write_table(
                pa.Table.from_pylist([], schema=self.schema),
                self.temporary_path,
                compression="zstd",
            )
        else:
            self.writer.close()
            self.writer = None
        os.replace(self.temporary_path, self.final_path)
        return self.row_count

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.temporary_path.is_file():
            self.temporary_path.unlink()


def load_context() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    project_toml = project_root / "config" / "project.toml"
    amazon_config = project_root / "config" / "amazon_w1_files.json"
    rule_config = project_root / "config" / "product_filter_rules.toml"
    w2_status_path = (
        project_root
        / "data"
        / "amazon_reviews_2023"
        / "reports"
        / "w2"
        / "w2_status.json"
    )
    w2_inventory_path = w2_status_path.parent / "source_inventory.json"

    with project_toml.open("rb") as handle:
        project_config = tomllib.load(handle)
    with rule_config.open("rb") as handle:
        raw_rules = tomllib.load(handle)
    rules = compile_rules(raw_rules)
    amazon_files = json.loads(amazon_config.read_text(encoding="utf-8"))
    w2_status = json.loads(w2_status_path.read_text(encoding="utf-8"))
    w2_inventory = json.loads(w2_inventory_path.read_text(encoding="utf-8"))
    if w2_status.get("status") != "PASS":
        raise SourceMismatch("W2 status is not PASS.")

    raw_uncompressed = resolve_inside(
        project_root,
        project_config["paths"]["raw_uncompressed"],
    )
    interim = resolve_inside(project_root, project_config["paths"]["interim"])
    processed = resolve_inside(project_root, project_config["paths"]["processed"])
    reports = resolve_inside(project_root, project_config["paths"]["reports"]) / "w3"
    work = interim / "w3_work"
    checkpoints = reports / "checkpoints"
    w2_by_id = {item["id"]: item for item in w2_inventory["files"]}

    sources = []
    for item in amazon_files:
        if item["record_type"] != "metadata":
            continue
        relative = Path(item["uncompressed_relative"])
        path = raw_uncompressed / relative
        if path.suffix.lower() != ".jsonl" or path.name.lower().endswith(".jsonl.gz"):
            raise ValueError(f"Refusing non-JSONL metadata source: {relative}")
        w2_item = w2_by_id[item["id"]]
        sources.append(
            {
                "id": item["id"],
                "domain": item["domain"],
                "relative_path": str(relative).replace("\\", "/"),
                "path": path,
                "expected_records": w2_item["exact_nonempty_record_count"],
                "w2_identity": w2_item["input_identity"],
                "parent_index_path": work / f"{item['id']}_parent_index.parquet",
                "source_candidates_path": work / f"{item['id']}_candidates.parquet",
                "checkpoint_path": checkpoints / f"{item['id']}.json",
            }
        )
    if len(sources) != 2:
        raise ValueError(f"Expected two metadata sources, found {len(sources)}")

    fingerprint = hashlib.sha256()
    for path in (
        project_toml,
        amazon_config,
        rule_config,
        w2_status_path,
        w2_inventory_path,
        Path(__file__).resolve(),
    ):
        fingerprint.update(str(path.relative_to(project_root)).encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(path.read_bytes())
        fingerprint.update(b"\0")
    for version in (
        duckdb.__version__,
        pa.__version__,
        pl.__version__,
        orjson.__version__,
    ):
        fingerprint.update(version.encode("ascii"))

    return {
        "project_root": project_root,
        "project_config": project_config,
        "rule_config": rule_config,
        "raw_rules": raw_rules,
        "rules": rules,
        "w2_status": w2_status,
        "w2_inventory": w2_inventory,
        "raw_uncompressed": raw_uncompressed,
        "interim": interim,
        "processed": processed,
        "reports": reports,
        "work": work,
        "checkpoints": checkpoints,
        "sources": sources,
        "configuration_fingerprint": fingerprint.hexdigest(),
        "metadata_candidates": interim / "metadata_candidates.parquet",
        "target_products": processed / "target_products.parquet",
        "audit_sample": reports / "product_audit_sample.csv",
        "audit_decisions": reports / "product_audit_decisions.csv",
    }


def checkpoint_matches(
    checkpoint: dict[str, Any],
    source: dict[str, Any],
    identity: dict[str, Any],
    fingerprint: str,
) -> bool:
    if (
        checkpoint.get("status") != "COMPLETE"
        or checkpoint.get("configuration_fingerprint") != fingerprint
        or checkpoint.get("input_identity", {}).get("size_bytes")
        != identity["size_bytes"]
        or checkpoint.get("input_identity", {}).get("mtime_ns")
        != identity["mtime_ns"]
        or not identity["readonly"]
    ):
        return False
    for key in ("parent_index_path", "source_candidates_path"):
        path = source[key]
        if not path.is_file():
            return False
    return (
        pq.ParquetFile(source["parent_index_path"]).metadata.num_rows
        == checkpoint.get("object_count")
        and pq.ParquetFile(source["source_candidates_path"]).metadata.num_rows
        == checkpoint.get("candidate_record_count")
    )


def verify_source_against_w2(
    source: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    w2_identity = source["w2_identity"]
    if (
        identity["size_bytes"] != w2_identity["size_bytes"]
        or identity["mtime_ns"] != w2_identity["mtime_ns"]
        or not identity["readonly"]
    ):
        raise SourceMismatch(
            f"Metadata identity differs from W2: {source['relative_path']}"
        )


def scan_source(
    source: dict[str, Any],
    context: dict[str, Any],
    logger: W3Logger,
) -> dict[str, Any]:
    identity = input_identity(source["path"])
    verify_source_against_w2(source, identity)
    if free_bytes(context["project_root"]) < MINIMUM_FREE_BYTES:
        raise SpaceGate("Free space is below the 60 GiB W3 floor.")

    parent_sink = ParquetSink(
        source["parent_index_path"],
        PARENT_INDEX_SCHEMA,
        chunk_size=50_000,
    )
    candidate_sink = ParquetSink(
        source["source_candidates_path"],
        CANDIDATE_SOURCE_SCHEMA,
        chunk_size=2_000,
    )
    started_at = now_iso()
    started = time.perf_counter()
    physical_lines = 0
    empty_lines = 0
    nonempty_records = 0
    parse_success = 0
    parse_errors = 0
    non_objects = 0
    objects = 0
    bytes_processed = 0
    next_progress_bytes = PROGRESS_BYTES
    next_progress_records = PROGRESS_RECORDS
    candidate_count = 0
    inclusion_term_counts: Counter[str] = Counter()
    smart_term_counts: Counter[str] = Counter()
    exclusion_reason_counts: Counter[str] = Counter()
    device_candidate_counts: Counter[str] = Counter()
    eligible_device_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    ambiguity_counts: Counter[str] = Counter()
    peak_rss = update_peak_memory(None)

    logger.write(
        "INFO",
        (
            f"Metadata scan started: id={source['id']}; "
            f"bytes={identity['size_bytes']}; expected_records={source['expected_records']}"
        ),
    )
    try:
        with source["path"].open("rb", buffering=1024 * 1024) as handle:
            for line in handle:
                physical_lines += 1
                bytes_processed += len(line)
                if not line or line.isspace():
                    empty_lines += 1
                    continue
                nonempty_records += 1
                try:
                    value = orjson.loads(line)
                except orjson.JSONDecodeError:
                    parse_errors += 1
                    continue
                parse_success += 1
                if not isinstance(value, dict):
                    non_objects += 1
                    continue
                objects += 1

                parent_asin = value.get("parent_asin")
                if not isinstance(parent_asin, str) or not parent_asin:
                    raise SourceMismatch(
                        f"Invalid parent_asin at {source['id']} line {physical_lines}"
                    )
                fingerprint = canonical_content_fingerprint(value)
                core_nonempty = sum(
                    1 for field in CORE_METADATA_FIELDS if is_nonempty(value.get(field))
                )
                identity_chars = sum(
                    len(flatten_text(value.get(field))) for field in IDENTITY_FIELDS
                )
                parent_sink.append(
                    {
                        "parent_asin": parent_asin,
                        "source_domain": source["domain"],
                        "source_row_number": physical_lines,
                        "content_fingerprint": fingerprint,
                        "core_nonempty_count": core_nonempty,
                        "identity_text_chars": identity_chars,
                    }
                )

                analysis = analyze_candidate(value, context["rules"])
                if analysis is not None:
                    candidate_count += 1
                    for device_type in analysis["candidate_device_types"]:
                        device_candidate_counts[device_type] += 1
                    for device_type in analysis["eligible_device_types"]:
                        eligible_device_counts[device_type] += 1
                    confidence_counts[analysis["candidate_confidence"]] += 1
                    ambiguity_counts[analysis["ambiguity_status"]] += 1
                    for term in analysis["candidate_device_terms"]:
                        inclusion_term_counts[term] += 1
                    for term in analysis["candidate_smart_terms"]:
                        smart_term_counts[term] += 1
                    for reason in analysis["exclusion_reasons"]:
                        exclusion_reason_counts[reason] += 1

                    candidate_sink.append(
                        {
                            "parent_asin": parent_asin,
                            "source_domain": source["domain"],
                            "source_row_number": physical_lines,
                            "main_category": coerce_string(value.get("main_category")),
                            "title": coerce_string(value.get("title")),
                            "categories": json_string(value.get("categories")),
                            "features": json_string(value.get("features")),
                            "description": json_string(value.get("description")),
                            "store": coerce_string(value.get("store")),
                            "details": json_string(value.get("details")),
                            "price": json_string(value.get("price")),
                            "average_rating": coerce_float(
                                value.get("average_rating")
                            ),
                            "rating_number": coerce_int(value.get("rating_number")),
                            **{
                                key: analysis[key]
                                for key in (
                                    "candidate_device_types",
                                    "eligible_device_types",
                                    "candidate_device_terms",
                                    "candidate_smart_terms",
                                    "matched_fields",
                                    "exclusion_reasons",
                                    "candidate_confidence",
                                    "provisional_device_type",
                                    "eligible_after_exclusions",
                                    "ambiguity_status",
                                    "candidate_reason",
                                    "title_only",
                                )
                            },
                            "filter_version": context["rules"]["filter"]["version"],
                            "content_fingerprint": fingerprint,
                            "core_nonempty_count": core_nonempty,
                            "identity_text_chars": identity_chars,
                        }
                    )

                if (
                    bytes_processed >= next_progress_bytes
                    or nonempty_records >= next_progress_records
                ):
                    current_free = free_bytes(context["project_root"])
                    if current_free < MINIMUM_FREE_BYTES:
                        raise SpaceGate(
                            f"Free space fell below 60 GiB during {source['id']}."
                        )
                    peak_rss = update_peak_memory(peak_rss)
                    elapsed = max(time.perf_counter() - started, 0.001)
                    logger.write(
                        "INFO",
                        (
                            f"Metadata progress: id={source['id']}; "
                            f"records={nonempty_records}; candidates={candidate_count}; "
                            f"bytes={bytes_processed}; "
                            f"bytes_per_second={int(bytes_processed / elapsed)}; "
                            f"free_bytes={current_free}"
                        ),
                    )
                    while bytes_processed >= next_progress_bytes:
                        next_progress_bytes += PROGRESS_BYTES
                    while nonempty_records >= next_progress_records:
                        next_progress_records += PROGRESS_RECORDS

        parent_rows = parent_sink.finish()
        candidate_rows = candidate_sink.finish()
    except Exception:
        parent_sink.abort()
        candidate_sink.abort()
        raise

    if (
        nonempty_records != source["expected_records"]
        or parse_errors != 0
        or non_objects != 0
        or objects != source["expected_records"]
        or parent_rows != source["expected_records"]
        or candidate_rows != candidate_count
    ):
        raise SourceMismatch(
            f"W3 count mismatch for {source['id']}: "
            f"nonempty={nonempty_records}, expected={source['expected_records']}, "
            f"errors={parse_errors}, nonobjects={non_objects}, objects={objects}"
        )
    final_identity = input_identity(source["path"])
    if final_identity != identity:
        raise SourceMismatch(
            f"Raw metadata identity changed during W3: {source['relative_path']}"
        )

    finished_at = now_iso()
    duration = time.perf_counter() - started
    result = {
        "phase": PHASE,
        "status": "COMPLETE",
        "configuration_fingerprint": context["configuration_fingerprint"],
        "id": source["id"],
        "domain": source["domain"],
        "relative_path": source["relative_path"],
        "input_identity": identity,
        "scan_started_at": started_at,
        "scan_finished_at": finished_at,
        "duration_seconds": duration,
        "physical_line_count": physical_lines,
        "empty_line_count": empty_lines,
        "nonempty_record_count": nonempty_records,
        "parse_success_count": parse_success,
        "parse_error_count": parse_errors,
        "non_object_count": non_objects,
        "object_count": objects,
        "candidate_record_count": candidate_count,
        "inclusion_term_counts": dict(sorted(inclusion_term_counts.items())),
        "smart_term_counts": dict(sorted(smart_term_counts.items())),
        "exclusion_reason_counts": dict(sorted(exclusion_reason_counts.items())),
        "device_candidate_counts": dict(sorted(device_candidate_counts.items())),
        "eligible_device_counts": dict(sorted(eligible_device_counts.items())),
        "confidence_counts": dict(sorted(confidence_counts.items())),
        "ambiguity_counts": dict(sorted(ambiguity_counts.items())),
        "parent_index_relative_path": str(
            source["parent_index_path"].relative_to(context["project_root"])
        ).replace("\\", "/"),
        "source_candidates_relative_path": str(
            source["source_candidates_path"].relative_to(context["project_root"])
        ).replace("\\", "/"),
        "peak_process_rss_bytes": update_peak_memory(peak_rss),
        "end_free_bytes": free_bytes(context["project_root"]),
    }
    atomic_json(source["checkpoint_path"], result)
    logger.write(
        "INFO",
        (
            f"Metadata scan completed: id={source['id']}; "
            f"records={objects}; candidates={candidate_count}; "
            f"seconds={duration:.3f}; peak_rss_bytes={result['peak_process_rss_bytes']}"
        ),
    )
    return result


def json_is_empty(value: str | None) -> bool:
    return value in (None, "", "null", "[]", "{}")


def merged_candidate_row(
    records: list[dict[str, Any]],
    rule_version: str,
) -> tuple[dict[str, Any], str]:
    eligible_records = [row for row in records if row["eligible_device_types"]]
    ranking_pool = eligible_records or records
    ranked = sorted(
        ranking_pool,
        key=lambda row: (
            -int(row["core_nonempty_count"]),
            -int(row["identity_text_chars"]),
            row["source_domain"],
            int(row["source_row_number"]),
        ),
    )
    primary = dict(ranked[0])
    if len(records) == 1:
        resolution_rule = "only_source_record"
    elif eligible_records and len(eligible_records) < len(records):
        resolution_rule = "eligible_record_preferred"
    elif len({row["core_nonempty_count"] for row in ranking_pool}) > 1:
        resolution_rule = "richest_core_fields"
    elif len({row["identity_text_chars"] for row in ranking_pool}) > 1:
        resolution_rule = "richest_identity_text"
    else:
        resolution_rule = "stable_domain_and_row_order"

    coalesced_fields = []
    for field in (
        "main_category",
        "title",
        "categories",
        "features",
        "description",
        "store",
        "details",
        "price",
        "average_rating",
        "rating_number",
    ):
        current = primary.get(field)
        current_empty = (
            json_is_empty(current)
            if field in {"categories", "features", "description", "details", "price"}
            else current is None or current == ""
        )
        if not current_empty:
            continue
        for other in ranked[1:]:
            replacement = other.get(field)
            replacement_empty = (
                json_is_empty(replacement)
                if field in {"categories", "features", "description", "details", "price"}
                else replacement is None or replacement == ""
            )
            if not replacement_empty:
                primary[field] = replacement
                coalesced_fields.append(field)
                break

    candidate_types = sorted(
        {
            value
            for row in records
            for value in row["candidate_device_types"]
        }
    )
    eligible_types = sorted(
        {
            value
            for row in records
            for value in row["eligible_device_types"]
        }
    )
    if len(candidate_types) > 1 and len(eligible_types) == 1:
        ambiguity_status = "resolved_by_explicit_exclusion"
    elif len(eligible_types) > 1:
        ambiguity_status = "unresolved_multiple_device_types"
    else:
        ambiguity_status = "single_device_type"
    device_type = eligible_types[0] if len(eligible_types) == 1 else None
    provisional = (
        device_type
        if device_type
        else ("ambiguous" if len(eligible_types) > 1 else "excluded")
    )
    confidence_order = {"high": 3, "medium": 2, "borderline": 1}
    if not eligible_types:
        confidence = "excluded"
    elif len(eligible_types) > 1:
        confidence = "ambiguous"
    else:
        matching_confidences = [
            row["candidate_confidence"]
            for row in records
            if eligible_types[0] in row["eligible_device_types"]
        ]
        confidence = max(
            matching_confidences,
            key=lambda value: confidence_order.get(value, 0),
        )

    combined_reason = {
        "candidate_source_records": len(records),
        "candidate_device_types": candidate_types,
        "eligible_device_types": eligible_types,
        "ambiguity_status": ambiguity_status,
        "duplicate_resolution_rule": resolution_rule,
    }
    merged = {
        "parent_asin": primary["parent_asin"],
        "source_domains": sorted({row["source_domain"] for row in records}),
        "primary_source_domain": primary["source_domain"],
        "primary_source_row_number": primary["source_row_number"],
        "main_category": primary.get("main_category"),
        "title": primary.get("title"),
        "categories": primary["categories"],
        "features": primary["features"],
        "description": primary["description"],
        "store": primary.get("store"),
        "details": primary["details"],
        "price": primary["price"],
        "average_rating": primary.get("average_rating"),
        "rating_number": primary.get("rating_number"),
        "candidate_device_types": candidate_types,
        "eligible_device_types": eligible_types,
        "candidate_device_terms": sorted(
            {
                value
                for row in records
                for value in row["candidate_device_terms"]
            }
        ),
        "candidate_smart_terms": sorted(
            {
                value
                for row in records
                for value in row["candidate_smart_terms"]
            }
        ),
        "matched_fields": sorted(
            {value for row in records for value in row["matched_fields"]}
        ),
        "exclusion_reasons": sorted(
            {value for row in records for value in row["exclusion_reasons"]}
        ),
        "candidate_confidence": confidence,
        "provisional_device_type": provisional,
        "device_type": device_type,
        "eligible_after_exclusions": device_type is not None,
        "ambiguity_status": ambiguity_status,
        "candidate_reason": json.dumps(
            combined_reason,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "title_only": all(row["title_only"] for row in records),
        "filter_version": rule_version,
        "candidate_source_record_count": len(records),
        "duplicate_resolution_rule": resolution_rule,
        "coalesced_fields": sorted(coalesced_fields),
        "content_fingerprint": primary["content_fingerprint"],
        "core_nonempty_count": primary["core_nonempty_count"],
        "identity_text_chars": primary["identity_text_chars"],
    }
    return merged, resolution_rule


def load_source_candidates(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=5_000):
            rows.extend(batch.to_pylist())
    return rows


def merge_candidates(
    context: dict[str, Any],
    logger: W3Logger,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    source_rows = load_source_candidates(
        [source["source_candidates_path"] for source in context["sources"]]
    )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        groups[row["parent_asin"]].append(row)
    merged_rows: list[dict[str, Any]] = []
    resolution_counts: Counter[str] = Counter()
    for parent_asin in sorted(groups):
        merged, rule = merged_candidate_row(
            groups[parent_asin],
            context["rules"]["filter"]["version"],
        )
        merged_rows.append(merged)
        resolution_counts[rule] += 1
    table = pa.Table.from_pylist(merged_rows, schema=MERGED_CANDIDATE_SCHEMA)
    temporary = context["metadata_candidates"].with_name(
        f"{context['metadata_candidates'].name}.tmp-{os.getpid()}"
    )
    context["metadata_candidates"].parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, temporary, compression="zstd", use_dictionary=True)
    os.replace(temporary, context["metadata_candidates"])
    logger.write(
        "INFO",
        (
            f"Candidate merge completed: source_rows={len(source_rows)}; "
            f"unique_parents={len(merged_rows)}"
        ),
    )
    return merged_rows, dict(sorted(resolution_counts.items()))


def sql_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def duplicate_parent_audit(
    context: dict[str, Any],
    resolution_counts: dict[str, int],
) -> dict[str, Any]:
    work_temp = context["work"] / "duckdb_temp"
    work_temp.mkdir(parents=True, exist_ok=True)
    paths_sql = ", ".join(
        f"'{sql_path(source['parent_index_path'])}'"
        for source in context["sources"]
    )
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET memory_limit='1GB'")
        connection.execute(f"SET temp_directory='{sql_path(work_temp)}'")
        connection.execute(
            f"CREATE VIEW parent_index AS SELECT * FROM read_parquet([{paths_sql}])"
        )
        per_domain_rows = connection.execute(
            """
            SELECT
                source_domain,
                COUNT(*) AS source_rows,
                COUNT(DISTINCT parent_asin) AS unique_parent_asin,
                COUNT(*) - COUNT(DISTINCT parent_asin) AS duplicate_rows
            FROM parent_index
            GROUP BY source_domain
            ORDER BY source_domain
            """
        ).fetchall()
        cross = connection.execute(
            """
            WITH grouped AS (
                SELECT
                    parent_asin,
                    COUNT(DISTINCT source_domain) AS domain_count,
                    COUNT(DISTINCT content_fingerprint) AS fingerprint_count,
                    COUNT(*) AS source_rows
                FROM parent_index
                GROUP BY parent_asin
            )
            SELECT
                COUNT(*) FILTER (WHERE domain_count > 1) AS cross_domain_parents,
                COUNT(*) FILTER (
                    WHERE domain_count > 1 AND fingerprint_count = 1
                ) AS cross_domain_identical,
                COUNT(*) FILTER (
                    WHERE domain_count > 1 AND fingerprint_count > 1
                ) AS cross_domain_conflicts,
                COUNT(*) AS union_unique_parents
            FROM grouped
            """
        ).fetchone()
    finally:
        connection.close()
    return {
        "phase": PHASE,
        "generated_at": now_iso(),
        "per_domain": [
            {
                "source_domain": row[0],
                "source_rows": int(row[1]),
                "unique_parent_asin": int(row[2]),
                "duplicate_rows": int(row[3]),
            }
            for row in per_domain_rows
        ],
        "cross_domain_parent_asin": int(cross[0]),
        "cross_domain_completely_identical": int(cross[1]),
        "cross_domain_content_conflicts": int(cross[2]),
        "union_unique_parent_asin": int(cross[3]),
        "candidate_duplicate_resolution_rule_counts": resolution_counts,
    }


def stable_random_key(seed: int, *values: str) -> str:
    payload = "\0".join([str(seed), *values]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evidence_snippet(json_value: str, limit: int = 320) -> str:
    try:
        value = orjson.loads(json_value)
        text = " ".join(flatten_text(value).split())
    except orjson.JSONDecodeError:
        text = ""
    return text[:limit]


def stratified_take(
    rows: list[dict[str, Any]],
    n: int,
    seed: int,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        bucket = (
            ",".join(row["source_domains"]),
            row["candidate_confidence"],
            "title_only" if row["title_only"] else "non_title_evidence",
        )
        buckets[bucket].append(row)
    for bucket_rows in buckets.values():
        bucket_rows.sort(
            key=lambda row: stable_random_key(
                seed,
                row["parent_asin"],
                row["filter_version"],
            )
        )
    selected: list[dict[str, Any]] = []
    ordered_buckets = sorted(buckets)
    while len(selected) < n and ordered_buckets:
        remaining = []
        for bucket in ordered_buckets:
            if buckets[bucket] and len(selected) < n:
                selected.append(buckets[bucket].pop(0))
            if buckets[bucket]:
                remaining.append(bucket)
        ordered_buckets = remaining
    return selected


def build_audit_sample(
    rows: list[dict[str, Any]],
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    seed = int(context["rules"]["filter"]["random_seed"])
    per_type = int(context["rules"]["filter"]["audit_per_device_type"])
    selected: list[tuple[str, dict[str, Any]]] = []
    selected_ids: set[str] = set()

    for device_type in DEVICE_TYPES:
        eligible = [
            row
            for row in rows
            if row["eligible_after_exclusions"]
            and row["device_type"] == device_type
        ]
        for row in stratified_take(eligible, min(per_type, len(eligible)), seed):
            selected.append(("eligible", row))
            selected_ids.add(row["parent_asin"])

    supplemental_groups = [
        (
            "ambiguous",
            [
                row
                for row in rows
                if row["ambiguity_status"] == "unresolved_multiple_device_types"
            ],
            50,
        ),
        (
            "strong_exclusion",
            [
                row
                for row in rows
                if not row["eligible_after_exclusions"]
                and row["exclusion_reasons"]
            ],
            50,
        ),
    ]
    for stratum, candidates, limit in supplemental_groups:
        candidates = [
            row for row in candidates if row["parent_asin"] not in selected_ids
        ]
        candidates.sort(
            key=lambda row: stable_random_key(
                seed,
                stratum,
                row["parent_asin"],
            )
        )
        for row in candidates[:limit]:
            selected.append((stratum, row))
            selected_ids.add(row["parent_asin"])

    audit_rows: list[dict[str, Any]] = []
    for stratum, row in selected:
        audit_id = hashlib.sha256(
            f"{row['filter_version']}\0{row['parent_asin']}".encode("utf-8")
        ).hexdigest()[:16]
        audit_rows.append(
            {
                "audit_id": audit_id,
                "audit_stratum": stratum,
                "parent_asin": row["parent_asin"],
                "expected_device_type": row["device_type"]
                or row["provisional_device_type"],
                "source_domains": "|".join(row["source_domains"]),
                "candidate_confidence": row["candidate_confidence"],
                "ambiguity_status": row["ambiguity_status"],
                "product_title": row["title"] or "",
                "main_category": row["main_category"] or "",
                "categories_evidence": evidence_snippet(row["categories"]),
                "features_evidence": evidence_snippet(row["features"]),
                "description_evidence": evidence_snippet(row["description"]),
                "candidate_device_terms": "|".join(
                    row["candidate_device_terms"]
                ),
                "candidate_smart_terms": "|".join(row["candidate_smart_terms"]),
                "matched_fields": "|".join(row["matched_fields"]),
                "exclusion_reasons": "|".join(row["exclusion_reasons"]),
                "audit_label": "",
                "audit_device_type": "",
                "audit_notes": "",
            }
        )
    sample_fingerprint = hashlib.sha256(
        "\n".join(sorted(row["audit_id"] for row in audit_rows)).encode("ascii")
    ).hexdigest()
    atomic_csv(context["audit_sample"], audit_rows)
    return audit_rows, sample_fingerprint


def load_audit_decisions(
    context: dict[str, Any],
    sample_rows: list[dict[str, Any]],
    sample_fingerprint: str,
) -> dict[str, Any]:
    labels = set(context["rules"]["audit"]["labels"])
    if not context["audit_decisions"].is_file():
        return {
            "complete": False,
            "reason": "product_audit_decisions.csv is not present.",
            "sample_fingerprint": sample_fingerprint,
            "reviewed_rows": 0,
            "precision_by_device_type": {},
        }
    with context["audit_decisions"].open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        decisions = list(csv.DictReader(handle))
    sample_by_id = {row["audit_id"]: row for row in sample_rows}
    decision_by_id = {row.get("audit_id", ""): row for row in decisions}
    unknown_ids = sorted(set(decision_by_id) - set(sample_by_id))
    missing_ids = sorted(set(sample_by_id) - set(decision_by_id))
    invalid_labels = sorted(
        {
            row.get("audit_label", "")
            for row in decisions
            if row.get("audit_label", "") not in labels
        }
    )
    if unknown_ids or missing_ids or invalid_labels:
        return {
            "complete": False,
            "reason": "Audit decisions do not exactly match the current sample.",
            "sample_fingerprint": sample_fingerprint,
            "reviewed_rows": len(decisions),
            "unknown_audit_ids": unknown_ids,
            "missing_audit_ids": missing_ids,
            "invalid_labels": invalid_labels,
            "precision_by_device_type": {},
        }

    label_counts: Counter[str] = Counter()
    precision: dict[str, Any] = {}
    for row in decisions:
        label_counts[row["audit_label"]] += 1
    for device_type in DEVICE_TYPES:
        type_ids = [
            row["audit_id"]
            for row in sample_rows
            if row["audit_stratum"] == "eligible"
            and row["expected_device_type"] == device_type
        ]
        correct = 0
        for audit_id in type_ids:
            decision = decision_by_id[audit_id]
            audited_type = decision.get("audit_device_type", "").strip()
            if (
                decision["audit_label"] == "correct_target"
                and (not audited_type or audited_type == device_type)
            ):
                correct += 1
        precision[device_type] = {
            "reviewed": len(type_ids),
            "correct_target": correct,
            "precision": correct / len(type_ids) if type_ids else None,
        }
    minimum_precision = float(context["rules"]["audit"]["minimum_precision"])
    precision_pass = all(
        item["precision"] is not None and item["precision"] >= minimum_precision
        for item in precision.values()
    )
    return {
        "complete": True,
        "reason": (
            "Audit decisions complete."
            if precision_pass
            else "One or more device types are below the configured precision floor."
        ),
        "sample_fingerprint": sample_fingerprint,
        "reviewed_rows": len(decisions),
        "label_counts": dict(sorted(label_counts.items())),
        "precision_by_device_type": precision,
        "minimum_precision": minimum_precision,
        "precision_floor_passed": precision_pass,
    }


def write_target_products(
    context: dict[str, Any],
) -> dict[str, Any]:
    table = pq.read_table(context["metadata_candidates"])
    mask = pc.equal(table["eligible_after_exclusions"], True)
    target = table.filter(mask)
    temporary = context["target_products"].with_name(
        f"{context['target_products'].name}.tmp-{os.getpid()}"
    )
    context["target_products"].parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(target, temporary, compression="zstd", use_dictionary=True)
    os.replace(temporary, context["target_products"])
    return {
        "path": str(
            context["target_products"].relative_to(context["project_root"])
        ).replace("\\", "/"),
        "rows": target.num_rows,
        "bytes": context["target_products"].stat().st_size,
        "fields": target.schema.names,
    }


def count_candidates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    device_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    ambiguity_counts: Counter[str] = Counter()
    exclusion_counts: Counter[str] = Counter()
    inclusion_counts: Counter[str] = Counter()
    smart_counts: Counter[str] = Counter()
    matched_field_counts: Counter[str] = Counter()
    for row in rows:
        if row["device_type"]:
            device_counts[row["device_type"]] += 1
        confidence_counts[row["candidate_confidence"]] += 1
        ambiguity_counts[row["ambiguity_status"]] += 1
        for reason in row["exclusion_reasons"]:
            exclusion_counts[reason] += 1
        for term in row["candidate_device_terms"]:
            inclusion_counts[term] += 1
        for term in row["candidate_smart_terms"]:
            smart_counts[term] += 1
        for field in row["matched_fields"]:
            matched_field_counts[field] += 1
    unresolved = ambiguity_counts["unresolved_multiple_device_types"]
    eligible = sum(device_counts.values())
    ambiguous_share = unresolved / (eligible + unresolved) if eligible + unresolved else 0
    return {
        "unique_candidate_parents": len(rows),
        "eligible_target_parents": eligible,
        "device_type_counts": dict(sorted(device_counts.items())),
        "confidence_counts": dict(sorted(confidence_counts.items())),
        "ambiguity_counts": dict(sorted(ambiguity_counts.items())),
        "unresolved_ambiguous_parents": unresolved,
        "unresolved_ambiguous_share": ambiguous_share,
        "exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
        "device_term_trigger_counts": dict(sorted(inclusion_counts.items())),
        "smart_term_trigger_counts": dict(sorted(smart_counts.items())),
        "matched_field_counts": dict(sorted(matched_field_counts.items())),
    }


def generate_reports(
    context: dict[str, Any],
    environment: dict[str, Any],
    scan_summaries: list[dict[str, Any]],
    duplicate_audit: dict[str, Any],
    candidate_stats: dict[str, Any],
    resolution_counts: dict[str, int],
    audit_result: dict[str, Any],
    disk_events: list[dict[str, Any]],
    logger: W3Logger,
) -> dict[str, Any]:
    reports = context["reports"]
    reports.mkdir(parents=True, exist_ok=True)
    target_info = None
    maximum_ambiguous = float(
        context["rules"]["audit"]["maximum_ambiguous_share"]
    )
    audit_passed = (
        audit_result.get("complete", False)
        and audit_result.get("precision_floor_passed", False)
        and candidate_stats["unresolved_ambiguous_share"] <= maximum_ambiguous
    )
    if audit_passed:
        target_info = write_target_products(context)

    flow = {
        "phase": PHASE,
        "generated_at": now_iso(),
        "configuration_fingerprint": context["configuration_fingerprint"],
        "filter_version": context["rules"]["filter"]["version"],
        "source_scans": scan_summaries,
        "candidate_statistics": candidate_stats,
        "duplicate_parent_asin": duplicate_audit,
        "candidate_duplicate_resolution_rule_counts": resolution_counts,
        "audit": audit_result,
        "target_products": target_info,
    }
    atomic_json(reports / "product_selection_flow.json", flow)

    flow_rows = []
    for summary in scan_summaries:
        flow_rows.extend(
            [
                {
                    "stage": "raw_metadata_records",
                    "domain": summary["domain"],
                    "reason": "",
                    "count": summary["object_count"],
                },
                {
                    "stage": "broad_candidate_source_records",
                    "domain": summary["domain"],
                    "reason": "",
                    "count": summary["candidate_record_count"],
                },
            ]
        )
    flow_rows.extend(
        [
            {
                "stage": "unique_candidate_parents",
                "domain": "All",
                "reason": "",
                "count": candidate_stats["unique_candidate_parents"],
            },
            {
                "stage": "eligible_target_parents",
                "domain": "All",
                "reason": "",
                "count": candidate_stats["eligible_target_parents"],
            },
            {
                "stage": "unresolved_ambiguous_parents",
                "domain": "All",
                "reason": "",
                "count": candidate_stats["unresolved_ambiguous_parents"],
            },
        ]
    )
    for reason, count in candidate_stats["exclusion_reason_counts"].items():
        flow_rows.append(
            {
                "stage": "exclusion_rule",
                "domain": "All",
                "reason": reason,
                "count": count,
            }
        )
    for term, count in candidate_stats["device_term_trigger_counts"].items():
        flow_rows.append(
            {
                "stage": "inclusion_device_term",
                "domain": "All",
                "reason": term,
                "count": count,
            }
        )
    for term, count in candidate_stats["smart_term_trigger_counts"].items():
        flow_rows.append(
            {
                "stage": "inclusion_smart_term",
                "domain": "All",
                "reason": term,
                "count": count,
            }
        )
    atomic_csv(reports / "product_selection_flow.csv", flow_rows)

    device_rows = [
        {
            "device_type": device_type,
            "final_rule_eligible_products": candidate_stats[
                "device_type_counts"
            ].get(device_type, 0),
            "audit_reviewed": audit_result.get(
                "precision_by_device_type",
                {},
            ).get(device_type, {}).get("reviewed"),
            "audit_correct_target": audit_result.get(
                "precision_by_device_type",
                {},
            ).get(device_type, {}).get("correct_target"),
            "audit_precision": audit_result.get(
                "precision_by_device_type",
                {},
            ).get(device_type, {}).get("precision"),
        }
        for device_type in DEVICE_TYPES
    ]
    atomic_csv(reports / "device_type_counts.csv", device_rows)
    atomic_json(reports / "duplicate_parent_asin_audit.json", duplicate_audit)
    atomic_json(reports / "product_audit_results.json", audit_result)

    vocabulary = {
        "phase": PHASE,
        "filter_version": context["rules"]["filter"]["version"],
        "configuration_fingerprint": context["configuration_fingerprint"],
        "frozen": audit_passed,
        "generated_at": now_iso(),
        "rules": context["raw_rules"],
    }
    atomic_json(reports / "product_filter_vocabulary.json", vocabulary)

    candidate_info = {
        "path": str(
            context["metadata_candidates"].relative_to(context["project_root"])
        ).replace("\\", "/"),
        "rows": pq.ParquetFile(context["metadata_candidates"]).metadata.num_rows,
        "bytes": context["metadata_candidates"].stat().st_size,
        "fields": MERGED_CANDIDATE_SCHEMA.names,
    }
    final_free = free_bytes(context["project_root"])
    disk_events.append(
        {
            "time": now_iso(),
            "event": "w3_reporting_complete",
            "free_bytes": final_free,
            "free_gib": final_free / 1024**3,
        }
    )
    atomic_json(
        reports / "w3_disk_usage.json",
        {
            "phase": PHASE,
            "generated_at": now_iso(),
            "minimum_free_bytes": MINIMUM_FREE_BYTES,
            "events": disk_events,
        },
    )

    summary_lines = [
        "# Phase W3 Product Selection Summary",
        "",
        f"- Filter version: `{context['rules']['filter']['version']}`",
        f"- Broad unique candidate parents: {candidate_stats['unique_candidate_parents']:,}",
        f"- Rule-eligible target parents: {candidate_stats['eligible_target_parents']:,}",
        f"- Unresolved ambiguous parents: {candidate_stats['unresolved_ambiguous_parents']:,}",
        "",
        "| Device type | Rule-eligible products | Audit reviewed | Correct | Precision |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in device_rows:
        precision = (
            f"{row['audit_precision']:.4f}"
            if isinstance(row["audit_precision"], float)
            else "pending"
        )
        summary_lines.append(
            f"| {row['device_type']} | "
            f"{row['final_rule_eligible_products']:,} | "
            f"{row['audit_reviewed'] if row['audit_reviewed'] is not None else 'pending'} | "
            f"{row['audit_correct_target'] if row['audit_correct_target'] is not None else 'pending'} | "
            f"{precision} |"
        )
    summary_lines.extend(
        [
            "",
            f"- Candidate Parquet: `{candidate_info['path']}` "
            f"({candidate_info['rows']:,} rows)",
            (
                f"- Target Parquet: `{target_info['path']}` "
                f"({target_info['rows']:,} rows)"
                if target_info
                else "- Target Parquet: not frozen; audit confirmation is pending."
            ),
            "",
            "W4 was not started. No review JSONL file was opened.",
            "",
        ]
    )
    atomic_text(
        reports / "product_selection_summary.md",
        "\n".join(summary_lines),
    )

    current_identities = {
        source["id"]: input_identity(source["path"]) for source in context["sources"]
    }
    raw_unchanged = all(
        current_identities[source["id"]] == summary["input_identity"]
        for source, summary in zip(context["sources"], scan_summaries)
    )
    required_reports = [
        "w3_execution.log",
        "product_selection_flow.json",
        "product_selection_flow.csv",
        "product_selection_summary.md",
        "product_filter_vocabulary.json",
        "duplicate_parent_asin_audit.json",
        "device_type_counts.csv",
        "product_audit_sample.csv",
        "product_audit_results.json",
        "w3_disk_usage.json",
    ]
    report_presence = {
        name: (reports / name).is_file() for name in required_reports
    }
    status_name = "PASS" if audit_passed else "PAUSED_AUDIT_REQUIRED"
    reason = (
        "All W3 acceptance criteria passed."
        if audit_passed
        else audit_result.get(
            "reason",
            "Product audit requires review or rule revision.",
        )
    )
    criteria = {
        "project_venv_environment_valid": (
            environment["project_venv_in_use"] and environment["python_64_bit"]
        ),
        "w2_status_pass": context["w2_status"]["status"] == "PASS",
        "two_metadata_full_scans_complete": len(scan_summaries) == 2
        and all(item["status"] == "COMPLETE" for item in scan_summaries),
        "input_record_counts_match_w2": all(
            item["object_count"] == source["expected_records"]
            for item, source in zip(scan_summaries, context["sources"])
        ),
        "broad_candidate_product_screening_complete": candidate_info["rows"] > 0,
        "exclusion_rules_applied": True,
        "duplicate_parent_asin_deterministically_resolved": True,
        "three_device_type_counts_available": all(
            device_type in candidate_stats["device_type_counts"]
            for device_type in DEVICE_TYPES
        ),
        "stratified_product_audit_complete": audit_result.get("complete", False),
        "precision_reported_by_device_type": all(
            audit_result.get("precision_by_device_type", {})
            .get(device_type, {})
            .get("precision")
            is not None
            for device_type in DEVICE_TYPES
        ),
        "candidate_parquet_valid": candidate_info["rows"] > 0,
        "target_products_parquet_valid": target_info is not None
        and target_info["rows"] > 0,
        "raw_metadata_identity_unchanged_and_readonly": raw_unchanged
        and all(identity["readonly"] for identity in current_identities.values()),
        "final_free_space_at_least_60_gib": final_free >= MINIMUM_FREE_BYTES,
        "review_jsonl_not_read": True,
        "compressed_archives_not_read": True,
        "w4_not_started": True,
        "required_reports_present": all(report_presence.values()),
    }
    status = {
        "phase": PHASE,
        "status": status_name,
        "reason": reason,
        "updated_at": now_iso(),
        "environment": environment,
        "configuration_fingerprint": context["configuration_fingerprint"],
        "filter_version": context["rules"]["filter"]["version"],
        "criteria": criteria,
        "report_presence": report_presence,
        "candidate_parquet": candidate_info,
        "target_products_parquet": target_info,
        "final_free_bytes": final_free,
        "final_free_gib": final_free / 1024**3,
        "raw_identities": current_identities,
        "policy_attestation": {
            "review_jsonl_opened": False,
            "compressed_archive_opened": False,
            "rating_used_for_product_selection": False,
            "average_rating_used_for_product_selection": False,
            "rating_number_used_for_product_selection": False,
            "price_used_for_product_selection": False,
            "metadata_review_join_performed": False,
            "review_cleaning_or_deduplication_performed": False,
            "annotation_or_modeling_performed": False,
            "w4_started": False,
        },
    }
    atomic_json(reports / "w3_status.json", status)
    logger.write("INFO", f"W3 finished with status={status_name}.")
    return status


def write_failure_status(
    context: dict[str, Any],
    *,
    status: str,
    reason: str,
    environment: dict[str, Any] | None,
    completed_ids: list[str],
) -> None:
    atomic_json(
        context["reports"] / "w3_status.json",
        {
            "phase": PHASE,
            "status": status,
            "reason": reason,
            "updated_at": now_iso(),
            "environment": environment,
            "completed_source_ids": completed_ids,
            "free_bytes": free_bytes(context["project_root"]),
            "w4_started": False,
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="W3 metadata-only broad candidate-product screening."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate W2, environment, metadata identities, rules, and disk only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    context = load_context()
    context["reports"].mkdir(parents=True, exist_ok=True)
    context["checkpoints"].mkdir(parents=True, exist_ok=True)
    context["work"].mkdir(parents=True, exist_ok=True)
    logger = W3Logger(context["reports"] / "w3_execution.log")
    environment: dict[str, Any] | None = None
    completed: list[dict[str, Any]] = []
    disk_events: list[dict[str, Any]] = []
    try:
        environment = environment_summary(context)
        initial_free = free_bytes(context["project_root"])
        if initial_free < MINIMUM_FREE_BYTES:
            raise SpaceGate("W3 start free space is below 60 GiB.")
        identities = []
        for source in context["sources"]:
            if not source["path"].is_file():
                raise FileNotFoundError(
                    f"Missing metadata source: {source['relative_path']}"
                )
            identity = input_identity(source["path"])
            verify_source_against_w2(source, identity)
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
                        "w2_status": context["w2_status"]["status"],
                        "free_bytes": initial_free,
                        "configuration_fingerprint": context[
                            "configuration_fingerprint"
                        ],
                        "filter_version": context["rules"]["filter"]["version"],
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
                f"W3 started; python={environment['python_version']}; "
                f"duckdb={environment['duckdb_version']}; "
                f"pyarrow={environment['pyarrow_version']}; "
                f"orjson={environment['orjson_version']}; "
                f"free_bytes={initial_free}; "
                f"filter_version={context['rules']['filter']['version']}"
            ),
        )
        disk_events.append(
            {
                "time": now_iso(),
                "event": "w3_start",
                "free_bytes": initial_free,
                "free_gib": initial_free / 1024**3,
            }
        )

        for source in context["sources"]:
            identity = input_identity(source["path"])
            summary = None
            if source["checkpoint_path"].is_file():
                checkpoint = json.loads(
                    source["checkpoint_path"].read_text(encoding="utf-8-sig")
                )
                if checkpoint_matches(
                    checkpoint,
                    source,
                    identity,
                    context["configuration_fingerprint"],
                ):
                    summary = checkpoint
                    logger.write(
                        "INFO",
                        f"Checkpoint reused; metadata scan skipped: id={source['id']}",
                    )
                    disk_events.append(
                        {
                            "time": now_iso(),
                            "event": "metadata_scan_skipped_checkpoint",
                            "source_id": source["id"],
                            "free_bytes": free_bytes(context["project_root"]),
                        }
                    )
            if summary is None:
                disk_events.append(
                    {
                        "time": now_iso(),
                        "event": "metadata_scan_start",
                        "source_id": source["id"],
                        "free_bytes": free_bytes(context["project_root"]),
                    }
                )
                summary = scan_source(source, context, logger)
                disk_events.append(
                    {
                        "time": now_iso(),
                        "event": "metadata_scan_complete",
                        "source_id": source["id"],
                        "free_bytes": free_bytes(context["project_root"]),
                        "records": summary["object_count"],
                        "candidates": summary["candidate_record_count"],
                        "duration_seconds": summary["duration_seconds"],
                    }
                )
            completed.append(summary)

        merged_rows, resolution_counts = merge_candidates(context, logger)
        duplicate_audit = duplicate_parent_audit(context, resolution_counts)
        candidate_stats = count_candidates(merged_rows)
        audit_rows, audit_fingerprint = build_audit_sample(merged_rows, context)
        audit_result = load_audit_decisions(
            context,
            audit_rows,
            audit_fingerprint,
        )
        status = generate_reports(
            context,
            environment,
            completed,
            duplicate_audit,
            candidate_stats,
            resolution_counts,
            audit_result,
            disk_events,
            logger,
        )
        return 0 if status["status"] in ("PASS", "PAUSED_AUDIT_REQUIRED") else 1
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
    except SourceMismatch as exc:
        write_failure_status(
            context,
            status="FAILED_SOURCE_MISMATCH",
            reason=str(exc),
            environment=environment,
            completed_ids=[item["id"] for item in completed],
        )
        logger.write("ERROR", str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 30
    except Exception as exc:
        write_failure_status(
            context,
            status="FAILED_PRODUCT_SELECTION",
            reason=f"{type(exc).__name__}: {exc}",
            environment=environment,
            completed_ids=[item["id"] for item in completed],
        )
        logger.write("ERROR", f"{type(exc).__name__}: {exc}")
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
