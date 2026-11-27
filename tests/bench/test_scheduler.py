"""Resumable E4 scheduler (M5 Task 7): order, retries, breaker, quota, disk, images, resume.

No Docker and no ``claude``: `ScriptBackend` plays a backend in-process (its learner state is a
``history`` file listing the tasks it has seen, so a wrong carried state is visible), and one
end-to-end test drives `bench.backends.FakeBackend`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bench import arms as am
from bench import backends as bk
from bench import freeze as fz
from bench import ledger as lg
from bench import quota as qt
from bench import scheduler as sc
from bench import tasks as tk
from bench.streamjson import RateLimit, RunMetrics

H = 3600.0
T0 = 1_000_000.0
STATE = "st"
TASKS = {
    t.instance_id: replace(t, image=f"img-{t.instance_id}:latest") for t in tk.FAKE_TASKS
}  # alpha-1, alpha-2, beta-1, beta-2 (two repos, commit order 1 then 2)
POLICY = qt.QuotaPolicy(max_runs_per_window=1000)


def _arm(name: str, state: str = "stream") -> am.Arm:
    return am.Arm(
        name=name,
        kind="baseline",
        state=state,  # type: ignore[arg-type]
        state_paths=(STATE,) if state == "stream" else (),
        append_system_prompt=None,
        uses_frozen_plugin=False,
        install=(),
        wrapper=(),
        env={},
        ts_config={},
        liveness="none",
        patch_excludes=(),
    )


ARMS = {"a": _arm("a"), "b": _arm("b"), "c": _arm("c", "none")}


class Clock:
    def __init__(self) -> None:
        self.t = T0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Crash(BaseException):
    """A simulated process crash (not an Exception: nothing in the scheduler may swallow it)."""


Plan = Callable[[bk.RunRequest], "str | bk.RunOutcome"]


class ScriptBackend:
    def __init__(
        self,
        plan: Plan | None = None,
        *,
        disk: Callable[[], float] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.plan = plan or (lambda _r: "ok/success")
        self.disk = disk or (lambda: 1000.0)
        self.clock = clock
        self.events: list[tuple[str, str]] = []
        self.calls: list[bk.RunRequest] = []
        self.histories: dict[str, list[str]] = {}  # key id -> state_in history at run time

    def reap(self, campaign: str) -> int:
        self.events.append(("reap", campaign))
        return 0

    def ensure_image(self, image: str) -> None:
        self.events.append(("ensure", image))

    def remove_image(self, image: str) -> None:
        self.events.append(("remove", image))

    def free_disk_gb(self) -> float:
        return self.disk()

    def run(self, req: bk.RunRequest) -> bk.RunOutcome:
        self.calls.append(req)
        self.events.append(("run", req.key_id))
        hist_file = req.state_in / STATE / "history"
        history = hist_file.read_text(encoding="utf-8").split() if hist_file.exists() else []
        self.histories[req.key_id] = history
        req.run_dir.mkdir(parents=True, exist_ok=False)
        out = req.run_dir / "state-out" / STATE
        out.mkdir(parents=True)
        (out / "history").write_text(" ".join([*history, req.task.instance_id]), encoding="utf-8")
        (req.run_dir / "patch.diff").write_text(f"patch {req.key_id}\n", encoding="utf-8")
        planned = self.plan(req)
        if self.clock is not None:
            self.clock.t += 60.0
        if isinstance(planned, bk.RunOutcome):
            return planned
        status, sub = planned.split("/")
        graded = status in ("ok", "agent_fail")
        metrics = RunMetrics(model="claude-test-1", claude_code_version="9.9.9")
        patch = f"patch {req.key_id}\n" if graded else ""
        state = req.run_dir / "state-out" if graded else None
        return bk.RunOutcome((status, sub), metrics, patch, state, 1.5, [])  # type: ignore[arg-type]


def _ledger(
    tmp: Path, *, plugin_hash: str = "h" * 64, readings: bool = True, model: str = "sonnet"
) -> lg.Ledger:
    header = {
        "campaign": "c1",
        "stage": "screening",
        "harness_commit": "0" * 40,
        "plugin_hash": plugin_hash,
        "plugin_ref": "v0.5.0-screen1",
        "pins": {},
        "model": model,
        "claude_code_version": "9.9.9",
    }
    led = lg.Ledger(tmp / "ledger.jsonl", header=header)
    if readings:
        _owner(led, weekly=10.0, five=10.0)
    return led


def _owner(led: lg.Ledger, *, weekly: float, five: float, at: float = T0 - 60) -> None:
    for kind, pct, resets in (("weekly", weekly, T0 + 72 * H), ("five_hour", five, T0 + 2 * H)):
        reading = qt.QuotaReading(at, "owner", kind, pct, resets)  # type: ignore[arg-type]
        led.append(qt.reading_record(reading))


class Grades:
    def __init__(self, backend: ScriptBackend | None = None) -> None:
        self.backend = backend
        self.graded: list[tuple[str, list[str]]] = []

    def __call__(self, ledger: lg.Ledger, *, stage: str, instance_id: str) -> None:
        keys = sorted(r.key for r in ledger.final_runs(stage) if r.instance_id == instance_id)
        self.graded.append((instance_id, keys))
        if self.backend is not None:
            self.backend.events.append(("grade", instance_id))


def _keys(arms: Sequence[str] = ("a", "b"), seeds: Sequence[int] = (0,)) -> list[lg.RunKey]:
    streams = tk.streams(list(TASKS.values()))
    return sc.plan_order(streams, arms, seeds, campaign="c1", stage="screening", shuffle_seed=7)


def _run(
    tmp: Path,
    backend: Any,
    keys: Sequence[lg.RunKey],
    led: lg.Ledger | None = None,
    *,
    clock: Clock | None = None,
    grader: Any = None,
    policy: qt.QuotaPolicy = POLICY,
    wait: bool = False,
    keep: frozenset[str] = frozenset(),
    frozen: fz.FrozenPlugin | None = None,
    tasks: Mapping[str, tk.Task] = TASKS,
) -> sc.CampaignStatus:
    clock = clock or Clock()
    return sc.run_campaign(
        keys,
        backend=backend,
        ledger=led or _ledger(tmp),
        arms={**ARMS, **am.load_arms()},
        tasks=tasks,
        workspace=tmp,
        frozen=frozen,
        policy=policy,
        grader=grader or Grades(),
        keep_images=keep,
        clock=clock,
        sleep=clock.sleep,
        wait=wait,
    )


def _ends(led: lg.Ledger) -> list[dict[str, Any]]:
    return [r for r in led.records() if r["kind"] == "end"]


def _final_history(tmp: Path, led: lg.Ledger, key: lg.RunKey) -> list[str]:
    final = led.final(key)
    assert final is not None
    return (tmp / final["run_dir"] / "state-out" / STATE / "history").read_text().split()


# --- order ---------------------------------------------------------------------------------


def test_arms_alternate_within_blocks() -> None:
    keys = _keys(arms=("a", "b", "c"), seeds=(0, 1))
    blocks = [keys[i : i + 6] for i in range(0, len(keys), 6)]
    assert [b[0].instance_id for b in blocks] == [
        "fake__alpha-1",
        "fake__beta-1",
        "fake__alpha-2",
        "fake__beta-2",
    ]
    for block in blocks:
        assert len({k.instance_id for k in block}) == 1  # a task's runs are back to back
        assert sorted((k.arm, k.seed) for k in block) == sorted(
            (a, s) for a in ("a", "b", "c") for s in (0, 1)
        )
    orders = {tuple((k.arm, k.seed) for k in b) for b in blocks}
    assert len(orders) > 1  # seeded permutation per block, not a fixed arm order
    assert keys == _keys(arms=("a", "b", "c"), seeds=(0, 1))
    other = sc.plan_order(
        tk.streams(list(TASKS.values())),
        ("a", "b", "c"),
        (0, 1),
        campaign="c1",
        stage="screening",
        shuffle_seed=8,
    )
    assert other != keys and sorted(k.id for k in other) == sorted(k.id for k in keys)


def test_stream_order_respected(tmp_path: Path) -> None:
    backend = ScriptBackend()
    keys = list(reversed(_keys()))
    assert _run(tmp_path, backend, keys) == "complete"
    seen = [(r.arm.name, r.task.instance_id) for r in backend.calls]
    for arm in ("a", "b"):
        for repo in ("alpha", "beta"):
            order = [iid for a, iid in seen if a == arm and repo in iid]
            assert order == [f"fake__{repo}-1", f"fake__{repo}-2"]
    second = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-2")
    assert backend.histories[second.id] == ["fake__alpha-1"]  # state carried in commit order


def test_task_runs_back_to_back_then_graded_and_image_removed(tmp_path: Path) -> None:
    backend = ScriptBackend()
    grades = Grades(backend)
    assert _run(tmp_path, backend, _keys(), grader=grades) == "complete"
    ev = [e for e in backend.events if e[0] != "reap"]
    blocks: list[list[tuple[str, str]]] = []
    for e in ev:
        if e[0] == "ensure":
            blocks.append([])
        blocks[-1].append(e)
    assert len(blocks) == 4
    for block in blocks:
        iid = block[0][1].removeprefix("img-").removesuffix(":latest")
        kinds = [k for k, _ in block]
        assert kinds == ["ensure", "run", "run", "grade", "remove"]
        assert all(iid in v for _, v in block)
    assert [len(keys) for _, keys in grades.graded] == [2, 2, 2, 2]
    assert backend.events[0] == ("reap", "c1")


def test_kept_images_survive(tmp_path: Path) -> None:
    backend = ScriptBackend()
    keep = frozenset({"img-fake__alpha-1:latest"})
    assert _run(tmp_path, backend, _keys(), keep=keep) == "complete"
    removed = {v for k, v in backend.events if k == "remove"}
    assert removed == {f"img-{i}:latest" for i in TASKS} - keep


# --- resume --------------------------------------------------------------------------------


def test_rerun_skips_final_keys(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    grades = Grades()
    assert _run(tmp_path, ScriptBackend(), _keys(), led, grader=grades) == "complete"
    again = ScriptBackend()
    assert _run(tmp_path, again, _keys(), led, grader=grades) == "complete"
    assert again.calls == []
    assert len(grades.graded) == 4  # graded blocks are not graded again
    assert len(_ends(led)) == 8


def test_resume_after_crash_reruns_from_promoted_state(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    crash_key = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-2")

    def plan(req: bk.RunRequest) -> str:
        if req.key_id == crash_key.id:
            raise Crash  # after writing a half state-out: must never be promoted
        return "ok/success"

    with pytest.raises(Crash):
        _run(tmp_path, ScriptBackend(plan), _keys(), led)
    assert led.final(crash_key) is None
    resumed = ScriptBackend()
    assert _run(tmp_path, resumed, _keys(), led) == "complete"
    assert [r.key_id for r in resumed.calls][0] == crash_key.id
    assert resumed.calls[0].attempt == 2  # a fresh container name and run dir
    assert resumed.histories[crash_key.id] == ["fake__alpha-1"]
    assert _final_history(tmp_path, led, crash_key) == ["fake__alpha-1", "fake__alpha-2"]
    assert led.infra_attempts(crash_key) == 0  # a crash of the scheduler is not the run's fault


def test_crash_between_end_and_promotion_promotes_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    led = _ledger(tmp_path)
    real = sc.promote_state
    first = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-1")

    def boom(*args: Any, **kwargs: Any) -> Path:
        if kwargs.get("key", args[1] if len(args) > 1 else None) == first:
            raise Crash
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "promote_state", boom)
    with pytest.raises(Crash):
        _run(tmp_path, ScriptBackend(), _keys(), led)
    assert led.final(first) is not None
    monkeypatch.setattr(sc, "promote_state", real)
    resumed = ScriptBackend()
    assert _run(tmp_path, resumed, _keys(), led) == "complete"
    assert first.id not in [r.key_id for r in resumed.calls]  # never run twice
    second = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-2")
    assert resumed.histories[second.id] == ["fake__alpha-1"]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    crashes=st.lists(
        st.tuples(st.integers(0, 12), st.sampled_from(["run", "promote", "grade"])), max_size=4
    ),
    infra=st.sets(st.integers(0, 15), max_size=4),
)
def test_crash_resume_property(crashes: list[tuple[int, str]], infra: set[int]) -> None:
    """Any sequence of crashes (in a run, in promotion, in grading) and transient infra
    failures: resuming until complete gives one final per key, state chained in commit order,
    no final key ever rerun and every task graded."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        led = _ledger(tmp)
        counter = {"run": 0, "promote": 0, "grade": 0}
        pending = list(crashes)
        calls = {"n": 0}

        def crash_point(where: str) -> None:
            n = counter[where]
            counter[where] += 1
            if pending and pending[0] == (n, where):
                pending.pop(0)
                raise Crash

        def plan(req: bk.RunRequest) -> str:
            crash_point("run")
            calls["n"] += 1
            if calls["n"] in infra:
                return "infra_fail/api"
            return "ok/success"

        real = sc.promote_state

        def promote(*args: Any, **kwargs: Any) -> Path:
            crash_point("promote")
            return real(*args, **kwargs)

        grades = Grades()

        def grade(ledger: lg.Ledger, *, stage: str, instance_id: str) -> None:
            crash_point("grade")
            grades(ledger, stage=stage, instance_id=instance_id)

        keys = _keys()
        sc_promote = sc.promote_state
        sc.promote_state = promote  # type: ignore[assignment]
        try:
            status: str | None = None
            for _ in range(20):
                backend = ScriptBackend(plan)
                finals_before = {k.id for k in keys if led.final(k) is not None}
                try:
                    status = _run(tmp, backend, keys, led, grader=grade)
                except Crash:
                    status = None
                assert not finals_before & {r.key_id for r in backend.calls}
                if status not in (None, "paused_breaker"):
                    break
        finally:
            sc.promote_state = sc_promote  # type: ignore[assignment]
        assert status == "complete"
        ends = _ends(led)
        for key in keys:
            graded = [e for e in ends if e["key"] == key.id and e["status"] == "ok"]
            if led.unrunnable(key):  # three counted infra failures: never graded
                assert graded == []
                continue
            assert len(graded) == 1
            repo = "alpha" if "alpha" in key.instance_id else "beta"
            chain = [f"fake__{repo}-1", f"fake__{repo}-2"]
            done = [
                iid
                for iid in chain[: chain.index(key.instance_id) + 1]
                if not led.unrunnable(replace(key, instance_id=iid))
            ]
            assert _final_history(tmp, led, key) == done  # carried in commit order
        assert {iid for iid, _ in grades.graded} == set(TASKS)


