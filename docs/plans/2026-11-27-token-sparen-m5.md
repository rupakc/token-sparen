# token-sparen M5 — Live benchmark E4 (screening → confirmation) and release gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure token-sparen live in Claude Code on SWE-bench Verified Mini against plain Claude Code, a concise-prompt control and five competitors under a Claude Max subscription, with a quota-aware resumable scheduler, the official grader and the §10.5 statistics; decide the output-style point (§6.6) and the v1.0 release gate from that evidence; publish a reproducible public report.

**Architecture:** A dev-time package `bench/` (never imported by `token_sparen/`, not in the wheel) runs each (arm, seed, task) as `claude -p … --output-format stream-json` inside the task's arm64 SWE-bench container, with a read-only toolchain mount and one persisted, snapshotted HOME per (stage, arm, seed, repo) stream, so learners accumulate state in commit order. An append-only ledger makes the scheduler idempotent across 5-hour windows; patches are graded by the official `swebench` harness locally (sb-cli fallback per task); `bench/stats.py`, `bench/stages.py` and `bench/report.py` turn the ledger into CIs, stage decisions, the release gate and a public report. A fake backend, fake task and fake grader run the whole pipeline in CI with no Docker, network or quota.

**Tech Stack:** Python ≥ 3.11 stdlib only for `token_sparen/` and `bench/` (`subprocess` drives `docker`, `claude`, `swebench`, `sb-cli`); the grader lives in its own venv (`bench/requirements-grade.txt`, `swebench==5.0.2`, `sb-cli==0.1.5`); figures are hand-written SVG (no plotting library). pytest, ruff, mypy for dev.

**Spec:** `docs/specs/2026-11-25-token-sparen-design.md` (§10.4, §10.5, §6.6, §6.7, §14 M5). Previous plan: `docs/plans/2026-11-27-token-sparen-m4.md`. Plan rulings: `.superpowers/sdd/2026-11-27-token-sparen-m5/progress.md`.

