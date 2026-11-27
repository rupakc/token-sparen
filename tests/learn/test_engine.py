from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import SessionCtx
from token_sparen.learn import engine as engine_mod
from token_sparen.learn.beta import Beta, Evidence
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, READ_DEDUPE, DecisionPoint, StaticPolicy
from token_sparen.store import session

KEY = "0123456789abcdef"
SID = "s-1"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"
HIGH = DecisionPoint("hp_test", ("off", "on"), safe_arm="off", tier="high")


def _ev(m: float, mass: float = 50.0, n: float = 50.0) -> Evidence:
    return Evidence(m * mass, (1 - m) * mass, n, m * m * mass)


def _entry(sav: Evidence, reg: Evidence) -> dict[str, Any]:
    return {"savings": sav.to_list(), "regret": reg.to_list()}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _project(home: Path, points: dict[str, Any]) -> None:
    _write(home / "projects" / KEY / "posteriors.json", {"version": 2, "points": points})


def _overlay(home: Path) -> Path:
    return session.session_path(home, KEY, SID)


def _pending_doc(home: Path) -> dict[str, Any]:
    from token_sparen.learn import engine as engine_mod

    return json.loads(engine_mod.pending_path(_overlay(home)).read_text("utf-8"))


def _ctx(home: Path) -> SessionCtx:
    return SessionCtx(SID, KEY, 1, None, None, _overlay(home))


