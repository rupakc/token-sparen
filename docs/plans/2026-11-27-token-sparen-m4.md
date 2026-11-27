# token-sparen M4 — Hill-climbing, M3 lever replay and parameter freeze — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tune parameters on offline evidence from the maintainer's local Claude Code sessions (E1) with one honest held-out confirmation, measure the M3 levers' risk offline, evaluate the carried learning-engine options and a LinTS challenger in simulation, clear the quality-relevant deferred minors, and freeze parameters as `v0.4.0-m4`.

**Architecture:** E1 replay (`token_sparen/replay/`) becomes parallel, resumable and contamination-safe, with lanes: compression (existing), facts (transcript-only mining stats), loop guard, pruning, and an online-learning simulation that runs the real `Engine` against proxy-regret feedback in an isolated state root. A hill-climbing harness in `evals/hillclimb/` searches a pre-declared grid on the **dev** split, produces one joint proposal, and confirms it once on **test** (all-or-nothing). All numbers stay in private reports outside any git work tree.

**Tech Stack:** Python ≥ 3.11 stdlib only at runtime (`concurrent.futures`, `tomllib`); pytest, ruff, mypy for dev.

**Spec:** `docs/specs/2026-11-25-token-sparen-design.md` (§7.2–§7.8, §10.0–§10.6, §14 M4). Previous plan and ledger: `docs/plans/2026-11-25-token-sparen-m0-m3.md`, `.superpowers/sdd/2026-11-25-token-sparen-m0-m3/progress.md`. Plan rulings: `.superpowers/sdd/2026-11-27-token-sparen-m4/progress.md`.

**Out of M4 (recorded deferrals):** output-style point §6.6 → M5 (needs E4 evidence); host/model-tier prior-sharing keys → v1.1; LinTS in hooks → after v1.0 and only with E3 evidence; PreToolUse wrap of failing Bash → rejected (invariant: no PreToolUse command rewriting).

## Global Constraints

