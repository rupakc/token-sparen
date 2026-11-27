"""OAuth token leak scan and quarantine (M5 Task 5, Review Focus 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bench import classify as cl
from bench import leak

# synthetic, never a real credential
TOKEN = "sk-ant-oat01-" + "Zz9_" * 12
OTHER = "sk-ant-oat01-" + "Qq7-" * 4


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "state-out" / ".claude" / "projects" / "p").mkdir(parents=True)
    (run / "stream.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    (run / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    return run


def test_leak_scan_quarantines(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    assert leak.scan_for_token([run], TOKEN) == []

    transcript = run / "state-out" / ".claude" / "projects" / "p" / "s.jsonl"
    transcript.write_text(json.dumps({"env": f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}"}), "utf-8")
    other = run / "patch.diff"
    other.write_text(f"+key = '{OTHER}'\n", encoding="utf-8")  # pattern, not the value
    hits = leak.scan_for_token([run], TOKEN)
    assert hits == sorted([transcript, other])

    # a hit is a typed infra failure, before anything else
    status = cl.classify(
        None,
        exit_code=0,
        killed_for_timeout=False,
        backstop_killed=False,
        disk_low=False,
        oom_killed=False,
        load_problems=[],
        live=True,
        leak=bool(hits),
    )
    assert status == ("infra_fail", "leak")

    marker = leak.quarantine(run, hits)
    assert marker == run / leak.QUARANTINE_MARKER
    assert sorted(p.name for p in run.iterdir()) == [leak.QUARANTINE_MARKER]
    record = marker.read_text(encoding="utf-8")
    assert TOKEN not in record and "sk-ant-oat" not in record
    assert json.loads(record)["files"] == sorted(
        ["patch.diff", "state-out/.claude/projects/p/s.jsonl"]
    )
    assert leak.scan_for_token([run], TOKEN) == []


def test_scan_handles_chunks_names_files_and_empty_token(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    # the token straddles the scanner's chunk boundary
    pad = b"x" * (leak.CHUNK - len(TOKEN) // 2)
    big.write_bytes(pad + TOKEN.encode() + b"y" * 100)
    assert leak.scan_for_token([big], TOKEN) == [big]
    named = tmp_path / f"{TOKEN}.txt"
    named.write_text("clean", encoding="utf-8")
    assert named in leak.scan_for_token([tmp_path], TOKEN)
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.txt").write_text("sk-ant-oat01-ab", encoding="utf-8")  # < 8 chars after oat
    (clean / "b.txt").write_text("nothing", encoding="utf-8")
    # an empty token only scans for the pattern (never matches everything)
    assert leak.scan_for_token([clean], "") == []
    assert leak.scan_for_token([clean / "missing"], TOKEN) == []


def test_token_from_env_only() -> None:
    assert leak.token_from_env({leak.TOKEN_ENV: TOKEN}) == TOKEN
    assert leak.token_from_env({}) == ""


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_scan_does_not_follow_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(TOKEN, encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "link.txt").symlink_to(outside)
    (run / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    assert leak.scan_for_token([run], TOKEN) == []
    leak.quarantine(run, [])
    assert outside.exists()  # quarantine never deletes through a link
