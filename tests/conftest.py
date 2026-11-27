"""Suite-wide fixtures."""

from __future__ import annotations

import os
from pathlib import Path
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


@pytest.fixture(autouse=True)
def _private_user_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected ``TOKEN_SPAREN_HOME`` (review M5) falls back to ``~/.token-sparen``: tests must
    never reach the real one, so the user's home is a temporary directory for every test."""
    from token_sparen import config

    for name in list(os.environ):  # nothing inherited from the developer's shell or session
        if name.startswith("TOKEN_SPAREN_") or name == "CLAUDE_CONFIG_DIR":
            monkeypatch.delenv(name)
    home = tmp_path_factory.mktemp("userhome")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home / "state"))
    config.reset_env_path_checks()

    def guarded_home() -> Path:
        here = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or "")
        if not here.parts or _is_real(here):
            raise AssertionError("a test would use the real home directory")
        return here

    real_tighten = config._tighten_root

    def guarded_root(root: Path) -> None:
        if _is_real(root):
            raise AssertionError(f"a test would use the real state root {root}")
        real_tighten(root)

    monkeypatch.setattr(Path, "home", staticmethod(guarded_home))
    monkeypatch.setattr(config, "_tighten_root", guarded_root)


_REAL_HOME = Path(os.path.expanduser("~")).resolve()  # read at import, before any fixture


def _is_real(path: Path) -> bool:
    """``path`` is the developer's real home or lies in its ``.token-sparen``."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == _REAL_HOME or resolved.is_relative_to(_REAL_HOME / ".token-sparen")
