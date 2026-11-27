"""Hill-climbing harness (M4 Task 9): grids, successive halving by project group, dev-only
search, one-shot test confirmation, fixed stdout template.

Synthetic transcripts only; nothing here reads the real home or real sessions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evals.hillclimb import run as hc
from token_sparen.config import packaged_defaults
from token_sparen.learn import ope
from token_sparen.replay import runner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay"))  # synthetic corpus
from test_replay import assert_mode  # noqa: E402
from test_sim_lane import session  # noqa: E402

M3_KEYS = ("facts.", "prune.", "loopguard.")
KEY_LINE = r"[a-z0-9_.]+: (?:accepted|rejected|unchanged)\n"
SMALL_SPACE = """
[[entry]]
key = "compression.min_saving_ratio"
values = [0.15, 0.3]
point = "compression"

[[entry]]
key = "learn.savings_reward"
values = ["scaled", "fraction"]
point = "compression"
"""


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake home: nothing may fall back to the real one."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "fakehome" / ".token-sparen"))


def corpus(root: Path, groups: int = 3, dev: int = 3, test: int = 2) -> Path:
    """Per group ``dev`` sessions before the M4 cutoff and ``test`` sessions after it."""
    for g in range(groups):
        for i in range(dev + test):
            day = f"2026-10-{2 + i:02d}" if i < dev else f"2026-11-{10 + i:02d}"
            tag = f"{g}{i:02d}"
            session(tag, f"{day}T09:00:00.000Z", n=3, log=True).write(
                root / f"proj{g}" / f"s{tag}.jsonl"
            )
    return root


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def entry(key: str, values: Sequence[Any], point: str = "compression") -> str:
    return f"[[entry]]\nkey = {json.dumps(key)}\nvalues = {json.dumps(list(values))}\n" + (
        f"point = {json.dumps(point)}\n"
    )


class Spy:
    """Records every ``runner.run_parallel`` call (the split it was asked for)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        real = runner.run_parallel

        def spy(paths: Any, specs: Any, **kw: Any) -> runner.RunResult:
            self.calls.append({"specs": list(specs), **kw})
            return real(paths, specs, **kw)

        monkeypatch.setattr(runner, "run_parallel", spy)


def _forbid_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no run may start")

    monkeypatch.setattr(runner, "run_parallel", boom)
    monkeypatch.setattr(hc.e2, "run", boom)


# ------------------------------------------------------------------ Review Focus 4


def test_space_values_within_module_clamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    coords = hc.load_space(hc.SPACE)
    keys = [c.key for c in coords]
    assert len(keys) == len(set(keys))
    for c in coords:
        assert 1 <= len(c.values) <= hc.MAX_VALUES, c.key
        assert c.default in c.values, c.key
        for value in c.values:
            # the owning module's reader honours the value as is (inside its clamp)
            assert hc.module_value(c.key, value) == value, (c.key, value)
        assert not c.key.startswith(("learn.tau.", "ope.")), c.key
    # the required coverage of the grid
    for need in (
        "compression.levels.light.keep_ratio",
        "compression.levels.standard.keep_ratio",
        "compression.levels.aggressive.keep_ratio",
        "compression.min_saving_ratio",
        "read_dedupe.min_tokens",
        "guards.pretrim_bounded_keep_ratio",
        "learn.savings_reward",
        "learn.ladder_retry_decisions",
        "facts.activate_lcb",
        "prune.idle_minutes",
        "loopguard.min_k",
    ):
        assert need in keys, need
    assert any(k.startswith("learn.defaults.") for k in keys)
    # every out-of-bounds or invariant-breaking entry fails before any run
    bad = [
        entry("compression.levels.standard.keep_ratio", [0.35, 1.5]),
        entry("compression.min_saving_ratio", [0.15, -0.1]),
        entry("compression.min_tokens", [2000, 1000]),  # invariant: never below 2000
        entry("read_dedupe.min_tokens", [2000, 1500], "read_dedupe"),
        entry("guards.pretrim_bounded_keep_ratio", [0.35, 1.2], "pretrim"),
        entry("learn.defaults.compression.light.savings", [0.35, 1.3]),
        entry("learn.savings_reward", ["scaled", "bogus"]),
        entry("learn.ladder_retry_decisions", [0, -3]),
        entry("learn.tau.medium", [0.05, 0.1]),  # tau is excluded
        entry("ope.min_trims", [60, 30]),  # [ope] is excluded
        entry("compression.min_saving_ratio", [0.2, 0.3]),  # grid misses the default
        entry("compression.min_saving_ratio", [0.1, 0.12, 0.15, 0.2, 0.25, 0.3]),  # > 5 values
        entry("compression.nonexistent_knob", [1, 2]),
        entry("compression.min_saving_ratio", [0.15, 0.2], "no_such_point"),
    ]
    _forbid_runs(monkeypatch)
    out = tmp_path / "private"
    for i, text in enumerate(bad):
        space = write(tmp_path / f"bad{i}.toml", text)
        with pytest.raises(hc.SpaceError):
            hc.load_space(space)
        code = hc.main(["--space", str(space), "--transcripts", str(tmp_path), "--out", str(out)])
        assert code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == "hillclimb: invalid space (SpaceError)"
    assert not out.exists()


