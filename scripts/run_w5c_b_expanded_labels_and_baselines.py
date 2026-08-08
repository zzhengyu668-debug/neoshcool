"""Freeze 1,500 W5-C-B labels and train the expanded transparent baselines.

This phase reads only approved Parquet, workbook, configuration, and report
inputs.  It does not scan raw JSONL/gzip sources, score the full 55,877-review
corpus, create product-month engineering signals, or execute W6.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


MODULE_PATH = Path(__file__).resolve().with_name(
    "run_w5b_annotation_and_baselines.py"
)
SPEC = importlib.util.spec_from_file_location("w5b_shared", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load W5-B helpers from {MODULE_PATH}")
w5b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w5b)


PHASE = "W5-C-B"
LABEL_VERSION = "w5c-b-labels-v1.0"
DEVICE_TYPES = ("smart_plug", "smart_bulb", "smart_switch")
FAILURE_TYPES = tuple(w5b.FAILURE_TYPES)
OLD_SOURCE = "w5b_frozen_label"
NEW_DOUBLE_SOURCE = "w5c_a_adjudicated_double_review"
NEW_SINGLE_SOURCE = "w5c_a_reviewer_1_single_review"
ADJUDICATION_COLUMNS = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_1_failure_binary",
    "reviewer_1_failure_type",
    "reviewer_1_severity",
    "reviewer_1_persistence",
    "reviewer_1_confidence",
    "reviewer_1_notes",
    "reviewer_2_failure_binary",
    "reviewer_2_failure_type",
    "reviewer_2_severity",
    "reviewer_2_persistence",
    "reviewer_2_confidence",
    "reviewer_2_notes",
    "binary_agreement",
    "type_agreement",
    "severity_agreement",
    "persistence_agreement",
    "any_core_disagreement",
    "adjudicated_failure_binary",
    "adjudicated_failure_type",
    "adjudicated_severity",
    "adjudicated_persistence",
    "adjudication_notes",
]


class W5CBError(RuntimeError):
    """Base W5-C-B controlled failure."""


class InputMismatch(W5CBError):
    """An approved input differs from its manifest."""


class LabelReviewRequired(W5CBError):
    """A human label is structurally invalid and must not be auto-repaired."""


def project_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "PROJECT_HANDOFF.md").is_file():
        raise W5CBError("Could not resolve project root from script location")
    return root


def canonical_cell(value: Any) -> str:
    if w5b.blank(value):
        return ""
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)) and float(value).is_integer():
        return str(int(value))
    return str(value).strip()


def assert_file(path: Path, expected_hash: str, label: str) -> None:
    if not path.is_file():
        raise InputMismatch(f"Missing {label}: {path}")
    actual = w5b.sha256_file(path)
    if actual.lower() != expected_hash.lower():
        raise InputMismatch(
            f"{label} SHA-256 mismatch: expected={expected_hash}, actual={actual}"
        )


def assert_parquet(path: Path, rows: int, expected_hash: str, label: str) -> None:
    assert_file(path, expected_hash, label)
    actual_rows = pq.ParquetFile(path).metadata.num_rows
    if actual_rows != rows:
        raise InputMismatch(
            f"{label} row mismatch: expected={rows}, actual={actual_rows}"
        )


def validate_label(
    binary_raw: Any,
    type_raw: Any,
    severity_raw: Any,
    persistence_raw: Any,
    row_id: str,
) -> tuple[str, str, int | None, int | None]:
    try:
        binary = w5b.normalize_binary(binary_raw)
        failure_type = w5b.normalize_failure_type(type_raw)
        severity = w5b.normalize_ordinal(severity_raw, {0, 1, 2, 3})
        persistence = w5b.normalize_ordinal(persistence_raw, {0, 1, 2})
        w5b.validate_label_combination(
            binary, failure_type, severity, persistence, row_id
        )
    except Exception as exc:
        raise LabelReviewRequired(f"{row_id}: {exc}") from exc
    return binary, failure_type, severity, persistence


def load_workbook_group(
    root: Path, entries: list[dict[str, Any]], expected_columns: list[str]
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for entry in entries:
        path = root / entry["path"]
        assert_file(path, entry["sha256"], path.name)
        frame = w5b.read_xlsx_table(path, "Annotation")
        if list(frame.columns) != expected_columns:
            raise InputMismatch(f"Unexpected workbook schema: {path.name}")
        if len(frame) != int(entry["rows"]):
            raise InputMismatch(
                f"Workbook row mismatch for {path.name}: {len(frame)}"
            )
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True)
    if not result["blind_review_id"].is_unique:
        raise InputMismatch("blind_review_id repeats across completed workbooks")
    return result


def validate_new_annotation_inputs(
    reviewer_1: pd.DataFrame,
    reviewer_2: pd.DataFrame,
    adjudication: pd.DataFrame,
    sampling: pd.DataFrame,
) -> dict[str, Any]:
    if len(reviewer_1) != 1200 or len(reviewer_2) != 240:
        raise InputMismatch(
            f"Returned row mismatch: reviewer1={len(reviewer_1)}, "
            f"reviewer2={len(reviewer_2)}"
        )
    if list(adjudication.columns) != ADJUDICATION_COLUMNS:
        raise InputMismatch("Completed adjudication workbook schema changed")
    if len(adjudication) != 240 or not adjudication["blind_review_id"].is_unique:
        raise InputMismatch("Completed adjudication must contain 240 unique IDs")
    expected_double = set(
        sampling.loc[
            sampling["selected_for_double_review"].astype(bool),
            "blind_review_id",
        ].astype(str)
    )
    reviewer_1_ids = set(reviewer_1["blind_review_id"].astype(str))
    reviewer_2_ids = set(reviewer_2["blind_review_id"].astype(str))
    adjudication_ids = set(adjudication["blind_review_id"].astype(str))
    if reviewer_1_ids != set(sampling["blind_review_id"].astype(str)):
        raise InputMismatch("Reviewer 1 IDs differ from the 1,200-row sample")
    if reviewer_2_ids != expected_double or adjudication_ids != expected_double:
        raise InputMismatch("Double-review/adjudication IDs differ from sampling frame")

    r1_index = reviewer_1.set_index("blind_review_id", drop=False)
    r2_index = reviewer_2.set_index("blind_review_id", drop=False)
    protected_mismatches = 0
    agreed_rows = 0
    for _, row in adjudication.iterrows():
        row_id = str(row["blind_review_id"])
        for field in w5b.MAIN_COLUMNS[:9]:
            if canonical_cell(row[field]) != canonical_cell(r1_index.at[row_id, field]):
                protected_mismatches += 1
        for field in w5b.REVIEWER_2_COLUMNS[3:]:
            if canonical_cell(row[field]) != canonical_cell(r2_index.at[row_id, field]):
                protected_mismatches += 1
        if str(row["device_type"]) != str(r2_index.at[row_id, "device_type"]):
            protected_mismatches += 1
        if str(row["review_text"]) != str(r2_index.at[row_id, "review_text"]):
            protected_mismatches += 1

        r1 = validate_label(
            row["reviewer_1_failure_binary"],
            row["reviewer_1_failure_type"],
            row["reviewer_1_severity"],
            row["reviewer_1_persistence"],
            f"{row_id}/reviewer1",
        )
        r2 = validate_label(
            row["reviewer_2_failure_binary"],
            row["reviewer_2_failure_type"],
            row["reviewer_2_severity"],
            row["reviewer_2_persistence"],
            f"{row_id}/reviewer2",
        )
        final = validate_label(
            row["adjudicated_failure_binary"],
            row["adjudicated_failure_type"],
            row["adjudicated_severity"],
            row["adjudicated_persistence"],
            f"{row_id}/adjudicated",
        )
        if r1 == r2:
            agreed_rows += 1
            if final != r1:
                raise LabelReviewRequired(
                    f"{row_id}: adjudication changed a common independent conclusion"
                )
    if protected_mismatches:
        raise InputMismatch(
            f"Completed adjudication differs from returned labels in "
            f"{protected_mismatches} protected cells"
        )

    for _, row in reviewer_1.iterrows():
        row_id = str(row["blind_review_id"])
        validate_label(
            row["reviewer_1_failure_binary"],
            row["reviewer_1_failure_type"],
            row["reviewer_1_severity"],
            row["reviewer_1_persistence"],
            f"{row_id}/reviewer1",
        )
        if any(
            not w5b.blank(row[field])
            for field in (
                "adjudicated_failure_binary",
                "adjudicated_failure_type",
                "adjudicated_severity",
                "adjudicated_persistence",
            )
        ):
            raise InputMismatch(
                f"{row_id}: Reviewer 1 return unexpectedly contains adjudication"
            )
    for _, row in reviewer_2.iterrows():
        validate_label(
            row["reviewer_2_failure_binary"],
            row["reviewer_2_failure_type"],
            row["reviewer_2_severity"],
            row["reviewer_2_persistence"],
            f"{row['blind_review_id']}/reviewer2",
        )
    return {
        "reviewer_1_rows": len(reviewer_1),
        "reviewer_2_rows": len(reviewer_2),
        "adjudicated_rows": len(adjudication),
        "protected_source_cell_mismatches": protected_mismatches,
        "independent_all_core_agreement_rows": agreed_rows,
        "adjudication_status": "VALIDATED",
    }


def build_expanded_labels(
    old_labels: pd.DataFrame,
    old_modeling: pd.DataFrame,
    old_sampling: pd.DataFrame,
    reviewer_1: pd.DataFrame,
    adjudication: pd.DataFrame,
    new_sampling: pd.DataFrame,
    new_blind_key: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if len(old_labels) != 300 or len(old_modeling) != 300:
        raise InputMismatch("W5-B frozen inputs must each contain 300 rows")
    if set(old_labels["blind_review_id"]) != set(old_modeling["blind_review_id"]):
        raise InputMismatch("W5-B label/modeling IDs differ")
    private = new_sampling.merge(
        new_blind_key.drop(columns=["device_type"], errors="ignore"),
        on="blind_review_id",
        how="inner",
        validate="one_to_one",
        suffixes=("_sample", "_key"),
    )
    if len(private) != 1200:
        raise InputMismatch("New sampling frame and blind key do not join one-to-one")
    for field in ("duplicate_key", "parent_asin", "rating", "review_datetime"):
        left = private[f"{field}_sample"].astype(str)
        right = private[f"{field}_key"].astype(str)
        if not left.equals(right):
            raise InputMismatch(f"Private W5-C-A mapping mismatch for {field}")
    if not new_sampling["duplicate_key"].is_unique:
        raise InputMismatch("New sampling frame duplicate_key is not unique")

    r1_index = reviewer_1.set_index("blind_review_id", drop=False)
    adj_index = adjudication.set_index("blind_review_id", drop=False)
    double_ids = set(adjudication["blind_review_id"].astype(str))
    new_rows: list[dict[str, Any]] = []
    new_model_rows: list[dict[str, Any]] = []
    star_headers_removed = 0
    for sample in new_sampling.to_dict(orient="records"):
        row_id = str(sample["blind_review_id"])
        annotation = (
            adj_index.loc[row_id] if row_id in double_ids else r1_index.loc[row_id]
        )
        if row_id in double_ids:
            prefix = "adjudicated"
            source = NEW_DOUBLE_SOURCE
        else:
            prefix = "reviewer_1"
            source = NEW_SINGLE_SOURCE
        binary, failure_type, severity, persistence = validate_label(
            annotation[f"{prefix}_failure_binary"],
            annotation[f"{prefix}_failure_type"],
            annotation[f"{prefix}_severity"],
            annotation[f"{prefix}_persistence"],
            f"{row_id}/final",
        )
        review_text = str(sample["review_text"])
        if review_text != str(r1_index.at[row_id, "review_text"]):
            raise InputMismatch(f"{row_id}: workbook text differs from sampling frame")
        model_text, substitutions = w5b.STAR_HEADER_RE.subn(
            "", review_text, count=1
        )
        star_headers_removed += substitutions
        row = {
            "blind_review_id": row_id,
            "duplicate_key": str(sample["duplicate_key"]),
            "parent_asin": str(sample["parent_asin"]),
            "device_type": str(sample["device_type"]),
            "review_datetime": pd.to_datetime(sample["review_datetime"], utc=True),
            "final_failure_binary": binary,
            "final_failure_type": failure_type or None,
            "final_severity": severity,
            "final_persistence": persistence,
            "annotation_source": source,
            "label_status": "definite" if binary in {"0", "1"} else "uncertain",
            "annotation_version": LABEL_VERSION,
            "keyword_candidate_hit": bool(sample["keyword_candidate_hit"]),
            "sampling_round": "W5-C-A",
            "sampling_strategy": str(sample["sampling_bucket"]),
            "previous_annotation_source": None,
            "previous_annotation_version": "w5a-annotation-v1.0-draft",
        }
        new_rows.append(row)
        new_model_rows.append(
            {
                **row,
                "review_text": review_text,
                "model_text": model_text,
                "rating": float(sample["rating"]),
                "low_star_indicator": int(float(sample["rating"]) <= 2.0),
                "split": None,
            }
        )

    old_sampling_lookup = old_sampling.set_index("blind_review_id")
    old_model_lookup = old_modeling.set_index("blind_review_id")
    old_rows: list[dict[str, Any]] = []
    old_model_rows: list[dict[str, Any]] = []
    label_fields = [
        "final_failure_binary",
        "final_failure_type",
        "final_severity",
        "final_persistence",
        "label_status",
    ]
    for old in old_labels.to_dict(orient="records"):
        row_id = str(old["blind_review_id"])
        for field in label_fields:
            if canonical_cell(old[field]) != canonical_cell(old_model_lookup.at[row_id, field]):
                raise InputMismatch(f"W5-B frozen label mismatch for {row_id}/{field}")
        row = {
            **old,
            "annotation_source": OLD_SOURCE,
            "annotation_version": LABEL_VERSION,
            "sampling_round": "W5-A",
            "sampling_strategy": str(
                old_sampling_lookup.at[row_id, "sampling_stratum"]
            ),
            "previous_annotation_source": str(old["annotation_source"]),
            "previous_annotation_version": str(old["annotation_version"]),
        }
        old_rows.append(row)
        old_model_rows.append(
            {
                **row,
                "review_text": str(old_model_lookup.at[row_id, "review_text"]),
                "model_text": str(old_model_lookup.at[row_id, "model_text"]),
                "rating": float(old_model_lookup.at[row_id, "rating"]),
                "low_star_indicator": int(
                    old_model_lookup.at[row_id, "low_star_indicator"]
                ),
                "split": None,
            }
        )

    labels = pd.DataFrame(old_rows + new_rows).sort_values(
        "blind_review_id", kind="mergesort"
    ).reset_index(drop=True)
    modeling = pd.DataFrame(old_model_rows + new_model_rows).sort_values(
        "blind_review_id", kind="mergesort"
    ).reset_index(drop=True)
    if len(labels) != 1500 or len(modeling) != 1500:
        raise InputMismatch("Expanded outputs must each contain 1,500 rows")
    if not labels["blind_review_id"].is_unique or not labels["duplicate_key"].is_unique:
        raise InputMismatch("Expanded labels contain duplicate identifiers")
    if set(old_labels["duplicate_key"]).intersection(set(new_sampling["duplicate_key"])):
        raise InputMismatch("Original 300 and new 1,200 duplicate_key sets overlap")
    counts = Counter(labels["device_type"])
    expected = {"smart_plug": 1137, "smart_bulb": 300, "smart_switch": 63}
    if dict(counts) != expected:
        raise InputMismatch(f"Expanded device counts differ: {dict(counts)}")
    for row in labels.itertuples(index=False):
        validate_label(
            row.final_failure_binary,
            row.final_failure_type,
            row.final_severity,
            row.final_persistence,
            f"{row.blind_review_id}/frozen",
        )
    return labels, modeling, {
        "old_rows": 300,
        "new_rows": 1200,
        "new_double_rows": len(double_ids),
        "new_single_rows": 1200 - len(double_ids),
        "total_rows": 1500,
        "blind_review_id_unique": True,
        "duplicate_key_unique": True,
        "old_new_duplicate_overlap": 0,
        "device_counts": expected,
        "leading_star_title_removed_new_rows": star_headers_removed,
        "old_label_decision_fields_preserved": True,
    }


def agreement_metrics(double: pd.DataFrame) -> dict[str, Any]:
    n_rows = len(double)
    r1_binary = [w5b.normalize_binary(v) for v in double["reviewer_1_failure_binary"]]
    r2_binary = [w5b.normalize_binary(v) for v in double["reviewer_2_failure_binary"]]
    binary_labels = ["0", "1", "uncertain"]
    definite_mask = [
        a in {"0", "1"} and b in {"0", "1"}
        for a, b in zip(r1_binary, r2_binary)
    ]
    definite_left = [v for v, keep in zip(r1_binary, definite_mask) if keep]
    definite_right = [v for v, keep in zip(r2_binary, definite_mask) if keep]

    left_matrix = np.array(
        [[int(code in w5b.failure_type_set(v)) for code in FAILURE_TYPES]
         for v in double["reviewer_1_failure_type"]]
    )
    right_matrix = np.array(
        [[int(code in w5b.failure_type_set(v)) for code in FAILURE_TYPES]
         for v in double["reviewer_2_failure_type"]]
    )
    exact = np.all(left_matrix == right_matrix, axis=1)
    jaccards = []
    for left, right in zip(left_matrix, right_matrix):
        union = int(np.logical_or(left, right).sum())
        intersection = int(np.logical_and(left, right).sum())
        jaccards.append(1.0 if union == 0 else intersection / union)
    micro = w5b.precision_recall_fscore_support(
        left_matrix, right_matrix, average="micro", zero_division=0
    )
    macro = w5b.precision_recall_fscore_support(
        left_matrix, right_matrix, average="macro", zero_division=0
    )
    per_type = []
    for index, code in enumerate(FAILURE_TYPES):
        left = left_matrix[:, index]
        right = right_matrix[:, index]
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

    def ordinal(field: str, allowed: set[int]) -> dict[str, Any]:
        left_values: list[int] = []
        right_values: list[int] = []
        for raw_left, raw_right in zip(
            double[f"reviewer_1_{field}"], double[f"reviewer_2_{field}"]
        ):
            left = w5b.normalize_ordinal(raw_left, allowed)
            right = w5b.normalize_ordinal(raw_right, allowed)
            if left is not None and right is not None:
                left_values.append(left)
                right_values.append(right)
        return {
            "valid_comparisons": len(left_values),
            "excluded_blank_or_uncertain": n_rows - len(left_values),
            "raw_agreement": (
                float(w5b.accuracy_score(left_values, right_values))
                if left_values else None
            ),
            "linear_weighted_cohens_kappa": w5b._safe_kappa(
                left_values, right_values, "linear"
            ),
            "quadratic_weighted_cohens_kappa": w5b._safe_kappa(
                left_values, right_values, "quadratic"
            ),
            "confusion_matrix": w5b.matrix_payload(
                left_values, right_values, sorted(allowed)
            ) if left_values else None,
        }

    return {
        "double_review_rows": n_rows,
        "failure_binary": {
            "including_uncertain": {
                "valid_comparisons": n_rows,
                "raw_agreement": float(w5b.accuracy_score(r1_binary, r2_binary)),
                "cohens_kappa": w5b._safe_kappa(r1_binary, r2_binary),
                "confusion_matrix": w5b.matrix_payload(
                    r1_binary, r2_binary, binary_labels
                ),
            },
            "excluding_rows_where_either_is_uncertain": {
                "valid_comparisons": len(definite_left),
                "excluded_rows": n_rows - len(definite_left),
                "raw_agreement": float(
                    w5b.accuracy_score(definite_left, definite_right)
                ) if definite_left else None,
                "cohens_kappa": w5b._safe_kappa(definite_left, definite_right),
                "confusion_matrix": w5b.matrix_payload(
                    definite_left, definite_right, ["0", "1"]
                ) if definite_left else None,
            },
        },
        "failure_type_multilabel": {
            "labels": list(FAILURE_TYPES),
            "exact_match_count": int(exact.sum()),
            "exact_match_agreement": float(exact.mean()),
            "mean_jaccard_similarity": float(np.mean(jaccards)),
            "median_jaccard_similarity": float(np.median(jaccards)),
            "micro_presence_absence_agreement": float(
                (left_matrix == right_matrix).mean()
            ),
            "macro_presence_absence_agreement": float(
                np.mean((left_matrix == right_matrix).mean(axis=0))
            ),
            "micro_precision": float(micro[0]),
            "micro_recall": float(micro[1]),
            "micro_f1": float(micro[2]),
            "macro_precision": float(macro[0]),
            "macro_recall": float(macro[1]),
            "macro_f1": float(macro[2]),
            "per_type": per_type,
        },
        "severity": ordinal("severity", {0, 1, 2, 3}),
        "persistence": ordinal("persistence", {0, 1, 2}),
        "agreement_uses_pre_adjudication_independent_labels": True,
    }


def agreement_bundle(
    old_main: pd.DataFrame,
    old_reviewer_2: pd.DataFrame,
    new_adjudication: pd.DataFrame,
) -> dict[str, Any]:
    old_double = old_main.merge(
        old_reviewer_2,
        on=["blind_review_id", "device_type", "review_text"],
        how="inner",
        validate="one_to_one",
    )
    if len(old_double) != 60:
        raise InputMismatch("Original double-review subset is not 60 rows")
    independent_columns = [
        "blind_review_id",
        "device_type",
        "review_text",
        *w5b.MAIN_COLUMNS[3:9],
        *w5b.REVIEWER_2_COLUMNS[3:],
    ]
    new_double = new_adjudication[independent_columns].copy()
    combined = pd.concat(
        [old_double[independent_columns], new_double], ignore_index=True
    )
    if len(combined) != 300 or not combined["blind_review_id"].is_unique:
        raise InputMismatch("Combined independent review set must be 300 unique rows")
    return {
        "method_note": (
            "All agreement statistics use the two reviewers' independent "
            "pre-adjudication labels; adjudicated labels are excluded."
        ),
        "w5a_original_60": agreement_metrics(old_double),
        "w5c_a_new_240": agreement_metrics(new_double),
        "combined_300": agreement_metrics(combined),
    }


def label_reports(labels: pd.DataFrame) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    definite = labels.loc[labels["label_status"] == "definite"]
    uncertain = labels.loc[labels["label_status"] == "uncertain"]
    type_counts: Counter[str] = Counter()
    for value in definite["final_failure_type"]:
        type_counts.update(w5b.failure_type_set(value))
    count_rows: list[dict[str, Any]] = []
    for scope, subset in [("overall", labels)] + [
        (device, labels.loc[labels["device_type"] == device])
        for device in DEVICE_TYPES
    ]:
        count_rows.append(
            {
                "scope": scope,
                "rows": len(subset),
                "definite": int((subset["label_status"] == "definite").sum()),
                "uncertain": int((subset["label_status"] == "uncertain").sum()),
                "failure_0": int((subset["final_failure_binary"] == "0").sum()),
                "failure_1": int((subset["final_failure_binary"] == "1").sum()),
            }
        )
    return {
        "annotation_version": LABEL_VERSION,
        "rows": len(labels),
        "definite_rows": len(definite),
        "uncertain_rows": len(uncertain),
        "uncertain_blind_review_ids": uncertain["blind_review_id"].tolist(),
        "failure_0": int((definite["final_failure_binary"] == "0").sum()),
        "failure_1": int((definite["final_failure_binary"] == "1").sum()),
        "device_type_counts": dict(Counter(labels["device_type"])),
        "annotation_source_counts": dict(Counter(labels["annotation_source"])),
        "failure_type_counts_overlapping": dict(sorted(type_counts.items())),
        "severity_counts": {
            str(k): int(v) for k, v in sorted(
                Counter(definite["final_severity"].dropna().astype(int)).items()
            )
        },
        "persistence_counts": {
            str(k): int(v) for k, v in sorted(
                Counter(definite["final_persistence"].dropna().astype(int)).items()
            )
        },
        "sampling_is_population_representative": False,
    }, count_rows


def enrich_split_manifest(
    manifest: dict[str, Any], definite: pd.DataFrame
) -> dict[str, Any]:
    rows_by_name = {row["split"]: row for row in manifest["split_rows"]}
    for split_name, row in rows_by_name.items():
        subset = definite.loc[definite["split"] == split_name]
        row["w5a_rows"] = int((subset["sampling_round"] == "W5-A").sum())
        row["w5c_a_rows"] = int((subset["sampling_round"] == "W5-C-A").sum())
    manifest["uncertain_blind_review_ids_excluded"] = []
    return manifest


def add_validation_fixed_metrics(
    evaluation: dict[str, Any], working: pd.DataFrame, prediction_column: str
) -> None:
    subset = working.loc[working["split"] == "validation"]
    evaluation["validation"] = w5b.binary_metrics(
        subset["final_failure_binary"].astype(int),
        subset[prediction_column].astype(int),
    )


def comparison_rows(
    old_eval: dict[str, Any], new_eval: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    metrics = [
        "accuracy", "balanced_accuracy", "precision", "recall", "f1",
        "specificity", "roc_auc", "pr_auc",
    ]
    for scope in ("validation", "test"):
        old = old_eval["evaluations"][scope]
        new = new_eval["evaluations"][scope]
        for metric in metrics:
            old_value = old.get(metric)
            new_value = new.get(metric)
            rows.append(
                {
                    "scope": scope,
                    "metric": metric,
                    "w5b_pilot": old_value,
                    "w5c_b_expanded": new_value,
                    "difference": (
                        new_value - old_value
                        if old_value is not None and new_value is not None else None
                    ),
                }
            )
    return rows


def agreement_markdown(bundle: dict[str, Any]) -> str:
    lines = [
        "# W5-C-B Inter-annotator Agreement",
        "",
        "All metrics below use the two reviewers' independent labels before adjudication.",
        "",
        "| Cohort | N | Binary agreement | Binary kappa | Type exact match | Mean Jaccard | Severity linear kappa | Persistence linear kappa |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (
        ("w5a_original_60", "Original W5-A"),
        ("w5c_a_new_240", "New W5-C-A"),
        ("combined_300", "Combined"),
    ):
        item = bundle[key]
        binary = item["failure_binary"]["including_uncertain"]
        failure_type = item["failure_type_multilabel"]
        lines.append(
            f"| {label} | {item['double_review_rows']} | "
            f"{binary['raw_agreement']:.4f} | {binary['cohens_kappa']:.4f} | "
            f"{failure_type['exact_match_agreement']:.4f} | "
            f"{failure_type['mean_jaccard_similarity']:.4f} | "
            f"{item['severity']['linear_weighted_cohens_kappa']:.4f} | "
            f"{item['persistence']['linear_weighted_cohens_kappa']:.4f} |"
        )
    lines.extend([
        "",
        "Failure type is multi-label; exact match, Jaccard, micro/macro and per-code statistics are available in the JSON report.",
        "",
    ])
    return "\n".join(lines)


def summary_markdown(
    labels: dict[str, Any], agreement: dict[str, Any], split: dict[str, Any],
    rating: dict[str, Any], keyword: dict[str, Any], dummy: dict[str, Any],
    tfidf: dict[str, Any], comparison: list[dict[str, Any]], readiness: str,
) -> str:
    combined = agreement["combined_300"]
    binary = combined["failure_binary"]["including_uncertain"]
    test = tfidf["evaluations"]["test"]
    validation = tfidf["evaluations"]["validation"]
    split_lines = "\n".join(
        f"| {row['split']} | {row['rows']} | {row['failure_0']} | "
        f"{row['failure_1']} | {row['earliest_utc']} | {row['latest_utc']} |"
        for row in split["split_rows"]
    )
    return f"""# Phase W5-C-B Summary

