"""The resumable E4 scheduler (M5 Task 7): one container at a time, idempotent across crashes.

Order (ruling M5-N2): `plan_order` gives one block per task, every (arm, seed) of that task back
to back in a seeded permutation; blocks go by commit position (task k of every repository
before task k + 1), so each stream (stage, arm, seed, repository) runs in commit order. In a
block the pending keys are retried round-robin; after every key of the block is final or
unrunnable the task is graded (Task 8's ``grade_task``), then its image is removed unless
``keep_images`` holds it, then a ``graded`` record is appended.

Before every run: the frozen plugin is re-verified (`bench.freeze.verify`; a mismatch with the
ledger header or the copy → ``plugin_changed``, no run), the free disk must be >= 30 GB (else a
``pause`` record with ``infra_fail/disk`` and ``paused_disk``; never a breaker count) and the
quota decision must say go (`bench.quota.decide`: weekly → stop; five-hour / window cap → sleep
until the reset with ``wait``, else pause). The disk floor is checked again before grading.

Typed outcomes: ``ok`` / ``agent_fail`` are final and their learner state is promoted to the
stream; ``infra_fail`` is never final and never promoted. ``infra_fail/{load,api,proxy,docker,
unknown}`` is retried up to `bench.ledger.MAX_INFRA_ATTEMPTS` per key, then the key is
unrunnable (its task leaves every pairing: `bench.stats.summarise` excludes it);
``infra_fail/rate`` re-queues without an attempt (the quota check then waits or stops; a rate
failure without a usable reading is recorded as a rejected limit of unknown kind: fail safe);
``infra_fail/auth`` and ``infra_fail/leak`` stop at once. `Breaker`: 3 consecutive counted
infra failures pause the campaign. The breaker starts fresh on every invocation (the owner
resumes after looking).

Idempotent resume: the ledger ``end`` record is the commit point. A crash before it reruns the
key with a new attempt (fresh container name and run dir) from the stream's promoted state; a
crash after it re-derives the promotion from the run's own ``state-out`` (`promote_state` is
idempotent; the stream keeps only its last promoted state); a final key is never rerun; a
graded block is not graded again (grading itself is idempotent too).

Locks: ``<workspace>/scheduler.lock`` for the whole campaign (a second scheduler is refused),
``<workspace>/bench.lock`` only around each agent container (grading takes it itself, in the
same process, after the run released it; it never runs while an agent container runs).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random
from typing import Any, Literal, Protocol

from bench.arms import Arm
from bench.backends import (
    BackendError,
    ImageBackend,
    RunOutcome,
    RunRequest,
    container_name,
    copy_state_out,
    release_image,
)
from bench.classify import RunStatus
from bench.freeze import FrozenPlugin, FrozenPluginChanged, verify
from bench.ledger import Ledger, LedgerError, RunKey, sha256_text
from bench.quota import (
    UTILIZATION_UNIT,
    QuotaPolicy,
    QuotaReading,
    Unit,
    decide,
    reading_from_rate_limit,
    reading_from_record,
    reading_record,
    window_bounds,
)
from bench.streamjson import run_cost
from bench.tasks import Task
from token_sparen.store.jsonstore import StoreBusy, locked

CampaignStatus = Literal[
    "complete",
    "paused_five_hour",
    "paused_weekly",
    "paused_breaker",
    "paused_disk",
    "stopped_auth",
    "stopped_leak",
    "plugin_changed",
    "incomplete",
]
EXIT_CODES: dict[CampaignStatus, int] = {
    "complete": 0,
    "paused_weekly": 3,
    "paused_five_hour": 4,
    "incomplete": 5,
    "paused_breaker": 6,
    "stopped_auth": 7,
    "stopped_leak": 8,
    "plugin_changed": 9,
    "paused_disk": 10,
}
DISK_FLOOR_GB = 30.0
SCHEDULER_LOCK = "scheduler"  # `locked` adds ".lock"
BENCH_LOCK = "bench"  # the shared one-container lock `bench.lock` (grading takes it too)
BENCH_LOCK_TIMEOUT_S = 6 * 3600.0
WAIT_MARGIN_S = 60.0
BREAKER_EXEMPT = frozenset({"rate", "disk"})
_STOP_AT_ONCE: dict[str, CampaignStatus] = {"auth": "stopped_auth", "leak": "stopped_leak"}


class SchedulerBusy(RuntimeError):
    """Another scheduler holds this workspace."""


class TaskGrader(Protocol):
    """Grades one task's final runs (Task 8: ``functools.partial(grade_task, grader=g)``)."""

    def __call__(self, ledger: Ledger, *, stage: str, instance_id: str) -> None: ...


