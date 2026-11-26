"""Regret detection (spec section 7.5) and savings attribution."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sampleoutputs import big_pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Event, EventKind, SessionCtx, ToolCall
from token_sparen.learn import regret
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import Decision, DecisionPoint, StaticPolicy
from token_sparen.learn.regret import RegretDetector, normalize_command
from token_sparen.mechanisms import compression, read_dedupe
from token_sparen.store import session

KEY = "0123456789abcdef"
SID = "s-1"


# --------------------------------------------------------------------------- helpers
def _env(tmp_path: Path) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}


def _cfg(tmp_path: Path, **over: str) -> Config:
    env = _env(tmp_path)
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return load_config(tmp_path / "proj", env)


def _proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


def _overlay(tmp_path: Path) -> Path:
    return session.session_path(tmp_path / "home", KEY, SID)


def _set_turn(tmp_path: Path, turn: int) -> None:
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        state["turn"] = turn
        return state

    session.update_session(_overlay(tmp_path), apply)


def _ctx(tmp_path: Path, turn: int) -> SessionCtx:
    return SessionCtx(SID, KEY, turn, None, None, _overlay(tmp_path))


def _event(
    tmp_path: Path,
    *,
    kind: EventKind = "post_tool",
    tool: str | None = "Bash",
    tool_input: Mapping[str, Any] | None = None,
    text: str | None = "ok",
    prompt: str | None = None,
    cwd: str | None = None,
) -> Event:
    from types import MappingProxyType

    from token_sparen.adapters.claude.translate import tool_category

    call = None
    if tool is not None:
        call = ToolCall(
            name=tool,
            category=tool_category(tool),
            input=dict(tool_input if tool_input is not None else {"command": "pytest -v"}),
            use_id="tu-1",
        )
    return Event(
        host="claude",
        kind=kind,
        session_id=SID,
        project_dir=_proj(tmp_path),
        project_key=KEY,
        transcript_path=None,
        tool=call,
        output_text=text,
        prompt=prompt,
        extra=MappingProxyType({} if cwd is None else {"cwd": cwd}),
    )


FULL = "\n".join(
    [
        "collected 3 items",
        "src/app/handlers.py:12 warning: deprecated_parameter_alpha used",
        "frobnicate_widget_matrix failed in quuxly_reticulated_spline",
        "detail: zanzibar_overflow_handler at offset 0x7ffd1234",
        "FAILED tests/test_core.py::test_ok - AssertionError",
    ]
)
KEPT = "collected 3 items\nFAILED tests/test_core.py::test_ok - AssertionError\n[marker]"


class Recorder:
    """A duck-typed engine: records every reward call."""

    def __init__(self, arm: str = "standard") -> None:
        self.arm = arm
        self.calls: list[tuple[str, float | None, float | None]] = []

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], ctx: SessionCtx
    ) -> Decision:
        return Decision(f"d-{len(self.calls)}-{point.name}", point.name, self.arm, 0.5, "b1")

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        self.calls.append((decision_id, savings, regret))

    def regrets(self) -> list[tuple[str, float]]:
        return [(d, r) for d, s, r in self.calls if r is not None]

    def savings(self) -> list[tuple[str, float]]:
        return [(d, s) for d, s, r in self.calls if s is not None]


def _track(
    tmp_path: Path,
    did: str = "d1",
    *,
    turn: int = 1,
    command: str = "pytest -q tests 2>&1",
    recovery_id: str | None = "abcdef012345",
    ctype: str = "test_report",
    point: str = "compression",
    full: str = FULL,
    kept: str = KEPT,
    files: list[str] | None = None,
    cwd: str | None = None,
) -> RegretDetector:
    _set_turn(tmp_path, turn)
    det = RegretDetector(_cfg(tmp_path), _env(tmp_path))
    det.track(
        _ctx(tmp_path, turn),
        did,
        point=point,
        bucket="b1",
        arm="standard",
        recovery_id=recovery_id,
        command_sig=normalize_command(command, project_dir=_proj(tmp_path), cwd=cwd),
        full=full,
        kept=kept,
        ctype=ctype,
        files=files,
    )
    return det


def _handle(tmp_path: Path, ev: Event, policy: Any) -> None:
    regret.handle(ev, _cfg(tmp_path), policy, env=_env(tmp_path))


def _state(tmp_path: Path) -> dict[str, Any]:
    return session.read_session(_overlay(tmp_path))


def _bash(tmp_path: Path, command: str, text: str = "ok", cwd: str | None = None) -> Event:
    return _event(tmp_path, tool_input={"command": command}, text=text, cwd=cwd)


# --------------------------------------------------------------------------- normalize_command
def test_normalize_strips_cd_and_env() -> None:
    app = "/srv/app"
    assert normalize_command("cd /srv/app && pytest -x tests", project_dir=app) == "pytest -x tests"
    assert normalize_command("pytest -x tests", project_dir=app, cwd=app) == "pytest -x tests"
    got = normalize_command("cd a && cd 'b c' && FOO=1 BAR='x y' make  build")
    assert got == "cd a/b c && make build"  # chained cds collapse to one resolved target
    assert normalize_command("env A=b B=c npm   test") == "npm test"
    assert normalize_command("A=1 env B=2 cargo test -p core") == "cargo test -p core"
    assert normalize_command("cd x; go test ./...") == "cd x && go test ./..."


def test_normalize_keeps_args() -> None:
    # the reference flaw: everything collapsed to "cd"
    a = normalize_command("cd /p && pytest tests/test_a.py -k fast", project_dir="/p")
    b = normalize_command("cd /p && pytest tests/test_b.py -k fast", project_dir="/p")
    assert a == "pytest tests/test_a.py -k fast"
    assert a != b
    assert normalize_command("sudo -n make install") == "sudo -n make install"
    assert normalize_command("cd /p") == "cd /p"


def test_normalize_drops_tail_and_redirect_and_caps_length() -> None:
    assert normalize_command("pytest -q 2>&1 | tail -n 50") == "pytest -q"
    assert normalize_command("pytest -q 2>&1 | tail -30") == "pytest -q"
    assert normalize_command("make 2>&1") == "make"
    assert normalize_command("  ls\t-la \n") == "ls -la"
    assert len(normalize_command("echo " + "x" * 2000)) == 500
    assert normalize_command("") == ""


def test_signature_by_tool(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x", encoding="utf-8")
    got = regret.signature("Bash", {"command": "cd /w/x && ls -la"}, project_dir="/w/x", cwd="/w/x")
    assert got == "ls -la"
    assert regret.signature("Read", {"file_path": str(f), "offset": 5}).startswith("Read:")
    assert regret.signature("Read", {"file_path": str(f)}) == regret.signature(
        "Read", {"file_path": str(f), "limit": 10}
    )
    assert regret.signature("Grep", {"pattern": "x"}) != regret.signature("Grep", {"pattern": "y"})


def test_distinctive_tokens_and_files() -> None:
    toks = regret.distinctive_tokens(FULL, KEPT, exclude="pytest -q tests")
    assert "frobnicate_widget_matrix" in toks
    assert "zanzibar_overflow_handler" in toks
    assert "collected" not in toks  # in kept text
    assert all(len(t) >= 8 for t in toks)
    assert "AssertionError" not in toks  # kept
    files = regret.span_files(FULL, KEPT)
    assert files[0] == "tests/test_core.py"  # kept text first: that is what the agent fixes
    assert "src/app/handlers.py" in files  # then the elided span
    many = "\n".join(f"tok_{i:06d}_abcdefgh /p/f{i}.py" for i in range(500))
    assert len(regret.distinctive_tokens(many, "")) == 64
    assert len(regret.span_files(many, "")) == 32


# --------------------------------------------------------------------------- signals
def test_recovery_read_is_full_regret(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    path = _proj(tmp_path) / ".token-sparen" / "outputs" / "abcdef012345.txt"
    path.parent.mkdir(parents=True)
    path.write_text(FULL, encoding="utf-8")
    _handle(tmp_path, _event(tmp_path, tool="Read", tool_input={"file_path": str(path)}), rec)
    assert rec.regrets() == [("d1", 1.0)]
    st = _state(tmp_path)
    assert st["regrets"] == 1
    assert st["recovery_read_tokens"] > 0
    assert "d1" not in st.get("regret", {})


def test_bash_cat_of_recovery_file_is_full_regret(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    cmd = "cat /x/proj/.token-sparen/outputs/abcdef012345.txt | grep -n FAILED"
    _handle(tmp_path, _bash(tmp_path, cmd, text="x" * 400), rec)
    assert rec.regrets() == [("d1", 1.0)]
    assert _state(tmp_path)["recovery_read_tokens"] == 100


def test_bash_show_of_recovery_id_is_full_regret(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "token-sparen show abcdef012345"), rec)
    assert rec.regrets() == [("d1", 1.0)]
    assert "recovery_read_tokens" not in _state(tmp_path)  # the CLI counts those


def test_unrelated_cat_is_not_regret(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "cat README.md"), rec)
    _handle(tmp_path, _bash(tmp_path, "echo .token-sparen/outputs/abcdef012345.txt"), rec)
    assert rec.regrets() == []


def test_cli_show_records_recovery_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen import recovery
    from token_sparen.cli import main
    from token_sparen.store import keycache

    proj = _proj(tmp_path)
    ref = recovery.save_full(proj, FULL)
    for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", SID)
    monkeypatch.chdir(proj)
    key = keycache.cached_project_key(proj.resolve(), _env(tmp_path))
    ov = session.session_path(tmp_path / "home", key, SID)
    det = RegretDetector(_cfg(tmp_path), _env(tmp_path))
    ctx = SessionCtx(SID, key, 0, None, None, ov)
    det.track(
        ctx,
        "d9",
        point="compression",
        bucket="b",
        arm="light",
        recovery_id=ref.id,
        command_sig="pytest",
        full=FULL,
        kept=KEPT,
        ctype="log",
    )
    assert main(["show", ref.id]) == 0
    st = session.read_session(ov)
    assert st["recovery_read_tokens"] > 0
    assert st["regret"]["d9"]["best"] == 1.0
    # the next hook pays it
    rec = Recorder()
    ev = Event("claude", "prompt", SID, proj, key, None, prompt="hi")
    regret.handle(ev, _cfg(tmp_path), rec, env=_env(tmp_path))
    assert rec.regrets() == [("d9", 1.0)]


def test_cli_show_without_session_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from token_sparen import recovery
    from token_sparen.cli import main

    proj = _proj(tmp_path)
    ref = recovery.save_full(proj, "hello\n")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(proj)
    assert main(["show", ref.id]) == 0
    assert capsys.readouterr().out == "hello\n"
    assert not (tmp_path / "home" / "projects").exists() or not list(
        (tmp_path / "home" / "projects").rglob("sessions/*.json")
    )


def test_rerun_within_two_turns_is_full_regret(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _set_turn(tmp_path, 3)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests 2>&1 | tail -n 40"), rec)
    assert rec.regrets() == [("d1", 1.0)]


def test_rerun_later_is_half_regret_paid_at_expiry(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _set_turn(tmp_path, 5)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []  # partial: the max over signals is paid at expiry
    _set_turn(tmp_path, 8)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == [("d1", 0.5)]


def test_rerun_after_relevant_edit_not_regret(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    edit = _event(
        tmp_path,
        tool="Edit",
        tool_input={"file_path": "/x/proj/src/app/handlers.py", "old_string": "a"},
    )
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []
    _set_turn(tmp_path, 8)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == [("d1", 0.0)]


def test_rerun_after_edit_of_file_in_kept_text_not_regret(tmp_path: Path) -> None:
    # fix-and-rerun on a failure the agent could see is normal, not regret
    _track(tmp_path, turn=1)
    rec = Recorder()
    edit = _event(tmp_path, tool="Write", tool_input={"file_path": "/x/proj/tests/test_core.py"})
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []


def test_rerun_after_unrelated_edit_is_partial_regret(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    edit = _event(tmp_path, tool="Edit", tool_input={"file_path": "/x/proj/docs/notes.md"})
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5
    _set_turn(tmp_path, 8)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == [("d1", 0.5)]


def test_different_command_is_not_rerun(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests/test_other.py"), rec)
    _handle(tmp_path, _bash(tmp_path, "cd / && ls"), rec)
    assert rec.regrets() == []


def test_distinctive_tokens_partial_regret(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    one = "grep frobnicate_widget_matrix src"
    _handle(tmp_path, _bash(tmp_path, one), rec)  # one token: not enough
    _set_turn(tmp_path, 2)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="hi"), rec)
    st = _state(tmp_path)["regret"]["d1"]
    assert st["best"] == 0.0
    two = "where is frobnicate_widget_matrix vs zanzibar_overflow_handler defined?"
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt=two), rec)
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5
    _set_turn(tmp_path, 9)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == [("d1", 0.5)]


def test_tokens_in_tool_input_count(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    ev = _event(
        tmp_path,
        tool="Grep",
        tool_input={"pattern": "frobnicate_widget_matrix|quuxly_reticulated_spline"},
    )
    _handle(tmp_path, ev, rec)
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5


def test_tokens_in_edit_input_do_not_count(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    body = "frobnicate_widget_matrix quuxly_reticulated_spline"
    ev = _event(tmp_path, tool="Write", tool_input={"file_path": "/x/a.txt", "content": body})
    _handle(tmp_path, ev, rec)
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.0


def test_no_regret_credited_on_expiry(tmp_path: Path) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _set_turn(tmp_path, 7)  # 7 - 1 = 6: still inside the window
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == []
    _set_turn(tmp_path, 8)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), rec)
    assert rec.regrets() == [("d1", 0.0)]  # explicit zero, never silence
    assert "d1" not in _state(tmp_path)["regret"]
    assert _state(tmp_path).get("regrets", 0) == 0


def test_expiry_with_real_engine_records_regret_kind(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cfg = _cfg(tmp_path)
    eng = Engine(StoreChain(KEY, home, SID, params=cfg.data), cfg, random.Random(3), env={})
    eng._remember("d1", "compression", "b1", "standard")
    _track(tmp_path, "d1", turn=1)
    _set_turn(tmp_path, 9)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="go"), eng)
    pending = _state(tmp_path)["pending"]["d1"]
    assert pending[4] == ["regret"]
    ev = _state(tmp_path)["evidence"]["compression"]["b1"]["standard"]["regret"]
    assert ev[0] == 0.0 and ev[1] > 0  # a zero observation


def test_one_regret_per_decision(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    cmd = "cat .token-sparen/outputs/abcdef012345.txt"
    for _ in range(3):
        _handle(tmp_path, _bash(tmp_path, cmd), rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    _set_turn(tmp_path, 20)
    _handle(tmp_path, _event(tmp_path, kind="session_end", tool=None), rec)
    assert rec.regrets() == [("d1", 1.0)]
    assert _state(tmp_path)["regrets"] == 1


def test_session_end_settles_everything(tmp_path: Path) -> None:
    _track(tmp_path, "d1", turn=1)
    _track(tmp_path, "d2", turn=1, command="make", recovery_id=None)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "make", text="x"), rec)
    rec.calls.clear()
    det = RegretDetector(_cfg(tmp_path), _env(tmp_path))
    det.on_event(
        _event(
            tmp_path,
            prompt="frobnicate_widget_matrix quuxly_reticulated_spline",
            kind="prompt",
            tool=None,
        ),
        _ctx(tmp_path, 2),
    )
    _handle(tmp_path, _event(tmp_path, kind="session_end", tool=None), rec)
    assert sorted(rec.regrets()) == [("d1", 0.5)]
    assert _state(tmp_path)["regret"] == {}


def test_on_loop_flags_error_bearing_same_turn(tmp_path: Path) -> None:
    _track(tmp_path, "d1", turn=4, ctype="stacktrace")
    _track(tmp_path, "d2", turn=4, ctype="json")
    _track(tmp_path, "d3", turn=3, ctype="log")
    _track(tmp_path, "d4", turn=4, ctype="log", point="read_dedupe")
    det = RegretDetector(_cfg(tmp_path), _env(tmp_path))
    got = det.on_loop(_ctx(tmp_path, 4), ["d1", "d2", "d3", "d4"])
    assert got == [("d1", 0.5)]
    assert det.on_loop(_ctx(tmp_path, 4), None) == [("d1", 0.5)]
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5


def test_on_event_returns_signals(tmp_path: Path) -> None:
    det = _track(tmp_path, turn=1)
    _set_turn(tmp_path, 2)
    got = det.on_event(_bash(tmp_path, "pytest -q tests"), _ctx(tmp_path, 2))
    assert got == [("d1", 1.0)]
    assert det.on_event(_bash(tmp_path, "ls"), _ctx(tmp_path, 2)) == []


def test_track_is_bounded_and_evicted_are_paid(tmp_path: Path) -> None:
    det = RegretDetector(_cfg(tmp_path), _env(tmp_path))
    ctx = _ctx(tmp_path, 1)
    paid: list[tuple[str, float]] = []
    for i in range(205):
        paid += det.track(
            ctx,
            f"d{i}",
            point="compression",
            bucket="b",
            arm="light",
            recovery_id=None,
            command_sig=f"cmd {i}",
            full=FULL,
            kept=KEPT,
            ctype="log",
        )
    st = _state(tmp_path)["regret"]
    assert len(st) == 200
    assert "d0" not in st and "d204" in st
    assert paid == [(f"d{i}", 0.0) for i in range(5)]


def test_entry_fields(tmp_path: Path) -> None:
    _track(tmp_path, turn=2)
    e = _state(tmp_path)["regret"]["d1"]
    assert e["point"] == "compression" and e["bucket"] == "b1" and e["arm"] == "standard"
    assert e["recovery_id"] == "abcdef012345"
    assert e["command_sig"] == "pytest -q tests"
    assert e["decided_turn"] == 2
    assert e["edited_paths_since"] == []
    assert regret.token_digest("frobnicate_widget_matrix") in e["elided_tokens"]
    assert "src/app/handlers.py" in e["files_in_span"]


def test_corrupt_state_fails_open(tmp_path: Path) -> None:
    path = _overlay(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"turn": 3, "regret": {"x": "junk", "y": {"best": "no"}}}), "utf-8")
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "pytest"), rec)
    _handle(tmp_path, _event(tmp_path, kind="prompt", tool=None, prompt="a"), rec)
    path.write_text(json.dumps({"turn": 3, "regret": [1, 2]}), "utf-8")
    _handle(tmp_path, _bash(tmp_path, "pytest"), rec)
    assert rec.regrets() == []


def test_handle_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("x")

    _track(tmp_path)
    monkeypatch.setattr(regret.RegretDetector, "_process", boom)
    _handle(tmp_path, _bash(tmp_path, "pytest"), Recorder())
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    assert "regret" in log.read_text(encoding="utf-8")


def test_window_is_configurable(tmp_path: Path) -> None:
    assert RegretDetector(_cfg(tmp_path), _env(tmp_path)).window == 6
    cfg = _cfg(tmp_path, LEARN__REGRET_WINDOW_TURNS="2")
    assert RegretDetector(cfg, _env(tmp_path)).window == 2


# --------------------------------------------------------------------------- savings
def test_savings_first_observation_is_half(tmp_path: Path) -> None:
    home = tmp_path / "home"
    v = regret.savings_value(home, KEY, "compression", "b1", 1000, 10.0, _env(tmp_path))
    assert v == 0.5
    doc = json.loads((home / "projects" / KEY / "savings_scale.json").read_text("utf-8"))
    assert doc["points"]["compression"]["b1"] == [10000.0]


def test_savings_p95_behaviour(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(tmp_path)
    regret.savings_value(home, KEY, "compression", "b1", 100, 1.0, env)
    for _ in range(70):
        regret.savings_value(home, KEY, "compression", "b1", 100, 1.0, env)
    assert regret.savings_value(home, KEY, "compression", "b1", 50, 1.0, env) == pytest.approx(0.5)
    assert regret.savings_value(home, KEY, "compression", "b1", 1000, 1.0, env) == 1.0  # capped
    assert regret.savings_value(home, KEY, "compression", "b1", 0, 1.0, env) == 0.0
    # other buckets are independent
    assert regret.savings_value(home, KEY, "compression", "b2", 50, 1.0, env) == 0.5
    doc = json.loads((home / "projects" / KEY / "savings_scale.json").read_text("utf-8"))
    assert len(doc["points"]["compression"]["b1"]) == 64  # bounded reservoir


def test_savings_scale_corrupt_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "projects" / KEY / "savings_scale.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"points": {"compression": {"b1": ["x", -1]}}}', "utf-8")
    assert regret.savings_value(home, KEY, "compression", "b1", 10, 1.0, _env(tmp_path)) == 0.5


def test_expected_resends() -> None:
    assert regret.expected_resends(None) == 10.0
    assert regret.expected_resends(0.2) == 1.0
    assert regret.expected_resends(37.5) == 37.5


def test_remaining_turns_from_governor_cache() -> None:
    from token_sparen.mechanisms import governor

    assert governor.remaining_turns({}) is None
    overlay = {"governor": {"burn": 1000, "ewma": 100.0}}
    cfg = Config()
    got = governor.remaining_turns(overlay, cfg)
    assert got is not None and got > 0
    assert governor.remaining_turns({"governor": {"burn": 1, "ewma": 0}}, cfg) is None


# --------------------------------------------------------------------------- mechanisms
def _bash_event(tmp_path: Path, stdout: str, command: str = "pytest -v") -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}
    return Event(
        host="claude",
        kind="post_tool",
        session_id=SID,
        project_dir=_proj(tmp_path),
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall("Bash", tool_category("Bash"), {"command": command}, "tu-1"),
        output_text=stdout,
        raw_response=response,
    )


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_compression_with_engine_rewards_savings_and_tracks(tmp_path: Path) -> None:
    rec = Recorder()
    ev = _bash_event(tmp_path, big_pytest(), command=f"cd {_proj(tmp_path)} && pytest -v")
    act = compression.handle_post_tool(ev, rec, _cfg(tmp_path), env=_env(tmp_path))
    assert act.replace_output is not None
    did = _decisions(tmp_path)[-1]["decision_id"]
    assert rec.savings() == [(did, 0.5)]  # first observation of the bucket
    assert rec.regrets() == []
    e = _state(tmp_path)["regret"][did]
    assert e["recovery_id"] == _decisions(tmp_path)[-1]["recovery_id"]
    assert e["command_sig"] == "pytest -v"
    assert _decisions(tmp_path)[-1]["command_sig"] == "pytest -v"  # normalised log


def test_compression_noop_trim_arm_rewards_zero(tmp_path: Path) -> None:
    rec = Recorder()
    ev = _bash_event(tmp_path, big_pytest())
    cfg = _cfg(tmp_path, COMPRESSION__MIN_SAVING_RATIO="0.99")
    act = compression.handle_post_tool(ev, rec, cfg, env=_env(tmp_path))
    assert act.replace_output is None
    did = _decisions(tmp_path)[-1]["decision_id"]
    assert rec.calls == [(did, 0.0, 0.0)]
    assert did not in _state(tmp_path).get("regret", {})


def test_compression_safe_arm_gets_no_reward(tmp_path: Path) -> None:
    rec = Recorder(arm="off")
    ev = _bash_event(tmp_path, big_pytest())
    compression.handle_post_tool(ev, rec, _cfg(tmp_path), env=_env(tmp_path))
    assert rec.calls == []
    assert "regret" not in _state(tmp_path)


def test_static_policy_path_unaffected(tmp_path: Path) -> None:
    ev = _bash_event(tmp_path, big_pytest())
    act = compression.handle_post_tool(ev, StaticPolicy({}), _cfg(tmp_path), env=_env(tmp_path))
    assert act.replace_output is not None
    assert not (tmp_path / "home" / "projects" / KEY / "savings_scale.json").exists()
    did = _decisions(tmp_path)[-1]["decision_id"]
    assert did in _state(tmp_path)["regret"]  # tracked for the ledger's regret count


def test_compression_skips_recovery_reads(tmp_path: Path) -> None:
    rec = Recorder()
    text = big_pytest()
    cmd = "cat .token-sparen/outputs/abcdef012345.txt"
    ev = _bash_event(tmp_path, text, command=cmd)
    assert compression.handle_post_tool(ev, rec, _cfg(tmp_path), env=_env(tmp_path)).is_noop()
    ev = _bash_event(tmp_path, text, command="token-sparen show abcdef012345")
    assert compression.handle_post_tool(ev, rec, _cfg(tmp_path), env=_env(tmp_path)).is_noop()
    assert rec.calls == []


def test_read_dedupe_pointer_tracked_with_engine(tmp_path: Path) -> None:
    from token_sparen.adapters.claude.translate import tool_category

    f = tmp_path / "big.py"
    content = "\n".join(f"{i:5d}\tvalue_{i} = compute(alpha, beta, gamma)" for i in range(1500))
    f.write_text(content, encoding="utf-8")
    ev = Event(
        host="claude",
        kind="post_tool",
        session_id=SID,
        project_dir=_proj(tmp_path),
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall("Read", tool_category("Read"), {"file_path": str(f)}, "tu"),
        output_text=content,
    )
    rec = Recorder(arm="pointer")
    cfg = _cfg(tmp_path)
    read_dedupe.handle_post_tool(ev, rec, cfg, env=_env(tmp_path))
    act = read_dedupe.handle_post_tool(ev, rec, cfg, env=_env(tmp_path))
    assert act.replace_output is not None
    did = rec.savings()[0][0]
    assert rec.savings() == [(did, 0.5)]
    e = _state(tmp_path)["regret"][did]
    assert e["point"] == "read_dedupe" and e["command_sig"].startswith("Read:")
    # re-reading the same file right after the pointer is regret
    rec2 = Recorder()
    _handle(tmp_path, ev, rec2)
    assert rec2.regrets() == [(did, 1.0)]
    assert _state(tmp_path)["reads"] == {}  # regret: the pointed-at read is forgotten


def test_pretrim_lookup_uses_normalised_command(tmp_path: Path) -> None:
    from token_sparen.mechanisms import guards

    store = tmp_path / "home" / "projects" / KEY
    store.mkdir(parents=True)
    (store / "pretrim.json").write_text(
        json.dumps({"candidates": {"npm test": {"lines": 40}}}), encoding="utf-8"
    )
    ev = _bash_event(tmp_path, "x", command=f"cd {_proj(tmp_path)} && npm test 2>&1")
    assert guards.pretrim_lines(ev, store, _cfg(tmp_path), env=_env(tmp_path)) == 40


# --------------------------------------------------------------------------- review round 1
def test_file_regex_needs_separator_or_known_extension() -> None:
    text = "cache.put('k1') pytest.mark.parametrize at foo.py and a/b.weird; Model.load(x) x.toml"
    files = regret.span_files(text, "")
    assert "foo.py" in files and "a/b.weird" in files and "x.toml" in files
    assert "cache.put" not in files and "pytest.mark.parametrize" not in files
    assert "Model.load" not in files


def test_large_suite_keeps_failing_file_and_fix_rerun_is_not_regret(tmp_path: Path) -> None:
    ev = _bash_event(tmp_path, big_pytest(400_000), command="pytest -v")
    act = compression.handle_post_tool(ev, StaticPolicy({}), _cfg(tmp_path), env=_env(tmp_path))
    assert act.replace_output is not None
    (did,) = _state(tmp_path)["regret"]
    files = _state(tmp_path)["regret"][did]["files_in_span"]
    assert "tests/test_models.py" in files  # > 32 passing files elided, the failing one kept
    rec = Recorder()
    edit = _event(tmp_path, tool="Edit", tool_input={"file_path": "/w/proj/tests/test_models.py"})
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -v"), rec)
    assert rec.regrets() == []


def test_edit_matches_mentioned_file_by_basename(tmp_path: Path) -> None:
    _track(tmp_path, files=["widget_store.py"])  # a bare name: matched by file name
    rec = Recorder()
    edit = _event(tmp_path, tool="Edit", tool_input={"file_path": "/repo/src/widget_store.py"})
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []


def test_normalize_keeps_cd_target_relative_to_project() -> None:
    repo = "/repo"
    a = normalize_command("cd packages/a && npm test", project_dir=repo)
    b = normalize_command("cd packages/b && npm test", project_dir=repo)
    assert a == "cd packages/a && npm test" and a != b
    assert normalize_command("cd /repo/packages/a && npm test", project_dir=repo) == a
    assert normalize_command("cd a && npm test", project_dir=repo, cwd="/repo/packages") == a
    # the effective run directory counts, with or without a cd
    assert normalize_command("npm test", project_dir=repo, cwd="/repo/packages/a") == a
    assert normalize_command("npm test", project_dir=repo, cwd="/repo") == "npm test"
    assert normalize_command("cd /repo && npm test", project_dir=repo, cwd="/repo/x") == "npm test"
    assert normalize_command("cd /tmp/x && ls", project_dir=repo) == "cd /tmp/x && ls"
    assert normalize_command("cd .. && cd repo && make", project_dir=repo) == "make"


def test_rerun_in_another_package_is_not_regret(tmp_path: Path) -> None:
    _track(tmp_path, command="cd packages/a && npm test")
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "cd packages/b && npm test"), rec)
    assert rec.regrets() == []
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.0
    _handle(tmp_path, _bash(tmp_path, f"cd {_proj(tmp_path)}/packages/a && npm test"), rec)
    # same command; the run in packages/b may have changed state, so only partial regret
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5


@pytest.mark.parametrize(
    ("tool", "field"),
    [("Grep", "path"), ("Glob", "path"), ("Glob", "pattern"), ("mcp__fs__read", "file")],
)
def test_recovery_path_in_any_tool_input_is_full_regret(
    tmp_path: Path, tool: str, field: str
) -> None:
    _track(tmp_path)
    rec = Recorder()
    target = f"{_proj(tmp_path)}/.token-sparen/outputs/abcdef012345.txt"
    ev = _event(tmp_path, tool=tool, tool_input={field: target, "other": "x"}, text="y" * 40)
    _handle(tmp_path, ev, rec)
    assert rec.regrets() == [("d1", 1.0)]
    assert _state(tmp_path)["recovery_read_tokens"] == 10


def test_recovery_path_not_at_end_of_input_is_not_a_read(tmp_path: Path) -> None:
    _track(tmp_path)
    rec = Recorder()
    note = ".token-sparen/outputs/abcdef012345.txt is where outputs go"
    _handle(tmp_path, _event(tmp_path, tool="WebSearch", tool_input={"query": note}), rec)
    assert rec.regrets() == []


@pytest.mark.parametrize(
    ("first", "again"),
    [
        ("pytest -q tests", "pytest -q tests | grep FAILED"),
        ("pytest -q tests | grep FAILED", "pytest -q tests"),
        ("make 2>&1 | grep error", "make | head -n 50"),
        ("make", "make 2>&1 | sed -n '1,40p' | awk '{print $1}'"),
        ("npm test | less", "npm test | tail -5"),
        ("cat app.log | grep ERROR", "cat app.log | grep WARN"),
    ],
)
def test_rerun_with_other_filter_pipeline_is_partial_regret(
    tmp_path: Path, first: str, again: str
) -> None:
    _track(tmp_path, command=first)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, again), rec)
    assert rec.regrets() == []  # only the filter-free base matches: 0.5, paid at expiry
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5


def test_rerun_with_same_filter_pipeline_is_full_regret(tmp_path: Path) -> None:
    _track(tmp_path, command="cat app.log | grep ERROR")
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "cat app.log | grep ERROR 2>&1"), rec)
    assert rec.regrets() == [("d1", 1.0)]


def test_filter_on_another_command_is_not_rerun(tmp_path: Path) -> None:
    _track(tmp_path, command="pytest -q tests | grep FAILED")
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests/test_x.py | grep FAILED"), rec)
    _handle(tmp_path, _bash(tmp_path, "cat log.txt | grep FAILED"), rec)
    assert rec.regrets() == []


def test_filter_base() -> None:
    assert regret.filter_base("pytest -q | grep -v x | head -n 3") == "pytest -q"
    assert regret.filter_base("pytest -q") == "pytest -q"
    assert regret.filter_base("cat a | wc -l") == "cat a | wc -l"


@pytest.mark.parametrize(
    "between",
    [
        "pip install requests",
        "echo x > notes.txt",
        "git checkout main",
        "rm -rf build",
        "find . -name '*.pyc' -delete",
        "npm run build",
        "ls && touch a",
    ],
)
def test_rerun_after_state_change_is_partial_regret(tmp_path: Path, between: str) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, between), rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == []
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5


@pytest.mark.parametrize(
    "between",
    [
        "git status",
        "git diff HEAD -- a.py",
        "ls -la | grep x",
        "cat a.py > /dev/null",
        "cd src && pwd",
        "which python",
        "wc -l a.py",
        "find . -name '*.py'",
        "echo hi",
        "rg -n 'a|b' src",
        "head -n 5 a.py 2>&1",
        "git log --oneline -5",
    ],
)
def test_rerun_after_read_only_command_is_full_regret(tmp_path: Path, between: str) -> None:
    _track(tmp_path, turn=1)
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, between), rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert rec.regrets() == [("d1", 1.0)]


def test_read_only_classifier() -> None:
    assert regret.read_only("git -C sub status")
    assert not regret.read_only("git push")
    assert not regret.read_only("cat 'unbalanced")
    assert not regret.read_only("ls >> listing.txt")
    assert regret.read_only("")


def test_elided_tokens_are_stored_hashed(tmp_path: Path) -> None:
    _track(tmp_path)
    raw = _overlay(tmp_path).read_text(encoding="utf-8")
    assert "frobnicate_widget_matrix" not in raw and "zanzibar_overflow_handler" not in raw
    toks = _state(tmp_path)["regret"]["d1"]["elided_tokens"]
    assert toks and all(len(t) == 16 and int(t, 16) >= 0 for t in toks)
    assert regret.token_digest("abc") == regret.token_digest("abc") != regret.token_digest("abd")


def test_plain_command_in_another_package_cwd_is_not_rerun(tmp_path: Path) -> None:
    proj = _proj(tmp_path)
    _track(tmp_path, command="npm test", cwd=f"{proj}/packages/a")
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "npm test", cwd=f"{proj}/packages/b"), rec)
    assert rec.regrets() == []
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.0


def test_cd_then_plain_command_from_that_cwd_is_rerun(tmp_path: Path) -> None:
    proj = _proj(tmp_path)
    _track(tmp_path, command="cd packages/a && npm test", cwd=str(proj))
    rec = Recorder()
    _handle(tmp_path, _bash(tmp_path, "npm test", cwd=f"{proj}/packages/a"), rec)
    assert rec.regrets() == [("d1", 1.0)]


def test_compression_signature_uses_effective_run_dir(tmp_path: Path) -> None:
    from types import MappingProxyType

    proj = _proj(tmp_path)
    ev = _bash_event(tmp_path, big_pytest(), command="pytest -v")
    ev = Event(**{**ev.__dict__, "extra": MappingProxyType({"cwd": f"{proj}/sub"})})
    compression.handle_post_tool(ev, StaticPolicy({}), _cfg(tmp_path), env=_env(tmp_path))
    assert _decisions(tmp_path)[-1]["command_sig"] == "cd sub && pytest -v"


def test_pretrim_lookup_uses_effective_run_dir(tmp_path: Path) -> None:
    from types import MappingProxyType

    from token_sparen.mechanisms import guards

    proj = _proj(tmp_path)
    store = tmp_path / "home" / "projects" / KEY
    store.mkdir(parents=True)
    (store / "pretrim.json").write_text(
        json.dumps({"candidates": {"cd web && npm test": {"lines": 40}}}), encoding="utf-8"
    )
    ev = _bash_event(tmp_path, "x", command="npm test")
    cfg = _cfg(tmp_path)
    assert guards.pretrim_lines(ev, store, cfg, env=_env(tmp_path)) is None
    ev = Event(**{**ev.__dict__, "extra": MappingProxyType({"cwd": f"{proj}/web"})})
    assert guards.pretrim_lines(ev, store, cfg, env=_env(tmp_path)) == 40


def test_basename_fallback_only_for_bare_mentions() -> None:
    assert not regret._path_match("/p/b/index.ts", "packages/a/src/index.ts")
    assert not regret._path_match("/x/pkg_b/__init__.py", "pkg_a/__init__.py")
    assert regret._path_match("/x/packages/a/src/index.ts", "packages/a/src/index.ts")
    assert regret._path_match("/x/tests/conftest.py", "conftest.py")
    assert regret._path_match("/abs/a.py", "/abs/a.py")


def test_edit_of_same_named_file_elsewhere_does_not_exempt(tmp_path: Path) -> None:
    _track(tmp_path, files=["packages/a/src/index.ts"])
    rec = Recorder()
    edit = _event(tmp_path, tool="Edit", tool_input={"file_path": "/p/b/index.ts"})
    _handle(tmp_path, edit, rec)
    _handle(tmp_path, _bash(tmp_path, "pytest -q tests"), rec)
    assert _state(tmp_path)["regret"]["d1"]["best"] == 0.5  # an edit elsewhere: partial


def test_recovery_paths_are_not_files_in_span(tmp_path: Path) -> None:
    kept = (
        KEPT + "\n[token-sparen: kept 2 of 5 lines · full output: "
        "/w/proj/.token-sparen/outputs/abcdef012345.txt · `token-sparen show abcdef012345`]"
    )
    _track(tmp_path, kept=kept)
    files = _state(tmp_path)["regret"]["d1"]["files_in_span"]
    assert files and not any(".token-sparen" in f for f in files)


def test_drop_reads_owned_by_read_dedupe() -> None:
    state = {"reads": {"k1": {"p": "aa"}, "k2": {"p": "bb"}, "k3": "junk"}}
    read_dedupe.drop_reads(state, {"aa"})
    assert set(state["reads"]) == {"k2", "k3"}
    empty: dict[str, Any] = {}
    read_dedupe.drop_reads(empty, {"aa"})
    assert empty == {}