Technical status: **PASS**
W6 readiness: **{readiness}**

## Frozen labels

- Total annotations: {labels['rows']}
- Definite labels used for modeling: {labels['definite_rows']}
- Uncertain labels retained but excluded: {labels['uncertain_rows']}
- Non-failure/failure definite labels: {labels['failure_0']} / {labels['failure_1']}
- Device totals: {labels['device_type_counts']}

The sample is deliberately stratified and includes model-uncertainty and disagreement sampling. Its failure share is not an estimate of prevalence in the full 55,877-review corpus.

## Independent agreement

- Combined double-review rows: 300
- Binary raw agreement including uncertain: {binary['raw_agreement']:.4f}
- Binary Cohen's kappa including uncertain: {binary['cohens_kappa']:.4f}
- Failure-type exact match: {combined['failure_type_multilabel']['exact_match_agreement']:.4f}
- Failure-type mean Jaccard: {combined['failure_type_multilabel']['mean_jaccard_similarity']:.4f}
- Severity linear/quadratic kappa: {combined['severity']['linear_weighted_cohens_kappa']:.4f} / {combined['severity']['quadratic_weighted_cohens_kappa']:.4f}
- Persistence linear/quadratic kappa: {combined['persistence']['linear_weighted_cohens_kappa']:.4f} / {combined['persistence']['quadratic_weighted_cohens_kappa']:.4f}

