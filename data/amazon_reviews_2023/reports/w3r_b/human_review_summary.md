# W3R-B human review reconciliation

- Status: `PAUSED_ADJUDICATION`
- Submitted workbooks: 2
- Complete independent reviews: 23 / 23 for each reviewer
- Evidence and blind-ID order: identical
- Blind-ID to parent-ASIN key: not opened

## Agreement

| Field | Exact agreement | Rate | Cohen's kappa |
|---|---:|---:|---:|
| Device type | 20/23 | 87.0% | 0.699 |
| Label | 20/23 | 87.0% | 0.373 |
| Confidence | 20/23 | 87.0% | 0.527 |
| Joint device type + label | 20/23 | 87.0% | N/A |

The label kappa is lower than the raw agreement because both reviewers used `correct_target` very frequently; this is a prevalence effect, not a contradiction in the calculations.

## Before adjudication

- Unanimous `correct_target`: 19
- Unanimous smart bulbs: 17
- Unanimous smart switches: 2
- Unanimous non-final/ambiguous decisions: 1
- Cases requiring adjudication: 3
- Blind IDs requiring adjudication: `W3RB-002`, `W3RB-009`, `W3RB-022`

No proposed product was automatically promoted. Fill only the yellow adjudication fields in the `Adjudication Queue` sheet, then return the workbook for final reconciliation.
