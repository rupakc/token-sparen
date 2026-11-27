"""Local grading with the official SWE-bench harness and the oracle preflight (M5 Task 8).

Patches are graded by the official ``swebench`` package, run from its own venv
(`bench/requirements-grade.txt`), on this machine: no hosted grader (owner ruling, M5). The mode
was chosen by the arm64 spike of ruling M5-N3 before any agent run and is recorded in
`bench/data/grade_mode.json`:

* ``swebench5-arm64``: swebench 5.x on the prebuilt arm64 images. swebench 5 takes each
  instance's image from the dataset row's ``image`` field and accepts a local dataset file, so
  `write_arm64_dataset` rewrites ``image`` to `bench.tasks.arm64_image` and the console script
  runs ``swebench eval <dataset> -p … --run-id … -i … -j 1 -t 1800 --report-dir …``.
* ``swebench-pinned-arm64``: a pinned 4.x release whose eval scripts match the arm64 images,
  run as ``python -m swebench.harness.run_evaluation … --namespace swebench``.
* ``amd64-emulation``: swebench 5.x on the x86_64 images under Docker's amd64 emulation
  (grading only; the dataset keeps its x86_64 ``image`` values).

Grading holds ``<workspace>/bench.lock`` like every container run (one container at a time).
The agent's patch failing to apply or its tests exceeding the timeout is the agent's failure
(``False``). An instance the environment broke (image missing, container or Docker error,
out of memory, network, no report) is ``None``, never ``False``: it is regraded, at most
`MAX_GRADE_ATTEMPTS` times per patch, then left ungradable (dropped and disclosed). The harness
runs with its HOME and caches in the workspace, and its containers are memory-capped.

`grade_task` runs right after a task's block, while its image is still present (the scheduler
removes it afterwards); `grade_stage` catches up and regrades harness errors. Both append to
``grades.jsonl`` next to the ledger and are idempotent: a run whose current patch already has a
definite verdict is never graded again. Each batch holds one arm and seed (swebench keys
predictions by instance id) and gets a fresh ``run_id``
(``<campaign>-<stage>-<arm>-s<seed>-b<n>``), so a harness log directory is never reused.

`preflight` grades every task's gold patch twice and a harmless no-op patch once; a task is
``ok`` only when gold resolves both times and the no-op does not, else it is excluded with a
reason (``build``, ``gold_fail``, ``flaky_gold``, ``noop_pass``, ``harness_error``), decided
before any agent run and disclosed in the report.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from bench.leak import TOKEN_ENV, scan_for_token, token_from_env
from bench.tasks import arm64_image
from token_sparen.store.jsonstore import locked

BENCH_DIR = Path(__file__).resolve().parent
GRADE_MODE_FILE = BENCH_DIR / "data" / "grade_mode.json"
REQUIREMENTS_FILE = BENCH_DIR / "requirements-grade.txt"
GRADES_FILE = "grades.jsonl"
LOCK_NAME = "bench"  # `locked` takes `<name>.lock`: the shared `bench.lock`
LOCK_TIMEOUT_S = 6 * 3600.0  # a run in progress holds it for at most its wall timeout + backstop
V4_MODULE = "swebench.harness.run_evaluation"
LOG_DIR = Path("logs") / "run_evaluation"  # relative to the harness's working directory
GOLD_MODEL = "gold"
NOOP_MODEL = "noop"
# A harmless patch that applies to every repository and changes no code: one new text file.
# swebench short-circuits an empty patch, so the no-op check needs a real one.
NOOP_PATCH = (
    "diff --git a/.tsb_preflight_noop.txt b/.tsb_preflight_noop.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.tsb_preflight_noop.txt\n"
    "@@ -0,0 +1 @@\n"
    "+# token-sparen bench preflight: a no-op patch must not resolve the task\n"
)

GradeMode = Literal["swebench5-arm64", "swebench-pinned-arm64", "amd64-emulation"]
GRADE_MODES: tuple[str, ...] = get_args(GradeMode)
PreflightStatus = Literal["ok", "excluded"]
ExclusionReason = Literal["build", "gold_fail", "flaky_gold", "noop_pass", "harness_error"]
MAX_GRADE_ATTEMPTS = 3  # harness errors per (key, patch) before the run is left ungradable
GRADER_HOME = "grading-home"  # HOME, XDG and caches of the harness, inside the workspace
GRADE_MEMORY = "4g"  # like an agent container (Global Constraints)
CAP_POLL_S = 2.0

# swebench 5.0.2 outcome markers (`harness/constants`, `harness/infra_failure`; checked against
# a real non-applying patch in fix round 1). The agent's patch not applying, or its tests
# exceeding the timeout, is the agent's failure (False); anything the environment caused is a
# harness error (None, regraded up to MAX_GRADE_ATTEMPTS).
APPLY_FAIL_MARKER = ">>>>> Patch Apply Failed"
TIMEOUT_REASON = "tests_timed_out"
TIMEOUT_MARKERS = ("Timeout error:", "Test timed out after")
_BUILD_MARKERS = ("not found for", "Error creating container", "Error building image")
# swebench's environment-tier `failure_reasons` → our reason
_INFRA_REASONS = {
    "out_of_memory": "oom",
    "container_unavailable": "docker",
    "network_unreachable": "network",
    "browser_launch_failed": "environment",
    "display_unavailable": "environment",
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class GradeError(ValueError):
    """The grade mode record is missing or malformed."""


class LeakInPredictions(RuntimeError):
    """A prediction holds an OAuth token (or the ``sk-ant-oat`` pattern); nothing was written."""


class Grader(Protocol):
    def grade(
        self, preds: Path, run_id: str, instance_ids: Sequence[str]
    ) -> dict[str, bool | None]: ...


@dataclass(frozen=True)
class FinalRun:
    """A ledger key whose final outcome is graded (status ``ok`` or ``agent_fail``)."""

    key: str
    stage: str
    arm: str
    seed: int
    instance_id: str
    patch: str


class GradeLedger(Protocol):
    """What grading reads from the ledger (Task 7): its file, campaign and final runs."""

    @property
    def path(self) -> Path: ...

    @property
    def campaign(self) -> str: ...

    def final_runs(self, stage: str) -> list[FinalRun]: ...


# --- mode and datasets --------------------------------------------------------------------


def load_grade_mode(path: Path = GRADE_MODE_FILE) -> tuple[GradeMode, str]:
    """(mode, swebench version) chosen by the spike."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GradeError(f"grade mode unreadable: {exc}") from exc
    mode, version = doc.get("mode"), doc.get("swebench_version")
    if mode not in GRADE_MODES or not isinstance(version, str) or not version:
        raise GradeError(f"grade mode must be one of {', '.join(GRADE_MODES)} with a version")
    return mode, version


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _write_rows(rows: Iterable[Mapping[str, Any]], path: Path) -> Path:
    data = "".join(json.dumps(dict(r), sort_keys=True) + "\n" for r in rows)
    _write_private(path, data.encode("utf-8"))
    return path


