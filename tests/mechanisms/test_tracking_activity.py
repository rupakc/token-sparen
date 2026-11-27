"""Per-session activity for the tool-pruning idle gate (sessions that ended do not count)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from token_sparen.events import Event
from token_sparen.mechanisms import tracking

KEY = "0123456789abcdef"


def _ev(tmp_path: Path, sid: str) -> Event:
    return Event("claude", "session_start", sid, tmp_path, KEY, None)  # type: ignore[arg-type]


def test_others_active_excludes_the_folded_and_ended_sessions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"TOKEN_SPAREN_HOME": str(home)}
    assert not tracking.others_active(home, KEY, "a", 3600)  # no record at all
    tracking.record_active(_ev(tmp_path, "a"), env=env)
    assert not tracking.others_active(home, KEY, "a", 3600)  # only the folded session
    tracking.record_active(_ev(tmp_path, "b"), env=env)
    assert tracking.others_active(home, KEY, "a", 3600)  # b is live
    tracking.record_end(_ev(tmp_path, "b"), env=env)
    assert not tracking.others_active(home, KEY, "a", 3600)  # b ended
    assert tracking.last_active(home, KEY, 3600)[0] == "b"  # `session disable` still sees it


def test_legacy_record_and_age(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = tracking.last_active_path(home, KEY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"session_id": "old", "ts": time.time() - 60}), "utf-8")
    assert tracking.others_active(home, KEY, "a", 3600)
    assert not tracking.others_active(home, KEY, "old", 3600)
    assert not tracking.others_active(home, KEY, "a", 30)  # older than the window


def test_touch_is_throttled_per_session(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"TOKEN_SPAREN_HOME": str(home)}
    tracking.record_active(_ev(tmp_path, "a"), env=env)
    tracking.touch_active(_ev(tmp_path, "b"), env=env)  # a's fresh stamp does not throttle b
    state = json.loads(tracking.last_active_path(home, KEY).read_text("utf-8"))
    assert set(state["sessions"]) == {"a", "b"}
