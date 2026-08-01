# W5-C-A Expanded Annotation Instructions

Use only the visible `review_text`. Do not seek or infer the hidden rating,
keyword hit, model prediction, product identifier, date, or another reviewer's
decision. The expanded sample is selected for boundary coverage and is not
representative of population failure prevalence.

Use the same W5-A definitions:

- `failure_binary = 1`: explicit core-function failure or abnormal technical behavior.
- `failure_binary = 0`: no engineering failure, or only price, delivery, packaging, appearance, service, or another non-technical issue.
- `failure_binary = uncertain`: insufficient textual evidence.
- Failure type: `F1`–`F8`, with multiple codes separated by semicolons; `N0` only for non-failure.
- Severity: `0` no failure; `1` minor/recoverable; `2` core loss/repeated/return; `3` safety, permanent damage, or property risk.
- Persistence: `0` single/unknown; `1` intermittent/repeated; `2` continuous or unresolved after an attempted remedy.
- Confidence: `low`, `medium`, or `high`.

Reviewer 2 must complete the separate 60-row workbook independently. Do not
compare reviewer decisions before both independent passes are complete.
