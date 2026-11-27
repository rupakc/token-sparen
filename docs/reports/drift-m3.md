# Drift report: M3

Date: 2026-11-27

## Scope

Milestone M3 (cross-session learning) against `docs/specs/2026-11-25-token-sparen-design.md`
§6.8 (repository facts), §6.9 (fact-injection security), §6.10 (tool pruning), §6.11
(failure-loop guard) and §7.5-§7.7 as they apply to M3, after the M3 review fix waves and the
in-place synchronisation of those spec bodies. Code: `token_sparen/facts/`,
`token_sparen/mechanisms/prune.py`, `token_sparen/mechanisms/loopguard.py`,
`token_sparen/adapters/claude/settings.py`, `token_sparen/adapters/claude/entry.py`,
`token_sparen/learn/engine.py`, `token_sparen/learn/hierarchy.py`, `PROJECT_POLICY` and
`env_path` in `token_sparen/config.py`, `token_sparen/params/defaults.toml`, and `tests/`. Also
checked: the `CLAUDE.md` invariants and the M3 claims in `README.md`.

Status key: matches / diverges / missing / extra; **deferred** = the spec itself puts the item
outside v1.0, so no code is expected. Evidence names the file and the function, class, constant
or README section (stable across edits); tests are named by function (`tests/` mirrors the
package).

## Requirements

