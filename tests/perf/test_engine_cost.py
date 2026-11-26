"""Cost of one engine decision with 4 arms all eligible: posteriors, ladder, Thompson draw,
exact propensity and the locked pending write to the session overlay."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from perfutil import best_of, perf_factor

from token_sparen.config import load_config
from token_sparen.events import SessionCtx
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION
from token_sparen.store import session

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"


def test_decide_under_two_ms(tmp_path: Path) -> None:
    home = tmp_path / "h"
    arms = {
        a: {"savings": [30 * m, 30 * (1 - m), 30], "regret": [0.1, 29.9, 30]}
        for a, m in (("light", 0.3), ("standard", 0.5), ("aggressive", 0.7))
    }
    path = home / "projects" / KEY / "posteriors.json"
    path.parent.mkdir(parents=True)
    doc = {"version": 2, "points": {"compression": {FULL_KEY: arms}}}
    path.write_text(json.dumps(doc), "utf-8")
    cfg = load_config(tmp_path / "p", {"TOKEN_SPAREN_HOME": str(home)})
    stores = StoreChain(KEY, home, "s-1", params=cfg.data)
    eng = Engine(stores, cfg, random.Random(1), env={})
    assert eng.eligible(COMPRESSION, FULL) == ["light", "standard", "aggressive"]
    ctx = SessionCtx("s-1", KEY, 1, None, None, session.session_path(home, KEY, "s-1"))
    eng.decide(COMPRESSION, FULL, ctx)  # first read of the stores, kappa estimates

    best, d = best_of(lambda: eng.decide(COMPRESSION, FULL, ctx), 20)
    assert d.bucket == FULL_KEY  # type: ignore[attr-defined]
    assert best < 0.002 * perf_factor()
