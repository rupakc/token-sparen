---
name: token-sparen
description: Use when the user asks about token usage, trimmed outputs, recovering elided output, or token-sparen settings. Also the /token-sparen command (stats, statusline, disable, enable).
argument-hint: "[stats | statusline | disable | enable]"
allowed-tools:
  - Bash(token-sparen stats:*)
  - Bash(token-sparen show:*)
  - Bash(token-sparen doctor:*)
  - Bash(token-sparen session disable:*)
  - 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" stats:*)'
  - 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" show:*)'
  - 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" doctor:*)'
  - 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" session disable:*)'
---

# token-sparen

token-sparen shortens large tool outputs (test runs, build logs, JSON, diffs, searches) before
the model sees them, and keeps the full text on disk so nothing is lost. File contents (Read,
`cat`, ...) and plain text such as subagent replies are shown in full by default.

## Running the CLI
Use `token-sparen <args>`. If that is not installed (command not found), run exactly
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <args>` instead (same arguments). Run nothing else.
Only `stats`, `show`, `doctor` and `session` are pre-approved. Every other subcommand
(`statusline install`, `config set`, `extensions trust`, `prune`, ...) goes through the normal
permission prompt: run it only when the user asked for it, and let the user approve it.

## /token-sparen
Argument given: `$ARGUMENTS`. Follow these steps only when the user explicitly typed
`/token-sparen`; if this skill was loaded for a question (e.g. how to recover a trimmed output),
answer from the sections below; do not run `stats` (run `token-sparen show <id>` only if the
user needs the full output).
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
A trimmed output ends with:
`[token-sparen: kept X of Y lines · full output: <path> · `token-sparen show <id>`]`
- Error lines, failures and stack-trace edges are never elided.
- To see everything, run `token-sparen show <id>` (12 hex characters), or read the `<path>` file.
- Recover only what you need: lines you can see are enough in most cases.
- Full outputs live in `<project>/.token-sparen/outputs/` and are pruned after 14 days (checked whenever a new output is saved).

## Commands
- `token-sparen stats [--session ID] [--json]`: net tokens saved (gross minus recovery reads minus
  injected tokens; as in the statusline, can be negative), its parts, decisions per arm.
- `token-sparen doctor [--fix]`: verifies Python, state home, hooks and a synthetic round-trip.
- `token-sparen run -- <command>`: run a command and print its compressed output.
- `token-sparen statusline install|uninstall`: opt-in live statusline (backs up settings).
- `token-sparen extensions list`: installed and drop-in extensions with their trust status.
  Trusting a drop-in (`extensions trust <path> --yes`) is the user's decision: never run it for them.
  Trust grants that file full code execution as the user (runtime checks stop buggy extensions, not
  malicious ones), and an identical copy anywhere else loads too.

## Switching it off
- This session only: `token-sparen session disable --session ${CLAUDE_SESSION_ID}` (`enable` undoes).
- Everywhere: `TOKEN_SPAREN_DISABLE=1` in the environment Claude Code is launched from, or
  `claude plugin disable token-sparen`.
- All trimming: `[learn.hold] compression = true` in `~/.token-sparen/config.toml` (one session:
  `token-sparen config set learn.hold.compression true --session ${CLAUDE_SESSION_ID}`) or
  `[compression] allow_ctypes = []`. Read pointers: `[learn.hold] read_dedupe = true`.
- Change config only when the user asks. Needs Claude Code >= 2.1.121; trimmed output is never
  lost (marker path or `show <id>`).

## Configuration
Precedence, lowest first: packaged defaults, `~/.token-sparen/config.toml`,
`<project>/token-sparen.toml`, environment `TOKEN_SPAREN_*`, then this session's overrides:
`token-sparen config set <section.key> <value> --session ${CLAUDE_SESSION_ID}` (e.g.
`guards.enabled false`). State root is `~/.token-sparen/`
(override with `TOKEN_SPAREN_HOME`). Outputs under 2000 estimated tokens are never modified.
