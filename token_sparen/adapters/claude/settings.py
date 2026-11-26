"""Opt-in install/uninstall of our ``statusLine`` in Claude Code's user settings.

Only the ``statusLine`` key is ever changed; every other key keeps its value and order. Rules:

- a settings file that is not a JSON object is refused (nothing is written anywhere);
- before the first change the file is copied to ``settings.json.token-sparen.bak``, once (an
  older pristine backup is never overwritten);
- the original ``statusLine`` value (or its absence) is recorded in
  ``~/.token-sparen/statusline_install.json`` keyed by the settings file's real path;
- an existing command statusline is composed: ``sh <launcher> --compose -- <original>``;
- re-install is idempotent; uninstall restores exactly the recorded value (or removes the key)
  and leaves a statusline the user changed since install alone;
- writes are atomic (temp file + replace), keep the file mode, and follow a symlinked file.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shlex
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from token_sparen.config import state_home
from token_sparen.store import jsonstore

BACKUP_SUFFIX = ".token-sparen.bak"
META_NAME = "statusline_install.json"
LAUNCHER_NAME = "statusline.sh"
LAUNCHER_MARKER = "token-sparen statusline launcher"
KEY = "statusLine"

Outcome = Literal[
    "installed", "composed", "already", "removed", "restored", "unchanged", "not-installed", "error"
]


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    action: Outcome
    message: str
    command: str | None = None


class _Invalid(ValueError):
    pass


def settings_path(env: Mapping[str, str] | None = None) -> Path:
    """``$CLAUDE_CONFIG_DIR/settings.json``, else ``~/.claude/settings.json``."""
    env = os.environ if env is None else env
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude" / "settings.json"


def _target(path: Path) -> Path:
    """The file actually written: a symlinked settings file is updated in place, link kept."""
    return path.resolve() if path.is_symlink() else path


def load_settings(path: Path) -> tuple[dict[str, Any], bool]:
    """(settings, existed); raises ValueError for a file that is not a JSON object."""
    return _load(_target(path))


def has_installs(env: Mapping[str, str] | None = None) -> bool:
    meta = jsonstore.read(_meta_path(os.environ if env is None else env), {})
    installs = meta.get("installs")
    return isinstance(installs, dict) and bool(installs)


def _load(path: Path) -> tuple[dict[str, Any], bool]:
    """(settings, existed); raises _Invalid for anything but a JSON object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _Invalid(f"{path} is not valid JSON ({exc}); nothing was changed") from exc
    if not isinstance(data, dict):
        raise _Invalid(f"{path} is not valid JSON settings (not an object); nothing was changed")
    return data, True


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: int | None = path.stat().st_mode & 0o7777
    except OSError:
        mode = None
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        jsonstore.replace_with_retry(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _meta_path(env: Mapping[str, str]) -> Path:
    return state_home(env) / META_NAME


def _slot(path: Path) -> str:
    return str(path.resolve())


def _split(command: Any) -> list[str] | None:
    if not isinstance(command, str):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _is_launcher(arg: str, launcher: str) -> bool:
    if arg == launcher:
        return True
    if Path(arg).name != LAUNCHER_NAME:
        return False
    try:
        return LAUNCHER_MARKER in Path(arg).read_text(encoding="utf-8")[:2000]
    except OSError:
        return False


def _ours(value: Any, launcher: str) -> list[str] | None:
    """The split command if ``value`` is a statusLine pointing at our launcher, else None."""
    if not isinstance(value, dict):
        return None
    args = _split(value.get("command"))
    if not args or len(args) < 2 or args[0] != "sh" or not _is_launcher(args[1], launcher):
        return None
    return args


def plain_command(launcher: str) -> str:
    return f"sh {shlex.quote(launcher)}"


def composed_command(launcher: str, original: str) -> str:
    return f"{plain_command(launcher)} --compose -- {shlex.quote(original)}"


def install_statusline(
    settings_file: Path, launcher: str, env: Mapping[str, str] | None = None
) -> InstallResult:
    env = os.environ if env is None else env
    target = _target(settings_file)
    try:
        data, existed = _load(target)
    except _Invalid as exc:
        return InstallResult(False, "error", str(exc))
    had = KEY in data
    current = data.get(KEY)
    if _ours(current, launcher) is not None:
        cmd = current.get("command") if isinstance(current, dict) else None
        return InstallResult(True, "already", "token-sparen statusline already installed", cmd)
    original_cmd = current.get("command") if isinstance(current, dict) else None
    if (
        isinstance(current, dict)
        and current.get("type") == "command"
        and isinstance(original_cmd, str)
        and original_cmd.strip()
    ):
        command = composed_command(launcher, original_cmd)
        new_value: dict[str, Any] = {**current, "command": command}
        action: Outcome = "composed"
    else:
        command = plain_command(launcher)
        new_value = {"type": "command", "command": command, "padding": 0}
        action = "installed"
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    if existed and not backup.exists():
        shutil.copy2(target, backup)
    record = {
        "had_statusline": had,
        "original": current if had else None,
        "installed_command": command,
        "ts": time.time(),
    }

    def put(meta: dict[str, Any]) -> dict[str, Any]:
        installs = meta.get("installs")
        installs = dict(installs) if isinstance(installs, dict) else {}
        installs[_slot(target)] = record
        return {**meta, "version": 1, "installs": installs}

    jsonstore.update(_meta_path(env), put, {}, env)
    data[KEY] = new_value  # replaces in place (order kept) or appends
    _write(target, data)
    what = "composed with your existing statusline" if action == "composed" else "installed"
    return InstallResult(
        True, action, f"token-sparen statusline {what} in {settings_file}", command
    )


def _forget(env: Mapping[str, str], slot: str) -> None:
    path = _meta_path(env)
    if not path.is_file():
        return

    def drop(meta: dict[str, Any]) -> dict[str, Any]:
        installs = meta.get("installs")
        if isinstance(installs, dict):
            meta["installs"] = {k: v for k, v in installs.items() if k != slot}
        return meta

    jsonstore.update(path, drop, {}, env)


def uninstall_statusline(
    settings_file: Path, env: Mapping[str, str] | None = None, launcher: str | None = None
) -> InstallResult:
    env = os.environ if env is None else env
    target = _target(settings_file)
    try:
        data, _ = _load(target)
    except _Invalid as exc:
        return InstallResult(False, "error", str(exc))
    slot = _slot(target)
    meta = jsonstore.read(_meta_path(env), {}, env)
    installs = meta.get("installs")
    record = installs.get(slot) if isinstance(installs, dict) else None
    current = data.get(KEY)
    recorded_cmd = record.get("installed_command") if isinstance(record, dict) else None
    recorded_args = _split(recorded_cmd) or []
    recorded_launcher = recorded_args[1] if len(recorded_args) > 1 else ""
    args = _ours(current, launcher or recorded_launcher) or _ours(current, recorded_launcher)
    if args is None:
        if isinstance(record, dict):
            _forget(env, slot)
            return InstallResult(
                True, "unchanged", "statusline was changed since install; left as it is"
            )
        return InstallResult(True, "not-installed", "token-sparen statusline is not installed")
    if isinstance(record, dict):
        had = record.get("had_statusline") is True
        original = record.get("original")
    elif len(args) == 5 and args[2:4] == ["--compose", "--"] and isinstance(current, dict):
        had, original = True, {**current, "command": args[4]}  # no record: unwrap the command
    else:
        had, original = False, None
    if had:
        data[KEY] = original
        action: Outcome = "restored"
    else:
        data.pop(KEY, None)
        action = "removed"
    _write(target, data)
    _forget(env, slot)
    what = "original statusline restored" if had else "statusline removed"
    return InstallResult(True, action, f"token-sparen: {what} in {settings_file}")