def _cfg(home: Path, extra: dict[str, Any] | None = None) -> Config:
    cfg = load_config(home.parent / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    if extra:
        _merge(cfg.data, extra)
    return cfg


def _merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _engine(home: Path, seed: int = 7, extra: dict[str, Any] | None = None) -> Engine:
    cfg = _cfg(home, extra)
    return Engine(StoreChain(KEY, home, SID, params=cfg.data), cfg, random.Random(seed))


def _log_lines(home: Path) -> list[str]:
    path = home / "logs" / "errors.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _arms(home: Path, sav: dict[str, float], reg: dict[str, float]) -> None:
    _project(
        home,
        {"compression": {FULL_KEY: {a: _entry(_ev(sav[a]), _ev(reg[a])) for a in sav}}},
    )


# --- brief tests ---------------------------------------------------------------------------


def test_off_wins_when_regret_high(tmp_path: Path) -> None:
    home = tmp_path / "h"
    trims = ("light", "standard", "aggressive")
    _arms(home, dict.fromkeys(trims, 0.9), dict.fromkeys(trims, 0.3))
    eng = _engine(home)
    ctx = _ctx(home)
    arms = Counter(eng.decide(COMPRESSION, FULL, ctx).arm for _ in range(1000))
    assert arms["off"] > 900


def test_aggressive_wins_when_safe(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _arms(
        home,
        {"light": 0.3, "standard": 0.6, "aggressive": 0.9},
        {"light": 0.0, "standard": 0.0, "aggressive": 0.0},
    )
    eng = _engine(home)
    ctx = _ctx(home)
    arms = Counter(eng.decide(COMPRESSION, FULL, ctx).arm for _ in range(1000))
    assert arms.most_common(1)[0][0] == "aggressive"
    assert arms["aggressive"] > 800


def test_propensity_floor_observed(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _arms(
        home,
        {"light": 0.3, "standard": 0.6, "aggressive": 0.9},
        {"light": 0.0, "standard": 0.0, "aggressive": 0.0},
    )
    eng = _engine(home, extra={"learn": {"pending_max": 5}})
    ctx = _ctx(home)
    decisions = [eng.decide(COMPRESSION, FULL, ctx) for _ in range(2000)]
    safe = [d for d in decisions if d.arm == "off"]
    floor = 0.05
    sigma = math.sqrt(floor * (1 - floor) / 2000)
    assert len(safe) / 2000 >= floor - 3 * sigma
    assert all(d.propensity >= floor for d in safe)


def _optin_user(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[learn.optin]\nhp_test = true\n", encoding="utf-8")


def test_high_tier_requires_ucb(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _optin_user(home)
    # Designed default: savings 0.9, regret 0.005 (mean < tau 0.01) but almost no evidence.
    extra = {"learn": {"defaults": {"hp_test": {"on": {"savings": 0.9, "regret": 0.005}}}}}
    eng = _engine(home, extra=extra)
    ctx = _ctx(home)
    prior = eng.posteriors(HIGH, {})["on"][1]
    assert prior.mean < 0.01 < prior.ucb(0.95)  # precondition: mean under tau, ucb over
    assert {eng.decide(HIGH, {}, ctx).arm for _ in range(300)} == {"off"}

    # Lots of zero-regret evidence: ucb clears tau, so "on" becomes feasible and wins.
    _project(home, {"hp_test": {"*": {"on": _entry(_ev(0.9), Evidence(0.0, 50.0, 50.0))}}})
    eng2 = _engine(home, extra=extra)
    assert eng2.posteriors(HIGH, {})["on"][1].ucb(0.95) <= 0.01
    arms = Counter(eng2.decide(HIGH, {}, ctx).arm for _ in range(300))
    assert arms["on"] > 200


def _high_ready(home: Path) -> dict[str, Any]:
    _project(home, {"hp_test": {"*": {"on": _entry(_ev(0.9), Evidence(0.0, 50.0, 50.0))}}})
    return {"learn": {"defaults": {"hp_test": {"on": {"savings": 0.9, "regret": 0.005}}}}}


def test_high_tier_without_optin_is_always_safe(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home, extra=_high_ready(home))
    ds = [eng.decide(HIGH, {}, _ctx(home)) for _ in range(100)]
    assert {(d.arm, d.propensity) for d in ds} == {("off", 1.0)}


def test_high_tier_optin_in_project_config_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "h"
    extra = _high_ready(home)
    extra["learn"]["optin"] = {"hp_test": True}  # merged config only (e.g. a project file)
    eng = _engine(home, extra=extra)
    assert {eng.decide(HIGH, {}, _ctx(home)).arm for _ in range(50)} == {"off"}
    assert any("optin" in line for line in _log_lines(home))


def test_high_tier_optin_by_user_config_or_env(tmp_path: Path) -> None:
    home = tmp_path / "h"
    extra = _high_ready(home)
    _optin_user(home)
    eng = _engine(home, extra=extra)
    assert Counter(eng.decide(HIGH, {}, _ctx(home)).arm for _ in range(200))["on"] > 150
    home2 = tmp_path / "h2"
    extra2 = _high_ready(home2)
    extra2["learn"]["optin"] = {"hp_test": True}
    cfg = _cfg(home2, extra2)
    env = {"TOKEN_SPAREN_HOME": str(home2), "TOKEN_SPAREN_LEARN__OPTIN__HP_TEST": "true"}
    eng2 = Engine(StoreChain(KEY, home2, SID, params=cfg.data), cfg, random.Random(1), env=env)
    assert Counter(eng2.decide(HIGH, {}, _ctx(home2)).arm for _ in range(200))["on"] > 150


def test_ladder_cold_bucket_only_first_arm(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home)
    arms = Counter(eng.decide(COMPRESSION, FULL, _ctx(home)).arm for _ in range(500))
    assert set(arms) <= {"off", "light"}
    assert arms["light"] > 300


def _reg_only(m: float, n: float) -> dict[str, Any]:
    return {"savings": _ev(0.5, n, n).to_list(), "regret": _ev(m, n, n).to_list()}


def test_ladder_unlocks_one_step_at_a_time(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {FULL_KEY: {"light": _reg_only(0.0, 20)}}})
    eng = _engine(home)
    arms = Counter(eng.decide(COMPRESSION, FULL, _ctx(home)).arm for _ in range(500))
    assert "standard" in arms and "aggressive" not in arms
    assert eng.eligible(COMPRESSION, FULL) == ["light", "standard"]
    assert eng.propensity(COMPRESSION, FULL, "aggressive") == 0.0
    assert sum(eng.propensity(COMPRESSION, FULL, a) for a in COMPRESSION.arms) == pytest.approx(
        1.0, abs=1e-6
    )


def test_ladder_needs_ladder_obs_and_low_regret(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {FULL_KEY: {"light": _reg_only(0.0, 19)}}})
    assert _engine(home).eligible(COMPRESSION, FULL) == ["light"]
    assert _engine(home, extra={"learn": {"ladder_obs": 19}}).eligible(COMPRESSION, FULL) == [
        "light",
        "standard",
    ]
    _project(home, {"compression": {FULL_KEY: {"light": _reg_only(0.3, 50)}}})
    assert _engine(home).eligible(COMPRESSION, FULL) == ["light"]  # established but too risky


def test_ladder_counts_session_and_backoff_evidence(tmp_path: Path) -> None:
    home = tmp_path / "h"
    # 10 project + 10 session observations at the full bucket pool to 20
    _project(home, {"compression": {FULL_KEY: {"light": _reg_only(0.0, 10)}}})
    _write(_overlay(home), {"evidence": {"compression": {FULL_KEY: {"light": _reg_only(0.0, 10)}}}})
    assert _engine(home).eligible(COMPRESSION, FULL) == ["light", "standard"]
    # a global coarse bucket with 25 observations also establishes the arm
    home2 = tmp_path / "h2"
    _write(
        home2 / "global" / "posteriors.json",
        {"version": 2, "points": {"compression": {"*": {"light": _reg_only(0.0, 25)}}}},
    )
    assert _engine(home2).eligible(COMPRESSION, FULL) == ["light", "standard"]


def test_ladder_is_a_chain(tmp_path: Path) -> None:
    home = tmp_path / "h"
    # standard is established and safe, but light is not established: aggressive stays locked
    _project(
        home,
        {"compression": {FULL_KEY: {"light": _reg_only(0.0, 5), "standard": _reg_only(0.0, 50)}}},
    )
    assert _engine(home).eligible(COMPRESSION, FULL) == ["light"]


def test_propensity_logged_and_in_unit_interval(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _arms(
        home,
        {"light": 0.4, "standard": 0.5, "aggressive": 0.6},
        {"light": 0.03, "standard": 0.04, "aggressive": 0.06},
    )
    eng = _engine(home)
    ctx = _ctx(home)
    for _ in range(200):
        d = eng.decide(COMPRESSION, FULL, ctx)
        assert d.bucket == FULL_KEY
        assert d.point == "compression"
        assert d.arm in COMPRESSION.arms
        assert 1e-3 <= d.propensity <= 1.0
    pending = _pending_doc(home)["pending"]
    assert pending[d.decision_id][:3] == ["compression", FULL_KEY, d.arm]


def test_reward_updates_overlay(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _arms(
        home,
        {"light": 0.3, "standard": 0.6, "aggressive": 0.9},
        {"light": 0.0, "standard": 0.0, "aggressive": 0.0},
    )
    eng = _engine(home, extra={"learn": {"floor": {"medium": 0.0}}})
    ctx = _ctx(home)
    d = eng.decide(COMPRESSION, FULL, ctx)
    assert d.arm != "off"
    eng.reward(d.decision_id, 0.7, 1.0)
    doc = json.loads(_overlay(home).read_text("utf-8"))
    node = doc["evidence"]["compression"][FULL_KEY][d.arm]
    assert Evidence.from_list(node["savings"]) == Evidence(0.0, 0.0, 0.0, 0.0).observe(
        0.7, gamma=0.98, cap=50
    )
    assert Evidence.from_list(node["regret"]).n == 1.0
    assert Evidence.from_list(node["regret"]).ea == 1.0
    # project store untouched
    proj = json.loads((home / "projects" / KEY / "posteriors.json").read_text("utf-8"))
    assert (
        proj["points"]["compression"][FULL_KEY][d.arm]["savings"]
        == _ev({"light": 0.3, "standard": 0.6, "aggressive": 0.9}[d.arm]).to_list()
    )


# --- additional tests ----------------------------------------------------------------------


def test_safe_arm_never_sampled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    asked: list[str] = []
    real = engine_mod.prior_for

    def spy(point: Any, features: Any, arm: str, kind: Any, stores: Any, **kw: Any) -> Beta:
        asked.append(arm)
        return real(point, features, arm, kind, stores, **kw)

    monkeypatch.setattr(engine_mod, "prior_for", spy)
    eng = _engine(home)
    for _ in range(20):
        eng.decide(COMPRESSION, FULL, _ctx(home))
    assert asked and "off" not in asked


def test_safe_arm_never_updated(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home, extra={"learn": {"floor": {"medium": 1.0}}})
    d = eng.decide(COMPRESSION, FULL, _ctx(home))
    assert d.arm == "off"
    eng.reward(d.decision_id, 0.5, 1.0)
    assert not _overlay(home).exists()  # nothing written: the safe arm has no evidence


def test_evidence_sum() -> None:
    a = Evidence(1.0, 2.0, 3.0, 0.5)
    b = Evidence(0.5, 0.25, 1.0, 0.25)
    s = a + b
    assert s == Evidence(1.5, 2.25, 4.0, 0.75)
    big = Evidence(40.0, 10.0, 60.0) + Evidence(40.0, 10.0, 60.0)
    assert big.ea + big.eb == 100.0  # cap not applied on sum
    assert (a + Evidence(0.0, 0.0, 0.0, 0.0)) == a


def test_posterior_sums_project_and_session_evidence(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"read_dedupe": {"cat=read|size=sm": {"pointer": _entry(_ev(0.9), _ev(0.5))}}})
    _write(
        _overlay(home),
        {
            "evidence": {
                "read_dedupe": {"cat=read|size=sm": {"pointer": _entry(_ev(0.1), _ev(0.5))}}
            }
        },
    )
    eng = _engine(home)
    post = eng.posteriors(READ_DEDUPE, {"cat": "read", "size": "sm"})
    sav = post["pointer"][0]
    # prior strength 6 from the designed default (own bucket is not its own parent) + mass 100
    assert sav.a + sav.b == pytest.approx(106.0)
    assert sav.n == 100.0
    assert sav.mean == pytest.approx((45 + 5 + 0.9 * 6) / 106)


def test_reward_bookkeeping_and_pending_eviction(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home, extra={"learn": {"pending_max": 3, "floor": {"low": 0.0}}})
    ctx = _ctx(home)
    ds = [eng.decide(READ_DEDUPE, {"cat": "read", "size": "sm"}, ctx) for _ in range(5)]
    pending = _pending_doc(home)["pending"]
    assert list(pending) == [d.decision_id for d in ds[-3:]]
    ts = pending[ds[-1].decision_id][3]
    assert isinstance(ts, float)
    before = _pending_doc(home)
    eng.reward(ds[0].decision_id, 1.0, 0.0)  # evicted: no-op
    assert _pending_doc(home) == before and not _overlay(home).exists()
    trimmed = [d for d in ds[-3:] if d.arm != "off"]
    assert trimmed
    d = trimmed[0]
    eng.reward(d.decision_id, 2.0, None)  # clamped to 1
    eng.reward(d.decision_id, None, float("nan"))  # NaN ignored + logged
    eng.reward(d.decision_id, None, -1.0)  # clamped to 0
    node = json.loads(_overlay(home).read_text("utf-8"))["evidence"]["read_dedupe"][
        "cat=read|size=sm"
    ][d.arm]
    sav = Evidence.from_list(node["savings"])
    reg = Evidence.from_list(node["regret"])
    assert (sav.ea, sav.eb, sav.n) == (1.0, 0.0, 1.0)
    assert (reg.ea, reg.eb, reg.n) == (0.0, 1.0, 1.0)
    assert any("NaN" in line for line in _log_lines(home))
    # the next decision sees the session evidence (cache invalidated)
    post = eng.posteriors(READ_DEDUPE, {"cat": "read", "size": "sm"})
    assert post[d.arm][0].n == 1.0


def test_error_returns_safe_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"

    def boom(*_a: Any, **_k: Any) -> Beta:
        raise RuntimeError("kaput")

    monkeypatch.setattr(engine_mod, "prior_for", boom)
    eng = _engine(home)
    d = eng.decide(COMPRESSION, FULL, _ctx(home))
    assert (d.point, d.arm, d.propensity, d.bucket) == ("compression", "off", 1.0, "error")
    assert any("kaput" in line for line in _log_lines(home))
    eng.reward("nope", 0.5, 0.5)  # unknown id never raises


def test_reward_error_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    eng = _engine(home)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(engine_mod.jsonstore, "read", boom)
    eng.reward("x", 0.5, 0.5)
    assert any("disk" in line for line in _log_lines(home))


def test_propensity_is_exact_and_logged(tmp_path: Path) -> None:
    from token_sparen.learn.beta import beta_cdf
    from token_sparen.learn.propensity import win_probabilities

    home = tmp_path / "h"
    _arms(
        home,
        {"light": 0.4, "standard": 0.5, "aggressive": 0.6},
        {"light": 0.01, "standard": 0.02, "aggressive": 0.03},
    )
    eng = _engine(home)
    posts = eng.posteriors(COMPRESSION, FULL)
    assert eng.eligible(COMPRESSION, FULL) == ["light", "standard", "aggressive"]
    safe, ps = win_probabilities(
        [(s, beta_cdf(0.05, r.a, r.b)) for s, r in (posts[a] for a in COMPRESSION.arms[1:])]
    )
    exact = dict(zip(COMPRESSION.arms, [0.05 + 0.95 * safe] + [0.95 * p for p in ps], strict=True))
    assert sum(exact.values()) == pytest.approx(1.0, abs=1e-6)
    for arm in COMPRESSION.arms:
        assert eng.propensity(COMPRESSION, FULL, arm) == pytest.approx(exact[arm], rel=1e-9)
    seen = Counter()
    for _ in range(300):
        d = eng.decide(COMPRESSION, FULL, _ctx(home))
        seen[d.arm] += 1
        assert d.propensity == pytest.approx(max(1e-3, exact[d.arm]), rel=1e-9)
    for arm in COMPRESSION.arms:
        assert seen[arm] / 300 == pytest.approx(exact[arm], abs=0.08)


def test_decision_ids_are_uuid4(tmp_path: Path) -> None:
    import uuid

    home = tmp_path / "h"
    a = _engine(home, seed=5).decide(COMPRESSION, FULL, _ctx(home))
    b = _engine(home, seed=5).decide(COMPRESSION, FULL, _ctx(home))
    assert a.decision_id != b.decision_id
    assert uuid.UUID(a.decision_id).version == 4


def test_choices_deterministic_given_seed(tmp_path: Path) -> None:
    home = tmp_path / "h"
    a = [_engine(home, seed=5).decide(COMPRESSION, FULL, _ctx(home)) for _ in range(3)]
    b = [_engine(home, seed=5).decide(COMPRESSION, FULL, _ctx(home)) for _ in range(3)]
    assert [(d.arm, d.propensity, d.bucket) for d in a] == [
        (d.arm, d.propensity, d.bucket) for d in b
    ]


def test_tier_none_has_no_floor_and_all_feasible(tmp_path: Path) -> None:
    home = tmp_path / "h"
    point = DecisionPoint("none_test", ("off", "on"), safe_arm="off", tier="none")
    extra = {"learn": {"defaults": {"none_test": {"on": {"savings": 0.9, "regret": 0.9}}}}}
    eng = _engine(home, extra=extra)
    arms = Counter(eng.decide(point, {}, _ctx(home)).arm for _ in range(200))
    assert arms["on"] == 200


def test_disabled_behaves_like_static(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home, extra={"learn": {"enabled": False}})
    d = eng.decide(COMPRESSION, FULL, _ctx(home))
    s = StaticPolicy({}).decide(COMPRESSION, FULL, _ctx(home))
    assert (d.arm, d.propensity, d.bucket) == (s.arm, s.propensity, s.bucket)
    assert not _overlay(home).exists()


def test_learn_defaults_present() -> None:
    from token_sparen.config import packaged_defaults

    learn = packaged_defaults()["learn"]
    assert learn["enabled"] is True
    assert "propensity_samples" not in learn  # exact propensity, no Monte-Carlo
    assert learn["pending_max"] == 500
    assert "cold_obs" not in learn  # superseded by ladder_obs
    assert learn["ladder_obs"] == 20
    assert learn["optin"] == {}
    assert learn["tau"] == {"low": 0.10, "medium": 0.05, "high": 0.01, "none": 1.0}
    assert learn["floor"] == {"low": 0.05, "medium": 0.05, "high": 0.02, "none": 0.0}


def test_one_reward_per_kind_per_decision(tmp_path: Path) -> None:
    home = tmp_path / "h"
    eng = _engine(home, extra={"learn": {"floor": {"low": 0.0}}})
    ctx = _ctx(home)
    d = next(
        d
        for d in (eng.decide(READ_DEDUPE, {"cat": "read", "size": "sm"}, ctx) for _ in range(50))
        if d.arm != "off"
    )
    eng.reward(d.decision_id, 0.8, None)
    eng.reward(d.decision_id, 0.1, None)  # second savings reward: ignored + logged
    eng.reward(d.decision_id, None, 0.0)  # first regret reward: applied (explicit "no regret")
    eng.reward(d.decision_id, 0.5, 1.0)  # both kinds already received: ignored
    doc = json.loads(_overlay(home).read_text("utf-8"))
    node = doc["evidence"]["read_dedupe"]["cat=read|size=sm"][d.arm]
    sav, reg = Evidence.from_list(node["savings"]), Evidence.from_list(node["regret"])
    assert (sav.n, sav.ea) == (1.0, pytest.approx(0.8))
    assert (reg.n, reg.ea) == (1.0, 0.0)
    assert sorted(_pending_doc(home)["pending"][d.decision_id][4]) == ["regret", "savings"]
    assert sum("already rewarded" in line for line in _log_lines(home)) == 2


def test_pending_decisions_live_outside_the_hot_overlay(tmp_path: Path) -> None:
    """Up to 500 pending decisions must not inflate the overlay every hook parses (CI perf)."""
    from token_sparen.learn import engine as engine_mod

    home = tmp_path / "h"
    eng = _engine(home)
    ctx = _ctx(home)
    ds = [eng.decide(COMPRESSION, FULL, ctx) for _ in range(300)]
    side = engine_mod.pending_path(_overlay(home))
    assert side.parent.name == "pending" and len(_pending_doc(home)["pending"]) == 300
    assert not _overlay(home).exists()
    trim = next(d for d in ds if d.arm != "off")
    eng.reward(trim.decision_id, 0.5, None)
    hot = json.loads(_overlay(home).read_text("utf-8"))
    assert "pending" not in hot and hot["evidence"]["compression"][FULL_KEY][trim.arm]
    assert _pending_doc(home)["pending"][trim.decision_id][4] == ["savings"]


# --- natural-rate gate (loop guard; wave C1 fix round 1) ---------------------------------------

LG_FEATS = {"cat": "shell"}


def _observe_natural(eng: Engine, home: Path, broken: float) -> None:
    from token_sparen.learn.hierarchy import NATURAL_KEY
    from token_sparen.store import jsonstore

    def fn(state: dict[str, Any]) -> dict[str, Any]:
        table = state.setdefault(NATURAL_KEY, {})
        raw = table.get("loopguard")
        ev = Evidence.from_list(raw) if raw else Evidence(0.0, 0.0, 0.0, 0.0)
        table["loopguard"] = ev.observe(broken, gamma=1.0, cap=50.0).to_list()
        return state

    jsonstore.update(_overlay(home), fn, {}, {"TOKEN_SPAREN_HOME": str(home)})
    eng.stores.invalidate("session")


def _simulate(
    home: Path, p_arm: float, p0: float, n: int, seed: int
) -> tuple[Counter[str], Engine]:
    from token_sparen.learn.policy import LOOPGUARD

    eng = _engine(home, seed=seed)
    rng = random.Random(seed + 1)
    chosen: Counter[str] = Counter()
    for _ in range(n):
        d = eng.decide(LOOPGUARD, LG_FEATS, _ctx(home))
        if not eng.last_explored:
            chosen[d.arm] += 1
        if d.arm == LOOPGUARD.safe_arm:
            _observe_natural(eng, home, float(rng.random() < p0))
        else:
            eng.reward(d.decision_id, float(rng.random() < p_arm), float(rng.random() < 0.01))
    return chosen, eng


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_loopguard_null_effect_stays_patient(tmp_path: Path, seed: int) -> None:
    """Redirects that change nothing (break rate == natural rate) never pass the gate: outside
    floor exploration the patient arm is chosen >= 95 % of the time."""
    chosen, _ = _simulate(tmp_path / "h", 0.6, 0.6, 200, seed)
    total = sum(chosen.values())
    assert total > 150 and chosen["patient"] / total >= 0.95, chosen


def test_loopguard_real_effect_becomes_eligible(tmp_path: Path) -> None:
    from token_sparen.learn.policy import LOOPGUARD

    home = tmp_path / "h"
    _, eng = _simulate(home, 0.8, 0.4, 300, 5)
    fresh = _engine(home, seed=9)
    assert "standard" in fresh.eligible(LOOPGUARD, LG_FEATS), eng.posteriors(LOOPGUARD, LG_FEATS)


def test_loopguard_cold_gate_explores_by_floor_only(tmp_path: Path) -> None:
    from token_sparen.learn.policy import LOOPGUARD

    eng = _engine(tmp_path / "h")
    assert eng.eligible(LOOPGUARD, LG_FEATS) == []
    probs = eng.action_probs(LOOPGUARD, LG_FEATS)
    assert probs == pytest.approx({"patient": 0.95, "standard": 0.05, "eager": 0.0})
    seen = Counter(eng.choose(LOOPGUARD, LG_FEATS) for _ in range(400))
    for (arm, p), _count in seen.items():
        assert p == pytest.approx(probs[arm])
    assert {a for a, _ in seen} == {"patient", "standard"}


def test_loopguard_held_never_explores(tmp_path: Path) -> None:
    from token_sparen.learn.policy import LOOPGUARD

    eng = _engine(tmp_path / "h", extra={"learn": {"hold": {"loopguard": True}}})
    assert eng.action_probs(LOOPGUARD, LG_FEATS) == {"patient": 1.0, "standard": 0.0, "eager": 0.0}


def test_forget_drops_the_pending_decision(tmp_path: Path) -> None:
    from token_sparen.learn.policy import LOOPGUARD

    home = tmp_path / "h"
    eng = _engine(home, extra={"learn": {"floor": {"medium": 1.0}}})
    d = eng.decide(LOOPGUARD, LG_FEATS, _ctx(home))
    assert d.arm == "standard" and d.decision_id in _pending_doc(home)["pending"]
    eng.forget(d.decision_id)
    assert d.decision_id not in _pending_doc(home)["pending"]
    eng.reward(d.decision_id, 1.0, 0.0)  # nothing to update any more
    assert eng.posteriors(LOOPGUARD, LG_FEATS)["standard"][0].n == 0.0
    eng.forget("unknown")  # never raises
