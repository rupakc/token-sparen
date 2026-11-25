"""Recoverable output store: full text saved on disk, marker points the agent at it."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from token_sparen.store.jsonstore import replace_with_retry

_ID_LEN = 12
_FILE_RE = re.compile(r"^([0-9a-f]{12})\.txt$")
_DAY_S = 86400.0


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


def save_full(project_dir: Path, text: str) -> RecoveryRef:
    """Persist ``text`` under its content id; identical content is touched, not rewritten."""
    data = _encode(text)
    rid = hashlib.sha256(data).hexdigest()[:_ID_LEN]
    root = _root(project_dir)
    out = root / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        with contextlib.suppress(OSError):
            gitignore.write_text("*\n", encoding="utf-8")
    path = (out / f"{rid}.txt").resolve()
    try:
        os.utime(path)
    except FileNotFoundError:
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(data)
            replace_with_retry(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    return RecoveryRef(id=rid, path=path)


def marker(kept_lines: int, total_lines: int, ref: RecoveryRef) -> str:
    return (
        f"[token-sparen: kept {kept_lines} of {total_lines} lines · "
        f"full output: {ref.path.resolve()} · `token-sparen show {ref.id}`]"
    )


def prune_outputs(
    project_dir: Path,
    ttl_days: float = 14,
    max_files: int = 200,
    max_bytes: int = 100_000_000,
) -> None:
    """Delete expired files, then oldest-first (LRU by mtime) until count and size fit."""
    out = _root(project_dir) / "outputs"
    entries: list[tuple[float, int, Path]] = []
    try:
        for p in out.iterdir():
            if not _FILE_RE.match(p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_file():
                entries.append((st.st_mtime, st.st_size, p))
    except OSError:
        return
    cutoff = time.time() - ttl_days * _DAY_S
    entries.sort(key=lambda e: e[0], reverse=True)  # newest first
    total = 0
    for i, (mtime, size, p) in enumerate(entries):
        total += size
        if mtime < cutoff or i >= max_files or total > max_bytes:
            total -= size
            with contextlib.suppress(OSError):
                p.unlink()


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
