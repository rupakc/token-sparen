"""Settled regret reaches the decision log (M2 close C1).

Every settled regret value (a signal, window expiry 0, session end 0, eviction, a trimming
no-op) appends ``{"kind": "outcome", "decision_id", "regret", "signal", "ts"}`` to
``projects/<key>/decisions.jsonl``; ``ope`` joins outcomes to decisions by id. Driven through
``entry.main`` (live hooks), then folded like the SessionEnd spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from integration.test_learning_loop import Driver, project_key

from token_sparen.learn import ope, overlay, regret
from token_sparen.learn.policy import StaticPolicy


def _driver(tmp_path: Path, sid: str = "s-out", **env: str) -> Driver:
    env.setdefault("TOKEN_SPAREN_LEARN__ENABLED", "false")  # deterministic: static standard
    return Driver(tmp_path / "home", tmp_path / "proj", sid, **env)


def _rows(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(home.glob("projects/*/decisions.jsonl")):
        out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return out


def _outcomes(home: Path) -> dict[str, dict[str, Any]]:
    return {r["decision_id"]: r for r in _rows(home) if r.get("kind") == "outcome"}


def _log_paths(home: Path) -> list[Path]:
    return sorted(home.glob("projects/*/decisions.jsonl"))


def test_recovery_read_logs_full_regret(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    arm, rid = d.bash()
    assert arm == "standard" and rid is not None
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    d.recover(rid)
    out = _outcomes(d.home)[did]
    assert out["regret"] == 1.0 and out["signal"] == "recovery_read"
    assert set(out) == {"kind", "decision_id", "regret", "signal", "ts"}
    assert isinstance(out["ts"], float)


def test_rerun_logs_partial_or_full_regret(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    d.n -= 1  # the same command again, next turn
    d.prompt()
    d.hook(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": f"pytest -q tests/test_mod_{d.n + 1:04d}.py"},
        tool_use_id="again",
        tool_response={"stdout": "ok", "stderr": "", "interrupted": False, "isImage": False},
    )
    d.hook("SessionEnd", reason="exit")
    out = _outcomes(d.home)[did]
    assert out["signal"] == "rerun" and out["regret"] > 0.0


def test_window_expiry_logs_zero_regret(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    for _ in range(regret.WINDOW_TURNS + 2):
        d.prompt()
    out = _outcomes(d.home)[did]
    assert out["regret"] == 0.0 and out["signal"] == "expiry"


def test_session_end_logs_zero_regret(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    assert did not in _outcomes(d.home)  # not settled yet
    d.hook("SessionEnd", reason="exit")
    out = _outcomes(d.home)[did]
    assert out["regret"] == 0.0 and out["signal"] == "session_end"


def test_eviction_logs_the_best_regret_so_far(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(regret, "MAX_TRACKED", 1)
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    first = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    d.prompt()
    d.bash()
    out = _outcomes(d.home)[first]
    assert out["regret"] == 0.0 and out["signal"] == "evicted"


def test_trimming_no_op_logs_zero_regret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.mechanisms import compression

    monkeypatch.setattr(compression, "pick_candidate", lambda *a, **k: None)
    d = _driver(tmp_path)
    d.prompt()
    arm, rid = d.bash()
    assert arm == "standard" and rid is None
    (dec,) = [r for r in _rows(d.home) if r.get("kind") != "outcome"]
    out = _outcomes(d.home)[dec["decision_id"]]
    assert out["regret"] == 0.0 and out["signal"] == "no_op"


def test_safe_arm_logs_no_outcome(tmp_path: Path) -> None:
    d = _driver(tmp_path, TOKEN_SPAREN_LEARN__HOLD__COMPRESSION="true")
    d.prompt()
    d.bash()
    d.hook("SessionEnd", reason="exit")
    assert _outcomes(d.home) == {}


def test_learning_engine_outcomes_are_logged_too(tmp_path: Path) -> None:
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-eng")
    for _ in range(8):
        d.prompt()
        _, rid = d.bash()
        if rid is not None:
            d.recover(rid)
    d.hook("SessionEnd", reason="exit")
    decs = [r for r in _rows(d.home) if r.get("kind") != "outcome"]
    trimmed = {r["decision_id"] for r in decs if r["arm"] != "off"}
    assert trimmed and trimmed <= set(_outcomes(d.home))


def test_ope_joins_outcomes_and_survives_fold(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    rids = []
    for i in range(4):
        d.prompt()
        _, rid = d.bash()
        rids.append(rid)
        if i % 2 == 0 and rid is not None:
            d.recover(rid)
    d.hook("SessionEnd", reason="exit")
    log, regrets = ope.load_log(_log_paths(d.home))
    assert len(log) == 4  # outcome lines are not decisions
    assert sorted(regrets.values()) == [0.0, 0.0, 1.0, 1.0]
    rep = overlay.fold(d.sid, project_key(d.home, d.proj), d.home)
    assert rep.outcomes == 4  # the fold still consumes the overlay's outcomes
    log2, regrets2 = ope.load_log(_log_paths(d.home))
    assert regrets2 == regrets and len(log2) == 4  # the fold neither drops nor duplicates
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=50)
    assert res["compression"]["regret_known_fraction"] == 1.0


def test_load_log_joins_outcomes_by_id_and_keeps_the_max(tmp_path: Path) -> None:
    dec = {
        "decision_id": "a",
        "point": "compression",
        "arm": "standard",
        "propensity": 1.0,
        "bucket": "static",
        "tokens_before": 100,
        "tokens_after": 40,
        "recovery_id": "r",
        "tool_use_id": "t",
        "command_sig": "c",
        "ts": 1.0,
    }
    lines = [
        {"kind": "outcome", "decision_id": "a", "regret": 0.5, "signal": "rerun", "ts": 2.0},
        dec,
        {**dec, "decision_id": "b"},
        {"kind": "outcome", "decision_id": "a", "regret": 0.0, "signal": "expiry", "ts": 3.0},
        {"kind": "outcome", "decision_id": "zz", "regret": 1.0, "signal": "x", "ts": 3.0},
        {"kind": "outcome", "decision_id": "b", "regret": "bad", "signal": "x", "ts": 3.0},
        {"kind": "outcome", "decision_id": "b", "regret": float("nan"), "signal": "x"},
        {"kind": "outcome", "regret": 1.0},
    ]
    path = tmp_path / "decisions.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    log, regrets = ope.load_log([path])
    assert [r.decision_id for r in log] == ["a", "b"]
    assert regrets == {"a": 0.5}  # max per decision, joined decisions only
    res = ope.evaluate(log, StaticPolicy({}), regrets=regrets, n_boot=20)
    assert res["compression"]["regret_known_fraction"] == 0.5


def test_outcome_rows_do_not_disturb_stats_or_pretrim(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    assert rid is not None
    d.recover(rid)
    d.end()  # fold: the pre-trim scan reads the log including the outcome line
    pre = json.loads(next(d.home.glob("projects/*/pretrim.json")).read_text(encoding="utf-8"))
    assert all(isinstance(v, list) for v in pre["stats"].values())


def test_rows_from_before_best_sig_still_decode_and_settle(tmp_path: Path) -> None:
    from token_sparen.config import Config
    from token_sparen.store import jsonstore, session

    entry = regret._entry("compression", "b", "standard", "r1", "pytest -q", "", "", None, None)
    entry["best"] = 0.5
    row = regret._encode(entry)
    assert len(row) == len(regret._FIELDS)
    old = row[:-1]  # the 14-field format
    assert regret._decode(old) is not None and "best_sig" not in regret._decode(old)  # type: ignore[operator]
    home = tmp_path / "home"
    path = session.session_path(home, "0123456789abcdef", "s1")
    jsonstore.update(regret.table_path(path), lambda d: {regret.TABLE_KEY: {"d1": old}}, {})
    det = regret.RegretDetector(Config(), {"TOKEN_SPAREN_HOME": str(home)})
    det._apply(path, regret._Info(), 1, True, True)
    log = path.parent.parent / "decisions.jsonl"
    (out,) = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert out["decision_id"] == "d1" and out["regret"] == 0.5 and out["signal"] == "unknown"


def test_cli_show_marks_a_recovery_read(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    assert rid is not None
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    key = project_key(d.home, d.proj)
    from token_sparen.store import session

    ov = session.session_path(d.home, key, d.sid)
    regret.record_recovery_read(ov, rid, 10, d.env)
    d.prompt()  # the next turn boundary settles the full regret
    out = _outcomes(d.home)[did]
    assert out["regret"] == 1.0 and out["signal"] == "recovery_read"


def test_fold_backfills_outcomes_the_live_write_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    from token_sparen.learn import decisions
    from token_sparen.store import jsonl, jsonstore

    def busy(*args: Any, **kwargs: Any) -> None:
        raise jsonstore.StoreBusy("decisions.jsonl busy")

    # only the outcome writer fails (decision lines go through jsonl.append directly)
    lossy = types.SimpleNamespace(append=jsonl.append, append_many=busy)

    d = _driver(tmp_path)
    d.prompt()
    _, rid = d.bash()
    assert rid is not None
    did = next(r["decision_id"] for r in _rows(d.home) if r.get("recovery_id") == rid)
    monkeypatch.setattr(decisions, "jsonl", lossy)
    d.recover(rid)  # settles regret 1; the live outcome write fails
    d.prompt()
    _, rid2 = d.bash()
    d.hook("SessionEnd", reason="exit")  # settles the second at 0; also lost
    monkeypatch.setattr(decisions, "jsonl", jsonl)
    assert _outcomes(d.home) == {}
    errors = (d.home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "StoreBusy" in errors
    key = project_key(d.home, d.proj)
    overlay.fold(d.sid, key, d.home)
    outs = _outcomes(d.home)
    assert outs[did]["regret"] == 1.0 and outs[did]["signal"] == "recovery_read"
    assert len(outs) == 2 and {o["signal"] for o in outs.values()} == {
        "recovery_read",
        "session_end",
    }
    n_lines = len(_rows(d.home))
    overlay.fold(d.sid, key, d.home)  # nothing staged again: nothing appended
    assert len(_rows(d.home)) == n_lines


def test_fold_does_not_duplicate_outcomes_already_logged(tmp_path: Path) -> None:
    d = _driver(tmp_path)
    for _ in range(3):
        d.prompt()
        d.bash()
    d.hook("SessionEnd", reason="exit")
    before = [r for r in _rows(d.home) if r.get("kind") == "outcome"]
    assert len(before) == 3
    overlay.fold(d.sid, project_key(d.home, d.proj), d.home)
    after = [r for r in _rows(d.home) if r.get("kind") == "outcome"]
    assert after == before


def test_fold_backfill_sees_the_rotated_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outcome already in decisions.jsonl.1 is not appended again."""
    d = _driver(tmp_path)
    d.prompt()
    d.bash()
    d.hook("SessionEnd", reason="exit")
    key = project_key(d.home, d.proj)
    log = _log_paths(d.home)[0]
    log.rename(log.with_name("decisions.jsonl.1"))
    overlay.fold(d.sid, key, d.home)
    assert not log.exists() or not [
        x for x in log.read_text(encoding="utf-8").splitlines() if '"outcome"' in x
    ]
