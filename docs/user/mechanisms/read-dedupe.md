# Read de-duplication

**Hook:** PreToolUse and PostToolUse on file reads. **Default:** on. **Learned:** yes.

Re-reading an unchanged file that is still in context (within 8 turns, not compacted since, and of at least 2000 estimated tokens) returns a short pointer, `[token-sparen: unchanged since turn N ...]`, instead of the file again.

This is not covered by `trim_file_contents`: it replaces a repeat of something the model already has, not the first read. It is a learned decision like [compression](compression.md): a small share of re-reads always returns the full file, and with learning off it always returns the pointer.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `read_dedupe.enabled` | true | return a pointer for unchanged re-reads |
| `read_dedupe.max_turn_gap` | 8 | pointer only if the earlier read is this recent |
| `read_dedupe.min_tokens` | 2000 | never below 2000 |
| `read_dedupe.max_entries` | 200 | reads remembered per session |

Turn it off with `read_dedupe.enabled = false`, or hold it with `[learn.hold] read_dedupe = true`. See [Configuration](../configuration.md).
