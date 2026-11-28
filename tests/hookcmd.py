"""Test helper: the hooks.json command, as v1 review rulings M9 and C1 define it.

POSIX shells (WSL included) run ``python3``. Only a Windows shell (Git Bash, MSYS, Cygwin: the
ones that ship ``cygpath``) that is not WSL (``uname -r`` naming microsoft) tries the ``py``
launcher, then ``python.exe``. Whatever fails (no interpreter, the Store alias stub, a non-zero
exit), the hook prints the no-op ``{}`` and exits 0: a hook never blocks the host. ``command -v``
is silent when the name is missing (``hash`` prints "not found" even with stderr closed), and
its path goes to stderr only in a Windows shell, so stdout stays the hook's JSON and POSIX
shells print nothing to stderr.
"""

from __future__ import annotations

SCRIPT = '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py"'


def hook_command(event: str) -> str:
    run = f"{SCRIPT} {event}"
    return (
        "{ command -v cygpath >&2 && ! uname -r 2>&- | grep -qi microsoft && "
        f"{{ command -v py.exe >&2 && py.exe -3 -I {run} || python.exe -I {run}; }} "
        f"|| python3 -I {run}; }} || echo {{}}"
    )
