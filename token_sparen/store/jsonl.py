"""Append-only JSONL with single-generation size rotation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import log_error
from token_sparen.store.jsonstore import locked, replace_with_retry


def append(
    path: Path, record: dict[str, Any], max_bytes: int, env: Mapping[str, str] | None = None
) -> None:
    """Append one record; rotate ``path`` to ``path.1`` once it would exceed ``max_bytes``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with locked(path):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > 0 and size + len(line) > max_bytes:
            try:
                replace_with_retry(path, path.with_name(path.name + ".1"))
            except OSError as exc:  # skip rotation this time rather than lose the record
                log_error(os.environ if env is None else env, "jsonl", f"rotate {path}: {exc}")
        with path.open("ab") as fh:
            fh.write(line)