# --- typed failures ------------------------------------------------------------------------


def test_rate_requeues_without_attempt(tmp_path: Path) -> None:
    clock = Clock()
    key = _keys()[0]
    seen: list[str] = []

    def plan(req: bk.RunRequest) -> bk.RunOutcome | str:
        seen.append(req.key_id)
        if len(seen) == 1:
            rl = RateLimit("rejected", "five_hour", clock.t + 600, None)
            m = RunMetrics(rate_limits=[rl])
            return bk.RunOutcome(("infra_fail", "rate"), m, "", None, 1.0, [])
        return "ok/success"

    led = _ledger(tmp_path)
    assert _run(tmp_path, ScriptBackend(plan), _keys(), led, clock=clock, wait=True) == "complete"
    assert seen[0] == key.id and seen.count(key.id) == 2  # re-queued in its block
    assert led.infra_attempts(key) == 0
    assert clock.sleeps and clock.sleeps[0] >= 600 - 60
    statuses = [e["sub"] for e in _ends(led) if e["key"] == key.id]
    assert statuses == ["rate", "success"]


def test_rate_without_a_usable_reading_fails_safe(tmp_path: Path) -> None:
    def plan(req: bk.RunRequest) -> str:
        return "infra_fail/rate"

    status = _run(tmp_path, ScriptBackend(plan), _keys(), wait=True)
    assert status == "paused_weekly" and sc.EXIT_CODES[status] == 3


