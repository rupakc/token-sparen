# Live statusline (opt-in)

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
| `saved` | estimated tokens removed by this session's trims minus recovery reads of trimmed output and the tokens token-sparen itself injected, with an approximate dollar figure derived from the session's average cost per token reported by Claude Code. Only the output kinds listed under [What changes in your session](../host-guides/claude-code.md#what-changes-in-your-session) are ever trimmed, so on file-heavy sessions it stays small or at 0 by design. It counts each removed token once (not its later re-sends) and can be negative |
| `trims` | `✂` trims made, `↺` regrets detected (recovery reads, re-runs) |
| `pruned` | `⊘ N`: number of tools pruned in this project (only with [tool pruning](tool-pruning.md)) |
| `learn` | learner state: `cold` (< 20 observations), `adapting`, `exploiting` (>= 80 % of recent decisions pick the best-known arm) or `safe-mode` (a decision point is paused by the circuit breaker) |

Choose segments with `statusline.segments`.
