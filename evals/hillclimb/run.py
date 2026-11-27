"""Hill-climbing harness (spec 10.6, M4): search the grids pre-declared in ``space.toml`` on the
E1 **dev** split only, write one joint proposal, and confirm it **once** on the **test** split.

    python evals/hillclimb/run.py --space evals/hillclimb/space.toml --jobs N --out DIR --quiet
    python evals/hillclimb/run.py --confirm DIR/proposal.toml --jobs N --out DIR --quiet

(``DIR``: a private directory outside any git work tree.)

**Space.** ``[[entry]]`` tables ``{key, values, point}``: explicit grids only (at most
``MAX_VALUES`` values, the current default among them), every value a config key the packaged
defaults know (``config.validate_overlay``) and honoured as is by the owning module's reader
(``module_value``: inside its clamp and the invariants, e.g. ``min_tokens >= 2000``, ratios in
[0, 1]). ``[learn.tau]`` and ``[ope]`` are never searched; the M3 keys (``CONSERVATIVE``) only
in the direction of less intervention (values >= current). A space that breaks any rule fails
before any run.

**Dev search** (``climb``). Coordinate ascent in the order of ``space.toml``: one pass, and a
second only if the first moved a key (cap 2). Per coordinate the other grid values (each joined
to the current point; a value equal to its default is never written) pass the E2 gate first
(``e2_gate``: 100 % and ``inline_rate`` per level not below the defaults'), then successive
halving by project group: a seeded 25 % sample of the groups screens them (the top half goes
on), 50 % keeps the best, and 100 % decides it with the acceptance rule (``judge``) against the
current point, while proxy regret is compared with the current **defaults**. The objective is
the cost-weighted net savings of one lane, never summed across lanes: the frozen-engine
``compression`` replay (its static arm, or the arm a ``compression.levels.<arm>`` key belongs
to, gated as in production; ``hc_compression``, per group) for static keys; the ``sim`` lane
(the shipped learner, candidate versus the packaged-defaults reference, paired per group) for
the learning keys (``LEARNING``), where breaker trips may not exceed the reference engine's.
A key that no lane reads (guards, read de-dupe, M3 keys) can never show a gain, so it stays
unchanged. Written privately: ``trail.jsonl`` (every screening and verdict), ``proposal.toml``
(the joint proposal: keys that moved) and ``dev.json`` (the run's summary, the ``--quiet``
report).

**Confirmation** (``--confirm``). One-shot per plan: the first confirmation records the
proposal's sha256 and the time in the private ledger ``confirm-ledger.json`` in ``--out``
before it runs; every later ``--confirm`` (any proposal) is refused. The proposal must hold grid
values only. It runs the ``sim`` lane once on the test split (proposal and defaults in the same
paired run) and accepts all or nothing: bootstrap (by project group, ``RESAMPLES`` seeded
resamples, 95 % percentile interval) lower bound of the net-savings difference > 0; for every
point the upper bound of the proxy-regret-per-trim difference <= 0.2 tau(point) and the proxy
regret per trim <= tau(point) (``ope.tau_for``); E2 100 % and ``inline_rate`` not below the
defaults'. Writes ``confirm.json`` privately.

**Privacy.** Files go through ``replay.report.write_private`` (0600, never inside a git work
tree). ``--quiet`` stdout is only ``report: <path> id: <12 hex>``, ``status: complete|incomplete``
and one ``<key>: accepted|rejected|unchanged`` line per space key; errors name the exception
type only. Commit messages cite the report id, never a figure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python evals/hillclimb/run.py` from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.e2 import run as e2
from token_sparen.compress import load_level
from token_sparen.config import (
    OPTIONAL_KEYS,
    UNSET_FOLLOWS,
    Config,
    packaged_defaults,
    validate_overlay,
)
from token_sparen.facts import lifecycle
from token_sparen.learn import hierarchy, ope, regret
from token_sparen.learn.features import lookup_param
from token_sparen.learn.policy import COMPRESSION, StaticPolicy, get_point
from token_sparen.mechanisms import compression, guards, loopguard, prune, read_dedupe
from token_sparen.replay import replay, report, runner
from token_sparen.replay.runner import Event, SessionInfo

HERE = Path(__file__).resolve().parent
SPACE = HERE / "space.toml"
MODULE = "evals.hillclimb.run"
DEV_CUTOFF = date(2026, 11, 8)  # dev < cutoff <= test (M4 plan)
DOGFOOD_START = date(2026, 11, 26)  # sessions from here on are excluded whole
MAX_VALUES = 5
STAGES = (0.25, 0.5, 1.0)  # successive halving: share of project groups per stage
RESAMPLES = 2000
ALPHA = 0.05  # 95 % percentile interval
NONINFERIORITY = 0.2  # regret-per-trim margin, times tau(point)
MAX_PASSES = 2
LEDGER = "confirm-ledger.json"
EXCLUDED = ("learn.tau", "ope")  # never searched (prefixes of dotted keys)
LEARNING = ("learn.savings_reward", "learn.ladder_retry_decisions", "learn.defaults")
# M3 keys: only values >= the current default (less intervention)
CONSERVATIVE = frozenset(
    {
        "facts.activate_lcb",  # a candidate fact needs more evidence to be injected
        "facts.retire_ucb",  # an active fact is retired sooner
        "prune.min_sessions",  # a tool needs more idle sessions before it is pruned
        "prune.min_prompts",
        "prune.idle_minutes",
        "loopguard.min_k",  # more repeats of an error before a redirect
    }
)
M3_PREFIXES = ("facts.", "prune.", "loopguard.")
EXTRA_POINTS = frozenset({"facts", "prune"})  # owners that are not engine decision points


class SpaceError(ValueError):
    """``space.toml`` breaks a rule (checked before any run)."""


class ProposalError(ValueError):
    """A proposal holds a key or value outside the space's grids."""


