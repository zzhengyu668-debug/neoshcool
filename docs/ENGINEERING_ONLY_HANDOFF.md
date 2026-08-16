# Engineering-only model handoff

> **Current scope note (2026-08-16):** This file preserves the earlier pure Engineering-only pilot contract. The current shared paper experiment is the controlled M0-M3 nested comparison. New development must start with [`NESTED_MODEL_COMPARISON_HANDOFF.md`](NESTED_MODEL_COMPARISON_HANDOFF.md). Pure Engineering-only results are supplemental and must not be substituted for M2 Rating + Engineering or M3 Rating + Sentiment + Engineering.

## Purpose

The Engineering-only model tests whether frozen engineering-failure signals can predict consumer-rating-based operational quality deterioration over the next three calendar months. The existing `Text + Engineering` route is not an Engineering-only model because it also uses review-text TF-IDF features.

## Frozen sample and target

- Unit: `parent_asin × review_month`.
- Main target: `target_quality_deterioration_h3`.
- Eligibility: `eligible_main_h3 == true`.
- Eligible rows: 515.
- Train: 205 rows.
- Validation: 150 rows.
- Embargo: 28 and 17 rows at the two temporal boundaries.
- Test: 115 rows.
- Split field: `proposed_split_h3`.
- Split type: chronological calendar-month split; never randomize it.

The target equals one when the frozen future three-month window has either a mean-rating decline of at least 0.3 or a low-star-share increase of at least 0.10 relative to the frozen historical baseline. Do not change this definition.

## Main Engineering-only features

Use only these three fields in the primary model:

1. `feature_mean_engineering_index_main`
2. `feature_predicted_failure_share`
3. `feature_mean_failure_probability`

The following may be evaluated only as separately labelled, predeclared ablations:

- `feature_mean_expected_severity_signal`
- `feature_mean_expected_persistence_signal`

Do not silently add the ablation fields to the primary result.

## Forbidden features

The Engineering-only model must not use:

- `review_text` or any TF-IDF representation;
- Rating or low-star features;
- VADER or any Sentiment feature;
- `parent_asin`, `device_type`, product title, description, or source domain;
- review month as a numerical shortcut;
- future rating, future low-star share, or any field prefixed with `target_` other than the outcome passed separately to evaluation;
- any Test result for feature selection, imputation, scaling, parameter selection, or threshold selection.

## Frozen baseline estimator

```python
from sklearn.linear_model import LogisticRegression

model = LogisticRegression(
    C=1.0,
    penalty="l2",
    class_weight="balanced",
    max_iter=1000,
    random_state=20260731,
)
decision_threshold = 0.5
```

Fit missing-value handling and standardization on Train only. Apply the frozen transformations to Validation and, only after approval, Test.

## Development sequence

1. Verify every file hash with `scripts/verify_collaboration_package.py`.
2. Select the 515 rows where `eligible_main_h3` is true.
3. Confirm split counts are exactly 205/28/150/17/115.
4. Separate the target from the feature matrix before preprocessing.
5. Fit preprocessing and Logistic Regression on Train only.
6. Evaluate and debug on Validation.
7. Freeze the feature list, code hash, model settings, threshold, and environment.
8. Compare the independent Yuchen Shen and Keyu Xu implementations.
9. Request group approval for one-time Test evaluation.
10. Preserve all outcomes, including no improvement or worse performance.

## Required metrics

- Confusion matrix
- Accuracy
- Balanced Accuracy
- Precision
- Recall
- F1
- Specificity
- ROC-AUC
- PR-AUC
- Brier Score
- Calibration curve
- Expected Calibration Error

The primary ranking metrics are PR-AUC, Brier Score, Recall, and F1, in that order. Accuracy must not be interpreted alone because the target classes are imbalanced.

## Required deliverables from each implementation

- source code and configuration;
- Python and package versions;
- input file SHA-256 values;
- exact feature list;
- Train/Validation/Test row counts;
- frozen model artifact;
- Validation predictions and metrics;
- one-time Test predictions and metrics only after approval;
- confusion matrices and calibration output;
- leakage audit;
- an objective note describing improvements, no improvements, and degradations.
