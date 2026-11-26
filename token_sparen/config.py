"""Layered configuration: defaults < user file < project file < env < session command."""

from __future__ import annotations

import hashlib
import json
import math
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
    # Every layer except the project file (defaults, user file, env, session overrides): the base
    # an allowlisted project learning setting may only tighten (``sanitise_project``).
    trusted: dict[str, Any] | None = None

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
_OFF = {"0", "no", "false", "off"}


def flag_off(value: object) -> bool:
    """A switch set to off in any spelling: False, 0, "0", "no", "false", "off"."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in _OFF


# The project file is committed, so possibly someone else's: of the learning settings it may set
# only these leaves, each only to a value tighter than every other layer's (defaults, user file,
# env, session overrides). Everything else under [learn], and extensions.allow_override, is user,
# env or session only. Directions: "min" lower is safer, "max" higher is safer, "on" only true,
# "off" only false. Not settable: breaker_window (neither direction is uniformly safer) and
# breaker_reentry_scale (it scales savings, regret and n alike, so neither direction is safer).
PROJECT_LEARN_KEYS: dict[tuple[str, ...], str] = {
    ("learn", "tau", "*"): "min",  # a lower regret target trims less
    ("learn", "floor", "*"): "max",  # a higher floor keeps more outputs untouched
    ("learn", "enabled"): "off",
    ("learn", "hold", "*"): "on",  # a held point never explores
    ("learn", "ladder_obs"): "max",  # stronger arms unlock later
    ("learn", "breaker_min_obs"): "min",  # the breaker judges sooner
    ("learn", "breaker_every"): "min",  # ... and more often
    ("learn", "breaker_lcb_q"): "max",  # a higher quantile trips more readily
    ("learn", "breaker_sessions"): "max",  # paused longer
}
SAFETY_WARNED_FILE = "config_safety_warned.json"


def _is_num(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _dig(data: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _direction(path: tuple[str, ...]) -> str | None:
    found = PROJECT_LEARN_KEYS.get(path)
    if found is None and len(path) > 1:
        found = PROJECT_LEARN_KEYS.get((*path[:-1], "*"))
    return found


def _tighter(value: Any, base: Any, direction: str) -> bool:
    """Is the project ``value`` a scalar of the right type and at least as safe as ``base``
    (the trusted value; for numbers, the packaged default when the trusted one is invalid)?"""
    if direction == "on":
        return value is True
    if direction == "off":
        return value is False
    if not _is_num(value) or not _is_num(base):
        return False
    return bool(value <= base) if direction == "min" else bool(value >= base)


def _leaves(node: Any, path: tuple[str, ...]) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(node, Mapping) and node:
        out: list[tuple[tuple[str, ...], Any]] = []
        for key, val in node.items():
            out += _leaves(val, (*path, str(key)))
        return out
    return [(path, node)]


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = node[key] = {}
        node = nxt
    node[path[-1]] = value


def sanitise_project(
    project: Mapping[str, Any], trusted: Mapping[str, Any], defaults: Mapping[str, Any]
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """(the project layer with only allowlisted, tighter learning leaves, dropped (key, value)).

    The whole ``learn`` subtree and ``extensions.allow_override`` are stripped; each leaf of the
    project's ``learn`` (a scalar replacing a table counts as a leaf) is re-added only if it is an
    allowlisted key with a tighter scalar of the right type. Other sections keep their normal
    project precedence."""
    clean = {k: v for k, v in project.items() if k != "learn"}
    dropped: list[tuple[str, Any]] = []
    ext = clean.get("extensions")
    if isinstance(ext, Mapping) and "allow_override" in ext:
        clean["extensions"] = {k: v for k, v in ext.items() if k != "allow_override"}
        dropped.append(("extensions.allow_override", ext["allow_override"]))
    elif "extensions" in clean and not isinstance(ext, Mapping):
        dropped.append(("extensions", clean.pop("extensions")))  # would wipe the user's table
    if "learn" not in project:
        return clean, dropped
    learn: dict[str, Any] = {}
    for path, value in _leaves(project["learn"], ("learn",)):
        direction = _direction(path)
        base = _dig(trusted, path)
        if direction in {"min", "max"} and not _is_num(base):
            base = _dig(defaults, path)
        if direction == "on" and base is _MISSING:
            base = False
        if direction is not None and base is not _MISSING and _tighter(value, base, direction):
            _set(learn, path[1:], value)
        else:
            dropped.append((".".join(path), value))
    if learn:
        clean["learn"] = learn
    return clean, dropped


def _warn_once(env: Mapping[str, str], project_dir: Path, key: str, value: Any) -> None:
    """Log an ignored project value once per (project, key, value). Best effort."""
    try:
        tag = hashlib.sha256(str(project_dir).encode("utf-8", "surrogatepass")).hexdigest()[:16]
        path = state_home(env) / SAFETY_WARNED_FILE
        try:
            seen = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seen = {}
        seen = seen if isinstance(seen, dict) else {}
        slot, shown = f"{tag}:{key}", json.dumps(value, default=str)
        if seen.get(slot) == shown:
            return
        seen[slot] = shown
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen), encoding="utf-8")
    except Exception:  # noqa: BLE001 - logging must never break a hook
        pass
    else:
        log_error(
            env,
            "config",
            f"{key} = {shown} in the project's token-sparen.toml ignored: a project file may only "
            "tighten tau, floor, enabled, hold, ladder_obs and breaker thresholds; other learning "
            "settings belong in ~/.token-sparen/config.toml, the env or `config set`",
        )


def load_config(
    project_dir: Path, env: Mapping[str, str], *, session: Mapping[str, Any] | None = None
) -> Config:
    """Merged config. ``session`` is the session overlay when the session is known: its
    ``config_overrides`` (written by ``token-sparen config set``) are merged last."""
    defaults = _load_defaults(env)
    user_file = state_home(env) / "config.toml"
    user = _load_toml(None, user_file, env) if user_file.is_file() else {}
    project_file = project_dir / "token-sparen.toml"
    project = _load_toml(None, project_file, env) if project_file.is_file() else {}
    env_layer = _env_overrides(env)
    overrides = None if session is None else session.get(SESSION_KEY)
    late = [env_layer, overrides] if isinstance(overrides, Mapping) else [env_layer]
    trusted: dict[str, Any] = {}
    for layer in (defaults, user, *late):
        _merge(trusted, layer)
    if project:
        project, dropped = sanitise_project(project, trusted, defaults)
        for key, value in dropped:
            _warn_once(env, project_dir, key, value)
    data: dict[str, Any] = {}
    for layer in (defaults, user, project, *late):
        _merge(data, layer)
    disabled = env.get("TOKEN_SPAREN_DISABLE", "").strip().lower() in _TRUE
    return Config(data=data, disabled=disabled, trusted=trusted)