class ConfirmRefused(RuntimeError):
    """The one confirmation of this plan already ran."""


# --------------------------------------------------------------------------- space


def _dig(data: Mapping[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def nest(flat: Mapping[str, Any]) -> dict[str, Any]:
    """``{"a.b": 1}`` -> ``{"a": {"b": 1}}``."""
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Leaves of nested tables as dotted keys."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{dotted}."))
        else:
            out[dotted] = value
    return out


def _num(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _cfg(key: str, value: Any) -> Config:
    return runner.apply_overlay(Config(data=packaged_defaults()), {key: value})


def _ratio(value: float) -> float | None:
    """An invariant with no module clamp: a ratio in [0, 1]."""
    return value if 0.0 <= value <= 1.0 else None


def _learn_number(cfg: Config, name: str) -> float | None:
    raw = lookup_param(cfg.data, ("learn", name), _num)
    return float(raw) if _num(raw) and float(raw) >= 0 else None  # type: ignore[arg-type]


_Reader = Callable[[Config, "re.Match[str]"], Any]
_ARMS = "|".join(a for a in COMPRESSION.arms if a != COMPRESSION.safe_arm)
READERS: list[tuple[re.Pattern[str], _Reader]] = [
    (
        re.compile(rf"compression\.levels\.({_ARMS})\.(keep_ratio|head|tail)"),
        lambda c, m: getattr(load_level(c, m[1]), m[2]),
    ),
    (
        re.compile(r"compression\.min_saving_ratio"),
        lambda c, m: _ratio(compression._num(c, m[0], -1.0)),
    ),
    (re.compile(r"compression\.min_tokens"), lambda c, m: compression.min_tokens(c)),
    (
        re.compile(r"read_dedupe\.min_tokens"),
        lambda c, m: max(read_dedupe.MIN_TOKENS, read_dedupe._cfg_int(c, m[0], 0)),
    ),
    (
        re.compile(r"read_dedupe\.(max_entries)"),
        lambda c, m: read_dedupe._cfg_int(c, m[0], -1),
    ),
    (
        re.compile(r"read_dedupe\.max_turn_gap"),
        lambda c, m: (lambda v: v if v is not None and v >= 0 else None)(
            read_dedupe._int_or_none(c.get(m[0]))
        ),
    ),
    (
        re.compile(r"guards\.pretrim_bounded_keep_ratio"),
        lambda c, m: guards.pretrim_level(c, guards.PRETRIM_LINES).keep_ratio,
    ),
    (
        re.compile(r"guards\.(pretrim_lines|pretrim_head|max_read_bytes)"),
        lambda c, m: guards._int(c, m[0], -1),
    ),
    (re.compile(r"learn\.savings_reward"), lambda c, m: regret.savings_mode(c)),
    (
        re.compile(r"learn\.(ladder_retry_decisions|resend_horizon)"),
        lambda c, m: _learn_number(c, m[1]),
    ),
    (
        re.compile(r"learn\.defaults\.(\w+)\.(\w+)\.(savings|regret)"),
        lambda c, m: hierarchy.designed_default(m[1], m[2], m[3], c.data),  # type: ignore[arg-type]
    ),
    (
        re.compile(r"learn\.defaults\.(\w+)\.(\w+)\.(savings|regret)_strength"),
        lambda c, m: hierarchy.designed_strength(m[1], m[2], m[3], c.data),  # type: ignore[arg-type]
    ),
    (
        re.compile(r"facts\.(activate_lcb|retire_ucb|withhold_floor|max_tokens|max_facts)"),
        lambda c, m: getattr(lifecycle.settings(c.data), m[1]),
    ),
    (
        re.compile(r"prune\.(min_sessions|min_prompts)"),
        lambda c, m: getattr(prune.settings(c.data), m[1]),
    ),
    (re.compile(r"prune\.idle_minutes"), lambda c, m: prune.settings(c.data).idle_s / 60.0),
    (re.compile(r"loopguard\.min_k"), lambda c, m: loopguard._param(c, "min_k", -1)),
]


def module_value(key: str, value: Any) -> Any:
    """The value the owning module's reader sees for ``key = value`` (over the packaged
    defaults): equal to ``value`` iff it is inside the module's clamp and the invariants."""
    for pattern, read in READERS:
        m = pattern.fullmatch(key)
        if m is not None:
            return read(_cfg(key, value), m)
    raise SpaceError(f"{key}: no known reader (cannot check its clamp)")


def _default(key: str) -> Any:
    value = _dig(packaged_defaults(), key)
    if value is None and key in OPTIONAL_KEYS:  # unset: follows another key
        follows = UNSET_FOLLOWS[tuple(key.split("."))]
        value = _dig(packaged_defaults(), ".".join(follows))
    return value


def _lane(key: str) -> str:
    return "sim" if key.startswith(LEARNING) else "compression"


@dataclass(frozen=True)
class Coordinate:
    key: str
    values: tuple[Any, ...]
    point: str
    default: Any
    lane: str  # "compression" or "sim": the objective's one lane


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b or (_num(a) and _num(b) and float(a) == float(b))


def coordinate(key: Any, values: Any, point: Any) -> Coordinate:
    """One checked ``space.toml`` entry (``SpaceError`` on any broken rule)."""
    if not isinstance(key, str) or not key:
        raise SpaceError("entry without a key")
    if any(key == p or key.startswith(f"{p}.") for p in EXCLUDED):
        raise SpaceError(f"{key}: tau and [ope] are never searched")
    if not isinstance(point, str) or not (get_point(point) or point in EXTRA_POINTS):
        raise SpaceError(f"{key}: unknown point {point!r}")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
        raise SpaceError(f"{key}: 1 to {MAX_VALUES} values")
    if any(_same(a, b) for i, a in enumerate(values) for b in values[i + 1 :]):
        raise SpaceError(f"{key}: repeated value")
    default = _default(key)
    if default is None:
        raise SpaceError(f"{key}: not a config key")
    if not any(_same(v, default) for v in values):
        raise SpaceError(f"{key}: the grid must include the current default")
    if key.startswith(M3_PREFIXES):
        if key not in CONSERVATIVE:
            raise SpaceError(f"{key}: an M3 key with no known conservative direction")
        if any(not _num(v) or v < default for v in values):
            raise SpaceError(f"{key}: M3 keys only move toward less intervention")
    for value in values:
        try:
            validate_overlay(nest({key: value}))
        except ValueError:
            raise SpaceError(f"{key}: {value!r} is not a valid config value") from None
        seen = module_value(key, value)
        if not _same(seen, value):
            raise SpaceError(f"{key}: {value!r} is outside the module's clamp")
    return Coordinate(key, tuple(values), point, default, _lane(key))


def load_space(path: Path) -> list[Coordinate]:
    """The checked coordinates of ``space.toml``, in file order."""
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SpaceError("unreadable space file") from None
    entries = raw.get("entry")
    if not isinstance(entries, list) or not entries:
        raise SpaceError("no [[entry]] tables")
    coords: list[Coordinate] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) - {"key", "values", "point"}:
            raise SpaceError("an entry holds only key, values and point")
        coords.append(coordinate(item.get("key"), item.get("values"), item.get("point")))
    keys = [c.key for c in coords]
    if len(set(keys)) != len(keys):
        raise SpaceError("a key is listed twice")
    return coords


