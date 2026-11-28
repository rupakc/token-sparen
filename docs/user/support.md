# Support matrix

## Surfaces

token-sparen v1.0 supports **Claude Code only**: the Claude Code CLI and the IDE extensions that use it.

Other surfaces can add a directory plugin (the claude.ai chat, Cowork), but they do not run command hooks or `python3` the same way. token-sparen is **unsupported** there. If its skill is loaded on such a surface it replies that token-sparen works in Claude Code only and runs nothing else.

## Platforms and versions

| Item | Support |
|---|---|
| macOS | Supported |
| Linux | Supported |
| Windows | Hooks supported in Git Bash (Claude Code's default shell on Windows): they start the Python launcher `py -3`, else `python`, never a bare `python3` (often missing or the Microsoft Store alias); then the launcher finds a Python 3.11 or newer (also `py -3.x`). A PowerShell-only setup (no Git Bash) and the statusline are not supported |
| Python | 3.11 or newer somewhere on your machine; the hooks start `python3 -I` (Windows: `py -3 -I`, else `python -I`) and, if that is older, the launcher finds a newer one (see [Troubleshooting](troubleshooting.md#the-launcher-and-token_sparen_python)). Tested in CI on Python 3.11, 3.12 and 3.13 |
| Claude Code | Minimum Claude Code version: 2.1.121 (declared in the plugin manifest) |
| Claude Code, last tested | 2.1.332 |
| Runtime dependencies | None (standard library only) |

Replacing a tool result needs the `updatedToolOutput` hook feature, which older Claude Code versions lack: hooks would run, but nothing would be trimmed. Hooks need Python 3.11 or newer; if the launcher finds none, token-sparen does nothing (your session keeps working) (see [Troubleshooting](troubleshooting.md#nothing-gets-trimmed)).

## Unsupported

- Other agent hosts (planned for later, not v1.0).
- The statusline on Windows.
- Windows without Git Bash (hooks in PowerShell).
- Claude surfaces that are not Claude Code, as above.
