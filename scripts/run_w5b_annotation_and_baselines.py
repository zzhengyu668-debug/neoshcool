"""Run Phase W5-B annotation freezing and transparent review-level baselines.

Approved inputs are the two completed W5-A Excel workbooks and the two small
W5-A private Parquets.  The script never reads raw JSONL or gzip files, never
scores the full 55,877-review corpus, and never creates product-month failure
signals or future quality-deterioration targets.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import sys
import time
import tomllib
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    cohen_kappa_score,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
    precision_score,
    recall_score,
    roc_auc_score,
)


PHASE = "W5-B"
LABEL_VERSION = "w5b-labels-v1.0"
RANDOM_SEED = 20260731
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
FAILURE_TYPES = ("N0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")
DOUBLE_SOURCE = "adjudicated_double_review"
SINGLE_SOURCE = "reviewer_1_single_review"
EXPECTED_WORKBOOK_HASHES = {
    "adjudicated_workbook": (
        "8315df0c48a9783db37772d6cc936a7e5f1d3f1770add77bd63912169e793bc0"
    ),
    "reviewer_2_workbook": (
        "f2fe4ad6f4d979d7fdb2edb36730349bc9a19b0028fc3da50f55d21329792612"
    ),
}
MAIN_COLUMNS = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_1_failure_binary",
    "reviewer_1_failure_type",
    "reviewer_1_severity",
    "reviewer_1_persistence",
    "reviewer_1_confidence",
    "reviewer_1_notes",
    "adjudicated_failure_binary",
    "adjudicated_failure_type",
    "adjudicated_severity",
    "adjudicated_persistence",
    "adjudication_notes",
]
REVIEWER_2_COLUMNS = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_2_failure_binary",
    "reviewer_2_failure_type",
    "reviewer_2_severity",
    "reviewer_2_persistence",
    "reviewer_2_confidence",
    "reviewer_2_notes",
]
# Ordinary reports may name approved audit fields (for example, an aggregate
# duplicate-key uniqueness check), but must not contain row-level private
# identifiers or review text.  Row-level text is never passed to report writers;
# this token scan is an additional guard against user-identifier leakage.
REPORT_TEXT_FORBIDDEN_FIELDS = {
    "user_id_hash",
    "raw_user_id",
    "original_user_id",
}
MODEL_FORBIDDEN_FEATURES = {
    "rating",
    "low_star_indicator",
    "keyword_candidate_hit",
    "device_type",
    "parent_asin",
    "asin",
    "product_title",
    "review_datetime",
    "review_month",
    "user_id",
    "user_id_hash",
}
STAR_HEADER_RE = re.compile(
    r"^\s*(?:one|two|three|four|five)\s+stars?"
    r"\s*(?:[.!:;\-–—]+\s*)?(?:(?:\r?\n)+|$)",
    flags=re.IGNORECASE,
)


class W5BError(RuntimeError):
    """Controlled W5-B failure."""


class SplitReviewRequired(W5BError):
    """Raised when a chronological split lacks one binary class."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W5BError(f"Could not resolve project root from {__file__}")
    if not (root / "config" / "project.toml").is_file():
        raise W5BError("config/project.toml is missing")
    return root


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "path": relative(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }
    if include_hash:
        result["sha256"] = sha256_file(path)
    return result


def parquet_identity(root: Path, path: Path, include_hash: bool = True) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    result = file_identity(root, path, include_hash=include_hash)
    result.update(
        {
            "rows": parquet.metadata.num_rows,
            "fields": parquet.schema_arrow.names,
            "field_count": len(parquet.schema_arrow.names),
            "compression": sorted(
                {
                    parquet.metadata.row_group(group).column(column).compression
                    for group in range(parquet.metadata.num_row_groups)
                    for column in range(
                        parquet.metadata.row_group(group).num_columns
                    )
                }
            ),
        }
    )
    return result


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def process_peak_working_set_bytes() -> int | None:
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
    process = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    )
    return int(counters.PeakWorkingSetSize) if ok else None


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: json_ready(row.get(field, "")) for field in fields})
    temporary.replace(path)


def write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(path)


def log_message(log_path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp}\t{message}\n")


def assert_hash(path: Path, expected: str, label: str) -> None:
    observed = sha256_file(path)
    if observed != expected:
        raise W5BError(
            f"{label} SHA-256 mismatch: expected {expected}, observed {observed}"
        )


def assert_parquet(
    path: Path, expected_rows: int, expected_hash: str, label: str
) -> None:
    if not path.is_file():
        raise W5BError(f"{label} is missing: {path}")
    assert_hash(path, expected_hash, label)
    rows = pq.ParquetFile(path).metadata.num_rows
    if rows != expected_rows:
        raise W5BError(f"{label} row mismatch: expected {expected_rows}, got {rows}")


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    value = 0
    for character in letters.upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value - 1


def _xlsx_sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationship_id: str | None = None
    for sheet in workbook.findall(f".//{{{spreadsheet_ns}}}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            relationship_id = sheet.attrib.get(f"{{{office_rel_ns}}}id")
            break
    if relationship_id is None:
        raise W5BError(f"Worksheet {sheet_name!r} not found")

    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target: str | None = None
    for relationship in relationships.findall(f"{{{package_rel_ns}}}Relationship"):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib.get("Target")
            break
    if target is None:
        raise W5BError(f"Worksheet relationship missing for {sheet_name!r}")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        return str(target_path).lstrip("/")
    return str(PurePosixPath("xl") / target_path)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(path))
    strings: list[str] = []
    for shared_item in root:
        strings.append("".join(node.text or "" for node in shared_item.iter() if node.tag.endswith("}t")))
    return strings


def read_xlsx_table(path: Path, sheet_name: str = "Annotation") -> pd.DataFrame:
    """Read one simple .xlsx worksheet without altering or recalculating it."""
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path, "r") as archive:
        shared = _xlsx_shared_strings(archive)
        sheet_path = _xlsx_sheet_path(archive, sheet_name)
        root = ET.fromstring(archive.read(sheet_path))
        rows: list[dict[int, Any]] = []
        max_column = -1
        for row_node in root.findall(f".//{{{spreadsheet_ns}}}sheetData/{{{spreadsheet_ns}}}row"):
            cells: dict[int, Any] = {}
            for cell in row_node.findall(f"{{{spreadsheet_ns}}}c"):
                reference = cell.attrib.get("r", "")
                column = _column_index(reference)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{spreadsheet_ns}}}v")
                value: Any = ""
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter() if node.tag.endswith("}t")
                    )
                elif value_node is not None:
                    raw = value_node.text or ""
                    if cell_type == "s":
                        value = shared[int(raw)]
                    elif cell_type == "b":
                        value = raw == "1"
                    elif cell_type in {"str", "e"}:
                        value = raw
                    else:
                        try:
                            numeric = float(raw)
                            value = int(numeric) if numeric.is_integer() else numeric
                        except ValueError:
                            value = raw
                cells[column] = value
                max_column = max(max_column, column)
            rows.append(cells)
    if not rows or max_column < 0:
        raise W5BError(f"No tabular values found in {path}")
    matrix = [
        [row.get(column, "") for column in range(max_column + 1)] for row in rows
    ]
    headers = [str(value).strip() for value in matrix[0]]
    if not all(headers):
        raise W5BError(f"Blank header found in {path}")
    return pd.DataFrame(matrix[1:], columns=headers)


def blank(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def normalize_binary(value: Any) -> str:
    if blank(value):
        return ""
    if isinstance(value, (int, np.integer)):
        text = str(int(value))
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value).strip().lower()
    if text not in {"0", "1", "uncertain"}:
        raise W5BError(f"Illegal failure_binary value: {value!r}")
    return text


def normalize_ordinal(value: Any, allowed: set[int]) -> int | None:
    if blank(value):
        return None
    try:
        result = int(float(str(value).strip()))
    except ValueError as exc:
        raise W5BError(f"Illegal ordinal value: {value!r}") from exc
    if result not in allowed:
        raise W5BError(f"Ordinal {result} is not in {sorted(allowed)}")
    return result


def normalize_failure_type(value: Any) -> str:
    if blank(value):
        return ""
    raw_codes = re.split(r"[;,|/\s]+", str(value).strip().upper())
    codes = {code for code in raw_codes if code}
    illegal = codes.difference(FAILURE_TYPES)
    if illegal:
        raise W5BError(f"Illegal failure type code(s): {sorted(illegal)}")
    return ";".join(code for code in FAILURE_TYPES if code in codes)


