# RA Class TA Discussion Summary — 2026-07-27

- **Participants used in this summary:** Speaker 1 = TA; Speaker 2 = Zhou Zhengyu
- **Source:** `20260727_143507_原文.docx`
- **Status:** Automatic transcript reviewed against the project handoff; obvious speech-recognition ambiguity was resolved from context.

## Overall result

The TA considered the project direction workable. The research should demonstrate, through an incremental and reproducible experiment, whether structured engineering-failure information provides earlier or clearer product-quality warnings than ratings and general sentiment.

The near-term priority is:

```text
clean and filter the data
→ inspect the processed sample
→ run simple baselines
→ label severity and persistence
→ define monthly warning formulas
→ add larger models only later
```

## Clarified research design

### Candidate-product screening

“High recall” means broad candidate retrieval, not an Amazon product-recall event or a high product recall rate. The team should use clearer wording when presenting the pipeline.

After the first screening pass, check the retained product and review counts. If the sample is unexpectedly small, revise search terms and conditional inclusion rules inside Electronics and Home and Kitchen before proposing a broader domain change.

### Human annotation and automated expansion

Start with a few hundred manually labeled reviews. Use these labels to:

- refine the annotation rules;
- train or prompt an automated method;
- compare automated labels with the human reference;
- expand to a larger review population only after the label logic is stable.

Low-star reviews must not become automatic failure labels.

### Severity and persistence

Severity is a review-level label and is comparatively straightforward.

Persistence has two meanings that must remain separate:

- **Review-level textual persistence:** the review explicitly says the fault recurs, lasts, or remains after an attempted fix.
- **Product-level temporal persistence:** repeated failure evidence across months for the same `parent_asin`.

The first can be annotated from review text. The second is computed after review-level predictions are aggregated over time. Future reviews must not influence an earlier review label.

Failure type remains useful for interpretation and error analysis, but the main proposed engineering signals should focus on severity and persistence.

### Baseline ladder

Use comparable data splits and warning targets for three increasingly structured information sets:

1. Rating and low-star statistics.
2. Rating plus general sentiment.
3. Rating, sentiment, and engineering-failure information.

TF-IDF + Logistic Regression remains the preferred first learnable baseline because it is fast and inspectable. A small LSTM/RNN is optional. BERT, Transformers, and larger models should wait until the baseline pipeline works.

### Early-warning calculation

The team must specify how review-level predictions become product-month indices. The paper should not stop at “a model produces a score”; it needs explicit formulas for:

- monthly rating deterioration;
- monthly sentiment aggregation;
- failure-review share;
- severity-weighted failure score;
- persistence-weighted failure score;
- the future quality-deterioration target;
- lead time and other early-warning metrics.

### Paper organization

The Data section should cover:

- dataset source and reliability;
- selected fields;
- product filtering and sample construction;
- processed sample size and date range;
- comparison with earlier datasets or studies;
- why this processed sample fits engineering-failure analysis.

Detailed baseline algorithms, model training, and warning-score formulas belong in Methodology.

## Zhou Zhengyu’s tasks for this week

### Priority 1 — Data screening and preprocessing

- Lead target-product screening for smart plugs, smart bulbs, and smart switches.
- Confirm the post-filter product/review counts rather than estimating them.
- Inspect false positives and adjust vocabulary when needed.
- Produce a clear processed-data schema or small inspectable example.
- Record the exact fields used in the final sample.

### Priority 2 — Reproduce the baseline pipeline

- Run the rating baseline.
- Run the sentiment baseline.
- Run the keyword/rule engineering baseline.
- Train TF-IDF + Logistic Regression once human labels are available.
- Keep the chronological split and product-month target consistent across comparisons.

The minimum weekly success condition is that preprocessing and at least one baseline run end to end reproducibly.

### Priority 3 — Annotation and engineering-signal design

- Organize the first few hundred manual annotations.
- Include `failure_binary`, severity, and review-level persistence.
- Review disagreement and boundary cases with teammates.
- Begin translating severity and persistence into explainable monthly formulas.

### Priority 4 — Team coordination

- Give each teammate a bounded task, such as one traditional baseline, annotation/QA, or Data-section comparison.
- Coordinate the writing work separately from the data/model implementation.
- Share only the processed, reduced dataset with the group after the approved data pipeline exists.

## Items not settled in the discussion

- Exact final annotation size.
- Whether the final taxonomy uses eight failure types or a smaller merged set.
- Whether LSTM/RNN belongs in the main results or only as an optional comparison.
- Exact deterioration thresholds and minimum product-month review count.
- Exact weights for severity and persistence.
- Whether the main contribution should emphasize classification metrics or warning lead time.

## Current execution gate

The Windows project has completed Phases W0-W4R and the W5-C-A expanded annotation and adjudication work. It is currently paused before explicit approval of W5-C-B label freezing and baseline retraining; W6 has not started.