# ------------------------------------------------------------------ Review Focus 5


def test_harness_stdout_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = corpus(tmp_path / "t")
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    out = tmp_path / "private"
    args = ["--space", str(space), "--transcripts", str(src), "--out", str(out), "--quiet"]
    assert hc.main([*args, "--jobs", "2"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    m = re.fullmatch(
        rf"report: (.+) id: ([0-9a-f]{{12}})\nstatus: complete\n((?:{KEY_LINE})+)", captured.out
    )
    assert m is not None, captured.out
    report = Path(m.group(1))
    assert report == (out / "dev.json").resolve()
    assert m.group(2) == hashlib.sha256(report.read_bytes()).hexdigest()[:12]
    keys = [line.split(":")[0] for line in m.group(3).splitlines()]
    assert keys == ["compression.min_saving_ratio", "learn.savings_reward"]
    for name in ("dev.json", "trail.jsonl", "proposal.toml"):
        assert_mode(out / name, 0o600)
    # the confirmation prints the same template
    proposal = write(tmp_path / "p.toml", '[learn]\nsavings_reward = "fraction"\n')
    assert hc.main([*args, "--confirm", str(proposal)]) == 0
    captured = capsys.readouterr()
    m = re.fullmatch(
        rf"report: (.+) id: ([0-9a-f]{{12}})\nstatus: complete\n((?:{KEY_LINE})+)", captured.out
    )
    assert m is not None, captured.out
    assert Path(m.group(1)) == (out / "confirm.json").resolve()
    lines = m.group(3).splitlines()
    assert lines[0] == "compression.min_saving_ratio: unchanged"
    assert lines[1] in ("learn.savings_reward: accepted", "learn.savings_reward: rejected")
    # an unexpected error is reported by its type only
    secret = "secret-session-text-zq"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"{secret} {src}")

    monkeypatch.setattr(runner, "run_parallel", boom)
    code = hc.main([*args[:4], "--out", str(tmp_path / "other"), "--quiet"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "hillclimb failed (RuntimeError)"
    assert secret not in captured.err and str(src) not in captured.err


# ------------------------------------------------------------------ dev only, one-shot test


def test_dev_only_without_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = corpus(tmp_path / "t")
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    out = tmp_path / "private"
    spy = Spy(monkeypatch)
    args = ["--space", str(space), "--transcripts", str(src), "--out", str(out), "--quiet"]
    assert hc.main(args) == 0
    capsys.readouterr()
    assert spy.calls
    for call in spy.calls:
        assert call["split"] == "dev"
        assert call["cutoff"] == date(2026, 11, 8) == hc.DEV_CUTOFF
        assert call["dogfood_start"] == date(2026, 11, 26)
    assert not (out / "confirm.json").exists()
    assert not (out / hc.LEDGER).exists()
    trail = [json.loads(line) for line in (out / "trail.jsonl").read_text().splitlines()]
    assert trail and all(row["split"] == "dev" for row in trail)
    proposal = tomllib.loads((out / "proposal.toml").read_text(encoding="utf-8"))
    assert hc.flatten(proposal).keys() <= {"compression.min_saving_ratio", "learn.savings_reward"}


def test_confirm_runs_once_per_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = corpus(tmp_path / "t")
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    out = tmp_path / "private"
    proposal = write(tmp_path / "p.toml", '[learn]\nsavings_reward = "fraction"\n')
    spy = Spy(monkeypatch)
    args = ["--space", str(space), "--transcripts", str(src), "--out", str(out), "--quiet"]
    assert hc.main([*args, "--confirm", str(proposal)]) == 0
    capsys.readouterr()
    assert [c["split"] for c in spy.calls] == ["test"]  # one paired run, on test only
    assert spy.calls[0]["cutoff"] == hc.DEV_CUTOFF
    doc = json.loads((out / "confirm.json").read_text(encoding="utf-8"))
    assert doc["split"] == "test"
    assert doc["proposal_overlay"] == {"learn.savings_reward": "fraction"}
    assert {"baseline", "proposal", "accepted", "verdict"} <= doc.keys()
    assert doc["baseline"]["groups"] == doc["proposal"]["groups"] == 3  # same paired run
    ledger = json.loads((out / hc.LEDGER).read_text(encoding="utf-8"))
    assert ledger["proposal_sha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()
    assert "recorded" in ledger
    assert_mode(out / hc.LEDGER, 0o600)
    before = (out / "confirm.json").read_bytes()
    other = write(tmp_path / "q.toml", "[compression]\nmin_saving_ratio = 0.3\n")
    for again in (proposal, other):  # the same proposal or any other: refused
        assert hc.main([*args, "--confirm", str(again)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == "hillclimb: confirm refused (already run once)"
    assert len(spy.calls) == 1
    assert (out / "confirm.json").read_bytes() == before


# ------------------------------------------------------------------ the search


def _coords(*specs: tuple[str, Sequence[Any]]) -> list[hc.Coordinate]:
    return [hc.coordinate(key, list(values), "compression") for key, values in specs]


def _row(net: float, trims: float = 10.0, regret: float = 0.0, trips: int = 0) -> hc.Row:
    return hc.Row(
        net=net, trims={"compression": trims}, regret={"compression": regret}, trips=trips
    )


class FakeEval:
    """Per group, net savings = value's gain (the key's value -> gain table) + small noise."""

    def __init__(self, key: str, gain: Mapping[Any, float], regret: Mapping[Any, float]) -> None:
        self.key, self.gain, self.regret = key, gain, regret
        self.calls: list[tuple[str, dict[str, dict[str, Any]], list[str]]] = []

    def __call__(
        self, lane: str, overlays: Mapping[str, Mapping[str, Any]], groups: Sequence[str]
    ) -> dict[str, tuple[hc.Rows, hc.Rows]]:
        self.calls.append((lane, {k: dict(v) for k, v in overlays.items()}, list(groups)))
        out: dict[str, tuple[hc.Rows, hc.Rows]] = {}
        for label, overlay in overlays.items():
            value = overlay.get(self.key)
            cand = {
                g: _row(10.0 + self.gain.get(value, 0.0) + i % 3, regret=self.regret.get(value, 0))
                for i, g in enumerate(groups)
            }
            ref = {g: _row(10.0 + i % 3) for i, g in enumerate(groups)}
            out[label] = (cand, ref)
        return out


def _ok_e2(overlay: Mapping[str, Any]) -> tuple[bool, list[str]]:
    return True, []


def test_successive_halving_by_group_keeps_best() -> None:
    key = "compression.levels.standard.keep_ratio"
    (coord,) = _coords((key, [0.25, 0.3, 0.35, 0.4, 0.5]))
    groups = [f"g{i:02d}" for i in range(16)]
    fake = FakeEval(key, {0.25: 1.0, 0.3: 3.0, 0.4: 2.0, 0.5: 0.5}, {})
    trail: list[dict[str, Any]] = []
    proposal = hc.climb([coord], fake, groups, seed=7, e2=_ok_e2, trail=trail)
    assert proposal == {key: 0.3}
    (s1, o1, g1), (s2, o2, g2), (s3, o3, g3) = fake.calls[:3]  # pass 1
    assert s1 == s2 == s3 == "compression"
    # 25 % -> top half on 50 % -> the best on 100 %, nested samples of project groups
    assert len(g1) == 4 and len(g2) == 8 and sorted(g3) == groups
    assert set(g1) <= set(g2) <= set(g3)
    assert sorted(o[key] for o in o1.values() if key in o) == [0.25, 0.3, 0.4, 0.5]
    assert sorted(o[key] for o in o2.values() if key in o) == [0.3, 0.4]
    assert [o[key] for o in o3.values() if key in o] == [0.3]
    # every comparison carries the current point (the defaults: an empty overlay)
    assert all({} in list(o.values()) for o in (o1, o2, o3))
    # seeded: the same seed draws the same groups, another seed others
    again = FakeEval(key, fake.gain, {})
    hc.climb([coord], again, groups, seed=7, e2=_ok_e2)
    assert [c[2] for c in again.calls[:3]] == [g1, g2, g3]
    other = FakeEval(key, fake.gain, {})
    hc.climb([coord], other, groups, seed=8, e2=_ok_e2)
    assert other.calls[0][2] != g1
    stages = [r["stage"] for r in trail if r["pass"] == 1 and r["value"] == 0.3]
    assert stages == ["25%", "50%", "100%", "verdict"]
    # the best on screening is inadmissible on 100 % (regret above tau): the default stays
    risky = FakeEval(key, fake.gain, {0.3: 5.0})  # 0.5 regret per trim > tau
    assert hc.climb([coord], risky, groups, seed=7, e2=_ok_e2) == {}

    # a value that fails E2 is never run
    def e2_no(overlay: Mapping[str, Any]) -> tuple[bool, list[str]]:
        return overlay.get(key) != 0.3, ["e2"]

    gated = FakeEval(key, fake.gain, {})
    assert hc.climb([coord], gated, groups, seed=7, e2=e2_no) == {key: 0.4}
    assert all(o.get(key) != 0.3 for c in gated.calls for o in c[1].values())


def test_second_pass_only_if_first_changed() -> None:
    key = "compression.min_saving_ratio"
    coords = _coords((key, [0.1, 0.15, 0.2]))
    groups = [f"g{i}" for i in range(8)]
    flat = FakeEval(key, {}, {})
    assert hc.climb(coords, flat, groups, e2=_ok_e2) == {}
    first = len(flat.calls)
    moved = FakeEval(key, {0.2: 4.0}, {})
    assert hc.climb(coords, moved, groups, e2=_ok_e2) == {key: 0.2}
    assert len(moved.calls) > first  # a second pass ran (and then stopped: cap 2)
    assert len(moved.calls) <= 2 * 3


def test_values_only_from_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "compression.levels.light.keep_ratio"
    coords = _coords((key, [0.5, 0.6, 0.7]))
    groups = [f"g{i}" for i in range(8)]
    # the search only ever proposes grid values
    fake = FakeEval(key, {0.7: 2.0, 0.65: 9.0}, {})
    proposal = hc.climb(coords, fake, groups, e2=_ok_e2)
    assert proposal == {key: 0.7}
    assert all(o.get(key, 0.6) in (0.5, 0.6, 0.7) for c in fake.calls for o in c[1].values())
    # a confirmation of an off-grid value (or an unknown key) is refused before anything runs
    space = write(tmp_path / "space.toml", entry(key, [0.5, 0.6, 0.7]))
    out = tmp_path / "private"
    _forbid_runs(monkeypatch)
    for text in (
        "[compression.levels.light]\nkeep_ratio = 0.65\n",
        "[compression]\nmin_saving_ratio = 0.3\n",
    ):
        prop = write(tmp_path / "p.toml", text)
        args = ["--space", str(space), "--transcripts", str(tmp_path), "--out", str(out)]
        assert hc.main([*args, "--confirm", str(prop), "--quiet"]) == 2
        captured = capsys.readouterr()
        assert captured.err.strip() == "hillclimb: invalid proposal (ProposalError)"
        with pytest.raises(hc.ProposalError):
            hc.load_proposal(prop, hc.load_space(space))
    assert not (out / hc.LEDGER).exists()  # a refused proposal does not use the one shot


def test_m3_keys_only_conservative_direction(tmp_path: Path) -> None:
    defaults = packaged_defaults()
    for c in hc.load_space(hc.SPACE):
        if c.key.startswith(M3_KEYS):
            assert c.key in hc.CONSERVATIVE, c.key
            assert all(v >= c.default for v in c.values), c.key
    for key in hc.CONSERVATIVE:
        node: Any = defaults
        for part in key.split("."):
            node = node[part]
        lower = node - 1 if isinstance(node, int) else round(node - 0.05, 4)
        space = write(tmp_path / "s.toml", entry(key, [node, lower], key.split(".")[0]))
        with pytest.raises(hc.SpaceError):
            hc.load_space(space)
        higher = node + 1 if isinstance(node, int) else round(node + 0.05, 4)
        space = write(tmp_path / "s.toml", entry(key, [node, higher], key.split(".")[0]))
        assert hc.load_space(space)[0].values == (node, higher)
    # an M3 key the harness has no direction for is refused outright
    space = write(tmp_path / "s.toml", entry("prune.tau", [0.01, 0.02], "prune"))
    with pytest.raises(hc.SpaceError):
        hc.load_space(space)


# ------------------------------------------------------------------ the acceptance rule


def test_judge_applies_the_acceptance_rule() -> None:
    tau = ope.tau_for("compression")
    groups = [f"g{i}" for i in range(12)]
    ref = {g: _row(10.0 + i % 4, regret=0.2) for i, g in enumerate(groups)}  # 0.02 per trim
    better = {g: _row(12.0 + i % 4, regret=0.2) for i, g in enumerate(groups)}
    v = hc.judge(better, ref, ref, seed="s")
    assert v.admissible, v.reasons
    assert v.stats["net_diff_lb"] > 0
    assert math.isclose(v.stats["points"]["compression"]["regret_per_trim"], 0.02)
    # no gain: the lower bound is not above 0
    assert not hc.judge(ref, ref, ref, seed="s").admissible
    # a mixed gain whose interval covers 0
    mixed = {g: _row(10.0 + i % 4 + (3 if i % 2 else -2.9)) for i, g in enumerate(groups)}
    assert "net_savings" in hc.judge(mixed, ref, ref, seed="s").reasons
    # regret per trim worse than the defaults by more than 0.2 tau (still below tau)
    worse = {g: _row(12.0 + i % 4, regret=0.2 + 10 * 0.3 * tau) for i, g in enumerate(groups)}
    v = hc.judge(worse, ref, ref, seed="s")
    assert "regret_noninferiority:compression" in v.reasons
    # above tau in absolute terms
    high = {g: _row(12.0 + i % 4, regret=10 * 1.5 * tau) for i, g in enumerate(groups)}
    hi_ref = {g: _row(10.0 + i % 4, regret=10 * 1.5 * tau) for i, g in enumerate(groups)}
    assert hc.judge(high, hi_ref, hi_ref, seed="s").reasons == ["regret_tau:compression"]
    # breaker trips above the reference engine's
    trips = {g: _row(12.0 + i % 4, regret=0.2, trips=1) for i, g in enumerate(groups)}
    assert hc.judge(trips, ref, ref, seed="s", breaker=True).reasons == ["breaker_trips"]
    assert hc.judge(trips, ref, ref, seed="s", breaker=False).admissible
    # seeded and resampled >= 2000 times
    assert hc.RESAMPLES >= 2000
    assert hc.judge(better, ref, ref, seed="s").stats == hc.judge(better, ref, ref, seed="s").stats


def test_e2_gate_needs_full_pass_and_inline_rate() -> None:
    ok, reasons = hc.e2_gate({})
    assert ok and reasons == []
    ok, reasons = hc.e2_gate({"compression.levels.standard.keep_ratio": 0.2})
    assert not ok and any(r.startswith("e2_inline:") for r in reasons)


def test_lanes_report_paired_rows_per_group(tmp_path: Path) -> None:
    src = corpus(tmp_path / "t")
    ev = hc.Evaluator([src], jobs=1, seed=0, split="dev")
    groups = hc._groups([src])
    assert len(groups) == 3
    got = ev("compression", {"base": {}, "tight": {"compression.min_saving_ratio": 0.99}}, groups)
    base, ref = got["base"]
    assert sorted(base) == sorted(ref) == groups  # one row per project group
    assert all(r.trims["compression"] > 0 and r.net > 0 for r in base.values())
    assert all(r.trims["compression"] == 0 for r in got["tight"][0].values())  # overlay reached
    assert ev.log.runs == 1 and ev.log.complete
    # a level's key is measured on that level's arm (the safe arm never trims)
    res = runner.run_parallel(
        [src],
        [("hc_compression", {"module": hc.MODULE, "arm": "off", "label": "off"})],
        jobs=1,
        cutoff=hc.DEV_CUTOFF,
        split="dev",
    )
    rows = hc.compression_rows(res.by_split["dev"]["off"])
    assert sorted(rows) == groups and all(r.trims["compression"] == 0 for r in rows.values())
    sim = ev("sim", {"base": {}, "f": {"learn.savings_reward": "fraction"}}, groups[:2])
    cand, ref = sim["base"]
    assert sorted(cand) == sorted(ref) == sorted(groups[:2])
    assert cand == ref  # no overlay: the candidate is the reference (common random numbers)


def test_write_private_raw_text(tmp_path: Path) -> None:
    from token_sparen.replay import report

    path = report.write_private(tmp_path / "p" / "x.toml", "a = 1\n", raw=True)
    assert path.read_text(encoding="utf-8") == "a = 1\n"
    assert_mode(path, 0o600)
    with pytest.raises(TypeError):
        report.write_private(tmp_path / "p" / "y.toml", {"a": 1}, raw=True)
