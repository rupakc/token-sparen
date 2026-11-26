"""Hook-path cost of extension discovery when no extension directory exists."""

from __future__ import annotations

from pathlib import Path

import pytest
from perfutil import best_of, perf_factor

from token_sparen import extensions
from token_sparen.config import Config

pytestmark = pytest.mark.perf


def test_discovery_without_dirs_is_cheap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extensions, "_entry_points", lambda group: [])
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    env = {"TOKEN_SPAREN_HOME": str(home)}

    def once() -> object:
        extensions.reset()
        loaded = extensions.load_extensions(proj, home, Config(), env=env)
        return loaded.compressor_for("text")

    best, _ = best_of(once, 50)
    extensions.reset()
    assert best < 0.0005 * perf_factor()  # two lstat calls, no hashing, no imports
