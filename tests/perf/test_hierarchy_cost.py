"""Cost of context features + hierarchical priors per decision (after the first file read)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from perfutil import best_of, perf_factor

from token_sparen.events import Event, SessionCtx, ToolCall
from token_sparen.learn import features
from token_sparen.learn.hierarchy import StoreChain, prior_for
from token_sparen.learn.policy import COMPRESSION

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
BANDS = ("lt50", "50_75", "75_95", "gt95")
CATS = ("shell", "read", "search", "web", "edit")
SIZES = ("sm", "md", "lg")
CTYPES = ("log", "json", "diff", "text")
PHASES = ("explore", "build", "debug", "mixed")


def _posteriors(home: Path) -> None:
    buckets = {}
    i = 0
    for b in BANDS:
        for c in CATS:
            for s in SIZES:
                for t in CTYPES[:2]:
                    for p in PHASES[:2]:
                        if i >= 200:
                            break
                        key = f"band={b}|cat={c}|size={s}|ctype={t}|phase={p}"
                        m = 0.2 + 0.6 * ((i * 37) % 100) / 100
                        arms = {
                            a: {"savings": [m * 10, (1 - m) * 10, 6], "regret": [0.5, 9.5, 6]}
                            for a in COMPRESSION.arms
                        }
                        buckets[key] = arms
                        i += 1
    assert len(buckets) == 200
    path = home / "projects" / KEY / "posteriors.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "points": {"compression": buckets}}), "utf-8")
    g = home / "global" / "posteriors.json"
    g.parent.mkdir(parents=True)
    g.write_text(json.dumps({"version": 1, "points": {"compression": buckets}}), "utf-8")


def test_features_and_priors_under_one_ms(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _posteriors(home)
    stores = StoreChain(KEY, home)
    ev = Event(
        "claude",
        "post_tool",
        "s-1",
        tmp_path,
        KEY,
        None,
        tool=ToolCall("Bash", "shell", {"command": "x"}, "u"),
        output_text="line\n" * 3000,
    )
    ctx = SessionCtx("s-1", KEY, 3, 0.97, None, tmp_path / "o.json", ("read", "edit") * 5)
    prior_for(COMPRESSION, {}, "light", "savings", stores)  # first read + kappa estimate

    def once() -> object:
        f = features.for_point(COMPRESSION, ev, ctx, "json", tokens=60000)
        return [
            prior_for(COMPRESSION, f, arm, kind, stores)
            for arm in COMPRESSION.arms
            for kind in ("savings", "regret")
        ]

    best, res = best_of(once, 20)
    assert len(res) == 8  # type: ignore[arg-type]
    assert best < 0.001 * perf_factor()
