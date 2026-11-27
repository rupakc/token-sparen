"""Hill-climbing harness (spec 10.6, M4): search the grids pre-declared in ``space.toml`` on the
E1 **dev** split only, write one joint proposal, and confirm it **once** on the **test** split.

    python evals/hillclimb/run.py --space evals/hillclimb/space.toml --jobs N --out DIR --quiet
    python evals/hillclimb/run.py --confirm DIR/proposal.toml --jobs N --out DIR --quiet

(``DIR``: a private directory outside any git work tree.) ``--campaign ID`` (default ``m4``),
``--cutoff`` (default 2026-11-08, ``replay.DEFAULT_CUTOFF``) and ``--dogfood-start`` (default
2026-11-26) set the campaign and its split; a new campaign needs a new id (and its own ledger).

**Space.** ``[[entry]]`` tables ``{key, values, point[, measured_by]}``: explicit grids only
(at most ``MAX_VALUES`` values, the current default among them), every value a config key the
packaged defaults know (``config.validate_overlay``) and honoured as is by the owning module's
reader (``module_value``: inside its clamp and the invariants, e.g. ``min_tokens >= 2000``,
ratios in [0, 1]). ``[learn.tau]`` and ``[ope]`` are never searched; the M3 keys
(``CONSERVATIVE``) only in the direction of less intervention (values >= current). Keys no E1
lane replays (``UNMEASURED``: guards, read de-dupe, M3) must say ``measured_by = "none"``: they
are never run, reported ``unmeasured`` and never changed. A space that breaks any rule fails
before any run.

**Dev search** (``climb``). Coordinate ascent in the order of ``space.toml``: one pass, and a
second only if the first moved a key (cap 2). Per coordinate the other grid values (each joined
to the current point; a value equal to its default is never written) pass the E2 gate first
(``e2_gate``: 100 % and ``inline_rate`` per level not below the defaults'), then successive
halving over the project groups that have a dev session: a seeded 25 % sample screens them (the
top half goes on), 50 % keeps the best, and 100 % decides it with the acceptance rule
(``judge``) against the current point, while proxy regret is compared with the current
**defaults**. The objective is the cost-weighted net savings of one lane, never summed across
lanes: the ``compression`` replay per group (``hc_compression``, gated as in production) for
static keys -- a ``compression.levels.<arm>`` key on the arm it governs, every other key under
the production policy (the learner frozen on its packaged priors in an empty temporary state
root); the ``sim`` lane (the shipped learner, candidate versus the packaged-defaults reference,
paired per group, rewarded with the re-sends the live hook projects; one run per seed of
``SIM_SEEDS`` in the same pass, averaged per group) for the learning keys (``LEARNING``), where
breaker trips may not exceed the reference engine's. No lane replays read de-dupe, so the
objective has no read de-dupe term.
Written privately: ``trail.jsonl`` (every screening and verdict), ``proposal.toml`` (the joint
proposal: keys that moved) and ``dev.json`` (the run's summary with the proposal's sha256, the
``--quiet`` report).

**Evidence.** Nothing is decided on fewer than ``MIN_GROUPS`` paired project groups with a trim
or fewer than ``MIN_TRIMS`` trims per evaluated point on either side: the verdict is
``insufficient`` (the coordinate stays; a confirmation rejects).

**Confirmation** (``--confirm``). One-shot per campaign: the proposal is checked (grid values of
measured keys only), then the ledger ``<state root>/hillclimb/confirm-<campaign>.json``
(whatever ``--out`` is) is created exclusively (``O_CREAT | O_EXCL``, 0600) with the campaign,
the proposal's sha256 and the time **before** any test outcome is computed (the runner's
date/marker pre-scan of the files is not an outcome); if it exists, the confirmation is refused,
so of any number of confirmations of a campaign exactly one runs. A deleted ledger cannot be
detected: any second look at test invalidates the result (cite the ``confirm.json`` id, which
records the ledger). It reads the test split once: the ``sim`` lane (proposal and defaults in
the same paired run, every seed of ``SIM_SEEDS``, averaged per group) and, for every
``compression.levels.<arm>`` key in the proposal, the ``compression`` lane on that arm
(proposal versus defaults, paired by group). It accepts all or nothing: on the sim lane the
bootstrap (by project group, ``RESAMPLES`` seeded resamples, 95 % percentile interval) lower
bound of the net-savings difference > 0, and for every point the upper bound of the
proxy-regret-per-trim difference <= 0.2 tau(point) and the proxy regret per trim <= tau(point)
(``ope.tau_for``); per proposed arm the same two regret bounds and the minimum evidence (reasons
``arm:<arm>:...``); E2 100 % and ``inline_rate`` not below the defaults'. Breaker trips are
reported, not gated (the plan's acceptance rule does not list them). Writes ``confirm.json``
privately (``dev_report``: the id of the ``dev.json`` beside the proposal that wrote it, else
null; ``arms``: the per-arm figures).

**Privacy.** Files go through ``replay.report.write_private`` (0600, never inside a git work
tree). ``--quiet`` stdout is only ``report: <path> id: <12 hex>``, ``status: complete|incomplete``
and one ``<key>: accepted|rejected|unchanged|unmeasured`` line per space key; errors name the
exception type only. Commit messages cite the report id, never a figure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import tomllib
import weakref
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
    state_home,
    validate_overlay,
)
from token_sparen.facts import lifecycle
from token_sparen.learn import hierarchy, ope, regret
from token_sparen.learn.engine import Engine
from token_sparen.learn.features import lookup_param
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, StaticPolicy, get_point
from token_sparen.mechanisms import compression, guards, loopguard, prune, read_dedupe
from token_sparen.replay import replay, report, runner
from token_sparen.replay.runner import Event, SessionInfo

HERE = Path(__file__).resolve().parent
SPACE = HERE / "space.toml"
MODULE = "evals.hillclimb.run"
DEV_CUTOFF = date(2026, 11, 8)  # dev < cutoff <= test (M4 plan; = replay's default); --cutoff
DOGFOOD_START = date(2026, 11, 26)  # sessions from here on are excluded whole; --dogfood-start
CAMPAIGN = "m4"  # ``--campaign``: one confirmation (one ledger) per campaign
CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SIM_SEEDS = (0, 1, 2)  # sim lane: seed offsets per evaluation (mean per group), M4-G2
MAX_VALUES = 5
STAGES = (0.25, 0.5, 1.0)  # successive halving: share of project groups per stage
RESAMPLES = 2000
ALPHA = 0.05  # 95 % percentile interval
NONINFERIORITY = 0.2  # regret-per-trim margin, times tau(point)
MAX_PASSES = 2
LEDGER_DIR = "hillclimb"  # under the state root: ``confirm-<campaign>.json``
MIN_GROUPS = 8  # project groups with >= 1 trim needed before anything is decided
MIN_TRIMS = 30  # trims per evaluated point (each side) needed before anything is decided
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
EXTRA_POINTS = frozenset({"facts", "prune", "guards"})  # owners that are not decision points
# keys no E1 lane replays (guard blocks, read de-dupe, M3 mechanisms): listed so a choice is
# never off-grid, labelled ``measured_by = "none"``, never run and never changed
UNMEASURED = ("guards.", "read_dedupe.", "facts.", "prune.", "loopguard.")
MEASURED_BY = ("compression", "sim", "none")


class SpaceError(ValueError):
    """``space.toml`` breaks a rule (checked before any run)."""


class ProposalError(ValueError):
    """A proposal holds a key or value outside the space's grids."""


