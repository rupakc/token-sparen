# Architecture

```
 Claude Code hook event ──▶ adapter (translate to a canonical event)
                                 │
                 sensors ─▶ context features ─▶ decision points ──▶ learning engine
                                 │                                        ▲ rewards, regret
                                 ▼
                    mechanisms: compress · guard · de-dupe · govern · facts · prune · loop guard
                                 │
                       emit gate (dedupe, budget, accounting) ─▶ canonical action ─▶ adapter ─▶ host
                                 │
        store (JSON + JSONL, file-locked) ~/.token-sparen/…     recovery files <project>/.token-sparen/outputs/
```

- **Adapters** own the host's protocol. The Claude Code adapter translates hook input into a canonical event (tool name and category, input, output text, session, project) and renders the canonical action (replace an output, block a call, add context, show a terminal message, give compaction instructions) back into what Claude Code expects. Mechanisms never depend on an adapter. Hook events handled: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PreCompact, SessionEnd and PermissionDenied.
- **Fail open.** Every hook entry point wraps the pipeline; on an exception or a latency overrun (400 ms) it returns the host's no-op response and logs one line to `~/.token-sparen/logs/errors.jsonl`.
- **Two-speed learning.** In a session, hooks update a small per-session overlay, so a recovery read immediately makes that bucket more careful. After the session ends, a background `token-sparen fold` merges the overlay into the project store and a pooled global store, settles delayed rewards, mines [facts](mechanisms/facts.md) and updates [tool pruning](mechanisms/tool-pruning.md). Sessions it missed are folded at the next session start. See the [learning engine](learning-engine.md).
- **State.** Plain JSON and JSON-lines files, file-locked, under `~/.token-sparen/` (projects keyed by hash); recovery files in the project. Exactly what is written is listed in [Privacy and security](privacy-security.md#what-it-writes-to-disk).
- **The plugin** is hooks (`hooks/hooks.json`, launched through `${CLAUDE_PLUGIN_ROOT}`), one skill (`/token-sparen`) and the Python package; it has no runtime dependencies. The skill runs the bundled CLI.
- **Extension points.** Compressors, decision points, signals and host adapters can be added by installed packages or trusted drop-ins: [Extending](extending.md).

The full design, including alternatives considered, is in the [design spec](docs/specs/2026-11-25-token-sparen-design.md).
