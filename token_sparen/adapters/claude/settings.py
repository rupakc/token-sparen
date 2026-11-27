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
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from token_sparen.config import state_home
from token_sparen.mechanisms import prune
from token_sparen.mechanisms.prune import WriteResult
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


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Strict: a duplicated key is refused (which value would win is parser-dependent)."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _digest(raw: bytes) -> bytes:
    return hashlib.blake2b(raw, digest_size=16).digest()


def _stamp_of(st: os.stat_result, raw: bytes) -> Stamp:
    return st.st_mtime_ns, st.st_size, st.st_ino, _digest(raw)


def _read_stamped(path: Path) -> tuple[bytes, Stamp] | None:
    """(bytes, stamp) from one stat plus one read, None if absent. The stat comes first so a
    change in between shows up as a stamp mismatch (the digest covers the bytes actually read)."""
    try:
        st = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return raw, _stamp_of(st, raw)


def _load_stamped(path: Path) -> tuple[dict[str, Any], bool, Stamp]:
    """(settings, existed, stamp of the exact bytes parsed); raises _Invalid for anything but a
    strict JSON object."""
    try:
        read = _read_stamped(path)
        text = None if read is None else read[0].decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Invalid(f"{path} cannot be read ({exc}); nothing was changed") from exc
    if read is None or text is None:
        return {}, False, None
    try:
        data = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise _Invalid(f"{path} is not valid JSON ({exc}); nothing was changed") from exc
    if not isinstance(data, dict):
        raise _Invalid(f"{path} is not valid JSON settings (not an object); nothing was changed")
    return data, True, read[1]


def _load(path: Path) -> tuple[dict[str, Any], bool]:
    """(settings, existed); raises _Invalid for anything but a strict JSON object."""
    data, existed, _ = _load_stamped(path)
    return data, existed


Stamp = tuple[int, int, int, bytes] | None


def _stamp(path: Path) -> Stamp:
    """Current stamp of ``path`` (mtime, size, inode and a content digest), None if absent.
    The digest catches a same-size edit inside the filesystem's mtime resolution."""
    try:
        read = _read_stamped(path)
    except OSError:
        return (-1, -1, -1, b"")  # unreadable now: never equals a good stamp
    return None if read is None else read[1]


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
        try:
            data, existed, stamp = _load_stamped(target)
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
        try:
            data, _, stamp = _load_stamped(target)
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


# --------------------------------------------------------------------------- tool pruning
# Deny rules in the project's ``.claude/settings.local.json`` (``mechanisms.prune``). The same
# rules as the statusline write: strict JSON object or refuse, one pristine backup
# (``settings.local.json.token-sparen.bak``), atomic replace after a stamp re-check (one re-read
# and retry), symlinks followed. Only ``permissions.deny`` entries are added or removed; every
# other key keeps its value and order. The file is written only when git positively confirms it
# is untracked and ignored; any other answer (tracked, not ignored, not a work tree, git missing,
# failing, slow or pointed elsewhere by GIT_DIR-style variables) means "exposed": never written.
LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
GIT_TIMEOUT_S = 2.0
# variables that point git at another repository or index: its answer would not be about ours
GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def local_settings_path(project_dir: Path) -> Path:
    return project_dir / LOCAL_SETTINGS


