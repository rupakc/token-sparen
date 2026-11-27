# token-sparen

An open-source Claude Code plugin that cuts token usage without hurting output quality. It trims
noisy tool output (test runs, build logs, JSON, diffs, searches) before the model sees it, never
trims file contents by default, keeps the **full text on disk so nothing is lost**, blocks a few clearly unbounded commands, and
shows a live token meter. Everything runs locally.

## Quick start

1. Install: `claude plugin marketplace add rupakc/token-sparen`, then `claude plugin install token-sparen@token-sparen`; start a new session.
2. Verify: `/token-sparen doctor`, then ask Claude to run `pytest -v` (or any big test run) and look for the `[token-sparen: kept ...]` marker ([details](#install)).
3. Recover: `token-sparen show <id>` (id in the marker) or read the `full output:` path.
4. See what it did: `/token-sparen` (net tokens saved).
5. Turn off: `TOKEN_SPAREN_DISABLE=1` before launching Claude Code, `/token-sparen disable` for one session, or `claude plugin disable token-sparen` ([all options](#turning-it-off)).

## Status

v1.0 target, Claude Code only (other hosts are planned for a later release). The mechanisms below
work today; how strongly to trim is learned per project from your own sessions (see
[How it learns](#how-it-learns)), starting conservatively. This README makes no savings claims
beyond the [Evidence](#evidence) section below; measure on your own sessions with `/token-sparen`
or `token-sparen stats`. Both report **net** savings:
tokens removed by trims, minus the tokens the agent spent re-reading trimmed output, minus the
tokens token-sparen itself injected (`stats` also prints the gross figure and both parts; the
statusline `saved` segment uses the same net figure). Net can be zero or negative.

## Evidence

<!-- evidence:start -->
No published benchmark results yet. Results appear here only from the live benchmark (E4), each with its 95 % CI, number of tasks, seeds, model, date and the plugin hash it measured.
<!-- evidence:end -->

## Requirements

- Claude Code: the minimum, 2.1.121, is declared in the plugin manifest; tested with 2.1.332.
  Replacing a tool result needs the `updatedToolOutput` hook feature, which older hosts do not
  have: hooks would run, but nothing would be trimmed.
- Python >= 3.11 somewhere on your machine. No other runtime dependencies. The hooks start
  `python3 -I`; if that `python3` is older (macOS ships 3.9, Ubuntu 22.04 3.10), the launcher
  finds a newer Python and re-runs the hook on it: `TOKEN_SPAREN_PYTHON` (a path or command
  name, if you set one), then `python3.15` ... `python3.11` and `python3` on your `PATH` (on
  Windows also `py -3.15` ... `py -3.11` and `py -3`), then `uv python find ">=3.11"` if `uv`
  is installed (with downloads disabled; set `TOKEN_SPAREN_NO_UV=1` to skip uv). A candidate is
  used only if it reports version >= 3.11 itself. The choice is cached in
  `~/.token-sparen/launcher.json`, so the search runs once (again when `python3` or the chosen
  interpreter changes or disappears); the whole search is bounded to 2 seconds. The cost of the
  extra interpreter start is measured by `tests/perf/test_launcher_reexec.py`: about 40-55 ms
  per hook call on the development machine, paid only when your `python3` is older than 3.11
  (without the launcher those hooks would not run at all); you avoid it by putting Python >= 3.11
  first on your `PATH` as `python3`. `token-sparen doctor` names the interpreter the hooks
  actually use and fails if there is none.
- `TOKEN_SPAREN_PYTHON` must be set in the environment Claude Code is started from (your shell
  profile); `config.toml` cannot set it, because the launcher runs before any Python >= 3.11 can
  read it. It is honoured only if it resolves outside the current project and its git work tree
  and, on macOS/Linux, the interpreter file and its directory are owned by you or root and not
  writable by group or others (a repository's committed Claude Code settings can set environment
  variables); otherwise it is skipped with one line in `logs/errors.jsonl`. Some installers
  (for example python.org's macOS framework, group `admin` writable) fail this check; such a
  Python is still found through `PATH` (`python3.12` etc.).
- macOS and Linux. Windows: hooks need a `python3` on `PATH` to start (`token-sparen doctor`
  suggests `py -3` or `python` if it is missing), and the statusline launcher is not supported yet.

**Troubleshooting: "token-sparen is inactive" / nothing is ever trimmed.** The hooks found no
Python >= 3.11. They keep your session working (fail open), show this warning at most once a day
and log one line a day to `~/.token-sparen/logs/errors.jsonl` (`"kind": "launcher"`). Install
Python 3.11 or newer (python.org, Homebrew, your package manager, or `uv python install 3.12`), or
`export TOKEN_SPAREN_PYTHON=/path/to/python3.12` in your shell profile, and restart Claude Code.
A "nothing found" result is remembered for up to 10 minutes; a new `python3.x` on `PATH` is
noticed at once, but a Python installed only through uv (found by `uv python find`) is picked up
after those 10 minutes (or delete `~/.token-sparen/launcher.json`). Then run
`token-sparen doctor` in a terminal: its `hook python` line shows which interpreter the hooks
will use. `doctor` itself needs a Python >= 3.11 too: `python3 -I <plugin dir>/hooks/run_cli.py
doctor` finds one the same way, and says so when there is none.

## Install

```
claude plugin marketplace add rupakc/token-sparen
claude plugin install token-sparen@token-sparen
```

The marketplace entry tracks the `release/claude` branch of this repository (the built plugin
tree). To work on token-sparen itself, see [CONTRIBUTING.md](CONTRIBUTING.md) for the development
install.

That is all the plugin needs. Start a new Claude Code session afterwards (or reload plugins) so
the hooks are picked up.

**Using the CLI as a plugin-only user.** You do not need the `token-sparen` command: ask Claude
`/token-sparen` (stats), `/token-sparen doctor`, `/token-sparen disable`, and it runs the bundled CLI
for you. To run it yourself in a terminal, call the bundled launcher from the plugin directory:
`python3 -I <plugin dir>/hooks/run_cli.py <args>` (for example `... stats`; it re-launches on a
Python >= 3.11 the same way the hooks do). The plugin directory is the
`installPath` entry for `token-sparen` in `~/.claude/plugins/installed_plugins.json`.

Optional: the `token-sparen` console command (handy in your own terminal). It is **not published on PyPI yet**;
from a checkout of this repository run `pip install .`.

**Verify.** Three steps:

1. `/token-sparen doctor` in Claude Code (or `token-sparen doctor` in a terminal). It checks Python,
   the state directory, hook files and a synthetic compress-and-recover round trip. That round
   trip runs inside token-sparen, so it cannot tell whether your Claude Code honours the hooks.
2. Smoke test in a real session: start a new Claude Code session and ask Claude to run your test
   suite verbosely (for example `pytest -v`) so that the output is longer than about 2000 tokens.
   Usually the result is trimmed and ends with a `[token-sparen: kept ... · full output: ...]`
   marker (see [Recovering trimmed output](#recovering-trimmed-output)), and `/token-sparen` then
   shows a non-zero trim count. On a brand-new project roughly one in five large outputs is
   deliberately left whole at first (a baseline the learner needs), so if you see no marker, try
   a second time. If you still never see one, check the Claude Code version above.
3. Without the host: `token-sparen run -- <a command with big output>` prints what token-sparen
   would show for that command, marker included. It does not prove your Claude Code honours the
   hooks; the smoke test does.

## What changes in your session

All of this is on by default. Nothing is sent anywhere.

- **Compression (quality first).** When a tool result is **at least 2000 estimated tokens** and
  is one of these kinds of output, token-sparen may replace it with a shorter version that keeps
  the head and tail, every error line and failures, and ends with a recovery marker:
  - diffs (`git diff`, `git show <commit>`, `git log -p`, `git stash show -p`) and search results (Grep/Glob, `grep`, `rg`);
  - test reports (pytest, jest, vitest, go test, cargo test, ...), compiler output (tsc, rustc,
    gcc, javac, ...), build and install logs (npm, pip, gradle, docker build, ...);
  - JSON (API responses, MCP tools, `curl`), `pplx` search results and timestamped logs.

  By default it does **not** trim **file contents**: the Read tool; any tool (MCP included)
  whose name says it reads a file (`read_file`, `ReadFile`, `get_file_contents`, `file_read`,
  `read_text`, `cat_file`, `view_file`, `open_file`); and any shell or PowerShell command with a
  part that prints a file, wherever it sits in a chain, pipe or loop (`cat`, `head`, `tail`,
  `sed -n`, `less`, `bat`, `nl`, `awk`, `jq <file>`, `xxd`, `sort`/`uniq`/`cut`/`rev`/`strings`
  on a file, `python -c`/`node -e` code that opens a file, `find -exec cat`, `... | xargs cat`,
  `git show <rev>:<path>`, `Get-Content`/`gc`/`type`; also inside `$(...)`, backticks, `eval`,
  `ssh`/`docker exec`/`kubectl exec` and similar wrappers, and any command whose stdin is
  redirected from a file). Nor does it trim plain text, tables or stack traces from any
  tool (Agent/Task and web results included). Those outputs are shown in full and never reach
  the learner. To widen it, in your own `~/.token-sparen/config.toml` or `config set` (a
  project file or a `TOKEN_SPAREN_*` variable may only narrow it):

  ```toml
  [compression]
  trim_file_contents = true   # also trim Read results and cat/head/tail/... output
  allow_ctypes = ["diff", "search", "test_report", "compiler", "build_log", "json", "pplx", "log", "text"]
  ```

  File-content detection is best effort: an unusual binary or wrapper may slip through, and
  scripts you run (`./x.sh`, `bash x.sh`) are not inspected. To stop all trimming, see
  [Turning it off](#turning-it-off).

  Quality warning: the agent usually goes on to use a file's contents (to edit or quote it), so
  trimming them often costs a recovery read and can cost answer quality; on the maintainer's own
  sessions it saved little once those re-reads were counted. You can check this on your own
  sessions with `token-sparen replay --policy static` against `--policy default` (see
  [Measuring it on your own sessions](#measuring-it-on-your-own-sessions-offline-private)).
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
  not compacted since, and of at least 2000 estimated tokens) returns a short pointer,
  `[token-sparen: unchanged since turn N ...]`, instead of the file again. This is not covered
  by `trim_file_contents`: it replaces a repeat of something the model already has, not the first
  read. It is a learned decision like compression (a small share of re-reads always returns the
  full file; with learning off it always returns the pointer). Turn it off with
  `read_dedupe.enabled = false`, or hold it with `[learn.hold] read_dedupe = true`.
- **Notices (terminal only, no model tokens).** Budget notices when your session passes 50 % and
  80 % of the configured token budget (default 500M billed context tokens), context-window notices at 60 % and 80 %, and at most an
  occasional "this looks like a new task, consider `/compact`" hint when context is fairly full.
  token-sparen never compacts for you; before a compaction it asks that recovery paths be kept in
  the summary.
- **Hard stop (opt-in, off by default).** With `governor.hard_stop = true`, once the session passes
  100 % of `session_budget_tokens`, context-adding tools (Read, Bash, WebFetch, WebSearch, Grep,
  Glob, LS, Task/Agent, MCP tools) are denied. Edit tools are never blocked.
- **Failure-loop guard.** When an agent (the main agent or one subagent, each counted on its own)
  hits the same error again and again within one turn, token-sparen adds one short line to that
  tool result for the model: `[token-sparen: this error has repeated 3× this turn (<id>).
  Consider stepping back: re-read the error, check assumptions, or try a different approach.]`
  "The same error" means the same tool, the same command (its program and subcommand, e.g.
  `pytest`, `git diff`) or file, and the same error line once directories, line:column, hex
  ids, durations and long numbers are ignored; names, quoted values and literals still count, so
  `No module named 'foo'` and `'bar'`, or `assert 3 == 5` and `assert 4 == 5`, are different
  errors. For test runs (pytest, jest, go test, cargo test) it is the whole set of failing tests (count and hash) plus
  the first failure's error, so fixing a suite one test at a time never looks like a loop. A
  failure with no error line (a bare `Exit code 1` from `grep`, `test` or `diff`, or only a
  "3 failed, 2 passed" summary) is never counted. The same edit made and undone twice on one file (A -> B -> A -> B; adding and
  then removing a debug line is fine) gets `[token-sparen: the same edit has been made and
  undone 2× this turn (<id>). ...]`. The tool output itself is never changed. At most one such
  line per agent and turn; if it happens again you get a terminal notice instead (no model
  tokens). How many repeats it waits for (5, 3 or 2) is learned, starting at the most patient
  setting. Only a line that was actually shown is judged: useful when the error then stops
  recurring within the agent's next 5 tool calls. A less patient setting is used only once its
  lines clearly end loops more often than loops end by themselves (measured on turns where an
  error reached 3 repeats without a line, per tool category and project with a cross-project
  fallback, and assumed high, 80 %, until measured); until then it is tried in about 5 % of
  loops. A line counts
  as harmful when you interrupt or your next prompt starts with stop, wait, undo, revert,
  "that's wrong" or "no, don't" (a prompt of at most 8 words may contain it anywhere). Only
  deterministic checks, no LLM. Turn it off with `loopguard.enabled = false`; `[learn.hold] loopguard = true`
  does **not** turn it off, it only keeps the most patient setting (5 repeats).
  `loopguard.min_k` sets the fewest repeats.
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

| To stop | How |
|---|---|
| Everything | `TOKEN_SPAREN_DISABLE=1` (every hook becomes a no-op; any of `1`, `true`, `yes`, `on`). It must be in the environment Claude Code is **launched** from; setting it in a running session's shell is too late. Or `claude plugin disable token-sparen` (and `claude plugin enable token-sparen`) |
| Everything, this session only | `/token-sparen disable` (and `/token-sparen enable`), or `token-sparen session disable` (with no `--session` it acts on this project's most recently active session, if active in the last 6 hours; otherwise pass `--session ID`) |
| All trimming of tool output | `[learn.hold] compression = true` in `~/.token-sparen/config.toml` (any truthy spelling: `true`, `1`, `yes`, `on`). It keeps compression and the learned pre-trim at their safe arm, whether learning is on or off, so nothing is trimmed. For one session only: `token-sparen config set learn.hold.compression true`. A project's `token-sparen.toml` may set it too. Config-only alternative: `[compression] allow_ctypes = []` (no content type may be trimmed). Guards and read de-duplication stay on |
| Read pointers only | `[learn.hold] read_dedupe = true` or `read_dedupe.enabled = false` |
| Learned repository facts | `token-sparen facts disable` (this project; `facts enable` undoes it), `[facts] enabled = false` (everywhere; a project's `token-sparen.toml` may set it too), or `token-sparen facts forget <id>` for one fact (see [Learned repository facts](#learned-repository-facts)) |
| Failure-loop guard | `[loopguard] enabled = false` (a project's `token-sparen.toml` may set it too). `[learn.hold] loopguard = true` does not switch it off, it only keeps the most patient setting |
| Output-style directive | off unless you opted in (`[output_style] enabled = true` in your `config.toml`, or `token-sparen config set output_style.enabled true` for one session); `enabled = false` switches it off again (a project's `token-sparen.toml` or a `TOKEN_SPAREN_*` variable may only set that) |
| Tool pruning | off unless you opted in; `token-sparen prune disable` opts out and removes every rule token-sparen wrote, `prune restore <tool>` gives one tool back, and a project's `token-sparen.toml` may set `[prune] allow = false` (see [Tool pruning](#tool-pruning-opt-in)) |
| Blocking, notices, other mechanisms | `guards.enabled`, `governor.enabled`, `boundary.enabled` or `subagent.enabled` set to `false` |
| Uninstall | `claude plugin uninstall token-sparen@token-sparen`; run `token-sparen statusline uninstall` first if you installed the statusline |

There is no `compression.enabled` key; use the hold or `allow_ctypes` row above. Setting
`[learn] enabled = false` does **not** stop trimming: it falls back to fixed defaults (standard
compression), which trims more than a cold learner does.

Uninstalling leaves `~/.token-sparen/` and each project's `.token-sparen/` in place; both are safe
to delete.

**Full uninstall**, in this order (the settings edits live outside the state folder, so deleting
`~/.token-sparen/` alone does not undo them):

1. `token-sparen prune disable` in each project where you enabled tool pruning: it removes every
   deny rule token-sparen wrote to `.claude/settings.local.json` (in every recorded checkout).
2. `token-sparen statusline uninstall`: restores your original `statusLine`.
3. `claude plugin uninstall token-sparen@token-sparen` (and `pipx uninstall token-sparen` or the
   like if you installed the CLI for your terminal).
4. Delete `~/.token-sparen/` and each project's `.token-sparen/` folder. Backups named
   `*.token-sparen.bak` next to your settings files can go too once you have checked them.

After a downgrade (an older token-sparen meeting state a newer one wrote), the older version
never rewrites that state. `prune disable` / `prune restore` then change nothing (which deny
rules are token-sparen's cannot be read): they list the rules in token-sparen's format that are
not in the `.token-sparen.bak` backup, with the file to edit by hand (keep any you added
yourself). `statusline uninstall` reverts the `statusLine` it recognises by its launcher; for
the rest each command prints `state written by token-sparen <version>; upgrade to it, or run
<command> from that version`.

## What it writes to disk

Everything stays on your machine, but some of it is plain text you should know about.

- `<project>/.token-sparen/`: recovery files under `outputs/` (full tool outputs, so they can
  contain anything a command printed), plus a `.gitignore` containing `*` so the folder is never
  committed. Created on the first compression in that project, owner-only (mode 0700 on POSIX;
  an older, looser folder of yours is tightened on the next write). Nothing is written, read or
  pruned through a symbolic link (a `.token-sparen`, `outputs` or `<id>.txt` link, e.g. committed
  by a repository, means nothing is saved and the output stays whole), and `token-sparen show`
  serves only outputs this install saved for this checkout.
- `~/.token-sparen/` (owner-only, mode 0700 on POSIX: created so, and an older looser root of
  yours is tightened; a directory not owned by you is never used; override with `TOKEN_SPAREN_HOME`; an override inside the current project
  or its git work tree (however it is spelled), or (POSIX) not owned by you, is ignored with one
  line in the default root's `logs/errors.jsonl`, because a repository's committed Claude Code
  settings can set environment variables; a not-yet-created path is accepted when its nearest
  existing parent is yours or a root-owned sticky dir such as /tmp. The statusline launcher
  applies the same rule to `TOKEN_SPAREN_ROOT`, except that its root must already exist and be
  yours):
  - `config.toml` (yours, optional), `trusted_extensions.json`, `statusline_install.json` (records
    your settings file's path and original `statusLine`), `statusline.sh` (launcher copy),
    `windows.json` (context window per session), `project_keys.json` (index from project to hash
    key), `config_safety_warned.json` (which ignored project-config values were already reported:
    a short hash of the project path, the key and the value; and a short hash of an ignored
    `TOKEN_SPAREN_HOME` / `TOKEN_SPAREN_ROOT` value), `prune_rules.json` (only with
    [tool pruning](#tool-pruning-opt-in): per checkout the **path of its settings file and of the
    checkout**, its project key, the deny rules token-sparen wrote there and what it created,
    rules about to be written, and pending notices; kept here, beside the statusline record, so a
    changed git remote never loses track of a rule);
  - `launcher.json`: the hook launcher's cached interpreter choice (only used when your `python3`
    is older than 3.11): per hashed key (plugin directory, `python3`, `PATH`,
    `TOKEN_SPAREN_PYTHON`) the **path of the chosen Python**, how it was found and its
    modification time, or that none was found (re-checked after 10 minutes or when a candidate
    appears);
  - `logs/errors.jsonl` and sampled `logs/latency.jsonl`; `logs/launcher-warned-<date>` (empty
    marker: the "no Python >= 3.11" warning was already given today); `logs/once.json` (short hashes of
    store path and session, so a broken or newer state file is reported once per session);
  - `recovery/<hash>/<id>`: one empty file per recovery file this install saved, per checkout (the
    folder name is a hash of the checkout path), so `show` never serves a file a repository
    planted;
  - small `<file>.lock` files next to the JSON files above, used for file locking;
  - `projects/<key>/`: `decisions.jsonl` (per-decision log; keeps the first 500 characters of Bash
    commands, and for each trim an outcome line with its settled regret value and signal; it is
    rotated to `decisions.jsonl.1` past 20 MB, which outcome lines make happen sooner), `sessions/<id>.json` (session overlay: the **last user prompt, up to 2000
    characters**, recently read/edited **file paths**, the last 10 tool categories (for the
    learning engine's task phase), identifiers used for task-boundary
    detection, the token ledger and read de-dupe memory, and the session's learned evidence, bucket
    labels and numbers only), `sessions/regret/<id>.json` (for each trim of the last few turns: the
    normalised command, up to 64 distinctive words of the trimmed part of the output stored
    **only as hashes** (not plain text) and up to 32 **file paths** it mentions, used to notice when
    a trim hurt), `sessions/pending/<id>.json` (the learner's decisions awaiting their reward: ids,
    bucket labels, arms, times; with `learn.ladder_retry_decisions` on, per-bucket decision counts), `last_active.json`
    (the project's most recent session id and when it was last active, plus up to 50 recent
    session ids with their last activity, removed when a session ends), `savings_scale.json` (numbers only: recent savings
    sizes per bucket), `floor_warned.json`, `style_warned.json` (only a hash of an unknown `output_style.directive` already reported), `posteriors.json` and `breaker.json` (numbers only:
    learned evidence per bucket, the loop guard's natural break rate per bucket, and recent
    regret values per decision point; an older layout is first copied to
    `posteriors.v<N>.bak.json`), `pending.json`
    (ids of sessions waiting to be folded) and `pretrim.json`
    (**normalised Bash commands** with per-command counts, and the learned pre-trim candidates),
    `facts.json` (repository facts mined when a session is folded: **normalised
    commands** such as the test command or a working spelling of a missing command,
    **environment variable names** a command needed (never their values), **directory names**
    relative to the project root where generated files live, and for each fact short hashes of
    the session ids that showed it; every entry passes a strict data-only filter that rejects
    URLs, shell operators, absolute paths, instruction-like words and anything that looks like a
    secret; per fact also its status, a few learned numbers and a contradiction count; the ids of
    facts you forgot and whether injection is disabled for the project). The session overlay
    records which facts were injected or withheld at session start (fact ids and
    probabilities). During a turn the session overlay keeps the loop guard's state per agent (up to 16
    agents, keyed by Claude Code's subagent id): up to 50 error fingerprints and up to 50 edited
    files per turn in total, each **only as short hashes** (of the tool, command head or file path and the
    normalised error, the file path and the edited text), with counts, and the session's natural
    break-rate counts (numbers only). The session overlay also keeps the settled regret values of its trims, each with the
    normalised command, until the session is folded, and the **path of the session's Claude Code
    transcript** and the project directory, which only the background fold reads (at most 50 MB
    per session) to mine those facts. Project folders are named by a hash, but the
    files inside hold the plain text above.
  - `global/posteriors.json`: numbers only (evidence pooled across projects, keyed by decision
    point, feature bucket and arm, plus recent fold ids per salted project tag); no paths,
    commands, project keys or session ids. `secret` (random, mode 0600) salts those tags.
  - `projects/<key>/prune.json` (only once you run `token-sparen prune enable` or enable pruning
    in your own config): whether pruning is enabled here, **tool names** (built-in and MCP)
    the project's sessions (and, once at enable, its transcripts of the last 60 days) listed and
    used, per candidate counts of sessions and prompts and a learned regret posterior (numbers),
    and tools restored once (the per-checkout records live in `prune_rules.json`).
- Claude Code `settings.json`: **only** if you run `token-sparen statusline install` (see below),
  with a backup and exact restore.
- `<project>/.claude/settings.local.json`: **only** if you opt in to
  [tool pruning](#tool-pruning-opt-in), and only when git confirms the file is untracked and
  ignored: bare `permissions.deny` entries, appended by the background session-end fold, with a
  backup `settings.local.json.token-sparen.bak` next to it and exact restore (`prune restore
  --all`, `prune disable`).
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

Illustrative example (the numbers are made up; colours on a terminal, `NO_COLOR` or
`statusline.ascii = true` simplify it):

```
⛁ ts 42% ctx │ burn 48M/500M 10% │ +2.4k/turn │ saved 31.2k (≈$0.84) │ ✂ 12 ↺ 1 │ learn: cold
```

| Segment | Meaning |
|---|---|
| `ctx` | share of the context window used by the last call |
| `burn` | billed context tokens summed over calls, against the session budget |
| `rate` | recent context growth per turn (`+2.4k/turn`) and your 5-hour rate-limit use when Claude Code reports it |
| `saved` | estimated tokens removed by this session's trims minus recovery reads of trimmed output and the tokens token-sparen itself injected, with an approximate dollar figure derived from the session's average cost per token reported by Claude Code. Only the output kinds listed under [What changes in your session](#what-changes-in-your-session) are ever trimmed, so on file-heavy sessions it stays small or at 0 by design. It counts each removed token once (not its later re-sends) and can be negative |
| `trims` | `✂` trims made, `↺` regrets detected (recovery reads, re-runs) |
| `learn` | learner state: `cold` (< 20 observations), `adapting`, `exploiting` (>= 80 % of recent decisions pick the best-known arm) or `safe-mode` (a decision point is paused by the circuit breaker) |

Choose segments with `statusline.segments`.

## Configuration

Later layers override earlier ones:

1. packaged defaults (`token_sparen/params/defaults.toml`, the full list of keys)
2. `~/.token-sparen/config.toml`
3. `<project>/token-sparen.toml`
4. environment: `TOKEN_SPAREN_<SECTION>__<KEY>=value` (double underscore between parts, for
   example `TOKEN_SPAREN_GUARDS__ENABLED=false`, `TOKEN_SPAREN_COMPRESSION__MIN_TOKENS=5000`;
   list values are comma-separated). **Narrowing only:** a repository's committed Claude Code
   settings can set environment variables, so a variable follows the same rules as a project file
   (see below): it may make token-sparen intervene less or keep more, never more; anything else
   is ignored and logged once. Widen in `~/.token-sparen/config.toml` or with `config set`.
   User-only keys are ignored from the environment even when they would narrow; use
   `TOKEN_SPAREN_DISABLE=1`, `TOKEN_SPAREN_GOVERNOR__ENABLED=false` or
   `TOKEN_SPAREN_PRUNE__ALLOW=false` instead of `LEARN__ENABLED`, `GOVERNOR__HARD_STOP` or
   `PRUNE__ENABLED`.
5. this session only: `token-sparen config set <section.key> <value> [--session ID]`
   (e.g. `config set guards.enabled false`)

`compression.min_tokens` can raise the 2000-token floor but never lower it (a lower value is
ignored and `doctor` flags it).

**What a project file may change.** `<project>/token-sparen.toml` is committed, so it may be
someone else's. It can only make token-sparen intervene less or keep more, never trim more,
recover less or block more. A value is used only if it has the right type, is within sane
bounds and moves in the allowed direction from your own value (defaults, your config, env,
session); anything else is ignored and logged once. `TOKEN_SPAREN_*` variables follow the same
table, measured from your config and session overrides.

| Settings | A project may |
|---|---|
| `statusline.{segments,ascii,color,parse_budget_ms}`, `pricing.*`, `boundary.{min_ctx_pct,min_overlap,min_turn,history,min_identifiers,cooldown_turns}`, `governor.{econ,ctx}_{notice,advise}` | set freely within bounds (display, advice) |
| `guards.enabled`, `read_dedupe.enabled`, `governor.enabled`, `boundary.enabled`, `subagent.enabled`, `facts.enabled`, `loopguard.enabled`, `output_style.enabled` | only turn off |
| `compression.min_tokens`, `min_saving_ratio`, `levels.*.keep_ratio`, the per-type knobs (`json.items`, `diff.context`, `stacktrace.edge_frames`, `test_report.block_lines`, `search.per_file`, `pplx.results`, `log.tail`), `recovery.*`, `guards.max_read_bytes`, `guards.pretrim_bounded_keep_ratio`, `read_dedupe.min_tokens`, `loopguard.min_k` | only raise (keep more, intervene later) |
| `compression.compiler.warnings.*` | only move from `count` to `keep` |
| `compression.skip_tools` | only add tools |
| `compression.allow_ctypes` | only remove content types |
| `compression.trim_file_contents`, `prune.allow` | only set `false` (`prune.allow = false`: never prune here, and token-sparen's rules are reverted) |
| `read_dedupe.max_turn_gap`, `read_dedupe.max_entries`, `compression.save_deadline_ms`, `adapter.latency_budget_ms`, `facts.max_facts`, `facts.max_tokens`, `prune.lease_sessions` | only lower (`prune.lease_sessions`: pruned tools are restored sooner) |
| `learn.tau.<tier>` lower; `learn.floor.<tier>`, `learn.ladder_obs` higher; `learn.hold.<point>` only `true`; `learn.breaker_min_obs`, `learn.breaker_every` lower; `learn.breaker_lcb_q`, `learn.breaker_sessions` higher | only make learning more careful |
| everything else, including `learn.enabled` and all other `learn.*`, `compression.levels.*.{head,tail}` and `guards.pretrim_{lines,head}` (a larger head/tail displaces prompt-relevant lines), `governor.hard_stop`, `governor.session_budget_tokens`, `governor.windows`, `adapter.latency_sample_rate`, `adapter.latency_log_max_bytes`, `extensions.*`, the other `facts.*` keys, the other `prune.*` keys (including `prune.enabled`: pruning is opted in only with `prune enable` or your own `config.toml`), `loopguard.watch_calls`, `output_style.directive`, `ope.*`, and any key not listed | nothing (your `config.toml` or `config set` only; a `TOKEN_SPAREN_*` variable is ignored too) |

| Key | Default | Meaning |
|---|---|---|
| `compression.min_tokens` | 2000 | outputs below this are never modified (floor) |
| `compression.min_saving_ratio` | 0.15 | minimum share saved for a replacement to be used |
| `compression.skip_tools` | edit tools, TodoWrite, ... | tools whose output is never compressed |
| `compression.allow_ctypes` | diff, search, test_report, compiler, build_log, json, pplx, log | content types that may be trimmed (add `text`, `table`, `stacktrace` to widen; `[]` trims nothing) |
| `compression.trim_file_contents` | false | also trim file contents (Read, `cat`, `head`, ...); see the quality warning above |
| `guards.enabled` | true | block the unbounded commands listed above |
| `guards.max_read_bytes` | 5000000 | size limit for `cat`/Read without a range |
| `read_dedupe.enabled` | true | return a pointer for unchanged re-reads |
| `read_dedupe.max_turn_gap` | 8 | pointer only if the earlier read is this recent |
| `governor.enabled` | true | budget and context notices |
| `governor.session_budget_tokens` | 500000000 | economy budget the notices and hard stop use |
| `governor.hard_stop` | false | deny context-adding tools at 100 % of the budget |
| `boundary.enabled` | true | suggest `/compact` at task boundaries |
| `subagent.enabled` | true | subagent brief decision point (its default arm is off) |
| `facts.enabled` | true | inject learned repository facts at session start ([details](#learned-repository-facts)) |
| `facts.max_facts`, `facts.max_tokens` | 8, 300 | size of the injected facts block |
| `loopguard.enabled` | true | one redirect line when the same error repeats within a turn |
| `loopguard.min_k` | 2 | fewest repeats of one error before the redirect (the learned setting is 2, 3 or 5) |
| `learn.hold.loopguard` | false | `true` keeps the most patient setting (5 repeats); it does not switch the guard off |
| `loopguard.watch_calls` | 5 | tool calls after the redirect that decide whether the loop broke (user only) |
| `output_style.enabled` | false | one line at session start (startup, `/clear`, after compaction; never on resume) asking for concise prose while never shortening code, commands, file paths, error messages or quoted tool output; opt-in only: enabled only from your `config.toml` or a session override (`config set`); a project or a `TOKEN_SPAREN_*` variable may only switch it off (spec 6.6) |
| `output_style.directive` | `concise` | which directive; an unknown name means off and is logged once (user only) |
| `prune.enabled` | false | tool pruning ([opt-in](#tool-pruning-opt-in)); read only from your own `config.toml` (or set per project by `token-sparen prune enable`) |
| `prune.builtins` | false | also prune built-in tools an SDK `init` record listed as always loaded (your `config.toml` only) |
| `prune.allow` | true | `false`: never prune here and revert token-sparen's rules (a project may set it) |
| `prune.min_sessions`, `prune.min_prompts` | 3, 15 | unused sessions and prompts before a tool is a candidate (user only) |
| `prune.tau`, `prune.ucb_q` | 0.01, 0.95 | prune only when the regret bound `ucb(ucb_q) <= tau` (about 300 unused prompts; user only) |
| `prune.idle_minutes` | 60 | new rules are written only if no other session of the project was active this long (user only) |
| `prune.lease_sessions` | 15 | a pruned tool is restored after this many sessions to observe it again (a project may only lower it) |
| `learn.enabled` | true | learn how strongly to trim; `false` uses the fixed defaults, which trim more than a cold learner (your `config.toml` or `config set` only) |
| `learn.hold.compression` | false | `true` stops all trimming of tool output (see [Turning it off](#turning-it-off)) |
| `learn.hold.read_dedupe` | false | `true` stops read pointers |
| `learn.savings_reward` | `"scaled"` | savings reward of a trim: `scaled` (removed x resends / recent p95), `fraction`, `counterfactual` (vs the best arm on the same output; `learn.resend_horizon`, 20) or `cost` (cache-weighted); a tuning option (your `config.toml` or `config set` only) |
| `learn.ladder_retry_decisions` | 0 | `N > 0`: re-open a locked, harsher trimming level for one probing decision after N decisions of that bucket within one session (counted per session; user only) |
| `guards.pretrim_bounded_keep_ratio` | unset | share of lines a learned pre-trim keeps; unset is the larger of the packaged and your configured `compression.levels.standard.keep_ratio` (raising the standard arm raises the pre-trim, lowering it never lowers the pre-trim), any number is used as is (a project may only raise it) |
| `statusline.segments` | all six | which segments to show |
| `statusline.ascii`, `statusline.color` | false, true | plain glyphs; colours |
| `recovery.ttl_days`, `max_files`, `max_bytes` | 14, 200, 100 MB | recovery file retention |
| `adapter.latency_budget_ms` | 400 | hook time cap before it becomes a no-op |
| `extensions.allow_override` | false | let a trusted extension replace a built-in (your `config.toml` only) |

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
`[extensions] allow_override = true` is set in `~/.token-sparen/config.toml`; a project
`token-sparen.toml`, `config set` and `TOKEN_SPAREN_*` variables cannot enable it.

**What trust means: trusting a drop-in gives it full code execution as you**, with your files and
credentials, every time a hook uses it. Only trust code you have read or whose author you trust.
The runtime checks (error lines kept, line counts consistent, failures disabled) protect you from
*buggy* extensions, not *malicious* ones. Trust follows content, not location: an identical copy of
a trusted file in another project also loads.

## Settings it may change (statusline, tool pruning)

Two opt-in features write Claude Code settings, each backed up and reversible: the
[live statusline](#live-statusline-opt-in) (`token-sparen statusline install`) and
[tool pruning](#tool-pruning-opt-in) (deny rules, only on git confirmation). Nothing else edits
your settings; details are in [What it writes to disk](#what-it-writes-to-disk).

## Privacy

Nothing leaves your machine. token-sparen has no telemetry and makes no network requests. Its local
files are plain text, though: recovered outputs, your last prompt, file paths and command prefixes
are stored as described in [What it writes to disk](#what-it-writes-to-disk). Delete
`~/.token-sparen/` and `<project>/.token-sparen/` at any time.

Replay and the hill-climbing harness read your own transcripts on your machine only; their
reports are written with owner-only permissions outside any git work tree, and the project never
commits figures derived from anyone's sessions.

## How it learns

A small constrained bandit chooses, per decision point and context bucket (context fill, tool
category, output size, content type, task phase), how strongly to trim, under a hard quality
constraint: it never trades quality for tokens in a weighted sum. Each trim is watched for signs
it hurt (the agent reading the saved full output, re-running the command, asking for trimmed
content); a trimming level is used only while its plausible regret stays under a target (5 % for
compression). A new project starts with the lightest level and unlocks stronger ones one at a time
only after the previous one has been observed at least 20 times with a low regret estimate
(experience from your other projects counts toward that, so a new project may unlock stronger
levels sooner), so early sessions trim less than later ones. A share of decisions always keeps the output untouched (roughly one in five at first on a new project), as a baseline. The
subagent brief stays off (it is not learned until a benchmark shows it does not hurt quality).
`[learn] enabled = false` switches learning off, but that is not a way to trim less: it uses fixed
defaults (standard compression, read de-dupe pointers), which trim more than a cold learner. To
trim nothing, use the hold in [Turning it off](#turning-it-off). A project's `token-sparen.toml`
can only make learning more careful (see [What a project file may change](#configuration)); it
cannot turn learning off, so a project that wants less trimming sets
`[learn.hold] compression = true`. When a session ends, a background `token-sparen fold` merges what the session learned into the
project (and, weighted 0.3, into a cross-project global store that forgets after 90 idle days);
sessions it missed are folded at the next session start. A circuit breaker pauses a decision point
for the project for 3 sessions when the learner's recent trims clearly caused re-reads at more than
twice the target rate, and tells you once. The starting values of the learner's priors are
hand-set for now.

### Across sessions: facts, tool pruning, loop guard

Three mechanisms learn from earlier sessions of the same project. None of them uses an LLM, and
there are no measured savings figures for them yet.

| Mechanism | What it does | Default | Switch off |
|---|---|---|---|
| [Learned repository facts](#learned-repository-facts) | at session start, a short labelled block of things earlier sessions found out (a working spelling of a missing command, the test command, a required variable name) | on (injects only well-evidenced facts) | `token-sparen facts disable` or `[facts] enabled = false` |
| [Tool pruning](#tool-pruning-opt-in) | denies MCP servers this project never uses, in the checkout's `.claude/settings.local.json`; any attempt to use one restores it at once | **off** (opt-in) | `token-sparen prune disable` (also reverts every rule) |
| Failure-loop guard ([details](#what-changes-in-your-session)) | one redirect line when the same error repeats within a turn | on | `[loopguard] enabled = false` |

`TOKEN_SPAREN_DISABLE=1` switches all three off along with everything else: every hook then
returns "no change" and writes nothing.

### Tool pruning (opt-in)

MCP servers you have configured but never use in a project still put their tool names and
server instructions into every request. Tool pruning removes such **unused MCP servers** from the
model's context in that project. The saving is modest (those servers' tool names and
instructions, on every request); token-sparen cannot measure it from hooks, so
the statusline (`⊘ N`) and `token-sparen stats` show only the number of pruned tools. Built-in
tools are left alone by default: in interactive Claude Code they are deferred and cost a few
tokens each. It **writes the checkout's `.claude/settings.local.json`**, so it is off until you
opt in:

```
token-sparen prune enable     # opt this project in (or [prune] enabled = true in ~/.token-sparen/config.toml)
token-sparen prune status     # state, pruned tools, candidates and their evidence
token-sparen prune restore mcp__github   # give one server back now (it is never pruned again)
token-sparen prune restore --all         # remove every rule token-sparen wrote (exact revert)
token-sparen prune disable    # opt out; also restores everything, in every checkout
```

Only `prune enable` or `[prune] enabled = true` in your own `~/.token-sparen/config.toml` turn it
on: `TOKEN_SPAREN_PRUNE__ENABLED` is ignored (a repository's committed Claude Code settings can
set environment variables), and a project's `token-sparen.toml` can only switch it off
(`[prune] allow = false`, which also reverts token-sparen's rules). `[prune] builtins = true` in
your `config.toml` also prunes built-in tools, but only those an SDK `init` record listed as always
loaded.

What it does: enabling first reads the project's earlier Claude Code transcripts (last 60 days, at
most 50 MB) so that nothing used before is ever pruned, and says how many it found. Nothing is
pruned until such a read has succeeded: if none were found, the first recorded session's folder
is read instead. `prune` commands find the project from `--project`, else `CLAUDE_PROJECT_DIR`,
else the git top level of the current directory; `prune restore <tool>` restores it wherever
token-sparen pruned it in this project (also in another checkout). When a session is folded, token-sparen
reads the tools the session had (from the transcript's tool list where Claude Code records one; a
session without one counts for the tools other sessions listed) and the tools it used, including
in subagents. A **whole MCP server** none of whose tools was ever used that stayed unused across
at least 3 sessions and 15 prompts becomes a candidate; it is pruned only once a deliberately
strict regret bound clears (about 300 unused prompts with the defaults). Never pruned: `Read`,
`Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Agent`/`Task*`, `TodoWrite`, `AskUserQuestion`,
`Skill`, `ToolSearch`, plan mode, `SendMessage`, and any tool ever used in the project. Pruning
adds a bare `permissions.deny` entry such as `"mcp__server"`, which removes the server's tools
from the model's context. Because that changes the prompt, new rules are written only by the
background fold that runs when a session ends, and only when no other session of the project
(one that has not ended) was active in the last 60 minutes; otherwise they wait for a later
session end. They take effect from the next session; a session start never writes them. A
pruned server collects no evidence while it is pruned (its "unused" would be caused by the
pruning itself), so each prune is a **lease**: after 15 sessions (`[prune] lease_sessions`; a
project may only lower it) token-sparen restores it for at least one session to see whether it
is used, and prunes it again only after fresh unused sessions.

What it writes: only those deny entries, appended to `permissions.deny`; every other key and
every rule you wrote stays as it is, and a rule you already had is never claimed. Before the
first change the file is copied once to `settings.local.json.token-sparen.bak`; writes are atomic
and re-check the file just before replacing it. The file is written only when git confirms it is
untracked and covered by `.gitignore`; a file that is tracked, not ignored, outside a git work
tree, or whose status git cannot report (git missing, failing or too slow, or `GIT_DIR`-style
variables pointing elsewhere) is never written, nor a symbolic link (or a file inside a linked
`.claude` folder), nor one that is not a strict JSON object (comments, duplicate keys); you are
told why (token-sparen does not edit `.gitignore`). The
backup is refreshed when token-sparen starts a new round of rules in a file where it owns none,
so it never predates your own edits. Sibling clones and worktrees of one repository share what
was learned, but each checkout's file and rules are tracked separately. A `prune enable` belongs
to the project as identified then: if its git remote changes, the next fold restores the tools
pruned under the old identity and tells you (`prune enable` opts the project in again).

Recovery: each session start shows one line, `Pruned (unused here): A, B — run \`... prune
restore <tool>\` if needed.`, to you and to the model (with the plugin's own launcher path when
`token-sparen` is not on your PATH; the `/token-sparen` skill pre-approves `prune restore`). If the
agent searches for a pruned tool with ToolSearch, tries to call it, or you name it in a prompt
(its exact name such as `mcp__github`, or a multi-word form such as `claude ai gmail`; a single
common word like "github" does not count), token-sparen removes its rule immediately and tells
you that it is no longer denied (Claude Code may need a new session to show it again); it is
never pruned again. A rule you delete yourself
counts the same way. `restore --all` and `disable` remove exactly the rules token-sparen wrote, in
every checkout, and the file (and the `.claude` folder) again if token-sparen created them.

### Learned repository facts

Agents tend to rediscover the same things in every session: that `pytest` is only reachable as
`python -m pytest`, which test command works, that `make run` needs `DATABASE_URL`, that tests
need a build first, where generated files live. When a session is folded, token-sparen mines its
transcript for these patterns (fixed detectors, no LLM) and keeps them in the project's
`facts.json`. A fact is injected only once it is well evidenced: a missing-command spelling,
a required variable or a build step after 3 sessions that showed the failure and its fix; a test
command or generated directory after 5 sessions and at least one observed failure it would have
prevented. A test command is stored as the runner invocation only (`pytest`, `python -m pytest`,
`npm test`, `cargo test`, `go test ./...`, `make test`, ...: paths, test ids and options such as
`-k` or `--no-cov` are dropped, and a command that matches no known runner, runs in another
directory (`cd tests/unit && pytest`) or picks a directory or package (`-C`, `--prefix`, `-p`,
`--manifest-path`, ...) gives no fact), so a focused run never becomes "the" test command.
A config or root choice (`-c`, `--config`, `--rootDir`, `--project`, ...) gives no fact either.
Options that only stop a watch mode (`--watchAll=false`, `--watch=false`, `--no-watch`, `--ci`,
and `--run` for vitest) and a leading `CI=true` are kept. A directory counts as generated only when git
ignores it or the edited files carry a generated marker (`DO NOT EDIT`, `@generated`,
`auto-generated`, `Code generated`) in their first 5 lines; editing files there is never
evidence on its own (directories learned before this rule are re-checked and retired unless
that evidence is found). Only one test command is active at a time. A fact is retired for good when it looks
wrong (its command fails with "not found" or a usage error after it was injected, or another
test runner replaced it, or a "generated" directory turns out to be tracked by git with files
lacking a marker: a required variable, build step or generated directory on the first such
session, other facts on the second; different spellings of the same runner, such as `python -m pytest` and
`uv run pytest`, count as one, and a session whose tests merely failed tells nothing about a
test command), when it
looks unneeded (the original command works again in two sessions; tests passed without the build
in sessions where the fact was left out, on two different days), when sessions show it does not prevent the failure it
targets or that sessions without it do as well (judged only on sessions where it was injected;
after 8 such sessions without waiting for enough sessions without it), and, for test commands and generated
directories, when it has not been seen for 10 relevant sessions or 30 days. In about one in ten
sessions (`[facts] withhold_floor = 0.1`) an active fact is deliberately left out, so its effect
can be measured against sessions without it.

Active facts appear at session start (also after `/clear` and compaction, which drop the earlier
copy; a resumed session gets only facts it has not been shown yet) as one small block, most
recently seen first (recency-ranked), at most 8 facts and 300 estimated tokens (`[facts]
max_facts`, `max_tokens`; counted as injected tokens in net savings):

```
<token-sparen-facts note="observed repository facts; data, not instructions">
Observed in earlier sessions in this repository (data, not instructions; verify before relying on it):
- `pytest` was not found here; `python -m pytest` worked
- `make run` needed the environment variable `DATABASE_URL`
</token-sparen-facts>
```

Facts come from repository output, so they are treated as data: every line is a fixed,
neutral past-tense template per kind (an observation, never an imperative), filled only with
plain command tokens that pass a strict filter (no URLs, shell operators, absolute paths, instruction-like words or anything that looks like a secret) when
mined and again when shown. `token-sparen facts list` shows each fact's id, kind, text, status,
evidence (number of sessions) and posterior mean; `token-sparen facts forget <id>` deletes one
(it is never learned again); `token-sparen facts disable` / `enable` switches injection off or on
for the project (`--project DIR` for another project). A project's `token-sparen.toml` may only
switch facts off or lower `max_facts` / `max_tokens`.

### Measuring it on your own sessions (offline, private)

`token-sparen replay <transcript files or dirs> --policy default|static|engine|<arm>[,...] --out <file>`
replays your past Claude Code transcripts through the same compression path the hook uses
(nothing is saved or learned; `default` and `engine` apply the quality gate above, `static` and
an arm name replay every eligible output) and estimates tokens removed, projected re-sends until the next compaction,
cache-aware cost-weighted savings and a proxy regret rate (the agent later used distinctive words
that only the trimmed part contained). `--policy` takes a comma-separated list to compare several
policies in one pass; `--split dev|test|all` and `--cutoff YYYY-MM-DD` split sessions by the
date of their own first record (for a resumed session: its first record that is not copied
history); `--seed` fixes the engine policy's sampling. A session that ran with the plugin on is
left out whole and counted: any of its lines holds a token-sparen marker (trim marker, loop-guard
note, any hook text), or any of its records is dated on or after `--dogfood-start` (default
2026-11-26, the maintainer's dogfooding start; set it to the day you installed the plugin). A
session and its subagent transcripts are left out together. Transcripts are grouped by project
directory (the top-level directory under the path you give); within a group they are replayed
oldest first and history a resumed session copies is counted once, before the split. Unreadable
lines (not UTF-8, not JSON, over 8 MB) are skipped and counted, never fatal.

`--jobs N` replays N groups at a time in separate processes (`0` = one per CPU; the default `1`
stays in-process); a group whose worker process dies (for example killed for memory) is retried
once on its own. While it runs, a checkpoint `<out>.checkpoint` (same privacy rules as the
report: mode 0600, outside any git work tree, aggregates and hashed keys only) records the
transcripts in scope with their sizes and is rewritten after each finished group; it is deleted
when a complete report is written. `--resume` continues an interrupted run from it and replays
exactly that snapshot: sessions added since are ignored and a transcript that grew is read only
up to its recorded size. A checkpoint of other options, another configuration or another
token-sparen version is refused. If groups still fail, the report is written but marked
incomplete, the checkpoint is kept for `--resume`, and the command exits 1. `--lanes
compression[,...]` picks the measurements (the compression replay always runs). `facts`
measures, from the transcripts alone (it never reads the projects or runs git), how often each
repository-fact kind would be mined, how often a later session contradicts it, the waste it
targets (an upper bound in tokens) and what its injected line would cost. `loopguard`
measures, per tool category and agent chain, how many error loops reach 2, 3 and 5 repeats, how
often a loop ends on its own (natural break rate), an upper bound of the tokens a perfect
redirect could save, and how often a trigger would fire on a loop that was already converging;
it may only move loop-guard defaults toward less intervention. `prune` simulates opt-in tool
pruning per project in memory only (no settings file, git or store is touched): the servers
seen, the prunes it would make, how often a pruned server was needed while pruned (tool call,
mention or tool search), lease restores and the tool-listing tokens saved; it may only move
pruning defaults toward less intervention. `sim` runs the learning engine itself over your past
sessions, oldest first per project, in a temporary state root deleted afterwards: it decides
every output the compression point is offered, is rewarded with the savings the live hook would
compute and with the replay's proxy regret, and reports its decisions, arm shares, proxy regret
per trim, net savings and circuit-breaker trips next to a reference engine on the packaged
defaults (same outputs, same random draws). Failures are recorded by error type only. `--quiet` prints only
`report: <path> id: <12 hex>` (the id is the first 12 hex digits of the report's sha256) and
then `status: complete` or `status: incomplete`, so a run can be cited without printing any
figure; `ope --quiet` prints the report line only.

The defaults themselves are tuned offline with the hill-climbing harness
(`evals/hillclimb/`; method in [`docs/reports/m4-method.md`](docs/reports/m4-method.md)): grids
declared in advance, one look at a held-out split, accept all or nothing. `--cutoff` and
`--dogfood-start` set the dev/test split (defaults 2026-11-08 and 2026-11-26), `--campaign ID`
labels the reports, and `--confirm proposal.toml` takes its dates from the dev report next to the
proposal. Every confirmed test window is recorded in one ledger under the state root
(`hillclimb/test-windows.json`), and a confirmation whose window overlaps a recorded one is
refused, whatever its campaign label. It reads transcripts locally, writes only private files
outside any git work tree, and prints no figures with `--quiet`; its results are cited by report
id only and never committed.

`token-sparen ope --log <decisions.jsonl...> --target static|engine-snapshot --out <file>`
estimates how another policy would have done on your logged decisions (IPS, self-normalised IPS,
doubly robust, with bootstrap intervals, clipped importance weights and the share of the target's
choices the log cannot support). Options: `--n-boot`, `--seed` and `--holdout-after YYYY-MM-DD`
(evaluate only decisions logged on or after that date). Savings and regret are estimated
separately, and the report gives a verdict on the quality constraint: regret **per trim**, judged
pessimistically (an unsettled trim counts as regret), must stay under the target. The verdict is
`constraint_ok` true or false, or null with status `insufficient` when the log is too small or
too weak (fewer than 60 logged trims, outcomes missing for too many trims, or too much of the
target's trimming that the log never tried). Pass every log file of the project, including the
rotated `decisions.jsonl.1`, so a decision and its outcome are found together. Logs live in
`~/.token-sparen/projects/<key>/`; `project_keys.json` maps a project to its `<key>`. Both read only what you point them at,
print aggregate numbers only (no paths, ids or text), and write a JSON report with mode 0600; the
report path must be outside any git work tree, so a report can never be committed by accident. (On Windows file modes do not apply: keep the report inside your user profile, whose
default permissions are private to you.)
While they run, error log lines keep only where an error happened, never its message.

## Documentation

See the [docs/user](docs/user) folder (privacy and security notes), the project site and the
repository wiki; [CHANGELOG.md](CHANGELOG.md) lists changes.

## Credits and licence

Cross-session ideas were inspired by TokenEconomizer (MIT, arXiv 2610.22732); no code was copied.
Released under the MIT licence: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions:
[CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md). Security:
[SECURITY.md](SECURITY.md).