class ConfirmRefused(RuntimeError):
    """The one confirmation of this campaign already ran."""


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
    if key.startswith(UNMEASURED):
        return "none"
    return "sim" if key.startswith(LEARNING) else "compression"


@dataclass(frozen=True)
class Coordinate:
    key: str
    values: tuple[Any, ...]
    point: str
    default: Any
    lane: str  # "compression" or "sim": the objective's one lane; "none": never measured


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b or (_num(a) and _num(b) and float(a) == float(b))


def coordinate(key: Any, values: Any, point: Any, measured_by: Any = None) -> Coordinate:
    """One checked ``space.toml`` entry (``SpaceError`` on any broken rule). ``measured_by``
    must be ``"none"`` exactly for the keys no lane replays; if given otherwise, it must name
    the key's lane."""
    if not isinstance(key, str) or not key:
        raise SpaceError("entry without a key")
    lane = _lane(key)
    if measured_by is not None and measured_by not in MEASURED_BY:
        raise SpaceError(f"{key}: measured_by is one of {', '.join(MEASURED_BY)}")
    if (lane == "none") != (measured_by == "none") or measured_by not in (None, lane):
        raise SpaceError(f"{key}: measured_by must be {lane!r}")
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
    return Coordinate(key, tuple(values), point, default, lane)


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
        if not isinstance(item, dict) or set(item) - {"key", "values", "point", "measured_by"}:
            raise SpaceError("an entry holds only key, values, point and measured_by")
        coords.append(
            coordinate(
                item.get("key"), item.get("values"), item.get("point"), item.get("measured_by")
            )
        )
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
    trips: float = 0  # circuit-breaker trips (sim lane; a mean over seeds)


