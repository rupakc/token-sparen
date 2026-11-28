"""`python -m bench` (M5 Task 12): the dry-run pipeline end to end, resume, status, the refusals of
a real campaign (untagged freeze, token from the environment, Windows) and anchor publication.

No Docker, no ``claude``, no ``swebench`` and no real quota: those subprocesses are patched to
raise, the dry run uses `bench.backends.FakeBackend`, a fake grader, the fake tasks, a scripted
quota in its own account directory and a temporary bare repository as the anchors remote.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bench import anchors, cli
from bench import scheduler as sc
from bench.ledger import Ledger, read_jsonl
from bench.stages import read_plan

SECRET = "sk-ant-oat01-" + "Z" * 40  # a look-alike, never a real credential
_FORBIDDEN = ("docker", "claude", "swebench")


def _guard(real: Any) -> Any:
    def run(argv: Any, *a: Any, **kw: Any) -> Any:
        words = [str(argv)] if isinstance(argv, str) else [str(x) for x in argv]
        head = os.path.basename(words[0]) if words else ""
        if head in _FORBIDDEN or any(w.endswith(_FORBIDDEN) for w in words):
            raise AssertionError(f"the dry run started a forbidden subprocess: {head}")
        return real(argv, *a, **kw)

    return run


@pytest.fixture
def no_real_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """docker / claude / swebench subprocesses raise; the real account dir is a fresh temp path
    that must stay untouched; no token in the environment."""
    monkeypatch.setattr(subprocess, "run", _guard(subprocess.run))
    monkeypatch.setattr(subprocess, "Popen", _guard(subprocess.Popen))
    account = tmp_path_factory.mktemp("acct") / "real-account"
    monkeypatch.setenv("TOKEN_SPAREN_BENCH_STATE", str(account))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv(cli.OUT_ENV, raising=False)
    return account


@pytest.fixture(scope="module")
def dry_ws(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One full dry run shared by the tests of this module (it takes a few seconds)."""
    mp = pytest.MonkeyPatch()
    mp.setattr(subprocess, "run", _guard(subprocess.run))
    mp.setattr(subprocess, "Popen", _guard(subprocess.Popen))
    account = tmp_path_factory.mktemp("acct-m") / "real-account"
    mp.setenv("TOKEN_SPAREN_BENCH_STATE", str(account))
    mp.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    ws = tmp_path_factory.mktemp("bench") / "ws"
    try:
        assert cli.main(["run", "--dry-run", "--campaign", "ci", "--out", str(ws)]) == 0
        assert not account.exists(), "the dry run must never touch the real quota account"
        yield ws
    finally:
        mp.undo()


def _ledger(ws: Path, stage: str) -> Ledger:
    return Ledger(ws / "stages" / stage / "ledger.jsonl")


