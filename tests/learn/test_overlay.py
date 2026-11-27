"""Session fold into the project and global stores, pretrim candidates, learner state (Task 22)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from token_sparen import cli
from token_sparen.adapters.claude import entry
from token_sparen.adapters.claude.translate import tool_category
from token_sparen.config import Config, load_config
from token_sparen.events import Event, SessionCtx, ToolCall
from token_sparen.learn import overlay, regret
from token_sparen.learn.beta import Evidence
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION
from token_sparen.mechanisms import guards
from token_sparen.store import jsonstore, session

KEY = "0123456789abcdef"
SID = "s-1"
FULL = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
FULL_KEY = "band=lt50|cat=shell|size=sm|ctype=log|phase=build"
COARSE = ["cat=shell|ctype=log", "ctype=log", "*"]


# --------------------------------------------------------------------------- helpers
def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _pdir(home: Path) -> Path:
    return home / "projects" / KEY


def _ov(home: Path, sid: str = SID) -> Path:
    return session.session_path(home, KEY, sid)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _cell(sav: Evidence, reg: Evidence | None = None) -> dict[str, Any]:
    out = {"savings": sav.to_list()}
    if reg is not None:
        out["regret"] = reg.to_list()
    return out


def _session(
    home: Path,
    sid: str = SID,
    evidence: Mapping[str, Any] | None = None,
    outcomes: list[list[Any]] | None = None,
    **extra: Any,
) -> Path:
    path = _ov(home, sid)
    doc: dict[str, Any] = dict(extra)
    if evidence is not None:
        doc["evidence"] = evidence
    if outcomes is not None:
        doc["regret_outcomes"] = outcomes
    _write(path, doc)
    return path


def _ev(path: Path, point: str, bucket: str, arm: str, kind: str = "savings") -> Evidence | None:
    doc = _read(path)
    raw = doc.get("points", {}).get(point, {}).get(bucket, {}).get(arm, {}).get(kind)
    return None if raw is None else Evidence.from_list(raw)


def _proj_ev(home: Path, bucket: str = FULL_KEY, arm: str = "standard", kind: str = "savings"):  # type: ignore[no-untyped-def]
    return _ev(_pdir(home) / "posteriors.json", "compression", bucket, arm, kind)


def _glob_ev(home: Path, bucket: str = FULL_KEY, arm: str = "standard", kind: str = "savings"):  # type: ignore[no-untyped-def]
    return _ev(home / "global" / "posteriors.json", "compression", bucket, arm, kind)


def _close(a: Evidence | None, b: Evidence, tol: float = 1e-9) -> bool:
    assert a is not None
    return all(abs(x - y) <= tol for x, y in zip(a.to_list(), b.to_list(), strict=True))


SAV = Evidence(0.6, 0.4, 1.0, 0.36)
REG = Evidence(0.0, 1.0, 1.0, 0.0)
ONE = {"compression": {FULL_KEY: {"standard": _cell(SAV, REG)}}}


def _fold(home: Path, sid: str = SID, **kw: Any) -> overlay.FoldReport:
    return overlay.fold(sid, KEY, home, **kw)


# --------------------------------------------------------------------------- brief tests
def test_fold_idempotent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, evidence=ONE)
    first = _fold(home)
    assert first.staged and first.cells == 2
    second = _fold(home)
    assert not second.staged and second.cells == 0
    assert _close(_proj_ev(home), SAV)
    assert _close(_proj_ev(home, kind="regret"), REG)
    doc = _read(_ov(home))
    assert doc["evidence"] == {} and isinstance(doc["folded_at"], float)
    assert "folding" not in doc
    g = _glob_ev(home)
    assert g is not None and abs(g.n - 0.3) < 1e-12  # not doubled either


def test_global_receives_scaled_delta_only(tmp_path: Path) -> None:
    home = _home(tmp_path)
    big = Evidence(20.0, 10.0, 30.0, 15.0)
    _write(
        _pdir(home) / "posteriors.json",
        {"version": 2, "points": {"compression": {FULL_KEY: {"standard": _cell(big)}}}},
    )
    _session(home, evidence=ONE)
    _fold(home)
    want = Evidence(0.6 * 0.3, 0.4 * 0.3, 0.3, 0.36 * 0.3)
    for bucket in [FULL_KEY, *COARSE]:
        assert _close(_glob_ev(home, bucket), want), bucket  # the delta, never the project total
    assert _close(_proj_ev(home), big + SAV)
    text = (home / "global" / "posteriors.json").read_text(encoding="utf-8")
    assert KEY not in text and str(tmp_path) not in text and SID not in text


def _fold_worker(args: tuple[str, str]) -> bool:
    home, sid = args
    return overlay.fold(sid, KEY, Path(home)).staged


def test_parallel_sessions_fold_without_loss(tmp_path: Path) -> None:
    home = _home(tmp_path)
    sids = [f"p-{i}" for i in range(6)]
    for sid in sids:
        _session(home, sid, evidence=ONE)
    with ProcessPoolExecutor(max_workers=6) as pool:
        staged = list(pool.map(_fold_worker, [(str(home), s) for s in sids]))
    assert all(staged)
    got = _proj_ev(home)
    assert got is not None and got.n == 6.0 and abs(got.ea - 3.6) < 1e-9
    g = _glob_ev(home)
    assert g is not None and abs(g.n - 1.8) < 1e-9


def test_concurrent_folds_of_same_session_count_once(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, evidence=ONE)
    with ProcessPoolExecutor(max_workers=4) as pool:
        staged = list(pool.map(_fold_worker, [(str(home), SID)] * 4))
    assert staged.count(True) == 1
    got = _proj_ev(home)
    assert got is not None and got.n == 1.0
    star = _proj_ev(home, "*")
    assert star is not None and star.n == 1.0


# --------------------------------------------------------------------------- double counting
def _cfg(home: Path, extra: dict[str, Any] | None = None) -> Config:
    cfg = load_config(home.parent / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    for k, v in (extra or {}).items():
        cfg.data.setdefault(k, {}).update(v)
    return cfg


def _engine(home: Path, seed: int = 3) -> Engine:
    cfg = _cfg(home, {"learn": {"floor": {"medium": 0.0}}})
    return Engine(StoreChain(KEY, home, SID, params=cfg.data), cfg, random.Random(seed))


def test_decide_reward_fold_decide_sees_evidence_once(tmp_path: Path) -> None:
    home = _home(tmp_path)
    ctx = SessionCtx(SID, KEY, 1, None, None, _ov(home))
    eng = _engine(home)
    d = eng.decide(COMPRESSION, FULL, ctx)
    for _ in range(50):
        if d.arm != "off":
            break
        d = eng.decide(COMPRESSION, FULL, ctx)
    assert d.arm != "off"
    eng.reward(d.decision_id, 0.6, 0.0)
    before = _engine(home)._evidence("compression", FULL_KEY, d.arm, "savings")
    assert before.n == 1.0
    _fold(home)
    after = _engine(home)._evidence("compression", FULL_KEY, d.arm, "savings")
    assert _close(after, before)  # project + session after the fold == session before it
    _fold(home)
    again = _engine(home)._evidence("compression", FULL_KEY, d.arm, "savings")
    assert _close(again, before)


def test_staged_fold_after_crash_applies_once(tmp_path: Path) -> None:
    home = _home(tmp_path)
    folding = {"id": "f" * 32, "evidence": ONE, "outcomes": [], "t": time.time()}
    _session(home, evidence={}, folding=folding)
    assert _fold(home).cells == 2
    assert _proj_ev(home).n == 1.0  # type: ignore[union-attr]
    # crash between the project write and the overlay clear: the project already applied it
    _session(home, evidence={}, folding=folding)
    _fold(home)
    assert _proj_ev(home).n == 1.0  # type: ignore[union-attr]
    assert "folding" not in _read(_ov(home))


def test_staged_session_evidence_is_not_counted_by_engine(tmp_path: Path) -> None:
    home = _home(tmp_path)
    folding = {"id": "a" * 32, "evidence": ONE, "outcomes": [], "t": time.time()}
    _session(home, evidence={}, folding=folding)
    assert _engine(home)._evidence("compression", FULL_KEY, "standard", "savings").n == 0.0


# --------------------------------------------------------------------------- merge semantics
def test_coarse_buckets_populated(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, evidence=ONE)
    _fold(home)
    for bucket in [FULL_KEY, *COARSE]:
        assert _close(_proj_ev(home, bucket), SAV), bucket
        assert _close(_proj_ev(home, bucket, kind="regret"), REG), bucket


def test_coarse_buckets_aggregate_children(tmp_path: Path) -> None:
    home = _home(tmp_path)
    other = FULL_KEY.replace("phase=build", "phase=debug")
    ev = {"compression": {FULL_KEY: {"standard": _cell(SAV)}, other: {"standard": _cell(SAV)}}}
    _session(home, evidence=ev)
    _fold(home)
    assert _close(_proj_ev(home, "ctype=log"), SAV + SAV)
    assert _close(_proj_ev(home, other), SAV)


def test_merge_sums_then_caps_without_rediscount(tmp_path: Path) -> None:
    home = _home(tmp_path)
    proj = Evidence(30.0, 10.0, 40.0, 20.0)
    ses = Evidence(15.0, 5.0, 20.0, 10.0)
    _write(
        _pdir(home) / "posteriors.json",
        {"version": 2, "points": {"compression": {FULL_KEY: {"standard": _cell(proj)}}}},
    )
    _session(home, evidence={"compression": {FULL_KEY: {"standard": _cell(ses)}}})
    _fold(home)
    got = _proj_ev(home)
    assert got is not None
    assert abs(got.ea + got.eb - 50.0) < 1e-9  # posterior_cap
    assert abs(got.ea / (got.ea + got.eb) - 0.75) < 1e-12  # mean preserved
    assert got.n == 60.0  # real observations are counted, not scaled
    assert abs(got.second - 30.0 * 50.0 / 60.0) < 1e-9


def test_global_ageing_halves_untouched_entries(tmp_path: Path) -> None:
    home = _home(tmp_path)
    now = time.time()
    old = {"savings": Evidence(4.0, 2.0, 8.0, 2.0).to_list(), "t": now - 91 * 86400}
    fresh = {"savings": Evidence(4.0, 2.0, 8.0, 2.0).to_list(), "t": now - 10 * 86400}
    _write(
        home / "global" / "posteriors.json",
        {
            "version": 2,
            "points": {"read_dedupe": {"*": {"pointer": old}, "size=sm": {"pointer": fresh}}},
        },
    )
    _session(home, evidence=ONE)
    rep = _fold(home, now=now)
    assert rep.aged == 1
    g = home / "global" / "posteriors.json"
    assert _close(_ev(g, "read_dedupe", "*", "pointer"), Evidence(2.0, 1.0, 4.0, 1.0))
    assert _close(_ev(g, "read_dedupe", "size=sm", "pointer"), Evidence(4.0, 2.0, 8.0, 2.0))
    t = _read(g)["points"]["read_dedupe"]["*"]["pointer"]["t"]
    assert t == pytest.approx(now)  # halved once per untouched 90 days, not every fold
    _session(home, "s-2", evidence=ONE)
    _fold(home, "s-2", now=now + 1)
    assert _close(_ev(g, "read_dedupe", "*", "pointer"), Evidence(2.0, 1.0, 4.0, 1.0))
    # StoreChain still reads global entries that carry "t"
    chain = StoreChain(KEY, home, None)
    assert chain.get("read_dedupe", "*", "pointer", "savings", "global") is not None


def test_global_weight_is_configurable(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, evidence=ONE)
    _fold(home, params={"learn": {"global_weight": 0.5}})
    assert _glob_ev(home).n == pytest.approx(0.5)  # type: ignore[union-attr]


def test_fold_without_overlay_or_with_corrupt_data_fails_open(tmp_path: Path) -> None:
    home = _home(tmp_path)
    rep = _fold(home, "missing")
    assert not rep.staged and "nothing" in rep.line()
    bad = {"compression": {FULL_KEY: {"standard": {"savings": [1, "x"]}, "light": 3}, "x": 1}}
    _session(home, evidence=bad)
    rep = _fold(home)
    assert rep.staged and rep.cells == 0
    _ov(home).write_text("{not json", encoding="utf-8")
    _fold(home)  # never raises


def test_first_fold_of_empty_session_still_marks_it(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _session(home, turn=3)
    assert _fold(home).staged
    assert not _fold(home).staged
    assert "folded_at" in _read(_ov(home))


# --------------------------------------------------------------------------- lock order
def test_lock_order_is_project_then_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _session(home, evidence=ONE, outcomes=[["d1", "compression", "standard", "pytest", 0.0]])
    stack: list[str] = []
    chains: list[list[str]] = []
    real = jsonstore.locked

    @contextmanager
    def spy(path: Path, *a: Any, **kw: Any) -> Iterator[None]:
        if path.parent.name == "sessions":
            name = "overlay"
        elif path.parent.name == "global":
            name = f"global/{path.name}"
        else:
            name = path.name
        stack.append(name)
        chains.append(list(stack))
        try:
            with real(path, *a, **kw):
                yield
        finally:
            stack.pop()

    monkeypatch.setattr(jsonstore, "locked", spy)
    _fold(home)
    order = overlay.LOCK_ORDER
    assert order[0] == "posteriors.json" and order[-2:] == ("overlay", "pending.json")
    for chain in chains:
        assert all(n in order for n in chain), chain
        idx = [order.index(n) for n in chain]
        assert idx == sorted(set(idx)), chain  # always acquired in LOCK_ORDER, never twice
    nested_overlay = [c for c in chains if c[-1] == "overlay"]
    assert nested_overlay and all(c[0] == "posteriors.json" for c in nested_overlay)


# --------------------------------------------------------------------------- pretrim
def _bash_event(tmp_path: Path, command: str) -> Event:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind="post_tool",
        session_id=SID,
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall("Bash", tool_category("Bash"), {"command": command}, "tu-1"),
        output_text="x",
        extra=MappingProxyType({}),
    )


def _decision(
    did: str,
    sig: str,
    before: int,
    after: int,
    *,
    arm: str = "standard",
    point: str = "compression",
) -> dict[str, Any]:
    return {
        "decision_id": did,
        "point": point,
        "arm": arm,
        "propensity": 0.5,
        "bucket": FULL_KEY,
        "tokens_before": before,
        "tokens_after": after,
        "recovery_id": None,
        "tool_use_id": None,
        "command_sig": sig,
        "ts": time.time(),
        "ctype": "log",
        "tool_name": "Bash",
    }


def _log(home: Path, recs: list[dict[str, Any]]) -> None:
    path = _pdir(home) / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")


def test_pretrim_candidate_written_and_consumed_by_guards(tmp_path: Path) -> None:
    home = _home(tmp_path)
    ev = _bash_event(tmp_path, f"cd {tmp_path / 'proj'} && npm   test 2>&1")
    sig = regret.event_signature(ev)
    _log(home, [_decision(f"d{i}", sig, 10_000, 3_000) for i in range(5)])
    _session(home, evidence={})
    rep = _fold(home)
    assert rep.candidates == 1
    doc = _read(_pdir(home) / "pretrim.json")
    assert doc["candidates"][sig]["lines"] == 120
    cfg = load_config(tmp_path / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    assert guards.pretrim_lines(ev, _pdir(home), cfg, env={"TOKEN_SPAREN_HOME": str(home)}) == 120


def test_pretrim_counts_decisions_across_folds_once(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _log(home, [_decision(f"a{i}", "make", 10_000, 3_000) for i in range(3)])
    _session(home, "s-a", evidence={})
    _fold(home, "s-a")
    _session(home, "s-b", evidence={})
    _fold(home, "s-b")  # the same 3 decisions are not counted again
    assert "make" not in _read(_pdir(home) / "pretrim.json").get("candidates", {})
    _log(home, [_decision(f"b{i}", "make", 10_000, 3_000) for i in range(2)])
    _session(home, "s-c", evidence={})
    _fold(home, "s-c")
    assert "make" in _read(_pdir(home) / "pretrim.json")["candidates"]


@pytest.mark.parametrize(
    ("n", "after", "arm", "regret_value"),
    [
        (4, 3_000, "standard", None),  # too few
        (5, 4_100, "standard", None),  # mean ratio > 0.4
        (5, 3_000, "off", None),  # safe-arm decisions are not compressions
        (5, 3_000, "standard", 0.5),  # a regret event
    ],
)
def test_pretrim_candidate_rules(
    tmp_path: Path, n: int, after: int, arm: str, regret_value: float | None
) -> None:
    home = _home(tmp_path)
    _log(home, [_decision(f"d{i}", "pytest -q", 10_000, after, arm=arm) for i in range(n)])
    outcomes = (
        [] if regret_value is None else [["d0", "compression", arm, "pytest -q", regret_value]]
    )
    _session(home, evidence={}, outcomes=outcomes)
    _fold(home)
    assert "pytest -q" not in _read(_pdir(home) / "pretrim.json").get("candidates", {})


def test_pretrim_regret_removes_existing_candidate(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _log(home, [_decision(f"d{i}", "pytest -q", 10_000, 1_000) for i in range(5)])
    _session(home, "s-a", evidence={})
    _fold(home, "s-a")
    assert "pytest -q" in _read(_pdir(home) / "pretrim.json")["candidates"]
    _session(home, "s-b", evidence={}, outcomes=[["dx", "pretrim", "bounded", "pytest -q", 1.0]])
    _fold(home, "s-b")
    assert "pytest -q" not in _read(_pdir(home) / "pretrim.json")["candidates"]


def test_pretrim_candidates_capped_lru(tmp_path: Path) -> None:
    home = _home(tmp_path)
    recs = [_decision(f"d{c}-{i}", f"cmd {c}", 10_000, 1_000) for c in range(205) for i in range(5)]
    for k, rec in enumerate(recs):
        rec["ts"] = 1000.0 + k
    _log(home, recs)
    _session(home, evidence={})
    _fold(home)
    cands = _read(_pdir(home) / "pretrim.json")["candidates"]
    assert len(cands) == 200
    assert "cmd 0" not in cands and "cmd 204" in cands


def test_pretrim_scan_survives_log_rotation(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _log(home, [_decision(f"a{i}", "x", 10_000, 1_000) for i in range(3)])
    _session(home, "s-a", evidence={})
    _fold(home, "s-a")
    path = _pdir(home) / "decisions.jsonl"
    path.replace(path.with_name("decisions.jsonl.1"))
    _log(home, [_decision(f"b{i}", "x", 10_000, 1_000) for i in range(2)])
    _session(home, "s-b", evidence={})
    _fold(home, "s-b")
    assert "x" in _read(_pdir(home) / "pretrim.json")["candidates"]


# --------------------------------------------------------------------------- learner state
def _decisions_for(arm: str, n: int) -> list[dict[str, Any]]:
    return [_decision(f"{arm}{i}", "", 10_000, 3_000, arm=arm) for i in range(n)]


def test_learner_state_transitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    assert overlay.learner_state(KEY, home) == "cold"
    best = {
        "standard": _cell(Evidence(30.0, 20.0, 25.0, 18.0), Evidence(0.0, 50.0, 25.0, 0.0)),
    }
    _write(
        _pdir(home) / "posteriors.json",
        {"version": 2, "points": {"compression": {FULL_KEY: best, "*": best}}},
    )
    _log(home, _decisions_for("off", 50))
    assert overlay.learner_state(KEY, home) == "adapting"  # >= 20 obs, choices not the best arm
    _log(home, _decisions_for("standard", 45))
    assert overlay.learner_state(KEY, home) == "exploiting"  # >= 80 % of the last 50
    _write(_pdir(home) / "breaker.json", {"points": {"compression": {"remaining": 2}}})
    assert overlay.learner_state(KEY, home) == "safe-mode"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    assert overlay.learner_state(KEY) == "safe-mode"  # the statusline's one-argument call


def test_learner_state_cold_below_20_obs(tmp_path: Path) -> None:
    home = _home(tmp_path)
    few = {"standard": _cell(Evidence(6.0, 4.0, 10.0, 4.0), Evidence(0.0, 10.0, 10.0, 0.0))}
    _write(
        _pdir(home) / "posteriors.json",
        {"version": 2, "points": {"compression": {FULL_KEY: few, "*": few}}},
    )
    _log(home, _decisions_for("standard", 50))
    assert overlay.learner_state(KEY, home) == "cold"


def test_statusline_imports_learner_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.adapters.claude import statusline

    monkeypatch.setattr(statusline, "_LEARNER", [])
    home = _home(tmp_path)
    _write(_pdir(home) / "breaker.json", {"points": {"compression": {"remaining": 1}}})
    assert statusline._learner_state(KEY, {"TOKEN_SPAREN_HOME": str(home)}) == "safe-mode"


# --------------------------------------------------------------------------- hooks and CLI
class _Popen:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def __init__(self, args: list[str], **kw: Any) -> None:
        _Popen.calls.append((list(args), kw))


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> type[_Popen]:
    _Popen.calls = []
    monkeypatch.setattr(overlay, "POPEN", _Popen)
    return _Popen


def _hook_event(tmp_path: Path, kind: str, sid: str = SID) -> Event:
    env = {"TOKEN_SPAREN_HOME": str(_home(tmp_path)), "CLAUDE_PROJECT_DIR": str(tmp_path / "proj")}
    (tmp_path / "proj").mkdir(exist_ok=True)
    ev = entry.ADAPTER.parse(kind, {"session_id": sid}, env)
    import dataclasses

    return dataclasses.replace(ev, project_key=KEY)


def test_session_end_spawns_detached_fold(tmp_path: Path, popen: type[_Popen]) -> None:
    env = {"TOKEN_SPAREN_HOME": str(_home(tmp_path)), "PATH": os.environ.get("PATH", "")}
    ev = _hook_event(tmp_path, "SessionEnd")
    token = entry.HOOK_ENV.set(env)
    try:
        t0 = time.monotonic()
        act = entry._on_fold_spawn(ev, Config())
        assert time.monotonic() - t0 < 0.05
    finally:
        entry.HOOK_ENV.reset(token)
    assert act.is_noop()
    ((args, kw),) = popen.calls
    assert args[1] == "-I" and args[2].endswith(os.path.join("hooks", "run_cli.py"))
    project = str(tmp_path / "proj")
    # the SessionEnd fold is the only one that may write tool-pruning deny rules
    assert args[3:] == ["fold", "--session", SID, "--project", project, "--prune-apply"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL
    assert kw["stderr"] is subprocess.DEVNULL
    assert kw["env"]["TOKEN_SPAREN_HOME"] == str(_home(tmp_path))
    if sys.platform == "win32":  # pragma: no cover
        assert kw["creationflags"]
    else:
        assert kw["start_new_session"] is True
    assert entry._on_fold_spawn in [
        getattr(f, "__wrapped__", f) for f in entry.HANDLERS["session_end"]
    ]


def test_spawn_flags_per_platform() -> None:
    assert overlay.spawn_options("linux") == {"start_new_session": True}
    win = overlay.spawn_options("win32")
    assert win["creationflags"] == 0x00000008 | 0x00000200


def test_spawn_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise OSError("no exec")

    monkeypatch.setattr(overlay, "POPEN", boom)
    env = {"TOKEN_SPAREN_HOME": str(_home(tmp_path))}
    assert overlay.spawn_fold(["--session", "x"], env) is False
    assert "no exec" in (_home(tmp_path) / "logs" / "errors.jsonl").read_text(encoding="utf-8")


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _index(home: Path, *sids: str) -> None:
    path = _pdir(home) / "pending.json"
    doc = _read(path)
    doc.update({sid: 1.0 for sid in sids})
    _write(path, doc)


def _start(tmp_path: Path) -> list[overlay.FoldReport]:
    home = _home(tmp_path)
    return overlay.fold_pending(
        KEY, home, tmp_path / "proj", exclude=SID, env={"TOKEN_SPAREN_HOME": str(home)}
    )


def test_pending_index_finds_crashed_session_among_many_folded(
    tmp_path: Path, popen: type[_Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    for i in range(60):
        _age(_session(home, f"f-{i:02d}", evidence={}, folded_at=1.0), 9000 + i)
    _session(home, "crashed", evidence=ONE)

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("crash after staging")

    with monkeypatch.context() as m:
        m.setattr(overlay, "_global", boom)
        assert _fold(home, "crashed").staged
    assert "folding" in _read(_ov(home, "crashed"))
    assert "crashed" in _read(_pdir(home) / "pending.json")
    _age(_ov(home, "crashed"), 7200)
    reps = _start(tmp_path)
    assert [r.session_id for r in reps] == ["crashed"]
    assert "folding" not in _read(_ov(home, "crashed"))
    assert _read(_pdir(home) / "pending.json") == {}
    assert _proj_ev(home).n == 1.0  # type: ignore[union-attr]


def test_session_start_reads_only_the_index(
    tmp_path: Path, popen: type[_Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _age(_session(home, "unindexed", evidence=ONE), 7200)
    _age(_session(home, "indexed", evidence=ONE), 7200)
    _index(home, "indexed", "gone")  # "gone" has no overlay any more: dropped by its fold

    def no_scan(*a: Any, **k: Any) -> None:
        raise AssertionError("SessionStart must not scan the sessions directory")

    monkeypatch.setattr(overlay.os, "scandir", no_scan)
    reps = _start(tmp_path)
    assert sorted(r.session_id for r in reps) == ["gone", "indexed"]
    assert "folded_at" not in _read(_ov(home, "unindexed"))
    assert _read(_pdir(home) / "pending.json") == {}


def test_engine_reward_and_regret_outcomes_index_the_session(tmp_path: Path) -> None:
    home = _home(tmp_path)
    ctx = SessionCtx(SID, KEY, 1, None, None, _ov(home))
    eng = _engine(home)
    d = eng.decide(COMPRESSION, FULL, ctx)
    while d.arm == "off":
        d = eng.decide(COMPRESSION, FULL, ctx)
    assert not (_pdir(home) / "pending.json").exists()  # a decision alone is not evidence
    eng.reward(d.decision_id, 0.5, None)
    assert SID in _read(_pdir(home) / "pending.json")
    _fold(home)
    assert _read(_pdir(home) / "pending.json") == {}
    # settled regret outcomes (any policy) index the session too
    env = {"TOKEN_SPAREN_HOME": str(home)}
    det = regret.RegretDetector(load_config(tmp_path / "proj", env), env)
    det.track(
        ctx,
        "d9",
        point="compression",
        bucket="static",
        arm="standard",
        recovery_id=None,
        command_sig="make",
        full="",
        kept="",
        ctype="log",
    )
    det._apply(_ov(home), regret._Info(), 1, True, True)
    assert SID in _read(_pdir(home) / "pending.json")


def test_breaker_notice_runs_after_pending_fold() -> None:
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["session_start"]]
    assert names.index("_on_fold_pending") < names.index("_on_breaker_notice")


def test_session_start_folds_old_unfolded_sessions(
    tmp_path: Path, popen: type[_Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(overlay, "_monotonic", lambda: 0.0)  # a slow runner never hits the budget
    home = _home(tmp_path)
    for i in range(5):
        _age(_session(home, f"old-{i}", evidence=ONE), 7200 + i)
    _session(home, "recent", evidence=ONE)
    _age(_session(home, SID, evidence=ONE), 7200)  # the starting session itself
    _age(_session(home, "done", evidence={}, folded_at=1.0), 7200)
    _index(home, *[f"old-{i}" for i in range(5)], "recent", SID)
    env = {"TOKEN_SPAREN_HOME": str(home)}
    token = entry.HOOK_ENV.set(env)
    try:
        entry._on_fold_pending(_hook_event(tmp_path, "SessionStart"), Config())
    finally:
        entry.HOOK_ENV.reset(token)
    folded = [s for s in [f"old-{i}" for i in range(5)] if "folded_at" in _read(_ov(home, s))]
    assert len(folded) == 3  # bounded; the rest wait for the next start
    assert "folded_at" not in _read(_ov(home, "recent"))
    assert "folded_at" not in _read(_ov(home, SID))
    assert _proj_ev(home).n == 3.0  # type: ignore[union-attr]
    assert popen.calls == []


def test_session_start_spawns_when_in_process_fold_is_too_big(
    tmp_path: Path, popen: type[_Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _age(_session(home, "old-0", evidence=ONE), 7200)
    _index(home, "old-0")
    monkeypatch.setattr(overlay, "INPROCESS_MAX_BYTES", 1)
    token = entry.HOOK_ENV.set({"TOKEN_SPAREN_HOME": str(home)})
    try:
        entry._on_fold_pending(_hook_event(tmp_path, "SessionStart"), Config())
    finally:
        entry.HOOK_ENV.reset(token)
    ((args, _),) = popen.calls
    assert args[3:] == [
        "fold",
        "--all",
        "--older-than",
        "3600",
        "--max",
        "3",
        "--project",
        str(tmp_path / "proj"),
    ]
    assert "folded_at" not in _read(_ov(home, "old-0"))


def test_pending_sessions_detection(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _age(_session(home, "a", evidence=ONE), 7200)
    _age(_session(home, "b", evidence={}, folded_at=1.0), 7200)
    _age(
        _session(
            home,
            "c",
            evidence={},
            folded_at=1.0,
            regret_outcomes=[["d", "compression", "light", "", 0.0]],
        ),
        7200,
    )
    _age(_session(home, "d", evidence={}, folded_at=1.0, folding={"id": "x" * 32}), 7200)
    got = overlay.pending_sessions(KEY, home, older_than=3600, limit=10)
    assert sorted(got) == ["a", "c", "d"]
    assert overlay.pending_sessions(KEY, home, older_than=3600, limit=10, exclude="a") != got


def test_cli_fold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    from token_sparen.store import keycache

    key = keycache.cached_project_key(proj.resolve(), {"TOKEN_SPAREN_HOME": str(home)})
    _write(session.session_path(home, key, "c-1"), {"evidence": ONE})
    _write(session.session_path(home, key, "c-2"), {"evidence": ONE})
    assert cli.main(["fold", "--session", "c-1", "--project", str(proj)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "c-1" in out[0] and "2 cells" in out[0]
    assert cli.main(["fold", "--all", "--project", str(proj)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "1 session" in out[0]
    assert cli.main(["fold", "--project", str(proj)]) == 2  # neither --session nor --all
    assert "fold" in capsys.readouterr().err


# --------------------------------------------------------------------------- review round 1
def test_invalid_cells_are_skipped_and_logged(tmp_path: Path) -> None:
    home = _home(tmp_path)
    cell = _cell(SAV)
    bad = {
        "compression": {
            "foo=bar": {"standard": cell},  # not a compression feature
            "ctype=/etc/passwd": {"standard": cell},  # value outside the safe charset
            "ctype=log|ctype=json": {"standard": cell},  # repeated key
            "ctype": {"standard": cell},  # not k=v
            FULL_KEY: {"evil": cell, "standard": cell},  # unknown arm / the one valid cell
        }
    }
    _session(home, evidence=bad)
    rep = _fold(home)
    assert rep.cells == 1 and rep.skipped == 5
    for path in (_pdir(home) / "posteriors.json", home / "global" / "posteriors.json"):
        text = path.read_text(encoding="utf-8")
        assert "foo" not in text and "passwd" not in text and "evil" not in text
        assert "json" not in text
    log = (home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "skipped 5 invalid" in log


def test_global_applied_ids_are_per_salted_project_tag(tmp_path: Path) -> None:
    home = _home(tmp_path)
    tag = overlay.project_tag(home, KEY)
    assert len(tag) == 16 and tag != KEY and tag == overlay.project_tag(home, KEY)
    secret = home / "secret"
    assert secret.exists()
    if os.name == "posix":
        assert secret.stat().st_mode & 0o777 == 0o600
    other = "fedcba9876543210"
    assert overlay.project_tag(home, other) != tag
    _write(
        home / "global" / "posteriors.json",
        {"version": 2, "points": {}, "applied": {tag: [f"x{i}" for i in range(200)]}},
    )
    _session(home, evidence=ONE)
    _fold(home)
    applied = _read(home / "global" / "posteriors.json")["applied"]
    assert set(applied) == {tag} and len(applied[tag]) == 200 and applied[tag][0] == "x1"
    text = (home / "global" / "posteriors.json").read_text(encoding="utf-8")
    assert KEY not in text


def test_global_legacy_applied_list_is_replaced(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write(home / "global" / "posteriors.json", {"version": 2, "points": {}, "applied": ["a"]})
    _session(home, evidence=ONE)
    _fold(home)
    applied = _read(home / "global" / "posteriors.json")["applied"]
    assert isinstance(applied, dict) and list(applied) == [overlay.project_tag(home, KEY)]


def test_pretrim_lookup_truncates_like_the_decision_log(tmp_path: Path) -> None:
    from token_sparen.mechanisms import compression

    home = _home(tmp_path)
    ev = _bash_event(
        tmp_path, "pytest " + " ".join(f"tests/test_mod_{i:03d}.py" for i in range(60))
    )
    sig = regret.event_signature(ev)[: compression.COMMAND_SIG_CHARS]
    assert len(sig) == 500
    _log(home, [_decision(f"d{i}", sig, 10_000, 1_000) for i in range(5)])
    _session(home, evidence={})
    _fold(home)
    cfg = load_config(tmp_path / "proj", {"TOKEN_SPAREN_HOME": str(home)})
    env = {"TOKEN_SPAREN_HOME": str(home)}
    assert guards._normalise(ev.tool.input["command"], ev) == sig  # type: ignore[union-attr]
    assert guards.pretrim_lines(ev, _pdir(home), cfg, env=env) == 120


# --------------------------------------------------------------------------- review round 2
def test_hook_evidence_racing_the_fold_keeps_its_index_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from token_sparen.learn import pending

    home = _home(tmp_path)
    env = {"TOKEN_SPAREN_HOME": str(home)}
    _session(home, evidence=ONE)
    real_remove = pending.remove
    hooks: list[threading.Thread] = []

    def hook() -> None:  # what Engine.reward does: overlay write, then index add
        def fn(state: dict[str, Any]) -> dict[str, Any]:
            state["evidence"] = ONE
            return state

        jsonstore.update(_ov(home), fn, {}, env)
        pending.add_overlay(_ov(home), env)

    def racing_remove(*a: Any, **k: Any) -> None:
        if not hooks:
            t = threading.Thread(target=hook)
            hooks.append(t)
            t.start()
            t.join(0.3)  # done unless the fold still holds the overlay lock
        real_remove(*a, **k)

    monkeypatch.setattr(pending, "remove", racing_remove)
    _fold(home)
    hooks[0].join(5)
    assert _read(_ov(home))["evidence"] == ONE  # new evidence after the clear
    assert SID in _read(_pdir(home) / "pending.json")  # still indexed: it will be folded


def test_secret_empty_or_short_file_is_regenerated(tmp_path: Path) -> None:
    home = _home(tmp_path)
    home.mkdir(parents=True)
    (home / "secret").write_text("", encoding="utf-8")  # a crash left an empty file
    tag = overlay.project_tag(home, KEY)
    secret = (home / "secret").read_text(encoding="utf-8")
    assert len(secret) == 32 and int(secret, 16) >= 0
    assert overlay.project_tag(home, KEY) == tag
    (home / "secret").write_text("abc", encoding="utf-8")
    assert len(overlay.project_tag(home, KEY)) == 16
    assert len((home / "secret").read_text(encoding="utf-8")) == 32
    assert [p.name for p in home.iterdir()] == ["secret"]  # no temp files left behind


def test_secret_is_installed_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    seen: list[str] = []
    real_link = os.link

    def link(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        seen.append(Path(src).read_text(encoding="utf-8"))  # complete before it appears
        real_link(src, dst, *a, **k)

    monkeypatch.setattr(overlay.os, "link", link)
    overlay.project_tag(home, KEY)
    assert len(seen) == 1 and len(seen[0]) == 32
    assert (home / "secret").read_text(encoding="utf-8") == seen[0]


def test_session_start_fold_stops_at_its_time_budget(
    tmp_path: Path, popen: type[_Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each in-process fold is charged against the 100 ms budget; the rest waits."""
    clock = iter([0.0, 0.0, overlay.INPROCESS_BUDGET_S / 2, overlay.INPROCESS_BUDGET_S * 2])
    monkeypatch.setattr(overlay, "_monotonic", lambda: next(clock))
    home = _home(tmp_path)
    for i in range(3):
        _age(_session(home, f"old-{i}", evidence=ONE), 7200 + i)
    _index(home, *[f"old-{i}" for i in range(3)])
    token = entry.HOOK_ENV.set({"TOKEN_SPAREN_HOME": str(home)})
    try:
        entry._on_fold_pending(_hook_event(tmp_path, "SessionStart"), Config())
    finally:
        entry.HOOK_ENV.reset(token)
    folded = [s for s in [f"old-{i}" for i in range(3)] if "folded_at" in _read(_ov(home, s))]
    assert len(folded) == 2 and popen.calls == []


