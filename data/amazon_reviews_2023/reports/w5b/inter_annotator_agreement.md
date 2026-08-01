# W5-B Inter-Annotator Agreement

The statistics below compare the two independent annotations for the 60-row
double-review subset. Adjudicated labels are not used in these agreement
calculations.

## Failure binary

| Scope | N | Raw agreement | Cohen's kappa |
|---|---:|---:|---:|
| Including `uncertain` | 60 | 0.9333 | 0.8727 |
| Excluding rows where either reviewer used `uncertain` | 57 | 0.9825 | 0.9649 |

## Failure type (multi-label)

- Exact-match agreement: 0.7333
- Mean Jaccard similarity: 0.8403
- Micro F1 (Reviewer 1 as reference): 0.8820
- Macro F1 (Reviewer 1 as reference): 0.7462

Failure type is multi-label; ordinary single-class accuracy is not used.

## Severity and persistence

| Label | Valid N | Raw agreement | Linear weighted kappa | Quadratic weighted kappa |
|---|---:|---:|---:|---:|
| Severity | 57 | 0.9123 | 0.9159 | 0.9571 |
| Persistence | 57 | 0.8421 | 0.6294 | 0.5653 |

Confidence is reported descriptively and is not a final research label.
