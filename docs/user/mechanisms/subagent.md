# Subagent brief

**Hook:** PreToolUse. **Default:** the decision point exists, its arm is **off**. **Learned:** not until a benchmark shows it does not hurt quality.

A decision point that can append a short "be concise" brief to subagent prompts exists, but it stays off by default and is not learned. Subagent replies and other plain text are shown in full.

| Key | Default | Meaning |
|---|---|---|
| `subagent.enabled` | true | the decision point is present (its default arm is off; a project may only turn it off) |

See [Configuration](../configuration.md) and [Benchmarks and methodology](../benchmarks-methodology.md).
