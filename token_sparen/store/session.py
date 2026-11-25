"""Per-session state files."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from token_sparen.store import jsonstore
from token_sparen.store.paths import project_dir_for

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(session_id: str) -> str:
    cleaned = _UNSAFE.sub("_", session_id)
    return "_" if cleaned.strip(".") == "" else cleaned


def session_path(home: Path, project_key: str, session_id: str) -> Path:
    return project_dir_for(home, project_key) / "sessions" / f"{_safe(session_id)}.json"


def read_session(path: Path) -> dict[str, Any]:
    return jsonstore.read(path, {})


def update_session(path: Path, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    return jsonstore.update(path, fn, {})
