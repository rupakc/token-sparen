"""State and recovery directories are private to the user (threat model: other local users) and a
repository cannot redirect recovery writes through committed symbolic links (threat model:
malicious repository)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from token_sparen import recovery
from token_sparen.config import log_error, private_mkdir
from token_sparen.store import jsonl, jsonstore

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture
def loose_umask() -> Iterator[None]:
    old = os.umask(0o022)  # the common default: new directories would be world-readable
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
def test_private_mkdir_creates_every_missing_level_0700(tmp_path: Path, loose_umask: None) -> None:
    os.chmod(tmp_path, 0o755)
    target = tmp_path / "a" / "b" / "c"
    private_mkdir(target)
    for d in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert _mode(d) == 0o700, d
    assert _mode(tmp_path) == 0o755  # an existing directory is left alone
    private_mkdir(target)  # idempotent
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        private_mkdir(tmp_path / "file")


@posix_only
def test_state_home_created_private_by_every_writer(tmp_path: Path, loose_umask: None) -> None:
    for name, write in (
        ("log", lambda home: log_error({"TOKEN_SPAREN_HOME": str(home)}, "t", "m")),
        ("store", lambda home: jsonstore.update(home / "p" / "k" / "x.json", dict, {})),
        ("jsonl", lambda home: jsonl.append(home / "p" / "k" / "d.jsonl", {"a": 1}, 1000)),
        ("locked", lambda home: jsonstore.write_locked(home / "p" / "y.json", {})),
    ):
        home = tmp_path / name / "state"
        write(home)
        assert _mode(tmp_path / name) == 0o700, name
        assert _mode(home) == 0o700, name
        for sub in home.rglob("*"):
            if sub.is_dir():
                assert _mode(sub) == 0o700, sub


@posix_only
def test_recovery_dirs_created_private(tmp_path: Path, loose_umask: None) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    ref = recovery.save_full(project, "full output\n")
    assert _mode(project / ".token-sparen") == 0o700
    assert _mode(project / ".token-sparen" / "outputs") == 0o700
    assert ref.path.read_text(encoding="utf-8") == "full output\n"


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("symbolic links unavailable")


def test_recovery_refuses_a_committed_symlinked_root(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _symlink(project / ".token-sparen", victim)
    with pytest.raises(OSError):
        recovery.save_full(project, "full output\n")
    assert list(victim.iterdir()) == []  # nothing written through the link
    recovery.prune_outputs(project)  # never follows it either


def test_recovery_refuses_a_symlinked_outputs_dir(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    stale = victim / "0123456789ab.txt"
    stale.write_text("someone else's file", encoding="utf-8")
    old = 1_000_000_000
    os.utime(stale, (old, old))
    project = tmp_path / "proj"
    (project / ".token-sparen").mkdir(parents=True)
    _symlink(project / ".token-sparen" / "outputs", victim)
    with pytest.raises(OSError):
        recovery.save_full(project, "full output\n")
    recovery.prune_outputs(project, ttl_days=1)
    assert stale.exists()  # pruning never deletes through a link
    assert sorted(p.name for p in victim.iterdir()) == ["0123456789ab.txt"]


def test_recovery_gitignore_never_written_through_a_link(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".token-sparen").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere" / "planted"
    elsewhere.parent.mkdir()
    try:
        (project / ".token-sparen" / ".gitignore").symlink_to(elsewhere)  # dangling
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("symbolic links unavailable")
    recovery.save_full(project, "full output\n")
    assert not elsewhere.exists()
