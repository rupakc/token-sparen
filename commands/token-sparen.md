---
description: Show token-sparen savings, recovery help, statusline setup, or disable it for this session
argument-hint: "[stats | statusline | disable | enable]"
allowed-tools: Bash(token-sparen:*), Bash(python3:*)
---

Run token-sparen commands with the `token-sparen` console script. If `token-sparen` is not
installed (command not found), run the same arguments through the plugin bootstrap instead:
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <args>`.

Argument given: `$ARGUMENTS`

1. No argument or `stats`: run `token-sparen stats` (add `--session <id>` when you know the
   current session id) and summarise tokens saved, trims and recovery reads in two or three lines.
2. Always end with short recovery help: a trimmed output ends with a
   `[token-sparen: kept X of Y lines ...]` marker; `token-sparen show <id>` prints the full text
   saved on disk.
3. `statusline`: offer `token-sparen statusline install` (it edits Claude Code settings, keeps a
   backup, and `token-sparen statusline uninstall` reverses it). Run it only if the user agrees.
4. `disable`: run `token-sparen session disable` (pass `--session <id>` if `CLAUDE_SESSION_ID`
   is not set) and confirm that outputs are no longer trimmed in this session.
   `enable` runs `token-sparen session enable`.
5. Anything else: run `token-sparen doctor` and report any failing check.
