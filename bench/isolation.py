"""Config isolation for the E4 benchmark: the owner's real Claude Code config is never touched.

Every agent run and every competitor install gets its own ``CLAUDE_CONFIG_DIR`` (and ``HOME``)
inside the bench workspace. `assert_isolated` refuses a config dir that is, contains or lies
under the real ``~/.claude``, the real ``~/.claude.json``, the caller's own
``CLAUDE_CONFIG_DIR`` or the caller's ``$HOME/.claude`` — compared after resolving symbolic
links, and also by file identity for existing paths — or that holds a symbolic link into one of
them. `check_workspace` refuses a workspace inside any git work tree (benchmark data is private
and must never be committed by accident) or under a real config dir, and creates missing
directories 0700.

`isolated_env` gives every install and run its own ``HOME`` and every other location a tool
may resolve on its own (XDG dirs, npm/pip/uv/cargo prefixes and caches, the global git
config, ``ZDOTDIR``, ``TMPDIR``, ``CLAUDE_CONFIG_DIR``, ``TOKEN_SPAREN_HOME``), all inside a
per-run directory of the workspace, so an ``rtk init -g``-style installer that edits "the
user's" settings edits the run's copy. `assert_isolated_env` verifies such an environment
before anything is started.

These are pre-run checks. The harness (Tasks 6 and 13) re-checks a copied-out run home for
symbolic links after every run, before `bench.arms.carry_state` or the leak scan.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

GIT_TIMEOUT_S = 10.0
# per-run locations, relative to the run HOME ("" = HOME itself); HOME first
_LAYOUT = {
    "HOME": "",
    "CLAUDE_CONFIG_DIR": ".claude",
    "TOKEN_SPAREN_HOME": ".token-sparen",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
    "npm_config_prefix": ".npm-global",
    "npm_config_cache": ".npm",
    "PIP_CACHE_DIR": ".cache/pip",
    "UV_CACHE_DIR": ".cache/uv",
    "CARGO_HOME": ".cargo",
    "ZDOTDIR": "",
    "TMPDIR": ".tmp",
}
ISOLATED_DIR_KEYS = tuple(_LAYOUT)
ISOLATED_PATH_KEYS = (*ISOLATED_DIR_KEYS, "GIT_CONFIG_GLOBAL")
# the real home's state an isolated location may never equal, contain or lie inside
REAL_DOT_STATE = (
    ".claude",
    ".claude.json",
    ".token-sparen",
    ".headroom",
    ".tokeneconomizer",
    ".context-mode",
    ".rtk",
    ".config",
    ".local",
    ".cache",
    ".npm",
    ".npm-global",
    ".cargo",
    ".gitconfig",
    ".zshrc",
    ".zshenv",
    ".zprofile",
    ".bashrc",
    ".bash_profile",
    ".profile",
)


class IsolationError(Exception):
    """A config dir or workspace would reach the owner's real Claude config or a git tree."""


