# Uninstall

The settings edits token-sparen can make live outside its state folder, so deleting `~/.token-sparen/` alone does not undo them. Do these in order; skip the steps for things you never enabled.

1. In each project where you enabled [tool pruning](mechanisms/tool-pruning.md), remove every deny rule token-sparen wrote (in every recorded checkout) and opt out:

   ```
   token-sparen prune disable
   ```

2. If you installed the [statusline](mechanisms/statusline.md), restore your original `statusLine` setting exactly:

   ```
   token-sparen statusline uninstall
   ```

3. Remove the plugin (and the CLI, if you installed it for your terminal):

   ```
   claude plugin uninstall token-sparen@token-sparen
   pipx uninstall token-sparen
   ```

4. Delete its data (plain text, safe to delete): `~/.token-sparen/` (or the directory in `TOKEN_SPAREN_HOME`) and each project's `<project>/.token-sparen/`. Backups named `*.token-sparen.bak` next to your settings files can go too once you have checked them.

Uninstalling the plugin alone leaves the data, the statusline setting and any pruning rules in place, which is why steps 1 and 2 come first. If the state was written by a newer token-sparen than the one you run (after a downgrade), steps 1 and 2 may only list what to edit by hand: see [Downgrading](upgrade.md#downgrading).

To pause instead of removing: `claude plugin disable token-sparen`, or [turn it off](host-guides/claude-code.md#turning-it-off).
