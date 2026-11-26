"""`[learn.hold] compression = true` is one clean off switch for output trimming (M2 close C2).

It holds the engine's compression decisions, the static fallback (learning disabled, engine
construction or decide errors) and the learned pre-trim arm (a compression hold implies a
pre-trim hold). ``compression.allow_ctypes = []`` is the config-only switch: no content type
may be trimmed, so no decision is even asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from integration.test_learning_loop import Driver, decisions
from sampleoutputs import big_pytest

from token_sparen.config import load_config
from token_sparen.events import SessionCtx, ToolCall
from token_sparen.learn import features
from token_sparen.learn.policy import COMPRESSION, PRETRIM, READ_DEDUPE, StaticPolicy
from token_sparen.mechanisms import compression
from token_sparen.store import keycache, paths

ROUNDS = 12
_CTX = SessionCtx("s", "0" * 16, 0, None, None, Path("s.json"))


def _command(n: int) -> str:
    return f"pytest -q tests/test_mod_{n:04d}.py"


def _seed_pretrim(d: Driver) -> None:
    """Every command this driver will run is a learned pre-trim candidate."""
    key = keycache.cached_project_key(d.proj, d.env)
    store = paths.project_dir_for(d.home, key)
    store.mkdir(parents=True, exist_ok=True)
    cands = {_command(n): {"lines": 40} for n in range(1, ROUNDS + 1)}
    (store / "pretrim.json").write_text(json.dumps({"candidates": cands}), encoding="utf-8")


def _run(d: Driver) -> list[bool]:
    """ROUNDS distinct pytest runs through ``entry.main``; whether each output was replaced."""
    replaced: list[bool] = []
    for n in range(1, ROUNDS + 1):
        d.prompt()
        out = d.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": _command(n)},
            tool_use_id=f"tu-{n}",
            tool_response={
                "stdout": big_pytest(12_000 + 900 * n),
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        )
        replaced.append("hookSpecificOutput" in out)
    return replaced


def _boom(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("engine exploded")


MODES = ["learning", "learning_off", "decide_error", "build_error"]


def _driver(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch, **env: str) -> Driver:
    from token_sparen.learn import engine as engine_mod
    from token_sparen.learn import hookpolicy

    if mode == "learning_off":
        env["TOKEN_SPAREN_LEARN__ENABLED"] = "off"
    elif mode == "decide_error":
        monkeypatch.setattr(engine_mod.Engine, "_decide", _boom)
    elif mode == "build_error":
        monkeypatch.setattr(hookpolicy, "StoreChain", _boom)
    return Driver(tmp_path / "home", tmp_path / "proj", f"s-{mode}", **env)


@pytest.mark.parametrize("mode", MODES)
def test_without_hold_outputs_are_trimmed(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same setup trims when nothing is held."""
    d = _driver(tmp_path, mode, monkeypatch)
    _seed_pretrim(d)
    assert any(_run(d))


@pytest.mark.parametrize("spelling", ["true", "1", "yes", "ON"])
@pytest.mark.parametrize("mode", MODES)
def test_compression_hold_stops_all_trimming(
    tmp_path: Path, mode: str, spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _driver(tmp_path, mode, monkeypatch, TOKEN_SPAREN_LEARN__HOLD__COMPRESSION=spelling)
    _seed_pretrim(d)
    assert not any(_run(d))
    recs = decisions(d.home)
    assert recs, "decisions are still logged (propensity 1, safe arm)"
    assert {(r["point"], r["arm"]) for r in recs} <= {("pretrim", "off"), ("compression", "off")}
    assert all(r["recovery_id"] is None for r in recs)
    assert not list((d.proj / ".token-sparen" / "outputs").glob("*.txt"))


def test_compression_hold_from_the_user_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[learn]\nenabled = false\n[learn.hold]\ncompression = "yes"\n', encoding="utf-8"
    )
    d = Driver(home, tmp_path / "proj", "s-user")
    _seed_pretrim(d)
    assert not any(_run(d))


@pytest.mark.parametrize("mode", MODES)
def test_empty_allow_ctypes_is_the_config_only_switch(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("[compression]\nallow_ctypes = []\n", encoding="utf-8")
    d = _driver(tmp_path, mode, monkeypatch)
    _seed_pretrim(d)
    assert not any(_run(d))
    assert decisions(d.home) == []  # gated before any decision


def test_cli_run_honours_the_hold(tmp_path: Path) -> None:
    """`token-sparen run` (static policy) honours holds too."""
    proj = tmp_path / "proj"
    proj.mkdir()
    tool = ToolCall("Bash", "execute", {"command": "pytest -q"}, "run")
    text = big_pytest(30_000)
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    body, info = compression.compress_text(text, tool, load_config(proj, env), proj, env)
    assert info is not None and body != text  # control
    env["TOKEN_SPAREN_LEARN__HOLD__COMPRESSION"] = "on"
    body, info = compression.compress_text(text, tool, load_config(proj, env), proj, env)
    assert info is None and body == text


def test_static_policy_honours_holds() -> None:
    held = {"compression", "read_dedupe"}
    pol = StaticPolicy({"compression": "aggressive", "pretrim": "bounded"}, held=held.__contains__)
    assert pol.decide(COMPRESSION, {}, _CTX).arm == "off"
    assert pol.decide(READ_DEDUPE, {}, _CTX).arm == "off"
    assert pol.decide(PRETRIM, {}, _CTX).arm == "bounded"  # not held by this predicate
    assert StaticPolicy({}).decide(COMPRESSION, {}, _CTX).arm == "standard"


def test_compression_hold_implies_pretrim_hold() -> None:
    assert features.held({"learn": {"hold": {"compression": "yes"}}}, "pretrim")
    assert features.held({"learn": {"hold": {"pretrim": True}}}, "pretrim")
    assert not features.held({"learn": {"hold": {"pretrim": True}}}, "compression")
    assert not features.held({}, "pretrim")
