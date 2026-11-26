from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from token_sparen.learn import hierarchy
from token_sparen.learn.beta import Beta
from token_sparen.learn.hierarchy import StoreChain, prior_for
from token_sparen.learn.policy import COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF
from token_sparen.store import session

KEY = "0123456789abcdef"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"


def _entry(sav: Beta | None = None, reg: Beta | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if sav is not None:
        out["savings"] = sav.to_list()
    if reg is not None:
        out["regret"] = reg.to_list()
    return out


def _doc(points: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "points": points}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _project(home: Path, points: dict[str, Any]) -> None:
    _write(home / "projects" / KEY / "posteriors.json", _doc(points))


def _global(home: Path, points: dict[str, Any]) -> None:
    _write(home / "global" / "posteriors.json", _doc(points))


def _beta_mean(m: float, n: float, mass: float = 10.0) -> Beta:
    return Beta(m * mass, (1 - m) * mass, n)


def test_get_scopes_and_missing(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(Beta(2, 3, 7), Beta(1, 9, 7))}}})
    _global(home, {"compression": {"*": {"light": _entry(Beta(4, 1, 9))}}})
    sess = session.session_path(home, KEY, "s-1")
    _write(
        sess, {"turn": 3, "posteriors": {"compression": {"*": {"light": _entry(Beta(5, 5, 2))}}}}
    )
    st = StoreChain(KEY, home, session_id="s-1")
    assert st.get("compression", "*", "light", "savings", "project") == Beta(2, 3, 7)
    assert st.get("compression", "*", "light", "regret", "project") == Beta(1, 9, 7)
    assert st.get("compression", "*", "light", "savings", "global") == Beta(4, 1, 9)
    assert st.get("compression", "*", "light", "regret", "global") is None
    assert st.get("compression", "*", "light", "savings", "session") == Beta(5, 5, 2)
    assert st.get("compression", "x", "light", "savings", "project") is None
    assert st.get("pretrim", "*", "light", "savings", "project") is None
    assert StoreChain(KEY, home).get("compression", "*", "light", "savings", "session") is None
    assert (
        StoreChain(KEY, tmp_path / "empty").get("compression", "*", "off", "savings", "global")
        is None
    )
    with pytest.raises(ValueError):
        st.get("compression", "*", "light", "savings", "nowhere")  # type: ignore[arg-type]


