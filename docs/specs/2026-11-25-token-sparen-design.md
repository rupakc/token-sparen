# token-sparen — Design Specification

Status: APPROVED 2026-11-25 (with §6.12 live statusline added at approval) · Date: 2026-11-25 · Owner: Rupak Chakraborty · License: MIT

> "sparen" (German): *to save, to economise*. token-sparen makes autonomous coding agents spend
> fewer tokens **without making their work worse**.

## Revision log

**2026-11-26 (after M1).** Synced with the decisions recorded in `MEMORY.md` and the shipped code.
Changes: §3 C1 (Python >= 3.11); §5 hook events (PostToolUseFailure and SessionEnd instead of
PostToolBatch) and the Event/Action field names; §6.1 never-larger rule, failed tool results,
duplicated-field elision dropped, table type handled by the text compressor; §6.2 pre-trim is a
PostToolUse arm, no command rewriting; §6.5 containment instead of Jaccard, no idle-gap signal;
§6.7 subagent brief is an arm that is off by default, Agent result shape; §6.12 dollar figure,
restore of the recorded original, Windows launcher deferred; §8 Claude row; §9 trust through the
CLI, interface version 1.0, session config layer; §10.6 and §11 defaults path and CI matrix.
Later-milestone items (M2/M3) are unchanged and remain planned.

**2026-11-26 (M2, Task 19 review).** §7.3/§7.4: the stores keep **evidence** (discounted, capped
counts `ea, eb, n` per point/bucket/arm/kind, schema version 2), not posteriors; the posterior is
`prior + evidence`, formed at decision time, so a prior is never folded in for good and the κ
estimator sees data-only means (no feedback loop through its own priors). A parent with
`n >= min_parent_obs` lends `m = (ea + κ0·d)/(ea + eb + κ0)` (its data shrunk toward the designed
default d, κ0 = `kappa_default`); κ is method of moments on sibling data means with the sampling
noise `mean(m_i(1−m_i)/(w_i+1))` subtracted from their variance (≤ 0 → `kappa_max`).

**2026-11-26 (M2, Task 19 review round 2).** §7.4: evidence also keeps the discounted second
moment `es2` (stored `[ea, eb, n, es2]`; a 3-element list means es2 = ea, the Bernoulli case), and
the κ noise term is `mean(v_i/(w_i+1))` with the within-bucket variance
`v_i = es2/w − (ea/w)²`, so fractional savings rewards no longer pin κ at `kappa_max`.

**2026-11-26 (M2, Task 20 review round 1).** Cold-start simulations showed the plain constrained
Thompson rule trims aggressively in fresh buckets (aggressive 44 % of cold decisions, ~7 regret
events per fresh bucket) and the Monte-Carlo propensity is unusable for IPS on rare arms.
- §7.4: every regret prior (designed default and parent derived) has the fixed strength
  `kappa_regret = 20`; savings keep the estimated κ.
- §7.6 safe-exploration ladder: arms are ordered (safe first, then increasingly aggressive). The
  first trimming arm is always eligible; each next one only while every safer one is eligible and
  the next-safer one is *established*: regret evidence n ≥ `ladder_obs = 20` (pooled project +
  session full bucket, or any back-off level in project/global scope, max taken) and its regret
  posterior mean ≤ τ. Ineligible arms are excluded before sampling (deterministic).
- §7.6 high tier is opt-in by design: a high-tier point learns only with
  `[learn.optin] <point> = true` set by the user (user config or env, never a project file, same
  rule as `allow_override`); otherwise the safe arm always (propensity 1). With opt-in the ladder
  and Thompson rule apply plus the gate regret `ucb(0.95) ≤ τ`.
- §7.6/§7.8 exact propensity (replaces Monte-Carlo): with the floor and the deterministic gates
  applied, `p_i = P_i ∫ f_{S_i}(s) Π_{j≠i}[1 − P_j + P_j F_{S_j}(s)] ds` with `P_j = F_{R_j}(τ)`,
  `p_safe = Π_j (1 − P_j)` in the sampling branch, logged `p = floor·[safe] + (1−floor)·p_branch`.
  Integrated in logit space by the trapezoid rule (smooth, no endpoint singularities): Σ p = 1
  within 1e-6, agrees with 100 000-draw frequencies within 0.005. Decision ids are uuid4.
- §7.5 reward bookkeeping: each decision takes at most one savings and one regret reward (a repeat
  is ignored and logged); regret detection must report regret 0 explicitly when its window
  expires (silence is not evidence). `cold_obs` is removed (superseded by `ladder_obs`).

**2026-11-26 (M2, Task 21).** §7.5 regret detection (`learn/regret.py`):
- Re-run exemption: an edit since the decision to a file the output mentions (kept text first,
  then the elided span; matched by path or file name) exempts a re-run; any other edit or a
  state-changing command since the decision lowers a quick re-run to 0.5. Signatures keep the
  effective run directory (hook cwd + leading `cd`s, relative to the project, omitted at the
  root); a match only after stripping a trailing filter pipeline is a partial regret; re-runs also match with a trailing filter pipeline stripped; distinctive
  tokens are stored hashed. Exempting only elided-span files was too narrow (fixing a failure
  the agent could see and re-running is normal; counting it would make every test-fix loop a
  regret). Read de-dupe serves a pointer once per stored read; a regret forgets the read.
- A decision keeps the max of its signals: a full regret (1.0) is paid at once, a partial one
  (0.5) when the window (`regret_window_turns = 6`) expires, which is also when 0 is paid; at
  SessionEnd every open decision is settled. Distinctive tokens in *edit* tool inputs do not
  count (the agent writing a name is not asking for elided content).
- Recovery reads (Read of a recovery file, `cat`/`head`/`tail`/`less`/`sed`/`grep` of its path,
  `token-sparen show`) are never trimmed or de-duplicated again.
- A trimming arm that ended as a no-op gets savings 0 and regret 0 at once; the safe arm nothing.

**2026-11-26 (M2, Task 22).** §7.4 / §7.6 session fold and circuit breaker (`learn/overlay.py`,
`learn/breaker.py`):
- The fold moves session evidence into the project store (sum, then the mean-preserving cap; no
  re-discount) and empties the overlay first, so the engine's project + session sum never counts
  it twice; it is idempotent (per-document applied fold ids). Coarse back-off buckets in the
  project and global stores are aggregates of their children. Global receives the session delta
  x 0.3 (`global_weight`), and entries untouched for 90 days are halved.
