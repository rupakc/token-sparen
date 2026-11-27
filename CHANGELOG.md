# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Pre-release (`0.1.0.dev0`; nothing is published on PyPI yet). Covers milestones M0
(foundations), M1 (Claude Code plugin), M2 (learning, offline evaluation, quality-first defaults
and config safety) and M3 (cross-session learning). Milestones are tagged in git (`v0.3.0-m3`
for M3); the package version stays `0.1.0.dev0` until v1.0.

### M4 hill-climbing harness

- `evals/hillclimb/run.py`: searches the grids pre-declared in `evals/hillclimb/space.toml` on
  the E1 dev split only (coordinate ascent in file order, a second pass only if the first moved
  a key; per coordinate successive halving by project group: seeded 25 % sample, top half on
  50 %, best on 100 %) and writes one joint `proposal.toml`. Objective: cost-weighted net
  savings of one lane, never summed across lanes (frozen-engine compression replay per project
  group for static keys, the `sim` lane for `learn.savings_reward`,
  `learn.ladder_retry_decisions` and `learn.defaults.*`). A value is admissible only under the
  acceptance rule (paired bootstrap by project group, 2000 seeded resamples: net-savings lower
  bound > 0, proxy regret per trim non-inferior to the defaults within 0.2 tau and at most tau,
  E2 100 % with `inline_rate` per level not below the defaults', sim breaker trips not above the
  reference engine's).
- `--confirm proposal.toml`: evaluates the joint proposal against the defaults once on the test
  split (one paired `sim` run), all or nothing; one-shot per plan (a private ledger in `--out`
  refuses every later confirmation); only grid values are accepted.
- `space.toml`: every grid includes the current default, holds at most 5 values and is checked
  against the owning module's clamp before any run; `[learn.tau]` and `[ope]` are never
  searched; M3 keys (facts, pruning, loop guard) only toward less intervention.
- Private files only (`trail.jsonl`, `proposal.toml`, `dev.json`, `confirm.json`, mode 0600,
  outside any git work tree; `report.write_private(..., raw=True)` writes text files);
  `--quiet` prints only the report path and id, `status:` and `<key>: accepted|rejected|unchanged`.
- `hill-climb` skill and `eval-hillclimber` agent: commit messages cite the private report id,
  never a figure.

### M4 simulation lane and offline LinTS challenger

- E2 eval: a `pretrim` pass (the `guards.pretrim_level` arm on Bash-output cases) gates `guards.*` keys too; `run(levels, params)` and `--params OVERLAY.toml` evaluate candidate parameters (a temporary user config in the isolated home; unknown or mistyped keys raise `ValueError`); the report gains `inline_rate` per level, the share of needles visible in the compressed text rather than only via the recovery file.

- `replay --lanes sim`: runs the live learning engine chronologically over past sessions per
  project group, in a temporary state root deleted afterwards (its own `HOME` and
  `TOKEN_SPAREN_HOME`, passed explicitly; no fact miner, settings writer or git call): decide on
  every output the compression point is offered, savings reward from the tokens the chosen arm
  removes on that output (the hook's `[learn] savings_reward` mode, re-sends = the trim's actual
  later API calls before the next compaction, delivered once known), the replay's proxy regret
  delivered once when it settles (also as a circuit-breaker outcome), and an engine-only session
  fold (`mine_facts=False, prune=False`). Both roles use the packaged defaults only (the
  run's user, project and env config is ignored): the candidate adds the lane's `overlay`, the
  reference engine (current defaults) runs in the same pass on the same outputs with the same random draws, so
  proposals compare paired per project group. Per split and group: decisions, arm share and
  proxy regret per trim by epoch (thirds), tokens removed, net savings (minus the compression
  lane's recovery-read estimate), breaker trips; `sim_lane.summary` / `sim_lane.pairs` render
  them. Same seed, same report; parallel equals serial.
- `params["chooser_factory"]` (`"module:callable"`) swaps the candidate learner; the offline
  constrained LinTS challenger (`evals/challengers/lints.py`, two Sherman-Morrison linear
  posteriors per arm over a 32-wide one-hot context, the engine's regret constraint) runs
  through it with `evals/challengers/lints_sim.py`. Never wired to hooks; the package never
  imports `evals`.
- The compression replay gained no-op learner hooks (`_trimmed`, `_untrimmed`, `_settled`).
- Fix (compression lane): every trim is now watched for a re-run of its command / re-read of its
  file, as the live detector tracks every trim; before, a trim whose elided span held fewer than
  2 distinctive tokens could never be regretted (`proxy_regrets` can rise; `watched_spans` still
  counts the trims with distinctive tokens).

### M4 engine options (default off)

- `[learn] savings_reward`: `"scaled"` (default, unchanged), `"fraction"` (removed / output
  tokens), `"counterfactual"` (removed / the largest removal over the point's trimming arms on
  the same output, times `min(1, resends / learn.resend_horizon)`, default horizon 20; the other
  arms are compressed only in this mode, hook p95 gated by `tests/perf/test_counterfactual_reward.py`)
  and `"cost"` (cache-weighted cost removed with the `[pricing]` ratios, scaled by its own p95).
- `[learn] ladder_retry_decisions` (default 0 = off): after N decisions of a bucket whose next
  harsher arm is locked only because the safer arm is not yet established, that arm competes
  for one decision; the logged propensity stays exact.
- `[guards] pretrim_bounded_keep_ratio` (no default: unset follows the standard arm's keep
  ratio, as before; any number, 0.35 included, is used as is); a project may only raise it
  (compared with the standard ratio while unset), and never below the user's own ratio.
- `overlay.fold(..., mine_facts=True, prune=True)`: false folds engine evidence only (offline
  simulation).
- Fix (on by default): the governor and the `band` feature use a per-session 1M-window latch.
  Without the statusline sensor (or another authoritative window), SessionStart's model id with
  the `[1m]` marker (kept in the session overlay; SessionStart is the only hook input with
  `model`) or any call above 200k tokens latches a 1M window for the rest of the session,
  compactions included, so a 1M-context session is no longer banded `unk` or too full and gets
  no false context notices; replay uses the same latch per transcript.

### M4 E1 at real-data scale

- `token-sparen replay` runs in parallel and resumes: `--jobs N` (one project group per worker
  process, `0` = one per CPU), a checkpoint next to the report rewritten after each finished
  group and `--resume` to continue an interrupted run, `--lanes` (the compression replay is now
  the `compression` lane; later lanes plug into `token_sparen/replay/runner.py`), and `--quiet`
  (prints only `report: <path> id: <12 hex>`; `ope --quiet` too).
- Contamination-safe splits: sessions are ordered by their first timestamp within a project
  group and copied history (resumed sessions) is de-duplicated over the whole group before the
  dev/test split; sessions from 2026-11-26 on or with a `[token-sparen:` marker in a tool result
  are excluded whole and counted.
- A resumed session is dated by its own first new record (not its copied history); a session
  is excluded when any record is dated from the dogfood start on or any line holds the marker
  (hook context included), together with its subagent transcripts; groups are project
  directories. A dead worker's groups are retried once alone; still failing, the report is
  marked incomplete, the checkpoint is kept and the command exits 1 (`--quiet` adds
  `status: complete|incomplete`). `--resume` replays the first run's snapshot of transcripts
  and sizes, and refuses a checkpoint of another config or package version.
- Malformed transcripts never stop a run: lines over 8 MB are skipped in bounded chunks, lines
  that are not UTF-8, not JSON or not an object are skipped, and both are counted; an exception
  in a group is recorded as its type name only.
- `replay --lanes facts`: per split and fact kind, the candidates the production miners would
  mine, how many a later session of the same split contradicts (wrong-fact risk; dev facts
  contradicted in test are reported apart), the waste events and an upper bound of the waste
  tokens each kind targets (only calls showing that waste, never red runs of the right test
  command), and the tokens its injected line costs. A split's figures never depend on the
  other split. Past
  sessions had no injections, so no effect is simulated. The miners and the lifecycle observer
  gain `evidence="transcript"`: no project file read and no git (downloader aliases that need
  the manifest are skipped and counted; generated-directory evidence comes from file heads the
  session showed).
- `replay --lanes loopguard`: per split, tool category and agent chain (subagent transcripts
  apart), the redirects each arm (k = 2, 3, 5) would send (one per agent and turn, error loop
  or edit oscillation, with the production fingerprint), the natural break rate sampled as the
  live gate's p0 (the first loop at its third repeat per agent and turn, not recurring within 5
  calls), an upper bound of the tokens after each trigger until the loop stopped, and the
  triggers followed within 5 calls by a success of the same command head or file (converging
  loops); every-loop counts are kept as diagnostics. `loopguard.reversals`, `recurs_within` and `tool_target` are now public pure
  helpers that the live guard calls (behaviour unchanged).
- `replay --lanes prune`: simulates opt-in MCP tool pruning per project over past sessions in
  memory only (the production `observe_session` / `plan` gates and lease; no settings writer, git
  or store): per split the servers seen, simulated prunes, sessions run with a prune, needs while
  pruned (tool call, user mention or ToolSearch, by kind), lease restores, the tool-name listing
  tokens saved and sessions without a tool listing (`unobservable_sessions`, never guessed);
  rules pruned in dev and needed in test are reported under `*_cross_split` keys. The runner
  shows raw records to lanes with `feed_record`; `transcript.add_inventory_record` is the
  per-record fold of `tool_inventory` (behaviour unchanged).

### M4 triage (quality-relevant fixes before tuning)

- Loop guard: a test report's fingerprint also carries its summary's failing total (`N failed`,
  `N errors`, jest `Tests: N failed`, cargo `N failed;`; unittest `FAILED (failures=N)` for other
  failures), so fixing one test of a suite with more failures than the scanned text holds no
  longer looks like the same error repeating.
- Loop guard: the prompt-side settlement is always-run and runs first in the UserPromptSubmit
  chain, so a "stop" after a redirect settles regret 1 even when another prompt handler
  overruns the latency budget.
- Loop guard: the redirect's state, its injected-tokens cost, the decision record and the
  regret detector's loop signal are written only after the hook's response reached Claude Code
  (new `token_sparen/deferred.py`); a hook answered with the watchdog's no-op records none of
  them. The redirect is reserved under the overlay lock first, so parallel failing calls of one
  agent still get at most one redirect per turn; an unconfirmed reservation is never re-sent.
- Compression: a stack-frame head is an error line only when it matches outside its file
  location, so library frames such as `requests/exceptions.py` can be folded again (E2 stays
  100 %).
- Docs: params path `token_sparen/params/defaults.toml` in agent and skill docs; README "Verify"
  has three steps; M3 heading wording.

### M3: cross-session learning (milestone 0.3.0; package version 0.1.0.dev0 until v1.0)

Three mechanisms that learn across sessions of one project, each with its own off switch:
repository facts (`token-sparen facts disable`, `[facts] enabled = false`), opt-in tool pruning
(off unless `token-sparen prune enable`; `prune disable` reverts every rule) and the failure-loop
guard (`[loopguard] enabled = false`). `TOKEN_SPAREN_DISABLE=1` turns all of them off with
everything else. No savings figures are claimed for them yet.

#### Added
- Repository-fact sanitiser (facts are data, never instructions: allow-listed
  characters, no URLs, shell operators, instruction words, secrets or outside paths; fixed
  render templates) and deterministic miners run at fold time over the session transcript
  (command alias, required env var name, test command, build-first, generated dir) into
  `projects/<key>/facts.json` with session-id digests as evidence.
- Fact lifecycle and injection (M3): per fact a Beta posterior over "injecting prevents the
  waste it targets when relevant", a withheld baseline (propensity floor
  `[facts] withhold_floor = 0.1`, logged per session), candidate -> active -> retired
  (activation at lcb(0.1) > 0.55 or the 3-session bootstrap; retirement at ucb(0.9) < 0.4 or on
  contradiction, one for requires_env/build_first, two otherwise). Active facts are injected at
  SessionStart (startup, resume, clear, compact) as a labelled data block, recency-ranked,
  at most 8 facts / 300 tokens, through the emit gate (deduplicated, counted as injected).
  `token-sparen facts list|forget|disable|enable`. A project file may only disable facts or
  lower their budget. The sanitiser also rejects `--passphrase` and joined
  `accesstoken`/`refreshtoken`/`clientsecret`/`privatekey` options with a value.
- Fact lifecycle hardening: neutral past-tense fact lines; a test command earns credit only when
  its own runner succeeds and is contradicted when another runner dominates; one active test
  command at a time; alias / build-first facts retire after two sessions that did not need
  them; retirement when the withheld baseline does as well; test-command and generated-dir
  facts need 5 sessions plus an observed waste event and go stale (10 relevant sessions or 30
  days); resume injects only facts not yet shown; a failed mining step keeps the session's
  facts log; more glued credential option names rejected. Test runners are compared by
  normalised program (`python -m pytest` = `uv run pytest` = `pytest`; npm/pnpm/yarn test =
  one test script), red runs are waste not contradictions, and only a runner that replaced
  the fact's (>= 2 runs while it never ran) contradicts it. Any option whose name ends with a
  credential word (`--dbpassword`, `--sshkey`, `--npmtoken`) or contains `bearer` is treated
  as a secret.
- Opt-in learned tool pruning (`token-sparen prune status|enable|disable|restore <tool>|restore
  --all`): MCP servers none of whose tools was ever used are denied in the checkout's
  `.claude/settings.local.json` after >= 3 sessions / 15 prompts and a high-tier regret bound,
  only while the project is idle (session starts, prompts and tool calls count as activity).
  Savings are modest (unused servers' tool names and instructions) and reported as a count only.
  Built-ins only with `[prune] builtins = true` in the user's config.toml and an SDK init record.
  Enabling needs `prune enable` or the user's config.toml (never env or a project file) and seeds
  tools used in the last 60 days of transcripts. Owned rules only, tracked per settings-file real
  path (sibling clones and worktrees), one backup, stamp re-check, strict JSON (duplicate keys
  refused, with a notice), tracked files refused, `.gitignore` warning. SessionStart line with
  the restore command (plugin launcher when needed; `prune restore` pre-approved in the skill),
  truncated to its own budget; immediate restore (regret 1.0, kept for good) on a ToolSearch
  query, a direct call, a `PermissionDenied` event, a user prompt naming it (exact or multi-word)
  or a fold-time attempt. `⊘ N` statusline segment and `stats` count.
- Failure-loop guard (spec 6.11, decision point `loopguard`, medium tier): within one turn and
  per agent (`agent_id`), the same failure fingerprint (tool + command head or file + most
  specific error line; for pytest/jest/go test/cargo test reports a hash of the whole sorted failing-test id set and its size
  plus the first failure's error line; bare `Exit code N` failures and test-run summary lines
  never count) `k` times (learned
  arm: patient 5, standard 3, eager 2; safe arm patient), or the same edit made and undone twice
  (A -> B -> A -> B), adds one redirect line as `additionalContext` (PostToolUse and
  PostToolUseFailure; the tool output is never touched), with its own wording for edits; at most
  one per agent and turn, a second detection is a terminal notice only. The decision is logged at
  the first repeat for every arm. Reward: savings 1 only for a redirect delivered at >= 3
  occurrences after which the error does not recur in the agent's next 5 tool calls, else 0;
  regret 1 on a user interrupt or a next prompt saying stop/undo/revert/that's wrong/no, don't.
  Error-loop triggers also flag this turn's error-bearing compressions to the regret detector
  (0.5). `[loopguard] enabled`, `min_k` (a project may only switch it off or raise it),
  `watch_calls`. Bounded per-turn state (hashes only) in the session overlay.

#### Fixed
- A not-yet-created `TOKEN_SPAREN_HOME` under a root-owned sticky directory such as `/tmp` is
  accepted (the nearest existing parent may be ours or root-owned with the sticky bit).
- With `TOKEN_SPAREN_DISABLE=1` a hook no longer writes its sampled line to
  `logs/latency.jsonl`: the kill switch now writes nothing at all.
- Settings writes now detect a concurrent same-size edit (within mtime resolution) by a content
  digest in the pre-replace stamp, instead of overwriting it.
- Tool pruning: a process killed between the settings write and its own record no longer
  leaves a deny rule nothing owns. The rules are journaled in `prune.json` before the write and
  reconciled on every read, so hooks, `restore --all` and `prune disable` still remove them.
- Tool pruning writes new deny rules only from the background fold that runs at session end
  (effective from the next session), never at a session start; git is asked before the state
  locks are taken, so an in-session restore never waits on git.
- Tool pruning writes `.claude/settings.local.json` only when git confirms it is untracked and
  ignored; any other git answer (exit 128 such as "dubious ownership", a corrupt index,
  `GIT_DIR`/`GIT_INDEX_FILE` set, not a work tree) counts as exposed.
- Ownership of tool-pruning deny rules is kept per settings file in `~/.token-sparen/
  prune_rules.json`, so a changed git remote (new project key) never orphans a rule; older
  per-project records are migrated on read. A per-project opt-in is not carried to the new key:
  the next fold restores those rules and says so.
- Tool pruning refuses to write a settings file that is a symbolic link (or inside a linked
  `.claude` folder), whose target git cannot vouch for.
- Tool pruning no longer grows confident from its own intervention: a pruned server collects no
  evidence, and each prune is a lease (`[prune] lease_sessions`, default 15; a project may only
  lower it) after which the server is restored and pruned again only on fresh evidence.
- Tool pruning refreshes its backup when it starts with no rule of its own in the file, and a
  restore after an interrupted write also removes the containers that write created.
- The prune restore message no longer claims the tool is available mid-session.
- Repository facts: a directory is labelled generated only when git ignores it or the edited
  files carry a generated marker (`DO NOT EDIT`, `@generated`, ...) in their first 5 lines;
  hand-edited files are no longer evidence, and a tracked directory without markers retires
  the fact.
- Repository facts: the test command is stored as the runner invocation only (`pytest`,
  `npm test`, `go test ./...`, ...), never a focused run with paths, test ids or options such
  as `-k` or `--no-cov`; stored narrowed commands are re-normalised or retired.
- Repository facts: sessions that only confirm a fact is true no longer count as evidence that
  injecting it helps (separate `truth` count for activation); after 8 injected sessions a fact
  that is not clearly helping is retired.
- Repository facts: a build step is no longer credited when an edit or a lint / type-check ran
  between the failing and the passing test; in-project absolute paths are stored
  project-relative instead of being dropped.
- Repository facts: a generated-directory fact learned from edits alone is no longer injected
  and is retired unless git or a generated marker confirms it; a test command run from a
  subdirectory or for one package (`cd tests/unit && pytest`, `cargo test -p core`) is no
  longer learned, nor one that picks a config or root (`-c`, `--config`, `--rootDir`); watch-mode
  stoppers such as `--watchAll=false` and `CI=true` are kept (`--run` for vitest only).
- Failure-loop guard: learning no longer drifts toward nudging early. Only a delivered redirect
  is observed (loop broken or not within the next 5 calls; a decision that never redirected adds
  no evidence), and an earlier-nudging setting is used only once its break rate is clearly
  above the rate at which loops end by themselves (measured on turns with no redirect, per
  tool category and project with a global fallback, assumed 80 % until measured); otherwise the guard stays at the
  most patient setting, exploring the next one in about 5 % of decisions.
- Failure-loop guard: a stop phrase (now also "wait") counts as regret only at the start of the
  next prompt or in a prompt of at most 8 words ("add a stop button ..." no longer counts).

#### Security
- `TOKEN_SPAREN_HOME` is ignored (default state root used, one line in `errors.jsonl`) when it
  points inside the current project or its git work tree, or (POSIX) at a path not owned by the
  user: a cloned repository's committed settings `env` can no longer plant "observed" facts, a
  config that enables pruning, or other state. "Inside" is decided by file identity, so a case
  variant (case-insensitive file systems), `..` spelling or directory alias does not bypass it;
  the rejection is logged once. The statusline launcher applies the same rule to
  `TOKEN_SPAREN_ROOT`.
- Tool pruning is the second settings write after the statusline: it needs an explicit opt-in
  (`prune enable` or the user's own `config.toml`; never an environment variable or a project
  file), touches only `permissions.deny` in the checkout's `.claude/settings.local.json`, keeps
  one backup and reverts exactly the rules it wrote.

#### Tests
- End-to-end cross-session tests through the real hook entry point and `token-sparen fold`
  (`tests/integration/test_cross_session.py`): a missing-command fact learned over three sessions
  and injected at the fourth session start, the full pruning cycle (opt-in, evidence, idle gap,
  deny rule, restore on attempted use), the loop guard (one redirect at the third identical
  failing test run, tool output untouched, rewarded at the next prompt) and the kill switch (no
  output and no file written by any hook).

#### Documentation
- The design spec's M3 sections (6.8, 6.10, 6.11, 7.5-7.7) now describe the shipped code; the
  optional LLM fact proposer is marked deferred and pruning for hosts other than Claude Code is
  scoped to v1.1. README lists every `[prune]` default and what a project file may change
  (`prune.allow` off only, `prune.lease_sessions` lower only). Drift report:
  `docs/reports/drift-m3.md`.

### M0 to M2

#### Added
- Repository bootstrap: package skeleton, CLI `--version`, CI, deny-list check, MIT licence and
  NOTICE.
- Core: canonical events and actions, layered config (defaults, user, project, environment,
  per-session), token estimation, transcript usage parser, atomic file-locked JSON/JSONL store.
- Recoverable compression: full output saved to `<project>/.token-sparen/outputs/<id>.txt`
  (git-ignored, 14-day TTL, size caps) and a `[token-sparen: kept X of Y lines ...]` marker.
- Compressors with deterministic golden tests: text (head/tail, error pinning, repeated-line
  folding, prompt-relevance), JSON, diff, stack trace, test report, compiler, build log, search,
  Perplexity-style results and logs, with a content-type router.
- E2 fact-preservation suite (51 cases at 3 compression levels, 153 case-level runs) run in CI.
- Claude Code plugin: fail-open hook entry (`hooks/run.py`), manifest, `/token-sparen` skill.
- Mechanisms: compression on PostToolUse (including structured Agent/Task results), a
  tail-biased pre-trim arm, PreToolUse guards for unbounded scans, read de-duplication, budget
  governor with burn projection and notices, opt-in hard stop, task-boundary `/compact` advice,
  PreCompact recovery-path preservation, subagent brief arm (off by default).
- Live statusline (opt-in install/uninstall that composes with an existing statusline, backs up
  settings and restores the original exactly).
- CLI: `stats`, `show`, `run`, `doctor`, `session disable|enable`, `config set`,
  `statusline`, `extensions`, `fold`, `replay`, `ope`, `facts`, `prune`.
- Trusted extension loading (entry points and content-hash-trusted drop-ins), a compressor
  conformance kit and a versioned extension interface (`EXTENSION_API_VERSION = "1.0"`).
- Per-session config overrides (`config set`) and a per-session kill switch that finds the
  project's last active session.
- Learning (M2): `token-sparen fold [--session ID] [--all]` merges a finished
  session's evidence into the project store (and coarse back-off buckets), contributes 0.3 x the
  delta to the global store (halved after 90 untouched days), learns pre-trim candidates, and runs
  a per-project circuit breaker that pauses a decision point after repeated regret. SessionEnd
  spawns the fold detached; SessionStart folds left-over sessions. The statusline `learn` segment
  shows `cold`, `adapting`, `exploiting` or `safe-mode`.
- Learning engine drives every hook decision point (compression, pre-trim, read de-dupe, subagent
  brief): built lazily per hook, falls back to the static policy on any engine/store error, and
  `[learn] enabled = false` restores the fixed defaults. Decisions use the shared context buckets
  (context fill band from the governor, tool category, size, content type, task phase). New
  projects start with light compression and unlock stronger levels only after the lighter one has
  been observed at least 20 times with a low regret estimate (experience from other projects
  counts). `[learn.hold]` keeps a point at its safe arm: the subagent brief by default, and
  `compression`, `pretrim` and `read_dedupe` on request.
- Regret detection (recovery reads, re-runs, asking for elided content, failure loops) with a
  settled regret value per trim; each settlement is appended to `decisions.jsonl` as an outcome
  line (so the log rotates sooner), and the fold backfills any the live hook could not write.
- `token-sparen replay` (E1): replays your own Claude Code transcripts offline through the
  production compression path and writes a private aggregate report (policies `default`,
  `static`, `engine` or an arm, comma-separated lists, dev/test split by date).
- `token-sparen ope` (E3): off-policy evaluation of logged decisions (IPS, SNIPS, doubly robust,
  bootstrap intervals). It estimates savings and regret separately and reports a quality
  verdict on regret per trim, judged pessimistically and `insufficient` when the log is small
  or does not cover the target (`[ope]` thresholds are user-only). Reports are written with mode
  0600 and only outside a git work tree; never commit them.
- `token-sparen stats` reports net savings (gross minus recovery reads minus injected tokens),
  as the statusline does, with the gross figure and its parts in `--json`.
- Config safety: the project file `token-sparen.toml` is deny-by-default and may only make
  token-sparen intervene less or keep more; ignored values are reported once
  (`config_safety_warned.json`). Every boolean setting accepts `true/1/yes/on` and
  `false/0/no/off`, including `TOKEN_SPAREN_DISABLE`.
- Requires Claude Code >= 2.1.121 (`engines` in the plugin manifest; tested with 2.1.332).

#### Changed
- Quality-first compression defaults (maintainer decision after replaying real sessions; check
  it on your own with `token-sparen replay --policy static` against `default`): file
  contents (the Read tool and shell commands that print a file, such as `cat`, `head`, `tail`,
  `sed -n`, `git show <rev>:<path>`) are never trimmed, and only diff, search, test report,
  compiler, build log, JSON, `pplx` and log output is offered for compression; plain text,
  tables and stack traces are left whole. Widen with `[compression] trim_file_contents = true`
  and `[compression] allow_ctypes` in your own config (a project file may only narrow them).
- `token-sparen replay --policy default` replays the production gate; a re-run no longer counts
  as a recovery cost; recovery cost is reported per regret signal.
- `git show <commit>`, `git log -p` and `git stash show -p` output is recognised as a diff (and
  so offered for compression); `git show <rev>:<path>` stays file content and is never trimmed.
- `[learn.hold] compression = true` now stops every output trim: it also holds the learned
  pre-trim and applies when learning is off or the engine fails. `allow_ctypes = []` is the
  config-only switch. `[learn] enabled = false` still uses the fixed standard compression. The command-family
  classifier is shared with the hook and handles `source`/`export`/`echo` prefixes, `timeout`,
  `sudo -u`, `bash -c`, `python3.X -m`, `yarn jest`, `pnpm exec` and `xargs cat`.
- Compression never returns more than the model would have seen and enforces the 2000-token floor
  (a lower `compression.min_tokens` is ignored with a one-time warning).
- Mechanisms are host-neutral: tool-output shapes live behind the host adapter.
- Pre-trim is a PostToolUse compression arm (inert until M2 learns candidates); commands are never rewritten.
- `/token-sparen` is a plugin skill with only read-only subcommands pre-approved.
- The hard stop also covers Grep, Glob, LS and MCP tools.
- `doctor` suggests `py -3`/`python` on Windows when `python3` is missing.
- All text I/O is explicit UTF-8; `run` output is byte-exact.

#### Security
- Extension trust is explicit and content-based; symlinked, foreign-owned and world-writable
  drop-ins are skipped; trust is documented as full code execution as the user.
- The slash command has least-privilege tool permissions.
- Settings are written only on opt-in statusline install, with a backup.
- Failing tool results and error lines are never elided.
