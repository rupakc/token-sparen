"""Local grading with the official harness and the oracle preflight (M5 Task 8).

No test here needs Docker, the network or the grader venv: the swebench subprocess is replaced
by a recorder. `test_live_gold_grading` (marker ``docker``) is the optional real check and is
skipped unless ``TOKEN_SPAREN_BENCH_GRADE_VENV`` names a venv with swebench installed.

The ``docker`` marker is registered in pyproject.toml. Do not add a ``tests/bench/conftest.py``:
it would shadow ``import conftest`` (tests/conftest.py) in tests/test_private_state.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bench import grade as gr
from bench.tasks import arm64_image

# synthetic, never a real credential
TOKEN = "sk-ant-oat01-" + "Zz9_" * 12
DJANGO = "django__django-12774"
SPHINX = "sphinx-doc__sphinx-8265"


def _row(iid: str) -> dict[str, Any]:
    image = "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_") + ":latest"
    return {"instance_id": iid, "image": image, "patch": f"diff --git a/{iid} b/{iid}\n"}


# --- recorded subprocess ------------------------------------------------------------------


# What swebench 5.0.2 writes per outcome (checked against the wheel source and a real
# non-applying patch on django__django-12774, fix round 1): the run report's id lists, the
# `failure_reasons` of `infra_failure.classify_logs`, and the instance's log lines.
_OUTCOMES: dict[str, tuple[str, str | None, str, str]] = {
    # kind: (report list, failure_reasons value, run_instance.log, test_output.txt)
    "resolved": ("resolved_ids", None, "", "ok"),
    "unresolved": ("unresolved_ids", None, "", "FAILED"),
    "apply_fail": (
        "error_ids",
        None,
        ">>>>> Patch Apply Failed:\nerror: patch failed: django/__init__.py:1\n",
        "",
    ),
    "timeout": (
        "error_ids",
        "tests_timed_out",
        "Test timed out after 1800 seconds.\n",
        "...\n\nTimeout error: 1800 seconds exceeded.",
    ),
    "oom": ("error_ids", "out_of_memory", "", "Killed\n"),
    "docker": ("error_ids", "container_unavailable", "Error response from daemon: x\n", ""),
    "build": ("error_ids", None, "Error creating container for i: Image x not found for i\n", ""),
    "crash": ("error_ids", None, "Error in evaluating model for i: KeyError\n", ""),
    "empty": ("empty_patch_ids", None, "", ""),
}
_ENVIRONMENT = {"out_of_memory", "container_unavailable"}
_KIND = {True: "resolved", False: "unresolved", None: "build"}


@dataclass
class Recorder:
    """Stands in for `subprocess.run`: writes the report and logs swebench 5 would write.
    ``resolved`` maps an instance id to a verdict (True/False/None = build error) or to an
    outcome kind of `_OUTCOMES`."""

    resolved: Mapping[str, bool | str | None] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), **kw})
        cwd = Path(kw["cwd"])
        run_id = _flag(argv, "--run-id") or _flag(argv, "--run_id")
        preds = Path(_flag(argv, "-p") or _flag(argv, "--predictions_path") or "")
        rows = [json.loads(x) for x in preds.read_text("utf-8").splitlines() if x]
        model = rows[0]["model_name_or_path"]
        report: dict[str, Any] = {k: [] for k in {v[0] for v in _OUTCOMES.values()}}
        report |= {"infra_failure_ids": [], "failure_reasons": {}, "schema_version": 2}
        for row in rows:
            iid = row["instance_id"]
            verdict = self.resolved.get(iid)
            kind = verdict if isinstance(verdict, str) else _KIND[verdict]
            listed, reason, inst_log, test_out = _OUTCOMES[kind]
            report[listed].append(iid)
            if reason:
                report["failure_reasons"][iid] = reason
                if reason in _ENVIRONMENT:
                    report["infra_failure_ids"].append(iid)
            log = cwd / "logs" / "run_evaluation" / str(run_id) / model / iid
            log.mkdir(parents=True)
            (log / "run_instance.log").write_text(inst_log, "utf-8")
            if test_out:
                (log / "test_output.txt").write_text(test_out, "utf-8")
        report_dir = Path(_flag(argv, "--report-dir") or cwd)
        (report_dir / f"{model}.{run_id}.json").write_text(json.dumps(report), "utf-8")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@dataclass
class FakeCapper:
    events: list[tuple[str, str]] = field(default_factory=list)

    def start(self, run_id: str) -> None:
        self.events.append(("start", run_id))

    def stop(self) -> None:
        self.events.append(("stop", ""))


def _flag(argv: Sequence[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _ids(argv: Sequence[str], flag: str = "-i") -> list[str]:
    return [argv[k + 1] for k, a in enumerate(argv) if a == flag]


def _local(tmp_path: Path, mode: gr.GradeMode, rec: Recorder) -> gr.LocalSwebench:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    dataset = tmp_path / "dataset.arm64.jsonl"
    dataset.write_text("", "utf-8")
    return gr.LocalSwebench(
        venv, dataset, tmp_path / "reports", mode, run=rec, workspace=tmp_path, capper=FakeCapper()
    )


def _preds(tmp_path: Path, ids: Sequence[str], model: str = "baseline") -> Path:
    path = tmp_path / "preds.jsonl"
    gr.write_predictions(((i, model, f"diff {i}\n") for i in ids), path, environ={})
    return path


# --- grade mode and commands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "version", "module"),
    [
        ("swebench5-arm64", "5.0.2", False),
        ("swebench-pinned-arm64", "4.1.0", True),
        ("amd64-emulation", "5.0.2", False),
    ],
)
def test_grade_mode_selects_command(
    tmp_path: Path, mode: gr.GradeMode, version: str, module: bool
) -> None:
    mode_file = tmp_path / "grade_mode.json"
    mode_file.write_text(json.dumps({"mode": mode, "swebench_version": version}), "utf-8")
    assert gr.load_grade_mode(mode_file) == (mode, version)

    rec = Recorder({DJANGO: True})
    local = _local(tmp_path, mode, rec)
    assert local.grade(_preds(tmp_path, [DJANGO]), "c-s-a-s1-b1", [DJANGO]) == {DJANGO: True}
    argv = rec.calls[0]["argv"]
    if module:
        assert argv[:3] == [str(tmp_path / "venv" / "bin" / "python"), "-m", gr.V4_MODULE]
        assert _flag(argv, "--dataset_name") == str(tmp_path / "dataset.arm64.jsonl")
        assert _flag(argv, "--namespace") == "swebench"
        assert _flag(argv, "--max_workers") == "1"
        assert argv[argv.index("--instance_ids") + 1] == DJANGO
    else:
        assert argv[:2] == [str(tmp_path / "venv" / "bin" / "swebench"), "eval"]


def test_committed_grade_mode_is_valid() -> None:
    mode, version = gr.load_grade_mode()
    assert mode in gr.GRADE_MODES
    pinned = gr.REQUIREMENTS_FILE.read_text("utf-8").split()
    assert f"swebench=={version}" in pinned


def test_requirements_are_hash_locked() -> None:
    """Every requirement is pinned with ``==`` and carries hashes (pip --require-hashes); the
    lock's sha256 is recorded in grade_mode.json for disclosure."""
    text = gr.REQUIREMENTS_FILE.read_text("utf-8")
    reqs = [ln for ln in text.splitlines() if ln and not ln.startswith((" ", "#"))]
    assert len(reqs) > 10
    assert all("==" in r and r.endswith("\\") for r in reqs)
    assert text.count("--hash=sha256:") >= len(reqs)
    doc = json.loads(gr.GRADE_MODE_FILE.read_text("utf-8"))
    assert doc["requirements_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_grade_mode_refuses_unknown(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"mode": "sb-cli", "swebench_version": "5.0.2"}), "utf-8")
    with pytest.raises(gr.GradeError):
        gr.load_grade_mode(bad)


def test_amd64_mode_sets_platform_and_keeps_x86_images(tmp_path: Path) -> None:
    rows = [_row(DJANGO), _row(SPHINX)]
    out = gr.write_grading_dataset(rows, tmp_path / "ds.jsonl", "amd64-emulation")
    written = [json.loads(x) for x in out.read_text("utf-8").splitlines()]
    assert [r["image"] for r in written] == [r["image"] for r in rows]
    assert all("x86_64" in r["image"] for r in written)

    rec = Recorder({DJANGO: True})
    _local(tmp_path, "amd64-emulation", rec).grade(_preds(tmp_path, [DJANGO]), "r1", [DJANGO])
    assert rec.calls[0]["env"]["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"

    rec5 = Recorder({DJANGO: True})
    _local(tmp_path, "swebench5-arm64", rec5).grade(_preds(tmp_path, [DJANGO]), "r2", [DJANGO])
    assert "DOCKER_DEFAULT_PLATFORM" not in rec5.calls[0]["env"]


def test_arm64_dataset_rewrites_image(tmp_path: Path) -> None:
    rows = [_row(DJANGO), _row(SPHINX)]
    out = gr.write_arm64_dataset(rows, tmp_path / "ds.jsonl")
    written = [json.loads(x) for x in out.read_text("utf-8").splitlines()]
    assert [r["image"] for r in written] == [arm64_image(DJANGO), arm64_image(SPHINX)]
    assert written[0]["patch"] == rows[0]["patch"]  # every other field kept
    assert rows[0]["image"].startswith("swebench/sweb.eval.x86_64.")  # input untouched
    assert os.stat(out).st_mode & 0o077 == 0


def test_swebench_argv_v5(tmp_path: Path) -> None:
    rec = Recorder({DJANGO: True, SPHINX: False})
    local = _local(tmp_path, "swebench5-arm64", rec)
    preds = _preds(tmp_path, [DJANGO, SPHINX])
    assert local.grade(preds, "camp-screening-baseline-s1-b1", [DJANGO, SPHINX]) == {
        DJANGO: True,
        SPHINX: False,
    }
    argv = rec.calls[0]["argv"]
    assert argv[:3] == [
        str(tmp_path / "venv" / "bin" / "swebench"),
        "eval",
        str(tmp_path / "dataset.arm64.jsonl"),
    ]
    assert _flag(argv, "-p") == str(preds)
    assert _flag(argv, "--run-id") == "camp-screening-baseline-s1-b1"
    assert _ids(argv) == [DJANGO, SPHINX]
    assert _flag(argv, "-j") == "1"
    assert _flag(argv, "-t") == "1800"
    assert _flag(argv, "--report-dir") == str(tmp_path / "reports")
    for absent in ("-r", "--namespace", "--task-repo", "--gold", "--modal"):
        assert absent not in argv
    assert rec.calls[0]["cwd"] == str(tmp_path / "reports")
    assert rec.calls[0]["timeout"] > 1800
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in rec.calls[0]["env"]


def test_harness_error_is_none_and_reason_recorded(tmp_path: Path) -> None:
    rec = Recorder({DJANGO: None, SPHINX: True})
    local = _local(tmp_path, "swebench5-arm64", rec)
    got = local.grade(_preds(tmp_path, [DJANGO, SPHINX]), "r", [DJANGO, SPHINX])
    assert got == {DJANGO: None, SPHINX: True}
    assert local.last_reasons == {DJANGO: "build"}


def test_missing_report_or_failed_process_is_none(tmp_path: Path) -> None:
    def broken(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "boom")

    local = gr.LocalSwebench(
        tmp_path / "v",
        tmp_path / "d.jsonl",
        tmp_path / "rep",
        "swebench5-arm64",
        run=broken,
        workspace=tmp_path,
        capper=FakeCapper(),
    )
    assert local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO]) == {DJANGO: None}
    assert local.last_reasons == {DJANGO: "harness_error"}