- Circuit breaker (learned decisions only; static-policy trims never count): evaluated after
  every 25 new settled regret values of a point's trimming arms, over the last 50 (at least 20);
  with regret sum k of n it trips iff k/n > 2 tau and the `breaker_lcb_q` (0.0005) lower bound
  of Beta(1+k, 1+n-k) exceeds tau. It pauses the point for the project (safe arm, propensity 1)
  for 3 folded sessions with one user message; on re-entry its trimming-arm project evidence is
  scaled by 0.25. Applies to every tier, not only high. Simulated (medium tier, 1000 trims at
  10 per session): ~2 % of runs trip at a true regret rate equal to tau, 100 % at 3 tau; the
  first-proposed q = 0.05 tripped ~64 % at tau.
- Pre-trim candidates are learned at fold time from the decision log (>= 5 trimming
  compressions, mean kept ratio <= 0.4, never a regret).

**2026-11-26 (M2, Task 23).** §7.1 the engine drives every hook decision point
(`learn/hookpolicy.py`): one Engine per hook, built only when a decision point (or a reward) is
reached; `[learn] enabled = false` -> the static arms; an engine build or decide error -> the
static decision for the rest of that hook, logged. Every point uses `features.for_point` buckets;
`ctx_pct` (the `band` feature) comes from the governor's incremental cache (`unk` only when
unknown). §6.7: the subagent brief is decided by the engine but held at `off`
(`[learn.hold] subagent_brief = true`) until the live benchmark; it has no reward signal yet.
`token-sparen run` keeps the static policy (no session lifecycle to settle regret or fold).
A trimming arm that ends as a no-op for any reason (nothing worth trimming, save deadline,
never-larger check) gets savings 0 and regret 0 at once.
Review round 3 supersedes rounds 1-2 below: the **whole** project layer is deny-by-default
(`config.PROJECT_POLICY`, `config.sanitise_project`). A cloned repository's config may only make
token-sparen intervene less or keep more, never trim more, recover less or block more. Every
leaf of `params/defaults.toml` has a kind (completeness test; unlisted keys are user-only):
free (display/advice/timing), user_only, max/min (direction-only numbers within bounds),
true_only (`learn.hold.*`), false_only (mechanism switches), union (`compression.skip_tools`),
enum (`compiler.warnings` toward `keep`); each relative to the trusted value. `learn.enabled` is
user/env/session only (a project disabling learning would fall back to the static standard arm,
ignoring the user's holds/tau/floor). README has the table. Round 4: level `head`/`tail` and
`guards.pretrim_*` are user-only (a larger head/tail displaces prompt-relevant lines); bounds
exclude values readers map to their default (<= 0); free keys are listed one by one;
`save_deadline_ms` and `adapter.latency_budget_ms` are lower-only.
Rounds 1-2 (superseded): the project `token-sparen.toml` layer is sanitised before merging with an
**allowlist** (`config.PROJECT_LEARN_KEYS`, `config.sanitise_project`): the whole `learn` subtree
and `extensions.allow_override` are stripped, then only these leaves are re-added, each only as a
scalar of the right type tighter than the trusted value (defaults + user file + env + session
overrides): `learn.enabled` false; `learn.tau.<tier>` lower; `learn.floor.<tier>` higher;
`learn.hold.<point>` true; `ladder_obs` higher; `breaker_min_obs`, `breaker_every` lower;
`breaker_lcb_q`, `breaker_sessions` higher. Anything else (scalars replacing tables, unknown or
other learn keys, wrong types) is dropped and logged once per key. `breaker_window` and
`breaker_reentry_scale` are user/env/session only (re-entry scaling multiplies savings, regret and
n alike, so neither direction is uniformly safer). Other sections keep normal project precedence. `learn.enabled` is off in any spelling (false, 0, "0", "no", "off").
Accepted fail-open behaviour: an engine or store error falls back to the static policy (the
known-good, E2-tested arms) for that hook, and a corrupt `breaker.json` reads as not tripped.
M4 items: (1) savings reward as the fraction removed (`removed / before`, optionally x
`min(1, resends / R)`) or the counterfactual `removed / max_removed` over the arms, validated on
E1 before any change (the p95-normalised value saturates, so trimming arms look alike);
(2) 1M-context models without the statusline sensor are mis-banded (the governor's window table
falls back to 200k and `band` reads `unk` once a call exceeds it).

**2026-11-26 (M2, Task 24b; owner decision).** §6.1 revised to **quality-first defaults**. An
offline replay of the owner's own sessions (E1, private) showed that trimming file contents
saves little net of recovery re-reads while the agent usually uses those contents. New gate
(`compression.gated`, before the decision point, so nothing is logged or learned for a gated
output): (1) file contents are never trimmed by default: the Read tool (any content type) and
shell commands of the file-dump family (`cat`, `head`, `tail`, `sed -n`, `less`, `bat`, `nl`,
`awk`, `xxd`/`od`, `jq <file>`, `... | xargs cat`, `git show <rev>:<path>`, `git cat-file -p`);
`[compression] trim_file_contents = true` opts in (user/env/session; a project may only set
false). (2) Only these content types are offered to the decision point: diff, search,
test_report, compiler, build_log, json, pplx, log (`[compression] allow_ctypes`; text, table and
stacktrace are not, for lack of evidence; a project may only remove entries, new config kind
`subset`). The command-family classifier moved to `compress/families.py` and is shared by the
gate and E1. E1 `--policy default` replays the gate with the static arm; `engine` is gated too;
`static` and arm names stay ungated. A rerun regret no longer charges a recovery read (its
tokens are already in the baseline); recovery cost is reported per signal.
Fix round 1: the gate's file-dump check looks at **every** part of a command (chains, pipes,
loops, `find -exec`, `bash -c`, `xargs`), counts `sort`/`uniq`/`cut`/`rev`/`strings` on a file,
`python -c`/`node -e` code that opens a file and PowerShell `Get-Content`/`gc`/`type`; tools
named like `read_file`/`get_file`/`view_file`/... (MCP included) are file content. E1 families
keep the first program. Fix round 2: detection lives in `compress/filedump.py` and also unwraps
command substitutions, `eval` and remote/container wrappers, treats stdin from a file as file
content, and covers more dump binaries, archives, cloud `cat`s and interpreters; tool names
with a read verb and a file/document/media/resource/content noun are file content.