def test_fold_merges_natural_break_rates(tmp_path: Path) -> None:
    """Review M1 / N-1: the loop guard's natural break observations (no nudge), per bucket and
    pooled, reach the project store and, weighted, the global store; a re-fold adds nothing; a
    legacy flat project entry becomes the pooled one."""
    home = _home(tmp_path)
    nat = Evidence(2.0, 1.0, 3.0, 2.0)
    shell = Evidence(1.0, 0.0, 1.0, 1.0)
    _write(
        _pdir(home) / "posteriors.json",
        {"version": 2, "points": {}, "natural": {"loopguard": Evidence(1, 1, 2, 1).to_list()}},
    )
    bad = {"bad key": [1, 0, 1, 1], "cat=a/b": [1, 0, 1, 1]}
    table = {"*": nat.to_list(), "cat=shell": shell.to_list(), **bad}
    _session(home, natural={"loopguard": table, "bogus": {"*": [1, 2, 3]}})
    assert _fold(home).staged
    _fold(home)
    proj = _read(_pdir(home) / "posteriors.json")["natural"]["loopguard"]
    assert _close(Evidence.from_list(proj["*"]), Evidence(3.0, 2.0, 5.0, 3.0))
    assert _close(Evidence.from_list(proj["cat=shell"]), shell)
    assert set(proj) == {"*", "cat=shell"}
    glob = _read(home / "global" / "posteriors.json")
    assert _close(Evidence.from_list(glob["natural"]["loopguard"]["*"]), nat.scaled(0.3))
    assert set(glob["natural"]) == {"loopguard"}  # only known points
    assert "natural" not in _read(_ov(home)) or not _read(_ov(home))["natural"]