@pytest.mark.parametrize(
    ("kind", "verdict", "reason"),
    [
        ("apply_fail", False, "apply_fail"),
        ("timeout", False, "timeout"),
        ("unresolved", False, None),
        ("empty", False, "empty_patch"),
    ],
)
def test_apply_fail_is_unresolved(
    tmp_path: Path, kind: str, verdict: bool, reason: str | None
) -> None:
    """The agent's patch failing to apply, or its tests running out of time, is the agent's
    failure: unresolved, never a harness error that would be regraded and dropped."""
    local = _local(tmp_path, "swebench5-arm64", Recorder({DJANGO: kind}))
    assert local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO]) == {DJANGO: verdict}
    assert local.last_reasons == ({DJANGO: reason} if reason else {})


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("oom", "oom"), ("docker", "docker"), ("build", "build"), ("crash", "harness_error")],
)
def test_infra_failure_is_none(tmp_path: Path, kind: str, reason: str) -> None:
    local = _local(tmp_path, "swebench5-arm64", Recorder({DJANGO: kind, SPHINX: True}))
    got = local.grade(_preds(tmp_path, [DJANGO, SPHINX]), "r", [DJANGO, SPHINX])
    assert got == {DJANGO: None, SPHINX: True}
    assert local.last_reasons == {DJANGO: reason}


