"""`scripts/push_tree_guard.py`: `release/claude` only ever moves to a newer version (M3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import push_tree_guard as guard


def _manifest(version: object) -> str:
    return json.dumps({"name": "token-sparen", "version": version})


def test_first_release_passes() -> None:
    assert guard.downgrade_problems("v1.0.0", None) == []


@pytest.mark.parametrize(
    ("current", "tag"),
    [("1.0.0", "v1.0.1"), ("1.0.9", "v1.1.0"), ("1.9.9", "v2.0.0"), ("1.0.9", "v1.0.10")],
)
def test_newer_tag_passes(current: str, tag: str) -> None:
    assert guard.downgrade_problems(tag, _manifest(current)) == []


@pytest.mark.parametrize(
    ("current", "tag"),
    [("1.0.2", "v1.0.1"), ("1.0.1", "v1.0.1"), ("2.0.0", "v1.9.9"), ("1.0.10", "v1.0.9")],
)
def test_same_or_older_tag_refused(current: str, tag: str) -> None:
    (problem,) = guard.downgrade_problems(tag, _manifest(current))
    assert "downgrade" in problem and current in problem


@pytest.mark.parametrize("current", ["{bad", _manifest("1.0.1.dev0"), _manifest(None), "[]"])
def test_unreadable_branch_version_fails_closed(current: str) -> None:
    assert guard.downgrade_problems("v9.0.0", current)


def test_same_version_only_for_the_identical_seeded_plugin() -> None:
    """Review I2: the first release seeds release/claude with its measured tree; the release's
    push-tree may then add the release commit for that same plugin, never a different one."""
    seeded = _manifest("1.0.0")
    assert (
        guard.downgrade_problems("v1.0.0", seeded, current_hash="a" * 64, new_hash="a" * 64) == []
    )
    (problem,) = guard.downgrade_problems(
        "v1.0.0", seeded, current_hash="a" * 64, new_hash="b" * 64
    )
    assert "different plugin" in problem
    assert guard.downgrade_problems("v1.0.0", seeded, current_hash="a" * 64)  # no new hash
    # an older version is refused even when the hashes happen to be equal
    assert guard.downgrade_problems("v0.9.0", seeded, current_hash="a" * 64, new_hash="a" * 64)


def test_bad_tag_refused() -> None:
    assert guard.downgrade_problems("v1.0.0-rc1", None)


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text(_manifest("1.0.1"), encoding="utf-8")
    assert guard.main(["--tag", "v1.0.2", "--current-manifest", str(manifest)]) == 0
    assert guard.main(["--tag", "v1.0.1", "--current-manifest", str(manifest)]) == 1
    assert "::error::" in capsys.readouterr().err
    assert guard.main(["--tag", "v1.0.0"]) == 0
    same = ["--current-hash", "c" * 64, "--new-hash", "c" * 64]
    assert guard.main(["--tag", "v1.0.1", "--current-manifest", str(manifest), *same]) == 0
    assert guard.main(["--tag", "v1.0.2", "--current-manifest", str(tmp_path / "missing")]) == 1
