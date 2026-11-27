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
KEY_LINE = r"[a-z0-9_.]+: (?:accepted|rejected|unchanged|unmeasured)\n"
SMALL_SPACE = """
[[entry]]
key = "compression.min_saving_ratio"
values = [0.15, 0.3]
point = "compression"

[[entry]]
key = "learn.savings_reward"
values = ["scaled", "fraction"]
point = "compression"

[[entry]]
key = "read_dedupe.max_turn_gap"
values = [4, 8]
point = "read_dedupe"
measured_by = "none"
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


def proposed(
    folder: Path,
    text: str,
    cutoff: str = "2026-11-08",
    dogfood_start: str = "2026-11-26",
) -> Path:
    """A proposal file with the dev report that wrote it (``dev.json`` beside it, its dates)."""
    path = write(folder / "proposal.toml", text)
    doc = {
        "kind": "hillclimb-dev",
        "split": "dev",
        "cutoff": cutoff,
        "dogfood_start": dogfood_start,
        "proposal_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    write(folder / "dev.json", json.dumps(doc))
    return path


def entry(
    key: str, values: Sequence[Any], point: str = "compression", measured_by: str | None = None
) -> str:
    text = f"[[entry]]\nkey = {json.dumps(key)}\nvalues = {json.dumps(list(values))}\n"
    text += f"point = {json.dumps(point)}\n"
    return text + (f"measured_by = {json.dumps(measured_by)}\n" if measured_by else "")


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
        entry("read_dedupe.min_tokens", [2000, 1500], "read_dedupe", "none"),
        entry("guards.pretrim_bounded_keep_ratio", [0.35, 1.2], "pretrim", "none"),
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
    assert keys == [
        "compression.min_saving_ratio",
        "learn.savings_reward",
        "read_dedupe.max_turn_gap",
    ]
    assert m.group(3).endswith("read_dedupe.max_turn_gap: unmeasured\n")
    for name in ("dev.json", "trail.jsonl", "proposal.toml"):
        assert_mode(out / name, 0o600)
    # the confirmation prints the same template
    proposal = proposed(tmp_path / "p", '[learn]\nsavings_reward = "fraction"\n')
    assert hc.main([*args, "--confirm", str(proposal)]) == 0
    captured = capsys.readouterr()
    m = re.fullmatch(
        rf"report: (.+) id: ([0-9a-f]{{12}})\nstatus: complete\n((?:{KEY_LINE})+)", captured.out
    )
    assert m is not None, captured.out
    assert Path(m.group(1)) == (out / "confirm.json").resolve()
    lines = m.group(3).splitlines()
    assert lines[0] == "compression.min_saving_ratio: unchanged"
    # three groups are too few to decide anything: the proposal is rejected (insufficient)
    assert lines[1:] == ["learn.savings_reward: rejected", "read_dedupe.max_turn_gap: unmeasured"]
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
    assert not hc.ledger_path().exists()
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
    proposal = proposed(tmp_path / "p", '[learn]\nsavings_reward = "fraction"\n')
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
    assert doc["insufficient"] is True and doc["accepted"] is False  # 3 groups only
    ledger = json.loads(hc.ledger_path().read_text(encoding="utf-8"))
    (window,) = ledger["windows"]
    assert window["proposal_sha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()
    assert (window["cutoff"], window["dogfood_start"]) == ("2026-11-08", "2026-11-26")
    assert doc["ledger"] == window
    assert "recorded" in window
    assert_mode(hc.ledger_path(), 0o600)
    before = (out / "confirm.json").read_bytes()
    other = proposed(tmp_path / "q", "[compression]\nmin_saving_ratio = 0.3\n")
    elsewhere = [*args[:4], "--out", str(tmp_path / "elsewhere"), "--quiet"]
    for again, argv in ((proposal, args), (other, args), (other, elsewhere)):
        # the same proposal or any other, into the same --out or another: refused
        assert hc.main([*argv, "--confirm", str(again)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == "hillclimb: confirm refused (already run once)"
    assert len(spy.calls) == 1
    assert (out / "confirm.json").read_bytes() == before
    assert not (tmp_path / "elsewhere").exists()


def test_concurrent_confirms_run_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading
    import time

    space = write(tmp_path / "space.toml", SMALL_SPACE)
    proposal = proposed(tmp_path / "p", '[learn]\nsavings_reward = "fraction"\n')
    runs: list[int] = []

    def slow(*_a: Any, **_k: Any) -> runner.RunResult:
        runs.append(1)
        time.sleep(0.3)
        return runner.RunResult(by_split={"test": {}})

    monkeypatch.setattr(runner, "run_parallel", slow)
    codes: list[int] = []
    start = threading.Barrier(4)

    def go(i: int) -> None:
        argv = ["--space", str(space), "--transcripts", str(tmp_path / "none")]
        start.wait()
        codes.append(hc.main([*argv, "--out", str(tmp_path / f"o{i}"), "--confirm", str(proposal)]))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    capsys.readouterr()
    assert len(runs) == 1
    assert sorted(codes) == [0, 2, 2, 2]


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
        self,
        lane: str,
        overlays: Mapping[str, Mapping[str, Any]],
        groups: Sequence[str],
        key: str,
    ) -> dict[str, tuple[hc.Rows, hc.Rows]]:
        assert key == self.key
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
    assert not hc.ledger_path().exists()  # a refused proposal does not use the one shot


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
        space = write(tmp_path / "s.toml", entry(key, [node, lower], key.split(".")[0], "none"))
        with pytest.raises(hc.SpaceError):
            hc.load_space(space)
        higher = node + 1 if isinstance(node, int) else round(node + 0.05, 4)
        space = write(tmp_path / "s.toml", entry(key, [node, higher], key.split(".")[0], "none"))
        assert hc.load_space(space)[0].values == (node, higher)
    # an M3 key the harness has no direction for is refused outright
    space = write(tmp_path / "s.toml", entry("prune.tau", [0.01, 0.02], "prune", "none"))
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
    key = "compression.min_saving_ratio"
    got = ev("compression", {"base": {}, "tight": {key: 0.99}}, groups, key)
    base, ref = got["base"]
    assert sorted(base) == sorted(ref) == groups  # one row per project group
    assert all(r.trims["compression"] > 0 and r.net > 0 for r in base.values())
    assert all(r.trims["compression"] == 0 for r in got["tight"][0].values())  # overlay reached
    assert ev.log.runs == 1 and ev.log.complete and ev.log.sessions_failed == 0
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
    key = "learn.savings_reward"
    sim = ev("sim", {"base": {}, "f": {key: "fraction"}}, groups[:2], key)
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


# ------------------------------------------------------------------ fix round 1


def test_minimum_evidence_or_insufficient() -> None:
    """Below 8 project groups with a trim, or 30 trims per point, nothing is decided."""
    one = {"g0": _row(10.001, trims=100)}
    v = hc.judge(one, {"g0": _row(10.0, trims=100)}, {"g0": _row(10.0, trims=100)}, seed="s")
    assert not v.admissible and v.insufficient and "insufficient:groups" in v.reasons
    three = {f"g{i}": _row(10.0 + 0.01 * (i + 1), trims=40) for i in range(3)}
    flat = {f"g{i}": _row(10.0, trims=40) for i in range(3)}
    v = hc.judge(three, flat, flat, seed="s")
    assert not v.admissible and v.insufficient
    # eight groups, but a group without trims does not count
    groups = [f"g{i}" for i in range(8)]
    cand = {g: _row(12.0, trims=10 if i else 0) for i, g in enumerate(groups)}
    base = {g: _row(10.0, trims=10 if i else 0) for i, g in enumerate(groups)}
    assert hc.judge(cand, base, base, seed="s").insufficient
    # eight groups with a trim each, but fewer than 30 trims at the point
    cand = {g: _row(12.0 + i % 3, trims=3) for i, g in enumerate(groups)}
    base = {g: _row(10.0 + i % 3, trims=3) for i, g in enumerate(groups)}
    v = hc.judge(cand, base, base, seed="s")
    assert v.insufficient and "insufficient:trims:compression" in v.reasons
    # enough: decided on the evidence
    cand = {g: _row(12.0 + i % 3, trims=4) for i, g in enumerate(groups)}
    base = {g: _row(10.0 + i % 3, trims=4) for i, g in enumerate(groups)}
    v = hc.judge(cand, base, base, seed="s")
    assert v.admissible and not v.insufficient
    # an unpaired group never counts (only groups every side reports)
    extra = {**cand, "lonely": _row(1000.0, trims=50)}
    assert hc.judge(extra, base, base, seed="s").stats["unpaired"] == 1
    # the search keeps the current value on insufficient evidence
    key = "compression.min_saving_ratio"
    small = FakeEval(key, {0.2: 4.0}, {})
    assert hc.climb(_coords((key, [0.1, 0.15, 0.2])), small, groups[:3], e2=_ok_e2) == {}


def test_each_level_measured_on_its_own_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = corpus(tmp_path / "t", groups=2, dev=1, test=0)
    spy = Spy(monkeypatch)
    ev = hc.Evaluator([src], jobs=1, seed=0, split="dev")
    moved = {"compression.levels.standard.keep_ratio": 0.3}
    groups = hc._groups([src])

    def lane_params() -> list[dict[str, Any]]:
        return [p for _, p in spy.calls[-1]["specs"]]

    key = "compression.levels.light.keep_ratio"
    ev("compression", {"base": moved, "v": {**moved, key: 0.7}}, groups, key)
    assert {p.get("arm") for p in lane_params()} == {"light"}  # not frozen by standard's move
    key = "compression.levels.aggressive.keep_ratio"
    ev("compression", {"base": moved, "v": {**moved, key: 0.1}}, groups, key)
    assert {p.get("arm") for p in lane_params()} == {"aggressive"}
    # a global key runs the production policy: the engine from packaged priors, no fixed arm
    key = "compression.min_saving_ratio"
    agg = {"compression.levels.aggressive.keep_ratio": 0.1}
    got = ev("compression", {"base": agg, "v": {**agg, key: 0.2}}, groups, key)
    assert all("arm" not in p and p["policy"] == "engine" for p in lane_params())
    assert all(r.trims["compression"] > 0 for r in got["base"][0].values())
    assert ev.log.complete and ev.log.sessions_failed == 0


def test_unmeasured_keys_are_never_run_or_changed(tmp_path: Path) -> None:
    coords = hc.load_space(hc.SPACE)
    unmeasured = {c.key for c in coords if c.lane == "none"}
    assert {"read_dedupe.min_tokens", "guards.max_read_bytes", "loopguard.min_k"} <= unmeasured
    assert all(c.lane in ("compression", "sim") for c in coords if c.key not in unmeasured)
    calls: list[Any] = []

    def never(*a: Any) -> Any:
        calls.append(a)
        raise AssertionError("an unmeasured key must not be run")

    trail: list[dict[str, Any]] = []
    picked = [c for c in coords if c.lane == "none"]
    assert hc.climb(picked, never, ["g"], e2=_ok_e2, trail=trail) == {}
    assert not calls
    assert {r["stage"] for r in trail} == {"unmeasured"}
    # the space must label them: a missing or wrong label is refused
    for text in (
        entry("read_dedupe.max_turn_gap", [4, 8], "read_dedupe"),
        entry("compression.min_saving_ratio", [0.15, 0.2]) + 'measured_by = "none"\n',
        entry("compression.min_saving_ratio", [0.15, 0.2]) + 'measured_by = "sim"\n',
    ):
        with pytest.raises(hc.SpaceError):
            hc.load_space(write(tmp_path / "s.toml", text))
    # a proposal can never change an unmeasured key
    prop = write(tmp_path / "p.toml", "[read_dedupe]\nmax_turn_gap = 4\n")
    with pytest.raises(hc.ProposalError):
        hc.load_proposal(prop, coords)


def test_dev_search_samples_only_groups_with_dev_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = corpus(tmp_path / "t", groups=2)
    session("x0", "2026-11-12T09:00:00.000Z", n=1, log=True).write(src / "late" / "a.jsonl")
    seen: list[list[str]] = []
    real = hc.climb

    def spy(coords: Any, evaluate: Any, groups: Sequence[str], **kw: Any) -> dict[str, Any]:
        seen.append(list(groups))
        return real(coords, evaluate, groups, **kw)

    monkeypatch.setattr(hc, "climb", spy)
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    out = tmp_path / "private"
    argv = ["--space", str(space), "--transcripts", str(src), "--out", str(out), "--quiet"]
    assert hc.main(argv) == 0
    capsys.readouterr()
    assert len(seen) == 1 and len(seen[0]) == 2  # the test-only group is not sampled
    doc = json.loads((out / "dev.json").read_text(encoding="utf-8"))
    sha = hashlib.sha256((out / "proposal.toml").read_bytes()).hexdigest()
    assert doc["proposal_sha256"] == sha
    assert doc["keys"]["read_dedupe.max_turn_gap"] == "unmeasured"


# ------------------------------------------------------------------ M4 final-review fixes


def test_sim_rows_are_averaged_over_at_least_three_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4-G2: every sim evaluation runs >= 3 seeds per group (one pass) and averages them."""
    assert len(hc.SIM_SEEDS) >= 3
    src = corpus(tmp_path / "t", groups=2, dev=2, test=0)
    spy = Spy(monkeypatch)
    ev = hc.Evaluator([src], jobs=1, seed=5, split="dev")
    assert ev.sim_seeds == tuple(5 + s for s in hc.SIM_SEEDS)
    key = "learn.savings_reward"
    got = ev("sim", {"base": {}, "f": {key: "fraction"}}, hc._groups([src]), key)
    assert len(spy.calls) == 1  # every seed in the same pass
    specs = [p for _, p in spy.calls[0]["specs"]]
    for label in ("base", "f"):
        seeds = sorted(p["seed"] for p in specs if p["label"].startswith(f"hc_{label}@"))
        assert seeds == [5 + s for s in hc.SIM_SEEDS]
    cand, ref = got["base"]
    assert cand == ref and sorted(cand) == hc._groups([src])
    # the mean per group, over the groups every seed reports
    a = {"g": hc.Row(1.0, {"compression": 10.0}, {"compression": 1.0}, 1), "h": hc.Row(5.0)}
    b = {"g": hc.Row(3.0, {"compression": 20.0}, {"compression": 0.0}, 0)}
    mean = hc.mean_rows([a, b])
    assert mean == {"g": hc.Row(2.0, {"compression": 15.0}, {"compression": 0.5}, 0.5)}


