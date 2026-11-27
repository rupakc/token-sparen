"""Repository-level meta tests."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

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


def _denylist() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_denylist as cd
    finally:
        sys.path.pop(0)
    return cd


_HOME_PATH = "/Use" + "rs/someone/x"  # built at runtime so this file stays clean


def test_denylist_path_mode_scans_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cd = _denylist()
    tree = tmp_path / "site"
    (tree / "deep" / "er").mkdir(parents=True)
    (tree / "index.html").write_text("clean\n", encoding="utf-8")
    (tree / "deep" / "er" / "page.md").write_text(f"ok\nsee {_HOME_PATH}\n", encoding="utf-8")
    (tree / "deep" / "blob.bin").write_bytes(b"\x00\x01" + _HOME_PATH.encode())  # binary: skipped
    (tree / "latin.txt").write_bytes(b"caf\xe9 Secretword\n")  # not UTF-8: still scanned
    (tree / "Secretword-notes.md").write_text("clean\n", encoding="utf-8")  # a file name hit
    env = {"TOKEN_SPAREN_DENYLIST": "secretword"}
    assert cd.main(["--path", str(tree)], env=env) == 1
    out = capsys.readouterr().out
    assert "page.md:2" in out
    assert "latin.txt:1" in out
    assert "Secretword-notes.md (file name)" in out
    assert "blob.bin" not in out
    assert "someone" not in out  # locations only, never the matched text
    for name in ("deep/er/page.md", "latin.txt", "Secretword-notes.md"):
        (tree / name).unlink()
    other = tmp_path / "notes.txt"
    other.write_text("clean\n", encoding="utf-8")
    assert cd.main(["--path", str(tree), "--path", str(other)], env=env) == 0
    other.write_text(_HOME_PATH, encoding="utf-8")
    assert cd.main(["--path", str(tree), "--path", str(other)], env=env) == 1
    assert cd.main(["--path", str(tmp_path / "missing")], env=env) == 2


def test_denylist_path_mode_skips_symlinks_and_git(tmp_path: Path) -> None:
    cd = _denylist()
    outside = tmp_path / "outside.txt"
    outside.write_text(_HOME_PATH, encoding="utf-8")
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    (tree / ".git" / "config").write_text(_HOME_PATH, encoding="utf-8")
    try:
        (tree / "link.txt").symlink_to(outside)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pass
    assert cd.main(["--path", str(tree)], env={}) == 0


def test_denylist_scans_archive_members(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import io
    import tarfile
    import zipfile

    cd = _denylist()
    dist = tmp_path / "dist"
    dist.mkdir()
    whl = dist / "pkg-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("pkg/__init__.py", "x = 1\n")
        zf.writestr("pkg-1.0.0.dist-info/METADATA", f"Name: pkg\n\n{_HOME_PATH}\n")
    sdist = dist / "pkg-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as tf:
        data = b"a\nb\nSecretword\n"
        info = tarfile.TarInfo("pkg-1.0.0/README.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    env = {"TOKEN_SPAREN_DENYLIST": "secretword"}
    assert cd.main(["--path", str(dist)], env=env) == 1  # archives inside a tree
    out = capsys.readouterr().out
    assert "METADATA:3" in out and "README.md:3" in out
    assert cd.main(["--path", str(sdist)], env=env) == 1  # an archive given directly
    assert "README.md:3" in capsys.readouterr().out
    clean = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean, "w") as zf:
        zf.writestr("a.txt", "fine\n")
    assert cd.main(["--path", str(clean)], env=env) == 0
    broken = tmp_path / "broken.whl"
    broken.write_bytes(b"PK not really a zip")
    assert cd.main(["--path", str(broken)], env=env) == 1  # unreadable archive: fail closed


def test_require_private_words_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cd = _denylist()
    (tmp_path / "a.txt").write_text("clean\n", encoding="utf-8")
    for empty in ({}, {"TOKEN_SPAREN_DENYLIST": ""}, {"TOKEN_SPAREN_DENYLIST": " , ,"}):
        assert cd.main(["--require-private-words"], env=empty) == 2
        assert "TOKEN_SPAREN_DENYLIST is empty" in capsys.readouterr().out
        assert cd.main(["--path", str(tmp_path), "--require-private-words"], env=empty) == 2
        capsys.readouterr()
    words = {"TOKEN_SPAREN_DENYLIST": "secretword"}
    assert cd.main(["--path", str(tmp_path), "--require-private-words"], env=words) == 0
    assert cd.main(["--path", str(tmp_path)], env={}) == 0  # without the flag: built-ins only
