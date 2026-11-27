from __future__ import annotations

import json
import random
import statistics
from pathlib import Path
from typing import Any

import pytest

from token_sparen.config import load_config
from token_sparen.learn import hierarchy
from token_sparen.learn.beta import Beta, Evidence
from token_sparen.learn.hierarchy import StoreChain, prior_for
from token_sparen.learn.policy import COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF
from token_sparen.store import session

KEY = "0123456789abcdef"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"
K0 = hierarchy.DEFAULT_KAPPA


def _entry(sav: Evidence | None = None, reg: Evidence | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if sav is not None:
        out["savings"] = sav.to_list()
    if reg is not None:
        out["regret"] = reg.to_list()
    return out


def _doc(points: dict[str, Any], version: int = 2) -> dict[str, Any]:
    return {"version": version, "points": points}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _project(home: Path, points: dict[str, Any]) -> None:
    _write(home / "projects" / KEY / "posteriors.json", _doc(points))


def _global(home: Path, points: dict[str, Any]) -> None:
    _write(home / "global" / "posteriors.json", _doc(points))


def _ev(m: float, n: float, mass: float = 10.0) -> Evidence:
    return Evidence(m * mass, (1 - m) * mass, n)


def _shrunk(ev: Evidence, d: float, k0: float = K0) -> float:
    return (ev.ea + k0 * d) / (ev.ea + ev.eb + k0)


def _log_lines(home: Path) -> list[str]:
    path = home / "logs" / "errors.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_get_scopes_and_missing(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(Evidence(2, 3, 7), Evidence(1, 9, 7))}}})
    _global(home, {"compression": {"*": {"light": _entry(Evidence(4, 1, 9))}}})
    sess = session.session_path(home, KEY, "s-1")
    _write(
        sess,
        {"turn": 3, "evidence": {"compression": {"*": {"light": _entry(Evidence(5, 0, 2))}}}},
    )
    st = StoreChain(KEY, home, session_id="s-1")
    assert st.get("compression", "*", "light", "savings", "project") == Evidence(2, 3, 7)
    assert st.get("compression", "*", "light", "regret", "project") == Evidence(1, 9, 7)
    assert st.get("compression", "*", "light", "savings", "global") == Evidence(4, 1, 9)
    assert st.get("compression", "*", "light", "regret", "global") is None
    assert st.get("compression", "*", "light", "savings", "session") == Evidence(5, 0, 2)
    assert st.get("compression", "x", "light", "savings", "project") is None
    assert st.get("pretrim", "*", "light", "savings", "project") is None
    assert StoreChain(KEY, home).get("compression", "*", "light", "savings", "session") is None
    empty = StoreChain(KEY, tmp_path / "empty")
    assert empty.get("compression", "*", "off", "savings", "global") is None
    with pytest.raises(ValueError):
        st.get("compression", "*", "light", "savings", "nowhere")  # type: ignore[arg-type]


def test_version_1_store_is_empty_and_logged(tmp_path: Path) -> None:
    home = tmp_path / "h"
    pts = {"compression": {"*": {"light": _entry(Evidence(2, 3, 7))}}}
    _write(home / "projects" / KEY / "posteriors.json", _doc(pts, version=1))
    _write(home / "global" / "posteriors.json", _doc(pts, version=1))
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") is None
    assert st.get("compression", "*", "light", "savings", "global") is None
    assert len(_log_lines(home)) == 1


def test_files_read_once_per_instance(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(Evidence(2, 3, 7))}}})
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") == Evidence(2, 3, 7)
    _project(home, {"compression": {"*": {"light": _entry(Evidence(9, 1, 7))}}})
    assert st.get("compression", "*", "light", "savings", "project") == Evidence(2, 3, 7)
    fresh = StoreChain(KEY, home)
    assert fresh.get("compression", "*", "light", "savings", "project") == Evidence(9, 1, 7)