def failure_type_set(value: Any) -> set[str]:
    normalized = normalize_failure_type(value)
    return set(normalized.split(";")) if normalized else set()


def validate_label_combination(
    binary: str,
    failure_type: str,
    severity: int | None,
    persistence: int | None,
    row_id: str,
) -> None:
    codes = failure_type_set(failure_type)
    if binary == "0":
        if codes != {"N0"} or severity != 0 or persistence != 0:
            raise W5BError(
                f"{row_id}: non-failure must be N0/severity 0/persistence 0"
            )
    elif binary == "1":
        if not codes or "N0" in codes or not codes.issubset(set(FAILURE_TYPES[1:])):
            raise W5BError(f"{row_id}: failure must use one or more F1-F8 codes")
        if severity not in {1, 2, 3} or persistence not in {0, 1, 2}:
            raise W5BError(f"{row_id}: failure severity/persistence is incomplete")
    elif binary == "uncertain":
        if failure_type or severity is not None or persistence is not None:
            raise W5BError(f"{row_id}: uncertain must leave dependent labels blank")
    else:
        raise W5BError(f"{row_id}: missing or illegal failure_binary")


def validate_workbooks(
    main: pd.DataFrame, reviewer_2: pd.DataFrame
) -> tuple[set[str], dict[str, Any]]:
    if list(main.columns) != MAIN_COLUMNS:
        raise W5BError(f"Main workbook columns differ from approved schema: {main.columns}")
    if list(reviewer_2.columns) != REVIEWER_2_COLUMNS:
        raise W5BError(
            f"Reviewer 2 workbook columns differ from approved schema: {reviewer_2.columns}"
        )
    if len(main) != 300 or len(reviewer_2) != 60:
        raise W5BError(
            f"Workbook row mismatch: main={len(main)}, reviewer2={len(reviewer_2)}"
        )
    if not main["blind_review_id"].is_unique or not reviewer_2["blind_review_id"].is_unique:
        raise W5BError("blind_review_id must be unique in each workbook")
    main_ids = set(main["blind_review_id"].astype(str))
    double_ids = set(reviewer_2["blind_review_id"].astype(str))
    if not double_ids.issubset(main_ids):
        raise W5BError("Reviewer 2 contains IDs not found in the 300-row workbook")

    main_index = main.set_index("blind_review_id", drop=False)
    reviewer_2_index = reviewer_2.set_index("blind_review_id", drop=False)
    text_mismatches = 0
    device_mismatches = 0
    for row_id in sorted(double_ids):
        if str(main_index.at[row_id, "review_text"]) != str(
            reviewer_2_index.at[row_id, "review_text"]
        ):
            text_mismatches += 1
        if str(main_index.at[row_id, "device_type"]) != str(
            reviewer_2_index.at[row_id, "device_type"]
        ):
            device_mismatches += 1
    if text_mismatches or device_mismatches:
        raise W5BError(
            f"Double-review identity mismatch: text={text_mismatches}, "
            f"device={device_mismatches}"
        )

    double_final_count = 0
    non_double_adjudication_count = 0
    for _, row in main.iterrows():
        row_id = str(row["blind_review_id"])
        r1_binary = normalize_binary(row["reviewer_1_failure_binary"])
        r1_type = normalize_failure_type(row["reviewer_1_failure_type"])
        r1_severity = normalize_ordinal(row["reviewer_1_severity"], {0, 1, 2, 3})
        r1_persistence = normalize_ordinal(row["reviewer_1_persistence"], {0, 1, 2})
        validate_label_combination(
            r1_binary, r1_type, r1_severity, r1_persistence, f"{row_id}/reviewer1"
        )
        adjudication_values = [
            row["adjudicated_failure_binary"],
            row["adjudicated_failure_type"],
            row["adjudicated_severity"],
            row["adjudicated_persistence"],
        ]
        if row_id in double_ids:
            final_binary = normalize_binary(row["adjudicated_failure_binary"])
            final_type = normalize_failure_type(row["adjudicated_failure_type"])
            final_severity = normalize_ordinal(
                row["adjudicated_severity"], {0, 1, 2, 3}
            )
            final_persistence = normalize_ordinal(
                row["adjudicated_persistence"], {0, 1, 2}
            )
            validate_label_combination(
                final_binary,
                final_type,
                final_severity,
                final_persistence,
                f"{row_id}/adjudicated",
            )
            double_final_count += 1
        elif any(not blank(value) for value in adjudication_values):
            non_double_adjudication_count += 1

    for _, row in reviewer_2.iterrows():
        row_id = str(row["blind_review_id"])
        binary = normalize_binary(row["reviewer_2_failure_binary"])
        failure_type = normalize_failure_type(row["reviewer_2_failure_type"])
        severity = normalize_ordinal(row["reviewer_2_severity"], {0, 1, 2, 3})
        persistence = normalize_ordinal(row["reviewer_2_persistence"], {0, 1, 2})
        validate_label_combination(
            binary, failure_type, severity, persistence, f"{row_id}/reviewer2"
        )
    if double_final_count != 60 or non_double_adjudication_count != 0:
        raise W5BError(
            "Adjudication scope mismatch: "
            f"double={double_final_count}, non_double={non_double_adjudication_count}"
        )
    return double_ids, {
        "main_rows": len(main),
        "reviewer_2_rows": len(reviewer_2),
        "main_unique_ids": int(main["blind_review_id"].nunique()),
        "reviewer_2_unique_ids": int(reviewer_2["blind_review_id"].nunique()),
        "double_review_text_mismatches": text_mismatches,
        "double_review_device_mismatches": device_mismatches,
        "double_rows_with_valid_adjudication": double_final_count,
        "non_double_rows_with_adjudication": non_double_adjudication_count,
        "status": "ADJUDICATION_VALIDATED",
    }