def test_infra_beats_unresolved_in_the_report(tmp_path: Path) -> None:
    """An environment failure classified by swebench on an unresolved id is still infra."""

    def rec(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        out = Recorder({DJANGO: False})(argv, **kw)
        report = next((tmp_path / "reports").glob("*.json"))
        doc = json.loads(report.read_text("utf-8"))
        doc["infra_failure_ids"] = [DJANGO]
        doc["failure_reasons"] = {DJANGO: "network_unreachable"}
        report.write_text(json.dumps(doc), "utf-8")
        return out

    local = _local(tmp_path, "swebench5-arm64", Recorder())
    local._run = rec
    assert local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO]) == {DJANGO: None}
    assert local.last_reasons == {DJANGO: "network"}


def test_stale_report_never_read(tmp_path: Path) -> None:
    def dies(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 1)

    local = _local(tmp_path, "swebench5-arm64", Recorder())
    (tmp_path / "reports").mkdir()
    stale = tmp_path / "reports" / "baseline.r.json"
    stale.write_text(json.dumps({"resolved_ids": [DJANGO]}), "utf-8")
    local._run = dies
    assert local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO]) == {DJANGO: None}
    assert not stale.exists()


def test_memory_cap_and_private_home_during_grading(tmp_path: Path) -> None:
    capper = FakeCapper()
    seen: list[list[tuple[str, str]]] = []

    def rec(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(capper.events))
        return Recorder({DJANGO: True})(argv, **kw)

    local = gr.LocalSwebench(
        tmp_path / "v",
        tmp_path / "d.jsonl",
        tmp_path / "rep",
        "swebench5-arm64",
        run=rec,
        workspace=tmp_path,
        capper=capper,
        environ={"HOME": "/home/owner", "PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
    )
    local.grade(_preds(tmp_path, [DJANGO]), "camp-x-b1", [DJANGO])
    assert seen == [[("start", "camp-x-b1")]]
    assert capper.events[-1] == ("stop", "")
    env = local.env()
    home = tmp_path / gr.GRADER_HOME
    for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "HF_HOME", "PIP_CACHE_DIR"):
        assert Path(env[key]).is_relative_to(home), key
    assert env["PATH"] == "/usr/bin"
    assert TOKEN not in json.dumps(env)