Rows = dict[str, Row]  # group key -> row


@dataclass
class Verdict:
    admissible: bool
    reasons: list[str]
    stats: dict[str, Any]
    insufficient: bool = False  # too little evidence: nothing decided (never admissible)


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


def judge(
    cand: Rows,
    base: Rows,
    ref: Rows,
    *,
    seed: str,
    breaker: bool = False,
    net: bool = True,
) -> Verdict:
    """The acceptance rule, paired by project group (groups every side reports): with at least
    ``MIN_GROUPS`` groups that trimmed and ``MIN_TRIMS`` trims per evaluated point on each side
    (else ``insufficient``: nothing decided), bootstrap 95 % lower bound of the net savings
    difference (``cand - base``) > 0 (only with ``net``; without it, the quality half alone);
    per point, upper bound of the proxy-regret-per-trim difference (``cand - ref``, ref = the
    current defaults) <= 0.2 tau and ``cand``'s proxy regret per trim <= tau; with ``breaker``,
    ``cand``'s breaker trips <= ``ref``'s."""
    every = set(cand) | set(base) | set(ref)
    groups = sorted(set(cand) & set(base) & set(ref))
    c = [cand[g] for g in groups]
    b = [base[g] for g in groups]
    r = [ref[g] for g in groups]
    points = sorted({p for row in (*c, *r) for p, t in row.trims.items() if t > 0})
    trimmed = sum(
        1
        for x, y, z in zip(c, b, r, strict=True)
        if any(sum(v.trims.values()) > 0 for v in (x, y, z))
    )
    evidence: list[str] = []
    if trimmed < MIN_GROUPS:
        evidence.append("insufficient:groups")
    for p in points:
        if min(sum(v.trims.get(p, 0.0) for v in side) for side in (c, r)) < MIN_TRIMS:
            evidence.append(f"insufficient:trims:{p}")
    if not points and "insufficient:groups" not in evidence:
        evidence.append("insufficient:trims")
    if evidence:
        stats0 = {
            "groups": len(groups),
            "groups_trimmed": trimmed,
            "unpaired": len(every) - len(groups),
        }
        return Verdict(False, evidence, stats0, True)
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
        "groups_trimmed": trimmed,
        "unpaired": len(every) - len(groups),
        "resamples": len(nets),
        "net_diff": sum(x.net - y.net for x, y in zip(c, b, strict=True)),
        "net_diff_lb": lb,
        "points": {},
    }
    if net and not lb > 0:
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

