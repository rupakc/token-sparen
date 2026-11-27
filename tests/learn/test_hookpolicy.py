"""Per-hook policy: lazy Engine, static fallback, test seed gate (Task 23)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from userconf import to_user_config

from token_sparen.config import load_config
from token_sparen.events import SessionCtx
from token_sparen.learn import hookpolicy, regret
from token_sparen.learn.engine import Engine
from token_sparen.learn.hookpolicy import HookPolicy
from token_sparen.learn.policy import COMPRESSION, SUBAGENT_BRIEF

KEY = "0123456789abcdef"
FULL = {"band": "unk", "cat": "shell", "size": "sm", "ctype": "log", "phase": "mixed"}


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    # widening settings live in the user's file (a TOKEN_SPAREN_* variable may only narrow)
    return to_user_config({"TOKEN_SPAREN_HOME": str(tmp_path / "home"), **extra})


def _pol(tmp_path: Path, **extra: str) -> HookPolicy:
    env = _env(tmp_path, **extra)
    return HookPolicy(load_config(tmp_path / "proj", env), env, KEY, "s-1", salt="x")


def _ctx(tmp_path: Path) -> SessionCtx:
    path = tmp_path / "home" / "projects" / KEY / "sessions" / "s-1.json"
    return SessionCtx("s-1", KEY, 0, None, None, path)


def test_disabled_is_static_and_never_rewards(tmp_path: Path) -> None:
    pol = _pol(tmp_path, TOKEN_SPAREN_LEARN__ENABLED="false")
    d = pol.decide(COMPRESSION, FULL, _ctx(tmp_path))
    assert (d.arm, d.bucket, d.propensity) == ("standard", "static", 1.0)
    assert pol.engine() is None and pol.reward is None and not pol.learning
    assert regret.reward_fn(pol) is None  # no savings scale is computed for a static hook


def test_enabled_builds_one_engine_on_first_use(tmp_path: Path) -> None:
    pol = _pol(tmp_path)
    assert pol._engine is None  # nothing built until asked
    d = pol.decide(COMPRESSION, FULL, _ctx(tmp_path))
    assert d.bucket == "band=unk|cat=shell|size=sm|ctype=log|phase=mixed"
    eng = pol.engine()
    assert isinstance(eng, Engine) and pol.engine() is eng and pol.learning
    assert pol.reward is not None and regret.reward_fn(pol) is not None


def test_decide_error_is_static_for_the_rest_of_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def boom(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise RuntimeError("bad store")

    monkeypatch.setattr(Engine, "_decide", boom)
    pol = _pol(tmp_path)
    for _ in range(3):
        assert pol.decide(COMPRESSION, FULL, _ctx(tmp_path)).bucket == "static"
    assert calls == [1] and not pol.learning
    assert pol.reward is not None  # earlier hooks' decisions can still be rewarded


def test_held_point_is_decided_but_never_explored(tmp_path: Path) -> None:
    pol = _pol(tmp_path)
    eng = pol.engine()
    assert eng is not None and eng.held(SUBAGENT_BRIEF) and not eng.held(COMPRESSION)
    for _ in range(50):
        d = pol.decide(SUBAGENT_BRIEF, {"phase": "mixed"}, _ctx(tmp_path))
        assert (d.arm, d.propensity, d.bucket) == ("off", 1.0, "phase=mixed")
    assert eng.eligible(SUBAGENT_BRIEF, {"phase": "mixed"}) == []
    unheld = _pol(tmp_path, TOKEN_SPAREN_LEARN__HOLD__SUBAGENT_BRIEF="false").engine()
    assert unheld is not None and unheld.eligible(SUBAGENT_BRIEF, {"phase": "mixed"}) == ["on"]


@pytest.mark.real_seed
def test_seeded_rng_is_deterministic_per_salt(tmp_path: Path) -> None:
    env = _env(tmp_path, TOKEN_SPAREN_TEST_SEED="3")
    cfg = load_config(tmp_path / "proj", env)

    def draws(salt: str) -> list[float]:
        eng = HookPolicy(cfg, env, KEY, "s-1", salt=salt).engine()
        assert eng is not None
        return [eng.rng.random() for _ in range(3)]

    assert draws("a") == draws("a") and draws("a") != draws("b")
    unseeded = HookPolicy(cfg, _env(tmp_path), KEY, "s-1", salt="a").engine()
    assert unseeded is not None and [unseeded.rng.random() for _ in range(3)] != draws("a")


@pytest.mark.real_seed
def test_test_seed_needs_a_non_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hookpolicy.test_seed({"TOKEN_SPAREN_HOME": "/x/ts"}) is None  # no seed
    assert hookpolicy.test_seed({"TOKEN_SPAREN_TEST_SEED": "1"}) is None  # default home

    def broken() -> Path:
        raise RuntimeError("no home")

    monkeypatch.setattr(hookpolicy, "_default_home", broken)
    env = {"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": "/x/ts"}
    assert hookpolicy.test_seed(env) is None  # cannot tell: never seed


@pytest.mark.parametrize("value", ["0", "no", "false", "off"])
def test_any_false_spelling_disables_learning(tmp_path: Path, value: str) -> None:
    pol = _pol(tmp_path, TOKEN_SPAREN_LEARN__ENABLED=value)
    assert pol.decide(COMPRESSION, FULL, _ctx(tmp_path)).bucket == "static"
    env = _env(tmp_path, TOKEN_SPAREN_LEARN__ENABLED=value)
    cfg = load_config(tmp_path / "proj", env)
    from token_sparen.learn.hierarchy import StoreChain

    eng = Engine(
        StoreChain(KEY, tmp_path / "home", "s-1", params=cfg.data),
        cfg,
        __import__("random").Random(1),
    )
    assert not eng.enabled()
    assert eng.decide(COMPRESSION, FULL, _ctx(tmp_path)).bucket == "static"


@pytest.mark.real_seed
def test_test_seed_needs_a_temp_home(tmp_path: Path) -> None:
    import tempfile

    assert hookpolicy.test_seed({"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": str(tmp_path)})
    inside = Path(tempfile.gettempdir()) / "ts-home"
    assert hookpolicy.test_seed({"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": str(inside)})
    for home in ("/srv/ts-home", "/nonexistent-user-home/ts-elsewhere"):
        env = {"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": home}
        assert hookpolicy.test_seed(env) is None
