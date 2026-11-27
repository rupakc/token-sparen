"""Offline LinTS challenger (M4 Task 7): closed-form posterior, regret constraint, simulation."""

from __future__ import annotations

import ast
import random
import sys
from datetime import date
from pathlib import Path

import pytest

from evals.challengers import lints, lints_sim
from token_sparen.learn.features import FEATURE_KEYS
from token_sparen.learn.policy import COMPRESSION
from token_sparen.replay import sim_lane

ARMS = COMPRESSION.arms
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay"))  # synthetic corpus


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan elimination with partial pivoting (test oracle)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    cols = [_solve(a, [float(i == j) for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def test_lints_matches_closed_form_posterior() -> None:
    rng = random.Random(1)
    dim, lam, v = 6, 2.0, 0.5
    model = lints.LinTS(ARMS, "off", tau=0.05, dim=dim, lam=lam, v=v)
    xs, ss, rs = [], [], []
    for _ in range(40):
        x = [float(rng.random() < 0.4) for _ in range(dim)]
        s, r = rng.random(), float(rng.random() < 0.2)
        model.update("standard", x, s, r)
        xs.append(x), ss.append(s), rs.append(r)
    gram = [[lam * (i == j) + sum(x[i] * x[j] for x in xs) for j in range(dim)] for i in range(dim)]
    for kind, ys in (("savings", ss), ("regret", rs)):
        rhs = [sum(y * x[i] for x, y in zip(xs, ys, strict=True)) for i in range(dim)]
        mean, cov = model.posterior("standard", kind)
        assert mean == pytest.approx(_solve(gram, rhs), abs=1e-9)
        inv = _inverse(gram)
        for i in range(dim):
            assert cov[i] == pytest.approx([v * v * c for c in inv[i]], abs=1e-9)
    # an arm without data keeps the prior: mean 0, covariance v^2 / lam * I
    mean, cov = model.posterior("light", "regret")
    assert mean == [0.0] * dim
    assert cov[0][0] == pytest.approx(v * v / lam) and cov[0][1] == 0.0


def test_lints_respects_regret_constraint() -> None:
    dim = 4
    x = [1.0, 0.0, 1.0, 0.0]
    model = lints.LinTS(ARMS, "off", tau=0.05, dim=dim, v=0.05)
    for _ in range(200):  # aggressive: big savings, always regretted
        model.update("aggressive", x, 1.0, 1.0)
        model.update("light", x, 0.3, 0.0)
    rng = random.Random(7)
    picks = [model.choose(x, rng) for _ in range(500)]
    assert "aggressive" not in picks  # sampled regret far above tau: never feasible
    assert picks.count("light") > 450
    # every trimming arm infeasible -> the safe arm (S = R = 0 by definition)
    for _ in range(200):
        model.update("light", x, 0.3, 1.0)
        model.update("standard", x, 0.9, 1.0)
    assert {model.choose(x, rng) for _ in range(200)} == {"off"}
    with pytest.raises(ValueError):
        model.update("off", x, 0.0, 0.0)  # the safe arm is never updated


def test_lints_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        lints.LinTS(ARMS, "nope", tau=0.05, dim=4)
    with pytest.raises(ValueError):
        lints.LinTS(ARMS, "off", tau=0.05, dim=lints.MAX_DIM + 1)
    model = lints.LinTS(ARMS, "off", tau=0.05, dim=4)
    with pytest.raises(ValueError):
        model.choose([1.0], random.Random(0))


def test_encode_is_one_hot_per_feature_within_32() -> None:
    assert lints.DIM <= 32
    feats = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "debug"}
    x = lints.encode(feats)
    assert len(x) == lints.DIM and sum(x) == len(FEATURE_KEYS["compression"])
    unknown = lints.encode({"band": "weird", "ctype": "brandnew"})
    assert sum(unknown) == len(FEATURE_KEYS["compression"])  # unknown values share a slot


def test_token_sparen_never_imports_evals() -> None:
    pkg = Path(__file__).resolve().parents[2] / "token_sparen"
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(n == "evals" or n.startswith("evals.") for n in names), path


def test_lints_runs_through_the_sim_lane(tmp_path: Path) -> None:
    from test_sim_lane import LEARN, corpus

    src = corpus(tmp_path / "t", sessions=30)
    res = lints_sim.run([src], seed=3, cutoff=date(2026, 11, 1), overlay=LEARN)
    assert res.worker_errors == {}
    view = sim_lane.summary(res.by_split["all"]["sim"])
    cand = view["candidate"]["points"]["compression"]
    assert cand["decisions"] == 150
    first, last = cand["arm_share"]["1"], cand["arm_share"]["3"]
    assert last.get("aggressive", 0.0) < 0.05  # LinTS also learns that aggressive regrets
    assert last.get("aggressive", 0.0) <= first.get("aggressive", 0.0)
    assert cand["proxy_regret"] > 0  # ... from the proxy regret the lane fed it
    # the reference runs the packaged defaults, which never offer plain text for compression
    assert "compression" not in view["reference"]["points"]
    again = lints_sim.run([src], seed=3, cutoff=date(2026, 11, 1), overlay=LEARN, jobs=2)
    assert again.by_split == res.by_split


def test_lints_sim_main_writes_private_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_sim_lane import session

    session("q1", "2026-10-02T09:00:00.000Z").write(tmp_path / "t" / "p" / "a.jsonl")
    out = tmp_path / "private" / "lints.json"
    assert lints_sim.main([str(tmp_path / "t"), "--out", str(out), "--quiet"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("report: ") and " id: " in line
    assert out.is_file()
