---
name: hill-climb
description: Use when tuning parameters in token_sparen/params/defaults.toml to improve savings without hurting quality.
---
# Hill-climb (spec 10.6)
1. Pick one tunable in `token_sparen/params/defaults.toml`; propose a single change.
2. `python evals/e2/run.py` must stay 100 %; otherwise revert.
3. Evaluate on the DEV split: objective = savings with regret at or below target. Must improve.
4. Confirm once on the held-out TEST split. Never tune against it; never iterate on it.
5. Accept by committing only the param change, with the eval delta in the message
   (`chore(params): <name> a -> b (dev +x%, test +y%)`).
6. Search by coordinate ascent with successive halving; periodically confirm with E4.
Use the `eval-hillclimber` agent for the loop. Report aggregates only.
