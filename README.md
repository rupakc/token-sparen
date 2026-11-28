# token-sparen

An open-source Claude Code plugin that cuts token usage without hurting output quality. It trims
noisy tool output (test runs, build logs, JSON, diffs, searches) before the model sees it, never
trims file contents by default, keeps the **full text on disk so nothing is lost**, blocks a few clearly unbounded commands, and
shows a live token meter. Everything runs locally.

## Quick start

1. Install: `claude plugin marketplace add rupakc/token-sparen`, then `claude plugin install token-sparen@token-sparen`; start a new session.
2. Verify: `/token-sparen doctor`, then ask Claude to run `pytest -v` (or any big test run) and look for the `[token-sparen: kept ...]` marker ([details](#install)).
3. Recover: run the `show` command in the marker (`token-sparen show <id>`, or the plugin's
   bundled CLI when the `token-sparen` command is not installed) or read the `full output:` path.
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
  `python3 -I` (on Windows `py -3 -I`, else `python -I`); if that `python3` is older (macOS ships 3.9, Ubuntu 22.04 3.10), the launcher
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
- macOS and Linux. Windows: the hooks run in Git Bash (Claude Code's default shell there) and
  start the Python launcher `py -3`, else `python` (never a bare `python3`, often missing or the
  Microsoft Store alias there); a PowerShell-only setup (no Git Bash) is not supported, nor is
  the statusline launcher yet. `token-sparen doctor` names the interpreter the hooks use.

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

All of this is on by default unless marked opt-in. Nothing is sent anywhere. Each item links to its page in the [documentation](docs/user/index.md).

- **Compression (quality first).** A tool result of at least 2000 estimated tokens that is a diff,
  search result, test report, compiler output, build or install log, JSON, `pplx` result or
  timestamped log may be replaced by a shorter version that keeps the head and tail and every error
  line, and ends with a recovery marker. **File contents** (the Read tool, `cat`, `head`, ...),
  plain text, tables and stack traces are shown in full by default.
  [Details and how to widen it](docs/user/mechanisms/compression.md).
- **Never larger.** The replacement is used only if it is no longer than what the model would
  have seen anyway and saves at least 15 % of it; otherwise the output is left untouched.
- **What is never touched.** Results below 2000 tokens, edit tools (`Edit`, `Write`, `MultiEdit`,
  `NotebookEdit`), `TodoWrite`, `AskUserQuestion`, `ExitPlanMode`, images/PDFs, and **failing tool
  calls** (Claude Code cannot replace the result of a failed call).
- **[Guards](docs/user/mechanisms/guards.md)** block a few clearly unbounded calls (`find` from `/`
  or `~` without `-maxdepth`, recursive search into `node_modules`/`.git`/`dist`/`build`/`.venv`,
  `cat` or Read of a text file over 5 MB without a range, `ls -R` on `/` or `~`) with a reason that
  names the bounded alternative. Turn off with `guards.enabled = false`.
- **[Read de-duplication](docs/user/mechanisms/read-dedupe.md)** returns a short pointer for an
  unchanged file that is still in context.
- **Notices (terminal only, no model tokens)** from the [governor](docs/user/mechanisms/governor.md)
  and [boundary](docs/user/mechanisms/boundary.md) mechanisms; an opt-in hard stop is off by default.
- **[Failure-loop guard](docs/user/mechanisms/loop-guard.md):** one short redirect line when the same
  error repeats within a turn; the tool output itself is never changed.
- **[Learned repository facts](docs/user/mechanisms/facts.md)** at session start (data, not
  instructions) and opt-in [tool pruning](docs/user/mechanisms/tool-pruning.md).
- **Subagent brief:** a decision point exists but is **off** by default
  ([details](docs/user/mechanisms/subagent.md)).
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
| Learned repository facts | `token-sparen facts disable` (this project; `facts enable` undoes it), `[facts] enabled = false` (everywhere; a project's `token-sparen.toml` may set it too), or `token-sparen facts forget <id>` for one fact (see [Learned repository facts](docs/user/mechanisms/facts.md)) |
| Failure-loop guard | `[loopguard] enabled = false` (a project's `token-sparen.toml` may set it too). `[learn.hold] loopguard = true` does not switch it off, it only keeps the most patient setting |
| Output-style directive | off unless you opted in (`[output_style] enabled = true` in your `config.toml`, or `token-sparen config set output_style.enabled true` for one session); `enabled = false` switches it off again (a project's `token-sparen.toml` or a `TOKEN_SPAREN_*` variable may only set that) |
| Tool pruning | off unless you opted in; `token-sparen prune disable` opts out and removes every rule token-sparen wrote, `prune restore <tool>` gives one tool back, and a project's `token-sparen.toml` may set `[prune] allow = false` (see [Tool pruning](docs/user/mechanisms/tool-pruning.md)) |
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
    [tool pruning](docs/user/mechanisms/tool-pruning.md): per checkout the **path of its settings file and of the
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
  [tool pruning](docs/user/mechanisms/tool-pruning.md), and only when git confirms the file is untracked and
  ignored: bare `permissions.deny` entries, appended by the background session-end fold, with a
  backup `<project>/.token-sparen/settings.local.json.token-sparen.bak` (git-ignored like the
  recovery files; written only once git confirms that too) and exact restore (`prune restore
  --all`, `prune disable`).
- Hooks fail open: any internal error or a hook slower than 400 ms returns "no change" and logs one
  line to `errors.jsonl`.

## Live statusline (opt-in)

Plugins cannot register a statusline, so this is an explicit step: `token-sparen statusline
install` edits only the `statusLine` key of your Claude Code `settings.json`, after a backup, and
`token-sparen statusline uninstall` restores it. Not supported on Windows. Segments and what they
mean: [Live statusline](docs/user/mechanisms/statusline.md).

## Configuration

Later layers override earlier ones: packaged defaults (`token_sparen/params/defaults.toml`, the
full list of keys), `~/.token-sparen/config.toml`, `<project>/token-sparen.toml`, environment
`TOKEN_SPAREN_<SECTION>__<KEY>`, then `token-sparen config set` for one session. A project file and
the environment can only make token-sparen intervene less or keep more; widening happens only in
your own `config.toml` or with `config set`. `compression.min_tokens` can raise the 2000-token
floor but never lower it. The full precedence rules, what a project file may change and every key
are in [Configuration](docs/user/configuration.md).

## Extensions (opt-in, explicit trust)

Installed packages can add compressors, decision points, signals or host adapters, and drop-in
`*.py` files run only after you trust their exact content. Trusting code gives it full code
execution as you. See [Extending](docs/user/extending.md).

## Settings it may change (statusline, tool pruning)

Two opt-in features write Claude Code settings, each backed up and reversible: the
[live statusline](#live-statusline-opt-in) (`token-sparen statusline install`) and
[tool pruning](docs/user/mechanisms/tool-pruning.md) (deny rules, only on git confirmation). Nothing else edits
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

A small constrained bandit chooses, per decision point and context bucket, how strongly to trim,
under a hard quality constraint: it never trades quality for tokens in a weighted sum. A new
project starts with the lightest level and unlocks stronger ones only after the previous one has
been observed enough with low regret, so early sessions trim less than later ones.
`[learn] enabled = false` is not a way to trim less (it uses fixed defaults that trim more than a
cold learner); to trim nothing use the hold in [Turning it off](#turning-it-off). Details:
[Learning engine](docs/user/learning-engine.md); cross-session mechanisms:
[facts](docs/user/mechanisms/facts.md), [tool pruning](docs/user/mechanisms/tool-pruning.md) and
the [loop guard](docs/user/mechanisms/loop-guard.md). Measuring it on your own sessions (offline,
private): [Benchmarks and methodology](docs/user/benchmarks-methodology.md).

## Documentation

The user documentation lives in [`docs/user/`](docs/user/index.md): [install](docs/user/install.md), [support matrix](docs/user/support.md), [upgrade](docs/user/upgrade.md), [uninstall](docs/user/uninstall.md), [configuration](docs/user/configuration.md), [troubleshooting](docs/user/troubleshooting.md), [privacy and security](docs/user/privacy-security.md) and one page per mechanism; [CHANGELOG.md](CHANGELOG.md) lists changes.

## Credits and licence

Cross-session ideas were inspired by TokenEconomizer (MIT, arXiv 2610.22732); no code was copied.
Released under the MIT licence: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions:
[CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md). Security:
[SECURITY.md](SECURITY.md).
