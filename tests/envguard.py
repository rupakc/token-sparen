"""Autouse fixture failing tests that set a widening TOKEN_SPAREN_* env var (see its docstring)."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_ignored_widening_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """The env layer is narrowing-only (``config.sanitise_env``): a test that sets a widening
    ``TOKEN_SPAREN_*`` variable silently runs the default behaviour and proves nothing. Fail it;
    widening belongs in a temporary user ``config.toml``. Tests of the rule itself carry
    ``@pytest.mark.env_drop_expected``."""
    from token_sparen import config

    dropped: list[str] = []
    real = config.sanitise_env

    def spy(layer: Any, trusted: Any, defaults: Any) -> Any:
        clean, gone = real(layer, trusted, defaults)
        dropped.extend(key for key, _ in gone)
        return clean, gone

    monkeypatch.setattr(config, "sanitise_env", spy)
    yield
    if dropped and request.node.get_closest_marker("env_drop_expected") is None:
        raise AssertionError(
            f"TOKEN_SPAREN_* env var(s) ignored as widening: {sorted(set(dropped))}; "
            "use a temporary user config.toml (or mark the test env_drop_expected)"
        )