def test_files_read_once_per_instance(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"*": {"light": _entry(Beta(2, 3, 7))}}})
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") == Beta(2, 3, 7)
    _project(home, {"compression": {"*": {"light": _entry(Beta(9, 1, 7))}}})
    assert st.get("compression", "*", "light", "savings", "project") == Beta(2, 3, 7)
    assert StoreChain(KEY, home).get("compression", "*", "light", "savings", "project") == Beta(
        9, 1, 7
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"savings": [1, 2]},
        {"savings": "x"},
        {"savings": [0, 1, 1]},
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
    log = (home / "logs" / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and "hierarchy" in log[0]


@pytest.mark.parametrize(
    "doc",
    [
        "{not json",
        [1, 2],
        {"version": 2, "points": {}},
        {"version": 1, "points": []},
        {"version": 1, "points": {"compression": []}},
        {"version": 1, "points": {"compression": {"*": []}}},
    ],
)
def test_corrupt_documents_are_empty(tmp_path: Path, doc: Any) -> None:
    home = tmp_path / "h"
    _write(
        home / "projects" / KEY / "posteriors.json",
        doc if isinstance(doc, str) else json.dumps(doc),
    )
    st = StoreChain(KEY, home)
    assert st.get("compression", "*", "light", "savings", "project") is None
    assert prior_for(COMPRESSION, FULL, "light", "savings", st).mean == pytest.approx(0.35)


def test_session_overlay_without_posteriors(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(session.session_path(home, KEY, "s-1"), {"turn": 1, "posteriors": "bad"})
    st = StoreChain(KEY, home, session_id="s-1")
    assert st.get("compression", "*", "light", "savings", "session") is None


def test_prior_from_parent_mean(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": {"cat=shell|ctype=log": {"light": _entry(_beta_mean(0.7, 12))}}})
    st = StoreChain(KEY, home)
    p = prior_for(COMPRESSION, FULL, "light", "savings", st)
    k = hierarchy.DEFAULT_KAPPA
    assert k == 6.0
    assert p.a == pytest.approx(0.7 * k) and p.b == pytest.approx(0.3 * k)
    assert p.n == 0.0 and p.mean == pytest.approx(0.7)


def test_parent_needs_min_obs(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(
        home,
        {
            "compression": {
                FULL_KEY: {"light": _entry(_beta_mean(0.9, 4.99))},  # too few real obs
                "ctype=log": {"light": _entry(_beta_mean(0.2, 5))},
            }
        },
    )
    p = prior_for(COMPRESSION, FULL, "light", "savings", StoreChain(KEY, home))
    assert p.mean == pytest.approx(0.2)


def test_scope_fallback_order(tmp_path: Path) -> None:
    home = tmp_path / "h"
    st_none = StoreChain(KEY, home)
    # nothing anywhere -> designed default
    assert prior_for(COMPRESSION, FULL, "standard", "savings", st_none).mean == pytest.approx(0.6)
    assert prior_for(COMPRESSION, FULL, "standard", "regret", st_none).mean == pytest.approx(0.04)
    # global coarse beats designed default
    _global(home, {"compression": {"*": {"standard": _entry(_beta_mean(0.11, 50))}}})
    assert prior_for(COMPRESSION, FULL, "standard", "savings", StoreChain(KEY, home)).mean == (
        pytest.approx(0.11)
    )
    # global full beats global coarse
    _global(
        home,
        {
            "compression": {
                "*": {"standard": _entry(_beta_mean(0.11, 50))},
                FULL_KEY: {"standard": _entry(_beta_mean(0.22, 50))},
            }
        },
    )
    assert prior_for(COMPRESSION, FULL, "standard", "savings", StoreChain(KEY, home)).mean == (
        pytest.approx(0.22)
    )
    # project coarsest (*) beats any global level
    _project(home, {"compression": {"*": {"standard": _entry(_beta_mean(0.33, 5))}}})
    assert prior_for(COMPRESSION, FULL, "standard", "savings", StoreChain(KEY, home)).mean == (
        pytest.approx(0.33)
    )
    # a more specific project level beats a coarser one
    _project(
        home,
        {
            "compression": {
                "*": {"standard": _entry(_beta_mean(0.33, 5))},
                "ctype=log": {"standard": _entry(_beta_mean(0.44, 5))},
            }
        },
    )
    assert prior_for(COMPRESSION, FULL, "standard", "savings", StoreChain(KEY, home)).mean == (
        pytest.approx(0.44)
    )
    # the session scope is not part of the prior chain
    _write(
        session.session_path(home, KEY, "s-1"),
        {"posteriors": {"compression": {FULL_KEY: {"standard": _entry(_beta_mean(0.9, 50))}}}},
    )
    st = StoreChain(KEY, home, session_id="s-1")
    assert prior_for(COMPRESSION, FULL, "standard", "savings", st).mean == pytest.approx(0.44)


@pytest.mark.parametrize(
    ("point", "arm", "sav", "reg"),
    [
        (COMPRESSION, "light", 0.35, 0.02),
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


def test_safe_arm_epsilon_prior(tmp_path: Path) -> None:
    st = StoreChain(KEY, tmp_path / "h")
    for point in (COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF):
        for kind in ("savings", "regret"):
            p = prior_for(point, {}, point.safe_arm, kind, st)  # type: ignore[arg-type]
            assert p == Beta(hierarchy.EPS, hierarchy.DEFAULT_KAPPA, 0.0)
            assert hierarchy.EPS == 1e-3


def test_unknown_point_or_arm_default(tmp_path: Path) -> None:
    from token_sparen.learn.policy import DecisionPoint

    st = StoreChain(KEY, tmp_path / "h")
    ext = DecisionPoint("ext_point", ("off", "on"), "off", "low")
    assert prior_for(ext, {}, "on", "savings", st).mean == pytest.approx(EPS_MEAN)
    assert prior_for(ext, {}, "on", "regret", st).mean == pytest.approx(0.5)


EPS_MEAN = hierarchy.EPS / (hierarchy.EPS + hierarchy.DEFAULT_KAPPA)


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
    assert prior_for(COMPRESSION, FULL, "light", "regret", st).mean == pytest.approx(0.5)
    _project(home, {"compression": {"*": {"light": _entry(_beta_mean(0.25, 1))}}})
    st = StoreChain(KEY, home, params=params)
    assert prior_for(COMPRESSION, FULL, "light", "savings", st).mean == pytest.approx(0.25)


def _siblings(means: list[float], n: float = 8.0) -> dict[str, Any]:
    return {
        f"band=lt50|cat=c{i}|size=sm|ctype=log|phase=build": {"light": _entry(_beta_mean(m, n))}
        for i, m in enumerate(means)
    }


def test_kappa_moments_clamped(tmp_path: Path) -> None:
    home = tmp_path / "h"
    means = [0.3, 0.5, 0.7, 0.4]
    _project(home, {"compression": _siblings(means)})
    m, var = statistics.fmean(means), statistics.pvariance(means)
    expect = m * (1 - m) / var - 1  # ~ 15.6, inside [2, 20]
    assert 2 < expect < 20
    st = StoreChain(KEY, home)
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) == pytest.approx(expect)
    # widely spread siblings -> tiny kappa -> clamped to 2
    _project(home, {"compression": _siblings([0.01, 0.99, 0.02, 0.98])})
    assert hierarchy.kappa(COMPRESSION, "light", "savings", StoreChain(KEY, home)) == 2.0
    # near-identical siblings -> huge kappa -> clamped to 20
    _project(home, {"compression": _siblings([0.5, 0.5001, 0.4999])})
    assert hierarchy.kappa(COMPRESSION, "light", "savings", StoreChain(KEY, home)) == 20.0
    _project(home, {"compression": _siblings([0.5, 0.5, 0.5])})  # zero variance
    assert hierarchy.kappa(COMPRESSION, "light", "savings", StoreChain(KEY, home)) == 20.0
    # the prior uses the estimated kappa
    _project(
        home,
        {"compression": {**_siblings(means), "ctype=log": {"light": _entry(_beta_mean(0.6, 9))}}},
    )
    p = prior_for(COMPRESSION, FULL, "light", "savings", StoreChain(KEY, home))
    assert p.a + p.b == pytest.approx(expect) and p.mean == pytest.approx(0.6)


def test_kappa_default_with_few_siblings(tmp_path: Path) -> None:
    home = tmp_path / "h"
    # two qualifying siblings, one under-observed, one coarse bucket, one other arm
    pts = _siblings([0.3, 0.7])
    pts["band=gt95|cat=x|size=lg|ctype=log|phase=build"] = {"light": _entry(_beta_mean(0.9, 4))}
    pts["ctype=log"] = {"light": _entry(_beta_mean(0.1, 40))}
    pts["band=gt95|cat=y|size=lg|ctype=log|phase=build"] = {"standard": _entry(_beta_mean(0.9, 40))}
    _project(home, {"compression": pts})
    st = StoreChain(KEY, home)
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) == 6.0
    assert hierarchy.kappa(COMPRESSION, "light", "regret", st) == 6.0  # no regret posteriors
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) == 6.0  # cached path


def test_designed_default_uses_default_kappa(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _project(home, {"compression": _siblings([0.3, 0.5, 0.7, 0.4])})
    st = StoreChain(KEY, home)
    assert hierarchy.kappa(COMPRESSION, "light", "savings", st) != 6.0
    p = prior_for(COMPRESSION, {"ctype": "json"}, "light", "savings", st)
    assert p.mean == pytest.approx(0.35) and p.a + p.b == pytest.approx(6.0)
