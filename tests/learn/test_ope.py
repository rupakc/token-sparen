"""E3 off-policy evaluation on synthetic decision logs."""

from __future__ import annotations

import json
import os
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
        code = cli.main(["ope", "--log", str(log), "--target", target, "--out", str(out)])
        assert code == 0, capsys.readouterr().err
        if os.name == "posix":  # Windows: inherited directory ACLs (see replay.report)
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


def test_snips_is_conditional_on_support_mixed_logger() -> None:
    """Half the decisions come from a static logger (standard, p = 1), half from a uniform one.
    The target splits standard/aggressive; SNIPS must not fill the unsupported aggressive mass
    of the static rows with the supported rows' value."""
    rng = random.Random(5)
    log: list[DecisionRecord] = []
    for i in range(400):
        if i % 2:
            log.append(_rec(f"s{i}", "standard", 1.0, CTX[0], 10_000, 5_000))
        else:
            arm = rng.choice(ARMS)
            after = {"off": 10_000, "light": 8_000, "standard": 5_000, "aggressive": 2_000}[arm]
            log.append(_rec(f"u{i}", arm, 0.25, CTX[1], 10_000, after))

    class Half:
        def probs(self, rec: DecisionRecord) -> Mapping[str, float]:
            return {"standard": 0.5, "aggressive": 0.5}

    est = ope.snips(log, Half(), n_boot=50)
    assert est.rows == 200  # normalised over fully supported rows only
    assert est.value == pytest.approx(0.5 * 0.5 + 0.5 * 0.8, abs=0.08)
    assert est.to_dict()["conditional_on_support"] is True
    assert est.unsupported_mass == pytest.approx(0.25)


def test_engine_target_honours_learn_disabled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cfg = load_config(home.parent / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    cfg.data.setdefault("learn", {})["enabled"] = False
    engine = Engine(StoreChain(KEY, home, None, params=cfg.data), cfg, random.Random(0))
    probs = ope.as_target(engine).probs(_rec("a", "light", 0.5, CTX[0]))
    assert probs == {"standard": 1.0}


def test_ope_cli_holdout_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    early = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    late = datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    rows = [{**asdict(_rec(f"e{i}", "light", 0.5, CTX[0])), "ts": early} for i in range(5)]
    rows += [{**asdict(_rec(f"l{i}", "light", 0.5, CTX[0])), "ts": late} for i in range(3)]
    log = tmp_path / "decisions.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "priv" / "e3.json"
    args = ["ope", "--log", str(log), "--target", "static", "--out", str(out)]
    code = cli.main([*args, "--holdout-after", "2026-11-10"])
    assert code == 0, capsys.readouterr().err
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["points"]["compression"]["logged"] == 3
    assert data["holdout_after"] == "2026-11-10" and "in-sample" in json.dumps(data["notes"])
    assert cli.main([*args, "--holdout-after", "bad"]) == 2


# --------------------------------------------------------------------------- M2 close C4
def true_parts() -> tuple[float, float]:
    sav = reg = 0.0
    for c in range(2):
        for a in range(4):
            sav += 0.5 * TARGET_P[c][a] * MU[c][a]
            reg += 0.5 * TARGET_P[c][a] * REG[c][a]
    return sav, reg


def test_savings_and_regret_reported_separately_with_cis() -> None:
    log, regrets = synth_log(11, n=4000)
    res = ope.evaluate(log, TableTarget(), regrets=regrets, n_boot=200)["compression"]
    sav, reg = true_parts()
    for est in ("ips", "snips", "dr"):
        s, r, c = res["savings"][est], res["regret"][est], res["combined"][est]
        assert s["ci95"][0] <= s["value"] <= s["ci95"][1]
        assert r["ci95"][0] <= r["value"] <= r["ci95"][1]
        assert abs(s["value"] - sav) < 0.03, (est, s, sav)
        assert abs(r["value"] - reg) < 0.01, (est, r, reg)
        assert c == res[est]  # the old keys stay the combined objective
    # IPS is linear in the reward: combined = savings - regret / tau, decision by decision
    combined = res["savings"]["ips"]["value"] - LAMBDA * res["regret"]["ips"]["value"]
    assert abs(res["ips"]["value"] - combined) < 1e-5
    assert res["tau"] == 0.05 and res["objective"] == "savings - regret / tau"


def test_constraint_ok_uses_the_pessimistic_per_trim_upper_bound() -> None:
    log, regrets = synth_log(12, n=3000)
    res = ope.evaluate(log, TableTarget(), regrets=regrets, n_boot=200)["compression"]
    pess = res["regret_per_trim"]["pessimistic"]
    upper = max(pess[e]["ci95"][1] for e in ("ips", "dr"))
    assert res["constraint_upper"] == round(upper, 6)
    assert res["constraint_ok"] is (upper <= 0.05)
    assert res["constraint_ok"] is True and res["constraint_status"] == "ok"  # ~0.02 per trim
    tight = ope.evaluate(log, TableTarget(), regrets=regrets, n_boot=200, tau={"medium": 0.005})[
        "compression"
    ]
    assert tight["constraint_ok"] is False and tight["tau"] == 0.005
    assert tight["constraint_status"] == "violated"
    assert tight["regret"] == res["regret"]  # tau only moves the bar and the combined value


def test_constraint_fails_when_the_target_is_regretted() -> None:
    log = [_rec(f"d{i}", "standard", 0.5, CTX[0]) for i in range(200)]
    regrets = {f"d{i}": 1.0 if i % 5 == 0 else 0.0 for i in range(200)}  # 20 % regret
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=100)["compression"]
    assert res["regret"]["ips"]["value"] > 0.3  # weight 2 on every row
    assert res["constraint_ok"] is False


