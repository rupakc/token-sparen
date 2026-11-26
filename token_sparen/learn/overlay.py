"""Session fold: session evidence into the project and global stores (spec sections 7.4, 7.6).

``fold(session_id, project_key, home)`` runs after a session (SessionEnd spawns a detached
``token-sparen fold``; SessionStart folds left-over sessions older than an hour). It

1. **stages** the session overlay: its ``evidence`` and ``regret_outcomes`` move under
   ``folding`` with a fresh fold id, leaving ``evidence`` empty. The engine sums project +
   session evidence, so evidence must never be in both places: from here on it is in neither
   (briefly under-counted) until the project write, never double counted;
2. **circuit breaker** (``learn.breaker``): the outcomes of trimming arms enter each point's
   ring; points over the limit trip, paused points count this session down, re-entering points
   get their trimming-arm project evidence deleted (designed priors apply again);
3. **project merge**: per cell ``project' = cap(project + session)`` (``ea``, ``eb``, ``es2``,
   ``n`` summed, then the mean-preserving whole-vector cap of ``Evidence.observe``; session
   evidence was discounted in-session and is not discounted again). Each full bucket's evidence
   is also added to every coarser back-off level of the point, so **coarse buckets are
   aggregates** of their children (back-off parents and the ladder need data there);
4. **global** ``global/posteriors.json``: the session's evidence (the delta, never the project
   total) times ``[learn] global_weight`` (0.3, all four fields scaled) at every level; arm
   entries carry ``t`` (last update); entries untouched for ``global_age_days`` (90) are halved
   (all four fields) once per such period. Global holds no project keys, paths or session ids;
5. **pre-trim candidates** ``projects/<key>/pretrim.json``: decisions appended to
   ``decisions.jsonl`` since the last scan (byte offset, reset on rotation) update per-command
   stats; a normalised command (``regret.event_signature``) with >= 5 trimming compression
   decisions, mean ``tokens_after / tokens_before`` <= 0.4 and no regret ever becomes
   ``{"lines": 120}`` (at most 200, least recently seen dropped); a regret removes it;
6. clears ``folding`` and sets ``folded_at``.

Idempotent and crash-safe: every target document records the fold ids it applied (last 200),
so a re-run after a crash between steps applies nothing twice; re-folding a folded session
finds nothing staged. **Lock order** (``LOCK_ORDER``), the same everywhere: the project's
``posteriors.json`` lock is held for the whole fold (it serialises folds of a project); under
it ``breaker.json``, then ``global/posteriors.json``, then ``pretrim.json``, then the session
overlay, then ``pending.json`` (the pending-fold index, ``learn.pending``) are taken one at a
time, each innermost. Hooks only ever take the overlay or the index lock alone, so no cycle is
possible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

from token_sparen.config import log_error, state_home
from token_sparen.learn import breaker, pending
from token_sparen.learn.beta import Beta, Evidence
from token_sparen.learn.features import ANY_BUCKET, backoff_keys
from token_sparen.learn.hierarchy import (
    POSTERIORS_FILE,
    SCHEMA_VERSION,
    SESSION_KEY,
    Kind,
    StoreChain,
    prior_for,
)
from token_sparen.learn.policy import DecisionPoint, get_point
from token_sparen.learn.regret import OUTCOMES_KEY
from token_sparen.store import jsonstore, session
from token_sparen.store.paths import project_dir_for

LOCK_ORDER = (
    "posteriors.json",
    "breaker.json",
    "global/posteriors.json",
    "pretrim.json",
    "overlay",
    "pending.json",
)
UNLEARNED_BUCKETS = frozenset({"static", "error"})  # StaticPolicy / engine-error decisions
FOLDING_KEY = "folding"
FOLDED_AT = "folded_at"
APPLIED_KEY = "applied"
APPLIED_MAX = 200
PRETRIM_FILE = "pretrim.json"  # read by mechanisms.guards
DECISIONS_FILE = "decisions.jsonl"
GLOBAL_WEIGHT = 0.3
GLOBAL_AGE_DAYS = 90.0
PRETRIM_MIN = 5
PRETRIM_MAX_RATIO = 0.4
PRETRIM_LINES = 120
PRETRIM_MAX = 200
PRETRIM_STATS_MAX = 2000
PENDING_AGE_S = 3600.0  # SessionStart folds left-over sessions idle at least this long
PENDING_PER_START = 3
PENDING_SCAN_MAX = 50  # overlay files read per SessionStart at most
INPROCESS_MAX_BYTES = 256 * 1024  # bigger -> SessionStart spawns the fold instead
INPROCESS_BUDGET_S = 0.1
INPROCESS_LOCK_S = 0.05
COLD_OBS = 20
EXPLOIT_WINDOW = 50
EXPLOIT_SHARE = 0.8
TAIL_BYTES = 256 * 1024
_KINDS: tuple[Kind, ...] = ("savings", "regret")
_EMPTY = Evidence(0.0, 0.0, 0.0, 0.0)
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
POPEN: Any = subprocess.Popen  # replaced in tests

LearnerState = Literal["cold", "adapting", "exploiting", "safe-mode"]


@dataclass
class FoldReport:
    """What one ``fold`` did."""

    session_id: str
    staged: bool = False
    cells: int = 0  # session evidence cells merged into the project
    global_cells: int = 0  # (point, bucket, arm, kind) cells updated in the global store
    outcomes: int = 0  # settled regret outcomes consumed
    candidates: int = 0  # pre-trim candidates after the fold
    aged: int = 0  # global entries halved for age
    tripped: list[str] = field(default_factory=list)
    reentered: list[str] = field(default_factory=list)

    def line(self) -> str:
        if not self.staged:
            return f"fold {self.session_id}: nothing to fold"
        text = (
            f"fold {self.session_id}: merged {self.cells} cells (global {self.global_cells}), "
            f"{self.outcomes} regret outcomes, {self.candidates} pretrim candidates"
        )
        if self.aged:
            text += f", {self.aged} global entries aged"
        if self.tripped:
            text += f"; breaker tripped: {', '.join(self.tripped)}"
        if self.reentered:
            text += f"; re-entered: {', '.join(self.reentered)}"
        return text


# --------------------------------------------------------------------------- helpers
_num = breaker.num
_param = breaker.param


def _applied(doc: Mapping[str, Any]) -> list[str]:
    raw = doc.get(APPLIED_KEY)
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def _mark(doc: dict[str, Any], fold_id: str) -> None:
    doc[APPLIED_KEY] = [*_applied(doc), fold_id][-APPLIED_MAX:]


def _evidence_doc(doc: dict[str, Any], env: Mapping[str, str], where: str) -> dict[str, Any]:
    """A v2 evidence document (``{"version": 2, "points": {...}}``) in place; anything else is
    logged and started afresh (v1 held posteriors and reads as empty anyway)."""
    if doc and doc.get("version") != SCHEMA_VERSION:
        log_error(env, "fold", f"{where}: unsupported version {doc.get('version')!r}, rebuilt")
        doc.clear()
    doc["version"] = SCHEMA_VERSION
    if not isinstance(doc.get("points"), dict):
        doc["points"] = {}
    return doc


def _node(points: dict[str, Any], point: str, bucket: str, arm: str) -> dict[str, Any]:
    node: dict[str, Any] = points
    for key in (point, bucket, arm):
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    return node


def _get(node: Mapping[str, Any], kind: str) -> Evidence:
    raw = node.get(kind)
    if raw is None:
        return _EMPTY
    try:
        return Evidence.from_list(raw)
    except ValueError:
        return _EMPTY


def _cells(doc: object) -> Iterator[tuple[str, str, str, Kind, Evidence]]:
    """Well-formed (point, bucket, arm, kind, evidence) cells of a session evidence mapping."""
    if not isinstance(doc, dict):
        return
    for point, buckets in doc.items():
        if not isinstance(buckets, dict) or get_point(point) is None:
            continue
        for bucket, arms in buckets.items():
            if not isinstance(arms, dict):
                continue
            for arm, kinds in arms.items():
                if not isinstance(kinds, dict):
                    continue
                for kind in _KINDS:
                    raw = kinds.get(kind)
                    if raw is None:
                        continue
                    try:
                        ev = Evidence.from_list(raw)
                    except ValueError:
                        continue
                    yield point, bucket, arm, kind, ev


def features_of(bucket: str) -> dict[str, str] | None:
    """Features of a ``k=v|k=v`` bucket key (``{}`` for ``*``); None if it is not one."""
    if bucket == ANY_BUCKET:
        return {}
    out: dict[str, str] = {}
    for part in bucket.split("|"):
        k, sep, v = part.partition("=")
        if not sep or not k:
            return None
        out[k] = v
    return out


def targets(point: str, bucket: str, params: Mapping[str, Any] | None) -> list[str]:
    """The bucket itself, then every coarser back-off level of the same point."""
    feats = features_of(bucket)
    out = [bucket]
    if feats is None:
        return out
    try:
        keys = backoff_keys(point, feats, params)
    except ValueError:
        return out
    out.extend(k for k in keys if k not in out)
    return out


def _add(node: dict[str, Any], kind: str, ev: Evidence, cap: float) -> None:
    node[kind] = (_get(node, kind) + ev).capped(cap).to_list()


# --------------------------------------------------------------------------- fold
def fold(
    session_id: str,
    project_key: str,
    home: Path,
    *,
    params: Mapping[str, Any] | None = None,
    now: float | None = None,
    lock_timeout: float | None = None,
) -> FoldReport:
    """Fold one session (see the module docstring). Raises only ``jsonstore.StoreBusy`` (a lock
    could not be taken; nothing is lost, retry later); every other failure is logged."""
    env = {"TOKEN_SPAREN_HOME": str(home)}
    rep = FoldReport(session_id)
    try:
        _fold(rep, project_key, home, params, time.time() if now is None else now, lock_timeout)
    except jsonstore.StoreBusy:
        raise
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "fold", f"{type(exc).__name__}: {exc}")
    return rep


def _stage(
    path: Path, now: float, env: Mapping[str, str], timeout: float | None
) -> dict[str, Any] | None:
    out: list[dict[str, Any]] = []

    def fn(state: dict[str, Any]) -> dict[str, Any]:
        out.clear()
        staged = state.get(FOLDING_KEY)
        if isinstance(staged, dict) and isinstance(staged.get("id"), str):
            out.append(staged)  # an interrupted fold: finish it
            return state
        evidence = state.get(SESSION_KEY)
        outcomes = state.get(OUTCOMES_KEY)
        if FOLDED_AT in state and not evidence and not outcomes:
            return state  # already folded, nothing new
        staged = {
            "id": uuid.uuid4().hex,
            "evidence": evidence if isinstance(evidence, dict) else {},
            "outcomes": outcomes if isinstance(outcomes, list) else [],
            "t": now,
        }
        state[FOLDING_KEY] = staged
        state[SESSION_KEY] = {}
        state[OUTCOMES_KEY] = []
        out.append(staged)
        return state

    jsonstore.update(path, fn, {}, env, timeout=timeout)
    return out[0] if out else None


class Outcome(NamedTuple):
    """One settled regret outcome of the session."""

    point: str
    arm: str
    sig: str
    value: float
    learned: bool  # decided by the learner (bucket recorded and not static/error)


def _outcomes(raw: object) -> list[Outcome]:
    """Well-formed ``[id, point, arm, command_sig, value(, bucket)]`` rows (a row without a
    bucket predates its recording and is not known to be learned)."""
    out: list[Outcome] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, list) or len(row) not in (5, 6):
            continue
        point, arm, sig, value = row[1:5]
        bucket = row[5] if len(row) == 6 else None
        if isinstance(point, str) and isinstance(arm, str) and isinstance(sig, str):
            x = _num(value)
            if x is not None:
                learned = isinstance(bucket, str) and bucket not in UNLEARNED_BUCKETS
                out.append(Outcome(point, arm, sig, min(1.0, max(0.0, x)), learned))
    return out


def _trimming(point: str, arm: str) -> DecisionPoint | None:
    known = get_point(point)
    if known is None or arm == known.safe_arm or arm not in known.arms:
        return None
    return known


def _fold(
    rep: FoldReport,
    key: str,
    home: Path,
    params: Mapping[str, Any] | None,
    now: float,
    timeout: float | None,
) -> None:
    env = {"TOKEN_SPAREN_HOME": str(home)}
    ov = session.session_path(home, key, rep.session_id)
    name = ov.name.removesuffix(".json")
    if not ov.exists():
        pending.remove(home, key, name, env, timeout=timeout)
        return
    pdir = project_dir_for(home, key)
    post = pdir / POSTERIORS_FILE
    with jsonstore.locked(post, timeout):
        staged = _stage(ov, now, env, timeout)
        if staged is None:
            pending.remove(home, key, name, env, timeout=timeout)
            return
        pending.add(home, key, name, env, timeout=timeout)  # a crash from here on is resumed
        rep.staged = True
        fid = str(staged["id"])
        cells = list(_cells(staged.get("evidence")))
        outcomes = _outcomes(staged.get("outcomes"))
        rep.outcomes = len(outcomes)
        cap = _param(params, "posterior_cap", 50.0)
        cap = cap if cap > 0.0 else 50.0
        _breaker_and_project(rep, fid, cells, outcomes, pdir, params, now, cap, env, timeout)
        _global(rep, fid, cells, home, params, now, cap, env, timeout)
        _pretrim(rep, fid, outcomes, pdir, params, now, env, timeout)

        def clear(state: dict[str, Any]) -> dict[str, Any]:
            cur = state.get(FOLDING_KEY)
            if isinstance(cur, dict) and cur.get("id") == fid:
                del state[FOLDING_KEY]
            state[FOLDED_AT] = now
            return state

        jsonstore.update(ov, clear, {}, env, timeout=timeout)
        pending.remove(home, key, name, env, timeout=timeout)


def _breaker_and_project(
    rep: FoldReport,
    fid: str,
    cells: list[tuple[str, str, str, Kind, Evidence]],
    outcomes: list[Outcome],
    pdir: Path,
    params: Mapping[str, Any] | None,
    now: float,
    cap: float,
    env: Mapping[str, str],
    timeout: float | None,
) -> None:
    """Breaker first (it decides the resets), then the project store (held lock)."""
    bpath = pdir / breaker.BREAKER_FILE
    post = pdir / POSTERIORS_FILE
    with jsonstore.locked(bpath, timeout):
        bdoc = jsonstore.read_locked(bpath, {}, env)
        fresh = fid not in _applied(bdoc)
        if fresh:
            obs: list[tuple[DecisionPoint, float]] = []
            for o in outcomes:
                known = _trimming(o.point, o.arm) if o.learned else None
                if known is not None:  # the breaker judges the learner, not the static policy
                    obs.append((known, o.value))
            rep.tripped, rep.reentered = breaker.apply_session(bdoc, obs, now=now, params=params)
        resets = breaker.pending_resets(bdoc)
        pdoc = _evidence_doc(jsonstore.read_locked(post, {}, env), env, "project")
        points: dict[str, Any] = pdoc["points"]
        scale = min(1.0, max(0.0, _param(params, "breaker_reentry_scale", breaker.REENTRY_SCALE)))
        for name in resets:
            _reset(points, name, scale)
        merge = fid not in _applied(pdoc)
        if merge:
            for point, bucket, arm, kind, ev in cells:
                for target in targets(point, bucket, params):
                    _add(_node(points, point, target, arm), kind, ev, cap)
                rep.cells += 1
            _mark(pdoc, fid)
        if merge or resets:
            jsonstore.write_locked(post, pdoc)
        breaker.take_resets(bdoc)
        if fresh:
            _mark(bdoc, fid)
        if fresh or resets:
            jsonstore.write_locked(bpath, bdoc)


def _reset(points: dict[str, Any], name: str, scale: float) -> None:
    """Re-entry after a trip: scale the project evidence of ``name``'s trimming arms by
    ``scale`` (all four fields), in every bucket."""
    known = get_point(name)
    buckets = points.get(name)
    if not isinstance(buckets, dict):
        return
    for arms in buckets.values():
        if not isinstance(arms, dict):
            continue
        for arm, node in arms.items():
            if not isinstance(node, dict) or (known is not None and arm == known.safe_arm):
                continue
            for kind in _KINDS:
                if node.get(kind) is not None:
                    node[kind] = _get(node, kind).scaled(scale).to_list()


def _global(
    rep: FoldReport,
    fid: str,
    cells: list[tuple[str, str, str, Kind, Evidence]],
    home: Path,
    params: Mapping[str, Any] | None,
    now: float,
    cap: float,
    env: Mapping[str, str],
    timeout: float | None,
) -> None:
    gpath = home / "global" / POSTERIORS_FILE
    weight = min(1.0, max(0.0, _param(params, "global_weight", GLOBAL_WEIGHT)))
    max_age = _param(params, "global_age_days", GLOBAL_AGE_DAYS) * 86400.0
    with jsonstore.locked(gpath, timeout):
        gdoc = _evidence_doc(jsonstore.read_locked(gpath, {}, env), env, "global")
        points: dict[str, Any] = gdoc["points"]
        rep.aged = _age(points, now, max_age)
        if fid not in _applied(gdoc):
            for point, bucket, arm, kind, ev in cells:
                if weight <= 0.0:
                    break
                delta = ev.scaled(weight)
                for target in targets(point, bucket, params):
                    node = _node(points, point, target, arm)
                    _add(node, kind, delta, cap)
                    node["t"] = now
                    rep.global_cells += 1
            _mark(gdoc, fid)
        jsonstore.write_locked(gpath, gdoc)


def _age(points: dict[str, Any], now: float, max_age: float) -> int:
    """Halve global arm entries untouched for ``max_age`` seconds (once per period)."""
    aged = 0
    for buckets in points.values():
        if not isinstance(buckets, dict):
            continue
        for arms in buckets.values():
            if not isinstance(arms, dict):
                continue
            for node in arms.values():
                if not isinstance(node, dict):
                    continue
                t = _num(node.get("t"))
                if t is None:
                    node["t"] = now  # start the clock
                elif now - t > max_age:
                    for kind in _KINDS:
                        if node.get(kind) is not None:
                            node[kind] = _get(node, kind).scaled(0.5).to_list()
                    node["t"] = now
                    aged += 1
    return aged


# --------------------------------------------------------------------------- pre-trim
def _head_id(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            first = fh.readline(4096)
    except OSError:
        return ""
    return hashlib.blake2b(first, digest_size=8).hexdigest()


def _scan(path: Path, scan: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Decision records appended since the last scan; (records, new scan state)."""
    head = _head_id(path)
    offset = scan.get("offset")
    start = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    try:
        size = path.stat().st_size
    except OSError:
        return [], {"offset": 0, "head": ""}
    if scan.get("head") != head or start > size or start < 0:
        start = 0  # rotated or rewritten: scan the new file from the top
    recs: list[dict[str, Any]] = []
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read()
    end = data.rfind(b"\n") + 1  # a line still being written is left for next time
    for raw in data[:end].splitlines():
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs, {"offset": start + end, "head": head}


