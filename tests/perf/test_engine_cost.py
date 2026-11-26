"""Cost of the engine's Monte-Carlo propensity (64 replays, 4 arms) per decision."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from perfutil import best_of, perf_factor

from token_sparen.config import load_config
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"


def test_propensity_under_two_ms(tmp_path: Path) -> None:
    home = tmp_path / "h"
    arms = {
        a: {"savings": [10 * m, 10 * (1 - m), 10], "regret": [0.3, 9.7, 10]}
        for a, m in (("light", 0.3), ("standard", 0.5), ("aggressive", 0.7))
    }
    path = home / "projects" / KEY / "posteriors.json"
    path.parent.mkdir(parents=True)
    doc = {"version": 2, "points": {"compression": {FULL_KEY: arms}}}
    path.write_text(json.dumps(doc), "utf-8")
    cfg = load_config(tmp_path / "p", {"TOKEN_SPAREN_HOME": str(home)})
    eng = Engine(StoreChain(KEY, home, params=cfg.data), cfg, random.Random(1))
    cands = eng._candidates(COMPRESSION, FULL)  # posteriors once, as in decide()
    assert len(cands) == 3
    ids = iter(range(10**6))

    def once() -> float:
        return eng._propensity(COMPRESSION, cands, "standard", f"{next(ids):032x}")

    best, p = best_of(once, 20)
    assert 0.0 < p <= 1.0  # type: ignore[operator]
    assert best < 0.002 * perf_factor()
