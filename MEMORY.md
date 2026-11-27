# MEMORY: decision log

Append-only. One entry per decision: date, decision, reason. Newest at the bottom.

## 2026-11-25 Owner decisions (spec section 17)
- **Approach A**: one learning engine drives all mechanisms (not two bolted-together optimizers).
- **Benchmarks are Claude-Code-only**, run under a Claude Max subscription, calendar time unconstrained;
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
- Private inputs (the maintainer's local sessions, E1 results) stay outside the repo and are only referred to generically.

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

## 2026-11-26 User surface (Task 17)
- `run` reuses the hook path through `compression.compress_text` (synthetic Bash event, static
  policy, same decision log and ledger); a single argument with shell syntax runs via `/bin/sh -c`.
- Per-session kill switch: overlay flag `disabled: true` (`token-sparen session disable|enable`),
  honoured by `entry.main` for every event. Session id from `CLAUDE_SESSION_ID`, else `--session`.
- The slash command also allows `Bash(python3:*)` so it can fall back to `hooks/run_cli.py`.
- pyproject `packages` now lists `token_sparen.mechanisms` and `token_sparen.learn` (were missing).

## 2026-11-26 Extensions and conformance kit (Task 17a)
- Drop-ins load only if sha256(file bytes) is in `~/.token-sparen/trusted_extensions.json`
  (`{"trusted": {sha: {"path": realpath, "ts"}}}`); trust is by content, read only from the user's
  state home. The trust file itself is ignored if symlinked, foreign-owned or world-writable.
- Drop-ins are executed from the exact bytes that were hashed (spec built with
  `spec_from_file_location`, code compiled from the verified bytes) so there is no check/use race.
  Module name `token_sparen_ext_<sha[:12]>`; identical content is imported once.
- Skipped: symlinked files, symlinked `extensions/` dirs or project `.token-sparen/`, files not
  owned by the current user, world-writable files, files > 1 MB. Owner/mode checks are POSIX-only.
- Supersedes Task 8 note: built-ins now win over entry-point and drop-in compressors unless
  `[extensions] allow_override = true`, which is honoured only from `~/.token-sparen/config.toml`
  or `TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE`, never from a project's `token-sparen.toml`.
- Extension compressors (entry point or drop-in) always run inside `CheckedCompressor`: an
  exception, malformed result or any dropped ERROR_PATTERNS input line (stripped line must appear
  in the output) disables that compressor for the process (logged once) and the built-in answers.
- Laziness: discovery is two `lstat`s when no extension dir exists, cached per process; drop-ins
  are imported on the first lookup that can use one (a content type without a built-in, or
  override on). Extension decision points cannot reuse a built-in point name.
- `extensions trust` without `--yes` previews only and exits 1; control characters in the preview
  are escaped.

## 2026-11-26 Extensions fix round 1 (Task 17a review)
- Trust semantics (documented in README and skill): trusting a drop-in grants full code execution
  as the user. The runtime checks guard against buggy extensions, not malicious ones (hostile code
  acts at import). Trust follows content: an identical copy in any location also loads.
- Count validation for extension results: `total_lines` must equal `line_count(input)` (the
  built-in rule: split on newline, one trailing newline ignored, empty input = 0), and
  `kept_lines` may not exceed the output's non-marker lines (`content_lines`; marker lines start
  with `…` or `[N passed tests|warnings elided`). Violations discard the result and disable the
  extension. The conformance kit applies the same rule; it found that `search`, `log` and
  `build_log` reported 1 line for empty input, now 0.
- Error-line survival is whole-line membership after stripping trailing whitespace (leading
  whitespace is significant); a substring of a longer output line no longer counts.
- `allow_override` accepts true, int 1 and "1"/"true" (any case) from every source alike.

## 2026-11-26 M1 fix wave (code)
- **Layering**: tool-result shapes live only in the host adapter. `HostAdapter` has
  `render_tool_output()` and `replaceable()`; mechanisms receive the predicate from the entry
  (`ADAPTER.replaceable`). The mechanism default predicate is host-neutral (any post-tool event);
  a static test keeps `mechanisms/ compress/ learn/ store/` free of `adapters` imports.
- **Agent/Task results** (supersedes the Task 15 note): the documented object shape
  `{status, agentId, content: [{type: "text", text}], usage, ...}` is replaceable when `content`
  is a non-empty list of text blocks only. The text blocks are joined with `\n` (that join is the
  preview the never-exceed bound applies to); the replacement deep-copies every other field and
  sets `content` to one text block. Any non-text block leaves the result untouched.
- **`/token-sparen` is a plugin skill** (`skills/token-sparen/SKILL.md`), not a command:
  `commands/token-sparen.md` is removed (a skill and a command with the same name would shadow
  each other; the skill wins). Plugin skills substitute `${CLAUDE_PLUGIN_ROOT}` in
  `allowed-tools` and `${CLAUDE_SESSION_ID}` in the body, so `allowed-tools` is exactly
  `Bash(token-sparen:*)` and `Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py":*)` and the
  body passes `--session ${CLAUDE_SESSION_ID}`. Plugin-only installs work through the second rule.
- **Last active session**: SessionStart and UserPromptSubmit write
  `projects/<key>/last_active.json` `{session_id, ts}` (best effort). `token-sparen session
  disable|enable` resolves `--session` (non-empty) > `$CLAUDE_SESSION_ID` > that record if it is
  < 6 h old; otherwise it fails (exit 2) and says to pass `--session`.
- **`compression.min_tokens` is floored at 2000** (CLAUDE.md invariant wins over the spec's
  "default 2000"), like `read_dedupe.min_tokens`. A lower configured value is ignored and logged
  once per process to `errors.jsonl`; a higher value is honoured.
- **JSON same-value elision** (spec §6.1, plan Task 8; judged safe): in an array whose first item
  is an object, a later *kept* object's scalar value that equals the first item's value for the
  same key (same type) is shown as `"key": …same`. Never for keys `id name path url file` (any
  case), never for ERROR_PATTERNS values, never inside an item that carries an error (kept whole),
  never in the first item, and only when `…same` is shorter than the value (output never grows).
  Line counts are unchanged (the line is kept, its value abbreviated, like `…(+N chars)`); the
  full document stays recoverable through the marker. E2 stays 153/153.
