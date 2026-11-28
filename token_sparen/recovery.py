"""Recoverable output store: full text saved on disk, marker points the agent at it."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from token_sparen.config import private_mkdir, state_home
from token_sparen.store.jsonstore import replace_with_retry

_ID_LEN = 12
_FILE_RE = re.compile(r"^([0-9a-f]{12})\.txt$")
_TMP_RE = re.compile(r"^[0-9a-f]{12}\.txt\.tmp-\d+-[0-9a-f]+$")
_TMP_STALE_S = 3600.0
_DAY_S = 86400.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_NAME_SURROGATE = 0x20000000  # reparse-tag bit: the entry names another path (link, junction)


@dataclass(frozen=True)
class RecoveryRef:
    id: str
    path: Path


def _root(project_dir: Path) -> Path:
    return project_dir / ".token-sparen"


def _encode(text: str) -> bytes:
    try:
        return text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:  # lone surrogates outside the escape range
        return text.encode("utf-8", errors="surrogatepass")


def _is_link(st: os.stat_result) -> bool:
    """``st`` (from ``lstat``) is a link of any kind: a symbolic link, or on Windows a junction
    or any other name-surrogate reparse point (``lstat`` there reports a junction as a plain
    directory and ``is_symlink`` is false). A reparse point without a tag is refused too; a
    non-surrogate tag (e.g. a cloud placeholder) is a real file and is allowed."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if not getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    tag = getattr(st, "st_reparse_tag", None)
    return not tag or bool(tag & _NAME_SURROGATE)


def _refuse_links(*dirs: Path) -> None:
    """A repository can commit ``.token-sparen`` (or ``outputs``) as a symbolic link: writing,
    reading or pruning through it would touch files outside the project, so it is refused."""
    for d in dirs:
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        if _is_link(st):
            raise OSError(f"{d} is a link; recovery files are not used through it")


def _real_dirs(*dirs: Path) -> None:
    """Each of ``dirs`` exists and is a real directory (lstat: never a link); else OSError."""
    for d in dirs:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or _is_link(st):
            raise OSError(f"{d} is not a directory (or is a link)")


def _create_once(path: Path, data: str) -> None:
    """Create ``path`` with ``data`` unless any entry (a dangling link included) is there.
    Windows has no ``O_NOFOLLOW`` and ``O_EXCL`` there creates a dangling link's target, so an
    existing entry is checked with ``lstat`` first (a committed link is static; on POSIX
    ``O_EXCL | O_NOFOLLOW`` also closes the race)."""
    try:
        os.lstat(path)
        return
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.encode("utf-8"))


def private_root(project_dir: Path) -> Path:
    """``<project>/.token-sparen`` as a real private directory holding its ``*`` .gitignore
    (created once; an existing entry is left alone). OSError when it is a link. Callers that
    put user data there confirm with git that it is ignored (a repository may commit its own
    ``.gitignore`` there)."""
    root = _root(project_dir)
    _refuse_links(root)
    private_mkdir(root)
    _real_dirs(root)
    with contextlib.suppress(OSError):
        _create_once(root / ".gitignore", "*\n")
    return root


def registry_dir(project_dir: Path, env: Mapping[str, str]) -> Path:
    """The ids this install wrote for this checkout, one empty file per id:
    ``<state root>/recovery/<16 hex>/<id>``, keyed by a hash of the checkout's real path (no raw
    path is stored). One file per id: no lock, a single ``open`` to record, a ``stat`` to check."""
    tag = hashlib.sha256(os.path.realpath(project_dir).encode("utf-8", "surrogatepass"))
    return state_home(env) / "recovery" / tag.hexdigest()[:16]


def _registered(project_dir: Path, rid: str, env: Mapping[str, str]) -> bool:
    return (registry_dir(project_dir, env) / rid).is_file()


def _register(project_dir: Path, rid: str, env: Mapping[str, str]) -> None:
    """Record ``rid``; raises OSError when it cannot (then nothing is elided: fail open)."""
    reg = registry_dir(project_dir, env)
    private_mkdir(reg)
    fd = os.open(reg / rid, os.O_WRONLY | os.O_CREAT | _NOFOLLOW, 0o600)
    os.close(fd)


def _unregister(project_dir: Path, rid: str, env: Mapping[str, str]) -> None:
    with contextlib.suppress(OSError):
        (registry_dir(project_dir, env) / rid).unlink()