# (lane, {label: flat overlay}, groups, the coordinate's key)
#   -> {label: (rows of the overlay, rows of the defaults)}
Evaluate = Callable[
    [str, Mapping[str, Mapping[str, Any]], Sequence[str], str], dict[str, tuple[Rows, Rows]]
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
    if coord.lane == "none":  # no lane replays it: never run, never changed
        log({**head, "stage": "unmeasured"})
        return now
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
        rows = evaluate(coord.lane, overlays, sample, coord.key)
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
            "insufficient": verdict.insufficient,
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


PRIOR_KEY = "0" * 16  # the empty state root's one project key


class _PriorEngine(replay.FrozenEngine):
    """The production learner as it decides on day one: a frozen ``Engine`` over an empty,
    temporary state root (packaged priors only; nothing read from or written to any real
    home)."""

    def __init__(self, cfg: Config, seed: int) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="token-sparen-hc-"))
        self._cleanup = weakref.finalize(self, shutil.rmtree, str(self.tmp), True)
        super().__init__(cfg, self.tmp / ".token-sparen", seed)
        self.env = {"HOME": str(self.tmp), "TOKEN_SPAREN_HOME": str(self.home)}

    def engine(self, project_key: str) -> Engine:
        """One engine for every session (no project has evidence; the key is a placeholder)."""
        eng = self._engines.get(PRIOR_KEY)
        if eng is None:
            stores = StoreChain(PRIOR_KEY, self.home, None, params=self.cfg.data)
            rng = random.Random(f"{self.seed}|{PRIOR_KEY}")
            eng = self._engines[PRIOR_KEY] = Engine(stores, self.cfg, rng, env=self.env)
        return eng


class GroupedCompression:
    """The ``compression`` lane (production quality gate) reported per project group
    (``runner.LANES["hc_compression"]``). Params: ``arm`` (a static arm: the arm a
    ``compression.levels.<arm>`` coordinate governs) or else the production policy (the
    learner frozen on its packaged priors, ``_PriorEngine``), ``overlay``, ``seed``."""

    history = False

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        seed = params.get("seed")
        seed = seed if isinstance(seed, int) and not isinstance(seed, bool) else 0
        self.inner = replay.CompressionLane(cfg, {"policy": "default", "seed": seed})
        arm = params.get("arm")
        policy: Any
        if isinstance(arm, str) and arm in COMPRESSION.arms:
            policy = StaticPolicy({COMPRESSION.name: arm})
        else:
            policy = _PriorEngine(cfg, seed)
        # named "default": gated like production; one project key (no git probe of any cwd)
        self.inner.policies = [("default", policy)]
        self.inner.needs_key = False
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
                float(row.get("breaker_trips", 0)),
            )
    return cand, ref