def test_memory_capper_updates_this_runs_containers() -> None:
    calls: list[list[str]] = []

    def docker(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        out = "sweb.eval.django__django-12774.camp-x-b1\nsweb.eval.other.camp-y-b1\n"
        return subprocess.CompletedProcess(list(argv), 0, out if "ps" in argv else "", "")

    capper = gr.DockerMemoryCapper(memory="4g", run=docker)
    capper.cap_once("camp-x-b1")
    capper.cap_once("camp-x-b1")  # a container is updated once
    updates = [c for c in calls if c[:2] == ["docker", "update"]]
    assert updates == [
        [
            "docker",
            "update",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "sweb.eval.django__django-12774.camp-x-b1",
        ]
    ]


def test_grading_holds_bench_lock(tmp_path: Path) -> None:
    seen: list[bool] = []

    def probe(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        import fcntl

        with (tmp_path / "bench.lock").open("a+b") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                seen.append(True)
            else:
                seen.append(False)
        return Recorder({DJANGO: True})(argv, **kw)

    if os.name != "posix":
        pytest.skip("flock probe is POSIX only")
    local = gr.LocalSwebench(
        tmp_path / "v",
        tmp_path / "d.jsonl",
        tmp_path / "rep",
        "swebench5-arm64",
        run=probe,
        workspace=tmp_path,
        capper=FakeCapper(),
    )
    local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO])
    assert seen == [True]


# --- predictions --------------------------------------------------------------------------


