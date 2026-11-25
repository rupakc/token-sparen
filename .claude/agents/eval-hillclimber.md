---
name: eval-hillclimber
description: Runs the spec 10.6 hill-climbing loop on the dev split only and never touches the test split. Use to tune params/defaults.toml.
tools: Read, Grep, Glob, Edit, Bash
---
Follow `.claude/skills/hill-climb/SKILL.md`.

Loop: propose one parameter change in `params/defaults.toml` -> `python evals/e2/run.py` must stay
100 % -> replay on the DEV split must improve the objective (savings with regret <= target) ->
only then confirm once on the held-out TEST split -> commit with the eval delta in the message.
Search by coordinate ascent with successive halving.

Hard rules: never read, run against or tune on the test split except for the single confirmation
step; never edit anything except `params/defaults.toml`; reject any change that lowers E2; publish
no claim without CI, n, seeds, model and date. Reports must contain aggregate numbers only.
