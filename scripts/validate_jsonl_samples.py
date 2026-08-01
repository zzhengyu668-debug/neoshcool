from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import orjson
except ImportError:  # pragma: no cover - the environment checker reports this
    orjson = None


MAX_LINE_BYTES = 16 * 1024 * 1024
SAMPLES_PER_POSITION = 3
END_WINDOW_BYTES = 1024 * 1024
RESERVE_BYTES = 60 * 1024**3
FILE_ATTRIBUTE_READONLY = 0x1
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path escapes project root: {relative}")
    return candidate


def load_context() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    project_toml = project_root / "config" / "project.toml"
    dataset_config = project_root / "config" / "amazon_w1_files.json"
    with project_toml.open("rb") as handle:
        project_config = tomllib.load(handle)
    paths = project_config["paths"]
    raw_compressed = resolve_inside(project_root, paths["raw_compressed"])
    raw_uncompressed = resolve_inside(project_root, paths["raw_uncompressed"])
    reports = resolve_inside(project_root, paths["reports"]) / "w1"
    reports.mkdir(parents=True, exist_ok=True)
    datasets = json.loads(dataset_config.read_text(encoding="utf-8"))
    if len(datasets) != 4:
        raise RuntimeError(f"Expected four W1 datasets, found {len(datasets)}")
    return {
        "project_root": project_root,
        "project_toml": project_toml,
        "dataset_config": dataset_config,
        "raw_compressed": raw_compressed,
        "raw_uncompressed": raw_uncompressed,
        "reports": reports,
        "datasets": datasets,
    }


def log(context: dict[str, Any], level: str, message: str) -> None:
    safe = " ".join(message.splitlines())
    with (context["reports"] / "w1_execution.log").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(f"[{now_iso()}] [{level}] {safe}\n")


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


def parse_object(line: bytes) -> dict[str, Any]:
    if not line.strip():
        raise ValueError("Encountered an empty sample line")
    if len(line) > MAX_LINE_BYTES:
        raise ValueError(
            f"Sample line exceeds safety cap of {MAX_LINE_BYTES} bytes"
        )
    value = orjson.loads(line) if orjson is not None else json.loads(line)
    if not isinstance(value, dict):
        raise TypeError("JSONL sample is not a JSON object")
    return value


