# Uninstall

Do these in order; skip the steps for things you never enabled.

1. If you installed the [statusline](mechanisms/statusline.md), remove it first so your original `statusLine` setting is restored exactly:

   ```
   token-sparen statusline uninstall
   ```

2. If you opted in to [tool pruning](mechanisms/tool-pruning.md), remove every rule token-sparen wrote (and disable pruning):

   ```
   token-sparen prune restore --all
   token-sparen prune disable
   ```

   `prune disable` also restores everything in every checkout and removes the settings file again if token-sparen created it.

3. Remove the plugin:

   ```
   claude plugin uninstall token-sparen@token-sparen
   ```

4. Delete its data (both are plain text and safe to delete): `~/.token-sparen/` (or the directory in `TOKEN_SPAREN_HOME`) and each project's `<project>/.token-sparen/`.

5. If you installed the CLI: `pipx uninstall token-sparen`.

Uninstalling the plugin does not delete the data, the statusline setting or pruning rules by itself, which is why steps 1 and 2 come first. The backups token-sparen made (`settings.json.token-sparen.bak`, `settings.local.json.token-sparen.bak`) are left in place for you to delete.

To pause instead of removing: `claude plugin disable token-sparen`, or [turn it off](host-guides/claude-code.md#turning-it-off).
