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
    # Every layer except the project file (defaults, user file, env, session overrides): the
    # base a project value of a safety parameter may only tighten (``safety_param``).
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


# Learning safety parameters a project file (committed, so possibly someone else's) may only
# tighten: "min" lower is safer, "max" higher is safer, "on" only true, "off" only off, "fixed"
# unchanged. User file, env and session overrides are trusted and may set anything.
SAFETY: dict[tuple[str, ...], str] = {
    ("learn", "tau", "*"): "min",  # a lower regret target trims less
    ("learn", "floor", "*"): "max",  # a higher floor keeps more outputs untouched
    ("learn", "hold", "*"): "on",  # a held point never explores
    ("learn", "optin", "*"): "off",  # high-tier opt-in is the user's call
    ("learn", "enabled"): "off",
    ("learn", "ladder_obs"): "max",  # stronger arms unlock later
    ("learn", "breaker_min_obs"): "min",  # the breaker judges sooner
    ("learn", "breaker_every"): "min",  # ... and more often
    ("learn", "breaker_lcb_q"): "max",  # a higher quantile trips more readily
    ("learn", "breaker_sessions"): "max",  # paused longer
    ("learn", "breaker_reentry_scale"): "max",  # keeps more of the evidence that tripped it
    ("learn", "breaker_window"): "fixed",  # neither direction is uniformly safer
    ("extensions", "allow_override"): "off",
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


def _not_looser(value: Any, base: Any, direction: str) -> bool:
    """Is ``value`` at least as safe as ``base``?"""
    if value == base and type(value) is type(base):
        return True
    if direction == "on":
        return value is True
    if direction == "off":
        return flag_off(value)
    if direction == "fixed" or not (_is_num(value) and _is_num(base)):
        return False
    return bool(value <= base) if direction == "min" else bool(value >= base)


def _direction(path: tuple[str, ...]) -> str | None:
    found = SAFETY.get(path)
    if found is None and len(path) > 1:
        found = SAFETY.get((*path[:-1], "*"))
    return found


def safety_param(cfg: Config, key: str) -> Any:
    """The effective value of a safety parameter: the merged value unless it came from the
    project file and is looser than every other layer's (``cfg.trusted``) value; then the
    trusted value. Keys outside ``SAFETY`` (or a config without layers) read as merged."""
    path = tuple(key.split("."))
    value = _dig(cfg.data, path)
    direction = _direction(path)
    if direction is None or cfg.trusted is None:
        return None if value is _MISSING else value
    if value is _MISSING:
        return None
    base = _dig(cfg.trusted, path)
    if base is _MISSING:  # only the project sets it: a switch must be the safe side
        if direction in {"on", "off"}:
            return value if _not_looser(value, None, direction) else None
        return value  # a number with no baseline (e.g. a new tier): readers validate it
    return value if _not_looser(value, base, direction) else base


def _safety_keys(cfg: Config) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for pattern in SAFETY:
        if pattern[-1] != "*":
            out.append(pattern)
            continue
        names: set[str] = set()
        for data in (cfg.data, cfg.trusted):
            node = _dig(data, pattern[:-1])
            if isinstance(node, Mapping):
                names.update(k for k in node if isinstance(k, str))
        out += [(*pattern[:-1], n) for n in sorted(names)]
    return out


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = node[key] = {}
        node = nxt
    if value is _MISSING or value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value


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
            "tighten learning safety settings (set it in ~/.token-sparen/config.toml or the env)",
        )


def _enforce_safety(cfg: Config, project_dir: Path, env: Mapping[str, str]) -> None:
    for path in _safety_keys(cfg):
        key = ".".join(path)
        merged = _dig(cfg.data, path)
        value = safety_param(cfg, key)
        if merged is not _MISSING and value != merged:
            _warn_once(env, project_dir, key, merged)
            _set(cfg.data, path, _MISSING if value is None else value)


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
    data: dict[str, Any] = {}
    trusted: dict[str, Any] = {}
    for target, layers in (
        (data, [defaults, user, project, *late]),
        (trusted, [defaults, user, *late]),
    ):
        for layer in layers:
            _merge(target, layer)
    disabled = env.get("TOKEN_SPAREN_DISABLE", "").strip().lower() in _TRUE
    cfg = Config(data=data, disabled=disabled, trusted=trusted)
    if project:
        _enforce_safety(cfg, project_dir, env)
    return cfg
