# MEMORY: decision log

Append-only. One entry per decision: date, decision, reason. Newest at the bottom.

## 2026-11-25 Owner decisions (spec section 17)
- **Approach A**: one learning engine drives all mechanisms (not two bolted-together optimizers).
- **Benchmarks are Claude-Code-only**, run under a Max 5x plan, calendar time unconstrained;
  scheduler respects 5-hour windows and the weekly cap.
- **Dogfooding**: about 2 weeks of real use to collect decision logs with propensities, needed for
  off-policy evaluation (E3).
- **Quality gate**: non-inferiority on resolved rate with a -5 percentage point margin; exact CI is
  always published; release is blocked if the gate fails.
- **Scope**: v1.0 = Claude Code complete; v1.1 = other hosts (Codex, Gemini CLI, Qwen, OpenCode).
  Clients without hooks are deferred.
- **Live statusline is required** (zero model tokens).

## 2026-11-25 Amendment
- **Python floor is 3.11** (spec said 3.10): 3.10 reached end-of-life in Oct 2026 and `tomllib`
  is only in 3.11+. Runtime remains stdlib only.

## 2026-11-25 Designer calls (spec section 17; owner to check)
- Python stdlib core plus a thin TypeScript shim where a host needs one.
- Gemini CLI supported; Antigravity deferred.
- Output-style directives off by default.
- LLM side-calls off by default.
- Tool pruning needs opt-in on Claude Code because it writes settings.
- Coverage gate >= 90 %.
- CLI name `token-sparen`.

