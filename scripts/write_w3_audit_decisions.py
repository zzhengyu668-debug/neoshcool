from __future__ import annotations

import csv
import os
from collections import Counter
from pathlib import Path


EXPECTED_ELIGIBLE_COUNTS = {
    "smart_plug": 95,
    "smart_bulb": 8,
    "smart_switch": 3,
}
EXPECTED_EXCLUDED_COUNT = 50

# These labels were assigned after manual review of the fixed-seed W3 audit
# sample's product title, category, feature, description, and rule evidence.
ACCESSORY_IDS = {
    "599f047f78a36177",  # outlet wall-mount holder for an Echo Dot
}
NON_SMART_IDS = {
    "d939a5d83766332e",  # conventional battery charger
    "f9f8d0c7ec35ed5f",  # conventional decorative LED icicle set
    "7849b9e7fa93be68",  # dishpan with a drain plug
    "320c6fe56ceb36b5",  # conventional phone charger/plug
    "cc2bbd6408d25946",  # conventional power strip
    "cf6500ecac406ae5",  # non-controllable electricity saver
}
WRONG_DEVICE_TYPE_IDS = {
    "26ae5bc21f0b16ff",  # HDMI selector switch, not a wall-light switch
}


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    reports = (
        project_root
        / "data"
        / "amazon_reviews_2023"
        / "reports"
        / "w3"
    )
    sample_path = reports / "product_audit_sample.csv"
    decision_path = reports / "product_audit_decisions.csv"

    with sample_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))

    eligible_counts = Counter(
        row["expected_device_type"]
        for row in sample_rows
        if row["audit_stratum"] == "eligible"
    )
    excluded_count = sum(
        row["audit_stratum"] == "strong_exclusion" for row in sample_rows
    )
    if dict(eligible_counts) != EXPECTED_ELIGIBLE_COUNTS:
        raise RuntimeError(
            f"Unexpected eligible audit counts: {dict(eligible_counts)}"
        )
    if excluded_count != EXPECTED_EXCLUDED_COUNT:
        raise RuntimeError(
            f"Unexpected excluded audit count: {excluded_count}"
        )

    all_ids = {row["audit_id"] for row in sample_rows}
    classified_special_ids = (
        ACCESSORY_IDS | NON_SMART_IDS | WRONG_DEVICE_TYPE_IDS
    )
    missing_special_ids = classified_special_ids - all_ids
    if missing_special_ids:
        raise RuntimeError(
            f"Expected manually reviewed audit IDs missing: "
            f"{sorted(missing_special_ids)}"
        )

    decisions = []
    for row in sample_rows:
        audit_id = row["audit_id"]
        if row["audit_stratum"] == "eligible":
            label = "correct_target"
            audit_device_type = row["expected_device_type"]
            note = (
                "Manual Metadata review: product identity and smart-control "
                "evidence support the assigned target device type."
            )
        elif audit_id in ACCESSORY_IDS:
            label = "accessory"
            audit_device_type = ""
            note = "Manual Metadata review: accessory rather than target product."
        elif audit_id in NON_SMART_IDS:
            label = "non_smart"
            audit_device_type = ""
            note = (
                "Manual Metadata review: no qualifying smart-control product "
                "identity."
            )
        elif audit_id in WRONG_DEVICE_TYPE_IDS:
            label = "wrong_device_type"
            audit_device_type = ""
            note = (
                "Manual Metadata review: switch terminology refers to a "
                "non-target switch class."
            )
        else:
            label = "false_positive"
            audit_device_type = ""
            note = (
                "Manual Metadata review: Metadata identifies a different "
                "primary product."
            )
        decisions.append(
            {
                "audit_id": audit_id,
                "audit_label": label,
                "audit_device_type": audit_device_type,
                "audit_notes": note,
            }
        )

    temporary = decision_path.with_name(
        f"{decision_path.name}.tmp-{os.getpid()}"
    )
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(decisions[0]))
        writer.writeheader()
        writer.writerows(decisions)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, decision_path)
    print(f"Wrote {len(decisions)} manual W3 audit decisions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
