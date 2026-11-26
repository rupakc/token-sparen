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