def test_split_dates_and_campaign_are_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4-G4: ``--cutoff``, ``--dogfood-start`` and ``--campaign`` (a label); the plain replay
    default cutoff is the harness's; a confirmation takes its dates from its dev report."""
    from token_sparen.replay import replay

    assert replay.DEFAULT_CUTOFF == hc.DEV_CUTOFF == date(2026, 11, 8)
    assert runner.DEFAULT_CUTOFF == hc.DEV_CUTOFF
    src = corpus(tmp_path / "t")
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    out = tmp_path / "private"
    spy = Spy(monkeypatch)
    dates = ["--cutoff", "2026-10-04", "--dogfood-start", "2026-12-01"]
    args = ["--space", str(space), "--transcripts", str(src), "--out", str(out), "--quiet"]
    assert hc.main([*args, *dates, "--campaign", "m5"]) == 0
    capsys.readouterr()
    assert spy.calls
    for call in spy.calls:
        assert call["cutoff"] == date(2026, 10, 4)
        assert call["dogfood_start"] == date(2026, 12, 1)
    doc = json.loads((out / "dev.json").read_text(encoding="utf-8"))
    assert (doc["campaign"], doc["cutoff"], doc["dogfood_start"]) == (
        "m5",
        "2026-10-04",
        "2026-12-01",
    )
    # confirm with no date flags: the dev report's dates
    proposal = proposed(
        tmp_path / "p", '[learn]\nsavings_reward = "fraction"\n', "2026-10-04", "2026-12-01"
    )
    spy.calls.clear()
    conf = ["--space", str(space), "--transcripts", str(src), "--quiet"]
    assert hc.main([*conf, "--out", str(tmp_path / "c"), "--confirm", str(proposal)]) == 0
    capsys.readouterr()
    assert [(c["cutoff"], c["dogfood_start"]) for c in spy.calls] == [
        (date(2026, 10, 4), date(2026, 12, 1))
    ]
    doc = json.loads((tmp_path / "c" / "confirm.json").read_text(encoding="utf-8"))
    assert doc["cutoff"] == "2026-10-04" and doc["dogfood_start"] == "2026-12-01"
    # malformed ids or dates are refused before anything runs
    _forbid_runs(monkeypatch)
    for bad in (
        ["--campaign", "../x"],
        ["--campaign", ""],
        ["--campaign", "M4/"],
        ["--cutoff", "2026-13-01"],
        ["--cutoff", "2026-12-01", "--dogfood-start", "2026-11-26"],
    ):
        with pytest.raises(SystemExit) as exc:
            hc.main([*args, *bad, "--confirm", str(proposal)])
        assert exc.value.code == 2
    capsys.readouterr()


