# PROJECT HANDOFF

## Amazon Reviews 2023 Smart-Home Failure Early-Warning Project

- **Project lead:** 周正宇
- **Migration:** macOS → Windows
- **Handoff date:** 2026-07-27
- **Current Windows project root:** `C:\Users\30649\Desktop\neoschool`
- **Data source:** Amazon Reviews 2023, Electronics and Home and Kitchen
- **Target products:** smart plugs, smart bulbs, and smart switches
- **Primary language of analysis:** English review text

---

## 1. Purpose of this file

This file gives a new Codex/ChatGPT task on Windows enough context to continue the project without relying on the original Mac Codex conversation.

The Windows task should read this file before editing code, processing data, or revising the paper. It should also inspect all files under `documents/` and treat the decisions marked **confirmed** below as the current project specification.

### Important processing decision

The original plan proposed reading the compressed `.jsonl.gz` files as streams. That plan has been superseded.

**Confirmed decision as of 2026-07-27:**

1. Download all four source files again on Windows.
2. Retain the downloaded `.jsonl.gz` files until integrity checks are complete.
3. Fully decompress the four files to ordinary `.jsonl` files.
4. Run all subsequent data processing against the uncompressed `.jsonl` files.
5. Do not use `gzip.open`, direct compressed-file scans, or a compressed streaming pipeline.

Out-of-core tools such as DuckDB or Polars lazy scans may still scan the **uncompressed** JSONL files without loading the entire dataset into RAM. This is allowed and is strongly recommended. “Fully decompressed processing” does not mean that a 50–100+ GB collection of JSON text should be loaded into memory at once.

---

## 2. Research objective

The project studies whether engineering-failure signals extracted from Amazon smart-home reviews can identify emerging product-quality problems earlier than conventional rating or sentiment indicators.

### Main research question

> Can AI-detected engineering-failure signals identify emerging quality deterioration in smart-home devices earlier and more accurately than average ratings, low-star shares, and general sentiment?

### Working hypotheses

- **H1 — Early-warning value:** The share of reviews reporting engineering failures rises before a product’s average rating shows a substantial decline.
- **H2 — Incremental information:** Failure type, severity, and persistence add predictive information beyond ordinary negative sentiment.
- **H3 — Reproducibility:** Metadata filtering, human annotation, and interpretable NLP baselines can form a stable and reproducible failure-detection pipeline.

### Units of analysis

- **Review level:** One row per review. Used for failure detection, failure type, severity, persistence, sentiment, and error analysis.
- **Product level:** One row per `parent_asin`. Used for product selection and product attributes.
- **Product-month level:** One row per `parent_asin × review_month`. Used for trend construction and early-warning evaluation.

---

## 3. TA requirements captured from the meeting

The TA asked the team to do the following:

1. State the data source precisely and establish its reliability.
2. Report exact sample sizes and the final time range after filtering.
3. Explain the original JSON/JSONL structure and available field types.
4. Clearly identify which raw fields are actually used by the method.
5. Use a table when the final field list is too long for prose.
6. Explain why the processed sample is better aligned with the research task than broad, unfiltered review datasets used in earlier work.
7. Include a descriptive-statistics table in the final report.
8. Define “engineering failure” explicitly rather than equating failure with a low rating.
9. Make the preprocessing and baseline pipeline run before writing the final Method section.
10. Begin with simple, inspectable baselines and add the proposed engineering-failure signals afterward.

The Data section may leave sample counts as placeholders only until preprocessing produces the real values. The final paper must replace every placeholder with an exact number.

---

## 4. Dataset background

The project uses the public **Amazon Reviews 2023** dataset released by the McAuley Lab.

- Full release: 571.54 million reviews.
- Users: 54.51 million.
- Items: 48.19 million.
- Product domains: 33.
- Full time coverage: May 1996 through September 2023.
- Electronics category: approximately 43.9 million ratings.
- Home and Kitchen category: approximately 67.4 million ratings.

Official documentation:

<https://amazon-reviews-2023.github.io/main.html>

Associated paper:

> Hou, Y., Li, J., He, Z., Yan, A., Chen, X., and McAuley, J. (2024). Bridging Language and Items for Retrieval and Recommendation. arXiv:2403.03952.

The project does **not** use the complete 33-domain database. Only Electronics and Home and Kitchen are required because the three target device classes are distributed across both category systems.

---

## 5. Windows data download specification

### 5.1 Required files

| Domain | Record type | Filename | Expected compressed bytes | Approximate displayed size |
|---|---|---|---:|---:|
| Electronics | Metadata | `meta_Electronics.jsonl.gz` | 1,312,900,427 | 1.2 GiB |
| Home and Kitchen | Metadata | `meta_Home_and_Kitchen.jsonl.gz` | 2,964,930,883 | 2.8 GiB |
| Electronics | Reviews | `Electronics.jsonl.gz` | 6,474,438,619 | 6.0 GiB |
| Home and Kitchen | Reviews | `Home_and_Kitchen.jsonl.gz` | 8,307,664,299 | 7.7 GiB |

Total compressed size is approximately 19.06 GB in decimal units, or 17.75 GiB.

### 5.2 Download URLs

```text
https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Electronics.jsonl.gz
https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Home_and_Kitchen.jsonl.gz
https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Electronics.jsonl.gz
https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Home_and_Kitchen.jsonl.gz
```

