"""Local grading with the official harness and the oracle preflight (M5 Task 8).

No test here needs Docker, the network or the grader venv: the swebench subprocess is replaced
by a recorder. `test_live_gold_grading` (marker ``docker``) is the optional real check and is
skipped unless ``TOKEN_SPAREN_BENCH_GRADE_VENV`` names a venv with swebench installed.
"""

from __future__ import annotations

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


@dataclass
class Recorder:
    """Stands in for `subprocess.run`: writes the report swebench would write."""

    resolved: Mapping[str, bool | None] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), **kw})
        cwd = Path(kw["cwd"])
        run_id = _flag(argv, "--run-id") or _flag(argv, "--run_id")
        preds = Path(_flag(argv, "-p") or _flag(argv, "--predictions_path") or "")
        rows = [json.loads(x) for x in preds.read_text("utf-8").splitlines() if x]
        model = rows[0]["model_name_or_path"]
        ids = [r["instance_id"] for r in rows]
        report = {
            "resolved_ids": [i for i in ids if self.resolved.get(i) is True],
            "unresolved_ids": [i for i in ids if self.resolved.get(i) is False],
            "error_ids": [i for i in ids if self.resolved.get(i) is None],
            "infra_failure_ids": [],
            "empty_patch_ids": [],
            "failure_reasons": {},
            "schema_version": 2,
        }
        report_dir = Path(_flag(argv, "--report-dir") or cwd)
        (report_dir / f"{model}.{run_id}.json").write_text(json.dumps(report), "utf-8")
        for iid in report["error_ids"]:
            log = cwd / "logs" / "run_evaluation" / str(run_id) / model / iid
            log.mkdir(parents=True)
            (log / "run_instance.log").write_text(
                f"Error creating container for {iid}: Image x not found for {iid}\n", "utf-8"
            )
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def _flag(argv: Sequence[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _ids(argv: Sequence[str], flag: str = "-i") -> list[str]:
    return [argv[k + 1] for k, a in enumerate(argv) if a == flag]


def _local(tmp_path: Path, mode: gr.GradeMode, rec: Recorder) -> gr.LocalSwebench:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    dataset = tmp_path / "dataset.arm64.jsonl"
    dataset.write_text("", "utf-8")
    return gr.LocalSwebench(venv, dataset, tmp_path / "reports", mode, run=rec, workspace=tmp_path)


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
    )
    assert local.grade(_preds(tmp_path, [DJANGO]), "r", [DJANGO]) == {DJANGO: None}
    assert local.last_reasons == {DJANGO: "harness_error"}


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
    grader = Scripted([first, second], {ids[3]: "build", ids[4]: "harness_error"})
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
    assert len(grader.calls) == 2
    first_ids, second_ids = grader.calls[0][1], grader.calls[1][1]
    assert first_ids != second_ids
    gold = [json.loads(x) for x in grader.calls[0][0].read_text().splitlines()]
    assert [g["model_patch"] for g in gold] == [r["patch"] for r in rows]


def test_preflight_run_ids_fresh_on_rerun(tmp_path: Path) -> None:
    rows = [_row(DJANGO)]
    grader = gr.FakeGrader(results={DJANGO: True})
    assert gr.preflight(rows, grader, work=tmp_path / "pf") == {DJANGO: "ok"}
    gr.preflight(rows, grader, work=tmp_path / "pf")
    run_ids = [c[1] for c in grader.calls]
    assert len(run_ids) == 4 and len(set(run_ids)) == 4


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
