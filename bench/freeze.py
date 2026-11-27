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

from scripts.plugin_hash import MANIFEST, PLUGIN_PATHS, plugin_files, plugin_hash

FILE_MODE = 0o444
EXEC_MODE = 0o555  # files committed as 100755 keep their exec bit
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


def _is_tag_of(repo: Path, ref: str, commit: str) -> bool:
    """True iff ``ref`` is a plain tag name (or ``refs/tags/<name>``) whose commit is ``commit``.

    Revision expressions rooted at a tag (``v1~1``, ``v1^``, ``refs/tags/v1~1``) are not tags.
    """
    full = "refs/tags/" + ref.removeprefix("refs/tags/")
    if _git(repo, "check-ref-format", full).returncode != 0:
        return False
    if _git(repo, "show-ref", "--verify", "--quiet", full).returncode != 0:
        return False
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{full}^{{commit}}")
    return proc.returncode == 0 and proc.stdout.decode("ascii").strip() == commit


def _present_paths(repo: Path, commit: str) -> list[str]:
    wanted = [p.rstrip("/") for p in PLUGIN_PATHS]
    proc = _git(repo, "ls-tree", "-z", "--name-only", commit, "--", *wanted)
    if proc.returncode != 0:
        raise FreezeError(f"git ls-tree failed: {proc.stderr.decode(errors='replace').strip()}")
    present = [p for p in proc.stdout.decode("utf-8").split("\0") if p]
    if MANIFEST not in present:
        raise FreezeError(f"{MANIFEST} missing at {commit}")
    return present


def _extract(archive: bytes, into: Path) -> set[str]:
    """Extract regular files and dirs; return the POSIX paths of executable files."""
    executable: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise FreezeError(f"refusing non-regular archive member {member.name!r}")
            if member.isfile() and member.mode & 0o111:
                executable.add(member.name)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:  # pragma: no cover - Python < 3.11.4
            tar.extractall(into)  # noqa: S202 - members checked above
    return executable


def _drop_unhashed(root: Path) -> None:
    """Remove exported entries outside the hash (e.g. a tracked nested `.gitignore`), so a
    frozen tree holds exactly `plugin_files()` and `verify` can flag any extra entry."""
    try:
        hashed = set(plugin_files(root))
    except (OSError, ValueError) as exc:
        raise FreezeError(f"exported plugin tree rejected: {exc}") from exc
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        here = Path(dirpath)
        for name in filenames:
            if (here / name).relative_to(root).as_posix() not in hashed:
                (here / name).unlink()
        if here != root and not any(here.iterdir()):
            here.rmdir()


def _make_read_only(root: Path, executable: set[str], *, include_root: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        here = Path(dirpath)
        for name in filenames:
            rel = (here / name).relative_to(root).as_posix()
            os.chmod(here / name, EXEC_MODE if rel in executable else FILE_MODE)
        for name in dirnames:
            os.chmod(here / name, DIR_MODE)
    if include_root:
        os.chmod(root, DIR_MODE)


def freeze(repo: Path, ref: str, dest: Path, *, dry_run: bool = False) -> FrozenPlugin:
    """Export the plugin content of ``ref`` into the new directory ``dest``, read-only.

    A real stage freezes a plain tag name only (``v1.0.0-rc1`` or ``refs/tags/v1.0.0-rc1``);
    ``dry_run=True`` accepts any committed ref (tests, CI dry runs). The export is of the
    committed ref, never of uncommitted working-tree edits. `git archive` applies the ref's
    ``.gitattributes`` (eol, export-ignore, export-subst); today only ``eol=lf`` applies, so the
    frozen bytes equal a checkout of the same commit (tested).
    """
    if dest.exists():
        raise FreezeError(f"destination exists: {dest}")
    commit = _resolve_commit(repo, ref)
    if not dry_run and not _is_tag_of(repo, ref, commit):
        raise FreezeError(f"ref {ref!r} is not a tag; only tagged commits are frozen for a stage")
    paths = _present_paths(repo, commit)
    proc = _git(repo, "archive", "--format=tar", commit, "--", *paths)
    if proc.returncode != 0:
        raise FreezeError(f"git archive failed: {proc.stderr.decode(errors='replace').strip()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        executable = _extract(proc.stdout, staging)
        _drop_unhashed(staging)
        _make_read_only(staging, executable, include_root=False)
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


def _extras(root: Path, hashed: set[str]) -> list[str]:
    """Entries a fresh export would not contain (unhashed files, caches, empty dirs)."""
    keep_dirs = {p.as_posix() for rel in hashed for p in Path(rel).parents if p != Path(".")}
    extras: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rels = {d: (here / d).relative_to(root).as_posix() for d in dirnames}
        extras += [rel + "/" for rel in rels.values() if rel not in keep_dirs]
        dirnames[:] = [d for d in dirnames if rels[d] in keep_dirs]
        for name in filenames:
            rel = (here / name).relative_to(root).as_posix()
            if rel not in hashed:
                extras.append(rel)
    return sorted(extras)


def verify(frozen: FrozenPlugin) -> None:
    """Raise `FrozenPluginChanged` unless the frozen copy is intact, read-only, same-hash and
    holds nothing but the hashed files (no `__pycache__`, `.pyc` or other extra entry)."""
    if not frozen.path.is_dir():
        raise FrozenPluginChanged(f"frozen plugin missing: {frozen.path}")
    if os.name != "nt":
        writable = _writable(frozen.path)
        if writable:
            raise FrozenPluginChanged(f"frozen plugin is writable: {', '.join(writable[:5])}")
    try:
        hashed = set(plugin_files(frozen.path))
        found = plugin_hash(frozen.path)
    except (OSError, ValueError) as exc:
        raise FrozenPluginChanged(f"frozen plugin unreadable: {exc}") from exc
    extras = _extras(frozen.path, hashed)
    if extras:
        raise FrozenPluginChanged(f"frozen plugin has extra entries: {', '.join(extras[:5])}")
    if found != frozen.plugin_hash:
        raise FrozenPluginChanged(
            f"plugin hash changed: frozen {frozen.plugin_hash}, now {found} ({frozen.path})"
        )
