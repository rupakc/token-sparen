# Upgrade

## Upgrading the plugin

Refresh the marketplace and update:

```
claude plugin marketplace update token-sparen
claude plugin update token-sparen@token-sparen
```

Start a new Claude Code session afterwards so the new hooks load. Run `/token-sparen doctor` to confirm the plugin version.

## Upgrading the command-line tool

If you installed the optional CLI: `pipx upgrade token-sparen`. Keep it on the same version as the plugin. The plugin's hooks always use the plugin's own bundled copy, and the `/token-sparen` skill runs that bundled CLI first (`token-sparen` on your `PATH` is only a fallback). `doctor` prints the plugin version and the version of the `token-sparen` on your `PATH`, and warns when they differ: `CLI x differs from plugin y; the plugin's hooks use y`. It is a warning only.

## What is kept

State lives in `~/.token-sparen/` and each project's `.token-sparen/`. Learning state, learned facts, the tool-pruning opt-in and records, the statusline install record, sessions and recovery files survive an upgrade.

State written by any 0.x milestone loads on the current code, or is quarantined (or started afresh) with one line in `~/.token-sparen/logs/errors.jsonl`; nothing learned is lost. A versioned state file is never rewritten by a reader that is older than the file: if it was written by a newer token-sparen, the older one leaves it alone and logs it once. Before a v1 `posteriors.json` is rewritten it is copied to `posteriors.v1.bak.json`.

**Recovered outputs from before the upgrade.** `token-sparen show <id>` serves only outputs that this install saved itself, so it does not serve outputs saved before the upgrade; they stay readable at their marker path until the 14-day cleanup.

## Downgrading

Do not downgrade if you can avoid it. If an older token-sparen meets state a newer one wrote, it never rewrites that state, and commands that would change it say so (`state written by token-sparen <version>; upgrade to it, or run <command> from that version`). In particular `prune disable` and `prune restore` then change nothing and list the rules in token-sparen's format that are not in the settings backup, with the file to edit by hand (keep any you added yourself); `statusline uninstall` reverts the `statusLine` it recognises. Upgrading again fixes all of it.

## When to reset

`rm -r ~/.token-sparen` is always safe. Learning restarts conservatively (the lightest trimming level, an untouched baseline share), and facts are re-learned over later sessions. Recovery files in `<project>/.token-sparen/outputs/` can be deleted too; trimmed outputs already in a live session then cannot be recovered. If you enabled tool pruning or the statusline, follow [Uninstall](uninstall.md) first, because those edit Claude Code settings outside the state folder.

## Config across versions

`~/.token-sparen/config.toml` is yours and is never rewritten. See [Configuration](configuration.md) and the [CHANGELOG](CHANGELOG.md) for changes.