@pytest.mark.parametrize(
    "bad",
    [
        {"savings": [1, 2]},
        {"savings": "x"},
        {"savings": [-1, 1, 1]},
        {"savings": [1, 1, -1]},
        {"savings": [True, 1, 1]},
        "notadict",
    ],
)
def test_corrupt_entry_is_none_and_logged_once(tmp_path: Path, bad: Any) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": bad, "standard": bad}}})
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") is None
    assert st.get("compression", "*", "standard", "savings", "project") is None
    assert st.get("compression", "*", "light", "savings", "project") is None
    log = _log_lines(home)
    assert len(log) == 1 and "hierarchy" in log[0]


@pytest.mark.parametrize(
    "doc",
    [
        "{not json",
        [1, 2],
        {"version": 3, "points": {}},
        {"version": 2, "points": []},
        {"version": 2, "points": {"compression": []}},
        {"version": 2, "points": {"compression": {"*": []}}},
    ],
)
def test_corrupt_documents_are_empty(tmp_path: Path, doc: Any) -> None:
    home = tmp_path / "h"
    text = doc if isinstance(doc, str) else json.dumps(doc)
    _write(home / "projects" / KEY / "posteriors.json", text)
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") is None
    assert prior_for(COMPRESSION, FULL, "light", "savings", st).mean == pytest.approx(0.35)


def test_session_overlay_without_posteriors(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(session.session_path(home, KEY, "s-1"), {"turn": 1, "evidence": "bad"})
    st = StoreChain(KEY, home, session_id="s-1")
    assert st.get("compression", "*", "light", "savings", "session") is None


def test_prior_from_parent_mean(tmp_path: Path) -> None:
    home = tmp_path / "h"
    parent = _ev(0.7, 12)
    _project(home, {"compression": {"cat=shell|ctype=log": {"light": _entry(parent)}}})
    st = StoreChain(KEY, home)
    p = prior_for(COMPRESSION, FULL, "light", "savings", st)
    m = _shrunk(parent, 0.35)  # data mean 0.7 shrunk toward the designed 0.35 by kappa0 = 6
    assert m == pytest.approx((7 + 6 * 0.35) / 16)
    assert p.a == pytest.approx(m * K0) and p.b == pytest.approx((1 - m) * K0)
    assert p.n == 0.0 and p.mean == pytest.approx(m)


def test_parent_needs_min_obs(tmp_path: Path) -> None:
    home = tmp_path / "h"
    low = _ev(0.2, 5)
    _project(
        home,
        {
            "compression": {
                FULL_KEY: {"light": _entry(_ev(0.9, 4.99))},  # too few real obs
                "ctype=log": {"light": _entry(low)},
            }
        },
    )
    p = prior_for(COMPRESSION, FULL, "light", "savings", StoreChain(KEY, home))
    assert p.mean == pytest.approx(_shrunk(low, 0.35))


def test_scope_fallback_order(tmp_path: Path) -> None:
    home = tmp_path / "h"
    st_none = StoreChain(KEY, home)
    assert prior_for(COMPRESSION, FULL, "standard", "savings", st_none).mean == pytest.approx(0.6)
    assert prior_for(COMPRESSION, FULL, "standard", "regret", st_none).mean == pytest.approx(0.04)

    def mean() -> float:
        return prior_for(COMPRESSION, FULL, "standard", "savings", StoreChain(KEY, home)).mean

    g_star, g_full = _ev(0.11, 50, 40), _ev(0.22, 50, 40)
    p_star, p_log = _ev(0.33, 5), _ev(0.44, 5)
    _global(home, {"compression": {"*": {"standard": _entry(g_star)}}})
    assert mean() == pytest.approx(_shrunk(g_star, 0.6))  # global coarse beats the default
    _global(
        home,
        {
            "compression": {
                "*": {"standard": _entry(g_star)},
                FULL_KEY: {"standard": _entry(g_full)},
            }
        },
    )
    assert mean() == pytest.approx(_shrunk(g_full, 0.6))  # global full beats global coarse
    _project(home, {"compression": {"*": {"standard": _entry(p_star)}}})
    assert mean() == pytest.approx(_shrunk(p_star, 0.6))  # project * beats any global level
    _project(
        home,
        {
            "compression": {
                "*": {"standard": _entry(p_star)},
                "ctype=log": {"standard": _entry(p_log)},
            }
        },
    )
    assert mean() == pytest.approx(_shrunk(p_log, 0.6))  # specific beats coarse
    _write(
        session.session_path(home, KEY, "s-1"),
        {"evidence": {"compression": {FULL_KEY: {"standard": _entry(_ev(0.9, 50))}}}},
    )
    st = StoreChain(KEY, home, session_id="s-1")  # the session is the child, not a parent
    assert prior_for(COMPRESSION, FULL, "standard", "savings", st).mean == pytest.approx(
        _shrunk(p_log, 0.6)
    )


def test_parent_with_zero_mass_uses_default_mean(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(Evidence(0.0, 0.0, 9))}}})
    p = prior_for(COMPRESSION, FULL, "light", "savings", StoreChain(KEY, home))
    assert p.mean == pytest.approx(0.35)


