from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from token_sparen.events import Event, SessionCtx, ToolCall, ToolCategory
from token_sparen.learn import features
from token_sparen.learn.policy import COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF
from token_sparen.mechanisms import tracking

KEY = "0123456789abcdef"


def _event(
    tmp_path: Path,
    *,
    category: ToolCategory = "shell",
    text: str | None = "x",
    kind: Any = "post_tool",
    file_path: str | None = None,
) -> Event:
    return Event(
        host="claude",
        kind=kind,
        session_id="s-1",
        project_dir=tmp_path,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall(
            name="T",
            category=category,
            input={} if file_path is None else {"file_path": file_path},
            use_id="u1",
        ),
        output_text=text,
    )


def _ctx(tmp_path: Path, ctx_pct: float | None = None, cats: tuple[str, ...] = ()) -> SessionCtx:
    return SessionCtx(
        session_id="s-1",
        project_key=KEY,
        turn=1,
        ctx_pct=ctx_pct,
        model=None,
        overlay_path=tmp_path / "o.json",
        recent_cats=cats,
    )


@pytest.mark.parametrize(
    ("pct", "band"),
    [
        (None, "unk"),
        (0.0, "lt50"),
        (0.4999, "lt50"),
        (0.5, "50_75"),
        (0.7499, "50_75"),
        (0.75, "75_95"),
        (0.9499, "75_95"),
        (0.95, "gt95"),
        (1.2, "gt95"),
    ],
)
def test_band_boundaries(tmp_path: Path, pct: float | None, band: str) -> None:
    assert features.band(pct) == band
    f = features.for_point(COMPRESSION, _event(tmp_path), _ctx(tmp_path, pct), "log")
    assert f["band"] == band


@pytest.mark.parametrize(
    ("tokens", "size"),
    [(0, "sm"), (7999, "sm"), (8000, "md"), (49999, "md"), (50000, "lg"), (10**6, "lg")],
)
def test_band_and_size_boundaries(tmp_path: Path, tokens: int, size: str) -> None:
    assert features.size_band(tokens) == size
    f = features.for_point(COMPRESSION, _event(tmp_path), _ctx(tmp_path, 0.3), "log", tokens=tokens)
    assert f["size"] == size


def test_size_estimated_from_output_when_not_given(tmp_path: Path) -> None:
    ev = _event(tmp_path, text="word " * 20000)
    assert features.for_point(READ_DEDUPE, ev, _ctx(tmp_path), None)["size"] == "md"
    ev = _event(tmp_path, text=None)
    assert features.for_point(READ_DEDUPE, ev, _ctx(tmp_path), None)["size"] == "sm"


def test_compression_key_order_and_values(tmp_path: Path) -> None:
    cats = ("edit",) * 5
    f = features.for_point(
        COMPRESSION, _event(tmp_path, category="search"), _ctx(tmp_path, 0.8, cats), "json"
    )
    assert list(f) == ["band", "cat", "size", "ctype", "phase"]
    assert f == {"band": "75_95", "cat": "search", "size": "sm", "ctype": "json", "phase": "build"}


def test_other_points_feature_sets(tmp_path: Path) -> None:
    ev, ctx = _event(tmp_path, category="read"), _ctx(tmp_path)
    assert list(features.for_point(READ_DEDUPE, ev, ctx, None)) == ["cat", "size"]
    assert features.for_point(READ_DEDUPE, ev, ctx, None)["cat"] == "read"
    pre = features.for_point(PRETRIM, ev, ctx, None)
    assert pre == {"ctype": "unk", "size": "sm"}
    assert features.for_point(SUBAGENT_BRIEF, ev, ctx, None) == {"phase": "mixed"}


def test_no_tool_and_unknown_point(tmp_path: Path) -> None:
    from token_sparen.learn.policy import DecisionPoint

    ev = Event("claude", "pre_tool", "s", tmp_path, KEY, None)
    f = features.for_point(COMPRESSION, ev, _ctx(tmp_path), None)
    assert f["cat"] == "other" and f["ctype"] == "unk"
    ext = DecisionPoint("ext_point", ("off", "on"), "off", "low")
    assert features.for_point(ext, ev, _ctx(tmp_path), None) == {}


