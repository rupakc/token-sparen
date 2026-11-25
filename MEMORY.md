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
