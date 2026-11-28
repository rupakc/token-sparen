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
    dry_ws: Path, no_real_tools: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = str(dry_ws)
    assert cli.main(["report", "--stage", "confirmation", "--publish", "--out", out]) == 2
    assert "dry run" in capsys.readouterr().err
    argv = ["run", "--stage", "screening", "--campaign", "ci", "--model", "m", "--out", out]
    assert cli.main(argv) == 2


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
