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


def _in_git_tree(project_dir: Path, env: Mapping[str, str]) -> bool:
    """A ``.git`` entry in ``project_dir`` or any parent, or a ``GIT_DIR`` in the environment:
    only then can git name a remote. Spares a git spawn per new project outside any repository
    (on macOS ``/usr/bin/git`` is an xcrun shim whose cold first call can take most of a hook's
    budget)."""
    if env.get("GIT_DIR"):
        return True
    try:
        cur = Path(os.path.abspath(project_dir))
    except (OSError, ValueError):
        return True
    for d in (cur, *cur.parents):
        try:
            if os.path.lexists(d / ".git"):
                return True
        except (OSError, ValueError):
            return True
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
