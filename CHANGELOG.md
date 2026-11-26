# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Pre-release (`0.1.0.dev0`). Covers milestones M0 (foundations) and M1 (Claude Code plugin).

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
- E2 fact-preservation suite (153 cases) run in CI.
- Claude Code plugin: fail-open hook entry (`hooks/run.py`), manifest, `/token-sparen` skill.
- Mechanisms: compression on PostToolUse (including structured Agent/Task results), a
  tail-biased pre-trim arm, PreToolUse guards for unbounded scans, read de-duplication, budget
  governor with burn projection and notices, opt-in hard stop, task-boundary `/compact` advice,
  PreCompact recovery-path preservation, subagent brief arm (off by default).
- Live statusline (opt-in install/uninstall that composes with an existing statusline, backs up
  settings and restores the original exactly).
- CLI: `stats`, `show`, `run`, `doctor`, `session disable|enable`, `config set`,
  `statusline`, `extensions`, `fold`; placeholders for `replay`, `facts`, `prune`.
- Trusted extension loading (entry points and content-hash-trusted drop-ins), a compressor
  conformance kit and a versioned extension interface (`EXTENSION_API_VERSION = "1.0"`).
- Per-session config overrides (`config set`) and a per-session kill switch that finds the
  project's last active session.
- Learning (M2, in progress): `token-sparen fold [--session ID] [--all]` merges a finished
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
  been observed without regret. `[learn.hold]` keeps a point at its safe arm (the subagent brief,
  until benchmarked).

### Changed
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
