# Beyond Ratings and Sentiment

This repository supports a smart-home product early-warning study using a cleaned subset of Amazon Reviews 2023. It tests whether rating, general sentiment, and AI-detected engineering-failure signals can anticipate consumer-rating-based operational quality deterioration.

## Current project status

- Formal product set: 125 products (95 smart plugs, 25 smart bulbs, and 5 smart switches).
- Cleaned review corpus: 55,877 English, deduplicated reviews.
- Human annotation: 1,500 reviews, including 300 independently double-reviewed cases.
- Observed product-months: 1,911.
- Main three-month eligible sample: 515 product-months.
- Frozen split: 205 Train, 150 Validation, 45 Embargo, and 115 Test rows.
- The pure Rating-only, Sentiment-only, and Engineering-only pilots have been completed as supplemental signal-only analyses.
- The current shared development task is the controlled nested M0-M3 comparison described below.
- Test remains off-limits for new development until the four routes, feature contract, code, and threshold are frozen and the group explicitly approves one-time evaluation.

`Text + Engineering` is a frozen supplemental route and is **not** an Engineering-only model.

## Three primary analysis models

| Model | Permitted main signals | Current status |
|---|---|---|
| Rating analysis model | Rating, low-star share, and review volume | Pure pilot complete; M0 baseline in current comparison |
| Sentiment analysis model | Frozen VADER sentiment aggregates | Pure pilot complete; incremental M1 route pending |
| Engineering fault analysis model | Frozen Failure, Severity, Persistence, and EngineeringIndex aggregates | Pure pilots complete; incremental M2/M3 routes pending |

The earlier Text-only, Text + Sentiment, and Text + Engineering experiments remain frozen supplemental analyses and are not the current paper comparison.

## Current controlled comparison: M0-M3

The future-quality target is constructed from later consumer-rating deterioration. Rating is therefore held as the common base, and the new experiment asks whether Sentiment or Engineering adds information beyond that base:

| Route | Frozen feature groups | Main question |
|---|---|---|
| M0 Rating-only | Rating | Common reference |
| M1 Rating + Sentiment | Rating + frozen VADER aggregates | Does Sentiment add value beyond Rating? |
| M2 Rating + Engineering | Rating + frozen engineering aggregates | Does Engineering add value beyond Rating? |
| M3 Rating + Sentiment + Engineering | Rating + Sentiment + Engineering | Does Engineering add value beyond Rating and Sentiment? |

The title-facing comparison is M3 minus M1. M2 minus M0 is a complementary ablation. All routes use the same 515 eligible product-months, chronological split, Train-fitted preprocessing, balanced Logistic Regression, 0.5 threshold, and metric implementation. Read [`docs/NESTED_MODEL_COMPARISON_HANDOFF.md`](docs/NESTED_MODEL_COMPARISON_HANDOFF.md) before development.

## Published collaboration data

The project owner explicitly approved public release of the cleaned research subset on 8 August 2026. The repository includes the formal 55,877-row Parquet unchanged for reproducibility, including the pseudonymous `user_id_hash`; it does not include the source `user_id`. An automated pattern audit found four email-shaped and five phone-shaped matches across eight review rows. These strings remain part of the public user-generated review text and were not rewritten. The release excludes all raw JSONL/GZ archives, private annotation mappings, completed annotation workbooks, chats, transcripts, and private error-analysis files.

## Clone and create the Windows environment

Install Git and 64-bit Python 3.11, then run the following commands in PowerShell:

```powershell
git clone https://github.com/zzhengyu668-debug/neoshcool.git
Set-Location .\neoshcool
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-collaboration.txt
```

If PowerShell blocks activation, the environment can still be used directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-collaboration.txt
```

## Verify the collaboration package

After cloning the repository and installing the frozen dependencies, run:

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_collaboration_package.py
```

The verifier checks Python and dependency versions, file hashes, Parquet row counts, model loading, product-month keys, eligibility counts, frozen splits, and publication exclusions.

## Read the cleaned review data

```python
import pandas as pd

reviews = pd.read_parquet(
    "data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet"
)
print(reviews.shape)
print(reviews[["parent_asin", "device_type", "review_month", "rating"]].head())
```

The formal published file contains a pseudonymous `user_id_hash` but no source `user_id`. Its approved SHA-256 and complete schema are recorded in `collaboration/package_manifest.json` and `collaboration/data_dictionary.md`.

## Load a trusted frozen model