def read_samples(
    handle: Any,
    *,
    start_offset: int,
    align_to_next_line: bool,
    count: int = SAMPLES_PER_POSITION,
) -> list[tuple[int, dict[str, Any]]]:
    handle.seek(start_offset)
    if align_to_next_line and start_offset > 0:
        discarded = handle.readline(MAX_LINE_BYTES + 1)
        if len(discarded) > MAX_LINE_BYTES and not discarded.endswith(b"\n"):
            raise ValueError("Could not find a newline within the safety cap")

    records: list[tuple[int, dict[str, Any]]] = []
    while len(records) < count:
        offset = handle.tell()
        line = handle.readline(MAX_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
            raise ValueError("Sample line exceeds the safety cap")
        if not line.strip():
            continue
        records.append((offset, parse_object(line)))
    if not records:
        raise ValueError("No complete non-empty JSON line found at sample position")
    return records


def sample_file(path: Path, record_type: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("JSONL file is empty")
    positions = {
        "beginning": (0, False),
        "middle": (size // 2, True),
        "end": (max(0, size - END_WINDOW_BYTES), size > END_WINDOW_BYTES),
    }
    required = (
        {"parent_asin", "title"}
        if record_type == "metadata"
        else {"parent_asin", "rating", "text", "timestamp"}
    )
    observed_required: set[str] = set()
    position_results: dict[str, Any] = {}

    with path.open("rb") as handle:
        for position, (offset, align) in positions.items():
            samples = read_samples(
                handle,
                start_offset=offset,
                align_to_next_line=align,
            )
            safe_samples = []
            for sample_offset, value in samples:
                keys = sorted(value)
                observed_required.update(required.intersection(value))
                safe_samples.append(
                    {
                        "byte_offset": sample_offset,
                        "object": True,
                        "field_count": len(value),
                        "field_types": {
                            key: json_type(value[key]) for key in keys
                        },
                        "required_fields_present": sorted(
                            required.intersection(value)
                        ),
                        "required_fields_missing_in_this_sample": sorted(
                            required.difference(value)
                        ),
                    }
                )
            position_results[position] = {
                "requested_offset": offset,
                "records_parsed": len(safe_samples),
                "samples": safe_samples,
            }

    not_observed = sorted(required.difference(observed_required))
    return {
        "size_bytes": size,
        "positions": position_results,
        "records_parsed": sum(
            item["records_parsed"] for item in position_results.values()
        ),
        "all_sampled_lines_are_objects": True,
        "required_fields": sorted(required),
        "required_fields_observed": sorted(observed_required),
        "required_fields_not_observed_in_small_sample": not_observed,
        "required_field_note": (
            "A field missing from one sampled record is not treated as a file "
            "failure. Fields not observed in this small W1 sample require W2 "
            "schema inventory rather than a fabricated W1 conclusion."
        ),
        "parse_success": True,
    }


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


def set_readonly_preserving_attributes(path: Path) -> None:
    if os.name == "nt":
        attributes = get_windows_attributes(path)
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        set_attributes.restype = ctypes.c_int
        if not set_attributes(str(path), attributes | FILE_ATTRIBUTE_READONLY):
            raise ctypes.WinError()
    else:  # pragma: no cover
        path.chmod(path.stat().st_mode & ~0o222)


def is_readonly(path: Path) -> bool:
    if os.name == "nt":
        return bool(get_windows_attributes(path) & FILE_ATTRIBUTE_READONLY)
    return not bool(path.stat().st_mode & 0o222)  # pragma: no cover


def append_disk_event(
    context: dict[str, Any], event: str, **details: Any
) -> None:
    path = context["reports"] / "w1_disk_usage.json"
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        document = {"phase": "W1", "events": []}
    free = shutil.disk_usage(context["project_root"]).free
    document["updated_at"] = now_iso()
    document.setdefault("events", []).append(
        {
            "time": now_iso(),
            "event": event,
            "free_bytes": free,
            "free_gib": round(free / 1024**3, 3),
            **details,
        }
    )
    atomic_json(path, document)


def update_manifests_readonly(context: dict[str, Any]) -> None:
    reports = context["reports"]
    download_json = reports / "w1_download_manifest.json"
    extract_json = reports / "w1_extract_manifest.json"
    verified_at = now_iso()

    download = json.loads(download_json.read_text(encoding="utf-8-sig"))
    for row, dataset in zip(download["files"], context["datasets"], strict=True):
        path = context["raw_compressed"] / dataset["compressed_relative"]
        row["readonly"] = is_readonly(path)
        row["verified_at"] = verified_at
    download["updated_at"] = verified_at
    atomic_json(download_json, download)
    atomic_csv(reports / "w1_download_manifest.csv", download["files"])

    extract = json.loads(extract_json.read_text(encoding="utf-8-sig"))
    for row, dataset in zip(extract["files"], context["datasets"], strict=True):
        path = context["raw_uncompressed"] / dataset["uncompressed_relative"]
        row["readonly"] = is_readonly(path)
        row["validated_at"] = verified_at
        row["sha256"] = None
        row["sha256_status"] = "not_computed_optional"
        if row["status"].startswith("COMPLETE"):
            row["status"] = "COMPLETE_VALIDATED_READONLY"
    extract["updated_at"] = verified_at
    atomic_json(extract_json, extract)
    atomic_csv(reports / "w1_extract_manifest.csv", extract["files"])


def build_status(
    context: dict[str, Any], validation: dict[str, Any]
) -> dict[str, Any]:
    reports = context["reports"]
    download = json.loads(
        (reports / "w1_download_manifest.json").read_text(encoding="utf-8-sig")
    )
    archives = json.loads(
        (reports / "w1_archive_test.json").read_text(encoding="utf-8-sig")
    )
    extract = json.loads(
        (reports / "w1_extract_manifest.json").read_text(encoding="utf-8-sig")
    )
    free = shutil.disk_usage(context["project_root"]).free
    required_reports = [
        "w1_execution.log",
        "w1_download_manifest.csv",
        "w1_download_manifest.json",
        "w1_archive_test.json",
        "w1_extract_manifest.csv",
        "w1_extract_manifest.json",
        "w1_sample_validation.json",
        "w1_disk_usage.json",
    ]
    report_presence = {
        name: (reports / name).is_file() for name in required_reports
    }

    criteria = {
        "four_compressed_files_exist": len(download["files"]) == 4
        and all(
            (context["raw_compressed"] / item["compressed_relative"]).is_file()
            for item in context["datasets"]
        ),
        "compressed_byte_counts_match": len(download["files"]) == 4
        and all(
            row.get("actual_bytes") == row.get("expected_bytes")
            for row in download["files"]
        ),
        "four_project_sha256_values_recorded": len(download["files"]) == 4
        and all(
            isinstance(row.get("sha256"), str)
            and len(row["sha256"]) == 64
            for row in download["files"]
        ),
        "four_archive_tests_passed": len(archives["files"]) == 4
        and all(row.get("success") for row in archives["files"]),
        "four_jsonl_files_exist_and_nonempty": len(extract["files"]) == 4
        and all(
            (
                context["raw_uncompressed"]
                / item["uncompressed_relative"]
            ).is_file()
            and (
                context["raw_uncompressed"]
                / item["uncompressed_relative"]
            ).stat().st_size
            > 0
            for item in context["datasets"]
        ),
        "all_beginning_middle_end_samples_parse": len(validation["files"]) == 4
        and all(
            row.get("parse_success")
            and set(row.get("positions", {}))
            == {"beginning", "middle", "end"}
            for row in validation["files"]
        ),
        "all_raw_files_readonly": all(
            is_readonly(
                context["raw_compressed"] / item["compressed_relative"]
            )
            and is_readonly(
                context["raw_uncompressed"] / item["uncompressed_relative"]
            )
            for item in context["datasets"]
        ),
        "final_free_space_at_least_60_gib": free >= RESERVE_BYTES,
        "no_direct_compressed_scan_performed": True,
        "w2_not_executed": True,
        "required_w1_reports_present": all(report_presence.values()),
    }
    return {
        "phase": "W1",
        "status": "PASS" if all(criteria.values()) else "FAILED_VALIDATION",
        "reason": (
            "All W1 acceptance criteria passed."
            if all(criteria.values())
            else "One or more W1 acceptance criteria did not pass."
        ),
        "updated_at": now_iso(),
        "project_root": str(context["project_root"]),
        "criteria": criteria,
        "report_presence": report_presence,
        "final_free_bytes": free,
        "final_free_gib": round(free / 1024**3, 3),
        "policy_attestation": {
            "compressed_archives_were_not_parsed_as_json": True,
            "gzip_open_used": False,
            "full_line_count_performed": False,
            "full_schema_inference_performed": False,
            "parquet_created": False,
            "w2_started": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate tiny beginning/middle/end samples from W1 JSONL files."
    )
    parser.parse_args()
    context = load_context()
    reports = context["reports"]
    log(context, "INFO", "Small beginning/middle/end JSONL validation started.")
    results: list[dict[str, Any]] = []

    try:
        for dataset in context["datasets"]:
            path = (
                context["raw_uncompressed"]
                / dataset["uncompressed_relative"]
            )
            if not path.is_file():
                raise FileNotFoundError(f"Missing JSONL file: {path}")
            result = sample_file(path, dataset["record_type"])
            result.update(
                {
                    "id": dataset["id"],
                    "record_type": dataset["record_type"],
                    "domain": dataset["domain"],
                    "relative_path": dataset["uncompressed_relative"],
                    "validated_at": now_iso(),
                }
            )
            results.append(result)
            log(
                context,
                "INFO",
                f"Sample validation passed: {dataset['id']}; "
                f"records_parsed={result['records_parsed']}",
            )

        validation = {
            "phase": "W1",
            "method": (
                "Binary seeks into uncompressed JSONL only; a few complete "
                "lines at beginning, middle, and near end."
            ),
            "privacy": (
                "Reports contain only byte offsets, field names, and value "
                "types; no field values or review text are logged."
            ),
            "updated_at": now_iso(),
            "files": results,
        }
        atomic_json(reports / "w1_sample_validation.json", validation)

        for dataset in context["datasets"]:
            compressed = (
                context["raw_compressed"]
                / dataset["compressed_relative"]
            )
            uncompressed = (
                context["raw_uncompressed"]
                / dataset["uncompressed_relative"]
            )
            set_readonly_preserving_attributes(compressed)
            set_readonly_preserving_attributes(uncompressed)
        update_manifests_readonly(context)
        append_disk_event(context, "sample_validation_complete", files_validated=4)
        status = build_status(context, validation)
        atomic_json(reports / "w1_status.json", status)
        log(
            context,
            "INFO",
            f"W1 validation completed with status={status['status']}; "
            f"final_free_bytes={status['final_free_bytes']}",
        )
        return 0 if status["status"] == "PASS" else 1
    except Exception as exc:
        failure = {
            "phase": "W1",
            "status": "FAILED_VALIDATION",
            "reason": str(exc),
            "updated_at": now_iso(),
            "files_validated_before_failure": len(results),
        }
        atomic_json(reports / "w1_sample_validation.json", failure)
        atomic_json(reports / "w1_status.json", failure)
        log(context, "ERROR", str(exc))
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
