# Privacy and security

## Privacy in one paragraph

Nothing leaves your machine. token-sparen has no telemetry and makes no network requests. Its local files are plain text, though: recovered outputs, your last prompt, file paths and command prefixes are stored as described in [What it writes to disk](#what-it-writes-to-disk). Delete `~/.token-sparen/` and `<project>/.token-sparen/` at any time ([Uninstall](uninstall.md)).

Replay and the hill-climbing harness read your own transcripts on your machine only; their reports are written with owner-only permissions outside any git work tree, and the project never commits figures derived from anyone's sessions.

## Security notes

<!-- TODO(merge): Task 3 owns this page's threat model; merge its content here (the section below is the user-facing summary written in Task 5). -->

- **Hooks fail open.** Any internal error, or a hook slower than 400 ms, returns "no change" and logs one line. A bug cannot block your session.
- **A repository cannot make token-sparen intervene more.** `<project>/token-sparen.toml` is deny-by-default: it may only make token-sparen intervene less or keep more; an unlisted key is user-only ([Configuration](configuration.md)). Environment variables, which a repository's committed Claude Code settings could set, can only narrow too, and path overrides inside the project are ignored.
- **Settings writes are opt-in, backed up and reversible.** Only the statusline install and tool pruning write Claude Code settings: see [What it writes to disk](#what-it-writes-to-disk).
- **Learned facts are data, not instructions.** They come from repository output, so every line is a fixed past-tense template filled only with strictly filtered plain tokens ([Facts](mechanisms/facts.md)).
- **Extensions run code as you.** Installing a package or trusting a drop-in is the trust decision ([Extending](extending.md)).
- **Hook commands** reference files only through `${CLAUDE_PLUGIN_ROOT}`; the plugin ships no binaries and no unpinned `npx` or `uvx` commands.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## What it writes to disk

Everything stays on your machine, but some of it is plain text you should know about.

- `<project>/.token-sparen/`: recovery files under `outputs/` (full tool outputs, so they can
  contain anything a command printed), plus a `.gitignore` containing `*` so the folder is never
  committed. Created on the first compression in that project.
- `~/.token-sparen/` (override with `TOKEN_SPAREN_HOME`; an override inside the current project
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
    [tool pruning](mechanisms/tool-pruning.md): per checkout the **path of its settings file and of the
    checkout**, its project key, the deny rules token-sparen wrote there and what it created,
    rules about to be written, and pending notices; kept here, beside the statusline record, so a
    changed git remote never loses track of a rule);
  - `logs/errors.jsonl` and sampled `logs/latency.jsonl`;
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
    regret values per decision point), `pending.json`
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
- Claude Code `settings.json`: **only** if you run `token-sparen statusline install` (see [Live statusline](mechanisms/statusline.md)),
  with a backup and exact restore.
- `<project>/.claude/settings.local.json`: **only** if you opt in to
  [tool pruning](mechanisms/tool-pruning.md), and only when git confirms the file is untracked and
  ignored: bare `permissions.deny` entries, appended by the background session-end fold, with a
  backup `settings.local.json.token-sparen.bak` next to it and exact restore (`prune restore
  --all`, `prune disable`).
- Hooks fail open: any internal error or a hook slower than 400 ms returns "no change" and logs one
  line to `errors.jsonl`.
