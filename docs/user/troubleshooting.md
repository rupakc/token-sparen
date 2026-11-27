# Troubleshooting

Start with `/token-sparen doctor` (or `token-sparen doctor` in a terminal). It prints one line per check: token-sparen and Python versions, whether `python3` on your `PATH` is new enough, whether the state directory is writable, the plugin's hook files, a synthetic compress-and-recover round trip and leftover temp files (`doctor --fix` removes those). It exits non-zero if a check fails.

Hooks fail open: any internal error, or a hook slower than 400 ms, returns "no change" and logs one line to `~/.token-sparen/logs/errors.jsonl`. Look there when something seems silently off.

## Nothing gets trimmed

Work through these in order.

1. **Hooks need Python ≥ 3.11.** The hooks run `python3`; with an older interpreter token-sparen silently does nothing rather than breaking your session. macOS's system `python3` is often older than 3.11. Check `python3 --version`; `doctor` warns when `python3` on your `PATH` is below 3.11. Install a newer Python and make it the first `python3` on `PATH` for the environment Claude Code is launched from. On Windows, the hook launcher looks for a Python 3.11 or newer; `doctor` suggests `py -3` or `python` if `python3` is missing.
2. **Claude Code is too old.** Replacing a tool result needs the `updatedToolOutput` hook feature. The minimum is in the [support matrix](support.md); older hosts run the hooks but nothing is trimmed.
3. **The content type is not allowed.** Only diffs, search results, test reports, compiler output, build logs, JSON, `pplx` results and timestamped logs are offered for trimming. File contents (the Read tool, `cat`, `head`, ...), plain text, tables and stack traces are shown in full by default. See [Output compression](mechanisms/compression.md).
4. **The output is below the floor.** Results under 2000 estimated tokens are never modified.
5. **The result was not smaller enough**: a replacement is used only if it saves at least 15 % and is not larger than what the model would have seen.
6. **A failing command.** Claude Code cannot replace the result of a failed call.
7. **Early baseline.** On a brand-new project roughly one in five large outputs is deliberately left whole; try again.
8. **It is switched off.** Check `TOKEN_SPAREN_DISABLE` in the environment Claude Code was launched from, `claude plugin list`, `[learn.hold] compression = true`, `allow_ctypes = []` or a `/token-sparen disable` earlier in the session.
9. **A project file narrowed it.** `<project>/token-sparen.toml` can only make token-sparen intervene less. Ignored values are logged once.

## Switch it off to rule it out

`TOKEN_SPAREN_DISABLE=1` before launching Claude Code makes every hook a no-op. If the problem persists, it is not token-sparen.

## Recovering output

Use the id or path in the marker: `token-sparen show <id>` or read `<project>/.token-sparen/outputs/<id>.txt`. Files older than 14 days (or beyond 200 files or 100 MB) are pruned. If `show` finds nothing, pass `--project <dir>` for the right project. See [Recovering trimmed output](host-guides/claude-code.md#recovering-trimmed-output).

## CLI and plugin version skew

The hooks use the plugin's own copy; the `token-sparen` command on your `PATH` may be another version. `doctor` prints both and warns on a mismatch. Upgrade the older one: [Upgrade](upgrade.md).

## The skill says it works in Claude Code only

You are on a surface other than Claude Code, such as claude.ai chat or Cowork. token-sparen is unsupported there; see the [support matrix](support.md).

## A hook seems slow or erroring

Read `~/.token-sparen/logs/errors.jsonl` (one JSON line per error) and the sampled `~/.token-sparen/logs/latency.jsonl`. Report problems at the repository's issue tracker without pasting private output; for vulnerabilities use [SECURITY.md](SECURITY.md).

## Reset

`rm -r ~/.token-sparen` is always safe: learning restarts conservatively. Delete `<project>/.token-sparen/` to drop recovery files for one project. To remove everything see [Uninstall](uninstall.md).
