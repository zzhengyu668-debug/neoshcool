from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import joblib
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_nested_model_comparison_development.py"
SPEC = importlib.util.spec_from_file_location("nested_development", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.fixture(scope="session")
def config() -> dict:
    return MODULE.load_config(ROOT / "config" / "nested_model_comparison_rules.toml")


@pytest.fixture(scope="session")
def development_data(config: dict):
    return MODULE.load_development_data(config)


@pytest.fixture(scope="session")
def run_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("nested-comparison")
    return MODULE.run(
        ROOT / "config" / "nested_model_comparison_rules.toml",
        "pytest",
        output,
        25,
    )


def test_feature_contract_is_exact_and_nested(config: dict) -> None:
    contract = MODULE.validate_feature_contract(config)
    assert contract["passed"] is True
    routes = contract["routes"]
    assert list(routes) == [
        "m0_rating_only",
        "m1_rating_sentiment",
        "m2_rating_engineering",
        "m3_rating_sentiment_engineering",
    ]
    assert routes["m1_rating_sentiment"][: len(routes["m0_rating_only"])] == routes["m0_rating_only"]
    assert routes["m2_rating_engineering"][: len(routes["m0_rating_only"])] == routes["m0_rating_only"]
    assert set(routes["m3_rating_sentiment_engineering"]) == (
        set(routes["m1_rating_sentiment"]) | set(config["features"]["engineering"])
    )


def test_no_identity_future_or_target_feature(config: dict) -> None:
    for features in MODULE.route_features(config).values():
        assert "parent_asin" not in features
        assert "device_type" not in features
        assert "review_month" not in features
        assert "review_text" not in features
        assert not any(feature.startswith(("target_", "future_", "target_future_")) for feature in features)


def test_frozen_sample_and_split_counts(development_data) -> None:
    assert development_data.split_counts == {
        "train": 205,
        "embargo_train_validation": 28,
        "validation": 150,
        "embargo_validation_test": 17,
        "test": 115,
    }
    assert len(development_data.train) == 205
    assert len(development_data.validation) == 150
    assert development_data.test_key_count == 115


def test_frozen_development_class_counts(config: dict, development_data) -> None:
    target = config["input"]["target_field"]
    assert int(development_data.train[target].sum()) == 50
    assert int(development_data.validation[target].sum()) == 47


def test_common_pipeline_parameters(config: dict) -> None:
    pipeline = MODULE.build_pipeline(config)
    assert list(pipeline.named_steps) == ["imputer", "scaler", "logistic_regression"]
    assert pipeline.named_steps["imputer"].strategy == "median"
    assert pipeline.named_steps["imputer"].add_indicator is True
    model = pipeline.named_steps["logistic_regression"]
    assert model.C == 1.0
    assert model.penalty == "l2"
    assert model.solver == "liblinear"
    assert model.class_weight == "balanced"
    assert model.max_iter == 1000
    assert model.random_state == 20260731
    assert config["model"]["decision_threshold"] == 0.5


def test_development_run_outputs_all_routes_on_same_validation_rows(run_output: Path) -> None:
    predictions = pd.read_parquet(run_output / "validation_predictions.parquet")
    assert set(predictions["route"]) == {
        "m0_rating_only",
        "m1_rating_sentiment",
        "m2_rating_engineering",
        "m3_rating_sentiment_engineering",
    }
    assert len(predictions) == 4 * 150
    key_sets = {
        route: set(map(tuple, frame[["parent_asin", "review_month"]].to_numpy()))
        for route, frame in predictions.groupby("route")
    }
    assert len({frozenset(keys) for keys in key_sets.values()}) == 1
    assert set(predictions["split"]) == {"validation"}


def test_test_is_sealed_and_not_predicted(run_output: Path) -> None:
    audit = json.loads((run_output / "sealed_test_audit.json").read_text(encoding="utf-8"))
    assert audit["passed"] is True
    assert audit["test_key_count"] == 115
    assert audit["test_target_materialized"] is False
    assert audit["test_predictions_generated"] is False
    assert audit["test_metrics_computed"] is False
    predictions = pd.read_parquet(run_output / "validation_predictions.parquet")
    assert "test" not in set(predictions["split"])


def test_saved_models_preserve_route_features_and_train_scope(run_output: Path) -> None:
    manifest = json.loads((run_output / "model_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["models"]) == 4
    for item in manifest["models"]:
        model_path = run_output / "models" / f"{item['route']}_train_fitted.joblib"
        payload = joblib.load(model_path)
        assert payload["training_scope"] == "train_only"
        assert payload["decision_threshold"] == 0.5
        assert payload["features"] == item["features"]


def test_metric_arithmetic_audit_and_bootstrap_outputs(run_output: Path) -> None:
    metrics = json.loads((run_output / "validation_metrics.json").read_text(encoding="utf-8"))
    assert set(metrics) == {
        "m0_rating_only",
        "m1_rating_sentiment",
        "m2_rating_engineering",
        "m3_rating_sentiment_engineering",
    }
    assert all(route["manual_metric_max_abs_error"] <= 1e-12 for route in metrics.values())
    bootstrap = pd.read_csv(run_output / "validation_bootstrap_intervals.csv")
    assert set(bootstrap["metric"]) == {"pr_auc_average_precision", "brier_score", "recall", "f1"}
    assert bootstrap["valid_replicates"].min() > 0


def test_reports_do_not_contain_test_target_values(run_output: Path) -> None:
    for path in run_output.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "test_positive_count" not in text
        assert "test_negative_count" not in text
        assert "test_pr_auc" not in text
