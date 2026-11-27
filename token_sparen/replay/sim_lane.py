"""E1 simulation lane (``"sim"``): the live learning engine, run chronologically over past
sessions against the replay's proxy feedback, in a throw-away state root (spec 7.3-7.8, 10.1).

Past sessions ran without the learner, so its choices cannot be read from them; this lane plays
them back through the compression lane's machinery (``replay._Session``: the production gates,
``compression.pick_candidate``, the proxy regret detector and the cost accounting) and lets a
**live** learner decide every output the compression point is offered:

- **decide**: ``Engine.decide`` with the session's features (pending entry, propensity, ladder,
  breaker), exactly as the PostToolUse hook asks it;
- **savings**: from the tokens the chosen arm removed on that output (``before -
  tokens_after``, as the compression lane measures them), through ``regret.reward_savings`` (the
  hook's ``[learn] savings_reward`` mode; ``counterfactual`` measures the other arms on the same
  output at trim time). The re-sends are the **actual** later API calls of the session before
  the next compaction (the calls the compression lane prices the removal on; at least 1, as
  ``regret.expected_resends``), so the reward is delivered when they are known: at the next
  compaction or the session end. A trimming arm that found nothing to trim gets savings 0 and
  regret 0 at once, as in the hook;
- **regret**: the replay's proxy regret of that trim (``replay`` module docstring; every trim is
  watched for a re-run of its signature, token signals need >= 2 distinctive tokens), delivered
  once, when the proxy settles: a full regret at once, a partial one or 0 at the expiry of its
  window, an eviction or the session end. Every payout is also a ``regret_outcomes`` row of the
  session overlay, so the circuit breaker judges the learner as it would live. A session that
  fails mid-way (counted in ``sessions_failed``, per group too) is still settled as SessionEnd
  would (every open trim paid its best value so far, every pending savings reward delivered,
  each role on its own) and folded;
- **fold**: at each session end, ``overlay.fold(..., mine_facts=False, prune=False)``: engine
  evidence only (posteriors, back-off aggregates, global, breaker, pre-trim candidates), dated
  by the session's day.

**Configs.** Both roles are built from the **packaged defaults only**
(``config.packaged_defaults``): the run's config (user ``config.toml``, project file,
``TOKEN_SPAREN_*`` env) is ignored. The candidate is packaged defaults + the lane's ``overlay``;
the reference is the packaged defaults (current defaults), run in the same pass on the same
outputs with the same random draws (common random numbers: the engine's rng is seeded by run
seed, group and session, not role), so a proposal and the defaults are compared paired per
project group. With no overlay the two rows are identical. The measurement itself (the regret
window) is the reference's for both roles, so an overlay cannot shorten its own regret window.

**Isolation.** Each lane instance (one project group) creates a temporary directory with one
state root per role (``<tmp>/<role>/.token-sparen``, also the role's ``HOME``), passes it to
every engine, store and fold call through explicit env mappings (``os.environ`` is never
changed) and deletes the directory when it reports (or when the lane is collected). A state
root that ``config.state_home`` would not honour refuses the run. No fact miner, settings
writer, git call or file outside the temporary directory is touched. The project key is the
first 16 hex of the sha256 of the session's recorded working directory (no git probe).

**Choosers.** ``params["chooser_factory"]`` (``"module:callable"``, resolved in the worker)
replaces the production engine for the candidate only, e.g. the offline LinTS challenger in
``evals/challengers``; default ``None`` = the production engine (``EngineChooser``). The factory is
called as ``factory(cfg, env, home, seed)`` and must return a ``Chooser``. This package never
imports ``evals``.

**Report.** Per split (sessions of that split only; ``history = True``: a test session runs on
what dev sessions taught, never counted in dev) ``sessions``, ``sessions_failed`` and
``groups: {group: {role: row}}`` with, per role, the compression accounting of its trims
(``acc``, the compression lane's accumulator) and per decision point ``decisions``, ``trims``,
``savings_tokens`` (tokens removed), ``savings_reward``, ``proxy_regret``, ``breaker_trips`` and
``epochs`` (thirds of the group's decisions in the split, by order: ``decisions``, ``arms``,
``trims``, ``proxy_regret``). ``summary`` renders the merged view (``arm_share`` and
``proxy_regret_per_trim`` by epoch, ``net_savings`` = cost-weighted saved minus the recovery
reads the compression lane charges, ``net_savings_tokens`` the same in tokens), ``pairs`` the
same per group for paired comparisons. Aggregates only: no path, text or id; group keys are
the runner's hashes.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import random
import shutil
import tempfile
import weakref
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from token_sparen.config import Config, packaged_defaults, state_home
from token_sparen.estimate import Prices
from token_sparen.events import SessionCtx
from token_sparen.learn import overlay, regret
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, Decision, DecisionPoint
from token_sparen.replay import runner
from token_sparen.replay.replay import SPLITS, _Acc, _Session, _Span, _window, acc_view
from token_sparen.replay.runner import Event, SessionInfo
from token_sparen.store import jsonstore, session
from token_sparen.transcript import AssistantRecord, Compaction, Prompt, ToolResultRecord

ROLES = ("candidate", "reference")
POINTS = (COMPRESSION.name,)  # the points the replay offers (pre-trim candidates are not replayed)
EPOCHS = ("1", "2", "3")
_ARM, _TRIM, _REGRET = 0, 1, 2  # a decision record: [arm, trimmed, proxy regret]


class Chooser(Protocol):
    """A learner the lane drives: a ``Policy`` (``decide``) fed the hook's rewards."""

    def start_session(self, session_id: str, project_key: str, now: float) -> None: ...

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision: ...

    def reward(self, decision: Decision, savings: float | None, regret: float | None) -> None: ...

    def outcome(self, decision: Decision, value: float, signal: str) -> None:
        """A settled regret payout (the hook's ``regret_outcomes`` row)."""
        ...

    def end_session(self) -> list[str]:
        """The session ends (fold); the decision points the circuit breaker tripped."""
        ...