def test_test_window_is_read_once_whatever_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-review New-2: one ledger of used test windows [cutoff, dogfood_start); a confirm whose
    window overlaps a recorded one is refused, whatever its campaign label; a confirm whose
    date flags disagree with its dev report (or without a dev report) is refused."""
    space = write(tmp_path / "space.toml", SMALL_SPACE)
    runs: list[Any] = []

    def fake(*_a: Any, **kw: Any) -> runner.RunResult:
        runs.append((kw["cutoff"], kw["dogfood_start"]))
        return runner.RunResult(by_split={"test": {}})

    monkeypatch.setattr(runner, "run_parallel", fake)
    text = '[learn]\nsavings_reward = "fraction"\n'
    n = iter(range(100))

    def go(*extra: str, prop: Path) -> tuple[int, str]:
        argv = ["--space", str(space), "--transcripts", str(tmp_path / "none")]
        out = str(tmp_path / f"o{next(n)}")
        code = hc.main([*argv, "--out", out, "--confirm", str(prop), *extra])
        return code, capsys.readouterr().err.strip()

    refused = "hillclimb: confirm refused (already run once)"
    first = proposed(tmp_path / "a", text)  # 2026-11-08 .. 2026-11-26
    assert go("--campaign", "m4", prop=first) == (0, "")
    # another campaign label, same window
    assert go("--campaign", "m4b", prop=first) == (2, refused)
    # an overlapping window (a later cutoff inside the used one), any label
    inside = proposed(tmp_path / "b", text, "2026-11-20", "2026-12-20")
    assert go("--campaign", "m5", prop=inside) == (2, refused)
    earlier = proposed(tmp_path / "c", text, "2026-10-01", "2026-11-09")
    assert go("--campaign", "m5", prop=earlier) == (2, refused)
    # a later window that does not overlap is allowed, once
    later = proposed(tmp_path / "d", text, "2026-11-26", "2026-12-31")
    assert go("--campaign", "m5", prop=later) == (0, "")
    assert go("--campaign", "m6", prop=later) == (2, refused)
    assert runs == [
        (date(2026, 11, 8), date(2026, 11, 26)),
        (date(2026, 11, 26), date(2026, 12, 31)),
    ]
    windows = json.loads(hc.ledger_path().read_text(encoding="utf-8"))["windows"]
    assert [(w["campaign"], w["cutoff"], w["dogfood_start"]) for w in windows] == [
        ("m4", "2026-11-08", "2026-11-26"),
        ("m5", "2026-11-26", "2026-12-31"),
    ]
    # flags that disagree with the dev report, or no dev report at all: refused, nothing read
    mismatch = "hillclimb: confirm refused (split dates differ from the dev report)"
    fresh = proposed(tmp_path / "e", text, "2027-01-10", "2027-02-01")
    assert go("--cutoff", "2027-01-11", prop=fresh) == (2, mismatch)
    assert go("--dogfood-start", "2027-02-02", prop=fresh) == (2, mismatch)
    lonely = write(tmp_path / "f" / "proposal.toml", text)
    no_dev = "hillclimb: confirm refused (no dev report for this proposal)"
    assert go(prop=lonely) == (2, no_dev)
    write(tmp_path / "f" / "dev.json", json.dumps({"proposal_sha256": "0" * 64}))
    assert go(prop=lonely) == (2, no_dev)
    assert len(runs) == 2 and len(json.loads(hc.ledger_path().read_text())["windows"]) == 2
    # matching flags are fine
    assert go("--cutoff", "2027-01-10", "--dogfood-start", "2027-02-01", prop=fresh) == (0, "")
    # a corrupt ledger refuses (never reset)
    hc.ledger_path().write_text("{not json", encoding="utf-8")
    again = proposed(tmp_path / "g", text, "2028-01-01", "2028-02-01")
    assert go(prop=again) == (2, refused)
    assert len(runs) == 3


LEVEL_SPACE = (
    entry("compression.levels.aggressive.keep_ratio", [0.1, 0.15])
    + entry("compression.levels.light.keep_ratio", [0.6, 0.7])
    + entry("learn.savings_reward", ["scaled", "fraction"])
)


def _confirm_fake(
    monkeypatch: pytest.MonkeyPatch, rows: Mapping[str, hc.Rows]
) -> list[list[runner.LaneSpec]]:
    """Fake test run: ``rows`` per lane label (sim labels: ``(cand, ref)`` via the same table
    under ``<label>:cand`` / ``<label>:ref``); every call's specs are recorded."""
    calls: list[list[runner.LaneSpec]] = []

    def fake(paths: Any, specs: Any, **kw: Any) -> runner.RunResult:
        calls.append(list(specs))
        assert kw["split"] == "test"
        return runner.RunResult(
            by_split={"test": {p["label"]: {"_": p["label"]} for _, p in specs}}
        )

    def sim(raw: Mapping[str, Any]) -> tuple[hc.Rows, hc.Rows]:
        return rows["sim:cand"], rows["sim:ref"]

    def comp(raw: Mapping[str, Any]) -> hc.Rows:
        return rows[raw["_"]]

    monkeypatch.setattr(runner, "run_parallel", fake)
    monkeypatch.setattr(hc, "sim_rows", sim)
    monkeypatch.setattr(hc, "compression_rows", comp)
    monkeypatch.setattr(hc, "e2_gate", _ok_e2)
    return calls


