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


def _case_insensitive(base: Path) -> bool:
    probe = base / "CaseProbe"
    probe.mkdir()
    try:
        return (base / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_case_and_dotdot_spellings_cannot_bypass(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1 (I-3): compared by file identity, so `../myrepo/.ts` from `MyRepo` (a
    case-insensitive file system, the macOS default) or a symlinked alias of the checkout is
    still inside the project."""
    repo = tmp_path / "MyRepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".ts").mkdir()
    monkeypatch.chdir(repo)
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    default = user_home / ".token-sparen"
    assert state_home({**env, "TOKEN_SPAREN_HOME": "../MyRepo/.ts"}) == default
    if _case_insensitive(tmp_path):
        for spelling in ("../myrepo/.ts", str(tmp_path / "MYREPO" / ".ts")):
            assert state_home({**env, "TOKEN_SPAREN_HOME": spelling}) == default, spelling
    else:  # simulate the alias on a case-sensitive file system
        (tmp_path / "myrepo").symlink_to(repo)
        assert state_home({**env, "TOKEN_SPAREN_HOME": "../myrepo/.ts"}) == default
    assert state_home({**env, "TOKEN_SPAREN_HOME": "../MyRepo/.ts/../../state"}) == Path(
        "../MyRepo/.ts/../../state"
    )


def test_rejection_logged_once_across_processes(tmp_path: Path, user_home: Path) -> None:
    repo = _repo(tmp_path)
    env = {"TOKEN_SPAREN_HOME": str(repo / ".ts"), "CLAUDE_PROJECT_DIR": str(repo)}
    for _ in range(3):
        config.reset_env_path_checks()  # a fresh hook process each time
        state_home(env)
    assert len(_errors(user_home)) == 1


@pytest.mark.real_seed
def test_test_seed_ignored_with_a_rejected_home(tmp_path: Path, user_home: Path) -> None:
    from token_sparen.learn import hookpolicy

    repo = _repo(tmp_path)
    env = {
        "TOKEN_SPAREN_HOME": str(repo / ".ts"),
        "CLAUDE_PROJECT_DIR": str(repo),
        "TOKEN_SPAREN_TEST_SEED": "1",
    }
    assert hookpolicy.test_seed(env) is None


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


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
def test_launcher_root_alias_spelling_is_still_inside(tmp_path: Path, user_home: Path) -> None:
    repo = tmp_path / "MyRepo"
    (repo / ".git").mkdir(parents=True)
    plugin = _plugin_copy(repo / "vendor" / "ts")
    (plugin / "token_sparen" / "adapters" / "claude" / "statusline.py").write_text(
        "def main(args):\n    print('PLANTED')\n    return 0\n", encoding="utf-8"
    )
    alias = "../myrepo/vendor/ts"
    if not (tmp_path / "myrepo").exists():
        (tmp_path / "myrepo").symlink_to(repo)  # case-sensitive FS: simulate the alias
    base = {k: v for k, v in os.environ.items() if k not in {"CLAUDE_CONFIG_DIR"}}
    env = {**base, "HOME": str(user_home), "TOKEN_SPAREN_ROOT": alias}
    assert "PLANTED" not in _launch({**env, "CLAUDE_PROJECT_DIR": str(repo)}, repo)


def _fake_stat(monkeypatch: pytest.MonkeyPatch, at: Path, *, uid: int, mode: int) -> None:
    real = os.stat
    fake = os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))

    def stat(path: object, *a: object, **k: object) -> os.stat_result:
        if os.fspath(path) == str(at):  # type: ignore[call-overload]
            return fake
        return real(path, *a, **k)  # type: ignore[call-overload]

    monkeypatch.setattr(config.os, "stat", stat)


_DIR = 0o040000
_STICKY = 0o1000


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="ownership is checked on POSIX only")
@pytest.mark.parametrize(
    ("uid_off", "mode", "exists", "expected"),
    [
        (0, _DIR | 0o755, False, True),  # new path, user-owned parent
        (None, _DIR | 0o777 | _STICKY, False, True),  # new path, root-owned sticky parent
        (None, _DIR | 0o755, False, False),  # new path, root-owned non-sticky parent
        (1, _DIR | 0o777 | _STICKY, False, False),  # new path, other user's parent
        (None, _DIR | 0o777 | _STICKY, True, False),  # existing path, root-owned (sticky)
        (1, _DIR | 0o755, True, False),  # existing path, other uid
        (0, _DIR | 0o755, True, True),  # existing path, ours
    ],
)
def test_ownership_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uid_off: int | None,
    mode: int,
    exists: bool,
    expected: bool,
) -> None:
    me = os.getuid()
    uid = 0 if uid_off is None else me + uid_off
    if uid_off is None and me == 0:
        pytest.skip("running as root")
    target = tmp_path / "state"
    if exists:
        target.mkdir()
    _fake_stat(monkeypatch, target if exists else tmp_path, uid=uid, mode=mode)
    assert config._owned(str(target if exists else target / "new")) is expected
