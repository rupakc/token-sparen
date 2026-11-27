---
name: hill-climb
description: Use when tuning parameters in token_sparen/params/defaults.toml to improve savings without hurting quality.
---
# Hill-climb (spec 10.6)
The harness is `evals/hillclimb/run.py` (see `evals/hillclimb/README.md`); never tune by hand.
1. Grids live in `evals/hillclimb/space.toml` and are committed BEFORE any real-data run. A
   tuned value can only be a grid value (each grid includes the current default, <= 5 values,
   inside the module clamp; `[learn.tau]` and `[ope]` never; M3 keys only toward less
   intervention).
2. Dev search only: `python evals/hillclimb/run.py --jobs N --out PRIVATE_DIR --quiet`
   (coordinate ascent, successive halving by project group, E2 100 % and inline_rate gates,
   acceptance rule per value). It writes a private `proposal.toml`.
3. Confirm ONCE on the held-out TEST split: `--confirm PRIVATE_DIR/proposal.toml`. The ledger
   refuses any second confirmation. All or nothing: rejected means the defaults stay.
4. Only an accepted proposal changes `token_sparen/params/defaults.toml`, as one commit
   (`chore(params): M4 joint proposal, E1 dev-selected, test-confirmed (report <id12>)`, the
   `confirm.json` id).
5. Private-figure rule: E1/E3 numbers never leave the private files. Run real data with
   `--quiet` (stdout: report path + 12-hex id, status, `<key>: accepted|rejected|unchanged|unmeasured`).
   Commit messages, CHANGELOG, docs and chat cite the report id only, never a figure.
Use the `eval-hillclimber` agent for the loop.
