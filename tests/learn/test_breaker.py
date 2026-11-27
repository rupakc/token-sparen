"""Circuit breaker per (project, point) (Task 22, spec section 7.6)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from token_sparen.adapters.claude import entry
from token_sparen.config import Config, load_config
from token_sparen.events import SessionCtx
from token_sparen.learn import breaker, overlay, regret
from token_sparen.learn.beta import Evidence
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, READ_DEDUPE
from token_sparen.learn.regret import RegretDetector
from token_sparen.store import session

KEY = "0123456789abcdef"
SID = "s-1"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _bpath(home: Path) -> Path:
    return home / "projects" / KEY / "breaker.json"


def _outcomes(
    values: list[float],
    arm: str = "aggressive",
    point: str = "compression",
    bucket: str = FULL_KEY,
) -> list[Any]:
    return [[f"d{i}", point, arm, "pytest", v, bucket] for i, v in enumerate(values)]


def _session(home: Path, sid: str, outcomes: list[Any] | None = None, **extra: Any) -> None:
    doc: dict[str, Any] = dict(extra)
    if outcomes is not None:
        doc["regret_outcomes"] = outcomes
    _write(session.session_path(home, KEY, sid), doc)


def _engine(home: Path, sid: str = SID) -> Engine:
    cfg = load_config(home.parent / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    cfg.data.setdefault("learn", {}).setdefault("floor", {})["medium"] = 0.0
    return Engine(StoreChain(KEY, home, sid, params=cfg.data), cfg, random.Random(1))


def _good_arms(home: Path) -> None:
    good = {
        arm: {
            "savings": Evidence(40.0, 10.0, 50.0, 32.0).to_list(),
            "regret": Evidence(0.0, 50.0, 50.0, 0.0).to_list(),
        }
        for arm in ("light", "standard", "aggressive")
    }
    _write(
        home / "projects" / KEY / "posteriors.json",
        {"version": 2, "points": {"compression": {FULL_KEY: good, "*": good}}},
    )


# --------------------------------------------------------------------------- check
def test_check_ok_and_tripped() -> None:
    tau = 0.05
    ok = "ok"
    assert breaker.check(COMPRESSION, {}, tau=tau) == ok
    assert breaker.check(COMPRESSION, {"ring": [0.0] * 50}, tau=tau) == ok
    # rate > 2 tau is not enough: the regret rate's lower bound must clear tau too
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 6 + [0.0] * 44}, tau=tau) == ok
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 8 + [0.0] * 42}, tau=tau) == ok
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 9 + [0.0] * 41}, tau=tau) == "tripped"
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 5 + [0.0] * 15}, tau=tau) == "tripped"
    # only the last 50 count
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 30 + [0.0] * 50}, tau=tau) == ok
    # too few observations (< breaker_min_obs = 20): no verdict yet
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 19}, tau=tau) == ok
    assert breaker.check(COMPRESSION, {"remaining": 2}, tau=tau) == "tripped"
    assert breaker.check(COMPRESSION, {"ring": "junk", "remaining": "x"}, tau=tau) == ok
    # the lower-bound quantile is a parameter (the review's lcb(0.05) trips at 6 of 50)
    loose = {"learn": {"breaker_lcb_q": 0.05}}
    ring = {"ring": [1.0] * 6 + [0.0] * 44}
    assert breaker.check(COMPRESSION, ring, tau=tau, params=loose) == "tripped"


def test_check_uses_tier_tau_from_params() -> None:
    stats = {"ring": [1.0] * 15 + [0.0] * 35}  # rate 0.3
    assert breaker.check(READ_DEDUPE, stats) == "tripped"  # low tier: tau 0.10
    assert breaker.check(READ_DEDUPE, stats, params={"learn": {"tau": {"low": 0.2}}}) == "ok"


def test_evaluated_only_after_25_new_observations() -> None:
    doc: dict[str, Any] = {}
    bad = [(COMPRESSION, 1.0)] * 20
    assert breaker.apply_session(doc, bad, now=1.0) == ([], [])  # 20 new: not evaluated yet
    assert breaker.apply_session(doc, [(COMPRESSION, 1.0)] * 4, now=2.0) == ([], [])
    assert breaker.apply_session(doc, [(COMPRESSION, 1.0)], now=3.0) == (["compression"], [])


def _sim(p: float, runs: int, seed: int) -> float:
    rng = random.Random(seed)
    trips = 0
    for _ in range(runs):
        doc: dict[str, Any] = {}
        for _ in range(100):  # 1000 trims, 10 per session
            obs = [(COMPRESSION, 1.0 if rng.random() < p else 0.0) for _ in range(10)]
            tripped, _ = breaker.apply_session(doc, obs, now=0.0)
            if tripped:
                trips += 1
                break
    return trips / runs


def test_false_trip_rate_at_tau_and_power_at_three_tau() -> None:
    assert _sim(0.05, 300, 11) <= 0.08  # simulated 0.029 over 4000 runs
    assert _sim(0.15, 100, 12) >= 0.9  # simulated 1.000


def test_static_policy_outcomes_never_trip(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, "s", _outcomes([1.0] * 50, bucket="static"))
    assert overlay.fold("s", KEY, home).tripped == []
    _session(home, "e", _outcomes([1.0] * 50, bucket="error"))
    legacy = [[f"x{i}", "compression", "aggressive", "pytest", 1.0] for i in range(50)]
    _session(home, "l", legacy)  # no bucket recorded: not known to be learned
    overlay.fold("e", KEY, home)
    overlay.fold("l", KEY, home)
    points = _read(_bpath(home)).get("points", {})
    assert not points.get("compression", {}).get("ring")
    assert breaker.notices(KEY, home) == []
    assert overlay.learner_state(KEY, home) == "cold"


# --------------------------------------------------------------------------- end to end
def test_breaker_trips_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    _good_arms(home)
    ctx = SessionCtx(SID, KEY, 1, None, None, session.session_path(home, KEY, SID))
    assert any(_engine(home).decide(COMPRESSION, FULL, ctx).arm != "off" for _ in range(20))

    _session(home, "bad", _outcomes([1.0] * 20 + [0.0] * 30))
    rep = overlay.fold("bad", KEY, home)
    assert rep.tripped == ["compression"]
    state = _read(_bpath(home))["points"]["compression"]
    assert state["remaining"] == 3 and state["ring"] == []
    assert breaker.is_tripped(_read(_bpath(home)), "compression")

    for _ in range(30):
        d = _engine(home).decide(COMPRESSION, FULL, ctx)
        assert d.arm == "off" and d.propensity == 1.0

    # one user message, at the next SessionStart only
    msgs = breaker.notices(KEY, home)
    assert msgs == [
        "token-sparen: compression paused for this project after repeated re-reads; "
        "it will retry in 3 sessions."
    ]
    assert breaker.notices(KEY, home) == []

    for i in range(3):
        assert breaker.is_tripped(_read(_bpath(home)), "compression")
        _session(home, f"later-{i}")
        rep = overlay.fold(f"later-{i}", KEY, home)
    assert rep.reentered == ["compression"]
    assert not breaker.is_tripped(_read(_bpath(home)), "compression")
    # re-entry: the trimming arms' project evidence shrinks x 0.25 (priors weigh in again)
    doc = _read(home / "projects" / KEY / "posteriors.json")
    for bucket in (FULL_KEY, "*"):
        for arm in ("light", "standard", "aggressive"):
            node = doc["points"]["compression"][bucket][arm]
            assert Evidence.from_list(node["savings"]).to_list() == [10.0, 2.5, 12.5, 8.0]
            assert Evidence.from_list(node["regret"]).to_list() == [0.0, 12.5, 12.5, 0.0]
    assert any(_engine(home).decide(COMPRESSION, FULL, ctx).arm != "off" for _ in range(20))


def test_breaker_ignores_safe_arm_and_refold(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, "a", _outcomes([1.0] * 50, arm="off"))
    assert overlay.fold("a", KEY, home).tripped == []
    _session(home, "b", _outcomes([1.0] * 3 + [0.0] * 30))
    overlay.fold("b", KEY, home)
    overlay.fold("b", KEY, home)  # idempotent: the ring does not get the outcomes twice
    ring = _read(_bpath(home))["points"]["compression"]["ring"]
    assert len(ring) == 33


def test_ring_keeps_last_window(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, "a", _outcomes([0.0] * 70))
    overlay.fold("a", KEY, home)
    assert len(_read(_bpath(home))["points"]["compression"]["ring"]) == 50


def test_breaker_notice_at_session_start_hook(tmp_path: Path) -> None:
    import dataclasses

    home = _home(tmp_path)
    _write(_bpath(home), {"points": {"read_dedupe": {"remaining": 3, "ring": []}}})
    env = {"TOKEN_SPAREN_HOME": str(home), "CLAUDE_PROJECT_DIR": str(tmp_path / "proj")}
    ev = dataclasses.replace(
        entry.ADAPTER.parse("SessionStart", {"session_id": SID}, env), project_key=KEY
    )
    token = entry.HOOK_ENV.set(env)
    try:
        act = entry._on_breaker_notice(ev, Config())
        again = entry._on_breaker_notice(ev, Config())
    finally:
        entry.HOOK_ENV.reset(token)
    assert act.user_message is not None and "read_dedupe paused" in act.user_message
    assert again.user_message is None


def test_corrupt_breaker_file_reads_as_ok(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _bpath(home).parent.mkdir(parents=True)
    _bpath(home).write_text("{broken", encoding="utf-8")
    ctx = SessionCtx(SID, KEY, 1, None, None, session.session_path(home, KEY, SID))
    _engine(home).decide(COMPRESSION, FULL, ctx)  # no raise
    assert breaker.notices(KEY, home) == []
    _session(home, "a", _outcomes([0.0]))
    assert overlay.fold("a", KEY, home).staged


# --------------------------------------------------------------------------- outcome recording
def test_regret_settlement_records_outcomes_in_overlay(tmp_path: Path) -> None:
    home = _home(tmp_path)
    env = {"TOKEN_SPAREN_HOME": str(home)}
    cfg = load_config(tmp_path / "proj", env)
    path = session.session_path(home, KEY, SID)
    ctx = SessionCtx(SID, KEY, 1, None, None, path)
    det = RegretDetector(cfg, env)
    det.track(
        ctx,
        "d1",
        point="compression",
        bucket="b",
        arm="standard",
        recovery_id=None,
        command_sig="pytest -q",
        full="",
        kept="",
        ctype="log",
    )
    det._apply(path, regret._Info(), 1, True, True)  # SessionEnd settles everything
    outs = session.read_session(path)["regret_outcomes"]
    assert outs == [["d1", "compression", "standard", "pytest -q", 0.0, "b", "session_end"]]
