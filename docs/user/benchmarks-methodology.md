# Benchmarks and methodology

token-sparen's north star is to maximise tokens saved subject to no measurable quality loss. Quality is a hard constraint, never traded in a weighted sum. This page describes how that is measured and what may be claimed; it contains no results outside the Evidence block.

## Evidence

<!-- evidence -->
No published results are included in this source. The release build fills this block from the public E4 evidence file; until a release has one, no savings are claimed.
<!-- /evidence -->


## The four evaluations

| Id | What | Data | Where |
|---|---|---|---|
| E1 | Offline replay of transcripts through the same compression path the hook uses | the maintainer's own local sessions (private, never committed) or yours | `token-sparen replay` |
| E2 | Fact preservation: compressed output must still contain every needle fact | a public synthetic corpus, deterministic, run in CI | `evals/e2/run.py --strict`, must stay at 100 % |
| E3 | Off-policy evaluation of logged decisions (IPS, self-normalised IPS, doubly robust, with bootstrap intervals) | decision logs | `token-sparen ope` |
| E4 | Live benchmark of Claude Code on real tasks, plugin arm against a plain Claude Code arm, graded by the official grader | a public software-engineering task set (SWE-bench Verified Mini, extended with SWE-bench Verified tasks when the power step needs more) | published as the Evidence block |

E1 and E3 are private by design: figures derived from anyone's sessions are never committed or published, and results are cited by report id only.

## Claims policy

- A number may appear in the README, site or release notes only if it comes from a public E4 result for the plugin version being released (named by version and plugin hash).
- Comparisons are paired per task. Cost is reported as a paired bootstrap 95 % confidence interval of the median ratio. Quality is judged by non-inferiority of the resolved rate with a margin of −5 pp; the release gate requires non-inferiority at that margin. A result that cannot show it is reported as failed or underpowered, with its intervals; it is never rounded up.
- A result is published only with its interval, task count, seeds, model and date, and it measures the released plugin version (its hash is recorded).
- The E4 harness (task pool, arms, prompt, isolation and the evidence schema) lives in [`bench/`](bench/e4.py) and its arms are declared in [`bench/arms.toml`](bench/arms.toml); the evidence file is `docs/evidence/e4.json` and the README and site render their Evidence block only from it.
- A release passes only if the evidence exists, its plugin hash equals the hash of the tree being released and its gate result is `passed`. The one override is an `underpowered` result together with an owner-written `docs/release/gate-decision.toml`; a `failed` or missing result can never be overridden, and the README and site then state the underpowered result with its intervals.
- Tuned parameters are chosen offline with the hill-climbing harness: grids declared in advance, one look at a held-out split, accept all or nothing, with the method in [`docs/reports/m4-method.md`](docs/reports/m4-method.md). Test windows are recorded and never reused.
- Pages and README never state a savings, quality or cost result outside the Evidence block; this is enforced by a test.

## How E4 runs

The harness in `bench/` is driven by `python -m bench` (method:
[`docs/reports/m5-method.md`](docs/reports/m5-method.md)).

- **Stages.** A screening stage compares every arm (plain Claude Code, a concise-prompt control,
  token-sparen, its output-style variant, a cold-start ablation and the competitors in their
  documented defaults) on the Mini tasks; it is descriptive only. A power step then sizes the
  confirmation from the screening data, so that a plugin with no true quality difference passes
  the −5 pp margin with high probability. The confirmation compares token-sparen with plain
  Claude Code (plus extra descriptive arms only while quota allows) on that many tasks, seed
  zero only. If no affordable size reaches the required power, the result is `underpowered`:
  not a failure, published with its intervals, and the owner decides about the release.
- **Isolation.** Each run happens in the task's own container with its own Claude Code
  configuration; the plugin under test is a read-only copy of a tagged commit whose hash is
  checked before every run. The only credential is a dedicated token, revoked after the
  campaign.
- **Grading.** The official SWE-bench harness grades every patch locally on the same image the
  agent ran in. A task whose reference patch does not pass twice is dropped before any agent run
  and listed in the report.
- **Statistics.** Quality: the confirmatory lower bound of the paired difference is the smaller
  of a task-clustered bootstrap bound and a paired score bound; tasks excluded for one arm are
  re-run, and any remaining excess of token-sparen exclusions counts against token-sparen. The
  conditional exact McNemar interval is published alongside. Cost: the paired BCa bootstrap 95 %
  interval of the median per-task ratio, from list prices with every token class.
- **Tamper evidence.** The confirmation's ledger is hash-chained and its chain heads are pushed
  to a public branch during the campaign; the release job checks them against server-side push
  receipts before a result can be released.
- **Reproducibility.** `python -m bench run --dry-run --campaign demo --out <folder outside any
  repository>` runs the whole pipeline on synthetic tasks with no Docker, no account and no
  credential; CI runs it on every change.


## Measuring it on your own sessions (offline, private)

