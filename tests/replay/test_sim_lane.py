"""E1 simulation lane: the live learning engine run chronologically over synthetic transcripts in a
throw-away state root (M4 Task 7).

Synthetic transcripts only; nothing here reads the real home or real sessions.
"""

from __future__ import annotations

import builtins
import io
import os
import random
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_replay import Session, big_output

from token_sparen.adapters.claude import settings
from token_sparen.config import Config, packaged_defaults
from token_sparen.events import SessionCtx
from token_sparen.facts import miners
from token_sparen.learn.policy import COMPRESSION, Decision, DecisionPoint
from token_sparen.mechanisms import prune
from token_sparen.replay import runner, sim_lane
from token_sparen.replay.runner import apply_overlay

CUTOFF = date(2026, 11, 1)
ALLOW = ["diff", "search", "test_report", "compiler", "build_log", "json", "pplx", "log", "text"]
# aggressive looks best a priori and every arm is eligible at once (no ladder wait), so the
# learner has to find out from regret alone that aggressive hurts on this corpus
LEARN: dict[str, Any] = {
    "compression": {"allow_ctypes": ALLOW},
    "learn": {
        "ladder_obs": 0,
        "defaults": {
            "compression": {
                "light": {"savings": 0.3, "regret": 0.01},
                "standard": {"savings": 0.4, "regret": 0.01},
                "aggressive": {"savings": 0.95, "regret": 0.01},
            }
        },
    },
}
DEFAULTS = Config(data=packaged_defaults())
_WORDS = "alpha beta gamma delta lorem ipsum dolor amet copy move sync file slot node".split()


def output(tag: str, k: int, log: bool = False) -> tuple[str, tuple[str, str]]:
    """~4300 tokens of text (``log``: a ~6000-token log, a content type the packaged defaults
    offer); line 35 (kept by light and standard, elided by aggressive) holds two distinctive
    tokens unique to this output."""
    a, b = f"zqvarlok{tag}x{k}", f"plimtrex{tag}y{k}"
    if not log:
        return big_output(seed=k + 7, extra={35: f"note {a} linked with {b}"}), (a, b)
    rng = random.Random(k)
    lines = [
        f"2026-10-01T09:{i // 60:02d}:{i % 60:02d}Z INFO worker{i % 5} "
        + " ".join(rng.choice(_WORDS) for _ in range(6))
        for i in range(400)
    ]
    lines[35] = f"2026-10-01T09:00:35Z INFO worker0 note {a} linked with {b}"
    return "\n".join(lines), (a, b)


def session(
    tag: str,
    ts: str,
    n: int = 5,
    cite: bool = True,
    sidechain: bool = False,
    log: bool = False,
) -> Session:
    """``n`` large Bash outputs; with ``cite`` the agent then names the line-35 tokens (a full
    proxy regret exactly when the trim elided that line, i.e. under ``aggressive``)."""
    s = Session(f"s{tag}", ts).prompt("please run the checks")
    for k in range(n):
        text, (a, b) = output(tag, k, log)
        s.call(f"m{k}a", tool=(f"t{k}", "Bash", {"command": f"make step{k}"}), sidechain=sidechain)
        s.result(f"t{k}", text, sidechain=sidechain)
        if cite:
            s.call(f"m{k}b", text=f"The run shows {a} together with {b}.", sidechain=sidechain)
        s.prompt(f"continue with part {k}")
    return s


def corpus(
    root: Path, sessions: int = 30, groups: int = 1, cite: bool = True, log: bool = False
) -> Path:
    for g in range(groups):
        for i in range(sessions):
            ts = f"2026-10-{1 + i % 28:02d}T{9 + i // 28:02d}:00:00.000Z"
            tag = f"{g}{i:02d}"
            session(tag, ts, cite=cite, log=log).write(root / f"proj{g}" / f"s{tag}.jsonl")
    return root


def run(
    root: Path,
    *,
    jobs: int = 1,
    split: str = "all",
    cfg: Config = DEFAULTS,
    overlay: Mapping[str, Any] | None = LEARN,
    **params: Any,
) -> runner.RunResult:
    """The sim lane; the candidate gets ``overlay`` (default ``LEARN``), the reference runs the
    packaged defaults (``cfg``, the run's config, is ignored by the lane)."""
    if overlay is not None:
        params["overlay"] = dict(overlay)
    return runner.run_parallel(
        [root], [("sim", params)], jobs=jobs, cutoff=CUTOFF, split=split, cfg=cfg
    )