def test_predictions_leak_scanned(tmp_path: Path) -> None:
    path = tmp_path / "preds.jsonl"
    leaky = [(DJANGO, "baseline", f"+token = '{TOKEN}'\n")]
    with pytest.raises(gr.LeakInPredictions) as err:
        gr.write_predictions(leaky, path, environ={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
    assert TOKEN not in str(err.value)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []

    # the pattern alone is caught without the token in the environment
    with pytest.raises(gr.LeakInPredictions):
        gr.write_predictions(leaky, path, environ={})

    gr.write_predictions([(DJANGO, "baseline", "diff\n")], path, environ={})
    row = json.loads(path.read_text("utf-8"))
    assert row == {"instance_id": DJANGO, "model_name_or_path": "baseline", "model_patch": "diff\n"}


# --- grade_task / grade_stage -------------------------------------------------------------


@dataclass
class FakeLedger:
    path: Path
    campaign: str = "camp"
    runs: list[gr.FinalRun] = field(default_factory=list)

    def final_runs(self, stage: str) -> list[gr.FinalRun]:
        return [r for r in self.runs if r.stage == stage]


def _ledger(tmp_path: Path, runs: Sequence[tuple[str, str, int, str]]) -> FakeLedger:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    finals = [
        gr.FinalRun(f"{iid}/{arm}/s{seed}", "screening", arm, seed, iid, patch)
        for iid, arm, seed, patch in runs
    ]
    return FakeLedger(ws / "ledger.jsonl", runs=finals)


def _grades(ledger: FakeLedger) -> list[dict[str, Any]]:
    path = gr.grades_path(ledger)
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x]


def test_empty_patch_unresolved_without_grading(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [(DJANGO, "baseline", 1, ""), (DJANGO, "ts", 1, "  \n")])
    grader = gr.FakeGrader()
    gr.grade_task(ledger, grader, stage="screening", instance_id=DJANGO)
    assert grader.calls == []
    grades = [g for g in _grades(ledger) if g["kind"] == "grade"]
    assert {(g["arm"], g["resolved"], g["graded_by"]) for g in grades} == {
        ("baseline", False, "empty_patch"),
        ("ts", False, "empty_patch"),
    }


def test_run_ids_unique_per_batch(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        [
            (DJANGO, "baseline", 1, "p1"),
            (DJANGO, "ts", 1, "p2"),
            (DJANGO, "ts", 2, "p3"),
            (SPHINX, "baseline", 1, "p4"),
        ],
    )
    grader = gr.FakeGrader(results={DJANGO: None, SPHINX: True})
    gr.grade_task(ledger, grader, stage="screening", instance_id=DJANGO)
    gr.grade_task(ledger, grader, stage="screening", instance_id=SPHINX)
    gr.grade_stage(ledger, grader, stage="screening")  # regrades the harness errors
    run_ids = [c[1] for c in grader.calls]
    assert len(run_ids) == len(set(run_ids))
    assert run_ids[:3] == [
        "camp-screening-baseline-s1-b1",
        "camp-screening-ts-s1-b1",
        "camp-screening-ts-s2-b1",
    ]
    assert "camp-screening-baseline-s1-b2" in run_ids  # SPHINX, second baseline-s1 batch
    # each batch holds one arm and seed (swebench keys predictions by instance id)
    for preds, _, ids in grader.calls:
        models = {json.loads(x)["model_name_or_path"] for x in preds.read_text().splitlines()}
        assert len(models) == 1
        assert len(set(ids)) == len(ids)


def test_regrade_is_idempotent(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        [(DJANGO, "baseline", 1, "p1"), (SPHINX, "baseline", 1, "p2"), (SPHINX, "ts", 1, "")],
    )
    flaky = gr.FakeGrader(results={DJANGO: True, SPHINX: None})
    out = gr.grade_stage(ledger, flaky, stage="screening")
    assert out == gr.grades_path(ledger)
    assert len(flaky.calls) == 1

    fixed = gr.FakeGrader(results={DJANGO: False, SPHINX: True})
    gr.grade_stage(ledger, fixed, stage="screening")
    assert [c[2] for c in fixed.calls] == [[SPHINX]]  # only the harness error is regraded
    before = out.read_bytes()

    gr.grade_stage(ledger, fixed, stage="screening")
    gr.grade_task(ledger, fixed, stage="screening", instance_id=DJANGO)
    assert len(fixed.calls) == 1
    assert out.read_bytes() == before
    assert gr.load_grades(out, stage="screening") == {
        f"{DJANGO}/baseline/s1": True,  # first definite verdict kept
        f"{SPHINX}/baseline/s1": True,
        f"{SPHINX}/ts/s1": False,
    }


def test_changed_patch_is_regraded(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [(DJANGO, "baseline", 1, "p1")])
    grader = gr.FakeGrader(results={DJANGO: True})
    gr.grade_task(ledger, grader, stage="screening", instance_id=DJANGO)
    ledger.runs[0] = gr.FinalRun(f"{DJANGO}/baseline/s1", "screening", "baseline", 1, DJANGO, "p2")
    gr.grade_task(ledger, grader, stage="screening", instance_id=DJANGO)
    assert len(grader.calls) == 2


def test_harness_errors_regraded_up_to_cap(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [(DJANGO, "baseline", 1, "p1")])
    grader = gr.FakeGrader(results={DJANGO: None})
    for _ in range(gr.MAX_GRADE_ATTEMPTS + 2):
        gr.grade_stage(ledger, grader, stage="screening")
    assert len(grader.calls) == gr.MAX_GRADE_ATTEMPTS
    assert gr.load_grades(gr.grades_path(ledger), stage="screening") == {
        f"{DJANGO}/baseline/s1": None
    }
    assert gr.ungradable(gr.grades_path(ledger), stage="screening") == {f"{DJANGO}/baseline/s1"}


def test_grade_task_before_image_removal(tmp_path: Path) -> None:
    """The scheduler calls grade_task after a task's block and only then removes its image:
    grade_task returns only once every final run of the task is graded, and it never removes
    an image itself (the grader needs it; the scheduler owns removal)."""
    events: list[str] = []
    image_present = {arm64_image(DJANGO): True}

    class Watching(gr.FakeGrader):
        def grade(self, preds: Path, run_id: str, ids: Sequence[str]) -> dict[str, bool | None]:
            assert all(image_present[arm64_image(i)] for i in ids)
            events.append("grade:" + run_id)
            return super().grade(preds, run_id, ids)

    ledger = _ledger(tmp_path, [(DJANGO, "baseline", 1, "p1"), (DJANGO, "ts", 1, "p2")])
    gr.grade_task(ledger, Watching(results={DJANGO: True}), stage="screening", instance_id=DJANGO)
    image_present[arm64_image(DJANGO)] = False
    events.append("remove")
    assert events[-1] == "remove" and len(events) == 3
    assert gr.load_grades(gr.grades_path(ledger), stage="screening") == {
        f"{DJANGO}/baseline/s1": True,
        f"{DJANGO}/ts/s1": True,
    }


def test_grades_file_is_private(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [(DJANGO, "baseline", 1, "")])
    gr.grade_task(ledger, gr.FakeGrader(), stage="screening", instance_id=DJANGO)
    if os.name == "posix":
        assert os.stat(gr.grades_path(ledger)).st_mode & 0o077 == 0


# --- preflight ----------------------------------------------------------------------------


class Scripted(gr.FakeGrader):
    def __init__(self, passes: Sequence[Mapping[str, bool | None]], reasons: Mapping[str, str]):
        super().__init__()
        self._passes = list(passes)
        self.last_reasons: dict[str, str] = {}
        self._reasons = dict(reasons)

    def grade(self, preds: Path, run_id: str, ids: Sequence[str]) -> dict[str, bool | None]:
        self.calls.append((preds, run_id, list(ids)))
        got = self._passes.pop(0)
        self.last_reasons = {i: self._reasons[i] for i in ids if got[i] is None}
        return {i: got[i] for i in ids}


def test_preflight_requires_two_gold_passes(tmp_path: Path) -> None:
    rows = [_row(f"r__r-{k}") for k in range(5)]
    ids = [r["instance_id"] for r in rows]
    first = {ids[0]: True, ids[1]: True, ids[2]: False, ids[3]: None, ids[4]: True}
    second = {ids[0]: True, ids[1]: False, ids[2]: False, ids[3]: None, ids[4]: None}
    noop = dict.fromkeys(ids, False)
    grader = Scripted([first, second, noop], {ids[3]: "build", ids[4]: "harness_error"})
    out = gr.preflight(rows, grader, work=tmp_path / "pf")
    assert out == {
        ids[0]: "ok",
        ids[1]: "excluded",
        ids[2]: "excluded",
        ids[3]: "excluded",
        ids[4]: "excluded",
    }
    assert gr.preflight_reasons(tmp_path / "pf") == {
        ids[1]: "flaky_gold",
        ids[2]: "gold_fail",
        ids[3]: "build",
        ids[4]: "harness_error",
    }
    assert len(grader.calls) == 3
    assert len({c[1] for c in grader.calls}) == 3
    gold = [json.loads(x) for x in grader.calls[0][0].read_text().splitlines()]
    assert [g["model_patch"] for g in gold] == [r["patch"] for r in rows]


def test_preflight_excludes_noop_pass(tmp_path: Path) -> None:
    """Gold must resolve and a harmless applying no-op patch must not: a task whose tests pass
    without a fix would be resolved by every arm for free."""
    rows = [_row(DJANGO), _row(SPHINX), _row("r__r-3")]
    ids = [r["instance_id"] for r in rows]
    gold = dict.fromkeys(ids, True)
    noop = {ids[0]: False, ids[1]: True, ids[2]: None}
    grader = Scripted([gold, gold, noop], {ids[2]: "harness_error"})
    out = gr.preflight(rows, grader, work=tmp_path / "pf")
    assert out == {ids[0]: "ok", ids[1]: "excluded", ids[2]: "excluded"}
    assert gr.preflight_reasons(tmp_path / "pf") == {
        ids[1]: "noop_pass",
        ids[2]: "harness_error",
    }
    sent = [json.loads(x) for x in grader.calls[2][0].read_text().splitlines()]
    assert {s["model_patch"] for s in sent} == {gr.NOOP_PATCH}
    assert {s["model_name_or_path"] for s in sent} == {gr.NOOP_MODEL}
    # the no-op patch adds one new file and touches nothing in the repository
    assert gr.NOOP_PATCH.count("new file mode") == 1
    assert "--- /dev/null" in gr.NOOP_PATCH and gr.NOOP_PATCH.strip()


def test_preflight_run_ids_fresh_on_rerun(tmp_path: Path) -> None:
    rows = [_row(DJANGO)]
    grader = gr.FakeGrader(results={DJANGO: True})
    assert gr.preflight(rows, grader, work=tmp_path / "pf") == {DJANGO: "ok"}
    gr.preflight(rows, grader, work=tmp_path / "pf")
    run_ids = [c[1] for c in grader.calls]
    assert len(run_ids) == 6 and len(set(run_ids)) == 6


def test_preflight_attempt_reserved_before_grading(tmp_path: Path) -> None:
    """A crash mid-preflight never lets the next attempt reuse a run id (swebench would skip
    instances that already have a report under it)."""

    class Crashes(gr.FakeGrader):
        def grade(self, preds: Path, run_id: str, ids: Sequence[str]) -> dict[str, bool | None]:
            super().grade(preds, run_id, ids)
            raise RuntimeError("grader died")

    crashing = Crashes(results={DJANGO: True})
    with pytest.raises(RuntimeError):
        gr.preflight([_row(DJANGO)], crashing, work=tmp_path / "pf")
    ok = gr.FakeGrader(results={DJANGO: True})
    assert gr.preflight([_row(DJANGO)], ok, work=tmp_path / "pf") == {DJANGO: "ok"}
    assert not {c[1] for c in crashing.calls} & {c[1] for c in ok.calls}


# --- the optional live check ------------------------------------------------------------


@pytest.mark.docker
def test_live_gold_grading(tmp_path: Path) -> None:  # pragma: no cover - needs Docker
    venv = os.environ.get("TOKEN_SPAREN_BENCH_GRADE_VENV")
    rows_file = os.environ.get("TOKEN_SPAREN_BENCH_GRADE_ROWS")
    if not venv or not rows_file:
        pytest.skip("set TOKEN_SPAREN_BENCH_GRADE_VENV and TOKEN_SPAREN_BENCH_GRADE_ROWS")
    rows = [json.loads(x) for x in Path(rows_file).read_text("utf-8").splitlines() if x]
    mode, _ = gr.load_grade_mode()
    dataset = gr.write_grading_dataset(rows, tmp_path / "ds.jsonl", mode)
    local = gr.LocalSwebench(Path(venv), dataset, tmp_path / "rep", mode, workspace=tmp_path)
    out = gr.preflight(rows, local, work=tmp_path / "pf")
    assert set(out.values()) == {"ok"}
