"""OAuth token leak scan (Global Constraints; Review Focus 5; Task 5 review ruling I5).

Before anything of a run reaches the ledger, every output (stream, patch, copied-out state and
transcripts) is scanned for the token in raw form, in base64 (standard and URL-safe, all three
alignments) and percent-encoded (upper and lower case), and for the ``sk-ant-oat`` pattern raw
or base64-encoded. The scan fails closed: a path it cannot list or read counts as a hit. Files
are scanned as written by the run, before any packing: compressed archives and git objects are
not decompressed (the backend copies state out unpacked and the scan runs on that copy).

A hit quarantines the run: its files are deleted and a marker records which (redacted relative)
paths held it; the run is ``infra_fail/leak`` and the campaign stops. The token comes from the
bench process environment only and never appears in a message, marker or exception.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
PATTERN = re.compile(rb"sk-ant-oat[0-9A-Za-z_-]{8,}")
OAT_PREFIX = b"sk-ant-oat"
QUARANTINE_MARKER = "quarantined.json"
CHUNK = 1 << 20
_OVERLAP = 512


def token_from_env(environ: Mapping[str, str] | None = None) -> str:
    """The OAuth token from the bench process environment ("" when unset)."""
    env = os.environ if environ is None else environ
    return env.get(TOKEN_ENV, "")


def _b64_forms(value: bytes) -> list[bytes]:
    """The base64 text that encodes ``value`` whatever its offset in the encoded data: for
    each of the 3 alignments, the characters determined by ``value``'s bytes alone."""
    out = []
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        for lead in range(3):
            enc = encode(b"\0" * lead + value)
            start = -(-8 * lead // 6)  # ceil: first char without bits of the lead
            end = 8 * (lead + len(value)) // 6  # floor: last char fully inside value
            out.append(enc[start:end])
    return out


def needles(token: str) -> list[bytes]:
    """Every byte form the scan looks for (empty token: only the ``sk-ant-oat`` forms)."""
    forms = _b64_forms(OAT_PREFIX)
    if token:
        raw = token.encode("utf-8")
        pct = "".join(f"%{b:02X}" for b in raw).encode("ascii")
        forms += [raw, pct, pct.lower(), *_b64_forms(raw)]
    return list(dict.fromkeys(f for f in forms if f))


def _walk(paths: Sequence[Path], unreadable: set[Path]) -> Iterator[Path]:
    for root in paths:
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.add(root)
            continue
        if stat.S_ISREG(mode):
            yield root
        elif stat.S_ISDIR(mode):
            yield from _walk_dir(root, unreadable)


def _walk_dir(directory: Path, unreadable: set[Path]) -> Iterator[Path]:
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        unreadable.add(directory)  # fail closed: its files cannot be cleared
        return
    for entry in entries:
        path = Path(entry.path)
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            unreadable.add(path)
            continue
        if stat.S_ISDIR(mode):
            yield from _walk_dir(path, unreadable)
        elif stat.S_ISREG(mode):
            yield path


def _holds(path: Path, forms: Sequence[bytes]) -> bool:
    keep = max(_OVERLAP, *(len(f) for f in forms)) - 1
    tail = b""
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            window = tail + chunk
            if PATTERN.search(window) or any(f in window for f in forms):
                return True
            tail = window[-keep:]
    return False


def scan_for_token(paths: Sequence[Path], token: str) -> list[Path]:
    """Paths under ``paths`` (links never followed) whose name or content holds the token or an
    ``sk-ant-oat`` token in any form of `needles`, plus every path that could not be listed or
    read (fail closed); sorted."""
    forms = needles(token)
    hits: set[Path] = set()
    for path in _walk(paths, hits):
        name = os.fsencode(path.name)
        try:
            if PATTERN.search(name) or any(f in name for f in forms) or _holds(path, forms):
                hits.add(path)
        except OSError:
            hits.add(path)
    return sorted(hits)


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "[REDACTED]")
    data = PATTERN.sub(b"[REDACTED]", text.encode("utf-8", "surrogateescape"))
    return data.decode("utf-8", "replace")


def _force_remove(path: Path) -> None:
    """Remove ``path`` (links unlinked, never followed); directories we cannot list are made
    accessible first (the run's state may arrive with 0000/0700 modes)."""
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        path.unlink()
        return
    try:
        children = list(path.iterdir())
    except PermissionError:
        os.chmod(path, 0o700)
        children = list(path.iterdir())
    for child in children:
        _force_remove(child)
    try:
        path.rmdir()
    except PermissionError:
        os.chmod(path, 0o700)
        path.rmdir()


def quarantine(run_dir: Path, hits: Sequence[Path], token: str = "") -> Path:
    """Delete everything in ``run_dir`` and write a marker that lists the hit paths relative to
    ``run_dir`` with any token text redacted."""
    names = []
    for hit in hits:
        try:
            rel = hit.relative_to(run_dir).as_posix()
        except ValueError:
            rel = hit.name
        names.append(_redact(rel, token))
    for child in list(run_dir.iterdir()):
        _force_remove(child)
    marker = run_dir / QUARANTINE_MARKER
    marker.write_text(
        json.dumps({"reason": "token leak", "files": sorted(names)}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return marker
