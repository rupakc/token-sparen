"""Plugin hash: the published identity of the executable plugin content (M5 Task 3).

Definition (binding ruling N1): sha256 hex over the concatenation of
``f"{path}\\0{sha256(bytes)}\\n"`` for every hashed file, in sorted repo-relative POSIX path
order. Hashed: ``.claude-plugin/plugin.json``, ``hooks/``, ``skills/``, ``token_sparen/``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts import plugin_hash as ph

ROOT = Path(__file__).resolve().parent.parent

PLUGIN_JSON = b'{"name": "demo", "version": "1.0.0"}\n'
RUN_PY = b'print("hi")\r\n'  # CRLF as stored: hashed raw, never normalised
# Hand-computed once from the two files above (reference serialisation); a change to this
# value means every published hash changes: never edit it to make a test pass.
GOLDEN = "070aa9e39df77cf6c758c3c4d621ee27564b69fa932841b377b7fe5405c8c200"


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _two_file_plugin(root: Path) -> Path:
    _write(root, ".claude-plugin/plugin.json", PLUGIN_JSON)
    _write(root, "hooks/run.py", RUN_PY)
    return root


def _full_plugin(root: Path) -> Path:
    _two_file_plugin(root)
    _write(root, "hooks/hooks.json", b"{}\n")
    _write(root, "skills/token-sparen/SKILL.md", b"# skill\n")
    _write(root, "token_sparen/__init__.py", b'__version__ = "1.0.0"\n')
    _write(root, "token_sparen/params/defaults.toml", b"[compression]\nmin_tokens = 2000\n")
    _write(root, "token_sparen/b/z.py", b"z = 1\n")
    _write(root, "token_sparen/a.py", b"a = 1\n")
    return root


def test_plugin_paths_are_the_ruling() -> None:
    assert ph.PLUGIN_PATHS == (".claude-plugin/plugin.json", "hooks/", "skills/", "token_sparen/")
    assert ph.PLUGIN_FILES == ph.PLUGIN_PATHS


def test_plugin_hash_matches_reference_serialisation(tmp_path: Path) -> None:
    root = _two_file_plugin(tmp_path)
    lines = "".join(
        f"{rel}\0{hashlib.sha256(data).hexdigest()}\n"
        for rel, data in sorted(
            {".claude-plugin/plugin.json": PLUGIN_JSON, "hooks/run.py": RUN_PY}.items()
        )
    )
    assert ph.plugin_hash(root) == hashlib.sha256(lines.encode("utf-8")).hexdigest()
    assert ph.plugin_hash(root) == GOLDEN  # golden: same value on every OS


def test_plugin_hash_is_raw_bytes_not_normalised(tmp_path: Path) -> None:
    root = _two_file_plugin(tmp_path)
    before = ph.plugin_hash(root)
    _write(root, "hooks/run.py", RUN_PY.replace(b"\r\n", b"\n"))
    assert ph.plugin_hash(root) != before


def test_plugin_hash_stable_and_sorted(tmp_path: Path) -> None:
    root = _full_plugin(tmp_path)
    files = ph.plugin_files(root)
    assert files == sorted(files)
    assert files == [
        ".claude-plugin/plugin.json",
        "hooks/hooks.json",
        "hooks/run.py",
        "skills/token-sparen/SKILL.md",
        "token_sparen/__init__.py",
        "token_sparen/a.py",
        "token_sparen/b/z.py",
        "token_sparen/params/defaults.toml",
    ]
    assert all("\\" not in f for f in files)
    first = ph.plugin_hash(root)
    assert first == ph.plugin_hash(root)
    assert len(first) == 64 and int(first, 16) >= 0
    # creation order and mtimes do not matter, only paths and bytes
    other = tmp_path.parent / (tmp_path.name + "-copy")
    for rel in reversed(files):
        _write(other, rel, (root / rel).read_bytes())
    os.utime(other / "hooks/run.py", (1, 1))
    assert ph.plugin_hash(other) == first


def test_plugin_hash_ignores_docs_tests_vcs_and_marketplace_json(tmp_path: Path) -> None:
    root = _full_plugin(tmp_path)
    before = ph.plugin_hash(root)
    noise = {
        ".claude-plugin/marketplace.json": b'{"plugins": []}\n',
        "README.md": b"# readme\n",
        "LICENSE": b"MIT\n",
        "NOTICE": b"notice\n",
        "SECURITY.md": b"security\n",
        "CHANGELOG.md": b"## [1.0.0]\n",
        "docs/evidence/e4.json": b"{}\n",
        "tests/test_x.py": b"x = 1\n",
        ".git/HEAD": b"ref: refs/heads/main\n",
        ".gitignore": b"*.pyc\n",
        ".gitattributes": b"* text=auto\n",
        ".gitmodules": b"",
        "hooks/__pycache__/run.cpython-312.pyc": b"\x00\x01",
        "token_sparen/__pycache__/a.cpython-312.pyc": b"\x00\x02",
        "token_sparen/stray.pyc": b"\x00\x03",
        "skills/.DS_Store": b"\x00",
        "token_sparen/.gitignore": b"x\n",
        "bench/freeze.py": b"x = 1\n",
        "scripts/plugin_hash.py": b"x = 1\n",
    }
    for rel, data in noise.items():
        _write(root, rel, data)
    assert ph.plugin_hash(root) == before
    assert not set(noise) & set(ph.plugin_files(root))


@pytest.mark.parametrize(
    "rel",
    [
        ".claude-plugin/plugin.json",
        "hooks/hooks.json",
        "skills/token-sparen/SKILL.md",
        "token_sparen/__init__.py",
        "token_sparen/params/defaults.toml",
    ],
)
def test_plugin_hash_changes_on_any_plugin_edit_incl_version(tmp_path: Path, rel: str) -> None:
    root = _full_plugin(tmp_path)
    before = ph.plugin_hash(root)
    data = (root / rel).read_bytes()
    _write(root, rel, data.replace(b"1.0.0", b"1.0.1") if b"1.0.0" in data else data + b" ")
    assert ph.plugin_hash(root) != before


def test_plugin_hash_changes_on_added_removed_or_renamed_file(tmp_path: Path) -> None:
    root = _full_plugin(tmp_path)
    before = ph.plugin_hash(root)
    _write(root, "token_sparen/new.py", b"")
    added = ph.plugin_hash(root)
    assert added != before
    (root / "token_sparen/new.py").rename(root / "token_sparen/new2.py")
    assert ph.plugin_hash(root) not in (before, added)
    (root / "token_sparen/new2.py").unlink()
    assert ph.plugin_hash(root) == before


def test_plugin_hash_requires_plugin_json(tmp_path: Path) -> None:
    _write(tmp_path, "hooks/run.py", RUN_PY)
    with pytest.raises(FileNotFoundError, match="plugin.json"):
        ph.plugin_hash(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_plugin_hash_refuses_symlinks(tmp_path: Path) -> None:
    root = _two_file_plugin(tmp_path)
    (root / "hooks" / "link.py").symlink_to(root / "hooks" / "run.py")
    with pytest.raises(ValueError, match="symlink"):
        ph.plugin_files(root)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("linked", [".claude-plugin", "skills", "token_sparen/sub"])
def test_plugin_hash_refuses_symlinked_dirs(tmp_path: Path, linked: str) -> None:
    root = _full_plugin(tmp_path / "plugin")
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "plugin.json").write_bytes(PLUGIN_JSON)
    target = root / linked
    if target.exists():
        for p in sorted(target.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        target.rmdir()
    target.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ph.plugin_files(root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_plugin_hash_refuses_fifo_instead_of_blocking(tmp_path: Path) -> None:
    root = _two_file_plugin(tmp_path)
    os.mkfifo(root / "hooks" / "pipe.py")
    with pytest.raises(ValueError, match="not a regular file"):
        ph.plugin_hash(root)


def test_cli_prints_hash_and_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _two_file_plugin(tmp_path)
    assert ph.main([str(root)]) == 0
    assert capsys.readouterr().out == GOLDEN + "\n"
    assert ph.main(["--files", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [".claude-plugin/plugin.json", "hooks/run.py"]
    assert ph.main([str(tmp_path / "missing")]) == 2
    assert "plugin.json" in capsys.readouterr().err


def test_repo_hash_covers_params_and_not_marketplace() -> None:
    files = ph.plugin_files(ROOT)
    assert "token_sparen/params/defaults.toml" in files
    assert ".claude-plugin/plugin.json" in files
    assert ".claude-plugin/marketplace.json" not in files
    assert not any(f.startswith(("docs/", "tests/", "bench/", "scripts/")) for f in files)
