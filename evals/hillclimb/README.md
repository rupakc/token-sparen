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
`--cutoff YYYY-MM-DD` (default 2026-11-08, `replay.DEFAULT_CUTOFF`) and `--dogfood-start
YYYY-MM-DD` (default 2026-11-26) set the dev search's split; `--confirm` takes both from the dev
report and refuses given values that differ. `--campaign ID` (default `m4`,
`[a-z0-9][a-z0-9_-]{0,31}`) is a label recorded in the reports and the ledger, nothing more.

## Splits

By a session's first non-copied record: **dev** before the cutoff ≤ **test** before the
dogfood start (M4: 2026-11-08 and 2026-11-26). Sessions on or after the dogfood start or with
any `[token-sparen:` marker are excluded whole. The dev search never reads a test session; each
test window `[cutoff, dogfood start)` is evaluated once (see Confirmation).

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
  the same pass; rewarded with the re-sends the live hook projects, never the actual ones; one
  run per seed of `SIM_SEEDS`, three seeds in one pass, averaged per project group), where
  breaker trips may not exceed the reference engine's. The learner is rewarded and judged on
  the same replay proxy, so a learning key can look better offline than live (method doc).

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

`--confirm` checks that the proposal holds grid values of measured keys only and that its dev
report (`dev.json` beside it, carrying the proposal's sha256) exists (`NoDevReport`, exit 2);
the split dates are the dev report's (`SplitMismatch`, exit 2, if given flags differ). Then,
under a lock and **before** any test outcome is computed, it appends its test window
`[cutoff, dogfood_start)` with the campaign label, the proposal's sha256 and the time to the one
ledger `<state root>/hillclimb/test-windows.json` (`~/.token-sparen` or `TOKEN_SPAREN_HOME`;
whatever `--out` or `--campaign` is; atomic write, 0600). A window that overlaps any recorded
window is refused (`ConfirmRefused`), whatever the campaign label; an unreadable ledger refuses
too and is never reset. A deleted ledger cannot be detected: any second look at a test window
invalidates the result; cite the `confirm.json` id (it records the ledger entry and, as
`dev_report`, the id of the `dev.json` whose dates it used).

It reads the test split once, in one pass: the paired `sim` run (proposal versus defaults,
every seed of `SIM_SEEDS`, averaged per group) and, for every `compression.levels.<arm>` key in
the proposal, the `compression` lane on that arm (proposal versus defaults, paired by group;
the same two regret bounds and the minimum evidence, reasons `arm:<arm>:...`, figures under
`arms` in `confirm.json`). All or nothing: any failed rule rejects the whole proposal and the
defaults stay unchanged.

A later campaign needs a new count-based cutoff and a window no confirmation has read, and
either sessions run with token-sparen disabled or a contamination-aware replay (deferred): once
the plugin is installed, new sessions are excluded as dogfooded.

## Output

`--quiet` stdout is exactly:

```
report: <path> id: <12 hex>
status: complete|incomplete
<key>: accepted|rejected|unchanged|unmeasured      (one line per space key)
```

Errors print the exception type only. Cite results as "selected on E1 dev, private report
<id>"; never a figure (commit messages included).
