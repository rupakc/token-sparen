"""Layered configuration: defaults < user file < project file < env < session command."""

from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

ENV_PREFIX = "TOKEN_SPAREN_"
_TRUE = {"true", "1"}
_MISSING: Any = object()


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def state_home(env: Mapping[str, str]) -> Path:
    override = env.get("TOKEN_SPAREN_HOME")
    return Path(override) if override else Path.home() / ".token-sparen"


def log_error(env: Mapping[str, str], where: str, message: str) -> None:
    """Append one JSON line to errors.jsonl; never raises."""
    try:
        path = state_home(env) / "logs" / "errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "where": where, "error": message}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - fail open
        pass


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> None:
    for key, val in over.items():
        if isinstance(val, Mapping):
            cur = base.get(key)
            if not isinstance(cur, dict):
                cur = {}
                base[key] = cur
            _merge(cur, val)
        else:
            base[key] = val


def parse_value(raw: str) -> Any:
    low = raw.strip().lower()
    if low in _TRUE and low != "1":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX) or "__" not in name:
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        if len(parts) < 2:
            continue
        node = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = parse_value(raw)
    return out


def _load_toml(text: str | None, path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        raw = text if text is not None else path.read_text(encoding="utf-8")
        return tomllib.loads(raw)
    except Exception as exc:  # noqa: BLE001 - bad config must never break a hook
        log_error(env, "config", f"{path}: {exc}")
        return {}


def _load_defaults(env: Mapping[str, str]) -> dict[str, Any]:
    text = resources.files("token_sparen").joinpath("params/defaults.toml").read_text("utf-8")
    return _load_toml(text, Path("defaults.toml"), env)


def packaged_defaults() -> dict[str, Any]:
    """The packaged `params/defaults.toml`, parsed (the single source of default values)."""
    return _load_defaults({})


SESSION_KEY = "config_overrides"


def load_config(
    project_dir: Path, env: Mapping[str, str], *, session: Mapping[str, Any] | None = None
) -> Config:
    """Merged config. ``session`` is the session overlay when the session is known: its
    ``config_overrides`` (written by ``token-sparen config set``) are merged last."""
    data: dict[str, Any] = {}
    _merge(data, _load_defaults(env))
    for path in (state_home(env) / "config.toml", project_dir / "token-sparen.toml"):
        if path.is_file():
            _merge(data, _load_toml(None, path, env))
    _merge(data, _env_overrides(env))
    overrides = None if session is None else session.get(SESSION_KEY)
    if isinstance(overrides, Mapping):
        _merge(data, overrides)
    disabled = env.get("TOKEN_SPAREN_DISABLE", "").strip().lower() in _TRUE
    return Config(data=data, disabled=disabled)
