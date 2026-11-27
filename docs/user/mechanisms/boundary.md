# Task boundaries and compaction

**Hooks:** UserPromptSubmit, PreCompact. **Default:** on, advisory only. **Learned:** no.

When your context is fairly full and a new prompt looks like a new task (few of its identifiers appear in recent prompts or paths), token-sparen may show an occasional "this looks like a new task, consider `/compact`" hint in the terminal. It never compacts for you.

Before a compaction, token-sparen asks that recovery paths be kept in the summary, so trimmed outputs stay recoverable afterwards.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `boundary.enabled` | true | suggest `/compact` at task boundaries |
| `boundary.min_ctx_pct` | 0.40 | advise only when context is this full (a share) |
| `boundary.min_overlap` | 0.15 | identifier overlap below this counts as a new task |
| `boundary.cooldown_turns` | 10 | fewest turns between hints |

See [Configuration](../configuration.md).
