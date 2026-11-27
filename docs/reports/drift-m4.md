# Drift report: M4

Date: 2026-11-27

## Scope

Milestone M4 (hill-climbing, M3 lever replay and parameter freeze) against
`docs/specs/2026-11-25-token-sparen-design.md` §7.2–§7.9, §10.0–§10.6 and §14, with the M4
revision-log entries (later entries win, including "M4 final review"); the plan
`docs/plans/2026-11-27-token-sparen-m4.md` (Global Constraints, Review Focus, per-task
Interfaces and their "as built" notes); `docs/reports/m4-method.md`. Code:
`token_sparen/replay/`, `token_sparen/learn/`, `token_sparen/mechanisms/loopguard.py`,
`token_sparen/mechanisms/guards.py`, `token_sparen/mechanisms/governor.py`,
`token_sparen/compress/base.py`, `token_sparen/adapters/claude/entry.py`,
`token_sparen/config.py`, `token_sparen/cli.py`, `evals/hillclimb/`, `evals/e2/`,
`evals/challengers/` and `tests/`. Also checked: the `CLAUDE.md` invariants, `README.md`
(replay and hill-climbing sections, configuration table) and `evals/hillclimb/README.md`.

Checked against the final M4 code, after the cold-review fix wave and the final docs wave.
Read-only for code; no figures from any run.

Status key: matches / diverges / missing / extra; **deferred** = the spec or plan itself puts
the item outside M4. Evidence names the file and the function, class, constant or section;
tests are named by function (`tests/` mirrors the package).

## Requirements

