"""OAuth token leak scan and quarantine (M5 Task 5, Review Focus 5)."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
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


PLAIN = "plainSecretValue_" + "x7Y-" * 6  # a token value without the sk-ant-oat prefix


@pytest.mark.parametrize("token", [TOKEN, PLAIN])
@pytest.mark.parametrize("lead", [b"", b"u", b"us"])  # all three base64 alignments
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
def test_scan_finds_base64_forms(
    tmp_path: Path, token: str, lead: bytes, encode: Callable[[bytes], bytes]
) -> None:
    blob = encode(lead + b"Authorization: " + token.encode() + b"\n")
    (tmp_path / "creds").write_bytes(b"header " + blob + b" tail")
    assert leak.scan_for_token([tmp_path], token) == [tmp_path / "creds"]


def test_scan_finds_url_encoded_and_prefix_only_base64(tmp_path: Path) -> None:
    pct = "".join(f"%{b:02X}" for b in PLAIN.encode())
    (tmp_path / "a.log").write_text(f"GET /?t={pct}", encoding="utf-8")
    (tmp_path / "b.log").write_text(f"GET /?t={pct.lower()}", encoding="utf-8")
    assert leak.scan_for_token([tmp_path], PLAIN) == [tmp_path / "a.log", tmp_path / "b.log"]
    # an sk-ant-oat token that is not the bench's, base64-encoded
    other = tmp_path / "other"
    other.mkdir()
    (other / "c.bin").write_bytes(base64.b64encode(b"x" + OTHER.encode()))
    assert leak.scan_for_token([other], PLAIN) == [other / "c.bin"]


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not enforced",
)
def test_scan_fails_closed_on_unreadable_paths(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    locked = run / "state-out" / "locked"
    locked.mkdir()
    (locked / "inside.txt").write_text(TOKEN, encoding="utf-8")
    locked.chmod(0)
    unreadable = run / "secret.txt"
    unreadable.write_text("x", encoding="utf-8")
    unreadable.chmod(0)
    try:
        hits = leak.scan_for_token([run], TOKEN)
        assert locked in hits and unreadable in hits
        marker = leak.quarantine(run, hits)  # deletes through the unreadable directory too
        assert sorted(p.name for p in run.iterdir()) == [leak.QUARANTINE_MARKER]
        assert TOKEN not in marker.read_text(encoding="utf-8")
    finally:
        if locked.exists():
            locked.chmod(0o700)


def test_scan_never_prints_the_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / f"{TOKEN}.txt").write_text(TOKEN, encoding="utf-8")
    hits = leak.scan_for_token([tmp_path], TOKEN)
    leak.quarantine(tmp_path, hits, TOKEN)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    record = (tmp_path / leak.QUARANTINE_MARKER).read_text(encoding="utf-8")
    assert TOKEN not in record and "[REDACTED]" in record