def test_safe_target_always_satisfies_the_constraint() -> None:
    log, regrets = synth_log(13, n=500)
    pol = StaticPolicy({"compression": "off"})
    res = ope.evaluate(log, pol, regrets=regrets, n_boot=50)["compression"]
    assert res["regret"]["ips"]["value"] == 0.0 and res["savings"]["ips"]["value"] == 0.0
    assert res["constraint_ok"] is True


def test_reward_parts() -> None:
    rec = _rec("x", "standard", 0.5, CTX[0], 10_000, 4_000)
    assert ope.reward(rec, 0.5, part="savings") == pytest.approx(0.6)
    assert ope.reward(rec, 0.5, part="regret") == 0.5
    assert ope.reward(rec, 0.5) == pytest.approx(0.6 - 0.5 * LAMBDA)
    safe = _rec("y", "off", 0.5, CTX[0])
    assert ope.reward(safe, 1.0, part="regret") == 0.0
    with pytest.raises(ValueError, match="part"):
        ope.reward(rec, 0.0, part="nope")


def test_ope_cli_reports_parts_and_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(_beside_home(tmp_path))
    log = _write_log(home / "projects" / KEY / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    assert cli.main(["ope", "--log", str(log), "--target", "static", "--out", str(out)]) == 0
    point = json.loads(out.read_text(encoding="utf-8"))["points"]["compression"]
    keys = {"savings", "regret", "combined", "tau", "constraint_ok", "constraint_status"}
    assert keys | {"regret_per_trim", "outcome_coverage", "unsupported_trim_fraction"} <= set(point)
    text = capsys.readouterr().out
    assert "savings" in text and "regret" in text and "constraint" in text


def test_ope_cli_uses_the_configured_tau(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("[learn.tau]\nmedium = 0.001\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(_beside_home(tmp_path))
    log = _write_log(home / "projects" / KEY / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    assert cli.main(["ope", "--log", str(log), "--target", "static", "--out", str(out)]) == 0
    point = json.loads(out.read_text(encoding="utf-8"))["points"]["compression"]
    assert point["tau"] == 0.001


def test_regret_known_fraction_counts_only_decisions_needing_an_outcome() -> None:
    log = [_rec(f"s{i}", "off", 0.5, CTX[0]) for i in range(6)]  # safe arm: no outcome ever
    log += [_rec(f"t{i}", "standard", 0.5, CTX[0]) for i in range(4)]
    regrets = {"t0": 0.0, "t1": 1.0, "t2": 0.0}
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=10)["compression"]
    assert res["regret_known_fraction"] == 0.75  # 3 of the 4 trimming decisions
    only_safe = ope.evaluate(log[:6], StaticPolicy({}), n_boot=10)["compression"]
    assert only_safe["regret_known_fraction"] == 1.0  # nothing needs an outcome


# --------------------------------------------------------------------------- fix round 1: per trim
def _mixed_log(
    n: int, regret_rate: float, coverage: float
) -> tuple[list[DecisionRecord], dict[str, float]]:
    """A stochastic logger (off / standard at 0.5 each); trims regretted at ``regret_rate``;
    only the first ``coverage`` share of trimming decisions has a settled outcome."""
    log: list[DecisionRecord] = []
    regrets: dict[str, float] = {}
    trims = 0
    n_trims = n // 2
    for i in range(n):
        arm = "standard" if i % 2 else "off"
        log.append(_rec(f"m{i}", arm, 0.5, CTX[0]))
        if arm == "standard":
            if trims < round(coverage * n_trims):
                regrets[f"m{i}"] = 1.0 if (trims % 10) < round(regret_rate * 10) else 0.0
            trims += 1
    return log, regrets


class MixTarget:
    def __init__(self, trim: float) -> None:
        self.trim = trim

    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
        return {"off": 1.0 - self.trim, "standard": self.trim}


def test_probe_a_no_support_is_insufficient() -> None:
    """A deterministic logger that never trimmed cannot vouch for a target that trims."""
    log = [_rec(f"a{i}", "off", 1.0, "static") for i in range(300)]
    res = ope.evaluate(log, StaticPolicy({}), n_boot=50)["compression"]
    assert res["unsupported_trim_fraction"] == 1.0
    assert res["constraint_ok"] is None and res["constraint_status"] == "insufficient"


@pytest.mark.parametrize("coverage", [0.1, 0.2, 1.0])
def test_probe_b_true_regret_point_three_is_never_ok(coverage: float) -> None:
    log, regrets = _mixed_log(400, 0.3, coverage)
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=200)["compression"]
    assert res["constraint_ok"] is not True
    assert res["outcome_coverage"] == pytest.approx(coverage, abs=0.01)
    if coverage < 0.8:
        assert res["constraint_status"] == "insufficient" and res["constraint_ok"] is None
    else:
        assert res["constraint_status"] == "violated"
        assert res["regret_per_trim"]["pessimistic"]["ips"]["value"] == pytest.approx(0.3)


def test_probe_c_rare_trims_with_high_regret_are_not_ok() -> None:
    """Trimming 10 % of the time at 40 % regret averages to 0.04 per decision (under the
    0.05 bar) but is 0.4 per trim."""
    log, regrets = _mixed_log(1000, 0.4, 1.0)
    res = ope.evaluate(log, MixTarget(0.1), regrets=regrets, n_boot=200)["compression"]
    assert res["regret"]["ips"]["value"] < 0.05  # the per-decision average would pass
    per_trim = res["regret_per_trim"]
    assert per_trim["pessimistic"]["ips"]["value"] == pytest.approx(0.4)
    assert per_trim["pessimistic"]["dr"]["value"] == pytest.approx(0.4, abs=0.02)
    assert res["constraint_ok"] is False and res["constraint_status"] == "violated"


def test_optimistic_and_pessimistic_bounds() -> None:
    log, regrets = _mixed_log(400, 0.0, 0.9)  # settled trims never regretted; 10 % unsettled
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=200)["compression"]
    per_trim = res["regret_per_trim"]
    assert per_trim["optimistic"]["ips"]["value"] == 0.0
    assert per_trim["pessimistic"]["ips"]["value"] == pytest.approx(0.1)
    assert res["constraint_ok"] is False  # 0.1 pessimistic > 0.05
    full, regrets_full = _mixed_log(400, 0.0, 1.0)
    ok = ope.evaluate(full, StaticPolicy({}), regrets=regrets_full, n_boot=200)["compression"]
    assert ok["constraint_ok"] is True and ok["constraint_status"] == "ok"


def test_target_that_never_trims_passes() -> None:
    log, regrets = _mixed_log(100, 0.5, 0.1)
    res = ope.evaluate(log, StaticPolicy({"compression": "off"}), regrets=regrets, n_boot=20)
    point = res["compression"]
    assert point["constraint_ok"] is True and point["constraint_status"] == "no_trims"


def test_thresholds_are_parameters() -> None:
    log, regrets = _mixed_log(400, 0.0, 0.5)
    lax = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=50, min_outcome_coverage=0.4)[
        "compression"
    ]
    assert lax["constraint_status"] == "violated"  # pessimistic 0.5 > tau
    strict = ope.evaluate(
        log, StaticPolicy({}), regrets=regrets, n_boot=50, max_unsupported_trim=-1.0
    )["compression"]
    assert strict["constraint_status"] == "insufficient"


