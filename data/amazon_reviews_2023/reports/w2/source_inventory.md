# Amazon Reviews 2023 Source Inventory — Phase W2

This report contains aggregate source statistics only. It does not contain review text, user IDs, product text, or other raw field values.

| File | Type | Exact non-empty records | Parsed | Errors | Empty | Non-object | Bytes | Fields | parent_asin effective missing | Timestamp range (UTC) | Seconds |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| meta_categories/meta_Electronics.jsonl | metadata | 1,610,012 | 1,610,012 | 0 | 0 | 0 | 5,246,144,134 | 16 | 0 | N/A | 19.944 |
| meta_categories/meta_Home_and_Kitchen.jsonl | metadata | 3,735,584 | 3,735,584 | 0 | 0 | 0 | 11,788,767,944 | 16 | 0 | N/A | 50.372 |
| review_categories/Electronics.jsonl | reviews | 43,886,944 | 43,886,944 | 0 | 0 | 0 | 22,616,233,652 | 10 | 0 | 1996-11-18T16:58:00+00:00 — 2023-09-13T17:26:21.867000+00:00 | 260.619 |
| review_categories/Home_and_Kitchen.jsonl | reviews | 67,409,944 | 67,409,944 | 0 | 0 | 0 | 31,408,889,188 | 10 | 0 | 1998-05-29T02:46:44+00:00 — 2023-09-13T02:17:45.551000+00:00 | 435.480 |

Record counts are exact for non-empty physical JSONL lines. Missingness uses parsed top-level JSON objects as the denominator.