def test_confirm_checks_each_proposed_level_on_its_own_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4-G1: a level key in the proposal is also confirmed on its own arm (compression lane,
    paired by group, in the same single test pass): per-arm proxy regret per trim non-inferior
    (upper bound of the difference <= 0.2 tau) and <= tau; any failure rejects everything."""
    tau = ope.tau_for("compression")
    groups = [f"g{i}" for i in range(12)]
    good = {g: _row(12.0 + i % 4, trims=10, regret=0.2) for i, g in enumerate(groups)}
    ref = {g: _row(10.0 + i % 4, trims=10, regret=0.2) for i, g in enumerate(groups)}
    worse = {g: _row(10.0, trims=10, regret=0.2 + 10 * 0.3 * tau) for g in groups}
    space = write(tmp_path / "space.toml", LEVEL_SPACE)
    coords = hc.load_space(space)
    proposal = proposed(
        tmp_path / "p",
        '[compression.levels.aggressive]\nkeep_ratio = 0.1\n[learn]\nsavings_reward = "fraction"\n',
    )
    table = {
        "sim:cand": good,
        "sim:ref": ref,
        "cmp_aggressive_cand": worse,
        "cmp_aggressive_ref": ref,
    }
    calls = _confirm_fake(monkeypatch, table)
    out = tmp_path / "private"
    path, complete, lines = hc.confirm(coords, proposal, [tmp_path], out, jobs=1, seed=0)
    assert complete and len(calls) == 1  # ONE test read for sim and every arm
    arms = {(p.get("arm"), p["label"]) for name, p in calls[0] if name == "hc_compression"}
    assert arms == {("aggressive", "cmp_aggressive_cand"), ("aggressive", "cmp_aggressive_ref")}
    by_label = {p["label"]: p for _, p in calls[0]}
    assert by_label["cmp_aggressive_ref"]["overlay"] == {}
    assert by_label["cmp_aggressive_cand"]["overlay"] == hc.nest(hc.load_proposal(proposal, coords))
    assert sum(1 for name, _ in calls[0] if name == "sim") == len(hc.SIM_SEEDS)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["accepted"] is False
    assert "arm:aggressive:regret_noninferiority:compression" in doc["verdict"]["reasons"]
    assert "net_savings" not in doc["verdict"]["reasons"]  # the sim verdict itself passed
    assert "aggressive" in doc["arms"]
    assert lines == [
        "compression.levels.aggressive.keep_ratio: rejected",
        "compression.levels.light.keep_ratio: unchanged",
        "learn.savings_reward: rejected",
    ]
    # absolute tau per arm, too
    high = {g: _row(10.0, trims=10, regret=10 * 1.5 * tau) for g in groups}
    hc._E2_CACHE.clear()
    monkeypatch.setattr(hc, "claim_window", lambda sha, *a, **k: {"proposal_sha256": sha})
    table.update({"cmp_aggressive_cand": high, "cmp_aggressive_ref": high})
    path, _, _ = hc.confirm(coords, proposal, [tmp_path], tmp_path / "o2", jobs=1, seed=0)
    reasons = json.loads(path.read_text(encoding="utf-8"))["verdict"]["reasons"]
    assert reasons == ["arm:aggressive:regret_tau:compression"]
    # the arm holds: accepted; a sim-lane-only proposal runs no compression lane at all
    table.update({"cmp_aggressive_cand": good, "cmp_aggressive_ref": ref})
    path, _, lines = hc.confirm(coords, proposal, [tmp_path], tmp_path / "o3", jobs=1, seed=0)
    assert json.loads(path.read_text(encoding="utf-8"))["accepted"] is True
    assert lines[0] == "compression.levels.aggressive.keep_ratio: accepted"
    only_sim = proposed(tmp_path / "q", '[learn]\nsavings_reward = "fraction"\n')
    calls.clear()
    hc.confirm(coords, only_sim, [tmp_path], tmp_path / "o4", jobs=1, seed=0)
    assert [name for name, _ in calls[0]] == ["sim"] * len(hc.SIM_SEEDS)
    # too little evidence on an arm rejects as well
    few = {g: _row(12.0, trims=1, regret=0.0) for g in groups[:3]}
    table.update({"cmp_aggressive_cand": few, "cmp_aggressive_ref": few})
    path, _, _ = hc.confirm(coords, proposal, [tmp_path], tmp_path / "o5", jobs=1, seed=0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["accepted"] is False
    assert any(r.startswith("arm:aggressive:insufficient") for r in doc["verdict"]["reasons"])