**Design decisions taken here (record in `MEMORY.md` and the spec revision log, Task 11):**
- **Own runner, not Harbor** (spec's named fallback). Harbor 0.27's `claude-code` agent accepts no `--plugin-dir` or arbitrary flags, forces `CLAUDE_CONFIG_DIR` into its log dir, uses amd64-only task images (emulated on arm64) capped at 1 CPU / 4 GB, and has no notion of per-repo learner streams or quota windows. Harbor stays the candidate for the optional E5 (Terminal-Bench).
- **Task set**: SWE-bench Verified Mini (HF `MariusHobbhahn/swe-bench-verified-mini`, 50 tasks: 25 django, 25 sphinx) pinned by dataset revision → exactly two learner streams of 25 tasks in `created_at` order.
- **Images**: prebuilt `swebench/sweb.eval.arm64.<id>` images where Docker Hub has them, otherwise built locally by the official harness on arm64; a task whose gold patch does not resolve locally is graded by `sb-cli`; a task that fails both is excluded **before** any agent run (decided from the oracle only, recorded with reason).
- **Output style (§6.6)** ships off by default in v1.0 whatever E4 shows; a passing rule (Task 9) lets the README recommend the opt-in with its published numbers. Flipping the default needs a new confirmation on the new plugin hash (future).
- **Cold-start ablation** (`ts-cold`) is a screening arm only; confirmation has baseline, token-sparen, the best two competitors, plus `ts-style` if it passes the screening rule.

## Global Constraints

- Everything in the M0–M4 global constraints still binds (CLAUDE.md "Invariants" and "Never commit private data").
- `token_sparen/` never imports `bench`; `bench/` may import `token_sparen` (stdlib-only) and is excluded from the wheel (`python scripts/check_wheel.py` asserts no `bench/` file in it). Every committed `bench/` file stays < 256 KiB.
- **Never touch the real Claude config**: every agent and install process runs with `CLAUDE_CONFIG_DIR` pointing into the bench output dir; `bench.isolation.assert_isolated` refuses `~/.claude`, anything under it, the caller's own `CLAUDE_CONFIG_DIR`, and any output dir inside a git work tree. `ANTHROPIC_API_KEY` is never passed to an agent.
- **Credentials**: the OAuth token (`claude setup-token`, owner step) and `SWEBENCH_API_KEY` are passed to child processes **by name only** (`docker run -e CLAUDE_CODE_OAUTH_TOKEN`), never in argv, ledger, logs or reports.
- **Bench output** (`--out`, env `TOKEN_SPAREN_BENCH_OUT`; no default) lives outside any git work tree, written via `token_sparen.replay.report.write_private` / `check_out_path` (0600/0700). Only `bench report --publish` writes into the repo (`docs/benchmarks/<campaign>/`).
- **Figures are allowed only for E4 public benchmark results** (spec §10.5, amended in Task 11). E1/E3 data never feeds `bench/report.py`; it refuses any input that is not an E4 ledger.
- **Claims policy (§10.5)**: every published number carries its 95 % CI, n (tasks), seeds, model id, Claude Code version, date range, released plugin hash (git tree hash of the plugin files at the tested commit) and harness commit. Cost: paired bootstrap 95 % CI of the median per-task cost ratio (`total_cost_usd`, list-price, cache-aware) with all token classes; quality: non-inferiority on resolved rate at margin −5 pp (lower bound of the two-sided 95 % CI > −0.05), exact McNemar-based CI published alongside. Quota % is secondary.
- **Fixed per campaign stage** (recorded, identical across arms): Claude Code binary version (pinned, `DISABLE_AUTOUPDATER=1`), `--model`, `--effort high`, `--max-turns 80`, wall timeout 3600 s, prompt template `bench/prompt.md`, `--permission-mode bypassPermissions` with `IS_SANDBOX=1` inside the container, `--setting-sources user`, `--strict-mcp-config --mcp-config <arm>.json`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DO_NOT_TRACK=1`.
- **Stages**: screening = 1 seed on a cheaper model (`sonnet` alias, resolved id recorded); confirmation = 3 seeds on Claude Code's default model at run time (resolved id recorded, then pinned for the stage). Competitors in their documented default configuration at a pinned version/commit recorded in `pins.json`.
- **Quota**: the scheduler never starts a run after a `rate_limit_event` with status `allowed_warning` or `rejected` until its `resetsAt`; a window whose reset lies more than 5 h ahead is treated as weekly → stop and ask the owner (exit 3), never sleep through it. Owner readings from `/usage` may be entered with `bench quota set`.
- No real-quota run before Task 12; tests and CI use the dry-run backend only.

## Review Focus

1. **Silently inert arm**: the plugin is listed in `system/init` but its hooks never ran (hook `python3` < 3.11 inside the container, a plugin MCP server dropped by `--strict-mcp-config`, a wrapper proxy not on the request path) → the run is `load_failed`, retried, never graded; three in a row stop the campaign. → Task 1 `test_launcher_reexecs_newer_python`, Task 4 `test_liveness_required_per_arm`.
2. **Tool artefacts in the graded patch** (`.token-sparen/`, `.context-mode/`, `RTK.md`, a tool-written `CLAUDE.md`, `.claude/`) → excluded from the patch; the agent's own new source or test files are kept. → Task 5 `test_patch_excludes_tool_artefacts_keeps_new_files`.
3. **Quota exhaustion mid-run counted as an agent failure** (biases against whichever arm runs late in a window) → `quota_aborted`, learner state restored from the pre-run snapshot, re-queued, not an attempt. → Task 6 `test_quota_abort_restores_and_requeues`.
4. **Resume after a crash mid-stream** double-applies learner state or skips a task in commit order → a `started` key without an end restores the snapshot and reruns; task k+1 of a stream never starts before k is final. → Task 6 `test_resume_after_crash_restores_snapshot`, `test_stream_order_respected`.
5. **Private data in published artefacts** (token in argv, absolute host paths from `cwd`, account details from `apiKeySource`/init) → argv carries no secret; report scan refuses home paths and denylisted words. → Task 5 `test_token_never_in_argv`, Task 10 `test_report_has_no_private_paths`.

---

### Task 1: Hook launcher finds a Python ≥ 3.11

`hooks/run.py` runs under whatever `python3` is on PATH; under < 3.11 the import fails and every hook silently no-ops (macOS ships 3.9; SWE-bench testbeds put old Pythons first). Product fix, benefits users too.

**Files:** Modify `hooks/run.py`, `README.md` ("Requirements"), `CHANGELOG.md`. Test `tests/test_hook_launcher.py`.

**Interfaces (produces):** `hooks/run.py` stays parseable by Python 3.6 (no walrus, no `from __future__ import annotations`, no PEP 604). If `sys.version_info < (3, 11)`: candidates in order `$TOKEN_SPAREN_PYTHON`, `python3.13`, `python3.12`, `python3.11` via `shutil.which`; the first that exists and is not the current interpreter gets `os.execv(cand, [cand, "-I", __file__, *sys.argv[1:]])` with env `TOKEN_SPAREN_REEXEC=1` (a second hop is refused: no loops); none → print `{}` and exit 0 (fail-open unchanged). `TOKEN_SPAREN_PYTHON` is documented in README.

- [ ] **Step 1:** Failing tests: `test_launcher_reexecs_newer_python` (fake old interpreter = a stub script reporting 3.9 that runs the launcher logic via `runpy` with `sys.version_info` patched; asserts `os.execv` called with `$TOKEN_SPAREN_PYTHON`), `test_launcher_no_candidate_prints_noop`, `test_launcher_never_loops` (`TOKEN_SPAREN_REEXEC=1` → `{}`), `test_launcher_source_parses_as_py36` (`ast.parse(src, feature_version=(3, 6))`).
- [ ] **Step 2:** Run `pytest tests/test_hook_launcher.py -v` → FAIL.
- [ ] **Step 3:** Implement. **Step 4:** full battery (CLAUDE.md "Commands") → PASS; hook p95 perf unchanged.
- [ ] **Step 5:** Commit `fix(hooks): re-exec under Python >= 3.11 when python3 is older`.

### Task 2: Output-style directive (§6.6), off by default

**Files:** Create `token_sparen/mechanisms/outputstyle.py`; modify `token_sparen/adapters/claude/entry.py` (register on `session_start` after `_on_facts`), `token_sparen/params/defaults.toml`, `token_sparen/config.py` (`PROJECT_POLICY`), `README.md` (mechanism list, opt-in), `CHANGELOG.md`. Test `tests/mechanisms/test_outputstyle.py`, `tests/adapters/claude/test_entry.py`.

**Interfaces (produces):**
- `[output_style] enabled = false`, `directive = "concise"`; `DIRECTIVES: dict[str, str] = {"concise": "Be concise in prose. Never shorten code, commands, file paths, error messages or quoted tool output."}` (fixed set; an unknown name → off, logged once).
- `on_session_start(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action`: `additional_context` = the directive, labelled `[token-sparen output style]`, on sources `startup`, `clear`, `compact` (the earlier copy is gone) and not on `resume`; at most once per context epoch (session store flag, as `facts.inject`). Static (no decision point, no learning; tier high).
- `PROJECT_POLICY`: `("output_style", "enabled")` → `false_only`; `("output_style", "directive")` → `user_only`.

- [ ] **Step 1:** Failing tests `test_off_by_default_injects_nothing`, `test_on_injects_once_per_context_epoch`, `test_resume_does_not_reinject_compact_does`, `test_project_file_cannot_enable`, `test_unknown_directive_is_off`, `test_disable_env_noop`.
- [ ] **Step 2–4:** fail → implement → full battery.
- [ ] **Step 5:** Commit `feat: output-style directive (off by default, spec 6.6)`.

### Task 3: `bench/` foundations — tasks, arms registry, isolation

**Files:** Create `bench/__init__.py`, `bench/tasks.py`, `bench/arms.py`, `bench/arms.toml`, `bench/isolation.py`, `bench/prompt.md`, `bench/data/verified_mini.jsonl`, `bench/data/fetch_mini.py`, `bench/fixtures/fake/` (tiny git repo `calc.py` with an off-by-one, `test_calc.py`, gold `fix.patch`, `agent.py` fake `claude`); modify `pyproject.toml` (ruff/mypy include `bench`), `scripts/check_wheel.py`. Test `tests/bench/test_tasks.py`, `tests/bench/test_arms.py`, `tests/bench/test_isolation.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class Task: instance_id: str; repo: str; base_commit: str; created_at: str; problem_statement: str; gold_patch: str; image: str` ; `load_tasks(path: Path = TASKS_FILE) -> list[Task]`; `streams(tasks: Sequence[Task]) -> dict[str, list[Task]]` (repo → sorted by `(created_at, instance_id)`); `FAKE_TASKS: list[Task]` (two streams of two tasks over the fake repo). `verified_mini.jsonl` starts with a header row `{"dataset": "MariusHobbhahn/swe-bench-verified-mini", "revision": "<hf sha>", "fetched": "<date>"}`; `fetch_mini.py` (network, dev-time) regenerates it.
- `@dataclass(frozen=True) class Arm: name: str; kind: Literal["baseline", "prompt", "plugin", "hook", "wrapper"]; state: Literal["none", "stream", "cold"]; append_system_prompt: str | None; plugin_dirs: tuple[str, ...]; install: tuple[str, ...]; wrapper: tuple[str, ...]; env: Mapping[str, str]; ts_config: Mapping[str, Any]; mcp: Mapping[str, Any]; expect_plugins: tuple[str, ...]; expect_mcp: tuple[str, ...]; liveness: str; patch_excludes: tuple[str, ...]` ; `load_arms(path: Path = ARMS_FILE) -> dict[str, Arm]` (unique names, known `liveness` probe, `state = "cold"` only on a learner).
- `arms.toml` entries: `baseline`; `concise` (`append_system_prompt = "Answer concisely."`); `token-sparen` (plugin dir = the tested checkout, `state = "stream"`); `ts-style` (+ `ts_config = {output_style = {enabled = true}}`); `ts-cold` (`state = "cold"`); `rtk` (`rtk-oss`, `rtk init -g`); `headroom` (`wrapper = ["headroom", "wrap", "claude", "--"]`); `caveman` (plugin `caveman@caveman` only, no CLI/proxy); `context-mode` (plugin `context-mode@context-mode`); `tokeneconomizer` (plugin from a pinned clone, `state = "stream"`). Common `patch_excludes`: `.token-sparen/`, `.claude/`, `CLAUDE.md`, `RTK.md`, `.rtk/`, `.context-mode/`, `.tokeneconomizer/`, `.headroom/` — applied only to paths untracked at `base_commit` (edits to a tracked file of that name stay in the patch).
- `assert_isolated(config_dir: Path, *, real_home: Path, environ: Mapping[str, str]) -> None` raises `IsolationError`; `check_out_dir(out: Path) -> Path` (delegates to `replay.report.check_out_path`).

- [ ] **Step 1:** Failing tests `test_streams_in_commit_order`, `test_mini_has_two_streams_of_25` (reads the committed jsonl), `test_tasks_file_under_256k`, `test_arms_registry_validates`, `test_cold_only_on_learners`, `test_refuses_real_claude_dir` (incl. symlink into it), `test_refuses_out_in_git_tree`, `test_wheel_has_no_bench` (via `check_wheel` helper on a built file list).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): tasks, arms registry and config isolation`.

### Task 4: Stream-json metrics, overhead and load/liveness verification

**Files:** Create `bench/streamjson.py`, `bench/liveness.py`; test `tests/bench/test_streamjson.py`, `tests/bench/test_liveness.py`, fixtures `tests/bench/fixtures/*.jsonl` (synthetic).

**Interfaces (produces):**
- `@dataclass class Tokens: input: int; output: int; cache_creation: int; cache_read: int` (`__add__`).
- `@dataclass class RateLimit: status: str; resets_at: float | None; utilization: float | None`.
- `@dataclass class RunMetrics: session_id: str | None; model: str | None; claude_code_version: str | None; plugins: tuple[str, ...]; plugin_errors: tuple[str, ...]; mcp_servers: dict[str, str]; subtype: str | None; is_error: bool; total_cost_usd: float | None; tokens: Tokens; model_usage: dict[str, Tokens]; num_turns: int | None; duration_ms: int | None; rate_limits: list[RateLimit]; api_retries: int; quota_rejected: bool; malformed_lines: int; overhead: Tokens; overhead_cost_usd: float | None`.
- `parse_stream(lines: Iterable[str]) -> RunMetrics` (never raises; `quota_rejected` = a `rate_limit_event` with status `rejected`, or a result/error text matching `hit your (session|weekly|.*) limit`).
- `overhead_from_config_dir(config_dir: Path, main_session: str, since: float) -> tuple[Tokens, float | None]` — sums assistant `usage` of every other session transcript under `config_dir/projects/` written after `since` (plugins' own `claude -p` side calls, e.g. TokenEconomizer's judge), priced with `token_sparen.estimate.Prices` for the cost.
- `verify_load(m: RunMetrics, arm: Arm) -> list[str]` (missing expected plugin, any `plugin_errors`, expected MCP server not `connected`).
- `LIVENESS: dict[str, Callable[[Path, Path], bool]]` keyed by `Arm.liveness`, args `(stream_home, run_dir)`: `none`; `token_sparen` (a session file for this run under `TOKEN_SPAREN_HOME` and no new `errors.jsonl` import errors); `rtk` (a PreToolUse rewrite visible in the transcript: a Bash call whose command starts with `rtk `); `headroom` (proxy log line under `~/.headroom/logs` for this run); `context_mode` (`~/.context-mode/` db touched or a `ctx_` tool call); `tokeneconomizer` (state dir touched); `caveman` (skill listed in init `skills`).

- [ ] **Step 1:** Failing tests `test_parses_cost_and_all_token_classes`, `test_model_usage_per_model`, `test_quota_rejection_detected`, `test_malformed_lines_counted`, `test_overhead_sums_other_sessions_only`, `test_verify_load_flags_plugin_errors_and_missing_mcp`, `test_liveness_required_per_arm` (every `liveness` value in `arms.toml` has a probe; a token-sparen run with no session file is not live).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): stream-json metrics and plugin verification`.

### Task 5: Execution backends (fake and Docker)

**Files:** Create `bench/backends.py`, `bench/state.py`, `bench/patch.py`; test `tests/bench/test_backends.py`, `tests/bench/test_state.py`, `tests/bench/test_patch.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class RunRequest: task: Task; arm: Arm; seed: int; stage: str; model: str; effort: str; max_turns: int; timeout_s: int; stream_home: Path; run_dir: Path`.
- `RunStatus = Literal["ok", "agent_error", "timeout", "quota_aborted", "load_failed", "infra_error"]`; `@dataclass class RunOutcome: status: RunStatus; metrics: RunMetrics | None; patch: str; wall_s: float; problems: list[str]`.
- `class Backend(Protocol): def run(self, req: RunRequest) -> RunOutcome`.
- `build_claude_argv(req: RunRequest, *, mcp_path: str, plugin_dirs: Sequence[str]) -> list[str]` (the Global Constraints flags; prompt from `bench/prompt.md` with the problem statement; wrapper prefix for `kind = "wrapper"`).
- `FakeBackend(fixtures: Path = FAKE_DIR)`: copies the fake repo, runs `fixtures/fake/agent.py` (canned stream-json; applies `fix.patch` for seeds where `(seed + index) % 3 != 0`; scenario switches `quota`, `crash`, `inert`, `timeout` by env) — no Docker, no network.
- `DockerBackend(toolchain: Path, *, memory: str = "6g", cpus: int = 4)`: one `docker run --rm` per run on `task.image` with `--platform linux/arm64`, mounts `toolchain:/opt/bench:ro`, `stream_home:/home/bench`, `run_dir:/run-out`; env by name only; `HOME=/home/bench`, `CLAUDE_CONFIG_DIR=/home/bench/.claude`, `TOKEN_SPAREN_HOME=/home/bench/.token-sparen`, `TOKEN_SPAREN_PYTHON=/opt/bench/python/bin/python3.12`; working dir `/testbed`; stdout → `run_dir/stream.jsonl`; wall timeout kills the container (`timeout`).
- `bench/state.py`: `snapshot(home: Path, dest: Path) -> None`, `restore(snapshot: Path, home: Path) -> None` (exact tree copy, mtimes kept), `reset(home: Path, template: Path) -> None` (cold arm: before every task).
- `bench/patch.py`: `extract_patch(repo: Path | str, base_commit: str, excludes: Sequence[str], *, runner: Callable[[list[str]], str]) -> str` (`git add -A`, unstage excluded paths that are untracked at `<base>`, then `git diff --cached --binary <base>`; run inside the container by DockerBackend, locally by FakeBackend).

- [ ] **Step 1:** Failing tests `test_patch_excludes_tool_artefacts_keeps_new_files`, `test_token_never_in_argv` (argv and `docker` env flags hold names only), `test_argv_pins_flags`, `test_wrapper_prefix_for_headroom`, `test_snapshot_restore_roundtrip`, `test_fake_backend_scenarios` (each scenario → its `RunStatus`), `test_docker_backend_command` (subprocess patched; asserts mounts, platform, no `ANTHROPIC_API_KEY`).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): fake and docker backends, patch extraction, state snapshots`.

### Task 6: Ledger, quota and the resumable scheduler

**Files:** Create `bench/ledger.py`, `bench/quota.py`, `bench/scheduler.py`; test `tests/bench/test_ledger.py`, `tests/bench/test_quota.py`, `tests/bench/test_scheduler.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class RunKey: campaign: str; stage: str; arm: str; seed: int; instance_id: str` (`id` property = `"/".join(...)`).
- `Ledger(path: Path)`: first line `{"kind": "e4-ledger", "schema": 1, "campaign": ..., "harness_commit": ..., "plugin_hash": ..., "pins": {...}}`; `append(rec: dict[str, Any]) -> None` (one JSON line, `flock`, `fsync`); `events(key: RunKey) -> list[dict]`; `final(key) -> dict | None` (last `end` record whose status is final); `quota_readings() -> list[QuotaReading]`. Record kinds: `start`, `end` (status, metrics, patch sha256, wall_s, attempt), `quota`, `pause`.
- Final statuses: `ok`, `agent_error`, `timeout` (graded; empty patch = unresolved); `infra_failed` after 3 `infra_error`/`load_failed` attempts (counted unresolved for quality, excluded from cost pairing, reported). `quota_aborted` is never final and never an attempt.
- `@dataclass(frozen=True) class QuotaReading: at: float; source: Literal["stream", "owner"]; status: str; utilization: float | None; resets_at: float | None; five_hour_pct: float | None = None; weekly_pct: float | None = None`.
- `@dataclass(frozen=True) class QuotaPolicy: five_hour_stop_pct: float = 90.0; weekly_stop_pct: float = 80.0; max_runs_per_window: int | None = None`.
- `decide(readings: Sequence[QuotaReading], policy: QuotaPolicy, now: float, runs_this_window: int) -> QuotaDecision` with `go: bool; reason: Literal["ok", "five_hour", "weekly", "window_budget"]; wait_until: float | None`. A stream reading with status `allowed_warning`/`rejected` blocks until `resets_at`; `resets_at - now > 5 h` → `weekly`. Owner readings apply their thresholds.
- `plan_order(streams: Mapping[str, Sequence[Task]], arms: Sequence[str], seeds: Sequence[int], *, campaign: str, stage: str, shuffle_seed: int) -> list[RunKey]`: for each seed, for position k, for each repo: one block of all arms in a seeded per-block permutation (so arms alternate inside every window).
- `run_campaign(keys: Sequence[RunKey], *, backend: Backend, ledger: Ledger, arms: Mapping[str, Arm], tasks: Mapping[str, Task], out: Path, policy: QuotaPolicy, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, wait: bool = False) -> CampaignStatus` (`complete | paused_five_hour | paused_weekly | stopped_inert | incomplete`). Per run: skip if final; enforce stream order; snapshot `stream_home` → run; `quota_aborted` → restore, record `pause`, stop or wait; `load_failed` → restore and retry; three consecutive `load_failed` for one arm → `stopped_inert`. Stream home = `out/streams/<stage>/<arm>/s<seed>/<repo-slug>/home`; `state = "cold"` resets it from the arm template before every task.
- CLI exit codes (Task 11): complete 0, incomplete 5, paused five-hour 4, paused weekly 3, stopped inert 6.

- [ ] **Step 1:** Failing tests `test_rerun_skips_final_keys` (idempotence: second run makes no backend call), `test_resume_after_crash_restores_snapshot`, `test_stream_order_respected`, `test_quota_abort_restores_and_requeues`, `test_weekly_warning_stops_never_sleeps`, `test_five_hour_waits_until_reset_with_wait`, `test_arms_alternate_within_blocks`, `test_three_load_failures_stop_campaign`, `test_cold_arm_resets_every_task`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): run ledger, quota policy and resumable scheduler`.

### Task 7: Grading and oracle preflight

**Files:** Create `bench/grade.py`, `bench/requirements-grade.txt` (`swebench==5.0.2`, `sb-cli==0.1.5`), `bench/data/routes.json` (written in Task 12); test `tests/bench/test_grade.py`.

**Interfaces (produces):**
- `Route = Literal["local", "sbcli", "excluded"]`; `class Grader(Protocol): def grade(self, preds: Path, run_id: str, instance_ids: Sequence[str]) -> dict[str, bool | None]` (None = harness error).
- `LocalSwebench(python: Path, task_repo: Path | None, workers: int = 2)`: `<python> -m swebench ... eval verified -p preds.jsonl -r <run_id> -i <ids> -j 2 [--task-repo <dir>]` (v5 CLI; arm64 local builds), parses the run's report JSON. `SbCli(python: Path)`: `sb-cli submit swe-bench_verified test --predictions_path … --run_id … --instance_ids …` then `get-report`; needs `SWEBENCH_API_KEY` in env (owner account step), else raises `GraderUnavailable`. `FakeGrader()`: applies the patch to a temp copy of the fake repo and runs its test.
- `write_predictions(rows: Iterable[tuple[str, str, str]], path: Path) -> None` (`instance_id, model_name_or_path = f"{arm}-s{seed}", model_patch`).
- `grade_stage(ledger: Ledger, routes: Mapping[str, Route], graders: Mapping[Route, Grader], *, stage: str) -> Path` writes `grades.jsonl` (key id, resolved, route, grader run_id); empty patch → resolved false without grading; one `run_id` per (stage, arm, seed, attempt batch) — never reused (the harness caches by run_id); idempotent on rerun.
- `preflight(tasks: Sequence[Task], local: Grader, sbcli: Grader | None) -> dict[str, Route]` grades each gold patch: local pass → `local`; else sb-cli pass → `sbcli`; else `excluded` (with reason).

- [ ] **Step 1:** Failing tests `test_empty_patch_unresolved_without_grading`, `test_run_ids_unique_per_batch`, `test_regrade_is_idempotent`, `test_preflight_routes_by_oracle_only`, `test_sbcli_without_key_raises`, `test_fake_grader_resolves_gold_patch`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): official-grader integration and oracle preflight`.