- Everything in the M0–M3 global constraints still binds (`global-constraints.md` in this plan's workspace).
- **E1/E3 numbers are never committed** (spec §10.1, C4): not in code, tests, fixtures, docs, commit messages, CHANGELOG, MEMORY, or stdout of any M4 command run against real data. Real-data commands run with `--quiet` (prints only the private report path and its id = first 12 hex of the report's sha256).
- **Tuned values come only from grids pre-declared in `evals/hillclimb/space.toml`**, committed before any real-data run. Refit priors snap to mean ∈ {0.02, 0.05, 0.1, 0.2, 0.3, 0.5}, strength ∈ {2, 4, 8}. Provenance is cited as "selected on E1 dev, private report <id12>", never a figure.
- Private reports go only through `replay.report.write_private` (0600, refuses paths inside any git work tree).
- Tests use synthetic transcripts only; nothing in tests reads `~/.claude/projects`, `~/token-sparen-private` or the real home. Real-data runs happen only in Task 11 (controller-run, owner-authorised).
- **Splits** (by a session's first record timestamp): dev < 2026-11-08 ≤ test < 2026-11-26 (cutoff chosen 2026-11-27 from session counts only, no outcomes: Claude Code keeps transcripts ~30 days, so a 2026-11-01 cutoff left dev nearly empty). Sessions on/after 2026-11-26 or containing any `[token-sparen:` marker are excluded whole (`excluded_dogfood`). Test is evaluated **once**, in Task 11, on one joint proposal.
- **Acceptance (all-or-nothing, Task 11):** versus current defaults on test, paired by project group, measured in the `sim` lane (the shipped learner with the proposal vs with current defaults): bootstrap 95 % CI lower bound of the net-savings difference > 0; for every point, upper 95 % bound of the proxy-regret-per-trim difference ≤ 0.2 · τ(point) and absolute proxy regret per trim ≤ τ(point) (`ope.tau_for`); E2 = 100 %; E2 `inline_rate` per level not below current defaults. Otherwise defaults stay unchanged and the method doc says so.
- M3 lane results (facts, loop guard, pruning) may move an M3 default only toward **less** intervention.
- Engine changes ship default-off (current behaviour) unless chosen by the Task 11 joint proposal.

## Review Focus

1. **Dogfooding contamination**: a session with any `[token-sparen:` marker or first timestamp ≥ 2026-11-26 is excluded whole and counted. → Task 2 `test_dogfood_sessions_excluded_whole`.
2. **Malformed real data**: truncated last line, non-UTF-8 bytes, a line over 8 MB, unknown record types → skipped and counted; never a crash; worker memory bounded. → Task 2 `test_malformed_lines_are_counted_not_fatal`.
3. **Resumed sessions across the split**: copied history is de-duplicated per project group across all files before split attribution, so a test-period resume never re-counts dev history. → Task 2 `test_copied_history_not_recounted_across_split`.
4. **Out-of-bounds proposals**: every `space.toml` value lies inside the owning module's clamp and the invariants (`min_tokens ≥ 2000`, ratios in [0,1]); an out-of-bounds entry fails the harness before any run. → Task 7 `test_space_values_within_module_clamps`.
5. **Figure leakage**: `--quiet` stdout of `replay`, `ope` and the harness matches a fixed template (path + 12-hex id + accept/reject words) and worker tracebacks are recorded as exception type only. → Task 2 `test_quiet_stdout_template`, Task 7 `test_harness_stdout_template`.

---

### Task 1: Quality-relevant deferred minors

**Files:** Modify `token_sparen/mechanisms/loopguard.py`, `token_sparen/adapters/claude/entry.py`, `token_sparen/compress/base.py`, `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `.claude/skills/*`, `.claude/agents/*`. Test `tests/mechanisms/test_loopguard.py`, `tests/adapters/claude/test_entry.py`, `tests/compress/test_base.py`.

In scope: (a) loop-guard test-report fingerprint adds the summary's total failing count (`N failed`), so sets beyond the scan window still change it; (b) the loop-guard prompt handler runs in the always-run chain; (c) loop-guard nudge state and decision record are written only after the response is emitted; (d) `is_error_line` does not treat a stack-frame head as an error line merely because its path contains `exception`/`error` (E2 stays 100 %); (e) docs: params path `token_sparen/params/defaults.toml` in CLAUDE.md/agents/skills, README "Verify" step count, CHANGELOG 0.3.0 wording ("milestone 0.3.0; package version 0.1.0.dev0 until v1.0"). All other ledger minors stay deferred and are listed in `MEMORY.md` in Task 12.

- [ ] **Step 1:** Failing tests `test_report_fingerprint_changes_with_total_beyond_scan_window`, `test_stop_prompt_settles_even_after_overrun`, `test_no_nudge_state_without_delivery`, `test_frame_head_with_exception_in_path_is_not_error_line`.
- [ ] **Step 2:** See them fail. **Step 3:** implement (a)–(e). **Step 4:** full battery (CLAUDE.md "Commands"). **Step 5:** commit `fix: M4 triage of quality-relevant deferred minors`.

### Task 2: E1 at real-data scale — parallel, resumable, contamination-safe

**Files:** Create `token_sparen/replay/runner.py`; modify `token_sparen/replay/replay.py`, `token_sparen/replay/report.py`, `token_sparen/cli.py` (`replay`, `ope`); test `tests/replay/test_runner.py`, `tests/perf/test_replay_throughput.py`.

**Interfaces (produces):**
- `class Lane(Protocol)`: `feed(event: Event, session: SessionInfo) -> None`, `end_session(session: SessionInfo) -> None`, `report() -> dict[str, Any]`; module-level `merge_reports(name: str, a: dict, b: dict) -> dict` (associative, order-independent).
- `LaneSpec = tuple[str, dict[str, Any]]` (lane name, picklable params overlay + seed); `LANES: dict[str, Callable[[Config, dict], Lane]]` registry (lanes register by name).
- `runner.run_parallel(paths: Sequence[Path], specs: Sequence[LaneSpec], *, jobs: int, cutoff: date = date(2026, 11, 8), dogfood_start: date = date(2026, 11, 26), groups: set[str] | None = None, checkpoint: Path | None = None) -> RunResult`; `RunResult.by_split: dict[Split, dict[str, dict]]` and counters `files, sessions, excluded_dogfood, malformed_lines, oversize_lines, worker_errors: dict[str, int]` (exception type → count), `wall_s`.
- Group key = transcript file's parent directory; one group per worker task, files chronological; `RunIds` de-dup runs over the whole group before split attribution. `groups` selects a subset (Task 7 subsampling).
- Checkpoint: whole JSON document rewritten via `write_private` after each finished group; `--resume` skips finished groups.
- CLI: `token-sparen replay --jobs N --resume --lanes compression[,facts,loopguard,prune,sim] --split dev|test|all --quiet --out PATH`; `token-sparen ope ... --quiet`. `--quiet` prints exactly `report: <path> id: <12 hex>`; `replay --quiet` adds a second line `status: complete|incomplete` (as built; no figure).
- As built: the cutoff default is `replay.DEFAULT_CUTOFF` = 2026-11-08 (the Global Constraints split; this interface first said 2026-11-01).
- The existing compression replay becomes the `compression` lane (behaviour unchanged; its existing tests stay green).

- [ ] **Step 1:** Failing tests: Review Focus 1, 2, 3, 5 plus `test_parallel_equals_serial` (jobs=1 vs 4 on a 40-file synthetic corpus), `test_resume_skips_finished_groups`, `test_worker_error_recorded_as_type_only`.
- [ ] **Step 2–4:** fail → implement (`ProcessPoolExecutor`, spawn-safe top-level worker, bounded `readline` skipping lines > 8 MB) → full battery; perf test on a 20 MB synthetic corpus generated in tmp (jobs=2; throughput floor relative to a jobs=1 run, peak RSS per worker < 300 MB where measurable).
- [ ] **Step 5:** commit `feat(replay): parallel resumable E1 runs with lanes`.

### Task 3: Facts lane (transcript-only)

**Files:** Create `token_sparen/replay/facts_lane.py`; modify `token_sparen/facts/miners.py` (evidence mode); test `tests/replay/test_facts_lane.py`, `tests/facts/test_miners.py`.

**Interfaces:** `miners` mining entry points gain `evidence: Literal["full", "transcript"] = "full"`; `"transcript"` disables every filesystem and git probe (`declared_packages`, `git_ignored`, file sampling in `generated_evidence`) and uses only transcript content (markers in tool results that showed file heads). `FactsLane` (registered `"facts"`) reports per fact kind: `candidates`, `contradicted_later` (a later session contradicts the candidate — wrong-fact risk), `waste_events` and `waste_tokens` (the waste each kind targets; labelled upper bound of what a fact could avoid), `render_tokens` (cost of the rendered block via `facts.sanitize.render` / `facts.inject.build`). No activation/effect simulation (history had no injections).

- [ ] **Step 1:** Failing tests `test_transcript_mode_never_touches_fs_or_git` (monkeypatch `subprocess.run`, `Path.read_text`, `os.stat` on project paths to raise), `test_contradiction_counts_risk`, `test_waste_counted_per_kind`.
- [ ] **Step 2–5:** fail → implement → battery → commit `feat(replay): facts lane`.

### Task 4: Loop-guard lane

**Files:** Create `token_sparen/replay/loopguard_lane.py`; modify `token_sparen/mechanisms/loopguard.py` (extract pure helpers, behaviour unchanged); test `tests/replay/test_loopguard_lane.py`, `tests/mechanisms/test_loopguard.py`.

**Interfaces:** public pure helpers in `loopguard`: `fingerprint(text: str, *, tool: str = "", target: str = "") -> str | None` (existing signature, unchanged), `reversals(edits: Sequence[tuple[str, str, str]]) -> int` (file, old, new → full A→B→A→B reversal count), `recurs_within(seq: Sequence[str | None], i: int, n: int = 5) -> bool`; production code calls them (existing loop-guard tests unchanged and green). `LoopguardLane` (`"loopguard"`) per tool category: `loops_reaching` for k ∈ {2, 3, 5}, `natural_break_rate` (share of fingerprints at count 3–4 not recurring within 5 calls), `exposure_tokens` per k (upper bound of what a perfect nudge could save), `converging_triggers` per k (trigger followed within 5 calls by success of the same command head), separate per agent chain.

- [ ] **Step 1:** Failing tests `test_natural_break_rate`, `test_exposure_after_kth_repeat`, `test_converging_trigger_counted`, `test_subagent_chains_separate`, `test_helpers_match_production_behaviour`.
- [ ] **Step 2–5:** → commit `feat(replay): loop-guard lane`.

### Task 5: Pruning lane

**Files:** Create `token_sparen/replay/prune_lane.py`; test `tests/replay/test_prune_lane.py`.

**Interfaces:** consumes `prune.candidate_names`, `prune.is_candidate`, `prune.observe_session`, `prune.ready` on an in-memory store doc (no settings writer); at each simulated session end a rule that is `ready` is marked pruned **in memory** (simulated apply, lease counted in sessions), so later sessions can register `needed_while_pruned` and `lease_restores`. `PruneLane` (`"prune"`) per project group: `servers_seen`, `would_prune`, `sessions_pruned`, `needed_while_pruned` (tool call, user mention or ToolSearch for a pruned server within its lease), `lease_restores`, `listing_tokens_saved` (tool-name listing tokens only; MCP definitions are deferred by the host).

- [ ] **Step 1:** Failing tests `test_idle_server_would_be_pruned`, `test_use_while_pruned_counts_needed`, `test_lease_restore_counted`, `test_never_calls_settings_writer` (writer patched to raise).
- [ ] **Step 2–5:** → commit `feat(replay): pruning lane`.

### Task 6: Engine options carried into M4 (default-off)

**Files:** Modify `token_sparen/learn/engine.py`, `token_sparen/learn/features.py`, `token_sparen/mechanisms/compression.py` (savings measurement), `token_sparen/learn/overlay.py`, `token_sparen/params/defaults.toml`, `token_sparen/config.py` (PROJECT_POLICY: new keys user-only); test `tests/learn/test_engine.py`, `tests/learn/test_features.py`, `tests/learn/test_overlay.py`.

**Interfaces:**
- `[learn] savings_reward = "scaled"` (current) | `"fraction"` (removed / original) | `"counterfactual"` (removed / max removed over the point's arms on the same output, times `min(1, resends / R)`, `R = learn.resend_horizon`, default 20; computes every arm, so it is admissible in the grid only if `tests/perf/test_counterfactual_reward.py` keeps the PostToolUse hook p95 < 50 ms) | `"cost"` (cache-weighted cost removed, `estimate.Prices`, scaled by the point's p95).
- `[learn] ladder_retry_decisions = 0` (off): when > 0, `Engine._ladder` re-opens the next harsher arm for probing after that many decisions since it was locked.
- Context-window band uses the transcript/hook-reported model window (200k vs 1M) instead of a fixed threshold (correctness fix, ships on).
- `[guards] pretrim_bounded_keep_ratio` (default = the value that reproduces current behaviour). As built: no default; unset = `max(packaged standard keep ratio, configured standard keep ratio)` (final-review ruling New-1); any number is used as is.
- `overlay.fold(..., mine_facts: bool = True, prune: bool = True)` so the simulation can fold engine evidence only.

- [ ] **Step 1:** Failing tests `test_each_savings_reward_in_unit_interval`, `test_counterfactual_reward_uses_best_arm`, `test_ladder_retry_off_by_default_and_reopens_when_set`, `test_window_band_uses_reported_window`, `test_bounded_pretrim_default_reproduces_current`, `test_fold_engine_only_skips_facts_and_prune`.
- [ ] **Step 2–5:** → commit `feat(learn): M4 engine options (default off)`.

### Task 7: Simulation lane and LinTS challenger (offline)

**Files:** Create `token_sparen/replay/sim_lane.py`, `evals/challengers/lints.py`; test `tests/replay/test_sim_lane.py`, `tests/evals/test_lints.py`.

**Interfaces:**
- `SimLane` (`"sim"`, params: overlay, seed; optional `chooser_factory` injected only by `evals/`, default = the production engine): runs the **live** engine (decide → reward from the compression lane's measured savings and the replay proxy regret → `overlay.fold(..., mine_facts=False, prune=False)` at session end) chronologically per group, in a temp state root deleted afterwards (HOME and TOKEN_SPAREN_HOME pointed into it). Reports per point: `decisions`, `arm_share` by epoch (thirds), `savings_tokens`, `proxy_regret_per_trim` by epoch, `breaker_trips`; plus the same for a reference engine with current defaults in the same run.
- `evals/challengers/lints.py`: `LinTS(arms, safe_arm, tau, dim, lam=1.0, v=0.5)` with `choose(x, rng) -> str`, `update(arm, x, savings, regret)`; two linear posteriors per arm (Sherman–Morrison), constraint as the engine (sampled regret ≤ τ, safe arm S = R = 0); `x` = one-hot concatenation of the point's features (`learn/features.py`), d ≤ 32. Offline only — `token_sparen/` never imports it; `evals/challengers/lints_sim.py` runs SimLane with a LinTS `chooser_factory`; never wired to hooks.
- Proxy regret machinery is reused from `replay.py` (refactor into `ProxyRegret` if needed; compression lane output unchanged).

- [ ] **Step 1:** Failing tests `test_sim_learns_to_avoid_regretting_arm` (synthetic corpus where `aggressive` always regrets → its share < 5 % in the last epoch), `test_sim_never_writes_outside_temp_root` (facts miners and settings writer patched to raise; real HOME untouched), `test_sim_reproducible_with_seed`, `test_lints_matches_closed_form_posterior`, `test_lints_respects_regret_constraint`.
- [ ] **Step 2–5:** → commit `feat(replay): simulation lane and offline LinTS challenger`.

### Task 8: E2 params overlay and inline rate

**Files:** Modify `evals/e2/run.py`; test `tests/evals/test_e2.py`.

**Interfaces:** `run(levels, params: Mapping[str, Any] | None = None) -> E2Report` (overlay written as a temporary user config inside `_isolated_home`); `E2Report` gains `inline_rate: dict[level, float]` (needles visible in the compressed text, not only reachable via the marker). `--strict` unchanged.

- [ ] **Step 1:** Failing tests `test_params_overlay_reaches_compressors`, `test_inline_rate_per_level`.
- [ ] **Step 2–5:** → commit `feat(evals): E2 params overlay and inline rate`.

### Task 9: Hill-climbing harness

**Files:** Create `evals/hillclimb/run.py`, `evals/hillclimb/space.toml`, `evals/hillclimb/README.md`; modify `.claude/skills/hill-climb/SKILL.md`, `.claude/agents/eval-hillclimber.md`; test `tests/evals/test_hillclimb.py`.

**Interfaces:**
- `space.toml`: entries `{key, values = [...], point}` — explicit grids only, each including the current default; covers compression keep ratios per level, `min_saving_ratio`, relevance weights (as built: code constants, out of M4 scope), read-dedupe thresholds, guard limits incl. `guards.pretrim_bounded_keep_ratio` (as built: read-dedupe, guard and M3 keys are declared with `measured_by = "none"` and never searched, because no E1 lane replays them; a lane for them is future work), designed priors `learn.defaults.*` (the snapped grid; as built: prior means only, the strength grid is deferred), `learn.savings_reward`, `learn.ladder_retry_decisions`, and the conservative-direction-only M3 keys (facts activation thresholds and prune idle thresholds: values ≥ current only). τ and `[ope]` thresholds are excluded.
- `python evals/hillclimb/run.py --space evals/hillclimb/space.toml --jobs N --out PRIVATE_DIR --quiet` (dev only): coordinate ascent; per coordinate, values screened on a seeded 25 % sample of project groups, top half on 50 %, best on 100 % (successive halving); objective = net savings (compression removed minus recovery reads, cost-weighted; as built: no lane replays read-dedupe, so it has no term): from the `compression` lane (frozen-engine replay) for static parameter coordinates, and from the `sim` lane for learning coordinates (`learn.savings_reward`, `learn.ladder_retry_decisions`, `learn.defaults.*`) — never summed across lanes; a value is admissible on dev only if E2 = 100 %, inline_rate ≥ default, proxy regret per trim non-inferior to the defaults (difference upper bound ≤ 0.2 τ) and ≤ τ, and sim breaker trips ≤ the reference engine's. Writes private `trail.jsonl` and one joint `proposal.toml`.
- `--confirm proposal.toml` evaluates the joint proposal vs defaults once on test with the Global Constraints acceptance rule; is one-shot per plan: the first `--confirm` records itself in a private ledger and every later `--confirm` (any proposal) is refused; writes private `confirm.json` (test baseline and proposal in the same paired run).
- As built after the final review (rulings M4-G1, M4-G2, M4-G4, New-2): `--confirm` also runs the `compression` lane on each proposed `compression.levels.<arm>` key's arm (per-arm regret bounds and minimum evidence, same single test read); the `sim` lane rewards with the live re-send estimate and averages ≥ 3 seeds per group; `--campaign ID` is a label, `--cutoff`/`--dogfood-start` set the dev split and `--confirm` takes them from the dev report beside the proposal (mismatch refused); the ledger is one file of used test windows (`<state root>/hillclimb/test-windows.json`) and any overlapping window is refused whatever the campaign.
- Stdout with `--quiet`: fixed template lines `report: <path> id: <12hex>` and `<key>: accepted|rejected|unchanged` (as built: also `status: complete|incomplete` and `unmeasured`).

- [ ] **Step 1:** Failing tests: Review Focus 4 and 5, `test_dev_only_without_confirm`, `test_confirm_runs_once_per_proposal`, `test_successive_halving_by_group_keeps_best`, `test_values_only_from_grid`, `test_m3_keys_only_conservative_direction`.
- [ ] **Step 2–5:** → commit `feat(evals): hill-climbing harness`.

### Task 10: Method document and spec text

**Files:** Create `docs/reports/m4-method.md`; modify `docs/specs/2026-11-25-token-sparen-design.md` (§7.9 provenance vs §10.1 resolution, §10.6 accept rule and commit-message rule, §7.8 LinTS offline-only, §6.6 → M5, revision-log entry), `README.md` (replay `--lanes/--jobs/--quiet`, privacy note).

Method doc (public): splits by date, exclusion of dogfooded sessions, lanes, grids, one-look all-or-nothing acceptance, statistics; no counts, rates or figures; acknowledges that an earlier aggregate E1 look over all sessions informed the quality-first defaults and that no parameter was tuned on it.

- [ ] **Step 1:** Write; `check_denylist`; a test `test_method_doc_has_no_figures` (no digits except dates and section numbers) in `tests/test_docs.py`. **Step 2:** commit `docs: M4 method and spec revisions`.

### Task 11: Real-data campaign and freeze (controller-run)

Private outputs only under `~/token-sparen-private/reports/m4/` (0600). All commands with `--quiet`.

- [ ] **Step 1:** Baseline on dev only: `token-sparen replay --lanes compression,facts,loopguard,prune,sim --split dev --jobs N --quiet --out ~/token-sparen-private/reports/m4/baseline-dev`. No test-split run of any kind before Step 4.
- [ ] **Step 2:** Harness on dev; SimLane comparison of `savings_reward` options and `ladder_retry_decisions` (in the grid); LinTS vs beta in SimLane on dev (informational; promotion out of scope).
- [ ] **Step 3:** M3 rulings from the facts/loop-guard/prune lanes, conservative direction only; recorded in `MEMORY.md` without figures.
- [ ] **Step 4:** `--confirm` once on test. Accepted → apply the joint proposal to `defaults.toml` in one commit `chore(params): M4 joint proposal, E1 dev-selected, test-confirmed (report <id12>)`; rejected → no parameter change, record in `MEMORY.md` and the method doc ("joint proposal not confirmed").
- [ ] **Step 5:** If dogfooding decision logs reach `[ope] min_trims`, run `token-sparen ope --quiet` for the frozen engine vs `static` (private); `insufficient` is recorded as such.
- [ ] **Step 6:** Full battery; push.

### Task 12: M4 review, drift report and freeze tag

**Files:** Create `docs/reports/drift-m4.md`; modify `README.md`, `CHANGELOG.md`, `MEMORY.md` (known minors list), `CLAUDE.md`.

- [ ] **Step 1:** Goldfish cold review of the M4 diff against spec §7.2–§7.8, §10 (controller-dispatched); fix wave as needed.
- [ ] **Step 2:** Drift check → `docs/reports/drift-m4.md` (function/section references, no line numbers).
- [ ] **Step 3:** Commit `docs: M4 readme, changelog and drift report`; push; tag `v0.4.0-m4` after CI is green.
