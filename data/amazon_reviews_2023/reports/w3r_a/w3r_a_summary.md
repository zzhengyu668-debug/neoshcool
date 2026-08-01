# Phase W3R-A recall diagnosis

- Status: `PAUSED_INSUFFICIENT_RECOVERY`
- Draft version: `w3-v1.4.0-draft` (not frozen)
- Frozen baseline retained: `w3-v1.3.2`
- Inputs: W3 metadata candidate and target-product Parquet only
- Reviews, ratings, prices, raw JSONL, and review-level Parquet were not read

## Product counts

| Device type | Frozen W3 | Reliable draft additions | Draft total | Diagnostic target (30) |
|---|---:|---:|---:|---:|
| smart_plug | 95 | 0 | 95 | N/A |
| smart_bulb | 8 | 17 | 25 | not met |
| smart_switch | 3 | 6 | 9 | not met |

## Rule-revision review

| Device type | Excluded candidate pool | Potential false negatives | Recoverable | Preliminary precision | Ambiguous |
|---|---:|---:|---:|---:|---:|
| smart_bulb | 1,598 | 17 | 17 | 1.000 | 0 |
| smart_switch | 3,999 | 313 | 6 | 1.000 | 0 |

The precision values above are initial metadata rule-revision estimates, not independent dual-annotator blind-review results.

## Main evidence patterns

- Bulbs: explicit bulb/form-factor identity plus Wi-Fi, Zigbee, Z-Wave, HomeKit, Matter, app/voice control, or supported Bluetooth control.
- Switches: explicit switch/dimmer identity plus wall-lighting context and connected-control evidence; relay-only, remote-only, network, and RF-only items remain excluded.
- Incidental wrong-product words in auxiliary descriptions no longer override a clear primary identity in title/categories.

## Decision

The available W3 candidate pool does not support the requested reliable expansion while preserving the product boundary. Do not promote this draft or rerun W4 without a new decision.