@pytest.mark.parametrize(
    ("point", "arm", "sav", "reg"),
    [
        (COMPRESSION, "light", 0.35, 0.02),
        (COMPRESSION, "standard", 0.6, 0.04),
        (COMPRESSION, "aggressive", 0.8, 0.08),
        (PRETRIM, "bounded", 0.5, 0.05),
        (READ_DEDUPE, "pointer", 0.9, 0.03),
        (SUBAGENT_BRIEF, "on", 0.3, 0.05),
    ],
)
def test_designed_defaults(tmp_path: Path, point: Any, arm: str, sav: float, reg: float) -> None:
    st = StoreChain(KEY, tmp_path / "h")
    s = prior_for(point, {}, arm, "savings", st)
    r = prior_for(point, {}, arm, "regret", st)
    assert s.mean == pytest.approx(sav) and r.mean == pytest.approx(reg)
    assert s.a + s.b == pytest.approx(6.0) and s.n == 0.0
    assert r.a + r.b == pytest.approx(hierarchy.DEFAULT_KAPPA_REGRET) and r.n == 0.0


def test_safe_arm_epsilon_prior(tmp_path: Path) -> None:
    st = StoreChain(KEY, tmp_path / "h")
    assert hierarchy.EPS == 1e-3
    for point in (COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF):
        for kind in ("savings", "regret"):
            p = prior_for(point, {}, point.safe_arm, kind, st)  # type: ignore[arg-type]
            k = K0 if kind == "savings" else hierarchy.DEFAULT_KAPPA_REGRET
            assert p == Beta(hierarchy.EPS, k, 0.0)


def test_unknown_point_or_arm_default(tmp_path: Path) -> None:
    from token_sparen.learn.policy import DecisionPoint

    st = StoreChain(KEY, tmp_path / "h")
    ext = DecisionPoint("ext_point", ("off", "on"), "off", "low")
    eps_mean = hierarchy.EPS / (hierarchy.EPS + K0)
    assert prior_for(ext, {}, "on", "savings", st).mean == pytest.approx(eps_mean)
    assert prior_for(ext, {}, "on", "regret", st).mean == pytest.approx(0.5)