def joined(current: Mapping[str, Any], coord: Coordinate, value: Any) -> dict[str, Any]:
    """The current point with ``coord`` set to ``value`` (a default value is never written)."""
    out = {k: v for k, v in current.items() if k != coord.key}
    if not _same(value, coord.default):
        out[coord.key] = value
    return out


# --------------------------------------------------------------------------- statistics


@dataclass
class Row:
    """One project group's figures under one configuration."""

    net: float  # cost-weighted net savings (saved minus recovery reads)
    trims: dict[str, float] = field(default_factory=dict)  # per point
    regret: dict[str, float] = field(default_factory=dict)  # proxy regret sum per point
    trips: int = 0  # circuit-breaker trips (sim lane)


Rows = dict[str, Row]  # group key -> row


@dataclass
class Verdict:
    admissible: bool
    reasons: list[str]
    stats: dict[str, Any]


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    i = min(len(sorted_xs) - 1, max(0, math.ceil(q * len(sorted_xs)) - 1))
    return sorted_xs[i]


def _per_trim(rows: Iterable[Row], point: str) -> float:
    trims = regrets = 0.0
    for r in rows:
        trims += r.trims.get(point, 0.0)
        regrets += r.regret.get(point, 0.0)
    return regrets / trims if trims > 0 else 0.0


