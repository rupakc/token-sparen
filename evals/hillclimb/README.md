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

`space.toml` holds `[[entry]]` tables `{key, values, point[, measured_by]}`, committed before any real-data
run. A tuned value can only be a grid value. Checked before any run (the harness exits 2
otherwise): at most 5 values, the current default included, a known config key with the right
type, every value honoured as is by the owning module's reader (its clamp and the invariants:
`min_tokens >= 2000`, ratios in [0, 1]). `[learn.tau]` and `[ope]` are never searched. M3 keys
(facts activation/retirement, pruning idle thresholds, loop-guard `min_k`) only move toward
less intervention (values >= current). Keys no E1 lane replays (guard and read de-dupe limits,
M3 keys) must carry `measured_by = "none"`: never run, reported `unmeasured`, never changed.
The BM25 relevance weights are module constants, not config keys: deferred, not in the space.

## Dev search

Coordinate ascent in file order; a second pass only if the first moved a key (cap 2). For each
coordinate, every other grid value (joined to the current point) must first pass E2 (100 % and
`inline_rate` per level not below the defaults'); then successive halving over the project
groups that have a dev session: a
seeded 25 % sample screens the values, the top half goes on to 50 %, the best to 100 %, where
the acceptance rule decides. Objective = cost-weighted net savings (saved minus the recovery
reads the replay charges), from one lane, never summed across lanes:

- static keys: the `compression` replay per project group (the production quality gate): a
  `compression.levels.<arm>` key on the arm it governs, every other key under the production
  policy (the learner frozen on its packaged priors, in an empty temporary state root);
- learning keys (`learn.savings_reward`, `learn.ladder_retry_decisions`, `learn.defaults.*`):
  the `sim` lane (the shipped learner with the candidate versus with the packaged defaults, in
  the same pass), where breaker trips may not exceed the reference engine's.

No lane replays read de-dupe, so the objective has no read de-dupe term. Writes `trail.jsonl` (every screening and verdict), `proposal.toml` (the joint
proposal: only keys that moved) and `dev.json` (the summary with the proposal's sha256; the
`--quiet` report).

## Acceptance rule

Paired by project group, 2000 seeded bootstrap resamples, 95 % percentile interval: lower
bound of the net-savings difference > 0; for every point, upper bound of the
proxy-regret-per-trim difference versus the current defaults <= 0.2 tau(point) and proxy
regret per trim <= tau(point) (`ope.tau_for`); E2 100 % and `inline_rate` per level not below
the defaults'. On dev it decides each coordinate value (dev may be evaluated many times).
Minimum evidence on dev and test: at least 8 paired project groups with a trim and 30 trims per
evaluated point on each side; below that the verdict is `insufficient` (the coordinate stays,
the proposal is rejected). Breaker trips gate dev values (sim lane) and are only reported at
the confirmation (the plan's acceptance rule does not list them).

## Confirmation (once)

`--confirm` checks that the proposal holds grid values of measured keys only, then creates
the ledger `<state root>/hillclimb/confirm-m4.json` (`~/.token-sparen` or `TOKEN_SPAREN_HOME`;
whatever `--out` is) exclusively (`O_CREAT | O_EXCL`, 0600) with the proposal's sha256 and the
time, **before** any test-split data is read; any later or concurrent `--confirm` (any proposal,
any `--out`) is refused. A deleted ledger cannot be detected: any second look at test
invalidates the result; cite the `confirm.json` id (it records the ledger and, as `dev_report`,
the id of the `dev.json` that wrote the proposal). One paired `sim` run on the test split (proposal versus defaults),
all or nothing; `confirm.json` holds both sides and the verdict. Rejected: the defaults stay
unchanged.

## Output

`--quiet` stdout is exactly:

```
report: <path> id: <12 hex>
status: complete|incomplete
<key>: accepted|rejected|unchanged|unmeasured      (one line per space key)
```

Errors print the exception type only. Cite results as "selected on E1 dev, private report
<id>"; never a figure (commit messages included).
