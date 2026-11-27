"""CLI/plugin version skew: `doctor` compares the plugin's version with the `token-sparen` on
PATH, and the skill runs the plugin's bundled CLI first."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import token_sparen.cli as cli
from token_sparen.cli import main

ROOT = Path(__file__).resolve().parents[1]
BOOT = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py" <args>'


def _plugin(root: Path, version: str) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "token-sparen", "version": version}), encoding="utf-8"
    )
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_bytes((ROOT / "hooks" / "hooks.json").read_bytes())
    (root / "hooks" / "run.py").write_text("", encoding="utf-8")
    return root


def _path_cli(bindir: Path, version: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "token-sparen"
    exe.write_text(f'#!/bin/sh\necho "token-sparen {version}"\n', encoding="utf-8")
    os.chmod(exe, 0o755)


@pytest.fixture
def doctor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_round_trip", lambda root: (True, "skipped in this test"))
    return tmp_path


def test_plugin_version_reads_the_manifest(tmp_path: Path) -> None:
    assert cli.plugin_version(_plugin(tmp_path / "p", "1.2.3")) == "1.2.3"
    assert cli.plugin_version(tmp_path / "missing") is None
    bad = tmp_path / "bad" / ".claude-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text("[1]", encoding="utf-8")
    assert cli.plugin_version(tmp_path / "bad") is None


def test_skew_lines() -> None:
    rows = cli.version_skew("1.0.0", "0.4.0", "/x/token-sparen")
    assert (
        "!",
        "version skew",
        "CLI 0.4.0 differs from plugin 1.0.0; the plugin's hooks use 1.0.0",
    ) in rows
    assert not [r for r in cli.version_skew("1.0.0", "1.0.0", "/x") if r[1] == "version skew"]
    none = cli.version_skew("1.0.0", None, None)
    assert none[-1][1] == "CLI on PATH" and "bundled" in none[-1][2]
    unknown = cli.version_skew(None, "1.0.0", "/x/token-sparen")
    assert unknown[0][1] == "plugin version" and "unknown" in unknown[0][2]
    assert not [r for r in unknown if r[1] == "version skew"]


def test_path_cli_version_parses_and_fails_soft(tmp_path: Path) -> None:
    assert cli.path_cli_version(lambda _: None) == (None, None)
    if sys.platform != "win32":
        _path_cli(tmp_path / "bin", "0.9.1")
        exe = str(tmp_path / "bin" / "token-sparen")
        assert cli.path_cli_version(lambda _: exe) == ("0.9.1", exe)
    assert cli.path_cli_version(lambda _: str(tmp_path / "nope")) == (None, None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub on PATH")
def test_doctor_reports_cli_plugin_skew(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _plugin(doctor_env / "plug", "1.0.0")
    _path_cli(doctor_env / "bin", "0.4.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("PATH", str(doctor_env / "bin") + os.pathsep + os.environ["PATH"])
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "plugin version: 1.0.0" in out
    assert "CLI on PATH: 0.4.0" in out
    assert (
        "! version skew: CLI 0.4.0 differs from plugin 1.0.0; the plugin's hooks use 1.0.0" in out
    )
    assert rc == 0, out  # a warning, not a failure: the hooks never use the PATH CLI
    # the same version: no warning
    _path_cli(doctor_env / "bin", "1.0.0")
    main(["doctor"])
    assert "version skew" not in capsys.readouterr().out


def _running_section() -> str:
    text = (ROOT / "skills" / "token-sparen" / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    return body.split("## Running the CLI", 1)[1].split("\n## ", 1)[0]


def test_skill_prefers_bundled_cli() -> None:
    section = _running_section()
    assert BOOT in section and "`token-sparen <args>`" in section
    assert section.index(BOOT) < section.index("`token-sparen <args>`")
    flat = " ".join(section.split())
    assert "plugin's own version" in flat
    assert "Only if that cannot run" in flat


def test_skill_says_claude_code_only_without_cli() -> None:
    flat = " ".join(_running_section().split())
    assert "If neither runs" in flat
    assert "token-sparen works in Claude Code only" in flat
    assert "run nothing else" in flat
