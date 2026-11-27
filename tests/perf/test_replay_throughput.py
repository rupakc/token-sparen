"""E1 runner at scale (M4 Task 2): a ~20 MB synthetic corpus generated in tmp. Two workers are
not slower than one, and a worker's peak RSS stays under 300 MB where it can be measured."""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from perfutil import perf_factor

from token_sparen.replay import runner

pytestmark = pytest.mark.perf
TARGET_BYTES = 20 * 1024 * 1024
GROUPS = 8
RSS_BUDGET = 300 * 1024 * 1024
_WORDS = "alpha beta gamma delta lorem ipsum dolor amet copy move sync file slot node".split()


def _rec(kind: str, sid: str, ts: str, n: int, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "sessionId": sid, "timestamp": ts, "uuid": f"u{n}", **extra}


def _session(sid: str, ts: str, rng: random.Random, calls: int = 30) -> list[dict[str, Any]]:
    recs = [_rec("user", sid, ts, 0, message={"role": "user", "content": "build it"})]
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 9}
    for i in range(calls):
        tid, mid = f"{sid}:t{i}", f"{sid}:m{i}"
        cmd = f"make step{i % 5}"
        use = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}
        msg = {"id": mid, "model": "model-x", "role": "assistant", "content": [use]}
        recs.append(_rec("assistant", sid, ts, 2 * i + 1, message={**msg, "usage": usage}))
        text = "\n".join(" ".join(rng.choice(_WORDS) for _ in range(9)) for _ in range(220))
        block = {"type": "tool_result", "tool_use_id": tid, "content": text}
        recs.append(
            _rec(
                "user",
                sid,
                ts,
                2 * i + 2,
                message={"role": "user", "content": [block]},
                toolUseResult={"stdout": text, "stderr": "", "interrupted": False},
            )
        )
    return recs


def _corpus(root: Path) -> int:
    rng = random.Random(5)
    total = i = 0
    while total < TARGET_BYTES:
        day = 1 + i % 28
        ts = f"2026-{10 if i % 2 else 11:02d}-{day if i % 2 else min(day, 25):02d}T10:00:00Z"
        path = root / f"g{i % GROUPS}" / f"s{i:03d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(r) + "\n" for r in _session(f"s{i}", ts, rng))
        path.write_text(data, encoding="utf-8")
        total += len(data.encode())
        i += 1
    return total


def _child_peak_rss() -> int | None:
    try:
        import resource  # noqa: PLC0415 - POSIX only
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)  # Linux reports KiB


def test_two_workers_not_slower_and_bounded_memory(tmp_path: Path) -> None:
    size = _corpus(tmp_path / "corpus")
    assert size >= TARGET_BYTES
    specs = [("compression", {"policy": "standard"})]
    cutoff = date(2026, 11, 1)
    start = time.perf_counter()
    one = runner.run_parallel([tmp_path / "corpus"], specs, jobs=1, cutoff=cutoff)
    t1 = time.perf_counter() - start
    start = time.perf_counter()
    two = runner.run_parallel([tmp_path / "corpus"], specs, jobs=2, cutoff=cutoff)
    t2 = time.perf_counter() - start
    assert one.worker_errors == two.worker_errors == {}
    assert one.by_split == two.by_split
    trimmed = sum(
        row["trimmed"]
        for row in one.by_split["all"]["compression"]["standard"]["per_ctype"].values()
    )
    assert trimmed > 0  # the corpus exercises the compression path, not only the reader
    mb = size / 1e6
    print(f"jobs=1 {mb / t1:.1f} MB/s, jobs=2 {mb / t2:.1f} MB/s ({os.cpu_count()} CPUs)")
    assert t2 <= t1 * perf_factor(), (t1, t2)
    peak = _child_peak_rss()
    if peak is not None:  # the max over every finished child of this process: an upper bound
        assert peak < RSS_BUDGET, peak