def _resolve(path: Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def _related(a: Path, b: Path) -> bool:
    """``a`` equals, contains or lies under ``b``."""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def real_config_paths(real_home: Path, environ: Mapping[str, str]) -> list[Path]:
    """Every location the caller's real Claude Code config may live at (resolved)."""
    homes = [real_home]
    if environ.get("HOME"):
        homes.append(Path(environ["HOME"]))
    paths: list[Path] = []
    for home in homes:
        paths += [_resolve(home / ".claude"), _resolve(home / ".claude.json")]
    if environ.get("CLAUDE_CONFIG_DIR"):
        paths.append(_resolve(Path(environ["CLAUDE_CONFIG_DIR"])))
    return paths


def _walk_links(root: Path) -> Iterator[Path]:
    if root.is_symlink():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                yield path


def assert_isolated(config_dir: Path, *, real_home: Path, environ: Mapping[str, str]) -> None:
    """Raise `IsolationError` unless ``config_dir`` is a separate, bench-owned config dir."""
    if not Path(os.path.expanduser(str(config_dir))).is_absolute():
        raise IsolationError("config dir must be an absolute path")
    target = _resolve(config_dir)
    for real in real_config_paths(real_home, environ):
        if _related(target, real) or _same_file(target, real):
            raise IsolationError(f"config dir {target} overlaps the real Claude config {real}")
    real_paths = real_config_paths(real_home, environ)
    if target.is_dir():
        for link in _walk_links(target):
            pointed = link.resolve()
            if any(_related(pointed, real) or _same_file(pointed, real) for real in real_paths):
                raise IsolationError(f"symlink {link} points into the real Claude config")


def _in_git_work_tree(directory: Path) -> bool:
    for node in (directory, *directory.parents):
        if (node / ".git").exists():
            return True
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def _existing_ancestor(path: Path) -> Path:
    node = path
    while not node.exists() and node.parent != node:
        node = node.parent
    return node


def check_workspace(out: Path) -> Path:
    """The resolved bench workspace, created 0700 (new directories only) if missing.

    Refused when it is not a directory, lies inside any git work tree, or overlaps the real
    Claude config of the current user.
    """
    ws = _resolve(out)
    if ws.exists() and not ws.is_dir():
        raise IsolationError("workspace must be a directory")
    if _in_git_work_tree(_existing_ancestor(ws)):
        raise IsolationError("workspace must be outside any git work tree (bench data is private)")
    for real in real_config_paths(Path.home(), os.environ):
        if _related(ws, real):
            raise IsolationError("workspace must not overlap the real Claude config")
    missing: list[Path] = []
    node = ws
    while not node.exists():
        missing.append(node)
        node = node.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    return ws


def isolated_env(run_home: Path) -> dict[str, str]:
    """The isolation variables of one install or run rooted at ``run_home`` (pure: nothing is
    created; `make_isolated_dirs` does that after `assert_isolated_env`)."""
    home = Path(run_home)
    env = {key: str(home / rel) if rel else str(home) for key, rel in _LAYOUT.items()}
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def make_isolated_dirs(env: Mapping[str, str]) -> None:
    """Create the directories of an isolated environment; new ones 0700."""
    for key in ISOLATED_DIR_KEYS:
        missing: list[Path] = []
        node = Path(env[key])
        while not node.exists():
            missing.append(node)
            node = node.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            if os.name == "posix":
                os.chmod(directory, 0o700)


def _real_state(real_home: Path, environ: Mapping[str, str]) -> tuple[list[Path], list[Path]]:
    homes = [_resolve(real_home)]
    if environ.get("HOME"):
        homes.append(_resolve(Path(environ["HOME"])))
    state = [home / name for home in homes for name in REAL_DOT_STATE]
    if environ.get("CLAUDE_CONFIG_DIR"):
        state.append(_resolve(Path(environ["CLAUDE_CONFIG_DIR"])))
    return homes, state


def assert_isolated_env(
    env: Mapping[str, str], *, workspace: Path, real_home: Path, environ: Mapping[str, str]
) -> None:
    """Raise `IsolationError` unless every isolation variable of ``env`` is set, absolute,
    inside ``workspace``, never the real home (nor an ancestor of it) and never equal to,
    inside or holding the real home's dot-state; ``GIT_CONFIG_NOSYSTEM`` must be ``1``."""
    ws = _resolve(workspace)
    homes, state = _real_state(real_home, environ)
    for key in ISOLATED_PATH_KEYS:
        value = env.get(key)
        if not value:
            raise IsolationError(f"{key} is not set for the isolated run")
        if not Path(value).is_absolute():
            raise IsolationError(f"{key} must be an absolute path")
        path = _resolve(Path(value))
        if not path.is_relative_to(ws):
            raise IsolationError(f"{key} must lie inside the bench workspace")
        if any(home.is_relative_to(path) for home in homes):
            raise IsolationError(f"{key} is, or holds, the real home")
        if any(_related(path, real) or _same_file(path, real) for real in state):
            raise IsolationError(f"{key} overlaps the real home's tool state")
    if env.get("GIT_CONFIG_NOSYSTEM") != "1":
        raise IsolationError("GIT_CONFIG_NOSYSTEM must be 1")
    assert_isolated(Path(env["CLAUDE_CONFIG_DIR"]), real_home=real_home, environ=environ)