def _ratio(rec: Mapping[str, Any]) -> tuple[str, float, float] | None:
    """(command_sig, tokens_after / tokens_before, ts) of a trimming Bash compression."""
    if rec.get("point") != "compression" or rec.get("tool_name") not in (None, "Bash"):
        return None
    arm, sig = rec.get("arm"), rec.get("command_sig")
    before, after = rec.get("tokens_before"), rec.get("tokens_after")
    if not isinstance(arm, str) or _trimming("compression", arm) is None:
        return None
    b, a = _num(before), _num(after)
    if not isinstance(sig, str) or not sig or b is None or a is None or b <= 0.0 or a < 0.0:
        return None
    return sig, a / b, _num(rec.get("ts")) or 0.0


def _pretrim(
    rep: FoldReport,
    fid: str,
    outcomes: list[Outcome],
    pdir: Path,
    params: Mapping[str, Any] | None,
    now: float,
    env: Mapping[str, str],
    timeout: float | None,
) -> None:
    path = pdir / PRETRIM_FILE
    need = _param(params, "pretrim_min_decisions", PRETRIM_MIN)
    max_ratio = _param(params, "pretrim_max_ratio", PRETRIM_MAX_RATIO)
    lines = max(1, int(_param(params, "pretrim_lines", PRETRIM_LINES)))
    keep = max(1, int(_param(params, "pretrim_max", PRETRIM_MAX)))
    with jsonstore.locked(path, timeout):
        doc = jsonstore.read_locked(path, {}, env)
        cands = doc.get("candidates")
        cands = cands if isinstance(cands, dict) else {}
        stats = doc.get("stats")
        stats = stats if isinstance(stats, dict) else {}
        scan = doc.get("scan")
        recs, doc["scan"] = _scan(pdir / DECISIONS_FILE, scan if isinstance(scan, dict) else {})
        touched: set[str] = set()

        def entry(sig: str) -> list[float]:
            cur = stats.get(sig)
            vals = [_num(v) for v in cur] if isinstance(cur, list) and len(cur) == 4 else []
            row = [v or 0.0 for v in vals] if vals and None not in vals else [0.0] * 4
            stats[sig] = row
            touched.add(sig)
            return row

        for rec in recs:
            got = _ratio(rec)
            if got is not None:
                sig, ratio, ts = got
                row = entry(sig)
                row[0] += 1.0
                row[1] += ratio
                row[3] = max(row[3], ts)
        if fid not in _applied(doc):
            for o in outcomes:
                if o.sig and o.value > 0.0:
                    row = entry(o.sig)
                    row[2] += 1.0
                    row[3] = max(row[3], now)
            _mark(doc, fid)
        for sig in touched:
            count, total, regrets, ts = stats[sig]
            if count >= need and regrets == 0.0 and total / count <= max_ratio:
                cur = cands.get(sig)
                cand = dict(cur) if isinstance(cur, dict) else {"lines": lines}
                cand["t"] = ts
                cands[sig] = cand
            else:
                cands.pop(sig, None)
        doc["candidates"] = _lru(cands, keep, lambda v: v.get("t") if isinstance(v, dict) else 0)
        doc["stats"] = _lru(stats, PRETRIM_STATS_MAX, lambda v: v[3] if isinstance(v, list) else 0)
        rep.candidates = len(doc["candidates"])
        jsonstore.write_locked(path, doc)


