# token-sparen

An open-source Claude Code plugin that cuts token usage without hurting output quality. It trims
noisy tool output (test runs, logs, JSON, diffs, searches, subagent results) before the model sees
it, keeps the **full text on disk so nothing is lost**, blocks a few clearly unbounded commands, and
shows a live token meter. Everything runs locally.

## Status

Pre-release (`0.1.0.dev0`). The mechanisms below work today with fixed, conservative defaults.
There are **no published benchmark numbers yet**, so this README makes no savings claims; measure
on your own sessions with `/token-sparen` or `token-sparen stats`. Learning from past sessions is
planned (M2/M3), see [How it learns](#how-it-learns-planned).

## Requirements

- Claude Code with plugin support.
- `python3` >= 3.11 on your `PATH` (the hooks run `python3`). No other runtime dependencies.
- macOS and Linux. Windows: hooks need a `python3` on `PATH` (`token-sparen doctor` suggests
  `py -3` or `python` if it is missing), and the statusline launcher is not supported yet.

## Install

```
claude plugin marketplace add rupakc/token-sparen
claude plugin install token-sparen@token-sparen
```

That is all the plugin needs. The `/token-sparen` skill works without anything else: it runs the
bundled CLI through `hooks/run_cli.py`.

Optional: the `token-sparen` console command (handy in your own terminal). It is **not published
on PyPI yet**; from a checkout of this repository run `pip install .`.

**Verify:** in Claude Code run `/token-sparen` (it prints your totals and runs the checks), or in a
terminal run `token-sparen doctor`. `doctor` checks Python, the state directory, hook files and a
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
  1. `find /` or `find ~` without `-maxdepth`;
  2. recursive `grep`/`rg` into `node_modules`, `.git`, `dist`, `build` or `.venv` without an
     include filter;
  3. `cat` or Read of a text file larger than 5 MB without a range;
  4. `ls -R` on `/` or `~`.
  
  A command that cannot be parsed is never blocked. Turn guards off with `guards.enabled = false`.
- **Read de-duplication.** Re-reading an unchanged file that is still in context (within 8 turns,
  not compacted since) returns a short pointer, `[token-sparen: unchanged since turn N ...]`,
  instead of the file again.
- **Notices (terminal only, no model tokens).** Budget notices when your session passes 50 % and
  80 % of the configured token budget, context-window notices at 60 % and 80 %, and at most an
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
- Full outputs are stored in `<project>/.token-sparen/outputs/<id>.txt` and expire after 14 days
  (also capped at 200 files / 100 MB, oldest first).
- Ask the model to read the file when it needs more; most of the time the visible lines suffice.

## Turning it off

| Scope | How |
|---|---|
| Everywhere | `TOKEN_SPAREN_DISABLE=1` in the environment (every hook becomes a no-op) |
| This session | `/token-sparen disable` (and `/token-sparen enable`), or `token-sparen session disable` (acts on the project's most recent session if you pass no `--session`) |
| One mechanism | set its `enabled` key to `false` in config, e.g. `guards.enabled` |
| Uninstall | `claude plugin uninstall token-sparen@token-sparen`; if you installed the statusline, run `token-sparen statusline uninstall` first |

## What it writes to disk

- `<project>/.token-sparen/`: recovery files under `outputs/`, plus a `.gitignore` containing `*`
  so the folder is never committed. Created on first compression in that project.
- `~/.token-sparen/` (override with `TOKEN_SPAREN_HOME`): project index, per-project and
  per-session state (decision log, token ledger, read de-dupe memory), `logs/errors.jsonl`,
  sampled `logs/latency.jsonl`, and the optional `config.toml`. Project folders are keyed by a hash,
  never by a raw path.
- Claude Code `settings.json`: **only** if you run `token-sparen statusline install`
  (see below), with a backup and exact restore. Pruning tools from your settings is planned
  (M3) and will be opt-in too.
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
| `saved` | net tokens removed (after recovery reads), with an approximate dollar figure derived from the session's average cost per token reported by Claude Code |
| `trims` | `✂` trims made, `↺` recovery reads (regrets; counted from M2, shows 0 for now) |
| `learn` | learner state; `cold` until learning ships (M2) |

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
| `statusline.segments` | all six | which segments to show |
| `statusline.ascii`, `statusline.color` | false, true | plain glyphs; colours |
| `recovery.ttl_days`, `max_files`, `max_bytes` | 14, 200, 100 MB | recovery file retention |
| `adapter.latency_budget_ms` | 400 | hook time cap before it becomes a no-op |
| `extensions.allow_override` | false | let a trusted extension replace a built-in (user config or env only) |

## Extensions (opt-in, explicit trust)

Installed Python packages can add compressors, decision points, signals or host adapters through
the entry-point groups `token_sparen.compressors`, `.decision_points`, `.signals` and `.adapters`.
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
`[extensions] allow_override = true` is set in `~/.token-sparen/config.toml`.

**What trust means: trusting a drop-in gives it full code execution as you**, with your files and
credentials, every time a hook uses it. Only trust code you have read or whose author you trust.
The runtime checks (error lines kept, line counts consistent, failures disabled) protect you from
*buggy* extensions, not *malicious* ones. Trust follows content, not location: an identical copy of
a trusted file in another project also loads.

## Privacy

Everything is local. token-sparen has no telemetry and makes no network requests. State and
recovered outputs stay on your machine (a hash-keyed `~/.token-sparen/` and the project's
git-ignored `.token-sparen/`).

## How it learns (planned)

Planned for M2: a small constrained bandit chooses, per content type and project, how aggressively
to trim, under a hard quality constraint (it never trades quality for tokens in a weighted sum).
Until it ships, fixed conservative defaults apply and `learn` shows `cold`. Repo-fact injection and
tool pruning are planned for M3; `prune`, `facts`, `fold` and `replay` CLI commands are placeholders.

## Credits and licence

Cross-session ideas were inspired by TokenEconomizer (MIT, arXiv 2610.22732); no code was copied.
Released under the MIT licence: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions:
[CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md). Security:
[SECURITY.md](SECURITY.md).
