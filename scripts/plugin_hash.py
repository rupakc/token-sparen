"""Plugin hash: the identity of the executable plugin content (published interface, M5/M6).

Definition (binding ruling N1): the sha256 hex digest of the concatenation of
``f"{path}\\0{sha256(file bytes)}\\n"`` for every hashed file, in sorted order of the
repo-relative POSIX path. Hashed: ``.claude-plugin/plugin.json``, ``hooks/``, ``skills/`` and
``token_sparen/`` (``token_sparen/params/defaults.toml`` included). Not hashed:
``.claude-plugin/marketplace.json`` (distribution metadata), README, LICENSE, NOTICE,
SECURITY.md, CHANGELOG, ``docs/``, ``tests/``, ``bench/``, ``scripts/``, VCS metadata
(``.git/``, ``.gitignore``, ``.gitattributes``, ``.gitmodules``) and files never shipped
(``__pycache__/``, ``*.pyc``, ``*.pyo``, ``.DS_Store``).

Raw bytes, no line-ending normalisation, not a git tree hash: any change to a hashed file (a
version bump included) changes it. Symlinks are refused (the shipped tree has none).

Usage: ``python scripts/plugin_hash.py [--files] [ROOT]`` prints the hash (or the file list).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path

PLUGIN_PATHS = (".claude-plugin/plugin.json", "hooks/", "skills/", "token_sparen/")
PLUGIN_FILES = PLUGIN_PATHS  # name used by the M6 plan
MANIFEST = ".claude-plugin/plugin.json"
EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".git"})
EXCLUDED_FILE_NAMES = frozenset(
    {".DS_Store", ".gitignore", ".gitattributes", ".gitmodules", ".git"}
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def _shipped(name: str) -> bool:
    return name not in EXCLUDED_FILE_NAMES and not name.endswith(EXCLUDED_SUFFIXES)


def _refuse_symlinks(root: Path, entry: str) -> None:
    """Refuse a symlink at ``entry`` or at any of its parent components below ``root``."""
    parts = entry.rstrip("/").split("/")
    for i in range(1, len(parts) + 1):
        rel = "/".join(parts[:i])
        if (root / rel).is_symlink():
            raise ValueError(f"symlink in plugin tree: {rel}")


def _regular(path: Path, rel: str) -> None:
    """Refuse symlinks, FIFOs, sockets and device files (reading a FIFO would block)."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError(f"symlink in plugin tree: {rel}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"not a regular file in plugin tree: {rel}")


def plugin_files(root: Path) -> list[str]:
    """Sorted repo-relative POSIX paths of every hashed file under ``root``.

    Raises `FileNotFoundError` without ``.claude-plugin/plugin.json`` and `ValueError` on a
    symlink (anywhere on a hashed path) or a non-regular file (FIFO, socket, device).
    """
    for entry in PLUGIN_PATHS:
        _refuse_symlinks(root, entry)
    if not (root / MANIFEST).exists():
        raise FileNotFoundError(f"{MANIFEST} not found under {root}")
    found: list[str] = []
    for entry in PLUGIN_PATHS:
        base = root / entry
        if not entry.endswith("/"):
            _regular(base, entry)
            found.append(entry)
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
            for name in dirnames:
                if (here / name).is_symlink():
                    raise ValueError(f"symlink in plugin tree: {_rel(here / name, root)}")
            for name in filenames:
                if _shipped(name):
                    rel = _rel(here / name, root)
                    _regular(here / name, rel)
                    found.append(rel)
                elif (here / name).is_symlink():
                    raise ValueError(f"symlink in plugin tree: {_rel(here / name, root)}")
    return sorted(found)


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def plugin_hash(root: Path) -> str:
    """sha256 hex over ``f"{path}\\0{sha256(bytes)}\\n"`` lines in sorted path order."""
    digest = hashlib.sha256()
    for rel in plugin_files(root):
        file_digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        digest.update(f"{rel}\0{file_digest}\n".encode())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plugin_hash", description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".", help="plugin root (default: .)")
    parser.add_argument("--files", action="store_true", help="list the hashed files instead")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.files:
            print("\n".join(plugin_files(root)))
        else:
            print(plugin_hash(root))
    except (OSError, ValueError) as exc:
        print(f"plugin_hash: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