### Task 8: Statistics

**Files:** Create `bench/stats.py`; modify `token_sparen/learn/beta.py` (public alias `beta_quantile = _beta_quantile`); test `tests/bench/test_stats.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class Estimate: point: float; lo: float; hi: float; n: int; method: str`.
- `median_cost_ratio(arm: Mapping[str, Sequence[float]], base: Mapping[str, Sequence[float]], *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0) -> Estimate`: tasks paired by instance id; per-task cost = mean over seeds; ratio per task; median over tasks; percentile bootstrap resampling tasks. Tasks with a missing cost on either side are dropped and counted (`n`).
- `resolved_diff(arm: Mapping[str, Sequence[int]], base: Mapping[str, Sequence[int]], *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0) -> Estimate`: per-task mean over seeds, difference of means, task-cluster percentile bootstrap.
- `mcnemar_exact_diff(n10: int, n01: int, n: int, *, alpha: float = 0.05) -> Estimate`: conditional on m = n10 + n01, Clopper–Pearson on n10/m via `beta_quantile`, mapped to d = (m/n)(2p − 1); m = 0 → point 0, bounds 0. Published per seed.
- `clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]`; `non_inferior(e: Estimate, margin: float = -0.05) -> bool` (`e.lo > margin`).
- `summarise(ledger_rows, grades, *, stage: str, reference: str = "baseline") -> Summary` (per arm vs reference and per repo: cost ratio, resolved diff, exact per seed, turns, wall, token classes, overhead, quota %, counts of `infra_failed`/dropped).