def _lru(table: dict[str, Any], keep: int, ts: Any) -> dict[str, Any]:
    if len(table) <= keep:
        return table

    def when(item: tuple[str, Any]) -> float:
        return _num(ts(item[1])) or 0.0

    return dict(sorted(table.items(), key=when)[-keep:])


# --------------------------------------------------------------------------- pending folds
def _unfolded(doc: Mapping[str, Any]) -> bool:
    return (
        isinstance(doc.get(FOLDING_KEY), dict)
        or FOLDED_AT not in doc
        or bool(doc.get(SESSION_KEY))
        or bool(doc.get(OUTCOMES_KEY))
    )


def pending_sessions(
    project_key: str,
    home: Path,
    *,
    older_than: float = PENDING_AGE_S,
    limit: int = PENDING_PER_START,
    exclude: str | None = None,
    now: float | None = None,
    max_scan: int | None = PENDING_SCAN_MAX,
) -> list[str]:
    """Overlay ids of this project not (fully) folded and idle for ``older_than`` seconds,
    oldest first; at most ``limit``, reading at most ``max_scan`` overlay files."""
    sdir = project_dir_for(home, project_key) / "sessions"
    now = time.time() if now is None else now
    skip = None if exclude is None else session.session_path(home, project_key, exclude).name
    found: list[tuple[float, Path]] = []
    try:
        with os.scandir(sdir) as it:
            for de in it:
                if not de.name.endswith(".json") or de.name == skip:
                    continue
                with contextlib.suppress(OSError):
                    mtime = de.stat().st_mtime
                    if now - mtime >= older_than:
                        found.append((mtime, Path(de.path)))
    except OSError:
        return []
    found.sort()
    env = {"TOKEN_SPAREN_HOME": str(home)}
    out: list[str] = []
    for i, (_, path) in enumerate(found):
        if len(out) >= limit or (max_scan is not None and i >= max_scan):
            break
        if _unfolded(jsonstore.read(path, {"folded_at": 0}, env)):
            out.append(path.name.removesuffix(".json"))
    return out


