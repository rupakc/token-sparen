"""Config isolation: never the real Claude config, never a workspace in a git tree (M5 Task 4)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bench import isolation as iso

POSIX = os.name != "nt"


@pytest.fixture
def real_home(tmp_path: Path) -> Path:
    home = tmp_path / "realhome"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    return home


def _ok(config: Path, real_home: Path, environ: dict[str, str] | None = None) -> None:
    iso.assert_isolated(config, real_home=real_home, environ=environ or {})


def test_refuses_real_claude_dir_and_claude_json(tmp_path: Path, real_home: Path) -> None:
    bench = tmp_path / "ws" / "arm" / ".claude"
    bench.mkdir(parents=True)
    _ok(bench, real_home)
    refused = [
        real_home / ".claude",
        real_home / ".claude" / "projects",
        real_home / ".claude" / "not-yet" / "deeper",
        real_home / ".claude.json",
        real_home,  # $CLAUDE_CONFIG_DIR=$HOME would write the real ~/.claude.json
        real_home.parent,
    ]
    for path in refused:
        with pytest.raises(iso.IsolationError):
            _ok(path, real_home)
    # the caller's own CLAUDE_CONFIG_DIR and HOME count as real too
    mine = tmp_path / "callers-config"
    mine.mkdir()
    with pytest.raises(iso.IsolationError):
        _ok(mine, real_home, {"CLAUDE_CONFIG_DIR": str(mine)})
    other_home = tmp_path / "otherhome"
    (other_home / ".claude").mkdir(parents=True)
    with pytest.raises(iso.IsolationError):
        _ok(other_home / ".claude", real_home, {"HOME": str(other_home)})
    with pytest.raises(iso.IsolationError):
        _ok(Path("relative/.claude"), real_home)


@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
def test_refuses_symlinks_into_real_config(tmp_path: Path, real_home: Path) -> None:
    link = tmp_path / "ws" / "link-config"
    link.parent.mkdir(parents=True)
    link.symlink_to(real_home / ".claude")
    with pytest.raises(iso.IsolationError):
        _ok(link, real_home)
    # a symlinked real home is still the real home
    alias = tmp_path / "alias-home"
    alias.symlink_to(real_home)
    with pytest.raises(iso.IsolationError):
        _ok(alias / ".claude", real_home)
    # a symlink inside an otherwise fresh config dir that points at the real config
    config = tmp_path / "ws" / "arm" / ".claude"
    config.mkdir(parents=True)
    (config / "settings.json").symlink_to(real_home / ".claude" / "projects")
    with pytest.raises(iso.IsolationError, match="symlink"):
        _ok(config, real_home)
    (config / "settings.json").unlink()
    (config / ".claude.json").symlink_to(real_home / ".claude.json")
    with pytest.raises(iso.IsolationError, match="symlink"):
        _ok(config, real_home)
    (config / ".claude.json").unlink()
    (config / "harmless").symlink_to(tmp_path / "ws")
    _ok(config, real_home)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def test_refuses_workspace_in_git_tree(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    for inside in (repo, repo / "ws", repo / "a" / "b" / "ws"):
        with pytest.raises(iso.IsolationError, match="git work tree"):
            iso.check_workspace(inside)
    out = iso.check_workspace(tmp_path / "outside" / "ws")
    assert out == (tmp_path / "outside" / "ws").resolve()
    assert out.is_dir()
    if POSIX:
        assert out.stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "outside").stat().st_mode & 0o777 == 0o700
    assert iso.check_workspace(out) == out  # existing workspace accepted
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(iso.IsolationError):
        iso.check_workspace(tmp_path / "file")
    if POSIX:
        link = tmp_path / "into-repo"
        link.symlink_to(repo)
        with pytest.raises(iso.IsolationError, match="git work tree"):
            iso.check_workspace(link / "ws")


def test_refuses_workspace_under_real_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(os.environ["HOME"])  # the suite's temporary home
    (home / ".claude").mkdir(exist_ok=True)
    with pytest.raises(iso.IsolationError):
        iso.check_workspace(home / ".claude" / "ws")


def test_workspace_does_not_tighten_existing_dirs(tmp_path: Path) -> None:
    existing = tmp_path / "shared"
    existing.mkdir(mode=0o755)
    if POSIX:
        os.chmod(existing, 0o755)
    iso.check_workspace(existing / "ws")
    if POSIX:
        assert existing.stat().st_mode & 0o777 == 0o755
