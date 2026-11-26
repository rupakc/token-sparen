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
