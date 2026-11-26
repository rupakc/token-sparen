"""Layered configuration: defaults < user file < project file < env < session command."""

from __future__ import annotations

import hashlib
import json
import math
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
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


_REDACT: ContextVar[bool] = ContextVar("token_sparen_redact_logs", default=False)
REDACTED = "(details withheld: offline evaluation of private transcripts)"


@contextmanager
def redacted_logs() -> Iterator[None]:
    """While active, ``log_error`` keeps only where an error happened, never its message: offline
    evaluation runs compressors over private transcripts, and an exception text could quote them."""
    token = _REDACT.set(True)
    try:
        yield
    finally:
        _REDACT.reset(token)


def log_error(env: Mapping[str, str], where: str, message: str) -> None:
    """Append one JSON line to errors.jsonl; never raises."""
    if _REDACT.get():
        message = REDACTED
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


# The project file (``<project>/token-sparen.toml``) is committed, so possibly someone else's. It
# is deny-by-default: a cloned repository's config may only make token-sparen intervene less or
# keep more, never trim more, recover less or block more. Every project leaf is checked against
# ``PROJECT_POLICY`` (first matching pattern; ``*`` one segment, ``**`` any rest) and the trusted
# value (defaults, user file, env, session overrides); an unlisted key is user-only. Kinds:
#   free       cosmetic or advisory, normal precedence (still type-checked and bounded);
#              listed key by key, never by wildcard, so a new key is never free by accident
#   user_only  never from a project
#   max / min  project may only raise / lower it (numbers, within bounds)
#   true_only / false_only   only that boolean (holds; switching a mechanism off)
#   union      list the project may only add to (tools never compressed)
#   subset     list the project may only remove from (content types that may be trimmed)
#   enum       an ordered choice the project may only move toward the conservative end
RATIO = (0.0, 1.0)
COUNT = (0.0, 1e12)
POSITIVE = (1.0, 1e12)  # for readers that map <= 0 to their default (a "lower" 0 would loosen)
PROJECT_POLICY: list[tuple[tuple[str, ...], str, Any]] = [
    # learning: only tighter targets, holds and stricter breaker thresholds
    (("learn", "tau", "*"), "min", RATIO),  # a lower regret target trims less
    (("learn", "floor", "*"), "max", RATIO),  # more outputs left untouched
    (("learn", "hold", "*"), "true_only", None),  # a held point never explores
    (("learn", "ladder_obs"), "max", COUNT),  # stronger arms unlock later
    (("learn", "breaker_min_obs"), "min", (1.0, 1e12)),  # the breaker judges sooner
    (("learn", "breaker_every"), "min", (1.0, 1e12)),  # ... and more often
    (("learn", "breaker_lcb_q"), "max", (0.0, 0.49)),  # trips more readily
    (("learn", "breaker_sessions"), "max", COUNT),  # paused longer
    # learn.enabled: switching learning off would fall back to the static arms (standard
    # compression), ignoring the user's holds, tau and floor; a project uses learn.hold instead.
    # breaker_window / breaker_reentry_scale: neither direction is uniformly safer.
    (("learn", "**"), "user_only", None),
    (("extensions", "**"), "user_only", None),
    (("facts", "**"), "user_only", None),
    (("prune", "**"), "user_only", None),
    (("loopguard", "**"), "user_only", None),
    # compression: keep more, compress less
    (("compression", "min_tokens"), "max", COUNT),
    (("compression", "min_saving_ratio"), "max", RATIO),
    (("compression", "skip_tools"), "union", None),
    (("compression", "allow_ctypes"), "subset", None),  # fewer content types trimmed
    (("compression", "trim_file_contents"), "false_only", None),  # file contents kept whole
    (("compression", "save_deadline_ms"), "min", COUNT),  # lower = fewer replacements
    # head/tail are user-only: the budget is max(head + tail, keep_ratio x total), so a larger
    # head/tail displaces prompt-relevant lines rather than keeping more of them
    (("compression", "levels", "*", "keep_ratio"), "max", RATIO),
    (("compression", "levels", "*", "head"), "user_only", None),
    (("compression", "levels", "*", "tail"), "user_only", None),
    (("compression", "json", "items", "*"), "max", COUNT),
    (("compression", "diff", "context", "*"), "max", COUNT),
    (("compression", "stacktrace", "edge_frames", "*"), "max", COUNT),
    (("compression", "test_report", "block_lines", "*"), "max", COUNT),
    (("compression", "search", "per_file", "*"), "max", COUNT),
    (("compression", "pplx", "results", "*"), "max", COUNT),
    (("compression", "log", "tail"), "max", COUNT),
    (("compression", "compiler", "warnings", "*"), "enum", ("count", "keep")),
    (("compression", "persisted_max_bytes"), "user_only", None),
    (("compression", "json", "max_bytes"), "user_only", None),
    (("compression", "**"), "user_only", None),
    (("pricing", "input"), "free", COUNT),  # reporting only
    (("pricing", "cache_write"), "free", COUNT),
    (("pricing", "cache_read"), "free", COUNT),
    (("pricing", "output"), "free", COUNT),
    # recovery: keep saved outputs longer, never less
    (("recovery", "ttl_days"), "max", COUNT),
    (("recovery", "max_files"), "max", COUNT),
    (("recovery", "max_bytes"), "max", COUNT),
    # guards: block less
    (("guards", "enabled"), "false_only", None),
    (("guards", "max_read_bytes"), "max", POSITIVE),
    (("guards", "pretrim_lines"), "user_only", None),  # same displacement as head/tail
    (("guards", "pretrim_head"), "user_only", None),
    # read de-dupe: fewer pointers
    (("read_dedupe", "enabled"), "false_only", None),
    (("read_dedupe", "min_tokens"), "max", COUNT),
    (("read_dedupe", "max_turn_gap"), "min", COUNT),
    (("read_dedupe", "max_entries"), "min", POSITIVE),
    # governor: advisories are free; budgets, the hard stop and parsing are the user's
    (("governor", "enabled"), "false_only", None),
    (("governor", "econ_notice"), "free", RATIO),
    (("governor", "econ_advise"), "free", RATIO),
    (("governor", "ctx_notice"), "free", RATIO),
    (("governor", "ctx_advise"), "free", RATIO),
    (("governor", "**"), "user_only", None),  # hard_stop, session_budget_tokens, windows, ...
    (("boundary", "enabled"), "false_only", None),
    (("boundary", "min_ctx_pct"), "free", RATIO),  # advice thresholds
    (("boundary", "min_overlap"), "free", RATIO),
    (("boundary", "min_turn"), "free", COUNT),
    (("boundary", "history"), "free", (1.0, 20.0)),
    (("boundary", "min_identifiers"), "free", COUNT),
    (("boundary", "cooldown_turns"), "free", COUNT),
    (("subagent", "enabled"), "false_only", None),
    (("statusline", "segments"), "free", None),
    (("statusline", "ascii"), "free", None),
    (("statusline", "color"), "free", None),
    (("statusline", "parse_budget_ms"), "free", (1.0, 1000.0)),
    (("adapter", "latency_budget_ms"), "min", POSITIVE),  # lower = more hooks give up (no-op)
    (("adapter", "latency_sample_rate"), "user_only", None),  # the user's own logs
    (("adapter", "latency_log_max_bytes"), "user_only", None),
]
SAFETY_WARNED_FILE = "config_safety_warned.json"


