"""Focused tests for the frozen W6-C formulas, calendar windows, and leakage rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_w6c_engineering_targets.py"
SPEC = importlib.util.spec_from_file_location("w6c", SCRIPT)
assert SPEC and SPEC.loader
w6c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w6c)


def rules() -> dict:
    return w6c.load_toml(ROOT / "config" / "w6c_engineering_target_rules.toml")


def component_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "duplicate_key": ["a", "b"],
            "parent_asin": ["P1", "P1"],
            "device_type": ["smart_plug", "smart_plug"],
            "source_domain": ["Electronics", "Electronics"],
            "review_datetime": pd.to_datetime(
                ["2020-01-01T00:00:00Z", "2020-02-01T00:00:00Z"]
            ),
            "review_month": [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-02-01")],
            "analysis_role": ["primary", "primary"],
            "failure_probability": [0.8, 0.2],
            "severity_probability_ge2_given_failure": [0.6, 0.4],
            "severity_probability_ge3_given_failure": [0.2, 0.9],
            "expected_persistence_given_failure": [1.0, 0.0],
            "failure_model_version": ["f", "f"],
            "severity_model_version": ["s", "s"],
            "persistence_model_version": ["p", "p"],
            "sentiment_model_version": ["v", "v"],
            "product_filter_version": ["w3", "w3"],
        }
    )


def observed_rating_frame() -> pd.DataFrame:
    # February is deliberately absent: March history must still be Jan+Feb+Mar,
    # and March h=1 must use April rather than the next observed row in June.
    return pd.DataFrame(
        {
            "parent_asin": ["P1", "P1", "P1", "P1"],
            "review_month": pd.PeriodIndex(
                ["2020-01", "2020-03", "2020-04", "2020-06"], freq="M"
            ),
            "device_type": ["smart_plug"] * 4,
            "n_reviews": [5, 5, 10, 2],
            "rating_sum": [25.0, 15.0, 20.0, 10.0],
            "low_star_count": [0, 1, 6, 0],
            "mean_rating": [5.0, 3.0, 2.0, 5.0],
            "low_star_share": [0.0, 0.2, 0.6, 0.0],
        }
    )


def test_project_root_and_config_paths_are_portable() -> None:
    assert w6c.project_root() == ROOT
    config = rules()
    for value in [*config["inputs"].values(), *config["outputs"].values()]:
        if isinstance(value, str) and ("/" in value or "\\" in value):
            assert not Path(value).is_absolute()


def test_engineering_index_formulas_and_bounds() -> None:
    result = w6c.compute_engineering_indices(component_frame(), rules())
    expected_main = 0.8 * (0.50 + 0.25 * 0.6 + 0.25 * 0.5)
    expected_equal = 0.8 * ((1 + 0.6 + 0.5) / 3)
    expected_emphasis = 0.8 * (0.60 + 0.20 * 0.6 + 0.20 * 0.5)
    expected_full = 0.8 * (0.50 + 0.25 * ((0.6 + 0.2) / 2) + 0.25 * 0.5)
    assert np.isclose(result.loc[0, "engineering_index_main"], expected_main)
    assert np.isclose(result.loc[0, "engineering_index_failure_only"], 0.8)
    assert np.isclose(result.loc[0, "engineering_index_equal_weight"], expected_equal)
    assert np.isclose(
        result.loc[0, "engineering_index_failure_emphasis"], expected_emphasis
    )
    assert np.isclose(
        result.loc[0, "engineering_index_full_severity_exploratory"], expected_full
    )
    index_columns = [column for column in result if column.startswith("engineering_index_")]
    numeric = [column for column in index_columns if column != "engineering_index_version"]
    assert (result[numeric].to_numpy() >= 0).all()
    assert (result[numeric].to_numpy() <= 1).all()


def test_main_engineering_index_ignores_severity_three() -> None:
    first = component_frame()
    second = first.copy()
    second["severity_probability_ge3_given_failure"] = [1.0, 0.0]
    a = w6c.compute_engineering_indices(first, rules())
    b = w6c.compute_engineering_indices(second, rules())
    assert np.allclose(a["engineering_index_main"], b["engineering_index_main"])
    assert not np.allclose(
        a["engineering_index_full_severity_exploratory"],
        b["engineering_index_full_severity_exploratory"],
    )


def test_rating_low_star_definition_and_weighted_aggregation() -> None:
    source = pd.DataFrame(
        {
            "duplicate_key": ["a", "b", "c"],
            "parent_asin": ["P", "P", "P"],
            "device_type": ["smart_plug"] * 3,
            "review_month": [pd.Timestamp("2020-01-01")] * 3,
            "rating": [1.0, 2.0, 5.0],
        }
    )
    result = w6c.aggregate_rating_product_month(source).iloc[0]
    assert result["n_reviews"] == 3
    assert result["low_star_count"] == 2
    assert np.isclose(result["low_star_share"], 2 / 3)
    assert np.isclose(result["mean_rating"], 8 / 3)


def test_calendar_windows_use_actual_months_and_weighted_review_sums() -> None:
    grid, audit = w6c.complete_calendar_windows(
        observed_rating_frame(), {"smart_plug": "primary"}, [1, 2, 3], 3
    )
    march = grid.loc[
        (grid["parent_asin"] == "P1") & (grid["review_month"] == pd.Period("2020-03"))
    ].iloc[0]
    assert march["historical_n_reviews"] == 10  # Jan + missing Feb + Mar
    assert np.isclose(march["historical_rating_mean"], 4.0)  # (25 + 15) / 10
    assert np.isclose(march["historical_low_star_share"], 0.1)
    assert march["future_n_reviews_h1"] == 10  # April only, not June
    assert np.isclose(march["future_rating_mean_h1"], 2.0)
    assert march["future_n_reviews_h2"] == 10  # April + empty May
    assert march["future_n_reviews_h3"] == 12  # April + May + June
    assert audit["uses_next_observed_row"] is False
    february = grid.loc[grid["review_month"] == pd.Period("2020-02")].iloc[0]
    assert february["origin_has_reviews"] == False  # noqa: E712
    assert february["n_reviews"] == 0
    assert pd.isna(february["mean_rating"])


def test_targets_thresholds_or_rule_and_support_flags() -> None:
    grid, _ = w6c.complete_calendar_windows(
        observed_rating_frame(), {"smart_plug": "primary"}, [1, 2, 3], 3
    )
    result = w6c.construct_targets(grid, rules())
    march = result.loc[pd.to_datetime(result["review_month"]).dt.month == 3].iloc[0]
    assert march["rating_deterioration_h1"] == 1
    assert march["low_star_deterioration_h1"] == 1
    assert march["quality_deterioration_h1"] == 1
    assert bool(march["support_main_counts_h1"])
    assert bool(march["eligible_main_h1"])
    assert bool(march["eligible_current_ge10_h1"]) is False
    assert "quality_deterioration_h1_r30_l10" in result.columns


def test_case_study_rows_are_retained_but_not_main_eligible() -> None:
    observed = observed_rating_frame()
    observed["device_type"] = "smart_switch"
    grid, _ = w6c.complete_calendar_windows(
        observed, {"smart_switch": "case_study"}, [1, 2, 3], 3
    )
    result = w6c.construct_targets(grid, rules())
    assert len(result) == 4
    assert result["support_main_counts_h1"].any()
    assert not result["eligible_main_h1"].any()


def test_proposed_split_is_chronological_with_three_month_embargo() -> None:
    months = pd.period_range("2010-01", periods=60, freq="M")
    frame = pd.DataFrame(
        {
            "parent_asin": [f"P{i % 4}" for i in range(60)],
            "review_month": months.to_timestamp().date,
            "device_type": ["smart_plug"] * 60,
            "analysis_role": ["primary"] * 60,
            "eligible_main_h3": [True] * 60,
            "quality_deterioration_h3": pd.Series([i % 2 for i in range(60)], dtype="Int8"),
        }
    )
    split, manifest, counts = w6c.assign_proposed_split(frame, rules())
    month = pd.to_datetime(split["review_month"]).dt.to_period("M")
    ranges = {
        name: month[split["proposed_split_h3"] == name]
        for name in ["train", "validation", "test"]
    }
    assert ranges["train"].max() + 3 < ranges["validation"].min()
    assert ranges["validation"].max() + 3 < ranges["test"].min()
    assert manifest["calendar_month_overlap_counts"] == {
        "train_validation": 0,
        "train_test": 0,
        "validation_test": 0,
    }
    assert manifest["random_shuffle"] is False
    assert all(row["negative"] and row["positive"] for row in counts if row["split"] in {"train", "validation", "test"})


def test_route_contract_has_three_fixed_core_routes_and_rating_reference() -> None:
    contract = w6c.route_contract()
    assert [route["route"] for route in contract["core_routes"]] == [
        "text_only",
        "text_plus_sentiment",
        "text_plus_engineering",
    ]
    assert contract["optional_reference"]["route"] == "rating_only"
    assert contract["optional_reference"]["is_core"] is False
    text_only = contract["core_routes"][0]
    assert text_only["added_numeric_features"] == []
    assert contract["models_trained_in_w6c"] is False


def test_analysis_panel_separates_features_targets_and_management() -> None:
    pm = pd.DataFrame(
        {
            "parent_asin": ["P"],
            "review_month": [pd.Timestamp("2020-03-01").date()],
            "device_type": ["smart_plug"],
            "analysis_role": ["primary"],
            "n_reviews": [5],
            "mean_rating": [4.0],
            "low_star_share": [0.1],
            "predicted_failure_share": [0.2],
            "mean_failure_probability": [0.3],
            "mean_expected_severity_signal": [0.4],
            "mean_expected_persistence_signal": [0.2],
            "mean_sentiment_compound": [0.5],
            "negative_sentiment_share": [0.1],
            "mean_engineering_index_main": [0.25],
            "mean_engineering_index_failure_only": [0.3],
            "mean_engineering_index_equal_weight": [0.24],
            "mean_engineering_index_failure_emphasis": [0.27],
            "mean_engineering_index_full_severity_exploratory": [0.23],
        }
    )
    targets = pm[["parent_asin", "review_month", "device_type", "analysis_role"]].copy()
    targets["historical_n_reviews"] = 12
    targets["historical_rating_mean"] = 4.2
    targets["historical_low_star_share"] = 0.05
    targets["future_n_reviews_h3"] = 10
    targets["future_rating_mean_h3"] = 3.8
    targets["future_low_star_share_h3"] = 0.2
    targets["quality_deterioration_h3"] = pd.Series([1], dtype="Int8")
    targets["eligible_main_h3"] = True
    targets["target_definite_h3"] = True
    targets["proposed_split_h3"] = "train"
    targets["split_version"] = "split"
    targets["target_version"] = "target"
    targets["historical_window_complete"] = True
    panel, contract = w6c.build_analysis_panel(pm, targets)
    assert len(panel) == 1
    assert all(column.startswith("feature_") for column in contract["feature_columns"])
    assert all(column.startswith("target_") for column in contract["target_columns"])
    assert not any(
        "future" in column or "deterioration" in column
        for column in contract["feature_columns"]
    )
    assert set(contract["feature_columns"]).isdisjoint(contract["target_columns"])
