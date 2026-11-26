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