def _write_new(out: Path, name: str, data: bytes) -> None:
    """Write ``data`` to a fresh temporary file (``O_EXCL``, ``O_NOFOLLOW``, mode 0600) in the
    verified ``out`` and rename it onto ``name``: a rename replaces a planted link, never
    follows it."""
    tmp = out / f"{name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        replace_with_retry(tmp, out / name)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def save_full(project_dir: Path, text: str, env: Mapping[str, str] | None = None) -> RecoveryRef:
    """Persist ``text`` under its content id and record the id in this install's registry.

    Nothing is ever written, touched or read through a link: ``.token-sparen`` and ``outputs``
    must be real directories, and an existing ``<id>.txt`` must be a regular file (else
    OSError; the hook fails open). An existing file whose id this install wrote is only
    touched; any other one (e.g. committed by the repository) is replaced by the real text."""
    env = os.environ if env is None else env
    data = _encode(text)
    rid = hashlib.sha256(data).hexdigest()[:_ID_LEN]
    root = _root(project_dir)
    out = root / "outputs"
    _refuse_links(root, out)
    private_mkdir(root)
    private_mkdir(out)
    _real_dirs(root, out)  # re-checked: a link may have been planted meanwhile
    with contextlib.suppress(OSError):
        _create_once(root / ".gitignore", "*\n")
    name = f"{rid}.txt"
    path = out / name
    try:
        st: os.stat_result | None = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and (not stat.S_ISREG(st.st_mode) or _is_link(st)):
        raise OSError(f"{path} is not a regular file (a link?); nothing was saved")
    if st is not None and _registered(project_dir, rid, env):
        if os.utime in os.supports_follow_symlinks:
            os.utime(path, follow_symlinks=False)
        else:  # pragma: no cover - Windows: lstat above showed a regular file
            os.utime(path)
    else:
        _write_new(out, name, data)
        _register(project_dir, rid, env)
    return RecoveryRef(id=rid, path=out.resolve() / name)


def read_full(project_dir: Path, rid: str, env: Mapping[str, str] | None = None) -> bytes | None:
    """The saved full output ``rid`` of this checkout, or None: only ids this install wrote,
    only a regular file in real directories, opened without following links."""
    env = os.environ if env is None else env
    if not re.fullmatch(r"[0-9a-f]{12}", rid):
        return None
    root = _root(project_dir)
    out = root / "outputs"
    try:
        _real_dirs(root, out)
        if not _registered(project_dir, rid, env):
            return None
        path = out / f"{rid}.txt"
        # Windows has no O_NOFOLLOW: the open below would follow a link, so lstat first (the
        # threat is a link committed by the repository, which is there before we look).
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or _is_link(st):
            return None
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            return fh.read()
    except OSError:
        return None


def marker(kept_lines: int, total_lines: int, ref: RecoveryRef) -> str:
    """The elision marker; the path's directories are resolved, never the file itself (a link
    there must not point the agent elsewhere)."""
    where = ref.path.parent.resolve() / ref.path.name
    return (
        f"[token-sparen: kept {kept_lines} of {total_lines} lines · "
        f"full output: {where} · `token-sparen show {ref.id}`]"
    )


def prune_outputs(
    project_dir: Path,
    ttl_days: float = 14,
    max_files: int = 200,
    max_bytes: int = 100_000_000,
    env: Mapping[str, str] | None = None,
) -> None:
    """Delete expired files, then oldest-first (LRU by mtime) until count and size fit. Also
    deletes temporary files older than an hour (a write the hook watchdog cut short)."""
    out = _root(project_dir) / "outputs"
    entries: list[tuple[float, int, Path]] = []
    now = time.time()
    try:
        _refuse_links(out.parent, out)
        for p in out.iterdir():
            tmp = _TMP_RE.match(p.name) is not None
            if not tmp and not _FILE_RE.match(p.name):
                continue
            try:
                st = p.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or _is_link(st):
                continue  # a link or anything else: never followed
            if tmp:
                if st.st_mtime < now - _TMP_STALE_S:
                    with contextlib.suppress(OSError):
                        p.unlink()
                continue
            entries.append((st.st_mtime, st.st_size, p))
    except OSError:
        return
    cutoff = now - ttl_days * _DAY_S
    entries.sort(key=lambda e: e[0], reverse=True)  # newest first
    total = kept = 0
    env = os.environ if env is None else env
    for mtime, size, p in entries:
        if mtime < cutoff or kept >= max_files or total + size > max_bytes:
            with contextlib.suppress(OSError):
                p.unlink()
                _unregister(project_dir, p.name[:_ID_LEN], env)
            continue
        total += size
        kept += 1


def is_recovery_read(project_dir: Path, read_path: Path) -> str | None:
    """Return the id when ``read_path`` resolves to one of this project's recovery files."""
    try:
        resolved = Path(read_path).resolve()
        out = (_root(project_dir) / "outputs").resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != out:
        return None
    m = _FILE_RE.match(resolved.name)
    return m.group(1) if m else None