### 5.3 Recommended Windows directory layout

```text
C:\Users\30649\Desktop\neoschool\
├── PROJECT_HANDOFF.md
├── README.md
├── requirements.txt
├── config\
├── data\
│   └── amazon_reviews_2023\
│       ├── raw\
│       │   ├── compressed\
│       │   │   ├── meta_categories\
│       │   │   └── review_categories\
│       │   └── uncompressed\
│       │       ├── meta_categories\
│       │       └── review_categories\
│       ├── interim\
│       ├── processed\
│       └── reports\
├── documents\
├── notebooks\
├── outputs\
│   ├── figures\
│   ├── tables\
│   └── models\
├── src\
│   ├── data\
│   ├── features\
│   ├── annotation\
│   ├── baselines\
│   ├── models\
│   └── evaluation\
└── tests\
```

Use the Windows internal SSD rather than an external drive or a OneDrive-synchronized folder during processing.

### 5.4 Disk-space planning

The compressed files occupy about 19 GB. The decompressed JSONL files will be several times larger, and the workflow will also create Parquet tables, temporary DuckDB files, logs, models, and evaluation outputs.

Before decompression:

- **Minimum practical free space:** approximately 150 GB.
- **Recommended free space:** 200–250 GB.
- More space is preferable if both compressed and uncompressed copies will be retained throughout the project.

These are planning estimates, not official uncompressed file sizes. Record the actual sizes immediately after decompression.

### 5.5 PowerShell download commands

Run PowerShell in the Windows project directory. `curl.exe -C -` allows a partially downloaded file to resume.

```powershell
$ProjectRoot = "C:\Users\30649\Desktop\neoschool"
$CompressedRoot = "$ProjectRoot\data\amazon_reviews_2023\raw\compressed"

New-Item -ItemType Directory -Force "$CompressedRoot\meta_categories"
New-Item -ItemType Directory -Force "$CompressedRoot\review_categories"

curl.exe -L --retry 5 -C - `
  -o "$CompressedRoot\meta_categories\meta_Electronics.jsonl.gz" `
  "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Electronics.jsonl.gz"

curl.exe -L --retry 5 -C - `
  -o "$CompressedRoot\meta_categories\meta_Home_and_Kitchen.jsonl.gz" `
  "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Home_and_Kitchen.jsonl.gz"

curl.exe -L --retry 5 -C - `
  -o "$CompressedRoot\review_categories\Electronics.jsonl.gz" `
  "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Electronics.jsonl.gz"

curl.exe -L --retry 5 -C - `
  -o "$CompressedRoot\review_categories\Home_and_Kitchen.jsonl.gz" `
  "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Home_and_Kitchen.jsonl.gz"
```

### 5.6 Download verification

First compare file lengths with the expected-byte table:

```powershell
Get-ChildItem "$CompressedRoot" -Recurse -Filter "*.jsonl.gz" |
  Select-Object FullName, Length
```

Then generate a local SHA-256 manifest:

```powershell
Get-ChildItem "$CompressedRoot" -Recurse -Filter "*.jsonl.gz" |
  Get-FileHash -Algorithm SHA256 |
  Export-Csv "$ProjectRoot\data\amazon_reviews_2023\compressed_sha256.csv" -NoTypeInformation
```

The manifest records the exact files used by this project. Do not claim that these hashes are official publisher checksums unless the publisher provides matching values.

### 5.7 Full decompression with 7-Zip

Install 7-Zip, create the target directories, and test each gzip archive before extraction.

```powershell
$SevenZip = "C:\Program Files\7-Zip\7z.exe"
$UncompressedRoot = "$ProjectRoot\data\amazon_reviews_2023\raw\uncompressed"

New-Item -ItemType Directory -Force "$UncompressedRoot\meta_categories"
New-Item -ItemType Directory -Force "$UncompressedRoot\review_categories"

& $SevenZip t "$CompressedRoot\meta_categories\meta_Electronics.jsonl.gz"
& $SevenZip t "$CompressedRoot\meta_categories\meta_Home_and_Kitchen.jsonl.gz"
& $SevenZip t "$CompressedRoot\review_categories\Electronics.jsonl.gz"
& $SevenZip t "$CompressedRoot\review_categories\Home_and_Kitchen.jsonl.gz"

& $SevenZip x -y `
  -o"$UncompressedRoot\meta_categories" `
  "$CompressedRoot\meta_categories\meta_Electronics.jsonl.gz"

& $SevenZip x -y `
  -o"$UncompressedRoot\meta_categories" `
  "$CompressedRoot\meta_categories\meta_Home_and_Kitchen.jsonl.gz"

& $SevenZip x -y `
  -o"$UncompressedRoot\review_categories" `
  "$CompressedRoot\review_categories\Electronics.jsonl.gz"

& $SevenZip x -y `
  -o"$UncompressedRoot\review_categories" `
  "$CompressedRoot\review_categories\Home_and_Kitchen.jsonl.gz"
```

Expected uncompressed filenames:

```text
meta_Electronics.jsonl
meta_Home_and_Kitchen.jsonl
Electronics.jsonl
Home_and_Kitchen.jsonl
```

After extraction, record sizes:

```powershell
Get-ChildItem "$UncompressedRoot" -Recurse -Filter "*.jsonl" |
  Select-Object FullName, Length |
  Export-Csv "$ProjectRoot\data\amazon_reviews_2023\uncompressed_sizes.csv" -NoTypeInformation
```

