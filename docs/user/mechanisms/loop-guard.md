# Failure-loop guard

**Hooks:** UserPromptSubmit, PostToolUse, PostToolUseFailure and SessionEnd. **Default:** on. **Learned:** yes, how many repeats to wait for. No LLM: deterministic checks only.

When an agent (the main agent or one subagent, each counted on its own) hits the same error again and again within one turn, token-sparen adds one short line to that tool result for the model:

```
[token-sparen: this error has repeated 3× this turn (<id>). Consider stepping back: re-read the error, check assumptions, or try a different approach.]
```

The tool output itself is never changed. At most one such line per agent and turn; if it happens again you get a terminal notice instead (no model tokens).

## What counts as "the same error"

The same tool, the same command (its program and subcommand, for example `pytest` or `git diff`) or file, and the same error line once directories, line:column, hex ids, durations and long numbers are ignored. Names, quoted values and literals still count, so `No module named 'foo'` and `'bar'` are different errors. For test runs (pytest, jest, go test, cargo test) it is the whole set of failing tests plus the first failure's error, so fixing a suite one test at a time never looks like a loop. A failure with no error line (a bare `Exit code 1` from `grep`, `test` or `diff`, or only a summary line) is never counted.

The same edit made and undone twice on one file (A to B to A to B) gets `[token-sparen: the same edit has been made and undone 2× this turn (<id>). ...]`; adding and then removing a debug line is fine.

## How it learns

How many repeats it waits for (5, 3 or 2) is learned, starting at the most patient setting. Only a line that was actually shown is judged: it is useful when the error then stops recurring within the agent's next 5 tool calls. A less patient setting is used only once its lines clearly end loops more often than loops end by themselves. A line counts as harmful when you interrupt, or your next prompt starts with stop, wait, undo, revert, "that's wrong" or "no, don't".

## Settings

| Key | Default | Meaning |
|---|---|---|
| `loopguard.enabled` | true | `false` switches it off (a project may only turn it off) |
| `loopguard.min_k` | 2 | fewest repeats before the redirect (a project may only raise it) |
| `loopguard.watch_calls` | 5 | tool calls after the redirect that decide whether the loop broke (user only) |
| `learn.hold.loopguard` | false | `true` keeps the most patient setting (5 repeats); it does not switch the guard off |

See [Configuration](../configuration.md) and the [learning engine](../learning-engine.md).