| Requirement | Status | Evidence | Note |
|---|---|---|---|
| Parallel, resumable, group-based replay with lanes and an associative merge | matches | `token_sparen/replay/runner.py` `run_parallel`, `merge_reports`, `Lane`; `test_parallel_equals_serial`, `test_resume_skips_finished_groups` | Checkpoint also fingerprints the config and snapshots file sizes (documented in README). |
| Dogfood sessions excluded whole (marker or date from the dogfood start), subagents together | matches | `runner._excluded`; `test_dogfood_sessions_excluded_whole` | |
| Malformed or oversize lines counted, never fatal; 8 MB line bound | matches | `runner.iter_lines`, `MAX_LINE_BYTES`; `test_malformed_lines_are_counted_not_fatal` | |
| Copied history de-duplicated per group before the split | matches | `runner.session_day`, `replay.RunIds`; `test_copied_history_not_recounted_across_split` | |
| Worker errors recorded as exception type only | matches | `runner._isolated`; `test_worker_error_recorded_as_type_only` | |
| `--quiet` stdout template (report line, then status line) | matches | `token_sparen/cli.py` replay handler; `replay/report.py` `quiet_line`; plan Task 2 Interfaces (as-built note); `test_quiet_stdout_template` | Plan now records the `status:` line. |
| Split cutoff dev < 2026-11-08 ≤ test < 2026-11-26, shared by `replay` and the harness | matches | `replay/replay.py` `DEFAULT_CUTOFF`; `runner.DEFAULT_CUTOFF`; `evals/hillclimb/run.py` `DEV_CUTOFF`, `DOGFOOD_START`; plan Task 2 Interfaces | Plan Task 2 default corrected; harness and plain `replay` agree. |
| Compression lane behaviour unchanged except the every-trim signature watch | matches | `runner._compression`, `replay/replay.py`; CHANGELOG "M4 simulation lane" | The every-trim watch is a deliberate, recorded correctness fix. |
| Facts lane: transcript-only evidence, no file system or git probes | matches | `replay/facts_lane.py` `FactsLane`; `facts/miners.py` `check_evidence`; `test_transcript_mode_never_touches_fs_or_git` | |
| Loop-guard lane and pure helpers shared with production | matches | `replay/loopguard_lane.py` `LoopguardLane`; `mechanisms/loopguard.py` `fingerprint`, `reversals`, `recurs_within`; `test_helpers_match_production_behaviour` | |
| Prune lane in memory, never calls the settings writer | matches | `replay/prune_lane.py` `PruneLane`; `test_never_calls_settings_writer` | |
| Sim lane: live engine in an isolated temp root, injectable chooser, epochs, breaker trips, reference engine | matches | `replay/sim_lane.py` `SimLane`, `_epochs`, `resolve_factory`; `test_sim_never_writes_outside_temp_root` | `chooser_factory` is a `module:callable` string (picklable). |
| Sim reward uses the live hook's re-send estimate; actual re-sends only price net savings (M4-G2) | matches | `replay/sim_lane.py` module docstring and savings path; `learn/regret.py` `expected_resends`; `test_savings_reward_uses_the_live_resend_estimate` | Shared-proxy limitation stated in the method doc. |
| Sim evaluated over at least three seeds per group, averaged before the bootstrap (M4-G2) | matches | `evals/hillclimb/run.py` `SIM_SEEDS`, `mean_rows`, `Evaluator.sim_specs`; `test_sim_rows_are_averaged_over_at_least_three_seeds` | |
| LinTS challenger offline only, never imported by the package | matches | `evals/challengers/lints.py` `LinTS`, `MAX_DIM`; `evals/challengers/lints_sim.py`; `test_lints_respects_regret_constraint` | §7.8 and `CLAUDE.md` layout agree. |
| `[learn] savings_reward` options, default `scaled` | matches | `token_sparen/params/defaults.toml` `[learn]`; `learn/regret.py`; `test_each_savings_reward_in_unit_interval`; `tests/perf/test_counterfactual_reward.py` | |
| `ladder_retry_decisions` default off, counted per session | matches | `learn/engine.py` `Engine._ladder`; `test_ladder_retry_off_by_default_and_reopens_when_set` | |
| `guards.pretrim_bounded_keep_ratio` optional; unset = `max(packaged standard, configured standard)`; a project may only raise it | matches | `mechanisms/guards.py` `PACKAGED_STANDARD_KEEP_RATIO`, `_bounded_keep_ratio`; `config.py` `OPTIONAL_KEYS`, `UNSET_FOLLOWS`, `sanitise_project`, `PROJECT_POLICY`; `test_unset_pretrim_follows_packaged_standard`, `test_bounded_pretrim_project_may_only_raise` | Spec revision "M4 final review", README configuration table and plan Task 6 as-built note describe the max rule. |
| New `learn.*` keys user-only | matches | `config.py` `PROJECT_POLICY` | |
| Long-context window latch for the governor and the `band` feature | matches | `mechanisms/governor.py` `record_session_model`, `band_window`; `learn/features.py` `window_1m`; `test_window_band_uses_reported_window` | Listed as a default-on, test-backed change (M4-G5). |
| `overlay.fold(mine_facts=, prune=)` | matches | `learn/overlay.py` `fold`; `test_fold_engine_only_skips_facts_and_prune` | |
| Task 1 triage: fingerprint total, prompt handler order, post-emit state, stack-frame heads | matches | `mechanisms/loopguard.py` `_summary_total`, `fingerprint`; `adapters/claude/entry.py` handler table and `register`; `compress/base.py` `is_error_line`; `test_report_fingerprint_changes_with_total_beyond_scan_window`, `test_stop_prompt_settles_even_after_overrun`, `test_frame_head_with_exception_in_path_is_not_error_line` | See Divergences 1 and 2 for wording left behind. |
| E2 params overlay, pre-trim pass and `inline_rate` per level | matches | `evals/e2/run.py` `run`; `test_params_overlay_reaches_compressors`, `test_inline_rate_per_level` | |
| Grids pre-declared, each with the default, inside clamps; values only from the grid | matches | `evals/hillclimb/space.toml`; `run.py` `load_space`; `test_space_values_within_module_clamps`, `test_values_only_from_grid` | |
| M3 keys only toward less intervention | matches | `run.py` `CONSERVATIVE`; `test_m3_keys_only_conservative_direction` | |
| Read-dedupe, guard and M3 keys declared but unmeasured | matches | `space.toml` `measured_by = "none"`; `run.py` `UNMEASURED`; plan Task 9 as-built note; method doc "Pre-declared grids" | Plan now says these are never searched in M4. |
| Coordinate ascent with successive halving over project groups, dev only | matches | `run.py` `climb`, `_step`, `STAGES`; `test_successive_halving_by_group_keeps_best`, `test_dev_only_without_confirm` | |
| Objective per lane, never summed; no read-dedupe term | matches | `run.py` `compression_rows`, `sim_rows`; plan Task 9 as-built note | Plan objective text corrected. |
| Acceptance rule: bootstrap lower bound, 0.2 · τ non-inferiority, absolute τ, E2 and inline rate, minimum evidence | matches | `run.py` `judge`, `e2_gate`, `MIN_GROUPS`, `MIN_TRIMS`, `NONINFERIORITY` | |
| Per-arm check at confirmation for proposed `compression.levels.<arm>` keys (M4-G1) | matches | `run.py` `level_arms`, `confirm` (`judge(net=False)` per arm, reasons `arm:<arm>:...`); `test_confirm_checks_each_proposed_level_on_its_own_arm` | Same single test read. |
| Each test window read once: one ledger of used windows, overlap refused whatever the campaign (New-2) | matches | `run.py` `ledger_path`, `claim_window`, `ConfirmRefused`; `test_test_window_is_read_once_whatever_the_campaign`, `test_confirm_runs_once_per_proposal` | Method doc and both READMEs describe `test-windows.json`. |
| `--confirm` takes its dates from the dev report; mismatch or missing report refused | matches | `run.py` `dev_report`, `NoDevReport`, `SplitMismatch`, `main` | |
| `--campaign`, `--cutoff`, `--dogfood-start` flags | matches | `run.py` `main`, `campaign_id`; `test_split_dates_and_campaign_are_flags` | README and `evals/hillclimb/README.md` list them. |
| Harness stdout template | matches | `run.py` `_emit`; `test_harness_stdout_template` | Adds `status:` and `unmeasured`; plan as-built note. |
| Method doc and spec M4 entry carry no figures | matches | `docs/reports/m4-method.md`; `tests/test_docs.py` `test_method_doc_has_no_figures`, `test_spec_m4_revision_has_no_figures` | Allowed phrase list holds exact method parameters only. |
| M4 outcome: tuned parameters unchanged; default-on correctness changes listed as test-backed (M4-G5) | matches | method doc "Outcome"; spec revision log "M4 final review"; CHANGELOG "M4 campaign outcome"; `MEMORY.md` M4 entries | |
| Future campaigns: new count-based cutoff, sessions with token-sparen disabled or a contamination-aware replay | deferred | method doc "Future campaigns"; spec revision log "M4 final review" | The contamination-aware replay is not built. |
| Prior strengths searchable (plan Global Constraints) | deferred | `space.toml` header; spec §7.9; plan Task 9 as-built note | Only prior means are searchable; no config key for the compression point's strengths. |
| §7.2 host and model-tier prior sharing | deferred | spec §7.2; plan "Out of M4" | Now v1.1 in both. |
| §7.5 cost weighting | matches | spec §7.5; `learn/regret.py` (`cost` mode) | Spec now says optional since M4, off by default. |
| §7.9 priors refit | matches | spec §7.9 | Spec now says no refit was confirmed and priors remain hand-set. |
| §6.6 output-style directives | deferred | spec §6.6; plan "Out of M4" | Moved to M5. |
| E3 not wired into the harness (§10.6, §14) | matches | `run.py` uses `ope.tau_for` only (no off-policy estimate) | §14 M4 line now reads "E1 (E3 informational only)". |
| E1 numbers never committed; private reports 0600 outside git | matches | `replay/report.py` `write_private`; `scripts/check_denylist.py` | |
| `CLAUDE.md` invariants touched by M4 | matches | `token_sparen/mechanisms/` (no adapter import), `evals/challengers/` (not imported by the package), `config.py` `PROJECT_POLICY` | Error-line invariant states the frame-head rule. |
| README documents every replay lane, the pre-trim rule and the harness flags | matches | `README.md` "Measuring it on your own sessions", configuration table; `token_sparen/cli.py` `--lanes` | `prune` and `sim` lanes now described. |
| Post-emit work runs "after the response reached the host" | diverges | spec §6.11; `token_sparen/deferred.py` module docstring; `adapters/claude/entry.py` | See Divergence 1. |
| `loopguard.on_prompt` docstring ordering | diverges | `mechanisms/loopguard.py` `on_prompt` | See Divergence 2. |

