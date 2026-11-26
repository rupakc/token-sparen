# token-sparen

An open-source Claude Code plugin that cuts token usage without hurting output quality. It trims
noisy tool output (test runs, logs, JSON, diffs, searches, subagent results) before the model sees
it, keeps the **full text on disk so nothing is lost**, blocks a few clearly unbounded commands, and
shows a live token meter. Everything runs locally.

## Status

Pre-release (`0.1.0.dev0`). The mechanisms below work today; how strongly to trim is learned per
project from your own sessions (see [How it learns](#how-it-learns)), starting conservatively.
There are **no published benchmark numbers yet**, so this README makes no savings claims; measure
on your own sessions with `/token-sparen` or `token-sparen stats`.

## Requirements

- Claude Code with plugin support.
- `python3` >= 3.11 on your `PATH` (the hooks run `python3`). No other runtime dependencies.
  macOS's system `python3` is often older than 3.11: check with `python3 --version`, and run
  `/token-sparen doctor` (below) first if you are unsure. Hooks fail open, so an old Python means
  token-sparen silently does nothing rather than breaking your session.
- macOS and Linux. Windows: hooks need a `python3` on `PATH` (`token-sparen doctor` suggests
  `py -3` or `python` if it is missing), and the statusline launcher is not supported yet.

## Install

```
claude plugin marketplace add rupakc/token-sparen
claude plugin install token-sparen@token-sparen
```

That is all the plugin needs. Start a new Claude Code session afterwards (or reload plugins) so
the hooks are picked up.

**Using the CLI as a plugin-only user.** You do not need the `token-sparen` command: ask Claude
`/token-sparen` (stats), `/token-sparen doctor`, `/token-sparen disable`, and it runs the bundled CLI
for you. To run it yourself in a terminal, call the bundled launcher from the plugin directory:
`python3 <plugin dir>/hooks/run_cli.py <args>` (for example `... stats`). The plugin directory is the
`installPath` entry for `token-sparen` in `~/.claude/plugins/installed_plugins.json`.

Optional: the `token-sparen` console command (handy in your own terminal). It is **not published on PyPI yet**;
from a checkout of this repository run `pip install .`.

**Verify:** in Claude Code run `/token-sparen doctor`, or in a terminal `token-sparen doctor`. `doctor` checks Python, the state directory, hook files and a
synthetic compress-and-recover round trip.

## What changes in your session

All of this is on by default. Nothing is sent anywhere.

- **Compression.** When a tool result is **at least 2000 estimated tokens**, token-sparen replaces
  it with a shorter version that keeps the head and tail, every error line, failures and
  stack-trace edges, and ends with a recovery marker. It applies to Bash output, text Read results,
  Agent/Task results and plain-text results from search, web and MCP tools. It chooses a
  compressor by content type (JSON, diff, stack trace, test report, compiler output, build log,
  search results, logs, plain text).
- **Never larger.** The replacement is used only if it is no longer than what the model would
  have seen anyway and saves at least 15 % of it; otherwise the output is left untouched.
- **What is never touched.** Results below 2000 tokens (a lower `min_tokens` in config is
  ignored; a higher one is honoured), edit tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`),
  `TodoWrite`, `AskUserQuestion`, `ExitPlanMode`, images/PDFs, and **failing tool calls**. Claude
  Code cannot replace the result of a failed call, so a failing Bash command always shows in full.
- **Guards (block a call).** Exactly these are blocked, with a one-sentence reason that names the
  bounded alternative (for example "add `-maxdepth 3`"):
  1. `find` starting at `/`, `~` or `~/` without `-maxdepth`;
  2. recursive `grep`/`rg` into an existing `node_modules`, `.git`, `dist`, `build` or `.venv`
     directory without an include filter;
  3. `cat` of a file larger than 5 MB whose output goes to the model (not when piped or
     redirected), and the Read tool on a text file larger than 5 MB without an offset/limit range
     (images, PDFs and notebooks are not blocked);
  4. `ls -R` on `/`, `~` or `~/`.

  A command that cannot be parsed is never blocked. Turn guards off with `guards.enabled = false`.
- **Read de-duplication.** Re-reading an unchanged file that is still in context (within 8 turns,
  not compacted since) returns a short pointer, `[token-sparen: unchanged since turn N ...]`,
  instead of the file again.
- **Notices (terminal only, no model tokens).** Budget notices when your session passes 50 % and
  80 % of the configured token budget (default 500M billed context tokens), context-window notices at 60 % and 80 %, and at most an
  occasional "this looks like a new task, consider `/compact`" hint when context is fairly full.
  token-sparen never compacts for you; before a compaction it asks that recovery paths be kept in
  the summary.
- **Hard stop (opt-in, off by default).** With `governor.hard_stop = true`, once the session passes
  100 % of `session_budget_tokens`, context-adding tools (Read, Bash, WebFetch, WebSearch, Grep,
  Glob, LS, Task/Agent, MCP tools) are denied. Edit tools are never blocked.
- **Subagent brief.** A decision point that can append a short "be concise" brief to subagent
  prompts exists but is **off** by default.
- **Never rewritten.** token-sparen never modifies a command you or the model typed.

## Recovering trimmed output

A trimmed result ends with a marker:

```
[token-sparen: kept 48 of 1930 lines · full output: <path> · `token-sparen show a1b2c3d4e5f6`]
```

- `token-sparen show <id>` prints the full text (the id is 12 hex characters), or just read the
  `<path>` file. Add `--project <dir>` if you are not in that project.
- Full outputs are stored in `<project>/.token-sparen/outputs/<id>.txt`. Old files are pruned
  whenever a new one is saved: those older than 14 days, then the oldest beyond 200 files or 100 MB.
- Ask the model to read the file when it needs more; most of the time the visible lines suffice.

## Turning it off

| Scope | How |
|---|---|
| Everywhere | `TOKEN_SPAREN_DISABLE=1` (every hook becomes a no-op). It must be in the environment Claude Code is **launched** from; setting it in a running session's shell is too late |
| This session | `/token-sparen disable` (and `/token-sparen enable`), or `token-sparen session disable` (with no `--session` it acts on this project's most recently active session, if active in the last 6 hours; otherwise pass `--session ID`) |
| The plugin | `claude plugin disable token-sparen` (and `claude plugin enable token-sparen`) |
| One mechanism | `guards.enabled`, `read_dedupe.enabled`, `governor.enabled`, `boundary.enabled` or `subagent.enabled` set to `false`. There is no `compression.enabled`: turn compression off with one of the first three rows |
| Uninstall | `claude plugin uninstall token-sparen@token-sparen`; run `token-sparen statusline uninstall` first if you installed the statusline |

Uninstalling leaves `~/.token-sparen/` and each project's `.token-sparen/` in place; both are safe
to delete.

## What it writes to disk

Everything stays on your machine, but some of it is plain text you should know about.

- `<project>/.token-sparen/`: recovery files under `outputs/` (full tool outputs, so they can
  contain anything a command printed), plus a `.gitignore` containing `*` so the folder is never
  committed. Created on the first compression in that project.
- `~/.token-sparen/` (override with `TOKEN_SPAREN_HOME`):
  - `config.toml` (yours, optional), `trusted_extensions.json`, `statusline_install.json` (records
    your settings file's path and original `statusLine`), `statusline.sh` (launcher copy),
    `windows.json` (context window per session), `project_keys.json` (index from project to hash
    key);
  - `logs/errors.jsonl` and sampled `logs/latency.jsonl`;
  - `projects/<key>/`: `decisions.jsonl` (per-decision log; keeps the first 500 characters of Bash
    commands), `sessions/<id>.json` (session overlay: the **last user prompt, up to 2000
    characters**, recently read/edited **file paths**, the last 10 tool categories (for the
    learning engine's task phase), identifiers used for task-boundary
    detection, the token ledger and read de-dupe memory, and the session's learned evidence, bucket
    labels and numbers only), `sessions/regret/<id>.json` (for each trim of the last few turns: the
    normalised command, up to 64 distinctive words of the trimmed part of the output stored
    **only as hashes** (not plain text) and up to 32 **file paths** it mentions, used to notice when
    a trim hurt), `sessions/pending/<id>.json` (the learner's decisions awaiting their reward: ids,
    bucket labels, arms, times), `last_active.json`
    (the project's most recent session id), `savings_scale.json` (numbers only: recent savings
    sizes per bucket), `floor_warned.json`, `posteriors.json` and `breaker.json` (numbers only:
    learned evidence per bucket and recent regret values per decision point), `pending.json`
    (ids of sessions waiting to be folded) and `pretrim.json`
    (**normalised Bash commands** with per-command counts, and the learned pre-trim candidates).
    The session overlay also keeps the settled regret values of its trims, each with the
    normalised command, until the session is folded. Project folders are named by a hash, but the
    files inside hold the plain text above.
  - `global/posteriors.json`: numbers only (evidence pooled across projects, keyed by decision
    point, feature bucket and arm, plus recent fold ids per salted project tag); no paths,
    commands, project keys or session ids. `secret` (random, mode 0600) salts those tags.
- Claude Code `settings.json`: **only** if you run `token-sparen statusline install` (see below),
  with a backup and exact restore. Pruning tools from your settings is planned (M3) and will be
  opt-in too.
- Hooks fail open: any internal error or a hook slower than 400 ms returns "no change" and logs one
  line to `errors.jsonl`.

## Live statusline (opt-in)

Plugins cannot register a statusline, so this is an explicit step: `token-sparen statusline install`.
It edits the `statusLine` key of `$CLAUDE_CONFIG_DIR/settings.json` (else `~/.claude/settings.json`),
after copying the file once to `settings.json.token-sparen.bak` and recording your original
`statusLine` in `~/.token-sparen/statusline_install.json`. If you already have a statusline command,
ours is appended after it. Only `statusLine` is touched; a settings file that is not valid JSON is
refused. `token-sparen statusline uninstall` restores your original `statusLine` exactly (or removes
the key) and leaves every other setting alone. Not supported on Windows yet.

Example (colours on a terminal; `NO_COLOR` or `statusline.ascii = true` simplify it):

```
⛁ ts 42% ctx │ burn 48M/500M 10% │ +2.4k/turn │ saved 31.2k (≈$0.84) │ ✂ 12 ↺ 1 │ learn: cold
```

| Segment | Meaning |
|---|---|
| `ctx` | share of the context window used by the last call |
| `burn` | billed context tokens summed over calls, against the session budget |
| `rate` | recent context growth per turn (`+2.4k/turn`) and your 5-hour rate-limit use when Claude Code reports it |
| `saved` | tokens removed so far, with an approximate dollar figure derived from the session's average cost per token reported by Claude Code. Recovery reads are not yet subtracted (planned, M2), so treat it as gross savings |
| `trims` | `✂` trims made, `↺` recovery reads (regrets; counted from M2, shows 0 for now) |
| `learn` | learner state: `cold` (< 20 observations), `adapting`, `exploiting` (>= 80 % of recent decisions pick the best-known arm) or `safe-mode` (a decision point is paused by the circuit breaker) |

Choose segments with `statusline.segments`.

## Configuration

Later layers override earlier ones:

1. packaged defaults (`token_sparen/params/defaults.toml`, the full list of keys)
2. `~/.token-sparen/config.toml`
3. `<project>/token-sparen.toml`
4. environment: `TOKEN_SPAREN_<SECTION>__<KEY>=value` (double underscore between parts, for
   example `TOKEN_SPAREN_GUARDS__ENABLED=false`, `TOKEN_SPAREN_GOVERNOR__HARD_STOP=true`; list
   values are comma-separated)
5. this session only: `token-sparen config set <section.key> <value> [--session ID]`
   (e.g. `config set guards.enabled false`)

`compression.min_tokens` can raise the 2000-token floor but never lower it (a lower value is
ignored and `doctor` flags it).

**What a project file may change.** `<project>/token-sparen.toml` is committed, so it may be
someone else's. It can only make token-sparen intervene less or keep more, never trim more,
recover less or block more. A value is used only if it has the right type, is within sane
bounds and moves in the allowed direction from your own value (defaults, your config, env,
session); anything else is ignored and logged once.

| Settings | A project may |
|---|---|
| `statusline.{segments,ascii,color,parse_budget_ms}`, `pricing.*`, `boundary.{min_ctx_pct,min_overlap,min_turn,history,min_identifiers,cooldown_turns}`, `governor.{econ,ctx}_{notice,advise}` | set freely within bounds (display, advice) |
| `guards.enabled`, `read_dedupe.enabled`, `governor.enabled`, `boundary.enabled`, `subagent.enabled` | only turn off |
| `compression.min_tokens`, `min_saving_ratio`, `levels.*.keep_ratio`, the per-type knobs (`json.items`, `diff.context`, `stacktrace.edge_frames`, `test_report.block_lines`, `search.per_file`, `pplx.results`, `log.tail`), `recovery.*`, `guards.max_read_bytes`, `read_dedupe.min_tokens` | only raise (keep more) |
| `compression.compiler.warnings.*` | only move from `count` to `keep` |
| `compression.skip_tools` | only add tools |
| `read_dedupe.max_turn_gap`, `read_dedupe.max_entries`, `compression.save_deadline_ms`, `adapter.latency_budget_ms` | only lower |
| `learn.tau.<tier>` lower; `learn.floor.<tier>`, `learn.ladder_obs` higher; `learn.hold.<point>` only `true`; `learn.breaker_min_obs`, `learn.breaker_every` lower; `learn.breaker_lcb_q`, `learn.breaker_sessions` higher | only make learning more careful |
| everything else, including `learn.enabled` and all other `learn.*`, `compression.levels.*.{head,tail}` and `guards.pretrim_*` (a larger head/tail displaces prompt-relevant lines), `governor.hard_stop`, `governor.session_budget_tokens`, `governor.windows`, `adapter.latency_sample_rate`, `adapter.latency_log_max_bytes`, `extensions.*`, and any key not listed | nothing (user config, env or `config set` only) |

| Key | Default | Meaning |
|---|---|---|
| `compression.min_tokens` | 2000 | outputs below this are never modified (floor) |
| `compression.min_saving_ratio` | 0.15 | minimum share saved for a replacement to be used |
| `compression.skip_tools` | edit tools, TodoWrite, ... | tools whose output is never compressed |
| `guards.enabled` | true | block the unbounded commands listed above |
| `guards.max_read_bytes` | 5000000 | size limit for `cat`/Read without a range |
| `read_dedupe.enabled` | true | return a pointer for unchanged re-reads |
| `read_dedupe.max_turn_gap` | 8 | pointer only if the earlier read is this recent |
| `governor.enabled` | true | budget and context notices |
| `governor.session_budget_tokens` | 500000000 | economy budget the notices and hard stop use |
| `governor.hard_stop` | false | deny context-adding tools at 100 % of the budget |
| `boundary.enabled` | true | suggest `/compact` at task boundaries |
| `subagent.enabled` | true | subagent brief decision point (its default arm is off) |
| `learn.enabled` | true | learn how strongly to trim; `false` uses the fixed defaults (user config, env or `config set` only) |
| `statusline.segments` | all six | which segments to show |
| `statusline.ascii`, `statusline.color` | false, true | plain glyphs; colours |
| `recovery.ttl_days`, `max_files`, `max_bytes` | 14, 200, 100 MB | recovery file retention |
| `adapter.latency_budget_ms` | 400 | hook time cap before it becomes a no-op |
| `extensions.allow_override` | false | let a trusted extension replace a built-in (user config or env only) |

## Extensions (opt-in, explicit trust)

Installed Python packages can add compressors, decision points, signals or host adapters through
the entry-point groups `token_sparen.compressors`, `.decision_points`, `.signals` and `.adapters`.
Installing such a package is itself the trust decision: entry points have no separate trust step,
so install only packages you trust.
The extension interface is versioned (`token_sparen.EXTENSION_API_VERSION`, currently `1.0`);
an extension may declare `TOKEN_SPAREN_EXTENSION = {"api": "1.0"}` and one that needs a newer or
different major is skipped. Check a compressor with
`token_sparen.testing.conformance.check_compressor(c) == []`.

Drop-in `*.py` files in `<project>/.token-sparen/extensions/` or `~/.token-sparen/extensions/` run
only after you trust their exact content: `token-sparen extensions trust <path>` shows the sha256
and the first 40 lines, and `--yes` records it in `~/.token-sparen/trusted_extensions.json`. Any
edit revokes trust, and a repository cannot trust its own files. Symlinked, foreign-owned and
world-writable files are skipped. `token-sparen extensions list` shows each drop-in's status;
`extensions untrust <path|sha256>` revokes. Built-in compressors win unless
`[extensions] allow_override = true` is set in `~/.token-sparen/config.toml` or via
`TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE`; a project `token-sparen.toml` and `config set` cannot
enable it.

**What trust means: trusting a drop-in gives it full code execution as you**, with your files and
credentials, every time a hook uses it. Only trust code you have read or whose author you trust.
The runtime checks (error lines kept, line counts consistent, failures disabled) protect you from
*buggy* extensions, not *malicious* ones. Trust follows content, not location: an identical copy of
a trusted file in another project also loads.

## Privacy

Nothing leaves your machine. token-sparen has no telemetry and makes no network requests. Its local
files are plain text, though: recovered outputs, your last prompt, file paths and command prefixes
are stored as described in [What it writes to disk](#what-it-writes-to-disk). Delete
`~/.token-sparen/` and `<project>/.token-sparen/` at any time.

## How it learns

A small constrained bandit chooses, per decision point and context bucket (context fill, tool
category, output size, content type, task phase), how strongly to trim, under a hard quality
constraint: it never trades quality for tokens in a weighted sum. Each trim is watched for signs
it hurt (the agent reading the saved full output, re-running the command, asking for trimmed
content); a trimming level is used only while its plausible regret stays under a target (5 % for
compression). A new project starts with the lightest level and unlocks stronger ones one at a time
only after the previous one has been observed enough without regret, so early sessions trim less
than later ones. A small share of decisions always keeps the output untouched, as a baseline. The
subagent brief stays off (it is not learned until a benchmark shows it does not hurt quality).
Turn learning off with `[learn] enabled = false` (fixed defaults: standard compression, read
de-dupe pointers). A project's `token-sparen.toml` can only make learning more careful (see [What a
project file may change](#configuration)); it cannot turn learning off (that would fall back to
the fixed standard compression), so a project that wants less trimming sets
`[learn.hold] compression = true`. When a session ends, a background `token-sparen fold` merges what the session learned into the
project (and, weighted 0.3, into a cross-project global store that forgets after 90 idle days);
sessions it missed are folded at the next session start. A circuit breaker pauses a decision point
for the project for 3 sessions when the learner's recent trims clearly caused re-reads at more than
twice the target rate, and tells you once. Repo-fact injection and tool pruning are planned for M3; `prune` and
`facts` CLI commands are placeholders.

### Measuring it on your own sessions (offline, private)

`token-sparen replay <transcript files or dirs> --policy static|engine|<arm> --out <file>` replays
your past Claude Code transcripts through the same compression path the hook uses (nothing is
saved or learned) and estimates tokens removed, projected re-sends until the next compaction,
cache-aware cost-weighted savings and a proxy regret rate (the agent later used distinctive words
that only the trimmed part contained). `--split dev|test|all` and `--cutoff YYYY-MM-DD` split
sessions by date. `token-sparen ope --log <decisions.jsonl...> --target static|engine-snapshot
--out <file>` estimates how another policy would have done on your logged decisions (IPS,
self-normalised IPS, doubly robust, with bootstrap intervals, clipped importance weights and the
share of the target's choices the log cannot support). Both read only what you point them at,
print aggregate numbers only (no paths, ids or text), and write a JSON report with mode 0600; the
report path must be outside any git work tree, so a report can never be committed by accident. (On Windows file modes do not apply: keep the report inside your user profile, whose
default permissions are private to you.)
While they run, error log lines keep only where an error happened, never its message.

## Credits and licence

Cross-session ideas were inspired by TokenEconomizer (MIT, arXiv 2610.22732); no code was copied.
Released under the MIT licence: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions:
[CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md). Security:
[SECURITY.md](SECURITY.md).