def raw(res: runner.RunResult, split: str = "all") -> dict[str, Any]:
    out: dict[str, Any] = res.by_split[split]["sim"]
    return out


def comp(view: Mapping[str, Any], role: str = "candidate") -> dict[str, Any]:
    out: dict[str, Any] = view[role]["points"]["compression"]
    return out


# ------------------------------------------------------------------ the required behaviours


def test_sim_learns_to_avoid_regretting_arm(tmp_path: Path) -> None:
    res = run(corpus(tmp_path / "t"), seed=3)
    assert res.worker_errors == {}
    view = sim_lane.summary(raw(res))
    row = comp(view)
    assert row["decisions"] == 150
    first, last = row["arm_share"]["1"], row["arm_share"]["3"]
    assert first.get("aggressive", 0.0) > 0.0  # it was tried: the prior made it look best
    assert last.get("aggressive", 0.0) < 0.05
    assert last.get("aggressive", 0.0) < first["aggressive"]
    assert row["proxy_regret_per_trim"]["3"] < row["proxy_regret_per_trim"]["1"]
    # every proxy regret the compression accounting saw was fed back to the learner
    assert row["proxy_regret"] == pytest.approx(view["candidate"]["acc"]["proxy_regrets"])
    assert row["savings_tokens"] > 0


def test_sim_keeps_a_harmless_arm(tmp_path: Path) -> None:
    """Control: without citations nothing is regretted and aggressive keeps winning."""
    res = run(corpus(tmp_path / "t", cite=False), seed=3)
    row = comp(sim_lane.summary(raw(res)))
    assert row["arm_share"]["3"].get("aggressive", 0.0) > 0.5
    assert row["proxy_regret"] == 0


class _Recorder:
    """Raises (and records) on any call: the simulation must never reach it."""

    def __init__(self, calls: list[str], name: str) -> None:
        self.calls, self.name = calls, name

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.calls.append(self.name)
        raise AssertionError(f"{self.name} called")

    def __getattr__(self, attr: str) -> Any:
        return _Recorder(self.calls, f"{self.name}.{attr}")


def _fd_path(fd: int) -> str:
    """The path of an open directory descriptor (macOS ``F_GETPATH``, Linux ``/proc``)."""
    import fcntl

    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:
        raw = fcntl.fcntl(fd, getpath, bytes(1024))
        return os.fsdecode(raw.split(b"\0", 1)[0])
    return os.readlink(f"/proc/self/fd/{fd}")


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


@contextmanager
def writes_only_under(mp: pytest.MonkeyPatch, roots: list[str]) -> Iterator[list[str]]:
    """Every file write, directory creation, rename or removal outside ``roots`` (a list that
    may grow while the guard is on) is recorded."""
    bad: list[str] = []

    def check(p: Any, dir_fd: int | None = None) -> None:
        if isinstance(p, int):
            return
        name = os.fsdecode(os.fspath(p))
        if dir_fd is not None:  # relative to an open directory (shutil.rmtree)
            name = os.path.join(_fd_path(dir_fd), name)
        a = os.path.realpath(name)
        if "__pycache__" in a:
            return
        if not any(a == r or a.startswith(r + os.sep) for r in roots):
            bad.append(a)

    def writing(mode: Any) -> bool:
        return isinstance(mode, str) and any(c in mode for c in "wax+")

    def wrap_open(fn: Any) -> Any:
        def inner(p: Any, mode: Any = "r", *args: Any, **kw: Any) -> Any:
            if writing(mode):
                check(p)
            return fn(p, mode, *args, **kw)

        return inner

    def wrap_os_open(fn: Any) -> Any:
        def inner(p: Any, flags: int, *args: Any, **kw: Any) -> Any:
            if flags & _WRITE_FLAGS:
                check(p, kw.get("dir_fd"))
            return fn(p, flags, *args, **kw)

        return inner

    def wrap_path(fn: Any, n: int = 1) -> Any:
        def inner(*args: Any, **kw: Any) -> Any:
            for p in args[:n]:
                check(p, kw.get("dir_fd") or kw.get("src_dir_fd"))
            return fn(*args, **kw)

        return inner

    with mp.context() as m:
        m.setattr(builtins, "open", wrap_open(builtins.open))
        m.setattr(io, "open", wrap_open(io.open))
        m.setattr(os, "open", wrap_os_open(os.open))
        for name in ("mkdir", "makedirs", "remove", "unlink", "rmdir"):
            m.setattr(os, name, wrap_path(getattr(os, name)))
        for name in ("replace", "rename"):
            m.setattr(os, name, wrap_path(getattr(os, name), 2))
        yield bad