ChooserFactory = Callable[[Config, Mapping[str, str], Path, str], Chooser]


class EngineChooser:
    """The production learner: one ``Engine`` per session over the role's state root, folded
    (engine evidence only) at the session end."""

    def __init__(self, cfg: Config, env: Mapping[str, str], home: Path, seed: str) -> None:
        self.cfg, self.env, self.home, self.seed = cfg, env, home, seed
        self._engine: Engine | None = None
        self._sid = ""
        self._key = ""
        self._now = 0.0
        self._rows: list[list[Any]] = []

    def start_session(self, session_id: str, project_key: str, now: float) -> None:
        self._sid, self._key, self._now, self._rows = session_id, project_key, now, []
        stores = StoreChain(project_key, self.home, session_id, params=self.cfg.data)
        rng = random.Random(f"{self.seed}|{session_id}")
        self._engine = Engine(stores, self.cfg, rng, env=self.env)

    def _eng(self) -> Engine:
        assert self._engine is not None, "start_session first"
        return self._engine

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        return self._eng().decide(point, features, session)

    def reward(self, decision: Decision, savings: float | None, regret: float | None) -> None:
        self._eng().reward(decision.decision_id, savings, regret)

    def outcome(self, decision: Decision, value: float, signal: str) -> None:
        d = decision
        self._rows.append([d.decision_id, d.point, d.arm, "", value, d.bucket, signal])

    def end_session(self) -> list[str]:
        if self._engine is None:
            return []
        rows = self._rows
        if rows:
            path = session.session_path(self.home, self._key, self._sid)

            def add(state: dict[str, Any]) -> dict[str, Any]:
                outs = state.get(regret.OUTCOMES_KEY)
                outs = [*(outs if isinstance(outs, list) else []), *rows]
                state[regret.OUTCOMES_KEY] = outs[-regret.MAX_OUTCOMES :]
                return state

            jsonstore.update(path, add, {}, self.env)
        rep = overlay.fold(
            self._sid,
            self._key,
            self.home,
            params=self.cfg.data,
            now=self._now,
            mine_facts=False,
            prune=False,
        )
        self._engine, self._rows = None, []
        return list(rep.tripped)


