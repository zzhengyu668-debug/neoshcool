from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]


def test_collaboration_requirements_are_pinned() -> None:
    lines = (ROOT / "requirements-collaboration.txt").read_text(encoding="utf-8").splitlines()
    assert lines
    assert all("==" in line for line in lines if line.strip())


def test_readme_has_no_local_user_path() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "C:\\Users\\" not in readme


def test_engineering_handoff_freezes_main_features() -> None:
    handoff = (ROOT / "docs" / "ENGINEERING_ONLY_HANDOFF.md").read_text(encoding="utf-8")
    for feature in (
        "feature_mean_engineering_index_main",
        "feature_predicted_failure_share",
        "feature_mean_failure_probability",
    ):
        assert feature in handoff
    assert "Text + Engineering" in handoff
    assert "not an Engineering-only model" in handoff


def test_formal_review_release_has_only_pseudonymous_user_identifier() -> None:
    review = ROOT / "data" / "amazon_reviews_2023" / "processed" / "review_level_base_w3_v1_4_0.parquet"
    fields = pq.ParquetFile(review).schema_arrow.names
    assert "user_id" not in fields
    assert "user_id_hash" in fields
    assert pq.ParquetFile(review).metadata.num_rows == 55_877


def test_explicit_release_approval_is_recorded() -> None:
    status = json.loads((ROOT / "collaboration" / "w7c0_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "READY_FOR_GITHUB_PUBLISH_APPROVAL"
    assert status["publication_allowlist_contains_data"] is True
    assert status["public_release_approved_by_project_owner"] is True


def test_publication_allowlist_includes_approved_data_and_excludes_forbidden_files() -> None:
    entries = [
        line.strip()
        for line in (ROOT / "collaboration" / "publication_allowlist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet" in entries
    assert "data/amazon_reviews_2023/processed/product_month_analysis_panel_w6c_v1_0.parquet" in entries
    assert not any(entry.lower().endswith((".gz", ".jsonl", ".xlsx", ".mp4")) for entry in entries)


def test_formal_analysis_panel_counts_remain_frozen() -> None:
    panel = pq.read_table(
        ROOT / "data" / "amazon_reviews_2023" / "processed" / "product_month_analysis_panel_w6c_v1_0.parquet",
        columns=["eligible_main_h3", "proposed_split_h3"],
    ).to_pydict()
    indices = [i for i, value in enumerate(panel["eligible_main_h3"]) if bool(value)]
    assert len(indices) == 515
    counts: dict[str, int] = {}
    for i in indices:
        split = str(panel["proposed_split_h3"][i])
        counts[split] = counts.get(split, 0) + 1
    assert counts == {
        "train": 205,
        "embargo_train_validation": 28,
        "validation": 150,
        "embargo_validation_test": 17,
        "test": 115,
    }