| Requirement | Status | Evidence | Note |
|---|---|---|---|
| 6.8 Deterministic miners at fold time only, over the session transcript (<= 50 MB) | matches | `token_sparen/facts/miners.py` `mine_calls`, `MAX_TRANSCRIPT_BYTES`; `token_sparen/learn/overlay.py` `_mine_facts` | Five fixed detectors; `tests/facts/test_miners.py`, `tests/facts/test_fold_mining.py` |
| 6.8 A fact needs evidence from >= 2 distinct sessions (session-id digests) | matches | `token_sparen/facts/miners.py` `MIN_SESSIONS`, `record`; `token_sparen/facts/lifecycle.py` `_transition` | |
| 6.8 `generated_dir` only with positive evidence (git-ignored or generated marker in first 5 lines); edits alone never | matches | `token_sparen/facts/miners.py` `generated_evidence`, `GEN_MARKER`, `GEN_MARKER_LINES` | `test_generated_dir_never_from_edits_alone`, `test_generated_dir_from_a_git_ignored_dir`, `test_generated_dir_from_generated_markers`, `test_generated_dir_without_git_is_no_candidate` |
| 6.8 `generated_dir` contradicted when git says not ignored and a file lacks the marker; legacy unverified facts retired; <= 3 re-checks per fold | matches | `token_sparen/facts/lifecycle.py` `apply_session`, `Observer`, `GEN_RECHECKS`, `recheck_order` | `test_edit_under_a_tracked_marker_less_dir_contradicts_generated_dir`, `test_generated_dir_without_git_is_never_contradicted`, `test_legacy_generated_dir_without_evidence_is_retired_without_git` |
| 6.8 `test_command` = runner invocation only; scoped / `cd` / config-root runs give no fact; watch-mode stoppers kept | matches | `token_sparen/facts/miners.py` `test_invocation`, `TEST_RUNNERS`, `SCOPE_FLAGS`, `KEEP_FLAGS`, `KEEP_FLAGS_BY` | `test_test_invocation_is_the_runner_only`, `test_test_command_fact_is_the_runner_invocation` |
| 6.8 `build_first` only when the build is the call immediately before the passing test; checkers are not builds | matches | `token_sparen/facts/miners.py` `_Miner.feed` (`prev` reset on any other call), `checker` | `test_build_first_is_not_credited_across_an_edit_or_to_a_checker` |
| 6.8 In-project absolute paths made project-relative; outside paths rejected | matches | `token_sparen/facts/miners.py` `relativise`; `token_sparen/facts/sanitize.py` `_paths_ok` | `test_in_project_absolute_paths_are_relativised_and_stored`, `test_relativise_only_touches_in_project_paths` |
| 6.8(b) Optional LLM proposer | deferred | none | Spec body now marks it deferred (not in v1.0); README says the mechanisms use no LLM |
| 6.8 Lifecycle: `effect` (injected only), withheld `baseline` via propensity floor, separate `truth` for activation | matches | `token_sparen/facts/lifecycle.py` `effect`, `baseline`, `truth`, `withhold`, `apply_session` | `test_withhold_floor_observes_counterfactual`, `test_confirmations_count_as_truth_never_as_effect` |
| 6.8 Activation: lcb rule or 3-session bootstrap; frequency kinds need 5 sessions and a waste event; one active test command | matches | `token_sparen/facts/lifecycle.py` `_transition`, `BOOTSTRAP_SESSIONS`, `FREQUENCY_SESSIONS`, `_exclusive` | `test_activation_after_confirmations`, `test_frequency_kinds_need_five_sessions_and_an_observed_waste_event` |
| 6.8 Retirement: contradiction (1 for high-stakes, else 2), unneeded, `ucb < retire_ucb`, baseline, 8-session effect horizon, staleness | matches | `token_sparen/facts/lifecycle.py` `_transition`, `HIGH_STAKES`, `RETIRE_AFTER`, `EFFECT_HORIZON`, `STALE_SESSIONS` | `test_single_contradiction_retires_high_stakes`, `test_retire_when_ucb_below_threshold`, `test_effect_ucb_retires_after_eight_injected_relevant_sessions` |
| 6.8 Injection at SessionStart (startup, resume deduplicated, clear, compact), <= 8 facts / 300 tokens, recency-ranked, via emit gate | matches | `token_sparen/facts/inject.py` `on_session_start`, `build`, `SOURCES`; `token_sparen/facts/lifecycle.py` `select` | `test_block_budget_and_wrapper_exact`, `test_resume_offers_only_facts_not_yet_shown`, `test_select_ranks_active_by_recent_evidence` |
| 6.8 `[facts] enabled` default true; a project may only disable or lower the budget | matches | `token_sparen/config.py` `PROJECT_POLICY` (`facts` entries); `token_sparen/params/defaults.toml` `[facts]` | `test_project_config_may_only_narrow_facts` |
| 6.9 Data, not instructions: allow-listed tokens, no URLs / metacharacters / instruction words / secrets / outside paths, checked when mined and rendered | matches | `token_sparen/facts/sanitize.py` `sanitize`, `render` | `test_sanitize_rejects_injection`, `test_sanitize_rejects_secret_like_values`, `test_sanitize_rejects_paths_outside_project` |
| 6.9 Fixed per-kind past-tense templates inside a labelled data block | matches | `token_sparen/facts/sanitize.py` `TEMPLATES`; `token_sparen/facts/inject.py` `OPEN`, `DATA_LINE` | `test_block_skips_unsafe_facts_by_resanitising` |
| 6.10 Opt-in only via `prune enable` or the user's own `config.toml`; project may only set `allow = false` or lower `lease_sessions` | matches | `token_sparen/mechanisms/prune.py` `settings`, `settings_for`, `enable`; `token_sparen/config.py` `PROJECT_POLICY` (`prune` entries) | `test_user_config_enables_and_project_may_disable`, `test_m6_project_may_only_lower_the_lease` |
| 6.10 Candidates: whole MCP servers; built-ins only with user-only `builtins` and an SDK init listing | matches | `token_sparen/mechanisms/prune.py` `candidate_names`, `is_candidate` | `test_default_scope_is_mcp_servers_only`, `test_builtins_only_when_always_loaded`, `test_mcp_server_level_candidates` |
| 6.10 Own Beta per candidate, `ucb(0.95) <= 0.01` plus `min_sessions` / `min_prompts`; no Thompson exploration | matches | `token_sparen/mechanisms/prune.py` `PRIOR`, `observe_session`, `ucb`, `ready`, `plan` | Spec 6.10 / 7.5 now state pruning is not an engine point |
| 6.10 Protected tools never pruned (core tools, `Task*`, anything used or restored once) | matches | `token_sparen/mechanisms/prune.py` `PROTECTED`, `PROTECTED_PREFIXES`, `is_protected` | `test_protected_never_pruned` |
| 6.10 New rules written only by the detached session-end fold, after the idle gate; SessionStart writes nothing | matches | `token_sparen/adapters/claude/entry.py` `_on_fold_spawn`; `token_sparen/mechanisms/prune.py` `fold_session`, `_idle`; `token_sparen/mechanisms/tracking.py` `others_active` | `test_session_start_never_writes_rules_nor_spawns_a_writer`, `test_only_the_session_end_fold_applies`, `test_idle_excludes_the_session_being_folded`, `test_hook_restore_never_waits_on_git_during_a_fold` |
| 6.10 Lease: no evidence while pruned; restored after `lease_sessions`; re-prune only on fresh evidence | matches | `token_sparen/mechanisms/prune.py` `observe_session`, `_end_leases`, `_fresh_ok` | `test_m6_pruned_rules_accrue_no_evidence`, `test_m6_lease_restores_then_needs_fresh_evidence`, `test_m6_tool_used_after_the_lease_is_kept_for_good` |
| 6.10 In-session recovery: SessionStart line with `token-sparen prune restore <tool>`; attempted use, mention, user deletion or transcript attempt restores at once, regret 1.0, kept for good | matches | `token_sparen/mechanisms/prune.py` `notice_line`, `on_tool`, `on_tool_search`, `on_prompt`, `_check_owned`, `_regret` | `test_restore_on_attempted_use`, `test_restore_on_user_mention`, `test_restore_on_tool_search_and_failures`, `test_restore_on_permission_denied`, `test_fold_restores_after_an_attempt_seen_in_the_transcript`, `test_rule_removed_by_the_user_counts_as_a_restore` |
| 6.10 Restore message makes no mid-session availability claim | matches | `token_sparen/mechanisms/prune.py` `_restored_message` | `test_restore_message_makes_no_mid_session_claim` |
| 6.10 Claude Code writer: deny rules in `.claude/settings.local.json`, written only on positive git confirmation; symlinks and non-JSON refused; backup, atomic write, exact revert | matches | `token_sparen/adapters/claude/settings.py` `git_exposure`, `add_deny_rules`, `_linked`, `_load_stamped`, `remove_deny_rules`, `BACKUP_SUFFIX` | `test_writes_only_owned_rules_and_backup`, `test_restore_cli_exact_revert`, `test_any_unexpected_git_exit_means_unknown`, `test_git_exposure_probes`, `test_unignored_or_non_git_checkout_is_never_written`, `test_symlinked_settings_file_is_never_written` |
| 6.10 Crash safety: journal before the settings write, reconcile on every read | matches | `token_sparen/mechanisms/prune.py` `_apply`, `reconcile` | `test_b1_kill_after_settings_write_is_owned_next_run`, `test_b1_kill_after_settings_write_then_disable`, `test_b1_a_rule_already_present_is_never_journaled` |
| 6.10 User-level ownership registry `prune_rules.json` keyed by settings-file real path; remote change never orphans a rule; opt-in not carried across keys | matches | `token_sparen/mechanisms/prune.py` `REGISTRY_FILE`, `registry_path`, `attach`, `_revert_policy`, `_foreign_key_record` | `test_remote_change_never_orphans_a_rule`, `test_legacy_per_project_records_are_migrated`, `test_remote_change_restores_at_the_next_fold`, `test_remote_change_with_user_wide_opt_in_keeps_the_lease` |
| 6.10 Other hosts (Codex, Gemini/Qwen, OpenCode) | deferred | `token_sparen/mechanisms/prune.py` `DenyWriter` | Spec body now scopes them to v1.1 (§14); the host-neutral `DenyWriter` protocol is the extension point |
| 6.11 Repeated identical error per agent and turn; no fingerprint for a bare exit code; no burst trigger | matches | `token_sparen/mechanisms/loopguard.py` `fingerprint`, `_agent_id`, `on_tool` | `test_uninformative_failure_has_no_fingerprint`, `test_agents_are_counted_separately` |
| 6.11 Edit oscillation at `max(2, k - 1)` reversals; one add-then-remove never counts | matches | `token_sparen/mechanisms/loopguard.py` `MIN_REVERSALS`, `_oscillation` | `test_debug_line_add_and_remove_is_not_oscillation`, `test_edit_oscillation`, `test_edit_oscillation_patient_waits`, `test_write_oscillation` |
| 6.11 Learned threshold patient / standard / eager = 5 / 3 / 2, floored by `min_k` | matches | `token_sparen/learn/policy.py` `LOOPGUARD`; `token_sparen/mechanisms/loopguard.py` `ARM_K`, `_k` | Project may only raise `min_k` (`token_sparen/config.py` `PROJECT_POLICY`, `loopguard` entries) |
| 6.11 One redirect as `additionalContext` per agent and turn; output untouched; second detection tells the user only | matches | `token_sparen/mechanisms/loopguard.py` `NUDGE`, `OSC_NUDGE`, `USER_NOTICE`, `_evaluate` | `test_one_redirect_per_turn`, `test_second_trigger_user_only`, `test_failure_hook_delivers_additional_context`, `test_post_tool_output_untouched` |
| 6.11 Reward: delivered redirects only (Bernoulli "broken" at >= 3; eager@2 / oscillation regret only; undelivered forgotten); stop-phrase rule | matches | `token_sparen/mechanisms/loopguard.py` `_pay`, `SAVINGS_MIN_N`, `is_stop`; `token_sparen/learn/engine.py` `Engine.forget` | `test_eager_nudge_at_two_observes_regret_only`, `test_undelivered_decision_adds_no_evidence`, `test_oscillation_redirect_observes_regret_only`, `test_stop_phrase_counts`, `test_stop_word_inside_a_longer_prompt_does_not_count` |
| 6.11 Error-loop trigger calls `RegretDetector.on_loop`; no LLM judge | matches | `token_sparen/mechanisms/loopguard.py` module docstring; `token_sparen/learn/regret.py` `on_loop` | `test_loop_calls_regret_on_loop` |
| 7.5 Natural break rate: own Beta (0.8 / 10), per bucket and pooled, global fallback below 5 observations | matches | `token_sparen/learn/hierarchy.py` `NATURAL_PRIOR_MEAN`, `NATURAL_PRIOR_STRENGTH`, `NATURAL_MIN_OBS`, `StoreChain.natural_posterior` | `test_natural_posterior_prior_project_and_global`, `test_natural_legacy_flat_entry_reads_as_pooled`, `test_natural_break_observed_without_nudge`, `test_natural_rate_of_an_unlearned_bucket_is_pooled_only` |
| 7.5 Natural-rate gate: lcb_0.9(arm) > bar (bucket ucb_0.9 at >= 20 obs, else max with pooled) and sampled regret <= tau | matches | `token_sparen/learn/engine.py` `Engine._arms`, `GATE_Q`; `token_sparen/learn/hierarchy.py` `StoreChain.natural_bar`, `NATURAL_BUCKET_OBS` | `test_loopguard_null_effect_stays_patient`, `test_loopguard_null_effect_per_category_stays_patient`, `test_natural_bar_per_bucket_never_looser_than_pooled` |
| 7.5 Facts and pruning use their own constrained Betas, outside the engine | matches | `token_sparen/facts/lifecycle.py` module docstring; `token_sparen/mechanisms/prune.py` `PRIOR`, `ucb` | Previously a spec divergence; spec 7.5 now says so |
| 7.6 Gated point: floor explores the ladder's intervening arms (cold: `standard`); exact propensities; hold explores nothing | matches | `token_sparen/learn/engine.py` `Engine._ladder`, `Engine._propensity`, `Engine.choose` | `test_loopguard_cold_gate_explores_by_floor_only` |
| 7.6 Circuit breaker on learned engine points (loop guard included); pruning's breaker equivalent is restore-for-good | matches | `token_sparen/learn/breaker.py` module docstring; `token_sparen/mechanisms/prune.py` `_regret`, `is_protected` | Spec 7.6 now states the pruning equivalent |
| 7.7 Fingerprint = tool + target + normalised error line (or failing-test-set hash), exact by design | matches | `token_sparen/mechanisms/loopguard.py` `fingerprint`, `_normalise`, `command_head` | `test_fingerprint_normalises_numbers_and_paths`, `test_fingerprint_keeps_distinct_failures_apart`, `test_fingerprint_mixes_tool_and_command_head`; spec no longer says "token shingles" |
| CLAUDE.md: mechanisms never import `token_sparen.adapters` | matches | `token_sparen/mechanisms/prune.py` `register_writer` | Writer injected; no adapter import under `mechanisms/` or `facts/` |
| CLAUDE.md: project file deny-by-default; every default key has a policy | matches | `token_sparen/config.py` `PROJECT_POLICY` | `test_every_default_key_has_a_project_policy` |
| CLAUDE.md: non-LLM hook p95 < 50 ms (incl. pruning enabled with a full store) | matches | `tests/perf/test_prune_cost.py`, `tests/perf/test_loopguard_cost.py`, `tests/perf/test_facts_inject_cost.py` | Previously missing for enabled pruning; added in fix wave A |
| CLAUDE.md: `TOKEN_SPAREN_HOME` override only outside the project (file identity) and owned by the user, or a not-yet-created path under a root-owned sticky dir such as /tmp | matches | `token_sparen/config.py` `env_path`, `_owned`, `_project_roots`; `token_sparen/assets/statusline.sh` `trusted` | `tests/test_env_paths.py` (`test_home_inside_project_dir_is_ignored`, `test_symlink_into_the_project_is_ignored`, `test_home_owned_by_someone_else_is_ignored`, `test_case_and_dotdot_spellings_cannot_bypass`) |
| CLAUDE.md: settings writes opt-in, backed up, reversible, disclosed | matches | `token_sparen/adapters/claude/settings.py` `add_deny_rules`, `BACKUP_SUFFIX`; `README.md` "What it writes to disk" (the `settings.local.json` and `prune_rules.json` entries) and "Tool pruning (opt-in)" ("What it writes") | Disclosure lists `prune_rules.json`, `facts.json`, loop-guard state, natural break-rate counts and the `.token-sparen.bak` backup |
| README: defaults table covers `[facts]`, `[loopguard]` and `[prune]` | matches | `README.md` "Configuration" (defaults table); `token_sparen/params/defaults.toml` `[facts]`, `[prune]`, `[loopguard]` | Values agree with `defaults.toml` |
| README: project-config table matches `PROJECT_POLICY` for M3 keys | matches | `README.md` "Configuration" ("What a project file may change" table); `token_sparen/config.py` `PROJECT_POLICY` | `facts.enabled`/`loopguard.enabled` off only; `loopguard.min_k` raise only; `prune.allow` false only; `facts.max_*`, `prune.lease_sessions` lower only; rest user-only |

