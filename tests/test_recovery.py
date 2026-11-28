import hashlib
import inspect
import os
import time
from pathlib import Path

import pytest

from token_sparen import recovery
from token_sparen.config import load_config
from token_sparen.recovery import (
    RecoveryRef,
    is_recovery_read,
    marker,
    prune_outputs,
    save_full,
)


def _outputs(project: Path) -> Path:
    return project / ".token-sparen" / "outputs"


def test_marker_exact_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "_SHOW_COMMAND", None)
    monkeypatch.setattr(recovery.shutil, "which", lambda name: f"/bin/{name}")
    ref = save_full(tmp_path, "hello\n")
    expected = (
        f"[token-sparen: kept 3 of 40 lines · full output: {ref.path.resolve()}"
        f" · `token-sparen show {ref.id}`]"
    )
    assert marker(3, 40, ref) == expected
    assert ref.path.is_absolute()


@pytest.mark.parametrize(
    ("osname", "found", "launcher"),
    [
        ("posix", set(), "python3"),
        ("nt", {"py"}, "py -3"),
        ("nt", set(), "python"),
    ],
)
def test_marker_names_the_bundled_cli_without_a_console_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, osname: str, found: set[str], launcher: str
) -> None:
    """v1 review M7: on a plugin-only install there is no ``token-sparen`` on PATH, so the
    marker names the plugin's bundled CLI (the interpreter the hooks use on that platform)."""
    monkeypatch.setattr(recovery, "_SHOW_COMMAND", None)
    monkeypatch.setattr(recovery, "_is_windows", lambda: osname == "nt")
    monkeypatch.setattr(
        recovery.shutil, "which", lambda name: f"/bin/{name}" if name in found else None
    )
    ref = save_full(tmp_path, "hello\n")
    bundled = (Path(recovery.__file__).resolve().parents[1] / "hooks" / "run_cli.py").as_posix()
    assert marker(1, 9, ref).endswith(f' · `{launcher} -I "{bundled}" show {ref.id}`]')


def test_save_idempotent_same_id(tmp_path: Path) -> None:
    a = save_full(tmp_path, "same text")
    assert a.id == hashlib.sha256(b"same text").hexdigest()[:12]
    assert a.path == (_outputs(tmp_path) / f"{a.id}.txt").resolve()
    old = time.time() - 5000
    os.utime(a.path, (old, old))
    inode = a.path.stat().st_ino
    b = save_full(tmp_path, "same text")
    assert a == b
    assert a.path.stat().st_ino == inode  # not rewritten
    assert a.path.stat().st_mtime > old + 1000  # touched
    assert a.path.read_text(encoding="utf-8") == "same text"
    assert [p.name for p in _outputs(tmp_path).iterdir()] == [f"{a.id}.txt"]


def test_roundtrip_surrogates_and_nul(tmp_path: Path) -> None:
    text = "a\x00b\udc80c ünï"
    ref = save_full(tmp_path, text)
    got = ref.path.read_bytes().decode("utf-8", errors="surrogateescape")
    assert got == text
    assert ref.id == hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    # lone surrogates outside the escape range must not raise
    assert save_full(tmp_path, "x\ud800y").path.is_file()


def test_gitignore_created(tmp_path: Path) -> None:
    save_full(tmp_path, "x")
    gi = tmp_path / ".token-sparen" / ".gitignore"
    assert gi.read_text(encoding="utf-8").strip() == "*"
    gi.write_text("custom\n", encoding="utf-8")
    save_full(tmp_path, "y")
    assert gi.read_text(encoding="utf-8") == "custom\n"


def test_prune_by_ttl_and_count(tmp_path: Path) -> None:
    refs = [save_full(tmp_path, f"t{i}") for i in range(6)]
    now = time.time()
    os.utime(refs[0].path, (now - 20 * 86400,) * 2)
    for i in range(1, 6):
        os.utime(refs[i].path, (now - (10 - i),) * 2)
    prune_outputs(tmp_path)
    assert not refs[0].path.exists()
    assert all(r.path.exists() for r in refs[1:])
    prune_outputs(tmp_path, max_files=3)
    left = {r.id for r in refs if r.path.exists()}
    assert left == {refs[3].id, refs[4].id, refs[5].id}  # newest three


def test_prune_by_bytes_and_ignores_foreign(tmp_path: Path) -> None:
    out = _outputs(tmp_path)
    refs = [save_full(tmp_path, str(i) * 100) for i in range(3)]
    now = time.time()
    for i, r in enumerate(refs):
        os.utime(r.path, (now - (10 - i),) * 2)
    foreign = out / "notes.txt"
    foreign.write_text("keep", encoding="utf-8")
    old = now - 99 * 86400
    os.utime(foreign, (old, old))
    (out / "sub").mkdir()
    prune_outputs(tmp_path, max_bytes=150)
    assert [r.path.exists() for r in refs] == [False, False, True]
    assert foreign.exists()
    assert (out / "sub").is_dir()


def test_prune_count_ignores_files_the_byte_cap_removed(tmp_path: Path) -> None:
    refs = [save_full(tmp_path, s) for s in ("a" * 100, "b" * 100, "c" * 10, "d" * 10)]
    now = time.time()
    for i, r in enumerate(refs):
        os.utime(r.path, (now - i,) * 2)  # refs[0] newest
    prune_outputs(tmp_path, max_files=2, max_bytes=150)
    assert [r.path.exists() for r in refs] == [True, False, True, False]


def test_prune_removes_stale_tmp_files_only(tmp_path: Path) -> None:
    ref = save_full(tmp_path, "x")
    out = _outputs(tmp_path)
    stale = out / f"{ref.id}.txt.tmp-123-deadbeef"
    fresh = out / f"{ref.id}.txt.tmp-456-cafebabe"
    for p in (stale, fresh):
        p.write_text("partial", encoding="utf-8")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    prune_outputs(tmp_path)
    assert not stale.exists()
    assert fresh.exists()  # may be a write in flight
    assert ref.path.exists()


def test_prune_missing_dir_is_noop(tmp_path: Path) -> None:
    prune_outputs(tmp_path)


def test_prune_defaults_match_params(tmp_path: Path) -> None:
    sig = inspect.signature(prune_outputs).parameters
    cfg = load_config(tmp_path, {})
    for name in ("ttl_days", "max_files", "max_bytes"):
        assert sig[name].default == cfg.get(f"recovery.{name}")


def test_is_recovery_read(tmp_path: Path) -> None:
    ref = save_full(tmp_path, "abc")
    assert is_recovery_read(tmp_path, ref.path) == ref.id
    assert is_recovery_read(tmp_path, _outputs(tmp_path) / f"{ref.id}.txt") == ref.id
    sneaky = _outputs(tmp_path) / ".." / "outputs" / f"{ref.id}.txt"
    assert is_recovery_read(tmp_path, sneaky) == ref.id
    assert is_recovery_read(tmp_path, _outputs(tmp_path) / "notes.txt") is None
    assert is_recovery_read(tmp_path, _outputs(tmp_path) / "ABCDEF012345.txt") is None
    assert is_recovery_read(tmp_path, tmp_path / "other.txt") is None
    other = tmp_path / "other"
    other.mkdir()
    assert is_recovery_read(other, ref.path) is None
    link = tmp_path / "link.txt"
    link.symlink_to(ref.path)
    assert is_recovery_read(tmp_path, link) == ref.id
    assert isinstance(ref, RecoveryRef)