def indexed_sessions(
    project_key: str,
    home: Path,
    *,
    older_than: float = PENDING_AGE_S,
    limit: int = PENDING_PER_START,
    exclude: str | None = None,
    now: float | None = None,
) -> list[str]:
    """Sessions in the pending-fold index (``learn.pending``) whose overlay has been idle for
    ``older_than`` seconds (or is gone), oldest first, at most ``limit``. No directory scan."""
    env = {"TOKEN_SPAREN_HOME": str(home)}
    now = time.time() if now is None else now
    skip = None if exclude is None else session.session_path(home, project_key, exclude).name
    found: list[tuple[float, str]] = []
    for name in pending.entries(home, project_key, env):
        path = session.session_path(home, project_key, name)
        if path.name == skip:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0  # gone: its fold drops the entry
        if now - mtime >= older_than:
            found.append((mtime, name))
    found.sort()
    return [name for _, name in found[: max(0, limit)]]


def _bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        with contextlib.suppress(OSError):
            total += path.stat().st_size
    return total


def fold_pending(
    project_key: str,
    home: Path,
    project_dir: Path,
    *,
    exclude: str | None,
    env: Mapping[str, str],
    params: Mapping[str, Any] | None = None,
) -> list[FoldReport]:
    """SessionStart: fold up to 3 left-over sessions (idle >= 1 h) in-process when small and
    quick, else spawn a detached ``fold --all``. Never raises."""
    try:
        sids = indexed_sessions(project_key, home, exclude=exclude)
        if not sids:
            return []
        pdir = project_dir_for(home, project_key)
        size = _bytes(
            [session.session_path(home, project_key, s) for s in sids]
            + [pdir / DECISIONS_FILE, pdir / POSTERIORS_FILE, home / "global" / POSTERIORS_FILE]
        )
        spawn_args = [
            "fold",
            "--all",
            "--older-than",
            str(int(PENDING_AGE_S)),
            "--max",
            str(PENDING_PER_START),
            "--project",
            str(project_dir),
        ]
        if size > INPROCESS_MAX_BYTES:
            spawn_fold(spawn_args, env)
            return []
        out: list[FoldReport] = []
        start = time.monotonic()
        for sid in sids:
            if time.monotonic() - start > INPROCESS_BUDGET_S:
                break  # the rest waits for the next SessionStart
            try:
                out.append(
                    fold(sid, project_key, home, params=params, lock_timeout=INPROCESS_LOCK_S)
                )
            except jsonstore.StoreBusy:
                spawn_fold(spawn_args, env)
                break
        return out
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "fold", f"pending: {type(exc).__name__}: {exc}")
        return []


