# W3R-C Product Promotion Summary

- Status: **PASS**
- Formal version: `w3-v1.4.0`
- W4R readiness: **READY_FOR_EXPLICIT_APPROVAL**

## Promotion result

| Device type | W3 v1.3.2 | Promoted additions | W3 v1.4.0 |
|---|---:|---:|---:|
| Smart plug | 95 | 0 | 95 |
| Smart bulb | 8 | 17 | 25 |
| Smart switch | 3 | 2 | 5 |
| **Total** | **106** | **19** | **125** |

- The 19 additions are exactly the W3R-B records with `final_decision = include`,
  `final_label = correct_target`, and an approved bulb/switch device type.
- One ambiguous product remains on hold.
- Three products adjudicated as accessories remain excluded.
- All 125 `parent_asin` values are non-empty and unique.
- The 106 baseline products were retained without Metadata changes; only their
  version-management fields were updated for the new catalog release.
- The 106 shared baseline rows have no Metadata conflicts between the W3 and
  W3R-A product files.

## Output

`data\amazon_reviews_2023\processed\target_products_w3_v1_4_0.parquet`

- Rows: 125
- Fields: 38
- Compression: ZSTD
- Bytes: 94,709
- SHA-256: `e9d0a7548f1568b2bff7b0e2cedd5fa2702cdda2bf507a674a0ad741e89e9a88`

The original `target_products.parquet` remains unchanged. The W4
`review_level_base.parquet` was not opened or modified. No raw JSONL, gzip,
W4R, W5, or Git commit was used.

Smart plug remains the primary longitudinal class. Smart bulb and smart switch
remain exploratory until an explicitly approved W4R measures their review and
product-month coverage.
