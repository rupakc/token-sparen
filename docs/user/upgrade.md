# Upgrade

## Upgrading the plugin

Refresh the marketplace and update:

```
claude plugin marketplace update token-sparen
claude plugin update token-sparen@token-sparen
```

Start a new Claude Code session afterwards so the new hooks load. Run `/token-sparen doctor` to confirm the plugin version.

## Upgrading the command-line tool

If you installed the optional CLI: `pipx upgrade token-sparen`. Keep it on the same version as the plugin. The plugin's hooks and the `/token-sparen` skill always use the plugin's own bundled copy; the `token-sparen` command on your `PATH` is for your terminal. `doctor` prints both versions and warns when they differ (`CLI x differs from plugin y; the plugin's hooks use y`).

<!-- TODO(merge): the skill-prefers-bundled-CLI behaviour and the doctor skew warning land in Task 4 (pre-freeze); verify wording against that branch. -->

## What is kept

State lives in `~/.token-sparen/` and each project's `.token-sparen/`: learning state, learned facts and recovery files survive an upgrade. A newer state file is never overwritten by an older reader: if a file was written by a newer token-sparen than the one reading it, the hooks leave it alone. A state file from an older (0.x) install is loaded, or moved aside with one line in `~/.token-sparen/logs/errors.jsonl`; hooks never fail because of it.

<!-- TODO(merge): 0.x state loading/quarantine and the newer-schema read-only rule land in Task 4 (pre-freeze). -->

## When to reset

`rm -r ~/.token-sparen` is always safe. Learning restarts conservatively (the lightest trimming level, an untouched baseline share), and facts are re-learned over later sessions. Recovery files in `<project>/.token-sparen/outputs/` can be deleted too; trimmed outputs already in a live session then cannot be recovered.

## Config across versions

`~/.token-sparen/config.toml` is yours and is never rewritten. See [Configuration](configuration.md) and the [CHANGELOG](CHANGELOG.md) for changes.
