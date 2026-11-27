# Support matrix

## Surfaces

token-sparen v1.0 supports **Claude Code only**: the Claude Code CLI and the IDE extensions that use it.

Other surfaces can add a directory plugin (the claude.ai chat, Cowork), but they do not run command hooks or `python3` the same way. token-sparen is **unsupported** there. If its skill is loaded on such a surface it replies that token-sparen works in Claude Code only and runs nothing else.

<!-- TODO(merge): the skill's "Claude Code only" reply ships in Task 4 (pre-freeze); Task 15 verifies each surface. -->

## Platforms and versions

| Item | Support |
|---|---|
| macOS | Supported |
| Linux | Supported |
| Windows | Hooks supported where a Python 3.11 or newer is found by the hook launcher; the statusline is not supported |
| Python | 3.11 or newer. Tested in CI on Python 3.11, 3.12 and 3.13 |
| Claude Code | Minimum Claude Code version: 2.1.121 (declared in the plugin manifest) |
| Claude Code, last tested | 2.1.332 |
| Runtime dependencies | None (standard library only) |

<!-- TODO(merge): Windows launcher (Python 3.11 or newer discovery) lands in the M5 launcher task; until it is on main the hooks call `python3` directly. Update the tested Claude Code version after the M5 confirmation run. -->

Replacing a tool result needs the `updatedToolOutput` hook feature, which older Claude Code versions lack: hooks would run, but nothing would be trimmed. Hooks need Python 3.11 or newer; with an older interpreter token-sparen silently does nothing (see [Troubleshooting](troubleshooting.md#nothing-gets-trimmed)).

## Unsupported

- Other agent hosts (planned for later, not v1.0).
- The statusline on Windows.
- Claude surfaces that are not Claude Code, as above.
