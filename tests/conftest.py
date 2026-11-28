"""Suite-wide fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import perfutil  # noqa: F401  (reads TOKEN_SPAREN_PERF_FACTOR before any fixture deletes it)
import pytest
from envguard import _no_ignored_widening_env  # noqa: F401  (autouse fixture)


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
    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_popen_init)


_REAL_POPEN_INIT = subprocess.Popen.__init__
_HOME_VARS = ("USERPROFILE",) if os.name == "nt" else ("HOME",)


def _runs_token_sparen(argv: Any) -> bool:
    """A Python interpreter, the hooks or bundled CLI (also inside an ``sh -c`` string), or the
    ``token-sparen`` console script."""
    parts = [argv] if isinstance(argv, str | os.PathLike) else list(argv or [])
    if not parts:
        return False
    first = os.path.basename(os.fspath(parts[0])).lower()
    if first.startswith(("python", "token-sparen")) or first in ("py", "py.exe"):
        return True
    for part in parts:
        text = os.fspath(part).replace("\\", "/")
        if "hooks/run.py" in text or "hooks/run_cli.py" in text or text == "token_sparen":
            return True
    return os.fspath(parts[0]) == sys.executable


def _guarded_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
    """v1 review m-Q4: a child process falls back to the real ``~/.token-sparen`` when its
    ``TOKEN_SPAREN_HOME`` is rejected (inside the project, not ours) and its home is the real one,
    which an explicit ``env`` without ``HOME`` gives it (the password database is consulted). Such
    a probe wrote to the real state root once; every token-sparen process started with an
    explicit environment must carry a temporary home (git, uv and other tools are not checked)."""
    env = kwargs.get("env")
    if env is None and len(args) > 9:  # positional ``env`` (never used in this suite)
        env = args[9]
    if env is not None and _runs_token_sparen(args[0] if args else kwargs.get("args")):
        for name in _HOME_VARS:
            value = env.get(name)
            if not value or _is_real(Path(value)):
                raise AssertionError(
                    f"a test starts a subprocess whose {name} is "
                    f"{'missing' if not value else 'the real home'}: set it to a temporary "
                    "directory (a rejected TOKEN_SPAREN_HOME falls back to ~/.token-sparen)"
                )
    _REAL_POPEN_INIT(self, *args, **kwargs)


_REAL_HOME = Path(os.path.expanduser("~")).resolve()  # read at import, before any fixture


def _is_real(path: Path) -> bool:
    """``path`` is the developer's real home or lies in its ``.token-sparen``."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == _REAL_HOME or resolved.is_relative_to(_REAL_HOME / ".token-sparen")
