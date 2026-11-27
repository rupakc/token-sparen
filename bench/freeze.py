"""Frozen read-only plugin per stage (ruling M5-B3; dry runs need no tag, ruling M6-B2).

`freeze` exports the hashed plugin content (`scripts.plugin_hash.PLUGIN_PATHS`) of one git ref
with `git archive` into a new directory, makes it read-only (files 0444, dirs 0555) and records
its commit and `plugin_hash`. `verify` re-checks a frozen copy before every run.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scripts.plugin_hash import MANIFEST, PLUGIN_PATHS, plugin_hash

FILE_MODE = 0o444
DIR_MODE = 0o555
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


class FreezeError(Exception):
    """The plugin could not be frozen from the requested ref."""


class FrozenPluginChanged(Exception):
    """A frozen plugin copy no longer matches the hash recorded when it was frozen."""


@dataclass(frozen=True)
class FrozenPlugin:
    path: Path
    ref: str
    commit: str
    plugin_hash: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args], capture_output=True, check=False, env=env
    )


def _resolve_commit(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise FreezeError(f"unknown ref {ref!r}")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise FreezeError(f"unknown ref {ref!r} in {repo}")
    return proc.stdout.decode("ascii").strip()


def _is_tag(repo: Path, ref: str) -> bool:
    name = ref.removeprefix("refs/tags/")
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{name}").returncode == 0


def _present_paths(repo: Path, commit: str) -> list[str]:
    wanted = [p.rstrip("/") for p in PLUGIN_PATHS]
    proc = _git(repo, "ls-tree", "-z", "--name-only", commit, "--", *wanted)
    if proc.returncode != 0:
        raise FreezeError(f"git ls-tree failed: {proc.stderr.decode(errors='replace').strip()}")
    present = [p for p in proc.stdout.decode("utf-8").split("\0") if p]
    if MANIFEST not in present:
        raise FreezeError(f"{MANIFEST} missing at {commit}")
    return present


def _extract(archive: bytes, into: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise FreezeError(f"refusing non-regular archive member {member.name!r}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:  # pragma: no cover - Python < 3.11.4
            tar.extractall(into)  # noqa: S202 - members checked above


def _make_read_only(root: Path, *, include_root: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        here = Path(dirpath)
        for name in filenames:
            os.chmod(here / name, FILE_MODE)
        for name in dirnames:
            os.chmod(here / name, DIR_MODE)
    if include_root:
        os.chmod(root, DIR_MODE)


def freeze(repo: Path, ref: str, dest: Path, *, dry_run: bool = False) -> FrozenPlugin:
    """Export the plugin content of ``ref`` into the new directory ``dest``, read-only.

    A real stage freezes a tag only; ``dry_run=True`` accepts any ref (tests, CI dry runs).
    """
    if dest.exists():
        raise FreezeError(f"destination exists: {dest}")
    commit = _resolve_commit(repo, ref)
    if not dry_run and not _is_tag(repo, ref):
        raise FreezeError(f"ref {ref!r} is not a tag; only tagged commits are frozen for a stage")
    paths = _present_paths(repo, commit)
    proc = _git(repo, "archive", "--format=tar", commit, "--", *paths)
    if proc.returncode != 0:
        raise FreezeError(f"git archive failed: {proc.stderr.decode(errors='replace').strip()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        _extract(proc.stdout, staging)
        _make_read_only(staging, include_root=False)
        staging.rename(dest)
    except BaseException:
        _remove_tree(staging)
        raise
    os.chmod(dest, DIR_MODE)
    return FrozenPlugin(path=dest, ref=ref, commit=commit, plugin_hash=plugin_hash(dest))


def _remove_tree(root: Path) -> None:
    """Remove a (possibly partly read-only) staging tree; best effort."""
    for dirpath, _dirnames, _filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
    shutil.rmtree(root, ignore_errors=True)


def _writable(root: Path) -> list[str]:
    paths = [root]
    for dirpath, dirnames, filenames in os.walk(root):
        paths.extend(Path(dirpath) / name for name in [*dirnames, *filenames])
    return [
        p.relative_to(root).as_posix()
        for p in paths
        if stat.S_IMODE(p.lstat().st_mode) & _WRITE_BITS
    ]


def verify(frozen: FrozenPlugin) -> None:
    """Raise `FrozenPluginChanged` unless the frozen copy is intact, read-only and same-hash."""
    if not frozen.path.is_dir():
        raise FrozenPluginChanged(f"frozen plugin missing: {frozen.path}")
    if os.name != "nt":
        writable = _writable(frozen.path)
        if writable:
            raise FrozenPluginChanged(f"frozen plugin is writable: {', '.join(writable[:5])}")
    try:
        found = plugin_hash(frozen.path)
    except (OSError, ValueError) as exc:
        raise FrozenPluginChanged(f"frozen plugin unreadable: {exc}") from exc
    if found != frozen.plugin_hash:
        raise FrozenPluginChanged(
            f"plugin hash changed: frozen {frozen.plugin_hash}, now {found} ({frozen.path})"
        )