@pytest.mark.parametrize(
    ("cats", "phase"),
    [
        ((), "mixed"),
        (("edit", "edit"), "mixed"),  # fewer than 3 tool calls
        (("read", "search", "read"), "explore"),
        (
            ("read", "read", "search", "read", "shell", "shell", "web", "read", "mcp", "read"),
            "explore",
        ),
        (
            ("read", "read", "search", "read", "shell", "shell", "web", "agent", "mcp", "other"),
            "mixed",
        ),
        (("edit", "edit", "edit", "read", "read", "read", "read", "read", "read", "read"), "build"),
        (
            ("edit", "edit", "read", "read", "read", "read", "read", "shell", "shell", "shell"),
            "mixed",
        ),
        (
            (
                "shell!",
                "shell!",
                "shell!",
                "shell",
                "read",
                "read",
                "read",
                "shell",
                "shell",
                "web",
            ),
            "debug",
        ),
        # failures and edits both >= 30 %: debugging wins
        (
            ("edit", "edit", "edit", "shell!", "shell!", "shell!", "read", "read", "read", "read"),
            "debug",
        ),
        # only the last 10 count
        (("edit",) * 10 + ("read",) * 10, "explore"),
    ],
)
def test_phase_inference(cats: tuple[str, ...], phase: str) -> None:
    assert features.phase(cats) == phase


def test_bucket_key() -> None:
    f = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
    assert features.bucket_key(f, ["band", "cat", "size", "ctype", "phase"]) == (
        "band=lt50|cat=shell|size=sm|ctype=log|phase=build"
    )
    assert features.bucket_key(f, ["ctype", "cat"]) == "ctype=log|cat=shell"
    assert features.bucket_key(f, []) == "*"
    assert features.bucket_key(f, ["missing"]) == "missing=unk"


def test_backoff_order() -> None:
    levels = features.backoff_levels(COMPRESSION.name)
    assert levels == [
        ["band", "cat", "size", "ctype", "phase"],
        ["cat", "ctype"],
        ["ctype"],
        [],
    ]
    f = {"band": "lt50", "cat": "shell", "size": "sm", "ctype": "log", "phase": "build"}
    assert features.backoff_keys(COMPRESSION.name, f) == [
        "band=lt50|cat=shell|size=sm|ctype=log|phase=build",
        "cat=shell|ctype=log",
        "ctype=log",
        "*",
    ]
    for p in (PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF):
        lv = features.backoff_levels(p.name)
        assert lv[0] == list(features.FEATURE_KEYS[p.name]) and lv[-1] == []
    assert features.backoff_levels("ext_point") == [[]]


def test_backoff_levels_from_params() -> None:
    params = {"learn": {"backoff": {"compression": {"levels": [["ctype"], []]}}}}
    assert features.backoff_levels("compression", params) == [["ctype"], []]
    bad = {"learn": {"backoff": {"compression": {"levels": "nope"}}}}
    assert features.backoff_levels("compression", bad)[0] == list(
        features.FEATURE_KEYS["compression"]
    )
    worse = {"learn": {"backoff": {"compression": {"levels": [["ctype", 3]]}}}}
    assert features.backoff_levels("compression", worse) == features.backoff_levels("compression")
    # a params dict without the table falls back to the packaged defaults
    assert features.backoff_levels("pretrim", {"learn": {}}) == [["ctype", "size"], ["ctype"], []]
    no_star = {"learn": {"backoff": {"compression": {"levels": [["ctype"]]}}}}
    assert features.backoff_levels("compression", no_star) == [["ctype"], []]


