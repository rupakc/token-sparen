"""Frozen read-only plugin per stage (ruling M5-B3, M6-B2)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from bench import freeze as fz
from scripts.plugin_hash import plugin_hash

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
GIT += ["-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", "-c", "init.defaultBranch=main"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, ".claude-plugin/plugin.json", b'{"name": "demo", "version": "1.0.0"}\n')
    _write(root, ".claude-plugin/marketplace.json", b"{}\n")
    _write(root, "hooks/hooks.json", b"{}\n")
    _write(root, "hooks/run.py", b"print(1)\n")
    _write(root, "skills/demo/SKILL.md", b"# skill\n")
    _write(root, "token_sparen/__init__.py", b'__version__ = "1.0.0"\n')
    _write(root, "token_sparen/params/defaults.toml", b"[x]\ny = 1\n")
    _write(root, "README.md", b"# readme\n")
    _write(root, "tests/test_x.py", b"x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "tag", "-a", "v1.0.0-rc1", "-m", "rc")
    _git(root, "tag", "light")
    return root


def _modes(root: Path) -> list[tuple[str, int]]:
    out = [(".", stat.S_IMODE(root.stat().st_mode))]
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            out.append((path.relative_to(root).as_posix(), stat.S_IMODE(path.stat().st_mode)))
    return out


def test_freeze_read_only_and_hash_matches(repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "frozen"
    frozen = fz.freeze(repo, "v1.0.0-rc1", dest)
    assert frozen.path == dest
    assert frozen.ref == "v1.0.0-rc1"
    assert frozen.commit == _git(repo, "rev-parse", "HEAD")  # peeled annotated tag
    assert frozen.plugin_hash == plugin_hash(repo) == plugin_hash(dest)
    shipped = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert shipped == [
        ".claude-plugin/plugin.json",
        "hooks/hooks.json",
        "hooks/run.py",
        "skills/demo/SKILL.md",
        "token_sparen/__init__.py",
        "token_sparen/params/defaults.toml",
    ]
    if os.name != "nt":
        for rel, mode in _modes(dest):
            expected = 0o555 if (dest / rel).is_dir() else 0o444
            assert mode == expected, rel
    fz.verify(frozen)


def test_freeze_takes_the_ref_not_the_working_tree(repo: Path, tmp_path: Path) -> None:
    tagged = plugin_hash(repo)
    _write(repo, "token_sparen/__init__.py", b'__version__ = "9.9.9"\n')  # uncommitted edit
    frozen = fz.freeze(repo, "v1.0.0-rc1", tmp_path / "frozen")
    assert frozen.plugin_hash == tagged != plugin_hash(repo)


def test_freeze_accepts_lightweight_tag_and_refs_tags_prefix(repo: Path, tmp_path: Path) -> None:
    assert fz.freeze(repo, "light", tmp_path / "a").commit == _git(repo, "rev-parse", "HEAD")
    assert fz.freeze(repo, "refs/tags/v1.0.0-rc1", tmp_path / "b").ref == "refs/tags/v1.0.0-rc1"


def test_freeze_refuses_untagged_ref_unless_dry_run(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    for ref in ("HEAD", "main", head):
        with pytest.raises(fz.FreezeError, match="not a tag"):
            fz.freeze(repo, ref, tmp_path / "refused")
        assert not (tmp_path / "refused").exists()
    frozen = fz.freeze(repo, "HEAD", tmp_path / "dry", dry_run=True)
    assert frozen.commit == head
    assert frozen.plugin_hash == plugin_hash(repo)


def test_freeze_refuses_unknown_ref_and_existing_dest(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(fz.FreezeError, match="unknown ref"):
        fz.freeze(repo, "no-such-ref", tmp_path / "x", dry_run=True)
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "file").write_text("x", encoding="utf-8")
    with pytest.raises(fz.FreezeError, match="exists"):
        fz.freeze(repo, "v1.0.0-rc1", dest)


def test_freeze_refuses_ref_missing_plugin_json(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "hooks/run.py", b"x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "tag", "t")
    with pytest.raises(fz.FreezeError):
        fz.freeze(root, "t", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_verify_detects_change(repo: Path, tmp_path: Path) -> None:
    frozen = fz.freeze(repo, "v1.0.0-rc1", tmp_path / "frozen")
    target = frozen.path / "token_sparen" / "__init__.py"
    os.chmod(target, 0o644)
    target.write_bytes(b'__version__ = "1.0.1"\n')
    os.chmod(target, 0o444)
    with pytest.raises(fz.FrozenPluginChanged, match="hash"):
        fz.verify(frozen)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_verify_detects_writable_tree(repo: Path, tmp_path: Path) -> None:
    frozen = fz.freeze(repo, "v1.0.0-rc1", tmp_path / "frozen")
    os.chmod(frozen.path / "hooks", 0o755)
    with pytest.raises(fz.FrozenPluginChanged, match="writable"):
        fz.verify(frozen)


def test_verify_detects_missing_tree(repo: Path, tmp_path: Path) -> None:
    frozen = fz.freeze(repo, "v1.0.0-rc1", tmp_path / "frozen")
    gone = fz.FrozenPlugin(tmp_path / "gone", frozen.ref, frozen.commit, frozen.plugin_hash)
    with pytest.raises(fz.FrozenPluginChanged, match="missing"):
        fz.verify(gone)


def test_wheel_has_no_bench() -> None:
    import sys
    import tomllib

    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    packages = pyproject["tool"]["setuptools"]["packages"]
    assert not any(p == "bench" or p.startswith("bench.") for p in packages)
    sys.path.insert(0, str(root / "scripts"))
    try:
        import check_wheel
    finally:
        sys.path.pop(0)
    names = {"token_sparen/__init__.py", "bench/__init__.py", "bench/e4.schema.json"}
    assert check_wheel.forbidden_entries(names) == ["bench/__init__.py", "bench/e4.schema.json"]
    assert check_wheel.forbidden_entries({"token_sparen/cli.py", "x.dist-info/METADATA"}) == []
    assert "forbidden_entries(names)" in (root / "scripts" / "check_wheel.py").read_text(
        encoding="utf-8"
    )
