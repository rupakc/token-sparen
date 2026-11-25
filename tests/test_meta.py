"""Repository-level meta tests."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import token_sparen
from token_sparen.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert token_sparen.__version__ == data["project"]["version"]


def test_cli_version_prints(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert "0.1.0.dev0" in capsys.readouterr().out


def test_cli_no_args_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_license_owner() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Rupak Chakraborty" in text
    assert "MIT License" in text


def test_denylist_clean() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_denylist.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_denylist_detects_hits(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_denylist as cd
    finally:
        sys.path.pop(0)
    patterns = cd.build_patterns("Secretword, other")
    assert cd.scan_text("a /Use" + "rs/bob/x", patterns)
    assert cd.scan_text("C:" + "\\Use" + "rs\\bob\\x", patterns)
    assert cd.scan_text("has SECRETWORD here", patterns)
    assert not cd.scan_text("clean text, /Use" + "rs/ alone", patterns)
    assert cd.main([], env={"TOKEN_SPAREN_DENYLIST": ""}) == 0