- [ ] **Step 1:** Failing tests `test_clopper_pearson_known_values` (`(5, 10)` → `(0.1871, 0.8129)` ± 1e-4), `test_identical_arms_ratio_one`, `test_bootstrap_reproducible_with_seed`, `test_mcnemar_symmetric_when_balanced`, `test_noninferior_boundary` (lo = −0.05 is not non-inferior), `test_missing_cost_dropped_and_counted`, `test_cluster_bootstrap_uses_tasks_not_runs` (3 seeds of one task move the CI like one task).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): paired bootstrap and exact non-inferiority statistics`.

### Task 9: Stages, output-style rule and release gate

**Files:** Create `bench/stages.py`, `bench/gate.py`; test `tests/bench/test_stages.py`, `tests/bench/test_gate.py`.

**Interfaces (produces):**
- `@dataclass(frozen=True) class StageSpec: name: str; arms: tuple[str, ...]; optional_arms: tuple[str, ...]; seeds: tuple[int, ...]; model: str`. `SCREENING = StageSpec("screening", ("baseline", "concise", "token-sparen", "ts-style", "ts-cold", "rtk", "headroom", "tokeneconomizer"), ("caveman", "context-mode"), (0,), "sonnet")`; confirmation spec built by `confirmation_spec(screen: Summary, model: str) -> StageSpec` with seeds `(0, 1, 2)`.
- Arm selection (pre-declared): the two competitors (`rtk`, `headroom`, `tokeneconomizer`, `caveman`, `context-mode`) with the lowest screening median cost ratio vs baseline among those whose resolved-diff point estimate is ≥ −0.05 (ties by name); `ts-style` joins iff its screening resolved-diff point vs `token-sparen` ≥ −0.05 and its median cost ratio vs `token-sparen` < 1.
- `style_rule(confirm: Summary) -> Literal["recommend_opt_in", "keep_off", "not_run"]`: `recommend_opt_in` iff `ts-style` is non-inferior vs `baseline` and vs `token-sparen` (bootstrap) and its cost-ratio CI upper bound vs `token-sparen` < 1. Default stays off either way (Design decisions).
- `gate(confirm: Summary, *, release_hash: str, e2_ok: bool) -> GateResult(passed: bool, reasons: list[str])`: pass iff `token-sparen` vs `baseline` resolved-diff bootstrap lower bound > −0.05; the measured `plugin_hash` equals `release_hash`; every `token-sparen` run load-verified and live; ≤ 2 `infra_failed` tasks per arm; ≥ 45 paired tasks; one model id and one Claude Code version across the stage; `e2_ok`. Cost is reported, never gated.

- [ ] **Step 1:** Failing tests `test_confirmation_picks_two_cheapest_noninferior_competitors`, `test_style_joins_only_when_rule_holds`, `test_gate_passes_on_noninferior_valid_run`, `test_gate_fails_on_hash_mismatch`, `test_gate_fails_on_mixed_model_or_version`, `test_gate_fails_on_too_many_infra_failures`, `test_style_rule_never_changes_default` (defaults.toml untouched).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): stage selection, output-style rule and release gate`.