def _is_num(value: object) -> bool:
    """A finite real number (bool excluded; an int too large for a float is rejected)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _dig(data: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if pattern and pattern[-1] == "**":
        head = pattern[:-1]
        return len(path) >= len(head) and _matches(head, path[: len(head)])
    return len(pattern) == len(path) and all(
        p in ("*", k) for p, k in zip(pattern, path, strict=True)
    )


def _rule(path: tuple[str, ...]) -> tuple[str, Any] | None:
    for pattern, kind, extra in PROJECT_POLICY:
        if _matches(pattern, path):
            return kind, extra
    return None


def policy_for(path: tuple[str, ...]) -> str | None:
    """The project-layer kind of a config key, None when unlisted (treated as user-only)."""
    rule = _rule(path)
    return None if rule is None else rule[0]


def _same_type(value: Any, base: Any) -> bool:
    if isinstance(base, bool) or isinstance(value, bool):
        return isinstance(base, bool) and isinstance(value, bool)
    if isinstance(base, int | float):
        if not _is_num(value):
            return False
        return isinstance(value, int) if isinstance(base, int) else True
    if isinstance(base, str):
        return isinstance(value, str)
    if isinstance(base, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return False


def _in_bounds(value: Any, bounds: Any) -> bool:
    if not _is_num(value) or bounds is None:
        return True
    lo, hi = bounds
    return bool(lo <= value <= hi)


def _tool_list(value: Any) -> list[str] | None:
    if isinstance(value, str):  # env override form: comma-separated
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    return None


def _accept(kind: str, extra: Any, value: Any, base: Any) -> tuple[bool, Any]:
    """(accepted, the value to merge) for one project leaf against its trusted base."""
    if kind == "true_only":
        return value is True, value
    if kind == "false_only":
        return value is False, value
    if kind == "union":
        add, have = _tool_list(value) if isinstance(value, list) else None, _tool_list(base)
        if add is None or have is None:
            return False, value
        return True, have + [x for x in add if x not in have]
    if kind == "subset":
        keep, have = _tool_list(value) if isinstance(value, list) else None, _tool_list(base)
        if keep is None or have is None or any(x not in have for x in keep):
            return False, value
        return True, [x for x in have if x in keep]
    if kind == "enum":
        order = list(extra)
        if value not in order or base not in order:
            return False, value
        return order.index(value) >= order.index(base), value
    if base is _MISSING or not _same_type(value, base) or not _in_bounds(value, extra):
        return False, value
    if kind == "free":
        return True, value
    if kind == "max":
        return bool(value >= base), value
    if kind == "min":
        return bool(value <= base), value
    return False, value  # user_only


def _leaves(node: Any, path: tuple[str, ...]) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(node, Mapping):
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
    """(the project layer rebuilt from its accepted leaves, dropped (key, value) pairs).

    Each leaf of the project file (a scalar replacing a table counts as a leaf; empty tables are
    ignored) is kept only if ``PROJECT_POLICY`` allows its kind and direction against the trusted
    value (the packaged default when the trusted value is missing or of the wrong type)."""
    clean: dict[str, Any] = {}
    dropped: list[tuple[str, Any]] = []
    for path, value in _leaves(project, ()):
        rule = _rule(path)
        if rule is None or rule[0] == "user_only":
            dropped.append((".".join(path), value))
            continue
        kind, extra = rule
        base = _dig(trusted, path)
        fallback = _dig(defaults, path)
        if kind in {"max", "min", "free"} and (base is _MISSING or not _same_type(base, fallback)):
            base = fallback
        if kind == "subset" and _tool_list(base) is None:
            base = fallback
        if kind == "true_only" and base is _MISSING:
            base = False
        ok, merged = _accept(kind, extra, value, base)
        if ok:
            _set(clean, path, merged)
        else:
            dropped.append((".".join(path), value))
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
            "make token-sparen intervene less or keep more (see README); set it in "
            "~/.token-sparen/config.toml, a TOKEN_SPAREN_* variable or `token-sparen config set`",
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
