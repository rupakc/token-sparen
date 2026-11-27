# token-sparen M5 — Live benchmark E4 (screening → power → confirmation) and release gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure token-sparen live in Claude Code on SWE-bench Verified tasks (Mini first, extended to the size a power step requires) against plain Claude Code, a concise-prompt control and competitors under a Claude Max subscription, with a frozen plugin, a quota-aware resumable scheduler, the official grader on arm64 and the §10.5 statistics; decide the output-style point (§6.6) and the v1.0 release gate; publish `e4.json` and a public report that M6 consumes.

**Architecture:** A dev-time package `bench/` (never imported by `token_sparen/`, not in the wheel) runs each (arm, seed, task) as `claude -p … --output-format stream-json` inside the task's arm64 SWE-bench image, with a read-only toolchain mount, a read-only frozen plugin copy and per-run state copied in and out of the container, so learners accumulate state per repository in commit order and a crashed run never touches the persisted state. An append-only ledger with typed statuses and a circuit breaker makes the scheduler idempotent across 5-hour windows. Patches are graded by the official `swebench` 5.0.2 CLI against a dataset override that points at the arm64 images. `bench/stats.py`, `bench/power.py`, `bench/stages.py`, `bench/gate.py` and `bench/report.py` turn the ledger into CIs, the confirmation size, the release gate and the published `e4.json`. A fake backend, fake task and fake grader run the whole pipeline in CI with no Docker, network, tag or quota.

**Tech Stack:** Python ≥ 3.11 stdlib only for `token_sparen/`, `bench/`, `scripts/plugin_hash.py`; `hooks/run.py` and `hooks/_pyfind.py` parse under Python 3.6. The grader lives in its own venv (`bench/requirements-grade.txt`: `swebench==5.0.2`). Figures are hand-written SVG. pytest, ruff, mypy for dev.

**Spec:** `docs/specs/2026-11-25-token-sparen-design.md` (§10.4, §10.5, §6.6, §6.7, §14 M5). Previous plan: `docs/plans/2026-11-27-token-sparen-m4.md`. Binding rulings for this plan: `.superpowers/sdd/2026-11-27-token-sparen-m4/progress.md` (M5-B1, M5-B2, M5-B3, M5-M10/Task 1, M6-B1, M6-B2, and the owner answers of 2026-11-27); cold review: `.superpowers/sdd/2026-11-27-token-sparen-m4/m5-plan-goldfish.md`. Execution ledger: `.superpowers/sdd/2026-11-27-token-sparen-m5/progress.md` (created at execution). Consumer: `docs/plans/2026-11-27-token-sparen-m6.md` (reads `e4.json`, `scripts/plugin_hash.py`).