def test_dry_run_end_to_end_needs_no_quota(
    dry_ws: Path, no_real_tools: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = str(dry_ws)
    for argv in (
        ["grade", "--stage", "screening"],
        ["grade", "--stage", "confirmation"],
        ["analyse", "--stage", "screening"],
        ["analyse", "--stage", "confirmation"],
        ["power"],
        ["gate"],
        ["report", "--stage", "screening"],
        ["report", "--stage", "confirmation"],
    ):
        assert cli.main([*argv, "--out", out]) == 0, argv
    assert not no_real_tools.exists()
    screen = _ledger(dry_ws, "screening")
    confirm = _ledger(dry_ws, "confirmation")
    assert screen.final_runs("screening") and confirm.final_runs("confirmation")
    doc = json.loads((dry_ws / "reports" / "confirmation" / "e4.json").read_text("utf-8"))
    assert doc["stage"] == "confirmation" and doc["gate"]["verdict"] in (
        "passed",
        "failed",
        "underpowered",
    )
    assert doc["power"] is not None and doc["report"]["n_boot"] == cli.DRY_N_BOOT
    assert (dry_ws / "reports" / "screening" / "report.md").is_file()
    # the gate printed the advisory local verdict, and the stored gate used simulated receipts
    gate = json.loads((dry_ws / "stages" / "confirmation" / "gate.json").read_text("utf-8"))
    assert gate["advisory"] is False and gate["receipts"] == "simulated"
    assert "advisory" in capsys.readouterr().out
    # anchors: the plan first, small steps, the last equal to the final heads
    clone = dry_ws / "anchors-git"
    pubs = anchors.read_published(clone, "origin/bench-anchors", "ci")
    assert len(pubs) >= 3
    assert pubs[0].anchors[-1]["runs_started"] == 0 and pubs[0].anchors[-1]["plan_sha256"]
    times = [float(p.at) for p in pubs]
    assert anchors.check_published(confirm, pubs, None) == []
    assert all(b - a <= anchors.MAX_GAP_S for a, b in zip(times, times[1:], strict=False))


def test_dry_run_resume_is_idempotent(dry_ws: Path, no_real_tools: Path) -> None:
    before = {s: len(_ledger(dry_ws, s).records()) for s in cli.STAGES}
    grades = {s: len(read_jsonl(dry_ws / "stages" / s / "grades.jsonl")) for s in cli.STAGES}
    pubs = anchors.read_published(dry_ws / "anchors-git", "origin/bench-anchors", "ci")
    assert cli.main(["run", "--dry-run", "--campaign", "ci", "--out", str(dry_ws)]) == 0
    for s in cli.STAGES:
        runs = [r for r in _ledger(dry_ws, s).records() if r.get("kind") in ("start", "end")]
        assert len(runs) == sum(
            1
            for r in _ledger(dry_ws, s).records()[: before[s]]
            if r.get("kind") in ("start", "end")
        )
        assert len(read_jsonl(dry_ws / "stages" / s / "grades.jsonl")) == grades[s]
    again = anchors.read_published(dry_ws / "anchors-git", "origin/bench-anchors", "ci")
    assert anchors.check_published(_ledger(dry_ws, "confirmation"), again, None) == []
    assert len(again) >= len(pubs)
    # another campaign id in the same workspace is refused
    assert cli.main(["run", "--dry-run", "--campaign", "other", "--out", str(dry_ws)]) == 2


def test_status_prints_no_secret(
    dry_ws: Path,
    no_real_tools: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = tmp_path / "token"
    token.write_text(SECRET + "\n", encoding="utf-8")
    os.chmod(token, 0o600)
    monkeypatch.setenv(cli.TOKEN_FILE_ENV, str(token))
    assert cli.main(["status", "--out", str(dry_ws)]) == 0
    text = capsys.readouterr().out
    assert SECRET not in text and "sk-ant" not in text
    assert "screening" in text and "confirmation" in text
    assert "baseline" in text and "token-sparen" in text
    assert "quota" in text and "breaker" in text


@pytest.mark.skipif(os.name == "nt", reason="real workspaces are refused on Windows")
def test_real_run_refuses_untagged_freeze(
    no_real_tools: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "real"
    assert cli.main(["freeze", "--stage", "screening", "--ref", "HEAD", "--out", str(ws)]) == 2
    assert "tag" in capsys.readouterr().err
    # a stage freezes only its own tag
    rc = cli.main(["freeze", "--stage", "screening", "--ref", "v1.0.0-rc1", "--out", str(ws)])
    assert rc == 2 and "v0.5.0-screen1" in capsys.readouterr().err
    assert not (ws / "frozen" / "screening").exists()
    # and nothing runs without a freeze
    argv = ["run", "--stage", "screening", "--campaign", "m5", "--model", "m", "--out", str(ws)]
    assert cli.main(argv) == 2
    assert "freeze" in capsys.readouterr().err
    assert not (ws / "stages" / "screening" / "ledger.jsonl").exists()


def test_token_comes_from_a_private_file_only(
    no_real_tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    with pytest.raises(cli.CliError, match="file"):
        cli.read_token(None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(cli.CliError, match="token file"):
        cli.read_token(None)
    path = tmp_path / "tok"
    path.write_text(SECRET + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o644)
        with pytest.raises(cli.CliError, match="0600"):
            cli.read_token(path)
        os.chmod(path, 0o600)
    assert cli.read_token(path) == SECRET
    try:
        cli.read_token(path.with_name("missing"))
    except cli.CliError as exc:
        assert SECRET not in str(exc)


def test_real_campaign_refused_on_windows(
    no_real_tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "_HOST_OS", "nt")
    argv = ["run", "--stage", "screening", "--campaign", "m5", "--model", "m"]
    assert cli.main([*argv, "--out", str(tmp_path / "w")]) == 2
    assert not (tmp_path / "w" / "stages").exists()


def test_dry_workspace_cannot_publish_or_run_real(
    dry_ws: Path,
    no_real_tools: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc, "_HOST_OS", "posix")  # the refusal under test, not the platform's
    out = str(dry_ws)
    assert cli.main(["report", "--stage", "confirmation", "--publish", "--out", out]) == 2
    assert "dry run" in capsys.readouterr().err
    argv = ["run", "--stage", "screening", "--campaign", "ci", "--model", "m", "--out", out]
    assert cli.main(argv) == 2
    assert "dry run" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="real workspaces are refused on Windows")
def test_quota_set_records_an_owner_reading(
    no_real_tools: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "q"
    rc = cli.main(["quota", "set", "--weekly", "12", "--five-hour", "30", "--out", str(ws)])
    assert rc == 0
    recs = read_jsonl(no_real_tools / "quota.jsonl")
    kinds = {r["quota_kind"]: r for r in recs}
    assert kinds["weekly"]["utilization_pct"] == 12 and kinds["weekly"]["source"] == "owner"
    assert kinds["five_hour"]["utilization_pct"] == 30
    assert "recorded" in capsys.readouterr().out
    assert cli.main(["quota", "set", "--weekly", "120", "--five-hour", "1", "--out", str(ws)]) == 2


def test_report_refuses_stale_or_advisory_gate(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(dry_ws, copy, symlinks=True)
    gate = copy / "stages" / "confirmation" / "gate.json"
    doc = json.loads(gate.read_text("utf-8"))
    gate.write_text(json.dumps({**doc, "advisory": True}), "utf-8")
    assert cli.main(["report", "--stage", "confirmation", "--out", str(copy)]) == 2
    gate.write_text(json.dumps({**doc, "ledger_chain_head": "0" * 64}), "utf-8")
    assert cli.main(["report", "--stage", "confirmation", "--out", str(copy)]) == 2


def test_out_is_required(no_real_tools: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 2
    assert "--out" in capsys.readouterr().err


def test_ci_runs_the_dry_run_with_least_privilege() -> None:
    ci = (cli.REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("\n  bench-dry-run:\n", 1)[1].split("\n  test:\n", 1)[0]
    assert "permissions:\n      contents: read" in job
    assert "persist-credentials: false" in job
    assert "python -m bench run --dry-run --campaign ci" in job
    for cmd in ("grade", "analyse", "power", "gate", "report"):
        assert f"python -m bench {cmd}" in job
    assert "mypy --strict bench" in ci


def _copy(dry_ws: Path, tmp_path: Path) -> tuple[cli.Workspace, Ledger, cli.VirtualClock]:
    copy = tmp_path / "copy"
    shutil.copytree(dry_ws, copy, symlinks=True)
    remote = str(copy / "dry-remote.git")  # never push to the shared workspace's remote
    env = cli._git_env(isolated=True)
    cli._git(copy / "anchors-git", "remote", "set-url", "origin", remote, env=env)
    ws = cli.Workspace(copy, json.loads((copy / "workspace.json").read_text("utf-8")))
    return ws, _ledger(copy, "confirmation"), cli.VirtualClock(copy / "dry-clock.json")


def test_long_wait_publishes_heartbeats(dry_ws: Path, no_real_tools: Path, tmp_path: Path) -> None:
    """Pause ruling (a): while the scheduler waits, heartbeats go out at least every 4 h."""
    ws, led, clock = _copy(dry_ws, tmp_path)
    pub = cli._publisher(ws, clock)
    before = len(anchors.read_published(pub.clone, "HEAD", "ci"))
    cli.heartbeat_sleep(26 * 3600.0, clock.sleep, lambda: pub.beat(led))
    pubs = anchors.read_published(pub.clone, "HEAD", "ci")
    assert len(pubs) - before >= 8
    times = [p.at for p in pubs[before - 1 :]]
    assert all(b - a <= 4 * 3600 for a, b in zip(times, times[1:], strict=False))
    pub.fetch()
    assert anchors.check_published(led, anchors.read_published(pub.clone, pub.ref, "ci")) == []


def test_scheduler_stopped_for_a_day_then_resumed_without_runs_passes(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path
) -> None:
    """Pause ruling (b): nothing ran while unanchored, so a day-long gap is no anchor problem."""
    ws, led, clock = _copy(dry_ws, tmp_path)
    clock.sleep(24 * 3600.0)  # the scheduler was not running: no heartbeat
    led.append({"kind": "pause", "at": clock(), "reason": "weekly"})
    pub = cli._publisher(ws, clock)
    assert pub.publish(led)
    pub.fetch()
    pubs = anchors.read_published(pub.clone, pub.ref, "ci")
    assert pubs[-1].at - pubs[-2].at > anchors.MAX_GAP_S
    assert anchors.check_published(led, pubs) == []


def _real_copy(dry_ws: Path, tmp_path: Path) -> tuple[cli.Workspace, Ledger, cli.VirtualClock]:
    """A copy of the dry workspace relabelled as real (to reach the real-only checks)."""
    ws, led, clock = _copy(dry_ws, tmp_path)
    ws.meta["dry_run"] = False
    ws.save()
    return ws, led, clock


def test_failed_push_is_retried_one_commit_per_push(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review C1: a push that failed leaves its commit local; the retry pushes every pending
    commit alone and in order, so each gets its own server receipt."""
    ws, led, clock = _copy(dry_ws, tmp_path)
    pub = cli._publisher(ws, clock)
    real_git = cli._git
    pushes: list[str] = []
    state = {"fail": True}

    def git(cwd: Path, *args: str, **kw: Any) -> str:
        if args[:1] == ("push",):
            if state["fail"]:
                raise cli.CliError("git push failed: offline", cli.EXIT_ANCHORS)
            pushes.append(args[-1].split(":")[0])
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(cli, "_git", git)
    clock.sleep(3600.0)
    with pytest.raises(cli.CliError):
        pub.publish(led, heartbeat=True)  # commit made, push failed
    clock.sleep(3600.0)
    with pytest.raises(cli.CliError):
        pub.publish(led, heartbeat=True)  # the first is retried first and fails again
    pending = pub.pending()
    assert len(pending) == 1
    state["fail"] = False
    clock.sleep(3600.0)
    assert pub.publish(led, heartbeat=True)
    new = pending + [pushes[-1]]
    assert pushes == new and len(set(pushes)) == len(pushes)  # one commit per push, in order
    assert pub.pending() == []
    assert (ws.root / "anchors-pushed.json").is_file()
    pubs = anchors.read_published(pub.clone, pub.ref, "ci")
    receipts = [anchors.Receipt(p.commit, float(p.at)) for p in pubs]
    assert anchors.check_published(led, pubs, receipts) == []


def test_plan_only_rerun_pushes_the_unpushed_plan_anchor(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, led, clock = _copy(dry_ws, tmp_path)
    pub = cli._publisher(ws, clock)
    env = cli._git_env(isolated=True)
    # a local commit the remote never got (as after a failed --plan-only push)
    (pub.clone / "anchors").mkdir(exist_ok=True)
    clock.sleep(60.0)
    led.append({"kind": "pause", "at": clock(), "reason": "weekly"})
    anchors.record_anchor(led, at=clock())
    text = "".join(json.dumps(d, sort_keys=True) + "\n" for d in anchors.anchors_to_publish(led))
    (pub.clone / anchors.branch_path("ci")).write_text(text, encoding="utf-8")
    cli._git(pub.clone, "add", "-A", env=env)
    cli._git(pub.clone, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "x", env=env)
    assert len(pub.pending()) == 1
    assert pub.publish(led) is True  # nothing new locally, but the pending commit goes out
    assert pub.pending() == []


def test_harness_must_be_the_pinned_clean_clone(tmp_path: Path) -> None:
    repo = tmp_path / "h"
    repo.mkdir()
    env = cli._git_env(isolated=True)
    cli._git(repo, "init", "-q", env=env)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    cli._git(repo, "add", "f.txt", env=env)
    cli._git(repo, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "c", env=env)
    head = cli._git(repo, "rev-parse", "HEAD", env=env).strip()
    assert cli.harness_problem(repo, repo, head) is None
    assert "prepare harness" in str(cli.harness_problem(repo, repo, None))
    assert "harness clone" in str(cli.harness_problem(cli.REPO, repo, head))
    assert "pinned" in str(cli.harness_problem(repo, repo, "0" * 40))
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    assert "uncommitted" in str(cli.harness_problem(repo, repo, head))


def test_anchors_remote_validation(dry_ws: Path, no_real_tools: Path, tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="credentials"):
        cli.check_remote_url("https://user:secret@github.com/o/r.git")
    cli.check_remote_url("git@github.com:o/r.git")
    cli.check_remote_url("https://github.com/o/r.git")
    assert cli.remote_matches("git@github.com:o/r.git", "o/r")
    assert cli.remote_matches("https://github.com/o/r", "o/r")
    assert not cli.remote_matches("https://github.com/o/r2.git", "o/r")
    ws, _, clock = _real_copy(dry_ws, tmp_path)
    ws.meta["anchors_remote"] = "https://github.com/o/r.git"
    with pytest.raises(cli.CliError, match="differs"):
        cli._publisher(ws, clock, "https://github.com/x/y.git")


@pytest.mark.skipif(os.name == "nt", reason="POSIX links and modes")
def test_token_file_link_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    real = tmp_path / "tok"
    real.write_text(SECRET, encoding="utf-8")
    os.chmod(real, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(cli.CliError, match="link"):
        cli.read_token(link)


def test_windows_refuses_real_workspaces_before_writing(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "_HOST_OS", "nt")
    for argv in (
        ["quota", "set", "--weekly", "1", "--five-hour", "1"],
        ["freeze", "--stage", "screening", "--ref", "v0.5.0-screen1"],
        ["prepare", "toolchain"],
        ["anchors", "publish"],
    ):
        assert cli.main([*argv, "--out", str(tmp_path / "w")]) == 2, argv
    assert not (tmp_path / "w").exists()
    assert cli.main(["status", "--out", str(dry_ws)]) == 0  # a dry workspace is fine


def test_power_refuses_a_pre_screening_calibration(
    dry_ws: Path,
    no_real_tools: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc, "_HOST_OS", "posix")  # a real workspace would be refused on Windows
    ws, _, _ = _real_copy(dry_ws, tmp_path)
    assert cli.main(["power", "--out", str(ws.root)]) == 2
    assert "calibrate" in capsys.readouterr().err


def test_plan_hash_must_be_committed(dry_ws: Path, no_real_tools: Path, tmp_path: Path) -> None:
    ws, led, _ = _real_copy(dry_ws, tmp_path)
    with pytest.raises(cli.CliError, match="evidence-repo"):
        cli._check_plan_file(ws, None)
    main = tmp_path / "main"
    (main / "docs" / "evidence").mkdir(parents=True)
    with pytest.raises(cli.CliError, match="missing"):
        cli._check_plan_file(ws, main)
    plan = read_plan(led)
    (main / anchors.PLAN_FILE).write_text("0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(cli.CliError, match="differs"):
        cli._check_plan_file(ws, main)
    (main / anchors.PLAN_FILE).write_text(plan.sha256 + "\n", encoding="utf-8")
    cli._check_plan_file(ws, main)


def test_real_publish_needs_the_main_checkout(
    dry_ws: Path, no_real_tools: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, _, _ = _real_copy(dry_ws, tmp_path)
    with pytest.raises(cli.CliError, match="--repo"):
        cli.report_step(ws, "screening", publish=True)
