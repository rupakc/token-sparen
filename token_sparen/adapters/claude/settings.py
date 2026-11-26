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


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not strict JSON")


def _load(path: Path) -> tuple[dict[str, Any], bool]:
    """(settings, existed); raises _Invalid for anything but a strict JSON object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    except (OSError, UnicodeDecodeError) as exc:
        raise _Invalid(f"{path} cannot be read ({exc}); nothing was changed") from exc
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise _Invalid(f"{path} is not valid JSON ({exc}); nothing was changed") from exc
    if not isinstance(data, dict):
        raise _Invalid(f"{path} is not valid JSON settings (not an object); nothing was changed")
    return data, True


Stamp = tuple[int, int, int] | None


def _stamp(path: Path) -> Stamp:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


class _Changed(RuntimeError):
    """The settings file changed between our read and the replace."""


def _before_replace(path: Path) -> None:
    """Test seam: runs after the temp file is written, just before the stamp check."""


def _write(path: Path, data: Mapping[str, Any], expected: Stamp | bool = False) -> None:
    """Atomic strict-JSON write keeping the file mode.

    With ``expected`` (a stamp taken when the file was read), the file must be unchanged just
    before the replace, else ``_Changed`` is raised and nothing is written.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: int | None = path.stat().st_mode & 0o7777
    except OSError:
        mode = None
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        if expected is not False:
            _before_replace(path)
            if _stamp(path) != expected:
                raise _Changed(str(path))
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


def _plan_install(
    data: dict[str, Any], launcher: str
) -> tuple[Outcome, str, dict[str, Any] | None]:
    """(action, command, new statusLine value or None when already installed)."""
    current = data.get(KEY)
    if _ours(current, launcher) is not None:
        cmd = current.get("command") if isinstance(current, dict) else None
        return "already", cmd if isinstance(cmd, str) else "", None
    original_cmd = current.get("command") if isinstance(current, dict) else None
    if (
        isinstance(current, dict)
        and current.get("type") == "command"
        and isinstance(original_cmd, str)
        and original_cmd.strip()
    ):
        command = composed_command(launcher, original_cmd)
        return "composed", command, {**current, "command": command}
    command = plain_command(launcher)
    return "installed", command, {"type": "command", "command": command, "padding": 0}


def _set_record(env: Mapping[str, str], slot: str, record: Any) -> Any:
    """Store (or with None remove) the install record; returns the previous one."""
    previous: list[Any] = [None]

    def put(meta: dict[str, Any]) -> dict[str, Any]:
        installs = meta.get("installs")
        installs = dict(installs) if isinstance(installs, dict) else {}
        previous[0] = installs.pop(slot, None)
        if record is not None:
            installs[slot] = record
        return {**meta, "version": 1, "installs": installs}

    path = _meta_path(env)
    if record is None and not path.is_file():
        return None
    jsonstore.update(path, put, {}, env)
    return previous[0]


def _failure(exc: BaseException) -> InstallResult:
    if isinstance(exc, _Changed):
        reason = f"{exc} kept changing while we wrote it; nothing was changed, try again"
    else:
        reason = f"{type(exc).__name__}: {exc}"
    return InstallResult(False, "error", f"token-sparen: {reason}")


def install_statusline(
    settings_file: Path, launcher: str, env: Mapping[str, str] | None = None
) -> InstallResult:
    env = os.environ if env is None else env
    target = _target(settings_file)
    slot = _slot(target)
    for attempt in (0, 1):
        stamp = _stamp(target)
        try:
            data, existed = _load(target)
        except _Invalid as exc:
            return InstallResult(False, "error", str(exc))
        action, command, new_value = _plan_install(data, launcher)
        if new_value is None:
            return InstallResult(True, action, "token-sparen statusline already installed", command)
        had = KEY in data
        record = {
            "had_statusline": had,
            "original": data.get(KEY) if had else None,
            "installed_command": command,
            "ts": time.time(),
        }
        recorded = False
        previous: Any = None
        try:
            backup = target.with_name(target.name + BACKUP_SUFFIX)
            if existed and not backup.exists():
                shutil.copy2(target, backup)
            previous = _set_record(env, slot, record)
            recorded = True
            data[KEY] = new_value  # replaces in place (order kept) or appends
            _write(target, data, stamp)
        except (OSError, jsonstore.StoreBusy, ValueError, _Changed) as exc:
            if recorded:
                with contextlib.suppress(OSError, jsonstore.StoreBusy):
                    _set_record(env, slot, previous)  # roll the record back
            if isinstance(exc, _Changed) and attempt == 0:
                continue  # re-read and re-apply once
            return _failure(exc)
        what = "composed with your existing statusline" if action == "composed" else "installed"
        return InstallResult(
            True, action, f"token-sparen statusline {what} in {settings_file}", command
        )
    raise AssertionError("unreachable")  # pragma: no cover


def _restore_value(current: Any, args: list[str], record: Any) -> tuple[bool, Any]:
    """(had a statusLine before install, value to restore)."""
    composed = len(args) == 5 and args[2:4] == ["--compose", "--"] and isinstance(current, dict)
    if isinstance(record, dict):
        command = current.get("command") if isinstance(current, dict) else None
        if composed and command != record.get("installed_command"):
            return True, {**current, "command": args[4]}  # the user edited inside our wrapper
        return record.get("had_statusline") is True, record.get("original")
    if composed:
        return True, {**current, "command": args[4]}  # no record: unwrap the command
    return False, None


def uninstall_statusline(
    settings_file: Path, env: Mapping[str, str] | None = None, launcher: str | None = None
) -> InstallResult:
    env = os.environ if env is None else env
    target = _target(settings_file)
    slot = _slot(target)
    for attempt in (0, 1):
        stamp = _stamp(target)
        try:
            data, _ = _load(target)
            meta = jsonstore.read(_meta_path(env), {}, env)
        except _Invalid as exc:
            return InstallResult(False, "error", str(exc))
        except OSError as exc:
            return _failure(exc)
        installs = meta.get("installs")
        record = installs.get(slot) if isinstance(installs, dict) else None
        current = data.get(KEY)
        recorded_cmd = record.get("installed_command") if isinstance(record, dict) else None
        recorded_args = _split(recorded_cmd) or []
        recorded_launcher = recorded_args[1] if len(recorded_args) > 1 else ""
        args = _ours(current, launcher or recorded_launcher) or _ours(current, recorded_launcher)
        try:
            if args is None:
                if isinstance(record, dict):
                    _set_record(env, slot, None)
                    return InstallResult(
                        True, "unchanged", "statusline was changed since install; left as it is"
                    )
                return InstallResult(
                    True, "not-installed", "token-sparen statusline is not installed"
                )
            had, original = _restore_value(current, args, record)
            if had:
                data[KEY] = original
            else:
                data.pop(KEY, None)
            _write(target, data, stamp)
            _set_record(env, slot, None)
        except (OSError, jsonstore.StoreBusy, ValueError, _Changed) as exc:
            if isinstance(exc, _Changed) and attempt == 0:
                continue
            return _failure(exc)
        action: Outcome = "restored" if had else "removed"
        what = "original statusline restored" if had else "statusline removed"
        return InstallResult(True, action, f"token-sparen: {what} in {settings_file}")
    raise AssertionError("unreachable")  # pragma: no cover
