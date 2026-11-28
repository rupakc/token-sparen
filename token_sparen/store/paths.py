"""State locations and stable project keys."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from token_sparen.config import state_home

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_KEY = re.compile(r"[0-9a-f]{16}")
_SCP = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(?!//)(.*)$")


def home(env: Mapping[str, str] | None = None) -> Path:
    return state_home(os.environ if env is None else env)


def project_dir_for(home_dir: Path, key: str) -> Path:
    if not _KEY.fullmatch(key):
        raise ValueError(f"invalid project key: {key!r}")
    return home_dir / "projects" / key


def normalise_remote(url: str) -> str:
    """Reduce ssh/https remote spellings to ``host/path`` (lowercase host, no .git)."""
    text = url.strip()
    if _SCHEME.match(text):
        text = _SCHEME.sub("", text, count=1)
        if "@" in text.split("/", 1)[0]:
            text = text.split("@", 1)[1]
        host, _, rest = text.partition("/")
        host = host.split(":", 1)[0]
    else:
        m = _SCP.match(text)
        if m is None:
            return text.rstrip("/").removesuffix(".git")
        host, rest = m.group(1), m.group(2)
    rest = rest.strip("/").removesuffix(".git").rstrip("/")
    return f"{host.lower()}/{rest}"


def _bare_repo(d: Path) -> bool:
    """``d`` looks like a bare repository's git dir (``HEAD``, ``objects/``, ``refs/``)."""
    return (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir()


def _in_git_tree(project_dir: Path, env: Mapping[str, str]) -> bool:
    """Can git name a remote for ``project_dir``? Only with a ``GIT_DIR`` in the environment,
    or a ``.git`` entry (or a bare repository) in the directory or a parent, along the path as
    given and along its realpath (git resolves a symlinked project dir and discovers the
    repository physically, so a link into a repository's sub-dir keeps its remote key; the
    hook passes the path unresolved, the CLI resolved, and both must agree). Spares a git spawn
    per new project outside any repository (on macOS ``/usr/bin/git`` is an xcrun shim whose
    cold first call can take most of a hook's budget)."""
    if env.get("GIT_DIR"):
        return True
    try:
        chains = {Path(os.path.abspath(project_dir)), Path(os.path.realpath(project_dir))}
        for cur in chains:
            for d in (cur, *cur.parents):
                if os.path.lexists(d / ".git") or _bare_repo(d):
                    return True
    except (OSError, ValueError):
        return True  # cannot tell: let git decide
    return False


def _origin(project_dir: Path) -> str | None:
    if not _in_git_tree(project_dir, os.environ):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


def project_key(project_dir: Path) -> str:
    """First 16 hex of sha256(normalised origin remote), else of the realpath."""
    remote = _origin(project_dir)
    basis = normalise_remote(remote) if remote else os.path.realpath(project_dir)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