## 2026-11-25 Process
- Development follows Elephant-Goldfish (spec section 13); see `.claude/skills/egm-cycle`.
- Private inputs (owner's local sessions, E1 results) stay outside the repo and are only referred to generically.

## 2026-11-25 Text compressor (Task 7)
- Error lines (ERROR_PATTERNS) never take part in a fold run, so folding can never hide one.
- Digit-run normalisation for folding uses a marker byte that cannot occur in UTF-8, so a literal
  `#` is not treated as a number.
- The case-insensitive error regex folds ASCII case only; whole-text scanning uses literal byte
  anchors plus per-line regex verification (exactly equivalent, several times faster).
- `CompressResult.text` excludes the recovery marker; elided gaps show as `… [N lines elided]`.
- Output with >= 90 % of lines pinned (head, tail, errors with context) is returned unchanged
  (lossless); invalid `[compression.levels.*]` values are logged and fall back to the packaged
  defaults. Strict wall-clock budgets run only under `pytest -m perf` (CI job `perf`).

## 2026-11-25 Structured compressors (Task 8)
- Per-type knobs live in `params/defaults.toml` (`[compression.json] items`, `[compression.diff]
  context`, `[compression.stacktrace] edge_frames`), each a light/standard/aggressive table. The arm
  is chosen from `Level.keep_ratio` (>= 0.5 light, >= 0.25 standard, else aggressive), so the
  compressors need no arm name. Knobs read the packaged defaults only.
- JSON output is a readable summary, not valid JSON (`… N more items with same keys`, `… N more
  keys`, `…(+N chars)`). Strings/keys/subtrees matching ERROR_PATTERNS are never elided or cut.
  `total_lines` is the indent-2 pretty-print line count, computed arithmetically.
- Diff: only unchanged context lines inside a hunk (counted from the `@@` header) can be elided,
  and only runs of >= 2 lines (marker ` … [N unchanged lines]`); every other line is kept.
- Stack traces: frames are Python `File` units, or `at ...` lines (JVM/JS); the line before a
  first `at` run is kept as the exception header. Library = site-packages, dist-packages,
  node_modules, java.*, jdk.internal, `<frozen`. Non-trace text goes through TextCompressor.
- Built-ins are registered in `compress/__init__.py`; entry-point plugins registered later override.

## 2026-11-26 E2 pplx needle policy (Task 11)
- A web-search result whose snippet merely mentions an error is search content, not a tool failure:
  its URL must stay inline but the snippet text only needs to be recoverable via the marker.
  Per-result `error` keys (real fetch failures) are must_inline. Pinning every error-mentioning
  snippet would defeat compression.
- Node/JS traces (`XxxError: msg` then an `at ...:line:col` frame within 3 lines) are sniffed as
  stacktrace.

## 2026-11-26 Claude Code adapter (Task 12)
- Hook entry fails open on everything (bad payload, unknown event, exception, StoreBusy, unwritable
  home, unserialisable action): stdout is exactly one JSON object line, exit 0. `{}` is the no-op.
- Latency cap `[adapter] latency_budget_ms = 400`: a watchdog timer writes `{}`, flushes, logs and
  `os._exit(0)`s on expiry; a lock + flag guarantees a single write. A hook that times out returns
  the no-op. Durations are sampled (10 %) to `logs/latency.jsonl`.
- Project keys are cached in `~/.token-sparen/project_keys.json` keyed by sha256(realpath), not the
  raw path (shared-store invariant); an entry is recomputed when `<dir>/.git/config` mtime changes.
- Project dir = `CLAUDE_PROJECT_DIR`, else payload `cwd`.
- Render rules: on PreToolUse a block wins over a rewrite (never both); a rewrite never sets
  `permissionDecision`. `additionalContext` is emitted only on SessionStart, UserPromptSubmit and
  PostToolUse; unsupported Action fields for an event are dropped. Output replacement only for
  Bash (non-image), text Read and plain-string responses; any other shape is left untouched.
- Emit gate: dedupe keys (`emitted_keys`) and the `injected_tokens` ledger live in the session
  overlay; items are taken by ascending `priority` and skipped when they would exceed the budget.
  If the overlay cannot be updated (StoreBusy/OSError) nothing is injected, so dedupe and accounting
  are never bypassed.
- hooks.json invokes `python3`. On Windows the interpreter may only be `py` or `python`;
  `token-sparen doctor` (Task 17) must check this and advise.

## 2026-11-26 Compression decision point (Task 13)
- `learn/policy.py` holds the stable engine contract (`DecisionPoint`, `Policy`, `Decision`,
  `DecisionRecord`, `StaticPolicy`, `COMPRESSION`). `StaticPolicy` uses propensity 1.0, bucket
  `"static"`, uuid4-hex ids; an unknown point or an arm not in `point.arms` falls back to the safe
  arm. `DecisionRecord` adds `ctype` and `tool_name` after the eleven contract fields.
- Every policy decision is logged to `projects/<key>/decisions.jsonl`, including those that end as
  a no-op (arm `off`, lossless result, saving < `min_saving_ratio`): then `tokens_after ==
  tokens_before` and `recovery_id` is null. Gates before the decision (deny-set, unreplaceable
  shape, < `min_tokens`) log nothing. The size gate uses the text ratio (conservative); ledger and
  log use the content-type ratio. `command_sig` = raw Bash command[:500] (Task 21 normalises).
- **PostToolUseFailure cannot replace the result on Claude Code** (docs: only `additionalContext`).
  The failure event routes to the same handler, but the Claude predicate
  `translate.replaceable` returns False for it, so no work, recovery file or decision happens and no
  fake savings are booked. The mechanism itself supports failures (host-neutral default predicate)
  for hosts that can replace them. Consequence: failing Bash commands are not compressed on Claude
  Code today; revisit if the host adds failure replacement.
- Bash: the replacement keeps `persistedOutputPath`/`persistedOutputSize`. Read: `file.content` is
  compressed; `numLines`/`totalLines` stay as the host reported them, the marker states kept/total
  lines of the compressed text.
- `[compression] skip_tools` (env override: comma-separated) plus every `edit`-category tool are
  never compressed; an invalid value falls back to the packaged deny-set.
- Session overlay (local per-session file, not a shared store) keeps `turn`, `last_prompt`
  (<= 2000 chars, from UserPromptSubmit), `recent_paths` (last 20 `file_path`s of read/edit tools,
  recorded on PostToolUse/Failure) and the statusline counters `trims`, `tokens_removed`. Ledger
  write failures are logged and do not undo a trim.
- Handlers get the hook env through `entry.HOOK_ENV` (a ContextVar set around `handle`), so the
  `Handler(event, cfg)` signature stays unchanged. Outputs are pruned after each save.

## 2026-11-26 Preview-bounded compression (Task 13 fix round 1)
- **The hook never returns more than the model would have seen.** Baseline = the preview
  (`output_text`, stdout+stderr as the host shows it); `min_tokens`, size features, `tokens_before`
  and the ledger all use the preview. With a persisted Bash output (<= `[compression]
  persisted_max_bytes = 5000000`) the full file is compressed first, then the preview; a candidate
  is used only if `len <= len(preview)` and `tokens_after <= tokens_before * (1 - min_saving_ratio)`.
  A final explicit check refuses a body longer than the preview. The recovery file always holds the
  full text when available. `DecisionRecord.tokens_full` (appended, default None) records the
  persisted size.
- Persisted file: whether it includes stderr is undocumented, so stderr is not appended to it.
- `[compression] save_deadline_ms = 250`: past this hook time (entry passes the watchdog start via
  `HOOK_STARTED`) no recovery file is written and nothing is replaced, so the watchdog never leaves
  orphan recovery files.
- `TodoWrite`, `AskUserQuestion`, `ExitPlanMode` and the edit category are a hard skip floor that a
  `skip_tools` override cannot remove.

## 2026-11-26 Guards, pre-trim point, read de-dupe (Task 14)
- **Handler chains.** `entry.register` appends to an ordered per-kind list (a new list each time,
  so snapshots stay valid). PreToolUse: first block wins (rewrite dropped); else the first rewrite
  wins and later handlers see the rewritten input. PostToolUse(+Failure): the first replacement
  ends the chain; order is track -> read_dedupe -> compression, so a pointer skips compression.
  `inject_context`/`user_message` of every handler that ran are joined. Built-in mechanisms are
  wrapped: an exception is logged and that mechanism is a no-op; the rest of the chain still runs.
- **Guards** parse with `shlex` (posix, operators split); a parse error never blocks; every simple
  command of a pipeline/chain is checked (leading `VAR=x`, `sudo`, `time`, `command`, `nohup`,
  `exec` skipped). Roots are `/`, `~`, `~/`. grep/rg block only when a path argument (not the
  pattern) has a `node_modules|.git|dist|build|.venv` component and is an existing directory;
  includes are `--include`, rg `-g/--glob/--iglob/-t/--type/--type-add`. **`cat` blocks only when its
  stdout reaches the tool result** (last command of its pipeline, no `>` redirect): `cat big | head`
  is bounded and stays allowed. `[guards] enabled = false` also disables pre-trims.
- **Pre-trim**: asked only when `projects/<key>/pretrim.json` lists the command (exact match via
  `guards._normalise` until Task 21); then a decision is logged (tokens 0/0, unknown pre-tool).
  Candidate `lines` (positive int) beats `[guards] pretrim_lines`. Never rewritten: any of
  `| > < & ; \` $( newline`, `run_in_background`, editors/pagers/top/watch/ssh, bare REPLs/shells.
  The rewrite masks the command's exit status (pipeline exit = tail's); accepted for now because
  the format is fixed by the plan and the command prefix must stay intact for allow-rules.
