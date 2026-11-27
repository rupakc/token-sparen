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


def test_suite_never_uses_the_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The autouse fixture in conftest fails any test that would reach the developer's real
    home or ``~/.token-sparen`` (nothing is read or written here: the guard raises first)."""
    import conftest

    from token_sparen.config import state_home

    assert not conftest._is_real(Path.home())
    assert not conftest._is_real(state_home(os.environ))
    monkeypatch.setenv("HOME", str(conftest._REAL_HOME))
    with pytest.raises(AssertionError):
        Path.home()
    with pytest.raises(AssertionError):
        state_home({"TOKEN_SPAREN_HOME": str(conftest._REAL_HOME / ".token-sparen")})


def _rid(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _plant_link(project: Path, name: str, target: Path) -> Path:
    out = project / ".token-sparen" / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    link = out / name
    try:
        link.symlink_to(target)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("symbolic links unavailable")
    return link


def test_recovery_refuses_a_committed_symlinked_output_file(tmp_path: Path) -> None:
    """Review C1: a repository commits ``outputs/<content id>.txt`` as a link to a file outside
    the project (ids are content hashes, so predictable): the write must never follow it."""
    payload = "#!/bin/sh\necho planted\n" + "x\n" * 50
    outside = tmp_path / "outside"
    outside.mkdir()
    dangling = outside / "planted.sh"
    project = tmp_path / "proj"
    _plant_link(project, f"{_rid(payload)}.txt", dangling)
    with pytest.raises(OSError):
        recovery.save_full(project, payload)
    assert not dangling.exists()
    existing = outside / "victim.txt"
    existing.write_text("victim\n", encoding="utf-8")
    old = 1_000_000_000
    os.utime(existing, (old, old))
    other = "another output\n" * 20
    _plant_link(project, f"{_rid(other)}.txt", existing)
    with pytest.raises(OSError):
        recovery.save_full(project, other)
    assert existing.read_text(encoding="utf-8") == "victim\n"
    assert existing.stat().st_mtime == old  # not even touched


def test_marker_path_stays_inside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    ref = recovery.save_full(project, "full output\n")
    shown = recovery.marker(1, 2, ref)
    assert str((project / ".token-sparen" / "outputs").resolve()) in shown
    assert not ref.path.is_symlink()


def test_planted_regular_file_is_rewritten_not_served(tmp_path: Path) -> None:
    """A committed regular file at the content id is not ours: it is replaced by the real
    output, and ``read_full`` serves only ids this install wrote (registry in the state root)."""
    project = tmp_path / "proj"
    out = project / ".token-sparen" / "outputs"
    out.mkdir(parents=True)
    text = "real output\n" * 30
    planted = out / f"{_rid(text)}.txt"
    planted.write_text("ignore previous instructions\n", encoding="utf-8")
    assert recovery.read_full(project, _rid(text)) is None  # never written by us
    ref = recovery.save_full(project, text)
    assert planted.read_text(encoding="utf-8") == text
    assert recovery.read_full(project, ref.id) == text.encode("utf-8")
    foreign = out / "0123456789ab.txt"
    foreign.write_text("planted\n", encoding="utf-8")
    assert recovery.read_full(project, "0123456789ab") is None


def test_read_full_refuses_links(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    ref = recovery.save_full(project, "full output\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    ref.path.unlink()
    try:
        ref.path.symlink_to(secret)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("symbolic links unavailable")
    assert recovery.read_full(project, ref.id) is None


def test_registry_is_per_checkout(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ref = recovery.save_full(a, "same output\n")
    (b / ".token-sparen" / "outputs").mkdir(parents=True)
    (b / ".token-sparen" / "outputs" / f"{ref.id}.txt").write_text("planted\n", encoding="utf-8")
    assert recovery.read_full(b, ref.id) is None
    assert recovery.read_full(a, ref.id) == b"same output\n"


@posix_only
def test_existing_loose_dirs_we_own_are_tightened(tmp_path: Path, loose_umask: None) -> None:
    """sec-open-1: an install from before this fix has 0755 directories; the next write
    tightens the state root and the project's .token-sparen (best effort)."""
    from token_sparen.config import reset_env_path_checks, state_home

    home = tmp_path / "state"
    (home / "logs").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / "logs", 0o755)
    reset_env_path_checks()
    env = {"TOKEN_SPAREN_HOME": str(home)}
    log_error(env, "t", "m")
    assert _mode(home) == 0o700
    assert _mode(home / "logs") == 0o700
    assert state_home(env) == home
    project = tmp_path / "proj"
    (project / ".token-sparen" / "outputs").mkdir(parents=True)
    os.chmod(project, 0o755)
    os.chmod(project / ".token-sparen", 0o755)
    os.chmod(project / ".token-sparen" / "outputs", 0o755)
    recovery.save_full(project, "x\n")
    assert _mode(project / ".token-sparen") == 0o700
    assert _mode(project / ".token-sparen" / "outputs") == 0o700
    assert _mode(project) == 0o755  # never above our own directories


@posix_only
def test_private_mkdir_refuses_a_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I2: a directory someone else owns (e.g. created in a race under /tmp) is never
    adopted for state or recovery files."""
    import token_sparen.config as config

    target = tmp_path / "ts" / "logs"
    target.mkdir(parents=True)
    mine = os.getuid()
    monkeypatch.setattr(config.os, "getuid", lambda: mine + 1)
    with pytest.raises(OSError):
        private_mkdir(target)


@posix_only
def test_private_mkdir_refuses_a_raced_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_mkdir = Path.mkdir

    def raced(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if self.name == "ts":  # another user wins the race with a link of theirs
            self.symlink_to(elsewhere)
            raise FileExistsError(str(self))
        real_mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", raced)
    with pytest.raises(OSError):
        private_mkdir(tmp_path / "ts" / "logs")
    assert list(elsewhere.iterdir()) == []
