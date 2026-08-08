# Reproduction checklist

- [ ] Clone the repository into a path without hard-coded user names.
- [ ] Install 64-bit Python 3.11.
- [ ] Create `.venv` and install `requirements-collaboration.txt`.
    - [ ] Confirm the approved data files were downloaded with the repository.
- [ ] Run `scripts/verify_collaboration_package.py`.
- [ ] Confirm all SHA-256 values match.
- [ ] Confirm 55,877 cleaned reviews and 125 target products.
- [ ] Confirm 1,911 unique product-month rows.
- [ ] Confirm 515 h=3 eligible rows and split counts 205/28/150/17/115.
- [ ] Read `docs/ENGINEERING_ONLY_HANDOFF.md`.
- [ ] Keep Rating, Sentiment, text, identity, and future fields out of Engineering-only.
- [ ] Fit preprocessing on Train only.
- [ ] Develop on Validation only.
- [ ] Freeze code, features, environment, parameters, and threshold before requesting Test approval.
- [ ] Compare the two independent Engineering-only implementations.
- [ ] Preserve negative and uncertain results.