- **Read de-dupe**: overlay `reads` keyed by sha256(realpath)[:16] + offset + limit + `agent_id`
  (subagents have their own context; `agent_id`/`agent_type` added to the adapter extras). No raw
  paths stored. A pointer needs equal mtime_ns, size and sha1 of the returned content, entry turn
  >= `last_compact_turn`, and >= 2000 estimated tokens (config floor clamped). PreCompact sets
  `last_compact_turn` and clears the table; SessionStart(compact|clear) clears it; PreToolUse of
  Edit/Write/MultiEdit/NotebookEdit drops entries of that path; a Read output that compression
  replaced is forgotten (the model never saw it in full). The pointer keeps the original turn.
  Static default arms: `pretrim = bounded`, `read_dedupe = pointer`.

## 2026-11-26 Task 14 fix round 1 (controller rulings)
- **Pre-trim is a PostToolUse compression arm, never a shell rewrite** (spec deviation from §6.2
  "`| tail -n K`"; the §2 anti-goal "never silently drop information" wins). The rewrite
  masked the exit status, lost lines before the tail unrecoverably and gave no partial output on
  timeout. Now PreToolUse never rewrites Bash. When the normalised command is a candidate in
  `projects/<key>/pretrim.json`, compression asks the policy for `pretrim`. If the arm is
  `bounded`, the output is compressed with `Level(head=[guards] pretrim_head=10,
  tail=K=[guards] pretrim_lines=120 or the candidate's lines, keep_ratio=standard)`, still with
  the recovery marker, error pinning and the preview bound, and the decision is logged as point
  `pretrim`. If the arm is `off`, a no-op `pretrim` record is logged and the normal `compression`
  decision follows. Static default `pretrim = off`. `[guards] enabled` no longer affects pre-trims.
  Structured compressors still pick their knobs from keep_ratio, so the tail bias applies fully
  only to the text compressor.