def test_sim_never_writes_outside_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = corpus(tmp_path / "t", sessions=4)
    calls: list[str] = []
    for name in ("mine_transcript", "mine_calls", "record", "declared_packages", "git_ignored"):
        monkeypatch.setattr(miners, name, _Recorder(calls, f"miners.{name}"))
    for name in ("add_deny_rules", "remove_deny_rules", "install_statusline"):
        monkeypatch.setattr(settings, name, _Recorder(calls, f"settings.{name}"))
    monkeypatch.setattr(prune, "WRITER", _Recorder(calls, "prune.WRITER"))
    home = tmp_path / "fakehome"  # a fake user home (never the real one)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    state = tmp_path / "state"  # the user's state root: never touched either
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(state))
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    roots: list[str] = []
    made = sim_lane.tempfile.mkdtemp

    def mkdtemp(*args: Any, **kw: Any) -> str:
        path = made(*args, **kw)
        roots.append(os.path.realpath(path))
        return path

    monkeypatch.setattr(sim_lane.tempfile, "mkdtemp", mkdtemp)
    with writes_only_under(monkeypatch, roots) as bad:
        res = run(src, seed=1)
    assert [b for b in bad if b not in roots] == []  # (mkdtemp creates the root itself)
    assert calls == []
    assert roots and not any(os.path.exists(r) for r in roots)  # deleted afterwards
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before
    assert not state.exists()
    row = raw(res)
    assert row["sessions"] == 4 and row["sessions_failed"] == 0
    assert comp(sim_lane.summary(row))["decisions"] == 20


def test_sim_reproducible_with_seed(tmp_path: Path) -> None:
    src = corpus(tmp_path / "t", sessions=8, groups=3)
    a = run(src, seed=5)
    b = run(src, seed=5)
    parallel = run(src, seed=5, jobs=3)
    assert a.worker_errors == parallel.worker_errors == {}
    assert a.by_split == b.by_split == parallel.by_split
    other = run(src, seed=6)
    assert other.by_split != a.by_split  # the seed drives the draws


# ------------------------------------------------------------------ pairing and reporting


def test_reference_runs_current_defaults_paired_per_group(tmp_path: Path) -> None:
    src = corpus(tmp_path / "t", sessions=4, groups=2, log=True, cite=False)
    same = sim_lane.pairs(raw(run(src, seed=2, overlay=None)))
    assert len(same) == 2
    for row in same.values():  # no overlay: the same learner with the same draws
        assert row["candidate"] == row["reference"]
        assert row["sessions_failed"] == 0
    held = raw(run(src, seed=2, overlay={"learn": {"hold": {"compression": True}}}))
    view = sim_lane.summary(held)
    assert comp(view)["savings_tokens"] == 0  # the proposal holds compression ...
    assert comp(view, "reference")["savings_tokens"] > 0  # ... current defaults trim
    for row in sim_lane.pairs(held).values():
        cand, ref = row["candidate"]["compression"], row["reference"]["compression"]
        assert cand["decisions"] == ref["decisions"] == 20  # same outputs, decided by both
        assert cand["net_savings"] == 0 and ref["net_savings"] > 0
        assert set(cand) >= {"net_savings", "net_savings_tokens", "proxy_regret_per_trim"}


def test_report_fields_per_split_and_point(tmp_path: Path) -> None:
    src = tmp_path / "t"
    for i in range(3):
        d, e = f"2026-10-0{i + 1}T09:00:00.000Z", f"2026-11-1{i}T09:00:00.000Z"
        session(f"d{i}", d, log=True).write(src / "p" / f"d{i}.jsonl")
        session(f"e{i}", e, log=True).write(src / "p" / f"e{i}.jsonl")
    res = run(src, seed=4)
    for part in ("dev", "test"):
        view = sim_lane.summary(raw(res, part))
        assert view["sessions"] == 3
        for role in ("candidate", "reference"):
            row = comp(view, role)
            assert row["decisions"] == 15
            assert set(row) >= {
                "decisions",
                "arm_share",
                "savings_tokens",
                "net_savings",
                "net_savings_tokens",
                "proxy_regret_per_trim",
                "breaker_trips",
            }
            assert set(row["arm_share"]) == {"1", "2", "3"}
            assert set(row["proxy_regret_per_trim"]) == {"1", "2", "3", "all"}
            assert sum(row["arm_share"]["1"].values()) == pytest.approx(1.0)
    # the test split ran with what dev taught (history), but counts only its own sessions:
    test_only = sim_lane.summary(raw(run(src, seed=4, split="test")))
    assert test_only == sim_lane.summary(raw(res, "test"))


