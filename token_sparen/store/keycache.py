"""Cached project keys: avoid spawning git on every hook.

``~/.token-sparen/project_keys.json`` maps sha256(realpath(project_dir)) to
``{"key": <project key>, "git_config_mtime_ns": <int | None>}``. An entry is recomputed only when
it is missing or ``<dir>/.git/config`` changed mtime. The map is keyed by a hash so no raw path
lands in the shared store.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import log_error, state_home
from token_sparen.store import jsonstore, paths

CACHE_NAME = "project_keys.json"


def _git_config_mtime(project_dir: Path) -> int | None:
    try:
        return (project_dir / ".git" / "config").stat().st_mtime_ns
    except OSError:
        return None


def cached_project_key(project_dir: Path, env: Mapping[str, str]) -> str:
    """Project key for ``project_dir``; never raises on cache trouble (computes directly)."""
    real = os.path.realpath(project_dir)
    slot = hashlib.sha256(real.encode("utf-8")).hexdigest()
    mtime = _git_config_mtime(project_dir)
    cache_path = state_home(env) / CACHE_NAME
    try:
        entry = jsonstore.read(cache_path, {}, env).get(slot)
    except OSError:
        entry = None
    if (
        isinstance(entry, dict)
        and isinstance(entry.get("key"), str)
        and entry.get("git_config_mtime_ns") == mtime
    ):
        return str(entry["key"])
    key = paths.project_key(project_dir)

    def put(data: dict[str, Any]) -> dict[str, Any]:
        data[slot] = {"key": key, "git_config_mtime_ns": mtime}
        return data

    try:
        jsonstore.update(cache_path, put, {}, env)
    except (jsonstore.StoreBusy, OSError) as exc:
        log_error(env, "keycache", f"{type(exc).__name__}: {exc}")
    return key