# --------------------------------------------------------------------------- spawning
def spawn_options(platform: str = sys.platform) -> dict[str, Any]:
    """Popen options that detach the child from the hook's process group and console."""
    if platform == "win32":
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def cli_command(args: list[str]) -> list[str]:
    """``python3 -I <plugin>/hooks/run_cli.py <args>`` (``-m token_sparen`` outside a plugin)."""
    script = Path(__file__).resolve().parents[2] / "hooks" / "run_cli.py"
    if script.is_file():
        return [sys.executable, "-I", str(script), *args]
    return [sys.executable, "-I", "-m", "token_sparen", *args]


def spawn_fold(args: list[str], env: Mapping[str, str]) -> bool:
    """Start ``token-sparen <args>`` detached (stdio to DEVNULL); never waits, never raises."""
    try:
        POPEN(
            cli_command(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env},
            close_fds=True,
            **spawn_options(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "fold", f"spawn: {type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------------------- learner state
def _tail_decisions(path: Path, n: int) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in data.splitlines()[-n:]:
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _real_obs(chain: StoreChain) -> float:
    """Real observations in the project store: per point and arm, the ``*`` bucket's count
    (it aggregates every bucket of the point)."""
    total = 0.0
    for point, buckets in chain.points("project").items():
        star = buckets.get(ANY_BUCKET) if isinstance(buckets, dict) else None
        if not isinstance(star, dict) or get_point(point) is None:
            continue
        for arm in star:
            total += max(
                (ev.n for k in _KINDS if (ev := chain.get(point, ANY_BUCKET, arm, k, "project"))),
                default=0.0,
            )
    return total


def best_arm(chain: StoreChain, point: DecisionPoint, bucket: str) -> str:
    """The posterior-best arm of a bucket: the largest savings mean among trimming arms whose
    regret mean is <= tau (project evidence + prior); the safe arm when none qualifies."""
    feats = features_of(bucket) or {}
    tau = breaker.tau_for(point, chain.params)
    best, best_s = point.safe_arm, 0.0
    for arm in point.arms:
        if arm == point.safe_arm:
            continue
        post: list[Beta] = []
        for kind in _KINDS:
            prior = prior_for(point, feats, arm, kind, chain, child_bucket=bucket)
            ev = chain.get(point.name, bucket, arm, kind, "project") or _EMPTY
            post.append(ev.posterior(prior))
        if post[1].mean <= tau and post[0].mean > best_s:
            best, best_s = arm, post[0].mean
    return best


def learner_state(project_key: str, home: Path | None = None) -> LearnerState:
    """For the statusline: ``safe-mode`` if any point is paused by the breaker; ``cold`` with
    < 20 real observations in the project; ``exploiting`` when >= 80 % of the last 50 logged
    decisions chose their bucket's posterior-best arm; else ``adapting``."""
    home = state_home(os.environ) if home is None else home
    pdir = project_dir_for(home, project_key)
    env = {"TOKEN_SPAREN_HOME": str(home)}
    bpath = pdir / breaker.BREAKER_FILE
    bdoc = jsonstore.read(bpath, {}, env) if bpath.exists() else {}
    points = bdoc.get("points")
    if isinstance(points, dict) and any(breaker.is_tripped(bdoc, p) for p in points):
        return "safe-mode"
    chain = StoreChain(project_key, home, None)
    if _real_obs(chain) < COLD_OBS:
        return "cold"
    recs = _tail_decisions(pdir / DECISIONS_FILE, EXPLOIT_WINDOW)
    best: dict[tuple[str, str], str] = {}
    hits = total = 0
    for rec in recs:
        point = get_point(str(rec.get("point")))
        bucket, arm = rec.get("bucket"), rec.get("arm")
        if point is None or not isinstance(bucket, str) or not isinstance(arm, str):
            continue
        if bucket == "error" or features_of(bucket) is None:
            continue
        key = (point.name, bucket)
        if key not in best:
            best[key] = best_arm(chain, point, bucket)
        total += 1
        hits += best[key] == arm
    if total and hits / total >= EXPLOIT_SHARE:
        return "exploiting"
    return "adapting"