@dataclass(frozen=True)
class RunSettings:
    effort: str = "high"
    max_turns: int = 80
    timeout_s: int = 3600


class Breaker:
    """Trips after ``limit`` consecutive infra failures (``rate`` and ``disk`` excepted)."""

    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self.count = 0

    def record(self, status: RunStatus) -> bool:
        kind, sub = status
        if kind != "infra_fail":
            self.count = 0
        elif sub not in BREAKER_EXEMPT:
            self.count += 1
        return self.count >= self.limit


def plan_order(
    streams: Mapping[str, Sequence[Task]],
    arms: Sequence[str],
    seeds: Sequence[int],
    *,
    campaign: str,
    stage: str,
    shuffle_seed: int,
) -> list[RunKey]:
    """Per commit position k, per repository (name order): the block of task k = every
    (arm, seed) in a permutation seeded by ``shuffle_seed`` and the task."""
    keys: list[RunKey] = []
    depth = max((len(tasks) for tasks in streams.values()), default=0)
    for k in range(depth):
        for repo in sorted(streams):
            if k >= len(streams[repo]):
                continue
            task = streams[repo][k]
            pairs = [(arm, seed) for arm in arms for seed in seeds]
            Random(f"{shuffle_seed}:{stage}:{task.instance_id}").shuffle(pairs)
            keys += [RunKey(campaign, stage, arm, seed, task.instance_id) for arm, seed in pairs]
    return keys


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def stream_dir(workspace: Path, key: RunKey, repo: str) -> Path:
    sid = _short("/".join((key.campaign, key.stage, key.arm, str(key.seed), repo)))
    return workspace / "streams" / key.stage / sid


def promote_state(
    workspace: Path, *, key: RunKey, repo: str, arm: Arm, source: Path, ledger: Ledger
) -> Path:
    """The stream's state after ``key`` (a final run): ``arm.state_paths`` copied from the
    run's ``state-out`` into a temp dir, renamed atomically to ``streams/…/<key>``; the
    stream's other states are removed. Idempotent: an existing promotion is returned."""
    base = _private_dir(stream_dir(workspace, key, repo))
    dest = base / _short(key.id)
    if dest.is_dir():
        return dest
    if not source.is_dir():
        raise LedgerError(f"state of final run {key.id} is gone; cannot promote it")
    tmp = base / f".tmp-{_short(key.id)}-{os.getpid()}-{secrets.token_hex(4)}"
    copy_state_out(source, arm.state_paths, tmp)
    os.replace(tmp, dest)
    for other in base.iterdir():
        if other != dest:
            shutil.rmtree(other, ignore_errors=True)
    ledger.append({"kind": "promote", "stream": base.name, "key": key.id})
    return dest


class _Stop(Exception):
    def __init__(self, status: CampaignStatus) -> None:
        super().__init__(status)
        self.status = status


