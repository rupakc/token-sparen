# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Pre-release (`0.1.0.dev0`). Covers milestones M0 (foundations), M1 (Claude Code plugin) and M2
(learning, offline evaluation, quality-first defaults and config safety). M3 is not started.

### Added
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
- M3 start: repository-fact sanitiser (facts are data, never instructions: allow-listed
  characters, no URLs, shell operators, instruction words, secrets or outside paths; fixed
  render templates) and deterministic miners run at fold time over the session transcript
  (command alias, required env var name, test command, build-first, generated dir) into
  `projects/<key>/facts.json` with session-id digests as evidence.
- Fact lifecycle and injection (M3): per fact a Beta posterior over "injecting prevents the
  waste it targets when relevant", a withheld baseline (propensity floor
  `[facts] withhold_floor = 0.1`, logged per session), candidate -> active -> retired
  (activation at lcb(0.1) > 0.55 or the 3-session bootstrap; retirement at ucb(0.9) < 0.4 or on
  contradiction, one for requires_env/build_first, two otherwise). Active facts are injected at
  SessionStart (startup, resume, clear, compact) as a labelled data block, relevance-ranked,
  at most 8 facts / 300 tokens, through the emit gate (deduplicated, counted as injected).
  `token-sparen facts list|forget|disable|enable`. A project file may only disable facts or
  lower their budget. The sanitiser also rejects `--passphrase` and joined
  `accesstoken`/`refreshtoken`/`clientsecret`/`privatekey` options with a value.
- CLI: `stats`, `show`, `run`, `doctor`, `session disable|enable`, `config set`,
  `statusline`, `extensions`, `fold`, `replay`, `ope`, `facts`; placeholder for `prune` (M3).
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

### Changed
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

### Security
- Extension trust is explicit and content-based; symlinked, foreign-owned and world-writable
  drop-ins are skipped; trust is documented as full code execution as the user.
- The slash command has least-privilege tool permissions.
- Settings are written only on opt-in statusline install, with a backup.
- Failing tool results and error lines are never elided.