def judge(cand: Rows, base: Rows, ref: Rows, *, seed: str, breaker: bool = False) -> Verdict:
    """The acceptance rule, paired by project group: bootstrap 95 % lower bound of the net
    savings difference (``cand - base``) > 0; per point, upper bound of the proxy-regret-per-trim
    difference (``cand - ref``, ref = the current defaults) <= 0.2 tau and ``cand``'s proxy
    regret per trim <= tau; with ``breaker``, ``cand``'s breaker trips <= ``ref``'s."""
    groups = sorted(set(cand) | set(base) | set(ref))
    empty = Row(0.0)
    c = [cand.get(g, empty) for g in groups]
    b = [base.get(g, empty) for g in groups]
    r = [ref.get(g, empty) for g in groups]
    points = sorted({p for row in (*c, *r) for p in row.trims})
    rng = random.Random(f"hillclimb|{seed}")
    nets: list[float] = []
    diffs: dict[str, list[float]] = {p: [] for p in points}
    n = len(groups)
    for _ in range(RESAMPLES if n else 0):
        idx = [rng.randrange(n) for _ in range(n)]
        nets.append(sum(c[i].net - b[i].net for i in idx))
        for p in points:
            diffs[p].append(_per_trim((c[i] for i in idx), p) - _per_trim((r[i] for i in idx), p))
    nets.sort()
    reasons: list[str] = []
    lb = _quantile(nets, ALPHA / 2)
    stats: dict[str, Any] = {
        "groups": n,
        "resamples": len(nets),
        "net_diff": sum(x.net - y.net for x, y in zip(c, b, strict=True)),
        "net_diff_lb": lb,
        "points": {},
    }
    if not lb > 0:
        reasons.append("net_savings")
    for p in points:
        tau = ope.tau_for(p)
        ub = _quantile(sorted(diffs[p]), 1 - ALPHA / 2)
        per = _per_trim(c, p)
        stats["points"][p] = {
            "tau": tau,
            "regret_per_trim": per,
            "regret_per_trim_ref": _per_trim(r, p),
            "regret_diff_ub": ub,
        }
        if ub > NONINFERIORITY * tau:
            reasons.append(f"regret_noninferiority:{p}")
        if per > tau:
            reasons.append(f"regret_tau:{p}")
    if breaker:
        trips, ref_trips = sum(x.trips for x in c), sum(x.trips for x in r)
        stats["breaker_trips"], stats["breaker_trips_ref"] = trips, ref_trips
        if trips > ref_trips:
            reasons.append("breaker_trips")
    return Verdict(not reasons, reasons, stats)


_E2_CACHE: dict[str, tuple[bool, list[str]]] = {}
_E2_BASE: list[e2.E2Report] = []  # the defaults' E2 report, computed once


