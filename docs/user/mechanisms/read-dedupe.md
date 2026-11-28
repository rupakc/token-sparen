# Read de-duplication

**Hook:** PreToolUse and PostToolUse on file reads. **Default:** on. **Learned:** yes.

Re-reading an unchanged file that is still in context (within 8 turns and 30 minutes, in the same context epoch, and of at least 2000 estimated tokens) returns a short pointer, `[token-sparen: unchanged since turn N ...]`, instead of the file again.

A pointer only ever refers to a read from the same **context epoch**: a new epoch starts at a compaction (`/compact` or automatic), `/clear`, a resumed or forked session, and when you send a prompt you already sent in this session (that is what `/rewind` and "Summarize from here" lead to: they fire no hook, but put the earlier prompt back into the input). Claude Code may also clear old tool results after a long pause without telling hooks, hence the 30-minute limit. A rewind after which you edit the prompt before sending it cannot be detected; within 8 turns and 30 minutes the pointer may then refer to a read that is gone, and the model re-reads with a range (the pointer says how), or you can turn the mechanism off.

This is not covered by `trim_file_contents`: it replaces a repeat of something the model already has, not the first read. It is a learned decision like [compression](compression.md): a small share of re-reads always returns the full file, and with learning off it always returns the pointer.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `read_dedupe.enabled` | true | return a pointer for unchanged re-reads |
| `read_dedupe.max_turn_gap` | 8 | pointer only if the earlier read is this recent |
| `read_dedupe.min_tokens` | 2000 | never below 2000 |
| `read_dedupe.max_entries` | 200 | reads remembered per session |

Turn it off with `read_dedupe.enabled = false`, or hold it with `[learn.hold] read_dedupe = true`. See [Configuration](../configuration.md).
