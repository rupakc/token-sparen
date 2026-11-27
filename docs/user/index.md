# token-sparen

token-sparen is an open-source Claude Code plugin that cuts token usage without hurting output quality. It trims noisy tool output (test runs, build logs, JSON, diffs, searches) before the model sees it, never trims file contents by default, keeps the full text on disk so nothing is lost, blocks a few clearly unbounded commands and shows a live token meter. Everything runs locally; there is no telemetry and no network access.

## Start here

1. [Install](install.md) the plugin (and, optionally, the command-line tool).
2. Check the [support matrix](support.md): macOS and Linux, Claude Code only for v1.0.
3. Run `/token-sparen doctor`, then see the [Claude Code guide](host-guides/claude-code.md) for what changes in your session.
4. Something not trimmed, or not working? [Troubleshooting](troubleshooting.md).
5. Want it off? [Turning it off](host-guides/claude-code.md#turning-it-off) and [Uninstall](uninstall.md).

## What to read

| If you want to know | Read |
|---|---|
| What it does and the vocabulary | [Concepts](concepts.md) |
| How the pieces fit | [Architecture](architecture.md), [Learning engine](learning-engine.md) |
| One mechanism in depth | [Compression](mechanisms/compression.md), [Guards](mechanisms/guards.md), [Read de-duplication](mechanisms/read-dedupe.md), [Governor](mechanisms/governor.md), [Boundary](mechanisms/boundary.md), [Subagent brief](mechanisms/subagent.md), [Facts](mechanisms/facts.md), [Tool pruning](mechanisms/tool-pruning.md), [Loop guard](mechanisms/loop-guard.md), [Statusline](mechanisms/statusline.md) |
| Every setting | [Configuration](configuration.md) |
| What is written to disk and the security model | [Privacy and security](privacy-security.md) |
| How results are measured and what may be claimed | [Benchmarks and methodology](benchmarks-methodology.md) |
| Writing a compressor or other extension | [Extending](extending.md) |
| Upgrading | [Upgrade](upgrade.md) |

## Status and roadmap

This is the Claude Code release (v1.0). Support for other agent hosts and further ideas (an API compression proxy for clients without hooks, shared team priors, more host adapters) are listed as future work in the [design spec](docs/specs/2026-11-25-token-sparen-design.md), section 15; none of it is promised for a date.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE); security reports go through [SECURITY.md](SECURITY.md); contributing is in [CONTRIBUTING.md](CONTRIBUTING.md); changes are in [CHANGELOG.md](CHANGELOG.md).