def build_final_labels(
    main: pd.DataFrame,
    reviewer_2: pd.DataFrame,
    blind_key: pd.DataFrame,
    sampling_frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    double_ids, validation = validate_workbooks(main, reviewer_2)
    if len(blind_key) != 300 or len(sampling_frame) != 300:
        raise W5BError("W5-A private input row count differs from 300")
    if not blind_key["blind_review_id"].is_unique:
        raise W5BError("blind_review_key blind_review_id is not unique")
    if not blind_key["duplicate_key"].is_unique:
        raise W5BError("blind_review_key duplicate_key is not unique")
    if not sampling_frame["blind_review_id"].is_unique:
        raise W5BError("annotation_sampling_frame blind_review_id is not unique")

    merged_private = blind_key.merge(
        sampling_frame[
            [
                "blind_review_id",
                "device_type",
                "review_text",
                "keyword_candidate_hit",
                "selected_for_double_review",
            ]
        ],
        on="blind_review_id",
        how="inner",
        validate="one_to_one",
        suffixes=("_key", "_frame"),
    )
    if len(merged_private) != 300:
        raise W5BError("Private mapping and sampling frame do not join one-to-one")
    keyword_mismatch = (
        merged_private["keyword_candidate_hit_key"].astype(bool)
        != merged_private["keyword_candidate_hit_frame"].astype(bool)
    ).sum()
    if keyword_mismatch:
        raise W5BError(f"Keyword candidate mismatch across private inputs: {keyword_mismatch}")
    selected_double = set(
        merged_private.loc[
            merged_private["selected_for_double_review"].astype(bool),
            "blind_review_id",
        ].astype(str)
    )
    if selected_double != double_ids:
        raise W5BError("Reviewer 2 IDs differ from W5-A selected double-review IDs")

    main_index = main.set_index("blind_review_id", drop=False)
    rows: list[dict[str, Any]] = []
    modeling_rows: list[dict[str, Any]] = []
    star_headers_removed = 0
    for private in merged_private.to_dict(orient="records"):
        row_id = str(private["blind_review_id"])
        annotation = main_index.loc[row_id]
        if row_id in double_ids:
            prefix = "adjudicated"
            source = DOUBLE_SOURCE
        else:
            prefix = "reviewer_1"
            source = SINGLE_SOURCE
        binary = normalize_binary(annotation[f"{prefix}_failure_binary"])
        failure_type = normalize_failure_type(annotation[f"{prefix}_failure_type"])
        severity = normalize_ordinal(
            annotation[f"{prefix}_severity"], {0, 1, 2, 3}
        )
        persistence = normalize_ordinal(
            annotation[f"{prefix}_persistence"], {0, 1, 2}
        )
        validate_label_combination(
            binary, failure_type, severity, persistence, f"{row_id}/final"
        )
        review_datetime = pd.to_datetime(private["review_datetime"], utc=True)
        keyword_hit = bool(private["keyword_candidate_hit_key"])
        label_status = "definite" if binary in {"0", "1"} else "uncertain"
        final_row = {
            "blind_review_id": row_id,
            "duplicate_key": str(private["duplicate_key"]),
            "parent_asin": str(private["parent_asin"]),
            "device_type": str(private["device_type"]),
            "review_datetime": review_datetime,
            "final_failure_binary": binary,
            "final_failure_type": failure_type or None,
            "final_severity": severity,
            "final_persistence": persistence,
            "annotation_source": source,
            "label_status": label_status,
            "annotation_version": LABEL_VERSION,
            "keyword_candidate_hit": keyword_hit,
        }
        rows.append(final_row)

        review_text = str(private["review_text"])
        model_text, substitutions = STAR_HEADER_RE.subn("", review_text, count=1)
        if substitutions:
            star_headers_removed += 1
        modeling_rows.append(
            {
                **final_row,
                "review_text": review_text,
                "model_text": model_text,
                "rating": float(private["rating"]),
                "low_star_indicator": int(float(private["rating"]) <= 2.0),
                "split": None,
            }
        )

    labels = pd.DataFrame(rows).sort_values("blind_review_id").reset_index(drop=True)
    modeling = pd.DataFrame(modeling_rows).sort_values(
        "blind_review_id"
    ).reset_index(drop=True)
    if len(labels) != 300 or not labels["blind_review_id"].is_unique:
        raise W5BError("Frozen final label table must contain 300 unique rows")
    if not labels["duplicate_key"].is_unique:
        raise W5BError("Frozen final labels must have unique duplicate_key")
    validation["leading_star_title_removed_rows"] = star_headers_removed
    validation["double_review_ids"] = len(double_ids)
    validation["single_review_ids"] = 300 - len(double_ids)
    return labels, modeling, validation


def _safe_kappa(left: Sequence[Any], right: Sequence[Any], weights: str | None = None) -> float | None:
    if not left:
        return None
    value = cohen_kappa_score(left, right, weights=weights)
    return None if np.isnan(value) else float(value)


def matrix_payload(
    left: Sequence[Any], right: Sequence[Any], labels: list[Any]
) -> dict[str, Any]:
    matrix = confusion_matrix(left, right, labels=labels)
    return {
        "row_labels_reviewer_1": labels,
        "column_labels_reviewer_2": labels,
        "matrix": matrix.tolist(),
    }


def inter_annotator_agreement(
    main: pd.DataFrame, reviewer_2: pd.DataFrame
) -> dict[str, Any]:
    double = main.merge(
        reviewer_2,
        on=["blind_review_id", "device_type", "review_text"],
        how="inner",
        validate="one_to_one",
    )
    if len(double) != 60:
        raise W5BError(f"Expected 60 independent double reviews, got {len(double)}")
    r1_binary = [
        normalize_binary(value) for value in double["reviewer_1_failure_binary"]
    ]
    r2_binary = [
        normalize_binary(value) for value in double["reviewer_2_failure_binary"]
    ]
    binary_labels = ["0", "1", "uncertain"]
    include_agreement = accuracy_score(r1_binary, r2_binary)
    include_kappa = _safe_kappa(r1_binary, r2_binary)
    definite_mask = [
        left in {"0", "1"} and right in {"0", "1"}
        for left, right in zip(r1_binary, r2_binary)
    ]
    definite_left = [value for value, keep in zip(r1_binary, definite_mask) if keep]
    definite_right = [value for value, keep in zip(r2_binary, definite_mask) if keep]

    label_matrix_1 = np.array(
        [
            [int(code in failure_type_set(value)) for code in FAILURE_TYPES]
            for value in double["reviewer_1_failure_type"]
        ]
    )
    label_matrix_2 = np.array(
        [
            [int(code in failure_type_set(value)) for code in FAILURE_TYPES]
            for value in double["reviewer_2_failure_type"]
        ]
    )
    exact_matches = np.all(label_matrix_1 == label_matrix_2, axis=1)
    jaccard_values: list[float] = []
    for left, right in zip(label_matrix_1, label_matrix_2):
        union = int(np.logical_or(left, right).sum())
        intersection = int(np.logical_and(left, right).sum())
        jaccard_values.append(1.0 if union == 0 else intersection / union)
    micro = precision_recall_fscore_support(
        label_matrix_1,
        label_matrix_2,
        average="micro",
        zero_division=0,
    )
    macro = precision_recall_fscore_support(
        label_matrix_1,
        label_matrix_2,
        average="macro",
        zero_division=0,
    )
    per_type: list[dict[str, Any]] = []
    for index, code in enumerate(FAILURE_TYPES):
        left = label_matrix_1[:, index]
        right = label_matrix_2[:, index]
        per_type.append(
            {
                "failure_type": code,
                "reviewer_1_positive": int(left.sum()),
                "reviewer_2_positive": int(right.sum()),
                "both_positive": int(np.logical_and(left, right).sum()),
                "presence_absence_agreement": int((left == right).sum()),
                "presence_absence_agreement_rate": float((left == right).mean()),
            }
        )

    def ordinal_agreement(field: str, allowed: set[int]) -> dict[str, Any]:
        left_values: list[int] = []
        right_values: list[int] = []
        for left_raw, right_raw in zip(
            double[f"reviewer_1_{field}"], double[f"reviewer_2_{field}"]
        ):
            left = normalize_ordinal(left_raw, allowed)
            right = normalize_ordinal(right_raw, allowed)
            if left is not None and right is not None:
                left_values.append(left)
                right_values.append(right)
        return {
            "valid_comparisons": len(left_values),
            "excluded_blank_or_uncertain": 60 - len(left_values),
            "raw_agreement": (
                float(accuracy_score(left_values, right_values))
                if left_values
                else None
            ),
            "linear_weighted_cohens_kappa": _safe_kappa(
                left_values, right_values, weights="linear"
            ),
            "quadratic_weighted_cohens_kappa": _safe_kappa(
                left_values, right_values, weights="quadratic"
            ),
            "confusion_matrix": matrix_payload(
                left_values, right_values, sorted(allowed)
            ),
        }

    r1_confidence = [str(value).strip().lower() for value in double["reviewer_1_confidence"]]
    r2_confidence = [str(value).strip().lower() for value in double["reviewer_2_confidence"]]
    return {
        "double_review_rows": len(double),
        "failure_binary": {
            "including_uncertain": {
                "valid_comparisons": 60,
                "raw_agreement": float(include_agreement),
                "cohens_kappa": include_kappa,
                "confusion_matrix": matrix_payload(
                    r1_binary, r2_binary, binary_labels
                ),
            },
            "excluding_rows_where_either_is_uncertain": {
                "valid_comparisons": len(definite_left),
                "excluded_rows": 60 - len(definite_left),
                "raw_agreement": float(
                    accuracy_score(definite_left, definite_right)
                ),
                "cohens_kappa": _safe_kappa(definite_left, definite_right),
                "confusion_matrix": matrix_payload(
                    definite_left, definite_right, ["0", "1"]
                ),
            },
        },
        "failure_type_multilabel": {
            "labels": list(FAILURE_TYPES),
            "exact_match_count": int(exact_matches.sum()),
            "exact_match_agreement": float(exact_matches.mean()),
            "mean_jaccard_similarity": float(np.mean(jaccard_values)),
            "median_jaccard_similarity": float(np.median(jaccard_values)),
            "micro_presence_absence_agreement": float(
                (label_matrix_1 == label_matrix_2).mean()
            ),
            "macro_presence_absence_agreement": float(
                np.mean(
                    [
                        (label_matrix_1[:, index] == label_matrix_2[:, index]).mean()
                        for index in range(len(FAILURE_TYPES))
                    ]
                )
            ),
            "micro_precision": float(micro[0]),
            "micro_recall": float(micro[1]),
            "micro_f1": float(micro[2]),
            "macro_precision": float(macro[0]),
            "macro_recall": float(macro[1]),
            "macro_f1": float(macro[2]),
            "per_type": per_type,
        },
        "severity": ordinal_agreement("severity", {0, 1, 2, 3}),
        "persistence": ordinal_agreement("persistence", {0, 1, 2}),
        "confidence_descriptive": {
            "raw_agreement": float(accuracy_score(r1_confidence, r2_confidence)),
            "reviewer_1_counts": dict(sorted(Counter(r1_confidence).items())),
            "reviewer_2_counts": dict(sorted(Counter(r2_confidence).items())),
            "used_as_final_research_label": False,
        },
    }


def chronological_split(modeling: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    definite = modeling.loc[modeling["label_status"] == "definite"].copy()
    definite["review_datetime"] = pd.to_datetime(
        definite["review_datetime"], utc=True
    )
    definite = definite.sort_values(
        ["review_datetime", "blind_review_id"], kind="mergesort"
    ).reset_index(drop=True)
    n_rows = len(definite)
    train_end = math.floor(n_rows * 0.60)
    validation_end = math.floor(n_rows * 0.80)
    definite.loc[: train_end - 1, "split"] = "train"
    definite.loc[train_end : validation_end - 1, "split"] = "validation"
    definite.loc[validation_end:, "split"] = "test"
    if definite["split"].isna().any():
        raise W5BError("Chronological split left definite rows unassigned")

    split_rows: list[dict[str, Any]] = []
    for split_name in ("train", "validation", "test"):
        subset = definite.loc[definite["split"] == split_name]
        counts = Counter(subset["final_failure_binary"])
        split_rows.append(
            {
                "split": split_name,
                "rows": len(subset),
                "failure_0": int(counts.get("0", 0)),
                "failure_1": int(counts.get("1", 0)),
                "smart_plug": int((subset["device_type"] == "smart_plug").sum()),
                "smart_bulb": int((subset["device_type"] == "smart_bulb").sum()),
                "smart_switch": int((subset["device_type"] == "smart_switch").sum()),
                "earliest_utc": subset["review_datetime"].min(),
                "latest_utc": subset["review_datetime"].max(),
                "unique_parent_asin": int(subset["parent_asin"].nunique()),
            }
        )
        if counts.get("0", 0) == 0 or counts.get("1", 0) == 0:
            raise SplitReviewRequired(
                f"{split_name} lacks one binary class: {dict(counts)}"
            )
    train_max = definite.loc[
        definite["split"] == "train", "review_datetime"
    ].max()
    validation_min = definite.loc[
        definite["split"] == "validation", "review_datetime"
    ].min()
    validation_max = definite.loc[
        definite["split"] == "validation", "review_datetime"
    ].max()
    test_min = definite.loc[definite["split"] == "test", "review_datetime"].min()
    if train_max > validation_min or validation_max > test_min:
        raise W5BError("Chronological split ordering is invalid")
    duplicate_crossovers = (
        definite.groupby("duplicate_key")["split"].nunique().gt(1).sum()
    )
    if duplicate_crossovers:
        raise W5BError("duplicate_key appears in more than one split")
    return definite, {
        "method": "strict_chronological",
        "sort_fields": ["review_datetime", "blind_review_id"],
        "random_shuffle": False,
        "definite_rows": n_rows,
        "uncertain_rows_excluded": int(
            (modeling["label_status"] == "uncertain").sum()
        ),
        "train_end_index_exclusive": train_end,
        "validation_end_index_exclusive": validation_end,
        "split_rows": split_rows,
        "chronological_order_validated": True,
        "duplicate_key_cross_split_count": int(duplicate_crossovers),
    }


def binary_metrics(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    scores: Sequence[float] | None = None,
) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=int)
    predicted = np.asarray(y_pred, dtype=int)
    matrix = confusion_matrix(truth, predicted, labels=[0, 1])
    tn, fp, fn, tp = matrix.ravel()
    specificity = float(tn / (tn + fp)) if tn + fp else None
    result: dict[str, Any] = {
        "n": len(truth),
        "true_failure_0": int((truth == 0).sum()),
        "true_failure_1": int((truth == 1).sum()),
        "confusion_matrix": {
            "labels": [0, 1],
            "matrix": matrix.tolist(),
            "tn": int(tn),
            "fp": int(fp),
            "fn": int(fn),
            "tp": int(tp),
        },
        "accuracy": float(accuracy_score(truth, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(truth, predicted)),
        "precision": float(precision_score(truth, predicted, zero_division=0)),
        "recall": float(recall_score(truth, predicted, zero_division=0)),
        "f1": float(f1_score(truth, predicted, zero_division=0)),
        "specificity": specificity,
    }
    if scores is not None and len(set(truth.tolist())) == 2:
        score_array = np.asarray(scores, dtype=float)
        result["roc_auc"] = float(roc_auc_score(truth, score_array))
        result["pr_auc"] = float(average_precision_score(truth, score_array))
        result["roc_auc_pr_auc_available"] = True
    else:
        result["roc_auc"] = None
        result["pr_auc"] = None
        result["roc_auc_pr_auc_available"] = False
    return result


def evaluate_fixed_predictor(
    definite: pd.DataFrame, prediction_column: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scope, subset in (
        ("all_definite", definite),
        ("test", definite.loc[definite["split"] == "test"]),
    ):
        result[scope] = binary_metrics(
            subset["final_failure_binary"].astype(int),
            subset[prediction_column].astype(int),
        )
    return result


def device_support_report(
    test: pd.DataFrame,
    prediction_column: str,
    score_column: str | None,
    minimum_rows: int,
    minimum_per_class: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device_type in DEVICE_TYPES:
        subset = test.loc[test["device_type"] == device_type]
        label_counts = Counter(subset["final_failure_binary"].astype(int))
        sufficient = (
            len(subset) >= minimum_rows
            and label_counts.get(0, 0) >= minimum_per_class
            and label_counts.get(1, 0) >= minimum_per_class
        )
        row: dict[str, Any] = {
            "device_type": device_type,
            "test_rows": len(subset),
            "failure_0": int(label_counts.get(0, 0)),
            "failure_1": int(label_counts.get(1, 0)),
            "support_status": "SUFFICIENT" if sufficient else "INSUFFICIENT_SUPPORT",
        }
        if sufficient:
            scores = subset[score_column] if score_column else None
            row["metrics"] = binary_metrics(
                subset["final_failure_binary"].astype(int),
                subset[prediction_column].astype(int),
                scores,
            )
        rows.append(row)
    return rows


def model_and_evaluate(
    definite: pd.DataFrame, config: dict[str, Any]
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, Any],
]:
    working = definite.copy()
    working["final_failure_binary_int"] = working["final_failure_binary"].astype(int)
    working["rating_prediction"] = working["low_star_indicator"].astype(int)
    working["keyword_prediction"] = working["keyword_candidate_hit"].astype(int)
    rating_evaluation = evaluate_fixed_predictor(working, "rating_prediction")
    keyword_evaluation = evaluate_fixed_predictor(working, "keyword_prediction")

    train = working.loc[working["split"] == "train"].copy()
    validation = working.loc[working["split"] == "validation"].copy()
    test = working.loc[working["split"] == "test"].copy()
    dummy = DummyClassifier(
        strategy=config["dummy_baseline"]["strategy"],
        random_state=config["dummy_baseline"]["random_state"],
    )
    dummy.fit(np.zeros((len(train), 1)), train["final_failure_binary_int"])
    dummy_evaluation: dict[str, Any] = {
        "strategy": config["dummy_baseline"]["strategy"],
        "fit_split": "train",
        "random_state": config["dummy_baseline"]["random_state"],
        "evaluations": {},
    }
    working["dummy_prediction"] = dummy.predict(
        np.zeros((len(working), 1))
    ).astype(int)
    dummy_class_index = list(dummy.classes_).index(1)
    working["dummy_probability"] = dummy.predict_proba(
        np.zeros((len(working), 1))
    )[:, dummy_class_index]
    for split_name in ("validation", "test"):
        subset = working.loc[working["split"] == split_name]
        dummy_evaluation["evaluations"][split_name] = binary_metrics(
            subset["final_failure_binary_int"],
            subset["dummy_prediction"],
            subset["dummy_probability"],
        )

    vectorizer = TfidfVectorizer(
        lowercase=config["tfidf"]["lowercase"],
        ngram_range=(
            config["tfidf"]["ngram_min"],
            config["tfidf"]["ngram_max"],
        ),
        min_df=config["tfidf"]["min_df"],
        max_features=config["tfidf"]["max_features"],
        sublinear_tf=config["tfidf"]["sublinear_tf"],
        strip_accents=config["tfidf"]["strip_accents"],
    )
    classifier = LogisticRegression(
        C=config["logistic_regression"]["C"],
        class_weight=config["logistic_regression"]["class_weight"],
        max_iter=config["logistic_regression"]["max_iter"],
        random_state=config["logistic_regression"]["random_state"],
    )
    train_matrix = vectorizer.fit_transform(train["model_text"].fillna(""))
    classifier.fit(train_matrix, train["final_failure_binary_int"])
    feature_names = vectorizer.get_feature_names_out()
    positive_class_index = list(classifier.classes_).index(1)
    coefficients = (
        classifier.coef_[0]
        if classifier.coef_.shape[0] == 1
        else classifier.coef_[positive_class_index]
    )

    working["tfidf_prediction"] = pd.Series(pd.NA, index=working.index, dtype="Int64")
    working["tfidf_probability"] = np.nan
    tfidf_evaluations: dict[str, Any] = {
        "configuration": {
            "lowercase": vectorizer.lowercase,
            "ngram_range": list(vectorizer.ngram_range),
            "min_df": vectorizer.min_df,
            "max_features": vectorizer.max_features,
            "sublinear_tf": vectorizer.sublinear_tf,
            "strip_accents": vectorizer.strip_accents,
            "C": classifier.C,
            "class_weight": classifier.class_weight,
            "max_iter": classifier.max_iter,
            "random_state": classifier.random_state,
            "decision_threshold": config["logistic_regression"][
                "decision_threshold"
            ],
            "feature_field": "model_text",
            "fit_vocabulary_on": "train_only",
        },
        "vocabulary_size": len(feature_names),
        "evaluations": {},
    }
    for split_name in ("train", "validation", "test"):
        mask = working["split"] == split_name
        subset = working.loc[mask]
        matrix = vectorizer.transform(subset["model_text"].fillna(""))
        probabilities = classifier.predict_proba(matrix)[:, positive_class_index]
        predictions = (
            probabilities >= config["logistic_regression"]["decision_threshold"]
        ).astype(int)
        working.loc[mask, "tfidf_prediction"] = predictions
        working.loc[mask, "tfidf_probability"] = probabilities
        tfidf_evaluations["evaluations"][split_name] = binary_metrics(
            subset["final_failure_binary_int"], predictions, probabilities
        )
    train_f1 = tfidf_evaluations["evaluations"]["train"]["f1"]
    validation_f1 = tfidf_evaluations["evaluations"]["validation"]["f1"]
    test_f1 = tfidf_evaluations["evaluations"]["test"]["f1"]
    train_validation_gap = train_f1 - validation_f1
    train_test_gap = train_f1 - test_f1
    overfit_flag = (
        "POTENTIAL_OVERFITTING_TRAIN_VALIDATION_GAP"
        if train_validation_gap >= 0.15
        else "NO_LARGE_F1_GAP_DETECTED"
    )
    tfidf_evaluations["overfitting_diagnostic"] = {
        "status": overfit_flag,
        "train_f1": train_f1,
        "validation_f1": validation_f1,
        "test_f1": test_f1,
        "train_minus_validation_f1": train_validation_gap,
        "train_minus_test_f1": train_test_gap,
        "diagnostic_threshold": 0.15,
        "interpretation": (
            "The near-perfect training fit and materially lower validation "
            "performance indicate overfitting risk in this small sample. The "
            "higher test score does not remove that risk because validation "
            "and test sets are small chronological slices."
            if overfit_flag == "POTENTIAL_OVERFITTING_TRAIN_VALIDATION_GAP"
            else "No train-validation F1 gap of at least 0.15 was observed."
        ),
    }

    support_min_rows = config["device_support"]["minimum_test_rows"]
    support_min_class = config["device_support"]["minimum_rows_per_binary_class"]
    test_with_predictions = working.loc[working["split"] == "test"]
    tfidf_evaluations["test_by_device_type_exploratory"] = device_support_report(
        test_with_predictions,
        "tfidf_prediction",
        "tfidf_probability",
        support_min_rows,
        support_min_class,
    )
    rating_evaluation["test_by_device_type_exploratory"] = device_support_report(
        test_with_predictions,
        "rating_prediction",
        None,
        support_min_rows,
        support_min_class,
    )
    keyword_evaluation["test_by_device_type_exploratory"] = device_support_report(
        test_with_predictions,
        "keyword_prediction",
        None,
        support_min_rows,
        support_min_class,
    )

    positive_indices = np.argsort(coefficients)[-20:][::-1]
    negative_indices = np.argsort(coefficients)[:20]
    feature_rows: list[dict[str, Any]] = []
    for rank, index in enumerate(positive_indices, start=1):
        feature_rows.append(
            {
                "direction": "positive_failure_association",
                "rank": rank,
                "ngram": feature_names[index],
                "coefficient": float(coefficients[index]),
            }
        )
    for rank, index in enumerate(negative_indices, start=1):
        feature_rows.append(
            {
                "direction": "negative_failure_association",
                "rank": rank,
                "ngram": feature_names[index],
                "coefficient": float(coefficients[index]),
            }
        )
    feature_frame = pd.DataFrame(feature_rows)

    error_mask = (
        (working["split"] == "test")
        & (
            working["tfidf_prediction"].astype("Int64")
            != working["final_failure_binary_int"].astype("Int64")
        )
    )
    private_error_columns = [
        "blind_review_id",
        "device_type",
        "review_datetime",
        "review_text",
        "model_text",
        "final_failure_binary",
        "final_failure_type",
        "final_severity",
        "final_persistence",
        "tfidf_prediction",
        "tfidf_probability",
    ]
    errors_private = working.loc[error_mask, private_error_columns].copy()
    false_positives = errors_private.loc[
        errors_private["final_failure_binary"].astype(str) == "0"
    ]
    false_negatives = errors_private.loc[
        errors_private["final_failure_binary"].astype(str) == "1"
    ]
    fn_type_counts: Counter[str] = Counter()
    for value in false_negatives["final_failure_type"]:
        fn_type_counts.update(failure_type_set(value))
    error_summary = {
        "test_rows": len(test),
        "false_positive_count": len(false_positives),
        "false_negative_count": len(false_negatives),
        "false_negative_failure_type_counts_overlapping": dict(
            sorted(fn_type_counts.items())
        ),
        "errors_by_device_type": {
            device: int((errors_private["device_type"] == device).sum())
            for device in DEVICE_TYPES
        },
        "test_error_text_saved_only_in_private_parquet": True,
        "test_errors_used_for_retraining": False,
    }

    prediction_columns = [
        "blind_review_id",
        "split",
        "device_type",
        "review_datetime",
        "final_failure_binary",
        "rating_prediction",
        "keyword_prediction",
        "dummy_prediction",
        "dummy_probability",
        "tfidf_prediction",
        "tfidf_probability",
    ]
    predictions = working[prediction_columns].copy()
    model_bundle = {
        "vectorizer": vectorizer,
        "classifier": classifier,
        "label_version": LABEL_VERSION,
        "random_seed": RANDOM_SEED,
        "decision_threshold": config["logistic_regression"]["decision_threshold"],
        "feature_field": "model_text",
        "forbidden_features": sorted(MODEL_FORBIDDEN_FEATURES),
        "training_rows": len(train),
        "training_date_min": train["review_datetime"].min().isoformat(),
        "training_date_max": train["review_datetime"].max().isoformat(),
    }
    return (
        rating_evaluation,
        keyword_evaluation,
        dummy_evaluation,
        working,
        predictions,
        feature_frame,
        {
            "evaluation": tfidf_evaluations,
            "error_summary": error_summary,
            "errors_private": errors_private,
            "model_bundle": model_bundle,
        },
    )


def confusion_rows(
    evaluations: dict[str, tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model_name, (scope_name, evaluation) in evaluations.items():
        matrix = evaluation["confusion_matrix"]["matrix"]
        for true_label in (0, 1):
            for predicted_label in (0, 1):
                rows.append(
                    {
                        "model": model_name,
                        "scope": scope_name,
                        "true_label": true_label,
                        "predicted_label": predicted_label,
                        "count": matrix[true_label][predicted_label],
                    }
                )
    return rows


def final_label_reports(labels: pd.DataFrame) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    definite = labels.loc[labels["label_status"] == "definite"]
    uncertain_ids = labels.loc[
        labels["label_status"] == "uncertain", "blind_review_id"
    ].tolist()
    type_counts: Counter[str] = Counter()
    for value in definite["final_failure_type"]:
        type_counts.update(failure_type_set(value))
    rows: list[dict[str, Any]] = []
    for scope, subset in [("overall", labels)] + [
        (device, labels.loc[labels["device_type"] == device])
        for device in DEVICE_TYPES
    ]:
        for status in ("definite", "uncertain"):
            status_subset = subset.loc[subset["label_status"] == status]
            rows.append(
                {
                    "scope": scope,
                    "label_status": status,
                    "rows": len(status_subset),
                    "failure_0": int(
                        (status_subset["final_failure_binary"] == "0").sum()
                    ),
                    "failure_1": int(
                        (status_subset["final_failure_binary"] == "1").sum()
                    ),
                    "uncertain": int(
                        (status_subset["final_failure_binary"] == "uncertain").sum()
                    ),
                }
            )
    summary = {
        "annotation_version": LABEL_VERSION,
        "rows": len(labels),
        "definite_rows": len(definite),
        "uncertain_rows": len(uncertain_ids),
        "uncertain_blind_review_ids": uncertain_ids,
        "failure_0": int((definite["final_failure_binary"] == "0").sum()),
        "failure_1": int((definite["final_failure_binary"] == "1").sum()),
        "annotation_source_counts": dict(
            sorted(Counter(labels["annotation_source"]).items())
        ),
        "failure_type_counts_overlapping": dict(sorted(type_counts.items())),
        "severity_counts": {
            str(key): int(value)
            for key, value in sorted(
                Counter(
                    definite["final_severity"].dropna().astype(int)
                ).items()
            )
        },
        "persistence_counts": {
            str(key): int(value)
            for key, value in sorted(
                Counter(
                    definite["final_persistence"].dropna().astype(int)
                ).items()
            )
        },
        "stratified_annotation_sample_is_population_representative": False,
    }
    return summary, rows


def inter_annotator_markdown(agreement: dict[str, Any]) -> str:
    binary_all = agreement["failure_binary"]["including_uncertain"]
    binary_definite = agreement["failure_binary"][
        "excluding_rows_where_either_is_uncertain"
    ]
    failure_type = agreement["failure_type_multilabel"]
    severity = agreement["severity"]
    persistence = agreement["persistence"]
    return f"""# W5-B Inter-Annotator Agreement

The statistics below compare the two independent annotations for the 60-row
double-review subset. Adjudicated labels are not used in these agreement
calculations.

## Failure binary

| Scope | N | Raw agreement | Cohen's kappa |
|---|---:|---:|---:|
| Including `uncertain` | {binary_all['valid_comparisons']} | {binary_all['raw_agreement']:.4f} | {binary_all['cohens_kappa']:.4f} |
| Excluding rows where either reviewer used `uncertain` | {binary_definite['valid_comparisons']} | {binary_definite['raw_agreement']:.4f} | {binary_definite['cohens_kappa']:.4f} |

## Failure type (multi-label)

- Exact-match agreement: {failure_type['exact_match_agreement']:.4f}
- Mean Jaccard similarity: {failure_type['mean_jaccard_similarity']:.4f}
- Micro F1 (Reviewer 1 as reference): {failure_type['micro_f1']:.4f}
- Macro F1 (Reviewer 1 as reference): {failure_type['macro_f1']:.4f}

Failure type is multi-label; ordinary single-class accuracy is not used.

## Severity and persistence

| Label | Valid N | Raw agreement | Linear weighted kappa | Quadratic weighted kappa |
|---|---:|---:|---:|---:|
| Severity | {severity['valid_comparisons']} | {severity['raw_agreement']:.4f} | {severity['linear_weighted_cohens_kappa']:.4f} | {severity['quadratic_weighted_cohens_kappa']:.4f} |
| Persistence | {persistence['valid_comparisons']} | {persistence['raw_agreement']:.4f} | {persistence['linear_weighted_cohens_kappa']:.4f} | {persistence['quadratic_weighted_cohens_kappa']:.4f} |

Confidence is reported descriptively and is not a final research label.
"""


def summary_markdown(
    label_summary: dict[str, Any],
    agreement: dict[str, Any],
    split_manifest: dict[str, Any],
    rating: dict[str, Any],
    keyword: dict[str, Any],
    dummy: dict[str, Any],
    tfidf: dict[str, Any],
    error_summary: dict[str, Any],
    star_headers_removed: int,
    w6_readiness: str,
) -> str:
    test_rating = rating["test"]
    test_keyword = keyword["test"]
    test_dummy = dummy["evaluations"]["test"]
    validation_model = tfidf["evaluations"]["validation"]
    test_model = tfidf["evaluations"]["test"]
    split_lines = "\n".join(
        f"| {row['split']} | {row['rows']} | {row['failure_0']} | "
        f"{row['failure_1']} | {json_ready(row['earliest_utc'])} | "
        f"{json_ready(row['latest_utc'])} |"
        for row in split_manifest["split_rows"]
    )
    support_lines = "\n".join(
        f"| {row['device_type']} | {row['test_rows']} | {row['failure_0']} | "
        f"{row['failure_1']} | {row['support_status']} |"
        for row in tfidf["test_by_device_type_exploratory"]
    )
    return f"""# W5-B Summary

## Outcome

- Technical status: **PASS**
- W6 readiness: **{w6_readiness}**
- Frozen labels: {label_summary['rows']} total, {label_summary['definite_rows']} definite, {label_summary['uncertain_rows']} uncertain.
- Definite labels: {label_summary['failure_1']} engineering failures and {label_summary['failure_0']} non-failures.
- The 300-review annotation set is stratified for boundary coverage and is not a population-representative prevalence sample.

## Independent annotation agreement

- Failure binary agreement including `uncertain`: {agreement['failure_binary']['including_uncertain']['raw_agreement']:.4f}; Cohen's kappa {agreement['failure_binary']['including_uncertain']['cohens_kappa']:.4f}.
- Failure type exact match: {agreement['failure_type_multilabel']['exact_match_agreement']:.4f}; mean Jaccard {agreement['failure_type_multilabel']['mean_jaccard_similarity']:.4f}.
- Severity linear/quadratic weighted kappa: {agreement['severity']['linear_weighted_cohens_kappa']:.4f} / {agreement['severity']['quadratic_weighted_cohens_kappa']:.4f}.
- Persistence linear/quadratic weighted kappa: {agreement['persistence']['linear_weighted_cohens_kappa']:.4f} / {agreement['persistence']['quadratic_weighted_cohens_kappa']:.4f}.

## Chronological split

| Split | N | Non-failure | Failure | Earliest UTC | Latest UTC |
|---|---:|---:|---:|---|---|
{split_lines}

No review was randomly moved across time boundaries, and TF-IDF was fit only
on the training split.

## Test-set baseline comparison

| Baseline | Balanced accuracy | Precision | Recall | F1 | Specificity | ROC-AUC | PR-AUC |
|---|---:|---:|---:|---:|---:|---:|---:|
| B0 low-star (`rating <= 2`) | {test_rating['balanced_accuracy']:.4f} | {test_rating['precision']:.4f} | {test_rating['recall']:.4f} | {test_rating['f1']:.4f} | {test_rating['specificity']:.4f} | n/a | n/a |
| B3 keyword/rule draft | {test_keyword['balanced_accuracy']:.4f} | {test_keyword['precision']:.4f} | {test_keyword['recall']:.4f} | {test_keyword['f1']:.4f} | {test_keyword['specificity']:.4f} | n/a | n/a |
| Dummy most frequent | {test_dummy['balanced_accuracy']:.4f} | {test_dummy['precision']:.4f} | {test_dummy['recall']:.4f} | {test_dummy['f1']:.4f} | {test_dummy['specificity']:.4f} | {test_dummy['roc_auc']:.4f} | {test_dummy['pr_auc']:.4f} |
| TF-IDF + Logistic Regression | {test_model['balanced_accuracy']:.4f} | {test_model['precision']:.4f} | {test_model['recall']:.4f} | {test_model['f1']:.4f} | {test_model['specificity']:.4f} | {test_model['roc_auc']:.4f} | {test_model['pr_auc']:.4f} |

TF-IDF validation F1 is {validation_model['f1']:.4f}; test F1 is
{test_model['f1']:.4f}. The model removed a leading standalone star-title
header from {star_headers_removed} private model-text rows without altering the
formal review text.

Overfitting diagnostic:
`{tfidf['overfitting_diagnostic']['status']}`. Training F1 is
{tfidf['overfitting_diagnostic']['train_f1']:.4f}, creating a
train-minus-validation gap of
{tfidf['overfitting_diagnostic']['train_minus_validation_f1']:.4f}. The higher
test result does not remove this risk because both chronological evaluation
sets are small.

## Test support by device type

| Device type | Test N | Non-failure | Failure | Reporting status |
|---|---:|---:|---:|---|
{support_lines}

Device-specific metrics are exploratory. A class is marked
`INSUFFICIENT_SUPPORT` when the test subset has fewer than 20 rows or either
binary class has fewer than five rows.

## Error analysis

- TF-IDF false positives on test: {error_summary['false_positive_count']}
- TF-IDF false negatives on test: {error_summary['false_negative_count']}
- Complete review text for errors is stored only in the private interim Parquet.
- Test errors were not used to change rules, labels, thresholds, or training.

## Scope limitations

No model was applied to the full 55,877 reviews. No product-month engineering
signal, future deterioration target, product-level temporal persistence, W6
output, raw-source scan, or Git commit was created. Smart plugs remain the
primary analysis; smart bulbs are exploratory and smart switches are a
small-sample case study.
"""


def protected_files(root: Path) -> list[Path]:
    return [
        root / "data/amazon_reviews_2023/processed/target_products.parquet",
        root
        / "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base.parquet",
        root
        / "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet",
    ]


def record_protected_identities(root: Path) -> dict[str, dict[str, Any]]:
    return {
        relative(root, path): file_identity(root, path)
        for path in protected_files(root)
        if path.is_file()
    }


def verify_protected_unchanged(
    root: Path, before: dict[str, dict[str, Any]]
) -> tuple[bool, dict[str, dict[str, Any]]]:
    after = record_protected_identities(root)
    return before == after, after


def environment_payload() -> dict[str, Any]:
    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "pandas_version": pd.__version__,
        "pyarrow_version": pa.__version__,
        "scikit_learn_version": importlib.metadata.version("scikit-learn"),
        "joblib_version": joblib.__version__,
        "numpy_version": np.__version__,
    }


def report_contains_forbidden_field(path: Path) -> bool:
    if path.suffix.lower() not in {".json", ".md", ".csv", ".log"}:
        return False
    text = path.read_text(encoding="utf-8-sig", errors="replace").lower()
    return any(field in text for field in REPORT_TEXT_FORBIDDEN_FIELDS)


def run() -> int:
    started = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()
    root = project_root()
    config_path = root / "config" / "w5b_baseline_rules.toml"
    config = load_toml(config_path)
    if config["phase"]["name"] != PHASE:
        raise W5BError("Unexpected W5-B config phase")
    if config["phase"]["label_version"] != LABEL_VERSION:
        raise W5BError("Unexpected final annotation version")

    report_dir = root / config["outputs"]["report_dir"]
    interim_dir = root / "data/amazon_reviews_2023/interim/w5b"
    model_dir = root / "outputs/models"
    report_dir.mkdir(parents=True, exist_ok=True)
    interim_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w5b_execution.log"
    if log_path.exists():
        log_path.unlink()
    log_message(log_path, "W5-B start")
    initial_free = disk_free_gib(root)
    if initial_free < 60:
        raise W5BError(f"PAUSED_SPACE_GATE: only {initial_free:.3f} GiB free")

    w5a_status = load_json(root / config["inputs"]["w5a_status"])
    if w5a_status.get("status") != "PAUSED_HUMAN_ANNOTATION":
        raise W5BError("W5-A status is not PAUSED_HUMAN_ANNOTATION")
    if w5a_status.get("sample_rows") != 300 or w5a_status.get("double_review_rows") != 60:
        raise W5BError("W5-A status sample identity mismatch")

    input_paths = {
        key: root / config["inputs"][key]
        for key in (
            "adjudicated_workbook",
            "reviewer_2_workbook",
            "blind_review_key",
            "annotation_sampling_frame",
            "formal_review_parquet",
            "formal_product_parquet",
            "w5a_status",
            "annotation_rules",
            "keyword_rules",
        )
    }
    for label, path in input_paths.items():
        if not path.is_file():
            raise W5BError(f"Missing W5-B input {label}: {path}")
    assert_hash(
        input_paths["adjudicated_workbook"],
        config["inputs"]["adjudicated_workbook_sha256"],
        "adjudicated workbook",
    )
    assert_hash(
        input_paths["reviewer_2_workbook"],
        config["inputs"]["reviewer_2_workbook_sha256"],
        "reviewer 2 workbook",
    )
    assert_parquet(
        input_paths["blind_review_key"],
        config["inputs"]["blind_review_key_rows"],
        config["inputs"]["blind_review_key_sha256"],
        "blind_review_key",
    )
    assert_parquet(
        input_paths["annotation_sampling_frame"],
        config["inputs"]["annotation_sampling_frame_rows"],
        config["inputs"]["annotation_sampling_frame_sha256"],
        "annotation_sampling_frame",
    )
    assert_parquet(
        input_paths["formal_review_parquet"],
        config["inputs"]["formal_review_rows"],
        config["inputs"]["formal_review_sha256"],
        "formal review parquet",
    )
    assert_parquet(
        input_paths["formal_product_parquet"],
        config["inputs"]["formal_product_rows"],
        config["inputs"]["formal_product_sha256"],
        "formal product parquet",
    )
    protected_before = record_protected_identities(root)
    input_identities: dict[str, Any] = {}
    for label, path in input_paths.items():
        identity = (
            parquet_identity(root, path)
            if path.suffix.lower() == ".parquet"
            else file_identity(root, path)
        )
        if label == "formal_review_parquet":
            # The manifest needs file identity and row count, not the formal
            # corpus's privacy-management schema fields.
            identity.pop("fields", None)
            identity.pop("field_count", None)
        input_identities[label] = identity

    input_manifest = {
        "phase": PHASE,
        "started_at_utc": started.isoformat(),
        "project_root_resolved": str(root),
        "environment": environment_payload(),
        "initial_free_gib": initial_free,
        "inputs": input_identities,
        "protected_formal_files_before": protected_before,
        "raw_jsonl_read": False,
        "compressed_files_read": False,
        "full_55877_review_corpus_opened_for_modeling": False,
    }
    write_json(report_dir / "w5b_input_manifest.json", input_manifest)
    log_message(log_path, "Input identities validated")

    main_workbook = read_xlsx_table(input_paths["adjudicated_workbook"])
    reviewer_2_workbook = read_xlsx_table(input_paths["reviewer_2_workbook"])
    blind_key = pq.read_table(input_paths["blind_review_key"]).to_pandas()
    sampling_frame = pq.read_table(
        input_paths["annotation_sampling_frame"]
    ).to_pandas()
    labels, modeling, workbook_validation = build_final_labels(
        main_workbook, reviewer_2_workbook, blind_key, sampling_frame
    )
    agreement = inter_annotator_agreement(main_workbook, reviewer_2_workbook)
    label_summary, label_count_rows = final_label_reports(labels)
    log_message(
        log_path,
        f"Final labels frozen: definite={label_summary['definite_rows']} "
        f"uncertain={label_summary['uncertain_rows']}",
    )

    definite, split_manifest = chronological_split(modeling)
    split_lookup = definite.set_index("blind_review_id")["split"]
    modeling["split"] = modeling["blind_review_id"].map(split_lookup)
    log_message(log_path, "Strict chronological train/validation/test split created")

    (
        rating_evaluation,
        keyword_evaluation,
        dummy_evaluation,
        definite_predictions_source,
        predictions,
        top_features,
        model_results,
    ) = model_and_evaluate(definite, config)
    tfidf_evaluation = model_results["evaluation"]
    error_summary = model_results["error_summary"]
    errors_private = model_results["errors_private"]
    log_message(log_path, "B0, B3, Dummy, and TF-IDF Logistic baselines evaluated")

    processed_labels_path = root / config["outputs"]["processed_labels"]
    modeling_dataset_path = root / config["outputs"]["modeling_dataset"]
    predictions_path = root / config["outputs"]["baseline_predictions"]
    error_private_path = root / config["outputs"]["error_analysis_private"]
    model_path = root / config["outputs"]["model"]

    label_output = labels.copy()
    label_output["final_failure_binary"] = label_output[
        "final_failure_binary"
    ].astype("string")
    label_output["final_failure_type"] = label_output[
        "final_failure_type"
    ].astype("string")
    label_output["final_severity"] = label_output["final_severity"].astype("Int8")
    label_output["final_persistence"] = label_output[
        "final_persistence"
    ].astype("Int8")
    write_parquet(processed_labels_path, label_output)
    modeling["final_severity"] = modeling["final_severity"].astype("Int8")
    modeling["final_persistence"] = modeling["final_persistence"].astype("Int8")
    write_parquet(modeling_dataset_path, modeling)
    write_parquet(predictions_path, predictions)
    write_parquet(error_private_path, errors_private)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
    joblib.dump(model_results["model_bundle"], temporary_model)
    temporary_model.replace(model_path)

    write_json(report_dir / "final_label_summary.json", label_summary)
    write_csv(
        report_dir / "final_label_counts.csv",
        label_count_rows,
        ["scope", "label_status", "rows", "failure_0", "failure_1", "uncertain"],
    )
    write_json(report_dir / "inter_annotator_agreement.json", agreement)
    (report_dir / "inter_annotator_agreement.md").write_text(
        inter_annotator_markdown(agreement), encoding="utf-8"
    )
    write_json(report_dir / "time_split_manifest.json", split_manifest)
    write_csv(
        report_dir / "time_split_counts.csv",
        split_manifest["split_rows"],
        [
            "split",
            "rows",
            "failure_0",
            "failure_1",
            "smart_plug",
            "smart_bulb",
            "smart_switch",
            "earliest_utc",
            "latest_utc",
            "unique_parent_asin",
        ],
    )
    write_json(
        report_dir / "rating_baseline_evaluation.json",
        {
            "baseline": "B0 Rating",
            "prediction": "rating <= 2",
            "ground_truth": "final_failure_binary",
            "rating_is_ground_truth": False,
            **rating_evaluation,
        },
    )
    write_json(
        report_dir / "keyword_baseline_evaluation.json",
        {
            "baseline": "B3 Keyword/rule draft",
            "keyword_version": "w5a-keyword-v1.0-draft",
            "rules_refit_from_labels": False,
            **keyword_evaluation,
        },
    )
    write_json(report_dir / "dummy_baseline_evaluation.json", dummy_evaluation)
    write_json(report_dir / "tfidf_logistic_evaluation.json", tfidf_evaluation)

    confusion = confusion_rows(
        {
            "rating": ("test", rating_evaluation["test"]),
            "keyword": ("test", keyword_evaluation["test"]),
            "dummy_most_frequent": (
                "validation",
                dummy_evaluation["evaluations"]["validation"],
            ),
            "dummy_most_frequent_test": (
                "test",
                dummy_evaluation["evaluations"]["test"],
            ),
            "tfidf_logistic_validation": (
                "validation",
                tfidf_evaluation["evaluations"]["validation"],
            ),
            "tfidf_logistic_test": (
                "test",
                tfidf_evaluation["evaluations"]["test"],
            ),
        }
    )
    write_csv(
        report_dir / "confusion_matrices.csv",
        confusion,
        ["model", "scope", "true_label", "predicted_label", "count"],
    )
    write_csv(
        report_dir / "model_top_features.csv",
        top_features.to_dict(orient="records"),
        ["direction", "rank", "ngram", "coefficient"],
    )
    write_json(report_dir / "model_error_summary.json", error_summary)

    protected_unchanged, protected_after = verify_protected_unchanged(
        root, protected_before
    )
    if not protected_unchanged:
        raise W5BError("A protected W3/W4/W4R formal file changed during W5-B")
    final_free = disk_free_gib(root)
    if final_free < 60:
        raise W5BError(f"PAUSED_SPACE_GATE: final free space {final_free:.3f} GiB")

    device_support_insufficient = any(
        row["support_status"] == "INSUFFICIENT_SUPPORT"
        for row in tfidf_evaluation["test_by_device_type_exploratory"]
    )
    w6_readiness = "REVIEW_REQUIRED" if device_support_insufficient else (
        config["phase"]["w6_readiness_default"]
    )
    summary_text = summary_markdown(
        label_summary,
        agreement,
        split_manifest,
        rating_evaluation,
        keyword_evaluation,
        dummy_evaluation,
        tfidf_evaluation,
        error_summary,
        workbook_validation["leading_star_title_removed_rows"],
        w6_readiness,
    )
    (report_dir / "w5b_summary.md").write_text(summary_text, encoding="utf-8")
    disk_usage = {
        "initial_free_gib": initial_free,
        "final_free_gib": final_free,
        "used_by_phase_gib": initial_free - final_free,
        "minimum_required_free_gib": 60,
        "space_gate_passed": final_free >= 60,
        "process_peak_working_set_bytes": process_peak_working_set_bytes(),
    }
    write_json(report_dir / "w5b_disk_usage.json", disk_usage)

    output_identities = {
        "processed_labels": parquet_identity(root, processed_labels_path),
        "modeling_dataset": parquet_identity(root, modeling_dataset_path),
        "baseline_predictions": parquet_identity(root, predictions_path),
        "error_analysis_private": parquet_identity(root, error_private_path),
        "model": file_identity(root, model_path),
    }
    required_reports = [
        "w5b_execution.log",
        "w5b_input_manifest.json",
        "final_label_summary.json",
        "final_label_counts.csv",
        "inter_annotator_agreement.json",
        "inter_annotator_agreement.md",
        "time_split_manifest.json",
        "time_split_counts.csv",
        "rating_baseline_evaluation.json",
        "keyword_baseline_evaluation.json",
        "dummy_baseline_evaluation.json",
        "tfidf_logistic_evaluation.json",
        "confusion_matrices.csv",
        "model_top_features.csv",
        "model_error_summary.json",
        "w5b_summary.md",
        "w5b_disk_usage.json",
    ]
    missing_reports = [
        name for name in required_reports if not (report_dir / name).is_file()
    ]
    if missing_reports:
        raise W5BError(f"Missing required W5-B reports: {missing_reports}")

    report_leakage_scan = {
        name: report_contains_forbidden_field(report_dir / name)
        for name in required_reports
    }
    # Input manifests necessarily name the private key file but never contain its
    # row-level values. Only content leakage is prohibited, not input filenames.
    permitted_filename_mentions = {
        "w5b_input_manifest.json",
        "w5b_execution.log",
    }
    leaked_reports = [
        name
        for name, leaked in report_leakage_scan.items()
        if leaked and name not in permitted_filename_mentions
    ]
    if leaked_reports:
        raise W5BError(
            "Ordinary report leakage scan found forbidden row-level field names: "
            f"{leaked_reports}"
        )

    completed = datetime.now(timezone.utc)
    status = {
        "phase": PHASE,
        "status": "PASS",
        "w6_readiness": w6_readiness,
        "completed_at_utc": completed.isoformat(),
        "elapsed_seconds": time.monotonic() - start_monotonic,
        "environment": environment_payload(),
        "workbook_validation": workbook_validation,
        "label_summary": label_summary,
        "inter_annotator_agreement_completed": True,
        "chronological_split": split_manifest,
        "baselines_completed": [
            "B0 Rating",
            "B3 Keyword/rule draft",
            "DummyClassifier most_frequent",
            "TF-IDF + Logistic Regression",
        ],
        "tfidf_fit_on_train_only": True,
        "tfidf_overfitting_diagnostic": tfidf_evaluation[
            "overfitting_diagnostic"
        ],
        "model_feature_field": "model_text",
        "model_forbidden_features_used": [],
        "formal_files_unchanged": protected_unchanged,
        "protected_formal_files_after": protected_after,
        "raw_jsonl_read": False,
        "compressed_sources_read": False,
        "full_55877_reviews_scored": False,
        "product_month_failure_signals_created": False,
        "future_quality_target_created": False,
        "product_level_temporal_persistence_created": False,
        "w6_executed": False,
        "git_commit_created": False,
        "final_free_gib": final_free,
        "outputs": output_identities,
        "required_reports": [
            relative(root, report_dir / name) for name in required_reports
        ],
        "report_row_level_text_or_identifier_leakage": False,
        "limitations": [
            "The 300-review annotation sample is stratified and not population-representative.",
            "Device-specific test metrics are exploratory and may have insufficient support.",
            "Smart plugs are primary, smart bulbs exploratory, and smart switches a case study.",
            "W6 readiness requires research-design review before any full-corpus scoring.",
        ],
    }
    write_json(report_dir / "w5b_status.json", status)
    log_message(log_path, f"W5-B PASS; w6_readiness={w6_readiness}")
    return 0


def write_failure_status(error: Exception, status_name: str) -> None:
    try:
        root = project_root()
        report_dir = root / "data/amazon_reviews_2023/reports/w5b"
        report_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            report_dir / "w5b_status.json",
            {
                "phase": PHASE,
                "status": status_name,
                "w6_readiness": "REVIEW_REQUIRED",
                "error_type": type(error).__name__,
                "error_message": str(error),
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "w6_executed": False,
            },
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        return run()
    except SplitReviewRequired as error:
        write_failure_status(error, "PAUSED_SPLIT_REVIEW")
        print(f"[PAUSED_SPLIT_REVIEW] {error}", file=sys.stderr)
        return 4
    except W5BError as error:
        status = (
            "PAUSED_SPACE_GATE"
            if str(error).startswith("PAUSED_SPACE_GATE")
            else "FAILED_BASELINE_PREPARATION"
        )
        write_failure_status(error, status)
        print(f"[{status}] {error}", file=sys.stderr)
        return 2
    except Exception as error:
        write_failure_status(error, "FAILED_BASELINE_PREPARATION")
        print(
            f"[FAILED_BASELINE_PREPARATION] {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
