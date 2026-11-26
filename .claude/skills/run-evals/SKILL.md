---
name: run-evals
description: Use when running the token-sparen evaluations E1 (offline replay), E2 (fact preservation), E3 (off-policy) or E4 (live benchmark) and reporting results.
---
# Run evals
- **E2** (CI, deterministic): `python evals/e2/run.py`. Must be 100 %; any miss blocks merge.
- **E1** (private, free): replay the maintainer's local sessions through each policy on the dev/test
  split by date. Keep raw results outside the repo; only aggregate tables may be shared, never paths or ids.
- **E3**: off-policy evaluation on decision logs with propensities (needs the ~2-week dogfooding data).
- **E4** (live, Claude Code on a Claude Max subscription): delegate to `bench-runner`; screening before confirmation.
Reporting: every number carries CI, n, seeds, model and date, and the released plugin hash. Quality
is judged by non-inferiority at -5 pp on resolved rate; cost by paired bootstrap 95 % CI of the
median ratio. No claim without a reproducible script in `bench/`.