def test_defaults_from_params(tmp_path: Path) -> None:
    params = {
        "learn": {
            "min_parent_obs": 1,
            "kappa_default": 10,
            "defaults": {"compression": {"light": {"savings": 0.5, "regret": "bad"}}},
        }
    }
    home = tmp_path / "h"
    st = StoreChain(KEY, home, params=params)
    p = prior_for(COMPRESSION, FULL, "light", "savings", st)
    assert p.mean == pytest.approx(0.5) and p.a + p.b == pytest.approx(10.0)
    # an invalid override falls back to the packaged default, not the unknown-arm fallback
    assert prior_for(COMPRESSION, FULL, "light", "regret", st).mean == pytest.approx(0.02)
    # a partial params dict keeps every other packaged default
    assert prior_for(PRETRIM, {}, "bounded", "savings", st).mean == pytest.approx(0.5)
    parent = _ev(0.25, 1)
    _project(home, {"compression": {"*": {"light": _entry(parent)}}})
    st = StoreChain(KEY, home, params=params)
    got = prior_for(COMPRESSION, FULL, "light", "savings", st).mean
    assert got == pytest.approx(_shrunk(parent, 0.5, 10.0))


def test_user_config_overrides_apply(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "h").mkdir()
    # learning settings beyond the tighten-only allowlist are user/env/session only
    (tmp_path / "h" / "config.toml").write_text(
        "[learn]\nmin_parent_obs = 50\n[learn.defaults.compression.light]\nsavings = 0.1\n",
        encoding="utf-8",
    )
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "h"), "TOKEN_SPAREN_LEARN__KAPPA_DEFAULT": "12"}
    cfg = load_config(proj, env)
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(_ev(0.9, 20))}}})  # < 50 obs
    st = StoreChain(KEY, home, params=cfg.data)
    p = prior_for(COMPRESSION, FULL, "light", "savings", st)
    assert p.mean == pytest.approx(0.1) and p.a + p.b == pytest.approx(12.0)


def _siblings(means: list[float], n: float = 8.0, mass: float = 10.0) -> dict[str, Any]:
    return {
        f"band=lt50|cat=c{i}|size=sm|ctype=log|phase=build": {"light": _entry(_ev(m, n, mass))}
        for i, m in enumerate(means)
    }


def _kappa(home: Path) -> float:
    return hierarchy.kappa(COMPRESSION, "light", "savings", StoreChain(KEY, home))


def test_kappa_moments_clamped(tmp_path: Path) -> None:
    home = tmp_path / "h"
    means = [0.3, 0.5, 0.7, 0.4]
    _project(home, {"compression": _siblings(means, mass=100.0)})
    m = statistics.fmean(means)
    between = statistics.pvariance(means) - statistics.fmean([x * (1 - x) / 101 for x in means])
    expect = m * (1 - m) / between - 1
    assert 2 < expect < 20
    assert _kappa(home) == pytest.approx(expect)
    _project(home, {"compression": _siblings([0.01, 0.99, 0.02, 0.98])})
    assert _kappa(home) == 2.0  # widely spread -> tiny kappa -> clamped
    _project(home, {"compression": _siblings([0.45, 0.55, 0.5], mass=1000.0)})
    assert _kappa(home) == 20.0  # huge kappa -> clamped
    _project(home, {"compression": _siblings([0.4, 0.6, 0.5])})
    assert _kappa(home) == 20.0  # spread within sampling noise -> var_between <= 0
    # the prior from a parent uses the estimated kappa
    parent = _ev(0.6, 9)
    sibs = _siblings(means, mass=100.0)
    _project(home, {"compression": {**sibs, "ctype=log": {"light": _entry(parent)}}})
    p = prior_for(COMPRESSION, FULL, "light", "savings", StoreChain(KEY, home))
    assert p.a + p.b == pytest.approx(expect) and p.mean == pytest.approx(_shrunk(parent, 0.35))


