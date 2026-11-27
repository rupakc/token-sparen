"""Path-valued ``TOKEN_SPAREN_*`` variables (review M5): a committed Claude Code settings ``env``
must not point the state root (or the statusline's plugin root) at a directory the repository
controls. Temporary directories only; the user's home is faked."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from token_sparen import config
from token_sparen.config import state_home, user_config

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "token_sparen" / "assets" / "statusline.sh"


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config.reset_env_path_checks()
    return home


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    return repo


def _errors(home: Path) -> list[str]:
    path = home / ".token-sparen" / "logs" / "errors.jsonl"
    if not path.exists():
        return []
    return [json.loads(x)["error"] for x in path.read_text(encoding="utf-8").splitlines()]


def test_home_inside_project_dir_is_ignored(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    env = {"TOKEN_SPAREN_HOME": str(repo / ".ts"), "CLAUDE_PROJECT_DIR": str(repo)}
    assert state_home(env) == user_home / ".token-sparen"
    assert state_home(env) == user_home / ".token-sparen"
    errs = _errors(user_home)
    assert len(errs) == 1 and "TOKEN_SPAREN_HOME" in errs[0]  # once per process


def test_home_inside_the_git_work_tree_is_ignored(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    env = {"TOKEN_SPAREN_HOME": str(repo / ".ts"), "CLAUDE_PROJECT_DIR": str(repo / "sub")}
    assert state_home(env) == user_home / ".token-sparen"


def test_home_inside_cwd_is_ignored(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo / "sub")
    assert state_home({"TOKEN_SPAREN_HOME": str(repo / ".ts")}) == user_home / ".token-sparen"
    assert state_home({"TOKEN_SPAREN_HOME": ".ts"}) == user_home / ".token-sparen"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_into_the_project_is_ignored(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".ts").mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo / ".ts")
    env = {"TOKEN_SPAREN_HOME": str(link), "CLAUDE_PROJECT_DIR": str(repo)}
    assert state_home(env) == user_home / ".token-sparen"


def test_outside_owned_home_is_used(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    for target in (tmp_path / "state", tmp_path / "not" / "yet" / "there"):
        env = {"TOKEN_SPAREN_HOME": str(target), "CLAUDE_PROJECT_DIR": str(repo)}
        assert state_home(env) == target
    assert _errors(user_home) == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="ownership is checked on POSIX only")
def test_home_owned_by_someone_else_is_ignored(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state"
    target.mkdir()
    uid = os.getuid()
    monkeypatch.setattr(config.os, "getuid", lambda: uid + 1)
    assert state_home({"TOKEN_SPAREN_HOME": str(target)}) == user_home / ".token-sparen"
    assert state_home({"TOKEN_SPAREN_HOME": str(target / "new")}) == user_home / ".token-sparen"


def test_planted_user_config_is_not_read(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".ts").mkdir()
    (repo / ".ts" / "config.toml").write_text("[prune]\nenabled = true\n", encoding="utf-8")
    env = {"TOKEN_SPAREN_HOME": str(repo / ".ts"), "CLAUDE_PROJECT_DIR": str(repo)}
    assert user_config(env) == {}


def test_project_dir_containing_the_user_home_is_not_a_repository(
    tmp_path: Path, user_home: Path
) -> None:
    """A session started in (or above) the home directory: the user's own choice stands."""
    target = user_home / "state"
    env = {"TOKEN_SPAREN_HOME": str(target), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    assert state_home(env) == target


# --- the statusline launcher's TOKEN_SPAREN_ROOT ------------------------------------------------


def _plugin_copy(dest: Path) -> Path:
    shutil.copytree(ROOT / "token_sparen", dest / "token_sparen")
    return dest


def _launch(env: dict[str, str], cwd: Path) -> str:
    proc = subprocess.run(
        ["sh", str(LAUNCHER)],
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
def test_launcher_ignores_a_root_inside_the_project(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    plugin = _plugin_copy(repo / "vendor" / "ts")
    # a marker module that would show the planted copy ran
    (plugin / "token_sparen" / "adapters" / "claude" / "statusline.py").write_text(
        "def main(args):\n    print('PLANTED')\n    return 0\n", encoding="utf-8"
    )
    base = {k: v for k, v in os.environ.items() if k not in {"CLAUDE_CONFIG_DIR"}}
    env = {**base, "HOME": str(user_home), "TOKEN_SPAREN_ROOT": str(plugin)}
    assert "PLANTED" not in _launch({**env, "CLAUDE_PROJECT_DIR": str(repo / "sub")}, tmp_path)
    assert "PLANTED" not in _launch(env, repo / "sub")  # cwd inside the work tree
    outside = _plugin_copy(tmp_path / "outside")
    (outside / "token_sparen" / "adapters" / "claude" / "statusline.py").write_text(
        "def main(args):\n    print('OUTSIDE')\n    return 0\n", encoding="utf-8"
    )
    env["TOKEN_SPAREN_ROOT"] = str(outside)
    assert "OUTSIDE" in _launch({**env, "CLAUDE_PROJECT_DIR": str(repo)}, repo)
