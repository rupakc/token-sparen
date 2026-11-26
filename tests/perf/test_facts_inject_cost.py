"""Latency gate: SessionStart fact injection with a full fact store (500 active facts)."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from types import MappingProxyType

import pytest
from perfutil import perf_factor

from token_sparen.config import Config
from token_sparen.events import Event
from token_sparen.facts import inject, miners
from token_sparen.facts.sanitize import Fact, fact_id

KEY = "0123456789abcdef"
P95_BUDGET_MS = 20.0


@pytest.mark.perf
def test_session_start_injection_p95(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pdir = home / "projects" / KEY
    pdir.mkdir(parents=True)
    table = {}
    for i in range(miners.MAX_FACTS):
        fact = Fact("command_alias", f"tool{i}", f"python -m tool{i}", frozenset({"a" * 16}))
        table[fact_id(fact)] = {
            "kind": fact.kind,
            "subject": fact.subject,
            "value": fact.value,
            "sessions": ["a" * 16, "b" * 16, "c" * 16],
            "first_seen": 1.0,
            "last_seen": float(i),
            "contradictions": 0,
            "status": "active",
            "effect": [4.0, 1.0, 3.0],
        }
    (pdir / miners.FACTS_FILE).write_text(
        json.dumps({"version": 1, "facts": table}), encoding="utf-8"
    )
    env = {"TOKEN_SPAREN_HOME": str(home)}
    cfg = Config(data={"facts": {"enabled": True}})
    times: list[float] = []
    for n in range(60):
        event = Event(
            host="claude",
            kind="session_start",
            session_id=f"perf-{n}",
            project_dir=tmp_path,
            project_key=KEY,
            transcript_path=None,
            extra=MappingProxyType({"source": "startup"}),
        )
        t0 = time.perf_counter()
        action = inject.on_session_start(event, cfg, env=env)
        times.append((time.perf_counter() - t0) * 1000)
        assert action.inject_context is not None
    p95 = statistics.quantiles(times, n=100)[94]
    assert p95 < P95_BUDGET_MS * perf_factor(), f"p95 {p95:.1f} ms"