def resolve_factory(path: str | None) -> ChooserFactory:
    """``"module:callable"`` -> the callable (imported here, i.e. in the worker); None -> the
    production ``EngineChooser``."""
    if path is None:
        return EngineChooser
    module, sep, attr = path.partition(":") if isinstance(path, str) else ("", "", "")
    if not sep or not module or not attr:
        raise ValueError("chooser_factory must be a 'module:callable' path")
    target = importlib.import_module(module)
    for part in attr.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise ValueError("chooser_factory must name a callable")
    factory: ChooserFactory = target
    return factory


# --------------------------------------------------------------------------- state


def _point_row() -> dict[str, Any]:
    return {
        "decisions": 0,
        "trims": 0,
        "savings_tokens": 0,
        "savings_reward": 0.0,
        "proxy_regret": 0.0,
        "breaker_trips": 0,
    }


@dataclass
class _Role:
    """One learner of the lane: its config, state root, chooser and per-split tallies."""

    name: str
    cfg: Config
    home: Path
    env: dict[str, str]
    chooser: Chooser
    window: int
    accs: dict[str, _Acc] = field(default_factory=lambda: {p: _Acc() for p in SPLITS})
    # per split and point: one [arm, trimmed, regret] record per decision, in order
    records: dict[str, dict[str, list[list[Any]]]] = field(default_factory=dict)
    points: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    trips: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SPLITS, 0))

    def point(self, split: str, name: str) -> dict[str, Any]:
        return self.points.setdefault(split, {}).setdefault(name, _point_row())

    def row(self, split: str) -> dict[str, Any]:
        points: dict[str, Any] = {}
        for name, row in self.points.get(split, {}).items():
            out = dict(row)
            out["epochs"] = _epochs(self.records.get(split, {}).get(name, []))
            points[name] = out
        return {
            "acc": asdict(self.accs[split]),
            "breaker_trips": self.trips[split],
            "points": points,
        }