- **Guards split commands on unquoted newlines** (backslash-newline continues a line) and skip
  heredoc bodies (`<<`, `<<-`; delimiter matched per line). Redirection operators and their targets
  are removed from the words. Stdout counts as redirected for `>`/`>>`/`>|`/`>&` with no fd or fd 1,
  and for `&>`/`&>>`. `2>/dev/null` and `2>&1` do not count. Relative paths resolve against the
  payload `cwd` (passed through as an adapter extra) when it is absolute, else the project dir.
  The Read guard treats `pages` as a range and skips non-text files (.pdf, .ipynb, images).
- **Read de-dupe recency bound** `[read_dedupe] max_turn_gap = 8`: a pointer is used only if the
  earlier read is at most 8 turns old; an older one is re-sent in full and its entry refreshed. The
  pointer text itself leaves the agent a way out ("re-read with a different range or after edits"),
  so a mistaken pointer costs one extra ranged read, not lost information.

## 2026-11-26 Governor, boundary advice, subagent shaping (Task 15)
- **Context window is not guessed per model.** Resolution: `~/.token-sparen/windows.json`
  (`{session_id: {"window": N}}` or `{session_id: N}`, written by the statusline in Task 16, read
  only here) -> env `CLAUDE_CODE_MAX_CONTEXT_TOKENS` -> `[governor.windows]` fnmatch table on the
  model id (packaged table holds only `default = 200000`) -> 200 000. `ctx_pct` = last main-chain
  call's input + cache_read + cache_write over the window.
- **Incremental burn meter.** The session overlay key `governor` caches the byte offset reached
  (end of the last complete line), running burn, last call context/model, the last 64 message ids
  (streamed records of one id count once, last value wins), EWMA (alpha 0.3) of per-turn burn
  (closed on each UserPromptSubmit) and a digest of the first min(offset, 256) bytes. A smaller
  file, a changed head or another transcript path restarts from 0. A first contact with more than
  `max_unparsed_bytes = 50 MB` unparsed is skipped: `burn_unknown` (economy levels and the hard
  stop are off for the session) and the last call is found in the trailing `tail_scan_bytes = 1 MB`.
  Otherwise a parse stops after `parse_budget_ms = 150` and continues in the next hook
  (`catching_up`: no advisories, no EWMA update meanwhile). No new bytes = a stat, no lock.
- **Advisories** are `user_message` (systemMessage, terminal only), on UserPromptSubmit and
  PostToolUse(+Failure). Keys `ctx:notice|advise`, `econ:notice|advise` in overlay
  `governor_advised`, each said once per session; reaching advise implies notice. Levels do not
  re-arm after a compaction (strictly once per session).
- **Hard stop** (`[governor] hard_stop = true` and burn >= `session_budget_tokens`): PreToolUse
  deny of Read/Bash/WebFetch/WebSearch/Task/Agent only; never edits. Reason names
  `[governor] hard_stop = false` / raising `session_budget_tokens` in the user or project config
  (no slash command named until Task 17 adds one).
- **Boundary**: identifiers = path basenames, snake_case/camelCase (>= 4 chars), backticked words,
  lower-cased; compared with the last 3 prompts' identifiers plus identifiers of `recent_paths`
  by containment |A ∩ B| / |A| (A = the prompt), threshold `[boundary] min_overlap = 0.15`
  (fix round 1: Jaccard against the growing 3-prompt union flagged prompts naming only known
  identifiers).
  Extra guard: a prompt with < `min_identifiers = 2` identifiers (or an empty history) is never a
  boundary, otherwise "yes, go on" would always look like a topic shift. Cooldown 10 turns.
- **PreCompact output is plain-text stdout** (Claude Code docs: a non-blocking PreCompact hook's
  stdout becomes the compaction summary instructions; `systemMessage` is discarded). New
  `Action.compact_instructions` + `ClaudeAdapter.render_text`; entry writes that text instead of a
  JSON object for PreCompact only. Instructions ask to keep recovery-marker paths and
  `token-sparen show <id>` commands verbatim and list the files edited this session (tracking now
  keeps `edited_paths`, last 20). Composed after read de-dupe's handler (which sets
  `last_compact_turn`).