Do not delete the `.jsonl.gz` files until all four uncompressed files pass JSON parsing and the first Parquet outputs have been verified.

---

## 6. Recommended Windows software environment

Suggested tools:

- Windows 11 on an internal SSD.
- Python 3.11 or 3.12.
- VS Code with the Python extension.
- Git.
- 7-Zip.
- DuckDB for robust out-of-core JSONL scans and SQL joins.
- PyArrow for Parquet output.
- Pandas or Polars for smaller processed tables.
- scikit-learn for TF-IDF and Logistic Regression baselines.
- tqdm for progress reporting.
- orjson for small JSON samples and diagnostics.
- A language-identification package selected after the first sample inspection.

Example environment creation:

```powershell
cd C:\Users\30649\Desktop\neoschool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install duckdb pyarrow pandas polars scikit-learn tqdm orjson matplotlib seaborn jupyter
pip freeze > requirements-lock.txt
```

Do not copy a macOS `.venv` directory to Windows. Recreate the environment on Windows.

All project code must use relative paths or a central configuration file. Remove any hard-coded path beginning with `/Users/ziye/`.

---

## 7. Product population and candidate retrieval

### 7.1 Included product classes

1. **Smart plugs:** smart plugs, smart outlets, connected sockets, and smart power strips when their primary function is outlet control.
2. **Smart bulbs:** connected light bulbs with app, voice, or smart-home protocol control.
3. **Smart switches:** smart wall switches and connected dimmers.

### 7.2 Conditional inclusion

Smart dimmers, smart lamp holders, outlet adapters, and smart power strips may be included only when metadata clearly identifies both:

- the target physical device type; and
- connected, app-controlled, voice-controlled, or automated functionality.

### 7.3 Candidate retrieval rule

Search normalized metadata built from:

```text
product_title + categories + features + description
```

A product enters the high-recall candidate set when it contains:

```text
at least one device term
AND
at least one smart-function term
```

Terminology clarification from the 2026-07-27 TA discussion: “high recall” here means broad candidate-product retrieval in the information-retrieval sense. It does **not** mean an Amazon product-recall event, a recalled product, or a product with a high recall rate. In presentations and prose, prefer “broad candidate-product screening” or explicitly define the term before using it.

Representative device terms:

```text
plug, outlet, socket, receptacle, power strip,
bulb, light bulb,
switch, wall switch, dimmer, dimmer switch
```

Representative smart-function terms:

```text
smart, Wi-Fi, wifi, Zigbee, Z-Wave, Matter, HomeKit,
Alexa, Google Home, app control, voice control, remote control
```

### 7.4 Exclusions

Exclude products whose primary identity is:

- conventional/non-smart LED bulbs;
- mechanical switches;
- wall plates, switch covers, protective cases, or mounting hardware;
- replacement parts;
- ordinary extension cables;
- non-smart adapters;
- hubs, cameras, lamps, speakers, or unrelated appliances;
- accessory-only listings;
- products whose smart terms occur only in irrelevant compatibility or marketing text.

### 7.5 Product-filter validation

The candidate step is intentionally high recall and may contain false positives. It is not the final sample.

Required validation:

1. Create `candidate_reason` columns recording which device and capability terms matched.
2. Assign a provisional `device_type`.
3. Stratify by `device_type`, source domain, and match pattern.
4. Manually inspect positive candidates and likely false positives.
5. Revise rules and save a versioned vocabulary file.
6. Freeze the vocabulary before training the failure models.
7. Report product-filter precision separately for plugs, bulbs, and switches.

---

## 8. Review-metadata integration

### 8.1 Primary key

Use `parent_asin` to join review records to item metadata.

Product variants may have different `asin` values while sharing one `parent_asin`. Therefore:

- `parent_asin` is the main product identifier and aggregation key.
- `asin` is retained for variant-level audits only.
- `user_id` is retained only for deduplication and leakage checks and should be hashed in processed outputs.

### 8.2 Cross-domain duplicates

The same parent product or review may appear in more than one source category. During import, add:

```text
source_domain ∈ {Electronics, Home_and_Kitchen}
```

For metadata duplicates:

- retain one final row per `parent_asin`;
- retain `source_domains` showing all source domains;
- prefer or coalesce the record with the richest non-null title, categories, features, and description;
- log every duplicate-resolution rule.

For review duplicates, create a reproducible duplicate key based on:

```text
user_id + parent_asin + timestamp + rating + normalized review_text
```

Keep one record and report how many duplicates were removed within and across domains.

### 8.3 Unmatched records

Reviews with a missing `parent_asin` or no matching selected product metadata are not part of the main analytical sample. Record:

- total raw review records;
- records with missing keys;
- matched review-metadata records;
- match rate;
- unmatched records;
- records belonging to non-target products.

---

## 9. Final raw-field specification

### 9.1 Review fields

