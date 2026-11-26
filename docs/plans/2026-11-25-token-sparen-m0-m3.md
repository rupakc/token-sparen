# token-sparen M0–M3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a dogfoodable Claude Code build of token-sparen: recoverable compression, guards, read de-duplication, budget governor, live statusline, the learning engine with offline replay, and the cross-session mechanisms (facts, tool pruning, loop guard).

**Architecture:** A host-neutral Python core (`token_sparen/`) receives canonical `Event`s from a thin Claude Code adapter and returns canonical `Action`s. Every tunable behaviour is a `DecisionPoint` whose arm is chosen by one learning engine (constrained two-posterior Thompson sampling with hierarchical priors). Hooks only record decisions and apply O(1) session-overlay updates; `token-sparen fold` does cross-session learning after the session ends.

**Tech Stack:** Python ≥ 3.11 standard library only at runtime (`tomllib`, `json`, `fcntl`/`msvcrt`, `dataclasses`, `random`, `math`); dev: pytest, pytest-cov, hypothesis, ruff, mypy. GitHub Actions. Claude Code plugin format.

**Spec:** `docs/specs/2026-11-25-token-sparen-design.md` (read it first; § references below point into it).

**Private inputs (NEVER commit, never quote into the repo):** `~/token-sparen-private/research/{ito_notes,te_notes,landscape_notes}.md` and `~/.claude/projects/**` transcripts. E1 reports go to `~/token-sparen-private/reports/`.

## Global Constraints

- Python floor **3.11** (spec said ≥ 3.10; amended because 3.10 reached end-of-life in Oct 2026 and `tomllib` is 3.11+). Runtime = stdlib only.
- Licence MIT, `Copyright (c) 2026 Rupak Chakraborty`; NOTICE credits TokenEconomizer (MIT, arXiv 2610.22732). No code copied from any reference or competitor.
- No mention of any employer/organisation, internal URL, person, local path, transcript id or private figure anywhere in the repo. CI greps for a deny-list (Task 1).
- Hooks fail open: any exception or overrun of the latency budget → host no-op response + one line in `~/.token-sparen/logs/errors.jsonl`. Hook p95 < **50 ms** for non-LLM paths.
- Hook commands reference files only via `${CLAUDE_PLUGIN_ROOT}`; no top-level `bin/` dir in the plugin; no unpinned `npx`/`uvx`; no non-image file > **256 KiB** in the plugin tree.
- Every elision appends the marker `[token-sparen: kept X of Y lines · full output: <abs path> · `token-sparen show <id>`]` and saves the full text to `<project>/.token-sparen/outputs/<id>.txt`.
- Outputs below **2 000** estimated tokens are never modified.
- Never elide a line matching the error signature regex set `ERROR_PATTERNS` (Task 7).
- Kill switch: `TOKEN_SPAREN_DISABLE=1` → every hook returns no-op.
- Config precedence: defaults < `~/.token-sparen/config.toml` < `<project>/token-sparen.toml` < env `TOKEN_SPAREN_*` < session command.
- State root `~/.token-sparen/` (override env `TOKEN_SPAREN_HOME`); per-project `projects/<project_key>/`; project key = first 16 hex of sha256(normalised git remote) else sha256(realpath).
- Settings writes (statusline, tool pruning) are opt-in, backed up, reversible, disclosed in README.
- Coverage gate **≥ 90 %**; `mypy --strict` on `token_sparen/`; `ruff check` + `ruff format --check`.
- Conventional commits; push to `main` after each task once CI is green locally. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Concurrent hooks**: Claude Code runs hooks in parallel for multi-tool turns → two processes write the same project store. Expect no lost updates and no corrupt JSON (Task 4 test `test_parallel_writers_no_lost_update`).
2. **Unknown/odd tool_response shapes** (MCP tools, images, failures delivered as strings, missing fields): expect the hook to pass through unchanged, never emit a malformed `updatedToolOutput` (Task 12 `test_unknown_shape_passthrough`).
3. **Huge or binary output** (10 MB, invalid UTF-8, NUL bytes): expect bounded time (< 200 ms for 10 MB) and safe recovery file (Task 7 `test_huge_and_binary_output`).
4. **Missing/stale state** (first run, deleted `~/.token-sparen`, corrupt JSON, read-only disk): expect defaults and fail-open, never a crash (Task 4 `test_corrupt_file_recovers_to_default`, Task 12 `test_readonly_home_fail_open`).
5. **Statusline with a pre-existing user statusline / plugin uninstalled**: expect composition, exact restore on uninstall, and an empty line (not an error) when the plugin is gone (Task 16 tests).

## File Structure

```
token-sparen/
├── LICENSE  NOTICE  README.md  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md
├── CLAUDE.md  AGENTS.md  MEMORY.md
├── pyproject.toml                       # package + dev deps + tool config; entry point token-sparen
├── params/defaults.toml                 # every tunable (hill-climbing surface)
├── .claude-plugin/{plugin.json,marketplace.json}
├── hooks/hooks.json  hooks/run.py       # run.py: sys.path bootstrap → token_sparen.adapters.claude.entry
├── commands/token-sparen.md             # /token-sparen slash command
├── skills/token-sparen/SKILL.md         # user-facing skill (stats, recover, statusline, prune restore)
├── scripts/statusline.sh                # launcher resolving the active install
├── .claude/agents/*.md  .claude/skills/*/SKILL.md   # dev-time agents & skills
├── .github/workflows/{ci.yml,release.yml}
├── token_sparen/
│   ├── __init__.py  cli.py  events.py  config.py  estimate.py  transcript.py  emit.py  recovery.py
│   ├── store/{__init__,paths,jsonstore,jsonl}.py
│   ├── compress/{__init__,base,detect,text,relevance,structured,reports,search}.py
│   ├── mechanisms/{__init__,compression,guards,read_dedupe,governor,boundary,subagent,loopguard,prune}.py
│   ├── facts/{__init__,sanitize,miners,lifecycle,inject}.py
│   ├── learn/{__init__,beta,features,hierarchy,engine,regret,overlay,breaker,ope,policy}.py
│   ├── replay/{__init__,replay}.py
│   └── adapters/{__init__,base}.py  adapters/claude/{__init__,entry,translate,statusline,settings}.py
├── evals/e2/{corpus/,needles.toml,run.py}
└── tests/  (mirrors package; tests/fixtures/ with synthetic data only)
```

---

## M0 — Scaffolding, CI, Claude files

### Task 1: Repository bootstrap

**Files:** Create `LICENSE`, `NOTICE`, `README.md` (stub: one-paragraph pitch + status), `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.gitignore` (incl. `.token-sparen/`), `pyproject.toml`, `token_sparen/__init__.py` (`__version__ = "0.1.0.dev0"`), `token_sparen/cli.py` (argparse skeleton, `main(argv: list[str] | None = None) -> int`, `--version`), `.github/workflows/ci.yml`, `scripts/check_denylist.py`, `tests/test_meta.py`. Existing `docs/specs/…` and `docs/plans/…` are committed as-is.

- [ ] **Step 1: Write failing tests** in `tests/test_meta.py`:
  - `test_version_matches_pyproject`: `token_sparen.__version__` equals `[project].version` parsed with `tomllib`.
  - `test_cli_version_prints`: `main(["--version"]) == 0` and stdout contains `0.1.0.dev0`.
  - `test_license_owner`: `LICENSE` contains `Copyright (c) 2026 Rupak Chakraborty` and `MIT License`.
  - `test_denylist_clean`: `scripts/check_denylist.py` exits 0 on the repo. The deny-list is read from env `TOKEN_SPAREN_DENYLIST` (comma-separated, set locally by the owner and as a CI secret) plus built-ins `["/Users/", "C:\\Users\\"]`; it never stores the private words in the repo.
