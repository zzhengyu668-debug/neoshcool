# W5-B Summary

## Outcome

- Technical status: **PASS**
- W6 readiness: **REVIEW_REQUIRED**
- Frozen labels: 300 total, 296 definite, 4 uncertain.
- Definite labels: 153 engineering failures and 143 non-failures.
- The 300-review annotation set is stratified for boundary coverage and is not a population-representative prevalence sample.

## Independent annotation agreement

- Failure binary agreement including `uncertain`: 0.9333; Cohen's kappa 0.8727.
- Failure type exact match: 0.7333; mean Jaccard 0.8403.
- Severity linear/quadratic weighted kappa: 0.9159 / 0.9571.
- Persistence linear/quadratic weighted kappa: 0.6294 / 0.5653.

## Chronological split

| Split | N | Non-failure | Failure | Earliest UTC | Latest UTC |
|---|---:|---:|---:|---|---|
| train | 177 | 83 | 94 | 2011-03-13T12:29:41+00:00 | 2020-11-17T21:19:54.984000+00:00 |
| validation | 59 | 34 | 25 | 2020-11-21T05:19:14.783000+00:00 | 2022-02-25T19:53:03.587000+00:00 |
| test | 60 | 26 | 34 | 2022-02-26T17:21:44.747000+00:00 | 2023-05-09T19:56:35.045000+00:00 |

No review was randomly moved across time boundaries, and TF-IDF was fit only
on the training split.

## Test-set baseline comparison

| Baseline | Balanced accuracy | Precision | Recall | F1 | Specificity | ROC-AUC | PR-AUC |
|---|---:|---:|---:|---:|---:|---:|---:|
| B0 low-star (`rating <= 2`) | 0.7998 | 0.9200 | 0.6765 | 0.7797 | 0.9231 | n/a | n/a |
| B3 keyword/rule draft | 0.7568 | 0.8276 | 0.7059 | 0.7619 | 0.8077 | n/a | n/a |
| Dummy most frequent | 0.5000 | 0.5667 | 1.0000 | 0.7234 | 0.0000 | 0.5000 | 0.5667 |
| TF-IDF + Logistic Regression | 0.8643 | 0.8824 | 0.8824 | 0.8824 | 0.8462 | 0.8857 | 0.8977 |

TF-IDF validation F1 is 0.7636; test F1 is
0.8824. The model removed a leading standalone star-title
header from 12 private model-text rows without altering the
formal review text.

Overfitting diagnostic:
`POTENTIAL_OVERFITTING_TRAIN_VALIDATION_GAP`. Training F1 is
0.9947, creating a
train-minus-validation gap of
0.2310. The higher
test result does not remove this risk because both chronological evaluation
sets are small.

## Test support by device type

| Device type | Test N | Non-failure | Failure | Reporting status |
|---|---:|---:|---:|---|
| smart_plug | 39 | 16 | 23 | SUFFICIENT |
| smart_bulb | 13 | 7 | 6 | INSUFFICIENT_SUPPORT |
| smart_switch | 8 | 3 | 5 | INSUFFICIENT_SUPPORT |

Device-specific metrics are exploratory. A class is marked
`INSUFFICIENT_SUPPORT` when the test subset has fewer than 20 rows or either
binary class has fewer than five rows.

## Error analysis

- TF-IDF false positives on test: 4
- TF-IDF false negatives on test: 4
- Complete review text for errors is stored only in the private interim Parquet.
- Test errors were not used to change rules, labels, thresholds, or training.

## Scope limitations

No model was applied to the full 55,877 reviews. No product-month engineering
signal, future deterioration target, product-level temporal persistence, W6
output, raw-source scan, or Git commit was created. Smart plugs remain the
primary analysis; smart bulbs are exploratory and smart switches are a
small-sample case study.
