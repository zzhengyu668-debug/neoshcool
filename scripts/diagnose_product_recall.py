#!/usr/bin/env python3
"""W3R-A metadata-only recall diagnosis for smart bulbs and smart switches.

This script is deliberately restricted to the W3 candidate and target-product
Parquet files. It never reads raw JSONL, review-level Parquet, ratings, prices,
or user information. The output is a non-frozen W3 v1.4 draft.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import time
import tomllib
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import orjson
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_CONFIG = PROJECT_ROOT / "config" / "project.toml"
DRAFT_CONFIG = (
    PROJECT_ROOT / "config" / "product_filter_rules_w3r_v1_4_draft.toml"
)
BASE_CONFIG = PROJECT_ROOT / "config" / "product_filter_rules.toml"

ALLOWED_IDENTITY_COLUMNS = [
    "parent_asin",
    "main_category",
    "title",
    "categories",
    "features",
    "description",
    "store",
    "details",
    "source_domains",
]
CANDIDATE_AUDIT_COLUMNS = [
    "candidate_device_types",
    "eligible_device_types",
    "candidate_device_terms",
    "candidate_smart_terms",
    "matched_fields",
    "exclusion_reasons",
    "ambiguity_status",
]
TARGET_COLUMNS = ALLOWED_IDENTITY_COLUMNS + ["device_type", "filter_version"]

DEVICE_TYPES = ("smart_bulb", "smart_switch")
AUDIT_LABELS = {
    "correct_target",
    "false_positive",
    "ambiguous",
    "wrong_device_type",
    "accessory",
    "non_smart",
    "insufficient_evidence",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.write_bytes(
        orjson.dumps(
            value,
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        + b"\n"
    )


def csv_dump(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        "|".join(str(item) for item in value)
                        if isinstance(value, list)
                        else value
                    )
                    for key, value in row.items()
                }
            )


def flatten_jsonish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"[", "{"}:
            try:
                return flatten_jsonish(orjson.loads(stripped))
            except orjson.JSONDecodeError:
                pass
        return value
    if isinstance(value, dict):
        return " ".join(
            f"{flatten_jsonish(key)} {flatten_jsonish(item)}"
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return " ".join(flatten_jsonish(item) for item in value)
    return str(value)


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", flatten_jsonish(value)).casefold()
    text = text.replace("wi-fi", "wifi").replace("wi fi", "wifi")
    text = text.replace("z-wave", "zwave").replace("z wave", "zwave")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalized_term(term: str) -> str:
    return normalized(term)


def contains_term(text: str, term: str) -> bool:
    needle = normalized_term(term)
    return bool(
        needle
        and re.search(
            rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
            text,
        )
    )


def matched_terms(text: str, terms: Iterable[str]) -> list[str]:
    return sorted({term for term in terms if contains_term(text, term)})


def within_tokens(text: str, left: set[str], right: set[str], distance: int) -> bool:
    tokens = text.split()
    left_positions = [i for i, token in enumerate(tokens) if token in left]
    right_positions = [i for i, token in enumerate(tokens) if token in right]
    return any(abs(a - b) <= distance for a in left_positions for b in right_positions)


def limited_plain(value: Any, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", flatten_jsonish(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evidence_snippet(value: Any, terms: Iterable[str], limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", flatten_jsonish(value)).strip()
    if not text:
        return ""
    lower = normalized(text)
    positions: list[int] = []
    for term in terms:
        needle = normalized_term(term)
        if needle:
            pos = lower.find(needle)
            if pos >= 0:
                positions.append(pos)
    if not positions:
        return limited_plain(text, limit)
    start = max(0, min(positions) - limit // 3)
    snippet = text[start : start + limit]
    if start:
        snippet = "…" + snippet
    if start + limit < len(text):
        snippet += "…"
    return snippet


def stable_rank(seed: int, *parts: str) -> str:
    payload = "\x1f".join([str(seed), *parts]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def gate_categories(row: dict[str, Any], device_type: str) -> list[str]:
    categories: set[str] = set()
    for reason in row.get("exclusion_reasons") or []:
        if reason.startswith(f"{device_type}:"):
            suffix = reason.split(":", 1)[1]
            if suffix.startswith("wrong_product:"):
                categories.add("wrong_product")
            else:
                categories.add(suffix)
        elif reason.startswith("global:accessory"):
            categories.add("accessory")
    candidate_types = row.get("candidate_device_types") or []
    if len(candidate_types) > 1:
        categories.add("multi-type conflict")
    return sorted(categories)


def classify_bulb(
    row: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        name: normalized(row.get(name))
        for name in (
            "title",
            "main_category",
            "categories",
            "features",
            "description",
            "store",
            "details",
        )
    }
    title_categories = f"{fields['title']} {fields['categories']}".strip()
    auxiliary = " ".join(
        fields[name] for name in ("features", "description", "store", "details")
    )
    all_text = f"{title_categories} {auxiliary}".strip()

    identity_terms = rules["smart_bulb"]["identity_terms"]
    ecosystem_terms = rules["smart_bulb"]["ecosystem_terms"]
    smart_terms = rules["smart_control"]["specific_terms"]
    bluetooth_support = rules["smart_control"]["bluetooth_support_terms"]
    wrong_terms = rules["smart_bulb"]["wrong_primary_terms"]
    accessory_terms = rules["smart_bulb"]["accessory_terms"]
    collision_terms = rules["smart_bulb"]["non_smart_collision_terms"]

    title_device = matched_terms(fields["title"], identity_terms)
    category_device = matched_terms(fields["categories"], identity_terms)
    device_hits = matched_terms(all_text, identity_terms)
    title_smart = matched_terms(title_categories, smart_terms)
    auxiliary_smart = matched_terms(auxiliary, smart_terms)
    auxiliary_specific_smart = [
        term
        for term in auxiliary_smart
        if normalized_term(term) not in {"smart home", "connected home"}
    ]
    ecosystem_hits = matched_terms(all_text, ecosystem_terms)
    wrong_hits = matched_terms(fields["title"], wrong_terms)
    accessory_hits = matched_terms(fields["title"], accessory_terms)
    collision_hits = matched_terms(fields["title"], collision_terms)

    bluetooth_present = contains_term(all_text, "bluetooth")
    bluetooth_supported = bluetooth_present and bool(
        matched_terms(all_text, bluetooth_support)
        or within_tokens(
            fields["title"],
            {"smart"},
            {"bulb", "bulbs", "playbulb"},
            6,
        )
    )
    smart_bulb_proximity = within_tokens(
        fields["title"],
        {"smart"},
        {
            "bulb",
            "bulbs",
            "a15",
            "a19",
            "a21",
            "br30",
            "br40",
            "par38",
            "e12",
            "e26",
            "e27",
            "gu10",
            "b11",
        },
        6,
    )
    explicit_smart = bool(
        title_smart
        or auxiliary_specific_smart
        or bluetooth_supported
        or smart_bulb_proximity
        or ecosystem_hits
    )
    identity_primary = bool(title_device or category_device)

    wrong_label = None
    if accessory_hits and not smart_bulb_proximity:
        wrong_label = "accessory"
    elif wrong_hits:
        wrong_label = "wrong_device_type"
    elif collision_hits and not (title_smart or auxiliary_smart):
        wrong_label = "non_smart"

    draft_positive = bool(identity_primary and explicit_smart and not wrong_label)
    multi_type = len(row.get("candidate_device_types") or []) > 1

    if wrong_label:
        audit_label = wrong_label
        proposed_decision = "exclude"
        confidence = "high"
        note = "Primary title/category identifies a prohibited product or accessory."
    elif not identity_primary:
        audit_label = "insufficient_evidence"
        proposed_decision = "exclude"
        confidence = "low"
        note = "Bulb identity is not explicit in title/categories."
    elif not explicit_smart or collision_hits:
        audit_label = "non_smart"
        proposed_decision = "exclude"
        confidence = "high" if collision_hits else "medium"
        note = "Bulb identity is present but connected-control evidence is absent."
    elif multi_type and not title_device:
        audit_label = "ambiguous"
        proposed_decision = "hold"
        confidence = "low"
        note = "Multiple device identities are present without a dominant bulb title."
    elif draft_positive:
        audit_label = "correct_target"
        proposed_decision = "include"
        confidence = "high" if (title_smart or smart_bulb_proximity) else "medium"
        note = (
            "Primary bulb identity and specific connected-control evidence are both present."
        )
    else:
        audit_label = "false_positive"
        proposed_decision = "exclude"
        confidence = "medium"
        note = "The loose W3 candidate match does not establish a smart bulb product."

    supporting_fields = []
    for name, text in fields.items():
        if matched_terms(text, identity_terms) or matched_terms(text, smart_terms):
            supporting_fields.append(name)
    all_smart_hits = sorted(
        set(title_smart + auxiliary_specific_smart)
        | ({"bluetooth"} if bluetooth_supported else set())
        | ({"smart_near_bulb"} if smart_bulb_proximity else set())
        | set(ecosystem_hits)
    )
    evidence_terms = sorted(set(device_hits + all_smart_hits))
    evidence = []
    for name in ("categories", "features", "description", "details"):
        snippet = evidence_snippet(row.get(name), evidence_terms)
        if snippet:
            evidence.append(f"{name}: {snippet}")
        if len(" | ".join(evidence)) >= 620:
            break

    return {
        "proposed_device_type": "smart_bulb",
        "supporting_metadata_fields": supporting_fields,
        "matched_device_terms": device_hits,
        "matched_smart_terms": all_smart_hits,
        "proposed_decision": proposed_decision,
        "confidence": confidence,
        "audit_label": audit_label,
        "audit_notes": note,
        "limited_metadata_evidence": " | ".join(evidence)[:700],
        "draft_positive_before_audit": draft_positive,
    }


def classify_switch(
    row: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        name: normalized(row.get(name))
        for name in (
            "title",
            "main_category",
            "categories",
            "features",
            "description",
            "store",
            "details",
        )
    }
    title_categories = f"{fields['title']} {fields['categories']}".strip()
    auxiliary = " ".join(
        fields[name] for name in ("features", "description", "store", "details")
    )
    all_text = f"{title_categories} {auxiliary}".strip()

    identity_terms = rules["smart_switch"]["identity_terms"]
    context_terms = rules["smart_switch"]["wall_lighting_context_terms"]
    protocol_terms = rules["smart_switch"]["protocol_terms"]
    smart_terms = rules["smart_control"]["specific_terms"]
    wrong_terms = rules["smart_switch"]["wrong_primary_terms"]
    accessory_terms = rules["smart_switch"]["accessory_or_remote_only_terms"]
    rf_only_terms = rules["smart_switch"]["rf_only_terms"]

    title_device = matched_terms(fields["title"], identity_terms)
    category_device = matched_terms(fields["categories"], identity_terms)
    device_hits = matched_terms(all_text, identity_terms)
    context_hits = matched_terms(all_text, context_terms)
    title_smart = matched_terms(title_categories, smart_terms)
    auxiliary_smart = matched_terms(auxiliary, smart_terms)
    protocol_hits = matched_terms(all_text, protocol_terms)
    wrong_hits = matched_terms(fields["title"], wrong_terms)
    accessory_hits = matched_terms(fields["title"], accessory_terms)
    rf_hits = matched_terms(all_text, rf_only_terms)
    smart_switch_proximity = within_tokens(
        fields["title"],
        {"smart"},
        {"switch", "dimmer", "togglelinc", "keypadlinc"},
        4,
    )
    explicit_smart = bool(
        title_smart
        or auxiliary_smart
        or protocol_hits
        or smart_switch_proximity
    )
    identity_primary = bool(title_device or category_device)
    actual_wall_context = bool(context_hits)
    remote_only = bool(accessory_hits) and not any(
        phrase in all_text
        for phrase in (
            "snaps on top of an existing toggle",
            "snaps on top of an existing rocker",
            "mechanically controls the underlying switch",
        )
    )
    rf_only = bool(rf_hits) and not bool(
        title_smart
        or auxiliary_smart
        or matched_terms(all_text, ["wifi", "zigbee", "z wave", "homekit", "alexa"])
    )

    wrong_label = None
    if remote_only:
        wrong_label = "accessory"
    elif wrong_hits:
        wrong_label = "wrong_device_type"
    elif rf_only:
        wrong_label = "non_smart"

    draft_positive = bool(
        identity_primary
        and actual_wall_context
        and explicit_smart
        and not wrong_label
    )
    multi_type = len(row.get("candidate_device_types") or []) > 1

    if wrong_label:
        audit_label = wrong_label
        proposed_decision = "exclude"
        confidence = "high"
        note = "Primary identity is a prohibited switch type, accessory, or RF-only item."
    elif not identity_primary:
        audit_label = "insufficient_evidence"
        proposed_decision = "exclude"
        confidence = "low"
        note = "Switch/dimmer identity is not explicit in title/categories."
    elif not actual_wall_context:
        audit_label = "insufficient_evidence"
        proposed_decision = "hold" if explicit_smart else "exclude"
        confidence = "low"
        note = "Metadata does not clearly establish wall-lighting control."
    elif not explicit_smart:
        audit_label = "non_smart"
        proposed_decision = "exclude"
        confidence = "medium"
        note = "Wall-lighting identity is present without approved connected-control evidence."
    elif multi_type and not any(
        contains_term(fields["title"], term)
        for term in ("light switch", "wall switch", "dimmer switch", "togglelinc")
    ):
        audit_label = "ambiguous"
        proposed_decision = "hold"
        confidence = "low"
        note = "Multiple device identities remain plausible."
    elif draft_positive:
        audit_label = "correct_target"
        proposed_decision = "include"
        confidence = "high" if (title_smart or protocol_hits) else "medium"
        note = (
            "Primary switch identity, wall-lighting context, and connected-control "
            "evidence are all present."
        )
    else:
        audit_label = "false_positive"
        proposed_decision = "exclude"
        confidence = "medium"
        note = "The loose W3 candidate match does not establish a smart wall switch."

    supporting_fields = []
    for name, text in fields.items():
        if (
            matched_terms(text, identity_terms)
            or matched_terms(text, context_terms)
            or matched_terms(text, smart_terms)
            or matched_terms(text, protocol_terms)
        ):
            supporting_fields.append(name)
    all_smart_hits = sorted(
        set(title_smart + auxiliary_smart + protocol_hits)
        | ({"smart_near_switch"} if smart_switch_proximity else set())
    )
    evidence_terms = sorted(set(device_hits + context_hits + all_smart_hits))
    evidence = []
    for name in ("categories", "features", "description", "details"):
        snippet = evidence_snippet(row.get(name), evidence_terms)
        if snippet:
            evidence.append(f"{name}: {snippet}")
        if len(" | ".join(evidence)) >= 620:
            break

    return {
        "proposed_device_type": "smart_switch",
        "supporting_metadata_fields": supporting_fields,
        "matched_device_terms": sorted(set(device_hits + context_hits)),
        "matched_smart_terms": all_smart_hits,
        "proposed_decision": proposed_decision,
        "confidence": confidence,
        "audit_label": audit_label,
        "audit_notes": note,
        "limited_metadata_evidence": " | ".join(evidence)[:700],
        "draft_positive_before_audit": draft_positive,
    }


def recovery_row(
    source: dict[str, Any],
    result: dict[str, Any],
    version: str,
    seed: int,
) -> dict[str, Any]:
    device_type = result["proposed_device_type"]
    return {
        "parent_asin": source["parent_asin"],
        "proposed_device_type": device_type,
        "source_domains": source.get("source_domains") or [],
        "main_category": source.get("main_category"),
        "title": source.get("title"),
        "categories_evidence": limited_plain(source.get("categories"), 500),
        "limited_metadata_evidence": result["limited_metadata_evidence"],
        "original_exclusion_reason": source.get("exclusion_reasons") or [],
        "exclusion_gate_categories": gate_categories(source, device_type),
        "supporting_metadata_fields": result["supporting_metadata_fields"],
        "matched_device_terms": result["matched_device_terms"],
        "matched_smart_terms": result["matched_smart_terms"],
        "proposed_decision": result["proposed_decision"],
        "confidence": result["confidence"],
        "audit_label": result["audit_label"],
        "audit_notes": result["audit_notes"],
        "candidate_device_types": source.get("candidate_device_types") or [],
        "original_ambiguity_status": source.get("ambiguity_status"),
        "draft_positive_before_audit": result["draft_positive_before_audit"],
        "draft_version": version,
        "sampling_rank": stable_rank(seed, device_type, source["parent_asin"]),
    }


def make_audit_sample(
    recovery: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    device_type: str,
    minimum_recovery: int,
    strong_exclusion_minimum: int,
    version: str,
    seed: int,
) -> list[dict[str, Any]]:
    positives_and_holds = sorted(
        [
            row
            for row in recovery
            if row["proposed_decision"] in {"include", "hold"}
        ],
        key=lambda row: row["sampling_rank"],
    )
    remaining = sorted(
        [
            row
            for row in recovery
            if row["proposed_decision"] not in {"include", "hold"}
        ],
        key=lambda row: row["sampling_rank"],
    )
    chosen = positives_and_holds[:]
    chosen_ids = {row["parent_asin"] for row in chosen}
    for row in remaining:
        if len(chosen) >= minimum_recovery:
            break
        if row["parent_asin"] not in chosen_ids:
            chosen.append(row)
            chosen_ids.add(row["parent_asin"])

    controls = sorted(
        [
            row
            for row in recovery
            if row["parent_asin"] not in chosen_ids
            and row["audit_label"]
            in {"wrong_device_type", "accessory", "non_smart", "false_positive"}
        ],
        key=lambda row: stable_rank(
            seed,
            "strong_exclusion",
            device_type,
            row["parent_asin"],
        ),
    )[:strong_exclusion_minimum]

    sample: list[dict[str, Any]] = []
    for row in chosen:
        copied = dict(row)
        copied["sample_role"] = "recovery_candidate"
        sample.append(copied)
    for row in controls:
        copied = dict(row)
        copied["sample_role"] = "strong_exclusion_control"
        sample.append(copied)
    for row in baseline:
        copied = {
            "parent_asin": row["parent_asin"],
            "proposed_device_type": device_type,
            "source_domains": row.get("source_domains") or [],
            "main_category": row.get("main_category"),
            "title": row.get("title"),
            "categories_evidence": limited_plain(row.get("categories"), 500),
            "limited_metadata_evidence": "",
            "original_exclusion_reason": [],
            "exclusion_gate_categories": [],
            "supporting_metadata_fields": ["title"],
            "matched_device_terms": [],
            "matched_smart_terms": [],
            "proposed_decision": "retain_baseline",
            "confidence": "baseline",
            "audit_label": "correct_target",
            "audit_notes": "Positive control retained from frozen W3 v1.3.2.",
            "candidate_device_types": [device_type],
            "original_ambiguity_status": "baseline",
            "draft_positive_before_audit": True,
            "draft_version": version,
            "sampling_rank": stable_rank(
                seed, "positive_control", device_type, row["parent_asin"]
            ),
            "sample_role": "positive_control",
        }
        sample.append(copied)
    return sorted(
        sample,
        key=lambda row: (
            {"positive_control": 0, "recovery_candidate": 1, "strong_exclusion_control": 2}[
                row["sample_role"]
            ],
            row["sampling_rank"],
        ),
    )


def parquet_write(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError(f"Refusing to write empty Parquet: {path}")
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")
    check = pq.read_table(path)
    if check.num_rows != len(rows):
        raise RuntimeError(f"Parquet verification failed: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force-own-outputs",
        action="store_true",
        help="Replace only files inside the dedicated W3R-A output directories.",
    )
    args = parser.parse_args()

    started = utc_now()
    started_perf = time.perf_counter()
    project = tomllib.loads(PROJECT_CONFIG.read_text(encoding="utf-8"))
    rules = tomllib.loads(DRAFT_CONFIG.read_text(encoding="utf-8"))
    base_rules = tomllib.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    version = rules["draft"]["version"]
    seed = int(rules["audit"]["random_seed"])

    interim_root = PROJECT_ROOT / project["paths"]["interim"]
    processed_root = PROJECT_ROOT / project["paths"]["processed"]
    reports_root = PROJECT_ROOT / project["paths"]["reports"]
    interim_dir = interim_root / "w3r_a"
    report_dir = reports_root / "w3r_a"
    interim_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    owned_outputs = [
        interim_dir / "bulb_recovery_candidates.parquet",
        interim_dir / "switch_recovery_candidates.parquet",
        interim_dir / "proposed_target_products_w3_v1_4_draft.parquet",
        report_dir / "w3r_a_execution.log",
        report_dir / "exclusion_gate_diagnostics.json",
        report_dir / "recovery_candidate_flow.csv",
        report_dir / "recovery_candidate_flow.json",
        report_dir / "bulb_audit_sample.csv",
        report_dir / "switch_audit_sample.csv",
        report_dir / "recovery_audit_results.json",
        report_dir / "old_vs_draft_product_counts.csv",
        report_dir / "w3r_a_summary.md",
        report_dir / "w3r_a_status.json",
    ]
    existing = [path for path in owned_outputs if path.exists()]
    if existing and not args.force_own_outputs:
        raise RuntimeError(
            "Known W3R-A outputs already exist. Inspect them, then rerun with "
            "--force-own-outputs to replace only this phase's own files: "
            + ", ".join(str(path.relative_to(PROJECT_ROOT)) for path in existing)
        )

    log_path = report_dir / "w3r_a_execution.log"
    with log_path.open("w", encoding="utf-8") as log:
        def log_line(message: str) -> None:
            line = f"{utc_now()} {message}"
            print(line)
            log.write(line + "\n")
            log.flush()

        log_line("W3R-A started; metadata-only input restriction active.")
        disk_before = shutil.disk_usage(PROJECT_ROOT)
        free_gib_before = disk_before.free / (1024**3)
        if free_gib_before < float(rules["draft"]["minimum_free_gib"]):
            raise RuntimeError(
                f"PAUSED_SPACE_GATE: free space {free_gib_before:.3f} GiB"
            )

        candidate_path = PROJECT_ROOT / rules["inputs"]["metadata_candidates"]
        target_path = PROJECT_ROOT / rules["inputs"]["target_products"]
        if not candidate_path.is_file() or not target_path.is_file():
            raise FileNotFoundError("Required W3 Parquet inputs are missing.")

        input_identity = {
            "metadata_candidates": {
                "path": str(candidate_path.relative_to(PROJECT_ROOT)),
                "bytes": candidate_path.stat().st_size,
                "mtime_ns": candidate_path.stat().st_mtime_ns,
                "sha256": sha256_file(candidate_path),
            },
            "target_products": {
                "path": str(target_path.relative_to(PROJECT_ROOT)),
                "bytes": target_path.stat().st_size,
                "mtime_ns": target_path.stat().st_mtime_ns,
                "sha256": sha256_file(target_path),
            },
            "base_rules_sha256": sha256_file(BASE_CONFIG),
            "draft_rules_sha256": sha256_file(DRAFT_CONFIG),
        }

        candidate_table = pq.read_table(
            candidate_path,
            columns=ALLOWED_IDENTITY_COLUMNS + CANDIDATE_AUDIT_COLUMNS,
        )
        target_table = pq.read_table(target_path, columns=TARGET_COLUMNS)
        if candidate_table.num_rows != int(
            rules["inputs"]["expected_candidate_rows"]
        ):
            raise RuntimeError(
                f"Candidate row mismatch: {candidate_table.num_rows}"
            )
        if target_table.num_rows != int(rules["inputs"]["expected_target_rows"]):
            raise RuntimeError(f"Target row mismatch: {target_table.num_rows}")

        candidates = candidate_table.to_pylist()
        targets = target_table.to_pylist()
        candidate_ids = [row["parent_asin"] for row in candidates]
        target_ids = {row["parent_asin"] for row in targets}
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RuntimeError("metadata_candidates parent_asin is not unique.")
        if len(target_ids) != len(targets):
            raise RuntimeError("target_products parent_asin is not unique.")
        if not target_ids.issubset(set(candidate_ids)):
            raise RuntimeError("Frozen target products are not a subset of candidates.")

        baseline_counts = Counter(row["device_type"] for row in targets)
        expected_counts = {
            "smart_plug": int(rules["inputs"]["expected_smart_plugs"]),
            "smart_bulb": int(rules["inputs"]["expected_smart_bulbs"]),
            "smart_switch": int(rules["inputs"]["expected_smart_switches"]),
        }
        if dict(baseline_counts) != expected_counts:
            raise RuntimeError(
                f"Frozen target count mismatch: {dict(baseline_counts)}"
            )
        log_line(
            "Validated 37,158 unique candidates and 106 frozen target products."
        )

        excluded = [row for row in candidates if row["parent_asin"] not in target_ids]
        recovery_by_type: dict[str, list[dict[str, Any]]] = {
            device_type: [] for device_type in DEVICE_TYPES
        }
        for source in excluded:
            candidate_types = source.get("candidate_device_types") or []
            if "smart_bulb" in candidate_types:
                result = classify_bulb(source, rules)
                recovery_by_type["smart_bulb"].append(
                    recovery_row(source, result, version, seed)
                )
            if "smart_switch" in candidate_types:
                result = classify_switch(source, rules)
                recovery_by_type["smart_switch"].append(
                    recovery_row(source, result, version, seed)
                )

        # A product proposed for both types remains ambiguous and is not restored.
        bulb_positive = {
            row["parent_asin"]
            for row in recovery_by_type["smart_bulb"]
            if row["proposed_decision"] == "include"
        }
        switch_positive = {
            row["parent_asin"]
            for row in recovery_by_type["smart_switch"]
            if row["proposed_decision"] == "include"
        }
        cross_type = bulb_positive & switch_positive
        if cross_type:
            for device_type in DEVICE_TYPES:
                for row in recovery_by_type[device_type]:
                    if row["parent_asin"] in cross_type:
                        row["proposed_decision"] = "hold"
                        row["audit_label"] = "ambiguous"
                        row["confidence"] = "low"
                        row["audit_notes"] = (
                            "Draft rules support both bulb and switch identities; "
                            "not restored without adjudication."
                        )

        for device_type in DEVICE_TYPES:
            recovery_by_type[device_type].sort(
                key=lambda row: row["parent_asin"]
            )
            parquet_write(
                interim_dir
                / (
                    "bulb_recovery_candidates.parquet"
                    if device_type == "smart_bulb"
                    else "switch_recovery_candidates.parquet"
                ),
                recovery_by_type[device_type],
            )

        accepted: dict[str, dict[str, Any]] = {}
        for device_type in DEVICE_TYPES:
            for row in recovery_by_type[device_type]:
                if (
                    row["proposed_decision"] == "include"
                    and row["audit_label"] == "correct_target"
                ):
                    accepted[row["parent_asin"]] = row

        source_by_id = {row["parent_asin"]: row for row in candidates}
        proposed: list[dict[str, Any]] = []
        for row in targets:
            proposed.append(
                {
                    **{key: row.get(key) for key in ALLOWED_IDENTITY_COLUMNS},
                    "device_type": row["device_type"],
                    "filter_version": version,
                    "baseline_filter_version": row.get("filter_version"),
                    "selection_origin": "w3_v1_3_2_baseline",
                    "recovery_confidence": None,
                    "recovery_audit_label": "correct_target",
                    "recovery_original_exclusion_reason": [],
                }
            )
        for asin, audit in accepted.items():
            source = source_by_id[asin]
            proposed.append(
                {
                    **{key: source.get(key) for key in ALLOWED_IDENTITY_COLUMNS},
                    "device_type": audit["proposed_device_type"],
                    "filter_version": version,
                    "baseline_filter_version": base_rules["filter"]["version"],
                    "selection_origin": "w3r_a_recovered",
                    "recovery_confidence": audit["confidence"],
                    "recovery_audit_label": audit["audit_label"],
                    "recovery_original_exclusion_reason": audit[
                        "original_exclusion_reason"
                    ],
                }
            )
        proposed.sort(key=lambda row: (row["device_type"], row["parent_asin"]))
        parquet_write(
            interim_dir / "proposed_target_products_w3_v1_4_draft.parquet",
            proposed,
        )

        baseline_by_type = {
            device_type: [
                row for row in targets if row["device_type"] == device_type
            ]
            for device_type in DEVICE_TYPES
        }
        audit_samples = {
            "smart_bulb": make_audit_sample(
                recovery_by_type["smart_bulb"],
                baseline_by_type["smart_bulb"],
                "smart_bulb",
                int(rules["audit"]["bulb_recovery_sample_minimum"]),
                int(rules["audit"]["strong_exclusion_controls_per_type"]),
                version,
                seed,
            ),
            "smart_switch": make_audit_sample(
                recovery_by_type["smart_switch"],
                baseline_by_type["smart_switch"],
                "smart_switch",
                int(rules["audit"]["switch_recovery_sample_minimum"]),
                int(rules["audit"]["strong_exclusion_controls_per_type"]),
                version,
                seed,
            ),
        }
        audit_fields = [
            "sample_role",
            "parent_asin",
            "proposed_device_type",
            "source_domains",
            "main_category",
            "title",
            "categories_evidence",
            "limited_metadata_evidence",
            "original_exclusion_reason",
            "exclusion_gate_categories",
            "supporting_metadata_fields",
            "matched_device_terms",
            "matched_smart_terms",
            "proposed_decision",
            "confidence",
            "audit_label",
            "audit_notes",
            "draft_version",
            "sampling_rank",
        ]
        csv_dump(
            report_dir / "bulb_audit_sample.csv",
            audit_samples["smart_bulb"],
            audit_fields,
        )
        csv_dump(
            report_dir / "switch_audit_sample.csv",
            audit_samples["smart_switch"],
            audit_fields,
        )

        gate_diagnostics: dict[str, Any] = {
            "draft_version": version,
            "base_version": base_rules["filter"]["version"],
            "generated_at": utc_now(),
            "definitions": {
                "potential_false_negative": (
                    "A baseline-excluded candidate assigned include or hold by the "
                    "draft screen; this is diagnostic, not ground truth."
                ),
                "recoverable": (
                    "A draft-positive candidate whose metadata rule-revision review "
                    "label is correct_target."
                ),
            },
            "by_device_type": {},
        }
        requested_gates = [
            "approved_identity_phrase_absent",
            "identity_not_in_title_or_categories",
            "smart_evidence_not_in_title_or_categories",
            "conditional_without_specific_smart_control_evidence",
            "required_target_context_absent",
            "multi-type conflict",
            "wrong_product",
            "accessory",
            "non_smart",
            "insufficient_evidence",
        ]
        for device_type in DEVICE_TYPES:
            rows = recovery_by_type[device_type]
            gate_counts = Counter()
            for row in rows:
                gate_counts.update(row["exclusion_gate_categories"])
            gate_counts["non_smart"] = sum(
                row["audit_label"] == "non_smart" for row in rows
            )
            gate_counts["insufficient_evidence"] = sum(
                row["audit_label"] == "insufficient_evidence" for row in rows
            )
            gate_diagnostics["by_device_type"][device_type] = {
                "excluded_candidate_pool": len(rows),
                "requested_gate_counts": {
                    gate: gate_counts.get(gate, 0) for gate in requested_gates
                },
                "all_gate_counts": dict(sorted(gate_counts.items())),
                "potential_false_negatives": sum(
                    row["proposed_decision"] in {"include", "hold"} for row in rows
                ),
                "recoverable": sum(
                    row["proposed_decision"] == "include"
                    and row["audit_label"] == "correct_target"
                    for row in rows
                ),
            }
        json_dump(report_dir / "exclusion_gate_diagnostics.json", gate_diagnostics)

        proposed_counts = Counter(row["device_type"] for row in proposed)
        flow_rows: list[dict[str, Any]] = []
        audit_results: dict[str, Any] = {
            "draft_version": version,
            "review_type": "rule_revision_review_not_independent_blind_review",
            "random_seed": seed,
            "by_device_type": {},
        }
        for device_type in DEVICE_TYPES:
            rows = recovery_by_type[device_type]
            sample = audit_samples[device_type]
            sample_roles = Counter(row["sample_role"] for row in sample)
            labels = Counter(row["audit_label"] for row in sample)
            draft_positive_rows = [
                row for row in rows if row["draft_positive_before_audit"]
            ]
            true_positive = sum(
                row["audit_label"] == "correct_target"
                for row in draft_positive_rows
            )
            precision = (
                true_positive / len(draft_positive_rows)
                if draft_positive_rows
                else None
            )
            ambiguous = sum(row["audit_label"] == "ambiguous" for row in rows)
            potential = sum(
                row["proposed_decision"] in {"include", "hold"} for row in rows
            )
            flow = {
                "device_type": device_type,
                "baseline_products": baseline_counts[device_type],
                "baseline_excluded_candidate_pool": len(rows),
                "draft_screen_positive": len(draft_positive_rows),
                "draft_hold": sum(
                    row["proposed_decision"] == "hold" for row in rows
                ),
                "draft_exclude": sum(
                    row["proposed_decision"] == "exclude" for row in rows
                ),
                "potential_false_negatives": potential,
                "audited_recoverable": true_positive,
                "draft_total_products": proposed_counts[device_type],
            }
            flow_rows.append(flow)
            audit_results["by_device_type"][device_type] = {
                "sample_size_total": len(sample),
                "sample_role_counts": dict(sample_roles),
                "sample_label_counts": dict(labels),
                "draft_positive_count": len(draft_positive_rows),
                "draft_positive_correct_target": true_positive,
                "preliminary_precision": precision,
                "ambiguous_candidate_count": ambiguous,
                "ambiguous_rate_among_potential": (
                    ambiguous / potential if potential else 0.0
                ),
                "recoverable_parent_asins": sorted(
                    row["parent_asin"]
                    for row in rows
                    if row["proposed_decision"] == "include"
                    and row["audit_label"] == "correct_target"
                ),
                "interpretation": (
                    "Initial metadata rule-revision estimate; not independent "
                    "dual-annotator blind review."
                ),
            }

        csv_dump(
            report_dir / "recovery_candidate_flow.csv",
            flow_rows,
            list(flow_rows[0]),
        )
        json_dump(
            report_dir / "recovery_candidate_flow.json",
            {
                "draft_version": version,
                "generated_at": utc_now(),
                "flow": flow_rows,
            },
        )
        json_dump(report_dir / "recovery_audit_results.json", audit_results)

        old_vs_draft = []
        for device_type in ("smart_plug", "smart_bulb", "smart_switch"):
            old = baseline_counts[device_type]
            new = proposed_counts[device_type]
            old_vs_draft.append(
                {
                    "device_type": device_type,
                    "w3_v1_3_2_products": old,
                    "w3_v1_4_0_draft_products": new,
                    "net_change": new - old,
                    "diagnostic_target_30_met": (
                        "not_applicable"
                        if device_type == "smart_plug"
                        else str(new >= int(rules["audit"]["product_count_diagnostic_target"])).lower()
                    ),
                }
            )
        csv_dump(
            report_dir / "old_vs_draft_product_counts.csv",
            old_vs_draft,
            list(old_vs_draft[0]),
        )

        precision_ok = all(
            audit_results["by_device_type"][device_type][
                "preliminary_precision"
            ]
            is not None
            and audit_results["by_device_type"][device_type][
                "preliminary_precision"
            ]
            >= float(rules["audit"]["precision_target"])
            for device_type in DEVICE_TYPES
        )
        count_ok = all(
            proposed_counts[device_type]
            >= int(rules["audit"]["product_count_diagnostic_target"])
            for device_type in DEVICE_TYPES
        )
        ambiguity_ok = all(
            audit_results["by_device_type"][device_type][
                "ambiguous_rate_among_potential"
            ]
            <= float(rules["audit"]["ambiguous_rate_target"])
            for device_type in DEVICE_TYPES
        )
        status = (
            "PAUSED_PROMOTION_APPROVAL"
            if precision_ok and count_ok and ambiguity_ok
            else "PAUSED_INSUFFICIENT_RECOVERY"
        )

        disk_after = shutil.disk_usage(PROJECT_ROOT)
        free_gib_after = disk_after.free / (1024**3)
        duration = time.perf_counter() - started_perf
        output_identity = {}
        for path in owned_outputs:
            if path.exists() and path != report_dir / "w3r_a_status.json":
                output_identity[str(path.relative_to(PROJECT_ROOT))] = {
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }

        status_payload = {
            "phase": "W3R-A",
            "status": status,
            "draft_version": version,
            "draft_frozen": False,
            "started_at": started,
            "completed_at": utc_now(),
            "duration_seconds": round(duration, 3),
            "project_root_resolved": str(PROJECT_ROOT),
            "environment": {
                "python_executable": sys.executable,
                "python_version": platform.python_version(),
                "python_64_bit": platform.architecture()[0] == "64bit",
                "pyarrow_version": pa.__version__,
                "orjson_version": orjson.__version__,
            },
            "input_identity": input_identity,
            "validated_counts": {
                "metadata_candidates": candidate_table.num_rows,
                "target_products": target_table.num_rows,
                "baseline_by_device_type": dict(baseline_counts),
            },
            "recovery": {
                device_type: {
                    **audit_results["by_device_type"][device_type],
                    "excluded_candidate_pool": len(recovery_by_type[device_type]),
                    "potential_false_negatives": gate_diagnostics[
                        "by_device_type"
                    ][device_type]["potential_false_negatives"],
                    "recoverable": gate_diagnostics["by_device_type"][device_type][
                        "recoverable"
                    ],
                }
                for device_type in DEVICE_TYPES
            },
            "draft_product_counts": dict(proposed_counts),
            "diagnostic_targets": {
                "precision_target_met": precision_ok,
                "count_target_met": count_ok,
                "ambiguity_target_met": ambiguity_ok,
                "all_targets_met": precision_ok and count_ok and ambiguity_ok,
            },
            "recommend_promote_draft": status == "PAUSED_PROMOTION_APPROVAL",
            "promotion_blockers": [
                message
                for condition, message in (
                    (
                        count_ok,
                        "The draft does not recover at least 30 reliable products in both focus types.",
                    ),
                    (
                        precision_ok,
                        "At least one focus type is below preliminary precision 0.80.",
                    ),
                    (
                        ambiguity_ok,
                        "At least one focus type exceeds ambiguous rate 0.10.",
                    ),
                )
                if not condition
            ],
            "scope_compliance": {
                "reviews_jsonl_read": False,
                "metadata_jsonl_read": False,
                "gzip_read": False,
                "review_level_base_read": False,
                "ratings_used": False,
                "average_rating_used": False,
                "rating_number_used": False,
                "price_used": False,
                "baseline_w3_outputs_modified": False,
                "baseline_w4_outputs_modified": False,
                "w4_rerun": False,
                "w5_started": False,
                "git_commit": False,
            },
            "disk": {
                "free_gib_before": round(free_gib_before, 3),
                "free_gib_after": round(free_gib_after, 3),
                "minimum_free_gib": float(rules["draft"]["minimum_free_gib"]),
            },
            "outputs": output_identity,
        }

        summary_lines = [
            "# Phase W3R-A recall diagnosis",
            "",
            f"- Status: `{status}`",
            f"- Draft version: `{version}` (not frozen)",
            f"- Frozen baseline retained: `{base_rules['filter']['version']}`",
            "- Inputs: W3 metadata candidate and target-product Parquet only",
            "- Reviews, ratings, prices, raw JSONL, and review-level Parquet were not read",
            "",
            "## Product counts",
            "",
            "| Device type | Frozen W3 | Reliable draft additions | Draft total | Diagnostic target (30) |",
            "|---|---:|---:|---:|---:|",
        ]
        for device_type in ("smart_plug", "smart_bulb", "smart_switch"):
            additions = proposed_counts[device_type] - baseline_counts[device_type]
            target_text = (
                "N/A"
                if device_type == "smart_plug"
                else ("met" if proposed_counts[device_type] >= 30 else "not met")
            )
            summary_lines.append(
                f"| {device_type} | {baseline_counts[device_type]:,} | "
                f"{additions:,} | {proposed_counts[device_type]:,} | {target_text} |"
            )
        summary_lines.extend(
            [
                "",
                "## Rule-revision review",
                "",
                "| Device type | Excluded candidate pool | Potential false negatives | Recoverable | Preliminary precision | Ambiguous |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for device_type in DEVICE_TYPES:
            audit = audit_results["by_device_type"][device_type]
            gate = gate_diagnostics["by_device_type"][device_type]
            precision = audit["preliminary_precision"]
            precision_text = "n/a" if precision is None else f"{precision:.3f}"
            summary_lines.append(
                f"| {device_type} | {len(recovery_by_type[device_type]):,} | "
                f"{gate['potential_false_negatives']:,} | {gate['recoverable']:,} | "
                f"{precision_text} | {audit['ambiguous_candidate_count']:,} |"
            )
        summary_lines.extend(
            [
                "",
                "The precision values above are initial metadata rule-revision estimates, "
                "not independent dual-annotator blind-review results.",
                "",
                "## Main evidence patterns",
                "",
                "- Bulbs: explicit bulb/form-factor identity plus Wi-Fi, Zigbee, Z-Wave, "
                "HomeKit, Matter, app/voice control, or supported Bluetooth control.",
                "- Switches: explicit switch/dimmer identity plus wall-lighting context "
                "and connected-control evidence; relay-only, remote-only, network, and "
                "RF-only items remain excluded.",
                "- Incidental wrong-product words in auxiliary descriptions no longer "
                "override a clear primary identity in title/categories.",
                "",
                "## Decision",
                "",
            ]
        )
        if status == "PAUSED_PROMOTION_APPROVAL":
            summary_lines.append(
                "The draft meets the diagnostic targets, but still requires explicit "
                "user approval before promotion or W4R."
            )
        else:
            summary_lines.append(
                "The available W3 candidate pool does not support the requested reliable "
                "expansion while preserving the product boundary. Do not promote this "
                "draft or rerun W4 without a new decision."
            )
        (report_dir / "w3r_a_summary.md").write_text(
            "\n".join(summary_lines) + "\n",
            encoding="utf-8",
        )
        log_line(
            f"Draft counts: plugs={proposed_counts['smart_plug']}, "
            f"bulbs={proposed_counts['smart_bulb']}, "
            f"switches={proposed_counts['smart_switch']}."
        )
        log_line(f"W3R-A completed with status {status}; no promotion performed.")

        # Add summary/log identities produced after the first output inventory.
        for path in (
            report_dir / "w3r_a_summary.md",
            report_dir / "w3r_a_execution.log",
        ):
            output_identity[str(path.relative_to(PROJECT_ROOT))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        status_payload["outputs"] = output_identity
        json_dump(report_dir / "w3r_a_status.json", status_payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
