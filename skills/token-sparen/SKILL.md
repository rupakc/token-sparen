---
name: token-sparen
description: Use when the user asks about token usage, trimmed outputs, recovering elided output, or token-sparen settings
---

# token-sparen

token-sparen shortens large tool outputs (test runs, logs, JSON, diffs, searches) before the model
sees them, and keeps the full text on disk so nothing is lost.

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
- `python3 -m token_sparen ...` works when the console script is not on PATH.

## Switching it off
- This session only: `token-sparen session disable` (`enable` to undo).
- Everywhere: set `TOKEN_SPAREN_DISABLE=1` in the environment.

## Configuration
Precedence, lowest first: packaged defaults, `~/.token-sparen/config.toml`,
`<project>/token-sparen.toml`, environment `TOKEN_SPAREN_*`. State root is `~/.token-sparen/`
(override with `TOKEN_SPAREN_HOME`). Outputs under 2000 estimated tokens are never modified.
