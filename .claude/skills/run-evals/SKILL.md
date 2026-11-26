---
name: run-evals
description: Use when running the token-sparen evaluations E1 (offline replay), E2 (fact preservation), E3 (off-policy) or E4 (live benchmark) and reporting results.
---
# Run evals
- **E2** (CI, deterministic): `python evals/e2/run.py`. Must be 100 %; any miss blocks merge.
- **E1** (private, free): `token-sparen replay <transcripts> --split dev --policy default,static --out <file outside the repo>`
  (look at `--split test` once, never tune on it). Results from real sessions, aggregates included,
  are never committed or quoted in the repo; keep them in a private folder outside it.
- **E3**: `token-sparen ope --log <decisions.jsonl and .1> --target static|engine-snapshot --holdout-after YYYY-MM-DD --out <file outside the repo>`
  on logged decisions (needs a couple of weeks of logs). Same privacy rule; `insufficient` is not a pass.
- **E4** (live, Claude Code on a Claude Max subscription): delegate to `bench-runner`; screening before confirmation.
Reporting: every number carries CI, n, seeds, model and date, and the released plugin hash. Quality
is judged by non-inferiority at -5 pp on resolved rate; cost by paired bootstrap 95 % CI of the
median ratio. No claim without a reproducible script committed in the repo (E2 is the only eval with committed numbers).
