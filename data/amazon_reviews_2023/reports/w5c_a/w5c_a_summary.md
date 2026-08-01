# Phase W5-C-A summary

Status: `PAUSED_HUMAN_ANNOTATION`

Readiness: `WAITING_FOR_EXPANDED_ANNOTATION`

## Expanded sample

- 1,200 new reviews were selected without overlap with the existing 300.
- Device quotas are exact: 927 smart-plug, 240 smart-bulb, and 33 smart-switch reviews.
- The combined annotation sample will contain 1,500 reviews: 1,137 smart-plug, 300 smart-bulb, and 63 smart-switch reviews.
- All 33 previously unannotated smart-switch reviews were selected.
- The new sample covers 98 `parent_asin` values: 79 smart plug, 18 smart bulb, and 1 smart switch.
- The one-product smart-switch coverage in the new sample reflects the remaining eligible data, not a sampling error.

## Sampling balance

The sample is designed for boundary coverage and is not population-representative.

| Sampling dimension | Count |
|---|---:|
| High model uncertainty | 524 |
| Rating–keyword disagreement | 309 |
| Diversity control | 367 |
| Rating 1–2 | 431 |
| Rating 3 | 66 |
| Rating 4–5 | 703 |
| 2011–2017 | 240 |
| 2018–2020 | 531 |
| 2021–2023 | 429 |

The existing pilot model scored the eligible corpus only for private sample selection. It was not refit, and no prediction was used as an annotation label.

## Blind review packages

- Four Reviewer 1 files contain 300 rows each.
- Four Reviewer 2 files contain 60 rows each.
- The new double-review subset contains 240 rows: 185 smart plug, 48 smart bulb, and 7 smart switch.
- Together with the previous 60 double-reviewed rows, the final double-review total will be 300 of 1,500 reviews (20%).
- All human-label cells are blank.
- Rating, keyword hit, model probability, uncertainty score, product identifiers, source domain, and review date are hidden from annotators.
- Each XLSX contains `Annotation` and `Instructions` sheets and matches its CSV exactly.

## Protection and scope

- Formal W3/W4/W4R data and the existing 300 labels were not modified.
- Raw JSONL and compressed files were not read.
- No model was retrained.
- No product-month engineering-failure signal or future quality target was created.
- W6 was not started.
