"""E3 off-policy evaluation on synthetic decision logs."""

from __future__ import annotations

import json
import random
import stat
import statistics
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from token_sparen import cli
from token_sparen.config import load_config
from token_sparen.learn import ope
from token_sparen.learn.beta import Evidence
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, DecisionRecord, StaticPolicy

ARMS = COMPRESSION.arms  # off, light, standard, aggressive
CTX = (
    "band=lt50|cat=shell|size=sm|ctype=log|phase=build",
    "band=lt50|cat=read|size=md|ctype=text|phase=explore",
)
# Logging policy, mean savings and regret probability per context and arm.
LOG_P = ((0.4, 0.3, 0.2, 0.1), (0.25, 0.25, 0.25, 0.25))
MU = ((0.0, 0.3, 0.5, 0.7), (0.0, 0.2, 0.45, 0.8))
REG = ((0.0, 0.01, 0.02, 0.04), (0.0, 0.01, 0.03, 0.05))
TARGET_P = ((0.1, 0.2, 0.6, 0.1), (0.0, 0.5, 0.5, 0.0))
LAMBDA = 1 / 0.05  # compression is medium tier
KEY = "0123456789abcdef"


class TableTarget:
    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
        return dict(zip(ARMS, TARGET_P[CTX.index(rec.bucket)], strict=True))


def true_value(lam: float = LAMBDA) -> float:
    total = 0.0
    for c in range(2):
        for a in range(4):
            total += 0.5 * TARGET_P[c][a] * (MU[c][a] - lam * REG[c][a])
    return total


def synth_log(seed: int, n: int = 200) -> tuple[list[DecisionRecord], dict[str, float]]:
    rng = random.Random(seed)
    log: list[DecisionRecord] = []
    regrets: dict[str, float] = {}
    for i in range(n):
        c = rng.randrange(2)
        a = rng.choices(range(4), weights=LOG_P[c])[0]
        s = min(1.0, max(0.0, MU[c][a] + rng.uniform(-0.1, 0.1))) if a else 0.0
        before = 10_000
        did = f"d{seed}-{i}"
        log.append(_rec(did, ARMS[a], LOG_P[c][a], CTX[c], before, before - round(s * before)))
        regrets[did] = 1.0 if rng.random() < REG[c][a] else 0.0
    return log, regrets


def _rec(
    did: str, arm: str, p: float, bucket: str, before: int = 10_000, after: int = 5_000
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=did,
        point="compression",
        arm=arm,
        propensity=p,
        bucket=bucket,
        tokens_before=before,
        tokens_after=after if arm != "off" else before,
        recovery_id=None,
        tool_use_id=None,
        command_sig="Bash",
        ts=0.0,
        ctype="log",
        tool_name="Bash",
    )


def test_ips_unbiased_on_synthetic() -> None:
    truth = true_value()
    covered = 0
    for seed in range(50):  # n = 1000: rare regret events (x 20) make small-n bootstraps skewed
        log, regrets = synth_log(seed, n=1000)
        est = ope.ips(log, TableTarget(), regrets=regrets, seed=seed)
        lo, hi = est.ci95
        covered += lo <= truth <= hi
        assert est.clipped_fraction == 0.0 and est.unsupported_mass == 0.0
    assert covered >= 45


def test_snips_close_to_truth() -> None:
    log, regrets = synth_log(1, n=2000)
    est = ope.snips(log, TableTarget(), regrets=regrets, n_boot=200)
    assert est.value == pytest.approx(true_value(), abs=0.15)


def test_dr_lower_variance_than_ips() -> None:
    """Savings only (no regret noise the model cannot explain): the per (bucket, arm) model
    captures most of the reward, so DR's variance across logs is well below IPS's."""
    ips_vals, dr_vals = [], []
    for seed in range(40):
        log, _ = synth_log(seed)
        model = ope.fit_model(log)
        ips_vals.append(ope.ips(log, TableTarget(), n_boot=50).value)
        dr_vals.append(ope.doubly_robust(log, TableTarget(), model, n_boot=50).value)
    assert statistics.pvariance(dr_vals) < 0.5 * statistics.pvariance(ips_vals)
    assert statistics.mean(dr_vals) == pytest.approx(true_value(lam=0.0), abs=0.02)


def test_reward_uses_lambda_one_over_tau() -> None:
    rec = _rec("d", "light", 0.5, CTX[0], 1000, 600)
    assert ope.reward(rec, None) == pytest.approx(0.4)
    assert ope.reward(rec, 1.0) == pytest.approx(0.4 - 20.0)
    assert ope.reward(_rec("e", "off", 0.5, CTX[0]), None) == 0.0


def test_weights_clipped_and_reported() -> None:
    log = [_rec(f"d{i}", "aggressive", 0.01, CTX[0]) for i in range(4)]
    log += [_rec(f"e{i}", "light", 0.5, CTX[0]) for i in range(4)]

    class AllIn:
        def probs(self, rec: DecisionRecord) -> Mapping[str, float]:
            return {"aggressive": 1.0} if rec.arm == "aggressive" else {"light": 1.0}

    est = ope.ips(log, AllIn(), n_boot=20)
    assert est.clipped_fraction == pytest.approx(0.5)
    # aggressive terms: w = min(100, 20) * 0.5; light terms: 2 * 0.5
    assert est.value == pytest.approx((4 * 20 * 0.5 + 4 * 2 * 0.5) / 8)