**2026-11-26 (M2 close).** Reconciliation of this spec with the shipped M2 code; no behaviour
change. §7.1 reward is `engine.reward(decision_id, savings, regret)` (each optional, clamped to
[0,1], one of each kind per decision; a decision's regret is reported as 0 explicitly when its
window expires). §7.5: the `off` (safe) arm's reward is **defined** (savings 0, regret 0), not
observed; the propensity floor still runs it so its propensity is logged. Savings are **token
counts** (normalised by a running p95); cost-weighted savings are deferred to M4 (E1 reports a
cache-aware cost-weighted figure, but the learner does not use it). §7.9: the designed prior
means in `params/defaults.toml` are **hand-set** until M4 refits them from E1 data. §6.1: keep
ratios are fixed per level (`off`, `light`, `standard`, `aggressive`); the learner chooses the
level per bucket, it does not learn ratios. Deferred to M4: host and model-tier prior-sharing
keys (§7.2) and the LinTS challenger (§7.8). §9: the project config layer is deny-by-default (see
revision round 3 above) and every boolean setting accepts `true/1/yes/on` and `false/0/no/off`.
`[learn.hold] compression = true` also holds the learned pre-trim and applies when learning is off
or the engine fails; `allow_ctypes = []` is the config-only switch. §10: E1/E3 results, aggregates
included, are never committed (C4). E3 constraint semantics are in §10.3. Each settled regret is
appended to `decisions.jsonl` as an outcome line (decision id, regret, signal); the fold backfills
missing ones. `stats` reports net savings (gross minus recovery reads minus injected tokens), as
the statusline does. The plugin manifest requires Claude Code >= 2.1.121 (`updatedToolOutput` on
PostToolUse for all tools).

---

## 1. Intent and why

Coding agents burn most of their tokens on things that do not move the task forward: verbose tool
output re-sent on every turn, re-reading unchanged files, re-learning the same repository facts each
session, loading tool definitions that are never used, and looping on the same failure. Two prior
designs each attack half of this:

- **Reference A — an in-session optimizer** (private prior work by the owner): recoverable trimming of
  tool output, structure-aware compressors, command guards, a session budget governor, and — its
  essence — a **contextual Thompson-sampling bandit with warm priors, regret attribution and
  cross-project posteriors** that learns how aggressively to trim per situation.
- **Reference B — TokenEconomizer** (MIT, arXiv 2610.22732): a
  cross-session optimizer — learned repository facts ("micro-skills"), pruning of never-used tools,
  and failure-loop intervention.

token-sparen is a **from-scratch, open-source** reimplementation that unifies both under **one
learning engine**, fixes the measured weaknesses of each (§6.6, §7.9), and proves its claims with a
reproducible benchmark. No code is copied from either reference; ideas are reimplemented and credited.

**North star:** maximise tokens saved **subject to no measurable loss in output quality**. Quality is
a hard constraint, never a term traded off in a weighted sum.

## 2. Goals, non-goals, anti-goals

**Goals**
1. G1 Reduce billed tokens per completed task substantially (target: ≥25 % median on the live
   benchmark, ≥40 % of re-sent tool-output tokens in offline replay) with **non-inferior quality**
   (§10.5 defines the statistical test).
2. G2 Work natively in **Claude Code, OpenAI Codex CLI, Gemini CLI, Qwen Code, OpenCode**; cover
   DeepSeek/Qwen/other models when run inside those hosts; compress Perplexity `pplx` output when an
   agent calls it.
3. G3 Zero-config install in one command per host; sensible behaviour from the first turn.
4. G4 Adapts to the user's project and habits: within a session, across sessions, within a project
   and across projects (hierarchical learning, §7).
5. G5 Extensible: third parties add compressors, decision points, reward signals and host adapters
   without forking (§9).
6. G6 Robust: hooks fail open, never crash or block the host, bounded latency (p95 < 50 ms per hook
   for non-LLM paths), concurrent-session-safe storage.
7. G7 Transparent and private: every trim is visible and recoverable; all data stays local; no
   telemetry; no network calls except optional, explicitly enabled LLM side-calls.

**Non-goals (v1)**
- Clients without hooks (kardolus `chatgpt-cli`, raw DeepSeek/Perplexity API use) and an API
  compression proxy — listed as Future Work (§15).
- Antigravity CLI (`agy`) — cannot replace tool output today; Future Work.
- Model routing / switching the user's model (changes quality in ways we cannot verify per-task).

**Anti-goals (failure even if it "works")**
- Saving tokens by silently dropping information the agent later needs. Every elision must be
  marked and recoverable in one cheap read.
- A self-reinforcing learner that drifts toward ever more aggressive trimming because its reward is
  rigged (the measured flaw of Reference A, §7.9).
- Headline numbers that do not measure the shipped artefact (the measured flaw of Reference B, §10).
- Any persistent prompt-injection path from repository content into future sessions (§6.9).
- Any reference to the owner's employer or any organisation-specific content.

## 3. Constraints

- C1 Core in **Python ≥ 3.11, standard library only** at runtime (3.10 is end-of-life and
  `tomllib` ships from 3.11; fast hook start-up, no install friction, auditable). Dev dependencies (pytest, ruff, mypy, hypothesis) are fine.
- C2 OpenCode needs a small **TypeScript shim** (npm) that shells out to the Python core.
- C3 Benchmarks run with **Claude Code only**, under the maintainer's **Claude Max subscription** (no API
  keys): rate-limit-aware scheduling across 5-hour windows and weekly caps (§10.4).
- C4 The owner's local sessions may contain confidential material: they are used only for local,
  offline evaluation; **no numbers derived from them are committed or published, aggregates
  included**; no transcript text, paths, ids or project names leave the machine or enter the repo.
- C5 MIT licence, copyright "Rupak Chakraborty". Credit TokenEconomizer (MIT) in README/NOTICE.
  Do not reuse code from non-OSI-licensed competitors (e.g. context-mode, ELv2).
- C6 Anthropic directory rules: hook paths only via `${CLAUDE_PLUGIN_ROOT}`; no unpinned
  `npx`/`uvx`; no non-image file > 256 KiB in the plugin tree; settings writes disclosed and opt-in.

## 4. Approaches considered

| | Approach | Trade-off | Decision |
|---|---|---|---|
| **A** | **One learning engine for every decision point** (trim level, pre-trim, guards, directives, tool pruning, fact injection, loop-guard thresholds), shared context features, honest reward, hierarchical priors session→project→global | Most design work; risky arms (pruning) must start conservative | **Chosen by owner** |
| B | Bandit for trimming only; Reference-B mechanisms rule-based with fixed thresholds | Least risk, fastest; half the system never adapts | Rejected — loses "the essence" |
| C | Linear contextual bandit (LinTS) everywhere | Generalises across contexts but data-hungry, cold start, opaque | Rejected as default; kept as an **offline challenger** promoted only if it wins on held-out replay (§7.8) |

