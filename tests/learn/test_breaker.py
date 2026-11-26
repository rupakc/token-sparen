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
    values: list[float], arm: str = "aggressive", point: str = "compression"
) -> list[Any]:
    return [[f"d{i}", point, arm, "pytest", v] for i, v in enumerate(values)]


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
    assert breaker.check(COMPRESSION, {}, tau=tau) == "ok"
    assert breaker.check(COMPRESSION, {"ring": [0.0] * 50}, tau=tau) == "ok"
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 5 + [0.0] * 45}, tau=tau) == "ok"  # = 2 tau
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 6 + [0.0] * 44}, tau=tau) == "tripped"
    # only the last 50 count
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 30 + [0.0] * 50}, tau=tau) == "ok"
    # too few observations: no verdict yet
    assert breaker.check(COMPRESSION, {"ring": [1.0] * 5}, tau=tau) == "ok"
    assert breaker.check(COMPRESSION, {"remaining": 2}, tau=tau) == "tripped"
    assert breaker.check(COMPRESSION, {"ring": "junk", "remaining": "x"}, tau=tau) == "ok"


def test_check_uses_tier_tau_from_params() -> None:
    stats = {"ring": [1.0] * 15 + [0.0] * 35}  # rate 0.3
    assert breaker.check(READ_DEDUPE, stats) == "tripped"  # low tier: tau 0.10
    assert breaker.check(READ_DEDUPE, stats, params={"learn": {"tau": {"low": 0.2}}}) == "ok"


# --------------------------------------------------------------------------- end to end
def test_breaker_trips_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    _good_arms(home)
    ctx = SessionCtx(SID, KEY, 1, None, None, session.session_path(home, KEY, SID))
    assert any(_engine(home).decide(COMPRESSION, FULL, ctx).arm != "off" for _ in range(20))

    _session(home, "bad", _outcomes([1.0] * 10 + [0.0] * 40))
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
    # re-entry: non-safe project evidence of the point is gone; designed priors apply again
    doc = _read(home / "projects" / KEY / "posteriors.json")
    for bucket in doc["points"].get("compression", {}).values():
        assert set(bucket) <= {"off"}
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
    env = {"TOKEN_SPAREN_HOME": str(home), "CLAUDE_PROJECT_DIR": str(tmp_path)}
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
    assert outs == [["d1", "compression", "standard", "pytest -q", 0.0]]
