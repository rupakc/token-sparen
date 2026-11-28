# Configuration

Every tunable lives in one packaged file, [`token_sparen/params/defaults.toml`](token_sparen/params/defaults.toml), which is the full list of keys with comments. You change values in your own files or, for one session, with a command.

## Precedence

Later layers override earlier ones:

1. packaged defaults (`token_sparen/params/defaults.toml`);
2. `~/.token-sparen/config.toml` (yours, optional);
3. `<project>/token-sparen.toml` (committed to the repository, so it may be someone else's; see below);
4. environment variables `TOKEN_SPAREN_<SECTION>__<KEY>=value` (double underscore between parts, for example `TOKEN_SPAREN_GUARDS__ENABLED=false`; list values are comma-separated);
5. this session only: `token-sparen config set <section.key> <value> [--session ID]` (for example `config set guards.enabled false`).

**Environment variables can only narrow.** A `TOKEN_SPAREN_*` variable may make token-sparen intervene less or keep more, never trim more, recover less or block more, because an environment can be set by a repository's committed Claude Code settings. Widening a setting (for example allowing more content types to be trimmed, or opting in to file-content trimming) happens only in `~/.token-sparen/config.toml` or with `token-sparen config set`. A widening value found in the environment is ignored and logged once.

User-only keys are ignored from the environment even when they would narrow; use `TOKEN_SPAREN_DISABLE=1`, `TOKEN_SPAREN_GOVERNOR__ENABLED=false` or `TOKEN_SPAREN_PRUNE__ALLOW=false` instead of `LEARN__ENABLED`, `GOVERNOR__HARD_STOP` or `PRUNE__ENABLED`. Two further variables are not settings: `TOKEN_SPAREN_HOME` moves the state root (ignored when it points inside the current project, or on POSIX is not owned by you) and `TOKEN_SPAREN_PYTHON` picks the interpreter the hooks use (see the [support matrix](support.md) and [Troubleshooting](troubleshooting.md)); it must be set in the environment Claude Code starts from, not in `config.toml`.

`compression.min_tokens` can raise the 2000-token floor but never lower it (a lower value is
ignored and `doctor` flags it).

**What a project file may change.** `<project>/token-sparen.toml` is committed, so it may be
someone else's. It can only make token-sparen intervene less or keep more, never trim more,
recover less or block more. A value is used only if it has the right type, is within sane
bounds and moves in the allowed direction from your own value (defaults, your config, env,
session); anything else is ignored and logged once. `TOKEN_SPAREN_*` variables follow the same
table, measured from your config and session overrides.

| Settings | A project may |
|---|---|
| `statusline.{segments,ascii,color,parse_budget_ms}`, `pricing.*`, `boundary.{min_ctx_pct,min_overlap,min_turn,history,min_identifiers,cooldown_turns}`, `governor.{econ,ctx}_{notice,advise}` | set freely within bounds (display, advice) |
| `guards.enabled`, `read_dedupe.enabled`, `governor.enabled`, `boundary.enabled`, `subagent.enabled`, `facts.enabled`, `loopguard.enabled`, `output_style.enabled` | only turn off |
| `compression.min_tokens`, `min_saving_ratio`, `levels.*.keep_ratio`, the per-type knobs (`json.items`, `diff.context`, `stacktrace.edge_frames`, `test_report.block_lines`, `search.per_file`, `pplx.results`, `log.tail`), `recovery.*`, `guards.max_read_bytes`, `guards.pretrim_bounded_keep_ratio`, `read_dedupe.min_tokens`, `loopguard.min_k` | only raise (keep more, intervene later) |
| `compression.compiler.warnings.*` | only move from `count` to `keep` |
| `compression.skip_tools` | only add tools |
| `compression.allow_ctypes` | only remove content types |
| `compression.trim_file_contents`, `prune.allow` | only set `false` (`prune.allow = false`: never prune here, and token-sparen's rules are reverted) |
| `read_dedupe.max_turn_gap`, `read_dedupe.max_entries`, `compression.save_deadline_ms`, `adapter.latency_budget_ms`, `facts.max_facts`, `facts.max_tokens`, `prune.lease_sessions` | only lower (`prune.lease_sessions`: pruned tools are restored sooner) |
| `learn.tau.<tier>` lower; `learn.floor.<tier>`, `learn.ladder_obs` higher; `learn.hold.<point>` only `true`; `learn.breaker_min_obs`, `learn.breaker_every` lower; `learn.breaker_lcb_q`, `learn.breaker_sessions` higher | only make learning more careful |
| everything else, including `learn.enabled` and all other `learn.*`, `compression.levels.*.{head,tail}` and `guards.pretrim_{lines,head}` (a larger head/tail displaces prompt-relevant lines), `governor.hard_stop`, `governor.session_budget_tokens`, `governor.windows`, `adapter.latency_sample_rate`, `adapter.latency_log_max_bytes`, `extensions.*`, the other `facts.*` keys, the other `prune.*` keys (including `prune.enabled`: pruning is opted in only with `prune enable` or your own `config.toml`), `loopguard.watch_calls`, `output_style.directive`, `ope.*`, and any key not listed | nothing (your `config.toml` or `config set` only; a `TOKEN_SPAREN_*` variable is ignored too) |

| Key | Default | Meaning |
|---|---|---|
| `compression.min_tokens` | 2000 | outputs below this are never modified (floor) |
| `compression.min_saving_ratio` | 0.15 | minimum share removed for a replacement to be used |
| `compression.skip_tools` | edit tools, TodoWrite, ... | tools whose output is never compressed |
| `compression.allow_ctypes` | diff, search, test_report, compiler, build_log, json, pplx, log | content types that may be trimmed (add `text`, `table`, `stacktrace` to widen; `[]` trims nothing) |
| `compression.trim_file_contents` | false | also trim file contents (Read, `cat`, `head`, ...); see the quality warning above |
| `guards.enabled` | true | block the unbounded commands ([Guards](mechanisms/guards.md)) |
| `guards.max_read_bytes` | 5000000 | size limit for `cat`/Read without a range |
| `read_dedupe.enabled` | false | return a pointer for unchanged re-reads (opt-in; see [Read de-duplication](mechanisms/read-dedupe.md)) |
| `read_dedupe.max_turn_gap` | 8 | pointer only if the earlier read is this recent |
| `governor.enabled` | true | budget and context notices |
| `governor.session_budget_tokens` | 500000000 | economy budget the notices and hard stop use |
| `governor.hard_stop` | false | deny context-adding tools at 100 % of the budget |
| `boundary.enabled` | true | suggest `/compact` at task boundaries |
| `subagent.enabled` | true | subagent brief decision point (its default arm is off) |
| `facts.enabled` | true | inject learned repository facts at session start ([details](mechanisms/facts.md)) |
| `facts.max_facts`, `facts.max_tokens` | 8, 300 | size of the injected facts block |
| `loopguard.enabled` | true | one redirect line when the same error repeats within a turn |
| `loopguard.min_k` | 2 | fewest repeats of one error before the redirect (the learned setting is 2, 3 or 5) |
| `learn.hold.loopguard` | false | `true` keeps the most patient setting (5 repeats); it does not switch the guard off |
| `loopguard.watch_calls` | 5 | tool calls after the redirect that decide whether the loop broke (user only) |
| `output_style.enabled` | false | one line at session start (startup, `/clear`, after compaction; never on resume) asking for concise prose while never shortening code, commands, file paths, error messages or quoted tool output; opt-in only: enabled only from your `config.toml` or a session override (`config set`); a project or a `TOKEN_SPAREN_*` variable may only switch it off (spec 6.6) |
| `output_style.directive` | `concise` | which directive; an unknown name means off and is logged once (user only) |
| `prune.enabled` | false | tool pruning ([opt-in](mechanisms/tool-pruning.md)); read only from your own `config.toml` (or set per project by `token-sparen prune enable`) |
| `prune.builtins` | false | also prune built-in tools an SDK `init` record listed as always loaded (your `config.toml` only) |
| `prune.allow` | true | `false`: never prune here and revert token-sparen's rules (a project may set it) |
| `prune.min_sessions`, `prune.min_prompts` | 3, 15 | unused sessions and prompts before a tool is a candidate (user only) |
| `prune.tau`, `prune.ucb_q` | 0.01, 0.95 | prune only when the regret bound `ucb(ucb_q) <= tau` (about 300 unused prompts; user only) |
| `prune.idle_minutes` | 60 | new rules are written only if no other session of the project was active this long (user only) |
| `prune.lease_sessions` | 15 | a pruned tool is restored after this many sessions to observe it again (a project may only lower it) |
| `learn.enabled` | true | learn how strongly to trim; `false` uses the fixed defaults, which trim more than a cold learner (your `config.toml` or `config set` only) |
| `learn.hold.compression` | false | `true` stops all trimming of tool output (see [Turning it off](host-guides/claude-code.md#turning-it-off)) |
| `learn.hold.read_dedupe` | false | `true` stops read pointers |
| `learn.savings_reward` | `"scaled"` | savings reward of a trim: `scaled` (removed x resends / recent p95), `fraction`, `counterfactual` (vs the best arm on the same output; `learn.resend_horizon`, 20) or `cost` (cache-weighted); a tuning option (your `config.toml` or `config set` only) |
| `learn.ladder_retry_decisions` | 0 | `N > 0`: re-open a locked, harsher trimming level for one probing decision after N decisions of that bucket within one session (counted per session; user only) |
| `guards.pretrim_bounded_keep_ratio` | unset | share of lines a learned pre-trim keeps; unset is the larger of the packaged and your configured `compression.levels.standard.keep_ratio` (raising the standard arm raises the pre-trim, lowering it never lowers the pre-trim), any number is used as is (a project may only raise it) |
| `statusline.segments` | all seven | which segments to show |
| `statusline.ascii`, `statusline.color` | false, true | plain glyphs; colours |
| `recovery.ttl_days`, `max_files`, `max_bytes` | 14, 200, 100 MB | recovery file retention |
| `adapter.latency_budget_ms` | 400 | hook time cap before it becomes a no-op |
| `extensions.allow_override` | false | let a trusted extension replace a built-in (your `config.toml` only) |