def _epochs(records: list[list[Any]]) -> dict[str, dict[str, Any]]:
    """Thirds of the decisions in order: the i-th of n is in epoch ``floor(3 i / n) + 1``."""
    out: dict[str, dict[str, Any]] = {
        e: {"decisions": 0, "arms": {}, "trims": 0, "proxy_regret": 0.0} for e in EPOCHS
    }
    n = len(records)
    for i, (arm, trimmed, value) in enumerate(records):
        ep = out[EPOCHS[min(2, 3 * i // n)]]
        ep["decisions"] += 1
        ep["arms"][arm] = ep["arms"].get(arm, 0) + 1
        ep["trims"] += int(bool(trimmed))
        ep["proxy_regret"] += float(value)
    return out


class _Tap:
    """The ``Policy`` the replayed session asks: the role's chooser, every decision recorded."""

    def __init__(self, role: _Role, split: str) -> None:
        self.role, self.split = role, split
        self.last: list[Any] = []

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        decision = self.role.chooser.decide(point, features, session)
        self.last = [decision.arm, False, 0.0]
        self.role.records.setdefault(self.split, {}).setdefault(point.name, []).append(self.last)
        self.role.point(self.split, point.name)["decisions"] += 1
        return decision


class _Shim:
    """``regret.reward_savings`` calls ``policy.reward(id, savings, regret)``: forward it."""

    def __init__(self, fn: Callable[[float | None, float | None], None]) -> None:
        self._fn = fn

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        self._fn(savings, regret)


@dataclass
class _Pending:
    """A trim whose savings reward waits for its re-send count."""

    decision: Decision
    removed: float
    before: float
    best: float  # counterfactual mode: the largest removal over the trimming arms
    calls: int = 0  # later API calls so far (before the next compaction)


class _SimSession(_Session):
    """One role's replay of one session, feeding its chooser."""

    def __init__(
        self, role: _Role, split: str, cfg: Config, prices: Prices, cwd: str | None
    ) -> None:
        self.tap = _Tap(role, split)
        super().__init__((role.accs[split],), self.tap, cfg, prices, role.window, None, cwd, True)
        self.role, self.split = role, split
        self._spans: dict[int, tuple[Decision, list[Any]]] = {}
        self._savings: list[_Pending] = []  # trims whose re-sends are still being counted

    def _trimmed(
        self,
        decision: Decision,
        span: _Span,
        removed: float,
        before: float,
        redo: Callable[[str], float | None],
    ) -> None:
        rec = self.tap.last
        rec[_TRIM] = True
        row = self.role.point(self.split, decision.point)
        row["trims"] += 1
        row["savings_tokens"] += int(removed)
        best = removed
        if regret.savings_mode(self.role.cfg) == "counterfactual":  # measured now, text at hand
            for arm in COMPRESSION.arms:
                if arm not in (COMPRESSION.safe_arm, decision.arm):
                    other = redo(arm)
                    if other is not None:
                        best = max(best, other)
        self._savings.append(_Pending(decision, removed, before, best))
        self._spans[id(span)] = (decision, rec)

    def on_call(self, rec: AssistantRecord) -> None:
        super().on_call(rec)
        for pend in self._savings:
            pend.calls += 1

    def on_compaction(self) -> None:
        super().on_compaction()
        self._deliver()

    def _deliver(self) -> None:
        """Savings rewards of the trims so far: their re-sends are known now."""
        pending, self._savings = self._savings, []
        for pend in pending:
            self._reward_savings(pend)

    def _reward_savings(self, pend: _Pending) -> None:
        decision = pend.decision
        row = self.role.point(self.split, decision.point)

        def give(savings: float | None, regret_value: float | None) -> None:
            if savings is not None:
                row["savings_reward"] += float(savings)
            self.role.chooser.reward(decision, savings, regret_value)

        def max_removed() -> float:
            return pend.best

        regret.reward_savings(
            _Shim(give),
            decision.decision_id,
            decision.point,
            decision.bucket,
            pend.removed,
            regret.expected_resends(float(pend.calls)),
            home=self.role.home,
            project_key=self.project_key,
            env=self.role.env,
            cfg=self.role.cfg,
            before=pend.before,
            max_removed=max_removed,
        )

    def _untrimmed(self, decision: Decision) -> None:
        self.role.chooser.reward(decision, 0.0, 0.0)  # the hook's no-op: nothing to regret

    def _pay(self, decision: Decision, rec: list[Any], value: float, signal: str) -> None:
        rec[_REGRET] = value
        self.role.point(self.split, decision.point)["proxy_regret"] += value
        self.role.chooser.reward(decision, None, value)
        self.role.chooser.outcome(decision, value, signal)

    def _settled(self, span: _Span) -> None:
        entry = self._spans.pop(id(span), None)
        if entry is not None:
            value = span.best if span.best > 0 else 0.0
            self._pay(entry[0], entry[1], value, span.signal if value > 0 else "")

    def finish(self) -> None:
        """Session end (also after a failure): settle every watched trim, deliver every pending
        savings reward."""
        try:
            super().finish()
        finally:
            self._deliver()


# --------------------------------------------------------------------------- lane


def _project_key(cwd: str | None) -> str:
    return hashlib.sha256(f"sim|{cwd or ''}".encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _now(info: SessionInfo) -> float:
    """The session's day (UTC midnight) plus its index: folds age global evidence in sim time."""
    day = datetime(info.day.year, info.day.month, info.day.day, tzinfo=UTC)
    return day.timestamp() + info.index


class SimLane:
    """The simulation lane (``runner.LANES["sim"]``); see the module docstring. Params:
    ``overlay`` (the candidate's config; the reference runs without it), ``seed``,
    ``chooser_factory`` (``"module:callable"`` or None), ``prices`` (in-process)."""

    history = True

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        factory = resolve_factory(params.get("chooser_factory"))
        # packaged defaults only: the run's config (user, project, env layers) never shapes a role
        reference_cfg = Config(data=packaged_defaults())
        candidate_cfg = runner.apply_overlay(reference_cfg, params.get("overlay"))
        self.window = _window(reference_cfg)  # one measurement for both roles
        seed = params.get("seed", 0)
        self.seed = seed if isinstance(seed, int) and not isinstance(seed, bool) else 0
        prices = params.get("prices")
        self.prices = prices if isinstance(prices, Prices) else Prices.load(candidate_cfg)
        self.root = Path(tempfile.mkdtemp(prefix="token-sparen-sim-"))
        self._cleanup = weakref.finalize(self, shutil.rmtree, str(self.root), True)
        self.factories = {
            "candidate": (candidate_cfg, factory),
            "reference": (reference_cfg, EngineChooser),
        }
        self.roles: dict[str, _Role] = {}
        self.group: str | None = None
        self.counts = {p: {"sessions": 0, "sessions_failed": 0} for p in SPLITS}
        self._current: SessionInfo | None = None
        self._sessions: list[_SimSession] = []
        self._failed = False

    def _roles(self, group: str) -> dict[str, _Role]:
        if not self.roles:
            self.group = group
            for name, (cfg, factory) in self.factories.items():
                base = self.root / name
                home = base / ".token-sparen"
                env = {"HOME": str(base), "TOKEN_SPAREN_HOME": str(home)}
                home.mkdir(parents=True)
                if state_home(env) != home:
                    raise RuntimeError("the simulation state root would not be honoured")
                chooser = factory(cfg, env, home, f"{self.seed}|{group}")
                self.roles[name] = _Role(name, cfg, home, env, chooser, self.window)
        return self.roles

    def _start(self, info: SessionInfo) -> None:
        self._current, self._failed, self._sessions = info, False, []
        key = _project_key(info.cwd)
        sid = f"sim{info.index:06d}"
        for role in self._roles(info.group).values():
            role.chooser.start_session(sid, key, _now(info))
            sess = _SimSession(role, info.split, role.cfg, self.prices, info.cwd)
            sess.project_key = key
            self._sessions.append(sess)

    def feed(self, event: Event, info: SessionInfo) -> None:
        if info is not self._current:
            self._start(info)
        if self._failed:
            return
        try:
            for sess in self._sessions:
                if isinstance(event, AssistantRecord):
                    sess.on_assistant(event)
                elif isinstance(event, ToolResultRecord):
                    sess.on_result(event, event.duplicate)
                elif isinstance(event, Prompt):
                    sess.on_prompt(event)
                elif isinstance(event, Compaction):
                    sess.on_compaction()
        except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
            self._failed = True

    def end_session(self, info: SessionInfo) -> None:
        if info is not self._current:
            return  # no event of its chain: not a session
        for sess in self._sessions:  # each role settles on its own, even after a failure
            try:
                sess.finish()
            except Exception:  # noqa: BLE001 - counted, never described
                self._failed = True
        self._current, self._sessions = None, []
        for role in self.roles.values():  # the learner keeps what it saw, as it would live
            try:
                tripped = role.chooser.end_session()
            except Exception:  # noqa: BLE001 - e.g. a busy store: counted, never described
                self._failed, tripped = True, []
            role.trips[info.split] += len(tripped)
            for name in tripped:
                role.point(info.split, name)["breaker_trips"] += 1
        tally = "sessions_failed" if self._failed else "sessions"
        self.counts[info.split][tally] += 1
        for role in self.roles.values():
            setattr(role.accs[info.split], tally, getattr(role.accs[info.split], tally) + 1)

    def abort_session(self, info: SessionInfo) -> None:
        """The runner failed mid-session: count it failed."""
        if info is self._current:
            self._failed = True
            self.end_session(info)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for part in SPLITS:
            seen = self.counts[part]["sessions"] + self.counts[part]["sessions_failed"]
            if not seen:
                continue
            groups = (
                {self.group: {name: role.row(part) for name, role in self.roles.items()}}
                if self.group is not None and self.roles
                else {}
            )
            out[part] = {**self.counts[part], "groups": groups}
        self._cleanup()
        return out


runner.LANES["sim"] = SimLane


# --------------------------------------------------------------------------- views


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den > 0 else 0.0


def _point_view(row: Mapping[str, Any], acc: Mapping[str, Any]) -> dict[str, Any]:
    epochs = row.get("epochs", {})
    shares: dict[str, dict[str, float]] = {}
    per_trim: dict[str, float] = {}
    for e in EPOCHS:
        ep = epochs.get(e, {})
        n = ep.get("decisions", 0)
        shares[e] = {a: _ratio(c, n) for a, c in sorted(ep.get("arms", {}).items())}
        per_trim[e] = _ratio(ep.get("proxy_regret", 0.0), ep.get("trims", 0))
    per_trim["all"] = _ratio(row.get("proxy_regret", 0.0), row.get("trims", 0))
    return {
        "decisions": row.get("decisions", 0),
        "trims": row.get("trims", 0),
        "arm_share": shares,
        "savings_tokens": row.get("savings_tokens", 0),
        "savings_reward": round(float(row.get("savings_reward", 0.0)), 6),
        "net_savings": acc["net_cost_weighted_saved"],
        "net_savings_tokens": acc["tokens_resent_removed"] - acc["recovery_tokens_resent"],
        "proxy_regret": round(float(row.get("proxy_regret", 0.0)), 6),
        "proxy_regret_per_trim": per_trim,
        "breaker_trips": row.get("breaker_trips", 0),
    }


def _role_view(row: Mapping[str, Any]) -> dict[str, Any]:
    acc = acc_view(row.get("acc", asdict(_Acc())))
    return {
        "breaker_trips": row.get("breaker_trips", 0),
        "points": {
            name: _point_view(prow, acc) for name, prow in sorted(row.get("points", {}).items())
        },
        "acc": acc,
    }


def _merge_groups(groups: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in sorted(groups):
        for role, row in groups[key].items():
            merged[role] = runner.merge_reports("sim", merged[role], row) if role in merged else row
    return merged


def summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The merged view of one split's lane report (all groups): per role ``breaker_trips``,
    ``acc`` (the compression report shape) and per point the reported figures."""
    groups = raw.get("groups", {})
    merged = _merge_groups(groups)
    out: dict[str, Any] = {
        "sessions": raw.get("sessions", 0),
        "sessions_failed": raw.get("sessions_failed", 0),
        "groups": len(groups),
    }
    for role in ROLES:
        out[role] = _role_view(copy.deepcopy(merged.get(role, {})))
    return out


def pairs(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per project group and role, the per-point figures (``{group: {role: {point: ...},
    "sessions_failed": n}}``): the paired samples of a candidate-versus-reference comparison
    (Task 9, Task 11); a group with failed sessions can be dropped as a pair."""
    out: dict[str, dict[str, Any]] = {}
    for group, roles in sorted(raw.get("groups", {}).items()):
        row: dict[str, Any] = {role: _role_view(roles.get(role, {}))["points"] for role in ROLES}
        acc = roles.get("candidate", {}).get("acc", {})
        row["sessions_failed"] = acc.get("sessions_failed", 0)  # drop such pairs if needed
        out[group] = row
    return out
