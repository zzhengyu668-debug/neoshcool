# Smart-Home Engineering-Failure Early Warning

This project studies whether engineering-failure signals extracted from Amazon Reviews 2023 can identify quality deterioration in smart plugs, smart bulbs, and smart switches earlier than ratings and general sentiment.

## Current Windows location

The current project root is:

```text
C:\Users\30649\Desktop\neoschool
```

All project code resolves paths from the project root and `config/project.toml`. Production code must not depend on the current working directory.

## Python environment

Create and activate the local Python 3.11 environment from PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run the environment check:

```powershell
.\.venv\Scripts\python.exe .\scripts\check_environment.py
```

The script locates the project from its own file path, so it can also be invoked from another working directory.

## Current phase

Phases W0-W4R are complete. The expanded W5-C-A annotation and adjudication work is also complete, and the project is paused before the explicitly approved W5-C-B label freeze and baseline retraining step.

- Source inventory: 111,296,888 review records and 5,345,596 metadata records.
- Formal product set (`w3-v1.4.0`): 125 products (95 smart plugs, 25 smart bulbs, and 5 smart switches).
- Formal cleaned review corpus: 55,877 English, deduplicated reviews.
- Human annotation: 1,500 reviews completed, including 300 double-reviewed cases in total.
- A 300-review pilot has already run rating, keyword/rule, DummyClassifier, and TF-IDF + Logistic Regression baselines using a chronological split.
- The next approved implementation step must freeze the expanded labels and rerun the simple baselines before any W6 product-month early-warning analysis.

The confirmed source workflow remains: download the four gzip archives, fully decompress them to ordinary JSONL files, and process only the uncompressed JSONL files. Direct gzip scanning, `gzip.open`, and compressed streaming pipelines are prohibited.

## Repository data policy

This public repository intentionally excludes Amazon source data, processed Parquet files, model artifacts, private hashing material, and human-annotation workbooks containing review text. The tracked small reports contain aggregate counts, schemas, validation outcomes, and model metrics needed to audit the pipeline. Source data must be obtained separately under the Amazon Reviews 2023 dataset terms.

Run the available automated checks with:

```powershell
.\.venv\Scripts\python.exe -m pytest .\tests
```

See `PROJECT_HANDOFF.md` for the authoritative research and data-handling decisions.