- [ ] **Step 2: Run** `pytest -q` → FAIL (modules missing).
- [ ] **Step 3: Implement** the files. `pyproject.toml`: `requires-python = ">=3.11"`, no runtime deps, `[project.scripts] token-sparen = "token_sparen.cli:main"`, dev extras, ruff/mypy/pytest/coverage config (`fail_under = 90`).
- [ ] **Step 4: CI** `ci.yml`: jobs `lint` (ruff check, ruff format --check, mypy --strict token_sparen), `test` (matrix ubuntu/macos/windows × 3.11/3.12/3.13, `pytest --cov=token_sparen`), `denylist`. Run `pytest -q && ruff check . && mypy --strict token_sparen` → PASS.
- [ ] **Step 5: Create the GitHub repo and push** (owner pre-authorised in the request):
  `git init -b main && git add -A && git commit -m "chore: bootstrap token-sparen"`; `gh repo create rupakc/token-sparen --public --source . --push --description "Learned, recoverable token optimisation for coding agents"`. Confirm CI green with `gh run watch`.

### Task 2: Claude development scaffolding (agents, skills, memory)

**Files:** Create `CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, `.claude/agents/{goldfish-reviewer,drift-checker,compressor-author,eval-hillclimber,bench-runner,host-adapter-author}.md`, `.claude/skills/{add-compressor,run-evals,hill-climb,release,egm-cycle}/SKILL.md`, `tests/test_scaffolding.py`.

- [ ] **Step 1: Failing tests** `tests/test_scaffolding.py`:
  - `test_agent_frontmatter`: every `.claude/agents/*.md` has YAML front-matter with `name` (== filename stem), `description`, `tools`.
  - `test_skill_frontmatter`: every `SKILL.md` has `name` and a `description` starting with "Use when".
  - `test_claude_md_lists_commands`: `CLAUDE.md` contains the exact commands `pytest`, `ruff check .`, `mypy --strict token_sparen`, `python evals/e2/run.py`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write content.**
  - `CLAUDE.md` ≤ 120 lines: purpose, layout, commands, invariants (Global Constraints), "never commit private data", EGM workflow, where decisions go (`MEMORY.md`).
  - `AGENTS.md`: host-neutral version of the same for Codex/Gemini/OpenCode contributors.
  - `MEMORY.md`: decision log seeded with the owner decisions from spec §17 and the 3.11 amendment.
  - `goldfish-reviewer`: zero-context reviewer; input = a spec/plan/diff path; output = ranked gaps; read-only tools.
  - `drift-checker`: compares code to spec sections and lists divergences with file:line.
  - `compressor-author`: writes a compressor + golden tests + E2 needles.
  - `eval-hillclimber`: runs §10.6 loop on dev split, never touches test split.
  - `bench-runner`: drives E4 runs respecting quota.
  - `host-adapter-author`: (v1.1) adapters.
  - Skills: `add-compressor`, `run-evals`, `hill-climb`, `release`, `egm-cycle` (draft → goldfish review → implement → drift report).
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `docs: add Claude scaffolding, dev agents and skills` and push.

---

## M1 — Core, compressors, Claude Code adapter, statusline

### Task 3: Canonical events and config

**Files:** Create `token_sparen/events.py`, `token_sparen/config.py`, `params/defaults.toml`; Test `tests/test_events.py`, `tests/test_config.py`.

**Interfaces — Produces:**
- `ToolCategory = Literal["shell","read","search","web","edit","agent","mcp","other"]`
- `@dataclass(frozen=True) class ToolCall: name: str; category: ToolCategory; input: Mapping[str, Any]; use_id: str | None`
- `@dataclass(frozen=True) class Event: host: str; kind: EventKind; session_id: str; project_dir: Path; project_key: str; transcript_path: Path | None; tool: ToolCall | None = None; output_text: str | None = None; raw_response: Any = None; prompt: str | None = None; model: str | None = None; extra: Mapping[str, Any] = {}`
- `EventKind = Literal["session_start","prompt","pre_tool","post_tool","post_tool_failure","pre_compact","session_end","statusline"]`
- `@dataclass class Action: replace_output: str | None = None; rewrite_input: dict[str, Any] | None = None; block_reason: str | None = None; inject_context: str | None = None; user_message: str | None = None` with `is_noop() -> bool`.
- `@dataclass class SessionCtx: session_id: str; project_key: str; turn: int; ctx_pct: float | None; model: str | None; overlay_path: Path` (loaded per hook from the session overlay; `turn` = count of UserPromptSubmit events).
- `load_config(project_dir: Path, env: Mapping[str,str]) -> Config`; `Config.get(dotted_key: str) -> Any`; `Config.disabled -> bool`.

- [ ] **Step 1: Failing tests:**
  - `test_action_noop`: `Action().is_noop()`; `Action(user_message="x").is_noop() is False`.
  - `test_config_precedence`: defaults `compression.min_tokens = 2000`; user file sets 3000; project file sets 4000; env `TOKEN_SPAREN_COMPRESSION__MIN_TOKENS=5000` → 5000; removing env → 4000.
  - `test_kill_switch`: env `TOKEN_SPAREN_DISABLE=1` → `cfg.disabled`.
  - `test_bad_toml_falls_back`: malformed project toml → defaults used, error logged, no exception.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement; `params/defaults.toml` contains every constant in this plan under sections `[compression] [recovery] [guards] [read_dedupe] [governor] [statusline] [learn] [facts] [prune] [loopguard]` (values given in each task). Env key mapping: `TOKEN_SPAREN_<SECTION>__<KEY>`.
- [ ] **Step 4:** run → PASS. **Step 5:** commit `feat(core): canonical events and layered config`.

### Task 4: Store — paths, atomic JSON, locking, JSONL logs

**Files:** Create `token_sparen/store/{__init__,paths,jsonstore,jsonl}.py`; Test `tests/store/test_store.py`.

**Interfaces — Produces:**
- `paths.home(env) -> Path`, `paths.project_dir_for(key) -> Path`, `paths.project_key(project_dir: Path) -> str` (normalise remote: strip `.git`, scheme, user@, lowercase host).
- `jsonstore.update(path: Path, fn: Callable[[dict], dict], default: dict) -> dict` — exclusive lock (`fcntl.flock` / `msvcrt.locking` on `path.with_suffix(".lock")`), read, apply, write tmp + `os.replace`; lock timeout 2 s → raise `StoreBusy`.
- `jsonstore.read(path, default) -> dict` (corrupt → rename to `.corrupt-<ts>`, return default).
- `jsonl.append(path, record: dict, max_bytes: int)` — rotates to `.1` when exceeding `max_bytes`.

- [ ] **Step 1: Failing tests:**
  - `test_project_key_same_for_ssh_and_https`: `git@github.com:a/b.git` and `https://github.com/a/b` → same key.
  - `test_parallel_writers_no_lost_update`: 8 processes × 50 increments via `update` → counter == 400.
  - `test_corrupt_file_recovers_to_default`.
  - `test_jsonl_rotation`: appending past `max_bytes` creates `.1` and new file < max.
- [ ] **Steps 2–5:** run FAIL → implement → PASS → commit `feat(store): atomic locked JSON store and JSONL logs`.

### Task 5: Token estimation and transcript parsing

**Files:** Create `token_sparen/estimate.py`, `token_sparen/transcript.py`; Test `tests/test_estimate.py`, `tests/test_transcript.py`, fixtures `tests/fixtures/transcripts/*.jsonl` (synthetic).

**Interfaces — Produces:**
- `estimate_tokens(text: str, content_type: str = "text") -> int` — chars/ratio with ratios `{"json":3.2,"code":3.5,"text":4.0,"log":3.6}`.
- `@dataclass class Usage: input: int; cache_read: int; cache_write: int; output: int` with `cost_weighted(prices: Prices) -> float`.
- `Prices` = per-model list ratios from `params/defaults.toml [pricing]` (relative units: input 1.0, cache_write 1.25, cache_read 0.1, output 5.0).
- `iter_assistant_usage(path: Path) -> Iterator[tuple[str, Usage, str | None]]` — yields (message_id, usage, model); **dedupes records sharing `message.id`** (keep last), skips `isSidechain` unless `include_sidechains=True`, skips model `<synthetic>`.
- `session_burn(path: Path) -> int` — Σ(input + cache_read + cache_write) over deduped calls.
- `iter_tool_results(path) -> Iterator[ToolResultRecord]` (tool_use_id, tool_name, input, text, turn_index) for replay.

- [ ] **Step 1: Failing tests:** `test_dedupe_split_records` (two records same id → counted once); `test_session_burn_sums_context`; `test_sidechain_excluded_by_default`; `test_estimate_monotonic` (hypothesis: longer text ⇒ ≥ tokens).
- [ ] **Steps 2–5:** FAIL → implement → PASS → commit `feat(core): token estimation and transcript usage parser`.

### Task 6: Recovery store and marker

**Files:** Create `token_sparen/recovery.py`; Test `tests/test_recovery.py`.

**Interfaces — Produces:**
- `save_full(project_dir: Path, text: str) -> RecoveryRef` where `RecoveryRef(id: str, path: Path)`; id = first 12 hex of sha256(text); file `<project>/.token-sparen/outputs/<id>.txt`; ensures `<project>/.token-sparen/.gitignore` containing `*`.
- `marker(kept_lines: int, total_lines: int, ref: RecoveryRef) -> str` → exactly `[token-sparen: kept {kept} of {total} lines · full output: {abs path} · \`token-sparen show {id}\`]`.
- `prune_outputs(project_dir, ttl_days=14, max_files=200, max_bytes=100_000_000)` (LRU by mtime).
- `is_recovery_read(project_dir, read_path: Path) -> str | None` (returns id if the path is a recovery file) — consumed by Task 21.

- [ ] Tests: `test_marker_exact_format`; `test_save_idempotent_same_id`; `test_gitignore_created`; `test_prune_by_ttl_and_count`; `test_is_recovery_read`.
- [ ] FAIL → implement → PASS → commit `feat(core): recoverable output store and marker`.

### Task 7: Compression framework, detection, text compressor

**Files:** Create `token_sparen/compress/{__init__,base,detect,text,relevance}.py`; Test `tests/compress/test_text.py`, `tests/compress/test_detect.py`.

**Interfaces — Produces:**
- `ContentType = Literal["json","diff","stacktrace","test_report","compiler","build_log","search","pplx","log","table","text"]`
- `detect(text: str, tool: ToolCall | None) -> ContentType` (sniff first 4 KB + tool/command hints, e.g. command starts with `pytest`/`npm test` → `test_report`; `pplx` → `pplx`; `rg`/`grep` → `search`).
- `@dataclass class Level: head: int; tail: int; keep_ratio: float` loaded from `[compression.levels.<arm>]`: light `{head=80, tail=40, keep_ratio=0.6}`, standard `{head=40, tail=20, keep_ratio=0.35}`, aggressive `{head=15, tail=10, keep_ratio=0.15}`.
- `class Compressor(Protocol): name: str; handles: frozenset[ContentType]; def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult`
- `@dataclass class CompressContext: prompt: str | None; recent_paths: Sequence[str]`
- `@dataclass class CompressResult: text: str; kept_lines: int; total_lines: int; lossless: bool`
- `registry.register(c: Compressor)`, `registry.for_type(t) -> Compressor` (falls back to `TextCompressor`); entry-point group `token_sparen.compressors` loaded lazily.
- `ERROR_PATTERNS: tuple[re.Pattern, ...]` (error|exception|traceback|failed|fatal|panic|assert|✗|FAIL, case-insensitive, word-bounded).
- `TextCompressor`: keeps head/tail, **pins all ERROR_PATTERNS lines plus 2 lines of context**, folds runs of ≥ 3 lines identical after digit-normalisation into `"<line>  [× N similar lines]"`, fills remaining budget (`keep_ratio × total`) with top lines by `relevance.score(line, ctx)`; output preserves original order.
- `relevance.score(line: str, ctx: CompressContext) -> float` — BM25-lite: query terms = tokens of prompt + basenames of recent paths; k1=1.2, b=0.75 over the output's lines as documents.

- [ ] **Step 1: Failing tests:**
  - `test_error_lines_never_dropped` (hypothesis: random 5 000-line text with one `ERROR: boom` at random index; every level keeps it).
  - `test_fold_repeated_lines` (`"step 1".."step 500"` → one folded line with `× 500`).
  - `test_order_preserved`.
  - `test_relevance_prefers_prompt_terms`.
  - `test_huge_and_binary_output`: 10 MB text with NUL bytes and invalid UTF-8 surrogates compresses in < 200 ms (wall) and returns str.
  - `test_detect_table` covering each ContentType with one fixture.
- [ ] FAIL → implement → PASS → commit `feat(compress): framework, detection and text compressor`.

### Task 8: Structured compressors (json, diff, stacktrace)

**Files:** Create `token_sparen/compress/structured.py`; Test `tests/compress/test_structured.py`, golden files `tests/compress/golden/*`.

**Interfaces:** `JsonCompressor` (schema + first k items per array, `k = {light:10, standard:5, aggressive:2}`, `"… N more items with same keys"`, elides duplicate scalar values in arrays of objects; invalid JSON → delegate to TextCompressor), `DiffCompressor` (keep all hunk headers and +/- lines; trim unchanged context to 1 line per side at standard/aggressive; never drops a changed line), `StackTraceCompressor` (keep exception type/message, first and last 3 frames, all frames in project paths from `ctx.recent_paths` or not under site-packages/node_modules; collapse the rest with a count).

- [ ] Tests: `test_json_schema_and_counts` (golden); `test_json_invalid_falls_back`; `test_diff_keeps_all_changes` (hypothesis over generated diffs: set of +/- lines preserved); `test_stacktrace_keeps_exception_and_project_frames`.
- [ ] FAIL → implement → PASS → commit `feat(compress): json, diff and stack-trace compressors`.

### Task 9: Report compressors (test reports, compilers, build logs)

**Files:** Create `token_sparen/compress/reports.py`; Tests `tests/compress/test_reports.py` + fixtures (pytest, jest, go test, cargo test, tsc, rustc, javac, gcc, npm/pip/gradle/docker build logs — synthetic or from MIT/Apache public projects with attribution in `tests/fixtures/ATTRIBUTION.md`).

**Interfaces:** `TestReportCompressor` → summary line with pass/fail/skip counts + every failing test id + its first assertion/error block (≤ 15 lines each at standard); `CompilerCompressor` → every diagnostic `file:line:col: error|warning` grouped by file, warnings collapsed to counts at aggressive, errors never dropped; `BuildLogCompressor` → drops progress/download lines (`\d+%`, `Downloading`, `Collecting`, spinner glyphs), keeps step headers, warnings count, all errors and the final status.

- [ ] Tests: one `test_<tool>_failures_preserved` per fixture asserting each failing test id / error diagnostic appears in output; `test_all_pass_report_is_one_line_summary`.
- [ ] FAIL → implement → PASS → commit `feat(compress): test-report, compiler and build-log compressors`.

### Task 10: Search, `pplx` and log compressors

**Files:** Create `token_sparen/compress/search.py`; Tests `tests/compress/test_search.py`.

**Interfaces:** `SearchCompressor` (grep/rg `path:line:text`: group by file, keep up to `{light:20,standard:8,aggressive:3}` matches per file ranked by relevance, `… N more matches in <file>`; always keep the file list); `PplxCompressor` (parse JSON; per result keep `title`, `url`, `date`; keep `snippet` only if not a substring/near-duplicate (Jaccard ≥ 0.8 on word 3-shingles) of `summary`, top-k results `{light:10,standard:5,aggressive:3}`); `LogCompressor` (strip timestamps for dedupe, fold repeats, keep WARN/ERROR lines and last 30 lines).

- [ ] Tests: `test_search_keeps_every_file_name`; `test_pplx_dedupes_snippet_summary`; `test_pplx_invalid_json_falls_back`; `test_log_keeps_errors_and_tail`.
- [ ] FAIL → implement → PASS → commit `feat(compress): search, pplx and log compressors`.

### Task 11: E2 fact-preservation suite

**Files:** Create `evals/e2/corpus/*.txt|json` (≥ 40 items across every ContentType, synthetic or permissively licensed), `evals/e2/needles.toml` (`[[case]] file=…, needles=[…], types=[…]`), `evals/e2/run.py`; Test `tests/test_e2.py`; CI job `e2`.

**Interfaces:** `run(levels=("light","standard","aggressive")) -> E2Report{cases:int, passed:int, failures:list[(case, level, needle)], mean_ratio: dict[level,float]}`. A needle passes if present in the compressed text, or if the compressed text contains a marker whose recovery file contains it **and** the needle is not tagged `must_inline` (error lines, failing test ids, diagnostics are `must_inline`).

- [ ] Tests: `test_e2_all_pass` (100 % required); `test_e2_reports_ratio` (mean compression ratio at standard < 0.6 — guards against no-op compressors).
- [ ] FAIL (until corpus/needles exist) → build corpus → PASS → add CI job (`python evals/e2/run.py --strict`) → commit `test(evals): E2 fact-preservation suite in CI`.

### Task 12: Claude Code adapter — entry, translation, fail-open

**Files:** Create `hooks/run.py`, `hooks/hooks.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `token_sparen/adapters/base.py`, `token_sparen/adapters/claude/{entry,translate}.py`, `token_sparen/emit.py`; Test `tests/adapters/claude/test_translate.py`, `tests/adapters/claude/test_entry.py`, `tests/test_emit.py`.

**Interfaces — Produces:**
- `HostAdapter` protocol: `name: str; capabilities: frozenset[str]` (claude: `{"replace_output","rewrite_input","block","inject_context","statusline"}`); `parse(event_name: str, payload: dict, env) -> Event`; `render(event: Event, action: Action) -> dict` (host JSON).
- `claude.translate.render_tool_output(tool_name: str, original: Any, new_text: str) -> Any | None`: Bash `{"stdout": new_text, "stderr": "", "interrupted": original.get("interrupted", False), "isImage": False}`; Read: deep-copy with `file.content = new_text`; `str` original → `new_text`; any other shape → `None` (= do not replace).
- `entry.main(event_name: str, stdin: TextIO, stdout: TextIO, env) -> int` always returns 0; wraps `pipeline.handle(event) -> Action` (dispatch table filled by later tasks; Task 12 ships `pre_tool`/`post_tool` handlers that return no-op); enforces `[adapter] latency_budget_ms = 400` hard cap (hook times out → no-op) and records duration to `~/.token-sparen/logs/latency.jsonl` sampled 10 %.
- `emit.Gate.add(kind: str, text: str, key: str) -> None` / `emit.Gate.render(max_tokens: int) -> str | None`: dedupes by key per session (persisted in session overlay), drops lowest-priority items over budget, records injected tokens to the session ledger (`injected_tokens`), which net-savings subtract.
- `hooks.json`: events `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `SessionEnd`, command `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" <EventName>`, timeout 10 s.
- `plugin.json`: `name "token-sparen"`, version, description, author, license MIT, homepage, repository; `marketplace.json`: name `token-sparen`, owner Rupak Chakraborty, plugin source `./`.

- [ ] **Step 1: Failing tests:** `test_render_bash_shape`; `test_render_read_shape`; `test_unknown_shape_passthrough` (dict with unknown keys / list / image → `None` and the hook emits `{}`); `test_entry_exception_is_noop` (pipeline raises → stdout `{}`, exit 0, error logged); `test_readonly_home_fail_open` (TOKEN_SPAREN_HOME unwritable → exit 0, `{}`); `test_disable_env_noop`; `test_emit_dedupes_and_budgets`; `test_plugin_manifest_valid` (name kebab-case, no reserved prefix, hooks use `${CLAUDE_PLUGIN_ROOT}`).
- [ ] **Steps 2–4:** FAIL → implement → PASS. Run `claude plugin validate .` → no errors (add to CI as optional job when `claude` is available).
- [ ] **Step 5:** commit `feat(claude): adapter, fail-open entry, emit gate and plugin manifest`.

### Task 13: Compression decision point (static policy) wired to PostToolUse

**Files:** Create `token_sparen/learn/policy.py` (static), `token_sparen/mechanisms/compression.py`; Test `tests/mechanisms/test_compression.py`, `tests/adapters/claude/test_e2e_post_tool.py`.

**Interfaces — Produces (stable for Task 20):**
- `@dataclass(frozen=True) class DecisionPoint: name: str; arms: tuple[str, ...]; safe_arm: str; tier: Literal["low","medium","high","none"]`
- `class Policy(Protocol): def decide(self, point: DecisionPoint, features: Mapping[str,str], session: SessionCtx) -> Decision`
- `@dataclass(frozen=True) class Decision: decision_id: str; point: str; arm: str; propensity: float; bucket: str`
- `StaticPolicy(arms_by_point: dict[str,str])` — propensity 1.0; default compression arm `standard`.
- `COMPRESSION = DecisionPoint("compression", ("off","light","standard","aggressive"), safe_arm="off", tier="medium")`
- `compression.handle_post_tool(event: Event, policy: Policy, cfg: Config) -> Action`: skip if `estimate_tokens < compression.min_tokens (2000)`; detect → decide → compress → if result saves < `compression.min_saving_ratio = 0.15`, no-op; else save full, append marker, return `Action(replace_output=…)`; logs a `DecisionRecord` (dataclass in `learn/policy.py`: `decision_id, point, arm, propensity, bucket, tokens_before, tokens_after, recovery_id, tool_use_id, command_sig, ts`) to `projects/<key>/decisions.jsonl`. For Bash, prefer `tool_response.persistedOutputPath` content when present (Claude Code truncates stdout before hooks).
- `PostToolUseFailure` events route to the same handler (failing commands are the noisiest).

- [ ] Tests: `test_small_output_untouched`; `test_large_pytest_output_compressed_with_marker`; `test_decision_logged`; `test_persisted_output_used`; `test_failure_event_compressed`; e2e `test_e2e_post_tool` (pipe a fixture payload through `hooks/run.py PostToolUse` subprocess, assert `hookSpecificOutput.updatedToolOutput.stdout` contains the marker).
- [ ] FAIL → implement → PASS → commit `feat(mechanisms): recoverable compression on PostToolUse`.

### Task 14: Guards, learned-pre-trim hook point, read de-duplication

**Files:** Create `token_sparen/mechanisms/guards.py`, `token_sparen/mechanisms/read_dedupe.py`; Tests `tests/mechanisms/test_guards.py`, `tests/mechanisms/test_read_dedupe.py`.

**Interfaces:**
- `guards.check(event: Event, cfg) -> Action` blocks (with a reason that names the bounded alternative) exactly these: `find /` or `find ~` without `-maxdepth`; `grep -r`/`rg` over `node_modules|.git|dist|build|.venv` without explicit include; `cat`/`Read` of a file > `guards.max_read_bytes = 5_000_000` without a range; `ls -R` on `/` or `~`. Everything else → no-op.
- `PRETRIM = DecisionPoint("pretrim", ("off","bounded"), safe_arm="off", tier="medium")`; `guards.pretrim(event, policy, store) -> Action` rewrites Bash `command` to `<cmd> 2>&1 | tail -n <K>` only when the project store lists the normalised command as a pretrim candidate (populated in Task 22) and the arm is `bounded`; `updatedInput` contains all original fields; K from `[guards] pretrim_lines = 120`. Never rewrites commands containing `|`, `>`, `&&`, `;`, heredocs or interactive tools.
- `READ_DEDUPE = DecisionPoint("read_dedupe", ("off","pointer"), safe_arm="off", tier="low")`; on PostToolUse(Read) records `(path, mtime_ns, size, sha1, offset, limit, turn)` in session overlay; on a later identical unchanged read **with no compaction since** (overlay `last_compact_turn` from PreCompact), replace output with `"[token-sparen: unchanged since turn {n} — content already in context above; re-read with a different range or after edits]"`.

- [ ] Tests: `test_blocks_find_root`; `test_allows_find_with_maxdepth`; `test_blocks_cat_huge_file`; `test_pretrim_never_rewrites_pipelines`; `test_pretrim_preserves_other_input_fields`; `test_read_dedupe_pointer_when_unchanged`; `test_read_dedupe_full_after_edit`; `test_read_dedupe_full_after_compact`.
- [ ] FAIL → implement → PASS → commit `feat(mechanisms): guards, pretrim point and read de-duplication`.

### Task 15: Budget governor, burn meter, compaction advice, subagent shaping

**Files:** Create `token_sparen/mechanisms/{governor,boundary,subagent}.py`; Tests `tests/mechanisms/test_governor.py`, `test_boundary.py`, `test_subagent.py`.

**Interfaces:**
- `governor.state(session: SessionCtx, transcript: Path) -> GovernorState{burn:int, per_turn_ewma:float, window:int, ctx_pct:float, level: Literal["ok","notice","advise","stop"]}`; EWMA α = 0.3; budget `[governor] session_budget_tokens = 500_000_000` (economy, user-configurable); risk thresholds `[governor] ctx_notice=0.60, ctx_advise=0.80`; `stop` only if `[governor] hard_stop = false` is set true by the user. `advise` emits a **user_message** (terminal only, zero model tokens), once per level per session. `stop` → PreToolUse deny for context-adding tools (Read/Bash/WebFetch/Agent) with a reason telling the user how to lift it.
- Window resolution: last statusline-recorded window for the session (Task 16) → model-family table in `[governor.windows]` → 200 000.
- `boundary.detect(prompt: str, session) -> bool`: Jaccard(paths/identifiers in prompt, last-N-turn set) < 0.15 **and** context > `boundary.min_ctx_pct = 0.40` → user_message recommending `/compact` (never auto-compacts); PreCompact handler writes summary instructions asking to keep `token-sparen` markers' recovery paths.
- `subagent.brief() -> str` injected via `PreToolUse(Agent|Task)` as `updatedInput.prompt` suffix (one line: "Return conclusions, file paths and line numbers rather than raw dumps."); `SUBAGENT_BRIEF = DecisionPoint("subagent_brief", ("off","on"), safe_arm="off", tier="medium")`. Oversized Agent results route through compression.

- [ ] Tests: `test_governor_levels`; `test_advise_once_per_level`; `test_hard_stop_off_by_default`; `test_boundary_triggers_on_topic_shift`; `test_precompact_keeps_recovery_paths`; `test_subagent_brief_appended_once`.
- [ ] FAIL → implement → PASS → commit `feat(mechanisms): budget governor, boundary advice, subagent shaping`.

### Task 16: Live statusline (spec §6.12)

**Files:** Create `token_sparen/adapters/claude/{statusline,settings}.py`, `scripts/statusline.sh`; Tests `tests/adapters/claude/test_statusline.py`, `test_settings.py`.

**Interfaces:**
- `statusline.render(payload: dict, env) -> str`: reads `session_id`, `transcript_path`, `model.id`, `context_window.{used_percentage,context_window_size}`, `cost.total_cost_usd`, `rate_limits` if present. Records `(session_id, model, window)` to `~/.token-sparen/windows.json` (sensor). Segments (configurable `[statusline] segments = ["ctx","burn","rate","saved","trims","learn"]`):
  `⛁ ts {ctx}% ctx │ burn {burn}/{budget} {pct}% │ +{ewma}/turn │ saved {net} (≈${usd}) │ ✂ {trims} ↺ {regrets} │ learn: {state}`; colours green < 50 %, yellow < 75 %, red otherwise; `NO_COLOR` → no ANSI; `[statusline] ascii = false` → ASCII glyphs. `net` = tokens removed − recovery-read tokens − injected tokens (from session ledger). `learn` state from Task 22 (`cold` until then).
- **Bound to stdin `session_id`/`transcript_path` only**; missing → render `⛁ ts` minimal line.
- `settings.install_statusline(settings_path: Path, launcher: str) -> InstallResult`: backup to `settings.json.token-sparen.bak`; if an existing `statusLine.command` exists, write a composed command `sh <launcher> --compose -- <original command>` (runs original, appends our segment with ` │ `); `uninstall_statusline(settings_path)` restores the exact backed-up `statusLine` value (or removes the key if there was none).
- `scripts/statusline.sh`: resolve active install from `~/.claude/plugins/installed_plugins.json` (match plugin name `token-sparen`, newest mtime), fall back to the cache dir, then to the repo path; if nothing resolves print empty line and exit 0.
- CLI: `token-sparen statusline install|uninstall|render`.

- [ ] Tests: `test_render_segments_and_colours`; `test_no_color`; `test_records_window`; `test_bound_to_session_id` (two sessions' ledgers present → shows only stdin session); `test_install_fresh_and_uninstall_removes_key`; `test_install_composes_existing_and_uninstall_restores_exact`; `test_launcher_missing_plugin_prints_empty` (subprocess with fake HOME); `test_render_p95_under_50ms` (100 renders).
- [ ] FAIL → implement → PASS → commit `feat(claude): live statusline with composition-safe install`.

### Task 17: User surface — CLI, slash command, skill, latency CI

**Files:** Modify `token_sparen/cli.py`; Create `commands/token-sparen.md`, `skills/token-sparen/SKILL.md`, `tests/test_cli.py`, `tests/perf/test_hook_latency.py`; Modify `.github/workflows/ci.yml` (add `perf` job).

**Interfaces:** CLI subcommands: `show <id>` (print recovery file), `stats [--session ID] [--json]` (session ledger + project totals), `statusline …` (Task 16), `run -- <cmd…>` (executes command, compresses its combined output with the compression point, saves recovery — used by v1.1 Codex adapter and manual use; exit code = child's), `doctor` (Python version, home writable, hooks resolvable, synthetic PostToolUse round-trip through `hooks/run.py`), `fold`, `replay`, `facts`, `prune` (filled by later tasks; stubs print "available after M2/M3").
`/token-sparen` command: shows stats, offers statusline install, recovery help, `disable for this session` (writes session overlay flag).

- [ ] Tests: `test_show_prints_recovery`; `test_run_preserves_exit_code_and_compresses`; `test_doctor_ok_in_tmp_home`; `test_stats_json_schema`; `test_hook_latency_p95` (200 synthetic PostToolUse events of 50 KB pytest output via in-process `entry.main` → p95 < 50 ms on CI runners; fails the build otherwise).
- [ ] FAIL → implement → PASS → commit `feat(cli): user commands, slash command, skill and latency gate`; push.

### Task 17a: Extension loading and conformance kit (spec §9)

**Files:** Create `token_sparen/extensions.py`, `token_sparen/testing/conformance.py`; Modify `compress/__init__.py`, `learn/policy.py` (point registry), `cli.py` (`extensions list|trust <path>`); Test `tests/test_extensions.py`.

**Interfaces:** `load_extensions(project_dir, home, cfg) -> LoadedExtensions` — entry-point groups `token_sparen.compressors`, `token_sparen.decision_points`, `token_sparen.signals`, `token_sparen.adapters`, plus drop-in `*.py` in `<project>/.token-sparen/extensions/` and `~/.token-sparen/extensions/`; drop-ins load **only** if their sha256 is in `~/.token-sparen/trusted_extensions.json` (added via `token-sparen extensions trust <path>`); loading is lazy (imported on first matching content type / point); an extension raising is disabled for the session and logged. `conformance.check_compressor(c: Compressor) -> list[str]` runs error-pinning, order, marker-compat and size-bound checks (reused by Task 7 compressors in their tests).

- [ ] Tests: `test_untrusted_dropin_ignored`; `test_trusted_dropin_loaded_lazily`; `test_faulty_extension_disabled_not_crash`; `test_builtin_compressors_pass_conformance`.
- [ ] FAIL → implement → PASS → commit `feat(core): trusted extension loading and conformance kit`.
- [ ] Push; **M1 goldfish review**: dispatch `goldfish-reviewer` on spec §5–6, §9 + `git diff <M0 commit>..HEAD`; dispatch `drift-checker`; fix findings; tag `v0.1.0-m1`.

---

## M2 — Learning engine, replay, off-policy evaluation

### Task 18: Beta posterior with fractional, discounted, capped updates

**Files:** Create `token_sparen/learn/beta.py`; Test `tests/learn/test_beta.py`.

**Interfaces:** `@dataclass class Beta: a: float; b: float; n: float` (n = real observations); `update(x: float, gamma: float = 0.98, cap: float = 50.0, prior: Beta) -> Beta` — discount: `a = prior.a + gamma*(a-prior.a)`, same for b, then add `x` to a and `1-x` to b, n += 1; if `a+b > cap` rescale the excess over the prior proportionally (mean preserved). `sample(rng: random.Random) -> float` (`rng.betavariate`), `mean`, `lcb(q=0.05)` and `ucb(q=0.95)` via normal approximation with variance `ab/((a+b)^2(a+b+1))`.

- [ ] Tests: `test_fractional_update_mean`; `test_cap_preserves_mean`; `test_discount_forgets_toward_prior` (1 000 updates of 1.0 then 1 000 of 0.0 → mean < 0.2); `test_sample_in_unit_interval` (hypothesis).
- [ ] FAIL → implement → PASS → commit `feat(learn): discounted capped Beta posterior`.

### Task 19: Context features and hierarchical priors

**Files:** Create `token_sparen/learn/{features,hierarchy}.py`; Tests `tests/learn/test_features.py`, `test_hierarchy.py`.

**Interfaces:**
- `features.for_point(point: DecisionPoint, event: Event, session: SessionCtx, content_type: str | None) -> dict[str,str]` — compression: `band` (`lt50|50_75|75_95|gt95` of ctx %), `cat` (ToolCategory), `size` (`sm<8k|md<50k|lg` tokens), `ctype`, `phase` (`explore|build|debug|mixed` from last 10 tool categories: edits ≥ 30 % → build, failures ≥ 30 % → debug, reads/search ≥ 60 % → explore).
- `bucket_key(features, levels: Sequence[str]) -> str` ordered `k=v|…`; coarse levels per point declared in `[learn.backoff.<point>]` (compression: full → `cat,ctype` → `ctype` → `*`).
- `StoreChain(project_key: str, home: Path)` — read-only view over `projects/<key>/posteriors.json`, the session overlay and `global/posteriors.json`; `get(point, bucket, arm, kind: Literal["savings","regret"], scope) -> Beta | None`.
- `hierarchy.prior_for(point, features, arm, kind, stores: StoreChain) -> Beta` — chain `project-full → project-coarse… → global-full → global-coarse… → designed default ([learn.defaults.<point>.<arm>])`; prior = parent posterior mean `m` with strength `κ`: `Beta(m*κ, (1-m)*κ, n=0)`; κ estimated per point by method of moments across sibling buckets with ≥ 5 obs, clamped to `[2, 20]`, default 6.
- Designed defaults (to be refit in M4): compression savings `{off:0, light:0.35, standard:0.6, aggressive:0.8}`, regret `{off:0, light:0.02, standard:0.04, aggressive:0.08}`, all κ = 6.

- [ ] Tests: `test_band_and_size_boundaries`; `test_phase_inference`; `test_backoff_order`; `test_prior_from_parent_mean`; `test_kappa_moments_clamped`.
- [ ] FAIL → implement → PASS → commit `feat(learn): context features and empirical-Bayes hierarchical priors`.

### Task 20: Constrained two-posterior Thompson engine with decision logging

**Files:** Create `token_sparen/learn/engine.py`; Test `tests/learn/test_engine.py`.

**Interfaces — Produces:**
- `class Engine(Policy)`: `__init__(stores: StoreChain, cfg: Config, rng: random.Random)`; `decide(point, features, session) -> Decision`; `reward(decision_id: str, savings: float | None, regret: float | None) -> None` (applies to session overlay).
- Algorithm (spec §7.5–7.6), per arm: sample `S̃` from savings posterior, `R̃` from regret posterior; feasible = `R̃ ≤ τ[tier]` with `τ = {low:0.10, medium:0.05, high:0.01, none:1.0}`; choose feasible arm with max `S̃` (`safe_arm` always feasible, `S ≡ 0`). **Propensity floor**: with probability `floor[tier] = {low:0.05, medium:0.05, high:0.02}` choose `safe_arm`. High tier: a non-safe arm is feasible only if its regret `ucb ≤ τ`. Cold buckets (all arms n < `[learn] cold_obs = 3`) use the prior chain (no special default arm).
- Propensity: estimated by Monte-Carlo replay of the same sampling (`[learn] propensity_samples = 64`) mixed with the floor; logged in the decision record.
- In-session overlay update is O(1): one JSON read-modify-write of `projects/<key>/sessions/<session_id>.json` under lock.

- [ ] Tests: `test_off_wins_when_regret_high` (trim arms regret posterior mean 0.3, savings 0.9 → off chosen > 90 % of 1 000 draws); `test_aggressive_wins_when_safe` (regret ≈ 0, savings ordering aggressive > standard → aggressive most often); `test_propensity_floor_observed` (safe arm frequency ≥ floor); `test_high_tier_requires_ucb`; `test_propensity_logged_and_in_unit_interval`; `test_reward_updates_overlay`.
- [ ] FAIL → implement → PASS → commit `feat(learn): constrained two-posterior Thompson engine`.

### Task 21: Regret detection and savings attribution

**Files:** Create `token_sparen/learn/regret.py`; Modify `mechanisms/compression.py`, `mechanisms/read_dedupe.py`, `adapters/claude/entry.py` (dispatch); Test `tests/learn/test_regret.py`.

**Interfaces:**
- `normalize_command(cmd: str) -> str`: strip leading `cd <path> &&`, env assignments `A=b`, our own `| tail -n N` suffix, collapse whitespace; keep full argv (fixes reference flaw of collapsing to `cd`).
- `RegretDetector.on_event(event, session) -> list[tuple[decision_id, float]]` signals: recovery read (Read/`cat`/`token-sparen show` of a recovery path) → 1.0; re-run of same normalised command within `[learn] regret_window_turns = 6` turns **without an intervening edit to a file mentioned in the elided span** → 1.0 if ≤ 2 turns else 0.5; agent prompt/tool input containing ≥ 2 distinctive tokens from the elided span (8+ chars, not in kept text) → 0.5; failure-loop trigger (Task 28) within same turn after compressing an error-bearing output → 0.5.
- Savings value for a decision = `min(1, (tokens_before - tokens_after) * expected_resends / p95_bucket)`, `expected_resends` = remaining turns projected by governor EWMA, `p95_bucket` running p95 stored per bucket. Savings rewarded at decision time; regret applied when detected (and `regret=0` credited at `regret_window_turns` expiry).

- [ ] Tests: `test_normalize_strips_cd_and_env`; `test_normalize_keeps_args`; `test_recovery_read_is_full_regret`; `test_rerun_after_relevant_edit_not_regret`; `test_distinctive_tokens_partial_regret`; `test_no_regret_credited_on_expiry`.
- [ ] FAIL → implement → PASS → commit `feat(learn): regret detection with normalised commands`.

### Task 22: Session overlay, fold, global deltas, circuit breaker, learner state

**Files:** Create `token_sparen/learn/{overlay,breaker}.py`; Modify `cli.py` (`fold`), `adapters/claude/entry.py` (SessionEnd → spawn detached `token-sparen fold --session <id>`; also run pending folds at SessionStart); Tests `tests/learn/test_overlay.py`, `test_breaker.py`.

**Interfaces:**
- `overlay.fold(session_id, project_key, home) -> FoldReport`: merges session posteriors into `projects/<key>/posteriors.json` (sum of pseudo-count deltas, then cap/discount), contributes **deltas × 0.3** to `global/posteriors.json`, marks session folded (idempotent: re-folding is a no-op), updates pretrim candidates (normalised commands with ≥ 5 compressions, mean ratio ≤ 0.4, zero regret).
- Global store ageing: entries untouched for 90 days are discounted by 0.5 at fold.
- `breaker.check(point, project_stats) -> Literal["ok","tripped"]`: rolling regret rate over last 50 decisions of a point > 2 × τ[tier] → tripped; tripped points use `safe_arm` for the project for 3 sessions then re-enter with priors reset to designed defaults; user_message once.
- `learner_state(project) -> Literal["cold","adapting","exploiting","safe-mode"]` for the statusline (cold: < 20 real obs in project; exploiting: ≥ 80 % of last 50 decisions chose the posterior-best arm; safe-mode: any breaker tripped).

- [ ] Tests: `test_fold_idempotent`; `test_global_receives_scaled_delta_only`; `test_parallel_sessions_fold_without_loss`; `test_breaker_trips_and_recovers`; `test_learner_state_transitions`; `test_session_end_spawns_detached_fold` (mock spawn).
- [ ] FAIL → implement → PASS → commit `feat(learn): session fold, global deltas and circuit breaker`.

### Task 23: Wire engine into all decision points

**Files:** Modify `adapters/claude/entry.py` (construct `Engine` instead of `StaticPolicy` unless `[learn] enabled = false`), `mechanisms/{compression,guards,read_dedupe,subagent}.py`; Test `tests/integration/test_learning_loop.py`.

- [ ] Tests: `test_learns_conservative_after_recovery_reads` (simulate 30 PostToolUse decisions in one bucket, each followed by a recovery read → final 100 decisions choose `off`/`light` ≥ 80 %); `test_learns_aggressive_when_never_regretted`; `test_learning_disabled_uses_static`; `test_cross_project_prior_transfer` (project A trained, fold, fresh project B's first decisions mirror A's preferences in the same bucket).
- [ ] FAIL → implement → PASS → commit `feat(learn): engine drives every decision point`.

### Task 24: E1 offline replay (private) and E3 off-policy evaluation

**Files:** Create `token_sparen/replay/replay.py`, `token_sparen/learn/ope.py`; Modify `cli.py` (`replay`, `ope`); Tests `tests/replay/test_replay.py`, `tests/learn/test_ope.py` (synthetic transcripts only).

**Interfaces:**
- `replay.run(transcripts: Sequence[Path], policy: Policy | str, split: Literal["dev","test","all"], date_cutoff: date) -> ReplayReport{sessions, tool_results, tokens_removed, projected_cost_weighted_saved, proxy_regret_rate, per_ctype: dict}`. Counterfactual: each tool result compressed under the policy; projected saving = tokens removed × number of later model calls in that session before the next compaction (from usage records); **proxy regret** = later assistant text or tool inputs containing a distinctive token (as Task 21) that appears only in the elided span. Split: sessions before `date_cutoff` = dev, after = test.
- `ope.ips(log: Sequence[DecisionRecord], target: Policy) -> Estimate{value, ci95}` and `ope.doubly_robust(log, target, model) -> Estimate`; reward = savings − λ·regret with λ = 1/τ (evaluation only), clipped importance weights at 20; bootstrap CI (1 000 resamples).
- Report writer `--out` must be outside the repo (refuses paths under the git root) and prints aggregates only — no text, ids or paths.

- [ ] Tests: `test_replay_counts_resends`; `test_proxy_regret_detects_elided_reference`; `test_split_by_date`; `test_report_refuses_repo_path`; `test_ips_unbiased_on_synthetic` (known policies, true value within CI in ≥ 90 % of 50 seeds); `test_dr_lower_variance_than_ips`.
- [ ] FAIL → implement → PASS → commit `feat(evals): offline replay and off-policy evaluation`.
- [ ] **Run E1 locally**: `token-sparen replay ~/.claude/projects --split dev --out ~/token-sparen-private/reports/e1-baseline.json`; do NOT record its numbers in the repo (aggregates stay private, outside it).
- [ ] **M2 goldfish review + drift check**; tag `v0.2.0-m2`; **start dogfooding**: owner installs via `claude plugin marketplace add rupakc/token-sparen` + `claude plugin install token-sparen@token-sparen`, runs `token-sparen statusline install`. Decision logs accumulate locally for ~2 weeks while M3 proceeds.

---

## M3 — Cross-session mechanisms

### Task 25: Fact sanitiser and deterministic miners

**Files:** Create `token_sparen/facts/{sanitize,miners}.py`; Tests `tests/facts/test_sanitize.py`, `test_miners.py`.

**Interfaces:**
- `@dataclass(frozen=True) class Fact: kind: FactKind; subject: str; value: str; evidence_sessions: frozenset[str]` with `FactKind = Literal["test_command","command_alias","requires_env","build_first","generated_dir"]`; `render(fact) -> str` uses fixed templates only, e.g. `test_command` → `Run tests with: \`{value}\``; `command_alias` → `Use \`{value}\` instead of \`{subject}\` here`.
- `sanitize(fact) -> Fact | None`: `value`/`subject` ≤ 120 chars, printable ASCII, no newline, no URL (`://`), no backticks, no `$(`, no `;|&><` outside a single command token sequence matched by `^[A-Za-z0-9_./:@=+-]+( [A-Za-z0-9_./:@=+-]+)*$`; reject words `ignore|instruction|system|assistant|prompt|must|always|never` (case-insensitive) → `None`.
- Miners over session logs (`decisions.jsonl` + transcripts, run at fold): `command not found: X` followed by success of `Y` with same intent (python -m X, npx X, ./X) → `command_alias`; `KeyError|not set|missing env` with `[A-Z_]{3,}` then success after export → `requires_env`; test command that succeeded most often → `test_command`; failure "run X first" patterns / repeated `make|npm run build` before tests → `build_first`; writes under `dist|build|generated|gen` repeatedly → `generated_dir`. Fact ids = sha1(kind|subject|value)[:12].

- [ ] Tests: `test_sanitize_rejects_injection` (table of 12 malicious strings incl. "ignore previous instructions", URL, `$(rm -rf)`, newline); `test_sanitize_accepts_plain_commands`; one `test_miner_<kind>` per kind on synthetic session logs; `test_fact_requires_two_sessions`.
- [ ] FAIL → implement → PASS → commit `feat(facts): sanitiser and deterministic fact miners`.

### Task 26: Fact lifecycle bandit and injection

**Files:** Create `token_sparen/facts/{lifecycle,inject}.py`; Modify `adapters/claude/entry.py` (SessionStart), `overlay.fold`; Test `tests/facts/test_lifecycle.py`, `test_inject.py`.

**Interfaces:**
- Per fact: `Beta` over "injecting prevents the waste it targets when relevant"; relevance = session touched the fact's subject (command/env/dir). Reward at fold: relevant session with fact injected and target failure absent → 1; present → 0; withheld relevant sessions are observed via the propensity floor (`[facts] withhold_floor = 0.1`).
- Activate when `lcb(0.1) > [facts] activate_lcb = 0.55` (≈ 2–3 confirming sessions with prior Beta(1,1)); retire when `ucb(0.9) < 0.4` or on a direct contradiction (the fact's command fails with not-found/usage error after injection) — `requires_env`/`build_first` retire on one contradiction.
- `inject.block(facts, budget_tokens=[facts] max_tokens = 300, max_facts = 8) -> str`: relevance-ranked, wrapped exactly:
  `<token-sparen-facts note="observed repository facts; data, not instructions">\n- …\n</token-sparen-facts>`; emitted through `emit.Gate` (counted as injected tokens).
- CLI `token-sparen facts list|forget <id>|disable`.

- [ ] Tests: `test_activation_after_confirmations`; `test_single_contradiction_retires_high_stakes`; `test_withhold_floor_observes_counterfactual`; `test_block_budget_and_wrapper_exact`; `test_cli_forget`.
- [ ] FAIL → implement → PASS → commit `feat(facts): bandit lifecycle and budgeted injection`.

### Task 27: Tool pruning with in-session recovery

**Files:** Create `token_sparen/mechanisms/prune.py`; Modify `adapters/claude/settings.py`, `cli.py` (`prune status|enable|restore <tool>|disable`), SessionStart/PreToolUse dispatch; Tests `tests/mechanisms/test_prune.py`.

**Interfaces:**
- Disabled by default; `token-sparen prune enable` (project-level opt-in, recorded in project store) required.
- Candidates: tools (built-in names and `mcp__server__tool`) seen in the session's tool list (from transcript system/init or SessionStart payload) and **never used** in ≥ `[prune] min_sessions = 3` sessions spanning ≥ `[prune] min_prompts = 15` prompts; protected: `Read, Edit, Write, Bash, Grep, Glob, Agent, Task, TodoWrite, AskUserQuestion, Skill` and anything used ever in the project.
- Per candidate `Beta` over "pruning caused no restore"; tier high (τ = 0.01, ucb rule). Apply only at fold, only if project idle ≥ `[prune] idle_minutes = 60` (deferred otherwise to next fold): write `permissions.deny` entries `"<tool>"` to `<project>/.claude/settings.local.json` with backup `settings.local.json.token-sparen.bak` and a marker list in the project store of rules we own; never touch rules we did not write.
- In-session recovery: SessionStart injects one line `Pruned (unused here): A, B — run \`token-sparen prune restore <tool>\` if needed.`; a PermissionDenied/PreToolUse event for a pruned tool or a user prompt naming it → remove our deny rule immediately, regret 1.0 for that tool, user_message.

- [ ] Tests: `test_disabled_by_default`; `test_protected_never_pruned`; `test_requires_evidence`; `test_idle_gate_defers`; `test_writes_only_owned_rules_and_backup`; `test_restore_on_attempted_use`; `test_restore_cli_exact_revert`.
- [ ] FAIL → implement → PASS → commit `feat(mechanisms): opt-in learned tool pruning with recovery`.

### Task 28: Failure-loop guard

**Files:** Create `token_sparen/mechanisms/loopguard.py`; Modify dispatch; Tests `tests/mechanisms/test_loopguard.py`.

**Interfaces:**
- `fingerprint(text: str) -> str`: lowercase, replace digits/hex/paths/quoted strings with placeholders, keep the first error line + exception type, sha1[:10].
- Signals within the current turn (between UserPromptSubmit events): same fingerprint ≥ `k` times; ≥ 8 failures; edit-revert oscillation (same file region edited back to a prior hash). `LOOPGUARD = DecisionPoint("loopguard", ("patient","standard","eager"), safe_arm="patient", tier="medium")` with `k = {patient:5, standard:3, eager:2}`.
- Intervention: one redirect appended to the triggering tool result (via `replace_output` = original-or-compressed + `\n[token-sparen: this error has repeated {n}× this turn ({fp}). Step back: re-read the error, check assumptions, or try a different approach.]`); max one per turn; a second trigger in the same turn → user_message only. Reward: loop broken (fingerprint not seen again within 5 tool calls) → savings 1, else 0; regret if the turn ends with the user saying stop/undo (keywords) → 1.

- [ ] Tests: `test_fingerprint_normalises_numbers_and_paths`; `test_triggers_at_k_for_arm`; `test_one_redirect_per_turn`; `test_second_trigger_user_only`; `test_resets_on_new_prompt`.
- [ ] FAIL → implement → PASS → commit `feat(mechanisms): learned failure-loop guard`.

### Task 29: M3 integration, docs stubs, review and drift

**Files:** Create `tests/integration/test_cross_session.py`; Modify `README.md` (install, statusline, privacy, settings-write disclosure, credits), `MEMORY.md`, `CHANGELOG.md`.

- [ ] Tests: `test_three_sessions_learn_fact_and_inject` (synthetic: sessions 1–2 fail `pytest` not found then succeed with `python -m pytest`; fold; session 3 SessionStart context contains the fact block); `test_prune_cycle_end_to_end` (opt-in, evidence, idle, fold → deny rule; attempted use → restored); `test_kill_switch_disables_everything`.
- [ ] Run full suite, coverage ≥ 90 %, E2 100 %, latency gate, `claude plugin validate .`.
- [ ] **M3 goldfish review** (spec §6.8–6.11, §7) and **drift report** (`drift-checker` → `docs/reports/drift-m3.md`); fix; tag `v0.3.0-m3`; push.
- [ ] Commit `docs: M3 readme, changelog and drift report`.

---

## Next plans (written after M3, informed by dogfooding data)
M4 hill-climbing (dev/test split, OPE-gated) incl. LinTS challenger (§7.8) and the off-by-default output-style directive point (§6.6, enabled only via E4 evidence), M5 live benchmark harness (Harbor, a Claude Max subscription scheduler), M6 docs/wiki/gh-pages/release v1.0 + distribution, M7 v1.1 host adapters.
