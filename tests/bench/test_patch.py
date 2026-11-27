"""Graded-patch extraction (M5 Task 6; Review Focus 2: tool artefacts never graded)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bench import arms as am
from bench import patch as pt

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
GIT += ["-c", "commit.gpgSign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, data: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "testbed"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "src.py", "x = 1\n")
    _write(root, ".claude/settings.json", "{}\n")  # tracked at base: an edit is the agent's
    _write(root, "docs/notes.md", "notes\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _files(patch: str) -> set[str]:
    return {line.split(" b/", 1)[1] for line in patch.splitlines() if line.startswith("diff --git")}


def test_patch_excludes_tool_artefacts_keeps_new_files(repo: tuple[Path, str]) -> None:
    root, base = repo
    excludes = am.load_arms()["baseline"].patch_excludes
    _write(root, "src.py", "x = 2\n")
    _write(root, "newmod.py", "y = 1\n")  # the agent's own new file
    _write(root, "data.bin", b"\x00\x01\x02binary\xff")
    _write(root, ".claude/settings.json", '{"edited": true}\n')
    _write(root, "docs/CLAUDE.md", "nested: not a tool artefact at the root\n")
    for rel in (
        ".token-sparen/outputs/abc.txt",
        ".claude/projects/x.jsonl",
        "CLAUDE.md",
        "RTK.md",
        ".rtk/cache",
        ".context-mode/db",
        ".tokeneconomizer/state.json",
        ".headroom/logs/proxy.log",
        "weird:name*.txt",  # pathspec magic characters stay literal (kept: not excluded)
    ):
        _write(root, rel, "tool\n")
    patch = pt.extract_patch(str(root), base, excludes, run=pt.git_runner())
    assert _files(patch) == {
        "src.py",
        "newmod.py",
        "data.bin",
        ".claude/settings.json",
        "docs/CLAUDE.md",
        "weird:name*.txt",
    }
    assert "GIT binary patch" in patch
    # the patch applies cleanly to the base commit
    check = root.parent / "check"
    _git(root.parent, "clone", "-q", str(root), str(check))
    _git(check, "checkout", "-q", base)
    (root.parent / "p.diff").write_text(patch, encoding="utf-8", newline="\n")
    _git(check, "apply", "--check", str(root.parent / "p.diff"))


def test_patch_is_against_base_even_if_agent_committed(repo: tuple[Path, str]) -> None:
    root, base = repo
    _write(root, "src.py", "x = 3\n")
    _write(root, "CLAUDE.md", "tool\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "agent commit")
    patch = pt.extract_patch(str(root), base, ("CLAUDE.md",), run=pt.git_runner())
    assert _files(patch) == {"src.py"}


def test_patch_empty_when_nothing_changed(repo: tuple[Path, str]) -> None:
    root, base = repo
    _write(root, ".token-sparen/outputs/a.txt", "tool\n")
    assert pt.extract_patch(str(root), base, (".token-sparen/",), run=pt.git_runner()) == ""


def test_excluded_matches_root_relative_dirs_and_files() -> None:
    ex = (".claude/", "CLAUDE.md")
    assert pt.excluded(".claude/a/b", ex) and pt.excluded("CLAUDE.md", ex)
    assert not pt.excluded("docs/CLAUDE.md", ex) and not pt.excluded(".claudex/a", ex)
    assert not pt.excluded("CLAUDE.md.bak", ex)


def test_patch_cli_reads_excludes_from_env(
    repo: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, base = repo
    _write(root, "src.py", "x = 4\n")
    _write(root, "RTK.md", "tool\n")
    env = {"TSB_PATCH_EXCLUDES": "RTK.md\n.rtk/\n"}
    out = tmp_path / "patch.diff"
    rc = pt.main(
        [
            "--repo",
            str(root),
            "--base",
            base,
            "--excludes-env",
            "TSB_PATCH_EXCLUDES",
            "--out",
            str(out),
        ],
        environ=env,
    )
    assert rc == 0
    assert _files(out.read_text(encoding="utf-8")) == {"src.py"}


def test_patch_cli_fails_on_bad_base(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, _ = repo
    out = tmp_path / "patch.diff"
    assert pt.main(["--repo", str(root), "--base", "0" * 40, "--out", str(out)], environ={}) == 1


POSIX = pytest.mark.skipif(os.name != "posix", reason="shell hooks and symlinks")


@POSIX
def test_patch_ignores_hostile_repo_config_and_attributes(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    marks = {n: tmp_path / f"pwned-{n}" for n in ("fsmonitor", "hook", "external", "filter")}
    hooks = root / ".git" / "evil-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "post-index-change", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch '{marks['hook']}'\n", encoding="utf-8")
        (hooks / name).chmod(0o755)
    with (root / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(
            "[core]\n"
            f"\tfsmonitor = touch '{marks['fsmonitor']}'; echo\n"
            f"\thooksPath = {hooks.as_posix()}\n"
            "[diff]\n"
            "\tnoprefix = true\n"
            "\tmnemonicPrefix = true\n"
            f"\texternal = touch '{marks['external']}'; echo\n"
            "[color]\n\tui = always\n\tdiff = always\n"
            f"[filter \"evil\"]\n\tclean = touch '{marks['filter']}'; cat\n\trequired = true\n"
            '[diff "evil"]\n\ttextconv = echo HIJACKED\n'
        )
    _write(root, ".gitattributes", "* filter=evil diff=evil\n")
    _write(root, "src.py", "x = 9\n")
    patch = pt.extract_patch(str(root), base, ("CLAUDE.md",), run=pt.git_runner())
    assert not [n for n, m in marks.items() if m.exists()]
    assert "\x1b[" not in patch and "HIJACKED" not in patch
    assert "diff --git a/src.py b/src.py" in patch
    assert _files(patch) == {"src.py", ".gitattributes"}
    # the agent's repository is not modified by the extraction (its index untouched)
    assert _git(root, "diff", "--cached", "--name-only") == ""
    check = root.parent / "check"
    subprocess.run([*GIT, "clone", "-q", str(root), str(check)], check=True, capture_output=True)
    _git(check, "checkout", "-q", base)
    (root.parent / "p.diff").write_text(patch, encoding="utf-8", newline="\n")
    _git(check, "apply", "--check", str(root.parent / "p.diff"))


@POSIX
def test_patch_cli_writes_atomically_over_a_planted_link(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    _write(root, "src.py", "x = 5\n")
    target = tmp_path / "host-secret"
    target.write_text("secret\n", encoding="utf-8")
    out = tmp_path / "patch.diff"
    out.symlink_to(target)
    assert pt.main(["--repo", str(root), "--base", base, "--out", str(out)], environ={}) == 0
    assert not out.is_symlink() and target.read_text(encoding="utf-8") == "secret\n"
    assert "src.py" in out.read_text(encoding="utf-8")


def test_patch_cli_unwritable_out_fails_cleanly(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    out = tmp_path / "missing-dir" / "patch.diff"
    assert pt.main(["--repo", str(root), "--base", base, "--out", str(out)], environ={}) == 1


def test_git_runner_times_out(repo: tuple[Path, str]) -> None:
    root, base = repo
    with pytest.raises(pt.PatchError):
        pt.extract_patch(str(root), base, (), run=pt.git_runner(timeout=0.0001))


def test_git_runner_env_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/nowhere")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    env = pt.git_env(os.environ)
    assert "GIT_DIR" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
