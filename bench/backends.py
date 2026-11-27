"""Execution backends of the E4 benchmark (M5 Task 6): one (arm, seed, task) run per call.

`DockerBackend` runs ``claude -p … --output-format stream-json`` inside the task's arm64
SWE-bench image, one container at a time (4 GB / 4 CPU / 4096 pids caps on the 8 GB Docker VM):

* mounts: the toolchain ``/opt/bench`` (read-only), the stage's frozen plugin
  ``/opt/plugin/token-sparen`` (read-only, `bench.freeze.verify` re-checked before every run),
  the arm's config template ``/opt/template`` (read-only), the stream's carried state
  ``/state-in`` (read-only), `entry.sh` and `patch.py` (read-only) and the run directory
  ``/run-out`` (the only writable mount); ``--cap-drop ALL`` plus `CAP_ADD`,
  ``no-new-privileges`` and a per-arm ``--network`` (default ``bridge``);
* environment: built from an explicit base (`fixed_env`, the arm's allow-listed ``env`` and the
  ``TSB_*`` run inputs), never from ``os.environ``; every variable is passed by name only
  (``-e NAME``), the values live in the docker client's own explicit environment;
* the OAuth token is never an environment variable of the container: it is a 0400 file in a
  per-run host temp dir (outside the run dir, removed after the run) mounted read-only at
  `SECRET_MOUNT`; `entry.sh` sets it only in the agent command's environment, so it is in no
  argv, no ``docker inspect`` output and not in PID 1's environment (same-uid processes in the
  container can still read the agent's environment: the dedicated revocable token is the
  mitigation, the subprocess scrub is not a barrier);
* `entry.sh` builds a container-local ``HOME=/home/bench`` from template + state, runs the
  agent under coreutils ``timeout --signal=TERM --kill-after=30 <timeout_s>`` (ruling M5-N1:
  the agent timeout is enforced inside the container), then always extracts the patch and copies
  state paths, liveness paths and transcripts to ``/run-out/state-out/`` and writes
  ``/run-out/exit.json``. A timed-out run keeps its patch and state (``agent_fail/timeout``).
* the host only backs this up: ``docker kill <name>`` at ``timeout_s + 120`` s, which is
  ``infra_fail/docker`` with no patch and nothing promoted. Before a run, every container
  labelled with the run's key and the container of this attempt's name are removed (orphans of
  a crashed scheduler); `reap` removes every container of a campaign. OOM is read from
  ``docker inspect``.

`FakeBackend` keeps the same contract with no Docker and no ``claude``: it runs the fake agent
(`bench/fixtures/fake/agent.py`) on the fake task repository with an isolated ``HOME`` and does
`entry.sh`'s bookkeeping in Python. `collect_outcome` is shared: leak scan first (a hit
quarantines the run), then `audit_run_dir` (``/run-out`` is agent-writable: any link, special
file or oversized output makes the run ``infra_fail/load`` and nothing is read; reads use
``O_NOFOLLOW`` and per-file caps), then stream metrics, load verification, liveness and
`classify`. Known limit (disclosed): the agent can rewrite its own ``stream.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bench.arms import Arm, StateError, _copy_tree, _lstat_chain
from bench.classify import FROZEN_MOUNT, RunStatus, classify, verify_load
from bench.freeze import FrozenPlugin, verify
from bench.isolation import isolated_env, make_isolated_dirs
from bench.leak import TOKEN_ENV, quarantine, scan_for_token, token_from_env
from bench.liveness import LIVENESS_PATHS, is_live
from bench.patch import PatchError, extract_patch, git_runner
from bench.streamjson import RunMetrics, parse_stream, with_overhead
from bench.tasks import BENCH_DIR, FAKE_BASE_COMMIT, FAKE_DIR, Task, make_fake_repo

PROMPT_FILE = BENCH_DIR / "prompt.md"
ENTRY_SCRIPT = BENCH_DIR / "container" / "entry.sh"
PATCH_SCRIPT = BENCH_DIR / "patch.py"
BACKSTOP_S = 120  # host `docker kill` after timeout_s + this (ruling M5-N1)
PIDS_LIMIT = 4096
PLATFORM = "linux/arm64"
CONTAINER_HOME = "/home/bench"
CONTAINER_MCP = CONTAINER_HOME + "/mcp.json"
ENTRY_MOUNT = "/opt/tsb/entry.sh"
PATCH_MOUNT = "/opt/tsb/patch.py"
TRANSCRIPTS = ".claude/projects"
DISK_PROBE_IMAGE = "busybox:1.36.1"
DOCKER_TIMEOUT_S = 300.0
PULL_TIMEOUT_S = 3600.0
PATCH_TIMEOUT_S = 60  # in-container limit of the patch extraction (fits inside the backstop)
SECRET_MOUNT = "/run/tsb-secret"  # read-only dir holding the 0400 token file `token`
DEFAULT_NETWORK = "bridge"  # per-arm override (egress allow-lists, Task 13)
# root in the container keeps only what copying a host-owned template into a root-owned HOME
# and ordinary package installs need (calibrated in Task 13)
CAP_ADD = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")
RUN_OUT_MAX_BYTES = 4 * 1024**3
OUTPUT_CAPS: dict[str, int] = {
    "stream.jsonl": 512 * 1024**2,
    "patch.diff": 64 * 1024**2,
    "exit.json": 64 * 1024,
}
# kill-after 30 s + patch time limit (+ its 5 s kill-after) + copy-out must fit in the backstop
assert BACKSTOP_S > 30 + PATCH_TIMEOUT_S + 5 + 10
# the docker client's own environment: these keys of the host environment, nothing else
CLIENT_ENV_KEYS = (
    "PATH",
    "HOME",
    "SYSTEMROOT",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)
# what the fake agent process may inherit from the host (it needs nothing else)
_FAKE_BASE_KEYS = ("PATH", "SYSTEMROOT")

Runner = Callable[[list[str], Mapping[str, str], float | None], "subprocess.CompletedProcess[str]"]


class BackendError(RuntimeError):
    """The backend refused or could not start a run (nothing ran)."""


@dataclass(frozen=True)
class RunRequest:
    task: Task
    arm: Arm
    seed: int
    stage: str
    model: str
    effort: str
    max_turns: int
    timeout_s: int
    state_in: Path
    run_dir: Path
    frozen: FrozenPlugin | None
    attempt: int
    campaign: str = "dry"  # container label and key (Task 7 `RunKey.campaign`)
    problem_statement: str = ""  # appended to `bench/prompt.md`

    @property
    def key_id(self) -> str:
        """Same as Task 7's ``RunKey.id``: campaign/stage/arm/seed/instance_id."""
        parts = (self.campaign, self.stage, self.arm.name, str(self.seed), self.task.instance_id)
        return "/".join(parts)


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    metrics: RunMetrics | None
    patch: str
    state_out: Path | None  # set only for a graded outcome (ok / agent_fail): promotable
    wall_s: float
    problems: list[str] = field(default_factory=list)


