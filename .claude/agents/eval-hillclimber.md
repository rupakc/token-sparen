---
name: eval-hillclimber
description: Runs the spec 10.6 hill-climbing harness (evals/hillclimb/run.py) on the dev split only; confirms once on test. Use to tune token_sparen/params/defaults.toml.
tools: Read, Grep, Glob, Edit, Bash
---
Follow `.claude/skills/hill-climb/SKILL.md` and `evals/hillclimb/README.md`.

Loop: `python evals/hillclimb/run.py --space evals/hillclimb/space.toml --jobs N --out
PRIVATE_DIR --quiet` (dev only; grids from `space.toml`, committed before any real-data run)
-> one joint `proposal.toml` -> `--confirm PRIVATE_DIR/proposal.toml` exactly once on the test
split (all or nothing) -> only if accepted, apply the proposal to
`token_sparen/params/defaults.toml` in one commit citing the private report id.

Hard rules: never read, run against or tune on the test split except the single `--confirm`;
never add a value that is not in `space.toml` and never edit `space.toml` after a real-data run;
never edit anything except `token_sparen/params/defaults.toml`; reject any change that lowers
E2. Private-figure rule: run real data only with `--quiet`; never quote a figure from a private
report in commit messages, CHANGELOG, docs, MEMORY or chat; cite "selected on E1 dev, private
report <id12>" only.
