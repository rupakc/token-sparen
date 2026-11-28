"""Guard for the autouse ``_no_ignored_widening_env`` fixture (tests/envguard.py): a test that
sets a widening ``TOKEN_SPAREN_*`` variable runs the default behaviour (the env layer is
narrowing-only) and so proves nothing; it must error unless marked ``env_drop_expected``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

INNER = """
import pytest
from envguard import _no_ignored_widening_env  # noqa: F401
from token_sparen import config


def _load(tmp_path, **env):
    config.load_config(tmp_path, {"TOKEN_SPAREN_HOME": str(tmp_path), **env})


def test_widens(tmp_path):
    _load(tmp_path, TOKEN_SPAREN_LEARN__SAVINGS_REWARD="cost")


@pytest.mark.env_drop_expected
def test_widens_on_purpose(tmp_path):
    _load(tmp_path, TOKEN_SPAREN_LEARN__SAVINGS_REWARD="cost")


def test_narrows(tmp_path):
    _load(tmp_path, TOKEN_SPAREN_COMPRESSION__MIN_TOKENS="9999")
"""


def test_fixture_errors_a_test_that_widens_via_env(tmp_path: Path) -> None:
    (tmp_path / "test_inner.py").write_text(INNER, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    env_drop_expected\n", encoding="utf-8"
    )
    here = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOKEN_SPAREN_")}
    env["PYTHONPATH"] = os.pathsep.join([str(here), str(here.parent), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "."],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert "ignored as widening" in out and "learn.savings_reward" in out, out
    assert "1 error" in out and "3 passed" in out and "failed" not in out, out