def test_unsupported_mass_reported_not_inflated() -> None:
    static_log = [_rec(f"s{i}", "standard", 1.0, "static") for i in range(10)]

    class Half:
        def probs(self, rec: DecisionRecord) -> Mapping[str, float]:
            return {"standard": 0.5, "aggressive": 0.5}

    est = ope.ips(static_log, Half(), n_boot=20)
    assert est.unsupported_mass == pytest.approx(0.5)
    assert est.value == pytest.approx(0.5 * 0.5)  # only the supported half, weight 0.5
    dr = ope.doubly_robust(static_log, Half(), ope.fit_model(static_log), n_boot=20)
    assert dr.unsupported_mass == pytest.approx(0.5)
    assert dr.value == pytest.approx(0.25)  # no model extrapolation onto the unsupported arm


def test_zero_logging_propensity_is_unsupported() -> None:
    log = [_rec("z", "light", 0.0, CTX[0]), _rec("ok", "light", 0.5, CTX[0])]

    class Light:
        def probs(self, rec: DecisionRecord) -> Mapping[str, float]:
            return {"light": 1.0}

    est = ope.ips(log, Light(), n_boot=20)
    assert est.unsupported == 1 and est.n == 2
    assert est.unsupported_mass == pytest.approx(0.5)
    assert est.value == pytest.approx((0.0 + 2 * 0.5) / 2)


def test_no_context_records_counted() -> None:
    log = [_rec("a", "light", 1.0, "error"), _rec("b", "light", 0.5, CTX[0])]

    class NoneForError:
        def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
            return None if rec.bucket == "error" else {"light": 1.0}

    est = ope.ips(log, NoneForError(), n_boot=20)
    assert est.no_context == 1 and est.n == 1


def test_static_target_is_zero_one() -> None:
    target = ope.as_target(StaticPolicy({"compression": "light"}))
    probs = target.probs(_rec("a", "off", 0.5, CTX[0]))
    assert probs == {"light": 1.0}


def _cfg(home: Path) -> Any:
    return load_config(home.parent / "proj", {"TOKEN_SPAREN_HOME": str(home)})


def test_engine_target_uses_exact_propensity(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ev = Evidence(25.0, 25.0, 50.0, 12.5)
    low = Evidence(0.5, 49.5, 50.0, 0.5)
    points = {
        "compression": {CTX[0]: {"light": {"savings": ev.to_list(), "regret": low.to_list()}}}
    }
    pdoc = home / "projects" / KEY / "posteriors.json"
    pdoc.parent.mkdir(parents=True)
    pdoc.write_text(json.dumps({"version": 2, "points": points}), encoding="utf-8")
    cfg = _cfg(home)
    engine = Engine(StoreChain(KEY, home, None, params=cfg.data), cfg, random.Random(0))
    target = ope.as_target(engine)
    rec = _rec("a", "light", 0.5, CTX[0])
    probs = target.probs(rec)
    assert probs is not None
    feats = ope.bucket_features(CTX[0])
    for arm in ARMS:
        assert probs[arm] == engine.propensity(COMPRESSION, feats, arm)
    assert probs["aggressive"] == 0.0  # ladder-locked: exactly zero, never the 1e-3 clamp
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-5)
    assert target.probs(_rec("b", "light", 1.0, "static")) is None


def test_load_log_reads_records_and_regrets(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    rows = [asdict(_rec("a", "light", 0.5, CTX[0])), {**asdict(_rec("b", "off", 0.5, CTX[0]))}]
    rows[1]["regret"] = 1.0
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\nnot json\n" + json.dumps({"x": 1}) + "\n",
        encoding="utf-8",
    )
    log, regrets = ope.load_log([path])
    assert [r.decision_id for r in log] == ["a", "b"]
    assert regrets == {"b": 1.0}


def _write_log(where: Path) -> Path:
    log, regrets = synth_log(3, n=50)
    lines = []
    for r in log:
        row = asdict(r)
        row["regret"] = regrets[r.decision_id]
        lines.append(json.dumps(row))
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return where


def test_ope_cli_writes_private_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    log = _write_log(home / "projects" / KEY / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    for target in ("static", "engine-snapshot"):
        assert cli.main(["ope", "--log", str(log), "--target", target, "--out", str(out)]) == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["target"] == target and "compression" in data["points"]
        point = data["points"]["compression"]
        assert {"ips", "snips", "dr", "n", "clipped_fraction", "unsupported_mass"} <= set(point)
    text = capsys.readouterr().out
    assert KEY not in text and str(tmp_path) not in text and "d3-" not in text


def test_ope_cli_refuses_repo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    log = _write_log(tmp_path / "decisions.jsonl")
    target = cli.PACKAGE_ROOT / "e3-should-not-exist.json"
    assert cli.main(["ope", "--log", str(log), "--target", "static", "--out", str(target)]) != 0
    assert not target.exists()


def test_ope_cli_engine_snapshot_needs_project_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    log = _write_log(tmp_path / "loose" / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    code = cli.main(["ope", "--log", str(log), "--target", "engine-snapshot", "--out", str(out)])
    assert code == 2 and not out.exists()
    assert str(tmp_path) not in capsys.readouterr().err


def test_bucket_features() -> None:
    assert ope.bucket_features(CTX[0]) == {
        "band": "lt50",
        "cat": "shell",
        "size": "sm",
        "ctype": "log",
        "phase": "build",
    }
    assert ope.bucket_features("*") == {}
    assert ope.bucket_features("static") is None and ope.bucket_features("error") is None
    assert ope.bucket_features("garbage") is None


def test_as_target_rejects_unknown() -> None:
    with pytest.raises(TypeError):
        ope.as_target(object())  # type: ignore[arg-type]


def test_empty_log_estimate() -> None:
    est = ope.ips([], TableTarget())
    assert est.n == 0 and est.value == 0.0 and est.ci95 == (0.0, 0.0)
    assert ope.snips([], TableTarget()).value == 0.0