@pytest.mark.parametrize(
    "feats", [{"ctype": "a|b"}, {"ctype": "a=b"}, {"ct|ype": "x"}, {"ct=ype": "x"}]
)
def test_bucket_key_rejects_separators(feats: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        features.bucket_key(feats, list(feats))


def test_session_ctx_carries_recent_cats(tmp_path: Path) -> None:
    ev = _event(tmp_path)
    overlay = {"recent_cats": ["read", 5, "edit", "shell!"]}
    ctx = tracking.session_ctx(ev, overlay, tmp_path / "o.json")
    assert ctx.recent_cats == ("read", "edit", "shell!")
    assert tracking.session_ctx(ev, {"recent_cats": "x"}, tmp_path / "o.json").recent_cats == ()


def test_record_tool_history_bounded(tmp_path: Path) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    for i in range(12):
        tracking.record_paths(_event(tmp_path, category="read" if i % 2 else "edit"), env=env)
    tracking.record_paths(_event(tmp_path, category="shell", kind="post_tool_failure"), env=env)
    from token_sparen.store import session

    ov = session.read_session(tracking.overlay_path(_event(tmp_path), env))
    assert len(ov["recent_cats"]) == tracking.RECENT_CATS == 10
    assert ov["recent_cats"][-1] == "shell!"
    assert ov["recent_cats"][-2] == "read"
    assert "recent_paths" not in ov  # no file_path in these events


def test_feature_values_are_sanitised(tmp_path: Path) -> None:
    f = features.for_point(COMPRESSION, _event(tmp_path), _ctx(tmp_path), "ext|type=x")
    assert f["ctype"] == "ext_type_x"
    key = features.bucket_key(f, list(features.FEATURE_KEYS["compression"]))
    assert "ctype=ext_type_x" in key
    assert features.for_point(PRETRIM, _event(tmp_path), _ctx(tmp_path), "a=b")["ctype"] == "a_b"


# --- M4 Task 6: the band uses the reported context window ---------------------------------


def test_window_band_uses_reported_window(tmp_path: Path) -> None:
    from token_sparen.config import load_config
    from token_sparen.mechanisms import governor

    assert features.reported_window("claude-sonnet-4-5[1m]") == features.WINDOW_1M
    assert features.reported_window("CLAUDE-OPUS-4-6[1M]") == features.WINDOW_1M
    assert features.reported_window("claude-sonnet-4-5") is None
    assert features.reported_window(None) is None
    # pure: an authoritative window (statusline, env, table match) always wins
    assert features.band_window(500_000, True, ["x[1m]"], 900_000) == 500_000
    # defaulted: the reported model id, else a call larger than 200k proves a 1M window
    assert features.band_window(200_000, False, [None, "m[1m]"], 10_000) == features.WINDOW_1M
    assert features.band_window(200_000, False, ["m"], 300_000) == features.WINDOW_1M
    assert features.band_window(200_000, False, ["m"], 150_000) == 200_000  # unknown: as before
    assert features.band_window(200_000, False, ["m"], 2_000_000) == 200_000  # beyond any window

    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    cfg = load_config(tmp_path / "proj", env)
    # a 1M-context session without the statusline sensor, previously mis-banded ``unk``
    overlay = {"governor": {"last_ctx": 300_000, "model": "claude-sonnet-4-5"}}
    pct = governor.ctx_pct(overlay, cfg, env, "s-1")
    assert pct == pytest.approx(0.3)
    assert features.band(pct) == "lt50"
    # ... and one whose hook-reported model id carries the 1M marker (previously 75 % full)
    overlay = {"governor": {"last_ctx": 150_000, "model": "claude-sonnet-4-5"}}
    assert governor.ctx_pct(overlay, cfg, env, "s-1") == pytest.approx(0.75)  # unknown: as before
    pct = governor.ctx_pct(overlay, cfg, env, "s-1", model="claude-sonnet-4-5[1m]")
    assert pct == pytest.approx(0.15)
    # the statusline-recorded window stays authoritative
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "windows.json").write_text('{"s-1": {"window": 200000}}', "utf-8")
    assert governor.ctx_pct(overlay, cfg, env, "s-1", model="x[1m]") == pytest.approx(0.75)
