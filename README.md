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
