# Data provenance and redistribution notice

## Source

This project uses a cleaned research subset derived from **Amazon Reviews 2023**, released by the McAuley Lab. The source project page is <https://amazon-reviews-2023.github.io/>. The associated dataset paper is:

> Hou, Y., Li, J., He, Z., Yan, A., Chen, X., and McAuley, J. (2024). Bridging Language and Items for Retrieval and Recommendation. arXiv:2403.03952.

The full source inventory used by this project contains 111,296,888 review records and 5,345,596 item-metadata records across the Electronics and Home and Kitchen domains. The proposed collaboration package contains only the research subset needed for smart plugs, smart bulbs, and smart switches.

## What the derived data is—and is not

- It is a cleaned, English-language, deduplicated subset created for academic analysis.
- Review text remains user-generated content.
- It is not Amazon repair, return, warranty, or device-telemetry data.
- Model-generated failure probabilities and EngineeringIndex values are not human ground truth or true failure rates.
- Consumer-rating-based quality deterioration targets are operational research labels, not verified hardware-failure events.

## Redistribution decision

The project owner confirmed on 8 August 2026 that the cleaned research subset may be published in this public collaboration repository. This is a project-level release decision, not a new license grant and not a claim that the repository's code license automatically covers the source data. Downstream users should retain the Amazon Reviews 2023 citation, review the upstream terms for their own use, and treat review text as user-generated content. Raw source JSONL/GZ archives are not redistributed here.

## Privacy

The formal review file contains `user_id_hash`, a pseudonymous identifier, but not the source `user_id`. The project owner explicitly approved publication of this field. Automated pattern scanning identified four email-shaped and five phone-shaped matches across eight review rows. No automatic redaction was performed because it would change the frozen research input. This residual content risk is disclosed in `collaboration/privacy_audit.json` and was accepted for this release.
