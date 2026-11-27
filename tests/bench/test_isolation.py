"""Config isolation: never the real Claude config, never a workspace in a git tree (M5 Task 4)."""

from __future__ import annotations

import os
import subprocess
import sys
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


INSTALLER = """
import os, pathlib, subprocess
home = pathlib.Path(os.path.expanduser("~"))
settings = home / ".claude" / "settings.json"
settings.parent.mkdir(parents=True, exist_ok=True)
settings.write_text('{"hooks": {}}')
(home / "RTK.md").write_text("rtk")
xdg = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "tool" / "config.toml"
xdg.parent.mkdir(parents=True, exist_ok=True)
xdg.write_text("x = 1")
subprocess.run(["git", "config", "--global", "user.name", "bench"], check=True)
"""


def test_install_writes_land_in_run_home(tmp_path: Path) -> None:
    """Review I2: an `rtk init -g`-style installer resolving ~ and XDG dirs writes only under
    the per-run HOME; the caller's home stays untouched."""
    caller_home = Path(os.environ["HOME"])  # the suite's temporary home
    before = sorted(p.relative_to(caller_home) for p in caller_home.rglob("*"))
    ws = iso.check_workspace(tmp_path / "ws")
    home = ws / "templates" / "rtk" / "home"
    env = iso.isolated_env(home)
    iso.assert_isolated_env(env, workspace=ws, real_home=caller_home, environ=os.environ)
    assert not home.exists()  # isolated_env is pure; directories come after the check
    iso.make_isolated_dirs(env)
    if POSIX:
        assert home.stat().st_mode & 0o777 == 0o700
    assert env["HOME"] == str(home) and env["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    for key in iso.ISOLATED_DIR_KEYS:
        assert Path(env[key]).is_relative_to(home), key
    script = tmp_path / "installer.py"
    script.write_text(INSTALLER, encoding="utf-8")
    run_env = {**env, "PATH": os.environ.get("PATH", "")}
    if os.name == "nt":  # Python and git on Windows need these to start; not home locations
        run_env |= {k: os.environ[k] for k in ("SYSTEMROOT", "COMSPEC") if k in os.environ}
    subprocess.run([sys.executable, str(script)], env=run_env, check=True, cwd=tmp_path)
    assert (home / ".claude" / "settings.json").is_file()
    assert (home / "RTK.md").is_file()
    assert (Path(env["XDG_CONFIG_HOME"]) / "tool" / "config.toml").is_file()
    assert "bench" in Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
    assert sorted(p.relative_to(caller_home) for p in caller_home.rglob("*")) == before


def test_windows_home_variables(tmp_path: Path, real_home: Path) -> None:
    """CI run 46065317768: on Windows `~` expands from USERPROFILE (then HOMEDRIVE+HOMEPATH),
    not HOME, and tools keep state in APPDATA/LOCALAPPDATA; all point into the run home."""
    ws = iso.check_workspace(tmp_path / "ws")
    home = ws / "run" / "home"
    env = iso.isolated_env(home, windows=True)
    assert env["USERPROFILE"] == str(home)
    assert env["HOMEDRIVE"] + env["HOMEPATH"] == str(home)
    assert Path(env["APPDATA"]).is_relative_to(home)
    assert Path(env["LOCALAPPDATA"]).is_relative_to(home)
    iso.assert_isolated_env(env, workspace=ws, real_home=real_home, environ={}, windows=True)
    assert "USERPROFILE" not in iso.isolated_env(home, windows=False)
    for key, value in {
        "USERPROFILE": str(real_home),
        "APPDATA": str(real_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "elsewhere"),
    }.items():
        with pytest.raises(iso.IsolationError, match=key):
            iso.assert_isolated_env(
                {**env, key: value}, workspace=ws, real_home=real_home, environ={}, windows=True
            )
    with pytest.raises(iso.IsolationError, match="HOMEPATH"):
        iso.assert_isolated_env(
            {**env, "HOMEPATH": str(real_home)},
            workspace=ws,
            real_home=real_home,
            environ={},
            windows=True,
        )
    posix_only = iso.isolated_env(home, windows=False)
    with pytest.raises(iso.IsolationError, match="USERPROFILE"):
        iso.assert_isolated_env(
            posix_only, workspace=ws, real_home=real_home, environ={}, windows=True
        )
    # the caller's USERPROFILE counts as a real home
    with pytest.raises(iso.IsolationError, match="USERPROFILE"):
        iso.assert_isolated_env(
            {**env, "USERPROFILE": str(ws)},
            workspace=ws,
            real_home=real_home,
            environ={"USERPROFILE": str(ws / "run")},
            windows=True,
        )


def test_refuses_env_reaching_real_home(tmp_path: Path, real_home: Path) -> None:
    ws = iso.check_workspace(tmp_path / "ws")
    good = iso.isolated_env(ws / "run" / "home")
    iso.assert_isolated_env(good, workspace=ws, real_home=real_home, environ={})
    bad_values = {
        "HOME": str(real_home),
        "XDG_CONFIG_HOME": str(real_home / ".config"),
        "GIT_CONFIG_GLOBAL": str(real_home / ".gitconfig"),
        "npm_config_prefix": str(real_home / ".npm-global"),
        "ZDOTDIR": str(real_home),
        "CARGO_HOME": str(tmp_path / "elsewhere"),  # outside the workspace
        "CLAUDE_CONFIG_DIR": str(real_home / ".claude"),
        "XDG_CACHE_HOME": "relative/cache",
    }
    for key, value in bad_values.items():
        with pytest.raises(iso.IsolationError, match=key):
            iso.assert_isolated_env(
                {**good, key: value}, workspace=ws, real_home=real_home, environ={}
            )
    missing = {k: v for k, v in good.items() if k != "PIP_CACHE_DIR"}
    with pytest.raises(iso.IsolationError, match="PIP_CACHE_DIR"):
        iso.assert_isolated_env(missing, workspace=ws, real_home=real_home, environ={})
    with pytest.raises(iso.IsolationError, match="GIT_CONFIG_NOSYSTEM"):
        iso.assert_isolated_env(
            {**good, "GIT_CONFIG_NOSYSTEM": "0"}, workspace=ws, real_home=real_home, environ={}
        )
    # a workspace inside the real home is fine as long as the run home is under it
    inner_ws = iso.check_workspace(real_home / "private" / "bench")
    inner = iso.isolated_env(inner_ws / "run" / "home")
    iso.assert_isolated_env(inner, workspace=inner_ws, real_home=real_home, environ={})
    # but a run home that is (or holds) the real home's dot-state is refused
    for home in (real_home / ".claude" / "x", tmp_path):
        with pytest.raises(iso.IsolationError, match="HOME"):
            iso.assert_isolated_env(
                iso.isolated_env(home), workspace=tmp_path, real_home=real_home, environ={}
            )
