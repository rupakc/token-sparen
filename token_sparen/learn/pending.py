"""Per-project index of sessions awaiting a fold: ``projects/<key>/pending.json``.

``{<overlay name>: first_seen_epoch}``, where the overlay name is the session overlay's file stem
(the sanitised session id; ``session.session_path`` maps it to the same file). An entry is added
on the first evidence write of a session (engine reward), on the first settled regret outcome,
and when a fold stages; the fold removes it once it clears. SessionStart reads only this index
(no directory scan); ``token-sparen fold --all`` also scans the directory as a fallback.

Its lock is always taken alone by hooks, and by the fold only under the project posteriors lock
with nothing else held (see ``learn.overlay.LOCK_ORDER``). Fails open.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import log_error
from token_sparen.store import jsonstore
from token_sparen.store.paths import project_dir_for

PENDING_FILE = "pending.json"


def path_for(home: Path, project_key: str) -> Path:
    return project_dir_for(home, project_key) / PENDING_FILE


def entries(home: Path, project_key: str, env: Mapping[str, str]) -> dict[str, float]:
    """The index (lock-free read); malformed entries are dropped."""
    path = path_for(home, project_key)
    if not path.exists():
        return {}
    doc = jsonstore.read(path, {}, env)
    out: dict[str, float] = {}
    for name, ts in doc.items():
        if isinstance(name, str) and isinstance(ts, int | float) and not isinstance(ts, bool):
            out[name] = float(ts)
    return out


def add(
    home: Path,
    project_key: str,
    name: str,
    env: Mapping[str, str],
    *,
    timeout: float | None = None,
) -> None:
    """Index overlay ``name`` (no write when already there). Never raises."""
    try:
        if name in entries(home, project_key, env):
            return

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            doc.setdefault(name, time.time())
            return doc

        jsonstore.update(path_for(home, project_key), fn, {}, env, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "pending", f"add: {type(exc).__name__}: {exc}")


def add_overlay(overlay: Path, env: Mapping[str, str]) -> None:
    """``add`` for an overlay path ``<home>/projects/<key>/sessions/<name>.json``."""
    try:
        home, key = overlay.parents[3], overlay.parents[1].name
    except IndexError:
        return
    add(home, key, overlay.name.removesuffix(".json"), env)


def remove(
    home: Path,
    project_key: str,
    name: str,
    env: Mapping[str, str],
    *,
    timeout: float | None = None,
) -> None:
    """Drop overlay ``name`` from the index (no write when absent)."""
    if name not in entries(home, project_key, env):
        return

    def fn(doc: dict[str, Any]) -> dict[str, Any]:
        doc.pop(name, None)
        return doc

    jsonstore.update(path_for(home, project_key), fn, {}, env, timeout=timeout)
