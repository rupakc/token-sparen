"""E1 offline replay on synthetic transcripts only (never the real home or real sessions)."""

from __future__ import annotations

import json
import os
import random
import stat
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from token_sparen import cli
from token_sparen.replay import replay
from token_sparen.replay.report import OutPathError, check_out_path

MARKERS = ("quokkamarker77", "zebrafinch99x", "okapibravo12")
_WORDS = "alpha beta gamma delta lorem ipsum dolor amet copy move sync file slot node tree".split()
CUTOFF = date(2026, 11, 1)
POSIX = os.name == "posix"
NO_MODES = "POSIX file modes; on Windows the report relies on its directory's inherited ACLs"


def assert_mode(path: Path, mode: int) -> None:
    """File modes exist only on POSIX (see ``replay.report``); elsewhere nothing to check."""
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == mode, oct(path.stat().st_mode)


def ok(code: int, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 0, showing the CLI's stderr (aggregates-only messages) when it is not."""
    assert code == 0, capsys.readouterr().err


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    return home


def big_output(seed: int = 7, at: int = 200, extra: dict[int, str] | None = None) -> str:
    """~4300 estimated tokens of varied lines; the markers sit in the elided middle."""
    rng = random.Random(seed)
    lines = [" ".join(rng.choice(_WORDS) for _ in range(8)) for _ in range(400)]
    lines[at] = f"note {MARKERS[0]} linked with {MARKERS[1]} and {MARKERS[2]}"
    for i, text in (extra or {}).items():
        lines[i] = text
    return "\n".join(lines)


class Session:
    """Builder for a synthetic Claude Code transcript."""

    def __init__(self, sid: str = "sess-1", ts: str = "2026-11-20T10:00:00.000Z") -> None:
        self.sid = sid
        self.ts = ts
        self.records: list[dict[str, Any]] = []
        self.n = 0

    def _base(self, kind: str, **extra: Any) -> dict[str, Any]:
        self.n += 1
        rec = {
            "type": kind,
            "sessionId": self.sid,
            "timestamp": self.ts,
            "uuid": f"u{self.n}",
            "isSidechain": False,
            "cwd": "/nonexistent/project-dir",
        }
        rec.update(extra)
        return rec

    def prompt(self, text: str = "please sync the files") -> Session:
        self.records.append(self._base("user", message={"role": "user", "content": text}))
        return self

    def call(
        self,
        mid: str,
        *,
        text: str | None = None,
        tool: tuple[str, str, Any] | None = None,
        sidechain: bool = False,
    ) -> Session:
        content: list[dict[str, Any]] = []
        if text is not None:
            content.append({"type": "text", "text": text})
        if tool is not None:
            tid = f"{self.sid}:{tool[0]}"  # ids are unique across sessions, as in real transcripts
            content.append({"type": "tool_use", "id": tid, "name": tool[1], "input": tool[2]})
        mid = f"{self.sid}:{mid}"
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
        }
        msg = {
            "id": mid,
            "model": "model-x",
            "role": "assistant",
            "content": content,
            "usage": usage,
        }
        self.records.append(self._base("assistant", message=msg, isSidechain=sidechain))
        return self

    def result(
        self,
        tid: str,
        text: str,
        *,
        raw: Any = None,
        is_error: bool = False,
        sidechain: bool = False,
    ) -> Session:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": f"{self.sid}:{tid}",
            "content": text,
        }
        if is_error:
            block["is_error"] = True
        rec = self._base(
            "user", message={"role": "user", "content": [block]}, isSidechain=sidechain
        )
        rec["toolUseResult"] = (
            raw
            if raw is not None
            else {"stdout": text, "stderr": "", "interrupted": False, "isImage": False}
        )
        self.records.append(rec)
        return self

    def compact(self) -> Session:
        self.records.append(self._base("system", subtype="compact_boundary", content="compacted"))
        self.records.append(
            self._base("user", isCompactSummary=True, message={"role": "user", "content": "sum"})
        )
        return self

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.records), encoding="utf-8")
        return path


def _bash(tid: str) -> tuple[str, str, Any]:
    return (tid, "Bash", {"command": "make sync"})


def resend_session(sid: str = "sess-1", ts: str = "2026-11-20T10:00:00.000Z") -> Session:
    s = Session(sid, ts).prompt()
    s.call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", text="done one").call("m3", text="done two").call("m4", text="done three")
    s.compact().call("m5", text="after compaction").call("m6", text="still after")
    return s


