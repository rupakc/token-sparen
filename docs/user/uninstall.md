# Uninstall

The settings edits token-sparen can make live outside its state folder, so deleting `~/.token-sparen/` alone does not undo them. Do these in order; skip the steps for things you never enabled.

1. For each project where you enabled [tool pruning](mechanisms/tool-pruning.md), run this from that project's directory (or pass `--project <dir>`). It removes every deny rule token-sparen wrote for the project (in every recorded checkout) and opts it out:

   ```
   token-sparen prune disable
   ```

   To find the projects where pruning is on, look in each of your projects for `.claude/settings.local.json` entries and a `.claude/settings.local.json.token-sparen.bak` next to it, or run `token-sparen prune status` in a project to see its state. Run this while the plugin or CLI is still installed, which is why it comes first.

2. If you installed the [statusline](mechanisms/statusline.md), restore your original `statusLine` setting:

   ```
   token-sparen statusline uninstall
   ```

   This restores your original `statusLine` exactly (or removes the key), but only if you did not edit it after installing. If you did, it is left as it is and the command says so: open `settings.json` (in `$CLAUDE_CONFIG_DIR`, else `~/.claude`) and set or delete the `statusLine` key yourself; your original is in `settings.json.token-sparen.bak`.

3. Remove the plugin (and the CLI, if you installed it for your terminal):

   ```
   claude plugin uninstall token-sparen@token-sparen
   pipx uninstall token-sparen
   ```

4. Delete its data (plain text, safe to delete): `~/.token-sparen/` (or the directory in `TOKEN_SPAREN_HOME`) and each project's `<project>/.token-sparen/`. Backups named `*.token-sparen.bak` next to your settings files can go too once you have checked them.

Uninstalling the plugin alone leaves the data, the statusline setting and any pruning rules in place, which is why steps 1 and 2 come first. If the state was written by a newer token-sparen than the one you run (after a downgrade), steps 1 and 2 may only list what to edit by hand: see [Downgrading](upgrade.md#downgrading).

To pause instead of removing: `claude plugin disable token-sparen`, or [turn it off](host-guides/claude-code.md#turning-it-off).
