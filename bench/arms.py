"""The arms of the E4 benchmark (`bench/arms.toml`) and their per-run state semantics.

Every run starts from its arm's config template: a fresh ``CLAUDE_CONFIG_DIR`` and ``HOME``
(Claude Code auto-memory disabled), so nothing carries over between runs except, for an arm
with ``state = "stream"``, its ``state_paths`` (relative to the run's ``HOME``) between the
tasks of one (stage, arm, seed, repository) stream. `carry_state` is that copy: it takes only
the declared paths, never follows a symbolic link and replaces the destination.

Competitors run in their documented default configuration, installed into the template from
the pinned version recorded in ``pin`` and ``source`` (the toolchain build fetches exactly that
version; nothing is installed by this module or its tests).
"""

from __future__ import annotations

import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

ARMS_FILE = Path(__file__).resolve().parent / "arms.toml"
KINDS = ("baseline", "prompt", "plugin", "hook", "wrapper")
STATES = ("none", "stream")
LIVENESS_KINDS = frozenset(
    {"none", "token_sparen", "rtk", "headroom", "context_mode", "tokeneconomizer", "caveman"}
)
# set by the harness for every arm; an arm may not override them
HARNESS_ENV = frozenset(
    {
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "TOKEN_SPAREN_HOME",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_MAX_TURNS",
        "DISABLE_AUTOUPDATER",
        "IS_SANDBOX",
    }
)
ARM_KEYS = frozenset(
    {
        "kind",
        "state",
        "state_paths",
        "append_system_prompt",
        "uses_frozen_plugin",
        "install",
        "wrapper",
        "env",
        "ts_config",
        "liveness",
        "patch_excludes",
        "pin",
        "source",
    }
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ArmsError(ValueError):
    """`arms.toml` is malformed; the message names every problem found."""


@dataclass(frozen=True)
class Arm:
    name: str
    kind: Literal["baseline", "prompt", "plugin", "hook", "wrapper"]
    state: Literal["none", "stream"]
    state_paths: tuple[str, ...]
    append_system_prompt: str | None
    uses_frozen_plugin: bool
    install: tuple[str, ...]
    wrapper: tuple[str, ...]
    env: Mapping[str, str]
    ts_config: Mapping[str, Any]
    liveness: str
    patch_excludes: tuple[str, ...]
    pin: str | None = None  # version and commit of the competitor (e4.json `arms[].pin`)
    source: str | None = field(default=None)  # where that version comes from


def _strings(value: object, what: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{what} must be a list of non-empty strings")
        return ()
    return tuple(value)


def _state_path_ok(rel: str) -> bool:
    pure = PurePosixPath(rel)
    return not pure.is_absolute() and ".." not in pure.parts and "\\" not in rel and rel != "."


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    return value


def _arm(name: str, raw: Mapping[str, Any], common: tuple[str, ...], problems: list[str]) -> Arm:
    where = f"arm {name!r}"
    if not _NAME.match(name):
        problems.append(f"{where}: name must match {_NAME.pattern}")
    unknown = sorted(set(raw) - ARM_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {', '.join(unknown)}")
    kind = raw.get("kind")
    if kind not in KINDS:
        problems.append(f"{where}: kind must be one of {', '.join(KINDS)}")
    state = raw.get("state", "none")
    if state not in STATES:
        problems.append(f"{where}: state must be one of {', '.join(STATES)}")
    state_paths = _strings(raw.get("state_paths", []), f"{where}: state_paths", problems)
    if (state == "stream") != bool(state_paths):
        problems.append(f"{where}: state_paths are required for, and only for, state = stream")
    for rel in state_paths:
        if not _state_path_ok(rel):
            problems.append(f"{where}: state path {rel!r} must be relative to HOME, no '..'")
    prompt = raw.get("append_system_prompt")
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        problems.append(f"{where}: append_system_prompt must be a non-empty string")
    if (kind == "prompt") != (prompt is not None):
        problems.append(f"{where}: append_system_prompt is required for, and only for, prompt")
    frozen = raw.get("uses_frozen_plugin", False)
    if not isinstance(frozen, bool) or (frozen and kind != "plugin"):
        problems.append(f"{where}: uses_frozen_plugin must be a boolean, true only for a plugin")
    install = _strings(raw.get("install", []), f"{where}: install", problems)
    wrapper = _strings(raw.get("wrapper", []), f"{where}: wrapper", problems)
    if (kind == "wrapper") != bool(wrapper):
        problems.append(f"{where}: wrapper is required for, and only for, kind = wrapper")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        problems.append(f"{where}: env values must be strings")
        env = {}
    clash = sorted(set(env) & HARNESS_ENV)
    if clash:
        problems.append(f"{where}: env may not set harness variables {', '.join(clash)}")
    ts_config = raw.get("ts_config", {})
    if not isinstance(ts_config, dict) or (ts_config and not frozen):
        problems.append(f"{where}: ts_config is a table, only for token-sparen arms")
        ts_config = {}
    liveness = raw.get("liveness")
    if liveness not in LIVENESS_KINDS:
        problems.append(f"{where}: liveness must be one of {', '.join(sorted(LIVENESS_KINDS))}")
    extra = _strings(raw.get("patch_excludes", []), f"{where}: patch_excludes", problems)
    pin, source = raw.get("pin"), raw.get("source")
    for key, value in (("pin", pin), ("source", source)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{where}: {key} must be a non-empty string when given")
    return Arm(
        name=name,
        kind=cast(Any, kind),
        state=cast(Any, state),
        state_paths=state_paths,
        append_system_prompt=prompt if isinstance(prompt, str) else None,
        uses_frozen_plugin=frozen is True,
        install=install,
        wrapper=wrapper,
        env=MappingProxyType(dict(env)),
        ts_config=_freeze(dict(ts_config)),
        liveness=str(liveness),
        patch_excludes=tuple(dict.fromkeys([*common, *extra])),
        pin=pin if isinstance(pin, str) else None,
        source=source if isinstance(source, str) else None,
    )


def load_arms(path: Path = ARMS_FILE) -> dict[str, Arm]:
    """Every arm of ``path`` by name, validated (`ArmsError` lists all problems)."""
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ArmsError(f"arms file unreadable: {exc}") from exc
    problems: list[str] = []
    common_raw = doc.get("common", {})
    common = _strings(common_raw.get("patch_excludes", []), "common.patch_excludes", problems)
    raw_arms = doc.get("arms")
    if not isinstance(raw_arms, dict) or not raw_arms:
        raise ArmsError("arms file defines no [arms.<name>] table")
    arms = {name: _arm(name, raw, common, problems) for name, raw in raw_arms.items()}
    if problems:
        raise ArmsError("; ".join(problems))
    return arms


def carry_state(arm: Arm, home: Path, dest: Path) -> list[str]:
    """Copy the ``state_paths`` of a ``stream`` arm from a finished run's ``home`` to ``dest``.

    ``dest`` is replaced (only the last promoted state of a stream is kept). Nothing is copied
    for ``state = "none"``. Symbolic links are skipped, never followed, at any depth. Returns
    the state paths that existed and were copied.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied: list[str] = []
    if arm.state != "stream":
        return copied
    for rel in arm.state_paths:
        src = home.joinpath(*PurePosixPath(rel).parts)
        if src.is_symlink() or not src.exists():
            continue
        target = dest.joinpath(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=_ignore_symlinks)
        else:
            shutil.copy2(src, target)
        copied.append(rel)
    return copied


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if Path(directory, n).is_symlink()}