def _git(project_dir: Path, *args: str) -> tuple[int, bytes] | None:
    """(return code, stdout) of a git command in ``project_dir``; None when git is unavailable
    or too slow."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout


def git_exposure(project_dir: Path) -> str | None:
    """None only on positive confirmation (``ls-files`` exits 0 with no output and
    ``check-ignore`` exits 0); else ``"tracked"``, ``"unignored"`` (untracked, check-ignore exit
    1) or ``"unknown"`` (anything else). Only None may be written."""
    if any(os.environ.get(k) for k in GIT_LOCATION_VARS):
        return "unknown"
    rel = LOCAL_SETTINGS.as_posix()
    listed = _git(project_dir, "ls-files", "-z", "--", rel)
    if listed is None or listed[0] != 0:
        return "unknown"
    if listed[1].strip(b"\0\r\n "):
        return "tracked"
    ignored = _git(project_dir, "check-ignore", "-q", "--", rel)
    if ignored is None:
        return "unknown"
    if ignored[0] == 0:
        return None
    return "unignored" if ignored[0] == 1 else "unknown"


def _deny_list(data: dict[str, Any]) -> list[Any]:
    perms = data.get("permissions", {})
    if not isinstance(perms, dict):
        raise _Invalid("permissions is not an object; nothing was changed")
    deny = perms.get("deny", [])
    if not isinstance(deny, list):
        raise _Invalid("permissions.deny is not a list; nothing was changed")
    return deny


def _prune_failure(exc: BaseException) -> WriteResult:
    if isinstance(exc, _Changed):
        return WriteResult(False, message=f"{exc} kept changing while we wrote it")
    return WriteResult(False, message=f"{type(exc).__name__}: {exc}")


def add_deny_rules(
    path: Path,
    rules: Sequence[str],
    fresh_backup: bool = False,
    journal: Callable[[list[str], dict[str, bool]], None] | None = None,
) -> WriteResult:
    """Append the bare ``rules`` not yet in ``permissions.deny`` (a ``prune.WriteResult``).
    ``fresh_backup`` (no rule of ours is in the file): the backup is replaced by the file as it
    is now, so a backup left by an earlier cycle never holds an older state than the user's.
    ``journal(new, created)`` runs just before the write (the caller records its intent).
    A symbolic link (the file or its folder) is refused: git's answer is about the link, while
    the write would land in the target, possibly a tracked file in another repository."""
    if _linked(path):
        return WriteResult(
            False, message=f"{path} is a symbolic link (or inside one); nothing was changed"
        )
    target = _target(path)
    for attempt in (0, 1):
        try:
            data, existed, stamp = _load_stamped(target)
            deny = _deny_list(data)
        except _Invalid as exc:
            return WriteResult(False, message=str(exc))
        present = [r for r in rules if r in deny]
        new = [r for r in dict.fromkeys(rules) if r not in deny]
        if not new:
            return WriteResult(True, [], present)
        created = {
            "dir": not target.parent.exists(),
            "file": not existed,
            "permissions": "permissions" not in data,
            "deny": not isinstance(data.get("permissions"), dict)
            or "deny" not in data["permissions"],
        }
        try:
            if journal is not None:
                journal(new, created)
            backup = target.with_name(target.name + BACKUP_SUFFIX)
            if fresh_backup and not existed:
                with contextlib.suppress(FileNotFoundError):
                    backup.unlink()  # stale: there is no file to back up now
            if existed and (fresh_backup or not backup.exists()):
                shutil.copy2(target, backup)
            perms = data.setdefault("permissions", {})
            perms["deny"] = [*deny, *new]
            _write(target, data, stamp)
        except (OSError, ValueError, _Changed) as exc:
            if isinstance(exc, _Changed) and attempt == 0:
                continue
            return _prune_failure(exc)
        return WriteResult(True, new, present, created)
    raise AssertionError("unreachable")  # pragma: no cover


def _linked(path: Path) -> bool:
    """``path`` or its parent folder is a symbolic link (lstat; a missing path is not)."""
    return any(p.is_symlink() for p in (path, path.parent))


def remove_deny_rules(
    path: Path,
    rules: Sequence[str],
    created: Mapping[str, bool],
    cleanup: bool,
) -> WriteResult:
    """Remove one occurrence (the last) of each rule; with ``cleanup`` (no rule of ours left)
    also remove the containers we created, the file and directory if we created them and they
    are empty, and our backup when it equals the file it backs up."""
    target = _target(path)
    for attempt in (0, 1):
        try:
            data, existed, stamp = _load_stamped(target)
            deny = list(_deny_list(data))
        except _Invalid as exc:
            return WriteResult(False, message=str(exc))
        if not existed:
            return WriteResult(True, [], list(rules))
        removed: list[str] = []
        for rule in rules:
            for i in range(len(deny) - 1, -1, -1):
                if deny[i] == rule:
                    del deny[i]
                    removed.append(rule)
                    break
        missing = [r for r in rules if r not in removed]
        if not removed and not cleanup:
            return WriteResult(True, [], missing)
        perms = data.get("permissions")
        if isinstance(perms, dict):
            perms["deny"] = deny
            if cleanup and created.get("deny") and not deny:
                del perms["deny"]
            if cleanup and created.get("permissions") and not perms:
                del data["permissions"]
        try:
            if cleanup and created.get("file") and not data:
                if _stamp(target) != stamp:
                    raise _Changed(str(target))
                target.unlink()
                if created.get("dir"):
                    with contextlib.suppress(OSError):
                        target.parent.rmdir()  # only when empty
            else:
                _write(target, data, stamp)
                if cleanup:
                    _drop_backup(target, data)
        except (OSError, ValueError, _Changed) as exc:
            if isinstance(exc, _Changed) and attempt == 0:
                continue
            return _prune_failure(exc)
        return WriteResult(True, removed, missing)
    raise AssertionError("unreachable")  # pragma: no cover


def _drop_backup(target: Path, data: Mapping[str, Any]) -> None:
    """Our backup is redundant once the file holds exactly its content again."""
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    try:
        if json.loads(backup.read_text(encoding="utf-8")) == data:
            backup.unlink()
    except (OSError, ValueError):
        return


def deny_rules(path: Path) -> set[str] | None:
    try:
        data, _ = _load(_target(path))
        return {r for r in _deny_list(data) if isinstance(r, str)}
    except _Invalid:
        return None


class ClaudeDenyWriter:
    """``mechanisms.prune.DenyWriter`` for Claude Code."""

    def settings_file(self, project_dir: Path) -> Path:
        return local_settings_path(project_dir)

    def exposure(self, project_dir: Path) -> str | None:
        return git_exposure(project_dir)

    def add(
        self,
        project_dir: Path,
        rules: Sequence[str],
        fresh_backup: bool = False,
        journal: Callable[[list[str], dict[str, bool]], None] | None = None,
    ) -> WriteResult:
        return add_deny_rules(local_settings_path(project_dir), list(rules), fresh_backup, journal)

    def remove(
        self, project_dir: Path, rules: Sequence[str], created: Mapping[str, bool], cleanup: bool
    ) -> WriteResult:
        return remove_deny_rules(local_settings_path(project_dir), list(rules), created, cleanup)

    def present(self, project_dir: Path) -> set[str] | None:
        return deny_rules(local_settings_path(project_dir))

    def history_dirs(self, project_dir: Path, env: Mapping[str, str]) -> list[Path]:
        return transcript_dirs(project_dir, env)


def transcript_dirs(project_dir: Path, env: Mapping[str, str]) -> list[Path]:
    """Claude Code keeps a project's transcripts in ``<config dir>/projects/<cwd with every
    non-alphanumeric character replaced by ->``; both the given and the real path are tried."""
    config_dir = settings_path(env).parent
    out: list[Path] = []
    for p in dict.fromkeys([str(project_dir), str(project_dir.resolve())]):
        d = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", p)
        if d.is_dir() and d not in out:
            out.append(d)
    return out


prune.register_writer(ClaudeDenyWriter())
