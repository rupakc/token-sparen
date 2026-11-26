# token-sparen

A learned, recoverable token optimiser for coding agents. token-sparen trims
noisy tool output before it reaches the model, keeps the full text on disk so
nothing is lost, and learns from past sessions what is safe to elide.

**Status:** pre-release, under active development.

License: MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Live statusline (opt-in, writes your Claude Code settings)

Plugins cannot register a statusline, so `token-sparen statusline install` writes the `statusLine`
key of your user settings (`$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json`).
Before the first change it copies the file to `settings.json.token-sparen.bak` (never overwritten
later) and records the original `statusLine` in `~/.token-sparen/statusline_install.json`. An
existing statusline command is kept: ours is appended after it. Only `statusLine` is touched; a
settings file that is not valid JSON is refused. `token-sparen statusline uninstall` restores the
original `statusLine` exactly (or removes the key) and leaves every other setting as it is now.
Example: `⛁ ts 42% ctx │ burn 48M/500M 10% │ +2.4k/turn │ saved 31.2k (≈$0.84) │ ✂ 12 ↺ 1 │ learn: cold`.

## Extensions (opt-in, explicit trust)

Installed Python packages can add compressors, decision points, signals or host adapters through
the entry-point groups `token_sparen.compressors`, `.decision_points`, `.signals` and `.adapters`.
Drop-in `*.py` files in `<project>/.token-sparen/extensions/` or `~/.token-sparen/extensions/` run
only after you trust their exact content: `token-sparen extensions trust <path>` shows the sha256
and the first 40 lines, and `--yes` records it in `~/.token-sparen/trusted_extensions.json`. Any
edit to the file revokes trust; a repository cannot trust its own files. Symlinked, foreign-owned
and world-writable files are skipped. `token-sparen extensions list` shows each drop-in's status;
`extensions untrust <path|sha256>` revokes. Built-in compressors win unless you set
`[extensions] allow_override = true` in `~/.token-sparen/config.toml`. A failing extension, or one
that drops an error line, is disabled for the session and the built-in is used instead. Check your
compressor with `token_sparen.testing.conformance.check_compressor(c) == []`.

**What trust means.** Trusting a drop-in lets it run arbitrary Python code as you, with your files
and credentials, every time a hook uses it. Only trust code you have read or whose author you
trust. The runtime checks (error lines kept, line counts consistent, failures disabled) protect you
from *buggy* extensions, not *malicious* ones: hostile code can do harm the moment it is imported.
Trust follows content, not location: an identical copy of a trusted file in any other project's
`.token-sparen/extensions/` also loads. Use `extensions list` to see what would run, and
`extensions untrust` to revoke.
