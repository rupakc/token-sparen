"""Cross-process locked, atomic JSON documents."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, Any

from token_sparen.config import log_error

LOCK_TIMEOUT_S = 2.0
_POLL_S = 0.005
_REPLACE_TRIES = 5


class StoreBusy(RuntimeError):
    """The lock could not be taken within LOCK_TIMEOUT_S."""


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
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock on ``path.with_suffix('.lock')``; StoreBusy after 2 s."""
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise StoreBusy(str(lock_path))
            time.sleep(_POLL_S)
        try:
            yield
        finally:
            _unlock(fh)


def read(
    path: Path, default: dict[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read a JSON object; corrupt content is quarantined and ``default`` returned."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(default)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
    except ValueError as exc:
        _quarantine(path, str(exc), env)
        return dict(default)
    return data


def _quarantine(path: Path, reason: str, env: Mapping[str, str] | None) -> None:
    dest = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    with contextlib.suppress(OSError):
        os.replace(path, dest)
    log_error(os.environ if env is None else env, "jsonstore", f"{path}: corrupt ({reason})")


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a reader may briefly hold the target
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def update(
    path: Path,
    fn: Callable[[dict[str, Any]], dict[str, Any]],
    default: dict[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read-modify-write under an exclusive cross-process lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        new = fn(read(path, default, env))
        _write_atomic(path, new)
        return new
