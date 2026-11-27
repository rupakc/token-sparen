# Install

Check the [support matrix](support.md) first: Claude Code only, on macOS and Linux (Windows for hooks only).

## Release install (marketplace)

```
claude plugin marketplace add rupakc/token-sparen
claude plugin install token-sparen@token-sparen
```

Start a new Claude Code session afterwards (or reload plugins) so the hooks are picked up. That is all the plugin needs; it has no runtime dependencies beyond Python 3.11 or newer.

<!-- TODO(merge): Task 9/14 make the marketplace and the plugin directory track the generated release/claude branch; confirm this wording when that lands. -->

Installing from the Anthropic plugin directory, once the plugin is listed there, uses the same plugin files and the same `/token-sparen` skill.

## Dev install (from a checkout)

To try unreleased code, or to work on token-sparen, point Claude Code at a checkout of the repository:

```
claude --plugin-dir <checkout>
```

This loads the plugin for that session only. Nothing is installed; see [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup.

## Optional command-line tool

You do not need the `token-sparen` command. In Claude Code, ask for `/token-sparen` (stats), `/token-sparen doctor` or `/token-sparen disable`; the skill runs the CLI bundled with the plugin. For use in your own terminal:

```
pipx install token-sparen
```

<!-- TODO(merge): the PyPI project is published by the owner-approved release; until then use `pip install .` from a checkout. -->

To run the bundled copy from a terminal instead, call the plugin's launcher: `python3 <plugin dir>/hooks/run_cli.py <args>`. The plugin directory is the `installPath` entry for `token-sparen` in `~/.claude/plugins/installed_plugins.json`.

Keep the CLI and the plugin on the same version: the plugin's hooks always use the plugin's own copy. `doctor` warns when they differ; see [Upgrade](upgrade.md).

## Verify

1. `/token-sparen doctor` in Claude Code (or `token-sparen doctor` in a terminal). It checks Python, the state directory, the hook files and a synthetic compress-and-recover round trip. The round trip runs inside token-sparen, so it cannot tell whether your Claude Code honours the hooks.
2. Smoke test: in a new session ask Claude to run your test suite verbosely (for example `pytest -v`) so that the output is longer than about 2000 tokens. Usually the result is trimmed and ends with a `[token-sparen: kept ... · full output: ...]` marker, and `/token-sparen` then shows a non-zero trim count. On a brand-new project roughly one in five large outputs is deliberately left whole at first (a baseline the learner needs), so if you see no marker, try a second time. Still nothing? See [Troubleshooting](troubleshooting.md#nothing-gets-trimmed).
3. Without the host: `token-sparen run -- <a command with big output>` prints what token-sparen would show for that command, marker included. It does not prove your Claude Code honours the hooks; the smoke test does.

## Next

[Claude Code guide](host-guides/claude-code.md) for what changes in your session and [Uninstall](uninstall.md) to remove it again.
