---
name: token-sparen
description: Use when the user asks about token usage, trimmed outputs, recovering elided output, or token-sparen settings. Also the /token-sparen command (stats, statusline, disable, enable).
argument-hint: "[stats | statusline | disable | enable]"
allowed-tools:
  - Bash(token-sparen stats:*)
  - Bash(token-sparen show:*)
  - Bash(token-sparen doctor:*)
  - Bash(token-sparen session disable:*)
  - Bash(token-sparen prune restore:*)
  - 'Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" stats:*)'
  - 'Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" show:*)'
  - 'Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" doctor:*)'
  - 'Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" session disable:*)'
  - 'Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" prune restore:*)'
---

# token-sparen

token-sparen shortens large tool outputs (test runs, build logs, JSON, diffs, searches) before
the model sees them, and keeps the full text on disk so nothing is lost. File contents (Read,
`cat`, ...) and plain text such as subagent replies are shown in full by default.

## Running the CLI
Run the plugin's bundled CLI, exactly `python3 -I "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <args>`
(on Windows `py -3 -I`, else `python -I`, in place of `python3 -I`): it is the plugin's own
version, the one its hooks use. Only if that cannot run (no Python, or the file is missing), run
`token-sparen <args>` (same arguments; it may be another version). If neither runs, reply that
token-sparen works in Claude Code only and run nothing else. Below, `token-sparen <args>` means
whichever ran. Pre-approved: `stats`, `show`, `doctor`, `session disable`, `prune restore <tool>`
(when you need a tool the session-start line lists as pruned). Others (`statusline install`,
`config set`, `prune enable`, ...) get the permission prompt: only when asked.

## /token-sparen
Argument given: `$ARGUMENTS`. Follow these steps only when the user explicitly typed
`/token-sparen`; if this skill was loaded for a question (e.g. how to recover a trimmed output),
answer from the sections below; do not run `stats` (`token-sparen show <id>` only if needed).
1. `/token-sparen` with no argument, or `stats`: run
   `token-sparen stats --session ${CLAUDE_SESSION_ID}` and summarise net tokens saved (gross
   minus recovery reads minus injected tokens) and trims in two or three lines, then give the
   recovery help below.
2. `statusline`: offer `token-sparen statusline install` (edits Claude Code settings, keeps a
   backup; `token-sparen statusline uninstall` reverses it). Run it only if the user agrees.
3. `disable`: run `token-sparen session disable --session ${CLAUDE_SESSION_ID}` and confirm that
   outputs are no longer trimmed in this session. `enable`: the same with `session enable`
   (not pre-approved: it turns trimming back on, so Claude Code asks for permission).
   Without `--session` the CLI acts on this project's most recently active session (< 6 h).
4. Anything else: run `token-sparen doctor` and report any failing check.

## Markers and recovery
A trimmed output ends with (the bundled CLI in place of `token-sparen` if that is not installed):
`[token-sparen: kept X of Y lines · full output: <path> · `token-sparen show <id>`]`
- Error lines, failures and stack-trace edges are never elided.
- To see everything, run the marker's command as written (12 hex id), or read the `<path>` file.
- Recover only what you need. Full outputs live in `<project>/.token-sparen/outputs/` (14 days).

## Commands
- `token-sparen stats [--session ID] [--json]`: net tokens saved (gross minus recovery reads minus
  injected tokens; as in the statusline, can be negative), its parts, decisions per arm.
- `token-sparen doctor [--fix]`: checks Python, state home, hooks, CLI/plugin versions, a round-trip;
  `token-sparen run -- <command>`: run a command and print its compressed output.
- `token-sparen statusline install|uninstall`: opt-in live statusline (backs up settings).
- `token-sparen extensions list`: installed and drop-in extensions with their trust status.
  Trusting a drop-in (`extensions trust <path> --yes`) is the user's decision: never run it for them.
  Trust grants that file full code execution as the user (runtime checks stop buggy extensions, not
  malicious ones), and an identical copy anywhere else loads too.

## Switching it off
- This session only: `token-sparen session disable --session ${CLAUDE_SESSION_ID}` (`enable` undoes).
- Everywhere: `TOKEN_SPAREN_DISABLE=1` where Claude Code starts, or `claude plugin disable token-sparen`.
- All trimming: `[learn.hold] compression = true` in `~/.token-sparen/config.toml` (one session:
  `token-sparen config set learn.hold.compression true --session ${CLAUDE_SESSION_ID}`) or
  `[compression] allow_ctypes = []`. Read pointers: `[learn.hold] read_dedupe = true`.
- Change config only when the user asks. Needs Claude Code >= 2.1.121.

## Configuration
Precedence, lowest first: packaged defaults, `~/.token-sparen/config.toml`, `<project>/token-sparen.toml`,
`TOKEN_SPAREN_*` (those two only make it do less), this session: `token-sparen config set <section.key>
<value> --session ${CLAUDE_SESSION_ID}` (e.g. `guards.enabled false`). State root `~/.token-sparen/`
(`TOKEN_SPAREN_HOME`). Outputs under 2000 estimated tokens are never modified.
