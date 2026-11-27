"""E1 at real-data scale: parallel, resumable, contamination-safe runs with lanes (M4 Task 2).

Synthetic transcripts only; nothing here reads the real home or real sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_replay import MARKERS, Session, _bash, big_output, resend_session

from token_sparen import cli
from token_sparen.replay import replay, runner

CUTOFF = date(2026, 11, 1)
COMP = ("compression", {"policy": "static"})
POSIX = os.name == "posix"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    return home


def comp(res: runner.RunResult, split: str = "all", policy: str = "static") -> dict[str, Any]:
    """The compression lane's aggregates of one split (the report's ``totals`` shape)."""
    return replay.acc_view(res.by_split[split]["compression"][policy])


def regret_session(sid: str, ts: str) -> Session:
    s = resend_session(sid, ts)
    s.call("m9", text=f"{MARKERS[0]} {MARKERS[1]}")
    return s


def dogfood_result(sid: str, ts: str) -> Session:
    s = resend_session(sid, ts)
    s.call("m8", tool=("t8", "Bash", {"command": "make more"}))
    s.result("t8", "line one\n[token-sparen: kept 10 of 400 lines · full output: x]")
    return s


# ------------------------------------------------------------------ review focus 1


def test_dogfood_sessions_excluded_whole(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    resend_session("dev-ok", "2026-10-10T09:00:00.000Z").write(proj / "a.jsonl")
    dogfood_result("dev-marked", "2026-10-11T09:00:00.000Z").write(proj / "b.jsonl")
    resend_session("late", "2026-11-26T00:00:01.000Z").write(proj / "c.jsonl")
    resend_session("test-ok", "2026-11-20T09:00:00.000Z").write(proj / "d.jsonl")
    quoted = resend_session("quoted", "2026-11-21T09:00:00.000Z")
    quoted.prompt("what does [token-sparen: kept 3 of 9 lines] mean?")  # anywhere counts
    quoted.write(proj / "e.jsonl")
    res = runner.run_parallel([tmp_path], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.files == 5 and res.excluded_dogfood == 3 and res.sessions == 2
    assert comp(res, "dev")["sessions"] == 1 and comp(res, "test")["sessions"] == 1
    one = replay.run([proj / "a.jsonl"], "static", "all", CUTOFF)
    # the marked session's earlier (unmarked) trim is not counted either: excluded whole
    assert comp(res, "dev")["trimmed"] == one.trimmed == 1


def test_dogfood_start_is_configurable(tmp_path: Path) -> None:
    resend_session("late", "2026-11-26T00:00:01.000Z").write(tmp_path / "p" / "a.jsonl")
    res = runner.run_parallel(
        [tmp_path], [COMP], jobs=1, cutoff=CUTOFF, dogfood_start=date(2026, 12, 1)
    )
    assert res.excluded_dogfood == 0 and comp(res, "test")["sessions"] == 1


def test_marker_inside_oversize_line_still_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MAX_LINE_BYTES", 4096)
    s = resend_session("big", "2026-10-10T09:00:00.000Z")
    s.call("m8", tool=("t8", "Bash", {"command": "make more"}))
    s.result("t8", "x" * 9000 + "[token-sparen: kept 1 of 2 lines]" + "y" * 9000)
    s.write(tmp_path / "p" / "a.jsonl")
    res = runner.run_parallel([tmp_path], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.excluded_dogfood == 1 and res.sessions == 0


# ------------------------------------------------------------------ review focus 2


def test_malformed_lines_are_counted_not_fatal(tmp_path: Path) -> None:
    good = resend_session("s", "2026-11-20T10:00:00.000Z")
    body = "".join(json.dumps(r) + "\n" for r in good.records).encode()
    bad = (
        b"not json at all\n"
        + b'{"type": "user", "message": {"content": "caf\xff\xfe"}}\n'  # not UTF-8
        + b"[1, 2, 3]\n"  # JSON, but not a record
        + b'{"type": "weird-future-record", "payload": 1}\n'  # unknown type
        + b'{"type": "user", "x": "'
        + b"a" * (8 * 1024 * 1024 + 10)
        + b'"}\n'  # over 8 MB
        + b"\n"
    )
    path = tmp_path / "p" / "a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(bad + body + b'{"type": "assistant", "message": {"id": "trunc')  # truncated
    res = runner.run_parallel([tmp_path], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.worker_errors == {}
    assert res.malformed_lines == 4  # not JSON, not UTF-8, not an object, truncated
    assert res.oversize_lines == 1
    assert res.skipped_records >= 1
    assert res.sessions == 1 and comp(res)["trimmed"] == 1


def test_reader_memory_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "MAX_LINE_BYTES", 1000)
    path = tmp_path / "a.jsonl"
    path.write_bytes(b'{"a": "' + b"z" * 50_000 + b'"}\n{"type": "user"}\n')
    stats = runner.Stats()
    longest = 0
    for raw in runner.iter_lines(path, stats):
        longest = max(longest, len(raw))
    assert longest <= 1001 and stats.oversize_lines == 1


def test_unreadable_file_is_not_fatal(tmp_path: Path) -> None:
    resend_session("ok", "2026-11-20T10:00:00.000Z").write(tmp_path / "p" / "a.jsonl")
    (tmp_path / "p" / "dir.jsonl").mkdir()  # a directory named like a transcript
    res = runner.run_parallel([tmp_path / "p"], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.worker_errors == {} and comp(res)["sessions"] == 1


# ------------------------------------------------------------------ review focus 3


def test_copied_history_not_recounted_across_split(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    first = resend_session("a", "2026-10-20T10:00:00.000Z")
    resumed = Session("a", "2026-11-05T10:00:00.000Z")  # test period, same ids (a resume)
    resumed.records = [dict(r, timestamp=resumed.ts) for r in first.records]
    resumed.call("m7", text="new work")
    # path order would replay the resume first: the runner must order files by time
    first.write(proj / "z-first.jsonl")
    resumed.write(proj / "a-resumed.jsonl")
    res = runner.run_parallel([proj], [COMP], jobs=1, cutoff=CUTOFF)
    dev, test = comp(res, "dev"), comp(res, "test")
    assert dev["trimmed"] == 1 and test["trimmed"] == 0
    assert test["calls"] == 1 and test["duplicate_results"] == 1
    # a test-only run still reads the dev history first (de-dup before split attribution)
    only = replay.run([proj], "static", "test", CUTOFF)
    assert only.calls == 1 and only.trimmed == 0 and set(only.by_split) == {"test"}


# ------------------------------------------------------------------ review focus 5


def _corpus(root: Path, n: int = 40, groups: int = 8) -> Path:
    for i in range(n):
        month, day = (10, 1 + i % 28) if i % 3 else (11, 2 + i % 20)
        ts = f"2026-{month:02d}-{day:02d}T{i % 24:02d}:00:00.000Z"
        s = regret_session(f"s{i}", ts) if i % 4 == 0 else resend_session(f"s{i}", ts)
        s.prompt(f"prompt {i}").call("m20", tool=_bash("t20"))
        s.result("t20", big_output(seed=i)).call("m21", text="ok")
        s.write(root / f"g{i % groups}" / f"s{i:02d}.jsonl")
    return root


def test_quiet_stdout_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _corpus(tmp_path / "in", n=6, groups=2)
    out = tmp_path / "priv" / "e1.json"
    args = ["replay", str(src), "--policy", "static", "--out", str(out), "--quiet"]
    assert cli.main([*args, "--jobs", "2", "--lanes", "compression"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    m = re.fullmatch(r"report: (.+) id: ([0-9a-f]{12})\nstatus: complete\n", captured.out)
    assert m is not None, captured.out
    assert Path(m.group(1)) == out.resolve()
    assert m.group(2) == hashlib.sha256(out.read_bytes()).hexdigest()[:12]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run"]["files"] == 6 and data["totals"]["sessions"] == 6
    assert not out.with_name(out.name + ".checkpoint").exists()  # removed after success


def test_worker_error_recorded_as_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _corpus(tmp_path / "in", n=6, groups=3)

    def boom(self: Any, *_a: Any) -> None:
        raise RuntimeError(f"cannot handle {tmp_path} secret-session-text")

    with monkeypatch.context() as m:
        m.setattr(replay.CompressionLane, "report", boom)
        res = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.worker_errors == res.failed_groups == {"RuntimeError": 3}
    assert not res.complete
    dumped = json.dumps(res.to_dict())
    assert "secret" not in dumped and str(tmp_path) not in dumped
    # across processes too: the lane fails at construction in each worker
    bad = ("compression", {"policy": "nonesuch-secret-policy"})
    res = runner.run_parallel([src], [bad], jobs=2, cutoff=CUTOFF)
    assert res.worker_errors == {"ValueError": 3}
    assert "secret" not in json.dumps(res.to_dict())


# ------------------------------------------------------------------ parallel and resume


def test_parallel_equals_serial(tmp_path: Path) -> None:
    src = _corpus(tmp_path / "in")
    specs = [("compression", {"policy": "light,aggressive"})]
    serial = runner.run_parallel([src], specs, jobs=1, cutoff=CUTOFF)
    parallel = runner.run_parallel([src], specs, jobs=4, cutoff=CUTOFF)
    assert serial.files == parallel.files == 40
    assert serial.worker_errors == parallel.worker_errors == {}
    assert serial.by_split == parallel.by_split
    a, b = serial.to_dict(), parallel.to_dict()
    a.pop("wall_s"), b.pop("wall_s")
    assert a == b
    assert comp(serial, "all", "aggressive")["trimmed"] > 0
    assert set(serial.by_split) == {"dev", "test", "all"}


def test_compression_lane_matches_replay_run(tmp_path: Path) -> None:
    src = _corpus(tmp_path / "in", n=12, groups=1)
    rep = replay.run([src], "light,standard", "all", CUTOFF)
    res = runner.run_parallel(
        [src], [("compression", {"policy": "light,standard"})], jobs=2, cutoff=CUTOFF
    )
    assert comp(res, "all", "light") == rep.totals
    assert comp(res, "dev", "standard") == rep.by_arm["standard"]["by_split"]["dev"]


def test_groups_selects_subset(tmp_path: Path) -> None:
    src = _corpus(tmp_path / "in", n=8, groups=4)
    keys = sorted(runner.group_files([src]))
    assert len(keys) == 4 and all(re.fullmatch(r"[0-9a-f]{16}", k) for k in keys)
    res = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, groups=set(keys[:2]))
    assert res.files == 4


def test_split_dev_stops_before_test_sessions(tmp_path: Path) -> None:
    src = _corpus(tmp_path / "in", n=9, groups=1)
    res = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, split="dev")
    assert set(res.by_split) == {"dev", "all"}
    assert res.by_split["all"] == res.by_split["dev"]


def test_resume_skips_finished_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _corpus(tmp_path / "in", n=9, groups=3)
    ckpt = tmp_path / "priv" / "run.checkpoint"
    fresh = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF)
    real = runner._work_group
    seen: list[str] = []

    def interrupted(task: Any) -> Any:
        if len(seen) == 2:
            raise KeyboardInterrupt
        seen.append(task.key)
        return real(task)

    monkeypatch.setattr(runner, "_work_group", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    doc = json.loads(ckpt.read_text(encoding="utf-8"))
    assert sorted(doc["done"]) == sorted(seen)
    if POSIX:
        assert stat.S_IMODE(ckpt.stat().st_mode) == 0o600
    assert str(tmp_path) not in ckpt.read_text(encoding="utf-8")

    resumed_keys: list[str] = []

    def spy(task: Any) -> Any:
        resumed_keys.append(task.key)
        return real(task)

    monkeypatch.setattr(runner, "_work_group", spy)
    resumed = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    assert len(resumed_keys) == 1 and resumed_keys[0] not in seen
    assert resumed.by_split == fresh.by_split
    assert resumed.files == fresh.files and resumed.sessions == fresh.sessions
    assert resumed.groups_resumed == 2


def test_resume_refuses_a_different_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.config import load_config

    src = _corpus(tmp_path / "in", n=4, groups=2)
    ckpt = tmp_path / "priv" / "run.checkpoint"
    runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    with pytest.raises(runner.CheckpointError):
        runner.run_parallel([src], [COMP], jobs=1, cutoff=date(2026, 10, 1), checkpoint=ckpt)
    other = runner.apply_overlay(
        load_config(Path.cwd(), os.environ), {"compression.min_saving_ratio": 0.5}
    )
    with pytest.raises(runner.CheckpointError):  # another effective config
        runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt, cfg=other)
    import token_sparen

    monkeypatch.setattr(token_sparen, "__version__", "9.9.9")
    with pytest.raises(runner.CheckpointError) as err:  # another package version
        runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    assert str(tmp_path) not in str(err.value)


def test_resume_replays_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _corpus(tmp_path / "in", n=9, groups=3)
    fresh = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF)
    ckpt = tmp_path / "priv" / "run.checkpoint"
    real = runner._work_group

    def first_only(task: Any) -> Any:
        if first_only.calls:  # type: ignore[attr-defined]
            raise KeyboardInterrupt
        first_only.calls += 1  # type: ignore[attr-defined]
        return real(task)

    first_only.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(runner, "_work_group", first_only)
    with pytest.raises(KeyboardInterrupt):
        runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    monkeypatch.setattr(runner, "_work_group", real)
    # the live directory moves on: new sessions everywhere, and an existing file grows
    for g in sorted(src.iterdir()):
        regret_session(f"new-{g.name}", "2026-10-03T00:00:00.000Z").write(g / "zz-new.jsonl")
    grown = sorted((src / "g2").glob("s*.jsonl"))[0]
    extra = resend_session("extra", "2026-10-04T00:00:00.000Z").records
    with grown.open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in extra))
    resumed = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF, checkpoint=ckpt)
    assert resumed.groups_resumed == 1 and resumed.complete
    assert resumed.by_split == fresh.by_split and resumed.files == fresh.files


def test_cli_resume_and_lanes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _corpus(tmp_path / "in", n=4, groups=2)
    out = tmp_path / "priv" / "e1.json"
    base = ["replay", str(src), "--out", str(out)]
    assert cli.main([*base, "--lanes", "compression,nonesuch"]) == 2
    assert cli.main([*base, "--jobs", "-1"]) == 2
    assert cli.main([*base, "--resume"]) == 0
    text = capsys.readouterr().out
    assert "[run] files 4" in text and str(tmp_path) not in text


def test_lane_registry_and_merge() -> None:
    assert "compression" in runner.LANES
    a = {"n": 1, "x": 0.5, "flag": False, "rows": {"k": {"v": 2}}}
    b = {"n": 2, "x": 0.25, "flag": True, "rows": {"k": {"v": 1}, "j": {"v": 4}}}
    c = {"n": 4, "rows": {"j": {"v": 1}}}
    ab_c = runner.merge_reports("anything", runner.merge_reports("anything", a, b), c)
    a_bc = runner.merge_reports("anything", a, runner.merge_reports("anything", b, c))
    assert ab_c == a_bc == runner.merge_reports("anything", c, runner.merge_reports("x", b, a))
    assert ab_c == {"n": 7, "x": 0.75, "flag": True, "rows": {"k": {"v": 3}, "j": {"v": 5}}}
    with pytest.raises(ValueError):
        runner.run_parallel([], [("nonesuch", {})], jobs=1)
    with pytest.raises(ValueError):
        runner.run_parallel([], [COMP, COMP], jobs=1)


class _Probe:
    """A plug-in lane: counts sessions and events per split, sees the full history."""

    history = True

    def __init__(self, cfg: Any, params: dict[str, Any]) -> None:
        self.level = cfg.get("compression.default_level")
        self.seed = params.get("seed")
        self.rows: dict[str, dict[str, Any]] = {}
        self.fed = 0

    def feed(self, event: Any, session: runner.SessionInfo) -> None:
        row = self.rows.setdefault(session.split, {"sessions": 0, "events": 0, "levels": []})
        row["events"] += 1

    def end_session(self, session: runner.SessionInfo) -> None:
        row = self.rows.setdefault(session.split, {"sessions": 0, "events": 0, "levels": []})
        row["sessions"] += 1
        row["levels"] = [self.level]
        self.fed += 1

    def report(self) -> dict[str, Any]:
        return {part: {**row, "fed": self.fed} for part, row in self.rows.items()}


def test_plugin_lane_overlay_label_history_and_merger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(runner.LANES, "probe", _Probe)
    src = _corpus(tmp_path / "in", n=12, groups=4)
    specs = [
        ("probe", {"label": "a", "overlay": {"compression.default_level": "light"}, "seed": 1}),
        ("probe", {"label": "b", "overlay": {"compression": {"default_level": "aggressive"}}}),
    ]
    res = runner.run_parallel([src], specs, jobs=1, cutoff=CUTOFF, split="test")
    assert set(res.by_split) == {"test", "all"}
    a, b = res.by_split["test"]["a"], res.by_split["test"]["b"]
    assert a["sessions"] == b["sessions"] == comp_sessions(src, "test")
    assert a["levels"] == ["light"] * 4 and b["levels"] == ["aggressive"] * 4
    assert a["fed"] == 12  # history: the dev sessions were fed too, only test is reported
    monkeypatch.setitem(
        runner.MERGERS, "probe", lambda x, y: {"sessions": max(x["sessions"], y["sessions"])}
    )
    res = runner.run_parallel([src], specs[:1], jobs=1, cutoff=CUTOFF)
    assert res.by_split["all"]["a"] == {
        "sessions": max(res.by_split["dev"]["a"]["sessions"], res.by_split["test"]["a"]["sessions"])
    }


def comp_sessions(src: Path, part: str) -> int:
    res = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF)
    return int(comp(res, part)["sessions"])


def test_apply_overlay_leaves_the_base_untouched() -> None:
    from token_sparen.config import Config

    base = Config(data={"a": {"b": 1, "c": {"d": 2}}})
    out = runner.apply_overlay(base, {"a.b": 5, "a": {"c": {"e": 3}}, "x.y": True})
    assert out.data == {"a": {"b": 5, "c": {"d": 2, "e": 3}}, "x": {"y": True}}
    assert base.data == {"a": {"b": 1, "c": {"d": 2}}}
    assert runner.apply_overlay(base, None) is base


def test_cli_dogfood_start(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    resend_session("late", "2026-12-02T00:00:01.000Z").write(tmp_path / "in" / "p" / "a.jsonl")
    out = tmp_path / "priv" / "e1.json"
    base = ["replay", str(tmp_path / "in"), "--out", str(out)]
    assert cli.main([*base, "--dogfood-start", "nope"]) == 2
    assert cli.main(base) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["run"]["excluded_dogfood"] == 1
    assert cli.main([*base, "--dogfood-start", "2027-01-01"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run"]["excluded_dogfood"] == 0 and data["totals"]["sessions"] == 1


# ------------------------------------------------------------------ fix round 1


def _resume_of(first: Session, new_ts: str) -> Session:
    """A realistic resume: the copied history keeps its original timestamps and ids."""
    resumed = Session(f"{first.sid}-resumed", new_ts)  # a resume gets a new session id
    resumed.records = [dict(r) for r in first.records]
    resumed.n = 1000  # fresh uuids for the new records
    resumed.prompt("continue").call("m30", tool=("t30", "Bash", {"command": "make again"}))
    resumed.result("t30", big_output(seed=30)).call("m31", text="done").call("m32", text="ok")
    return resumed


@pytest.mark.parametrize(
    ("new_ts", "dev", "test", "excluded"),
    [("2026-11-05T10:00:00.000Z", 1, 1, 0), ("2026-12-05T10:00:00.000Z", 1, 0, 1)],
)
def test_resume_is_dated_by_its_own_work(
    tmp_path: Path, new_ts: str, dev: int, test: int, excluded: int
) -> None:
    proj = tmp_path / "proj"
    first = resend_session("a", "2026-10-20T10:00:00.000Z")
    first.write(proj / "z-first.jsonl")
    _resume_of(first, new_ts).write(proj / "a-resume.jsonl")  # sorts first by name
    res = runner.run_parallel([proj], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.excluded_dogfood == excluded
    assert comp(res, "dev")["sessions"] == dev and comp(res, "test")["sessions"] == test
    assert comp(res, "dev")["trimmed"] == 1  # the original's trim, once
    assert comp(res, "test")["trimmed"] == test  # the resume's own new trim
    assert comp(res, "all")["duplicate_results"] == 1 - excluded


def test_marker_in_hook_context_excludes(tmp_path: Path) -> None:
    s = resend_session("hooked", "2026-10-10T09:00:00.000Z")
    s.records.append(
        s._base(
            "attachment",
            attachment={
                "type": "hook_additional_context",
                "content": ["[token-sparen: this error has repeated 3 times]"],
            },
        )
    )
    s.write(tmp_path / "p" / "a.jsonl")
    res = runner.run_parallel([tmp_path], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.excluded_dogfood == 1 and res.sessions == 0


def _subagent(parent: str, marked: bool) -> Session:
    sub = Session(parent, "2026-10-12T09:00:00.000Z")
    sub.records.append(sub._base("user", message={"content": "brief"}, isSidechain=True))
    sub.call("s1", tool=_bash("t1"), sidechain=True)
    text = big_output() + ("\n[token-sparen: kept 1 of 2 lines]" if marked else "")
    sub.result("t1", text, sidechain=True).call("s2", text="x", sidechain=True)
    return sub


@pytest.mark.parametrize("marked", ["main", "subagent"])
@pytest.mark.parametrize("root_is_project", [False, True])
def test_exclusion_spreads_between_session_and_subagents(
    tmp_path: Path, marked: str, root_is_project: bool
) -> None:
    proj = tmp_path / "projects" / "proj"
    main = dogfood_result("sid1", "2026-10-12T09:00:00.000Z") if marked == "main" else None
    main = main or resend_session("sid1", "2026-10-12T09:00:00.000Z")
    main.write(proj / "sid1.jsonl")
    _subagent("sid1", marked == "subagent").write(proj / "sid1" / "subagents" / "agent-a.jsonl")
    resend_session("other", "2026-10-13T09:00:00.000Z").write(proj / "other.jsonl")
    root = proj if root_is_project else tmp_path / "projects"
    assert len(runner.group_files([root])) == 1  # a session and its subagents: one group
    res = runner.run_parallel([root], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.files == 3 and res.excluded_dogfood == 2 and res.sessions == 1


def test_session_failure_does_not_drop_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _corpus(tmp_path / "in", n=4, groups=1)
    real = runner._replay_session
    calls: list[int] = []

    def flaky(*args: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("secret disk trouble")
        real(*args)

    monkeypatch.setattr(runner, "_replay_session", flaky)
    res = runner.run_parallel([src], [COMP], jobs=1, cutoff=CUTOFF)
    assert res.complete and res.sessions == 3 and res.sessions_failed == 1
    assert comp(res)["sessions"] == 3


_KILLER = """
import os
from pathlib import Path

