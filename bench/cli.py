"""The E4 benchmark CLI (M5 Task 12): ``python -m bench <command> --out DIR``.

Every command works in one private workspace (``--out``, or ``$TOKEN_SPAREN_BENCH_OUT``; no
default): outside any git work tree, 0700 directories and 0600 files
(`bench.isolation.check_workspace`). A workspace is either a dry run or a real campaign, fixed by
its first command (``workspace.json``), and holds one campaign id.

Layout: ``stages/<stage>/`` (``ledger.jsonl``, ``grades.jsonl``, ``anchors.jsonl``,
``keys.json``, ``analysis.json``, ``gate.json``), ``frozen/<stage>/`` (the read-only plugin) and
``frozen/<stage>.json``, ``grading/`` (``preflight.json``, harness reports), ``power.json`` (the
confirmation spec and verdict), ``calibration.json`` (quota and disk projections),
``reports/<stage>/`` (``e4.json``, ``report.md``, figures), ``anchors-git/`` (the clone that
publishes anchors), ``toolchain/`` and ``templates/`` (Task 13), ``dataset.jsonl``.

Real campaigns (Task 13 onwards) need: a tagged freeze per stage (``v0.5.0-screen1`` for
screening, ``v1.0.0-rc1`` for confirmation, plain tag names only), the oracle preflight, the
owner's quota readings (``quota set``; account-wide, `bench.quota`), the dedicated OAuth token in
a 0600 file (``--token-file`` or ``$TOKEN_SPAREN_BENCH_TOKEN_FILE``; never the token itself in
the environment or argv), and macOS or Linux. Confirmation writes the plan into the ledger
(`bench.stages.write_plan`, with the frozen plugin's version and the owner's live-pricing date)
and publishes the plan anchor to the ``bench-anchors`` branch before its first run; then one
commit per push after every graded block (at most `bench.anchors.RUN_CAP` graded runs per
publication), a heartbeat publication whenever the last one is `HEARTBEAT_S` old, and once at
the end. ``gate`` fetches that branch first; without server receipts (``--github-repo``) its
verdict is advisory and ``report`` refuses it.

``run --dry-run`` (CI): `FakeBackend`, a fake grader, `FAKE_TASKS`, a frozen copy of the committed
``HEAD`` without tag checks, a scripted quota in the workspace's own account directory (the real
account is never touched), a virtual clock, a temporary bare repository as the anchors remote,
``n_boot`` = `DRY_N_BOOT` and ``n_sim`` = `DRY_N_SIM`. Without ``--stage`` it runs the whole
pipeline: screening, grade, analyse, power, confirmation, gate (advisory, then with receipts
simulated from the temporary remote), report. A dry-run workspace never publishes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bench import scheduler as sc
from bench.anchors import (
    ANCHOR_KEYS,
    ANCHORS_BRANCH,
    PLAN_FILE,
    RUN_CAP,
    AnchorError,
    anchors_to_publish,
    branch_path,
    check_plan_file,
    current_heads,
    fetch_receipts,
    read_published,
    record_anchor,
)
from bench.arms import load_arms
from bench.backends import BackendError, DockerBackend, FakeBackend, ImageBackend, RunOutcome
from bench.backends import RunRequest as _RunRequest
from bench.freeze import FreezeError, FrozenPlugin, FrozenPluginChanged, freeze, verify
from bench.gate import GateRefused, GateResult
from bench.gate import gate as run_gate
from bench.grade import (
    Grader,
    LocalSwebench,
    grade_stage,
    grade_task,
    grades_chain_head,
    load_grade_mode,
    preflight,
    preflight_ok,
    write_grading_dataset,
)
from bench.isolation import IsolationError, check_workspace
from bench.leak import TOKEN_ENV
from bench.ledger import Ledger, LedgerError, RunKey, read_jsonl
from bench.power import FlipModel, PowerPlan
from bench.quota import (
    FIVE_HOURS_S,
    Account,
    QuotaPolicy,
    QuotaReading,
    default_account_dir,
    per_run_weekly_pct,
)
from bench.report import ReportError, build_report
from bench.report import publish as publish_report
from bench.stages import (
    SCREENING,
    STYLE_ARM,
    ConfirmationPlan,
    DiskProjection,
    PlanError,
    QuotaProjection,
    StageSpec,
    plan_confirmation,
    read_plan,
    style_rule,
    write_plan,
)
from bench.stats import GATE_SEED, Summary, summarise_ledger
from bench.tasks import FAKE_DIR, FAKE_TASKS, PoolError, Task, load_dataset_rows, load_pool, streams

REPO = Path(__file__).resolve().parent.parent
OUT_ENV = "TOKEN_SPAREN_BENCH_OUT"
TOKEN_FILE_ENV = "TOKEN_SPAREN_BENCH_TOKEN_FILE"
STAGES = ("screening", "confirmation")
STAGE_TAGS = {"screening": "v0.5.0-screen1", "confirmation": "v1.0.0-rc1"}
SHUFFLE_SEED = 0  # the seeded block permutation of `bench.scheduler.plan_order`
HEARTBEAT_S = 3 * 3600.0  # publish anchors when the last publication is this old (gap <= 5 h)
REAL_N_BOOT = 10000
DRY_N_BOOT = 200
DRY_N_SIM = 50
DRY_MODEL = "claude-sonnet-4-6"  # the fake agent echoes ``--model``; must be priced
DRY_CLAUDE_CODE_VERSION = "0.0.0-fake"  # `bench/fixtures/fake/agent.py` VERSION
DRY_SCREENING_ARMS = ("baseline", "concise", "token-sparen", "ts-style")
# deterministic agent failures of the dry run (so the statistics see discordant pairs)
DRY_FAILS = frozenset(
    {("concise", "fake__alpha-2"), ("baseline", "fake__beta-1"), ("token-sparen", "fake__beta-2")}
)
_FIXED = re.compile(r"^\+.*range\(1, n \+ 1\)", re.M)  # the fake task's fix
EXIT_USAGE = 2
EXIT_ANCHORS = 11
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


class CliError(Exception):
    """A refusal: printed as one line on stderr, exit ``code``."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- files


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_json(path: Path, doc: Any) -> None:
    _write_private(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CliError(f"no {what} in the workspace ({path.name}): {_HINTS.get(what, '')}") from exc
    except (OSError, ValueError) as exc:
        raise CliError(f"unreadable {what} ({path.name}): {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise CliError(f"malformed {what} ({path.name})")
    return doc


_HINTS = {
    "freeze record": "run `python -m bench freeze --stage S --ref TAG` first",
    "power result": "run `python -m bench power` after screening",
    "calibration": "run `python -m bench calibrate` first",
    "preflight": "run `python -m bench preflight` first",
    "toolchain pins": "build the toolchain first (docs/release/benchmark-runbook.md)",
    "gate result": "run `python -m bench gate` first",
}


# --------------------------------------------------------------------------- token


def read_token(path: Path | None) -> str:
    """The dedicated benchmark OAuth token, from a private file only (``--token-file`` or
    ``$TOKEN_SPAREN_BENCH_TOKEN_FILE``): owned by the user, mode 0600 (no group or other bits).
    The token in the environment is refused (it would reach every child process); its value is
    never printed."""
    if os.environ.get(TOKEN_ENV):
        raise CliError(
            f"{TOKEN_ENV} is set in the environment: unset it and give the token in a 0600 file "
            f"(--token-file or {TOKEN_FILE_ENV})"
        )
    if path is None:
        env = os.environ.get(TOKEN_FILE_ENV)
        path = Path(env) if env else None
    if path is None:
        raise CliError(f"a real run needs the token file (--token-file or {TOKEN_FILE_ENV})")
    # one open without following a link, then every check on that descriptor (no TOCTOU)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CliError("the token file cannot be opened (missing, unreadable or a link)") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CliError("the token file must be a regular file (not a link)")
        if os.name == "posix":
            if st.st_mode & 0o077:
                raise CliError("the token file must have mode 0600 (no group or other access)")
            if st.st_uid != os.getuid():
                raise CliError("the token file must be owned by the current user")
        raw = fh.read(4096)
    token = raw.decode("utf-8", errors="replace").strip()
    if not token or any(c.isspace() for c in token):
        raise CliError("the token file must hold exactly one token")
    return token


# --------------------------------------------------------------------------- workspace


@dataclass
class Workspace:
    root: Path
    meta: dict[str, Any]

    @property
    def dry_run(self) -> bool:
        return bool(self.meta.get("dry_run"))

    @property
    def campaign(self) -> str:
        campaign = self.meta.get("campaign")
        if not isinstance(campaign, str):
            raise CliError("no campaign in this workspace yet: `run --campaign ID` first")
        return campaign

    def bind_campaign(self, campaign: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", campaign):
            raise CliError("campaign ids are lowercase letters, digits and dashes")
        current = self.meta.get("campaign")
        if current is not None and current != campaign:
            raise CliError(f"this workspace holds campaign {current!r}, not {campaign!r}")
        if current is None:
            self.meta["campaign"] = campaign
            self.save()

    def save(self) -> None:
        _write_json(self.root / "workspace.json", self.meta)

    def stage_dir(self, stage: str) -> Path:
        return self.root / "stages" / stage

    def ledger_path(self, stage: str) -> Path:
        return self.stage_dir(stage) / "ledger.jsonl"

    def ledger(self, stage: str) -> Ledger:
        path = self.ledger_path(stage)
        if not path.is_file():
            raise CliError(f"no {stage} ledger in this workspace: run that stage first")
        return Ledger(path)

    @property
    def grading(self) -> Path:
        return self.root / "grading"

    @property
    def account(self) -> Path:
        """The quota account: the workspace's own scripted one for a dry run, else the real
        account-wide directory (`bench.quota.default_account_dir`)."""
        return self.root / "dry-account" if self.dry_run else default_account_dir()

    @property
    def n_boot(self) -> int:
        return DRY_N_BOOT if self.dry_run else REAL_N_BOOT


def _stage_arg(value: str) -> str:
    if value not in STAGES:
        raise argparse.ArgumentTypeError(f"stage must be one of {', '.join(STAGES)}")
    return value


def open_workspace(args: argparse.Namespace, *, dry_run: bool | None = None) -> Workspace:
    out = args.out or os.environ.get(OUT_ENV)
    if not out:
        raise CliError(f"--out DIR (or {OUT_ENV}) is required; there is no default workspace")
    if sc._HOST_OS == "nt" and dry_run is not True:  # refused before anything is written
        try:
            existing = json.loads((Path(out) / "workspace.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not (isinstance(existing, dict) and existing.get("dry_run") is True):
            _require_benchmark_host()
    try:
        root = check_workspace(Path(out))
    except IsolationError as exc:
        raise CliError(f"workspace refused: {exc}") from exc
    meta_path = root / "workspace.json"
    if meta_path.is_file():
        meta = _read_json(meta_path, "workspace record")
    else:
        meta = {"dry_run": bool(dry_run), "campaign": None}
        _write_json(meta_path, meta)
    ws = Workspace(root, meta)
    for name in ("stages", "frozen", "reports", "grading"):  # private before anything is put in
        (root / name).mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(root / name, 0o700)
    if dry_run is not None and ws.dry_run != dry_run:
        kind = "a dry run" if ws.dry_run else "a real campaign"
        raise CliError(f"this workspace is {kind}; use another --out")
    return ws


def _require_benchmark_host() -> None:
    if sc._HOST_OS == "nt":
        raise CliError("real benchmark campaigns run on macOS or Linux only (dry runs anywhere)")


# --------------------------------------------------------------------------- git (anchors)


def _git_env(*, isolated: bool, when: float | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    if isolated:  # the dry run: no user or system config, a fixed identity
        env |= {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "bench dry run",
            "GIT_AUTHOR_EMAIL": "bench@example.invalid",
            "GIT_COMMITTER_NAME": "bench dry run",
            "GIT_COMMITTER_EMAIL": "bench@example.invalid",
        }
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    return env


def _git(cwd: Path, *args: str, env: Mapping[str, str], check: bool = True) -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )
    if check and proc.returncode != 0:
        err = re.sub(r"://[^/@\s]*@", "://***@", proc.stderr.strip())[:200]
        raise CliError(f"git {args[0]} failed: {err}", EXIT_ANCHORS)
    return proc.stdout


def _git_run(cwd: Path, *args: str, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )


def _harness_commit() -> str:
    env = _git_env(isolated=True)
    return _git(REPO, "rev-parse", "HEAD", env=env).strip()


HARNESS_DIR = "harness"


def harness_problem(repo: Path, harness: Path, commit: object) -> str | None:
    """Why ``repo`` (the code running) is not the workspace's pinned harness (review I1): it
    must be the workspace's own clone at the recorded commit with no uncommitted change, so a
    multi-week campaign never runs from the owner's live checkout (a pull or an evidence
    commit there would change ``harness_commit`` mid-stage)."""
    if not isinstance(commit, str) or not (harness / ".git").exists():
        return "no pinned harness: run `python -m bench prepare harness` first"
    if repo.resolve() != harness.resolve():
        return f"run the bench from the workspace's harness clone: cd {harness} first"
    env = _git_env(isolated=True)
    head = _git_run(repo, "rev-parse", "HEAD", env=env).stdout.strip()
    if head != commit:
        return f"the harness is at {head[:12]}, pinned at {commit[:12]}"
    dirty = _git_run(repo, "status", "--porcelain", env=env).stdout.strip()
    if dirty:
        return "the harness has uncommitted changes; it must stay a clean checkout"
    return None


def _require_harness(ws: Workspace) -> None:
    if ws.dry_run:
        return
    problem = harness_problem(REPO, ws.root / HARNESS_DIR, ws.meta.get("harness_commit"))
    if problem:
        raise CliError(problem)


def _prepare_harness(ws: Workspace, ref: str, *, replace: bool) -> str:
    """A separate clean clone of the harness commit in the workspace (review I1); its
    ``origin`` points where the main checkout's does (anchors, stage tags)."""
    env = _git_env(isolated=True)
    proc = _git_run(REPO, "rev-parse", "--verify", f"{ref}^{{commit}}", env=env)
    if proc.returncode:
        raise CliError(f"not a commit: {ref!r}")
    commit = proc.stdout.strip()
    target = ws.root / HARNESS_DIR
    if target.exists():
        if ws.meta.get("harness_commit") == commit:
            return commit
        if not replace:
            raise CliError(
                f"a harness pinned at {str(ws.meta.get('harness_commit'))[:12]} exists; a stage "
                "ledger pins its harness commit (pass --replace only between stages)"
            )
        shutil.rmtree(target)
    _git(REPO, "clone", "-q", "--no-checkout", str(REPO), str(target), env=env)
    _git(target, "checkout", "-q", "--detach", commit, env=env)
    origin = _git_run(REPO, "remote", "get-url", "origin", env=_git_env(isolated=False))
    url = origin.stdout.strip()
    if origin.returncode == 0 and url:
        check_remote_url(url)
        _git(target, "remote", "set-url", "origin", url, env=env)
    ws.meta["harness_commit"] = commit
    ws.save()
    return commit


class Publisher:
    """Publishes a confirmation's local anchors (`bench.anchors.anchors_to_publish`) to the
    ``bench-anchors`` branch of ``remote``: the whole file at `bench.anchors.branch_path`, one
    commit per push, never a force push. Before publishing, a fresh anchor is recorded when the
    last local one no longer equals the current heads (or for a heartbeat, which must change
    the file to count as a publication); the first publication is the plan anchor as recorded."""

    def __init__(
        self, ws: Workspace, remote: str, *, isolated: bool, clock: Callable[[], float]
    ) -> None:
        self.ws, self.remote, self.isolated, self.clock = ws, remote, isolated, clock
        self.clone = ws.root / "anchors-git"

    def _env(self, when: float | None = None) -> dict[str, str]:
        return _git_env(isolated=self.isolated, when=when)

    def ensure_clone(self) -> None:
        env = self._env()
        if not (self.clone / ".git").is_dir():
            self.clone.mkdir(parents=True, exist_ok=True, mode=0o700)
            _git(self.clone, "init", "-q", env=env)
            _git(self.clone, "remote", "add", "origin", self.remote, env=env)
        self.fetch()
        _git(self.clone, "checkout", "-q", "-B", ANCHORS_BRANCH, self.ref, env=env)

    @property
    def ref(self) -> str:
        return f"origin/{ANCHORS_BRANCH}"

    def fetch(self) -> None:
        refspec = f"+refs/heads/{ANCHORS_BRANCH}:refs/remotes/origin/{ANCHORS_BRANCH}"
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.clone), "fetch", "-q", "--no-tags", "origin", refspec],  # noqa: S607
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        if proc.returncode != 0:
            raise CliError(
                f"cannot fetch the {ANCHORS_BRANCH} branch (create it from main and protect it "
                "first: docs/release/benchmark-runbook.md)",
                EXIT_ANCHORS,
            )

    def _path(self, campaign: str) -> Path:
        return self.clone / branch_path(campaign)

    def published_text(self, campaign: str) -> str:
        """The anchors file as the remote has it (the fetched ref, never the local clone)."""
        proc = _git_run(self.clone, "show", f"{self.ref}:{branch_path(campaign)}", env=self._env())
        return proc.stdout if proc.returncode == 0 else ""

    def last_published(self, campaign: str) -> float | None:
        """Commit time of the remote's last publication of the campaign's anchors."""
        out = _git(
            self.clone,
            "log",
            "-1",
            "--format=%ct",
            self.ref,
            "--",
            branch_path(campaign),
            env=self._env(),
        ).strip()
        return float(out) if out else None

    def pending(self) -> list[str]:
        """Local anchor commits the remote does not have yet, oldest first."""
        out = _git(self.clone, "rev-list", "--reverse", f"{self.ref}..HEAD", env=self._env())
        return out.split()

    def push_pending(self) -> int:
        """Retry-safe publication (review C1): push every local commit the remote lacks, in
        order, ONE commit per push (each gets its own server receipt); the last confirmed
        push is recorded in ``anchors-pushed.json``. A diverged remote is refused."""
        env = self._env()
        if _git_run(
            self.clone, "merge-base", "--is-ancestor", self.ref, "HEAD", env=env
        ).returncode:
            if _git_run(
                self.clone, "merge-base", "--is-ancestor", "HEAD", self.ref, env=env
            ).returncode:
                raise CliError(
                    f"the local anchors clone and {self.ref} diverged: inspect "
                    f"{self.clone} (never force-push)",
                    EXIT_ANCHORS,
                )
            _git(self.clone, "checkout", "-q", "-B", ANCHORS_BRANCH, self.ref, env=env)
            return 0
        pushed = 0
        for sha in self.pending():
            _git(self.clone, "push", "-q", "origin", f"{sha}:refs/heads/{ANCHORS_BRANCH}", env=env)
            _git(self.clone, "update-ref", f"refs/remotes/{self.ref}", sha, env=env)
            _write_json(self.ws.root / "anchors-pushed.json", {"commit": sha})
            pushed += 1
        return pushed

    def publish(self, ledger: Ledger, *, heartbeat: bool = False) -> bool:
        """Push what earlier attempts left un-pushed, then publish if anything changed (or
        ``heartbeat``); True when at least one commit was pushed."""
        campaign = ledger.campaign
        if not (self.clone / ".git").is_dir():
            self.ensure_clone()
        else:
            self.fetch()
        retried = self.push_pending() > 0
        published = self.published_text(campaign)
        local = anchors_to_publish(ledger)
        if not local:
            raise CliError("no local anchor: the confirmation plan must be written first")
        if published:
            heads = current_heads(ledger)
            stale = any(local[-1].get(k) != heads[k] for k in ANCHOR_KEYS)
            if stale or heartbeat:
                record_anchor(ledger, at=self.clock())
                local = anchors_to_publish(ledger)
        text = "".join(json.dumps(d, sort_keys=True) + "\n" for d in local)
        if text == published:
            return retried
        target = self._path(campaign)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        env = self._env(self.clock() if self.isolated else None)
        _git(self.clone, "add", "--", branch_path(campaign), env=env)
        msg = f"anchors: {campaign} ({len(local)} anchors)"
        _git(self.clone, "-c", "commit.gpgSign=false", "commit", "-q", "-m", msg, env=env)
        self.push_pending()  # exactly this commit; on failure it is retried next time, alone
        return True

    def beat(self, ledger: Ledger) -> None:
        """A heartbeat publication when the last one is `HEARTBEAT_S` old (never before the
        first, plan, publication)."""
        last = self.last_published(ledger.campaign)
        if last is not None and self.clock() - last >= HEARTBEAT_S:
            self.publish(ledger, heartbeat=True)


def _dry_remote(ws: Workspace) -> str:
    """A temporary bare repository with a ``bench-anchors`` branch (the dry run's remote)."""
    bare = ws.root / "dry-remote.git"
    if bare.is_dir():
        return str(bare)
    env = _git_env(isolated=True)
    seed = ws.root / "dry-remote-seed"
    for path in (bare, seed):
        path.mkdir(parents=True, mode=0o700)
    _git(bare, "init", "-q", "--bare", env=env)
    _git(seed, "init", "-q", env=env)
    _git(seed, "-c", "commit.gpgSign=false", "commit", "-q", "--allow-empty", "-m", "dry", env=env)
    _git(seed, "push", "-q", str(bare), f"HEAD:refs/heads/{ANCHORS_BRANCH}", env=env)
    shutil.rmtree(seed, ignore_errors=True)
    return str(bare)


def _publisher(ws: Workspace, clock: Callable[[], float], remote: str | None = None) -> Publisher:
    if ws.dry_run:
        return Publisher(ws, _dry_remote(ws), isolated=True, clock=clock)
    stored = ws.meta.get("anchors_remote")
    if stored is not None and remote is not None and remote != stored:
        raise CliError(f"this workspace publishes anchors to {stored}; --anchors-remote differs")
    url = stored or remote
    if not url:
        url = _git(REPO, "remote", "get-url", "origin", env=_git_env(isolated=False)).strip()
    check_remote_url(str(url))
    if stored is None:
        ws.meta["anchors_remote"] = url
        ws.save()
    return Publisher(ws, str(url), isolated=False, clock=clock)


_USERINFO = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@]*@", re.I)


def check_remote_url(url: str) -> None:
    """Refuse an anchors remote with embedded credentials (an ``https://user:token@`` URL
    would be stored in workspace.json and could echo in git errors); ssh's ``git@host:`` is
    fine."""
    if _USERINFO.match(url):
        raise CliError("the anchors remote URL carries credentials: use a credential helper or ssh")


def remote_matches(url: str, github_repo: str) -> bool:
    """True when ``url`` names the GitHub repository ``OWNER/NAME``."""
    pat = rf"github\.com[:/]{re.escape(github_repo)}(?:\.git)?/?$"
    return re.search(pat, url, re.I) is not None


# --------------------------------------------------------------------------- dry-run parts


class DryGrader:
    """The dry run's grader: a patch resolves the fake task iff it carries the fix (gold and
    agent patches alike; `bench.grade.NOOP_PATCH` never does)."""

    def __init__(self) -> None:
        self.last_reasons: dict[str, str] = {}

    def grade(
        self, preds: Path, run_id: str, instance_ids: Sequence[str]
    ) -> dict[str, bool | None]:
        rows = [json.loads(x) for x in preds.read_text(encoding="utf-8").splitlines() if x]
        patches = {str(r["instance_id"]): str(r["model_patch"]) for r in rows}
        return {i: bool(_FIXED.search(patches.get(i, ""))) for i in instance_ids}


def _dry_scenario(req: _RunRequest) -> str:
    return "fail" if (req.arm.name, req.task.instance_id) in DRY_FAILS else "success"


class VirtualClock:
    """The dry run's clock: one second per reading; sleeps advance it; persisted in the
    workspace so a resumed dry run continues where it stopped."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            saved = float(json.loads(path.read_text(encoding="utf-8"))["t"])
        except (OSError, ValueError, KeyError, TypeError):
            saved = 0.0
        self.t = max(time.time(), saved)

    def __call__(self) -> float:
        self.t += 1.0
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)
        self.save()

    def save(self) -> None:
        _write_json(self.path, {"t": self.t})


def _scripted_readings(account: Account, now: float) -> None:
    """The dry run's owner readings: well under both stops, a fresh five-hour window."""
    account.record_reading(QuotaReading(now, "owner", "weekly", 10.0, now + 7 * 86400.0), now=now)
    account.record_reading(
        QuotaReading(now, "owner", "five_hour", 10.0, now + FIVE_HOURS_S), now=now
    )


# --------------------------------------------------------------------------- rig


@dataclass
class Rig:
    """What a stage run needs, dry or real."""

    backend: ImageBackend
    grader: Grader
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    tasks: Mapping[str, Task]
    problems: Mapping[str, str]
    keep_images: frozenset[str]
    claude_code_version: str
    pins: Mapping[str, str]
    after_sleep: Callable[[float], None] = lambda _now: None
    virtual: VirtualClock | None = None
    gold_ok: Collection[str] = field(default_factory=frozenset)


def _dry_rig(ws: Workspace) -> Rig:
    clock = VirtualClock(ws.root / "dry-clock.json")
    account = Account(ws.account)
    environ = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    _dry_preflight(ws)
    return Rig(
        backend=FakeBackend(FAKE_DIR, scenario=_dry_scenario, environ=environ),
        grader=DryGrader(),
        clock=clock,
        sleep=clock.sleep,
        tasks={t.instance_id: t for t in FAKE_TASKS},
        problems={},
        keep_images=frozenset(),
        claude_code_version=DRY_CLAUDE_CODE_VERSION,
        pins={},
        after_sleep=lambda now: _scripted_readings(account, now),
        virtual=clock,
        gold_ok=preflight_ok(ws.grading),
    )


def _dry_preflight(ws: Workspace) -> None:
    if (ws.grading / "preflight.json").is_file():
        return
    gold = (FAKE_DIR / "fix.patch").read_text(encoding="utf-8").replace("\r\n", "\n")
    rows = [{"instance_id": t.instance_id, "patch": gold} for t in FAKE_TASKS]
    preflight(rows, DryGrader(), work=ws.grading)


def _real_grader(ws: Workspace) -> LocalSwebench:
    mode, _version = load_grade_mode()
    try:
        rows = load_dataset_rows(ws.root)
    except PoolError as exc:
        raise CliError(f"dataset: {exc} (run `python -m bench prepare dataset`)") from exc
    dataset = write_grading_dataset(rows.values(), ws.grading / "dataset.jsonl", mode)
    venv = ws.root / "grade-venv"
    if not (venv / "bin" / "python").exists():
        raise CliError("no grading venv (grade-venv): see docs/release/benchmark-runbook.md")
    return LocalSwebench(venv, dataset, ws.grading / "reports", mode, workspace=ws.root)


def _pins(ws: Workspace) -> dict[str, Any]:
    return _read_json(ws.root / "toolchain" / "pins.json", "toolchain pins")


def _real_rig(ws: Workspace, token_file: Path | None) -> Rig:
    _require_benchmark_host()
    pins = _pins(ws)
    version = pins.get("claude_code")
    if not isinstance(version, str) or not version:
        raise CliError("toolchain pins.json lacks the pinned claude_code version")
    token = read_token(token_file)
    environ = {k: v for k, v in os.environ.items() if k != TOKEN_ENV} | {TOKEN_ENV: token}
    backend = DockerBackend(ws.root / "toolchain", ws.root / "templates", environ=environ)
    try:
        rows = load_dataset_rows(ws.root)
    except PoolError as exc:
        raise CliError(f"dataset: {exc} (run `python -m bench prepare dataset`)") from exc
    pool = load_pool()
    raw = pins.get("arms")
    arms: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Rig(
        backend=backend,
        grader=_real_grader(ws),
        clock=time.time,
        sleep=time.sleep,
        tasks={t.instance_id: t for t in pool},
        problems={i: str(r.get("problem_statement", "")) for i, r in rows.items()},
        keep_images=frozenset(t.image for t in pool if t.mini),
        claude_code_version=version,
        pins={str(k): str(v) for k, v in arms.items()},
        gold_ok=_gold_ok(ws),
    )


def _gold_ok(ws: Workspace) -> set[str]:
    if not (ws.grading / "preflight.json").is_file():
        raise CliError(f"no preflight: {_HINTS['preflight']}")
    return preflight_ok(ws.grading)


def heartbeat_sleep(
    seconds: float, sleep: Callable[[float], None], beat: Callable[[], None]
) -> None:
    """Sleep in steps of at most `HEARTBEAT_S`, calling ``beat`` after each (pause ruling: while
    the scheduler is alive, including ``--wait`` pauses, a heartbeat anchor goes out at least
    every 4 h without any owner action)."""
    left = seconds
    while left > 0:
        step = min(left, HEARTBEAT_S)
        sleep(step)
        left -= step
        beat()


class _Heartbeat:
    """Delegates to the backend; a heartbeat anchor publication before each run."""

    def __init__(self, inner: ImageBackend, beat: Callable[[], None]) -> None:
        self.inner, self.beat = inner, beat

    def run(self, req: _RunRequest) -> RunOutcome:
        self.beat()
        return self.inner.run(req)

    def reap(self, campaign: str) -> int:
        return self.inner.reap(campaign)

    def ensure_image(self, image: str) -> None:
        self.inner.ensure_image(image)

    def remove_image(self, image: str) -> None:
        self.inner.remove_image(image)

    def free_disk_gb(self) -> float:
        return self.inner.free_disk_gb()


# --------------------------------------------------------------------------- freeze


def _plugin_version(path: Path) -> str:
    try:
        doc = json.loads((path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError("the frozen plugin has no readable .claude-plugin/plugin.json") from exc
    version = doc.get("version") if isinstance(doc, dict) else None
    if not isinstance(version, str) or not version:
        raise CliError("the frozen plugin's plugin.json has no version")
    return version


def _freeze_stage(ws: Workspace, stage: str, ref: str) -> dict[str, Any]:
    record = ws.root / "frozen" / f"{stage}.json"
    dest = ws.root / "frozen" / stage
    if not ws.dry_run:
        tag = ref.removeprefix("refs/tags/")
        if tag != STAGE_TAGS[stage]:
            raise CliError(
                f"the {stage} stage freezes the tag {STAGE_TAGS[stage]} only (plain tag name), "
                f"not {ref!r}"
            )
    if record.is_file():
        doc = _read_json(record, "freeze record")
        if doc.get("ref") != ref:
            raise CliError(f"{stage} is already frozen at {doc.get('ref')!r}")
        _load_frozen(ws, stage)  # verified
        return doc
    if not ws.dry_run:  # the pinned harness clone may predate the tag: fetch exactly that tag
        tag = ref.removeprefix("refs/tags/")
        env = _git_env(isolated=False)
        if _git_run(REPO, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", env=env).returncode:
            _git(
                REPO,
                "fetch",
                "-q",
                "--no-tags",
                "origin",
                f"refs/tags/{tag}:refs/tags/{tag}",
                env=env,
            )
    try:
        frozen = freeze(REPO, ref, dest, dry_run=ws.dry_run)
    except FreezeError as exc:
        raise CliError(f"freeze refused: {exc}") from exc
    doc = {
        "stage": stage,
        "ref": frozen.ref,
        "commit": frozen.commit,
        "plugin_hash": frozen.plugin_hash,
        "plugin_version": _plugin_version(dest),
        "dry_run": ws.dry_run,
    }
    _write_json(record, doc)
    return doc


def _load_frozen(ws: Workspace, stage: str) -> tuple[FrozenPlugin, dict[str, Any]]:
    doc = _read_json(ws.root / "frozen" / f"{stage}.json", "freeze record")
    frozen = FrozenPlugin(
        path=ws.root / "frozen" / stage,
        ref=str(doc["ref"]),
        commit=str(doc["commit"]),
        plugin_hash=str(doc["plugin_hash"]),
    )
    try:
        verify(frozen)
    except FrozenPluginChanged as exc:
        raise CliError(f"frozen plugin changed: {exc}", sc.EXIT_CODES["plugin_changed"]) from exc
    return frozen, doc


# --------------------------------------------------------------------------- stage runs


def _stage_tasks(ws: Workspace, rig: Rig) -> tuple[str, ...]:
    """Screening tasks: the Mini tasks of the pool (the fake tasks in a dry run) that passed
    the oracle preflight (a dropped task is listed in preflight.json and disclosed)."""
    if ws.dry_run:
        wanted: Sequence[str] = [t.instance_id for t in FAKE_TASKS]
    else:
        wanted = SCREENING.tasks
    ok = set(rig.gold_ok)
    if not (ws.grading / "preflight.json").is_file():
        raise CliError(f"no preflight: {_HINTS['preflight']}")
    return tuple(t for t in wanted if t in ok)


def _confirmation_plan(ws: Workspace, model: str) -> ConfirmationPlan:
    doc = _read_json(ws.root / "power.json", "power result")
    spec = doc["spec"]
    return ConfirmationPlan(
        spec=StageSpec(
            name="confirmation",
            arms=tuple(spec["arms"]),
            optional_arms=(),
            seeds=tuple(int(s) for s in spec["seeds"]),
            model=model,
            tasks=tuple(spec["tasks"]),
        ),
        verdict=doc["verdict"],
        power=PowerPlan(**doc["power"]),
        flip=FlipModel(**doc["flip"]),
        reasons=tuple(doc["reasons"]),
    )


def _stage_ledger(ws: Workspace, stage: str, header: Mapping[str, Any]) -> Ledger:
    try:
        return Ledger(ws.ledger_path(stage), header=header)
    except LedgerError as exc:
        hint = ""
        if "harness_commit" in str(exc):
            hint = (
                " (the stage was started from another harness commit: run it from the "
                "workspace's pinned harness clone, never the live checkout)"
            )
        raise CliError(f"{exc}{hint}") from exc


def _run_stage(
    ws: Workspace,
    stage: str,
    rig: Rig,
    *,
    model: str,
    arms_arg: str | None,
    wait: bool,
    price_confirmed_on: str | None,
    remote: str | None = None,
    plan_only: bool = False,
) -> int:
    """Run (or resume) one stage; a confirmation first records its plan and publishes the plan
    anchor (``plan_only``: only that, so the owner can commit the plan hash before any run)."""
    frozen, frec = _load_frozen(ws, stage)
    header: dict[str, Any] = {
        "campaign": ws.campaign,
        "stage": stage,
        "harness_commit": _harness_commit(),
        "plugin_hash": frozen.plugin_hash,
        "plugin_ref": frozen.ref,
        "pins": dict(rig.pins),
        "model": model,
        "claude_code_version": rig.claude_code_version,
        "plugin_version": frec["plugin_version"],
        "price_confirmed_on": price_confirmed_on,
    }
    if stage == "confirmation" and price_confirmed_on is None:
        raise CliError("confirmation needs --price-confirmed-on YYYY-MM-DD (live pricing check)")
    if price_confirmed_on is not None:
        try:
            dt.date.fromisoformat(price_confirmed_on)
        except ValueError as exc:
            raise CliError("--price-confirmed-on must be an ISO date") from exc
    registry = load_arms()
    publisher: Publisher | None = None
    if stage == "screening":
        task_ids = _stage_tasks(ws, rig)
        default = DRY_SCREENING_ARMS if ws.dry_run else SCREENING.arms
        arms = tuple(a for a in arms_arg.split(",") if a) if arms_arg else default
        seeds: tuple[int, ...] = (GATE_SEED,)
        unknown = [a for a in arms if a not in registry]
        if unknown or "baseline" not in arms or "token-sparen" not in arms:
            raise CliError(f"arms must be known and include baseline and token-sparen: {arms}")
    else:
        cplan = _confirmation_plan(ws, model)
        if len(cplan.spec.arms) * len(cplan.spec.seeds) > RUN_CAP:
            raise CliError(f"a confirmation block may hold at most {RUN_CAP} runs per anchor")
        ws.stage_dir(stage).mkdir(parents=True, exist_ok=True, mode=0o700)
        ledger = _stage_ledger(ws, stage, header)
        write_plan(
            ledger,
            cplan,
            plugin_version=str(frec["plugin_version"]),
            price_confirmed_on=str(price_confirmed_on),
        )
        plan = read_plan(ledger)
        if arms_arg and tuple(arms_arg.split(",")) != plan.arms:
            raise CliError(f"confirmation runs the planned arms {plan.arms} only")
        arms, seeds, task_ids = plan.arms, (plan.seed,), plan.tasks
        publisher = _publisher(ws, rig.clock, remote)
        publisher.publish(ledger)  # the plan anchor, before any run
        print(f"confirmation plan sha256 {plan.sha256} ({plan.verdict}, {len(plan.tasks)} tasks)")
        if plan_only:
            return 0
    if not task_ids:
        raise CliError(f"no {stage} task passed the preflight")
    ws.stage_dir(stage).mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = _stage_ledger(ws, stage, header)
    tasks = [rig.tasks[i] for i in task_ids]
    keys = sc.plan_order(
        streams(tasks), arms, seeds, campaign=ws.campaign, stage=stage, shuffle_seed=SHUFFLE_SEED
    )
    _write_json(ws.stage_dir(stage) / "keys.json", [k.id for k in keys])

    def beat() -> None:
        if publisher is not None:
            publisher.beat(ledger)

    def grader(ledger: Ledger, *, stage: str, instance_id: str, gold_ok: Collection[str]) -> None:
        if publisher is not None:
            publisher.publish(ledger)  # the anchors of the previous blocks and this block's runs
        grade_task(ledger, rig.grader, stage=stage, instance_id=instance_id, gold_ok=gold_ok)

    def sleep(seconds: float) -> None:
        heartbeat_sleep(seconds, rig.sleep, beat)
        rig.after_sleep(rig.clock())

    if rig.virtual is not None:
        rig.after_sleep(rig.clock())
    try:
        status = sc.run_campaign(
            keys,
            backend=_Heartbeat(rig.backend, beat),
            ledger=ledger,
            arms=registry,
            tasks={t.instance_id: t for t in tasks},
            workspace=ws.stage_dir(stage) / "work",
            frozen=frozen,
            policy=QuotaPolicy(),
            grader=grader,
            keep_images=rig.keep_images,
            clock=rig.clock,
            sleep=sleep,
            wait=wait,
            problems=rig.problems,
            account=ws.account,
            gold_ok=rig.gold_ok,
        )
    except sc.SchedulerBusy as exc:
        raise CliError(f"{exc} (one scheduler per account)") from exc
    finally:
        if rig.virtual is not None:
            rig.virtual.save()
    if publisher is not None:
        publisher.publish(ledger)  # the final heads
    print(f"{stage}: {status}")
    return sc.EXIT_CODES[status]


# --------------------------------------------------------------------------- grade / analyse


def _grade(ws: Workspace, stage: str, grader: Grader) -> Path:
    ledger = ws.ledger(stage)
    gold = preflight_ok(ws.grading) if (ws.grading / "preflight.json").is_file() else set()
    path = grade_stage(ledger, grader, stage=stage, gold_ok=gold)
    if stage == "confirmation":
        clock: Callable[[], float] = (
            VirtualClock(ws.root / "dry-clock.json") if ws.dry_run else time.time
        )
        _publisher(ws, clock).publish(ledger)
    return path


def _estimate(fn: Callable[..., Any], *args: Any, n_boot: int) -> dict[str, Any] | None:
    try:
        est = fn(*args, n_boot=n_boot, seed=0)
    except (ValueError, ZeroDivisionError):
        return None
    return {"point": est.point, "ci95": [est.lo, est.hi], "n": est.n, "method": est.method}


def analyse(ws: Workspace, stage: str) -> dict[str, Any]:
    summ: Summary = summarise_ledger(ws.ledger(stage), stage=stage)
    rows: dict[str, Any] = {}
    for arm in summ.arms:
        row: dict[str, Any] = {"resolved_rate": summ.resolved_rate(arm)}
        if arm != summ.reference:
            row["resolved_diff"] = _estimate(summ.resolved_diff, arm, n_boot=ws.n_boot)
            row["cost_ratio"] = _estimate(summ.cost_ratio, arm, n_boot=ws.n_boot)
        rows[arm] = row
    doc = {
        "stage": stage,
        "private": True,
        "n_boot": ws.n_boot,
        "arms": rows,
        "counts": dict(summ.counts),
        "exclusions": len(summ.exclusions),
    }
    _write_json(ws.stage_dir(stage) / "analysis.json", doc)
    return doc


# --------------------------------------------------------------------------- power


def _calibration(ws: Workspace) -> tuple[QuotaProjection, DiskProjection]:
    path = ws.root / "calibration.json"
    if ws.dry_run and not path.is_file():
        _write_json(
            path,
            {
                "quota": {"pct_per_run_weekly": 2.5, "runs_per_week": 4 * 7 * 24 / 5},
                "disk": {
                    "gb_per_image": 1.0,
                    "gb_kept": 0.0,
                    "free_gb": 1000.0,
                    "gb_per_run": 0.01,
                },
                "scripted": True,
            },
        )
    doc = _read_json(path, "calibration")
    try:
        return QuotaProjection(**doc["quota"]), DiskProjection(**doc["disk"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(f"malformed calibration: {exc}") from exc


def power_step(ws: Workspace) -> dict[str, Any]:
    screen = summarise_ledger(ws.ledger("screening"), stage="screening")
    if ws.dry_run:
        pool: list[Task] = list(FAKE_TASKS)
        kw: dict[str, Any] = {"n_sim": DRY_N_SIM, "n_boot": DRY_N_BOOT, "seed": 0}
    else:
        ok = preflight_ok(ws.grading)
        pool = [t for t in load_pool() if t.instance_id in ok]
        kw = {}
        cal = _read_json(ws.root / "calibration.json", "calibration")
        if cal.get("screening_last_end") != _last_end(ws, "screening"):
            raise CliError(  # review I3: size from the screening's observed usage and retries
                "calibration.json predates the screening's last run: run `python -m bench "
                "calibrate` again after screening, then `power`"
            )
    quota, disk = _calibration(ws)
    try:
        cp = plan_confirmation(
            screen, pool=pool, quota=quota, disk=disk, model="set-at-first-run", **kw
        )
    except ValueError as exc:
        raise CliError(f"power step: {exc}") from exc
    doc = {
        "spec": {
            "arms": list(cp.spec.arms),
            "seeds": list(cp.spec.seeds),
            "tasks": list(cp.spec.tasks),
        },
        "verdict": cp.verdict,
        "power": asdict(cp.power),
        "flip": asdict(cp.flip),
        "reasons": list(cp.reasons),
        "weeks_needed": quota.weeks_needed(cp.spec.runs),
        "runs": cp.spec.runs,
    }
    path = ws.root / "power.json"
    if path.is_file():
        old = _read_json(path, "power result")
        same = all(old.get(k) == doc[k] for k in ("spec", "verdict", "power", "reasons"))
        planned = ws.ledger_path("confirmation").is_file() and any(
            r.get("kind") == "plan" for r in read_jsonl(ws.ledger_path("confirmation"))
        )
        if not same and planned:
            raise CliError("the confirmation plan is recorded; the power result cannot change")
    _write_json(path, doc)
    return doc


# --------------------------------------------------------------------------- gate / report


def _check_plan_file(ws: Workspace, evidence_repo: Path | None) -> None:
    """The plan hash committed in the main checkout (``docs/evidence/plan.sha256``) must equal
    the recorded plan's before any confirmation run and at the gate (review M6)."""
    if evidence_repo is None:
        raise CliError(
            "--evidence-repo PATH (the main checkout holding docs/evidence/plan.sha256) is "
            "required: commit the plan hash printed by `run --plan-only` there first"
        )
    try:
        plan = read_plan(ws.ledger("confirmation"))
    except PlanError as exc:
        raise CliError(f"confirmation plan: {exc} (run --plan-only first)") from exc
    problems = check_plan_file(evidence_repo / PLAN_FILE, plan.sha256)
    if problems:
        raise CliError(f"{problems[0][1]}: commit {plan.sha256} there (signed) first")


def _last_end(ws: Workspace, stage: str) -> float | None:
    if not ws.ledger_path(stage).is_file():
        return None
    ats = [float(r.get("at", 0.0)) for r in ws.ledger(stage).iter_kind("end")]
    return max(ats) if ats else None


def _e2_ok() -> bool:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "evals" / "e2" / "run.py"), "--strict"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _gate_doc(res: GateResult, receipts: str) -> dict[str, Any]:
    return {
        "verdict": res.verdict,
        "reasons": list(res.reasons),
        "plan_sha256": res.plan_sha256,
        "plugin_hash": res.plugin_hash,
        "plugin_version": res.plugin_version,
        "model": res.model,
        "claude_code_version": res.claude_code_version,
        "price_confirmed_on": res.price_confirmed_on,
        "ledger_chain_head": res.ledger_chain_head,
        "grades_chain_head": res.grades_chain_head,
        "advisory": res.advisory,
        "receipts": receipts,
        "details": list(res.details),
    }


def gate_step(
    ws: Workspace,
    *,
    release_hash: str | None,
    github_repo: str | None,
    evidence_repo: Path | None = None,
) -> tuple[GateResult, dict[str, Any]]:
    """The release gate on the confirmation ledger, after fetching ``bench-anchors``. With
    ``github_repo`` the push receipts come from the GitHub API (``GH_TOKEN`` if set; what CI
    does) and the result is authoritative; without, it is advisory. A real gate also checks
    the committed plan hash (``--evidence-repo``) and that the anchors remote is
    ``github_repo``. A dry run prints the advisory gate, then gates again with receipts
    simulated from its temporary remote (the fake server), which is the stored result."""
    ledger = ws.ledger("confirmation")
    _, frec = _load_frozen(ws, "confirmation")
    rhash = release_hash or str(frec["plugin_hash"])
    clock: Callable[[], float] = time.time
    pub = _publisher(ws, clock)
    if not ws.dry_run:
        _require_harness(ws)
        _check_plan_file(ws, evidence_repo)
        if github_repo and not remote_matches(pub.remote, github_repo):
            raise CliError(f"the anchors remote is not github.com/{github_repo}")
    if not (pub.clone / ".git").is_dir():
        pub.ensure_clone()
    pub.fetch()
    e2 = _e2_ok()
    kw: dict[str, Any] = {
        "ledger": ledger,
        "repo": pub.clone,
        "release_hash": rhash,
        "e2_ok": e2,
        "n_boot": ws.n_boot,
        "anchors_ref": pub.ref,
    }
    advisory = run_gate(**kw)
    print(f"gate (local, {advisory.release_verdict}): {advisory.verdict}")
    for line in advisory.details:
        print(f"  {line}")
    if ws.dry_run:
        pubs = read_published(pub.clone, pub.ref, ledger.campaign)
        res = run_gate(**kw, receipts=[(p.commit, float(p.at)) for p in pubs])
        doc = _gate_doc(res, "simulated")
        print(f"gate (dry run, simulated receipts; not a release verdict): {res.verdict}")
    elif github_repo:
        receipts = fetch_receipts(github_repo, os.environ.get("GH_TOKEN"))
        res = run_gate(**kw, receipts=receipts)
        doc = _gate_doc(res, "github")
        print(f"gate (server receipts): {res.verdict}")
    else:
        res, doc = advisory, _gate_doc(advisory, "none")
    _write_json(ws.stage_dir("confirmation") / "gate.json", doc)
    return res, doc


@dataclass(frozen=True)
class StoredGate:
    """A gate result read back from ``gate.json`` (what `bench.report.build_report` reads)."""

    verdict: str
    reasons: list[str]
    plugin_hash: str
    model: str
    claude_code_version: str
    plan_sha256: str | None
    ledger_chain_head: str | None
    grades_chain_head: str | None
    advisory: bool


def _stored_gate(ws: Workspace, ledger: Ledger) -> StoredGate:
    doc = _read_json(ws.stage_dir("confirmation") / "gate.json", "gate result")
    if doc.get("advisory") is not False:
        raise CliError(
            "the gate result is advisory (no server receipts): run "
            "`python -m bench gate --github-repo OWNER/NAME` (what the release job does)"
        )
    if doc.get("ledger_chain_head") != ledger.chain_head() or doc.get(
        "grades_chain_head"
    ) != grades_chain_head(ledger.path.with_name("grades.jsonl")):
        raise CliError("the gate result is stale (the ledger changed since): run gate again")
    return StoredGate(
        verdict=str(doc["verdict"]),
        reasons=[str(r) for r in doc["reasons"]],
        plugin_hash=str(doc.get("plugin_hash")),
        model=str(doc.get("model")),
        claude_code_version=str(doc.get("claude_code_version")),
        plan_sha256=doc.get("plan_sha256"),
        ledger_chain_head=doc.get("ledger_chain_head"),
        grades_chain_head=doc.get("grades_chain_head"),
        advisory=False,
    )


def report_step(ws: Workspace, stage: str, *, publish: bool, repo: Path | None = None) -> Path:
    if publish and ws.dry_run:
        raise CliError("a dry run never publishes (synthetic tasks, small bootstrap)")
    ledger = ws.ledger(stage)
    gate: StoredGate | None = None
    power: Mapping[str, Any] | None = None
    style: str | None = None
    if stage == "confirmation":
        gate = _stored_gate(ws, ledger)
        try:
            plan = read_plan(ledger)
        except PlanError as exc:
            raise CliError(f"confirmation plan: {exc}") from exc
        power = dict(plan.power)
        if STYLE_ARM in plan.arms:
            style = style_rule(ledger=ledger, n_boot=ws.n_boot)
        if publish and not plan.price_confirmed_on:
            raise CliError("publishing needs the live-pricing date in the plan")
    pre = ws.grading / "preflight.json"
    out = ws.root / "reports" / stage
    built = build_report(
        ledger.path,
        ledger.path.with_name("grades.jsonl"),
        stage=stage,
        out_dir=out,
        gate=gate,
        power=power,
        n_boot=ws.n_boot,
        style_rule=style,
        preflight=pre if pre.is_file() else None,
    )
    print(f"{stage} report written ({len(list(built.rglob('*')))} files, private)")
    if publish:
        if repo is None or repo.resolve() == (ws.root / HARNESS_DIR).resolve():
            raise CliError("--publish needs --repo PATH: the main checkout (never the harness)")
        written = publish_report(built, repo)
        for path in written:
            print(f"published {path.relative_to(repo).as_posix()}")
    return built


# --------------------------------------------------------------------------- status


def _status_lines(ws: Workspace) -> Iterator[str]:
    yield f"workspace: {'dry run' if ws.dry_run else 'real'}; campaign {ws.meta.get('campaign')}"
    for stage in STAGES:
        path = ws.ledger_path(stage)
        if not path.is_file():
            yield f"{stage}: not started"
            continue
        ledger = Ledger(path)
        finals = ledger.finals(stage=stage)
        counts: dict[str, dict[str, int]] = {}
        for rec in ledger.iter_kind("end"):
            try:
                arm = RunKey.from_id(str(rec.get("key"))).arm
            except ValueError:
                continue
            status = str(rec.get("status")) + (f"/{rec['sub']}" if rec.get("sub") else "")
            per = counts.setdefault(arm, {})
            per[status] = per.get(status, 0) + 1
        yield f"{stage}: {len(finals)} final runs"
        for arm in sorted(counts):
            parts = ", ".join(f"{k} {v}" for k, v in sorted(counts[arm].items()))
            yield f"  {arm}: {parts}"
        keys_path = ws.stage_dir(stage) / "keys.json"
        pending = None
        if keys_path.is_file():
            for kid in json.loads(keys_path.read_text(encoding="utf-8")):
                key = RunKey.from_id(str(kid))
                if ledger.final(key) is None and not ledger.unrunnable(key):
                    pending = key.id
                    break
        yield f"  next key: {pending or 'none'}"
        pauses = list(ledger.iter_kind("pause"))
        last = pauses[-1] if pauses else None
        breaker = "tripped" if last and last.get("reason") == "breaker" else "ok"
        yield f"  last pause: {last.get('reason') if last else 'none'}; breaker {breaker}"
    readings = Account(ws.account).readings()
    for kind in ("weekly", "five_hour"):
        latest = max((r for r in readings if r.kind == kind), key=lambda r: r.at, default=None)
        if latest is None:
            yield f"quota {kind}: no reading"
        else:
            when = dt.datetime.fromtimestamp(latest.at, dt.UTC).isoformat(timespec="minutes")
            yield f"quota {kind}: {latest.utilization_pct} ({latest.source}, {when})"


# --------------------------------------------------------------------------- commands


def cmd_prepare(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    what = args.what
    if ws.dry_run:
        print(f"prepare {what}: nothing to do in a dry run")
        return 0
    _require_benchmark_host()
    if what == "harness":
        commit = _prepare_harness(ws, args.ref, replace=args.replace)
        print(f"harness pinned at {commit[:12]}: run the bench from {ws.root / HARNESS_DIR}")
        return 0
    if what == "dataset":
        from bench.data.fetch_pool import cmd_dataset

        return cmd_dataset(ws.root)
    if what == "images":  # no token, no preflight needed (review I2)
        environ = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
        backend = DockerBackend(ws.root / "toolchain", ws.root / "templates", environ=environ)
        for task in [t for t in load_pool() if t.mini]:
            backend.ensure_image(task.image)
        print("Mini images present")
        return 0
    if what == "toolchain":
        pins = _pins(ws)
        venv = ws.root / "grade-venv" / "bin" / "python"
        missing = [k for k in ("claude_code", "arms") if k not in pins]
        if missing or not venv.exists():
            raise CliError(f"toolchain incomplete: {', '.join(missing) or 'grade-venv'}")
        print("toolchain present (pins.json and grade-venv)")
        return 0
    arms = [a for a in load_arms() if not (ws.root / "templates" / a).is_dir()]
    if arms:
        raise CliError(f"templates missing for: {', '.join(arms)}")
    print("templates present for every arm")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    if ws.dry_run:
        _dry_preflight(ws)
    else:
        _require_benchmark_host()
        _require_harness(ws)
        grader = _real_grader(ws)
        rows = load_dataset_rows(ws.root)
        ids = {t.instance_id for t in load_pool() if args.tasks == "pool" or t.mini}
        from token_sparen.store.jsonstore import locked

        with locked(ws.root / "bench", timeout=sc.BENCH_LOCK_TIMEOUT_S):
            preflight([r for i, r in rows.items() if i in ids], grader, work=ws.grading)
    ok = preflight_ok(ws.grading)
    print(f"preflight: {len(ok)} task(s) ok")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    doc = _freeze_stage(ws, args.stage, args.ref)
    print(f"{args.stage} frozen at {doc['ref']} (plugin {doc['plugin_hash'][:12]})")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    if ws.dry_run:
        _calibration(ws)
        print("calibration: scripted (dry run)")
        return 0
    account = Account(ws.account)
    policy = QuotaPolicy()
    pct = args.pct_per_run or per_run_weekly_pct(account.readings(), account.starts(), policy)
    runs = args.runs_per_week or policy.max_runs_per_window * 7 * 24 * 3600 / FIVE_HOURS_S
    retry = args.retry_rate
    if retry is None:
        retry = 0.0
        if ws.ledger_path("screening").is_file():
            led = ws.ledger("screening")
            ends = sum(1 for _ in led.iter_kind("end"))
            finals = len(led.finals(stage="screening"))
            retry = max(0.0, ends / finals - 1) if finals else 0.0
    last_end = _last_end(ws, "screening")
    disk = [args.gb_per_image, args.gb_kept, args.gb_per_run]
    if any(v is None for v in disk):
        raise CliError("calibrate needs --gb-per-image, --gb-kept and --gb-per-run (no zero)")
    free = args.free_gb
    if free is None:
        _require_benchmark_host()
        free = DockerBackend(ws.root / "toolchain", ws.root / "templates").free_disk_gb()
    doc = {
        "quota": {"pct_per_run_weekly": pct, "runs_per_week": runs, "retry_rate": retry},
        "disk": {
            "gb_per_image": args.gb_per_image,
            "gb_kept": args.gb_kept,
            "free_gb": free,
            "gb_per_run": args.gb_per_run,
        },
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "screening_last_end": last_end,  # power refuses a calibration older than screening
    }
    _write_json(ws.root / "calibration.json", doc)
    _calibration(ws)  # validated
    print("calibration written")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    dry = bool(args.dry_run)
    if not dry:
        _require_benchmark_host()
        if not args.stage:
            raise CliError("--stage is required (the whole pipeline runs only with --dry-run)")
        if not args.model:
            raise CliError("--model is required: the resolved model id the stage pins")
    ws = open_workspace(args, dry_run=dry)
    ws.bind_campaign(args.campaign)
    if not dry:
        _load_frozen(ws, args.stage)  # refuse before any token or Docker use
        _require_harness(ws)
        if args.stage == "confirmation" and not args.plan_only:
            _check_plan_file(ws, args.evidence_repo)
        rig = _real_rig(ws, args.token_file)
        return _run_stage(
            ws,
            args.stage,
            rig,
            model=args.model,
            arms_arg=args.arms,
            wait=args.wait,
            price_confirmed_on=args.price_confirmed_on,
            plan_only=args.plan_only,
            remote=args.anchors_remote,
        )
    return _dry_pipeline(ws, [args.stage] if args.stage else list(STAGES), args.arms)


def _dry_pipeline(ws: Workspace, stages: Sequence[str], arms: str | None) -> int:
    rig = _dry_rig(ws)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    for stage in stages:
        _freeze_stage(ws, stage, "HEAD")
        if stage == "confirmation" and not (ws.root / "power.json").is_file():
            power_step(ws)
        rc = _run_stage(
            ws,
            stage,
            rig,
            model=DRY_MODEL,
            arms_arg=arms if stage == "screening" else None,
            wait=True,
            price_confirmed_on=today if stage == "confirmation" else None,
        )
        if rc:
            return rc
        _grade(ws, stage, rig.grader)
        analyse(ws, stage)
        if stage == "screening":
            doc = power_step(ws)
            print(f"power: {doc['verdict']} ({len(doc['spec']['tasks'])} tasks)")
    if "confirmation" in stages:
        gate_step(ws, release_hash=None, github_repo=None)
    for stage in stages:
        report_step(ws, stage, publish=False)
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    now = time.time()
    for pct in (args.weekly, args.five_hour):
        if not 0.0 <= pct <= 100.0:
            raise CliError("readings are percentages between 0 and 100")
    account = Account(ws.account)

    def at(text: str | None) -> float | None:
        if text is None:
            return None
        try:
            when = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise CliError(f"not an ISO timestamp: {text!r}") from exc
        if when.tzinfo is None:
            when = when.astimezone()
        return when.timestamp()

    try:
        account.record_reading(
            QuotaReading(now, "owner", "weekly", args.weekly, at(args.weekly_resets)), now=now
        )
        account.record_reading(
            QuotaReading(now, "owner", "five_hour", args.five_hour, at(args.five_hour_resets)),
            now=now,
        )
    except ValueError as exc:
        raise CliError(f"reading refused: {exc}") from exc
    stamp = dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="seconds")
    print(f"owner readings recorded at {stamp}: weekly {args.weekly}, five-hour {args.five_hour}")
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    if args.key:
        try:
            key = RunKey.from_id(args.key)
        except ValueError as exc:
            raise CliError(f"bad key: {exc}") from exc
        ws.ledger(key.stage).invalidate(key, reason=args.reason)
    else:
        if not args.stage or not args.since:
            raise CliError("invalidate needs --key ID, or --stage S with --since ISO")
        try:
            start = dt.datetime.fromisoformat(args.since).timestamp()
        except ValueError as exc:
            raise CliError("--since must be an ISO timestamp") from exc
        ws.ledger(args.stage).invalidate(None, reason=args.reason, start=start, end=time.time())
    print("invalidated")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    for line in _status_lines(ws):
        print(line)
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    grader: Grader = DryGrader() if ws.dry_run else _real_grader(ws)
    _require_harness(ws)
    _grade(ws, args.stage, grader)
    print(f"{args.stage}: graded")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    doc = analyse(ws, args.stage)
    print(f"{args.stage}: analysis written ({len(doc['arms'])} arms; private, not for publication)")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    doc = power_step(ws)
    print(f"power: {doc['verdict']}; confirmation spec written (power.json)")
    for reason in doc["reasons"]:
        print(f"  {reason}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    res, _ = gate_step(
        ws,
        release_hash=args.release_hash,
        github_repo=args.github_repo,
        evidence_repo=args.evidence_repo,
    )
    if ws.dry_run or res.verdict != "failed":
        return 0
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    report_step(ws, args.stage, publish=args.publish, repo=args.repo)
    return 0


def cmd_anchors(args: argparse.Namespace) -> int:
    """``anchors publish``: a heartbeat publication (during a pause of the confirmation)."""
    ws = open_workspace(args)
    ledger = ws.ledger("confirmation")
    pushed = _publisher(ws, time.time).publish(ledger, heartbeat=True)
    print("anchors published" if pushed else "anchors unchanged")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, help=f"the bench workspace (or ${OUT_ENV})")
    common.add_argument(
        "--token-file", type=Path, help=f"0600 file with the OAuth token (or ${TOKEN_FILE_ENV})"
    )
    parser = argparse.ArgumentParser(prog="python -m bench", description="E4 benchmark (M5)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", parents=[common], help="check or fetch the prerequisites")
    p.add_argument("what", choices=("harness", "toolchain", "templates", "images", "dataset"))
    p.add_argument("--ref", default="HEAD", help="harness: the commit to pin (default HEAD)")
    p.add_argument("--replace", action="store_true", help="harness: replace (between stages)")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("preflight", parents=[common], help="oracle check of the gold patches")
    p.add_argument("--tasks", choices=("mini", "pool"), default="pool")
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("freeze", parents=[common], help="freeze the plugin of a stage's tag")
    p.add_argument("--stage", type=_stage_arg, required=True)
    p.add_argument("--ref", required=True)
    p.set_defaults(fn=cmd_freeze)

    p = sub.add_parser("calibrate", parents=[common], help="quota and disk projections")
    for name in ("pct-per-run", "runs-per-week", "retry-rate", "gb-per-image", "gb-kept"):
        p.add_argument(f"--{name}", type=float)
    p.add_argument("--gb-per-run", type=float)
    p.add_argument("--free-gb", type=float)
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("run", parents=[common], help="run (or resume) a stage")
    p.add_argument("--stage", type=_stage_arg)
    p.add_argument("--campaign", required=True)
    p.add_argument("--arms")
    p.add_argument("--model", help="the resolved model id the stage pins")
    p.add_argument("--price-confirmed-on", help="date of the live-pricing check (confirmation)")
    p.add_argument("--anchors-remote", help="the anchors remote (default: the repo's origin)")
    p.add_argument("--evidence-repo", type=Path, help="the main checkout (plan.sha256)")
    p.add_argument("--wait", action="store_true", help="sleep through five-hour waits")
    p.add_argument(
        "--plan-only", action="store_true", help="confirmation: record and anchor the plan only"
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("quota", parents=[common], help="the owner's quota readings")
    qsub = p.add_subparsers(dest="quota_cmd", required=True)
    q = qsub.add_parser("set", parents=[common])
    q.add_argument("--weekly", type=float, required=True)
    q.add_argument("--five-hour", type=float, required=True)
    q.add_argument("--weekly-resets")
    q.add_argument("--five-hour-resets")
    q.set_defaults(fn=cmd_quota)

    p = sub.add_parser("invalidate", parents=[common], help="reopen a key or a time range")
    p.add_argument("--key")
    p.add_argument("--stage", type=_stage_arg)
    p.add_argument("--since")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_invalidate)

    p = sub.add_parser("status", parents=[common], help="progress, quota and breaker (no secrets)")
    p.set_defaults(fn=cmd_status)

    for name, fn, helptext in (
        ("grade", cmd_grade, "grade the stage's final runs"),
        ("analyse", cmd_analyse, "private analysis of a stage"),
    ):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--stage", type=_stage_arg, required=True)
        p.set_defaults(fn=fn)

    p = sub.add_parser("power", parents=[common], help="size the confirmation")
    p.set_defaults(fn=cmd_power)

    p = sub.add_parser("gate", parents=[common], help="the release gate (confirmation)")
    p.add_argument("--release-hash")
    p.add_argument("--github-repo", help="OWNER/NAME: fetch server receipts (authoritative)")
    p.add_argument("--evidence-repo", type=Path, help="the main checkout (plan.sha256)")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("report", parents=[common], help="e4.json, report and figures")
    p.add_argument("--stage", type=_stage_arg, required=True)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--repo", type=Path, help="--publish: the main checkout to write into")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("anchors", parents=[common], help="publish the confirmation anchors")
    p.add_argument("action", choices=("publish",))
    p.set_defaults(fn=cmd_anchors)
    return parser


_REFUSALS = (
    LedgerError,
    PlanError,
    GateRefused,
    ReportError,
    AnchorError,
    PoolError,
    BackendError,
    sc.UnsupportedHost,
)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.fn(args)
    except CliError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return exc.code
    except _REFUSALS as exc:
        print(f"bench: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return code


__all__ = ["CliError", "main", "read_token"]