### Task 10: Public report generator (E4 only)

**Files:** Create `bench/report.py`, `bench/svg.py`; test `tests/bench/test_report.py`.

**Interfaces (produces):**
- `build_report(ledger: Path, grades: Path, *, stage: str, out_dir: Path, gate: GateResult | None = None) -> Path` writes `report.json` (machine-readable, the site's future input), `report.md` and `figures/{cost_ratio,resolved_diff,pareto}.svg` (forest plots with 95 % CIs; cost vs resolved per arm). `report.md` opens with the claims block: n, seeds, model id, Claude Code version, date range, plugin hash, harness commit, pins, grader routes, exclusions, method links; every number is printed with its CI.
- `assert_e4_source(ledger: Path) -> None` raises `NotE4Error` unless the first line is the E4 ledger header (refuses replay/ope reports).
- `scan_private(text: str, *, home: Path, deny: Sequence[str]) -> list[str]` (absolute home paths, `/Users/`, `C:\Users\`, `TOKEN_SPAREN_DENYLIST` words, email addresses); `build_report` refuses to write on any hit.
- `publish(report_dir: Path, repo_docs: Path) -> Path` copies into `docs/benchmarks/<campaign>-<stage>/`; refused unless `claims_ok` (every claims field present) and the stage is graded completely.
- `svg.forest(rows: Sequence[tuple[str, Estimate]], *, ref: float, title: str) -> str`, `svg.scatter(points: Sequence[tuple[str, float, float]], *, title: str) -> str` — text labels, colour-blind-safe two-tone palette, readable in light and dark (CSS `prefers-color-scheme` inside the SVG).

- [ ] **Step 1:** Failing tests `test_report_refuses_non_e4_input`, `test_report_has_no_private_paths`, `test_every_number_has_ci_and_n`, `test_claims_block_complete`, `test_publish_refused_without_claims`, `test_svg_well_formed` (`xml.etree` parses; one row per arm).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(bench): public E4 report with figures`.

### Task 11: CLI, dry-run end to end, CI job and docs

**Files:** Create `bench/__main__.py`, `bench/cli.py`, `bench/README.md`, `docs/reports/m5-method.md`; modify `.github/workflows/ci.yml` (job `bench-dry-run`; `mypy --strict bench` in `lint`), `.claude/agents/bench-runner.md`, `.claude/skills/run-evals/SKILL.md`, `docs/specs/2026-11-25-token-sparen-design.md` (§10.4 own runner and Mini composition, §10.5 figures only for E4 and the exact gate wording, §6.6 as built, revision-log entry), `CLAUDE.md`, `AGENTS.md`, `MEMORY.md` (Design decisions above, dated); test `tests/bench/test_cli.py`, `tests/test_docs.py`.

**Interfaces (produces):** `python -m bench <cmd>` with `main(argv: list[str] | None = None) -> int`:
- `prepare toolchain|templates|images --out DIR` (Task 12), `preflight --out DIR`, `run --stage screening|confirmation --campaign ID --out DIR [--arms a,b] [--wait] [--dry-run]`, `quota set --five-hour P --weekly P --out DIR`, `status --out DIR` (counts per arm/status, next key, last quota reading; no secrets), `grade --stage S --out DIR`, `analyse --stage S --out DIR` (Summary JSON), `gate --out DIR --release-hash H`, `report --stage S --out DIR [--publish]`.
- `--dry-run`: `FakeBackend`, `FakeGrader`, `FAKE_TASKS`, scripted quota readings, `n_boot = 200`; touches no Docker, network or `claude`.
- `plugin_hash(root: Path) -> str`: git tree hash of `.claude-plugin/`, `hooks/`, `skills/`, `token_sparen/` at HEAD; refuses a dirty tree for confirmation.

- [ ] **Step 1:** Failing tests `test_dry_run_end_to_end_needs_no_quota` (`subprocess` calls to `docker`/`claude`/`sb-cli` patched to raise; run → grade → analyse → gate → report succeed in tmp), `test_dry_run_resume_is_idempotent`, `test_status_prints_no_secret`, `test_confirmation_refuses_dirty_tree`, `test_m5_method_doc_has_no_private_figures` (method doc: no digits except dates, section numbers, the fixed design constants 50/25/3/5/80/3600/−5).
- [ ] **Step 2–4:** fail → implement → battery; CI job runs `python -m bench run --dry-run --campaign ci --out "$RUNNER_TEMP/bench"` then `grade`, `analyse`, `report`.
- [ ] **Step 5:** Commit `feat(bench): CLI, dry-run pipeline and M5 method docs`.

### Task 12: Prepare — toolchain, competitor installs, images, smoke (controller-run, owner-authorised)

Owner steps first (cannot be automated): `claude setup-token` → token in a 0600 file outside the repo, exported as `CLAUDE_CODE_OAUTH_TOKEN` only in the bench shell; optional `sb-cli gen-api-key` / `verify-api-key` → `SWEBENCH_API_KEY`. Output dir under the owner's private folder (outside any git tree).

- [ ] **Step 1:** `python -m bench prepare toolchain --out DIR`: in an `ubuntu:22.04` arm64 container, build `toolchain/` (Claude Code native binary at the host's pinned version, Python 3.12 via `uv python install`, Node 22, `rtk-oss` aarch64, a `headroom-ai[all]` venv, a TokenEconomizer clone at a pinned commit); write `pins.json` (versions, commit SHAs, download sha256).
- [ ] **Step 2:** `prepare templates`: per arm, run the documented default install with `CLAUDE_CONFIG_DIR=<template>/.claude` and `HOME=<template>` inside the toolchain container (`rtk init -g`; `claude plugin marketplace add …` + `claude plugin install …` for caveman, context-mode, tokeneconomizer; headroom wrap needs no install). Assert the real `~/.claude` mtime/listing unchanged before and after.
- [ ] **Step 3:** `prepare images` (pull arm64 images, local build for the rest) and `preflight` → `bench/data/routes.json` committed (public oracle data, date) as `chore(bench): Verified Mini routes from oracle preflight`.
- [ ] **Step 4:** Smoke: every arm × the fake task, then every arm × one real django task on the screening model (owner-approved quota). Each arm must pass `verify_load` and its liveness probe. An arm that cannot run in its documented default config is marked `infeasible` with the reason in `MEMORY.md` and dropped (never silently reconfigured). Record calibration: quota % per run if readings exist.
- [ ] **Step 5:** `MEMORY.md` entry (pins, routes summary, infeasible arms, calibration without private figures); commit `docs: M5 preparation record`.

### Task 13: Screening campaign (controller-run)

- [ ] **Step 1:** Tag the release candidate `v0.5.0-rc1`; `python -m bench run --stage screening --campaign m5 --out DIR --wait` across as many windows as needed; on exit 3 (weekly) ask the owner before resuming; add `caveman`, `context-mode` only if the weekly projection after the core arms allows.
- [ ] **Step 2:** `grade`, `analyse`, `report` (private). Record the confirmation arm set from `confirmation_spec` in `MEMORY.md` (rule outputs only).
- [ ] **Step 3:** Optional `report --publish` labelled "screening" (cheaper model, 1 seed); commit `docs(bench): M5 screening report`.

### Task 14: Confirmation, release gate and publication (controller-run)

- [ ] **Step 1:** Clean tree at the release candidate; `run --stage confirmation --campaign m5 --out DIR --wait` (3 seeds, default model pinned at the first run).
- [ ] **Step 2:** `grade`, `analyse`, `gate --release-hash <plugin_hash of the RC>` (E2 run first). Gate fails → no v1.0 release; record in `MEMORY.md` and the method doc; triage via a new plan.
- [ ] **Step 3:** `style_rule` outcome recorded; README opt-in recommendation only on `recommend_opt_in`.
- [ ] **Step 4:** `report --publish`; full battery; commit `docs(bench): M5 confirmation report (plugin <hash12>)`.

### Task 15: M5 review, drift report and tag

**Files:** Create `docs/reports/drift-m5.md`; modify `README.md` (Benchmarks section linking the published report), `CHANGELOG.md`, `MEMORY.md`, `CLAUDE.md`.

- [ ] **Step 1:** Goldfish cold review of the M5 diff against spec §10.4, §10.5, §6.6 (controller-dispatched); fix wave.
- [ ] **Step 2:** Drift check → `docs/reports/drift-m5.md`.
- [ ] **Step 3:** Commit `docs: M5 readme, changelog and drift report`; push; tag `v0.5.0-m5` after CI is green.