class _Campaign:
    def __init__(
        self,
        keys: Sequence[RunKey],
        *,
        backend: ImageBackend,
        ledger: Ledger,
        arms: Mapping[str, Arm],
        tasks: Mapping[str, Task],
        workspace: Path,
        frozen: FrozenPlugin | None,
        policy: QuotaPolicy,
        grader: TaskGrader,
        keep_images: frozenset[str],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        wait: bool,
        settings: RunSettings,
        problems: Mapping[str, str],
        unit: Unit | None,
        breaker_limit: int,
    ) -> None:
        self.backend, self.ledger, self.arms, self.tasks = backend, ledger, arms, tasks
        self.workspace, self.frozen, self.policy, self.grader = workspace, frozen, policy, grader
        self.keep, self.clock, self.sleep, self.wait = keep_images, clock, sleep, wait
        self.settings, self.problems, self.unit = settings, problems, unit
        self.breaker = Breaker(breaker_limit)
        self.campaign = ledger.campaign
        self.stage = str(ledger.header["stage"])
        self.known = [
            k
            for k in dict.fromkeys(keys)
            if k.instance_id in tasks
            and k.arm in arms
            and (k.campaign, k.stage) == (self.campaign, self.stage)
        ]
        self.incomplete = len(self.known) != len(dict.fromkeys(keys))
        self.empty = _private_dir(workspace / "state-empty")
        self._index()

    def _index(self) -> None:
        """Predecessor of each key in its stream; blocks ordered by commit position."""
        groups: dict[tuple[str, int, str], list[RunKey]] = {}
        for k in self.known:
            groups.setdefault((k.arm, k.seed, self.tasks[k.instance_id].repo), []).append(k)
        order = lambda k: (self.tasks[k.instance_id].created_at, k.instance_id)  # noqa: E731
        self.pred: dict[RunKey, RunKey | None] = {}
        for members in groups.values():
            prev: RunKey | None = None
            for k in sorted(members, key=order):
                self.pred[k] = prev
                prev = k
        by_repo: dict[str, list[Task]] = {}
        for iid in dict.fromkeys(k.instance_id for k in self.known):
            by_repo.setdefault(self.tasks[iid].repo, []).append(self.tasks[iid])
        position = {
            t.instance_id: i
            for group in by_repo.values()
            for i, t in enumerate(sorted(group, key=lambda t: (t.created_at, t.instance_id)))
        }
        self.blocks: dict[str, list[RunKey]] = {}
        for k in self.known:
            self.blocks.setdefault(k.instance_id, []).append(k)
        firsts = list(self.blocks)
        self.block_order = sorted(firsts, key=lambda iid: position[iid])  # stable

    # --- ledger views --------------------------------------------------------------------

    def resolved(self, key: RunKey) -> bool:
        return self.ledger.final(key) is not None or self.ledger.unrunnable(key)

    def _rel(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.ledger.path.parent)).as_posix()

    def _pause(self, reason: str, **details: Any) -> None:
        self.ledger.append({"kind": "pause", "at": self.clock(), "reason": reason, **details})

    def _needs_grading(self, iid: str) -> bool:
        last_end, last_graded = -1, -1
        for i, rec in enumerate(self.ledger.records()):
            if rec.get("kind") == "end":
                try:
                    k = RunKey.from_id(str(rec.get("key")))
                except ValueError:
                    continue
                if k.instance_id == iid and k.stage == self.stage:
                    last_end = i
            elif rec.get("kind") in ("graded", "invalidate"):
                if rec.get("kind") == "invalidate" or (
                    rec.get("instance_id") == iid and rec.get("stage") == self.stage
                ):
                    if rec.get("kind") == "graded":
                        last_graded = i
                    else:
                        last_end = max(last_end, i)  # a reopened key may change the verdicts
        return last_end > last_graded

    # --- the run loop --------------------------------------------------------------------

    def run(self) -> CampaignStatus:
        self.backend.reap(self.campaign)
        try:
            if self.frozen is not None and (
                self.frozen.plugin_hash != self.ledger.header.get("plugin_hash")
            ):
                self._pause("plugin_changed", detail="frozen plugin hash differs from the ledger")
                raise _Stop("plugin_changed")
            for iid in self.block_order:
                self._block(iid)
        except _Stop as stop:
            return stop.status
        return "incomplete" if self.incomplete else "complete"

    def _block(self, iid: str) -> None:
        task = self.tasks[iid]
        pending = [k for k in self.blocks[iid] if not self.resolved(k)]
        ensured = False
        while pending:
            for key in list(pending):
                pred = self.pred[key]
                if pred is not None and not self.resolved(pred):  # pragma: no cover - order
                    raise _Stop("incomplete")
                self._checks(key)
                if not ensured:  # pulled when the block's first run is cleared to start
                    self.backend.ensure_image(task.image)
                    ensured = True
                self._run_one(key, task)
            pending = [k for k in pending if not self.resolved(k)]
        if self._needs_grading(iid):
            self._disk_floor(None)
            self.grader(self.ledger, stage=self.stage, instance_id=iid)
            release_image(self.backend, task.image, keep=self.keep)
            self.ledger.append(
                {"kind": "graded", "at": self.clock(), "stage": self.stage, "instance_id": iid}
            )

    def _disk_floor(self, key: RunKey | None) -> None:
        free = self.backend.free_disk_gb()
        if free < DISK_FLOOR_GB:
            where = {"key": key.id} if key is not None else {}
            self._pause("disk", status="infra_fail", sub="disk", free_gb=round(free, 1), **where)
            raise _Stop("paused_disk")

    def _checks(self, key: RunKey | None = None) -> None:
        """Plugin, disk and quota before a run (sleeping through five-hour waits)."""
        while True:
            if self.frozen is not None:
                try:
                    verify(self.frozen)
                except FrozenPluginChanged as exc:
                    self._pause("plugin_changed", detail=str(exc)[:500])
                    raise _Stop("plugin_changed") from exc
            self._disk_floor(key)
            now = self.clock()
            records = self.ledger.records()
            readings = [
                r
                for r in (reading_from_record(x) for x in records if x.get("kind") == "quota")
                if r is not None
            ]
            start, _ = window_bounds(readings, now)
            runs = sum(1 for x in records if x.get("kind") == "start" and x["at"] >= start)
            d = decide(readings, self.policy, now, runs)
            if d.go:
                return
            self._pause(d.reason, wait_until=d.wait_until)
            if d.reason == "weekly":
                raise _Stop("paused_weekly")
            if not self.wait or d.wait_until is None:
                raise _Stop("paused_five_hour")
            self.sleep(max(0.0, d.wait_until - now) + WAIT_MARGIN_S)

    def _state_in(self, key: RunKey, arm: Arm) -> Path:
        if arm.state != "stream":
            return self.empty
        pred = self.pred[key]
        while pred is not None:
            final = self.ledger.final(pred)
            if final is not None:
                return self._promote(pred, final)
            pred = self.pred[pred]  # an unrunnable predecessor: carry the state before it
        return self.empty

    def _promote(self, key: RunKey, end: Mapping[str, Any]) -> Path:
        source = self.ledger.resolve(str(end.get("run_dir", ""))) / "state-out"
        return promote_state(
            self.workspace,
            key=key,
            repo=self.tasks[key.instance_id].repo,
            arm=self.arms[key.arm],
            source=source,
            ledger=self.ledger,
        )

    def _run_one(self, key: RunKey, task: Task) -> None:
        arm = self.arms[key.arm]
        attempt = self.ledger.next_attempt(key)
        run_dir = self.workspace / "runs" / self.stage / _short(key.id) / f"a{attempt}"
        _private_dir(run_dir.parent)
        frozen_hash = self.frozen.plugin_hash if self.frozen is not None else None
        req = RunRequest(
            task=task,
            arm=arm,
            seed=key.seed,
            stage=self.stage,
            model=str(self.ledger.header["model"]),
            effort=self.settings.effort,
            max_turns=self.settings.max_turns,
            timeout_s=self.settings.timeout_s,
            state_in=self._state_in(key, arm),
            run_dir=run_dir,
            frozen=self.frozen,
            attempt=attempt,
            campaign=self.campaign,
            problem_statement=self.problems.get(task.instance_id, ""),
        )
        self.ledger.append(
            {
                "kind": "start",
                "at": self.clock(),
                "key": key.id,
                "attempt": attempt,
                "container": container_name(key.id, attempt),
                "plugin_hash": frozen_hash,
                "run_dir": self._rel(run_dir),
            }
        )
        with locked(self.workspace / BENCH_LOCK, timeout=BENCH_LOCK_TIMEOUT_S):
            try:
                outcome = self.backend.run(req)
            except FrozenPluginChanged as exc:
                self._pause("plugin_changed", detail=str(exc)[:500])
                raise _Stop("plugin_changed") from exc
            except BackendError as exc:
                problem = f"backend refused the run: {exc}"[:500]
                outcome = RunOutcome(("infra_fail", "docker"), None, "", None, 0.0, [problem])
        self._record(key, task, arm, attempt, run_dir, outcome)

    def _record(
        self, key: RunKey, task: Task, arm: Arm, attempt: int, run_dir: Path, out: RunOutcome
    ) -> None:
        now = self.clock()
        m = out.metrics
        rate_limits = m.rate_limits if m is not None else []
        readings = [reading_from_rate_limit(rl, at=now, unit=self.unit) for rl in rate_limits]
        if out.status == ("infra_fail", "rate") and not any(r.rejected for r in readings):
            readings.append(QuotaReading(now, "stream", "unknown", None, None, "rejected"))
        for reading in readings:
            self.ledger.append(reading_record(reading))
        kind, sub = out.status
        graded = kind in ("ok", "agent_fail")
        uses_plugin = arm.uses_frozen_plugin and self.frozen is not None
        end = {
            "kind": "end",
            "at": now,
            "key": key.id,
            "attempt": attempt,
            "status": kind,
            "sub": sub,
            "metrics": asdict(m) if m is not None else None,
            "run_cost": run_cost(m) if m is not None else None,
            "patch_sha256": sha256_text(out.patch),
            "wall_s": out.wall_s,
            "repo": task.repo,
            "model": m.model if m is not None else None,
            "claude_code_version": m.claude_code_version if m is not None else None,
            "plugin_hash": self.frozen.plugin_hash if uses_plugin and self.frozen else None,
            "live": True if graded else None,
            "problems": list(out.problems),
            "run_dir": self._rel(run_dir),
        }
        self.ledger.append(end)  # the commit point of the run
        if graded and arm.state == "stream" and out.state_out is not None:
            self._promote(key, end)
        if kind == "infra_fail" and sub in _STOP_AT_ONCE:
            self._pause(sub, key=key.id)
            raise _Stop(_STOP_AT_ONCE[sub])
        if self.breaker.record(out.status):
            self._pause("breaker", key=key.id, consecutive=self.breaker.count)
            raise _Stop("paused_breaker")