**Design decisions (record in `MEMORY.md` and the spec revision log, Task 12):**
- **Own runner, not Harbor** (spec's named fallback): Harbor 0.27's `claude-code` agent has no `--plugin-dir` or free flags, forces `CLAUDE_CONFIG_DIR` into its log dir, uses amd64-only images (emulated here, 1 CPU / 4 GB), and has no learner streams or quota windows. Harbor stays the candidate for E5.
- **Task pool**: SWE-bench Verified Mini (HF `MariusHobbhahn/swe-bench-verified-mini`, 25 django + 25 sphinx) first; the **extended pool** = SWE-bench Verified (`SWE-bench/SWE-bench_Verified`) tasks with a prebuilt Docker Hub image `swebench/sweb.eval.arm64.<instance_id with "__" replaced by "_1776_">:latest` (verified 2026-11-27 for django and sphinx; e.g. `sweb.eval.arm64.django_1776_django-12774`), repos of Mini first. No local image builds.
- **Grading local only** (owner, 2026-11-27): official `swebench` 5.0.2 CLI on the same arm64 image the agent ran in; no sb-cli. A task whose gold patch does not resolve twice in a row locally is dropped before any agent run and listed with its reason.
- **Power before confirmation** (M5-B1): screening data set the confirmation size (tasks × seeds) for ≥ 80 % power to pass −5 pp when the true difference is 0; confirmation arms = baseline + token-sparen (+ best competitor, then `ts-style`, only if quota allows). Required size beyond 6 weeks of quota → `underpowered`: the gate reads "not failed, documented" and the owner decides.
- **Frozen plugin per stage** (M5-B3): exported from a tag into the bench workspace, mounted read-only, hash checked before every run.
- **Plugin hash and `e4.json` are owned here** (M6-B1) and published as interfaces M6 consumes (Task 3).
- **Output style (§6.6)** ships off by default in v1.0 whatever E4 shows (spec amended in Task 2); a passing rule lets the README recommend the opt-in.
- **TokenEconomizer runs as documented** (owner): it does not learn from headless `claude -p` sessions; reported as a limitation of the comparison, not reconfigured.
- **Cold-start ablation** (`ts-cold`) is screening-only.

## Global Constraints

- Everything in the M0–M4 global constraints still binds (CLAUDE.md "Invariants" and "Never commit private data").
- `token_sparen/` never imports `bench`; `bench/` may import `token_sparen`. `bench/` is excluded from the wheel (`scripts/check_wheel.py` asserts it). Every committed file under `bench/` < 256 KiB.
- **Never touch the real Claude config**: agents and installs run with `CLAUDE_CONFIG_DIR` and `HOME` inside the bench workspace; `bench.isolation.assert_isolated` refuses `~/.claude`, anything under it, `~/.claude.json`, the caller's `CLAUDE_CONFIG_DIR`, and any workspace inside a git work tree.
- **OAuth token** (`claude setup-token`, owner step; the only credential, no `ANTHROPIC_API_KEY`, no sb-cli key): passed to `docker run` by name only (`-e CLAUDE_CODE_OAUTH_TOKEN`), never in argv, files, ledger, logs or reports. Inside the container `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` strips it from every Bash, hook and stdio MCP subprocess (agent and competitor code never see it). Every run's outputs are scanned for the token value and the `sk-ant-oat` pattern before anything is written to the ledger; a hit quarantines the run (`infra_fail{leak}`), deletes its files and stops the campaign (owner rotates the token). The token is revoked after the campaign (Task 16).
- **Workspace** (`--out`, env `TOKEN_SPAREN_BENCH_OUT`; no default) lives outside any git work tree (`replay.report.check_out_path`; 0700 dirs, 0600 files). Only `bench report --publish` writes into the repo (`docs/evidence/e4.json`, `docs/benchmarks/<campaign>/`).
- **Figures are allowed only for E4 public benchmark results** (spec §10.5, amended in Task 12). `bench/report.py` refuses any input that is not an E4 ledger; E1/E3 data never reaches it.
- **Claims policy (§10.5)**: every published number carries its 95 % CI and method, n (tasks), seeds, model id, Claude Code version, date range, plugin version, plugin hash (Task 3 definition) and harness commit. Cost per run = `total_cost_usd` + plugin overhead cost (side-call sessions), list price, cache-aware, all token classes kept; ratio = paired BCa bootstrap 95 % CI of the median per-task ratio. Quality: non-inferiority at −5 pp, the lower bound of the two-sided 95 % BCa task-cluster bootstrap CI of the resolved-rate difference must be > −0.05; the conditional exact McNemar CI per seed is published alongside (labelled "conditional exact"). Only token-sparen vs baseline is confirmatory; every other comparison is labelled descriptive. Quota % is secondary.
- **Fixed per stage, identical across arms** (recorded): Claude Code native binary version (pinned, `DISABLE_AUTOUPDATER=1`), `--model`, `--effort high`, `--max-turns 80` plus `CLAUDE_CODE_MAX_TURNS=80`, wall timeout 3600 s, prompt `bench/prompt.md`, `--output-format stream-json --verbose --include-hook-events`, `--permission-mode bypassPermissions` with `IS_SANDBOX=1`, `--setting-sources user`, `--strict-mcp-config --mcp-config <arm>.json`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, `DO_NOT_TRACK=1`. The agent has network access (needed by tools); disclosed in the report (upstream fixes are reachable; paired arms share the bias).
- **Stages**: screening = Mini, 1 seed, `--model sonnet` (resolved id recorded), effort high (owner). Power step (Task 14) fixes the confirmation size. Confirmation = Claude Code's default model at the first confirmation run (resolved id pinned for the stage).
- **Quota** (owner: pause at 60 % weekly): never start a run when the last weekly reading is ≥ 60 % (stop, exit 3, ask the owner; never sleep through a weekly reset) or the five-hour reading is ≥ 85 % (wait until `resets_at` with `--wait`, else exit 4); at most `max_runs_per_window` runs per five-hour window (default 4 until calibrated in Task 13).
- **Resources** (Docker VM 8 GB, shared machine): one container at a time (runs and grading share `workspace/bench.lock`); container `--memory 4g --memory-swap 4g --cpus 4 --pids-limit 4096`; disk is sized in the power step (Task 10); a task's image is pulled when its block starts, all arms and seeds of that task run back to back, the task is graded, then the image is removed unless a later stage needs it (Mini images are kept through confirmation); a free-disk floor of 30 GB (host and Docker disk) pauses the scheduler with `infra_fail/disk` (not counted by the breaker); per stream only the last promoted state is kept; `docker image prune -f` (dangling only) after each removal.
- No real-quota run before Task 13; tests and CI use the dry-run path only, which needs no tag (M6-B2).

## Review Focus

1. **Silently inert arm** (hook `python3` < 3.11, plugin MCP server dropped by `--strict-mcp-config`, wrapper proxy not on the request path, competitor default that needs a manual trigger) → `infra_fail{load}`, never graded; liveness from hook lifecycle events plus state probes. → Task 1 `test_launcher_reexecs_newer_python`, Task 5 `test_liveness_from_hook_events_per_arm`.
2. **Tool artefacts in the graded patch** (`.token-sparen/`, `.context-mode/`, `RTK.md`, a tool-created `CLAUDE.md`, `.claude/`) → dropped when untracked at the base commit; the agent's own new files kept. → Task 6 `test_patch_excludes_tool_artefacts_keeps_new_files`.
3. **Infrastructure failure graded as agent failure** (expired token, 5xx/529 after retries, dead proxy, Docker daemon down, OOM, quota rejection) → typed `infra_fail{…}`, retried, never graded; a breaker stops the campaign. → Task 5 `test_status_mapping_table`, Task 7 `test_breaker_pauses_after_consecutive_infra_fails`.
4. **Crash or restart mid-stream** (orphan container still running, state half-written, task k+1 before k) → state is promoted only from a finished run's copy-out; orphans are killed by name before any rerun. → Task 6 `test_orphan_container_killed_before_rerun`, Task 7 `test_resume_after_crash_reruns_from_promoted_state`, `test_stream_order_respected`.
5. **OAuth token reaching the agent or an artefact** → env scrub inside the container, name-only passing, leak scan before ledger write. → Task 6 `test_token_never_in_argv_or_files`, Task 5 `test_leak_scan_quarantines`.

---

### Task 1: Hook launcher finds a Python ≥ 3.11 (ships to users on its own)

Verified: `hooks.json` runs plain `python3`; `run.py` turns the failed import (`tomllib`, 3.11+) into a silent `{}` with no log line. macOS ships 3.9, Ubuntu 22.04 ships 3.10, SWE-bench testbeds put older Pythons first. This task is pushed as soon as it is green (ruling M5-M10/Task 1).

**Files:** Create `hooks/_pyfind.py`; modify `hooks/run.py`, `token_sparen/cli.py` (`doctor`, `_python3_version` → uses `_pyfind`), `README.md` ("Requirements", `TOKEN_SPAREN_PYTHON`), `CHANGELOG.md`. Test `tests/test_hook_launcher.py`, `tests/test_cli.py`, `tests/perf/test_launcher_reexec.py`.

**Interfaces (produces):**
- `hooks/_pyfind.py` (Python 3.6 syntax, stdlib): `find_python(env, current_version, which=shutil.which, run=subprocess.run) -> tuple[str, str] | None` returns `(path, how)`: order `$TOKEN_SPAREN_PYTHON` → `python3.15` … `python3.11` (descending) → on Windows `py -3.15` … `py -3.11` → `uv python find ">=3.11"` with `UV_PYTHON_DOWNLOADS=never` (never downloads). A candidate is accepted only if its own `sys.version_info >= (3, 11)` (one probe, result cached in `<state root>/launcher.json` keyed by the resolved `python3` realpath + mtime and the candidate's realpath + mtime; cache only read/written when the state root exists and is owned by the user).
- `hooks/run.py`: before reading or reconfiguring stdin, if `sys.version_info < (3, 11)`: `found = find_python(...)`; POSIX → `os.execv(path, [path, "-I", __file__, *argv[1:]])` with `TOKEN_SPAREN_REEXEC=1`; Windows → `subprocess.run([...], stdin=sys.stdin.buffer)` and exit with its code. The re-executed launcher pops `TOKEN_SPAREN_REEXEC` from `os.environ` at start (no leak to hook subprocesses) and refuses a second hop. Nothing found → append one line `{"ts":…, "kind":"launcher", "error":"no python >= 3.11"}` to `<state root>/logs/errors.jsonl` (at most once per day, stdlib only), print `{}`, exit 0.
- `doctor`: reports the interpreter the hook would actually use (`find_python` from the user's PATH and env) and how it was found; warns only when none is found.
- Perf: `tests/perf/test_launcher_reexec.py` measures the end-to-end subprocess PostToolUse call on the re-exec path (forced by `TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC=1`, honoured only with `TOKEN_SPAREN_HOME` set, as the test seed) vs the direct path; budget: re-exec p95 ≤ direct p95 + 40 ms (× `TOKEN_SPAREN_PERF_FACTOR`); the measured overhead is stated in README.

- [ ] **Step 1:** Failing tests `test_launcher_reexecs_newer_python` (version and `which` patched; asserts `os.execv` argv), `test_candidates_descend_from_315_to_311`, `test_uv_find_never_downloads`, `test_candidate_probe_rejects_old_python`, `test_cache_invalidated_when_python3_changes`, `test_no_candidate_logs_once_and_prints_noop`, `test_reexec_flag_not_leaked`, `test_never_loops`, `test_windows_spawn_passthrough_exit_code`, `test_launcher_sources_parse_as_py36` (`ast.parse(src, feature_version=(3, 6))` for both files), `test_doctor_reports_hook_interpreter`.
- [ ] **Step 2:** `pytest tests/test_hook_launcher.py tests/test_cli.py -v` → FAIL.
- [ ] **Step 3:** Implement. **Step 4:** full battery (CLAUDE.md "Commands") and `TOKEN_SPAREN_PERF_FACTOR=1.5 pytest -m perf --no-cov tests/perf/test_launcher_reexec.py` → PASS.
- [ ] **Step 5:** Commit `fix(hooks): find a Python >= 3.11 when python3 is older`; push once CI is green.

### Task 2: Output-style directive (§6.6), off by default

**Files:** Create `token_sparen/mechanisms/outputstyle.py`; modify `token_sparen/adapters/claude/entry.py` (register on `session_start` after `_on_facts`), `token_sparen/params/defaults.toml`, `token_sparen/config.py` (`PROJECT_POLICY`), `docs/specs/2026-11-25-token-sparen-design.md` (§6.6: "off by default in v1.0; E4 decides whether the README recommends the opt-in; a default change needs a new confirmation"), `README.md`, `CHANGELOG.md`. Test `tests/mechanisms/test_outputstyle.py`, `tests/adapters/claude/test_entry.py`.

**Interfaces (produces):**
- `[output_style] enabled = false`, `directive = "concise"`; `DIRECTIVES: dict[str, str] = {"concise": "Be concise in prose. Never shorten code, commands, file paths, error messages or quoted tool output."}`; an unknown name → off, logged once.
- `on_session_start(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action`: `additional_context` = `[token-sparen output style] ` + directive on sources `startup`, `clear`, `compact`, not `resume`; once per context epoch (session store flag, as `facts.inject`). Static, not a decision point.
- `PROJECT_POLICY`: `("output_style", "enabled")` → `false_only`; `("output_style", "directive")` → `user_only`.

- [ ] **Step 1:** Failing tests `test_off_by_default_injects_nothing`, `test_on_injects_once_per_context_epoch`, `test_resume_does_not_reinject_compact_does`, `test_project_file_cannot_enable`, `test_unknown_directive_is_off`, `test_disable_env_noop`.
- [ ] **Step 2–4:** fail → implement → full battery. **Step 5:** commit `feat: output-style directive (off by default, spec 6.6)`.

### Task 3: Published interfaces — plugin hash, frozen plugin, `e4.json` schema

**Files:** Create `scripts/plugin_hash.py`, `bench/__init__.py`, `bench/freeze.py`, `bench/e4.py`, `bench/e4.schema.json`; modify `pyproject.toml` (ruff/mypy include `bench`), `scripts/check_wheel.py`. Test `tests/test_plugin_hash.py`, `tests/bench/test_freeze.py`, `tests/bench/test_e4.py`.

**Interfaces (produces; M6 consumes `plugin_hash`, `PLUGIN_FILES`, `e4.json`):**
- `scripts/plugin_hash.py` (binding ruling; M6 consumes it by name): `PLUGIN_DIRS = (".claude-plugin/", "hooks/", "skills/", "token_sparen/")` — the executable plugin content as shipped on `release/claude`, `token_sparen/params/defaults.toml` included. Excluded: README, LICENSE, NOTICE, SECURITY.md, CHANGELOG, `docs/`, `tests/`, VCS metadata (`.git/`, `.gitignore`, `.gitattributes`, `.gitmodules`), and files never shipped (`__pycache__/`, `*.pyc`, `.DS_Store`). `plugin_files(root: Path) -> list[str]` (sorted repo-relative POSIX paths); `plugin_hash(root: Path) -> str` = sha256 hex over the sorted list of `(path, sha256(file bytes))`, serialised as the concatenation of `f"{path}\0{sha256_hex}\n"` lines in path order. Raw bytes, no normalisation, not a git tree hash: any change in those directories (version bump included) changes it. CLI prints the hash; `main(argv: list[str] | None = None) -> int`.
- `bench/freeze.py`: `freeze(repo: Path, ref: str, dest: Path) -> FrozenPlugin(path: Path, ref: str, commit: str, plugin_hash: str)`: `git archive <ref>` of `PLUGIN_FILES` into `dest`, files `0444`, dirs `0555`; refuses a ref that is not a tag unless `dry_run=True` (M6-B2); `verify(frozen: FrozenPlugin) -> None` recomputes the hash and raises `FrozenPluginChanged`.
- `bench/e4.py`: `SCHEMA_VERSION = 1`; `validate_e4(doc: Mapping[str, Any]) -> list[str]` (stdlib checker of `e4.schema.json`, JSON Schema 2020-12 subset). Required top level: `schema_version`, `source == "E4"`, `suite` (e.g. `"swe-bench-verified-mini"` / `"swe-bench-verified-arm64-pool"`), `stage`, `campaign`, `n_tasks`, `seeds`, `model`, `claude_code_version`, `date` (`{from, to}` ISO dates), `plugin_version`, `plugin_hash`, `harness_commit`, `script` (`"bench/"` command line), `grader` (`{name: "swebench", version, arch: "arm64"}`), `arms` (list of `{name, kind, pin}`), `exclusions` (list of `{instance_id, reason}`), `power` (`{required_tasks, seeds, achieved_power, verdict: "powered"|"underpowered"}` or null in screening), `results` (list of `{metric, arm, baseline, estimate, ci95: [lo, hi], n, method, confirmatory}`; metrics `cost_ratio_median`, `resolved_rate`, `resolved_diff_pp`, `tokens_<class>_median`, `turns_median`, `wall_s_median`), exactly one `resolved_diff_pp` result with `arm == "token-sparen"`, `baseline == "baseline"`, `confirmatory == true`, `margin_pp == -5` and `non_inferior: bool`, plus `gate` (`{verdict: "passed"|"failed"|"underpowered", reasons}`) and `style_rule`.

- [ ] **Step 1:** Failing tests `test_plugin_hash_stable_and_sorted`, `test_plugin_hash_matches_reference_serialisation` (hand-computed on a two-file fixture), `test_plugin_hash_ignores_docs_tests_and_vcs`, `test_plugin_hash_changes_on_any_plugin_edit_incl_version`, `test_freeze_read_only_and_hash_matches`, `test_freeze_refuses_untagged_ref_unless_dry_run`, `test_verify_detects_change`, `test_e4_example_valid` (fixture `tests/bench/fixtures/e4_example.json`), `test_e4_missing_key_named`, `test_wheel_has_no_bench`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): plugin hash, frozen plugin and e4.json schema`.

### Task 4: Tasks, arms registry and isolation

**Files:** Create `bench/tasks.py`, `bench/arms.py`, `bench/arms.toml`, `bench/isolation.py`, `bench/prompt.md`, `bench/data/pool.jsonl`, `bench/data/fetch_pool.py`, `bench/fixtures/fake/` (tiny git repo: `calc.py` off-by-one, `test_calc.py`, gold `fix.patch`, `agent.py` fake `claude`); test `tests/bench/test_tasks.py`, `tests/bench/test_arms.py`, `tests/bench/test_isolation.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class Task: instance_id: str; repo: str; base_commit: str; created_at: str; image: str; mini: bool`; `arm64_image(instance_id: str) -> str` = `"swebench/sweb.eval.arm64." + instance_id.replace("__", "_1776_") + ":latest"`.
- `pool.jsonl` (committed, no problem statements, < 256 KiB): header `{"verified_revision": "<hf sha>", "mini_revision": "<hf sha>", "fetched": "<date>"}`, then one row per Verified task with an arm64 image (Docker Hub tag API), `mini` flag. `fetch_pool.py` (network, dev-time) regenerates it. Problem statements and gold patches are fetched at prepare time into the workspace (`dataset.jsonl`, pinned revision, sha256 recorded) by `load_dataset_rows(workspace: Path) -> dict[str, dict]`.
- `load_pool(path: Path = POOL_FILE) -> list[Task]`; `streams(tasks: Sequence[Task]) -> dict[str, list[Task]]` (repo → sorted by `(created_at, instance_id)`); `extend(pool: Sequence[Task], have: Sequence[Task], n: int, *, seed: int) -> list[Task]` (seeded draw stratified by repo, Mini repos first, pre-declared); `FAKE_TASKS` (two streams × two tasks on the fake repo).
- `@dataclass(frozen=True) class Arm: name: str; kind: Literal["baseline", "prompt", "plugin", "hook", "wrapper"]; state: Literal["none", "stream"]; state_paths: tuple[str, ...]; append_system_prompt: str | None; uses_frozen_plugin: bool; install: tuple[str, ...]; wrapper: tuple[str, ...]; env: Mapping[str, str]; ts_config: Mapping[str, Any]; liveness: str; patch_excludes: tuple[str, ...]`; `load_arms(path: Path = ARMS_FILE) -> dict[str, Arm]`.
- State semantics (all arms): every run starts from the arm's config template (fresh `CLAUDE_CONFIG_DIR`, no Claude Code memory); only `state_paths` of a `stream` arm carry over between tasks of one (stage, arm, seed, repo) stream. `token-sparen`, `ts-style`: `state = "stream"`, `state_paths = (".token-sparen",)`; `ts-cold`: token-sparen with `state = "none"`; `tokeneconomizer`: `state = "stream"`, `state_paths = (".tokeneconomizer", ".claude/projects")` (it documents reading past transcripts); all others `state = "none"`.
- `arms.toml`: `baseline`; `concise` (`append_system_prompt` = the exact `DIRECTIVES["concise"]` text of Task 2, so `ts-style` vs `concise` differs only by channel and the plugin); `token-sparen`, `ts-style` (`ts_config = {output_style = {enabled = true}}`), `ts-cold` (all `uses_frozen_plugin = true`, `TOKEN_SPAREN_PYTHON=/opt/bench/bin/python3.12`); `rtk` (`rtk-oss`, `rtk init -g`); `headroom` (`wrapper = ["headroom", "wrap", "claude", "--"]`); `caveman` (plugin `caveman@caveman` only); `context-mode` (plugin `context-mode@context-mode`); `tokeneconomizer` (plugin from a pinned clone). Common `patch_excludes`: `.token-sparen/`, `.claude/`, `CLAUDE.md`, `RTK.md`, `.rtk/`, `.context-mode/`, `.tokeneconomizer/`, `.headroom/`, applied only to paths untracked at `base_commit`.
- `assert_isolated(config_dir: Path, *, real_home: Path, environ: Mapping[str, str]) -> None` raises `IsolationError`; `check_workspace(out: Path) -> Path`.

- [ ] **Step 1:** Failing tests `test_streams_in_commit_order`, `test_mini_is_two_streams_of_25`, `test_arm64_image_name` (`django__django-12774` → `swebench/sweb.eval.arm64.django_1776_django-12774:latest`), `test_extend_is_seeded_and_stratified`, `test_pool_file_under_256k`, `test_arms_registry_validates`, `test_only_state_paths_persist`, `test_concise_uses_directive_text`, `test_refuses_real_claude_dir_and_claude_json` (incl. symlinks), `test_refuses_workspace_in_git_tree`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): task pool, arms registry and config isolation`.

### Task 5: Stream-json metrics, status classification, liveness and leak scan

**Files:** Create `bench/streamjson.py`, `bench/classify.py`, `bench/liveness.py`, `bench/leak.py`; test `tests/bench/test_streamjson.py`, `tests/bench/test_classify.py`, `tests/bench/test_liveness.py`, `tests/bench/test_leak.py`; fixtures `tests/bench/fixtures/*.jsonl` (synthetic).

**Interfaces (produces):**
- `Tokens(input: int, output: int, cache_creation: int, cache_read: int)` with `__add__`; `RateLimit(status: str, kind: Literal["five_hour", "weekly", "unknown"], resets_at: float | None, utilization: float | None)` (`kind` from `rate_limit_info.rateLimitType` when present, else `weekly` iff `resets_at - observed_at > 5 h`).
- `RunMetrics`: `session_id, model, claude_code_version, plugins, plugin_errors, mcp_servers: dict[str, str], skills, hook_events: list[HookEvent], subtype, is_error, api_error_status: int | None, error_text: str | None, total_cost_usd, tokens, model_usage, num_turns, duration_ms, rate_limits, api_retries, malformed_lines, overhead: Tokens, overhead_cost_usd`; `HookEvent(event: str, command: str, exit_code: int | None, ok: bool)`.
- `parse_stream(lines: Iterable[str], *, observed_at: float) -> RunMetrics` (never raises).
- `overhead_from_config_dir(config_dir: Path, main_session: str, since: float) -> tuple[Tokens, float]` (other sessions' assistant usage, priced with `token_sparen.estimate.Prices`).
- `run_cost(m: RunMetrics) -> float | None` = `total_cost_usd + overhead_cost_usd` (None if `total_cost_usd` is None).
- `classify(m: RunMetrics | None, *, exit_code: int, killed_for_timeout: bool, backstop_killed: bool, disk_low: bool, oom_killed: bool, load_problems: list[str], live: bool, leak: bool) -> RunStatus`, `RunStatus = tuple[Literal["ok", "agent_fail", "infra_fail"], str]` — the mapping table (first match wins):

  | condition | status |
  |---|---|
  | `leak` | `infra_fail/leak` |
  | docker exit 125/126/127, daemon unreachable, or `backstop_killed` | `infra_fail/docker` |
  | `oom_killed` | `infra_fail/docker` |
  | `api_error_status` 401/403 or error text matches auth/login/token-expired patterns | `infra_fail/auth` |
  | a `rate_limit_event` `rejected`, `api_error_status` 429, or "hit your … limit" text | `infra_fail/rate` |
  | `api_error_status` ≥ 500 (incl. 529) or `api_retry` exhausted | `infra_fail/api` |
  | wrapper arm and connection refused/reset to the local base URL | `infra_fail/proxy` |
  | no `system/init` event, `load_problems` non-empty, or not `live` | `infra_fail/load` |
  | free disk below the 30 GB floor | `infra_fail/disk` (pauses; not counted by the breaker) |
  | in-container `timeout` fired (`exit.json.timed_out`) | `agent_fail/timeout` |
  | `subtype == "error_max_turns"` | `agent_fail/max_turns` |
  | `subtype == "error_during_execution"` (no API status) | `agent_fail/execution` |
  | `subtype == "success"` | `ok/success` |
  | anything else | `infra_fail/unknown` |

- `verify_load(m: RunMetrics, arm: Arm, frozen_hash: str | None) -> list[str]` (expected plugin missing, any `plugin_errors`, expected MCP server not `connected`).
- `LIVENESS: dict[str, Callable[[RunMetrics, Path], bool]]` keyed by `Arm.liveness`, args `(metrics, copied_out_home)`: `none`; `token_sparen` (a token-sparen hook event with `ok` and a session file for this session id under the copied-out `.token-sparen`); `rtk` (an `ok` PreToolUse hook event from the `rtk hook` command); `headroom` (proxy request log for this run in the copied-out `.headroom/logs`); `context_mode` (`ok` hook event from its plugin and its MCP server `connected`); `tokeneconomizer` (`ok` hook event from its plugin); `caveman` (`ok` SessionStart hook event from its plugin; whether its default is active under `-p` is settled in Task 13).
- `scan_for_token(paths: Sequence[Path], token: str) -> list[Path]` (exact value and `sk-ant-oat[0-9A-Za-z_-]{8,}`); token read from the bench process env only.

- [ ] **Step 1:** Failing tests `test_parses_cost_and_all_token_classes`, `test_run_cost_includes_overhead`, `test_status_mapping_table` (one fixture per row), `test_rate_limit_kind_from_type_or_reset`, `test_malformed_lines_counted`, `test_overhead_sums_other_sessions_only`, `test_verify_load_flags_plugin_errors_and_missing_mcp`, `test_liveness_from_hook_events_per_arm` (every `liveness` in `arms.toml` has a probe; a token-sparen run whose hooks printed `{}` without a session file is not live), `test_leak_scan_quarantines`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): stream-json metrics, typed run statuses and liveness`.

### Task 6: Execution backends (fake and Docker)

**Files:** Create `bench/backends.py`, `bench/patch.py`, `bench/container/entry.sh`; test `tests/bench/test_backends.py`, `tests/bench/test_patch.py`.

**Interfaces (produces):**
- `RunRequest(task: Task, arm: Arm, seed: int, stage: str, model: str, effort: str, max_turns: int, timeout_s: int, state_in: Path, run_dir: Path, frozen: FrozenPlugin | None, attempt: int)`; `RunOutcome(status: RunStatus, metrics: RunMetrics | None, patch: str, state_out: Path | None, wall_s: float, problems: list[str])`; `class Backend(Protocol): def run(self, req: RunRequest) -> RunOutcome; def reap(self, campaign: str) -> int`.
- `container_name(key_id: str, attempt: int) -> str` = `"tsb-" + sha256(key_id)[:12] + f"-a{attempt}"`; labels `tsb.campaign`, `tsb.key`.
- `build_claude_argv(req: RunRequest, *, mcp_path: str) -> list[str]` (Global Constraints flags; prompt from `bench/prompt.md` + problem statement; `--plugin-dir /opt/plugin/token-sparen` for `uses_frozen_plugin`; wrapper prefix for `kind = "wrapper"`).
- `DockerBackend(toolchain: Path, templates: Path, *, memory: str = "4g", cpus: int = 4)` also exposes `ensure_image(image: str) -> None`, `remove_image(image: str) -> None` and `free_disk_gb() -> float` (min of host and Docker disk): before a run, `docker rm -f` any container labelled with this key; `docker run --name <container_name> --platform linux/arm64 --memory 4g --memory-swap 4g --cpus 4 --pids-limit 4096` on `task.image`, mounts `toolchain:/opt/bench:ro`, frozen plugin `:/opt/plugin/token-sparen:ro`, `state_in:/state-in:ro`, `run_dir:/run-out`; env by name only; `entry.sh` copies the arm template + `/state-in` into a container-local `HOME=/home/bench` (so ownership checks see a local, root-owned tree), sets `CLAUDE_CONFIG_DIR=/home/bench/.claude`, `TOKEN_SPAREN_HOME=/home/bench/.token-sparen`, runs claude in `/testbed` under coreutils `timeout --signal=TERM --kill-after=30 <timeout_s>` (ruling M5-N1: the agent timeout is enforced inside the container), and then **always** extracts the patch and copies `state_paths` and transcripts to `/run-out/state-out/`, writing `/run-out/exit.json` (`{agent_exit, timed_out}`); a timed-out run keeps its patch and state (`agent_fail/timeout`, graded, state promoted). The host `docker kill <name>` (never only the client) is a backstop at `timeout_s + 120` s; a backstop kill is `infra_fail/docker` (no patch, nothing promoted). `reap(campaign)` kills and removes every container with that campaign label (run at scheduler start). OOM read from `docker inspect .State.OOMKilled`.
- `FakeBackend(fixtures: Path = FAKE_DIR)`: same contract without Docker; scenarios by env: `success`, `fail`, `quota`, `auth`, `api`, `inert`, `timeout`, `leak`, `crash` (dies before writing `state-out`).
- `extract_patch(repo: str, base_commit: str, excludes: Sequence[str], *, run: Callable[[list[str]], str]) -> str` (`git add -A`, unstage excluded paths untracked at `base_commit`, `git diff --cached --binary <base>`).

- [ ] **Step 1:** Failing tests `test_patch_excludes_tool_artefacts_keeps_new_files`, `test_token_never_in_argv_or_files` (argv, `-e` flags and every written file of a fake run hold no token value), `test_argv_pins_flags` (incl. `--verbose`, `--include-hook-events`, `--max-turns 80`), `test_orphan_container_killed_before_rerun`, `test_timeout_inside_container_keeps_patch_and_state` (fake `timeout` exit 124 → `agent_fail/timeout`, patch and `state-out` present), `test_backstop_kill_at_timeout_plus_120` (→ `infra_fail/docker`, nothing promoted), `test_image_removed_after_task_block_unless_kept`, `test_docker_command_limits_and_mounts` (subprocess patched; `:ro` toolchain and plugin, no `ANTHROPIC_API_KEY`), `test_fake_backend_scenarios`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): fake and docker backends with container lifecycle and patch extraction`.

### Task 7: Ledger, quota, breaker and the resumable scheduler

**Files:** Create `bench/ledger.py`, `bench/quota.py`, `bench/scheduler.py`; test `tests/bench/test_ledger.py`, `tests/bench/test_quota.py`, `tests/bench/test_scheduler.py`.

**Interfaces (produces):**
- `RunKey(campaign: str, stage: str, arm: str, seed: int, instance_id: str)` (`id` = `"/".join(...)`).
- `Ledger(path: Path)`: header `{"kind": "e4-ledger", "schema": 1, "campaign", "stage", "harness_commit", "plugin_hash", "plugin_ref", "pins", "model", "claude_code_version"}`; `append(rec) -> None` (`flock`, `fsync`); records `start` (key, attempt, container name, `plugin_hash` re-verified), `end` (status, sub-status, metrics, run cost, patch sha256, wall_s), `quota`, `pause`, `invalidate` (key or time range, reason; operator command), `promote` (stream, key). `final(key) -> dict | None` = last non-invalidated `end` with status `ok` or `agent_fail`; `infra_fail` is never final.
- Retry: `infra_fail/{load,api,proxy,docker,unknown}` retried up to 3 attempts per key, after which the key is `unrunnable` (not graded, its task dropped from every pairing of that stage and listed in `exclusions`); `infra_fail/rate` re-queues without counting an attempt; `infra_fail/auth` and `infra_fail/leak` stop at once.
- Breaker: `Breaker(limit: int = 3)`: 3 consecutive `infra_fail` of any arm/kind (except `rate` and `disk`) → `paused_breaker`.
- `QuotaReading(at, source: Literal["stream", "owner"], kind, utilization_pct, resets_at)`; `QuotaPolicy(weekly_stop_pct: float = 60.0, five_hour_stop_pct: float = 85.0, max_runs_per_window: int = 4)`; `decide(readings, policy, now, runs_this_window) -> QuotaDecision(go: bool, reason: Literal["ok", "five_hour", "weekly", "window_cap"], wait_until: float | None)`. A weekly reading ≥ 60 % or a `rejected` weekly event never waits: stop.
- `plan_order(streams, arms, seeds, *, campaign, stage, shuffle_seed) -> list[RunKey]`: per position k, per repo: one block = every (arm, seed) of that task in a seeded permutation (ruling M5-N2: a task's runs are back to back; stream order per (arm, seed) still holds).
- After a block's keys are all final or `unrunnable`: grade the task (`grade_task`, Task 8), then `remove_image` unless `keep_images` (images a later stage needs) contains it. Before every run: `free_disk_gb() < 30` → record `infra_fail/disk`, status `paused_disk` (exit 10), never counted by the breaker.
- `run_campaign(keys, *, backend, ledger, arms, tasks, workspace, frozen, policy, grader, keep_images: frozenset[str], clock=time.time, sleep=time.sleep, wait=False) -> CampaignStatus` ∈ `complete | paused_five_hour | paused_weekly | paused_breaker | paused_disk | stopped_auth | stopped_leak | plugin_changed | incomplete`. Per run: `backend.reap` at start; skip final keys; task k+1 of a stream waits for k to be final or `unrunnable`; `frozen` re-verified before every run (mismatch → `plugin_changed`, no run); `state_in` = the stream's last promoted state (or the template for `state = "none"`); on a graded outcome the run's `state_out` is promoted (atomic rename) to the stream; on `infra_fail` nothing is promoted. Exit codes: complete 0, weekly 3, five-hour 4, incomplete 5, breaker 6, auth 7, leak 8, plugin changed 9, disk 10.

- [ ] **Step 1:** Failing tests `test_rerun_skips_final_keys`, `test_resume_after_crash_reruns_from_promoted_state`, `test_stream_order_respected`, `test_rate_requeues_without_attempt`, `test_infra_never_graded_and_unrunnable_after_three`, `test_breaker_pauses_after_consecutive_infra_fails`, `test_auth_stops_immediately`, `test_invalidate_reopens_key`, `test_weekly_60_stops_never_sleeps`, `test_window_cap`, `test_five_hour_waits_with_wait`, `test_arms_alternate_within_blocks`, `test_task_runs_back_to_back_then_graded_and_image_removed`, `test_kept_images_survive`, `test_disk_floor_pauses_without_breaker`, `test_plugin_change_refuses_run`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): ledger, typed retries, breaker, quota and scheduler`.

### Task 8: Local grading and oracle preflight

**Files:** Create `bench/grade.py`, `bench/requirements-grade.txt` (`swebench==5.0.2`), `bench/data/routes.json` (written in Task 13); test `tests/bench/test_grade.py`.

**Spike first (ruling M5-N3; Docker and network, no quota; before any agent run):** grade the gold patch of one django and one sphinx Mini task on their arm64 images with swebench 5.0.2 via `write_arm64_dataset`. If that fails, pin the swebench release whose eval scripts match the images (the arm64 images date from 2025-04: try the 4.x line, newest first, in a separate venv) and retry; if that fails too, grade with the `swebench/sweb.eval.x86_64.*` images under Docker amd64 emulation (grading only; the agent still runs on arm64). The chosen `GradeMode` and the swebench version go into `bench/data/grade_mode.json` and `MEMORY.md` before Task 13.

**Interfaces (produces):**
- `GradeMode = Literal["swebench5-arm64", "swebench-pinned-arm64", "amd64-emulation"]`; `load_grade_mode(path: Path = GRADE_MODE_FILE) -> tuple[GradeMode, str]` (mode, swebench version); `requirements-grade.txt` follows the chosen version.
- `write_arm64_dataset(rows: Iterable[Mapping[str, Any]], path: Path) -> Path`: Verified rows with `image` rewritten to `arm64_image(instance_id)` (swebench 5 takes each instance's image from the row's `image` field and accepts a local dataset path).
- `class Grader(Protocol): def grade(self, preds: Path, run_id: str, instance_ids: Sequence[str]) -> dict[str, bool | None]`.
- `LocalSwebench(venv: Path, dataset: Path, report_dir: Path, mode: GradeMode, workers: int = 1, timeout_s: int = 1800)`: in `amd64-emulation` the dataset keeps the x86_64 `image` values and the run sets `DOCKER_DEFAULT_PLATFORM=linux/amd64`; for 5.x runs `<venv>/bin/swebench eval <dataset> -p <preds.jsonl> --run-id <id> -i <id1> -i <id2> … -j 1 -t 1800 --report-dir <dir>` (console script; repeatable `-i`; no `--task-repo`); for a pinned 4.x, `python -m swebench.harness.run_evaluation --dataset_name <dataset> --predictions_path … --run_id … --instance_ids … --max_workers 1 --namespace swebench` (flags verified against the pinned wheel in the spike); always holding `bench.lock`; parses the run report; `None` for an instance with a harness error. `FakeGrader()`.
- `write_predictions(rows: Iterable[tuple[str, str, str]], path: Path) -> None`; scanned with `scan_for_token` before writing.
- `grade_task(ledger, grader, *, stage, instance_id) -> None` (called by the scheduler after a task's block, before its image is removed) and `grade_stage(ledger, grader, *, stage) -> Path` (catch-up and regrade) → `grades.jsonl`; empty patch → unresolved without grading; a fresh `run_id` per batch (`<campaign>-<stage>-<arm>-s<seed>-b<n>`); idempotent.
- `preflight(tasks, grader) -> dict[str, Literal["ok", "excluded"]]`: gold patch graded twice; `ok` only if resolved both times; else `excluded` with reason (`build`, `gold_fail`, `flaky_gold`, `harness_error`).

- [ ] **Step 0:** Run the spike above; record `grade_mode.json`; commit `chore(bench): grading mode from the arm64 spike`.
- [ ] **Step 1:** Failing tests `test_grade_mode_selects_command` (one per mode), `test_amd64_mode_sets_platform_and_keeps_x86_images`, `test_grade_task_before_image_removal`, `test_arm64_dataset_rewrites_image`, `test_swebench_argv_v5` (console script, `--run-id`, one `-i` per id, no `-r`, no `--namespace`), `test_empty_patch_unresolved_without_grading`, `test_run_ids_unique_per_batch`, `test_regrade_is_idempotent`, `test_preflight_requires_two_gold_passes`, `test_predictions_leak_scanned`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): local arm64 grading with the official harness and oracle preflight`.

### Task 9: Statistics and power

**Files:** Create `bench/stats.py`, `bench/power.py`; modify `token_sparen/learn/beta.py` (public alias `beta_quantile = _beta_quantile`); test `tests/bench/test_stats.py`, `tests/bench/test_power.py`.

**Interfaces (produces):**
- `Estimate(point: float, lo: float, hi: float, n: int, method: str)`.
- `bca(stat: Callable[[Sequence[int]], float], n: int, *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]` (resampling of cluster indices, jackknife acceleration, bias correction; `statistics.NormalDist` for quantiles).
- `median_cost_ratio(arm, base, *, n_boot=10000, alpha=0.05, seed=0) -> Estimate` (tasks paired by id; per-task cost = mean of `run_cost` over seeds; tasks missing a cost on either side dropped and counted; BCa).
- `resolved_diff(arm, base, *, n_boot=10000, alpha=0.05, seed=0) -> Estimate` (per-task mean over seeds; difference of means; task-cluster BCa).
- `mcnemar_conditional_exact(n10: int, n01: int, n: int, *, alpha=0.05) -> Estimate` (Clopper–Pearson on n10/(n10+n01) via `beta_quantile`, mapped to d = (m/n)(2p − 1); method `"mcnemar-conditional-exact"`); `clopper_pearson(k, n, alpha=0.05)`; `non_inferior(e, margin=-0.05) -> bool` (`e.lo > margin`).
- `summarise(ledger, grades, *, stage, reference="baseline") -> Summary` (per arm and per repo; exclusions; counts).
- `bench/power.py`: `flip_model(screen: Summary) -> FlipModel(f: float, q: float, n: int)` with q = discordant share of token-sparen vs baseline pairs in screening, f = min(1, 2q) the share of "flaky" tasks (p = 0.5), plus the 80 % upper confidence bound of q (Clopper–Pearson) used for planning (conservative). `power(f: float, n_tasks: int, seeds: int, *, margin: float = -0.05, alpha: float = 0.05, n_sim: int = 2000, seed: int = 0) -> float` by simulation of the gate (true difference 0; flaky tasks p = 0.5 in both arms independently, others deterministic; BCa with `n_boot = 1000`); `required_size(f, *, pool: int, max_seeds: int = 3, target: float = 0.8) -> tuple[int, int] | None` = the (tasks, seeds) with the fewest runs reaching `target`, ties → more tasks; None if no size within the pool reaches it.

- [ ] **Step 1:** Failing tests `test_clopper_pearson_known_values` (`(5, 10)` → `(0.1871, 0.8129)` ± 1e-4), `test_identical_arms_ratio_one`, `test_bca_reproducible_and_covers_normal_mean` (coverage 0.93–0.97 over 400 synthetic draws, n = 50), `test_mcnemar_symmetric_when_balanced`, `test_noninferior_boundary`, `test_cluster_bootstrap_uses_tasks_not_runs`, `test_power_monotone_in_size`, `test_power_matches_normal_approx` (f = 0.3, s = 3: required tasks within ± 15 % of 0.5·f/(s·(0.05/2.80)²) ≈ 157), `test_required_size_none_when_pool_too_small`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): BCa statistics, exact non-inferiority and power`.

### Task 10: Stages, output-style rule and release gate

**Files:** Create `bench/stages.py`, `bench/gate.py`; test `tests/bench/test_stages.py`, `tests/bench/test_gate.py`.

**Interfaces (produces):**
- `StageSpec(name: str, arms: tuple[str, ...], optional_arms: tuple[str, ...], seeds: tuple[int, ...], model: str, tasks: tuple[str, ...])`. `SCREENING`: arms `baseline, concise, token-sparen, ts-style, ts-cold, rtk, headroom, tokeneconomizer`; optional `caveman, context-mode`; seeds `(0,)`; model `"sonnet"`; tasks = Mini minus exclusions.
- `confirmation_spec(screen: Summary, *, pool: Sequence[Task], quota: QuotaProjection, model: str) -> tuple[StageSpec, PowerVerdict]`: size from `required_size` (planning f from the conservative q); tasks = Mini ∪ `extend(...)` up to the size; arms `baseline, token-sparen`, then, while the projected weekly quota for the whole stage fits 6 weeks at the 60 % stop, add the best competitor (lowest screening median cost ratio among competitors with resolved-diff point ≥ −0.05; ties by name), then `ts-style` if its screening rule held (resolved-diff point vs `token-sparen` ≥ −0.05 and cost ratio vs `token-sparen` < 1). `PowerVerdict = Literal["powered", "underpowered"]`: underpowered when no size in the pool reaches 80 % or the two core arms alone do not fit 6 weeks.
- `QuotaProjection(pct_per_run_weekly: float, runs_per_week: float)` and `DiskProjection(gb_per_image: float, gb_kept: float, free_gb: float)` from Task 13 calibration; `fits(spec, proj, disk, weeks=6) -> bool` also requires `free_gb - gb_kept - gb_per_image - workspace growth ≥ 30` (Mini images kept plus one live image; ruling M5-N2).
- `style_rule(summary: Summary, *, confirmatory: bool) -> Literal["recommend_opt_in", "keep_off", "not_run"]`: recommend only from confirmation, iff `ts-style` is non-inferior vs `baseline` and vs `token-sparen` and its cost-ratio upper bound vs `token-sparen` < 1; screening-only evidence → `keep_off` (descriptive numbers still published). Defaults never change.
- `gate(confirm: Summary, *, verdict: PowerVerdict, release_hash: str, e2_ok: bool) -> GateResult(verdict: Literal["passed", "failed", "underpowered"], reasons: list[str])`: `underpowered` when the verdict says so (owner decides; documented); otherwise `passed` iff token-sparen vs baseline resolved-diff lower bound > −0.05, every run's `plugin_hash` = `release_hash`, every token-sparen run live, paired tasks ≥ the planned size minus 5 %, one model id and one Claude Code version, `e2_ok`. Cost is reported, never gated.

- [ ] **Step 1:** Failing tests `test_confirmation_core_arms_then_quota_extras`, `test_size_from_power`, `test_underpowered_when_pool_or_quota_short`, `test_disk_projection_in_sizing`, `test_style_needs_confirmation_evidence`, `test_gate_passes_on_noninferior_valid_run`, `test_gate_fails_on_hash_mismatch`, `test_gate_fails_on_mixed_model_or_version`, `test_gate_underpowered_is_not_failed`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): stages, power-sized confirmation and release gate`.

### Task 11: Public report and `e4.json` (E4 only)

**Files:** Create `bench/report.py`, `bench/svg.py`; test `tests/bench/test_report.py`.

**Interfaces (produces):**
- `build_report(ledger: Path, grades: Path, *, stage: str, out_dir: Path, gate: GateResult | None, power: dict | None) -> Path` writes `e4.json` (valid per `validate_e4`), `report.md` and `figures/{cost_ratio,resolved_diff,pareto}.svg`. `report.md` opens with the claims block (every field of the Claims policy, pins, exclusions per repo, the power verdict, the network-access and TokenEconomizer-headless disclosures, "descriptive" labels) and prints every number with its CI and n; per-repo tables.
- `assert_e4_source(ledger: Path) -> None` raises `NotE4Error` unless the first line is the E4 ledger header.
- `scan_private(text: str, *, home: Path, deny: Sequence[str], token: str | None) -> list[str]` (home paths, `/Users/`, `C:\Users\`, `TOKEN_SPAREN_DENYLIST` words, email addresses, token pattern); `build_report` refuses to write on any hit.
- `publish(report_dir: Path, repo: Path) -> list[Path]` → `docs/evidence/e4.json` (confirmation only; M6 reads this path) and `docs/benchmarks/<campaign>-<stage>/{report.md,figures/*.svg,e4.json}`; refused unless `validate_e4` is clean and the stage is completely graded.
- `svg.forest(rows, *, ref: float, title: str) -> str`, `svg.scatter(points, *, title: str) -> str` (text labels, colour-blind-safe two-tone palette, `prefers-color-scheme` inside the SVG).

- [ ] **Step 1:** Failing tests `test_report_refuses_non_e4_input`, `test_report_has_no_private_paths_or_token`, `test_every_number_has_ci_and_n`, `test_e4_json_validates_and_has_confirmatory_diff`, `test_publish_writes_docs_evidence_only_for_confirmation`, `test_svg_well_formed`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): public E4 report and e4.json`.

### Task 12: CLI, dry run end to end, CI job and docs

**Files:** Create `bench/__main__.py`, `bench/cli.py`, `bench/README.md`, `docs/reports/m5-method.md`; modify `.github/workflows/ci.yml` (job `bench-dry-run`; `mypy --strict bench` in `lint`), `.claude/agents/bench-runner.md`, `.claude/skills/run-evals/SKILL.md`, `docs/specs/2026-11-25-token-sparen-design.md` (§10.4 own runner, pool, local grading; §10.5 figures only for E4, BCa, conditional exact, power step, `underpowered`; plugin-hash definition; revision-log entry), `CLAUDE.md`, `AGENTS.md`, `MEMORY.md` (design decisions and owner answers, dated); test `tests/bench/test_cli.py`, `tests/test_docs.py`.

**Interfaces (produces):** `python -m bench <cmd>`, `main(argv: list[str] | None = None) -> int`: `prepare toolchain|templates|images|dataset`, `preflight`, `freeze --stage S --ref TAG`, `calibrate`, `run --stage screening|confirmation --campaign ID [--arms a,b] [--wait] [--dry-run]`, `quota set --five-hour P --weekly P`, `invalidate --key ID | --since ISO --reason TEXT`, `status` (counts per arm/status, next key, last quota reading, breaker state; no secrets), `grade --stage S`, `analyse --stage S`, `power` (writes the confirmation spec + verdict), `gate --release-hash H`, `report --stage S [--publish]`; all take `--out DIR`. `--dry-run`: `FakeBackend`, `FakeGrader`, `FAKE_TASKS`, a frozen copy of the working tree without tag checks, scripted quota, `n_boot = 200`, `n_sim = 50`.

- [ ] **Step 1:** Failing tests `test_dry_run_end_to_end_needs_no_quota` (`docker`/`claude`/`swebench` subprocesses patched to raise; run → grade → analyse → power → gate → report succeed in tmp), `test_dry_run_resume_is_idempotent`, `test_status_prints_no_secret`, `test_real_run_refuses_untagged_freeze`, `test_m5_method_doc_has_no_private_figures` (no digits except dates, section numbers and the design constants 50/25/3/5/60/80/85/3600/−5).
- [ ] **Step 2–4:** fail → implement → battery; CI job runs `python -m bench run --dry-run --campaign ci --out "$RUNNER_TEMP/bench"` then `grade`, `analyse`, `power`, `report`.
- [ ] **Step 5:** Commit `feat(bench): CLI, dry-run pipeline and M5 method docs`.

### Task 13: Prepare and calibrate (controller-run, owner-authorised)

Owner step first: `claude setup-token`; the token goes in a 0600 file outside the repo and is exported as `CLAUDE_CODE_OAUTH_TOKEN` only in the bench shell. Workspace in the owner's private folder (outside any git tree).

- [ ] **Step 1:** `prepare toolchain`: in an `ubuntu:22.04` arm64 container build `toolchain/`: Claude Code native binary at the pinned version, Python 3.12 via `UV_PYTHON_INSTALL_DIR=/opt/bench/python uv python install 3.12` plus symlink `/opt/bench/bin/python3.12`, Node 22, `rtk-oss` aarch64, a `headroom-ai[all]` venv, a TokenEconomizer clone at a pinned commit → `pins.json` (versions, SHAs, sha256).
- [ ] **Step 2:** `prepare templates`: per arm, the documented default install with `HOME=<template>` and `CLAUDE_CONFIG_DIR=<template>/.claude` inside the toolchain container; MCP servers that a plugin or wrapper registers (context-mode, headroom's retrieve tool) are copied verbatim into `<arm>.json` with `${CLAUDE_PLUGIN_ROOT}` resolved to the install path (equivalent to the default under `--strict-mcp-config`; recorded). Assert the real `~/.claude` and `~/.claude.json` unchanged.
- [ ] **Step 3:** `prepare dataset`, `prepare images` (Mini first; the rest of the pool is pulled lazily per stream), `preflight` → `bench/data/routes.json`; commit `chore(bench): oracle preflight routes for the arm64 pool`.
- [ ] **Step 4:** Smoke on the fake task, then one real django task per arm on `sonnet`: `verify_load` and liveness must pass; check `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` keeps every arm working (TokenEconomizer's side calls included; if a documented default breaks under the scrub, record it, never re-enable the token in subprocesses); check headroom under OAuth and caveman's documented activation under `-p`. An arm that cannot run in its documented default → `infeasible` in `MEMORY.md`, dropped.
- [ ] **Step 5:** `calibrate`: from the smoke and first screening runs, weekly % per run and runs per window → `QuotaProjection`; image sizes and free disk → `DiskProjection`; `MEMORY.md` entry (pins, exclusions, infeasible arms; no private figures); commit `docs: M5 preparation record`.

### Task 14: Screening and power step (controller-run)

- [ ] **Step 1:** Bump the version to `1.0.0` in `.claude-plugin/` and `token_sparen/__init__.py` first (the hash covers them), then tag `v1.0.0-rc1`; `freeze --stage screening --ref v1.0.0-rc1`; `run --stage screening --campaign m5 --wait` across windows (exit 3 → ask the owner); optional arms only if `calibrate` projects they fit.
- [ ] **Step 2:** `grade`, `analyse`, `report` (private); optional `report --publish` labelled screening (`docs/benchmarks/m5-screening/` only).
- [ ] **Step 3:** `power` → confirmation spec and verdict; show the owner the projected weeks and quota before Task 15; record the spec and verdict in `MEMORY.md` (rule outputs only).

### Task 15: Confirmation, release gate and publication (controller-run)

- [ ] **Step 1:** Runtime freeze starts (M6 constraint): `freeze --stage confirmation --ref <RC tag>`; `run --stage confirmation --campaign m5 --wait` with the power-sized pool.
- [ ] **Step 2:** E2 run; `grade`, `analyse`, `gate --release-hash <plugin_hash of the RC>`. `failed` → no v1.0 release, `MEMORY.md` + method doc, new plan; `underpowered` → documented, owner decides.
- [ ] **Step 3:** `style_rule` recorded; README opt-in recommendation only on `recommend_opt_in`.
- [ ] **Step 4:** `report --publish` (writes `docs/evidence/e4.json`); full battery; commit `docs(bench): M5 confirmation report (plugin <hash12>)`.

### Task 16: M5 review, drift report, token revocation and tag

**Files:** Create `docs/reports/drift-m5.md`; modify `README.md` (Benchmarks link), `CHANGELOG.md`, `MEMORY.md`, `CLAUDE.md`.

- [ ] **Step 1:** Goldfish cold review of the M5 diff against spec §10.4, §10.5, §6.6; fix wave.
- [ ] **Step 2:** Drift check → `docs/reports/drift-m5.md`.
- [ ] **Step 3:** Owner revokes the benchmark OAuth token (claude.ai settings); workspace token file deleted; recorded in `MEMORY.md` (no value).
- [ ] **Step 4:** Commit `docs: M5 readme, changelog and drift report`; push; tag `v0.5.0-m5` after CI is green.

---

## Revision 1 (2026-11-27, after the cold review and rulings)

- **Owner answers applied**: OAuth token only, local grading only (sb-cli removed; ungradable tasks dropped and listed); weekly stop 60 %; TokenEconomizer as documented (headless limitation disclosed); screening `sonnet`, effort high.
- **B1 / M5-B1**: new `bench/power.py` and power step (Task 14); extended arm64 Verified pool with a seeded stratified extension; confirmation = baseline + token-sparen (+ competitor, + `ts-style` only if quota allows); `underpowered` verdict; BCa instead of percentile.
- **B2 / M5-B2**: typed statuses `ok` / `agent_fail` / `infra_fail{auth,api,rate,docker,proxy,load,leak,unknown}` with a mapping table; infra failures are never graded; breaker (3 consecutive), per-window cap (default 4), `invalidate` records.
- **B3 / M5-B3, M12, M6-B1**: frozen read-only plugin from a tag, hash re-verified before every run; `scripts/plugin_hash.py` per the coordinator's binding definition (sha256 over sorted (path, sha256(bytes)) of `.claude-plugin/`, `hooks/`, `skills/`, `token_sparen/`; docs, tests, top-level texts and VCS metadata excluded; not a git tree hash; the version is bumped to 1.0.0 before the RC is frozen, and M6 keeps the runtime freeze) and the `e4.json` schema moved to Task 3 as published interfaces; publish path `docs/evidence/e4.json`.
- **M1**: swebench 5.0.2 console script, `--run-id`, repeatable `-i`, arm64 images through a dataset override of the `image` field; arm64 tag naming `_1776_` verified on Docker Hub.
- **M2**: the agent runs in the same arm64 image it is graded in; tasks without one are not in the pool.
- **M3**: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, name-only passing, token leak scan, token revocation (Task 16).
- **M4, M5**: named and labelled containers, reaper, `docker kill` on timeout, state copied in and out and promoted only on graded outcomes; 4 GB / 4 CPU caps, one container at a time, disk preflight and prune policy.
- **M6–M9**: per-arm state semantics with auto-memory disabled; plugin MCP servers copied verbatim under strict MCP; liveness from `--include-hook-events`; cost = main session + side calls.
- **M10 / Task 1 ruling**: 3.15→3.11 search, `py` launcher on Windows, `uv python find` fallback (no downloads), cached probe, measured re-exec overhead, doctor mirrors the launcher, no env leak, log line when nothing is found.
- **M11**: rate-limit kind from the event, 60 % weekly stop, window cap, quota projection shown to the owner before confirmation.
- **Minors**: rulings path fixed; `--verbose`/`--max-turns` pinned and tested; "conditional exact" label; gold preflight twice; gate size tied to the power plan; per-repo reporting and stratified extension; confirmatory vs descriptive labels; `concise` uses the directive text; §6.6 spec amended in Task 2; uv Python path; state after timeouts kept and after infra failures discarded; `~/.claude.json` isolation and container-local HOME; network access disclosed; headroom OAuth and caveman activation settled in Task 13. Not adopted: egress restriction (tools need network; disclosed instead).

## Revision 2 (2026-11-27, re-review rulings M5-N1..N3)

- **M5-N1**: the agent timeout runs inside the container (coreutils `timeout`), patch and learner state are always written and a timed-out run is graded with its patch; host `docker kill` only as a backstop at +120 s (`infra_fail/docker`).
- **M5-N2**: disk enters the sizing step (`DiskProjection`); each task's arms and seeds run back to back, the task is graded, then its image is removed unless a later stage needs it; a 30 GB free-disk floor pauses the scheduler (`infra_fail/disk`, exit 10, outside the breaker).
- **M5-N3**: Task 8 starts with a grading spike (gold patches, one django and one sphinx arm64 image): swebench 5.0.2, else a pinned release matching the images, else x86 images under amd64 emulation for grading only; the mode is recorded before any agent run.
