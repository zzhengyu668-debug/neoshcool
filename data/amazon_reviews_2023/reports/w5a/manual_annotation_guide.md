# W5-A Manual Annotation Guide

Version: `w5a-annotation-v1.0-draft`

## Purpose

Label what the review text explicitly says. Do not infer from star rating, product popularity, or a desire to increase the number of failures. The annotation sample is stratified for boundary coverage and is not representative of the population failure rate.

## Failure binary

- `1`: The text clearly describes failure of a core intended function or abnormal technical behavior.
- `0`: No engineering failure is described, or the issue concerns only price, delivery, packaging, appearance, customer service, or another non-technical matter.
- `uncertain`: The text does not provide enough evidence; leave the final decision for adjudication.

A low rating is never sufficient evidence of an engineering failure.

## Failure type

Use one or more codes separated by semicolons when multiple mechanisms are explicitly present.

- `F1`: Power supply, charging, relay, or hardware failure.
- `F2`: Connectivity or network failure.
- `F3`: Installation, setup, or pairing failure.
- `F4`: Firmware, software, or app failure.
- `F5`: Automation, voice-assistant, ecosystem, or compatibility failure.
- `F6`: Intermittent behavior, instability, latency, or random restart.
- `F7`: Safety, overheating, smoke, spark, shock, or electrical hazard.
- `F8`: Durability, premature wear, repeated breakage, or shortened service life.
- `N0`: No engineering failure. Use only with `failure_binary = 0`.

## Severity

- `0`: No engineering failure.
- `1`: Minor or temporary issue recoverable with one retry, reset, or simple action.
- `2`: Core-function loss, repeated failure, or a problem requiring return or replacement.
- `3`: Overheating, electrical or safety risk, permanent damage, or property risk.

## Persistence (review-level text evidence only)

- `0`: Single incident, unknown recurrence, or no explicit repetition evidence.
- `1`: Intermittent, repeated, or recurring behavior is explicitly described.
- `2`: Continuous failure or failure remaining after reset, upgrade, reinstallation, or another attempted remedy.

Do not use other reviews or future months to assign Persistence. Product-level cross-month Persistence is outside W5-A.

## Confidence and notes

Use `low`, `medium`, or `high` confidence. Notes should briefly identify the textual evidence or the source of uncertainty. Do not consult the hidden rating, keyword rule, product identifier, or another reviewer’s decisions.

## Double review and adjudication

Reviewer 2 independently labels the separate 60-row workbook without seeing Reviewer 1 results. After both independent passes are complete, disagreements are resolved in the adjudication columns of the 300-row workbook.