## 5. Architecture

```
            ┌────────────── host agent (Claude Code / Codex / Gemini / Qwen / OpenCode) ──────────────┐
 hook event │ SessionStart · UserPromptSubmit · PreToolUse · PostToolUse · PostToolUseFailure ·       │
            │ PreCompact · SessionEnd                                                                │
            └───────────────┬───────────────────────────────────────────────────────────────────────┘
                            │ stdin JSON (host-specific)
                  ┌─────────▼─────────┐
                  │ adapters/<host>   │  translate ⇄ canonical Event / Action; capability flags
                  └─────────┬─────────┘
                            │ canonical Event
  ┌─────────────────────────▼──────────────────────────────────────────────────────────────┐
  │ core pipeline (token_sparen/)                                                           │
  │  sensors ─▶ context features ─▶ decision points ──decide()──▶ learning engine (§7)      │
  │                                     │                             ▲                      │
  │                                     ▼                             │ rewards / regret     │
  │                   mechanisms: compress · guard · cache · govern · facts · prune · loop  │
  │                                     │                                                    │
  │                    emit gate (dedupe, budget, accounting) ─▶ canonical Action            │
  └─────────────────────────────────────┬──────────────────────────────────────────────────┘
                                        │
             store/ (JSON + JSONL, file-locked)   ~/.token-sparen/{global,projects/<key>/…}
             recovery/ (full outputs on disk)     background `token-sparen fold` after session end
```

- **Canonical event model** (as built): `Event{host, kind, session_id, project_dir, project_key,
  transcript_path, tool{name, category, input}, output_text, raw_response, prompt, model, extra}`
  and `Action{replace_output?, rewrite_input?, block_reason?, inject_context?, user_message?,
  compact_instructions?}` (`compact_instructions` is PreCompact only: Claude Code reads a plain-text
  stdout there). Hosts own tool-output shapes: the adapter exposes `replaceable(event)` and
  `render_tool_output(...)`, and mechanisms never import an adapter. Adapters declare
  **capabilities**; today the adapter's `replaceable` predicate, not the flags, decides what a
  mechanism may replace. Hook events handled on Claude Code: SessionStart, UserPromptSubmit,
  PreToolUse, PostToolUse, PostToolUseFailure, PreCompact, SessionEnd (SessionEnd is registered but
  has no handler yet; the detached `fold` it will start is planned, M2). There is no per-batch event; no mechanism needs one.
- **Fail-open everywhere**: every hook entrypoint wraps the pipeline; on exception or a
  latency-budget overrun it returns the host's no-op response and logs to
  `~/.token-sparen/logs/errors.jsonl`.
- **Two-speed learning**: (a) *in-session* — hooks apply cheap posterior updates to a
  **session-local overlay** file written only by that session (no cross-process contention, O(1) per
  event), so the policy adapts within the session (e.g. a recovery read immediately makes that bucket
  more conservative); (b) *cross-session* — a detached `token-sparen fold` (spawned at session end,
  also runnable manually) merges the overlay into project and global stores under a file lock,
  computes delayed rewards, mines facts and recomputes pruning sets.
- **Project key**: hash of normalised git remote URL (fallback: realpath of the project directory,
  `CLAUDE_PROJECT_DIR` or the hook's `cwd`); never the raw path in the shared stores (`projects/`,
  `global/`). User-local bookkeeping (trusted extensions, statusline install record) does hold paths.

## 6. Mechanisms (decision points)

Each mechanism is a **decision point** registered with the engine: it declares its arms, its
context features, its safe default arm, its quality-risk tier (low/medium/high), and the signals that
attribute reward to it. Tier governs exploration (§7.6).

### 6.1 Recoverable output compression (PostToolUse) — tier: medium
- **Quality gate (2026-11-26, owner decision).** Before the decision point, `compression.gated`
  leaves untouched (no decision, no log, no learning, no recovery file) every file's contents
  (Read results and file-dump shell commands) unless `trim_file_contents = true`, and every
  content type outside `allow_ctypes` (default: diff, search, test_report, compiler, build_log,
  json, pplx, log). Both widen only from the user's own config, env or session; a project file may
  only narrow them.
- **Router** by content type: `json`, `diff`, `stacktrace/error`, `test-report` (pytest, jest,
  go test, cargo test), `compiler` (tsc, rustc, javac, gcc), `build-log` (npm, pip, gradle, docker),
  `table/csv` (handled by the text compressor; no dedicated compressor), `search-results` (grep/rg, `pplx` JSON), `log` (timestamped), `text`.
- **Compressors** (deterministic, pure functions, each with golden tests): structure-aware
  summaries (e.g. test report → failures with first assertion + counts; JSON → schema + sampled
  items (duplicated-field elision was dropped: a `…same` marker is ambiguous about what it equals, and
  no exemption list can make it safe for every field);
  `pplx` → dedupe `snippet`/`summary`, keep titles/urls/top-k
  snippets), keyword-guided keep (lines scored by relevance to the current user prompt and recent
  edited paths — BM25-lite), head+tail with error-line pinning, and repetitive-line folding
  ("× 37 similar lines").
- **Arms**: `off`, `light`, `standard`, `aggressive`. The keep ratios are fixed per level
  (`[compression.levels.*]`); the learner chooses the level per context bucket, not the ratios.
- **Invariants**: never elide lines matching error/failure/exception signatures; always append a
  marker `[token-sparen: kept X of Y lines · full output: <abs path> · `token-sparen show <id>`]`;
  full output saved to `<project>/.token-sparen/outputs/<id>.txt` (gitignored, TTL + size cap).
  Below 2 000 tokens est. nothing is touched: this is a floor, `compression.min_tokens` can raise
  it but never lower it.
- **Never larger**: the baseline is the *preview* (the text the host would show the model); a
  replacement is used only if it is no longer than the preview and saves at least
  `min_saving_ratio` of it, and savings are booked against the preview.
- **Failed tool results are not replaceable on Claude Code** (PostToolUseFailure supports only
  `additionalContext`), so failing Bash commands are shown in full there; the mechanism supports
  failures for hosts that can replace them. Agent/Task results of shape
  `{status, content:[{type:text,text}], ...}` are compressed through their text blocks.