def test_breaker_trips_are_counted(tmp_path: Path) -> None:
    trips = {
        "learn": {
            "breaker_min_obs": 5,
            "breaker_every": 5,
            "breaker_lcb_q": 0.2,
            "defaults": {"compression": {"aggressive": {"savings": 0.95, "regret": 0.0}}},
            "kappa_regret": 200,
        }
    }
    overlay = apply_overlay(Config(data=LEARN), trips).data
    res = run(corpus(tmp_path / "t", sessions=6), seed=1, overlay=overlay)
    view = sim_lane.summary(raw(res))
    assert comp(view)["breaker_trips"] >= 1
    assert view["candidate"]["breaker_trips"] == comp(view)["breaker_trips"]


# ------------------------------------------------------------------ injected choosers

CALLS: list[tuple[str, str, float | None, float | None]] = []


class AlwaysAggressive:
    """A chooser for tests: always ``aggressive``; records every reward it receives."""

    def __init__(self, cfg: Config, env: Mapping[str, str], home: Path, seed: str) -> None:
        self.n = 0

    def start_session(self, session_id: str, project_key: str, now: float) -> None:
        pass

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        self.n += 1
        return Decision(f"d{self.n}", point.name, "aggressive", 1.0, "test")

    def reward(self, decision: Decision, savings: float | None, regret: float | None) -> None:
        CALLS.append((decision.decision_id, decision.arm, savings, regret))

    def outcome(self, decision: Decision, value: float, signal: str) -> None:
        pass

    def end_session(self) -> list[str]:
        return []


def test_chooser_factory_by_path_rewards_each_decision_once(tmp_path: Path) -> None:
    CALLS.clear()
    src = corpus(tmp_path / "t", sessions=3)
    res = run(src, seed=0, chooser_factory="test_sim_lane:AlwaysAggressive")
    assert res.worker_errors == {}
    row = comp(sim_lane.summary(raw(res)))
    assert row["arm_share"]["3"] == {"aggressive": 1.0}
    savings = [c for c in CALLS if c[2] is not None]
    regrets = [c for c in CALLS if c[3] is not None]
    assert len(savings) == len(regrets) == 15  # exactly once per decision, each kind
    assert len({c[0] for c in regrets}) == 15
    assert all(c[3] == 1.0 for c in regrets)  # every aggressive trim elided the cited line
    assert all(0.0 < (c[2] or 0.0) <= 1.0 for c in savings)
    assert row["proxy_regret_per_trim"]["all"] == 1.0
    # the reference is the production engine on the packaged defaults, which never offer plain
    # text to the compression point: no decision at all
    assert "compression" not in sim_lane.summary(raw(res))["reference"]["points"]


def test_bad_chooser_factory_rejected() -> None:
    with pytest.raises(ValueError, match="module:callable"):
        sim_lane.SimLane(DEFAULTS, {"chooser_factory": "no-colon-here"})


def test_trims_without_distinctive_tokens_still_regret_on_rerun(tmp_path: Path) -> None:
    """Every trim is watched for a re-run of its command, as live: one with no distinctive
    token is regretted (1.0) by the re-run, one never re-run is paid 0 once (expiry or end)."""
    CALLS.clear()
    s = Session("sflat", "2026-10-02T09:00:00.000Z").prompt("go")
    flat = "\n".join(f"line {i % 7} alpha beta gamma delta lorem ipsum" for i in range(900))
    s.call("m1", tool=("t1", "Bash", {"command": "make flat"})).result("t1", flat)
    s.prompt("again")
    s.call("m2", tool=("t2", "Bash", {"command": "make flat"})).result("t2", flat)
    for i in range(8):
        s.prompt(f"step {i}")
    s.call("m3", tool=("t3", "Bash", {"command": "make other"})).result("t3", flat)
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    res = run(tmp_path / "t", chooser_factory="test_sim_lane:AlwaysAggressive")
    assert res.worker_errors == {}
    regrets = [(c[0], c[3]) for c in CALLS if c[3] is not None]
    assert regrets == [("d1", 1.0), ("d2", 0.0), ("d3", 0.0)]
    view = sim_lane.summary(raw(res))
    assert comp(view)["decisions"] == 3 and comp(view)["proxy_regret"] == 1.0
    assert comp(view)["proxy_regret"] == view["candidate"]["acc"]["proxy_regrets"]


