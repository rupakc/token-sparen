# Uninstall

The settings edits token-sparen can make live outside its state folder, so deleting `~/.token-sparen/` alone does not undo them. Worse, that folder holds the records of which deny rules token-sparen wrote: delete it first and the rules stay in your settings for good, with nothing left that knows to remove them. Do these in order; skip the steps for things you never enabled.

**Without the `token-sparen` command** (you installed only the plugin), run each `token-sparen <args>` below as the plugin's bundled CLI: `python3 -I "<plugin dir>/hooks/run_cli.py" <args>` (on Windows `py -3 -I` or `python -I` instead of `python3 -I`). The plugin directory is the `installPath` entry for `token-sparen` in `~/.claude/plugins/installed_plugins.json`; this is why the plugin is removed only in step 3.

1. For each project where you enabled [tool pruning](mechanisms/tool-pruning.md), run these from that project's directory (or pass `--project <dir>`). The first removes every deny rule token-sparen wrote there; the second also covers the project's other recorded checkouts and opts it out:

   ```
   token-sparen prune restore --all
   token-sparen prune disable
   ```

   To find the projects where pruning is on, look in each of your projects for a `.token-sparen/settings.local.json.token-sparen.bak` (older versions put it next to `.claude/settings.local.json`), or run `token-sparen prune status` in a project to see its state. If you already deleted `~/.token-sparen/`, `token-sparen doctor` (run in the project, while its `.token-sparen/` folder with the pruning backup still exists) lists the deny rules added after that backup that no record covers any more; remove the ones you did not add yourself from `"permissions"."deny"` by hand.

2. If you installed the [statusline](mechanisms/statusline.md), restore your original `statusLine` setting:

   ```
   token-sparen statusline uninstall
   ```

   This restores your original `statusLine` exactly (or removes the key), but only if you did not edit it after installing. If you did, it is left as it is and the command says so: open `settings.json` (in `$CLAUDE_CONFIG_DIR`, else `~/.claude`) and set or delete the `statusLine` key yourself; your original is in `settings.json.token-sparen.bak`.

3. Remove the plugin (and the console command, if you installed one with `pip install .`, from that environment):

   ```
   claude plugin uninstall token-sparen@token-sparen
   pip uninstall token-sparen
   ```

4. Delete its data (plain text, safe to delete once steps 1 and 2 are done): `~/.token-sparen/` (or the directory in `TOKEN_SPAREN_HOME`) and each project's `<project>/.token-sparen/` (it also holds the tool-pruning backup). Backups named `*.token-sparen.bak` next to your settings files can go too once you have checked them.

Uninstalling the plugin alone leaves the data, the statusline setting and any pruning rules in place, which is why steps 1 and 2 come first. If the state was written by a newer token-sparen than the one you run (after a downgrade), steps 1 and 2 may only list what to edit by hand: see [Downgrading](upgrade.md#downgrading).

To pause instead of removing: `claude plugin disable token-sparen`, or [turn it off](host-guides/claude-code.md#turning-it-off).