def test_global_natural_rate_ages(tmp_path: Path) -> None:
    """m-4: an untouched global natural-rate entry is halved after global_age_days."""
    home = _home(tmp_path)
    old = Evidence(8.0, 2.0, 10.0, 8.0)
    _write(
        home / "global" / "posteriors.json",
        {
            "version": 2,
            "points": {},
            "natural": {"loopguard": {"*": old.to_list(), "cat=shell": old.to_list()}},
            "natural_t": {"loopguard|*": 0.0, "loopguard|cat=shell": 0.0},
        },
    )
    _session(home, evidence=ONE)
    _fold(home, now=100 * 86400.0)
    glob = _read(home / "global" / "posteriors.json")
    for bucket in ("*", "cat=shell"):
        got = Evidence.from_list(glob["natural"]["loopguard"][bucket])
        assert _close(got, old.scaled(0.5)), bucket
        assert glob["natural_t"][f"loopguard|{bucket}"] == 100 * 86400.0


# --------------------------------------------------------------------------- M4 Task 6
def test_fold_engine_only_skips_facts_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(overlay, "_mine_facts", lambda *a, **k: calls.append("facts") or 0)
    monkeypatch.setattr(overlay, "_prune", lambda *a, **k: calls.append("prune") or [])
    home = _home(tmp_path)
    _session(home, evidence=ONE)
    rep = _fold(home, mine_facts=False, prune=False)
    assert rep.staged and rep.cells == 2 and calls == []
    assert _close(_proj_ev(home), SAV)  # engine evidence still folded
    _session(home, "s-2", evidence=ONE)
    assert _fold(home, "s-2").staged
    assert calls == ["facts", "prune"]  # defaults unchanged