`joblib` files can execute code when loaded. Load only the hashes listed in `collaboration/model_manifest.json` and only from this trusted project source.

```python
import joblib

failure_model = joblib.load(
    "outputs/models/w5c_b_tfidf_logistic_regression.joblib"
)
print(failure_model["decision_threshold"])
```

## Nested comparison handoff

For the current shared task, follow [`docs/NESTED_MODEL_COMPARISON_HANDOFF.md`](docs/NESTED_MODEL_COMPARISON_HANDOFF.md). After package verification, a collaborator can run all four Train/Validation routes with:

```powershell
.\.venv\Scripts\python.exe .\scripts\run_nested_model_comparison_development.py --executor your_name
.\.venv\Scripts\python.exe -m pytest .\tests\test_nested_model_comparison_development.py -q
```

Outputs are written under `outputs/nested_model_comparison/<executor>/development/`. The script materializes targets only for Train and Validation. It audits the 115 Test keys without reading their target values or generating Test predictions.

## Pure Engineering-only reference handoff

The earlier pure Engineering-only task remains documented in [`docs/ENGINEERING_ONLY_HANDOFF.md`](docs/ENGINEERING_ONLY_HANDOFF.md). Its main inputs are:

- `feature_mean_engineering_index_main`
- `feature_predicted_failure_share`
- `feature_mean_failure_probability`

Those pure-model results are supplemental and do not replace M2 or M3. Do not use review text, TF-IDF, product identity, device type, or future target/audit fields. Fit imputation and scaling on Train only. Use Validation for development. Test may be evaluated only once after the feature contract, code, model settings, and threshold are frozen.

## Project layout

```text
config/                         Frozen research and model settings
scripts/                        Reproducible processing and verification scripts
tests/                          Automated leakage and reproducibility tests
data/amazon_reviews_2023/       Local data and aggregate reports
outputs/models/                 Trusted frozen model artifacts
docs/                           Collaboration handoff documentation
collaboration/                  Package manifests, audits, and release whitelist
```

Raw-data extraction code is retained for provenance but is not needed for Engineering-only development. W5-C-B through W6-D scripts reproduce the cleaned-data modeling pipeline. W7-B0 and W7-B1 are exploratory and do not replace the frozen monthly main experiment.

## Run tests

Run the collaboration verifier first, followed by the public-package reproducibility tests:

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_collaboration_package.py
.\.venv\Scripts\python.exe -m pytest `
  .\tests\test_w7c0_collaboration_package.py `
  .\tests\test_w6a_full_failure_inference.py `
  .\tests\test_w6b_signal_components.py `
  .\tests\test_w6c_engineering_targets.py `
  .\tests\test_w6d_controlled_warning_comparison.py `
  .\tests\test_nested_model_comparison_development.py -q
```

Some historical W4/W5 tests require private annotation workbooks and interim mappings that are intentionally excluded from the public collaboration package. Running the entire `tests/` directory after a public clone will therefore report missing private inputs; use the command above for the published package.

## Common problems

- **Wrong Python version:** use 64-bit Python 3.11.
- **Missing Parquet file:** place approved files at the exact relative paths in the package manifest.
- **SHA-256 mismatch:** stop; do not train with an unidentified file.
- **`joblib`/scikit-learn mismatch:** install `requirements-collaboration.txt` exactly.
- **Hard-coded local path:** use repository-relative paths only.
- **Different sample count:** the main eligible h=3 sample must contain 515 rows before embargo separation.
- **Test leakage:** never use Test labels to select features, parameters, or the decision threshold.

## Data provenance and limitations

See [`DATA_PROVENANCE.md`](DATA_PROVENANCE.md). The quality target is an operational definition based on future consumer ratings, not repair, return, telemetry, or verified hardware-failure truth. Smart plug is the primary analysis; smart bulb is exploratory; smart switch remains a case study.

## Team responsibilities

- **Zhengyu Zhou:** shared data/model/evaluation framework, M0 and M1, leakage audit, result integration, and paper reporting.
- **Assigned Engineering contributor:** M2 and M3 implementation review, Engineering ablations, cross-machine reproduction, and a pull request from an independent branch.

The two collaborators cross-review feature hashes, Validation rows, model settings, and outputs. Engineering improvement is a hypothesis, not an acceptance criterion; improvements, null results, and degradations must all be retained.

See `PROJECT_HANDOFF.md` for the frozen historical decisions. Do not change targets, split boundaries, EngineeringIndex weights, or upstream models without explicit group approval.
