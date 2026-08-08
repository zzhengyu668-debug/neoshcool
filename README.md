# Beyond Ratings and Sentiment

This repository supports a smart-home product early-warning study using a cleaned subset of Amazon Reviews 2023. It tests whether rating, general sentiment, and AI-detected engineering-failure signals can anticipate consumer-rating-based operational quality deterioration.

## Current project status

- Formal product set: 125 products (95 smart plugs, 25 smart bulbs, and 5 smart switches).
- Cleaned review corpus: 55,877 English, deduplicated reviews.
- Human annotation: 1,500 reviews, including 300 independently double-reviewed cases.
- Observed product-months: 1,911.
- Main three-month eligible sample: 515 product-months.
- Frozen split: 205 Train, 150 Validation, 45 Embargo, and 115 Test rows.
- Rating-only is complete.
- Sentiment-only is assigned to Zhengyu Zhou.
- Engineering-only has not been run and is assigned to Yuchen Shen and Keyu Xu for independent implementation and reproducibility checking.

`Text + Engineering` is a frozen supplemental route and is **not** an Engineering-only model.

## Three primary analysis models

| Model | Permitted main signals | Current status |
|---|---|---|
| Rating analysis model | Rating, low-star share, and review volume | Complete |
| Sentiment analysis model | Frozen VADER sentiment aggregates | Pending Sentiment-only run |
| Engineering fault analysis model | Frozen Failure, Severity, Persistence, and EngineeringIndex aggregates | Pending Engineering-only run |

The earlier Text-only, Text + Sentiment, and Text + Engineering experiments remain supplemental incremental-value analyses.

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

## Engineering-only handoff

Read [`docs/ENGINEERING_ONLY_HANDOFF.md`](docs/ENGINEERING_ONLY_HANDOFF.md) before implementing the model. The main Engineering-only inputs are:

- `feature_mean_engineering_index_main`
- `feature_predicted_failure_share`
- `feature_mean_failure_probability`

Do not use review text, TF-IDF, Rating, Sentiment, product identity, device type, or future target/audit fields. Fit imputation and scaling on Train only. Use Validation for development. Test may be evaluated only once after the feature contract, code, model settings, and threshold are frozen.

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

Run the collaboration checks first, followed by the project tests:

```powershell
.\.venv\Scripts\python.exe .\scripts\verify_collaboration_package.py
.\.venv\Scripts\python.exe -m pytest .\tests
```

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

- **Zhengyu Zhou:** Sentiment-only implementation, package coordination, and final result integration.
- **Yuchen Shen:** independent Engineering-only implementation A.
- **Keyu Xu:** independent Engineering-only implementation B and cross-machine reproducibility audit.

See `PROJECT_HANDOFF.md` for the frozen historical decisions. Do not change targets, split boundaries, EngineeringIndex weights, or upstream models without explicit group approval.
