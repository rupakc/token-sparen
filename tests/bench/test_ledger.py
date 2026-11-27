"""E4 ledger (M5 Task 7): header, locked fsynced appends, finals, attempts, invalidation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bench import ledger as lg
from bench.streamjson import PRICE_TABLE_VERSION

KEY = lg.RunKey("c1", "screening", "baseline", 0, "fake__alpha-1")


def _header(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "campaign": "c1",
        "stage": "screening",
        "harness_commit": "0" * 40,
        "plugin_hash": "a" * 64,
        "plugin_ref": "v0.5.0-screen1",
        "pins": {"claude_code": "2.0.0"},
        "model": "sonnet",
        "claude_code_version": "2.0.0",
    }
    return doc | over


def _ledger(tmp_path: Path, **over: Any) -> lg.Ledger:
    return lg.Ledger(tmp_path / "ledger.jsonl", header=_header(**over))


def _end(key: lg.RunKey, status: str, sub: str, at: float, attempt: int = 1) -> dict[str, Any]:
    return {
        "kind": "end",
        "key": key.id,
        "at": at,
        "attempt": attempt,
        "status": status,
        "sub": sub,
    }


def test_run_key_id_round_trip() -> None:
    assert KEY.id == "c1/screening/baseline/0/fake__alpha-1"
    assert lg.RunKey.from_id(KEY.id) == KEY
    with pytest.raises(ValueError):
        lg.RunKey.from_id("c1/screening/baseline/x/iid")
    with pytest.raises(ValueError):
        lg.RunKey("c/1", "s", "a", 0, "i")


def test_header_written_once_with_kind_schema_and_price_table(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    first = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "e4-ledger" and first["schema"] == 1
    assert first["price_table_version"] == PRICE_TABLE_VERSION
    assert led.header["plugin_hash"] == "a" * 64
    again = lg.Ledger(tmp_path / "ledger.jsonl", header=_header())
    assert again.header == led.header
    assert lg.Ledger(tmp_path / "ledger.jsonl").header == led.header


def test_header_mismatch_refused(tmp_path: Path) -> None:
    _ledger(tmp_path)
    with pytest.raises(lg.LedgerError, match="plugin_hash"):
        lg.Ledger(tmp_path / "ledger.jsonl", header=_header(plugin_hash="b" * 64))


def test_header_requires_every_key(tmp_path: Path) -> None:
    bad = _header()
    del bad["model"]
    with pytest.raises(lg.LedgerError, match="model"):
        lg.Ledger(tmp_path / "ledger.jsonl", header=bad)
    with pytest.raises(lg.LedgerError, match="no ledger"):
        lg.Ledger(tmp_path / "missing.jsonl")


def test_not_a_ledger_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.jsonl"
    path.write_text('{"kind": "something"}\n', encoding="utf-8")
    with pytest.raises(lg.LedgerError, match="not an E4 ledger"):
        lg.Ledger(path)


def test_append_validates_kind_and_stamps_time(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    with pytest.raises(lg.LedgerError):
        led.append({"kind": "nonsense"})
    led.append({"kind": "pause", "reason": "disk"})
    (rec,) = led.records()
    assert rec["kind"] == "pause" and isinstance(rec["at"], float)


def test_ledger_file_is_private(tmp_path: Path) -> None:
    import os

    led = _ledger(tmp_path)
    if os.name == "posix":
        assert (led.path.stat().st_mode & 0o777) == 0o600


def test_torn_last_line_ignored_and_next_append_on_new_line(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.append(_end(KEY, "ok", "success", 1.0))
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "end", "key": "trunc')  # a crash mid-write
    reopened = lg.Ledger(led.path)
    assert len(reopened.records()) == 1
    reopened.append({"kind": "pause", "reason": "x"})
    assert [r["kind"] for r in reopened.records()] == ["end", "pause"]


def test_final_is_last_graded_end_never_infra(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    assert led.final(KEY) is None
    led.append(_end(KEY, "infra_fail", "api", 1.0))
    assert led.final(KEY) is None
    led.append(_end(KEY, "agent_fail", "timeout", 2.0, attempt=2))
    final = led.final(KEY)
    assert final is not None and final["status"] == "agent_fail"


def test_invalidate_reopens_key(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.append(_end(KEY, "ok", "success", 1.0))
    led.invalidate(KEY, reason="operator: wrong image")
    assert led.final(KEY) is None
    led.append(_end(KEY, "ok", "success", 3.0, attempt=2))
    assert led.final(KEY) is not None
    other = lg.RunKey("c1", "screening", "baseline", 0, "fake__alpha-2")
    led.append(_end(other, "ok", "success", 10.0))
    led.invalidate(None, reason="api outage", start=9.0, end=11.0)
    assert led.final(other) is None
    assert led.final(KEY) is not None  # at 3.0, outside the range
    with pytest.raises(lg.LedgerError):
        led.invalidate(None, reason="no range")


def test_infra_attempts_cap_and_rate_not_counted(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.append(_end(KEY, "infra_fail", "rate", 1.0))
    led.append(_end(KEY, "infra_fail", "disk", 1.5))
    assert led.infra_attempts(KEY) == 0
    for i, sub in enumerate(("api", "docker")):
        led.append(_end(KEY, "infra_fail", sub, 2.0 + i))
    assert led.infra_attempts(KEY) == 2 and not led.unrunnable(KEY)
    led.append(_end(KEY, "infra_fail", "unknown", 5.0))
    assert led.unrunnable(KEY)
    led.invalidate(KEY, reason="infra fixed")
    assert not led.unrunnable(KEY)


def test_next_attempt_counts_starts(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    assert led.next_attempt(KEY) == 1
    led.append({"kind": "start", "key": KEY.id, "attempt": 1})
    led.append({"kind": "start", "key": KEY.id, "attempt": 2})
    assert led.next_attempt(KEY) == 3


def test_finals_by_task_and_stage(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    k2 = lg.RunKey("c1", "screening", "token-sparen", 0, KEY.instance_id)
    led.append(_end(KEY, "ok", "success", 1.0))
    led.append(_end(k2, "infra_fail", "api", 2.0))
    assert set(led.finals(stage="screening")) == {KEY.id}
    assert set(led.finals(stage="screening", instance_id="nope")) == set()


def test_final_runs_for_grading(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    patch = "diff --git a/x b/x\n"
    (run_dir / "patch.diff").write_text(patch, encoding="utf-8")
    rec = _end(KEY, "ok", "success", 1.0) | {
        "run_dir": "runs/r1",
        "patch_sha256": lg.sha256_text(patch),
    }
    led.append(rec)
    empty = lg.RunKey("c1", "screening", "concise", 0, KEY.instance_id)
    led.append(_end(empty, "agent_fail", "max_turns", 2.0) | {"patch_sha256": lg.sha256_text("")})
    led.append(_end(lg.RunKey("c1", "screening", "x", 0, "i"), "infra_fail", "api", 3.0))
    assert led.campaign == "c1"
    runs = {r.key: r for r in led.final_runs("screening")}
    assert set(runs) == {KEY.id, empty.id}
    assert runs[KEY.id] == lg.FinalRun(KEY.id, "screening", "baseline", 0, KEY.instance_id, patch)
    assert runs[empty.id].patch == ""
    assert led.final_runs("confirmation") == []


def test_final_runs_refuses_a_changed_patch(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "patch.diff").write_text("tampered", encoding="utf-8")
    rec = _end(KEY, "ok", "success", 1.0) | {"run_dir": "r", "patch_sha256": lg.sha256_text("x")}
    led.append(rec)
    with pytest.raises(lg.LedgerError, match="patch"):
        led.final_runs("screening")