- **Extension interface version** `token_sparen.EXTENSION_API_VERSION = "1.0"`. A drop-in or the
  module defining an entry-point object may set `TOKEN_SPAREN_EXTENSION["api"]` (`"1.x"`, `"1.0"`,
  `"1"`); undeclared counts as compatible. Another major (or an unreadable value) is skipped: a
  drop-in's literal `api` is read with `ast` before execution (status `skipped:api 2.0 (needs
  1.x)` in `extensions list`, never executed); a computed value fails at import (disabled,
  logged); an entry point is skipped after load with one log line (its listing loads nothing,
  so it shows no api status).
- **Hard stop tool set** (supersedes Task 15): Read, Bash, WebFetch, WebSearch, Task, Agent,
  Grep, Glob, LS and every `mcp__*` tool. Edit tools and interaction tools are never denied.
- **Windows doctor hint** (Task 12 follow-up done): when `python3` is missing, doctor on Windows
  names `py -3` or `python` (whichever exists) for the CLI (`<it> -m token_sparen`) and says the
  hooks need `python3` to resolve; with neither, it says to install Python >= 3.11 with PATH set.
- **Dropped (YAGNI)**: the spec §6.5 idle-gap boundary signal is not implemented; containment of
  identifiers is the only boundary signal (spec to drop it). No dedicated table/csv compressor:
  `table` content goes to the text compressor, which is safe (error pinning, marker, recovery);
  revisit only if replay shows tables are a large share of tokens.
- **Supersedes Task 17 "the slash command also allows `Bash(python3:*)`"**: that rule was removed
  in the least-privilege fix (`b7b2aa1`); since this wave the only rules are the two in the
  `token-sparen` skill (see above). No bare interpreter rule exists anywhere.
- **Defaults file location**: the packaged defaults live at `token_sparen/params/defaults.toml`
  (package data, read via `importlib.resources`), not a top-level `params/`, so wheels and plugin
  installs carry them. Docs that say `params/defaults.toml` mean this file.
- `uv.lock` is ignored: the project uses pip with the `dev` extra; no lock file is committed.
- **Session config layer** (spec §9 precedence, minimal): overlay key `config_overrides` (nested
  dict) is merged last by `load_config(..., session=overlay)`, after env. The hook entry reads the
  overlay once per hook (it already did for the kill switch). `token-sparen config set
  <section.key> <value> [--session ID]` writes it; the key must exist as a leaf in the packaged
  defaults (typos refused); values parse like env values; the session resolves like `session
  disable`. `extensions.allow_override` still needs the user file or env (a session value alone
  is ignored). The CLI `run`, statusline and other CLI commands do not apply the session layer.

## 2026-11-26 M1 fix wave, round 1 (controller rulings)
- **Supersedes "JSON same-value elision": dropped.** `…same` reads as "same as the previous
  item" but meant "same as the first", and an exemption list (id/name/path/url/file) cannot
  capture every identity-like key; the misreading risk outweighs the saving (quality first).
  Elision logic, golden and tests reverted; a regression test keeps repeated values verbatim.
  Spec §6.1 "duplicated-field elision" should be dropped by the docs task.
- **min_tokens floor warning is per project** (supersedes "logged once per process"):
  `projects/<key>/floor_warned.json` keeps `{min_tokens: <value warned about>}`; the warning is
  logged again only when the configured value changes. `doctor` shows a `!` line for any
  `compression.min_tokens` / `read_dedupe.min_tokens` below 2000 in the current directory's config.
- **Skill permissions narrowed (security, supersedes the C3 rule list)**: the skill is
  model-invocable, so a blanket `Bash(token-sparen:*)` let a prompt-injected model run
  `extensions trust --yes`, `config set`, `statusline install` without asking. `allowed-tools` now
  pre-approves only `stats`, `show`, `doctor`, `session`, each as `Bash(token-sparen <sub>:*)` and
  `Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <sub>:*)`; everything else goes through
  the normal permission prompt (the body says so). `stats` runs only when the user explicitly
  typed `/token-sparen`, not when the skill is auto-loaded for a question.
- **Extension api: newer minor rejected; entry-point status listed** (refines the C6 entry):
  compatible = same major and minor <= host minor (`"1.x"` = any 1.y); `"1.5"` on host `1.0` is
  skipped as `requires api 1.5 (host 1.0)`. `extensions list` shows an entry point's api status
  read with `ast` from the distribution's installed source file (`ep.dist.files`, no import);
  if that is not determinable it shows `api unknown (checked at load)`.
- **Layering test resolves relative imports**: `from ..adapters`, `from ...adapters`,
  `from .. import adapters` are resolved against the module's package before the check.

## 2026-11-26 Statusline launcher moved into the package (docs review round 1)
- `scripts/statusline.sh` moved to `token_sparen/assets/statusline.sh` (package-data) so a
  non-editable `pip install .` can run `statusline install`; `LAUNCHER_TEMPLATE` resolves it with
  `importlib.resources`. `scripts/check_wheel.py` now installs and uninstalls the statusline from the
  wheel venv. Supersedes the launcher path in the Task 20 entry above.

## 2026-11-26 Task 19: context features and hierarchical priors
- Phase precedence: failures >= 30 % -> debug is checked before edits >= 30 % -> build (a failing
  edit loop is debugging), then reads+searches >= 60 % -> explore, else mixed; < 3 calls -> mixed.
  The overlay keeps `recent_cats` (last 10 tool categories, a failure as `<cat>!`), written by
  `tracking.record_paths` on every post-tool hook; `SessionCtx.recent_cats` carries it.
- Prior chain: project levels (full -> `*`), then global levels; the session scope is the child,
  not part of the chain. Designed defaults always use `kappa_default` (6). A mean of 0 is
  represented as `Beta(1e-3, kappa)`.
- Unknown point/arm default means: savings 0, regret 0.5, deliberately conservative for undeclared
  extension arms. Designed defaults are hand-set, refit in M4.
- **Controller ruling (review round 1): store evidence, not posteriors.** A reviewer probe showed
  kappa-hat biased high (stuck at kappa_max) because sibling posterior means contained their own
  priors, and a prior folded into stored (a, b) could never be changed. Now `learn.beta.Evidence
  (ea, eb, n)` is stored (`observe` discounts by gamma, adds (x, 1-x), caps mean-preserving;
  `posterior(prior)` = prior + evidence); schema version 2 (version 1 files read as empty, logged
  once). `Beta.update` stays for compatibility; the engine (Task 20) uses Evidence.
  Parent mean `m = (ea + k0*d)/(ea + eb + k0)` (k0 = kappa_default, d = designed default; no
  recursion); prior `Beta(m*kappa, (1-m)*kappa)`. Kappa: method of moments on data-only sibling
  means with noise correction `var(m_i) - mean(m_i(1-m_i)/(w_i+1))`; < 3 siblings -> default,
  <= 0 -> kappa_max, else clamp [2, 20]. Simulation test recovers true kappa 3 and 20.
- `StoreChain(params=Config.data)`: every `learn.*` lookup falls back per key to the packaged
  default, so user/project/env overrides apply. `bucket_key` rejects `|` or `=` in keys/values.
- **Overlay lock contention (review round 1, item 4)**: tracking now writes the overlay on every
  PostToolUse, so `jsonstore.locked` polls from 1 ms doubling to a 5 ms cap, with full jitter
  (`_jitter`; without it waiters that queued together wake in lockstep and only one wins per
  round: 10 contenders measured ~45 ms for the last, ~7-15 ms with jitter). `_on_track` moved to
  the end of both PostToolUse chains, registered `always=True` (`entry.ALWAYS_RUN`) so it still
  runs after a replacement ends the chain; compression features now see the history before the
  current call. Perf test: 10 processes, one update each, p95 wait < 20 ms x perf factor.
- **Kappa noise term uses within-bucket variance (review round 2)**: the Bernoulli term
  m(1-m)/(w+1) overshoots for fractional savings rewards and pinned kappa at kappa_max (median
  16-20 for true 3-6). `Evidence` now keeps `es2` (discounted sum of x^2, scaled by the cap like
  ea/eb; serialised `[ea, eb, n, es2]`, a 3-element list reads es2 = ea; schema stays v2) and
  `var_within = es2/w - (ea/w)^2`; noise = mean(var_within_i/(w_i+1)). Simulation (sd 0.15
  fractional rewards, n=20): true kappa 3 -> median ~3.6, 6 -> ~6.3. `features.for_point`
  replaces `|`/`=` in values with `_` so `bucket_key` never raises on the hook path.

## 2026-11-26 Task 20: constrained two-posterior Thompson engine
- `learn/engine.py` `Engine(stores, cfg, rng)`: per trimming arm S and R = `prior_for(...)` +
  (project + session evidence of the full bucket, `Evidence.__add__`, cap not applied on sum).
  The safe arm is never sampled or updated (S = R = 0 by definition). Floor first (safe arm with
  probability `floor[tier]`), else sample R~ then S~ per arm in declared order; feasible iff
  R~ <= tau (tau >= 1 skips the R draw); high tier pre-filters arms with `R.ucb(0.95) > tau`;
  max S~ wins, ties to the safe arm then declared order. `[learn.tau]`, `[learn.floor]`,
  `propensity_samples = 64`, `pending_max = 500`, `enabled = true`, `cold_obs = 3` (learner state
  only; no cold special-casing) added to defaults.
- **Child bucket is not its own parent**: since the child is project + session evidence of the
  full bucket, `prior_for(..., child_bucket=bucket)` skips that bucket in project scope (else the
  project data would be counted twice: once as the parent mean, once as evidence).
- **Session overlay key renamed `posteriors` -> `evidence`** (hierarchy `SESSION_KEY`), matching
  the controller ruling; `pending` holds `{decision_id: [point, bucket, arm, ts]}`, oldest evicted.
- Propensity = floor*[safe] + (1-floor)*freq over 64 replays with `random.Random(decision_id)`,
  clamped [1e-3, 1]; ~0.3 ms for 3 trimming arms. Decision ids come from the engine rng (seeded
  tests are deterministic). decide writes `pending` (one locked overlay update; a failure there is
  logged and the decision still stands); reward does a lock-free pending pre-check, then one locked
  update of session evidence and `StoreChain.invalidate("session")`. Errors -> safe arm,
  propensity 1.0, bucket `error`; NaN/non-numeric rewards ignored + logged, others clamped.

## 2026-11-26 Task 20 review round 1 (controller design rulings)
Cold-start simulations: aggressive chosen 44 % of cold decisions, ~7 regret events per fresh
bucket; MC propensity gave E[p/p-hat] ~ 5.9 at p ~ .02 (unusable for IPS).
- **Regret priors use `kappa_regret = 20`** (designed default and parent derived; savings keep the
  estimated kappa). Cold P(R~ <= .05) at kappa 6 -> 20: light (.02) .884 -> .879, standard (.04)
  .768 -> .712, aggressive (.08) .554 -> .374. It mostly slows aggressive; the ladder does the rest.
- **Safe-exploration ladder** (`[learn] ladder_obs = 20`): trimming arms in declared order; the
  first is always eligible; arm i only while arms < i are eligible (a chain: deliberately stricter
  than "i-1 established" alone) and arm i-1 is established = regret evidence n >= ladder_obs (max
  over pooled project+session full bucket and every back-off level in project/global scope) and
  regret posterior mean <= tau. Excluded before sampling. `Engine.eligible(point, features)`.
- **High tier is opt-in by design**: `[learn.optin] <point> = true` must be in the merged config
  *and* set by the user (`~/.token-sparen/config.toml` or `TOKEN_SPAREN_LEARN__OPTIN__<POINT>`),
  as `allow_override`; a project-only opt-in is ignored and logged. No opt-in -> safe arm,
  propensity 1. With opt-in: ladder + Thompson + regret ucb(0.95) <= tau gate. `Engine(...,
  env=None)` reads `os.environ` unless an env mapping is given.
- **Exact propensity** (`learn/propensity.py`, replaces the 64-draw Monte-Carlo, key
  `propensity_samples` removed): p_i = P_i * int f_Si(s) prod_{j!=i} [1 - P_j + P_j F_Sj(s)] ds,
  P_j = F_Rj(tau) (`beta.beta_cdf`, new public, with a precise-complement argument);
  p_safe = prod (1 - P_j); logged p = floor*[safe] + (1-floor)*p_branch, clamped [1e-3, 1].
  Quadrature: trapezoid in logit space y = logit(s) (Beta densities there are smooth, log-concave,
  exponential tails, so a, b < 1 is fine), per arm over mode +- 6 sd + 20/a | 20/b, step
  min(0.7, 0.7 * smallest sd among overlapping arms); tail terms beyond +-700. Measured sum error
  <= 2e-7 on stress cases (heavy close peaks, singular ends), ~0.7 ms for 3 trimming arms, whole
  decide ~1.4 ms incl. the locked pending write. Gauss-Legendre / plain Simpson on s were rejected:
  they miss narrow peaks (sum error 1e-2) and endpoint singularities (cold pointer Beta(5.4, 0.6)).
- **Decision ids are uuid4** (not the injected rng); choices stay deterministic under a seeded rng.
- **One reward per kind per decision**: pending entry is `[point, bucket, arm, ts, [kinds]]`; a
  second savings (or regret) reward for the same decision is ignored with one log line per call.
  Task 21 must report `regret=0.0` explicitly when the regret window expires.
- `cold_obs` removed from code and defaults (superseded by `ladder_obs`); the plan's mention of it
  is historical.

## 2026-11-26 Task 21: regret detection and savings attribution
- `learn/regret.py`: `normalize_command` (whitespace collapsed; leading `cd <p> &&|;`, `A=b`,
  `env A=b` stripped repeatedly; trailing `| tail -n N` / `2>&1` dropped; `sudo` and every other
  arg kept; <= 500 chars). `signature(tool, input)`: Bash -> normalised command, Read ->
  `Read:<realpath>` (any range), else `<tool>:<sorted json input>`. `decisions.jsonl`
  `command_sig` and the pretrim lookup (`guards._normalise`) now use the normalised command.
- Overlay `regret` = `{decision_id: {point, bucket, arm, recovery_id, command_sig, elided_tokens
  (<= 64, 8+ chars with a letter, not in kept text or the command, spread over the span),
  files_in_span (<= 32, elided span first, then kept text), edited_paths_since, decided_turn,
  ctype, best}}`, last 200 (evicted entries are paid their best so far). Trims are tracked for
  any policy (ledger `regrets` counts paid regrets > 0); rewards go to the policy only if it has
  a callable `reward` (duck-typed; StaticPolicy unaffected).
- Signals: recovery read 1.0; re-run of the same signature within `[learn] regret_window_turns
  = 6` turns 1.0 (age <= 2) else 0.5, none if a file the output mentions (elided **or kept**,
  deviation from the brief, see spec revision note) was edited since; >= 2 elided tokens in a
  prompt or non-edit tool input 0.5; `on_loop(ctx, ids|None)` 0.5 for compression decisions of
  this turn with an error-bearing ctype. Max per decision: 1.0 paid at once, partials and 0 at
  expiry (`turn - decided_turn > window`), everything at SessionEnd. Paid entries are removed.
- Dispatch `_on_regret`: prompt (after the turn count), post_tool (right after the governor,
  before any replacement), post_tool_failure, session_end. Lock-free pre-check, one locked
  write only when something changes (~1.2 ms on a 200-entry overlay).
- `token-sparen show <id>` resolves the session like `session disable` (silently skipped when
  unresolved; only an existing overlay is touched), marks best = 1.0 (paid by the next hook) and
  adds the read's tokens to `recovery_read_tokens`; the hook adds tokens for direct reads only.
- Recovery reads are never compressed or de-duplicated (they are what the agent asked for).
- Savings at decision time: `min(1, removed * resends / p95)`, resends =
  `max(1, governor.remaining_turns)` (budget left / per-turn EWMA) else 10; p95 over the last 64
  values per (point, bucket) in `projects/<key>/savings_scale.json` (current value included);
  first observation 0.5, nothing removed 0. A trimming arm that ended as a no-op gets (0, 0).

## 2026-11-26 Task 21 review round 1 (controller rulings)
- `files_in_span`: kept text first, then the elided span (a 400 KB pytest run no longer drops
  the failing file); an edit matches a mentioned file by path suffix **or basename**. A dotted
  word counts as a file only with a path separator or a known source/config extension
  (`cache.put`, `pytest.mark` are not files).
- `normalize_command(cmd, *, project_dir, cwd)` keeps the **resolved `cd` target** (chained cds
  collapsed; relative to the project dir when inside it; dropped when it is the project dir or
  the payload cwd; kept as written without context). `regret.event_signature(event)` uses the
  event's project dir and `extra["cwd"]`; decision log, tracking and the pretrim lookup use it.
- Re-run matching also compares the commands with a trailing filter pipeline stripped
  (`filter_base`: `| grep|egrep|fgrep|rg|head|tail|sed|awk|less|more …`, then `2>&1`).
- Recovery read also when any non-edit tool input field *ends* in
  `.token-sparen/outputs/<id>.txt` (Grep/Glob `path`, MCP tools).
- Re-run within 2 turns is 1.0 only if nothing changed since the decision: an Edit/Write of a
  file the output does not name, or a Bash command outside the read-only allowlist
  (`regret.read_only`: ls cat head tail grep rg find(no actions) git status|diff|log|show pwd
  echo which wc cd, no redirect to a file; unparsable = state-changing) makes it 0.5.
- Elided tokens are stored as 8-byte blake2b hex digests (`token_digest`); prompts and inputs
  are hashed to match. README updated.
- Read de-dupe: a pointer is served once per stored read (entry flag `pointed`); the next
  identical read is delivered in full and refreshes the entry. A regret on a read_dedupe decision
  (`read_hash` in its regret entry) drops that file's `reads` entries.
- Known limitation: regret state is per session, shared by the main agent and its subagents
  (a subagent re-running the main agent's command counts as a re-run).

## 2026-11-26 Task 21 review round 2 (controller rulings)
- Command signatures always carry the **effective run directory**: payload `cwd` plus any
  leading `cd`s, resolved, relative to the project dir, prefixed as `cd <dir> && …`, omitted
  only at the project root (`npm test` in packages/a vs packages/b differ; `cd packages/a && npm
  test` equals `npm test` run from packages/a). Same function for the decision log, regret
  tracking and the pretrim lookup (`regret.event_signature` / `guards._normalise(cmd, event)`).
- Edit-to-mention matching: path equality or path suffix; the basename fallback applies only
  when the mention has no directory (`conftest.py` bare matches; `pkg_a/__init__.py` does not
  match `pkg_b/__init__.py`).
- Recovery-file paths (`.token-sparen/outputs/<id>.txt`, e.g. from our own marker) are never in
  `files_in_span`.
- A re-run that matches only after stripping trailing filters (`| grep ERROR` vs `| grep WARN`)
  scores 0.5; the exact signature keeps the 1.0 rule.
- `read_dedupe.drop_reads(state, path_hashes)` owns the `reads` schema; regret calls it (lazy
  import) when a read_dedupe decision is regretted.
- Known bias: any state-changing command (outside the read-only allowlist) that is not the
  tracked command itself lowers a later quick re-run of *every* open decision to 0.5, even when
  unrelated (e.g. `cd packages/b && npm test` before re-running packages/a). Slight
  under-reporting of regret, accepted.

## 2026-11-26 Task 22: session fold, global deltas, circuit breaker, learner state
- `learn/overlay.py` `fold(session_id, project_key, home)`: (1) stage the overlay (`evidence` and
  `regret_outcomes` move under `folding` with a uuid4 fold id, `evidence` emptied), (2) breaker,
  (3) project merge `cap(project + session)` (ea, eb, es2, n summed, mean-preserving cap of
  `Evidence.observe`, n not scaled, no re-discount), every full bucket also added to each coarser
  back-off level (**coarse buckets are aggregates**), (4) global += session x `global_weight`
  (0.3, all four fields) at every level with `t`; entries untouched > `global_age_days` (90) are
  halved and `t` reset (once per period), (5) pre-trim candidates, (6) clear `folding`, set
  `folded_at`. **Double counting**: evidence is never in both overlay and project (staged first;
  briefly in neither). **Idempotency/crash safety**: each target doc keeps `applied` (last 200
  fold ids); a staged-but-unfinished fold is resumed. A session's first fold always stages (even
  empty), so paused points count down in projects without trims.
- **Lock order** (`overlay.LOCK_ORDER`): project `posteriors.json` held for the whole fold
  (serialises a project's folds); under it `breaker.json`, `global/posteriors.json`,
  `pretrim.json`, overlay, one at a time. Hooks only take the overlay lock alone.
  `jsonstore.locked/update` gained `timeout`; `read_locked`/`write_locked` added.
- Regret outcomes: `RegretDetector` appends every settled payout (and table evictions) to the
  overlay `regret_outcomes` = `[decision_id, point, arm, command_sig, value]` (last 1000), for any
  policy; fold consumes them.
- **Circuit breaker** (`learn/breaker.py`, `projects/<key>/breaker.json`): ring of the last
  `breaker_window` (50) settled regret values of trimming arms per point; evaluated once >=
  `breaker_min_obs` (20, builder choice: no verdict from a handful) are in it; mean > 2 x tau[tier]
  trips -> `remaining = breaker_sessions` (3), ring cleared. Each newly folded session decrements;
  at 0 the point re-enters: its trimming-arm project evidence (all buckets) is deleted. While
  tripped `Engine.decide` returns the safe arm, propensity 1 (`StoreChain.tripped`, cached read).
  The user message is shown once at the next SessionStart (`breaker.notices`, flag `notified`).
  `DEFAULT_TAU` moved to `learn.policy` (engine and breaker share it).
- **Pre-trim candidates**: fold scans `decisions.jsonl` from a byte offset (`scan.offset`, reset
  when the first line's hash changes = rotation); per `command_sig` stats `[count, sum ratio,
  regrets, last ts]` over trimming Bash compression decisions; regrets from outcomes with value > 0
  (any point). Candidate iff count >= 5, mean ratio <= 0.4, regrets == 0 (ever) ->
  `{"lines": 120, "t"}`; a regret or a failing ratio removes it; <= 200 candidates, <= 2000 stats
  (least recent dropped). Keys are `regret.event_signature` values, so `guards.pretrim_lines`
  finds them.
- `learner_state(project_key, home=None)`: safe-mode (any point paused) > cold (< 20 real
  observations: sum over points and arms of the `*` bucket's max n) > exploiting (>= 80 % of the
  last 50 logged decisions chose their bucket's posterior-best arm: max savings mean among
  trimming arms with regret mean <= tau, prior + project evidence; else the safe arm) > adapting.
  The statusline now calls `learner_state(key, state_home(env))`.
- Hooks: SessionEnd `_on_fold_spawn` (after regret settlement) spawns `python3 -I
  <plugin>/hooks/run_cli.py fold --session <id> --project <dir>` (`-m token_sparen` without the
  plugin tree) with stdio DEVNULL, POSIX `start_new_session`, Windows `DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP`. SessionStart `_on_breaker_notice`, then `_on_fold_pending`: up to 3
  unfolded overlays idle >= 1 h (reading <= 50 files), in-process when their overlays + decision
  log + stores are <= 256 KiB, with a 50 ms lock timeout and a 100 ms budget; otherwise (or on a
  busy lock) spawn `fold --all --older-than 3600 --max 3`. `tests/conftest.py` stubs the spawn.
- CLI `fold [--session ID] [--all] [--older-than S] [--max N] [--project DIR]`; one line out;
  retries a busy lock 5 times.

## 2026-11-26 Task 22 review round 1 (controller rulings)
- **Pending-fold index** `projects/<key>/pending.json` `{overlay name: first_seen}`
  (`learn/pending.py`): added on the engine's first reward of a session, on settled regret
  outcomes, and at staging; removed when the fold clears (or finds no overlay). SessionStart reads
  only the index (no directory scan: the old scan stopped finding unfolded sessions behind ~50
  folded ones); `fold --all` = index + full directory scan. Lock order gains `pending.json` last
  (taken alone by hooks; under the posteriors lock only, nothing else held, in the fold).
- **Breaker judges learned decisions only**: outcome rows carry the bucket (6th field); buckets
  `static` / `error` (and legacy 5-field rows) never enter the ring.
- **Trip rule** (controller decision, calibrated by me): evaluate only after `breaker_every` (25)
  new observations; over the last 50 (>= 20) trip iff k/n > 2 tau AND Beta(1+k, 1+n-k).lcb(q) >
  tau. The ruled q = 0.05 failed the target (~64 % false trips at tau over 1000 trims), so q is
  `breaker_lcb_q = 0.0005` (Bonferroni-style over ~40 evaluations): 2.1 % at tau, 39 % at 8 %,
  97 % at 12 %, 100 % at 15 % (2000 runs). Re-entry scales trimming-arm project evidence x 0.25
  (`breaker_reentry_scale`) instead of deleting.
- Fold validates session evidence before writing project/global: known point, bucket `*` or
  distinct `FEATURE_KEYS[point]` keys with values in `[A-Za-z0-9_.*-]`, arm in `point.arms`;
  invalid entries skipped and logged once per fold (`FoldReport.skipped`).
- Global `applied` = `{project tag: [fold ids, last 200]}`, tag = 16 hex of
  sha256(`<home>/secret` : project key); the secret is 32 random hex, mode 0600, created on first
  use. A legacy flat list is dropped.
- `guards._normalise` truncates to 500 characters like the decision log's `command_sig`.
- SessionStart order: `_on_fold_pending` before `_on_breaker_notice` (a trip found by the
  pending fold is announced in the same start).


## 2026-11-26 Task 22 review round 2
- Fold clears the overlay and removes its pending-index entry under one overlay lock, and only
  when the re-read overlay holds no new evidence/outcomes (a racing hook either is seen or re-adds
  its entry afterwards). Overlay -> pending.json matches `LOCK_ORDER`.
- Install secret: written to a 0600 temp file, then `os.link`ed into place (atomic, never over a
  racing winner; `os.replace` fallback without hard links). An empty/malformed file is re-read up
  to 5 times (10 ms apart), then replaced. Outcome rows with an empty bucket are not learned.

## 2026-11-26 Task 23: engine drives every decision point
- `learn/hookpolicy.py` `HookPolicy(cfg, env, project_key, session_id, salt)`: per hook, set in
  `entry._respond` (ContextVar `HOOK_POLICY`); `entry.POLICY` (default None) still pins a fixed
  policy for tests. The Engine (`StoreChain(project_key, home, session_id, params=cfg.data)`,
  `random.Random(os.urandom)`) is built on the first `decide` or reward only; `regret.pay` with no
  payouts no longer touches the policy, so prompt/start/pre-tool hooks never build it.
  `[learn] enabled = false` -> `StaticPolicy`; build error -> static for the hook (logged once);
  a decide error (engine logs it, bucket `error`) -> static for the rest of the hook. `reward` is
  a property: None when disabled/unbuildable, so `regret.reward_fn` skips the savings scale.
- `TOKEN_SPAREN_TEST_SEED` is honoured only with `TOKEN_SPAREN_HOME` set and not resolving to
  `~/.token-sparen`; seed material `seed|session|<kind>|<tool use id>` (varies per hook).
- `ctx_pct`: `governor.ctx_pct(overlay, cfg, env, sid)` from the overlay cache (no transcript I/O;
  None without a last call or when the window is wrong); deciders build `SessionCtx` with
  `governor.decision_ctx`. Compression's private size bands (cutoff 32k) are gone: every point uses
  `features.for_point(..., tokens=before)`; buckets look like
  `band=unk|cat=shell|size=sm|ctype=test_report|phase=mixed`.
- **`[learn.hold] subagent_brief = true`** (builder call): spec 6.7 keeps the brief off until the
  live benchmark and it has no reward signal; unheld, the engine would turn it on for ~half of the
  subagent calls forever (cold `on` regret prior .05 = tau). Held points go through the engine
  (bucket, breaker, pending) with no candidates: safe arm, propensity 1.
- `token-sparen run` (`compression.compress_text`) keeps `StaticPolicy`: no session lifecycle, so
  its trims could never be settled or folded honestly.
- Compression: any no-op end of a trimming arm (nothing worth trimming, save deadline, never-larger
  check) pays savings 0 + regret 0 at once; the safe arm is never rewarded (tests).
- Measured (in-process PostToolUse, 50 KB pytest, 200 calls, this machine): static p50 15-16 ms /
  p95 22-23 ms; engine p50 18.5-20 ms / p95 28.6-30 ms (cold: ~81 % light, rest off). Under the
  50 ms gate, so the pending write is not batched. Perf test now runs with the engine (seeded) and
  measures the compressing calls only.
- Learning curves via `entry.main` (seeded, 10-27 KB pytest outputs; identical outputs saturate
  every savings reward at 1 because the bucket p95 equals the value): recovery read after every
  trim -> 3-4 light trims in the first 30, then 97-99 % off in the next 100 (5 seeds). Never
  regretted (sessions of 40 rounds, folded): standard first chosen at round 30-35, aggressive at
  81-169; last 80 of 400 rounds 46-63 of 80 standard/aggressive. Exploitation stays loose because
  savings posteriors of the trimming arms are close (p95-normalised, capped at mass 50). Cross
  project: after A's 6 folded sessions, fresh B chooses standard from its first decisions (5-10 of
  20 standard/aggressive) via global back-off evidence; an untrained home: 0 of 20.
- Existing tests that pin static arms through the hook (two pipeline tests) set
  `TOKEN_SPAREN_LEARN__ENABLED=false`; the subprocess e2e test is seeded and expects `light`.
- `doctor`'s synthetic hook round trip runs with `TOKEN_SPAREN_LEARN__ENABLED=false` (it checks
  the pipeline; a cold learner draws `off` ~16 % of the time and doctor would report a broken
  install). Tests: a suite-wide autouse fixture seeds the in-process engine per hook
  (`"suite"` when a test sets no seed; marker `real_seed` opts out) so hook tests are deterministic.
- Hook latency with a large store (~600 compression buckets, 263 KB project + 263 KB global
  posteriors.json, parsed every deciding hook): p50 29 ms / p95 38 ms. Still under 50 ms; store
  size is the next latency lever (compaction or a per-point split) if it grows further.

## 2026-11-26 Task 23 review round 1 (controller rulings)
- **Project config may only tighten learning safety** (`config.SAFETY`, `config.safety_param`):
  `Config.trusted` = defaults + user file + env + session overrides (everything but the project
  file); `load_config` resolves each registered key once (so the engine, hierarchy, breaker and
  fold all read the resolved value): a project value looser than the trusted one is replaced by it
  and logged once per (project hash, key, value) via `~/.token-sparen/config_safety_warned.json`.
  Directions: `learn.hold.*` on; `learn.optin.*`, `learn.enabled`, `extensions.allow_override`
  off; `learn.tau.*` min; `learn.floor.*`, `ladder_obs`, `breaker_lcb_q`, `breaker_sessions`,
  `breaker_reentry_scale` max (WRONG, corrected in round 2: user/env/session only);
  `breaker_min_obs`, `breaker_every` min; `breaker_window` fixed (neither direction is uniformly
  safer). A switch only the project sets must be the safe side; a number with no trusted baseline
  passes through (readers validate it). Probe before the fix: a project `hold = false` turned the
  subagent brief on for 32 of 40 calls.
- `config.flag_off`: false, 0, "0", "no", "false", "off" (any case) -> `learn.enabled` off in both
  `HookPolicy` and `Engine.enabled`.
- `TOKEN_SPAREN_TEST_SEED` is honoured only when `TOKEN_SPAREN_HOME` resolves inside
  `tempfile.gettempdir()` and is not the default root.
- Accepted: an engine/store error falls back to the static policy (known-good, E2-tested) for that
  hook; a corrupt `breaker.json` reads as not tripped.
- M4 items: savings reward as the fraction removed (`removed/before`, optionally x
  `min(1, resends/R)`) or counterfactual `removed/max_removed`, validated on E1 before changing;
  1M-window models without the statusline sensor are mis-banded (governor window table).

## 2026-11-26 Task 23 review round 2 (controller ruling: allowlist)
- Supersedes the round 1 denylist (`SAFETY`/`safety_param` removed). Re-review bypass: a project
  `learn = 1` (or `[learn] tau = 0`, `floor = 0`, `hold = 0`) replaced tables with scalars, no
  leaf was found to check, readers fell back to packaged defaults and the **user's** stricter
  values were lost (user `enabled = false` -> enabled). Also the README promise covered keys that
  were not protected (`regret_window_turns`, `learn.defaults.*`, `pretrim_*`, `gamma`, `kappa_*`).
- Now `config.sanitise_project(project, trusted, defaults)` runs **before** merging: the project's
  `learn` subtree and `extensions.allow_override` are stripped (a scalar `extensions` too), then
  each leaf of the project's `learn` (a scalar replacing a table is a leaf) is re-added only if it
  is in `PROJECT_LEARN_KEYS` and a scalar of the right type tighter than the trusted value
  (number compares fall back to the packaged default when the trusted value is invalid; a hold
  for a point with no trusted value compares against false). Allowlist: `learn.enabled` false,
  `tau.<tier>` min, `floor.<tier>` max, `hold.<point>` true, `ladder_obs` max, `breaker_min_obs`
  and `breaker_every` min, `breaker_lcb_q` and `breaker_sessions` max. Dropped leaves are logged
  once per (project hash, key, value). Other sections keep normal precedence.
- **`breaker_reentry_scale` correction**: round 1 called higher safer; wrong. It scales savings,
  regret and n of the trimming arms alike, so a higher value also keeps more n for the ladder
  (looser). It and `breaker_window` are user/env/session only.
- Test change: `test_hierarchy::test_user_config_overrides_apply` now writes its learn overrides
  to the user config (they were in a project file, which is no longer allowed).

## 2026-11-26 Task 23 review round 3 (controller ruling: deny-by-default project layer)
- Adversarial re-review: project `learn.enabled = false` fell back to StaticPolicy (standard,
  pointer) and ignored the user's `hold.compression = true` ("kept 1 of 3000" 20/20); project
  `[recovery] ttl_days|max_files|max_bytes = 0` made recovery impossible; project
  `[compression.levels.standard] keep_ratio = 0, head = 0, tail = 0` overrode the user's levels;
  plus knobs, `min_saving_ratio`, `skip_tools` wipe, `max_turn_gap`, hard stop/budget,
  `max_read_bytes`; `_is_num` raised OverflowError on huge ints.
- Principle: a cloned repository's config may only make token-sparen intervene less or keep
  more. `config.PROJECT_POLICY` (ordered patterns, `*` one segment, `**` any rest, first match)
  classifies every leaf: free, user_only, max, min (with bounds; int defaults need ints), true_only,
  false_only, union (`skip_tools`: trusted list + additions; env comma strings understood), enum
  (`compiler.warnings` toward `keep`). Every key of `defaults.toml` has an entry (test); section
  catch-alls (`learn.**`, `compression.**`, `governor.**`, ...) and unlisted sections are
  user-only. Comparisons are against the trusted value, or the packaged default when the trusted
  one is missing or the wrong type. Builder calls beyond the ruling's list: `persisted_max_bytes`,
  `json.max_bytes`, governor parsing/EWMA/windows user-only; `read_dedupe.max_entries` min;
  `guards.pretrim_*`, `read_dedupe.min_tokens` max; `pricing.*` free (only `estimate.Prices`, which
  nothing in the hook path uses).
- `learn.enabled` is user/env/session only; a project wanting less trimming uses `learn.hold`.
- `_is_num` catches OverflowError (300-digit ints rejected).
- Tests that configured compressor levels / search knobs / `min_tokens = 700` through a project
  file now use the user config (they test the loaders, not the project layer).

## 2026-11-26 Task 23 review round 4 (controller rulings)
- `compression.levels.*.head/tail`, `guards.pretrim_head/pretrim_lines`: user-only. The level
  budget is max(head + tail, keep_ratio x total), so a larger head/tail displaces prompt-relevant
  lines instead of keeping more (probe: head 329 kept 5 of 120 relevant lines). `keep_ratio`
  stays raise-only.
- "<= 0 means default" audit: `read_dedupe._cfg_int` (`max_entries`, `min_tokens`), `guards._int`
  (`max_read_bytes`), `entry._budget_ms` (`latency_budget_ms`) map <= 0 to the default, so their
  project bounds are `POSITIVE` (1..1e12). `read_dedupe.max_turn_gap` (< 0 -> default, 0 is
  valid), compressor knobs (`_shared.setting`: non-negative ints), recovery limits (no fallback;
  raise-only), breaker `min_obs`/`every` (bounds >= 1) and `tau`/`floor` (clamped) need nothing more.
- Free keys are listed one by one (no wildcard free rule; test). `compression.save_deadline_ms` and
  `adapter.latency_budget_ms` are lower-only; `boundary.history` free within 1..20;
  `statusline.parse_budget_ms` free within 1..1000; `adapter.latency_sample_rate` and
  `latency_log_max_bytes` user-only.
- The `/token-sparen` skill pre-approves `session disable` only (both the `token-sparen` and the
  `run_cli.py` form); `session enable` turns trimming back on and so asks for permission.

## 2026-11-26 CI perf failure after Task 23 (root cause: per-hook cost grew with the session)
- CI perf (ubuntu, 3.12): `test_hook_latency_p95` p95 160 ms (gate 75 ms). Profiling the 200-call
  loop: per-call time grew linearly with the call index (local median first 20 / middle / last 20:
  9.9 / 18.4 / 34.1 ms). The session overlay reached ~400 KB: the regret detector's decision table
  (up to 200 entries of ~2.2 KB, mostly the hashed elided tokens and file mentions; the perf loop
  has no prompts, so nothing expires) plus the engine's `pending` (up to 500 entries). Every
  PostToolUse parsed the overlay ~16 times and rewrote it ~6 times (governor, regret, compression,
  engine decide/reward, ledger, tracking). No fsync, no lock waits, no rotation involved.
- Fix: the decision table moves to `sessions/regret/<id>.json` (`regret.table_path`) and pending
  decisions to `sessions/pending/<id>.json` (`engine.pending_path`); the hot overlay stays ~1 KB.
  Subdirectories, so `sessions/*.json` scans never see them. Locks are taken one at a time (sidecar,
  then overlay; never nested). Regret: table change under the sidecar lock, then outcomes/ledger/
  forgotten reads under the overlay lock (a crash in between loses those outcome rows, payouts
  still happen). Engine reward: kinds are claimed under the pending lock first, then the evidence
  is written under the overlay lock (a crash in between loses one observation, never doubles it).
- After (same loop): local 10.4 / 13.3 / 16.5 ms, p95 17.3 ms (before 28-34). Linux container
  (python:3.12-slim, 1 CPU): p95 29.3 -> 13.7 ms, last-20 median 28.1 -> 12.9 ms. At 0.5 CPU p95
  stays ~67 ms in both versions: CFS quota throttling spikes, not session growth.
- Remaining growth is the regret sidecar (bounded by 200 entries, ~1.5 ms/parse locally), read
  ~2x and written ~1-2x per trimming hook; its entries could be packed (one string per field,
  ~30 % cheaper JSON) if needed.
- `test_session_start_folds_old_unfolded_sessions` flaked on a slow macOS runner (100 ms
  in-process fold budget): `overlay._monotonic` is now injectable; the test freezes it, and a new
  test drives a ticking clock to check the budget stops the loop.

## 2026-11-26 Compact regret table (CI: test_regret_cost 4.82 ms vs 4.5 ms gate)
- Sidecar rows are positional lists (`regret._FIELDS`, key `rows`): the 64 token digests are one
  packed string (16 hex each), files one newline-joined string (deduped, capped at 32),
  `command_base` null when equal to `command_sig`, `changed_since` 0/1. The hot path never splits
  them: digests are matched with `str.find` at 16-char boundaries (`_hits`), and an edit only
  splits an entry's files when the edited file's name occurs in the packed string (every match
  rule contains it). `read_table(overlay)` returns fully expanded entries (tools, tests).
- Backward compatible: the previous `{"regret": {id: {field: value}}}` format is still read and
  rewritten as rows on the next change (in-flight sessions keep their watched decisions).
- A first attempt that expanded every row on load was slower (1.9-3.1 ms): decoding cost beat the
  JSON savings. Per-hook regret cost, 200-entry table, median / p95 ms (grep, edit, prompt event):
  before local 1.22/1.55, 2.57/2.83, 1.20/1.46; after 0.93/1.12, 0.99/1.12, 0.93/1.05. Linux
  container (1 CPU): before 1.35/1.54, 2.55/2.70, 1.37/1.64; after 0.97/1.91, 0.98/1.23,
  0.97/1.12. Table 484 KB -> 385 KB. Reads per hook: one lock-free read, plus one locked
  read-modify-write only when something changes (minimal for the lock-free fast path).

## 2026-11-26 Task 24: E1 offline replay and E3 off-policy evaluation
- `token-sparen replay <paths...> --policy static|engine|<arm> [--split] [--cutoff] --out` and
  `token-sparen ope --log <decisions.jsonl...> --target static|engine-snapshot --out`. Paths must be
  explicit (directories recurse `*.jsonl`). `replay/report.py`: `--out` is refused under the git
  top-level of the cwd, under the package's own checkout, and anywhere inside a git work tree
  (`git -C <nearest existing ancestor> rev-parse` succeeds, or an ancestor holds `.git`, so it
  also works without git). Atomic write, mode 0600, new parent dirs 0700. Stdout and errors carry
  aggregates only: failures print `replay failed (<ExceptionType>)`, never a message; a failing
  session is counted (`sessions_failed`) without details. `config.redacted_logs()` (ContextVar)
  makes `log_error` keep only `where` during replay (compressor exception texts could quote
  transcripts).
- Replay reuses the hook path: the same gates (`compression.skipped`, recovery reads, Claude
  Code `translate.replaceable` on the record's `toolUseResult`, failed results, 2000-token floor),
  `detect`, `features.for_point`, `load_level`, and `compression.pick_candidate` (extracted from
  `handle_post_tool`: never-larger rule, marker of the real length). Built-in and entry-point
  compressors only (`registry.for_type`); project drop-ins are never executed offline.
  `--policy engine` = `FrozenEngine`: `Engine.choose` (new, side-effect free; `_decide` uses it)
  per project key (from the session's `cwd`), no pending entries, rewards or writes.
- Heuristics (in the report's `heuristics`): re-sends = later API calls (new message ids of the
  file's own chain) before the next compaction (`compact_boundary` system record or
  `isCompactSummary` user record; `type: summary` title records ignored). Cost: the first later
  call prices removed tokens as cache_write, each further one as cache_read; a result followed by
  no call saves nothing; baseline = every call's context priced the same way (output excluded).
  Proxy regret: >= 2 distinctive tokens found **only** in the elided span
  (`regret.distinctive_tokens`, excluding kept text and the tool input; digests; minus every token
  already in the session's context before the trim, and minus tokens that reappear in a later
  prompt or tool result) in one later assistant text block or **non-edit** tool input (as the
  live detector) within `regret_window_turns` user turns; <= `MAX_WATCHED` (200) spans, oldest
  evicted unregretted (`evicted`); context digests capped at 500 000 per session. Split by the first record `timestamp` (else mtime); default cutoff 2026-11-01.
- `transcript.py`: turn counting skips sidechain, `isMeta` and compaction-summary records
  (`is_prompt`); a read error mid-file ends iteration; a too-deep line (RecursionError) is
  skipped; `ToolResultRecord` gained `is_error`, `raw` (`toolUseResult` only when the record holds
  a single result), `sidechain`; tool uses are dropped once answered (cap 1024). `iter_events`
  streams one chain (the first record's `isSidechain`: subagent files replay as sessions, inline
  sidechain records of a main file are skipped).
- OPE (`learn/ope.py`): reward = removed/before - regret/tau[tier] (safe arm 0); the log line's
  optional `regret` value, missing = 0 and reported as `regret_known_fraction` (decision logs do
  not carry settled regret yet: today E3 sees savings only; joining regret outcomes to decisions
  is a follow-up). Weights clipped at 20 (`clipped_fraction`). Support: logging propensity <= 0
  -> the decision is unsupported (contributes 0); propensity 1 -> the target's mass on other arms
  is `unsupported_mass`, and DR does not extrapolate its model there. Engine targets use
  `Engine.action_probs` (new: exact, unclamped; ladder-locked arms 0; breaker-paused -> safe 1);
  buckets `static`/`error` have no recoverable context (`no_context`). Percentile bootstrap, 1000
  resamples. Synthetic coverage: with rare regret events (x20) small-n percentile intervals
  under-cover (31/50 at n = 200), so the unbiasedness test uses n = 1000 (46/50); bias over 400
  seeds is within one standard error.

## 2026-11-26 Task 24 review round 1 (controller rulings)
- E1 baseline exists privately (owner's machine); not recorded in the repo.
- SNIPS is reported **conditional on support**: normalised over fully supported decisions only
  (`rows`); a deterministic logger's row where the target also puts mass elsewhere is left out
  (before, SNIPS filled that mass with the supported rows' value). `ope --holdout-after
  YYYY-MM-DD` evaluates only later decisions (engine-snapshot on its own training logs is
  in-sample); report `notes` state units, the support caveat (a lower bound for stochastic
  loggers) and missing regret. An `EngineTarget` with `[learn] enabled = false` is the static
  policy. Every report directory the writer creates is 0700.
- Proxy regret (supersedes the Task 24 entry's rule): edit inputs count again (the only-in-span
  filter already drops text the agent saw elsewhere, so Read -> Edit of elided text is caught);
  the 64 span tokens are sampled after dropping seen tokens and ~60 common 8+ letter English words
  (`replay.STOPWORDS`); path-like tokens match by path or basename (one token); a user prompt
  with >= 2 tokens is partial 0.5 (span stays watched); a re-run of the same `regret.signature`
  (non-edit tool) is full 1.0. A trim keeps its max; partials are paid at expiry, eviction or
  session end; `proxy_regrets` is the sum of values (`proxy_regret_full` / `_partial` counts).
- Message and tool-use ids are de-duplicated across all transcripts of a run (8-byte digests,
  cap 5 000 000, `ids_capped` in the report): a resumed session's copied history is neither
  re-sent nor re-trimmed (`duplicate_results`). `transcript.iter_events(..., is_new_id=)`.
- Report heuristics state units (estimated tokens; baseline from API usage) and that cache TTL
  is ignored (conservative).

## 2026-11-26 Task 24 review round 2 (replay breakdowns)
- Replay report adds `per_tool` (`<tool category>/<content type>`, same fields as `per_ctype`;
  categories from `translate.tool_category`: shell = Bash, read, search, web, agent, mcp, other),
  `regret_signals` (the signal that settled each regretted trim: assistant_text, tool_input,
  edit_input, user_prompt, rerun; path_mention overlaps when a matched token was a path or its
  basename) and `regret_turns` (user turns from trim to signal: 0, 1, 2, 3-6, 7+).
- `--policy light,standard,aggressive` replays every listed policy in one pass over the
  transcripts (one `_Session` lane per policy; each arm compresses each result once); ids are
  de-duplicated once per run before the lanes see an event. `by_arm` holds each lane's totals
  and splits; `totals`/`by_split` are the first lane's.

## 2026-11-26 Task 24 review round 3 (command families, net savings)
- `replay/families.py` `command_family(cmd)`: normalised command, first simple command that is
  not `cd`/`pushd`, wrappers skipped (sudo, time, nohup, env, npx/bunx/uvx, uv/poetry/pipenv
  run, python -m), program (+ subcommand for git, npm/pnpm/yarn/bun, cargo, go, pip/uv, make,
  gradle, mvn) -> test, build (linters and type checkers included), install, listing, file_dump,
  vcs_read, container_cloud, http, other. Report `per_family` covers Bash results only.
- Net savings: each regretted trim is assumed to cost one recovery read of the full output
  (tokens_before x regret value; a partial charges half, a later full the other half) queued in
  the turn the regret fires and priced like removed tokens (cache write on the next call, cache
  read after, nothing after a compaction). `recovery_cost_weighted`, `net_cost_weighted_saved`,
  `net_saved_share_of_context_cost` per total, content type, tool and family.

## 2026-11-26 CI after Task 24 (Windows report writer, perf flake)
- Windows: `report.write_private` called `os.fchmod` (absent on Windows before Python 3.13):
  AttributeError -> `replay/ope failed` exit 1; the cleanup then unlinked the temp file while its
  descriptor was still open (WinError 32). On 3.13 the call exists but Windows only has a
  read-only flag (modes read back 0o666/0o777). Now the file object owns the descriptor
  immediately, modes are set only on POSIX (`report.POSIX`), and on Windows the report relies on
  inherited directory ACLs (documented: keep it in the user profile; no stdlib ACL API). Mode
  tests run on POSIX only; CLI exit-code assertions show stderr.
- `test_huge_and_binary_output` (default suite) asserted an absolute 600 ms on 10 MB and flaked
  at 641 ms on a macOS runner. It now checks scaling only (10 MB / 1 MB <= 15) plus a 5 s hang
  guard; the calibrated absolute budget stays in the perf job (`test_strict_budget_10mb`).

## 2026-11-26 Task 24b: quality-first compression defaults (owner decision)
- Evidence (E1 on the owner's own sessions, private; figures stay outside the repo): most
  trimming savings came from file contents the agent then used, and net savings after recovery
  re-reads were near zero. Owner decision: quality first.
- `compression.gated(event, ctype, cfg) -> str | None` (pure, no I/O) runs **before** the
  decision point in `handle_post_tool` (after the deny-set, replaceable, recovery-read and
  2000-token gates and `detect`; before the persisted file is read): a gated output gets no
  `DecisionRecord`, no policy call (static, engine or pre-trim), no reward, no regret watch, no
  recovery file. Reasons: `file_content` (the `read` tool category, any content type; or a
  `shell` command whose family is `file_dump`) unless `[compression] trim_file_contents = true`;
  `ctype_not_allowed` when the detected type is not in `[compression] allow_ctypes` (default
  diff, search, test_report, compiler, build_log, json, pplx, log; text, table, stacktrace not
  allowed for lack of evidence). `token-sparen run` goes through the same handler, so it is
  gated too (it is the wrapper for hosts without output replacement).
- Config: `allow_ctypes` kind `subset` (new; a project list is accepted only if every entry is
  in the trusted list, merged as the trusted list filtered to it; the env form is
  comma-separated), `trim_file_contents` kind `false_only`. Env and session overrides still
  beat the project layer. `allowed_ctypes` reads a list or a comma string; anything else -> the
  defaults. `trims_file_contents` is on only for an explicit true/1/yes/on.
- Classifier moved to `compress/families.py` (shared by the gate and E1; `replay.command_family`
  re-exported). Fixes: chained (not piped) setup segments skipped (`cd`, `pushd`, `source`, `.`,
  `export`, `set`, `echo`, `printf`, `mkdir`, ...; a lone/last one is classified as itself),
  wrappers with option arguments (`sudo -u U`, `env -u V`, `nice -n N`, `timeout [-s S] N`,
  `xargs -n N`), `bash|sh|zsh -c '<script>'` classified recursively (depth <= 3),
  `python3.X -m`, JS `exec`/`dlx`/`x` and direct tools (`yarn jest`, `pnpm exec tsc`), a later
  pipeline stage `xargs <dump>` -> file_dump, `git show <rev>:<path>` and `git cat-file -p|blob`
  -> file_dump (plain `git show <rev>` stays vcs_read), `od`/`hexdump`/`tac`/`zcat` added.
- `ls -R` listings sniff as `text`, so they are gated (`ctype_not_allowed`); the search
  compressor would not trim bare paths anyway. Glob/Grep (search category) are allowed.
- Replay: `--policy default` = the gate + the static arm; `engine` is gated too (production
  gates it); `static` and arm names stay ungated (comparable with earlier E1 runs). Gated
  results are counted in `gated` by reason and are not `eligible`. A `rerun` regret charges no
  recovery read (the re-run is already in the baseline); `recovery_by_signal` splits recovery
  tokens/cost by the signal that charged them.
- Tests that exercised trimming of plain text or file contents now opt in (env
  `COMPRESSION__ALLOW_CTYPES=text`, `COMPRESSION__TRIM_FILE_CONTENTS=true`), each paired with a
  default-behaviour test; the config-safety probes use a timestamped log (an allowed type) so
  they keep their teeth. E2 calls compressors directly (no gate), so its cases are unchanged.
- Consequence: the learned pre-trim (`make build` noise etc.) only acts on allowed types; plain
  text noise is now left whole unless the user widens `allow_ctypes`.

## 2026-11-26 Task 24b fix round 1 (controller rulings)
- `families.is_file_dump` (gating only, conservative) is true if **any** simple command dumps a
  file: split on `&&`, `||`, `;`, `|`, `&` and newlines (raw command, not normalised: that
  collapses newlines and truncates at 500 chars); `do`/`then`/... keywords dropped, `for ... in`
  word lists ignored; `find -exec/-execdir/-ok`, `fd -x` commands; `bash -c` scripts; `xargs
  <dump>`. A dump program counts only with a file operand (or `< file`): `... | head`, `sed -n
  1,9p` on a pipe, `jq .` on a pipe and here-documents are not file dumps (per-program option
  tables; sed/awk/jq skip the script operand unless `-e`/`-f`). Added for gating: `sort`, `uniq`,
  `cut`, `rev`, `strings`, `tac` with a file operand; `python -c`/`node -e` code containing
  `open(`, `read_text(`, `read_bytes(`, `readFileSync`, `readFile(`; PowerShell/cmd
  `Get-Content`, `gc`, `type` (case-insensitive). `command_family` keeps the first program (E1
  comparability); combined short wrapper options (`sudo -iu U`) fixed for both.
- `compression.is_file_content` also gates any tool whose name matches (case-insensitive)
  `read_?file|get_file|file_read|read_text|cat_file|view_file|open_file` (MCP tools included),
  and `PowerShell`/`pwsh` tool commands go through `is_file_dump`.

## 2026-11-26 Task 24b fix round 2 (controller rulings)
- Gating detection moved to `compress/filedump.py` (`is_file_dump`, `is_file_tool`; one place;
  `families.is_file_dump` delegates). Adds: `$(...)`/backticks (also in double quotes, not in
  single quotes, not `$((...))`) and `eval` unwrapped; stdin from a file (`<` token: `< f`,
  `0< f`, `<f`, `done < f`, `$(< f)`) counts, here-docs/here-strings do not; `dd if=` (output to
  stdout), `cp f /dev/stdout|/dev/stderr|-`; wrappers ssh, su -c, docker/podman/nerdctl
  exec|run and compose exec|run, kubectl exec -- (without `--`: not unwrapped), adb shell,
  script -c, watch, unbuffer, ionice, strace/ltrace, chroot, busybox, coreutils, g-prefixed
  coreutils, powershell/pwsh -Command; dump binaries fold, fmt, paste, column, pr, expand,
  unexpand, base64, xzcat, bzcat, zstdcat, view, most, batcat, highlight, pygmentize, glow,
  mdcat, yq (with a file operand), decompressors with -c/--stdout, `tar -xO`/`--to-stdout`,
  `unzip -p`, gsutil cat, gcloud storage cat, `aws s3 cp <src> -`; interpreters: perl/ruby
  `-e` code that reads a file or a file operand after the code (`perl -ne '...' f`), ruby
  File.read/readlines/IO.read, node createReadStream, python fileinput, PowerShell
  `[IO.File]::ReadAll*` anywhere in the command. grep/rg/git blame/git log -p stay allowed.
- Tool names: a `read` token (camelCase/underscore split), a verb (get, fetch, download, view,
  open, cat) with a noun (file(s), document(s), media, resource(s), content) or run together
  (`readfile`), or a bare last MCP part `cat`/`view`/`read`/`open`.
- README: detection is best effort; opt out with `[learn.hold] compression = true` or
  `TOKEN_SPAREN_DISABLE=1`.

## 2026-11-26 Task 24b fix round 3 (controller rulings)
- `filedump` also: `kubectl|oc exec POD CMD` without `--` (options -c/-n/-f/--context skipped,
  pod dropped), `oc rsh`, `docker-compose exec|run`; `fd -x|-X|--exec|--exec-batch CMD` gets an
  implicit path operand (treated like `xargs CMD`); wrappers nsenter, flock (`-c`), setsid,
  taskset (mask dropped), sshpass, `sg G -c`, `vagrant ssh -c`, `gcloud compute ssh --command`,
  `multipass exec I --`, lima, `parallel CMD :::` (implicit operands); reads `docker|kubectl cp
  SRC -|/dev/stdout`, `hg cat`, `openssl ... -in f` (no -out or -out to stdout), iconv, brotli/
  lz4/lzma -dc, zless/zmore/bzless/xzless/..., `dos2unix f /dev/stdout`; php `-r`, lua `-e`, awk
  `getline [var] < f`; here-documents: a shell interpreter's body is classified as commands,
  python/node/ruby/perl/php/lua bodies are searched for reads, any other body is data (dropped:
  fixes the round-2 `cat <<EOF` body false positive); match-everything searches (grep, egrep,
  fgrep, rg, ag, ack, z/bz/xz-grep with pattern `''`, `.`, `^`, `$`, `.*`, or `rg --passthru`,
  and at least one file/dir operand; -l/-L/-c/-q/-o excluded). Tool-name verbs add load, show;
  nouns add blob, raw. README: scripts you run are not inspected.

## 2026-11-26 M2 close-out code fixes (C1-C6)
- C3: one truthy parser. `config.flag_on` (true/1/yes/on, any case), `flag_off` (false/0/no/off),
  `flag(value, default)` (anything else keeps the default). Used by `learn.hold.*` (via
  `learn.features.held`: an unrecognised value falls back to the packaged default),
  `learn.optin.*`, `extensions.allow_override` (now also yes/on: it is user-only anyway),
  `TOKEN_SPAREN_DISABLE`, every `*.enabled`, `governor.hard_stop`,
  `compression.trim_file_contents`, `statusline.ascii/color`. The project-file policy stays
  strict (`true_only`/`false_only` accept only real booleans; a string is dropped and logged).
- C2: `[learn.hold] compression = true` (any on spelling) is one off switch for output trimming.
  `StaticPolicy(arms, held=...)` gives a held point its safe arm; `features.static_policy(cfg)`
  builds it from the merged config and is used by `HookPolicy` (learning disabled, engine
  build/decide error), `Engine` (disabled), `token-sparen run` and OPE's disabled-engine
  target. `features.HOLD_IMPLIES`: a compression hold also holds `pretrim`. Decisions are still
  logged (safe arm, propensity 1). `compression.allow_ctypes = []` is the config-only switch
  (gated before any decision; tested). `replay --policy static` and `ope --target static`
  evaluate the plain static arms (hypothetical policies, not the user's config).
- C1: settled regret reaches the decision log. Each settlement appends
  `{"kind": "outcome", "decision_id", "regret", "signal", "ts"}` to `projects/<key>/decisions.jsonl`
  (`learn.decisions.outcome` / `append_outcomes`, same size cap and rotation, one lock per
  batch). Written live by `RegretDetector._overlay_after` (signals, window `expiry`,
  `session_end`, `evicted`; the regret table row gained `best_sig`, 14-field rows still decode,
  signal `unknown`), by `token-sparen show` via the next settle, and by a trimming `no_op`
  (compression). The fold still consumes the overlay's `regret_outcomes` (rows now carry a 7th
  element, the signal; 5-7 accepted) and never writes outcome lines, so nothing is duplicated.
  `ope.load_log` joins outcomes by id (max per id; orphans dropped); `regret_known_fraction` =
  evaluated decisions with a joined outcome (safe-arm decisions have none: their reward is 0 by
  definition). Readers that want decisions only skip lines with a `kind`.
- C4: `ope` reports `savings`, `regret` and `combined` (each IPS/SNIPS/DR with 95 % CIs; the
  top-level `ips`/`snips`/`dr` keys stay the combined objective `savings - regret / tau`), plus
  `tau` (the point's tier; the CLI passes the merged config's `[learn.tau]`), `regret_upper`
  (the larger upper bound of IPS and DR regret; SNIPS is conditional on support, so not used)
  and `constraint_ok = regret_upper <= tau`. Promotion reads the constraint first, savings
  second; the combined scalar is kept for continuity, not for decisions.
- C5: `stats` reports savings like the statusline: `tracking.ledger_savings(state)` = (gross
  `tokens_removed`, net = gross - `recovery_read_tokens` - `injected_tokens`), used by both.
  Project totals sum every session overlay of the project (the `run` CLI session included:
  its trims are real); JSON adds `gross_saved`, `net_saved`, `recovery_read_tokens`,
  `injected_tokens` and per-session `gross_saved`/`net_saved`; the old `tokens_before`,
  `tokens_after`, `saved` stay (decision log, current generation; text labels them so).
- C6: `plugin.json` `engines["claude-code"] = ">=2.1.121"`. Source: the installed CLI's
  changelog (Claude Code 2.1.332): 2.1.121 "PostToolUse hooks can now replace tool output for
  all tools via `hookSpecificOutput.updatedToolOutput` (previously MCP-only)"; the `engines`
  key itself is honoured from 2.1.323 (older hosts ignore it). `claude plugin validate`
  passes (no engines warning). Tested with Claude Code 2.1.332.

## 2026-11-26 M2 close-out fix round 1 (coordinator rulings)
- OPE constraint is judged per trim: `regret_per_trim` = target regret mass / target trimming
  mass (IPS: sum w r 1[trim] / sum w 1[trim]; DR: model on settled trims + weighted residual,
  over the exact supported target trimming mass), `optimistic` (unsettled trims dropped) and
  `pessimistic` (unsettled trims = regret 1), bootstrap CIs. `constraint_ok` = max pessimistic
  upper (IPS, DR) <= tau; null + `constraint_status: "insufficient"` when
  `unsupported_trim_fraction` > `[ope] max_unsupported_trim` (0.2) or `outcome_coverage` <
  `[ope] min_outcome_coverage` (0.8) or no ratio; `no_trims` (ok) when the target never trims.
  `[ope]` is user-only. `regret_upper` is gone (replaced by `constraint_upper`); the
  per-decision `regret` part stays as information only.
- `regret_known_fraction` denominator = evaluated non-safe-arm decisions (1.0 when none).
- `ope --target engine-snapshot` reads every log once for the outcome join (global by decision id, across rotation generations and files), then maps each decision to its project's engine.
- `defaults.toml [learn.hold]` lists `compression`, `pretrim`, `read_dedupe = false` so `token-sparen config set learn.hold.<point> true` (which only accepts known keys) can hold a point for one session.
- Fold step 5b backfills outcome lines for staged `regret_outcomes` rows missing from `decisions.jsonl` / `.jsonl.1` (dedupe by decision id; `FoldReport.backfilled`), so a busy or failing live write loses nothing. The live write stays primary.
- Fix round 2: the pessimistic upper bound is max(bootstrap uppers of IPS/DR, `ope.exact_upper`
  = Beta(k+1, n-k) 95 % quantile, k = pessimistic regret sum, n = logged trims; 1 when k = n);
  `[ope] min_trims = 60` (user-only) else `insufficient`. Stochastic-logger support: a trimming
  arm is supported only if the log shows it chosen with propensity > 0 in the same (point,
  bucket); otherwise its target mass is unsupported.

## 2026-11-26 Task 25: fact sanitiser and deterministic miners (M3 start)
- `token_sparen/facts/sanitize.py`: `Fact(kind, subject, value, evidence_sessions)`,
  `sanitize(fact, project_dir=None)`, `render` (fixed templates; raises on an unsafe fact),
  `fact_id` = sha1(kind|subject|value)[:12]. Both subject and value must fullmatch the
  single-space command-token regex, <= 120 chars, printable ASCII; rejected: `://`, backticks,
  `$(`, the instruction words (substring, case-insensitive, so `systemctl`/`.gitignore` are
  rejected too: conservative), secret-like text (`[A-Za-z0-9_-]{32,}`, `sk-`, `gh[pousr]_`,
  `github_pat_`, `AKIA`/`ASIA`, `xox`), `~`, drive letters, `..` segments, absolute paths unless
  inside `project_dir`. `requires_env` values must be an ALL_CAPS name. The store re-sanitises
  without a project dir, so stored facts never hold absolute paths.
- `token_sparen/facts/miners.py`: miners per the coordinator rulings; mining is fold-only
  (`overlay` step 5a, between pre-trim and outcome backfill; lock `facts.json` after
  `pretrim.json`); transcript read with `transcript.iter_tool_results(max_bytes=50 MB)`
  (bounded binary reader, the cut is logged). Command alias needs an exact argv-tail match and a
  head that launches the missing program (launcher allow-list, `python -m X`, or a path ending
  in `/X`), so `echo X ...` or `pip install X` never qualify. `generated_dir` needs >= 2
  Write/Edit hits per session (so >= 2 sessions give >= 4 >= 3 hits) - the store keeps no hit
  counts, which keeps re-folds idempotent. `decisions.jsonl` carries no session id or success
  flag, so the miners read only the transcript.
- `facts.json` v1: `{version, facts: {id: {kind, subject, value, sessions (blake2b-8 digests,
  last 64), first_seen, last_seen, contradictions}}, applied: [fold ids]}`, at most 500 facts
  (least recently seen dropped); a newer version is left untouched. `eligible(doc)` = >= 2
  distinct sessions (activation is Task 26).
- The session overlay records `transcript` and `project_dir` at SessionStart
  (`tracking.record_transcript`) and on each prompt when missing/changed. SessionStart
  (`fold_pending`) spawns the detached fold instead of folding in-process when a pending session
  has a transcript, so a hook never mines.
- Fix round 1 (coordinator rulings, same day): `requires_env` subject and `build_first`
  subject/value are program + subcommand only (`miners._program`: `mysql`, `make build`,
  `npm run build`, `python -m pytest`; empty, so no fact, for compound commands). The sanitiser
  also rejects credential-named options/assignments with a value
  (`(pass(word)?|passwd|pwd|token|secret|api[-_]?key|auth|cred|bearer)[^ ]*[=:]`),
  `user:pw@host`, `mysql|psql|mariadb -p<value>`, the prefixes `sk_live_ sk_test_ glpat- AIza
  npm_ xapp- xoxb- xoxp- ghs_ github_pat_`; absolute path segments anywhere in a token (after
  `= : @ +`) unless inside the project, `-X/path`, and `/Users/ /home/ /root /etc/ file:`
  always. The instruction deny-list (now also disregard, forget, override, previous, rules,
  should, delete, exfil*, credential, developer, mode, pretend, jailbreak, you are, do not,
  dont) matches whole words with simple inflections after lowercasing, leetspeak mapping
  (0 o 1 i 3 e 4 a 5 s 7 t) and, separately, dropping `- _ .` inside a token, so `systemctl`
  and `.gitignore` pass. The long-run secret rule needs >= 2 digits or no `_`/`-` separator
  (long test names pass). `build_first` values never contain push|deploy|release|publish|
  upload|install. A command alias through a downloading launcher (npx, uvx, pipx, bunx, pnpx,
  dlx) needs the package name in `package.json` dependencies/devDependencies or
  `pyproject.toml` (PEP 621, extras, dependency groups, Poetry); strict, so `npx tsc` is
  dropped unless a package literally named `tsc` is declared. `test_command` may carry exactly
  one `cd <project-relative dir> && ` prefix. The fold records its id in `facts.json` even when
  nothing was mined.
- For Task 26 (coordinator): rendered facts must be injected inside a clearly labelled frame
  saying they are data, not instructions, observed in past sessions.
- Fix round 2: a credential-named option (whole words of its name: pass/password/passwd/pwd/pw,
  token, secret, key, api-key/apikey, auth, cred(ential)s, bearer, private-key, client-secret,
  access-key) is rejected when it has a value: attached (`=`/`:`) or any following token
  (`--token hunter2`); `--keyword x` passes, a trailing `--key` passes. `Bearer <tok>` (any case)
  and `Basic <tok>` are rejected anywhere; for mysql/psql/mariadb/redis-cli/mongosh also
  `-p<val>` and `-W`/`-a` followed by a token. `miners._program` keeps a subcommand only if it is
  letters/`:_-` (no digits, <= 20 chars) or in a small known vocabulary; else the program alone.

## 2026-11-26 Task 26: fact lifecycle bandit and SessionStart injection
- `facts/lifecycle.py`: per fact in `facts.json` `status` (candidate|active|retired), `effect`
  Beta [a, b, n] over "injecting prevents the targeted waste when relevant", `baseline` Beta
  (withheld relevant sessions), `contradictions`; prior Beta(1, 1), gamma 1, cap 50. Relevance /
  waste per kind as ruled. `build_first`: the code's subject is the test program and the value
  the build step (the ruling's "value (test) command" was read as the test command = subject);
  relevance = ran the subject, waste = subject failure -> successful build -> successful subject.
  `generated_dir` waste is not detectable -> relevant sessions reward 0.5 (effect and baseline),
  so the bandit never retires it; no contradiction is defined for it.
- Contradictions (counted only in sessions where the fact was injected): alias / test_command /
  build_first = the value command fails with not-found / No module / usage error (`usage:`,
  unrecognized arguments, unknown command/option, invalid/no such option, missing script);
  requires_env = the subject ran successfully without the variable being set by the command or
  an earlier `export` in the session (owner-style conservative call: a wrong fact costs
  quality, retiring a stale one costs little). Retire at 1 (requires_env, build_first) / 2.
- Confirmations: a candidate re-derived by the miners in a session that added NEW session
  evidence (not a re-fold of the same session; `miners.record(after=..., fresh ids)`) and was
  not injected/withheld adds 1 to `effect`, for the waste -> fix kinds only (alias, env,
  build_first). Activation: >= 2 sessions and (lcb(0.1) > activate_lcb, 3 confirmations from
  the prior) or the bootstrap rule (no baseline observation yet and >= 3 sessions; the only
  route for test_command / generated_dir). Retirement is final; `facts forget` deletes and adds
  the id to `forgotten` so mining never re-adds it.
- Withholding: decided at SessionStart per active fact in the showable ranked prefix
  (stops after `max_facts` offered), probability `withhold_floor` (clamped [0, 0.5]) seeded by
  blake2b(session|fact) so a resume decides the same; overlay `facts_session = {shown: {id:
  1 - floor}, withheld: {id: floor}}`. Fold staging moves the log into the staged doc and lists
  the ids in `facts_done`, so each fact is judged at most once per session even when a session
  is folded again after a resume. Observation shares the miner's single transcript read
  (`mine_transcript(tap=...)`) and the same `facts.json` update (fold-id idempotent).
- `facts/inject.py`: exact wrapper + first data line, `sanitize.render` per line (re-sanitises;
  unsafe lines skipped), ranked by `last_seen` then effect mean; `build` stops at the first fact
  that does not fit (no reordering). Sources startup/resume/clear/compact: resume dedupes via
  `emit.Gate` (same key), clear/compact bump `facts_epoch` in the overlay so the block is
  injected and counted again (the earlier copy left the context). Hook reads `facts.json` once.
- Config `[facts]`: enabled = true, withhold_floor 0.1, activate_lcb 0.55, retire_ucb 0.4,
  max_tokens 300, max_facts 8. PROJECT_POLICY: enabled false_only, max_facts / max_tokens min,
  rest user-only. CLI `facts list|forget <id>|disable|enable [--project DIR]` (`enable` added
  as the undo of `disable`; `disabled: true` lives in `facts.json`).
- Sanitiser carry from Task 25 review: credential option names also `passphrase` and the joined
  `accesstoken`, `refreshtoken`, `clientsecret`, `privatekey` (plus `access-token`,
  `refresh_token` spellings).
- Fix round 1 (coordinator rulings, same day): templates are neutral past-tense observations
  (alias "`X` was not found here; `Y` worked", test "Tests were run with `Y`", env "`X` needed
  the environment variable `V`", build "`X` needed `Y` to run first", generated "Files under
  `D/` appear to be generated"). test_command: runner = (cd dir, program + subcommand); reward 1
  only if its own runner succeeded without another runner failing first and no other runner
  ran successfully strictly more often; a dominating other runner while shown = contradiction
  (retire at 2); one active test_command per subject (newly activated wins; among several new,
  most evidence then most recent). New `unneeded` counter (shown or withheld): alias subject
  ran OK, build_first tests passed before any build; retire at 2 (separate from the hard
  contradiction count, which keeps 1 for requires_env/build_first). Baseline retirement:
  baseline.n >= 3 and baseline.mean >= effect.mean - 0.1. generated_dir: >= 2 writes or a write
  followed by a successful build-family command = waste (0); a single write stays 0.5; the
  write-then-build is the "regenerated" waste event. Frequency kinds (test_command,
  generated_dir) need >= 5 sessions and >= 1 `waste_events` to activate (frequency candidates
  with >= 2 sessions are observed at fold time for this); the lcb rule is unreachable for them
  as candidates (no confirmations; posterior moves only when injected) - documented. Staleness
  (active frequency kinds): `stale` +1 per relevant shown/withheld session without new evidence,
  reset by new evidence; retire at 10 or 30 days since `last_seen` (fold passes `now`).
  Resume: per-fact dedupe via overlay `facts_sent = {epoch, ids, tokens}` (clear/compact start
  a new epoch; a resume sends only unsent ids within the remaining slots/tokens). Failed mining
  restores the staged facts log into the overlay (`lifecycle.restore_log`). Docs: recency-ranked.
  Glued credential names: api/auth/session token, secret/master/encryption key.
- Fix round 2 (coordinator rulings, same day): `lifecycle.runner(norm)` = normalised program
  only (interpreters incl. `.venv/bin/python`, `python -m X` -> X, uv/poetry/pipenv/pdm/hatch
  run, npx/bunx/pnpm exec/yarn exec/npm exec/`yarn X`, path basename; npm|pnpm|yarn|bun test and
  `run test` -> `npm-test-script`; cd prefix dropped). test_command contradiction only when
  replaced: another runner >= 2 successful runs AND the own runner did not run at all; own
  runner never succeeding is waste (0), never a contradiction (red phase retires only via
  ucb). Waste event also on a not-found/usage failure of any spelling before the own success.
  Sanitiser: any `-`-option whose name (dashes/underscores dropped, lowercased) ends with
  pass|passwd|pwd|password|passphrase|secret|token|key|apikey|cred|creds|credentials or
  contains `bearer` is a credential when it has a value (accepted false drop: `--monkey x`).
  generated_dir regenerated ignores ruff/mypy/flake8/eslint/pyright/pylint/prettier,
  `tsc --noEmit`, `black --check`. build_first unneeded counts only in withheld sessions and
  retires once seen on 2 distinct UTC days (`unneeded_days`); alias unneeded unchanged.
- Fix round 3: test_command outcome per session - replaced (another normalised runner had >= 1
  successful run while the own runner never ran) = contradiction + waste; own runner failed
  with not-found/usage = waste; own runner succeeded = success; anything else (red phase:
  only ordinary failures of the own runner) is neutral (Obs not relevant: no reward, no
  stale count; waste events still recorded for activation). Retire still at 2 contradictions.
  `_checker` also treats npm/pnpm/yarn/bun scripts (`run X` or direct `yarn X`) and make/just/
  task targets matching lint|typecheck|type-check|check|clippy|format|fmt|vet|style, plus
  `cargo clippy|fmt|vet`, `go vet|fmt`, as non-regenerating.

## 2026-11-27 Task 27: opt-in tool pruning with in-session recovery
- `mechanisms/prune.py` (host-neutral) + `adapters/claude/settings.py` (`ClaudeDenyWriter`,
  registered into `prune.WRITER` on import; entry and the `fold`/`prune` CLI import it). Store
  `projects/<key>/prune.json`: `enabled` (project opt-in), `tools` {rule: s, p, r=[a, b, n]},
  `used`, `known` (listed names, fallback for sessions without a list), `owned` (rules we wrote),
  `foreign` (rules the user already had: skipped, never owned), `created` (dir/file/permissions/
  deny we created, for the exact revert), `kept` {rule: reason}, `pending`, `notices`, `applied`
  fold ids. Lock order: after `facts.json`, before the overlay.
- Enabling: `prune enable` (store) or user/env `[prune] enabled = true`; project file: `enabled`
  and every threshold user-only, `allow` false_only (off + revert at next fold / session start).
  No evidence is collected and no file is created until enabled.
- Tool list: Claude Code interactive transcripts have no documented tool list; we read the SDK
  `system/init.tools` record and `attachment.deferred_tools_delta` (`addedNames` minus
  `removedNames`) when present (both observed/undocumented formats; a missing list falls back
  to the project's `known` names from other sessions). Subagent transcripts
  (`<stem>/subagents/*.jsonl`, <= 50) add uses.
- Evidence: a session with prompts > 0 adds s += 1, p += prompts and `prompts` zero-regret
  observations (closed form, cap 1000) to each candidate available and unused. Gate: s >= 3,
  p >= 15, ucb(0.95) <= tau 0.01 (prior Beta(1, 1): ~300 unused prompts). Protected: brief list
  + ToolSearch, Task* prefix, plan mode, SendMessage, MultiEdit, LS, ever-used tools/servers,
  `kept`. MCP: server-level rules only.
- Apply: idle = `tracking.last_active` older than idle_minutes (60). Fold applies when idle,
  else `pending`; SessionStart (`_on_prune_start`, before `_on_session_active`) applies a pending
  plan when the gap before this session was idle. Restores never wait.
- Recovery detection (owner ruling asked "PermissionDenied if documented"): PermissionDenied is
  documented for auto-mode denials only and a bare-denied tool is absent from the model's list,
  so direct-use handlers (PreToolUse, PostToolUseFailure incl. "No such tool available: X",
  PermissionDenied - new hook) are defensive. The realistic paths: ToolSearch query naming a
  pruned tool (PostToolUse; restore + additionalContext "run ToolSearch again"), user prompt
  naming it (name, camel words "web search", MCP server full name or last segment >= 4 chars),
  fold transcript scan, a rule the user deleted. Each: rule removed, regret 1.0, `kept`.
- Writer: strict JSON object (else refuse), `permissions`/`deny` shape checked, one pristine
  backup `settings.local.json.token-sparen.bak`, stamp re-check with one retry, removes the last
  occurrence of an owned rule only; cleanup on the last owned rule removes containers/file/dir we
  created (if empty) and drops our backup when it equals the file. `git ls-files` tracked ->
  never written (notice); untracked and not ignored -> notice to add it to .gitignore (never
  edited). git runs with a 2 s timeout, only when applying.
- `restore <tool>` / `restore --all`: regret 1.0 and kept for good (user said the tool is
  needed); `disable` and the `allow = false` revert: no regret, not kept. Savings: count only
  (`⊘ N` statusline segment `pruned`, `stats` line, `pruned_tools` in stats JSON).
- Fix round 1 (coordinator rulings, same day): (1) owned rules, `created`, `foreign` and refusal
  state live per settings-file real path in `prune.json` `files` {realpath: {dir, owned, created,
  foreign}}; hooks/fold touch the current checkout's file only, `restore --all` / `disable` /
  `allow = false` revert every recorded file (tested with two clones + a worktree). (2) Notices
  render the CLI as `python3 "<CLAUDE_PLUGIN_ROOT>/hooks/run_cli.py"` when the plugin root is
  known (stored notices use a `{cli}` placeholder); SKILL.md pre-approves `prune restore` in both
  forms (lines trimmed to stay at 80). (3) The SessionStart line has its own budget
  (`NOTICE_TOKENS = 100`): names truncated to `A, B, … +N more — run ... prune status`.
  (4) `prune enable` and the first fold with a transcript after any enable seed `used` from the
  project's transcripts (`<claude config>/projects/<cwd, non-alphanumerics -> '-'>` plus the
  session transcript's directory; last 60 days, newest first, <= 50 MB). (5) PreToolUse and
  PostToolUse (always) refresh `last_active` at most once a minute (`tracking.touch_active`).
  (6) Scope: MCP servers only by default; built-ins need `[prune] builtins = true` AND an SDK
  `init` record listing them (`ToolInventory.always`, `doc.always`); owned rules that stop being
  candidates are reverted without regret. (7) Prompt matching: exact name (case-sensitive) or a
  multi-word form (camel/`_`/`-` words, >= 2); `restore mcp__srv__tool` -> `mcp__srv`; settings
  JSON with duplicate keys is refused (`object_pairs_hook`, also for the statusline); a refused
  write gives one notice per distinct message; `[prune] enabled` and `builtins` are read only
  from `~/.token-sparen/config.toml` (`config.user_config`), never env, session or project.
- Fix round 2 (coordinator rulings, same day): `seeded` is set only when >= 1 history transcript
  was read; `plan` returns nothing until `seeded` (safety gate); the first fold with a transcript
  retries with the Claude config folder plus the transcript's own folder. `prune enable` prints
  "found N earlier sessions" and, for 0, warns that pruning waits until a session has been
  recorded. CLI `prune` resolves the project from `--project`, `CLAUDE_PROJECT_DIR`, the git top
  level of the cwd, then the cwd; `restore <tool>` restores in this checkout when pruned here,
  else in every recorded checkout holding it, and prints each settings file. User notices are
  stored and shown per checkout (`files[fkey].notices`).


## 2026-11-27 Failure-loop guard (Task 28)
- Delivery is `additionalContext`, never `updatedToolOutput`: most failures arrive as
  PostToolUseFailure, whose result cannot be replaced, and rewriting a result could derail
  correct work. `translate` now renders `additionalContext` for PostToolUseFailure.
- Signals are deterministic only: failure events (PostToolUseFailure, never successful output)
  fingerprinted as sha1[:10] of the exception type + error line (the line naming the last
  exception type, else the first error-looking line) after normalising quoted values, paths,
  hex ids and digits; scans the first 8000 + last 4000 characters. Oscillation is a per-file
  pseudo-fingerprint: an Edit reversing an earlier (old, new) pair or a Write returning to an
  earlier content hash; the first reversal counts 2, so the arm's k applies to it too. A burst of
  `[loopguard] burst` (8) failures triggers regardless of fingerprint and arm.
- `LOOPGUARD` arms patient/standard/eager -> k 5/3/2, safe arm patient (the static fallback);
  decided once per turn at the first repeat (or burst); `k = max(arm k, min_k)`. Features: tool
  category only (`[["cat"], []]`).
- The decision record is logged only when the redirect fires (owner ruling: a turn without a
  trigger logs no decision). The engine's pending entry for an untriggered decision simply ages
  out. Consequence for OPE: loopguard decisions in `decisions.jsonl` are conditional on a trigger.
- Second detection = the trigger condition counted on events after the redirect (the redirected
  fingerprint counts its triggering occurrence too); one terminal notice per turn.
- Reward: savings 0 at a recurrence within `watch_calls` (5) calls, 1 at the window end or when
  the turn/session ends first. Regret 1 on `is_interrupt` after the redirect or a next prompt
  matching stop/undo/revert/that's wrong/no, don't (whole words, curly apostrophe folded), else
  an explicit 0 at the next prompt or SessionEnd. Regret outcomes go to `decisions.jsonl` and the
  overlay's `regret_outcomes` (circuit breaker) with an empty command signature (no pre-trim
  effect). The redirect's tokens are added to the injected-tokens ledger.
- A trigger calls `RegretDetector.on_loop(ctx, None)`: this turn's compression decisions on
  error-bearing ctypes get 0.5 regret (settled with their window).
- Config: project may only set `enabled = false` and raise `min_k` / `burst`; `watch_calls` is
  user-only.

## 2026-11-27 Loop guard fix round 1 (Task 28 review; controller rulings, supersede the above)
- Fingerprint = tool name + target (Bash: `command_head`, the program base name plus its first
  plain-word argument after the regret detector's `normalize_command`, leading `cd`s dropped;
  file tools: the file path) + the last exception type + the most specific error line: the last
  `<Type>Error/Exception: message` line, else the first pytest `E   ` detail line, a coded error
  (`error TS2322:`, `error[E0308]:`, `error:`), a line naming an exception type, a line with an
  error keyword. `Exit code N`, `Traceback (most recent call last):` and `<tool_use_error>` tags
  are skipped; no informative line -> no fingerprint, never counted.
- Normalisation narrowed (deviation from the brief's "replace digits / quoted strings"): only
  volatile parts are dropped (directories -> base name, hex ids, line:col / `line N` / `(r,c)`,
  durations, clock times, numbers of 5+ digits). Quoted values and short literals are kept: they
  are what tells `No module named 'foo'` from `'bar'` and `assert 3 == 5` from `assert 4 == 5`.
- The burst trigger and `[loopguard] burst` are removed (false "repeated 8×" message).
- Oscillation needs >= 2 reversals of the same (old, new) pair on one file (A -> B -> A -> B;
  Write: switching back and forth between two contents); it triggers at `max(2, k - 1)`
  reversals with its own text ("the same edit has been made and undone {n}× ..."). It does not
  call `on_loop` (no error involved).
- Reward: the decision is logged at the first repeat for every arm (tokens_after 0; the
  redirect's tokens are in the ledger). Savings 1 only if a redirect was delivered at >= 3
  occurrences (oscillation: reversals + 1) and the fingerprint does not recur within the
  agent's next 5 calls (turn end first counts as no recurrence); otherwise 0, including decisions
  whose k was never reached. Regret unchanged for redirected decisions; a decision without a
  redirect gets an explicit regret 0 (signal `no_redirect`). Rationale: crediting n = 2
  redirects rewarded natural resolution and drifted the bandit to `eager`.
- State is keyed by `agent_id` (main = ""), up to 16 agents per turn; one model-facing redirect
  per agent and turn; the terminal notice stays once per turn. Watches count only that agent's
  calls. Interrupts apply to the interrupted agent's redirect.
- `[learn.hold] loopguard = true` means the safe arm (k = 5), not off; `loopguard.enabled =
  false` is the switch (README says so).
- Known noise (review M2): `is_stop` also matches "add a stop button" or "git revert the bad
  commit"; such prompts record regret 1 for a redirected decision.

## 2026-11-27 Loop guard fix round 2 (Task 28 re-review R1; controller ruling)
- Test-runner output (pytest, jest, go test, cargo test, detected with the test-report
  compressor's own sniffing and parsers via `compress.reports.failing_tests`) is fingerprinted by
  the sorted set of failing test ids (first 20) plus the first failure block's error line, never
  the summary line, so fixing a suite one test at a time changes the fingerprint while the same
  failing set repeating still matches. jest ids include the `FAIL <file>` lines.
- Summary-like lines (`N failed, M passed`, `FAILED (failures=N)`, `Tests:` / `Test Suites:`,
  `test result:`, go `FAIL\t<pkg>`) are boilerplate for every failure; with no failing ids the
  fallback rule runs without them.
- Overlay bound tightened (re-review m1): `MAX_FINGERPRINTS` (50) now caps fingerprints and
  watched files across all agents of the turn (a new file evicts the agent's own oldest, or is
  not tracked), `MAX_PAIRS` 8 -> 4, post-redirect counts capped at 10 per agent: about 15 KB at
  worst instead of ~400 KB. Perf test covers 16 agents.

## 2026-11-27 Loop guard fix round 3 (Task 28 re-review R2; controller ruling)
- A test report's fingerprint uses the number of distinct failing ids and a hash of the **whole**
  sorted set (the first-20 cap made fixing tests that sort after the 20th invisible). Nothing is
  stored but the hash; parse work stays bounded by the 8000 + 4000 character scan.

## 2026-11-27 Task 29: M3 integration tests and docs
- End-to-end tests (`tests/integration/test_cross_session.py`) drive `entry.main` with synthetic
  payloads and fold through `token-sparen fold` (as the SessionEnd spawn does), in a temporary
  state root, project and Claude config dir.
- A `command_alias` fact needs **3** sessions of evidence before it is active under the
  production rules (bootstrap rule; with 2 sessions the lcb rule also needs a third
  confirmation), so the facts test learns over three sessions and asserts injection at the
  fourth session start (and nothing after two). Thresholds were not lowered; only the withhold
  floor is set to 0 in that test so the injecting session is not a baseline draw.
- The loop-guard test fixes the real Engine's draw at the `loopguard` point to `standard`
  (monkeypatched `Engine.choose`); decision logging, pending entry and reward stay real.
- Pruning in the test needs 100 prompts per session (the high-tier ucb gate needs about 300
  unused prompts); rules are applied at the next session start after an idle gap, the deferred
  path.
- Bug found and fixed: with `TOKEN_SPAREN_DISABLE=1` the entry still wrote its sampled
  `logs/latency.jsonl` line. The kill switch now writes nothing at all (unit test in
  `tests/adapters/claude/test_entry.py`). A per-session or config disable still samples latency
  (the hook did run to read that config).
- CHANGELOG: M3 is a sub-section of `[Unreleased]` labelled as the 0.3.0 milestone (git tag
  `v0.3.0-m3`); the package version stays `0.1.0.dev0` until the first release (not bumped in
  this task).
- README: an "Across sessions" overview table (facts, pruning, loop guard, each with its off
  switch) and off-switch rows for the loop guard and pruning in "Turning it off".

## 2026-11-27 M3 fix wave A: pruning (controller rulings on review B1, M6, minors 3/4/8/9)
- Settings writes for pruning are journaled (`pending_add` per settings file in `prune.json`)
  before the write and reconciled on every store read; a journaled rule is never `foreign`.
  `created` containers are unknown for a reconciled rule, so its restore leaves the (empty)
  containers in place rather than guessing.
- New deny rules are written only by the fold or the spawned `prune apply --idle-checked`
  (SessionStart measures idleness before its session counts, then spawns); a fold inside the
  SessionStart hook (`overlay.fold(in_hook=True)`) leaves them `pending` and ends no lease.
- Pruned rules accrue no evidence; lease `[prune] lease_sessions = 15` (project: lower only),
  then a no-regret restore and a `fresh` gate (min_sessions / min_prompts un-pruned).
- git missing/timeout -> exposure "unknown" -> never write. Backup refreshed when we own no rule
  in the file. Restore message makes no mid-session availability claim.