def test_kappa_default_with_few_siblings(tmp_path: Path) -> None:
    home = tmp_path / "h"
    pts = _siblings([0.3, 0.7])
    pts["band=gt95|cat=x|size=lg|ctype=log|phase=build"] = {"light": _entry(_ev(0.9, 4))}
    pts["band=gt95|cat=z|size=lg|ctype=log|phase=build"] = {"light": _entry(Evidence(0, 0, 9))}
    pts["ctype=log"] = {"light": _entry(_ev(0.1, 40))}
    pts["band=gt95|cat=y|size=lg|ctype=log|phase=build"] = {"standard": _entry(_ev(0.9, 40))}
    _project(home, {"compression": pts})
    st = StoreChain(KEY, home)
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) == 6.0
    assert hierarchy.kappa(COMPRESSION, "light", "regret", st) == 6.0  # no regret evidence
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) == 6.0  # cached path


def test_designed_default_uses_default_kappa(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": _siblings([0.3, 0.5, 0.7, 0.4], mass=100.0)})
    st = StoreChain(KEY, home)
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) != 6.0
    p = prior_for(COMPRESSION, {"ctype": "json"}, "light", "savings", st)
    assert p.mean == pytest.approx(0.35) and p.a + p.b == pytest.approx(6.0)


def _simulated(
    true_kappa: float,
    seed: int,
    *,
    buckets: int = 30,
    obs: int = 20,
    sd: float | None = None,
) -> dict[str, Any]:
    """Sibling evidence: bucket means from Beta(mu k, (1-mu) k); Bernoulli rewards, or
    fractional rewards N(p, sd) clamped to [0, 1] when ``sd`` is given."""
    rng = random.Random(seed)
    mu = 0.4
    pts: dict[str, Any] = {}
    for i in range(buckets):
        p = rng.betavariate(mu * true_kappa, (1 - mu) * true_kappa)
        ev = Evidence(0.0, 0.0, 0.0, 0.0)
        for _ in range(obs):
            if sd is None:
                x = 1.0 if rng.random() < p else 0.0
            else:
                x = min(1.0, max(0.0, rng.gauss(p, sd)))
            ev = ev.observe(x, gamma=0.98, cap=50.0)
        pts[f"band=lt50|cat=c{i}|size=sm|ctype=log|phase=build"] = {"light": _entry(ev)}
    return pts


@pytest.mark.parametrize(("true_kappa", "lo", "hi"), [(3.0, 2.0, 6.0), (20.0, 10.0, 20.0)])
def test_kappa_recovers_simulated_strength(
    tmp_path: Path, true_kappa: float, lo: float, hi: float
) -> None:
    estimates = []
    for seed in range(25):
        home = tmp_path / f"h{seed}"
        _project(home, {"compression": _simulated(true_kappa, seed)})
        estimates.append(_kappa(home))
    assert lo <= statistics.median(estimates) <= hi


@pytest.mark.parametrize("true_kappa", [3.0, 6.0])
def test_kappa_recovers_strength_with_fractional_rewards(tmp_path: Path, true_kappa: float) -> None:
    estimates = []
    for seed in range(25):
        home = tmp_path / f"h{seed}"
        _project(home, {"compression": _simulated(true_kappa, seed, sd=0.15)})
        estimates.append(_kappa(home))
    assert true_kappa / 2 <= statistics.median(estimates) <= 2 * true_kappa


def test_kappa_noise_uses_within_bucket_variance(tmp_path: Path) -> None:
    home = tmp_path / "h"
    means, w = [0.3, 0.5, 0.7, 0.4], 10.0
    var_w = 0.01  # fractional rewards: far below the Bernoulli m(1-m)
    pts = {
        f"band=lt50|cat=c{i}|size=sm|ctype=log|phase=build": {
            "light": _entry(Evidence(m * w, (1 - m) * w, 8, (var_w + m * m) * w))
        }
        for i, m in enumerate(means)
    }
    _project(home, {"compression": pts})
    m_bar = statistics.fmean(means)
    between = statistics.pvariance(means) - var_w / (w + 1)
    assert _kappa(home) == pytest.approx(m_bar * (1 - m_bar) / between - 1)


