"""Fail if any tracked text file (or, with ``--path``, any file of a generated tree) contains a
deny-listed pattern.

Built-in patterns flag local home-directory paths. Private words are read at runtime from the env
var ``TOKEN_SPAREN_DENYLIST`` (comma-separated, case-insensitive) and are never stored in the
repository.

Modes:

* no ``--path``: every git-tracked or untracked-but-not-ignored file of the repository
  (``.superpowers/`` excluded); a file that is not UTF-8 is skipped.
* ``--path P`` (repeatable): every regular file under ``P`` recursively (or ``P`` itself), without
  asking git; symbolic links and ``.git`` directories are not followed. A file holding a NUL byte
  is binary and skipped, other bytes are decoded leniently. File names are scanned too. Zip
  (``.zip``, ``.whl``) and tar (``.tar``, ``.tar.gz``, ``.tgz``) archives are opened and their
  members scanned the same way; an archive that cannot be read counts as a hit (fail closed).
* ``--require-private-words``: exit 2 when ``TOKEN_SPAREN_DENYLIST`` holds no word, so a
  publishing job whose secret is missing fails instead of checking only the built-in patterns.

Only locations are printed, never the matched text.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_PATTERNS = (r"/Users/[A-Za-z0-9._-]+/", r"C:\\Users\\[^\\]+\\")
EXCLUDED_PREFIXES = (".superpowers/",)
ZIP_SUFFIXES = (".zip", ".whl")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
EMPTY_MESSAGE = "TOKEN_SPAREN_DENYLIST is empty: refusing to check only the built-in patterns"


def private_words(env_value: str) -> list[str]:
    return [w.strip() for w in env_value.split(",") if w.strip()]


def build_patterns(env_value: str) -> list[re.Pattern[str]]:
    patterns = [re.compile(p) for p in BUILTIN_PATTERNS]
    for word in private_words(env_value):
        patterns.append(re.compile(re.escape(word), re.IGNORECASE))
    return patterns


def scan_text(text: str, patterns: Sequence[re.Pattern[str]]) -> list[int]:
    """Return 1-based line numbers containing any pattern hit."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if any(p.search(line) for p in patterns)
    ]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f and not f.startswith(EXCLUDED_PREFIXES)]


def _repo_hits(patterns: Sequence[re.Pattern[str]]) -> Iterator[str]:
    for rel in tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        # review m-R4: a text file that is not UTF-8 is scanned too; only NUL marks binary
        yield from _content_hits(rel, path.read_bytes(), patterns)


def _named(name: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(p.search(name) for p in patterns)


def _content_hits(shown: str, data: bytes, patterns: Sequence[re.Pattern[str]]) -> Iterator[str]:
    """Line hits in one file's bytes; a NUL byte marks a binary file (skipped)."""
    if b"\0" in data:
        return
    for line_no in scan_text(data.decode("utf-8", errors="replace"), patterns):
        yield f"{shown}:{line_no}"


def _archive_kind(name: str) -> str | None:
    low = name.lower()
    if low.endswith(ZIP_SUFFIXES):
        return "zip"
    if low.endswith(TAR_SUFFIXES):
        return "tar"
    return None


def _members(path: Path, kind: str) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    if kind == "zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    members.append((info.filename, zf.read(info)))
        return members
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            fh = tf.extractfile(member) if member.isfile() else None
            if fh is not None:
                members.append((member.name, fh.read()))
    return members


def _file_hits(
    path: Path, shown: str, rel: str, patterns: Sequence[re.Pattern[str]]
) -> Iterator[str]:
    """Hits in one regular file: its name relative to the scanned tree (``rel``; the tree's own
    location is the caller's choice), its content or, for an archive, every member's name and
    content. An archive that cannot be read is a hit (fail closed)."""
    if _named(rel, patterns):
        yield f"{shown} (file name)"
    kind = _archive_kind(path.name)
    if kind is None:
        yield from _content_hits(shown, path.read_bytes(), patterns)
        return
    try:
        members = _members(path, kind)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, ValueError):
        yield f"{shown} (archive could not be read)"
        return
    for name, data in members:
        if _named(name, patterns):
            yield f"{shown}!{name} (file name)"
        yield from _content_hits(f"{shown}!{name}", data, patterns)


def _tree_hits(top: Path, patterns: Sequence[re.Pattern[str]]) -> Iterator[str]:
    """Every regular file under ``top`` (or ``top`` itself); symbolic links and ``.git``
    directories are not followed."""
    if top.is_file():
        yield from _file_hits(top, str(top), top.name, patterns)
        return
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        here = Path(dirpath)
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(top).as_posix()
            yield from _file_hits(path, f"{top.as_posix()}/{rel}", rel, patterns)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        type=Path,
        help="scan every file under this path (repeatable) instead of the repository",
    )
    parser.add_argument(
        "--require-private-words",
        action="store_true",
        help="exit 2 when TOKEN_SPAREN_DENYLIST holds no word (fail closed)",
    )
    return parser.parse_args(list(argv))


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env
    raw = environ.get("TOKEN_SPAREN_DENYLIST", "")
    if args.require_private_words and not private_words(raw):
        print(EMPTY_MESSAGE)
        return 2
    patterns = build_patterns(raw)
    missing = [p for p in args.path if p.is_symlink() or not p.exists()]
    if missing:
        for p in missing:
            print(f"deny-list: {p} does not exist (or is a symbolic link)")
        return 2
    if args.path:
        hits: Iterator[str] = (h for top in args.path for h in _tree_hits(top, patterns))
    else:
        hits = _repo_hits(patterns)
    failures = 0
    for where in hits:
        # Report location only; never echo the matched content.
        print(f"deny-list hit: {where}")
        failures += 1
    if failures:
        print(f"{failures} deny-list hit(s)")
        return 1
    print("deny-list clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