## Chronological split

| Split | N | Non-failure | Failure | Earliest UTC | Latest UTC |
|---|---:|---:|---:|---|---|
{split_lines}

## Expanded TF-IDF + Logistic Regression

- Validation balanced accuracy/F1: {validation['balanced_accuracy']:.4f} / {validation['f1']:.4f}
- Test balanced accuracy/F1: {test['balanced_accuracy']:.4f} / {test['f1']:.4f}
- Test ROC-AUC/PR-AUC: {test['roc_auc']:.4f} / {test['pr_auc']:.4f}
- Overfitting diagnostic: {tfidf['overfitting_diagnostic']['status']}

Rating remains a fixed signal (`rating <= 2`) rather than a failure label. Keyword rules were not refit from these labels. Device-specific results are exploratory when support is insufficient.

No full-corpus scoring, product-month engineering signal, future target, W6 execution, raw-source read, or Git commit occurred.
"""


def protected_paths(root: Path, config: dict[str, Any]) -> list[Path]:
    paths = [
        root / "data/amazon_reviews_2023/processed/target_products.parquet",
        root / "data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base.parquet",
        root / "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet",
        root / config["inputs"]["old_labels"],
        root / config["inputs"]["old_modeling_dataset"],
        root / "data/amazon_reviews_2023/interim/w5b/baseline_predictions.parquet",
        root / "data/amazon_reviews_2023/interim/w5b/error_analysis_private.parquet",
        root / "outputs/models/w5b_tfidf_logistic_regression.joblib",
    ]
    paths.extend(sorted((root / "data/amazon_reviews_2023/reports/w5b").glob("*")))
    for entry in config["inputs"]["reviewer_1_workbooks"]:
        paths.append(root / entry["path"])
    for entry in config["inputs"]["reviewer_2_workbooks"]:
        paths.append(root / entry["path"])
    paths.append(root / config["inputs"]["completed_adjudication"])
    return [path for path in paths if path.is_file()]


def identity_map(root: Path, paths: Iterable[Path]) -> dict[str, Any]:
    return {
        w5b.relative(root, path): w5b.file_identity(root, path)
        for path in paths
    }


def environment_payload() -> dict[str, Any]:
    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "pandas_version": pd.__version__,
        "pyarrow_version": pa.__version__,
        "scikit_learn_version": w5b.importlib.metadata.version("scikit-learn"),
        "joblib_version": joblib.__version__,
        "numpy_version": np.__version__,
    }


def run() -> int:
    started = datetime.now(timezone.utc)
    started_clock = time.monotonic()
    root = project_root()
    config_path = root / "config/w5c_b_baseline_rules.toml"
    config = w5b.load_toml(config_path)
    if config["phase"]["name"] != PHASE or config["phase"]["label_version"] != LABEL_VERSION:
        raise InputMismatch("Unexpected W5-C-B configuration identity")
    report_dir = root / config["outputs"]["report_dir"]
    interim_dir = root / "data/amazon_reviews_2023/interim/w5c_b"
    report_dir.mkdir(parents=True, exist_ok=True)
    interim_dir.mkdir(parents=True, exist_ok=True)
    (root / "outputs/models").mkdir(parents=True, exist_ok=True)
    log_path = report_dir / "w5c_b_execution.log"
    if log_path.exists():
        log_path.unlink()
    w5b.log_message(log_path, "W5-C-B start")
    initial_free = w5b.disk_free_gib(root)
    if initial_free < 60:
        raise W5CBError(f"PAUSED_SPACE_GATE: only {initial_free:.3f} GiB free")

    inputs = config["inputs"]
    for key in ("w5b_status", "w5ca_adjudication_validation", "w5ca_adjudication_status"):
        assert_file(root / inputs[key], inputs[f"{key}_sha256"], key)
    w5b_status = w5b.load_json(root / inputs["w5b_status"])
    validation_status = w5b.load_json(root / inputs["w5ca_adjudication_validation"])
    adjudication_status = w5b.load_json(root / inputs["w5ca_adjudication_status"])
    if w5b_status.get("status") != "PASS":
        raise InputMismatch("W5-B status is not PASS")
    if validation_status.get("status") != "PASS" or validation_status.get("issue_count") != 0:
        raise InputMismatch("W5-C-A adjudication validation is not PASS")
    if adjudication_status.get("status") != "PAUSED_W5C_B_APPROVAL" or adjudication_status.get("w5c_b_readiness") != "READY_FOR_EXPLICIT_APPROVAL":
        raise InputMismatch("W5-C-A is not ready for explicit W5-C-B approval")

    parquet_specs = [
        ("old_labels", "old_labels_rows", "old_labels_sha256"),
        ("old_modeling_dataset", "old_modeling_rows", "old_modeling_sha256"),
        ("old_sampling_frame", "old_sampling_frame_rows", "old_sampling_frame_sha256"),
        ("new_sampling_frame", "new_sampling_frame_rows", "new_sampling_frame_sha256"),
        ("new_blind_key", "new_blind_key_rows", "new_blind_key_sha256"),
        ("formal_review_parquet", "formal_review_rows", "formal_review_sha256"),
    ]
    for path_key, row_key, hash_key in parquet_specs:
        assert_parquet(
            root / inputs[path_key], int(inputs[row_key]), inputs[hash_key], path_key
        )
    for key in ("old_adjudicated_workbook", "old_reviewer_2_workbook", "completed_adjudication"):
        assert_file(root / inputs[key], inputs[f"{key}_sha256"], key)

    protected_before = identity_map(root, protected_paths(root, config))
    input_manifest = {
        "phase": PHASE,
        "started_at_utc": started.isoformat(),
        "project_root_resolved": str(root),
        "environment": environment_payload(),
        "initial_free_gib": initial_free,
        "inputs": {},
        "protected_files_before": protected_before,
        "raw_jsonl_read": False,
        "compressed_source_read": False,
        "formal_review_rows_verified_from_parquet_metadata": 55877,
        "full_corpus_scored": False,
    }
    input_files = [
        config_path,
        *[root / inputs[key] for key, _, _ in parquet_specs],
        root / inputs["old_adjudicated_workbook"],
        root / inputs["old_reviewer_2_workbook"],
        root / inputs["completed_adjudication"],
        root / inputs["w5b_status"],
        root / inputs["w5ca_adjudication_validation"],
        root / inputs["w5ca_adjudication_status"],
    ]
    input_files.extend(root / e["path"] for e in inputs["reviewer_1_workbooks"])
    input_files.extend(root / e["path"] for e in inputs["reviewer_2_workbooks"])
    for path in input_files:
        input_manifest["inputs"][w5b.relative(root, path)] = w5b.file_identity(root, path)
    w5b.write_json(report_dir / "w5c_b_input_manifest.json", input_manifest)

    reviewer_1 = load_workbook_group(
        root, inputs["reviewer_1_workbooks"], list(w5b.MAIN_COLUMNS)
    )
    reviewer_2 = load_workbook_group(
        root, inputs["reviewer_2_workbooks"], list(w5b.REVIEWER_2_COLUMNS)
    )
    adjudication = w5b.read_xlsx_table(
        root / inputs["completed_adjudication"], "Adjudication"
    )
    old_main = w5b.read_xlsx_table(root / inputs["old_adjudicated_workbook"])
    old_reviewer_2 = w5b.read_xlsx_table(root / inputs["old_reviewer_2_workbook"])
    old_labels = pq.read_table(root / inputs["old_labels"]).to_pandas()
    old_modeling = pq.read_table(root / inputs["old_modeling_dataset"]).to_pandas()
    old_sampling = pq.read_table(root / inputs["old_sampling_frame"]).to_pandas()
    new_sampling = pq.read_table(root / inputs["new_sampling_frame"]).to_pandas()
    new_blind_key = pq.read_table(root / inputs["new_blind_key"]).to_pandas()

    return_validation = validate_new_annotation_inputs(
        reviewer_1, reviewer_2, adjudication, new_sampling
    )
    labels, modeling, merge_validation = build_expanded_labels(
        old_labels, old_modeling, old_sampling, reviewer_1, adjudication,
        new_sampling, new_blind_key,
    )
    label_summary, label_count_rows = label_reports(labels)
    agreement = agreement_bundle(old_main, old_reviewer_2, adjudication)
    w5b.log_message(
        log_path,
        f"Labels frozen: definite={label_summary['definite_rows']} "
        f"uncertain={label_summary['uncertain_rows']}",
    )

    definite, split_manifest = w5b.chronological_split(modeling)
    split_manifest = enrich_split_manifest(split_manifest, definite)
    split_manifest["uncertain_blind_review_ids_excluded"] = label_summary[
        "uncertain_blind_review_ids"
    ]
    split_lookup = definite.set_index("blind_review_id")["split"]
    modeling["split"] = modeling["blind_review_id"].map(split_lookup)
    (
        rating_evaluation,
        keyword_evaluation,
        dummy_evaluation,
        working,
        predictions,
        top_features,
        model_results,
    ) = w5b.model_and_evaluate(definite, config)
    add_validation_fixed_metrics(rating_evaluation, working, "rating_prediction")
    add_validation_fixed_metrics(keyword_evaluation, working, "keyword_prediction")
    tfidf_evaluation = model_results["evaluation"]
    model_results["model_bundle"]["label_version"] = LABEL_VERSION
    model_results["model_bundle"]["phase"] = PHASE
    old_eval = w5b.load_json(root / inputs["w5b_evaluation"])
    comparison = comparison_rows(old_eval, tfidf_evaluation)
    w5b.log_message(log_path, "Expanded baselines trained and evaluated")

    label_path = root / config["outputs"]["processed_labels"]
    modeling_path = root / config["outputs"]["modeling_dataset"]
    predictions_path = root / config["outputs"]["baseline_predictions"]
    error_path = root / config["outputs"]["error_analysis_private"]
    model_path = root / config["outputs"]["model"]
    label_output = labels.copy()
    label_output["final_failure_binary"] = label_output["final_failure_binary"].astype("string")
    label_output["final_failure_type"] = label_output["final_failure_type"].astype("string")
    label_output["final_severity"] = label_output["final_severity"].astype("Int8")
    label_output["final_persistence"] = label_output["final_persistence"].astype("Int8")
    modeling["final_severity"] = modeling["final_severity"].astype("Int8")
    modeling["final_persistence"] = modeling["final_persistence"].astype("Int8")
    w5b.write_parquet(label_path, label_output)
    w5b.write_parquet(modeling_path, modeling)
    w5b.write_parquet(predictions_path, predictions)
    w5b.write_parquet(error_path, model_results["errors_private"])
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
    joblib.dump(model_results["model_bundle"], temporary_model)
    temporary_model.replace(model_path)

    w5b.write_json(report_dir / "expanded_label_summary.json", label_summary)
    w5b.write_csv(
        report_dir / "expanded_label_counts.csv", label_count_rows,
        ["scope", "rows", "definite", "uncertain", "failure_0", "failure_1"],
    )
    w5b.write_json(report_dir / "inter_annotator_agreement_300.json", agreement)
    (report_dir / "inter_annotator_agreement_300.md").write_text(
        agreement_markdown(agreement), encoding="utf-8"
    )
    w5b.write_json(report_dir / "time_split_manifest.json", split_manifest)
    w5b.write_csv(
        report_dir / "time_split_counts.csv", split_manifest["split_rows"],
        ["split", "rows", "failure_0", "failure_1", "smart_plug", "smart_bulb", "smart_switch", "earliest_utc", "latest_utc", "unique_parent_asin", "w5a_rows", "w5c_a_rows"],
    )
    w5b.write_json(report_dir / "rating_baseline_evaluation.json", {
        "baseline": "B0 Rating", "prediction": "rating <= 2",
        "ground_truth": "final_failure_binary", "rating_is_ground_truth": False,
        **rating_evaluation,
    })
    w5b.write_json(report_dir / "keyword_baseline_evaluation.json", {
        "baseline": "B3 Keyword/rule draft",
        "keyword_version": "w5a-keyword-v1.0-draft",
        "rules_refit_from_labels": False, **keyword_evaluation,
    })
    w5b.write_json(report_dir / "dummy_baseline_evaluation.json", dummy_evaluation)
    w5b.write_json(report_dir / "tfidf_logistic_evaluation.json", tfidf_evaluation)
    w5b.write_csv(
        report_dir / "w5b_vs_w5c_b_comparison.csv", comparison,
        ["scope", "metric", "w5b_pilot", "w5c_b_expanded", "difference"],
    )
    confusion = w5b.confusion_rows({
        "rating_validation": ("validation", rating_evaluation["validation"]),
        "rating_test": ("test", rating_evaluation["test"]),
        "keyword_validation": ("validation", keyword_evaluation["validation"]),
        "keyword_test": ("test", keyword_evaluation["test"]),
        "dummy_validation": ("validation", dummy_evaluation["evaluations"]["validation"]),
        "dummy_test": ("test", dummy_evaluation["evaluations"]["test"]),
        "tfidf_validation": ("validation", tfidf_evaluation["evaluations"]["validation"]),
        "tfidf_test": ("test", tfidf_evaluation["evaluations"]["test"]),
    })
    w5b.write_csv(
        report_dir / "confusion_matrices.csv", confusion,
        ["model", "scope", "true_label", "predicted_label", "count"],
    )
    w5b.write_csv(
        report_dir / "model_top_features.csv", top_features.to_dict(orient="records"),
        ["direction", "rank", "ngram", "coefficient"],
    )
    w5b.write_json(report_dir / "model_error_summary.json", model_results["error_summary"])
    (report_dir / "sampling_bias_notes.md").write_text(
        """# W5-C-B Sampling Limitations\n\nThe 1,500-review annotation set is deliberately stratified. The additional 1,200 reviews oversample model uncertainty, rating-keyword disagreement, device classes, rating strata, and time strata. Consequently, the annotated failure share is not an estimate of failure prevalence in the 55,877-review corpus. Model metrics describe performance on chronological slices of this annotated sample. Smart plugs are the primary analysis, smart bulbs exploratory, and smart switches a small-sample case study.\n""",
        encoding="utf-8",
    )

    protected_after = identity_map(root, protected_paths(root, config))
    protected_unchanged = protected_before == protected_after
    if not protected_unchanged:
        raise W5CBError("A protected W5-B/W4R baseline or annotation input changed")
    final_free = w5b.disk_free_gib(root)
    if final_free < 60:
        raise W5CBError(f"PAUSED_SPACE_GATE: final free space {final_free:.3f} GiB")
    insufficient = any(
        row["support_status"] == "INSUFFICIENT_SUPPORT"
        for row in tfidf_evaluation["test_by_device_type_exploratory"]
    )
    overfit = tfidf_evaluation["overfitting_diagnostic"]["status"] != "NO_LARGE_F1_GAP_DETECTED"
    readiness = "REVIEW_REQUIRED" if insufficient or overfit else "READY_FOR_EXPLICIT_APPROVAL"
    summary = summary_markdown(
        label_summary, agreement, split_manifest, rating_evaluation,
        keyword_evaluation, dummy_evaluation, tfidf_evaluation,
        comparison, readiness,
    )
    (report_dir / "w5c_b_summary.md").write_text(summary, encoding="utf-8")
    disk = {
        "initial_free_gib": initial_free,
        "final_free_gib": final_free,
        "used_by_phase_gib": initial_free - final_free,
        "minimum_required_free_gib": 60,
        "space_gate_passed": final_free >= 60,
        "process_peak_working_set_bytes": w5b.process_peak_working_set_bytes(),
    }
    w5b.write_json(report_dir / "w5c_b_disk_usage.json", disk)

    required = [
        "w5c_b_execution.log", "w5c_b_input_manifest.json",
        "expanded_label_summary.json", "expanded_label_counts.csv",
        "inter_annotator_agreement_300.json", "inter_annotator_agreement_300.md",
        "time_split_manifest.json", "time_split_counts.csv",
        "rating_baseline_evaluation.json", "keyword_baseline_evaluation.json",
        "dummy_baseline_evaluation.json", "tfidf_logistic_evaluation.json",
        "w5b_vs_w5c_b_comparison.csv", "confusion_matrices.csv",
        "model_top_features.csv", "model_error_summary.json",
        "sampling_bias_notes.md", "w5c_b_summary.md", "w5c_b_disk_usage.json",
    ]
    missing = [name for name in required if not (report_dir / name).is_file()]
    if missing:
        raise W5CBError(f"Missing required reports: {missing}")
    forbidden_tokens = ("user_id_hash", "raw_user_id", "original_user_id")
    for name in required:
        text = (report_dir / name).read_text(
            encoding="utf-8-sig", errors="replace"
        ).lower()
        if any(token in text for token in forbidden_tokens):
            raise W5CBError(f"Private identifier token found in report {name}")

    status = {
        "phase": PHASE,
        "status": "PASS",
        "w6_readiness": readiness,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - started_clock,
        "environment": environment_payload(),
        "input_validation": return_validation,
        "merge_validation": merge_validation,
        "label_summary": label_summary,
        "agreement_completed_for_independent_rows": 300,
        "chronological_split": split_manifest,
        "baselines_completed": [
            "B0 Rating", "B3 Keyword/rule draft",
            "DummyClassifier most_frequent", "TF-IDF + Logistic Regression",
        ],
        "tfidf_fit_on_train_only": True,
        "tfidf_overfitting_diagnostic": tfidf_evaluation["overfitting_diagnostic"],
        "model_forbidden_features_used": [],
        "protected_inputs_unchanged": protected_unchanged,
        "protected_files_after": protected_after,
        "raw_jsonl_read": False,
        "compressed_sources_read": False,
        "full_55877_reviews_scored": False,
        "model_predictions_used_as_labels": False,
        "product_month_failure_signals_created": False,
        "future_quality_target_created": False,
        "product_level_temporal_persistence_created": False,
        "w6_executed": False,
        "git_commit_created": False,
        "final_free_gib": final_free,
        "outputs": {
            "processed_labels": w5b.parquet_identity(root, label_path),
            "modeling_dataset": w5b.parquet_identity(root, modeling_path),
            "baseline_predictions": w5b.parquet_identity(root, predictions_path),
            "error_analysis_private": w5b.parquet_identity(root, error_path),
            "model": w5b.file_identity(root, model_path),
        },
        "required_reports": [w5b.relative(root, report_dir / name) for name in required],
        "report_row_level_text_or_identifier_leakage": False,
        "limitations": [
            "The 1,500-review sample is stratified and not population-representative.",
            "Performance estimates apply to annotated chronological slices, not Amazon overall.",
            "Smart plugs are primary, smart bulbs exploratory, and smart switches a case study.",
        ],
    }
    w5b.write_json(report_dir / "w5c_b_status.json", status)
    w5b.log_message(log_path, f"W5-C-B PASS; w6_readiness={readiness}")
    return 0


