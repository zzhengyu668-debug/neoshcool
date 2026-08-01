from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq


def atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    reports = (
        project_root
        / "data"
        / "amazon_reviews_2023"
        / "reports"
        / "w3"
    )
    candidates_path = (
        project_root
        / "data"
        / "amazon_reviews_2023"
        / "interim"
        / "metadata_candidates.parquet"
    )
    flow_path = reports / "product_selection_flow.json"
    status_path = reports / "w3_status.json"
    disk_path = reports / "w3_disk_usage.json"
    summary_path = reports / "product_selection_summary.md"
    log_path = reports / "w3_execution.log"

    candidate_types = pq.read_table(
        candidates_path,
        columns=["candidate_device_types"],
    )["candidate_device_types"]
    multi_type_conflicts = int(
        pc.sum(pc.greater(pc.list_value_length(candidate_types), 1)).as_py()
    )

    flow = read_json(flow_path)
    flow["candidate_statistics"][
        "multi_type_conflict_parents"
    ] = multi_type_conflicts
    atomic_json(flow_path, flow)

    flow_csv_path = reports / "product_selection_flow.csv"
    with flow_csv_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        flow_rows = list(csv.DictReader(handle))
    flow_rows = [
        row
        for row in flow_rows
        if row.get("stage") != "multi_type_conflict_parents"
    ]
    flow_rows.append(
        {
            "stage": "multi_type_conflict_parents",
            "domain": "All",
            "reason": "candidate_device_types length > 1",
            "count": str(multi_type_conflicts),
        }
    )
    temporary_csv = flow_csv_path.with_name(
        f"{flow_csv_path.name}.tmp-{os.getpid()}"
    )
    with temporary_csv.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flow_rows[0]))
        writer.writeheader()
        writer.writerows(flow_rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_csv, flow_csv_path)

    summary = summary_path.read_text(encoding="utf-8")
    summary = re.sub(
        r"- Multi-type candidate conflicts: [^\r\n]*\r?\n",
        "",
        summary,
    )
    marker = "- Unresolved ambiguous parents:"
    lines = summary.splitlines()
    insertion = next(
        (index + 1 for index, line in enumerate(lines) if line.startswith(marker)),
        4,
    )
    lines.insert(
        insertion,
        f"- Multi-type candidate conflicts: {multi_type_conflicts:,}",
    )
    atomic_bytes(summary_path, ("\n".join(lines) + "\n").encode("utf-8"))

    status = read_json(status_path)
    status["multi_type_conflict_parents"] = multi_type_conflicts
    atomic_json(status_path, status)

    checkpoints = [
        read_json(reports / "checkpoints" / "meta_electronics.json"),
        read_json(reports / "checkpoints" / "meta_home_and_kitchen.json"),
    ]
    filter_version = status["filter_version"]
    start_pattern = re.compile(
        r"^\[(?P<time>[^\]]+)\] \[INFO\] W3 started; .*"
        r"free_bytes=(?P<bytes>\d+); "
        rf"filter_version={re.escape(filter_version)}$"
    )
    matching_starts = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = start_pattern.match(line)
        if match:
            matching_starts.append(
                {
                    "time": match.group("time"),
                    "free_bytes": int(match.group("bytes")),
                }
            )
    if not matching_starts:
        raise RuntimeError(
            f"No execution-log start found for filter version {filter_version}"
        )
    final_scan_start = min(
        matching_starts,
        key=lambda item: datetime.fromisoformat(item["time"]),
    )
    final_free_bytes = int(status["final_free_bytes"])
    disk = {
        "phase": "W3",
        "filter_version": filter_version,
        "generated_at": datetime.now().astimezone().isoformat(),
        "minimum_free_bytes": 60 * 1024**3,
        "events": [
            {
                "time": final_scan_start["time"],
                "event": "final_frozen_rule_scan_start",
                "free_bytes": final_scan_start["free_bytes"],
                "free_gib": final_scan_start["free_bytes"] / 1024**3,
            },
            *[
                {
                    "time": checkpoint["scan_finished_at"],
                    "event": "metadata_scan_complete",
                    "source_id": checkpoint["id"],
                    "free_bytes": checkpoint["end_free_bytes"],
                    "free_gib": checkpoint["end_free_bytes"] / 1024**3,
                }
                for checkpoint in checkpoints
            ],
            {
                "time": status["updated_at"],
                "event": "w3_pass_reporting_complete",
                "free_bytes": final_free_bytes,
                "free_gib": final_free_bytes / 1024**3,
            },
        ],
    }
    atomic_json(disk_path, disk)
    print(
        "Finalized W3 reports: "
        f"multi_type_conflicts={multi_type_conflicts}; "
        f"final_free_bytes={final_free_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
