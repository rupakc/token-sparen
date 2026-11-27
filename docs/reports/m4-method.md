# M4 method: tuning the defaults on offline evidence

This document describes how the M4 campaign selects (or declines to change) the packaged
defaults in `token_sparen/params/defaults.toml`. It describes a method only. It contains no
measured figure, and none will be committed to this repository (spec §10.1). Results are cited
only as "selected on maintainer E1 dev split, private report <id>", where the id is the first twelve hex digits
of a private report's content hash.

The harness is `evals/hillclimb/run.py` (see `evals/hillclimb/README.md`); the measurements come
from `token-sparen replay` (`token_sparen/replay/`) and the fact-preservation suite
(`evals/e2/run.py`).

## Prior look, stated up front

An earlier aggregate E1 look over all available sessions, with no split, informed the
quality-first shape of the shipped defaults. No parameter was tuned on that look, and none of
its numbers is used below. Everything in this document is a fresh, pre-declared procedure.

## Data and splits

- Evidence is the maintainer's own Claude Code transcripts, read locally. They never leave the
  machine, and every report is written outside any git work tree (files readable by the owner only).
- A session's split date is the timestamp of its first record that is not copied history (a
  resumed session copies earlier records; they are de-duplicated per project group across all
  files before the split is attributed).
- **dev** is before 2026-11-08; **test** is on or after 2026-11-08 and before 2026-11-26. The
  harness fixes this cutoff itself; plain `token-sparen replay` uses its own default cutoff (and
  `--cutoff`), so the two agree only when the cutoff is passed explicitly.
- The cutoff was chosen from session counts only, before any outcome was viewed: Claude Code keeps
  transcripts for about thirty days, so the retained history is short and the cutoff gives a
  workable share to each side. The honest consequence is low statistical power: the likeliest
  outcome of the campaign is that nothing is confirmed.
- **Dogfooded sessions are excluded whole.** A session is left out if any of its records contains
  the raw text `[token-sparen:` or is dated on or after 2026-11-26, when the plugin was first used
  on the maintainer's own work. Hook text that lacks that exact text does not exclude a session. A session and its
  subagent transcripts are excluded together, in both directions.
- Sessions are grouped by project (the top-level directory under the input root). Groups are
  the unit of replay, of subsampling and of resampling. Chronological lanes always see a group's
  full history, even when groups are subsampled.

## Lanes

Each lane is a separate measurement. A lane's numbers are never summed with another's.

- **compression**: replays every eligible tool output through the production compression path
  and estimates tokens removed, re-sends until the next compaction, cache-aware cost-weighted
  savings, and a proxy regret rate (the agent later used distinctive words, a command re-run or
  a file re-read that only the trimmed part could have served). Every trim is watched for
  signature regret, as in the live system.
- **sim**: the shipped learner against the packaged defaults on the same pass, using the engine
  evidence fold only (no fact mining, no pruning). It measures the learning keys and counts
  breaker trips.
- **facts**: transcript-only mining statistics for repository facts (candidates, contradiction
  rate, an upper bound on the waste targeted). It does not simulate activation. Contradictions
  are credited only within one split; candidates mined in dev and contradicted in test are
  reported separately.
- **loopguard** and **prune**: risk measurements for M3 features. They inform separate
  maintainer rulings only (see the next section on M3 keys).

The reference in every comparison is the **packaged defaults**: the user config file, the
project file and environment overrides are ignored in both roles, so a candidate is the packaged
defaults plus an overlay and nothing else.

## Pre-declared grids

`evals/hillclimb/space.toml` lists, for each tunable, the only values it may take. It was
committed before any real-data run, so a choice is a selection from a fixed menu, never a fitted
number. Each grid contains the current default, holds few values and lies inside the owning
module's clamp. Refit priors are snapped to a coarse grid. Keys that no lane measures are kept
in the file labelled `unmeasured`; they are skipped, reported as `unmeasured` and never changed.
All M3 keys (facts, pruning, loop guard) are of this kind: **the harness never changes an M3
key.** M3 lane results may only inform a separate maintainer ruling, which may move an M3 default
only toward less intervention and is recorded without figures.
Relevance weights are code constants and out of scope. The learner's regret constraint (`[learn.tau]`)
and the off-policy settings (`[ope]`) are never searched.

## Dev search

Coordinate ascent over the grids in file order, with successive halving over project groups:
a seeded subsample screens the values, the better half is re-measured on a larger one, and the
best on the full dev set. Each candidate value must first pass the fact-preservation suite
(below). The objective is cost-weighted net savings (saved tokens minus the recovery reads the
replay charges), taken from one lane per key and never mixed across lanes. Dev may be evaluated
many times; it yields exactly one joint proposal containing only the keys that moved, including
binary choices.

Minimum evidence applies to every evaluated point, on dev and on test: ≥ 8 project groups
with a trim and ≥ 30 trims. Below that the verdict is `insufficient`: on dev the
coordinate stays, on test the proposal is rejected. On dev a learning-key candidate is also
inadmissible if its breaker trips exceed the reference engine's.

## One look at test, all or nothing

The test split is read once. The one-shot ledger `<state root>/hillclimb/confirm-m4.json` (a
fixed location, independent of the output directory) is created atomically before any test data is
read, and any second confirmation attempt is refused. A deleted ledger cannot be detected, so any
second look at test invalidates the result. The confirmation is one paired `sim` lane run. The proposal is compared with the current
defaults, paired by project group, with a seeded bootstrap and a 95 % percentile interval. The
proposal is accepted **only if all** of the following hold; otherwise every default stays
unchanged and the rejection is recorded:

a) the lower bound of the net-savings difference is above zero;
b) for every point, the upper bound of the difference in proxy regret per trim is at most 0.2 · τ
   of that point (non-inferiority against the defaults, so the proxy's biases cancel), and the
   absolute proxy regret per trim is at most τ;
c) the fact-preservation suite stays at 100 %, including the pre-trim level, and its inline rate
   (needles visible in the output itself, not only reachable through the recovery marker) is not
   below the defaults';
d) the evidence minimum above is met.


## Accepted changes and provenance

An accepted change is committed with the first twelve hex digits of the confirming report's id
and the sentence "selected on maintainer E1 dev split, private report <id>"; a commit message contains no figure.
The report itself stays private (spec §7.9, §10.1, §10.6).

## Out of this campaign

Output-style directives (§6.6) need the live benchmark and move to M5. The linear contextual
bandit challenger (§7.8) exists only as an offline comparison and is not wired into any hook.
Model-tier prior sharing is deferred, as are the relevance weights and any live (E4)
confirmation.

## Outcome

Qualitative verdict only; the private report stays private.

- The dev-split search over the pre-declared grids produced an empty joint proposal. Learning
  related coordinates lacked enough evidence (below the minimum project groups and trims), and no
  compression value passed the dev admissibility rules (non-inferior proxy regret, a significant
  net-savings gain, and the E2 and inline-rate gates).
- Because the proposal was empty, the one-shot test-split confirmation was deliberately not used.
  It remains available for a future campaign with more data. Parameters are unchanged: the shipped
  defaults are frozen as they are for the M4 tag.
- The M3 lane results (facts, loop guard, pruning) were below their pre-declared evidence
  minimums, so no M3 default was changed.
- Off-policy evaluation on the dogfooding decision logs had no evaluable decision points and is
  recorded as insufficient.
- Main limitation: Claude Code keeps local transcripts for about thirty days by default, so the
  available history is short. The method is designed to say "not enough evidence" rather than tune
  on noise. Future campaigns can rerun the same harness as history accumulates.