def test_child_bucket_is_not_its_own_parent(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {FULL_KEY: {"standard": _entry(_ev(0.9, 50))}}})
    stores = StoreChain(KEY, home)
    with_own = prior_for(COMPRESSION, FULL, "standard", "savings", stores)
    skipped = prior_for(COMPRESSION, FULL, "standard", "savings", stores, child_bucket=FULL_KEY)
    assert with_own.mean == pytest.approx(_shrunk(_ev(0.9, 50), 0.6))
    assert skipped.mean == pytest.approx(0.6)
    assert skipped.a + skipped.b == pytest.approx(K0)


def test_invalidate_rereads_scope(tmp_path: Path) -> None:
    home = tmp_path / "h"
    stores = StoreChain(KEY, home, "s-1")
    assert stores.get("compression", "*", "light", "savings", "session") is None
    _write(
        session.session_path(home, KEY, "s-1"),
        {"evidence": {"compression": {"*": {"light": _entry(Evidence(1, 0, 1))}}}},
    )
    assert stores.get("compression", "*", "light", "savings", "session") is None  # cached
    stores.invalidate("session")
    assert stores.get("compression", "*", "light", "savings", "session") == Evidence(1, 0, 1)


def test_regret_priors_use_kappa_regret(tmp_path: Path) -> None:
    home = tmp_path / "h"
    assert hierarchy.DEFAULT_KAPPA_REGRET == 20.0
    # a qualifying parent at the coarse level lends its shrunk mean with strength kappa_regret
    parent = _ev(0.1, 10)
    _project(home, {"compression": {"ctype=log": {"light": _entry(parent, parent)}}})
    st = StoreChain(KEY, home)
    r = prior_for(COMPRESSION, FULL, "light", "regret", st)
    s = prior_for(COMPRESSION, FULL, "light", "savings", st)
    assert r.a + r.b == pytest.approx(20.0)
    assert r.mean == pytest.approx(_shrunk(parent, 0.02))
    assert s.a + s.b == pytest.approx(hierarchy.kappa(COMPRESSION, "light", "savings", st))
    cfg = load_config(tmp_path / "p", {"TOKEN_SPAREN_HOME": str(home)})
    cfg.data["learn"]["kappa_regret"] = 30
    st2 = StoreChain(KEY, home, params=cfg.data)
    r2 = prior_for(COMPRESSION, {}, "standard", "regret", st2)
    assert r2.a + r2.b == pytest.approx(30.0) and r2.mean == pytest.approx(0.04)


@pytest.mark.parametrize("arm", ["standard", "eager"])
def test_loopguard_priors_are_neutral_and_weak(tmp_path: Path, arm: str) -> None:
    """Review M1: the intervening loop-guard arms start neutral (savings mean 0.1, strength 2;
    regret mean = the medium tier's tau) so data, not the designed prior, decides them."""
    from token_sparen.learn.policy import LOOPGUARD

    st = StoreChain(KEY, tmp_path / "h")
    s = prior_for(LOOPGUARD, {}, arm, "savings", st)
    r = prior_for(LOOPGUARD, {}, arm, "regret", st)
    assert s.mean == pytest.approx(0.1) and s.a + s.b == pytest.approx(2.0)
    assert r.mean == pytest.approx(0.05)
    assert r.a + r.b == pytest.approx(hierarchy.DEFAULT_KAPPA_REGRET)


def test_designed_strength_only_where_declared(tmp_path: Path) -> None:
    params = {"learn": {"defaults": {"compression": {"light": {"savings_strength": 3}}}}}
    st = StoreChain(KEY, tmp_path / "h", params=params)
    assert prior_for(COMPRESSION, {}, "light", "savings", st).a + prior_for(
        COMPRESSION, {}, "light", "savings", st
    ).b == pytest.approx(3.0)
    st2 = StoreChain(KEY, tmp_path / "h")
    p = prior_for(COMPRESSION, {}, "standard", "savings", st2)
    assert p.a + p.b == pytest.approx(K0)