from token_sparen.replay import replay, runner


class Killer(replay.CompressionLane):
    def __init__(self, cfg, params):
        super().__init__(cfg, dict(params, policy="static"))
        self.victim = params["victim"]
        self.flag = params.get("flag")

    def feed(self, event, session):
        if session.group == self.victim:
            if self.flag is None or not Path(self.flag).exists():
                if self.flag is not None:
                    Path(self.flag).write_text("died once")
                os._exit(3)
        super().feed(event, session)


runner.LANES["killer"] = Killer
"""


@pytest.mark.parametrize("once", [False, True])
def test_dead_worker_retried_once_then_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, once: bool
) -> None:
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "killer_lane_mod.py").write_text(_KILLER, encoding="utf-8")
    monkeypatch.syspath_prepend(str(mods))
    src = _corpus(tmp_path / "in", n=8, groups=4)
    victim = sorted(runner.group_files([src]))[0]
    params: dict[str, Any] = {"module": "killer_lane_mod", "victim": victim, "label": "k"}
    if once:
        params["flag"] = str(tmp_path / "flag")
    ckpt = tmp_path / "priv" / "run.checkpoint"
    res = runner.run_parallel([src], [("killer", params)], jobs=2, cutoff=CUTOFF, checkpoint=ckpt)
    assert res.worker_errors.get("BrokenProcessPool", 0) >= 1
    if once:
        assert res.complete and res.sessions == 8
    else:
        assert res.failed_groups == {"BrokenProcessPool": 1} and not res.complete
        assert res.sessions == 6  # every other group was retried and counted
        doc = json.loads(ckpt.read_text(encoding="utf-8"))
        assert victim not in doc["done"] and len(doc["done"]) == 3


def test_cli_incomplete_run_keeps_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "priv" / "e1.json"
    ckpt = out.with_name(out.name + ".checkpoint")

    def partial(*_a: Any, checkpoint: Path | None = None, **_k: Any) -> runner.RunResult:
        assert checkpoint == ckpt
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_text("{}", encoding="utf-8")
        return runner.RunResult(by_split={}, failed_groups={"BrokenProcessPool": 1})

    monkeypatch.setattr(runner, "run_parallel", partial)
    args = ["replay", str(tmp_path), "--out", str(out)]
    assert cli.main([*args, "--quiet"]) == 1
    captured = capsys.readouterr()
    assert re.fullmatch(r"report: .+ id: [0-9a-f]{12}\nstatus: incomplete\n", captured.out)
    assert captured.err == "" and ckpt.exists() and out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["run"]["complete"] is False
    assert cli.main(args) == 1
    assert "incomplete" in capsys.readouterr().err and ckpt.exists()


def test_broken_lane_module_is_not_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken_lane_mod.py").write_text("import no_such_dependency_xyz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        runner.lane_factory("brokenlane", "broken_lane_mod")
    with pytest.raises(ValueError):
        runner.lane_factory("nonesuch")
