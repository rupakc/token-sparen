# M5 method: the live benchmark E4 and the release gate

This is the method of the live benchmark (E4, spec §10.4 and §10.5) as built in M5. It states the
design, never a result: results exist only in the published `docs/evidence/e4.json` and the
report rendered from it. The only numbers here are dates, section numbers and the design
constants of the plan (the margin of −5 pp, the 95 % intervals, the 80 % power target, the
weekly stop at 60 %, the five-hour stop at 85 %, the 3600 s wall timeout, the 80 turn and
80 task floors, the 50 Mini tasks of which 25 per repository, the 5 h anchor window, the 3
consecutive infrastructure failures of the breaker).

## Design decisions

- **Own runner, not Harbor** (the spec's named fallback). Harbor's `claude-code` agent has no
  `--plugin-dir` and no free flags, forces its own `CLAUDE_CONFIG_DIR`, uses `amd64`-only images and
  has no learner streams or quota windows. Harbor stays the candidate for E5.
- **Task pool.** SWE-bench Verified Mini first (50 tasks, 25 django and 25 sphinx), restricted
  to tasks with a prebuilt `arm64` evaluation image (decided by platform availability before
  any run and disclosed). The extended pool is every SWE-bench Verified task with such an image,
  drawn seeded and stratified by repository when the power step needs more tasks. No local image
  builds.
- **Local grading only** (owner, 2026-11-27). The official harness (`swebench 5.0.2`) grades each
  patch on the same `arm64` image the agent ran in. Before any agent run, each task's gold patch
  must resolve twice in separate harness runs and a no-op patch must not; a task that fails this
  oracle preflight is dropped and listed with its reason.
- **Frozen plugin per stage.** Screening freezes the tag `v0.5.0-screen1`, confirmation the tag
  `v1.0.0-rc1` (plain tag names only, neither matches the release pattern). The export is
  read-only and its plugin hash (`scripts/plugin_hash.py`) is re-verified before every run.
- **Isolation.** Every run has its own `CLAUDE_CONFIG_DIR` and `HOME`; the real Claude
  configuration is refused. The only credential is a dedicated OAuth token from
  `claude setup-token`, kept in a private file, passed to the container by name only, scrubbed
  from every subprocess inside it, and revoked after the campaign. Every run's output is scanned
  for the token before anything is recorded; a hit quarantines the run and stops the campaign.
- **Arms.** Plain Claude Code (`baseline`), a concise-prompt control, token-sparen as shipped
  (learning on, state carried per repository in commit order), its output-style variant, a
  cold-start ablation (screening only) and the competitors in their documented defaults
  (`bench/arms.toml`). An arm whose tooling does not demonstrably act in a run is an
  infrastructure failure for that run, never a graded outcome.

## Stages

1. **Screening.** The Mini tasks that passed the preflight, one seed, `--model sonnet` (the
   resolved id is recorded), effort high, every arm. Descriptive only.
2. **Power step.** From the screening pairs of token-sparen and baseline (seed 0), the share of
   discordant pairs gives a conservative flakiness model; simulation of the confirmatory rule,
   with exclusions at the screening rates, finds the smallest task count with at least 80 %
   power to pass the margin of −5 pp when the true difference is zero, never below 80 tasks.
   The required size beyond the pool, or beyond six weeks of quota at the weekly stop, is
   `underpowered`. Extra arms (the best competitor, then the output-style variant) join only
   while the projected quota and disk still fit.
3. **Confirmation.** Seed 0 only, Claude Code's default model at the first run (the resolved id
   is pinned), baseline and token-sparen plus any extra arm the power step allowed. The plan
   (tasks, arms, seed, model, Claude Code version, plugin hash, plugin version, the date the
   price table was confirmed against live pricing, the power verdict) is written into the ledger
   and hashed before the first run; every run record carries that hash.

## Runs, quota and resumption

Each task's arms run back to back in a seeded order; streams run in commit order per
repository. Typed outcomes: `ok` and `agent_fail` are graded; infrastructure failures (auth,
API, rate, Docker, proxy, load, leak, disk) are never graded, are retried within a bound and
leave the task's pairing when they stay; 3 consecutive counted failures trip the breaker. The
quota state is account-wide: no run starts when the owner's weekly reading is at or above 60 %
or the next run could cross it, or when the five-hour reading is at or above 85 % (then the
scheduler waits for the reset with `--wait`); at most four runs start per five-hour window until
calibration. The ledger is append-only, so a crash or restart resumes without re-running a final
run.

## Statistics (§10.5)

- **Quality (confirmatory).** Token-sparen against baseline only, seed 0 only: the lower bound
  is the minimum of a task-cluster percentile bootstrap and the Tango score interval for the
  paired difference of resolved rates; the gate passes only if it lies above −5 pp. Exclusions:
  one-sided exclusions are re-run first; afterwards only the net excess of token-sparen-only
  exclusions on tasks baseline resolved is imputed as token-sparen failures. The conditional
  exact McNemar interval is published alongside and labelled "conditional exact".
- **Cost.** Per run, `total_cost_usd` plus the plugin's own side calls at list price, every token
  class kept; the comparison is the paired BCa bootstrap 95 % interval of the median per-task
  ratio. Quota use is secondary.
- **Verdicts.** `passed` needs the bound above −5 pp, a `powered` plan and valid evidence (every
  run on the planned model, Claude Code version and plugin hash, token-sparen demonstrably live,
  E2 passing). A clearly inferior result (point below −5 pp and the upper bound below zero)
  fails whatever the power. An `underpowered` plan without that gives `underpowered`: not a
  failure, the owner decides in `docs/release/gate-decision.toml`. Everything other than the
  confirmatory comparison is descriptive.

## Tamper evidence

The confirmation ledger and its grades are hash-chained. The chain heads are anchored on the
public `bench-anchors` branch: the plan's anchor before any run, then one commit per push after
every graded block (at most four graded runs per publication), at least every 5 h while the
campaign runs, and once at the end. A server-side receipt workflow dates every push. The release
job fetches that branch and checks every publication against the final chains with the receipts;
a local gate has no receipts and is advisory only. The residual risk (records written after the
last publication of a window) is disclosed in the report.

## Publication

`python -m bench report --publish` writes `docs/evidence/e4.json` (confirmation) and the report
with its figures; figures are allowed only for these E4 results and are rendered from `e4.json`
alone. Publishing is refused without the live-pricing confirmation, with an advisory gate, with
a reduced bootstrap, with any run that differs from the plan, or when any file holds private
data. Every published number carries its interval, method, task count, seeds, model id, Claude
Code version, date range, plugin version, plugin hash and harness commit.

## Dry run

`python -m bench run --dry-run` runs the whole pipeline in CI with a fake backend, a fake
grader, synthetic tasks, a scripted quota in its own account directory, a virtual clock and a
temporary bare repository as the anchors remote. It needs no Docker, no `claude`, no grader, no
quota and no credential, and its output is never publishable.