## Divergences

1. **Post-emit timing wording (spec and docstrings vs host behaviour).** Spec §6.11, `CLAUDE.md`
   and `token_sparen/deferred.py` say deferred work runs after the response reached the host.
   Claude Code reads hook output when the process exits, so the work still counts toward the
   latency the host sees and runs under the armed watchdog. Correctness holds (a watchdog no-op
   drops the queued work; never a record without delivery). The docs should say "after the
   response is written"; whether the watchdog gets a separate budget is a code question.
   Deferred minor from the cold review.
2. **Stale handler-order docstring (code).** `loopguard.on_prompt` still says it runs "after the
   turn is counted"; it now runs first in the always-run prompt chain. Code comment only; the
   behaviour matches the spec and the tests. Deferred minor from the cold review.

Divergences closed in this wave (documentation only): the `--quiet` status line, the Task 2
cutoff interface, the unmeasured keys and the objective text in the plan; spec §7.2, §7.5, §7.9
and §14; README `prune` and `sim` lanes, the pre-trim max rule and the harness flags;
`evals/hillclimb/README.md` (ledger, dev-report dates, per-arm check, seeds). The cutoff default
itself was aligned in code by the fix wave.

## Review history

- **Plan review:** a cold goldfish review of the M4 plan before any code; its blockers and majors
  were ruled on and the plan revised.
- **Per-task reviews:** each implementation task got a spec-compliance and quality review, with
  fix rounds until clean; the real-data campaign task was controller-run and its rulings
  recorded in `MEMORY.md` without figures.
- **Cold review of the M4 diff:** 0 blockers / 5 majors. All majors were fixed in a final wave:
  per-arm confirmation, the live re-send reward with several seeds, the pre-trim keep-ratio rule,
  the campaign flags with one ledger of used test windows, and the corrected outcome text. Two
  re-review rounds closed the majors and the two issues the first round raised.

## Summary

matches 42, diverges 2, missing 0, extra 0, deferred 4.
