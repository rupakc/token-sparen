# Troubleshooting

Start with `/token-sparen doctor` (or `token-sparen doctor` in a terminal). It prints one line per check: token-sparen and Python versions, the Python the hooks will use (`hook python`), the plugin and `PATH` CLI versions (with a skew warning), whether the state directory is writable, the plugin's hook files, a synthetic compress-and-recover round trip and leftover temp files (`doctor --fix` removes those). It exits non-zero if a check fails.

Hooks fail open: any internal error, or a hook slower than 400 ms, returns "no change" and logs one line to `~/.token-sparen/logs/errors.jsonl`. Look there when something seems silently off.

## Nothing gets trimmed

Work through these in order.

1. **Hooks need Python ≥ 3.11.** The hooks start `python3 -I` (on Windows `py -3 -I`, else `python -I`); if that is older (macOS ships 3.9, Ubuntu 22.04 3.10) the launcher looks for a newer Python and re-runs the hook on it. If it finds none, the hooks keep your session working (fail open), show an "inactive" warning at most once a day and log one line a day to `~/.token-sparen/logs/errors.jsonl` (`"kind": "launcher"`). Install Python 3.11 or newer (python.org, Homebrew, your package manager, or `uv python install 3.12`) or set `TOKEN_SPAREN_PYTHON`, and restart Claude Code. Details below.
2. **Claude Code is too old.** Replacing a tool result needs the `updatedToolOutput` hook feature. The minimum is in the [support matrix](support.md); older hosts run the hooks but nothing is trimmed.
3. **The content type is not allowed.** Only diffs, search results, test reports, compiler output, build logs, JSON, `pplx` results and timestamped logs are offered for trimming. File contents (the Read tool, `cat`, `head`, ...), plain text, tables and stack traces are shown in full by default. See [Output compression](mechanisms/compression.md).
4. **The output is below the floor.** Results under 2000 estimated tokens are never modified.
5. **The result was not smaller enough**: a replacement is used only if it saves at least 15 % and is not larger than what the model would have seen.
6. **A failing command.** Claude Code cannot replace the result of a failed call.
7. **Early baseline.** On a brand-new project roughly one in five large outputs is deliberately left whole; try again.
8. **It is switched off.** Check `TOKEN_SPAREN_DISABLE` in the environment Claude Code was launched from, `claude plugin list`, `[learn.hold] compression = true`, `allow_ctypes = []` or a `/token-sparen disable` earlier in the session.
9. **A project file narrowed it.** `<project>/token-sparen.toml` can only make token-sparen intervene less. Ignored values are logged once.

## The launcher and TOKEN_SPAREN_PYTHON

The launcher tries, in order: `TOKEN_SPAREN_PYTHON` (a path or command name, if you set one), then `python3.15` down to `python3.11` and `python3` on your `PATH` (on Windows also `py -3.15` down to `py -3.11` and `py -3`), then `uv python find ">=3.11"` if `uv` is installed (downloads disabled; `TOKEN_SPAREN_NO_UV=1` skips it). A candidate is used only if it reports version 3.11 or newer itself. The choice is cached in `~/.token-sparen/launcher.json`, and the whole search is bounded to 2 seconds. The extra interpreter start costs tens of milliseconds per hook call and is paid only when your `python3` is older than 3.11; put Python 3.11 or newer first on your `PATH` as `python3` to avoid it.

- `TOKEN_SPAREN_PYTHON` must be set in the environment Claude Code is started from (your shell profile); `config.toml` cannot set it, because the launcher runs before any Python 3.11 can read it. It is honoured only if it resolves outside the current project and its git work tree and, on macOS and Linux, the interpreter file and its directory are owned by you or root and not writable by group or others; otherwise it is skipped with one line in `logs/errors.jsonl`. Some installers fail that ownership check; such a Python is still found through `PATH` (`python3.12` and so on).
- A "nothing found" result is remembered for up to 10 minutes. A new `python3.x` on `PATH` is noticed at once; a Python found only through uv is picked up after those 10 minutes, or delete `~/.token-sparen/launcher.json`.
- `token-sparen doctor` prints a `hook python` line naming the interpreter the hooks will use, and fails if there is none. `doctor` itself needs Python 3.11 or newer too: `python3 -I <plugin dir>/hooks/run_cli.py doctor` finds one the same way.

## Switch it off to rule it out

`TOKEN_SPAREN_DISABLE=1` before launching Claude Code makes every hook a no-op. If the problem persists, it is not token-sparen.

## Recovering output

Use the id or path in the marker: `token-sparen show <id>` or read `<project>/.token-sparen/outputs/<id>.txt`. Files older than 14 days (or beyond 200 files or 100 MB) are pruned. If `show` finds nothing, pass `--project <dir>` for the right project. `show` serves only outputs this install saved itself, so after an upgrade from an older version it does not serve outputs saved before the upgrade: read the file at the path in the marker instead (it stays until the 14-day cleanup). See [Recovering trimmed output](host-guides/claude-code.md#recovering-trimmed-output).

## CLI and plugin version skew

The hooks use the plugin's own copy; the `token-sparen` command on your `PATH` may be another version. `doctor` prints both and warns on a mismatch. Upgrade the older one: [Upgrade](upgrade.md).

## The skill says it works in Claude Code only

You are on a surface other than Claude Code, such as claude.ai chat or Cowork. token-sparen is unsupported there; see the [support matrix](support.md).

## A hook seems slow or erroring

Read `~/.token-sparen/logs/errors.jsonl` (one JSON line per error) and the sampled `~/.token-sparen/logs/latency.jsonl`. Report problems at the repository's issue tracker without pasting private output; for vulnerabilities use [SECURITY.md](SECURITY.md).

## Reset

`rm -r ~/.token-sparen` is always safe: learning restarts conservatively. Delete `<project>/.token-sparen/` to drop recovery files for one project. To remove everything see [Uninstall](uninstall.md).
