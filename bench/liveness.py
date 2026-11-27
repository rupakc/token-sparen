"""Per-arm liveness probes (Review Focus 1: a silently inert arm is ``infra_fail/load``).

Each probe takes the run's `RunMetrics` and its copied-out ``HOME`` and answers whether the arm's
tool actually acted in this run: hook lifecycle events from ``--include-hook-events`` (the hook's
command identifies the plugin; ``command`` is calibrated against real events in Task 13) plus a
state probe where a hook can succeed without doing anything (token-sparen printing ``{}``). Links
in the copied-out home are never followed. The backend copies `LIVENESS_PATHS` out of the
container whatever the arm's ``state_paths``.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from bench.arms import Arm
from bench.streamjson import HookEvent, RunMetrics
from token_sparen.store.session import _safe

Probe = Callable[[RunMetrics, Path], bool]
LIVENESS_PATHS: dict[str, tuple[str, ...]] = {
    "token_sparen": (".token-sparen",),
    "headroom": (".headroom/logs",),
}
_RTK_HOOK = re.compile(r"(?:^|[\s/])rtk\s+hook\b")
_PROXIED = b"/v1/messages"
_MAX_LOG_BYTES = 64 * 1024 * 1024


def _ok_hook(m: RunMetrics, needle: str, event: str | None = None) -> bool:
    return any(_matches(h, needle, event) for h in m.hook_events)


def _matches(h: HookEvent, needle: str, event: str | None) -> bool:
    return h.ok and needle in h.command.lower() and (event is None or h.event == event)


def _real_dir(home: Path, rel: str) -> Path | None:
    """``home/rel`` if every component from ``home`` down is a real directory (no link)."""
    node = home
    for part in ("", *Path(rel).parts):
        node = node / part if part else node
        try:
            mode = node.lstat().st_mode
        except OSError:
            return None
        if not stat.S_ISDIR(mode):
            return None
    return node


def _none(m: RunMetrics, home: Path) -> bool:
    return True


def _token_sparen(m: RunMetrics, home: Path) -> bool:
    if not m.session_id or not _ok_hook(m, "token-sparen"):
        return False
    projects = _real_dir(home, ".token-sparen/projects")
    if projects is None:
        return False
    name = f"{_safe(m.session_id)}.json"
    for project in sorted(projects.iterdir()):
        sessions = _real_dir(project, "sessions")
        if sessions is None:
            continue
        try:
            if stat.S_ISREG((sessions / name).lstat().st_mode):
                return True
        except OSError:
            continue
    return False


def _rtk(m: RunMetrics, home: Path) -> bool:
    return any(
        h.ok and h.event == "PreToolUse" and _RTK_HOOK.search(h.command) for h in m.hook_events
    )


def _headroom(m: RunMetrics, home: Path) -> bool:
    logs = _real_dir(home, ".headroom/logs")
    if logs is None:
        return False
    for dirpath, dirnames, filenames in os.walk(logs, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            path = here / name
            try:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_LOG_BYTES:
                    continue
                if _PROXIED in path.read_bytes():
                    return True
            except OSError:
                continue
    return False


def _context_mode(m: RunMetrics, home: Path) -> bool:
    server = any("context-mode" in n and s == "connected" for n, s in m.mcp_servers.items())
    return server and _ok_hook(m, "context-mode")


def _tokeneconomizer(m: RunMetrics, home: Path) -> bool:
    return _ok_hook(m, "tokeneconomizer")


def _caveman(m: RunMetrics, home: Path) -> bool:
    # whether its default is active under `claude -p` is settled in Task 13
    return _ok_hook(m, "caveman", "SessionStart")


LIVENESS: dict[str, Probe] = {
    "none": _none,
    "token_sparen": _token_sparen,
    "rtk": _rtk,
    "headroom": _headroom,
    "context_mode": _context_mode,
    "tokeneconomizer": _tokeneconomizer,
    "caveman": _caveman,
}


def is_live(m: RunMetrics, arm: Arm, copied_out_home: Path) -> bool:
    return LIVENESS[arm.liveness](m, copied_out_home)
