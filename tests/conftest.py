"""Suite-wide fixtures."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_detached_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook tests must not leave detached ``token-sparen fold`` processes behind."""
    from token_sparen.learn import overlay

    def popen(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(overlay, "POPEN", popen)


@pytest.fixture(autouse=True)
def _deterministic_engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook tests are deterministic: without ``TOKEN_SPAREN_TEST_SEED`` the in-process engine
    is seeded per hook from a fixed suite seed (every test uses a temporary state root).
    Production keeps ``os.urandom`` (``hookpolicy.test_seed`` itself is unchanged; tests of it
    are marked ``real_seed``)."""
    from token_sparen.learn import hookpolicy

    if request.node.get_closest_marker("real_seed") is not None:
        return

    real = hookpolicy.test_seed

    def seeded(env: Any) -> str | None:
        return real(env) or ("suite" if env.get("TOKEN_SPAREN_HOME") else None)

    monkeypatch.setattr(hookpolicy, "test_seed", seeded)
