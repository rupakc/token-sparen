"""OAuth token leak scan (Global Constraints; Review Focus 5).

Before anything of a run reaches the ledger, every output file (stream, patch, copied-out state
and transcripts) is scanned for the exact token value and for the ``sk-ant-oat`` pattern. A hit
quarantines the run: its files are deleted and a marker records which (redacted relative) paths
held it; the run is ``infra_fail/leak`` and the campaign stops. The token comes from the bench
process environment only and never appears in a message, marker or exception.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
PATTERN = re.compile(rb"sk-ant-oat[0-9A-Za-z_-]{8,}")
QUARANTINE_MARKER = "quarantined.json"
CHUNK = 1 << 20
_OVERLAP = 512


def token_from_env(environ: Mapping[str, str] | None = None) -> str:
    """The OAuth token from the bench process environment ("" when unset)."""
    env = os.environ if environ is None else environ
    return env.get(TOKEN_ENV, "")


def _files(paths: Sequence[Path]) -> Iterator[Path]:
    for root in paths:
        try:
            mode = root.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            yield root
        elif stat.S_ISDIR(mode):
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                here = Path(dirpath)
                dirnames[:] = [d for d in dirnames if stat.S_ISDIR((here / d).lstat().st_mode)]
                for name in filenames:
                    if stat.S_ISREG((here / name).lstat().st_mode):
                        yield here / name


def _holds(path: Path, needle: bytes | None) -> bool:
    keep = max(_OVERLAP, len(needle or b"")) - 1
    tail = b""
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            window = tail + chunk
            if (needle and needle in window) or PATTERN.search(window):
                return True
            tail = window[-keep:]
    return False


def scan_for_token(paths: Sequence[Path], token: str) -> list[Path]:
    """Regular files under ``paths`` (links not followed) whose name or content holds ``token``
    (exact value; skipped when empty) or the ``sk-ant-oat`` pattern; sorted."""
    needle = token.encode("utf-8") if token else None
    hits: set[Path] = set()
    for path in _files(paths):
        name = os.fsencode(path.name)
        try:
            if (needle and needle in name) or PATTERN.search(name) or _holds(path, needle):
                hits.add(path)
        except OSError:
            hits.add(path)  # unreadable output cannot be cleared: treat as a hit
    return sorted(hits)


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "[REDACTED]")
    return PATTERN.sub(b"[REDACTED]", text.encode("utf-8", "surrogateescape")).decode(
        "utf-8", "replace"
    )


def quarantine(run_dir: Path, hits: Sequence[Path], token: str = "") -> Path:
    """Delete everything in ``run_dir`` (links removed, never followed) and write a marker that
    lists the hit files relative to ``run_dir`` with any token text redacted."""
    names = []
    for hit in hits:
        try:
            rel = hit.relative_to(run_dir).as_posix()
        except ValueError:
            rel = hit.name
        names.append(_redact(rel, token))
    for child in list(run_dir.iterdir()):
        mode = child.lstat().st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(child)
        else:
            child.unlink()
    marker = run_dir / QUARANTINE_MARKER
    marker.write_text(
        json.dumps({"reason": "token leak", "files": sorted(names)}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return marker