def e2_gate(overlay: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """E2 at 100 % and ``inline_rate`` per level not below the current defaults'."""
    key = json.dumps(overlay, sort_keys=True)
    if key not in _E2_CACHE:
        base = _E2_BASE[0] if _E2_BASE else e2.run()
        _E2_BASE[:] = [base]
        rep = e2.run(params=nest(overlay)) if overlay else base
        reasons: list[str] = []
        if rep.failures or rep.passed != rep.cases:
            reasons.append("e2")
        for level, rate in base.inline_rate.items():
            if rep.inline_rate.get(level, 0.0) < rate:
                reasons.append(f"e2_inline:{level}")
        _E2_CACHE[key] = (not reasons, reasons)
    ok, reasons = _E2_CACHE[key]
    return ok, list(reasons)


# --------------------------------------------------------------------------- search

# (lane, {label: flat overlay}, groups) -> {label: (rows of the overlay, rows of the defaults)}
Evaluate = Callable[
    [str, Mapping[str, Mapping[str, Any]], Sequence[str]], dict[str, tuple[Rows, Rows]]
]
E2Gate = Callable[[Mapping[str, Any]], tuple[bool, list[str]]]


def _sample(groups: Sequence[str], seed: int, key: str, share: float) -> list[str]:
    """The seeded first ``share`` of a fixed shuffle of the groups (nested across stages)."""
    order = sorted(groups)
    random.Random(f"hillclimb|{seed}|{key}").shuffle(order)
    return order[: max(1, math.ceil(share * len(order)))] if order else []


def _score(cand: Rows, base: Rows) -> float:
    return sum(r.net for r in cand.values()) - sum(r.net for r in base.values())


def _step(
    coord: Coordinate,
    current: dict[str, Any],
    evaluate: Evaluate,
    groups: Sequence[str],
    seed: int,
    e2_check: E2Gate,
    log: Callable[[dict[str, Any]], None],
    pass_no: int,
) -> Any:
    """One coordinate: the value it ends at (the current one unless a grid value is admissible)."""
    now = current.get(coord.key, coord.default)
    head = {"split": "dev", "pass": pass_no, "key": coord.key, "lane": coord.lane}
    survivors: list[tuple[int, Any]] = []
    for i, value in enumerate(coord.values):
        if _same(value, now):
            continue
        ok, reasons = e2_check(joined(current, coord, value))
        if ok:
            survivors.append((i, value))
        else:
            log({**head, "value": value, "stage": "e2", "admissible": False, "reasons": reasons})
    if not survivors:
        return now
    rows: dict[str, tuple[Rows, Rows]] = {}
    sample: list[str] = []
    for share in STAGES:
        final = share == STAGES[-1]
        if not final and len(survivors) == 1:
            continue
        sample = _sample(groups, seed, coord.key, share)
        overlays = {"base": dict(current)}
        overlays.update({f"v{i}": joined(current, coord, v) for i, v in survivors})
        rows = evaluate(coord.lane, overlays, sample)
        scored = []
        for i, value in survivors:
            s = _score(rows[f"v{i}"][0], rows["base"][0])
            scored.append((-s, i, value))
            log(
                {
                    **head,
                    "value": value,
                    "stage": f"{round(100 * share)}%",
                    "groups": len(sample),
                    "net_diff": s,
                }
            )
        scored.sort(key=lambda t: (t[0], t[1]))
        keep = 1 if share >= STAGES[1] else math.ceil(len(scored) / 2)
        survivors = [(i, v) for _, i, v in scored[:keep]]
    (i, best) = survivors[0]
    cand, ref = rows[f"v{i}"]
    verdict = judge(
        cand,
        rows["base"][0],
        ref,
        seed=f"{seed}|{pass_no}|{coord.key}|{best!r}",
        breaker=coord.lane == "sim",
    )
    log(
        {
            **head,
            "value": best,
            "stage": "verdict",
            "groups": len(sample),
            "admissible": verdict.admissible,
            "reasons": verdict.reasons,
            "stats": verdict.stats,
        }
    )
    return best if verdict.admissible else now


def climb(
    coords: Sequence[Coordinate],
    evaluate: Evaluate,
    groups: Sequence[str],
    *,
    seed: int = 0,
    e2: E2Gate = e2_gate,
    trail: list[dict[str, Any]] | None = None,
    on_step: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Coordinate ascent over ``coords`` (successive halving per coordinate); the joint
    proposal as dotted keys (only keys that moved from their default)."""
    current: dict[str, Any] = {}

    def log(row: dict[str, Any]) -> None:
        if trail is not None:
            trail.append(row)

    for pass_no in range(1, MAX_PASSES + 1):
        changed = False
        for coord in coords:
            before = current.get(coord.key, coord.default)
            value = _step(coord, current, evaluate, groups, seed, e2, log, pass_no)
            if not _same(value, before):
                current = joined(current, coord, value)
                changed = True
            if on_step is not None:
                on_step()
        if not changed:
            break
    return current


# --------------------------------------------------------------------------- lanes


class GroupedCompression:
    """The ``compression`` lane (frozen static engine, production quality gate) reported per
    project group (``runner.LANES["hc_compression"]``). Params: ``arm`` (the static arm, default
    the packaged one), ``overlay``, ``seed``."""

    history = False

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.inner = replay.CompressionLane(cfg, {"policy": "default", "seed": params.get("seed")})
        arm = params.get("arm")
        if isinstance(arm, str) and arm in COMPRESSION.arms:  # still gated: named "default"
            self.inner.policies = [("default", StaticPolicy({COMPRESSION.name: arm}))]
        self.group: str | None = None

    def feed(self, event: Event, session: SessionInfo) -> None:
        self.group = session.group
        self.inner.feed(event, session)

    def end_session(self, session: SessionInfo) -> None:
        self.inner.end_session(session)

    def abort_session(self, session: SessionInfo) -> None:
        self.inner.abort_session(session)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.group is None:
            return out
        for part, by_name in self.inner.report().items():
            acc = by_name["default"]
            if acc["sessions"] + acc["sessions_failed"]:
                out[part] = {"groups": {self.group: acc}}
        return out


runner.LANES["hc_compression"] = GroupedCompression


def _arm_of(key: str) -> str | None:
    m = re.fullmatch(rf"compression\.levels\.({_ARMS})\..+", key)
    return m[1] if m else None


def compression_rows(raw: Mapping[str, Any]) -> Rows:
    out: Rows = {}
    for group, acc in raw.get("groups", {}).items():
        view = replay.acc_view(acc)
        trims = float(view["trimmed"])
        out[group] = Row(
            float(view["net_cost_weighted_saved"]),
            {COMPRESSION.name: trims},
            {COMPRESSION.name: float(view["proxy_regrets"])},
        )
    return out


def sim_rows(raw: Mapping[str, Any]) -> tuple[Rows, Rows]:
    """(candidate rows, reference rows) of one ``sim`` lane report."""
    cand: Rows = {}
    ref: Rows = {}
    for group, roles in raw.get("groups", {}).items():
        for role, into in (("candidate", cand), ("reference", ref)):
            row = roles.get(role, {})
            view = replay.acc_view(row.get("acc", {}))
            points = row.get("points", {})
            into[group] = Row(
                float(view["net_cost_weighted_saved"]),
                {p: float(v.get("trims", 0)) for p, v in points.items()},
                {p: float(v.get("proxy_regret", 0.0)) for p, v in points.items()},
                int(row.get("breaker_trips", 0)),
            )
    return cand, ref


@dataclass
class RunLog:
    runs: int = 0
    worker_errors: dict[str, int] = field(default_factory=dict)
    failed_groups: dict[str, int] = field(default_factory=dict)
    sessions_failed: int = 0

    @property
    def complete(self) -> bool:
        return not self.failed_groups

    def add(self, res: runner.RunResult) -> None:
        self.runs += 1
        self.sessions_failed += res.sessions_failed
        for src, into in (
            (res.worker_errors, self.worker_errors),
            (res.failed_groups, self.failed_groups),
        ):
            for k, v in src.items():
                into[k] = into.get(k, 0) + v


class Evaluator:
    """``Evaluate`` over real runs: one ``runner.run_parallel`` pass per call, every label a lane
    of that pass, on ``split`` only."""

    def __init__(self, paths: Sequence[Path], *, jobs: int, seed: int, split: str) -> None:
        self.paths, self.jobs, self.seed, self.split = list(paths), jobs, seed, split
        self.log = RunLog()

    def run(self, specs: list[runner.LaneSpec], groups: Sequence[str] | None) -> dict[str, Any]:
        res = runner.run_parallel(
            self.paths,
            specs,
            jobs=self.jobs,
            cutoff=DEV_CUTOFF,
            dogfood_start=DOGFOOD_START,
            groups=set(groups) if groups is not None else None,
            split=self.split,
            cfg=Config(data=packaged_defaults()),  # never the user's, project's or env config
        )
        self.log.add(res)
        out: dict[str, Any] = res.by_split.get(self.split, {})
        return out

    def __call__(
        self, lane: str, overlays: Mapping[str, Mapping[str, Any]], groups: Sequence[str]
    ) -> dict[str, tuple[Rows, Rows]]:
        if lane == "sim":
            specs: list[runner.LaneSpec] = [
                ("sim", {"overlay": nest(o), "seed": self.seed, "label": f"hc_{label}"})
                for label, o in overlays.items()
            ]
            got = self.run(specs, groups)
            return {label: sim_rows(got.get(f"hc_{label}", {})) for label in overlays}
        arms = {_arm_of(k) for o in overlays.values() for k in o} - {None}
        key_arm = next(iter(arms)) if len(arms) == 1 else None
        labels = {**{f"hc_{k}": dict(v) for k, v in overlays.items()}, "hc__defaults": {}}
        specs = [
            (
                "hc_compression",
                {
                    "module": MODULE,
                    "overlay": nest(o),
                    "seed": self.seed,
                    "label": label,
                    **({"arm": key_arm} if key_arm else {}),
                },
            )
            for label, o in labels.items()
        ]
        got = self.run(specs, groups)
        ref = compression_rows(got.get("hc__defaults", {}))
        return {label: (compression_rows(got.get(f"hc_{label}", {})), ref) for label in overlays}


# --------------------------------------------------------------------------- files


def _toml(flat: Mapping[str, Any]) -> str:
    head = "# token-sparen hill-climbing proposal (selected on the E1 dev split; private)\n"
    return head + (e2._toml_dump(nest(flat)) if flat else "")


def load_proposal(path: Path, coords: Sequence[Coordinate]) -> dict[str, Any]:
    """The proposal's moved keys (dotted); ``ProposalError`` unless every value is on a grid."""
    try:
        flat = flatten(tomllib.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise ProposalError("unreadable proposal") from None
    by_key = {c.key: c for c in coords}
    out: dict[str, Any] = {}
    for key, value in flat.items():
        coord = by_key.get(key)
        if coord is None or not any(_same(value, v) for v in coord.values):
            raise ProposalError(f"{key}: not a grid value")
        out = joined(out, coord, value)
    return out


def _summary(rows: Rows) -> dict[str, Any]:
    points = sorted({p for r in rows.values() for p in r.trims})
    return {
        "groups": len(rows),
        "net_savings": sum(r.net for r in rows.values()),
        "breaker_trips": sum(r.trips for r in rows.values()),
        "points": {
            p: {
                "trims": sum(r.trims.get(p, 0.0) for r in rows.values()),
                "proxy_regret": sum(r.regret.get(p, 0.0) for r in rows.values()),
                "proxy_regret_per_trim": _per_trim(rows.values(), p),
            }
            for p in points
        },
    }


def _emit(path: Path, complete: bool, lines: Sequence[str]) -> None:
    print(report.quiet_line(path))
    print(f"status: {'complete' if complete else 'incomplete'}")
    for line in lines:
        print(line)


def _groups(paths: Sequence[Path]) -> list[str]:
    return sorted(runner.group_files(paths))


def dev(
    coords: Sequence[Coordinate],
    paths: Sequence[Path],
    out: Path,
    *,
    jobs: int,
    seed: int,
    space_sha: str,
) -> tuple[Path, bool, list[str]]:
    evaluator = Evaluator(paths, jobs=jobs, seed=seed, split="dev")
    trail: list[dict[str, Any]] = []

    def save_trail() -> None:
        text = "".join(json.dumps(row, sort_keys=True, default=repr) + "\n" for row in trail)
        report.write_private(out / "trail.jsonl", text, raw=True)

    proposal = climb(coords, evaluator, _groups(paths), seed=seed, trail=trail, on_step=save_trail)
    save_trail()
    report.write_private(out / "proposal.toml", _toml(proposal), raw=True)
    keys = {c.key: ("accepted" if c.key in proposal else "unchanged") for c in coords}
    doc = {
        "version": 1,
        "kind": "hillclimb-dev",
        "split": "dev",
        "cutoff": DEV_CUTOFF.isoformat(),
        "dogfood_start": DOGFOOD_START.isoformat(),
        "generated": datetime.now(UTC).date().isoformat(),
        "space_sha256": space_sha,
        "seed": seed,
        "proposal": proposal,
        "keys": keys,
        "complete": evaluator.log.complete,
        "run": asdict(evaluator.log),
    }
    path = report.write_private(out / "dev.json", doc)
    return path, evaluator.log.complete, [f"{k}: {v}" for k, v in keys.items()]


def confirm(
    coords: Sequence[Coordinate],
    proposal_path: Path,
    paths: Sequence[Path],
    out: Path,
    *,
    jobs: int,
    seed: int,
) -> tuple[Path, bool, list[str]]:
    proposal = load_proposal(proposal_path, coords)  # before the one shot is used
    sha = hashlib.sha256(Path(proposal_path).read_bytes()).hexdigest()
    ledger = out / LEDGER
    if ledger.exists():
        raise ConfirmRefused("the confirmation of this plan already ran")
    report.write_private(
        ledger, {"proposal_sha256": sha, "recorded": datetime.now(UTC).isoformat()}
    )
    e2_ok, e2_reasons = e2_gate(proposal)
    evaluator = Evaluator(paths, jobs=jobs, seed=seed, split="test")
    got = evaluator.run([("sim", {"overlay": nest(proposal), "seed": seed, "label": "sim"})], None)
    cand, ref = sim_rows(got.get("sim", {}))
    verdict = judge(cand, ref, ref, seed=f"{seed}|confirm|{sha}")
    complete = evaluator.log.complete
    accepted = bool(proposal) and verdict.admissible and e2_ok and complete
    reasons = [*verdict.reasons, *e2_reasons, *([] if complete else ["incomplete"])]
    doc = {
        "version": 1,
        "kind": "hillclimb-confirm",
        "split": "test",
        "cutoff": DEV_CUTOFF.isoformat(),
        "dogfood_start": DOGFOOD_START.isoformat(),
        "generated": datetime.now(UTC).date().isoformat(),
        "proposal_sha256": sha,
        "proposal_overlay": proposal,
        "seed": seed,
        "baseline": _summary(ref),
        "proposal": _summary(cand),
        "verdict": {"reasons": reasons, "stats": verdict.stats},
        "accepted": accepted,
        "complete": complete,
        "run": asdict(evaluator.log),
    }
    path = report.write_private(out / "confirm.json", doc)
    word = "accepted" if accepted else "rejected"
    lines = [f"{c.key}: {word if c.key in proposal else 'unchanged'}" for c in coords]
    return path, complete, lines


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hillclimb", description=__doc__.split("\n\n")[0])
    ap.add_argument("--space", type=Path, default=SPACE, help="the pre-declared grids")
    ap.add_argument(
        "--transcripts",
        type=Path,
        nargs="+",
        help="transcript files or directories (default: Claude Code's projects directory)",
    )
    ap.add_argument("--out", type=Path, required=True, help="private directory (outside git)")
    ap.add_argument("--jobs", type=int, default=1, help="worker processes (0 = one per CPU)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--confirm", type=Path, help="the proposal to confirm once on test")
    ap.add_argument("--quiet", action="store_true", help="print only the fixed template")
    args = ap.parse_args(argv)
    try:
        coords = load_space(args.space)
    except SpaceError:
        print("hillclimb: invalid space (SpaceError)", file=sys.stderr)
        return 2
    try:
        out = Path(os.path.expanduser(str(args.out)))
        report.check_out_path(out / "dev.json")
        jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 1)
        paths = args.transcripts or [Path.home() / ".claude" / "projects"]
        if args.confirm is not None:
            path, complete, lines = confirm(
                coords, args.confirm, paths, out, jobs=jobs, seed=args.seed
            )
        else:
            space_sha = hashlib.sha256(Path(args.space).read_bytes()).hexdigest()
            path, complete, lines = dev(
                coords, paths, out, jobs=jobs, seed=args.seed, space_sha=space_sha
            )
    except ProposalError:
        print("hillclimb: invalid proposal (ProposalError)", file=sys.stderr)
        return 2
    except ConfirmRefused:
        print("hillclimb: confirm refused (already run once)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never echo a message that could quote data
        print(f"hillclimb failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    _emit(path, complete, lines)
    if not args.quiet:
        print("hillclimb: private files written (mode 0600); cite the report id only")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