- Hosts that cannot replace output (Codex): achieved via PreToolUse command wrapping
  `token-sparen run -- <cmd>` (§8).

### 6.2 Pre-tool guards and learned pre-trims (PreToolUse) — tier: low/medium
- Block clearly unbounded scans (`find /` or `find ~` without `-maxdepth`, recursive `grep`/`rg`
  over vendored or build directories, `cat`/Read of a > 5 MB file without a range, `ls -R` on `/`
  or `~`) with a reason that suggests the bounded form — never block anything else.
- **Learned pre-trims** are a **PostToolUse arm**, not a command rewrite: commands whose outputs
  were repeatedly compressed with no regret get a tail-biased compression level (K learned).
  Commands are never rewritten: a `| tail -n K` rewrite would mask the exit status and drop
  output that cannot be recovered. (The learning of K is planned, M2.)

### 6.3 Read de-duplication (PreToolUse/PostToolUse on file reads) — tier: low
- Track `(path, mtime, size, hash, range)` per session; on a re-read of unchanged content, replace
  output with a short pointer ("unchanged since turn N; content is above") **only if** the earlier
  read is still in context (not compacted). Arm: `off`/`pointer`.

### 6.4 Session budget governor and burn meter — tier: low (advisory only)
- Tracks cumulative billed tokens Σ(context at every model call) from the transcript; EWMA burn
  projector estimates remaining turns to the user's limit. Escalation: statusline meter → advisory
  to the user (terminal-only, zero model tokens) → optional (opt-in) hard stop on context-adding
  tools. Separates *economy* thresholds (absolute tokens) from *risk* thresholds (window fraction).

### 6.5 Compaction advice and task-boundary detection — tier: low
- Change-point detection on prompt/topic features (containment of the prompt's identifiers in recent
  prompts and paths, a prompt-embedding-free lexical shift; an idle-gap signal was dropped as not
  needed) flags task boundaries; recommend `/compact` (or host equivalent) to the
  **user** at boundaries when context is large. Never auto-compacts.

### 6.6 Output-style directives — tier: high (off by default)
- Optional one-time directives (e.g. "be concise in prose; never shorten code") injected once per
  session. Off by default; enabled only if the live benchmark shows non-inferior quality. (Evidence
  from the landscape: a plain "be concise" prompt captures most of what terse-output plugins claim.)

### 6.7 Subagent shaping — tier: medium
- For hosts with subagents (Claude Code Task/Agent tool): an arm that appends a short brief asking
  for conclusions-not-dumps to the subagent prompt — **default off** until the live benchmark shows
  non-inferior quality (as §6.6) — and compression of oversized subagent results with the same
  recoverability rules (Claude Code delivers them as `{status, agentId, content:[{type:text,...}]}`).

### 6.8 Micro-skills: learned repository facts (cross-session) — tier: medium
- **Sources**: (a) deterministic miners for repeated failures across sessions (command-not-found →
  working spelling, missing env var, required build step, generated-file locations, test command);
  (b) optional LLM proposer (off by default; uses the host's own CLI in headless mode, cheapest
  model, every k sessions) that proposes facts with citations to ≥ 2 sessions.
- **Lifecycle as a bandit, not fixed gates**: each candidate fact is an arm-pair
  (`inject`/`withhold`) with a Beta posterior over "injecting this fact reduces waste in sessions
  where it is relevant"; reward from relevance-conditioned waste (did the failure it prevents recur?).
  Facts activate as soon as the posterior clears a lower-confidence bound (faster than fixed
  3/4-session gates), and retire on contradiction (one contradiction retires high-stakes facts).
- Injection at SessionStart as a compact, budgeted block (max N facts / M tokens), relevance-ranked.

### 6.9 Fact-injection security
- Facts are derived from repo-controlled output, so they are **data, not instructions**: every
  fact passes a strict template (`<subject> <verb> <object>` with an allow-listed verb set such as
  "run tests with", "requires env", "build first with", "generated files live in"), length cap,
  no imperative free text, no URLs, no shell metacharacters outside a quoted command field; injected
  inside a clearly delimited, labelled data block. LLM-proposed detector regexes are rejected in
  favour of a fixed detector vocabulary (no ReDoS).

### 6.10 Tool pruning (cross-session) — tier: high
- Per project, tools never used across sufficient evidence are candidates. Each candidate tool is a
  bandit arm `keep`/`prune` with conservative priors; pruning is applied only between sessions and
  only after the repo has been idle (to protect prompt caching).
- **In-session recovery (new)**: the SessionStart context lists pruned tools in one line and how to
  restore (`token-sparen restore <tool>` or the host's re-enable); any attempt to use a pruned tool
  or explicit user mention immediately restores it for the project and records strong negative
  reward.
- Host mechanism: Claude Code — deny rules in project `.claude/settings.local.json` (opt-in,
  disclosed, reversible, backed up); Codex — `.codex/config.toml` toggles (trusted projects);
  Gemini/Qwen — `BeforeToolSelection` allow-list; OpenCode V2 — `session.hook("context")` tool list.
  Protected list (Read/Edit/Write/Bash and equivalents) is never pruned.

### 6.11 Failure-loop guard — tier: medium
- Detects repeated identical failure signatures, error bursts, and oscillating edits within a turn
  using cheap deterministic signals (normalised error fingerprints, edit-revert detection). The
  intervention threshold is a learned arm (`patient`/`standard`/`eager`). Intervention is one short
  redirect appended to the next tool result (or block reason) suggesting a different approach; at
  most one per turn; second detection notifies the **user** only. No LLM judge by default (optional).

### 6.12 Live statusline (Claude Code; owner requirement) — tier: none (zero model tokens)
- A live statusline rendered on every Claude Code refresh, e.g.
  `⛁ ts 42% ctx │ burn 48M/500M 10% │ +2.4k/turn │ saved 31.2k (≈$0.84) │ ✂ 12 ↺ 1 │ learn: adapting`
  — context fill (green < 50 %, yellow < 75 %, red), cumulative session burn vs the governor's budget,
  projected burn per turn (EWMA), **net** tokens saved this session (after subtracting recovery reads
  and our own injected text) with an approximate $ equivalent (the session's average cost per token from the host's reported
  cost, not weighted by token class), trims and regrets (recovery reads /
  re-runs), and the learner's state (`cold` / `adapting` / `exploiting` / `safe-mode` when a circuit
  breaker tripped); optional segments for active facts and pruned tools.