def test_infra_never_graded_and_unrunnable_after_three(tmp_path: Path) -> None:
    bad = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-1")

    def plan(req: bk.RunRequest) -> str:
        return "infra_fail/load" if req.key_id == bad.id else "ok/success"

    led = _ledger(tmp_path)
    keys = [bad, *[k for k in _keys(arms=("a", "b", "c")) if k != bad]]
    grades = Grades()
    backend = ScriptBackend(plan)
    status = _run(tmp_path, backend, keys, led, grader=grades)
    assert status == "complete"
    assert [r.key_id for r in backend.calls].count(bad.id) == 3
    assert [r.attempt for r in backend.calls if r.key_id == bad.id] == [1, 2, 3]
    assert led.unrunnable(bad) and led.final(bad) is None
    for _, graded_keys in grades.graded:
        assert bad.id not in graded_keys
    # infra failures never promote: the stream's next task starts from the empty state
    nxt = lg.RunKey("c1", "screening", "a", 0, "fake__alpha-2")
    assert backend.histories[nxt.id] == []


def test_breaker_pauses_after_consecutive_infra_fails(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    backend = ScriptBackend(lambda _r: "infra_fail/docker")
    status = _run(tmp_path, backend, _keys(), led)
    assert status == "paused_breaker" and sc.EXIT_CODES[status] == 6
    assert len(backend.calls) == 3
    assert [r["reason"] for r in led.records() if r["kind"] == "pause"] == ["breaker"]


def test_breaker_counts_only_consecutive_non_rate_non_disk() -> None:
    br = sc.Breaker()
    assert not br.record(("infra_fail", "api"))
    assert not br.record(("infra_fail", "rate"))
    assert not br.record(("infra_fail", "disk"))
    assert not br.record(("infra_fail", "load"))
    assert not br.record(("ok", "success"))  # resets
    assert not br.record(("infra_fail", "api"))
    assert not br.record(("infra_fail", "unknown"))
    assert br.record(("infra_fail", "proxy"))
    assert sc.Breaker(limit=1).record(("infra_fail", "docker"))


@pytest.mark.parametrize(("sub", "expect"), [("auth", "stopped_auth"), ("leak", "stopped_leak")])
def test_auth_stops_immediately(tmp_path: Path, sub: str, expect: str) -> None:
    backend = ScriptBackend(lambda _r: f"infra_fail/{sub}")
    status = _run(tmp_path, backend, _keys())
    assert status == expect and len(backend.calls) == 1
    assert sc.EXIT_CODES[status] == {"auth": 7, "leak": 8}[sub]


def test_backend_error_is_an_infra_docker_failure(tmp_path: Path) -> None:
    def plan(req: bk.RunRequest) -> str:
        raise bk.BackendError("another bench container is running")

    led = _ledger(tmp_path)
    assert _run(tmp_path, ScriptBackend(plan), _keys(), led) == "paused_breaker"
    assert {(e["status"], e["sub"]) for e in _ends(led)} == {("infra_fail", "docker")}


# --- quota, disk, plugin -------------------------------------------------------------------


def test_weekly_60_stops_never_sleeps(tmp_path: Path) -> None:
    led = _ledger(tmp_path, readings=False)
    _owner(led, weekly=60.0, five=1.0)
    clock = Clock()
    backend = ScriptBackend()
    status = _run(tmp_path, backend, _keys(), led, clock=clock, wait=True)
    assert status == "paused_weekly" and sc.EXIT_CODES[status] == 3
    assert backend.calls == [] and clock.sleeps == []


def test_no_quota_reading_fails_safe(tmp_path: Path) -> None:
    backend = ScriptBackend()
    status = _run(tmp_path, backend, _keys(), _ledger(tmp_path, readings=False), wait=True)
    assert status == "paused_weekly" and backend.calls == []


def test_window_cap(tmp_path: Path) -> None:
    policy = qt.QuotaPolicy(max_runs_per_window=3)
    backend = ScriptBackend()
    status = _run(tmp_path, backend, _keys(), policy=policy)
    assert status == "paused_five_hour" and sc.EXIT_CODES[status] == 4
    assert len(backend.calls) == 3
    tmp2 = tmp_path / "waiting"
    tmp2.mkdir()
    clock = Clock()
    waiter = ScriptBackend(clock=clock)
    patient = qt.QuotaPolicy(max_runs_per_window=3, max_reading_age_s=1e9)
    assert _run(tmp2, waiter, _keys(), clock=clock, policy=patient, wait=True) == "complete"
    assert len(waiter.calls) == 8 and clock.sleeps


def test_five_hour_waits_with_wait(tmp_path: Path) -> None:
    led = _ledger(tmp_path, readings=False)
    _owner(led, weekly=10.0, five=90.0)
    backend = ScriptBackend()
    assert _run(tmp_path, backend, _keys(), led) == "paused_five_hour"
    assert backend.calls == []
    clock = Clock()
    status = _run(tmp_path, backend, _keys(), led, clock=clock, wait=True)
    assert status == "complete"
    assert clock.sleeps[0] >= 2 * H  # until the five-hour reading's reset
    pauses = [r for r in led.records() if r["kind"] == "pause"]
    assert {p["reason"] for p in pauses} == {"five_hour"}


def test_disk_floor_pauses_without_breaker(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    readings = iter([1000.0, 1000.0, 10.0])
    backend = ScriptBackend(lambda _r: "infra_fail/api", disk=lambda: next(readings, 10.0))
    status = _run(tmp_path, backend, _keys(), led)
    assert status == "paused_disk" and sc.EXIT_CODES[status] == 10
    assert len(backend.calls) == 2  # two infra fails, then the floor: not a breaker trip
    (pause,) = [r for r in led.records() if r["kind"] == "pause"]
    assert (pause["reason"], pause["status"], pause["sub"]) == ("disk", "infra_fail", "disk")


def test_disk_floor_checked_before_grading(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    readings = iter([1000.0, 1000.0])
    backend = ScriptBackend(disk=lambda: next(readings, 10.0))
    grades = Grades()
    assert _run(tmp_path, backend, _keys(), led, grader=grades) == "paused_disk"
    assert grades.graded == [] and len(backend.calls) == 2
    assert _run(tmp_path, ScriptBackend(), _keys(), led, grader=grades) == "complete"
    assert grades.graded[0][0] == _keys()[0].instance_id


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
GIT += ["-c", "commit.gpgSign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def _frozen(tmp: Path) -> fz.FrozenPlugin:
    root = tmp / "plugin-src"
    files = {".claude-plugin/plugin.json": b'{"name": "token-sparen"}\n', "hooks/hooks.json": b"{}"}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "p"]):
        subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)
    return fz.freeze(root, "HEAD", tmp / "frozen", dry_run=True)


def test_plugin_change_refuses_run(tmp_path: Path) -> None:
    frozen = _frozen(tmp_path)
    led = _ledger(tmp_path, plugin_hash=frozen.plugin_hash)
    backend = ScriptBackend()
    keys = _keys()
    assert _run(tmp_path, backend, keys[:1], led, frozen=frozen) == "complete"
    assert len(backend.calls) == 1
    start = next(r for r in led.records() if r["kind"] == "start")
    assert start["plugin_hash"] == frozen.plugin_hash
    changed = replace(frozen, plugin_hash="0" * 64)  # the copy no longer matches its hash
    status = _run(tmp_path, backend, keys, led, frozen=changed)
    assert status == "plugin_changed" and sc.EXIT_CODES[status] == 9
    assert len(backend.calls) == 1
    if os.name == "posix":  # the frozen copy itself changes between runs: verify refuses
        target = frozen.path / "hooks" / "hooks.json"
        os.chmod(frozen.path / "hooks", 0o755)
        os.chmod(target, 0o644)
        target.write_bytes(b'{"changed": true}')
        assert _run(tmp_path, backend, keys, led, frozen=frozen) == "plugin_changed"
        assert len(backend.calls) == 1
        reasons = [r["reason"] for r in led.records() if r["kind"] == "pause"]
        assert reasons == ["plugin_changed", "plugin_changed"]
    (tmp_path / "x").mkdir()
    other = _ledger(tmp_path / "x")  # its header names another plugin hash
    assert _run(tmp_path / "x", ScriptBackend(), keys, other, frozen=frozen) == "plugin_changed"


def test_exit_codes() -> None:
    assert sc.EXIT_CODES == {
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


def test_unknown_task_or_arm_leaves_campaign_incomplete(tmp_path: Path) -> None:
    keys = [*_keys()[:2], lg.RunKey("c1", "screening", "a", 0, "nope__x-1")]
    assert _run(tmp_path, ScriptBackend(), keys) == "incomplete"


def test_second_scheduler_on_a_workspace_refused(tmp_path: Path) -> None:
    from token_sparen.store.jsonstore import locked

    with locked(tmp_path / sc.SCHEDULER_LOCK, timeout=0):
        with pytest.raises(sc.SchedulerBusy):
            _run(tmp_path, ScriptBackend(), _keys())


# --- end to end with the fake backend ------------------------------------------------------


def _fake_env() -> Mapping[str, str]:
    return {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}


def test_fake_backend_end_to_end_records_task9_shape(tmp_path: Path) -> None:
    backend = bk.FakeBackend(environ=_fake_env(), scenario=lambda _r: "success")
    led = _ledger(tmp_path, model="claude-sonnet-4-6")  # the fake agent echoes --model
    streams = tk.streams(list(tk.FAKE_TASKS))
    keys = sc.plan_order(
        streams, ("baseline",), (0,), campaign="c1", stage="screening", shuffle_seed=1
    )
    grades = Grades()
    fake_tasks = {t.instance_id: t for t in tk.FAKE_TASKS}
    assert _run(tmp_path, backend, keys, led, grader=grades, tasks=fake_tasks) == "complete"
    ends = _ends(led)
    assert len(ends) == 4
    for end in ends:
        assert end["status"] == "ok" and end["sub"] == "success"
        for field in (
            "key",
            "at",
            "attempt",
            "run_cost",
            "patch_sha256",
            "wall_s",
            "repo",
            "model",
            "claude_code_version",
            "plugin_hash",
            "live",
            "run_dir",
        ):
            assert field in end, field
        assert end["live"] is True and isinstance(end["metrics"], dict)
    runs = led.final_runs("screening")
    assert len(runs) == 4 and all("calc.py" in r.patch for r in runs)
    assert backend.removed == [] or set(backend.removed) == {tk.FAKE_IMAGE}
