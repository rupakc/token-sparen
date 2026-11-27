"""Opt-in statusLine install/uninstall in the user's Claude Code settings (exact restore)."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from token_sparen import cli
from token_sparen.adapters.claude import settings

LAUNCHER = "/opt/ts home/statusline.sh"  # a space: quoting must hold


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _meta(tmp_path: Path) -> Path:
    return tmp_path / "state" / "statusline_install.json"


def test_install_fresh_and_uninstall_removes_key(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}})
    result = settings.install_statusline(path, LAUNCHER)
    assert result.ok and result.action == "installed"
    data = _read(path)
    assert list(data) == ["model", "permissions", "statusLine"]
    assert data["statusLine"] == {
        "type": "command",
        "command": f"sh {shlex.quote(LAUNCHER)}",
        "padding": 0,
    }
    assert path.read_text(encoding="utf-8").startswith('{\n  "model"')  # indent=2
    backup = path.with_name("settings.json.token-sparen.bak")
    assert _read(backup) == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}
    meta = _read(_meta(tmp_path))
    assert meta["installs"][str(path.resolve())]["had_statusline"] is False

    undo = settings.uninstall_statusline(path)
    assert undo.ok and undo.action == "removed"
    assert _read(path) == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}
    assert str(path.resolve()) not in _read(_meta(tmp_path)).get("installs", {})


def test_install_missing_settings_file(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "settings.json"
    assert settings.install_statusline(path, LAUNCHER).ok
    assert set(_read(path)) == {"statusLine"}
    assert not path.with_name("settings.json.token-sparen.bak").exists()
    assert settings.uninstall_statusline(path).ok
    assert _read(path) == {}


def test_install_composes_existing_and_uninstall_restores_exact(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {"type": "command", "command": "~/bin/my line.sh 'a b'", "padding": 2}
    _write(path, {"statusLine": original, "theme": "dark"})
    result = settings.install_statusline(path, LAUNCHER)
    assert result.action == "composed"
    data = _read(path)
    assert list(data) == ["statusLine", "theme"]  # key order kept
    command = data["statusLine"]["command"]
    assert shlex.split(command) == ["sh", LAUNCHER, "--compose", "--", original["command"]]
    assert data["statusLine"]["padding"] == 2

    assert settings.uninstall_statusline(path).action == "restored"
    restored = _read(path)
    assert restored == {"statusLine": original, "theme": "dark"}
    assert list(restored) == ["statusLine", "theme"]


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {"type": "command", "command": "echo mine"}
    _write(path, {"statusLine": original})
    settings.install_statusline(path, LAUNCHER)
    once = path.read_text(encoding="utf-8")
    again = settings.install_statusline(path, LAUNCHER)
    assert again.ok and again.action == "already"
    assert path.read_text(encoding="utf-8") == once  # not double-composed
    settings.uninstall_statusline(path)
    assert _read(path) == {"statusLine": original}


def test_backup_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"pristine": True})
    settings.install_statusline(path, LAUNCHER)
    settings.uninstall_statusline(path)
    _write(path, {"pristine": False})
    settings.install_statusline(path, LAUNCHER)
    assert _read(path.with_name("settings.json.token-sparen.bak")) == {"pristine": True}


def test_uninstall_preserves_later_edits_to_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {"type": "command", "command": "echo mine", "padding": 1}
    _write(path, {"statusLine": original, "model": "a"})
    settings.install_statusline(path, LAUNCHER)
    data = _read(path)
    data["model"] = "b"  # the user edits settings after install
    data["env"] = {"X": "1"}
    _write(path, data)
    settings.uninstall_statusline(path)
    assert _read(path) == {"statusLine": original, "model": "b", "env": {"X": "1"}}


def test_uninstall_leaves_statusline_changed_by_user(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo old"}})
    settings.install_statusline(path, LAUNCHER)
    mine = {"type": "command", "command": "echo newer"}
    _write(path, {"statusLine": mine})
    result = settings.uninstall_statusline(path)
    assert result.ok and result.action == "unchanged"
    assert _read(path) == {"statusLine": mine}


def test_uninstall_without_record_recovers_composed_original(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo mine", "padding": 3}})
    settings.install_statusline(path, LAUNCHER)
    _meta(tmp_path).unlink()
    assert settings.uninstall_statusline(path).action == "not-installed"  # launcher unknown
    assert settings.uninstall_statusline(path, launcher=LAUNCHER).action == "restored"
    assert _read(path) == {"statusLine": {"type": "command", "command": "echo mine", "padding": 3}}


def test_uninstall_when_not_installed(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo mine"}})
    before = path.read_text(encoding="utf-8")
    result = settings.uninstall_statusline(path)
    assert result.ok and result.action == "not-installed"
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", ""])
def test_refuses_invalid_json(tmp_path: Path, text: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    for fn in (
        lambda: settings.install_statusline(path, LAUNCHER),
        lambda: settings.uninstall_statusline(path),
    ):
        result = fn()
        assert not result.ok and result.action == "error"
        assert path.read_text(encoding="utf-8") == text
    assert not path.with_name("settings.json.token-sparen.bak").exists()
    assert not _meta(tmp_path).exists()


def test_install_replaces_non_command_statusline_and_restores(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    odd = {"type": "static", "text": "hi"}
    _write(path, {"statusLine": odd})
    assert settings.install_statusline(path, LAUNCHER).action == "installed"
    settings.uninstall_statusline(path)
    assert _read(path) == {"statusLine": odd}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")
def test_symlink_and_mode_preserved(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    _write(real, {"a": 1})
    os.chmod(real, 0o600)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    settings.install_statusline(link, LAUNCHER)
    assert link.is_symlink()
    assert "statusLine" in _read(real)
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    settings.uninstall_statusline(link)
    assert link.is_symlink() and _read(real) == {"a": 1}


def test_settings_path_respects_claude_config_dir(tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cc"), "HOME": str(tmp_path / "h")}
    assert settings.settings_path(env) == tmp_path / "cc" / "settings.json"
    assert settings.settings_path({"HOME": str(tmp_path / "h")}) == (
        tmp_path / "h" / ".claude" / "settings.json"
    )


def test_cli_install_and_uninstall_with_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cc = tmp_path / "cc"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    _write(cc / "settings.json", {"keep": 1})
    assert cli.main(["statusline", "install"]) == 0
    data = _read(cc / "settings.json")
    launcher = tmp_path / "state" / "statusline.sh"
    assert shlex.split(data["statusLine"]["command"]) == ["sh", str(launcher)]
    text = launcher.read_text(encoding="utf-8")
    assert "FALLBACK_ROOT=''" not in text  # the package root is baked in as the last fallback
    assert "installed" in capsys.readouterr().out
    assert cli.main(["statusline", "uninstall"]) == 0
    assert _read(cc / "settings.json") == {"keep": 1}
    assert not (tmp_path / "userhome" / ".claude").exists()  # the real home was never touched


def test_cli_install_refuses_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "s.json"
    target.write_text("{oops", encoding="utf-8")
    assert cli.main(["statusline", "install", "--settings", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "{oops"
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["statusline", "render"]) == 0
    assert capsys.readouterr().out == "⛁ ts\n"


def test_uninstall_recognises_recorded_launcher(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo mine"}})
    settings.install_statusline(path, LAUNCHER)
    result = settings.uninstall_statusline(path, launcher="/elsewhere/statusline.sh")
    assert result.action == "restored"
    assert _read(path) == {"statusLine": {"type": "command", "command": "echo mine"}}


# --------------------------------------------------------------------------- fix round 1


def test_uninstall_keeps_user_edit_inside_compose_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo mine", "padding": 1}})
    settings.install_statusline(path, LAUNCHER)
    data = _read(path)
    data["statusLine"]["command"] = settings.composed_command(LAUNCHER, "echo edited")
    data["statusLine"]["padding"] = 4
    _write(path, data)
    assert settings.uninstall_statusline(path).action == "restored"
    assert _read(path) == {
        "statusLine": {"type": "command", "command": "echo edited", "padding": 4}
    }


@pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
def test_refuses_non_finite_constants(tmp_path: Path, const: str) -> None:
    path = tmp_path / "settings.json"
    text = '{"x": ' + const + "}"
    path.write_text(text, encoding="utf-8")
    for fn in (
        lambda: settings.install_statusline(path, LAUNCHER),
        lambda: settings.uninstall_statusline(path),
    ):
        result = fn()
        assert not result.ok and result.action == "error"
    assert path.read_text(encoding="utf-8") == text
    assert not _meta(tmp_path).exists()


def test_write_is_strict_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        settings._write(tmp_path / "s.json", {"x": float("nan")})
    assert not (tmp_path / "s.json").exists()


needs_perms = pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="POSIX permissions (not root)",
)


@needs_perms
def test_install_write_failure_rolls_back_record(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    path = cfg / "settings.json"
    _write(path, {"a": 1})
    (cfg / "settings.json.token-sparen.bak").write_text("{}", encoding="utf-8")  # backup exists
    before = path.read_text(encoding="utf-8")
    os.chmod(cfg, 0o500)
    try:
        result = settings.install_statusline(path, LAUNCHER)
    finally:
        os.chmod(cfg, 0o700)
    assert not result.ok and result.action == "error"
    assert "Traceback" not in result.message
    assert path.read_text(encoding="utf-8") == before
    meta = _read(_meta(tmp_path)) if _meta(tmp_path).exists() else {}
    assert str(path.resolve()) not in meta.get("installs", {})


@needs_perms
def test_install_backup_failure_is_an_error(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    path = cfg / "settings.json"
    _write(path, {"a": 1})
    os.chmod(cfg, 0o500)
    try:
        result = settings.install_statusline(path, LAUNCHER)
    finally:
        os.chmod(cfg, 0o700)
    assert not result.ok and result.action == "error"
    assert _read(path) == {"a": 1}
    assert not _meta(tmp_path).exists()


@needs_perms
def test_install_record_failure_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"a": 1})
    state = tmp_path / "state"
    state.mkdir()
    os.chmod(state, 0o500)
    try:
        result = settings.install_statusline(path, LAUNCHER)
    finally:
        os.chmod(state, 0o700)
    assert not result.ok and result.action == "error"
    assert _read(path) == {"a": 1}


@needs_perms
def test_uninstall_write_failure_keeps_record(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    path = cfg / "settings.json"
    _write(path, {"statusLine": {"type": "command", "command": "echo mine"}})
    settings.install_statusline(path, LAUNCHER)
    before = path.read_text(encoding="utf-8")
    os.chmod(cfg, 0o500)
    try:
        result = settings.uninstall_statusline(path)
    finally:
        os.chmod(cfg, 0o700)
    assert not result.ok and result.action == "error"
    assert path.read_text(encoding="utf-8") == before
    assert str(path.resolve()) in _read(_meta(tmp_path))["installs"]
    assert settings.uninstall_statusline(path).action == "restored"  # retry works later


def test_concurrent_edit_is_reapplied_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"a": 1})
    calls = []

    def edit_once(target: Path) -> None:
        if not calls:
            calls.append(1)
            _write(target, {"a": 1, "user": "edit"})

    monkeypatch.setattr(settings, "_before_replace", edit_once)
    result = settings.install_statusline(path, LAUNCHER)
    assert result.ok
    data = _read(path)
    assert data["user"] == "edit" and "statusLine" in data


def test_persistent_concurrent_edits_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"a": 1})
    n = [0]

    def edit_always(target: Path) -> None:
        n[0] += 1
        _write(target, {"a": 1, "edit": n[0]})

    monkeypatch.setattr(settings, "_before_replace", edit_always)
    result = settings.install_statusline(path, LAUNCHER)
    assert not result.ok and result.action == "error"
    data = _read(path)
    assert "statusLine" not in data and data["edit"] == 2
    meta = _read(_meta(tmp_path)) if _meta(tmp_path).exists() else {}
    assert str(path.resolve()) not in meta.get("installs", {})


@needs_perms
def test_cli_launcher_write_failure_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "s.json"
    _write(target, {"a": 1})
    state = tmp_path / "state"
    state.mkdir()
    os.chmod(state, 0o500)
    try:
        assert cli.main(["statusline", "install", "--settings", str(target)]) == 1
    finally:
        os.chmod(state, 0o700)
    assert "cannot write the launcher" in capsys.readouterr().err
    assert _read(target) == {"a": 1}


def test_same_size_edit_with_restored_mtime_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-length edit whose mtime is put back (coarse clocks) must not be clobbered."""
    path = tmp_path / "settings.json"
    path.write_text('{"a": "1"}\n', encoding="utf-8")
    orig = path.stat().st_mtime_ns
    calls = []

    def sneaky(target: Path) -> None:
        if calls:
            return
        calls.append(1)
        target.write_text('{"a": "2"}\n', encoding="utf-8")  # same length, other content
        os.utime(target, ns=(orig, orig))

    monkeypatch.setattr(settings, "_before_replace", sneaky)
    stamp = settings._load_stamped(path)[2]
    with pytest.raises(settings._Changed):
        settings._write(path, {"a": "x"}, stamp)
    assert _read(path) == {"a": "2"}


