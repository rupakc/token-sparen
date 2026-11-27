"""Test helper: settings that widen what token-sparen does belong in the user's own
``config.toml``. Since the M6 security review a ``TOKEN_SPAREN_*`` variable may only narrow (a
repository's committed Claude Code settings can set environment variables), so tests that used
env overrides to widen write them to the (temporary) user file instead."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import ENV_PREFIX, parse_value


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> None:
    for key, val in over.items():
        if isinstance(val, Mapping):
            cur = base.get(key)
            if not isinstance(cur, dict):
                cur = base[key] = {}
            _merge(cur, val)
        else:
            base[key] = val


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    return json.dumps(str(value))


def _dump(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, Mapping)}
    tables = {k: v for k, v in data.items() if isinstance(v, Mapping)}
    if scalars and prefix:
        lines.append("[" + ".".join(json.dumps(p) for p in prefix) + "]")
    for k, v in scalars.items():
        lines.append(f"{json.dumps(k)} = {_scalar(v)}")
    for k, v in tables.items():
        lines += _dump(v, (*prefix, k))
    return lines


def write_user_config(home: Path, settings: Mapping[str, Any]) -> None:
    """Merge nested ``settings`` into ``<home>/config.toml``."""
    path = home / "config.toml"
    data: dict[str, Any] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    _merge(data, settings)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_dump(data)) + "\n", encoding="utf-8")


def to_user_config(env: Mapping[str, str], home: Path | None = None) -> dict[str, str]:
    """``env`` without its ``TOKEN_SPAREN_<SECTION>__<KEY>`` overrides, which are written to the
    user's ``config.toml`` under ``home`` (default: ``env["TOKEN_SPAREN_HOME"]``) instead."""
    out: dict[str, str] = {}
    settings: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX) or "__" not in name:
            out[name] = raw
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        node = settings
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parse_value(raw)
    if settings:
        write_user_config(Path(home if home is not None else env["TOKEN_SPAREN_HOME"]), settings)
    return out