| Raw field | Final name | Status | Use |
|---|---|---|---|
| `parent_asin` | `parent_asin` | Core | Metadata join and product-month aggregation |
| `rating` | `rating` | Core | Rating baseline, low-star flag, monthly outcomes |
| `title` | `review_title` | Core | Concatenated with review body |
| `text` | `review_body` | Core | Primary failure-complaint text |
| `timestamp` | `review_datetime`, `review_month` | Core | Time ordering, aggregation, chronological split |
| `verified_purchase` | `verified_purchase` | Auxiliary | Verified-purchase robustness analysis |
| `helpful_vote` | `helpful_vote` | Auxiliary | Optional credibility weighting and robustness |
| `asin` | `asin` | Auxiliary | Variant-level audit |
| `user_id` | `user_id_hash` | Management only | Deduplication and leakage audit |
| `images` | Not retained | Excluded | Outside the current text-only scope |

### 9.2 Core metadata fields

| Raw field | Final name | Status | Use |
|---|---|---|---|
| `parent_asin` | `parent_asin` | Core | Review-metadata join |
| `main_category` | `main_category` | Core | Domain validation and stratification |
| `title` | `product_title` | Core | Primary product-identification text |
| `categories` | `categories` | Core | Hierarchical classification and filtering |
| `features` | `features` | Core | Smart-function and device evidence |
| `description` | `description` | Core | Fallback product-identification evidence |

### 9.3 Auxiliary, management-only, and excluded metadata

| Raw field | Final name | Status | Use |
|---|---|---|---|
| `store` | `store` | Auxiliary | Brand/store subgroup checks |
| `details` | `details` | Auxiliary | Selected relevant device attributes |
| `price` | `price` | Auxiliary | Exploratory price strata if sufficiently complete |
| `average_rating` | Not a model input | Management only | Descriptive check; may contain future information |
| `rating_number` | Not a model input | Management only | Source-scale check only |
| `images` | Not retained | Excluded | No multimodal modeling in the current study |
| `videos` | Not retained | Excluded | No multimodal modeling in the current study |
| `bought_together` | Not retained | Excluded | Not required for failure detection |

### 9.4 Field-use prohibitions

- Do not use `average_rating` or `rating_number` as predictive features.
- Do not use `parent_asin`, `asin`, or `user_id` as semantic model features.
- Do not treat `rating <= 2` as ground-truth engineering failure.
- Do not use future reviews or future rating aggregates when predicting an earlier month.
- Rename review `title` to `review_title` and metadata `title` to `product_title` before joining.

---

## 10. Cleaning and preparation of the uncompressed data

### 10.1 Processing policy

Every processing script should read from:

```text
data/amazon_reviews_2023/raw/uncompressed/
```

and never edit the original JSONL files in place.

Intermediate and final outputs should use Parquet because it is typed, compressed, columnar, and substantially faster for repeated analysis.

### 10.2 First-pass validation

For every uncompressed file:

1. Record file path, byte size, and modification time.
2. Parse a small sample from the beginning, middle, and end.
3. Confirm that each non-empty line is one JSON object.
4. Record observed keys and value types.
5. Count parse errors without silently discarding them.
6. Compare the observed schema across the two domains.

### 10.3 Review cleaning

- Preserve an untouched copy of the original title and body in the first controlled intermediate table if storage permits.
- Convert `timestamp` from Unix milliseconds to UTC datetime.
- Generate a calendar-month field.
- Normalize Unicode and whitespace.
- Remove HTML fragments where they interfere with text analysis.
- Do not remove negation words such as `not`, `never`, or `without`.
- Do not stem or aggressively normalize the archival text column.
- Drop records only when both title and body are unusable.
- Create a normalized text column for deduplication separately from the model text.
- Detect English-language reviews after product filtering, not across the entire raw database.
- Report all removals by reason.

### 10.4 Recommended import order

1. Validate and import the two metadata JSONL files.
2. Materialize a metadata Parquet table containing only the approved fields.
3. Create the candidate-product table.
4. Apply exclusions and manual QA to create `target_products`.
5. Import the two review JSONL files from disk.
6. Add `source_domain`.
7. Join/filter using the target `parent_asin` set.
8. Clean and deduplicate only the retained review population.
9. Save the review-level Parquet table.
10. Produce sample-flow and descriptive-statistics reports.

This order avoids repeatedly parsing irrelevant review text even though the source files have been fully decompressed.

### 10.5 Memory safety

Do not run:

```python
pandas.read_json("Electronics.jsonl", lines=True)
```

on the complete raw file unless the machine has been proven to have enough memory. Prefer DuckDB or a Polars lazy scan of the uncompressed JSONL files, then materialize selected columns and rows to Parquet.

---

## 11. Derived review-level fields

| Field | Construction | Purpose |
|---|---|---|
| `review_text` | `review_title + review_body` | Unified NLP input |
| `review_datetime` | converted `timestamp` | UTC review time |
| `review_month` | calendar month from datetime | Product-month aggregation |
| `device_type` | metadata filter | `smart_plug`, `smart_bulb`, or `smart_switch` |
| `low_star` | `rating <= 2` | Rating baseline |
| `failure_binary` | annotation/classifier | Whether an engineering failure is described |
| `failure_type` | annotation/classifier | Multi-label F1–F8; N0 for non-failure |
| `severity` | annotation/classifier | Ordinal level 0–3 |
| `persistence` | annotation/classifier | Ordinal level 0–2 |
| `sentiment_score` | sentiment model | General-sentiment baseline |
| `keyword_hit` | failure rule dictionary | Transparent rule baseline |

---

## 12. Engineering-failure definition

### 12.1 Operational definition

A review describes an engineering failure when the product fails to perform a core intended function or exhibits unintended, repeated, unrecoverable, or safety-relevant technical behavior.

