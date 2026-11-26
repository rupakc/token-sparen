"""The policy a hook hands to its mechanisms: the learning Engine, built on first use.

Most hooks never reach a decision point, so nothing is constructed until ``decide`` (or a
reward) is actually called. ``[learn] enabled = false`` -> ``StaticPolicy`` (the static arms,
propensity 1). Fail open: if the Engine cannot be built, or a decision errs (the engine logs it
and answers with bucket ``error``), this hook falls back to the static decision for the rest of
its decisions; construction failures are logged once per hook.

The rng is seeded from ``os.urandom``. Tests may set ``TOKEN_SPAREN_TEST_SEED``; it is honoured
only when ``TOKEN_SPAREN_HOME`` points somewhere other than the default state root, so a real
session can never be made deterministic by a stray variable.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable, Mapping
from pathlib import Path

from token_sparen.config import Config, log_error, state_home
from token_sparen.events import SessionCtx
from token_sparen.learn.engine import ERROR_BUCKET, Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import Decision, DecisionPoint, StaticPolicy

TEST_SEED_ENV = "TOKEN_SPAREN_TEST_SEED"
Reward = Callable[[str, float | None, float | None], None]


def _default_home() -> Path:
    return Path.home() / ".token-sparen"


def test_seed(env: Mapping[str, str]) -> str | None:
    """``TOKEN_SPAREN_TEST_SEED`` if set together with a non-default ``TOKEN_SPAREN_HOME``."""
    seed = env.get(TEST_SEED_ENV)
    home = env.get("TOKEN_SPAREN_HOME")
    if not seed or not home:
        return None
    try:
        if Path(home).expanduser().resolve() == _default_home().resolve():
            return None
    except (OSError, RuntimeError):
        return None
    return seed


def learning_enabled(cfg: Config) -> bool:
    return cfg.get("learn.enabled", True) is not False


class HookPolicy:
    """Per-hook policy (see the module docstring). ``salt`` varies a test seed per hook."""

    def __init__(
        self,
        cfg: Config,
        env: Mapping[str, str],
        project_key: str,
        session_id: str | None,
        *,
        salt: str = "",
    ) -> None:
        self.cfg = cfg
        self.env = env
        self.project_key = project_key
        self.session_id = session_id
        self.salt = salt
        self.static = StaticPolicy({})
        self._engine: Engine | None = None
        self._unavailable = not learning_enabled(cfg)  # disabled, or the engine failed to build
        self._decide_failed = False

    @property
    def learning(self) -> bool:
        """True while this hook's decisions still come from the Engine."""
        return not (self._unavailable or self._decide_failed)

    def _rng(self) -> random.Random:
        seed = test_seed(self.env)
        if seed is not None:
            return random.Random(f"{seed}|{self.session_id}|{self.salt}")  # noqa: S311
        return random.Random(int.from_bytes(os.urandom(16), "big"))  # noqa: S311

    def engine(self) -> Engine | None:
        """The Engine, built on first call; None when disabled or it could not be built."""
        if self._unavailable:
            return None
        if self._engine is None:
            try:
                stores = StoreChain(
                    self.project_key,
                    state_home(self.env),
                    self.session_id,
                    params=self.cfg.data,
                )
                self._engine = Engine(stores, self.cfg, self._rng(), env=self.env)
            except Exception as exc:  # noqa: BLE001 - fail open: static policy for this hook
                self._unavailable = True
                log_error(self.env, "hookpolicy", f"engine: {type(exc).__name__}: {exc}")
                return None
        return self._engine

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        eng = None if self._decide_failed else self.engine()
        if eng is not None:
            decision = eng.decide(point, features, session)
            if decision.bucket != ERROR_BUCKET:
                return decision
            self._decide_failed = True  # the engine logged it; static for the rest of the hook
        return self.static.decide(point, features, session)

    @property
    def reward(self) -> Reward | None:
        """The Engine's ``reward`` unless learning is disabled or the engine cannot be built
        (``regret.reward_fn`` duck-typing: then nothing is computed or paid). A decide error
        does not stop rewards for decisions of earlier hooks; static decisions are never
        pending, so the engine ignores rewards for them."""
        eng = self.engine()
        return None if eng is None else eng.reward
