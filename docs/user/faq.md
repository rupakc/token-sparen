# FAQ

## Does it send my code or prompts anywhere?

No. There is no telemetry and no network access; everything runs locally. See [Privacy and security](privacy-security.md).

## Could trimming hide something I need?

Error lines, failures and stack-trace edges are never elided, file contents are not trimmed by default, and the full text of every trim is on disk with its id in the marker: `token-sparen show <id>`. See [Recovering trimmed output](host-guides/claude-code.md#recovering-trimmed-output).

## How much will it save me?

This documentation makes no savings claims outside the Evidence block on the [benchmarks page](benchmarks-methodology.md). Measure your own sessions with `/token-sparen` (net, including recovery reads) or [replay your own transcripts](benchmarks-methodology.md#measuring-it-on-your-own-sessions-offline-private).

## Why did nothing get trimmed?

Usually Python below 3.11, an old Claude Code, a content type that is not offered for trimming, or an output below 2000 tokens. See [Troubleshooting](troubleshooting.md#nothing-gets-trimmed).

## Does it work in the claude.ai chat, Cowork or other agents?

No. v1.0 supports Claude Code only ([support matrix](support.md)).

## Does it work on Windows?

Hooks, where a Python 3.11 or newer is found; the statusline is not supported ([support matrix](support.md)).

## Does it change my commands?

Never. A command you or the model typed is never rewritten; a guard may block a clearly unbounded call with a reason ([Guards](mechanisms/guards.md)).

## Which files does it write outside its own folders?

Only if you opt in: the statusline writes Claude Code's `settings.json` (with a backup and an exact restore), tool pruning writes the checkout's `.claude/settings.local.json` under strict conditions. See [Privacy and security](privacy-security.md#what-it-writes-to-disk).

## Can a repository change how token-sparen behaves?

Only to make it intervene less. A project's `token-sparen.toml` is deny-by-default and environment variables can only narrow ([Configuration](configuration.md#precedence)).

## Why does the learner trim less at first?

A new project starts with the lightest level and unlocks stronger ones only after the previous one has been observed enough with low regret ([Learning engine](learning-engine.md)).

## How do I turn it off, or remove it?

[Turning it off](host-guides/claude-code.md#turning-it-off) and [Uninstall](uninstall.md).

## Can I write my own compressor?

Yes: [Extending](extending.md).