def mean_rows(runs: Sequence[Rows]) -> Rows:
    """Per project group, the mean row over ``runs`` (one per seed), for the groups every run
    reports: net savings, trims, proxy regret and breaker trips averaged."""
    if not runs:
        return {}
    n = float(len(runs))
    common = set(runs[0]).intersection(*runs[1:])
    out: Rows = {}
    for group in sorted(common):
        rows = [r[group] for r in runs]
        points = sorted({p for row in rows for p in (*row.trims, *row.regret)})
        out[group] = Row(
            sum(row.net for row in rows) / n,
            {p: sum(row.trims.get(p, 0.0) for row in rows) / n for p in points},
            {p: sum(row.regret.get(p, 0.0) for row in rows) / n for p in points},
            sum(row.trips for row in rows) / n,
        )
    return out


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
    of that pass, on ``split`` only (``cutoff``/``dogfood_start``: the campaign's split dates).
    The ``sim`` lane runs once per seed of ``sim_seeds`` (default ``seed + SIM_SEEDS``) in that
    same pass and its rows are averaged per group (``mean_rows``)."""

    def __init__(
        self,
        paths: Sequence[Path],
        *,
        jobs: int,
        seed: int,
        split: str,
        cutoff: date = DEV_CUTOFF,
        dogfood_start: date = DOGFOOD_START,
        sim_seeds: Sequence[int] | None = None,
    ) -> None:
        self.paths, self.jobs, self.seed, self.split = list(paths), jobs, seed, split
        self.cutoff, self.dogfood_start = cutoff, dogfood_start
        self.sim_seeds = tuple(sim_seeds) if sim_seeds else tuple(seed + s for s in SIM_SEEDS)
        self.log = RunLog()

    def sim_specs(self, label: str, overlay: Mapping[str, Any]) -> list[runner.LaneSpec]:
        """One ``sim`` lane per seed (labels ``<label>@<seed>``)."""
        return [
            ("sim", {"overlay": nest(overlay), "seed": s, "label": f"{label}@{s}"})
            for s in self.sim_seeds
        ]

    def sim_result(self, got: Mapping[str, Any], label: str) -> tuple[Rows, Rows]:
        """(candidate, reference) rows of ``label``, averaged over the seeds."""
        runs = [sim_rows(got.get(f"{label}@{s}", {})) for s in self.sim_seeds]
        return mean_rows([c for c, _ in runs]), mean_rows([r for _, r in runs])

    def run(self, specs: list[runner.LaneSpec], groups: Sequence[str] | None) -> dict[str, Any]:
        res = runner.run_parallel(
            self.paths,
            specs,
            jobs=self.jobs,
            cutoff=self.cutoff,
            dogfood_start=self.dogfood_start,
            groups=set(groups) if groups is not None else None,
            split=self.split,
            cfg=Config(data=packaged_defaults()),  # never the user's, project's or env config
        )
        self.log.add(res)
        out: dict[str, Any] = res.by_split.get(self.split, {})
        return out

    def __call__(
        self,
        lane: str,
        overlays: Mapping[str, Mapping[str, Any]],
        groups: Sequence[str],
        key: str,
    ) -> dict[str, tuple[Rows, Rows]]:
        if lane == "sim":
            specs: list[runner.LaneSpec] = [
                spec for label, o in overlays.items() for spec in self.sim_specs(f"hc_{label}", o)
            ]
            got = self.run(specs, groups)
            return {label: self.sim_result(got, f"hc_{label}") for label in overlays}
        key_arm = _arm_of(key)  # the arm this coordinate governs, else the production policy
        labels = {**{f"hc_{k}": dict(v) for k, v in overlays.items()}, "hc__defaults": {}}
        specs = [
            (
                "hc_compression",
                {
                    "module": MODULE,
                    "overlay": nest(o),
                    "seed": self.seed,
                    "label": label,
                    **({"arm": key_arm} if key_arm else {"policy": "engine"}),
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
        if coord is None or coord.lane == "none" or not any(_same(value, v) for v in coord.values):
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


def dev_groups(evaluator: Evaluator) -> list[str]:
    """The project groups with a dev session (one cheap pass on the safe arm): the samples of
    the successive halving are drawn from these only."""
    spec = {"module": MODULE, "label": "hc_scan", "arm": COMPRESSION.safe_arm}
    got = evaluator.run([("hc_compression", spec)], None)
    return sorted(got.get("hc_scan", {}).get("groups", {}))


def campaign_id(text: str) -> str:
    """A campaign id: 1-32 of ``[a-z0-9_-]``, starting with a letter or digit (a file name)."""
    if not isinstance(text, str) or CAMPAIGN_ID.fullmatch(text) is None:
        raise ValueError("campaign id: 1-32 of a-z 0-9 _ -")
    return text


def ledger_path(campaign: str = CAMPAIGN) -> Path:
    """The campaign's one-shot confirmation ledger: under the state root, whatever ``--out``
    is (``hillclimb/confirm-<campaign>.json``)."""
    return state_home(os.environ) / LEDGER_DIR / f"confirm-{campaign_id(campaign)}.json"


def claim_ledger(sha: str, campaign: str = CAMPAIGN) -> dict[str, str]:
    """Create the campaign's ledger exclusively (``O_CREAT | O_EXCL``, 0600) or raise
    ``ConfirmRefused``: of any number of confirmations of one campaign, concurrent or not,
    exactly one gets past this."""
    path = ledger_path(campaign)
    report._mkdirs_private(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfirmRefused("the confirmation of this campaign already ran") from None
    entry = {
        "campaign": campaign,
        "proposal_sha256": sha,
        "recorded": datetime.now(UTC).isoformat(),
    }
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def _dev_report(proposal_path: Path, sha: str) -> str | None:
    """The id of the dev report written with this proposal (``dev.json`` beside it), if any."""
    dev_json = Path(proposal_path).parent / "dev.json"
    try:
        doc = json.loads(dev_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(doc, dict) and doc.get("proposal_sha256") == sha:
        return report.report_id(dev_json)
    return None


def dev(
    coords: Sequence[Coordinate],
    paths: Sequence[Path],
    out: Path,
    *,
    jobs: int,
    seed: int,
    space_sha: str,
    campaign: str = CAMPAIGN,
    cutoff: date = DEV_CUTOFF,
    dogfood_start: date = DOGFOOD_START,
) -> tuple[Path, bool, list[str]]:
    evaluator = Evaluator(
        paths, jobs=jobs, seed=seed, split="dev", cutoff=cutoff, dogfood_start=dogfood_start
    )
    trail: list[dict[str, Any]] = []

    def save_trail() -> None:
        text = "".join(json.dumps(row, sort_keys=True, default=repr) + "\n" for row in trail)
        report.write_private(out / "trail.jsonl", text, raw=True)

    proposal = climb(
        coords, evaluator, dev_groups(evaluator), seed=seed, trail=trail, on_step=save_trail
    )
    save_trail()
    text = _toml(proposal)
    report.write_private(out / "proposal.toml", text, raw=True)
    keys = {
        c.key: "unmeasured"
        if c.lane == "none"
        else "accepted"
        if c.key in proposal
        else "unchanged"
        for c in coords
    }
    doc = {
        "version": 1,
        "kind": "hillclimb-dev",
        "split": "dev",
        "campaign": campaign,
        "cutoff": cutoff.isoformat(),
        "dogfood_start": dogfood_start.isoformat(),
        "generated": datetime.now(UTC).date().isoformat(),
        "space_sha256": space_sha,
        "seed": seed,
        "sim_seeds": list(evaluator.sim_seeds),
        "proposal": proposal,
        "proposal_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "keys": keys,
        "complete": evaluator.log.complete,
        "run": asdict(evaluator.log),
    }
    path = report.write_private(out / "dev.json", doc)
    return path, evaluator.log.complete, [f"{k}: {v}" for k, v in keys.items()]


def level_arms(proposal: Mapping[str, Any]) -> list[str]:
    """The arms whose ``compression.levels.<arm>`` keys the proposal moves."""
    return sorted({arm for key in proposal if (arm := _arm_of(key)) is not None})


def confirm(
    coords: Sequence[Coordinate],
    proposal_path: Path,
    paths: Sequence[Path],
    out: Path,
    *,
    jobs: int,
    seed: int,
    campaign: str = CAMPAIGN,
    cutoff: date = DEV_CUTOFF,
    dogfood_start: date = DOGFOOD_START,
) -> tuple[Path, bool, list[str]]:
    proposal = load_proposal(proposal_path, coords)  # before the one shot is used
    sha = hashlib.sha256(Path(proposal_path).read_bytes()).hexdigest()
    report.check_out_path(out / "confirm.json")
    ledger = claim_ledger(sha, campaign)  # before any test outcome is computed
    e2_ok, e2_reasons = e2_gate(proposal)
    evaluator = Evaluator(
        paths, jobs=jobs, seed=seed, split="test", cutoff=cutoff, dogfood_start=dogfood_start
    )
    arms = level_arms(proposal)
    specs = evaluator.sim_specs("sim", proposal)
    for arm in arms:  # each proposed level also on its own arm, paired, in the same pass (M4-G1)
        for role, overlay in (("cand", proposal), ("ref", {})):
            params = {"module": MODULE, "overlay": nest(overlay), "seed": seed, "arm": arm}
            specs.append(("hc_compression", {**params, "label": f"cmp_{arm}_{role}"}))
    got = evaluator.run(specs, None)  # the one read of the test split
    cand, ref = evaluator.sim_result(got, "sim")
    verdict = judge(cand, ref, ref, seed=f"{seed}|confirm|{sha}")
    reasons = list(verdict.reasons)
    insufficient = verdict.insufficient
    arm_docs: dict[str, Any] = {}
    for arm in arms:
        a_cand = compression_rows(got.get(f"cmp_{arm}_cand", {}))
        a_ref = compression_rows(got.get(f"cmp_{arm}_ref", {}))
        v = judge(a_cand, a_ref, a_ref, seed=f"{seed}|confirm|{sha}|{arm}", net=False)
        arm_docs[arm] = {
            "baseline": _summary(a_ref),
            "proposal": _summary(a_cand),
            "reasons": v.reasons,
            "stats": v.stats,
            "insufficient": v.insufficient,
        }
        reasons += [f"arm:{arm}:{r}" for r in v.reasons]
        insufficient = insufficient or v.insufficient
    complete = evaluator.log.complete
    accepted = bool(proposal) and not reasons and e2_ok and complete
    reasons += [*e2_reasons, *([] if complete else ["incomplete"])]
    doc = {
        "version": 1,
        "kind": "hillclimb-confirm",
        "split": "test",
        "campaign": campaign,
        "cutoff": cutoff.isoformat(),
        "dogfood_start": dogfood_start.isoformat(),
        "generated": datetime.now(UTC).date().isoformat(),
        "proposal_sha256": sha,
        "ledger": ledger,
        "dev_report": _dev_report(proposal_path, sha),  # None: not the dev run's proposal
        "proposal_overlay": proposal,
        "seed": seed,
        "sim_seeds": list(evaluator.sim_seeds),
        "baseline": _summary(ref),
        "proposal": _summary(cand),
        "arms": arm_docs,  # per proposed level: its own arm, compression lane (quality only)
        "verdict": {"reasons": reasons, "stats": verdict.stats},
        "accepted": accepted,
        "insufficient": insufficient,
        "complete": complete,
        "run": asdict(evaluator.log),
    }
    path = report.write_private(out / "confirm.json", doc)
    word = "accepted" if accepted else "rejected"
    lines = [
        f"{c.key}: "
        + ("unmeasured" if c.lane == "none" else word if c.key in proposal else "unchanged")
        for c in coords
    ]
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
    ap.add_argument(
        "--campaign",
        type=campaign_id,
        default=CAMPAIGN,
        help=f"campaign id: one test confirmation (ledger) per campaign (default {CAMPAIGN})",
    )
    ap.add_argument(
        "--cutoff",
        type=date.fromisoformat,
        default=DEV_CUTOFF,
        help=f"dev < cutoff <= test, YYYY-MM-DD (default {DEV_CUTOFF.isoformat()})",
    )
    ap.add_argument(
        "--dogfood-start",
        type=date.fromisoformat,
        default=DOGFOOD_START,
        help=f"sessions from this day on are excluded (default {DOGFOOD_START.isoformat()})",
    )
    ap.add_argument("--quiet", action="store_true", help="print only the fixed template")
    args = ap.parse_args(argv)
    if not args.cutoff < args.dogfood_start:
        ap.error("--cutoff must be before --dogfood-start")
    split = {"campaign": args.campaign, "cutoff": args.cutoff, "dogfood_start": args.dogfood_start}
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
                coords, args.confirm, paths, out, jobs=jobs, seed=args.seed, **split
            )
        else:
            space_sha = hashlib.sha256(Path(args.space).read_bytes()).hexdigest()
            path, complete, lines = dev(
                coords, paths, out, jobs=jobs, seed=args.seed, space_sha=space_sha, **split
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