The following are not engineering failures by themselves:

- price dissatisfaction;
- shipping or delivery problems;
- damaged packaging without a product malfunction;
- aesthetic preferences;
- dislike of the interface without functional impairment;
- customer-service complaints;
- general low ratings without a described technical problem.

### 12.2 Provisional failure taxonomy

| Code | Failure type | Examples | Boundary |
|---|---|---|---|
| F1 | Power/hardware | No power, relay failure, broken bulb or switch | Must describe functional failure, not build-quality preference |
| F2 | Connectivity | Offline, disconnecting, cannot join network | Must involve communication failure or interruption |
| F3 | Installation/pairing | Cannot pair, setup failure, reset does not work | Difficulty that is eventually resolved may have low severity |
| F4 | Software/firmware/app | Update failure, app cannot control device, firmware bug | Interface preference alone is not a failure |
| F5 | Automation/compatibility | Routine failure, platform/protocol incompatibility | Must impair a promised smart function |
| F6 | Performance instability | Delay, random restart, intermittent failure | Must describe observable instability |
| F7 | Safety/thermal | Overheating, spark, smoke, burning smell | Explicit evidence is a high-severity candidate |
| F8 | Durability | Early-life failure, repeated replacements | Should include usage duration or replacement evidence |
| N0 | Non-engineering complaint | Price, delivery, packaging, appearance, support | Used for negative and boundary examples |

`failure_type` may be multi-label. The taxonomy remains provisional until reviewed with the TA.

The 2026-07-27 TA discussion clarified that failure type is primarily a supporting annotation for screening, interpretation, and error analysis. The main proposed engineering-warning contribution should emphasize severity and persistence rather than treating eight-class failure-type accuracy as the sole outcome.

---

## 13. Severity and persistence labels

### 13.1 Severity

| Value | Meaning | Rule |
|---:|---|---|
| 0 | No engineering failure | No technical functional abnormality |
| 1 | Minor | Temporary and recoverable after one retry/reset; core function remains usable |
| 2 | Serious | Core function lost, repeated failures, or return/replacement required |
| 3 | High risk | Overheating, electrical/safety risk, permanent damage, or possible property damage |

### 13.2 Persistence

Persistence is a derived annotation, not an Amazon raw field. Apply it primarily to reviews where `failure_binary = 1`.

Keep two levels separate:

1. **Review-level textual persistence:** inferred only from explicit evidence inside the current review, such as recurrence, duration, or failure after an attempted fix. This is the `persistence` label below.
2. **Product-level temporal persistence:** derived later by aggregating reviews for the same `parent_asin` across months. It is a longitudinal signal, not a label imported from future reviews into an earlier review.

Do not use later reviews or later product-month outcomes to assign an earlier review-level persistence label.

| Value | Meaning | Rule |
|---:|---|---|
| 0 | Unknown/single event | The review reports one event or gives insufficient evidence of repetition/duration |
| 1 | Intermittent/repeated | The problem recurs or appears intermittently, possibly recovering between episodes |
| 2 | Continuous/unrecoverable | The problem remains present or continues after reset, update, reinstallation, or another attempted fix |

Decision priority:

```text
Explicitly continuous and not recovered after attempted repair → 2
Otherwise explicitly repeated or intermittent → 1
Otherwise insufficient persistence evidence → 0
```

Examples:

- “The bulb stopped working yesterday.” → `0`
- “It keeps disconnecting every few days.” → `1`
- “It still does not work after a factory reset.” → `2`
- “It has not happened again.” → do not label `1` merely because the word `again` occurs.
- “Resetting fixed the problem.” → do not label `2`.

When repetition and non-recovery both occur, use `2` if the final state is explicitly unrecovered.

---

## 14. Human annotation plan

### Stage 1

- Label 300 reviews.
- Use the first batch to revise the annotation manual and identify difficult boundaries.
- Stratify by product type, rating, year, keyword hit, and non-hit controls.
- Do not sample only one- or two-star reviews.

### Stage 2

- Expand to approximately 1,500–2,000 reviews after the rules stabilize.
- At least 20% should be independently annotated by two team members.
- Resolve disagreements through adjudication.
- Report Cohen’s kappa for applicable labels.
- Retain annotator IDs and annotation version, but do not expose raw user identifiers.

Required labels:

```text
failure_binary
failure_type
severity
persistence
annotation_notes
annotator_id
annotation_version
```

---

## 15. Baselines and proposed method

Run experiments from simple to complex.

| ID | Method | Inputs and outputs | Purpose |
|---|---|---|---|
| B0 | Rating baseline | Monthly mean rating, low-star share, review volume | Direct quality benchmark |
| B1 | Sentiment baseline | Review text → sentiment score → monthly aggregation | Test general negativity |
| B2 | TF-IDF + Logistic Regression | Review text → `failure_binary` | Simple learnable NLP baseline |
| B3 | Keyword/rule baseline | Failure dictionary and context rules | Transparent engineering baseline |
| M1 | Engineering-signal model | Failure type, severity, persistence → monthly indices | Test incremental warning value |
| M2 | Lightweight pretrained language model | Review text → structured failure labels | Later improvement after baselines stabilize |

Accuracy alone is insufficient because failure labels may be imbalanced. Report Precision, Recall, Macro-F1, Micro-F1, per-class F1, and confusion matrices.