- **Subagent brief**: point `subagent_brief` (`off`/`on`, safe `off`, tier medium), static
  default `off`. Only Task/Agent with a non-empty string `prompt` not already holding the brief;
  every such decision is logged. `updatedInput` without `permissionDecision` may be re-checked in
  auto mode (host docs); acceptable while the arm is off by default.
- **Agent results**: a plain-string Agent/Task result is compressed by the normal PostToolUse
  path; a structured result (`{"content": [...], ...}`) is not a replaceable shape yet and is left
  untouched.

## 2026-11-26 Task 15 fix round 1 (controller rulings)
- Transcript scan seeks to the offset reached before every read, so a record split by a chunk
  boundary is read whole next time (was: dropped, and the lagging offset re-parsed later, double
  counting beyond the 64-id dedupe). A line longer than a chunk is read whole up to
  `max_unparsed_bytes`; a longer one is skipped as unparseable, never stalling the scan.
- If the last call's context exceeds the resolved window, the window is treated as unknown:
  no ctx advisory and no boundary advice (`GovernorState.window_unknown`).
- PreCompact instructions are emitted only when the session has recovery markers (`trims` > 0)
  or edited files; otherwise the JSON no-op. Instruction text uses no nested backticks.
- Advisories check `governor_advised` in the lock-free overlay read first; no lock when every
  wanted level was already said.

## 2026-11-26 Live statusline (Task 16)
- **Launcher** `scripts/statusline.sh` (POSIX sh, `python3 -I -c`) is copied by `statusline install`
  to `~/.token-sparen/statusline.sh` (plugin updates move the versioned plugin dir) with
  `FALLBACK_ROOT` baked to the installing package root. Root resolution per run:
  `$TOKEN_SPAREN_ROOT` -> `installed_plugins.json` (recursive search for `installPath` containing
  `token-sparen`, newest mtime) -> `plugins/cache/*/token-sparen/*` (newest) -> `FALLBACK_ROOT`;
  `CLAUDE_CONFIG_DIR` respected. Nothing found: empty line (compose: the original's first line).
  The repo copy has an empty `FALLBACK_ROOT`. No `statusline.cmd` for Windows in v1 (doctor will
  report it; compose mode needs `sh`).
- **Settings**: only `statusLine` changes; JSON object required (else refused, nothing written);
  backup `settings.json.token-sparen.bak` only if absent; original recorded in
  `statusline_install.json` under `installs[<realpath of settings>]`; composed command
  `sh '<launcher>' --compose -- '<original>'` (shlex-quoted, other statusLine fields kept);
  re-install idempotent; atomic write keeping file mode; a symlinked settings file is written
  through (link kept). Uninstall restores the recorded value in place or removes the key; if the
  statusline was changed by the user since install it is left alone (`unchanged`); with no record,
  a composed command is unwrapped. Launcher copy removed when no install remains.
- **Render**: bound to stdin `session_id`/`transcript_path`; project dir = `CLAUDE_PROJECT_DIR` ->
  `workspace.project_dir` -> `cwd` (same key as hooks). Sensor writes `windows.json`
  `{sid: {window, model, ts}}` only for a positive int `context_window_size`, skips the write if
  unchanged and < 1 h old, prunes entries older than 7 days. Burn/EWMA via `governor.state` with
  `[statusline] parse_budget_ms = 20`. ctx % from stdin `used_percentage`, else the governor.
  `rate` segment = `+EWMA/turn` and `5h N%` from `rate_limits` when present. Net = `tokens_removed`
  - `recovery_read_tokens` - `injected_tokens`; `≈$` = net x total_cost_usd / (input+output tokens)
  only when both known (no model price table). Regrets = ledger `regrets` (0 until Task 21); learn
  = `token_sparen.learn.overlay.learner_state(project_key)` if importable, else `cold`.
  `NO_COLOR` / `[statusline] color = false` -> no ANSI; `ascii = true` -> `ts | x r`. Kill switch
  and any failure -> `⛁ ts`. Compose: original runs concurrently (`sh -c`, own process group,
  killed at 1 s); non-zero exit, timeout or empty output -> ours only.
