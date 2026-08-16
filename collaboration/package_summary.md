# W7-C0 collaboration package summary

## 2026-08-16 workflow addendum

The original W7-C0 data/model snapshot below remains frozen. The collaboration workflow has since moved from the pure Engineering-only pilot to a controlled nested M0-M3 comparison: Rating-only, Rating + Sentiment, Rating + Engineering, and Rating + Sentiment + Engineering. The public package now includes a frozen route config, a Train/Validation-only runner, automated Test-isolation checks, and `docs/NESTED_MODEL_COMPARISON_HANDOFF.md`. No existing data or upstream model identity was changed.

## Status

- Primary status: `READY_FOR_GITHUB_PUBLISH_APPROVAL`
- Secondary hold: none
- Frozen input identities: PASS
- Engineering-only trained at the time of this W7-C0 snapshot: no
- Test performance evaluated: no
- Test target used for development: no
- Process note: an aggregate all-eligible target count was loaded during precheck; no Test-specific metric, prediction, or model-selection calculation was performed.
- Git publication: explicitly authorized; the resulting branch, commit, push, and PR are recorded by Git/GitHub rather than this preparation snapshot

## Package inventory

- Candidate files in the original W7-C0 publication snapshot: 42
- Candidate total size: 36,645,938 bytes
- Single files over 50 MiB: 0
- Git LFS required by current sizes: no

## Privacy decision

The formal 55,877-row review file contains the pseudonymous `user_id_hash`. The project owner explicitly approved publishing it unchanged. Automated review-text scanning found 8 rows with email- or phone-shaped strings. No text was copied into reports and no automatic redaction occurred. The local hash-free derivative is excluded as an unnecessary duplicate.

## Redistribution decision

The project owner confirmed public release of this cleaned research subset on 2026-08-08. Raw source JSONL/GZ files remain excluded, and downstream users are directed to the upstream citation and terms.

## Next decision

Publish only the exact allowlist, then have both Engineering-only implementers run the verifier before development.