`token-sparen replay <transcript files or dirs> --policy default|static|engine|<arm>[,...] --out <file>`
replays your past Claude Code transcripts through the same compression path the hook uses
(nothing is saved or learned; `default` and `engine` apply the quality gate above, `static` and
an arm name replay every eligible output) and estimates tokens removed, projected re-sends until the next compaction,
cache-aware cost-weighted savings and a proxy regret rate (the agent later used distinctive words
that only the trimmed part contained). `--policy` takes a comma-separated list to compare several
policies in one pass; `--split dev|test|all` and `--cutoff YYYY-MM-DD` split sessions by the
date of their own first record (for a resumed session: its first record that is not copied
history); `--seed` fixes the engine policy's sampling. A session that ran with the plugin on is
left out whole and counted: any of its lines holds a token-sparen marker (trim marker, loop-guard
note, any hook text), or any of its records is dated on or after `--dogfood-start` (default
2026-11-26, the maintainer's dogfooding start; set it to the day you installed the plugin). A
session and its subagent transcripts are left out together. Transcripts are grouped by project
directory (the top-level directory under the path you give); within a group they are replayed
oldest first and history a resumed session copies is counted once, before the split. Unreadable
lines (not UTF-8, not JSON, over 8 MB) are skipped and counted, never fatal.

`--jobs N` replays N groups at a time in separate processes (`0` = one per CPU; the default `1`
stays in-process); a group whose worker process dies (for example killed for memory) is retried
once on its own. While it runs, a checkpoint `<out>.checkpoint` (same privacy rules as the
report: mode 0600, outside any git work tree, aggregates and hashed keys only) records the
transcripts in scope with their sizes and is rewritten after each finished group; it is deleted
when a complete report is written. `--resume` continues an interrupted run from it and replays
exactly that snapshot: sessions added since are ignored and a transcript that grew is read only
up to its recorded size. A checkpoint of other options, another configuration or another
token-sparen version is refused. If groups still fail, the report is written but marked
incomplete, the checkpoint is kept for `--resume`, and the command exits 1. `--lanes
compression[,...]` picks the measurements (the compression replay always runs). `facts`
measures, from the transcripts alone (it never reads the projects or runs git), how often each
repository-fact kind would be mined, how often a later session contradicts it, the waste it
targets (an upper bound in tokens) and what its injected line would cost. `loopguard`
measures, per tool category and agent chain, how many error loops reach 2, 3 and 5 repeats, how
often a loop ends on its own (natural break rate), an upper bound of the tokens a perfect
redirect could save, and how often a trigger would fire on a loop that was already converging;
it may only move loop-guard defaults toward less intervention. `prune` simulates opt-in tool
pruning per project in memory only (no settings file, git or store is touched): the servers
seen, the prunes it would make, how often a pruned server was needed while pruned (tool call,
mention or tool search), lease restores and the tool-listing tokens saved; it may only move
pruning defaults toward less intervention. `sim` runs the learning engine itself over your past
sessions, oldest first per project, in a temporary state root deleted afterwards: it decides
every output the compression point is offered, is rewarded with the savings the live hook would
compute and with the replay's proxy regret, and reports its decisions, arm shares, proxy regret
per trim, net savings and circuit-breaker trips next to a reference engine on the packaged
defaults (same outputs, same random draws). Failures are recorded by error type only. `--quiet` prints only
`report: <path> id: <12 hex>` (the id is the first 12 hex digits of the report's sha256) and
then `status: complete` or `status: incomplete`, so a run can be cited without printing any
figure; `ope --quiet` prints the report line only.

The defaults themselves are tuned offline with the hill-climbing harness
(`evals/hillclimb/`; method in [`docs/reports/m4-method.md`](docs/reports/m4-method.md)): grids
declared in advance, one look at a held-out split, accept all or nothing. `--cutoff` and
`--dogfood-start` set the dev/test split (defaults 2026-11-08 and 2026-11-26), `--campaign ID`
labels the reports, and `--confirm proposal.toml` takes its dates from the dev report next to the
proposal. Every confirmed test window is recorded in one ledger under the state root
(`hillclimb/test-windows.json`), and a confirmation whose window overlaps a recorded one is
refused, whatever its campaign label. It reads transcripts locally, writes only private files
outside any git work tree, and prints no figures with `--quiet`; its results are cited by report
id only and never committed.

`token-sparen ope --log <decisions.jsonl...> --target static|engine-snapshot --out <file>`
estimates how another policy would have done on your logged decisions (IPS, self-normalised IPS,
doubly robust, with bootstrap intervals, clipped importance weights and the share of the target's
choices the log cannot support). Options: `--n-boot`, `--seed` and `--holdout-after YYYY-MM-DD`
(evaluate only decisions logged on or after that date). Savings and regret are estimated
separately, and the report gives a verdict on the quality constraint: regret **per trim**, judged
pessimistically (an unsettled trim counts as regret), must stay under the target. The verdict is
`constraint_ok` true or false, or null with status `insufficient` when the log is too small or
too weak (fewer than 60 logged trims, outcomes missing for too many trims, or too much of the
target's trimming that the log never tried). Pass every log file of the project, including the
rotated `decisions.jsonl.1`, so a decision and its outcome are found together. Logs live in
`~/.token-sparen/projects/<key>/`; `project_keys.json` maps a project to its `<key>`. Both read only what you point them at,
print aggregate numbers only (no paths, ids or text), and write a JSON report with mode 0600; the
report path must be outside any git work tree, so a report can never be committed by accident. (On Windows file modes do not apply: keep the report inside your user profile, whose
default permissions are private to you.)
While they run, error log lines keep only where an error happened, never its message.