def write_arm64_dataset(rows: Iterable[Mapping[str, Any]], path: Path) -> Path:
    """Verified rows with ``image`` rewritten to the task's arm64 image (0600 jsonl)."""
    return _write_rows(({**r, "image": arm64_image(str(r["instance_id"]))} for r in rows), path)


def write_grading_dataset(rows: Iterable[Mapping[str, Any]], path: Path, mode: GradeMode) -> Path:
    """The dataset the grader reads in ``mode``: arm64 images, or the rows' own x86_64 images
    under amd64 emulation."""
    if mode == "amd64-emulation":
        return _write_rows(rows, path)
    return write_arm64_dataset(rows, path)


def write_predictions(
    rows: Iterable[tuple[str, str, str]],
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Write swebench predictions ``(instance_id, model_name_or_path, model_patch)`` to
    ``path`` (0600 jsonl). The file is scanned for the OAuth token before it is put in place;
    a hit deletes it and raises `LeakInPredictions` (the token is never named)."""
    data = "".join(
        json.dumps({"instance_id": i, "model_name_or_path": m, "model_patch": p}) + "\n"
        for i, m, p in rows
    )
    tmp = path.with_name(path.name + ".tmp")
    _write_private(tmp, data.encode("utf-8"))
    hits = scan_for_token([tmp], token_from_env(environ))
    if hits:
        tmp.unlink(missing_ok=True)
        raise LeakInPredictions("a prediction holds an OAuth token; nothing was written")
    os.replace(tmp, path)


def _models(preds: Path) -> list[str]:
    rows = [json.loads(x) for x in preds.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [str(r["model_name_or_path"]) for r in rows]


# --- graders ------------------------------------------------------------------------------


class LocalSwebench:
    """The official harness from its venv on local Docker (see the module docstring)."""

    def __init__(
        self,
        venv: Path,
        dataset: Path,
        report_dir: Path,
        mode: GradeMode,
        workers: int = 1,
        timeout_s: int = 1800,
        *,
        run: Runner = subprocess.run,
        workspace: Path | None = None,
        environ: Mapping[str, str] | None = None,
        capper: Capper | None = None,
    ) -> None:
        self.venv, self.dataset, self.report_dir = venv, dataset, report_dir
        self.mode, self.workers, self.timeout_s = mode, workers, timeout_s
        self.workspace = workspace if workspace is not None else report_dir.parent
        self._run = run
        self._environ = environ
        self.capper: Capper = capper if capper is not None else DockerMemoryCapper()
        self.last_reasons: dict[str, str] = {}

    def argv(self, preds: Path, run_id: str, instance_ids: Sequence[str]) -> list[str]:
        if self.mode == "swebench-pinned-arm64":
            return [
                str(self.venv / "bin" / "python"),
                "-m",
                V4_MODULE,
                "--dataset_name",
                str(self.dataset),
                "--predictions_path",
                str(preds),
                "--run_id",
                run_id,
                "--instance_ids",
                *instance_ids,
                "--max_workers",
                str(self.workers),
                "--timeout",
                str(self.timeout_s),
                "--namespace",
                "swebench",
                "--report_dir",
                str(self.report_dir),
            ]
        argv = [str(self.venv / "bin" / "swebench"), "eval", str(self.dataset)]
        argv += ["-p", str(preds), "--run-id", run_id]
        for iid in instance_ids:
            argv += ["-i", iid]
        argv += ["-j", str(self.workers), "-t", str(self.timeout_s)]
        return [*argv, "--report-dir", str(self.report_dir)]

    def env(self) -> dict[str, str]:
        """The owner's environment without the token, with HOME, XDG dirs and the HF and pip
        caches inside ``<workspace>/grading-home`` (nothing is written under the real home).
        Docker is reached through ``DOCKER_HOST`` or the default socket, not the home."""
        env = dict(os.environ if self._environ is None else self._environ)
        env.pop(TOKEN_ENV, None)  # the grader never sees the agent's credential
        env.pop("DOCKER_DEFAULT_PLATFORM", None)
        home = self.workspace / GRADER_HOME
        env |= {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "HF_HOME": str(home / ".cache" / "huggingface"),
            "PIP_CACHE_DIR": str(home / ".cache" / "pip"),
        }
        if self.mode == "amd64-emulation":
            env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
        return env

    def grade(
        self, preds: Path, run_id: str, instance_ids: Sequence[str]
    ) -> dict[str, bool | None]:
        self.report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.workspace / GRADER_HOME).mkdir(parents=True, exist_ok=True, mode=0o700)
        argv = self.argv(preds, run_id, instance_ids)
        budget = self.timeout_s * max(1, len(instance_ids)) + 900  # pulls and setup
        models = _models(preds)
        model = models[0] if models else ""
        report = self.report_dir / f"{model.replace('/', '__')}.{run_id}.json"
        report.unlink(missing_ok=True)  # never read a stale report for this run id
        with locked(self.workspace / LOCK_NAME, timeout=LOCK_TIMEOUT_S):
            self.capper.start(run_id)
            try:
                self._run(
                    argv,
                    cwd=str(self.report_dir),
                    env=self.env(),
                    capture_output=True,
                    text=True,
                    timeout=budget,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # no report: every instance is a harness error below
            finally:
                self.capper.stop()
        return self._parse(report if models else None, run_id, model, instance_ids)

    def _parse(
        self, report: Path | None, run_id: str, model: str, instance_ids: Sequence[str]
    ) -> dict[str, bool | None]:
        try:
            doc = json.loads(report.read_text(encoding="utf-8")) if report else {}
        except (OSError, json.JSONDecodeError):
            doc = {}
        out: dict[str, bool | None] = {}
        self.last_reasons = {}
        for iid in instance_ids:
            verdict, reason = self._classify(doc, run_id, model, iid)
            out[iid] = verdict
            if reason:
                self.last_reasons[iid] = reason
        return out

    def _classify(
        self, doc: Mapping[str, Any], run_id: str, model: str, iid: str
    ) -> tuple[bool | None, str | None]:
        """(verdict, reason) of one instance from the run report and its logs."""
        if not doc:
            return None, "harness_error"
        reasons = doc.get("failure_reasons") or {}
        if iid in set(doc.get("infra_failure_ids", [])):
            return None, _INFRA_REASONS.get(str(reasons.get(iid)), "environment")
        if iid in set(doc.get("empty_patch_ids", [])):
            return False, "empty_patch"
        if iid in set(doc.get("resolved_ids", [])):
            return True, None
        if iid in set(doc.get("unresolved_ids", [])):
            return False, None
        if iid in set(doc.get("error_ids", [])):
            inst, tests = self._logs(run_id, model, iid)
            if APPLY_FAIL_MARKER in inst:
                return False, "apply_fail"
            if reasons.get(iid) == TIMEOUT_REASON or any(
                m in tests or m in inst for m in TIMEOUT_MARKERS
            ):
                return False, "timeout"
            if any(m in inst for m in _BUILD_MARKERS):
                return None, "build"
        return None, "harness_error"

    def _logs(self, run_id: str, model: str, iid: str) -> tuple[str, str]:
        log = self.report_dir / LOG_DIR / run_id / model.replace("/", "__") / iid
        texts = []
        for name in ("run_instance.log", "test_output.txt"):
            try:
                texts.append((log / name).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                texts.append("")
        return texts[0], texts[1]


class Capper(Protocol):
    def start(self, run_id: str) -> None: ...

    def stop(self) -> None: ...


class DockerMemoryCapper:
    """Caps the memory of the harness's containers while a grading run is in progress.

    swebench 5 creates its containers without a memory limit (no flag for one), so while the
    run holds the lock a thread applies ``docker update --memory <cap> --memory-swap <cap>`` to
    every container whose name ends in the run id (``sweb.eval.<instance>.<run_id>``). A test
    process killed for memory then shows ``Killed`` in the test output, which swebench
    classifies ``out_of_memory`` (environment tier): a harness error, never ``False``."""

    def __init__(
        self,
        memory: str = GRADE_MEMORY,
        *,
        run: Runner = subprocess.run,
        poll_s: float = CAP_POLL_S,
    ) -> None:
        self.memory, self._run, self.poll_s = memory, run, poll_s
        self._done: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _docker(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run(
                ["docker", *args], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def cap_once(self, run_id: str) -> None:
        listed = self._docker("ps", "--filter", f"name={run_id}", "--format", "{{.Names}}")
        if listed is None:
            return
        for name in listed.stdout.split():
            if name in self._done or not name.endswith("." + run_id):
                continue
            mem = ("--memory", self.memory, "--memory-swap", self.memory)
            done = self._docker("update", *mem, name)
            if done is not None and done.returncode == 0:
                self._done.add(name)

    def start(self, run_id: str) -> None:
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.cap_once(run_id)
                self._stop.wait(self.poll_s)

        self._thread = threading.Thread(target=loop, name="grade-memory-cap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
            self._thread = None


@dataclass
class FakeGrader:
    """Grades without Docker (dry run and tests): `NOOP_PATCH` is never resolved; otherwise
    ``results`` per instance id, else resolved when the patch's sha256 starts with an even hex
    digit (deterministic)."""

    results: Mapping[str, bool | None] = field(default_factory=dict)
    calls: list[tuple[Path, str, list[str]]] = field(default_factory=list)

    def grade(
        self, preds: Path, run_id: str, instance_ids: Sequence[str]
    ) -> dict[str, bool | None]:
        self.calls.append((preds, run_id, list(instance_ids)))
        rows = [json.loads(x) for x in preds.read_text(encoding="utf-8").splitlines() if x]
        patches = {str(r["instance_id"]): str(r["model_patch"]) for r in rows}
        out: dict[str, bool | None] = {}
        for iid in instance_ids:
            if patches.get(iid) == NOOP_PATCH:
                out[iid] = False
            elif iid in self.results:
                out[iid] = self.results[iid]
            else:
                digest = hashlib.sha256(patches.get(iid, "").encode()).hexdigest()
                out[iid] = int(digest[0], 16) % 2 == 0
        return out


# --- grading the ledger -------------------------------------------------------------------


def grades_path(ledger: GradeLedger) -> Path:
    return ledger.path.with_name(GRADES_FILE)


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in lines if x.strip()]


def _append(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in records).encode())
        fh.flush()
        os.fsync(fh.fileno())


def load_grades(path: Path, *, stage: str) -> dict[str, bool | None]:
    """key → verdict of the stage (the last record per key: the latest patch's grading)."""
    out: dict[str, bool | None] = {}
    for rec in _records(path):
        if rec.get("kind") == "grade" and rec.get("stage") == stage:
            out[str(rec["key"])] = rec["resolved"]
    return out


def _sha(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _attempts(path: Path) -> tuple[set[tuple[str, str]], dict[tuple[str, str], int]]:
    """(key, patch sha) with a definite verdict, and harness errors per (key, patch sha)."""
    done: set[tuple[str, str]] = set()
    errors: dict[tuple[str, str], int] = {}
    for r in _records(path):
        if r.get("kind") != "grade":
            continue
        pair = (str(r["key"]), str(r["patch_sha256"]))
        if r.get("resolved") is None:
            errors[pair] = errors.get(pair, 0) + 1
        else:
            done.add(pair)
    return done, errors


def _pending(path: Path, runs: Sequence[FinalRun]) -> list[FinalRun]:
    """Runs whose current patch has no definite verdict yet and fewer than
    `MAX_GRADE_ATTEMPTS` harness errors."""
    done, errors = _attempts(path)
    out = []
    for r in runs:
        pair = (r.key, _sha(r.patch))
        if pair not in done and errors.get(pair, 0) < MAX_GRADE_ATTEMPTS:
            out.append(r)
    return out


def ungradable(path: Path, *, stage: str) -> set[str]:
    """Keys of the stage whose latest grading is a harness error after `MAX_GRADE_ATTEMPTS`
    attempts on the same patch: not graded, dropped from the pairing and disclosed."""
    done, errors = _attempts(path)
    latest: dict[str, str] = {}
    for r in _records(path):
        if r.get("kind") == "grade" and r.get("stage") == stage:
            latest[str(r["key"])] = str(r["patch_sha256"])
    return {
        k
        for k, sha in latest.items()
        if (k, sha) not in done and errors.get((k, sha), 0) >= MAX_GRADE_ATTEMPTS
    }


def _grade_runs(ledger: GradeLedger, grader: Grader, stage: str, runs: Sequence[FinalRun]) -> None:
    path = grades_path(ledger)
    todo = _pending(path, runs)
    empty = [r for r in todo if not r.patch.strip()]
    _append(path, [_grade_record(stage, r, False, "empty_patch", None) for r in empty])
    batches: dict[tuple[str, int], list[FinalRun]] = {}
    for r in todo:
        if r.patch.strip():
            batches.setdefault((r.arm, r.seed), []).append(r)
    work = path.parent / "grading"
    for (arm, seed), group in batches.items():
        prefix = f"{ledger.campaign}-{stage}-{arm}-s{seed}"
        ids = [r.instance_id for r in group]
        with locked(path, timeout=LOCK_TIMEOUT_S):  # `grades.jsonl.lock`: one allocator
            n = 1 + sum(
                1
                for rec in _records(path)
                if rec.get("kind") == "batch" and str(rec["run_id"]).rsplit("-b", 1)[0] == prefix
            )
            run_id = f"{prefix}-b{n}"
            batch = {"kind": "batch", "stage": stage, "run_id": run_id, "instance_ids": ids}
            _append(path, [batch])
        preds = work / f"{run_id}.preds.jsonl"
        write_predictions(((r.instance_id, arm, r.patch) for r in group), preds)
        verdicts = grader.grade(preds, run_id, ids)
        _append(
            path,
            [
                _grade_record(stage, r, verdicts.get(r.instance_id), "swebench", run_id)
                for r in group
            ],
        )


def _grade_record(
    stage: str, run: FinalRun, resolved: bool | None, by: str, run_id: str | None
) -> dict[str, Any]:
    return {
        "kind": "grade",
        "stage": stage,
        "key": run.key,
        "arm": run.arm,
        "seed": run.seed,
        "instance_id": run.instance_id,
        "patch_sha256": _sha(run.patch),
        "resolved": resolved,
        "graded_by": by,
        "run_id": run_id,
    }


def grade_task(ledger: GradeLedger, grader: Grader, *, stage: str, instance_id: str) -> None:
    """Grade every final run of one task (after its block, before its image is removed)."""
    runs = [r for r in ledger.final_runs(stage) if r.instance_id == instance_id]
    _grade_runs(ledger, grader, stage, runs)


def grade_stage(ledger: GradeLedger, grader: Grader, *, stage: str) -> Path:
    """Grade every final run of the stage not yet definitely graded; return ``grades.jsonl``."""
    _grade_runs(ledger, grader, stage, ledger.final_runs(stage))
    path = grades_path(ledger)
    if not path.exists():
        _write_private(path, b"")
    return path


# --- oracle preflight ---------------------------------------------------------------------

PREFLIGHT_FILE = "preflight.json"


def preflight(
    tasks: Sequence[Mapping[str, Any]], grader: Grader, *, work: Path
) -> dict[str, PreflightStatus]:
    """The oracle check, decided before any agent run: each task's gold patch (dataset row
    ``patch``) is graded twice in separate harness runs and `NOOP_PATCH` (applies cleanly,
    changes no code) once; ``ok`` only when gold resolves both times and the no-op does not.
    Reasons go to ``<work>/preflight.json``. The attempt number is reserved before any
    grading, so a crashed preflight never shares a run id with the next one."""
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = work / PREFLIGHT_FILE
    with locked(record, timeout=LOCK_TIMEOUT_S):
        try:
            prior = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        attempt = int(prior.get("attempts", 0)) + 1
        _write_private(record, _json({**prior, "attempts": attempt}))
    ids = [str(t["instance_id"]) for t in tasks]
    passes: list[dict[str, bool | None]] = []
    reasons: list[dict[str, str]] = []
    runs = [("g1", GOLD_MODEL, None), ("g2", GOLD_MODEL, None), ("noop", NOOP_MODEL, NOOP_PATCH)]
    for tag, model, patch in runs:
        run_id = f"preflight-{attempt}-{tag}"
        preds = work / f"{run_id}.preds.jsonl"
        write_predictions(
            ((str(t["instance_id"]), model, patch or str(t["patch"])) for t in tasks), preds
        )
        passes.append(grader.grade(preds, run_id, ids))
        reasons.append(dict(getattr(grader, "last_reasons", {}) or {}))
    status: dict[str, PreflightStatus] = {}
    why: dict[str, ExclusionReason] = {}
    for iid in ids:
        verdicts = [p.get(iid) for p in passes]
        reason = _exclusion(verdicts, [r.get(iid) for r in reasons])
        status[iid] = "ok" if reason is None else "excluded"
        if reason is not None:
            why[iid] = reason
    doc = {
        "attempts": attempt,
        "results": {i: {"status": status[i], "reason": why.get(i)} for i in ids},
    }
    _write_private(record, _json(doc))
    return status


def _exclusion(
    verdicts: Sequence[bool | None], reasons: Sequence[str | None]
) -> ExclusionReason | None:
    """Gold twice, then the no-op: why a task is excluded (None when it is ok)."""
    gold1, gold2, noop = verdicts
    if gold1 is None or gold2 is None:
        return "build" if "build" in reasons[:2] else "harness_error"
    if gold1 and gold2:
        if noop is None:
            return "build" if reasons[2] == "build" else "harness_error"
        return "noop_pass" if noop else None
    return "flaky_gold" if gold1 or gold2 else "gold_fail"


def _json(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


def preflight_reasons(work: Path) -> dict[str, str]:
    """instance id → exclusion reason of the last preflight in ``work``."""
    doc = json.loads((work / PREFLIGHT_FILE).read_text(encoding="utf-8"))
    return {i: r["reason"] for i, r in doc["results"].items() if r["status"] == "excluded"}