def test_replay_counts_resends(tmp_path: Path) -> None:
    path = resend_session().write(tmp_path / "proj" / "a.jsonl")
    rep = replay.run([path], "static", "all", CUTOFF)
    assert rep.sessions == 1 and rep.tool_results == 1 and rep.eligible == 1 and rep.trimmed == 1
    removed = rep.tokens_removed
    assert removed > 1000
    assert rep.tokens_resent_removed == 3 * removed  # m2..m4; nothing after the compaction
    expected = removed * (1.25 + 2 * 0.1)  # first send writes the cache, re-sends read it
    assert rep.projected_cost_weighted_saved == pytest.approx(expected)
    assert rep.per_ctype["text"]["tokens_removed"] == removed


def test_result_never_sent_saves_nothing(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output()).compact()
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.tokens_resent_removed == 0
    assert rep.projected_cost_weighted_saved == 0


def test_repeated_records_of_one_response_count_once(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", text="a").call("m2", text="b").call("m3", text="c")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.tokens_resent_removed == 2 * rep.tokens_removed


def test_proxy_regret_detects_elided_reference(tmp_path: Path) -> None:
    hit = Session("sess-hit").prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    hit.call("m2", text=f"The run mentions {MARKERS[0]} and {MARKERS[1]}.")
    one = Session("sess-one").prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    one.call("m2", text=f"Only {MARKERS[0]} here.")
    via_input = Session("sess-in").prompt().call("m1", tool=_bash("t1"))
    via_input.result("t1", big_output())
    via_input.call("m2", tool=("t2", "Grep", {"pattern": f"{MARKERS[1]}|{MARKERS[2]}"}))
    paths = [
        hit.write(tmp_path / "h.jsonl"),
        one.write(tmp_path / "o.jsonl"),
        via_input.write(tmp_path / "i.jsonl"),
    ]
    rep = replay.run(paths, "static", "all", CUTOFF)
    assert rep.trimmed == 3
    assert rep.proxy_regrets == 2
    assert rep.proxy_regret_rate == pytest.approx(2 / 3)


def test_proxy_regret_window_expires(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    for i in range(8):
        s.prompt(f"prompt {i}").call(f"mx{i}", text="ok")
    s.call("late", text=f"{MARKERS[0]} {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 0


def test_kept_tokens_are_not_regret(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output(at=0))
    s.call("m2", text=f"{MARKERS[0]} and {MARKERS[1]} are in the kept head")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.proxy_regrets == 0


def test_split_by_date(tmp_path: Path) -> None:
    old = resend_session("old", "2026-10-01T09:00:00.000Z").write(tmp_path / "old.jsonl")
    new = resend_session("new", "2026-11-20T09:00:00.000Z").write(tmp_path / "new.jsonl")
    dev = replay.run([old, new], "static", "dev", CUTOFF)
    test = replay.run([old, new], "static", "test", CUTOFF)
    both = replay.run([old, new], "static", "all", CUTOFF)
    assert (dev.sessions, test.sessions, both.sessions) == (1, 1, 2)
    assert both.by_split["dev"]["sessions"] == 1 and both.by_split["test"]["sessions"] == 1
    assert both.by_split["dev"]["tokens_removed"] == dev.tokens_removed
    assert set(dev.by_split) == {"dev"}


def test_ineligible_results(tmp_path: Path) -> None:
    s = Session().prompt()
    s.call("m1", tool=_bash("t1")).result("t1", "tiny output")
    s.call("m2", tool=_bash("t2")).result("t2", big_output(), is_error=True)
    s.call("m3", tool=("t3", "Write", {"file_path": "x.py", "content": "y"}))
    s.result("t3", big_output(), raw={"type": "create"})
    s.call("m4", tool=("t4", "mcp__x__y", {})).result("t4", big_output(), raw=[{"type": "text"}])
    s.call("m5", text="end")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.tool_results == 4 and rep.eligible == 0 and rep.trimmed == 0


def test_off_arm_trims_nothing(tmp_path: Path) -> None:
    path = resend_session().write(tmp_path / "a.jsonl")
    rep = replay.run([path], "off", "all", CUTOFF)
    assert rep.eligible == 1 and rep.trimmed == 0 and rep.tokens_removed == 0
    light = replay.run([path], "light", "all", CUTOFF)
    aggressive = replay.run([path], "aggressive", "all", CUTOFF)
    assert 0 < light.tokens_removed < aggressive.tokens_removed


def test_unknown_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        replay.run([], "bogus", "all", CUTOFF)


def test_subagent_file_and_inline_sidechain(tmp_path: Path) -> None:
    sub = Session("sub")
    sub.records.append(sub._base("user", message={"content": "brief"}, isSidechain=True))
    sub.call("s1", tool=_bash("t1"), sidechain=True).result("t1", big_output(), sidechain=True)
    sub.call("s2", text="x", sidechain=True).call("s3", text="y", sidechain=True)
    main = Session("main").prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    main.call("side", text="z", sidechain=True)  # an inline sidechain call is not a re-send
    main.call("m2", text="w")
    paths = [sub.write(tmp_path / "sub.jsonl"), main.write(tmp_path / "main.jsonl")]
    reps = [replay.run([p], "static", "all", CUTOFF) for p in paths]
    assert reps[0].tokens_resent_removed == 2 * reps[0].tokens_removed
    assert reps[1].tokens_resent_removed == reps[1].tokens_removed


def test_directories_recurse(tmp_path: Path) -> None:
    resend_session("a").write(tmp_path / "p1" / "a.jsonl")
    resend_session("b").write(tmp_path / "p2" / "deep" / "b.jsonl")
    (tmp_path / "p2" / "notes.txt").write_text("ignored", encoding="utf-8")
    rep = replay.run([tmp_path], "static", "all", CUTOFF)
    assert rep.sessions == 2


def test_garbage_files_are_tolerated(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n" + "[" * 50_000 + "\n{}\n", encoding="utf-8")
    good = resend_session().write(tmp_path / "good.jsonl")
    rep = replay.run([bad, good], "static", "all", CUTOFF)
    assert rep.trimmed == 1


def test_watch_window_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "MAX_WATCHED", 2)
    s = Session().prompt()
    for i in range(4):
        cmd = (f"t{i}", "Bash", {"command": f"make step{i}"})  # distinct: no re-run regret
        s.call(f"m{i}", tool=cmd).result(f"t{i}", big_output(seed=i))
    s.call("end", text=f"{MARKERS[0]} {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 4
    assert rep.proxy_regrets == 2  # the two oldest spans were evicted unregretted
    assert rep.evicted == 2


def test_engine_policy_is_frozen(
    tmp_path: Path, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = resend_session().write(tmp_path / "a.jsonl")
    rep = replay.run([path], "engine", "all", CUTOFF)
    assert rep.eligible == 0 and rep.totals["gated"] == {"ctype_not_allowed": 1}  # gated
    monkeypatch.setenv("TOKEN_SPAREN_COMPRESSION__ALLOW_CTYPES", "text")  # the user widens
    rep = replay.run([path], "engine", "all", CUTOFF)
    assert rep.eligible == 1
    written = [p for p in _home.rglob("*") if p.is_file()] if _home.exists() else []
    assert not [p for p in written if "sessions" in p.parts or p.name == "posteriors.json"]


def test_report_dict_is_aggregate_only(tmp_path: Path) -> None:
    path = resend_session("sess-SECRET-ID").write(tmp_path / "secretdir" / "a.jsonl")
    data = replay.run([path], "static", "all", CUTOFF).to_dict()
    dumped = json.dumps(data)
    for needle in ("SECRET", "secretdir", str(tmp_path), *MARKERS, "make sync", "nonexistent"):
        assert needle not in dumped
    assert data["totals"]["sessions"] == 1 and "per_ctype" in data["totals"]


# ------------------------------------------------------------------ CLI and out-path rules


def _git_init(where: Path) -> None:
    where.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(where)], check=True)


def test_report_refuses_repo_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = resend_session().write(tmp_path / "a.jsonl")
    target = cli.PACKAGE_ROOT / "e1-should-not-exist.json"
    code = cli.main(["replay", str(path), "--policy", "static", "--out", str(target)])
    assert code != 0 and not target.exists()
    assert "outside" in capsys.readouterr().err
    with pytest.raises(OutPathError):
        check_out_path(cli.PACKAGE_ROOT / "reports" / "x.json")


def test_report_refuses_cwd_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "work"
    _git_init(repo)
    monkeypatch.chdir(repo)
    with pytest.raises(OutPathError):
        check_out_path(repo / "sub" / "r.json")


def test_report_refuses_any_git_work_tree(tmp_path: Path) -> None:
    other = tmp_path / "other"
    _git_init(other)
    with pytest.raises(OutPathError):
        check_out_path(other / "deeper" / "r.json")
    nogit = tmp_path / "plain"
    (nogit / ".git").mkdir(parents=True)  # a .git marker without git available still refuses
    with pytest.raises(OutPathError):
        check_out_path(nogit / "r.json")


def test_report_written_0600_with_aggregates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = resend_session().write(tmp_path / "in" / "a.jsonl")
    out = tmp_path / "private" / "reports" / "e1.json"
    code = cli.main(["replay", str(tmp_path / "in"), "--policy", "static", "--out", str(out)])
    ok(code, capsys)
    assert_mode(out, 0o600)
    assert_mode(out.parent, 0o700)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["totals"]["trimmed"] == 1 and data["policy"] == "static"
    assert path.exists()


def test_stdout_has_no_paths_or_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = resend_session("sess-SECRET-ID")
    s.call("m9", text=f"{MARKERS[0]} {MARKERS[1]}")
    path = s.write(tmp_path / "secretproject" / "sess-SECRET-ID.jsonl")
    out = tmp_path / "outdirxyz" / "report.json"
    ok(cli.main(["replay", str(path), "--policy", "static", "--out", str(out)]), capsys)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "tool_results" in captured.out
    for needle in ("SECRET", "secretproject", str(tmp_path), "outdirxyz", *MARKERS, "make sync"):
        assert needle not in text


def test_replay_requires_explicit_paths_and_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["replay", "--policy", "static", "--out", str(tmp_path / "r.json")]) != 0
    assert cli.main(["replay", str(tmp_path), "--policy", "static"]) != 0
    assert not (tmp_path / "r.json").exists()


def test_replay_errors_never_leak_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError(f"cannot read {tmp_path}/secret-session.jsonl")

    monkeypatch.setattr(replay, "run", boom)
    code = cli.main(["replay", str(tmp_path), "--policy", "static", "--out", str(tmp_path / "x")])
    assert code != 0
    err = capsys.readouterr().err
    assert "secret-session" not in err and str(tmp_path) not in err


@pytest.mark.skipif(not POSIX, reason=NO_MODES)
def test_existing_report_keeps_0600(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = resend_session().write(tmp_path / "in" / "a.jsonl")
    out = tmp_path / "rep.json"
    out.write_text("{}", encoding="utf-8")
    os.chmod(out, 0o644)
    ok(cli.main(["replay", str(path), "--policy", "light", "--out", str(out)]), capsys)
    assert_mode(out, 0o600)


def test_error_log_is_redacted_during_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _home: Path
) -> None:
    from token_sparen import config
    from token_sparen.compress import detect as real_detect

    def leaky_detect(text: str, tool: Any = None) -> Any:
        config.log_error(os.environ, "compress.detect", f"failed on {MARKERS[0]}")
        return real_detect(text, tool)

    monkeypatch.setattr(replay, "detect", leaky_detect)
    replay.run([resend_session().write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    log = (_home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "compress.detect" in log and MARKERS[0] not in log
    config.log_error(os.environ, "after", "visible again")
    assert "visible again" in (_home / "logs" / "errors.jsonl").read_text(encoding="utf-8")


def test_failing_session_counted_without_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret content")

    monkeypatch.setattr(replay._Session, "on_result", boom)
    rep = replay.run([resend_session().write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.sessions_failed == 1 and rep.sessions == 0
    assert "secret" not in json.dumps(rep.to_dict())


def test_summary_lines_are_aggregates(tmp_path: Path) -> None:
    rep = replay.run([resend_session().write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    lines = replay.summary_lines(rep)
    assert any("tokens_removed" in ln for ln in lines) and any("[test]" in ln for ln in lines)
    assert all(str(tmp_path) not in ln for ln in lines)


def test_cli_rejects_bad_cutoff_and_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = str(tmp_path / "r.json")
    assert cli.main(["replay", str(tmp_path), "--cutoff", "nope", "--out", out]) == 2
    assert cli.main(["replay", str(tmp_path), "--policy", "wild", "--out", out]) == 2
    assert not Path(out).exists()


def test_edit_inputs_count_when_only_in_span(tmp_path: Path) -> None:
    """Read -> Edit: an edit quoting text the agent could only have seen in the elided part."""
    s = Session().prompt().call("m1", tool=("t1", "Read", {"file_path": "/w/notes.txt"}))
    s.result("t1", big_output(), raw={"type": "text", "file": {"content": big_output()}})
    edit = {"file_path": "/w/notes.txt", "old_string": f"{MARKERS[0]} {MARKERS[1]}"}
    s.call("m2", tool=("t2", "Edit", edit))
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 1


def test_tokens_seen_before_are_not_regret(tmp_path: Path) -> None:
    s = Session().prompt().call("m0", tool=("t0", "Grep", {"pattern": "note"}))
    s.result("t0", f"y.txt: {MARKERS[0]} {MARKERS[1]}", raw="r")  # seen before the trim
    s.call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", text=f"{MARKERS[0]} {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 0


def test_tokens_seen_again_later_are_not_regret(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", tool=("t2", "Grep", {"pattern": "note"}))
    s.result("t2", f"x.txt: {MARKERS[0]} {MARKERS[1]}", raw="r")  # small: shown in full
    s.call("m3", text=f"{MARKERS[0]} {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 0


def test_user_prompt_with_elided_tokens_is_partial(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.prompt(f"what about {MARKERS[0]} and {MARKERS[2]}?").call("m2", text="ok")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.proxy_regrets == pytest.approx(0.5)
    assert rep.totals["proxy_regret_partial"] == 1 and rep.totals["proxy_regret_full"] == 0


def test_partial_then_full_regret_counts_once(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.prompt(f"what about {MARKERS[0]} and {MARKERS[2]}?")  # partial; span stays watched
    s.call("m2", tool=_bash("t2"))  # then a re-run: full
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.proxy_regrets == 1 and rep.totals["proxy_regret_full"] == 1


def test_rerun_of_same_command_is_regret(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", tool=("t2", "Bash", {"command": "make   sync 2>&1"}))  # same normalised
    other = Session("o").prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    other.call("m2", tool=("t2", "Bash", {"command": "make check"}))
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.proxy_regrets == 1
    rep2 = replay.run([other.write(tmp_path / "b.jsonl")], "static", "all", CUTOFF)
    assert rep2.proxy_regrets == 0


def test_rerun_regrets_a_trim_without_distinctive_tokens(tmp_path: Path) -> None:
    """Every trim is watched for a re-run (as the live detector tracks every trim), even when
    its elided span holds fewer than 2 distinctive tokens (it is then not a watched span)."""
    flat = "\n".join(f"line {i % 7} alpha beta gamma delta lorem ipsum" for i in range(900))
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", flat)
    s.call("m2", tool=_bash("t2"))
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.watched_spans == 0
    assert rep.proxy_regrets == 1 and rep.regret_signals == {"rerun": 1}


def test_tokens_sampled_after_removing_seen(tmp_path: Path) -> None:
    """100 elided tokens the agent saw before crowd the 64-token sample unless removed first."""
    seen = [f"seentoken{i:03d}" for i in range(100)]
    extra = {150 + i: f"row {seen[2 * i]} {seen[2 * i + 1]}" for i in range(50)}
    extra[260] = f"tail {MARKERS[0]} {MARKERS[1]}"
    text = big_output(extra={**extra, 200: "plain middle line"})
    s = Session().prompt().call("m0", tool=("t0", "Grep", {"pattern": "x"}))
    s.result("t0", " ".join(seen), raw="r")
    s.call("m1", tool=_bash("t1")).result("t1", text)
    s.call("m2", text=f"{MARKERS[0]} {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 1


def test_common_words_are_not_distinctive(tmp_path: Path) -> None:
    text = big_output(extra={200: "configuration successfully initialized everything"})
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", text)
    s.call("m2", text="The configuration was successfully initialized.")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.trimmed == 1 and rep.proxy_regrets == 0


def test_path_and_basename_match_as_one_token(tmp_path: Path) -> None:
    text = big_output(extra={200: f"see src/deep/quokkafile_x.py near {MARKERS[1]}"})
    hit = Session().prompt().call("m1", tool=_bash("t1")).result("t1", text)
    hit.call("m2", text=f"open quokkafile_x.py for {MARKERS[1]}")
    one = Session("o").prompt().call("m1", tool=_bash("t1")).result("t1", text)
    one.call("m2", text="src/deep/quokkafile_x.py and quokkafile_x.py")  # one token, twice
    rep = replay.run([hit.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.proxy_regrets == 1
    rep2 = replay.run([one.write(tmp_path / "b.jsonl")], "static", "all", CUTOFF)
    assert rep2.proxy_regrets == 0


def test_copied_history_counted_once_across_files(tmp_path: Path) -> None:
    first = resend_session("a")
    resumed = Session("a")  # a resumed session copies the earlier records (same ids)
    resumed.records = [dict(r) for r in first.records]
    resumed.call("m7", text="new work")
    paths = [first.write(tmp_path / "a.jsonl"), resumed.write(tmp_path / "b.jsonl")]
    one = replay.run(paths[:1], "static", "all", CUTOFF)
    both = replay.run(paths, "static", "all", CUTOFF)
    assert both.calls == one.calls + 1
    assert both.trimmed == 1 and both.tokens_removed == one.tokens_removed
    assert both.tokens_resent_removed == one.tokens_resent_removed
    assert both.totals["duplicate_results"] == 1


@pytest.mark.skipif(not POSIX, reason=NO_MODES)
def test_new_report_dirs_are_all_0700(tmp_path: Path) -> None:
    from token_sparen.replay.report import write_private

    out = write_private(tmp_path / "x" / "y" / "z" / "r.json", {"a": 1})
    for d in (tmp_path / "x", tmp_path / "x" / "y", tmp_path / "x" / "y" / "z"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_report_states_units_and_caveats(tmp_path: Path) -> None:
    data = replay.run([resend_session().write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    text = json.dumps(data.to_dict()["heuristics"])
    assert "estimate" in text and "TTL" in text and "API usage" in text


def test_run_id_cap_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "MAX_RUN_IDS", 2)
    rep = replay.run([resend_session().write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.totals["ids_capped"] is True and rep.trimmed == 1


# ------------------------------------------------------------------ breakdowns (round 2)


def _read(tid: str, path: str = "/w/notes.txt") -> tuple[str, str, Any]:
    return (tid, "Read", {"file_path": path})


def _read_raw(text: str) -> dict[str, Any]:
    return {"type": "text", "file": {"content": text}}


def test_per_tool_breakdown(tmp_path: Path) -> None:
    s = Session().prompt()
    s.call("m1", tool=_read("t1")).result("t1", big_output(seed=1), raw=_read_raw("x"))
    s.call("m2", tool=_bash("t2")).result("t2", big_output(seed=2))
    s.call("m3", text="done")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    tools = rep.totals["per_tool"]
    assert set(tools) == {"read/text", "shell/text"}
    assert tools["read/text"]["trimmed"] == 1 and tools["shell/text"]["trimmed"] == 1
    saved = sum(row["projected_cost_weighted_saved"] for row in tools.values())
    assert saved == pytest.approx(rep.projected_cost_weighted_saved)
    assert sum(row["tokens_removed"] for row in tools.values()) == rep.tokens_removed


def test_regret_signals_and_turns(tmp_path: Path) -> None:
    def trimmed(sid: str) -> Session:
        return Session(sid).prompt().call("m1", tool=_bash("t1")).result("t1", big_output())

    said = trimmed("said").call("m2", text=f"{MARKERS[0]} {MARKERS[1]}")
    tool_in = trimmed("tin").call(
        "m2", tool=("t2", "Grep", {"pattern": f"{MARKERS[0]} {MARKERS[2]}"})
    )
    edit = trimmed("edit").call(
        "m2", tool=("t2", "Edit", {"file_path": "a.py", "old_string": f"{MARKERS[1]} {MARKERS[2]}"})
    )
    rerun = trimmed("rerun").call("m2", tool=_bash("t2"))
    asked = trimmed("asked").prompt(f"{MARKERS[0]} and {MARKERS[1]}?").call("m2", text="ok")
    later = trimmed("later")
    for i in range(3):
        later.prompt(f"next {i}").call(f"x{i}", text="ok")
    later.call("m9", text=f"{MARKERS[0]} {MARKERS[1]}")
    sessions = [said, tool_in, edit, rerun, asked, later]
    paths = [x.write(tmp_path / f"{x.sid}.jsonl") for x in sessions]
    rep = replay.run(paths, "static", "all", CUTOFF)
    assert rep.totals["regret_signals"] == {
        "assistant_text": 2,
        "tool_input": 1,
        "edit_input": 1,
        "rerun": 1,
        "user_prompt": 1,
    }
    assert rep.totals["regret_turns"] == {"0": 4, "1": 1, "2": 0, "3-6": 1, "7+": 0}


def test_path_mention_signal(tmp_path: Path) -> None:
    text = big_output(extra={200: f"see src/deep/quokkafile_x.py near {MARKERS[1]}"})
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", text)
    s.call("m2", text=f"open quokkafile_x.py for {MARKERS[1]}")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.totals["regret_signals"] == {"assistant_text": 1, "path_mention": 1}


def test_multi_arm_single_pass_matches_separate_runs(tmp_path: Path) -> None:
    s = resend_session().call("m9", text=f"{MARKERS[0]} {MARKERS[1]}")
    path = s.write(tmp_path / "a.jsonl")
    multi = replay.run([path], "light,standard,aggressive", "all", CUTOFF)
    assert multi.policy == "light,standard,aggressive"
    assert set(multi.by_arm) == {"light", "standard", "aggressive"}
    for arm in ("light", "standard", "aggressive"):
        alone = replay.run([path], arm, "all", CUTOFF)
        got = multi.by_arm[arm]["totals"]
        for key in ("calls", "trimmed", "tokens_removed", "tokens_resent_removed", "proxy_regrets"):
            assert got[key] == alone.totals[key], (arm, key)
        assert multi.by_arm[arm]["by_split"]["test"]["trimmed"] == alone.by_split["test"]["trimmed"]
    assert multi.totals == multi.by_arm["light"]["totals"]
    removed = [
        multi.by_arm[a]["totals"]["tokens_removed"] for a in ("light", "standard", "aggressive")
    ]
    assert removed == sorted(removed) and removed[0] < removed[2]
    assert "by_arm" in multi.to_dict()
    assert "by_arm" not in replay.run([path], "light", "all", CUTOFF).to_dict()


def test_multi_arm_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = resend_session().write(tmp_path / "in" / "a.jsonl")
    out = tmp_path / "rep.json"
    args = ["replay", str(path), "--out", str(out), "--policy"]
    ok(cli.main([*args, "light,standard,aggressive"]), capsys)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["by_arm"]) == {"light", "standard", "aggressive"}
    assert "[arm aggressive]" in capsys.readouterr().out
    assert cli.main([*args, "light,light"]) == 2
    assert cli.main([*args, "light,bogus"]) == 2


# ------------------------------------------------------------------ families and net (round 3)


@pytest.mark.parametrize(
    ("cmd", "family"),
    [
        ("pytest -q tests/", "test"),
        ("cd pkg && python -m pytest -x", "test"),
        ("uv run pytest", "test"),
        ("npx jest --ci", "test"),
        ("go test ./...", "test"),
        ("cargo test", "test"),
        ("npm test", "test"),
        ("pnpm run test:unit", "test"),
        ("make test", "test"),
        ("mvn test", "test"),
        ("tsc --noEmit", "build"),
        ("cargo build --release", "build"),
        ("make", "build"),
        ("gradle assemble", "build"),
        ("npm run build", "build"),
        ("FOO=1 mypy token_sparen", "build"),
        ("pip install -e .", "install"),
        ("uv pip install foo", "install"),
        ("yarn add left-pad", "install"),
        ("sudo apt-get install -y jq", "install"),
        ("brew install rg", "install"),
        ("ls -la", "listing"),
        ("find . -name '*.py' | head", "listing"),
        ("rg TODO src", "listing"),
        ("tree -L 2", "listing"),
        ("cat a.txt", "file_dump"),
        ("sed -n '1,80p' x.py", "file_dump"),
        ("tail -n 50 log.txt", "file_dump"),
        ("git log --oneline -20", "vcs_read"),
        ("git -C repo diff HEAD~1", "vcs_read"),
        ("git status", "vcs_read"),
        ("git commit -m x", "other"),
        ("docker compose logs api", "container_cloud"),
        ("kubectl get pods", "container_cloud"),
        ("gcloud run services list", "container_cloud"),
        ("curl -s https://example.org", "http"),
        ("echo hi", "other"),
        ("", "other"),
        ("'unbalanced", "other"),
    ],
)
def test_command_family(cmd: str, family: str) -> None:
    assert replay.command_family(cmd) == family


def test_net_savings_after_recovery(tmp_path: Path) -> None:
    """The regret fires in m2: one recovery read of the full output (tokens_before) is sent
    by m3 (cache write) and m4 (cache read); after the compaction nothing more."""
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", text=f"{MARKERS[0]} {MARKERS[1]}").call("m3", text="a").call("m4", text="b")
    s.compact().call("m5", text="c")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    t = rep.totals
    before = t["tokens_before_eligible"]
    removed = t["tokens_removed"]
    assert t["proxy_regret_full"] == 1
    saved = removed * (1.25 + 2 * 0.1)
    recovery = before * (1.25 + 0.1)
    assert t["projected_cost_weighted_saved"] == pytest.approx(saved)
    assert t["recovery_cost_weighted"] == pytest.approx(recovery)
    assert t["net_cost_weighted_saved"] == pytest.approx(saved - recovery)
    share = (saved - recovery) / t["baseline_context_cost"]
    assert t["net_saved_share_of_context_cost"] == pytest.approx(share, abs=1e-6)
    fam = t["per_family"]["build"]
    assert fam["trimmed"] == 1 and fam["net_cost_weighted_saved"] == pytest.approx(saved - recovery)


def test_partial_regret_charges_half_a_recovery(tmp_path: Path) -> None:
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.prompt(f"what about {MARKERS[0]} and {MARKERS[2]}?").call("m2", text="a")
    s.call("m3", text="b")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    before = rep.totals["tokens_before_eligible"]
    assert rep.totals["recovery_cost_weighted"] == pytest.approx(0.5 * before * (1.25 + 0.1))


def test_no_regret_no_recovery_and_families_only_for_shell(tmp_path: Path) -> None:
    s = Session().prompt()
    s.call("m1", tool=_read("t1")).result("t1", big_output(seed=1), raw=_read_raw("x"))
    s.call("m2", tool=("t2", "Bash", {"command": "pytest -q"})).result("t2", big_output(seed=2))
    s.call("m3", text="done")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    assert rep.totals["recovery_cost_weighted"] == 0
    assert set(rep.totals["per_family"]) == {"test"}
    assert rep.totals["net_cost_weighted_saved"] == rep.totals["projected_cost_weighted_saved"]


# ------------------------------------------------------------------ portability (CI round)


def test_write_private_without_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows before 3.13 has no os.fchmod: the report must still be written."""
    from token_sparen.replay import report

    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.setattr(report, "POSIX", False)
    out = report.write_private(tmp_path / "w" / "r.json", {"a": 1})
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in out.parent.iterdir()] == ["r.json"]


def test_failed_write_leaves_nothing_and_closes_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from token_sparen.replay import report

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(report.json, "dump", boom)
    with pytest.raises(OSError):
        report.write_private(tmp_path / "w" / "r.json", {"a": 1})
    assert list((tmp_path / "w").iterdir()) == []  # temp file removed (handle closed first)


# ------------------------------------------------------------------ quality-first gating (24b)


def _gating_session() -> tuple[Session, list[tuple[str, str, Any, str, Any]]]:
    """One result per gating case: (tool id, tool name, input, output, raw)."""
    from sampleoutputs import big_diff, big_pytest, plain_text, python_source

    src = python_source()
    cases: list[tuple[str, str, Any, str, Any]] = [
        ("t1", "Read", {"file_path": "/w/big.py"}, src, _read_raw(src)),
        ("t2", "Bash", {"command": "cat big.py"}, src, None),
        ("t3", "Bash", {"command": "pytest -v"}, big_pytest(), None),
        ("t4", "Bash", {"command": "./tool --dump"}, plain_text(), None),
        ("t5", "Bash", {"command": "git diff"}, big_diff(), None),
    ]
    s = Session().prompt()
    for i, (tid, name, inp, text, raw) in enumerate(cases):
        s.call(f"m{i}", tool=(tid, name, inp)).result(tid, text, raw=raw)
    s.call("m9", text="done")
    return s, cases


def test_default_policy_matches_hook_gating(tmp_path: Path, _home: Path) -> None:
    from token_sparen.adapters.claude import translate
    from token_sparen.config import load_config
    from token_sparen.events import Event, ToolCall
    from token_sparen.learn.policy import StaticPolicy
    from token_sparen.mechanisms import compression

    s, cases = _gating_session()
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "default", "all", CUTOFF)
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = load_config(proj, os.environ)
    hook_trims = 0
    for tid, name, inp, text, _raw in cases:
        ev = Event(
            "claude",
            "post_tool",
            "s",
            proj,
            "0123456789abcdef",
            None,
            ToolCall(name, translate.tool_category(name), inp, tid),
            text,
            text,
        )
        act = compression.handle_post_tool(ev, StaticPolicy({}), cfg)
        hook_trims += act.replace_output is not None
    assert rep.trimmed == hook_trims == 2  # pytest and git diff
    assert rep.eligible == 2
    assert rep.totals["gated"] == {"file_content": 2, "ctype_not_allowed": 1}
    assert set(rep.per_ctype) == {"test_report", "diff"}


def test_static_and_arm_lanes_stay_ungated(tmp_path: Path) -> None:
    s, _cases = _gating_session()
    path = s.write(tmp_path / "a.jsonl")
    rep = replay.run([path], "default,standard", "all", CUTOFF)
    assert rep.by_arm["default"]["totals"]["trimmed"] == 2
    standard = rep.by_arm["standard"]["totals"]
    assert standard["trimmed"] > 2 and standard["gated"] == {}
    assert replay.run([path], "static", "all", CUTOFF).trimmed == standard["trimmed"]


def test_default_policy_named_in_heuristics(tmp_path: Path) -> None:
    rep = replay.run([resend_session().write(tmp_path / "a.jsonl")], "default", "all", CUTOFF)
    assert "gating" in rep.to_dict()["heuristics"]
    assert rep.trimmed == 0 and rep.totals["gated"] == {"ctype_not_allowed": 1}


def test_rerun_regret_charges_no_recovery(tmp_path: Path) -> None:
    """A re-run's tokens are already in the baseline: the regret counts, no recovery cost."""
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output())
    s.call("m2", tool=("t2", "Bash", {"command": "make sync"})).call("m3", text="a")
    s.call("m4", text="b")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    t = rep.totals
    assert t["proxy_regret_full"] == 1 and t["regret_signals"] == {"rerun": 1}
    assert t["recovery_cost_weighted"] == 0
    assert t["recovery_by_signal"] == {}
    assert t["net_cost_weighted_saved"] == t["projected_cost_weighted_saved"]


def test_recovery_cost_split_by_signal(tmp_path: Path) -> None:
    extra = {201: "aardvarkone11 bisonbeta22 lemmingtail33"}
    s = Session().prompt().call("m1", tool=_bash("t1")).result("t1", big_output(extra=extra))
    s.prompt(f"what about {MARKERS[0]} and {MARKERS[2]}?").call("m2", text="a")
    s.call("m3", text="aardvarkone11 bisonbeta22").call("m4", text="b").call("m5", text="c")
    rep = replay.run([s.write(tmp_path / "a.jsonl")], "static", "all", CUTOFF)
    t = rep.totals
    before = t["tokens_before_eligible"]
    split = t["recovery_by_signal"]
    assert set(split) == {"user_prompt", "assistant_text"}
    assert split["user_prompt"]["recovery_tokens_resent"] == 2 * before  # m2..m5
    assert split["assistant_text"]["recovery_tokens_resent"] == before  # m4, m5
    total = sum(row["recovery_cost_weighted"] for row in split.values())
    assert total == pytest.approx(t["recovery_cost_weighted"], abs=1e-3)