def test_savings_reward_uses_the_live_resend_estimate(tmp_path: Path) -> None:
    """M4-G2: the learner is rewarded with the savings the live hook would compute, i.e. with
    the governor's projected re-sends (``regret.expected_resends(governor.remaining_turns)``),
    paid at trim time; the actual later re-sends only price the reported net savings.

    Every synthetic call burns 1110 context tokens; with a 11100-token budget and the per-turn
    EWMA at 1110 after one closed turn, the second trim projects (11100 - 3330) / 1110 = 7
    re-sends. The first trim comes before any closed turn: unknown, 10 (``DEFAULT_RESENDS``)."""
    CALLS.clear()
    s = Session("sres", "2026-10-02T09:00:00.000Z").prompt("go")
    s.call("m1", tool=("t1", "Bash", {"command": "make a"})).result("t1", output("r", 1)[0])
    s.prompt("next")  # closes the first turn: EWMA = 1110 (one call)
    s.call("m2", text="one").call("m3", tool=("t2", "Bash", {"command": "make b"}))
    s.result("t2", output("r", 2)[0])
    s.call("m4", text="three")
    s.compact().call("m5", text="after the compaction")
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    overlay = apply_overlay(
        Config(data=LEARN),
        {
            "learn": {"savings_reward": "counterfactual", "resend_horizon": 20},
            "governor": {"session_budget_tokens": 11100},
        },
    ).data
    res = run(tmp_path / "t", overlay=overlay, chooser_factory="test_sim_lane:AlwaysAggressive")
    assert res.worker_errors == {}
    savings = {c[0]: c[2] for c in CALLS if c[2] is not None}
    assert savings["d1"] == pytest.approx(10 / 20)  # no closed turn yet: the hook's default
    assert savings["d2"] == pytest.approx(7 / 20)  # the governor's projection, not the 1 actual
    # the reported objective still prices the ACTUAL later calls before the compaction
    acc = sim_lane.summary(raw(res))["candidate"]["acc"]
    removed = comp(sim_lane.summary(raw(res)))["savings_tokens"]
    assert removed > 0
    # trim 1 is re-sent by m2, m3, m4 (3 calls), trim 2 by m4 only (1 call)
    assert acc["tokens_resent_removed"] < 3 * removed


def test_run_config_never_shapes_either_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both roles come from the packaged defaults: the run's config (a user config.toml with
    learning off and other keep ratios, TOKEN_SPAREN_* env) changes nothing."""
    src = corpus(tmp_path / "t", sessions=3, log=True)
    plain = run(src, seed=7)
    user = apply_overlay(
        DEFAULTS,
        {
            "learn": {"enabled": False},
            "compression": {"levels": {"light": {"keep_ratio": 0.9}}, "allow_ctypes": ["text"]},
        },
    )
    monkeypatch.setenv("TOKEN_SPAREN_LEARN__ENABLED", "false")
    assert run(src, seed=7, cfg=user).by_split == plain.by_split
    view = sim_lane.summary(raw(plain))
    assert comp(view, "reference")["decisions"] == 15  # log is offered by the defaults


def test_failed_session_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = corpus(tmp_path / "t", sessions=2)

    def boom(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(sim_lane._SimSession, "on_prompt", boom)
    res = raw(run(src, seed=0))
    assert res["sessions_failed"] == 2 and res["sessions"] == 0
    for row in sim_lane.pairs(res).values():
        assert row["sessions_failed"] == 2  # per group, so such pairs can be dropped


def test_lane_registered_under_sim() -> None:
    assert runner.lane_factory("sim") is sim_lane.SimLane
    assert sim_lane.SimLane.history is True
    assert COMPRESSION.name in sim_lane.POINTS


def test_cli_replay_runs_the_sim_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from token_sparen import cli

    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "state"))
    src = corpus(tmp_path / "t", sessions=2)
    out = tmp_path / "private" / "rep.json"
    args = ["replay", str(src), "--lanes", "sim", "--out", str(out), "--quiet", "--jobs", "1"]
    assert cli.main([*args, "--cutoff", CUTOFF.isoformat()]) == 0
    assert capsys.readouterr().out.startswith("report: ")
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["lanes"]["sim"]["dev"]["sessions"] == 2