Counts: 46 requirements checked: 44 matches, 0 diverges, 0 missing, 0 extra, 2 deferred.

## Divergences

No open divergence between the M3 code and the synchronised spec or README. The pre-fix
drift check found eight items; their resolution:

- **LLM fact proposer (6.8(b)) missing**: resolved in the spec. Marked deferred; v1.0 ships the
  deterministic miners only. If built later, off by default and user-only.
- **Pruning for other hosts (6.10) missing**: resolved in the spec. Scoped to v1.1, matching §14;
  the `DenyWriter` protocol keeps the mechanism host-neutral.
- **"Token shingles" (7.7)**: resolved in the spec. Exact fingerprints are the intended property,
  so distinct errors never merge.
- **Pruning and facts outside the engine (7.5 / 7.6)**: resolved in the spec. Pruning is a
  settings write, not a per-call arm; its safety is the opt-in, the `ucb` gate, the idle gate,
  the lease and restore-for-good.
- **Extra pruning safety behaviour (6.10)**: resolved in the spec body (opt-in rules, git
  confirmation, symlink refusal, registry, `prune status`).
- **Spec body stale against its revision log (6.8, 6.10, 6.11)**: resolved; the bodies of
  6.8-6.11 and 7.5-7.7 now describe the code.
- **No latency test for pruning hooks when enabled**: resolved in code (perf test added).
- **README project-config and defaults tables**: resolved in the README.

Accepted trade-offs, documented in the spec rather than changed (all in the
"intervene less" direction or bounded):

- Floor exploration of the loop guard ignores the regret posterior; it is bounded by the 5 %
  floor and the circuit breaker still stops an arm users keep rejecting.
- The natural break rate is observed mostly on `patient` turns and held or static sessions; the
  same rate is the bar for redirects at any k.
- New pruning rules take effect one session later (written only by the session-end fold).

## Review history

A cold review of the M3 milestone found 1 blocker, 6 major and 9 minor issues. The blocker and
all major issues were fixed in three sequential waves (tool pruning; repository facts; loop
guard, environment paths and documentation), each with its own review rounds until clean. Some
minor issues were deferred as low-risk: narrow timing and locking windows in pruning, a few
edge cases in fact mining (unusual runner flags, marker matching, retired-entry housekeeping), a
user-facing notice when an environment override is ignored, and state-size and command-head
edge cases in the loop guard.
