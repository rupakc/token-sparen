"""Read de-duplication: a re-read of unchanged, still-in-context content becomes a pointer."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Event, EventKind, ToolCall
from token_sparen.learn.policy import READ_DEDUPE, StaticPolicy
from token_sparen.mechanisms import read_dedupe, tracking
from token_sparen.store import session

KEY = "0123456789abcdef"
POINTER = (
    "[token-sparen: unchanged since turn {n} — content already in context above; "
    "re-read with a different range or after edits]"
)


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return env


def _cfg(tmp_path: Path, **over: str) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _content(lines: int = 1500) -> str:
    return "\n".join(f"{i:5d}\tvalue_{i} = compute(alpha, beta, gamma)" for i in range(lines))


def _file(tmp_path: Path, text: str | None = None, name: str = "mod.py") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    path = proj / name
    path.write_text(_content() if text is None else text, encoding="utf-8")
    return path


def _event(
    tmp_path: Path,
    kind: EventKind,
    tool: str | None = None,
    tool_input: Mapping[str, Any] | None = None,
    response: Any = None,
    text: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    call = None
    if tool is not None:
        call = ToolCall(tool, tool_category(tool), dict(tool_input or {}), "tu-1")
    return Event(
        host="claude",
        kind=kind,
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=call,
        output_text=text,
        raw_response=response,
        extra=MappingProxyType(dict(extra or {})),
    )


def _read_event(tmp_path: Path, path: Path, content: str | None = None, **inp: Any) -> Event:
    text = path.read_text(encoding="utf-8") if content is None else content
    resp = {
        "type": "text",
        "file": {
            "filePath": str(path),
            "content": text,
            "numLines": text.count("\n") + 1,
            "startLine": 1,
            "totalLines": text.count("\n") + 1,
        },
    }
    extra = inp.pop("extra", None)
    return _event(tmp_path, "post_tool", "Read", {"file_path": str(path), **inp}, resp, text, extra)


def _read(tmp_path: Path, path: Path, arms: dict[str, str] | None = None, **inp: Any) -> Any:
    cfg = inp.pop("cfg", None) or _cfg(tmp_path)
    return read_dedupe.handle_post_tool(
        _read_event(tmp_path, path, **inp), StaticPolicy(arms or {}), cfg, env=_env(tmp_path)
    )


def _prompt(tmp_path: Path) -> None:
    ev = _event(tmp_path, "prompt")
    tracking.handle_prompt(ev, _cfg(tmp_path), env=_env(tmp_path))


def _overlay(tmp_path: Path) -> dict[str, Any]:
    return session.read_session(session.session_path(tmp_path / "home", KEY, "s-1"))


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_point_contract() -> None:
    assert read_dedupe.READ_DEDUPE is READ_DEDUPE
    assert READ_DEDUPE.name == "read_dedupe" and READ_DEDUPE.arms == ("off", "pointer")
    assert READ_DEDUPE.safe_arm == "off" and READ_DEDUPE.tier == "low"
    assert read_dedupe.POINTER == POINTER


def test_read_dedupe_pointer_when_unchanged(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _prompt(tmp_path)  # turn 1
    assert _read(tmp_path, path).is_noop()
    assert _decisions(tmp_path) == []
    _prompt(tmp_path)
    _prompt(tmp_path)  # turn 3
    act = _read(tmp_path, path)
    assert act.replace_output == POINTER.format(n=1)
    (rec,) = _decisions(tmp_path)
    assert rec["point"] == "read_dedupe" and rec["arm"] == "pointer"
    assert rec["tokens_after"] < rec["tokens_before"] and rec["tool_name"] == "Read"
    assert rec["recovery_id"] is None
    # the pointer keeps the original turn; the entry is not refreshed
    _prompt(tmp_path)
    assert _read(tmp_path, path).replace_output == POINTER.format(n=1)
    assert _overlay(tmp_path)["dedupes"] == 2


def test_read_dedupe_full_after_edit(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    for tool, key in (
        ("Edit", "file_path"),
        ("Write", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ):
        pre = _event(tmp_path, "pre_tool", tool, {key: str(path)})
        assert read_dedupe.invalidate(pre, env=_env(tmp_path)).is_noop()
        assert _read(tmp_path, path).is_noop()  # full content again (and re-recorded)
    assert _read(tmp_path, path).replace_output is not None


def test_read_dedupe_edit_of_other_file_keeps_entry(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    pre = _event(tmp_path, "pre_tool", "Edit", {"file_path": str(tmp_path / "proj" / "o.py")})
    read_dedupe.invalidate(pre, env=_env(tmp_path))
    read_dedupe.invalidate(_event(tmp_path, "pre_tool", "Bash", {"command": "ls"}))
    read_dedupe.invalidate(_event(tmp_path, "pre_tool", "Edit", {"file_path": 3}))
    assert _read(tmp_path, path).replace_output is not None


def test_read_dedupe_full_when_content_changed(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    path.write_text(_content() + "\nnew line", encoding="utf-8")  # e.g. `sed -i` via Bash
    assert _read(tmp_path, path).is_noop()
    # same bytes but a touched mtime: still re-sent in full
    _read(tmp_path, path)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _read(tmp_path, path).is_noop()


def test_read_dedupe_full_after_compact(tmp_path: Path) -> None:
    path = _file(tmp_path)
    env = _env(tmp_path)
    _prompt(tmp_path)
    _read(tmp_path, path)
    _prompt(tmp_path)
    read_dedupe.on_pre_compact(_event(tmp_path, "pre_compact"), env=env)
    assert _overlay(tmp_path)["last_compact_turn"] == 2
    assert _read(tmp_path, path).is_noop()  # earlier read was compacted away
    # a read after the compaction (same turn) is in context again
    assert _read(tmp_path, path).replace_output == POINTER.format(n=2)


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_session_start_compact_or_clear_resets(tmp_path: Path, source: str) -> None:
    path = _file(tmp_path)
    env = _env(tmp_path)
    _read(tmp_path, path)
    read_dedupe.on_session_start(
        _event(tmp_path, "session_start", extra={"source": source}), env=env
    )
    assert _read(tmp_path, path).is_noop()


@pytest.mark.parametrize("source", ["startup", "resume", None])
def test_session_start_other_sources_keep_table(tmp_path: Path, source: str | None) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    extra = {} if source is None else {"source": source}
    read_dedupe.on_session_start(_event(tmp_path, "session_start", extra=extra))
    assert _read(tmp_path, path).replace_output is not None


def test_stale_entry_before_compact_turn_ignored(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _prompt(tmp_path)
    _read(tmp_path, path)
    # simulate an entry that survived a compaction (e.g. written by an older version)
    sp = session.session_path(tmp_path / "home", KEY, "s-1")
    session.update_session(sp, lambda s: {**s, "last_compact_turn": 2})
    assert _read(tmp_path, path).is_noop()


def test_pointer_only_within_turn_gap(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _prompt(tmp_path)  # turn 1
    _read(tmp_path, path)
    for _ in range(8):
        _prompt(tmp_path)  # turn 9: gap 8
    assert _read(tmp_path, path).replace_output == POINTER.format(n=1)
    _prompt(tmp_path)  # turn 10: gap 9, too old to trust; full content, entry refreshed
    assert _read(tmp_path, path).is_noop()
    assert _read(tmp_path, path).replace_output == POINTER.format(n=10)
    cfg = _cfg(tmp_path, READ_DEDUPE__MAX_TURN_GAP="0")
    _prompt(tmp_path)
    assert _read(tmp_path, path, cfg=cfg).is_noop()


def test_range_is_part_of_the_key(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    lines = _content().splitlines()
    part = "\n".join(lines[10:1400])
    assert _read(tmp_path, path, content=part, offset=10, limit=1390).is_noop()
    assert _read(tmp_path, path, content=part, offset=10).is_noop()
    assert _read(tmp_path, path, content=part, offset=10, limit=1390).replace_output is not None
    assert _read(tmp_path, path).replace_output is not None


def test_small_reads_are_never_replaced(tmp_path: Path) -> None:
    path = _file(tmp_path, "x = 1\n")
    _read(tmp_path, path)
    assert _read(tmp_path, path).is_noop()
    assert _decisions(tmp_path) == []
    big = _file(tmp_path, name="b.py")
    _read(tmp_path, big)
    # a configured floor below 2000 tokens is clamped up to the invariant
    cfg = _cfg(tmp_path, READ_DEDUPE__MIN_TOKENS="10")
    assert _read(tmp_path, path, cfg=cfg).is_noop()


def test_arm_off_and_disabled(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    assert _read(tmp_path, path, {"read_dedupe": "off"}).is_noop()
    assert [r["arm"] for r in _decisions(tmp_path)] == ["off"]
    cfg = _cfg(tmp_path, READ_DEDUPE__ENABLED="false")
    assert _read(tmp_path, path, cfg=cfg).is_noop()
    assert len(_decisions(tmp_path)) == 1


def test_agent_scoping(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    # a subagent has its own context: the main agent's read is not "above" for it
    assert _read(tmp_path, path, extra={"agent_id": "sub-1"}).is_noop()
    assert _read(tmp_path, path, extra={"agent_id": "sub-1"}).replace_output is not None
    assert _read(tmp_path, path).replace_output is not None


def test_forget_drops_entry(tmp_path: Path) -> None:
    path = _file(tmp_path)
    ev = _read_event(tmp_path, path)
    read_dedupe.handle_post_tool(ev, StaticPolicy({}), _cfg(tmp_path), env=_env(tmp_path))
    read_dedupe.forget(ev, env=_env(tmp_path))  # e.g. compression replaced that output
    assert _read(tmp_path, path).is_noop()


def test_ignores_non_read_and_unreplaceable(tmp_path: Path) -> None:
    path = _file(tmp_path)
    cfg, env = _cfg(tmp_path), _env(tmp_path)
    pol = StaticPolicy({})
    bash = _event(tmp_path, "post_tool", "Bash", {"command": "cat x"}, {"stdout": "x"}, "x")
    assert read_dedupe.handle_post_tool(bash, pol, cfg, env=env).is_noop()
    ev = _read_event(tmp_path, path)
    fail = Event(**{**ev.__dict__, "kind": "post_tool_failure"})
    assert read_dedupe.handle_post_tool(fail, pol, cfg, env=env).is_noop()
    no_text = Event(**{**ev.__dict__, "output_text": None})
    assert read_dedupe.handle_post_tool(no_text, pol, cfg, env=env).is_noop()
    missing = _read_event(tmp_path, path)
    path.unlink()
    assert read_dedupe.handle_post_tool(missing, pol, cfg, env=env).is_noop()
    assert "reads" not in _overlay(tmp_path)
    veto = _read_event(tmp_path, _file(tmp_path))
    for _ in range(2):
        act = read_dedupe.handle_post_tool(veto, pol, cfg, env=env, replaceable=lambda e: False)
        assert act.is_noop()
    bad = _event(tmp_path, "post_tool", "Read", {"file_path": 7}, {}, "x" * 9000)
    assert read_dedupe.handle_post_tool(bad, pol, cfg, env=env).is_noop()


def test_table_is_bounded(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, READ_DEDUPE__MAX_ENTRIES="3")
    files = [_file(tmp_path, name=f"f{i}.py") for i in range(5)]
    for f in files:
        _read(tmp_path, f, cfg=cfg)
    assert len(_overlay(tmp_path)["reads"]) == 3
    assert _read(tmp_path, files[0], cfg=cfg).is_noop()  # evicted (oldest)
    assert _read(tmp_path, files[4], cfg=cfg).replace_output is not None


def test_overlay_stores_no_raw_paths(tmp_path: Path) -> None:
    path = _file(tmp_path)
    _read(tmp_path, path)
    reads = json.dumps(_overlay(tmp_path)["reads"])
    assert str(path) not in reads and "mod.py" not in reads


@pytest.mark.perf
def test_read_dedupe_fast(tmp_path: Path) -> None:
    from perfutil import best_of, perf_factor

    path = _file(tmp_path, _content(6000))
    cfg = _cfg(tmp_path)
    _read(tmp_path, path, cfg=cfg)
    best, act = best_of(lambda: _read(tmp_path, path, cfg=cfg), 5)
    assert act.replace_output is not None
    assert best < 0.025 * perf_factor()