def write_failure_status(error: Exception, status_name: str) -> None:
    try:
        root = project_root()
        report_dir = root / "data/amazon_reviews_2023/reports/w5c_b"
        report_dir.mkdir(parents=True, exist_ok=True)
        w5b.write_json(report_dir / "w5c_b_status.json", {
            "phase": PHASE,
            "status": status_name,
            "w6_readiness": "REVIEW_REQUIRED",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "w6_executed": False,
        })
    except Exception:
        pass


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        return run()
    except LabelReviewRequired as exc:
        write_failure_status(exc, "PAUSED_LABEL_REVIEW")
        print(f"[PAUSED_LABEL_REVIEW] {exc}", file=sys.stderr)
        return 2
    except w5b.SplitReviewRequired as exc:
        write_failure_status(exc, "PAUSED_SPLIT_REVIEW")
        print(f"[PAUSED_SPLIT_REVIEW] {exc}", file=sys.stderr)
        return 3
    except InputMismatch as exc:
        write_failure_status(exc, "FAILED_INPUT_MISMATCH")
        print(f"[FAILED_INPUT_MISMATCH] {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        status = "PAUSED_SPACE_GATE" if "PAUSED_SPACE_GATE" in str(exc) else "FAILED_BASELINE_PREPARATION"
        write_failure_status(exc, status)
        print(f"[{status}] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