The primary fast, interpretable learnable baseline remains TF-IDF + Logistic Regression. A small LSTM/RNN may be added as an optional secondary baseline only after B0–B3 run end to end; it does not replace the transparent baseline ladder. BERT/Transformer-style models remain later-stage work. Rating, sentiment, and engineering models should use the same chronological split and comparable product-month warning targets.

---

## 16. Product-month fields and warning targets

### 16.1 Final product-month fields

| Field | Definition |
|---|---|
| `n_reviews` | Number of valid reviews for one product-month |
| `mean_rating` | Monthly average rating |
| `low_star_share` | Share with `rating <= 2` |
| `failure_count` | Sum of `failure_binary` |
| `fail_review_share` | `failure_count / n_reviews` |
| `mean_severity` | Mean failure severity |
| `mean_persistence` | Mean persistence score among applicable reviews |
| `weighted_failure_score` | Severity- and optionally credibility-weighted failure signal |
| `persistent_failure_share` | Share of reviews with persistence level 1 or 2 |
| `complaint_acceleration` | First difference/change in failure share |
| `burst_score` | Deviation from the product’s prior history |
| `target_next_1m` | Quality deterioration during the next month |
| `target_next_3m` | Quality deterioration during the next three months |
| `split` | Chronological train/validation/test label |

### 16.2 Provisional quality-deterioration event

The provisional target is positive when, over a future one- to three-month horizon:

- mean rating falls by at least 0.3 points relative to the product’s recent baseline; **or**
- low-star share rises by at least 10 percentage points.

These thresholds require TA confirmation and must be varied in sensitivity analysis.

Compare minimum product-month review thresholds of:

```text
10, 20, and 30 reviews
```

### 16.3 Evaluation

- Review-level failure classification: Precision, Recall, Macro/Micro F1.
- Failure type/severity/persistence: per-class F1 and confusion matrix.
- Quality-deterioration prediction: AUROC, AUPRC, and Brier score.
- Early warning: lead time, Precision@k, and Recall@k.
- Robustness: product type, year, brand/store, verified purchase, price, and variant strata where feasible.

Use chronological splits. Do not use a random train/test split as the main evaluation.

---

## 17. Final analytical data products

### `product_catalog.parquet`

One row per `parent_asin`.

Expected columns:

```text
parent_asin
source_domains
main_category
product_title
categories
features
description
store
details
price
device_type
candidate_device_terms
candidate_smart_terms
candidate_reason
filter_version
```

### `review_level_final.parquet`

One row per retained review.

Expected columns:

```text
parent_asin
asin
source_domain
review_datetime
review_month
rating
low_star
verified_purchase
helpful_vote
review_title
review_body
review_text
device_type
user_id_hash
duplicate_key
failure_binary
failure_type
severity
persistence
sentiment_score
keyword_hit
split
```

### `product_month_features.parquet`

One row per product-month.

Expected columns:

```text
parent_asin
review_month
device_type
n_reviews
mean_rating
low_star_share
failure_count
fail_review_share
mean_severity
mean_persistence
persistent_failure_share
weighted_failure_score
complaint_acceleration
burst_score
target_next_1m
target_next_3m
split
```

---

## 18. Required sample-flow and reporting statistics

The pipeline must produce a machine-readable report and a paper-ready table containing:

- Raw metadata records by domain.
- Raw review records by domain.
- Records with JSON parse errors.
- Metadata records missing `parent_asin`.
- Candidate products before exclusions.
- Products removed by each exclusion rule.
- Final products by `device_type`.
- Duplicate parent products across domains.
- Reviews with missing `parent_asin`.
- Reviews matched to target metadata.
- Unmatched reviews and match rate.
- Reviews removed for empty text.
- Exact duplicate reviews removed.
- Non-English reviews excluded.
- Final reviews by device type, rating, year, and verified-purchase status.
- Earliest and latest retained review date.
- Product-month rows before and after minimum-volume filtering.
- Monthly mean/median review counts and relevant percentiles.

The paper-ready sample sentence is:

> After filtering and review-metadata matching, the final corpus contains **[N_PRODUCTS]** parent products and **[N_REVIEWS]** reviews, covering **[START_MONTH]** through **[END_MONTH]**. The three device classes contain **[N_PLUGS]** smart plugs, **[N_BULBS]** smart bulbs, and **[N_SWITCHES]** smart switches.

Every bracketed value must be replaced after extraction.

---

## 19. Reproducibility requirements

Every data-processing stage must:

1. Read immutable source files.
2. Write to a new versioned output path.
3. Log start time, end time, input files, input sizes, configuration version, output rows, and exclusion counts.
4. Use a fixed random seed for sampling.
5. Save the product vocabulary and exclusion rules as configuration data rather than scattering them through notebooks.
6. Keep notebooks for exploration only; production transformations belong in `src/`.
7. Produce Parquet outputs with explicit schemas.
8. Include tests for timestamp conversion, product-term matching, negation-sensitive persistence examples, and leakage rules.
9. Keep model artifacts and large datasets out of Git.
10. Commit code, configuration, documentation, small sample fixtures, and summary tables to Git.

Suggested `.gitignore` entries:

```gitignore
.venv/
__pycache__/
.ipynb_checkpoints/
*.pyc

data/amazon_reviews_2023/raw/
data/amazon_reviews_2023/interim/
data/amazon_reviews_2023/processed/

outputs/models/
*.duckdb
*.duckdb.wal
```

