# Hill-climbing harness (spec 10.6)

Tunes `token_sparen/params/defaults.toml` on offline evidence (E1 replay of the maintainer's
own Claude Code sessions) with one honest held-out confirmation. All figures stay in private
files; nothing here prints or commits a number.

```
python evals/hillclimb/run.py --space evals/hillclimb/space.toml --jobs N --out DIR --quiet
python evals/hillclimb/run.py --confirm DIR/proposal.toml --jobs N --out DIR --quiet
```

`DIR` must be outside any git work tree (`replay.report.write_private`: files 0600, new
directories 0700). `--transcripts PATH...` defaults to Claude Code's projects directory.

## Splits

By a session's first non-copied record: **dev** before 2026-11-08 ≤ **test** before
2026-11-26. Sessions on or after 2026-11-26 or with any `[token-sparen:` marker are excluded
whole. The dev search never reads a test session; the test split is evaluated once.

## Space

`space.toml` holds `[[entry]]` tables `{key, values, point}`, committed before any real-data
run. A tuned value can only be a grid value. Checked before any run (the harness exits 2
otherwise): at most 5 values, the current default included, a known config key with the right
type, every value honoured as is by the owning module's reader (its clamp and the invariants:
`min_tokens >= 2000`, ratios in [0, 1]). `[learn.tau]` and `[ope]` are never searched. M3 keys
(facts activation/retirement, pruning idle thresholds, loop-guard `min_k`) only move toward
less intervention (values >= current).

## Dev search

Coordinate ascent in file order; a second pass only if the first moved a key (cap 2). For each
coordinate, every other grid value (joined to the current point) must first pass E2 (100 % and
`inline_rate` per level not below the defaults'); then successive halving by project group: a
seeded 25 % sample screens the values, the top half goes on to 50 %, the best to 100 %, where
the acceptance rule decides. Objective = cost-weighted net savings (saved minus the recovery
reads the replay charges), from one lane, never summed across lanes:

- static keys: the frozen-engine `compression` replay (the static arm, or the arm a
  `compression.levels.<arm>` key belongs to; the production quality gate), per project group;
- learning keys (`learn.savings_reward`, `learn.ladder_retry_decisions`, `learn.defaults.*`):
  the `sim` lane (the shipped learner with the candidate versus with the packaged defaults, in
  the same pass), where breaker trips may not exceed the reference engine's.

Keys no lane reads (guard and read de-dupe limits, M3 keys) cannot show a gain on E1 and stay
unchanged. Writes `trail.jsonl` (every screening and verdict), `proposal.toml` (the joint
proposal: only keys that moved) and `dev.json` (the summary; the `--quiet` report).

## Acceptance rule

Paired by project group, 2000 seeded bootstrap resamples, 95 % percentile interval: lower
bound of the net-savings difference > 0; for every point, upper bound of the
proxy-regret-per-trim difference versus the current defaults <= 0.2 tau(point) and proxy
regret per trim <= tau(point) (`ope.tau_for`); E2 100 % and `inline_rate` per level not below
the defaults'. On dev it decides each coordinate value (dev may be evaluated many times).

## Confirmation (once)

`--confirm` checks that the proposal holds grid values only, then records the proposal's
sha256 and the time in `DIR/confirm-ledger.json` **before** it runs; any later `--confirm`
(any proposal) is refused. One paired `sim` run on the test split (proposal versus defaults),
all or nothing; `confirm.json` holds both sides and the verdict. Rejected: the defaults stay
unchanged.

## Output

`--quiet` stdout is exactly:

```
report: <path> id: <12 hex>
status: complete|incomplete
<key>: accepted|rejected|unchanged      (one line per space key)
```

Errors print the exception type only. Cite results as "selected on E1 dev, private report
<id>"; never a figure (commit messages included).