def run_campaign(
    keys: Sequence[RunKey],
    *,
    backend: ImageBackend,
    ledger: Ledger,
    arms: Mapping[str, Arm],
    tasks: Mapping[str, Task],
    workspace: Path,
    frozen: FrozenPlugin | None,
    policy: QuotaPolicy,
    grader: TaskGrader,
    keep_images: frozenset[str],
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    wait: bool = False,
    settings: RunSettings = RunSettings(),
    problems: Mapping[str, str] | None = None,
    unit: Unit | None = UTILIZATION_UNIT,
    breaker_limit: int = 3,
) -> CampaignStatus:
    """Run (or resume) ``keys`` of the ledger's campaign stage; see the module docstring.
    Keys of another campaign or stage, or naming an unknown task or arm, are skipped and the
    result is then at best ``incomplete``."""
    _private_dir(workspace)
    lock = locked(workspace / SCHEDULER_LOCK, timeout=0)
    try:
        lock.__enter__()
    except StoreBusy as exc:
        raise SchedulerBusy(f"another scheduler holds {workspace}") from exc
    try:
        return _Campaign(
            keys,
            backend=backend,
            ledger=ledger,
            arms=arms,
            tasks=tasks,
            workspace=workspace,
            frozen=frozen,
            policy=policy,
            grader=grader,
            keep_images=keep_images,
            clock=clock,
            sleep=sleep,
            wait=wait,
            settings=settings,
            problems=problems or {},
            unit=unit,
            breaker_limit=breaker_limit,
        ).run()
    finally:
        with contextlib.suppress(Exception):
            lock.__exit__(None, None, None)
