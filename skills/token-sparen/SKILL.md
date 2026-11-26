---
name: token-sparen
description: Use when the user asks about token usage, trimmed outputs, recovering elided output, or token-sparen settings. Also the /token-sparen command (stats, statusline, disable, enable).
argument-hint: "[stats | statusline | disable | enable]"
allowed-tools:
  - Bash(token-sparen:*)
  - 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py":*)'
---

# token-sparen

token-sparen shortens large tool outputs (test runs, logs, JSON, diffs, searches, subagent
results) before the model sees them, and keeps the full text on disk so nothing is lost.

## Running the CLI
Use `token-sparen <args>`. If that is not installed (command not found), run exactly
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <args>` instead (same arguments). Run nothing else.

## /token-sparen
Argument given: `$ARGUMENTS` (empty when the skill was not invoked as a command).
1. No argument or `stats`: run `token-sparen stats --session ${CLAUDE_SESSION_ID}` and summarise
   tokens saved and trims in two or three lines, then give the recovery help below.
2. `statusline`: offer `token-sparen statusline install` (edits Claude Code settings, keeps a
   backup; `token-sparen statusline uninstall` reverses it). Run it only if the user agrees.
3. `disable`: run `token-sparen session disable --session ${CLAUDE_SESSION_ID}` and confirm that
   outputs are no longer trimmed in this session. `enable`: the same with `session enable`.
   Without `--session` the CLI acts on this project's most recently active session (< 6 h).
4. Anything else: run `token-sparen doctor` and report any failing check.

## Markers and recovery
A trimmed output ends with:
`[token-sparen: kept X of Y lines · full output: <path> · `token-sparen show <id>`]`
- Error lines, failures and stack-trace edges are never elided.
- To see everything, run `token-sparen show <id>` (12 hex characters), or read the `<path>` file.
- Recover only what you need: lines you can see are enough in most cases.
- Full outputs live in `<project>/.token-sparen/outputs/` and expire after 14 days.

## Commands
- `token-sparen stats [--session ID] [--json]`: tokens saved, decisions per arm, recovery reads.
- `token-sparen doctor [--fix]`: verifies Python, state home, hooks and a synthetic round-trip.
- `token-sparen run -- <command>`: run a command and print its compressed output.
- `token-sparen statusline install|uninstall`: opt-in live statusline (backs up settings).
- `token-sparen extensions list`: installed and drop-in extensions with their trust status.
  Trusting a drop-in (`extensions trust <path> --yes`) is the user's decision: never run it for them.
  Trust grants that file full code execution as the user (runtime checks stop buggy extensions, not
  malicious ones), and an identical copy anywhere else loads too.

## Switching it off
- This session only: `token-sparen session disable --session ${CLAUDE_SESSION_ID}` (`enable` undoes).
- Everywhere: set `TOKEN_SPAREN_DISABLE=1` in the environment.

## Configuration
Precedence, lowest first: packaged defaults, `~/.token-sparen/config.toml`,
`<project>/token-sparen.toml`, environment `TOKEN_SPAREN_*`. State root is `~/.token-sparen/`
(override with `TOKEN_SPAREN_HOME`). Outputs under 2000 estimated tokens are never modified.
