"""Private report files for offline evaluation (E1 replay, E3 off-policy evaluation).

Reports are built from the owner's own transcripts and decision logs, so they hold aggregates
only and are written where they can never be committed by accident: never under the git work
tree of the current directory, never under this package's own checkout, and never inside any
git work tree at all (``git rev-parse`` succeeds, or an ancestor holds a ``.git`` entry). The
file is written atomically with mode 0600; missing parent directories are created 0700.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import token_sparen

GIT_TIMEOUT_S = 10.0


class OutPathError(ValueError):
    """The report path is not allowed (the message never repeats the path)."""


def package_checkout() -> Path:
    """The directory holding the ``token_sparen`` package (source checkout or site-packages)."""
    return Path(token_sparen.__file__).resolve().parent.parent


def _git(where: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(where), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _toplevel(where: Path) -> Path | None:
    top = _git(where, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def _existing_ancestor(path: Path) -> Path:
    node = path
    while not node.exists() and node.parent != node:
        node = node.parent
    return node


def _in_work_tree(directory: Path) -> bool:
    for node in (directory, *directory.parents):
        if (node / ".git").exists():
            return True
    return _git(directory, "rev-parse", "--is-inside-work-tree") is not None


def _under(path: Path, root: Path | None) -> bool:
    return root is not None and (path == root or path.is_relative_to(root))


def check_out_path(path: Path) -> Path:
    """The resolved report path, or ``OutPathError`` if it is inside a repository."""
    out = Path(os.path.expanduser(str(path))).resolve()
    if out.is_dir():
        raise OutPathError("--out must be a file path, not a directory")
    roots = (_toplevel(Path.cwd()), _toplevel(package_checkout()), package_checkout())
    if any(_under(out, root) for root in roots):
        raise OutPathError("--out must be outside the repository (reports are private)")
    if _in_work_tree(_existing_ancestor(out.parent)):
        raise OutPathError("--out must be outside any git work tree (reports are private)")
    return out


def write_private(path: Path, data: Any) -> Path:
    """Check ``path``, then write ``data`` as JSON atomically with mode 0600."""
    out = check_out_path(path)
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".report-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        os.chmod(out, 0o600)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out