Do not ignore small schema reports, sample-flow summaries, or final paper-ready tables if they contain no private data.

---

## 20. Existing project documents

At the time of this handoff, the Mac files are split across two folders.

### Historical macOS data folder (migration source only)

```text
/Users/ziye/Desktop/neoschool
```

This historical path is not a Windows runtime path. It contains the four macOS cloud-placeholder data paths. The data files carry the macOS `dataless` flag and must not be treated as verified local copies. They will be downloaded again on Windows, so this folder does not need to be copied as part of the migration package.

### Historical macOS document folder (migration source only)

```text
/Users/ziye/Desktop/neoschool1
```

This historical path is not a Windows runtime path. It is the Mac folder that was copied to Windows as the project migration package. The current Windows project root is `C:\Users\30649\Desktop\neoschool`.

The migration manifest expected the following files under `C:\Users\30649\Desktop\neoschool\documents\`:

```text
Data_Section_Draft_and_Final_Field_Specification.docx
Literature Review 20_7_2026_formatted.docx
Literature Review Group1_comment.docx
Research_Question_and_Dataset.pptx
ssrn-2527968.pdf
智能家居产品故障早期预警_实验方案.docx
智能家居故障早期预警_最终字段使用清单_写作交接版.docx
```

During Phase W0, the existing research documents remain in the project root by explicit user instruction. The `documents/` directory is reserved for later organization; do not move the existing Word, PDF, or PowerPoint files without approval.

The following existing documents still describe compressed streaming and must be revised after the Windows pipeline is confirmed:

- `Data_Section_Draft_and_Final_Field_Specification.docx`, especially the Data Cleaning section.
- `智能家居产品故障早期预警_实验方案.docx`, especially the preprocessing workflow and phase P1.

The field-selection logic, failure taxonomy, baselines, and evaluation plan remain applicable.

---

## 21. Current project status

### Completed

- Selected Amazon Reviews 2023 as the source dataset.
- Selected Electronics and Home and Kitchen.
- Downloaded the four files on Mac previously, but they are now cloud placeholders.
- Defined the target products: smart plugs, smart bulbs, and smart switches.
- Drafted product retrieval and exclusion rules.
- Defined the final raw-field list.
- Defined the review-level and product-month derived fields.
- Drafted the engineering-failure definition.
- Drafted F1–F8 failure categories plus N0.
- Drafted severity and persistence scales.
- Defined rating, sentiment, keyword, and TF-IDF baselines.
- Drafted the downstream quality-deterioration target.
- Created a manuscript-ready English Data-section draft with placeholders.
- Created the experimental-plan and writer field-handoff Word documents.
- Completed the Windows Phase W0 setup: relative-path configuration, project directories, local `.venv`, dependency lock file, Git initialization, `.gitignore`, 7-Zip verification, and the environment-check script.
- Synchronized the 2026-07-27 TA discussion decisions and Zhou Zhengyu's current weekly responsibilities into this handoff.

### Not completed

- Windows re-download and full decompression.
- Exact raw row counts.
- JSON schema audit on the uncompressed files.
- Product-filter implementation.
- Candidate-product manual QA.
- Final target product and review counts.
- Annotation dataset.
- Baseline results.
- Product-month panel.
- Final chronological split dates.
- Final descriptive-statistics table.
- Updated paper wording reflecting decompressed processing.

---

## 22. Recommended immediate Windows execution plan

### Phase W0 — Migration verification

**Status: completed on 2026-07-27.**

- Copy this handoff and all documents.
- Confirm the project root.
- Initialize Git for code and documentation.
- Create `.gitignore`.
- Create the Python environment.
- Run and pass the environment-check script, with missing Amazon data reported as pending rather than as an environment failure.

**Completion test:** Codex can list the documents and resolve every project path from `C:\Users\30649\Desktop\neoschool`.

### Phase W1 — Download and decompression

- Download all four gzip files.
- Compare byte sizes.
- Generate SHA-256 manifest.
- Run 7-Zip archive tests.
- Fully decompress to four JSONL files.
- Record uncompressed sizes.

**Completion test:** All four JSONL files can be sampled and parsed without modifying them.

### Phase W2 — Schema and source inventory

- Inspect keys and types.
- Produce exact row counts and parse-error counts.
- Save a schema report.
- Confirm timestamp units and missingness.

**Completion test:** `data/amazon_reviews_2023/reports/source_inventory.json` and a human-readable table exist.

### Phase W3 — Metadata and product selection

- Import approved metadata columns.
- Add source-domain fields.
- Resolve duplicate parents.
- Implement high-recall candidate rules.
- Apply exclusions.
- Manually audit a stratified sample.
- Save the frozen filter vocabulary.

**Completion test:** `target_products.parquet` exists and filter precision is reported by device type.

### Phase W4 — Review extraction and cleaning

- Import approved review columns from the uncompressed JSONL files.
- Join to target parents.
- Clean text and time.
- Deduplicate within and across domains.
- Apply the language rule.
- Produce sample-flow statistics.

**Completion test:** `review_level_base.parquet` exists with exact counts and date coverage.

### Phase W5 — Annotation and simple baselines

- Draw the first 300-review annotation sample.
- Run the rating and keyword baselines.
- Train TF-IDF + Logistic Regression after labels are available.
- Add sentiment baseline.
- Write explicit, comparable product-month formulas for the rating, sentiment, and engineering-warning indices.
- Perform error analysis.

**Completion test:** A reproducible baseline report includes per-class metrics and example errors.

### Phase W6 — Engineering signals and early warning

- Predict failure type, severity, and persistence.
- Aggregate to product-month.
- Build warning targets.
- Freeze chronological splits.
- Compare rating, sentiment, and engineering signals.

**Completion test:** Product-month table, early-warning metrics, lead-time results, and robustness tables exist.

---

## 23. TA discussion synchronization — 2026-07-27

The TA judged the overall research direction workable. The immediate goal is not a large model; it is a clear data flow and a reproducible comparison showing whether progressively more structured information improves warning performance:

```text
rating
→ rating + general sentiment
→ rating + sentiment + engineering-failure information
```

Confirmed or clarified directions:

1. Finish target-product filtering and preprocessing before making claims about final sample size.
2. If the first filter produces too few products or reviews, revise the vocabulary and conditional product boundary inside the approved Electronics and Home and Kitchen domains. Adding new source domains or changing the three primary device classes still requires approval.
3. Begin with a few hundred human annotations, then use models or an LLM to expand labels and compare automated outputs with the human reference. The final expanded annotation size remains provisional.
4. Severity and review-level textual persistence may both be manually annotated. Product-level temporal persistence is a later monthly aggregation and must not introduce future leakage.
5. Failure type supports interpretation; severity and persistence are the more important proposed engineering signals.
6. Run low-cost, inspectable baselines first. Large pretrained models come only after the data and baseline pipeline work.
7. Define the product-month warning formulas explicitly so rating, sentiment, and engineering signals can be compared using the same target and time split.
8. The Data section should emphasize source reliability, selected fields, sample construction, and why the processed dataset better fits engineering-failure analysis than broader datasets. Detailed baseline algorithms belong in Methodology.
9. Comparing this dataset and task framing with relevant earlier studies is required in the paper.

### Zhou Zhengyu’s current weekly responsibilities

1. Lead target-product screening, review preprocessing, and sample-count validation.
2. Produce an inspectable processed-data schema/example after preprocessing; do not rely on raw-file descriptions alone.
3. Run or coordinate the rating, sentiment, keyword/rule, and TF-IDF + Logistic Regression baselines end to end.
4. Coordinate a first manual-labeling exercise for `failure_binary`, severity, and persistence, and inspect what the labels look like in real reviews.
5. Draft the mathematical definitions for monthly aggregation and warning scores.
6. Assign bounded tasks to other team members: writing/data-section comparison, additional traditional baselines, or annotation/QA.
7. The minimum weekly success condition is a clear preprocessing flow plus at least one reproducible baseline run, not a final Transformer model.

Current Windows execution status: Phases W0-W4R are complete, the 1,500-review expanded annotation and adjudication work is complete, and execution is paused before explicit approval of W5-C-B label freezing and baseline retraining. W6 has not started.

---

## 24. Questions still requiring TA confirmation

1. Should smart dimmers, smart outlet adapters, and smart power strips be included?
2. Keep eight failure categories, or merge them into a smaller five-category taxonomy?
3. After the first approximately 300 reviews, what final annotation size is required?
4. Should a small LSTM/RNN be reported as an optional secondary baseline, or should the main paper keep only the more interpretable TF-IDF + Logistic Regression baseline?
5. Is quality deterioration defined as a 0.3 rating decline or a 10-percentage-point increase in low-star share?
6. Should the minimum product-month review count be 10, 20, or 30?
7. Should quarterly aggregation be included as a robustness analysis?
8. What exact weights and formulas should define severity-weighted and persistence-weighted warning indices?
9. Should the main contribution emphasize structured-label classification performance or early-warning lead time?

Until answered, retain the provisional settings and label them as provisional.

---

## 25. Prompt for the first Windows Codex task

Paste the following into a new Codex task after opening `C:\Users\30649\Desktop\neoschool`:

```text
Read PROJECT_HANDOFF.md completely, then inspect every file under documents/.
This project was migrated from macOS to Windows.

The confirmed data-handling decision is to download all four Amazon Reviews 2023
files again on Windows, fully decompress them to JSONL, and process only the
uncompressed JSONL files. Do not implement gzip streaming or direct .jsonl.gz
processing.

The target products are smart plugs, smart bulbs, and smart switches from the
Electronics and Home and Kitchen domains. The main join key is parent_asin.

First:
1. summarize the current project status;
2. verify the Windows directory structure and available disk space;
3. inspect the downloaded file sizes without modifying them;
4. propose the smallest reproducible implementation plan for W1 and W2;
5. do not begin model training until the source inventory and product filter
   have been verified.

Use relative paths, preserve raw files, write transformations to versioned
Parquet outputs, and log row counts and exclusions at every stage.
```

---

## 26. Handoff principle

The immediate priority is not a sophisticated model. The priority is a reproducible sequence:

```text
verified downloads
→ complete decompression
→ schema and row-count inventory
→ target-product selection
→ review-metadata join
→ text/time cleaning
→ annotation
→ transparent baselines
→ engineering-failure signals
→ product-month early-warning evaluation
```

Do not skip source validation, do not equate low ratings with engineering failure, and do not allow future information into earlier prediction months.