def test_ope_thresholds_are_user_only(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    env = {"TOKEN_SPAREN_HOME": str(home)}
    assert load_config(proj, env).get("ope.min_outcome_coverage") == 0.8
    assert load_config(proj, env).get("ope.max_unsupported_trim") == 0.2
    assert load_config(proj, env).get("ope.min_trims") == 60
    (proj / "token-sparen.toml").write_text("[ope]\nmin_outcome_coverage = 0.1\n", "utf-8")
    assert load_config(proj, env).get("ope.min_outcome_coverage") == 0.8
    (home / "config.toml").write_text("[ope]\nmin_outcome_coverage = 0.5\n", "utf-8")
    assert load_config(proj, env).get("ope.min_outcome_coverage") == 0.5


def test_ope_cli_reads_the_thresholds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("[ope]\nmin_outcome_coverage = 1.01\n", "utf-8")
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(_beside_home(tmp_path))
    log = _write_log(home / "projects" / KEY / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    assert cli.main(["ope", "--log", str(log), "--target", "static", "--out", str(out)]) == 0
    point = json.loads(out.read_text(encoding="utf-8"))["points"]["compression"]
    assert point["constraint_status"] == "insufficient"


def test_ope_cli_engine_snapshot_joins_outcomes_across_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decisions in the rotated generation, their outcomes in the current one: 12/12 joined."""
    home = tmp_path / "home"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(_beside_home(tmp_path))
    pdir = home / "projects" / KEY
    pdir.mkdir(parents=True)
    old = [json.dumps(asdict(_rec(f"r{i}", "light", 0.5, CTX[0]))) for i in range(12)]
    new = [
        json.dumps({"kind": "outcome", "decision_id": f"r{i}", "regret": 0.0, "signal": "expiry"})
        for i in range(12)
    ]
    (pdir / "decisions.jsonl.1").write_text("\n".join(old) + "\n", encoding="utf-8")
    (pdir / "decisions.jsonl").write_text("\n".join(new) + "\n", encoding="utf-8")
    out = tmp_path / "priv" / "e3.json"
    for target in ("engine-snapshot", "static"):
        args = ["ope", "--log", str(pdir / "decisions.jsonl.1"), str(pdir / "decisions.jsonl")]
        assert cli.main([*args, "--target", target, "--out", str(out)]) == 0
        point = json.loads(out.read_text(encoding="utf-8"))["points"]["compression"]
        assert point["logged"] == 12 and point["regret_known_fraction"] == 1.0, target


# --------------------------------------------------------------------------- fix round 2
@pytest.mark.parametrize("n_trims", [1, 3, 10, 40])
def test_few_zero_regret_trims_are_insufficient(n_trims: int) -> None:
    """A collapsed [0, 0] bootstrap must not certify a target on a handful of trims."""
    log, regrets = _mixed_log(2 * n_trims, 0.0, 1.0)
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=200)["compression"]
    assert res["constraint_status"] == "insufficient" and res["constraint_ok"] is None


def test_min_trims_is_a_parameter_and_the_exact_bound_still_applies() -> None:
    log, regrets = _mixed_log(2 * 40, 0.0, 1.0)  # 40 trims, none regretted
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=200, min_trims=1)
    point = res["compression"]
    # bootstrap collapses to [0, 0]; the exact bound for 0/40 is 1 - 0.05 ** (1 / 40) ~ 0.072
    assert point["regret_per_trim"]["pessimistic"]["ips"]["ci95"] == [0.0, 0.0]
    assert point["constraint_upper"] == pytest.approx(1 - 0.05 ** (1 / 40), abs=1e-4)
    assert point["constraint_ok"] is False and point["constraint_status"] == "violated"


def test_probe_d_many_safe_trims_are_ok() -> None:
    log, regrets = _mixed_log(2 * 2000, 0.0, 1.0)
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=100)["compression"]
    assert res["constraint_status"] == "ok" and res["constraint_ok"] is True
    assert res["constraint_upper"] == pytest.approx(1 - 0.05 ** (1 / 2000), abs=1e-4)


class AggressiveTarget:
    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
        return {"off": 0.1, "aggressive": 0.9}


def test_probe_f_unseen_arm_of_a_stochastic_logger_is_unsupported() -> None:
    """The logger (off / standard at 0.5) never picked aggressive: the target's 0.9 on it is
    unsupported mass, not an unbiased zero."""
    log, regrets = _mixed_log(400, 0.0, 1.0)
    res = ope.evaluate(log, AggressiveTarget(), regrets=regrets, n_boot=50)["compression"]
    assert res["unsupported_trim_fraction"] == pytest.approx(1.0)  # all trimming mass unseen
    assert res["constraint_status"] == "insufficient" and res["constraint_ok"] is None


def test_arm_support_is_per_point_and_bucket() -> None:
    log, regrets = _mixed_log(400, 0.0, 1.0)  # bucket CTX[0] only
    log += [_rec(f"x{i}", "aggressive", 0.5, CTX[1]) for i in range(10)]
    regrets.update({f"x{i}": 0.0 for i in range(10)})
    res = ope.evaluate(log, AggressiveTarget(), regrets=regrets, n_boot=20)["compression"]
    # aggressive is seen in CTX[1] only: the CTX[0] rows' 0.9 stays unsupported
    assert res["unsupported_trim_fraction"] == pytest.approx(400 / 410, abs=1e-6)


def test_ope_cli_reads_min_trims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("[ope]\nmin_trims = 100000\n", "utf-8")
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(_beside_home(tmp_path))
    log, regrets = _mixed_log(400, 0.0, 1.0)
    path = home / "projects" / KEY / "decisions.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{**asdict(r), "regret": regrets.get(r.decision_id)} for r in log]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    out = tmp_path / "priv" / "e3.json"
    assert cli.main(["ope", "--log", str(path), "--target", "static", "--out", str(out)]) == 0
    point = json.loads(out.read_text(encoding="utf-8"))["points"]["compression"]
    assert point["constraint_status"] == "insufficient"


def _beside_home(tmp_path: Path) -> Path:
    """A working directory beside (not containing) the state root (review M5)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return cwd


def test_ope_quiet_stdout_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib
    import re

    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    log = _write_log(tmp_path / "home" / "projects" / KEY / "decisions.jsonl")
    out = tmp_path / "priv" / "e3.json"
    args = ["ope", "--log", str(log), "--target", "static", "--out", str(out), "--quiet"]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    m = re.fullmatch(r"report: (.+) id: ([0-9a-f]{12})\n", captured.out)
    assert m is not None, captured.out
    assert Path(m.group(1)) == out.resolve()
    assert m.group(2) == hashlib.sha256(out.read_bytes()).hexdigest()[:12]
