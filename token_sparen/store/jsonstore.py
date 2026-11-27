"""Cross-process locked, atomic JSON documents."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import random
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, Any

from token_sparen.config import log_error, private_mkdir
from token_sparen.store import schema

LOCK_TIMEOUT_S = 2.0
_POLL_S = 0.001  # first lock retry; doubles per retry up to _POLL_MAX_S
_POLL_MAX_S = 0.005


def _jitter() -> float:
    """Full jitter in (0, 1]."""
    return 1.0 - random.random()  # noqa: S311 - scheduling, not crypto


_REPLACE_TRIES = 5


class StoreBusy(RuntimeError):
    """The lock could not be taken within LOCK_TIMEOUT_S."""


class NewerSchema(OSError):
    """The document was written by a newer token-sparen (a newer schema version): this code
    must not rewrite it. An OSError, so callers that skip unwritable state skip it too."""


def _try_lock(fh: IO[bytes]) -> bool:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        fh.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, timeout: float | None = None) -> Iterator[None]:
    """Exclusive lock on ``<name>.lock``; StoreBusy after ``timeout`` (default 2 s)."""
    lock_path = path.with_name(path.name + ".lock")
    private_mkdir(lock_path.parent)
    with lock_path.open("a+b") as fh:
        deadline = time.monotonic() + (LOCK_TIMEOUT_S if timeout is None else timeout)
        delay = _POLL_S
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise StoreBusy(str(lock_path))
            time.sleep(delay * _jitter())  # jitter: waiters that queued together spread out
            delay = min(delay * 2, _POLL_MAX_S)
        try:
            yield
        finally:
            _unlock(fh)


def _load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (data, None), (None, None) when missing, or (None, reason) when corrupt."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError included: a non-UTF-8 file is corrupt
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "top-level JSON value is not an object"
    return data, None


def read(
    path: Path, default: dict[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Lock-free read. Corrupt content yields ``default`` and a log line, never a rename."""
    data, bad = _load(path)
    if bad is not None:
        log_error(os.environ if env is None else env, "jsonstore", f"{path}: corrupt ({bad})")
    return copy.deepcopy(default) if data is None else data


def _quarantine(path: Path, reason: str, env: Mapping[str, str] | None) -> None:
    """Move a corrupt file aside under a unique name; call only while holding the lock."""
    stem = f"{path.name}.corrupt-{time.time_ns()}-{os.getpid()}"
    dest = path.with_name(f"{stem}-{secrets.token_hex(4)}")
    while dest.exists():  # never overwrite an earlier forensic copy
        dest = path.with_name(f"{stem}-{secrets.token_hex(4)}")
    with contextlib.suppress(OSError):
        os.replace(path, dest)
    log_error(os.environ if env is None else env, "jsonstore", f"{path}: corrupt ({reason})")


def replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace, retrying transient Windows PermissionError."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:  # Windows: a reader may briefly hold the target
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        replace_with_retry(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def update(
    path: Path,
    fn: Callable[[dict[str, Any]], dict[str, Any]],
    default: dict[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Read-modify-write under an exclusive cross-process lock."""
    private_mkdir(path.parent)
    with locked(path, timeout):
        data = read_locked(path, default, env)
        new = fn(data)
        _write_atomic(path, new)
        return new


def read_locked(
    path: Path, default: dict[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read for a read-modify-write; call only while holding ``locked(path)``. Corrupt content
    is quarantined (moved aside) and ``default`` returned. A document written by a newer
    token-sparen (``schema.newer_version``) is refused with ``NewerSchema``: never rewritten."""
    data, bad = _load(path)
    if bad is not None:
        _quarantine(path, bad, env)
    refuse_newer(path, data, env)
    return copy.deepcopy(default) if data is None else data


def refuse_newer(path: Path, doc: Any, env: Mapping[str, str] | None = None) -> None:
    """Raise ``NewerSchema`` (logged) when ``doc``, read from ``path``, has a newer version."""
    version = schema.newer_version(path.name, doc)
    if version is not None:
        exc = NewerSchema(
            f"{path}: version {version} is newer than this token-sparen knows "
            f"({schema.KNOWN_VERSIONS[path.name]}); left untouched"
        )
        log_error(os.environ if env is None else env, "jsonstore", str(exc))
        raise exc


def write_locked(path: Path, data: dict[str, Any]) -> None:
    """Atomic write; call only while holding ``locked(path)``."""
    private_mkdir(path.parent)
    _write_atomic(path, data)