- **Sensor role**: the statusline is the only Claude Code entry point that receives
  `context_window` (used %, size) and model id, so it records the real window per session/model for
  the governor (§6.4).
- Plugins cannot register a statusline, so `token-sparen statusline install` (offered by the `/token-sparen` skill
  when asked) writes the user's `statusLine` setting **opt-in**, with a backup, and
  **composes** with an existing statusline command (ours appended as a segment) instead of replacing
  it; `statusline uninstall` restores the recorded original `statusLine` exactly (a backup of the
  file is also kept). The launcher is POSIX shell: a Windows launcher is deferred (v1.1). The launcher resolves the active installed plugin
  copy robustly and renders an empty line (never an error) if the plugin is gone.
- Strictly bound to the current `session_id`/`transcript_path` from stdin (never "latest transcript in
  the directory"); p95 render < 50 ms; configurable segments, `NO_COLOR` and ASCII fallback.
- Other hosts: equivalent where the host supports a status line (v1.1); otherwise `token-sparen stats`.

## 7. Learning engine (the essence)

### 7.1 API
`engine.decide(point, features, session) -> Decision{arm, propensity, decision_id}` and
`engine.reward(decision_id, savings, regret)` (each value optional, in [0,1]); evidence and logs
live in the store.

### 7.2 Context features
Per decision point, a small discrete feature tuple, e.g. for compression:
`context_band × tool_category × size_band × content_type × task_phase`. (Host and model tier as
*prior-sharing* keys, not bucket explosion, are deferred to M4.) Bucket count is kept small by design; sparse buckets
back off hierarchically (§7.4).

### 7.3 Algorithm
Beta-Bernoulli **Thompson sampling** per (point, bucket, arm), fractional rewards applied as
fractional pseudo-counts (no coin-flip discretisation), **discounted** (γ per observation, default
0.98) to track drift, total mass capped (default 50) with mean-preserving rescale.

### 7.4 Hierarchical priors (within session → project → global)
Empirical-Bayes shrinkage: a bucket's prior = its parent's posterior mean with strength κ
(learned by method-of-moments across siblings), parent chain
`project-bucket → project-coarse → global-bucket → global-coarse → designed default`. Stores hold
evidence, not posteriors; see the 2026-11-26 Task 19 revision note for the parent-mean and κ formulas. Global stores
receive per-project **deltas** (not totals), weighted 0.3, and decay with age. This replaces
Reference A's six overlapping prior layers with one principled mechanism.

### 7.5 Honest reward — a constrained two-posterior bandit (fixes §7.9)
A single blended scalar is what let Reference A's trim arms beat `off` by construction. Instead each
(point, bucket, arm) keeps **two independent posteriors**:
- **savings** `S` ∈ [0,1]: normalised tokens removed × expected remaining re-sends (turn-count
  projection), divided by the bucket's running p95 so it is scale-free; modelled as a Beta with
  fractional pseudo-counts (`α += s`, `β += 1−s`). `off` has S ≡ 0 by definition.
- **regret probability** `R` ∈ [0,1]: Beta over "this decision caused regret", where regret is
  (i) a recovery read of the saved full output, (ii) a re-run of the same **normalised** command
  (full argv after stripping `cd … &&`, env prefixes and pipes we added) within W turns, (iii) the
  agent explicitly asking for elided content, (iv) a failure loop or failed task in the same turn after
  a trim of an error-bearing output. `off` has R ≡ 0 and S ≡ 0 **by definition** (defined, not
  observed: no reward is recorded for a safe-arm decision; the propensity floor in §7.6 still runs
  it and logs its propensity).

**Selection (Thompson, constrained):** sample S̃, R̃ for every arm; among arms with R̃ ≤ τ_tier
(default τ = 5 % medium tier, 1 % high tier) choose max S̃; `off` is always feasible. Thus `off` wins
exactly when every trimming arm's plausible regret exceeds the target — no hand-set reward for `off`,
no weights to rig. In M2 the units of "savings" are **token counts** (estimated tokens removed,
times the expected re-sends). Cost-weighting (cache-read/cache-write/input/output priced at list
ratios, so a change that breaks the prompt cache is charged) is deferred to M4; the E1 report and
E4 (§10.4) already use it.

### 7.6 Exploration safety
- Each tier has a **propensity floor** for the safe arm (always some probability of `off`), so the
  baseline is genuinely observed and logged propensities enable off-policy evaluation.
- High-tier arms (pruning, directives) never explore above their current level without a lower
  confidence bound clearing; **circuit breaker**: if rolling regret rate exceeds 2× target, the point
  reverts to its safe default for the project and notifies the user.

### 7.7 Other ML / statistics
- EWMA burn projection and cache-hit estimation (§6.4); change-point detection for task boundaries
  (§6.5); BM25-lite line relevance (§6.1); error fingerprinting via normalised token shingles
  (§6.11); fact lifecycle bandits (§6.8); pruning bandits (§6.10).

### 7.8 Offline evaluation and challengers
Decision logs with propensities allow **IPS and doubly-robust** estimates of any candidate policy
on past sessions. A LinTS challenger (deferred to M4) and every tuned hyper-parameter are
evaluated this way; promotion only on held-out improvement (§10.6).

### 7.9 Lessons from the references (design requirements)
- Reference A's reward let trim arms score ~0.9–1.0 while `off` capped at 0.85 → aggressive won with
  67 % re-reads. Fixed by §7.5–7.6.
- Reference A's command signature collapsed `cd x && …` to `cd` → fixed by normalised argv.
- Reference A's "empirically calibrated" priors were hand-set → our designed defaults are
  **hand-set until M4**, then refit by offline replay and documented with their provenance.
- Reference B's fixed gates delayed facts to ~session 5 and pruning had no in-session recovery →
  §6.8, §6.10.

## 8. Host adapters

| Host | Replace tool output | Rewrite/block pre-tool | Inject context | Tool pruning | Packaging |
|---|---|---|---|---|---|
| Claude Code | PostToolUse `updatedToolOutput` (shape per tool; not on PostToolUseFailure) | PreToolUse deny (no `updatedInput` rewriting by design) | SessionStart `additionalContext` | settings.local deny rules (opt-in) | `.claude-plugin/plugin.json` + hooks.json + marketplace.json |
| Codex CLI | not supported → PreToolUse rewrite to `token-sparen run -- <cmd>` (wrapper compresses & saves) | yes | SessionStart | config.toml toggles | `.codex-plugin/` + repo marketplace (hooks need user trust) |
| Gemini CLI | AfterTool `decision:"deny"` + `reason` | BeforeTool | SessionStart | BeforeToolSelection allow-list | `gemini-extension.json` at root, topic `gemini-cli-extension` |
| Qwen Code | Claude-style hooks; verify `updatedToolOutput` in released version, else Gemini path | yes | yes | allow-list | `qwen-extension.json` |
| OpenCode | V2 `tool.hook("execute.after")` (V1 fallback) | `execute.before` | `session.hook("context")` | context tool list | npm package `token-sparen-opencode` (TS shim → Python core) |
| `pplx` (any host) | compressed via the host path above when an agent calls `pplx` | — | — | — | compressor only |

`token-sparen install --host <name>` writes/links the host config; `token-sparen doctor` verifies
the host version, capabilities and that hooks fire (synthetic event round-trip).

## 9. Extensibility

- **Plugin registry** via Python entry points (`token_sparen.compressors`, `.decision_points`,
  `.signals`, `.adapters`) **and** drop-in files at `<project>/.token-sparen/extensions/*.py` and
  `~/.token-sparen/extensions/*.py` (loaded lazily, only when their trigger matches; run only after an explicit
  `token-sparen extensions trust <path>` step, since hooks cannot prompt; trust follows the file's
  content hash and equals full code execution as the user).
- Stable, versioned interfaces (version `1.0`, `token_sparen.EXTENSION_API_VERSION`; an extension
  declaring an incompatible version is skipped) (`Compressor.detect/compress`, `DecisionPoint`, `Signal`,
  `HostAdapter`) with a conformance test kit users can run on their extension.
- Config precedence: built-in defaults < `~/.token-sparen/config.toml` < `<project>/token-sparen.toml`
  < env vars (`TOKEN_SPAREN_*`) < per-session overrides (`token-sparen config set`, also reachable
  from the `/token-sparen` skill). `TOKEN_SPAREN_DISABLE=1`
  is a global kill switch.
- The project layer is **deny-by-default**: `<project>/token-sparen.toml` may be someone else's, so
  it can only make token-sparen intervene less or keep more (never trim more, recover less or block
  more). Each key has a kind (`config.PROJECT_POLICY`); an unlisted key is user-only, and a value
  that is out of bounds, of the wrong type or in the unsafe direction is ignored and logged once.

## 10. Evaluation, benchmarking and hill-climbing

### 10.0 Data sources
E1 uses existing transcripts (the maintainer's local sessions and their subagent logs) — they carry no
propensities, so E1 measures savings and a *proxy* regret ("did the agent later reference content
inside an elided span"). E3 (off-policy evaluation) needs **decision logs with propensities**, which
only exist once token-sparen is installed; these come from a dogfooding period (§17).

### 10.1 E1 — Offline replay on the maintainer's local sessions (free, private)
Replay transcripts (Claude Code JSONL) through the pipeline in each policy; measure tokens removed
from re-sent context, projected billed tokens (cache-aware), and **information preservation**: for
each elided span, whether the agent later referenced content inside it (strings, paths, line
numbers) → "would-have-regretted" rate. Dev/test split by session date. Output: a private
aggregate report written outside any git work tree. **Its numbers are never committed to the
repo** (C4), not even as aggregates.

### 10.2 E2 — Fact-preservation suite (public, deterministic, CI)
Corpus of synthetic and public tool outputs (test reports, compiler errors, JSON APIs, logs, diffs,
`pplx` results) with planted "needles" (the failing test name, the error line, the key field); a
compressor passes only if every needle survives or is reachable via the marker. Runs in CI on every PR.

### 10.3 E3 — Off-policy evaluation (§7.8) on decision logs.
`token-sparen ope` estimates a target policy (`static` or an `engine-snapshot`) from logged
decisions with IPS, self-normalised IPS and doubly-robust estimators (clipped weights, bootstrap
CIs), with savings and regret estimated separately. The quality constraint is judged on **regret
per trim** (the target's regret mass over its trimming mass, not an average over all decisions):
`pessimistic` counts each unsettled trim as regret 1. `constraint_ok` is true when the largest
upper bound (IPS bootstrap, DR bootstrap, and an exact Beta bound on the pessimistic regret count)
is at most τ. The verdict is **`insufficient`** (`constraint_ok = null`) when fewer than
`[ope] min_trims` trims were logged, when outcome coverage is below `min_outcome_coverage`, when
more than `max_unsupported_trim` of the target's trimming mass is on arms the log never chose in
that bucket, or when no ratio can be formed; a target that never trims is `no_trims` and ok. The
`[ope]` thresholds are user-only. Outcomes are joined to decisions by id across log files and
rotation generations; an unsettled outcome counts as regret in the pessimistic variant.

### 10.4 E4 — Live benchmark with Claude Code under Max
- Tasks: **SWE-bench Verified Mini (50 tasks)**; harness: Harbor (`claude-code` agent with
  OAuth token, isolated `CLAUDE_CONFIG_DIR` per arm, `--plugin-dir`, `--strict-mcp-config`);
  fallback: own runner + official `swebench` grader (local x86 Docker or sb-cli).
- Arms: (1) plain Claude Code; (2) "answer concisely" control prompt; (3) **token-sparen**;
  (4) RTK; (5) Headroom; (6) Caveman (skill only); (7) context-mode; (8) TokenEconomizer (shipped
  plugin, repo-ordered tasks). Each competitor in its documented default configuration; plugin load
  verified from the `stream-json` init event.
  (Host capability note: Claude Code PostToolUse `updatedToolOutput` for built-in tools was
  confirmed against the current hooks documentation on 2026-11-25; `doctor` re-verifies per version.)
- Metrics: cost (`total_cost_usd` and all token classes incl. cache read/write), turns, wall time,
  **resolved (Pass@1)** by the official grader, plus each plugin's own overhead calls.
- Learning state during E4: token-sparen runs **as shipped** (exploration on), tasks per repository in
  commit order with state accumulating — identical sequencing for TokenEconomizer — so both learners
  are measured the way users would experience them; a cold-start ablation arm is reported separately.
- Cost unit: cost-weighted tokens / `total_cost_usd` (list-price equivalent incl. cache classes) is
  primary; Max quota % consumed is recorded as secondary.
- Grading: official `swebench` harness; on the arm64 dev machine try locally built arm64 images first,
  fall back to the hosted `sb-cli` grader.
- Budget plan (maintainer on **a Claude Max subscription, no deadline**): **screening** — baseline, concise-prompt control,
  token-sparen, RTK, Headroom, TokenEconomizer × 50 tasks × 1 seed on a cheaper model;
  Caveman and context-mode added if quota allows; **confirmation** — baseline + token-sparen + best
  two competitors × 50 × 3 seeds on the default model. Calendar time is unconstrained. Scheduler alternates arms inside each 5-hour
  window, records usage %, pauses near the weekly cap, resumes idempotently. Model = Claude Code's
  default model at run time (recorded); a cheaper-model screening run is allowed if quota requires.
- Optional E5: Terminal-Bench 2.0 subset for non-SWE agent work.

### 10.5 Statistics and claims policy
Paired per-task comparisons; cost: paired bootstrap 95 % CI of the median ratio; quality:
**non-inferiority** on resolved rate with margin −5 pp (owner decision: what 50 tasks × 3 seeds can honestly resolve; exact CI always published alongside E1/E2 quality evidence; release gate = non-inferior at −5 pp) (paired McNemar / bootstrap). A result is
published only with its CI, n, seeds, model and date; the published numbers measure the **released
plugin version** (hash recorded). No claim without a reproducible script in `bench/`.

### 10.6 Hill-climbing
Every tunable (keep ratios, thresholds, priors, λ targets, size floors, compressor rules) is a
parameter in `token_sparen/params/defaults.toml` (inside the package so the wheel carries it). Loop: propose change → E2 must stay 100 % → E1/E3 on **dev**
split must improve the objective (savings with regret ≤ target) → confirm on **held-out test**
split → (periodically) E4 confirmation. Search: coordinate ascent + successive halving; every
accepted change is a commit with its eval delta. Never tune on the test split (Reference B's flaw).

## 11. Repository, CI, release, docs

- Repo **github.com/rupakc/token-sparen** (public, MIT), incremental commits pushed to `main`.
- Layout: `token_sparen/` (core), `adapters/{claude,codex,gemini,qwen,opencode}/`,
  `.claude-plugin/`, `.codex-plugin/`, `gemini-extension.json`, `qwen-extension.json`,
  `packages/opencode/` (TS), `bench/`, `evals/`, `docs/`, `site/` (gh-pages source),
  `tests/`.
- Claude scaffolding: `CLAUDE.md`, `AGENTS.md` (host-neutral), `MEMORY.md` (project decisions log),
  `.claude/agents/` (e.g. `goldfish-reviewer`, `drift-checker`, `compressor-author`,
  `bench-runner`, `eval-hillclimber`, `host-adapter-author`), `.claude/skills/` (e.g.
  `add-compressor`, `add-host-adapter`, `run-evals`, `hill-climb`, `release`).
- CI (GitHub Actions): lint (ruff), types (mypy --strict on core), tests (pytest, Linux/macOS/Windows
  × py3.11–3.13, coverage gate ≥ 90 %), E2 fact-preservation suite, hook latency benchmark, manifest
  drift tests across host manifests, plugin validation (`claude plugin validate`, `claude plugin
  eval` cases), TS shim build/tests, security scan (no secrets, file-size rule C6), docs/site build.
- Release workflow on tag: build wheels/sdist → PyPI (Trusted Publishing); per-host zips as GitHub
  Release assets; npm publish for OpenCode shim; generated `release/claude` branch for the Anthropic
  directory; changelog from conventional commits.
- **GitHub Wiki**: Home, Concepts, Architecture, Learning Engine, each Mechanism, Host Guides,
  Configuration, Extending, Benchmarks & Methodology, Privacy & Security, FAQ, Troubleshooting.
- **gh-pages site**: landing page with interactive savings explorer (driven by published benchmark
  JSON), "where it shines" scenarios, before/after tool-output demos, quick-start per host, docs.

## 12. Distribution
Anthropic plugin directory (submission via claude.ai form, from the `release/claude` branch), the
repo's own Claude marketplace, PyPI, npm (OpenCode) + OpenCode ecosystem PR, Gemini extension
gallery (topic + release), Codex via repo marketplace (OpenAI's directory forbids hooks; a
skills-only variant may be listed later).

## 13. Development process
Elephant–Goldfish: this spec (elephant) → cold `goldfish-reviewer` subagents review the spec and each
milestone's plan → TDD implementation via subagents → `drift-checker` compares code to spec at each
milestone → self-review and `code-review` before every push. `MEMORY.md` records decisions.

## 14. Milestones (owner decision: v1.0 = Claude Code complete; other hosts in v1.1)
1. M0 Scaffolding, CI skeleton, licence, Claude files, agents/skills.
2. M1 Core: event model, store, emit gate, compressors + E2 suite, Claude Code adapter (compression,
   guards, read dedupe, governor, **live statusline §6.12**).
3. M2 Learning engine (§7) + decision logging + offline replay/E1/E3.
4. M3 Cross-session: micro-skills, tool pruning, loop guard; `fold`.
   → **Dogfooding** (~2 weeks, owner's daily Claude Code use; decision logs local only) runs in
     parallel with M3/M4.
5. M4 Hill-climbing pass on E1/E3; parameter freeze.
6. M5 Live benchmark E4 (screening → confirmation).
7. M6 Docs: README, wiki, gh-pages; release **v1.0 (Claude Code)** and distribution.
8. M7 (v1.1) Adapters: Codex, Gemini, Qwen, OpenCode; `install`/`doctor`; host-matrix CI. The core
   and canonical event model are host-neutral from M1 so v1.1 adds adapters only.

## 15. Future work
API compression proxy for hook-less clients (chatgpt-cli, raw DeepSeek/Perplexity APIs);
Antigravity CLI; Cursor/Windsurf/Copilot CLI adapters; shared team priors (opt-in export/import of
anonymised posteriors).

## 16. Left to the builder
Exact module boundaries, file formats details, marker wording, compressor heuristics (subject to E2),
CLI ergonomics, site visual design, wiki prose.

## 17. Decisions
Owner: approach A; Claude Code-only benchmarks on a Claude Max subscription, unconstrained calendar; ~2-week dogfooding;
−5 pp non-inferiority gate; v1.0 = Claude Code, other hosts v1.1; hook-less clients deferred.

Calls made by the designer (owner should check):
Python stdlib core + TS shim; Gemini CLI supported but Antigravity deferred; output-style directives
off by default; LLM side-calls off by default; tool pruning requires opt-in on Claude Code because it
writes settings; ≥90 % coverage gate; CLI name `token-sparen`.
