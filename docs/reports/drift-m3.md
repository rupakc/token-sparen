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
outside v1.0, so no code is expected.

## Requirements

| Requirement | Status | Evidence | Note |
|---|---|---|---|
| 6.8 Deterministic miners at fold time only, over the session transcript (<= 50 MB) | matches | `token_sparen/facts/miners.py:792`, `token_sparen/learn/overlay.py:521` | Five fixed detectors; `tests/facts/test_miners.py` |
| 6.8 A fact needs evidence from >= 2 distinct sessions (session-id digests) | matches | `token_sparen/facts/miners.py:892`, `token_sparen/facts/lifecycle.py:345` | `MIN_SESSIONS = 2` |
| 6.8 `generated_dir` only with positive evidence (git-ignored or generated marker in first 5 lines); edits alone never | matches | `token_sparen/facts/miners.py:597` | `tests/facts/test_miners.py:476`, `:484`, `:502`, `:525` |
| 6.8 `generated_dir` contradicted when git says not ignored and a file lacks the marker; legacy unverified facts retired; <= 3 re-checks per fold | matches | `token_sparen/facts/lifecycle.py:278`, `:116`, `:655` | `tests/facts/test_lifecycle.py:790`, `:829`, `:855` |
| 6.8 `test_command` = runner invocation only; scoped / `cd` / config-root runs give no fact; watch-mode stoppers kept | matches | `token_sparen/facts/miners.py:327`, `:365`, `:402`, `:432` | `tests/facts/test_miners.py:634`, `:650` |
| 6.8 `build_first` only when the build is the call immediately before the passing test; checkers are not builds | matches | `token_sparen/facts/miners.py:522` | `tests/facts/test_miners.py:726` |
| 6.8 In-project absolute paths made project-relative; outside paths rejected | matches | `token_sparen/facts/miners.py:780` | `tests/facts/test_miners.py:741`, `:757` |
| 6.8(b) Optional LLM proposer | deferred | none | Spec body now marks it deferred (not in v1.0); README says the mechanisms use no LLM |
| 6.8 Lifecycle: `effect` (injected only), withheld `baseline` via propensity floor, separate `truth` for activation | matches | `token_sparen/facts/lifecycle.py:176`, `:181`, `:204`, `:378` | `tests/facts/test_lifecycle.py:158`, `:720` |
| 6.8 Activation: lcb rule or 3-session bootstrap; frequency kinds need 5 sessions and a waste event; one active test command | matches | `token_sparen/facts/lifecycle.py:321`, `:301` | `tests/facts/test_lifecycle.py:69`, `:87` |
| 6.8 Retirement: contradiction (1 for high-stakes, else 2), unneeded, `ucb < retire_ucb`, baseline, 8-session effect horizon, staleness | matches | `token_sparen/facts/lifecycle.py:321`-`:345` | `tests/facts/test_lifecycle.py:228`, `:252`, `:731` |
| 6.8 Injection at SessionStart (startup, resume deduplicated, clear, compact), <= 8 facts / 300 tokens, recency-ranked, via emit gate | matches | `token_sparen/facts/inject.py:74`, `token_sparen/facts/lifecycle.py:439` | `tests/facts/test_inject.py:28`, `:248`; `tests/facts/test_lifecycle.py:190` |
| 6.8 `[facts] enabled` default true; a project may only disable or lower the budget | matches | `token_sparen/config.py:354`-`:357`, `token_sparen/params/defaults.toml:270` | `tests/facts/test_inject.py:170` |
| 6.9 Data, not instructions: allow-listed tokens, no URLs / metacharacters / instruction words / secrets / outside paths, checked when mined and rendered | matches | `token_sparen/facts/sanitize.py:258`, `:275` | `tests/facts/test_sanitize.py:52`, `:68`, `:84` |
| 6.9 Fixed per-kind past-tense templates inside a labelled data block | matches | `token_sparen/facts/sanitize.py:117`, `token_sparen/facts/inject.py:45` | `tests/facts/test_inject.py:48` |
| 6.10 Opt-in only via `prune enable` or the user's own `config.toml`; project may only set `allow = false` or lower `lease_sessions` | matches | `token_sparen/mechanisms/prune.py:219`, `token_sparen/config.py:358`-`:360` | `tests/mechanisms/test_prune.py:414`, `:1220` |
| 6.10 Candidates: whole MCP servers; built-ins only with user-only `builtins` and an SDK init listing | matches | `token_sparen/mechanisms/prune.py:259`, `:300` | `tests/mechanisms/test_prune.py:438`, `:446`, `:453` |
| 6.10 Own Beta per candidate, `ucb(0.95) <= 0.01` plus `min_sessions` / `min_prompts`; no Thompson exploration | matches | `token_sparen/mechanisms/prune.py:387`, `:417`, `:429` | Spec 6.10 / 7.5 now state pruning is not an engine point |
| 6.10 Protected tools never pruned (core tools, `Task*`, anything used or restored once) | matches | `token_sparen/mechanisms/prune.py:294` | `tests/mechanisms/test_prune.py:250` |
| 6.10 New rules written only by the detached session-end fold, after the idle gate; SessionStart writes nothing | matches | `token_sparen/adapters/claude/entry.py:218`, `token_sparen/mechanisms/prune.py:704`, `:937` | `tests/mechanisms/test_prune.py:1087`, `:1103`, `:1129`, `:1152` |
| 6.10 Lease: no evidence while pruned; restored after `lease_sessions`; re-prune only on fresh evidence | matches | `token_sparen/mechanisms/prune.py:421`, `:464`, `:884` | `tests/mechanisms/test_prune.py:1175`, `:1185`, `:1207` |
| 6.10 In-session recovery: SessionStart line with `token-sparen prune restore <tool>`; attempted use, mention, user deletion or transcript attempt restores at once, regret 1.0, kept for good | matches | `token_sparen/mechanisms/prune.py:1265`, `:1285`, `:1302`, `:1315`, `:914` | `tests/mechanisms/test_prune.py:367`, `:561`, `:573`, `:591`, `:598`, `:609` |
| 6.10 Restore message makes no mid-session availability claim | matches | `token_sparen/mechanisms/prune.py:1249` | `tests/mechanisms/test_prune.py:1333` |
| 6.10 Claude Code writer: deny rules in `.claude/settings.local.json`, written only on positive git confirmation; symlinks and non-JSON refused; backup, atomic write, exact revert | matches | `token_sparen/adapters/claude/settings.py:434`, `:470`, `:524`, `:529` | `tests/mechanisms/test_prune.py:336`, `:385`, `:1231`, `:1252`, `:1271`, `:1296`, `:1454` |
| 6.10 Crash safety: journal before the settings write, reconcile on every read | matches | `token_sparen/mechanisms/prune.py:663`, `:804` | `tests/mechanisms/test_prune.py:1032`, `:1046`, `:1060`, `:1069` |
| 6.10 User-level ownership registry `prune_rules.json` keyed by settings-file real path; remote change never orphans a rule; opt-in not carried across keys | matches | `token_sparen/mechanisms/prune.py:558`, `:571`, `:750`, `:779` | `tests/mechanisms/test_prune.py:1344`, `:1376`, `:1424`, `:1440` |
| 6.10 Other hosts (Codex, Gemini/Qwen, OpenCode) | deferred | `token_sparen/mechanisms/prune.py:151` | Spec body now scopes them to v1.1 (§14); the host-neutral `DenyWriter` protocol is the extension point |
| 6.11 Repeated identical error per agent and turn; no fingerprint for a bare exit code; no burst trigger | matches | `token_sparen/mechanisms/loopguard.py:219`, `:341` | `tests/mechanisms/test_loopguard.py:228`, `:532` |
| 6.11 Edit oscillation at `max(2, k - 1)` reversals; one add-then-remove never counts | matches | `token_sparen/mechanisms/loopguard.py:99`, `:440` | `tests/mechanisms/test_loopguard.py:578`, `:587`, `:599`, `:616` |
| 6.11 Learned threshold patient / standard / eager = 5 / 3 / 2, floored by `min_k` | matches | `token_sparen/mechanisms/loopguard.py:307`, `token_sparen/learn/policy.py:77` | Project may only raise `min_k` (`token_sparen/config.py:363`) |
| 6.11 One redirect as `additionalContext` per agent and turn; output untouched; second detection tells the user only | matches | `token_sparen/mechanisms/loopguard.py:109`, `:482` | `tests/mechanisms/test_loopguard.py:459`, `:470`, `:813`, `:845` |
| 6.11 Reward: delivered redirects only (Bernoulli "broken" at >= 3; eager@2 / oscillation regret only; undelivered forgotten); stop-phrase rule | matches | `token_sparen/mechanisms/loopguard.py:281`, `:580`, `token_sparen/learn/engine.py:398` | `tests/mechanisms/test_loopguard.py:648`, `:663`, `:921`, `:951`, `:965` |
| 6.11 Error-loop trigger calls `RegretDetector.on_loop`; no LLM judge | matches | `token_sparen/mechanisms/loopguard.py:1` | `tests/mechanisms/test_loopguard.py:747` |
| 7.5 Natural break rate: own Beta (0.8 / 10), per bucket and pooled, global fallback below 5 observations | matches | `token_sparen/learn/hierarchy.py:61`-`:65`, `:172` | `tests/learn/test_hierarchy.py:512`, `:568`; `tests/mechanisms/test_loopguard.py:873`, `:969` |
| 7.5 Natural-rate gate: lcb_0.9(arm) > bar (bucket ucb_0.9 at >= 20 obs, else max with pooled) and sampled regret <= tau | matches | `token_sparen/learn/engine.py:257`, `token_sparen/learn/hierarchy.py:185` | `tests/learn/test_engine.py:581`, `:657`; `tests/learn/test_hierarchy.py:530` |
| 7.5 Facts and pruning use their own constrained Betas, outside the engine | matches | `token_sparen/facts/lifecycle.py:1`, `token_sparen/mechanisms/prune.py:387` | Previously a spec divergence; spec 7.5 now says so |
| 7.6 Gated point: floor explores the ladder's intervening arms (cold: `standard`); exact propensities; hold explores nothing | matches | `token_sparen/learn/engine.py:192`, `:287`, `:329` | `tests/learn/test_engine.py:598` |
| 7.6 Circuit breaker on learned engine points (loop guard included); pruning's breaker equivalent is restore-for-good | matches | `token_sparen/learn/breaker.py:1`, `token_sparen/mechanisms/prune.py:744` | Spec 7.6 now states the pruning equivalent |
| 7.7 Fingerprint = tool + target + normalised error line (or failing-test-set hash), exact by design | matches | `token_sparen/mechanisms/loopguard.py:174`, `:219` | `tests/mechanisms/test_loopguard.py:154`, `:196`, `:210`; spec no longer says "token shingles" |
| CLAUDE.md: mechanisms never import `token_sparen.adapters` | matches | `token_sparen/mechanisms/prune.py:189` | Writer injected via `register_writer` |
| CLAUDE.md: project file deny-by-default; every default key has a policy | matches | `token_sparen/config.py:338` | `tests/test_config_safety.py:239` |
| CLAUDE.md: non-LLM hook p95 < 50 ms (incl. pruning enabled with a full store) | matches | `tests/perf/test_prune_cost.py:65`, `tests/perf/test_loopguard_cost.py:30`, `tests/perf/test_facts_inject_cost.py:24` | Previously missing for enabled pruning; added in fix wave A |
| CLAUDE.md: `TOKEN_SPAREN_HOME` override only outside the project (file identity) and owned by the user | matches | `token_sparen/config.py:130`, `token_sparen/assets/statusline.sh:77` | `tests/test_env_paths.py:47`, `:72`, `:90`, `:127` |
| CLAUDE.md: settings writes opt-in, backed up, reversible, disclosed | matches | `token_sparen/adapters/claude/settings.py:470`, `README.md:211` | Disk disclosure lists `prune_rules.json`, `facts.json`, loop-guard state, natural break-rate counts and the `.token-sparen.bak` backup |
| README: defaults table covers `[facts]`, `[loopguard]` and `[prune]` | matches | `README.md:367`-`:379`, `token_sparen/params/defaults.toml:270`-`:304` | Values agree with `defaults.toml` |
| README: project-config table matches `PROJECT_POLICY` for M3 keys | matches | `README.md:341`-`:349`, `token_sparen/config.py:354`-`:364` | `facts.enabled`/`loopguard.enabled` off only; `loopguard.min_k` raise only; `prune.allow` false only; `facts.max_*`, `prune.lease_sessions` lower only; rest user-only |

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