# --------------------------------------------------------------------------- tool pruning writer
def test_deny_journal_runs_before_the_write_with_created_flags(tmp_path: Path) -> None:
    path = tmp_path / "p" / ".claude" / "settings.local.json"
    seen: list[tuple[list[str], dict[str, bool], bool]] = []

    def journal(new: list[str], created: dict[str, bool]) -> None:
        seen.append((new, created, path.exists()))

    res = settings.add_deny_rules(path, ["mcp__a", "mcp__a"], journal=journal)
    assert res.ok and res.changed == ["mcp__a"]
    assert seen == [(["mcp__a"], res.created, False)]  # recorded before the file existed
    assert res.created == {"dir": True, "file": True, "permissions": True, "deny": True}
    seen.clear()
    assert settings.add_deny_rules(path, ["mcp__a"], journal=journal).changed == []
    assert seen == []  # nothing to write: nothing journaled


def test_journal_failure_means_no_write(tmp_path: Path) -> None:
    path = tmp_path / "p" / ".claude" / "settings.local.json"

    def broken(new: list[str], created: dict[str, bool]) -> None:
        raise OSError("disk full")

    res = settings.add_deny_rules(path, ["mcp__a"], journal=broken)
    assert not res.ok and not path.exists()


def test_git_exposure_refuses_location_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "_git", lambda *a: (0, b""))  # git would say "ignored"
    assert settings.git_exposure(tmp_path) is None
    for var in settings.GIT_LOCATION_VARS:
        monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
        assert settings.git_exposure(tmp_path) == "unknown", var
        monkeypatch.delenv(var)