class Backend(Protocol):
    def run(self, req: RunRequest) -> RunOutcome: ...

    def reap(self, campaign: str) -> int: ...


class ImageBackend(Backend, Protocol):
    def ensure_image(self, image: str) -> None: ...

    def remove_image(self, image: str) -> None: ...

    def free_disk_gb(self) -> float: ...


def container_name(key_id: str, attempt: int) -> str:
    return "tsb-" + hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:12] + f"-a{attempt}"


def fixed_env(max_turns: int) -> dict[str, str]:
    """The run variables fixed for every arm (Global Constraints)."""
    return {
        "CLAUDE_CODE_MAX_TURNS": str(max_turns),
        "DISABLE_AUTOUPDATER": "1",
        "IS_SANDBOX": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
        "DO_NOT_TRACK": "1",
    }


def prompt_text(problem_statement: str) -> str:
    head = PROMPT_FILE.read_text(encoding="utf-8").rstrip("\n")
    return head + "\n\n" + problem_statement.strip() + "\n"


def build_claude_argv(req: RunRequest, *, mcp_path: str) -> list[str]:
    """The agent command: ``claude`` (or the arm's wrapper prefix) with the pinned flags."""
    arm = req.arm
    prefix = list(arm.wrapper) if arm.kind == "wrapper" else ["claude"]
    argv = [
        *prefix,
        "-p",
        prompt_text(req.problem_statement),
        "--model",
        req.model,
        "--effort",
        req.effort,
        "--max-turns",
        str(req.max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_path,
    ]
    if arm.uses_frozen_plugin:
        argv += ["--plugin-dir", FROZEN_MOUNT]
    if arm.append_system_prompt is not None:
        argv += ["--append-system-prompt", arm.append_system_prompt]
    return argv


def state_paths_out(arm: Arm) -> tuple[str, ...]:
    """HOME-relative paths copied out of every run: the arm's state, every liveness path and
    the transcripts (side-call overhead). Only ``arm.state_paths`` are ever promoted."""
    paths = [*arm.state_paths]
    for rels in LIVENESS_PATHS.values():
        paths += rels
    paths.append(TRANSCRIPTS)
    return tuple(dict.fromkeys(paths))


def container_env(req: RunRequest) -> dict[str, str]:
    """Every variable the container gets (passed by name; values only in the client env).
    Never the OAuth token: it reaches the container only as a read-only file (`SECRET_MOUNT`),
    so it is in no container config (``docker inspect``) and in no PID 1 environment."""
    arm = req.arm
    env = fixed_env(req.max_turns)
    env.update(arm.env)
    env.update(
        {
            "TSB_TIMEOUT_S": str(req.timeout_s),
            "TSB_BASE_COMMIT": req.task.base_commit,
            "TSB_PATCH_EXCLUDES": "\n".join(arm.patch_excludes),
            "TSB_STATE_PATHS": "\n".join(state_paths_out(arm)),
            "TSB_PATCH_TIMEOUT_S": str(PATCH_TIMEOUT_S),
        }
    )
    clash = set(env) & set(CLIENT_ENV_KEYS)
    if clash:  # pragma: no cover - arms.py refuses these keys already
        raise BackendError(f"container variables clash with the client env: {sorted(clash)}")
    return env


def release_image(backend: ImageBackend, image: str, *, keep: frozenset[str]) -> bool:
    """After a task's block (all arms and seeds, graded): remove its image unless a later
    stage needs it (ruling M5-N2). True when removed."""
    if image in keep:
        return False
    backend.remove_image(image)
    return True


def _prepare_run_dir(run_dir: Path) -> None:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise BackendError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(run_dir, 0o700)


def _missing_plugin(req: RunRequest) -> RunOutcome | None:
    """Pre-run check of a frozen-plugin arm (no quota spent on a run that cannot be valid);
    a changed frozen copy raises `bench.freeze.FrozenPluginChanged` (scheduler: stop)."""
    if not req.arm.uses_frozen_plugin:
        return None
    if req.frozen is None:
        problem = "frozen-plugin arm without a frozen plugin"
        return RunOutcome(("infra_fail", "load"), None, "", None, 0.0, [problem])
    verify(req.frozen)
    return None


class _UnsafeOutput(Exception):
    """A run output is a link, a special file or over its size cap: nothing is read."""


def audit_run_dir(run_dir: Path) -> list[str]:
    """Problems that forbid reading ``run_dir`` on the host (fix round 1, I1): any entry under
    it (``lstat``, never followed) that is not a regular file or a directory, the directory
    itself being a link, or more than `RUN_OUT_MAX_BYTES` in total. The agent can write
    ``/run-out`` (it can also rewrite its own ``stream.jsonl``: a disclosed limit)."""
    try:
        if not stat.S_ISDIR(run_dir.lstat().st_mode):
            return ["run directory is not a plain directory"]
    except OSError as exc:
        return [f"run directory unreadable: {exc.__class__.__name__}"]
    problems: list[str] = []
    total = 0
    stack = [run_dir]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            problems.append(f"unlistable output dir: {here.relative_to(run_dir).as_posix()}")
            continue
        for entry in entries:
            rel = Path(entry.path).relative_to(run_dir).as_posix()
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                problems.append(f"unreadable output: {rel}")
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
            else:
                problems.append(f"output is a link or special file: {rel}")
    if total > RUN_OUT_MAX_BYTES:
        problems.append(f"run outputs exceed {RUN_OUT_MAX_BYTES} bytes")
    return sorted(problems)


def _read_capped(run_dir: Path, name: str) -> bytes | None:
    """A top-level output of ``run_dir`` (None when absent); `_UnsafeOutput` when it is not a
    regular file or is over its cap. Opened with ``O_NOFOLLOW`` where the OS has it."""
    path = run_dir / name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _UnsafeOutput(f"{name} cannot be opened safely") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        cap = OUTPUT_CAPS[name]
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise _UnsafeOutput(f"{name} is not a regular file of at most {cap} bytes")
        return fh.read(cap + 1)[:cap]


def _json_object(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def collect_outcome(
    req: RunRequest,
    *,
    started: float,
    wall_s: float,
    docker_exit: int,
    backstop_killed: bool,
    oom_killed: bool,
    token: str,
    problems: list[str] | None = None,
) -> RunOutcome:
    """Turn a finished run directory into a `RunOutcome` (shared by both backends): leak scan
    first (a hit quarantines), then `audit_run_dir` (an unsafe output: ``infra_fail/load``,
    nothing read), then the capped reads, metrics, load checks, liveness and `classify`."""
    run_dir, arm = req.run_dir, req.arm
    notes = list(problems or [])
    hits = scan_for_token([run_dir], token)
    if hits:
        quarantine(run_dir, hits, token)
        notes.append("OAuth token found in the run outputs: run quarantined")
        return RunOutcome(("infra_fail", "leak"), None, "", None, wall_s, notes)
    if backstop_killed:
        notes.append(f"host backstop kill at timeout_s + {BACKSTOP_S} s")
    unsafe = audit_run_dir(run_dir)
    try:
        if unsafe:
            raise _UnsafeOutput("; ".join(unsafe))
        stream_data = _read_capped(run_dir, "stream.jsonl")
        exit_doc = _json_object(_read_capped(run_dir, "exit.json"))
        patch_data = _read_capped(run_dir, "patch.diff")
    except _UnsafeOutput as exc:
        bad: RunStatus = ("infra_fail", "docker" if backstop_killed or oom_killed else "load")
        notes.append(f"unsafe run outputs, nothing read: {exc}")
        return RunOutcome(bad, None, "", None, wall_s, notes)
    m: RunMetrics | None = None
    if stream_data is not None:
        lines = stream_data.decode("utf-8", "replace").splitlines()
        m = parse_stream(lines, observed_at=started)
    state_dir = run_dir / "state-out"
    finished = exit_doc is not None and state_dir.is_dir()
    if m is not None and finished and (state_dir / ".claude").is_dir():
        m = with_overhead(m, state_dir / ".claude", started)
    frozen_hash = req.frozen.plugin_hash if req.frozen is not None else None
    load_problems = verify_load(m, arm, frozen_hash) if m is not None else []
    live = m is not None and finished and is_live(m, arm, state_dir)
    timed_out = bool(exit_doc and exit_doc.get("timed_out") is True)
    status = classify(
        m,
        exit_code=docker_exit,
        killed_for_timeout=timed_out,
        backstop_killed=backstop_killed,
        disk_low=False,
        oom_killed=oom_killed,
        load_problems=load_problems,
        live=live,
        leak=False,
        wrapper=arm.kind == "wrapper",
    )
    notes += load_problems
    if oom_killed:
        notes.append("container OOM-killed")
    if status != ("infra_fail", "docker"):
        if not finished:
            status = ("infra_fail", "unknown")
            notes.append("run did not finish its bookkeeping (no exit.json or state-out)")
        elif exit_doc is not None and exit_doc.get("patch_exit") != 0:
            status = ("infra_fail", "unknown")
            notes.append("patch extraction failed or overran its time limit")
    patch = ""
    if not backstop_killed and patch_data is not None:
        patch = patch_data.decode("utf-8", "surrogateescape")
    graded = status[0] in ("ok", "agent_fail")
    return RunOutcome(status, m, patch, state_dir if graded else None, wall_s, notes)


# --- fake ----------------------------------------------------------------------------------


def _default_scenario(req: RunRequest) -> str:
    return os.environ.get("TSB_FAKE_SCENARIO", "success")


def copy_state_out(home: Path, rels: tuple[str, ...], dest: Path) -> None:
    """`entry.sh`'s copy-out in Python: each path only when no link lies between ``home`` and
    it; links inside a copied tree are skipped, never followed."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        try:
            src = _lstat_chain(home, rel)
        except StateError:
            continue
        if src is None:
            continue
        target = dest.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = src.lstat().st_mode
        if stat.S_ISDIR(mode):
            _copy_tree(src, target)
        elif stat.S_ISREG(mode):
            shutil.copy2(src, target, follow_symlinks=False)


class FakeBackend:
    """The backend contract with no Docker and no ``claude`` (CI and the dry run)."""

    def __init__(
        self,
        fixtures: Path = FAKE_DIR,
        *,
        scenario: Callable[[RunRequest], str] | None = None,
        environ: Mapping[str, str] | None = None,
        disk_gb: float = 1000.0,
    ) -> None:
        self.fixtures = fixtures
        self._scenario = scenario or _default_scenario
        self._environ = dict(os.environ if environ is None else environ)
        self._disk_gb = disk_gb
        self.last_argv: list[str] = []
        self.last_env_names: list[str] = []
        self.ensured: list[str] = []
        self.removed: list[str] = []

    def ensure_image(self, image: str) -> None:
        self.ensured.append(image)

    def remove_image(self, image: str) -> None:
        self.removed.append(image)

    def free_disk_gb(self) -> float:
        return self._disk_gb

    def reap(self, campaign: str) -> int:
        return 0

    def run(self, req: RunRequest) -> RunOutcome:
        refused = _missing_plugin(req)
        if refused is not None:
            return refused
        if req.task.base_commit != FAKE_BASE_COMMIT:
            raise BackendError(f"the fake backend runs fake tasks only, not {req.task.instance_id}")
        _prepare_run_dir(req.run_dir)
        scenario = self._scenario(req)
        token = token_from_env(self._environ)
        started = time.time()
        backstop, agent_exit = False, 0
        with tempfile.TemporaryDirectory(prefix="tsb-fake-", ignore_cleanup_errors=True) as tmp:
            testbed, home = Path(tmp) / "testbed", Path(tmp) / "home"
            make_fake_repo(testbed)
            iso = isolated_env(home)
            make_isolated_dirs(iso)
            if req.state_in.is_dir():
                _copy_tree(req.state_in, home)
            (home / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
            argv = build_claude_argv(req, mcp_path=str(home / "mcp.json"))
            skip = len(req.arm.wrapper) if req.arm.kind == "wrapper" else 1
            agent = [sys.executable, "-I", str(self.fixtures / "agent.py"), *argv[skip:]]
            env = {k: self._environ[k] for k in _FAKE_BASE_KEYS if k in self._environ}
            env |= iso | fixed_env(req.max_turns) | dict(req.arm.env)
            env["TSB_FAKE_SCENARIO"] = scenario
            if token:
                env[TOKEN_ENV] = token  # as in the container; the fake agent leaks it on `leak`
            self.last_argv, self.last_env_names = agent, sorted(env)
            out_path, err_path = req.run_dir / "stream.jsonl", req.run_dir / "stderr.txt"
            with out_path.open("wb") as out, err_path.open("wb") as err:
                try:
                    proc = subprocess.run(  # noqa: S603
                        agent,
                        cwd=testbed,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        timeout=req.timeout_s + BACKSTOP_S,
                        check=False,
                    )
                    agent_exit = proc.returncode
                except subprocess.TimeoutExpired:  # pragma: no cover - needs a hung fake agent
                    backstop = True
            if not backstop and scenario != "crash":
                self._bookkeeping(req, testbed, home, agent_exit)
        return collect_outcome(
            req,
            started=started,
            wall_s=time.time() - started,
            docker_exit=0,
            backstop_killed=backstop,
            oom_killed=False,
            token=token,
        )

    def _bookkeeping(self, req: RunRequest, testbed: Path, home: Path, agent_exit: int) -> None:
        """What `entry.sh` does after the agent: patch, state copy-out, then exit.json."""
        patch_exit = 0
        try:
            patch = extract_patch(
                str(testbed),
                req.task.base_commit,
                req.arm.patch_excludes,
                run=git_runner(self._environ),
            )
            (req.run_dir / "patch.diff").write_bytes(patch.encode("utf-8", "surrogateescape"))
        except PatchError:  # pragma: no cover - git failure on the fake repo
            patch_exit = 1
        copy_state_out(home, state_paths_out(req.arm), req.run_dir / "state-out")
        doc = {"agent_exit": agent_exit, "timed_out": agent_exit == 124, "patch_exit": patch_exit}
        (req.run_dir / "exit.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")


# --- docker --------------------------------------------------------------------------------


def _subprocess_runner(
    argv: list[str], env: Mapping[str, str], timeout: float | None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            argv,
            env=dict(env),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # docker CLI missing: same as "command not found"
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _volume(src: Path, dst: str, *, read_only: bool) -> list[str]:
    text = str(src)
    if os.name == "posix" and ":" in text:
        raise BackendError(f"mount source holds ':' and cannot be bind-mounted with -v: {text}")
    return ["-v", f"{text}:{dst}" + (":ro" if read_only else "")]


def _write_secret(root: Path | None, token: str) -> Path:
    """A fresh 0700 directory holding the token as a 0400 file ``token`` (mounted read-only at
    `SECRET_MOUNT`, removed by the caller after the run; never inside the run directory)."""
    directory = Path(tempfile.mkdtemp(prefix="tsb-secret-", dir=root))
    fd = os.open(directory / "token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return directory


def _remove_secret(directory: Path) -> None:
    for path in (directory / "token", directory):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    shutil.rmtree(directory, ignore_errors=True)


class DockerBackend:
    """Runs each request in its task's arm64 image (module docstring)."""

    def __init__(
        self,
        toolchain: Path,
        templates: Path,
        *,
        memory: str = "4g",
        cpus: int = 4,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        probe_image: str = DISK_PROBE_IMAGE,
        disk_path: Path | None = None,
        networks: Mapping[str, str] | None = None,
        secret_root: Path | None = None,
    ) -> None:
        self.toolchain, self.templates = toolchain, templates
        self.memory, self.cpus = memory, cpus
        self._runner: Runner = runner or _subprocess_runner
        self._environ = dict(os.environ if environ is None else environ)
        self.probe_image = probe_image
        self.disk_path = disk_path or templates
        self.networks = dict(networks or {})  # arm name -> docker network (default bridge)
        self.secret_root = secret_root  # where the per-run token dir is made (system temp)

    def _client_env(self, container: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: self._environ[k] for k in CLIENT_ENV_KEYS if k in self._environ}
        env.update(container or {})
        return env

    def _docker(
        self, *args: str, timeout: float = DOCKER_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(["docker", *args], self._client_env(), timeout)

    def _ids(self, *args: str) -> list[str]:
        """Container ids a ``docker ps`` lists; `BackendError` when docker fails (fail closed:
        an unreadable daemon never passes the orphan or one-container checks)."""
        proc = self._docker(*args)
        if proc.returncode != 0:
            raise BackendError(f"docker {' '.join(args[:2])} failed ({proc.returncode})")
        return [line for line in proc.stdout.split() if line]

    # images and disk (ruling M5-N2) --------------------------------------------------------

    def ensure_image(self, image: str) -> None:
        if self._docker("image", "inspect", image).returncode == 0:
            return
        pulled = self._docker("pull", "--platform", PLATFORM, image, timeout=PULL_TIMEOUT_S)
        if pulled.returncode != 0:
            raise BackendError(f"docker pull {image} failed: {pulled.stderr.strip()[:300]}")

    def remove_image(self, image: str) -> None:
        self._docker("image", "rm", image)
        self._docker("image", "prune", "-f")  # dangling layers only

    def free_disk_gb(self) -> float:
        """Free GB: the minimum of the host disk and the Docker VM disk (0.0 when the VM disk
        cannot be read: the scheduler then pauses rather than fill the disk)."""
        host = shutil.disk_usage(self.disk_path).free / 1e9
        probe = self._docker(
            "run", "--rm", "--network", "none", "--entrypoint", "df", self.probe_image, "-Pk", "/"
        )
        try:
            fields = probe.stdout.strip().splitlines()[-1].split()
            vm = int(fields[3]) * 1024 / 1e9
        except (IndexError, ValueError):
            return 0.0
        return min(host, vm) if probe.returncode == 0 else 0.0

    # containers --------------------------------------------------------------------------

    def reap(self, campaign: str) -> int:
        ids = self._ids("ps", "-aq", "--filter", f"label=tsb.campaign={campaign}")
        if ids:
            self._docker("rm", "-f", *ids)
        return len(ids)

    def _remove_orphans(self, key_id: str, name: str) -> None:
        """Remove every container of this key and of this attempt's name, then confirm none is
        left (`BackendError` otherwise: never two containers of one key)."""
        ids = self._ids("ps", "-aq", "--filter", f"label=tsb.key={key_id}")
        if ids:
            self._docker("rm", "-f", *ids)
        self._docker("rm", "-f", name)
        left = self._ids("ps", "-aq", "--filter", f"label=tsb.key={key_id}")
        left += self._ids("ps", "-aq", "--filter", f"name=^/?{name}$")
        if left:
            raise BackendError(f"orphan containers of {name} survived removal")

    def run_argv(
        self, req: RunRequest, name: str, env: Mapping[str, str], secret: Path | None = None
    ) -> list[str]:
        arm = req.arm
        argv = ["docker", "run", "--name", name]
        argv += ["--label", f"tsb.campaign={req.campaign}", "--label", f"tsb.key={req.key_id}"]
        argv += ["--platform", PLATFORM, "--pull", "never"]
        argv += ["--memory", self.memory, "--memory-swap", self.memory]
        argv += ["--cpus", str(self.cpus), "--pids-limit", str(PIDS_LIMIT)]
        argv += ["--network", self.networks.get(arm.name, DEFAULT_NETWORK)]
        argv += ["--cap-drop", "ALL"]
        for cap in CAP_ADD:
            argv += ["--cap-add", cap]
        argv += ["--security-opt", "no-new-privileges"]
        argv += ["--entrypoint", "/bin/sh", "--workdir", "/testbed"]
        argv += _volume(self.toolchain, "/opt/bench", read_only=True)
        argv += _volume(self.templates / arm.name, "/opt/template", read_only=True)
        if arm.uses_frozen_plugin and req.frozen is not None:
            argv += _volume(req.frozen.path, FROZEN_MOUNT, read_only=True)
        argv += _volume(req.state_in, "/state-in", read_only=True)
        argv += _volume(req.run_dir, "/run-out", read_only=False)
        argv += _volume(ENTRY_SCRIPT, ENTRY_MOUNT, read_only=True)
        argv += _volume(PATCH_SCRIPT, PATCH_MOUNT, read_only=True)
        if secret is not None:
            argv += _volume(secret, SECRET_MOUNT, read_only=True)
        for key in sorted(env):
            argv += ["-e", key]  # by name only: the value stays in the client's environment
        argv += [req.task.image, ENTRY_MOUNT]
        return argv + build_claude_argv(req, mcp_path=CONTAINER_MCP)

    def run(self, req: RunRequest) -> RunOutcome:
        refused = _missing_plugin(req)
        if refused is not None:
            return refused
        if not (self.templates / req.arm.name).is_dir():
            raise BackendError(f"no config template for arm {req.arm.name}")
        name = container_name(req.key_id, req.attempt)
        self._remove_orphans(req.key_id, name)
        if self._ids("ps", "-q", "--filter", "label=tsb.campaign"):
            raise BackendError("another bench container is running (one container at a time)")
        _prepare_run_dir(req.run_dir)
        token = token_from_env(self._environ)
        env = container_env(req)
        secret = _write_secret(self.secret_root, token) if token else None
        try:
            argv = self.run_argv(req, name, env, secret)
            started = time.time()
            backstop, problems = False, []
            try:
                proc = self._runner(argv, self._client_env(env), req.timeout_s + BACKSTOP_S)
                docker_exit = proc.returncode
            except subprocess.TimeoutExpired:
                backstop, docker_exit = True, -9
                self._docker("kill", name)  # the container, not only the client
            inspect = self._docker("inspect", "--format", "{{.State.OOMKilled}}", name)
            oom = inspect.stdout.strip() == "true"
            self._docker("rm", "-f", name)
        finally:
            if secret is not None:
                _remove_secret(secret)
        if docker_exit not in (0, -9):
            problems.append(f"docker run exited {docker_exit}")
        return collect_outcome(
            req,
            started=started,
            wall_s=time.time() - started,
            docker_exit=docker_exit,
            backstop_killed=backstop,
            oom_killed=oom,
            token=token,
            problems=problems,
        )
