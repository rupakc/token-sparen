"""E1 at real-data scale: parallel, resumable, contamination-safe replay runs with lanes.

**Groups.** A transcript's group is the top-level directory under the input root it was found
in (one Claude Code project directory); files directly in the root, and a session's own
directory ``<root>/<id>/`` next to ``<root>/<id>.jsonl`` (its ``subagents/``), belong to the
root's group. A file given directly belongs to its parent directory's group. The key is the
first 16 hex of the sha256 of the group directory's path, so neither reports nor checkpoints
hold a path. One worker replays one group with one ``RunIds`` de-duplication over the whole
group *before* split attribution.

**Order and dating.** A pre-scan reads every file of the group (bounded, see below). Files are
replayed in order of (first record timestamp, last record timestamp, name): a resume starts
with its original's copied history (same timestamps) but ends later. A session's day is the
timestamp of its first record that is *not* copied history, i.e. whose id (``m:`` assistant
message id, ``t:`` tool-use ids, else ``u:`` record uuid) the group has not seen yet; a
session made only of copies keeps its first timestamp. Before ``cutoff`` it is ``dev``, else
``test``.

**Contamination.** A session is excluded whole (``excluded_dogfood``) when any of its records
has a timestamp on or after ``dogfood_start`` or any line holds the raw ``[token-sparen:``
marker (tool results, hook context, attachments, system records, prompts alike). Exclusion
spreads between a session and its subagent files, both ways: files are linked by their file
stem, the last ``sessionId`` they record and any directory of their path named like a
session file of the group. The decision is made before any lane sees an event.
``split="dev"`` never feeds a test session; ``split="test"`` still reads the dev sessions
(their ids de-duplicate later copies) but feeds them only to lanes with ``history = True``.

**Malformed data.** Lines are read with a bounded ``readline``: a line over ``MAX_LINE_BYTES``
is skipped in chunks (``oversize_lines``; the old unbounded reader kept them), a line that is
not UTF-8, not JSON or not an object (a truncated last line included) is skipped
(``malformed_lines``), and records of a type the replay does not consume are counted
(``skipped_records``). An exception while replaying one session counts ``sessions_failed`` and
the group goes on.

**Lanes.** A lane (``Lane``) is built inside the worker from a picklable ``LaneSpec`` =
``(name, params)`` by the ``LANES`` registry. A worker process only knows lanes registered at
import time: a missing name is looked up once as the module ``token_sparen.replay.<name>_lane``
(or ``params["module"]``), which registers itself on import; a lane registered at run time in
the parent exists only for ``jobs=1``. ``params`` may hold ``overlay`` (config keys applied over
the loaded config), ``seed`` and ``label`` (the report key, default the name); a factory with
``base_config = True`` receives the run's config without the overlay and applies it itself (the
``sim`` lane also runs a reference learner without it). A lane's ``report()`` maps a split to
a mergeable dict. A lane with a ``feed_record(record, session)`` method is also shown every
parsed record of the session (system and attachment records too) before the runner registers
its ids, so ``session.run_ids`` still tells copied history apart;
it must not raise (an exception fails the session for every lane). Reports of different groups
are combined by ``merge_reports``. The default merge adds numbers exactly (floats summed as
fractions, so a run's figures depend neither on scheduling nor on resumes), ORs booleans and
merges dicts key by key. A lane may register its own merge in ``MERGERS``; that one runs on
plain floats, so its parallel results may differ from serial ones at rounding level.

**Failures.** A group whose lanes raise contributes nothing and is recorded by exception type
only. A worker process that dies (``BrokenProcessPool``, e.g. killed for memory) fails every
pending group of its pool: each is retried once, alone, in a fresh process. Groups still
failing are counted in ``failed_groups`` (type -> count) and the run is incomplete
(``RunResult.complete``); a resumed run retries them.

**Checkpoint.** The checkpoint is written at the start (a snapshot of every file in scope with
its size) and rewritten whole with ``report.write_private`` after each finished group (merged
state, counters, finished group keys). A run given an existing checkpoint resumes: finished
groups are skipped and the snapshot is replayed exactly (files added since are ignored, a file
that grew is read only up to its recorded size, a vanished file is skipped). A checkpoint whose
fingerprint (lane specs, effective config, package version, cutoff, dogfood start, split, group
selection) differs raises ``CheckpointError``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import multiprocessing
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

import token_sparen
from token_sparen.config import Config, load_config, redacted_logs
from token_sparen.replay import replay
from token_sparen.replay.report import write_private
from token_sparen.transcript import (
    SYNTHETIC_MODEL,
    SessionEvent,
    ToolResultRecord,
    events_from_records,
)

Split = replay.Split
Event = SessionEvent  # what a lane is fed: one transcript event of the session's own chain
LaneSpec = tuple[str, dict[str, Any]]

DEFAULT_CUTOFF = replay.DEFAULT_CUTOFF
DOGFOOD_START = replay.DOGFOOD_START
MAX_LINE_BYTES = 8 * 1024 * 1024
MARKER = "[token-sparen:"
_MARKER_BYTES = MARKER.encode()
_CHUNK = 1024 * 1024
HEAD_RECORDS = 50  # the cwd is looked for in this many records
CONSUMED_TYPES = frozenset({"user", "assistant", "system"})
_COUNTERS = (
    "sessions",
    "sessions_failed",
    "excluded_dogfood",
    "malformed_lines",
    "oversize_lines",
    "skipped_records",
)
_FRACTION = "__fraction__"
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_DEAD = "dead"  # a result whose worker process died


class CheckpointError(ValueError):
    """The checkpoint does not belong to this run (the message never quotes a path)."""


@dataclass(frozen=True)
class SessionInfo:
    """What a lane knows about the session it is fed (no path, no session id)."""

    group: str
    index: int  # position in the group's chronological order
    split: str  # "dev" | "test"
    day: date
    cwd: str | None
    run_ids: replay.RunIds = field(compare=False, repr=False)


class Lane(Protocol):
    """One measurement over the replayed sessions (see the module docstring for registration,
    merging and the optional ``history = True`` attribute: the lane is also fed sessions of a
    split the run does not report)."""

    def feed(self, event: Event, session: SessionInfo) -> None: ...

    def end_session(self, session: SessionInfo) -> None: ...

    def report(self) -> dict[str, Any]:
        """``{split: mergeable aggregates}`` for the splits it saw."""
        ...


LaneFactory = Callable[[Config, dict[str, Any]], Lane]


def _compression(cfg: Config, params: dict[str, Any]) -> Lane:
    return replay.CompressionLane(cfg, params)


LANES: dict[str, LaneFactory] = {"compression": _compression}
MERGERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}


def lane_factory(name: str, module: str | None = None) -> LaneFactory:
    """The registered factory of lane ``name``, importing ``module`` (default
    ``token_sparen.replay.<name>_lane``) once if it is not registered yet."""
    if name not in LANES and (module or _NAME.fullmatch(name)):
        target = module or f"token_sparen.replay.{name}_lane"
        try:
            importlib.import_module(target)
        except ModuleNotFoundError as exc:
            if exc.name != target:
                raise  # the lane module exists but one of its own imports is broken
    factory = LANES.get(name)
    if factory is None:
        raise ValueError(f"unknown lane {name!r}: one of {', '.join(sorted(LANES))}")
    return factory


def _module(params: Mapping[str, Any]) -> str | None:
    value = params.get("module")
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------- merging


def _merge(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    if isinstance(a, bool) and isinstance(b, bool):
        return a or b
    if isinstance(a, int | float | Fraction) and isinstance(b, int | float | Fraction):
        if isinstance(a, bool) or isinstance(b, bool):
            raise ValueError("cannot merge a flag with a number")
        return a + b
    if isinstance(a, list) and isinstance(b, list):
        return sorted([*a, *b], key=lambda x: json.dumps(_plain(x), sort_keys=True, default=str))
    raise ValueError(f"cannot merge {type(a).__name__} with {type(b).__name__}")


def merge_reports(name: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Combine two reports of lane ``name`` (associative and order-independent: numbers add,
    flags OR, dicts merge by key, lists concatenate sorted); ``MERGERS[name]`` overrides."""
    custom = MERGERS.get(name)
    if custom is not None:
        return custom(a, b)
    merged: dict[str, Any] = _merge(a, b)
    return merged


def _exact(obj: Any) -> Any:
    """Floats as exact fractions (finite ones), recursively."""
    if isinstance(obj, float):
        return Fraction(obj) if math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _exact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_exact(v) for v in obj]
    return obj


def _plain(obj: Any) -> Any:
    if isinstance(obj, Fraction):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def _encode(obj: Any) -> Any:
    if isinstance(obj, Fraction):
        return {_FRACTION: [obj.numerator, obj.denominator]}
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_encode(v) for v in obj]
    return obj


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj) == {_FRACTION}:
            n, d = obj[_FRACTION]
            return Fraction(int(n), int(d))
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- reading


@dataclass
class Stats:
    """Line and record counters of one read (aggregates only)."""

    malformed_lines: int = 0
    oversize_lines: int = 0
    skipped_records: int = 0
    marked: int = 0  # lines (oversize ones included) holding the dogfood marker


def iter_lines(path: Path, stats: Stats, size: int | None = None) -> Iterator[bytes]:
    """Raw lines of the first ``size`` bytes of ``path`` (all of it when None); a line longer
    than ``MAX_LINE_BYTES`` is skipped in bounded chunks (counted, and checked for the dogfood
    marker on the way). A read error ends the file."""
    limit = MAX_LINE_BYTES
    left = size if size is not None else -1

    def room(n: int) -> int:
        return n if left < 0 else min(n, left)

    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        while left != 0:
            try:
                raw = fh.readline(room(limit + 1))
            except (OSError, ValueError):
                return
            if not raw:
                return
            if left > 0:
                left -= len(raw)
            if len(raw) > limit and not raw.endswith(b"\n"):
                marked = _MARKER_BYTES in raw
                tail = raw[-len(_MARKER_BYTES) :]
                while left != 0:
                    try:
                        chunk = fh.readline(room(_CHUNK))
                    except (OSError, ValueError):
                        return
                    if not chunk:
                        break
                    if left > 0:
                        left -= len(chunk)
                    marked = marked or _MARKER_BYTES in tail + chunk
                    tail = chunk[-len(_MARKER_BYTES) :]
                    if chunk.endswith(b"\n"):
                        break
                stats.oversize_lines += 1
                stats.marked += marked
                continue
            if _MARKER_BYTES in raw:
                stats.marked += 1
            yield raw


def _parse(raw: bytes, stats: Stats) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        rec = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        stats.malformed_lines += 1
        return None
    if not isinstance(rec, dict):
        stats.malformed_lines += 1
        return None
    return rec


def iter_records(path: Path, stats: Stats, size: int | None = None) -> Iterator[dict[str, Any]]:
    """Records of the first ``size`` bytes of ``path`` (bounded, counted; see the module
    docstring)."""
    for raw in iter_lines(path, stats, size):
        rec = _parse(raw, stats)
        if rec is None:
            continue
        if rec.get("type") not in CONSUMED_TYPES:
            stats.skipped_records += 1
        yield rec


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _stamp(dt: datetime) -> float:
    try:
        return dt.timestamp()
    except (OverflowError, ValueError, OSError):
        return float("-inf")


@dataclass(frozen=True)
class _Scan:
    path: Path
    size: int
    first: float  # sort keys: POSIX seconds of the first and last timestamps (else mtime)
    last: float
    first_day: date
    max_day: date | None  # latest record day (any record, copied history included)
    cwd: str | None
    session_id: str | None  # the last ``sessionId`` recorded (a resume's own id)
    marked: bool


def scan(path: Path, size: int | None = None) -> _Scan:
    """Pre-scan of one transcript: first / last / latest timestamp, cwd, last session id and
    whether any line holds the dogfood marker."""
    stats = Stats()
    first: datetime | None = None
    last: datetime | None = None
    latest: datetime | None = None
    cwd: str | None = None
    sid: str | None = None
    for n, rec in enumerate(iter_records(path, stats, size)):
        when = _when(rec.get("timestamp"))
        if when is not None:
            first = first or when
            last = when
            latest = when if latest is None or when > latest else latest
        if n < HEAD_RECORDS and cwd is None:
            value = rec.get("cwd")
            cwd = value if isinstance(value, str) and value else None
        value = rec.get("sessionId")
        if isinstance(value, str) and value:
            sid = value
    if first is None or last is None:
        try:
            first = last = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            first = last = datetime.min.replace(tzinfo=UTC)
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    return _Scan(
        path,
        size,
        _stamp(first),
        _stamp(last),
        first.date(),
        latest.date() if latest is not None else None,
        cwd,
        sid,
        stats.marked > 0,
    )


def _has_result(rec: Mapping[str, Any]) -> bool:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _identity(rec: Mapping[str, Any]) -> list[str]:
    """The de-duplication keys of a record (empty: it says nothing about being copied)."""
    msg = rec.get("message")
    msg = msg if isinstance(msg, dict) else {}
    if rec.get("type") == "assistant":
        mid = msg.get("id")
        ok = isinstance(mid, str) and msg.get("model") != SYNTHETIC_MODEL
        return [f"m:{mid}"] if ok else []
    if rec.get("type") != "user":
        return []
    content = msg.get("content")
    if _has_result(rec) and isinstance(content, list):
        return [
            f"t:{b['tool_use_id']}"
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "tool_result"
            and isinstance(b.get("tool_use_id"), str)
        ]
    uid = rec.get("uuid")
    return [f"u:{uid}"] if isinstance(uid, str) and uid else []


def _with_uuids(records: Iterable[dict[str, Any]], ids: replay.RunIds) -> Iterator[dict[str, Any]]:
    """Records unchanged; user records without a tool result register their uuid (``u:``)."""
    for rec in records:
        if rec.get("type") == "user" and not _has_result(rec):
            uid = rec.get("uuid")
            if isinstance(uid, str) and uid:
                ids.add(f"u:{uid}")
        yield rec


def session_day(sc: _Scan, ids: replay.RunIds) -> date:
    """The day of the session's first record of its own chain that the group has not seen
    (not copied history); a session of copies only keeps its first day."""
    chain: bool | None = None
    for rec in iter_records(sc.path, Stats(), sc.size):
        if rec.get("type") not in ("user", "assistant"):
            continue
        side = bool(rec.get("isSidechain"))
        chain = side if chain is None else chain
        if side != chain:
            continue
        keys = _identity(rec)
        if keys and not all(ids.has(k) for k in keys):
            when = _when(rec.get("timestamp"))
            if when is not None:
                return when.date()
    return sc.first_day


# --------------------------------------------------------------------------- groups


def group_key(directory: Path) -> str:
    """First 16 hex of the sha256 of the (absolute) directory path."""
    return hashlib.sha256(str(directory.absolute()).encode("utf-8", "surrogatepass")).hexdigest()[
        :16
    ]


def _group_dir(root: Path, f: Path) -> Path:
    parts = f.relative_to(root).parts
    if len(parts) == 1 or (root / f"{parts[0]}.jsonl").is_file():
        return root  # in the root, or in a root session's own directory (its subagents)
    return root / parts[0]


def group_files(paths: Iterable[Path]) -> dict[str, list[Path]]:
    """Transcripts by group key (see the module docstring): directories recursively
    (``*.jsonl``), files as given."""
    out: dict[str, list[Path]] = {}
    seen: set[Path] = set()
    for p in paths:
        p = Path(os.path.expanduser(str(p))).absolute()
        if p.is_dir():
            pairs = [(f, _group_dir(p, f)) for f in sorted(p.rglob("*.jsonl")) if f.is_file()]
        elif p.is_file():
            pairs = [(p, p.parent)]
        else:
            continue
        for f, gdir in pairs:
            if f not in seen:
                seen.add(f)
                out.setdefault(group_key(gdir), []).append(f)
    return out


def _file_key(path: Path) -> str:
    return hashlib.sha256(str(path.absolute()).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _excluded(scans: Sequence[_Scan], dogfood_start: date) -> set[Path]:
    """Files excluded as dogfood, spread over linked files (a session and its subagents)."""
    stems = {sc.path.stem for sc in scans}
    links: dict[Path, set[str]] = {}
    for sc in scans:
        keys = {sc.path.stem, *(p.name for p in sc.path.parents if p.name in stems)}
        if sc.session_id:
            keys.add(sc.session_id)
        links[sc.path] = keys
    out = {
        sc.path
        for sc in scans
        if sc.marked or (sc.max_day is not None and sc.max_day >= dogfood_start)
    }
    tainted: set[str] = set().union(*(links[p] for p in out)) if out else set()
    changed = bool(out)
    while changed:
        changed = False
        for sc in scans:
            if sc.path not in out and links[sc.path] & tainted:
                out.add(sc.path)
                tainted |= links[sc.path]
                changed = True
    return out


# --------------------------------------------------------------------------- worker


@dataclass(frozen=True)
class _Task:
    key: str
    files: tuple[tuple[str, int], ...]  # (path, size to read)
    specs: tuple[LaneSpec, ...]
    cfg: Config
    cutoff: date
    dogfood_start: date
    split: str


def apply_overlay(cfg: Config, overlay: Mapping[str, Any] | None) -> Config:
    """``cfg`` with ``overlay`` (nested tables or dotted keys) merged over its data."""
    if not overlay:
        return cfg
    data = json.loads(json.dumps(cfg.data))
    for key, value in overlay.items():
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        leaf = parts[-1]
        if isinstance(value, Mapping) and isinstance(node.get(leaf), dict):
            node[leaf] = _merge_tables(node[leaf], value)
        else:
            node[leaf] = value
    return Config(data=data, disabled=cfg.disabled, trusted=cfg.trusted)


def _merge_tables(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = (
            _merge_tables(out[k], v)
            if isinstance(v, Mapping) and isinstance(out.get(k), dict)
            else v
        )
    return out


def _label(spec: LaneSpec) -> str:
    label = spec[1].get("label", spec[0])
    return label if isinstance(label, str) else spec[0]


def _abort(lanes: Iterable[Lane], info: SessionInfo) -> None:
    for lane in lanes:
        abort = getattr(lane, "abort_session", None)
        if abort is None:
            continue
        try:
            abort(info)
        except Exception:  # noqa: BLE001, S110 - the session is already counted as failed
            pass


def _tap(
    records: Iterable[dict[str, Any]], taps: Sequence[Any], info: SessionInfo
) -> Iterator[dict[str, Any]]:
    """Records unchanged; each is first shown to the lanes with ``feed_record``."""
    for rec in records:
        for lane in taps:
            lane.feed_record(rec, info)
        yield rec


def _replay_session(
    sc: _Scan, info: SessionInfo, fed: Sequence[Lane], ids: replay.RunIds, stats: Stats
) -> None:
    raw: Iterable[dict[str, Any]] = iter_records(sc.path, stats, sc.size)
    taps = [lane for lane in fed if callable(getattr(lane, "feed_record", None))]
    if taps:
        raw = _tap(raw, taps, info)
    records = _with_uuids(raw, ids)
    for ev in events_from_records(records, is_new_id=ids.add):
        if isinstance(ev, ToolResultRecord):
            ev.duplicate = not ids.add(f"t:{ev.tool_use_id}")
        for lane in fed:
            lane.feed(ev, info)
    for lane in fed:
        lane.end_session(info)


def _replay_group(task: _Task, counters: dict[str, int]) -> dict[str, dict[str, Any]]:
    lanes: list[tuple[str, Lane]] = []
    for name, params in task.specs:
        factory = lane_factory(name, _module(params))
        # a factory marked ``base_config`` gets the run's config and applies ``overlay`` itself
        # (the simulation lane also runs a reference learner without the overlay)
        base = getattr(factory, "base_config", False) is True
        cfg = task.cfg if base else apply_overlay(task.cfg, params.get("overlay"))
        lanes.append((_label((name, params)), factory(cfg, dict(params))))
    history = [lane for _, lane in lanes if getattr(lane, "history", False)]
    scans = sorted(
        (scan(Path(f), size) for f, size in task.files),
        key=lambda s: (s.first, s.last, s.path.name),
    )
    excluded = _excluded(scans, task.dogfood_start)
    ids = replay.RunIds()
    stats = Stats()
    for index, sc in enumerate(scans):
        if sc.path in excluded:
            counters["excluded_dogfood"] += 1
            continue
        fed: list[Lane] = []
        info: SessionInfo | None = None
        try:
            day = session_day(sc, ids)
            split = "dev" if day < task.cutoff else "test"
            reported = task.split in ("all", split)
            if task.split == "dev" and not reported:
                continue  # a test session: nothing reported needs it
            fed = [lane for _, lane in lanes] if reported else history
            info = SessionInfo(task.key, index, split, day, sc.cwd, ids)
            _replay_session(sc, info, fed, ids, stats)
            counters["sessions"] += reported
        except Exception:  # noqa: BLE001 - one bad session never stops the group (no details)
            counters["sessions_failed"] += 1
            if info is not None:
                _abort(fed, info)
    counters["malformed_lines"] += stats.malformed_lines
    counters["oversize_lines"] += stats.oversize_lines
    counters["skipped_records"] += stats.skipped_records
    state: dict[str, dict[str, Any]] = {}
    for label, lane in lanes:
        rep = lane.report()
        for part in replay.SPLITS:
            if task.split in ("all", part) and isinstance(rep.get(part), dict):
                state.setdefault(part, {})[label] = rep[part]
    return state


def _work_group(task: _Task) -> dict[str, Any]:
    """Replay one group (top level: spawn-safe). Never raises: an exception is returned as its
    type name only."""
    counters = dict.fromkeys(_COUNTERS, 0)
    try:
        with redacted_logs():
            state = _replay_group(task, counters)
    except Exception as exc:  # noqa: BLE001 - recorded as the type only, never the message
        return {"key": task.key, "error": type(exc).__name__}
    return {"key": task.key, "error": None, "state": state, "counters": counters}


def _isolated(task: _Task) -> dict[str, Any]:
    """Retry of a group whose worker died: alone, in a fresh process."""
    ctx = multiprocessing.get_context("spawn")
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            return pool.submit(_work_group, task).result()
    except Exception as exc:  # noqa: BLE001 - a dead worker: type only
        return {"key": task.key, "error": type(exc).__name__, _DEAD: True}


# --------------------------------------------------------------------------- parent


@dataclass
class RunResult:
    """Merged lane reports by split (``all`` = the reported splits merged) and run counters."""

    by_split: dict[str, dict[str, dict[str, Any]]]
    files: int = 0
    sessions: int = 0
    sessions_failed: int = 0
    excluded_dogfood: int = 0
    malformed_lines: int = 0
    oversize_lines: int = 0
    skipped_records: int = 0
    worker_errors: dict[str, int] = field(default_factory=dict)  # every failure, retries too
    failed_groups: dict[str, int] = field(default_factory=dict)  # groups that added nothing
    groups: int = 0
    groups_resumed: int = 0
    wall_s: float = 0.0

    @property
    def complete(self) -> bool:
        """Every group in scope contributed (nothing to retry with a resume)."""
        return not self.failed_groups

    def counters(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "files": self.files,
            "sessions": self.sessions,
            "sessions_failed": self.sessions_failed,
            "excluded_dogfood": self.excluded_dogfood,
            "malformed_lines": self.malformed_lines,
            "oversize_lines": self.oversize_lines,
            "skipped_records": self.skipped_records,
            "worker_errors": dict(sorted(self.worker_errors.items())),
            "failed_groups": dict(sorted(self.failed_groups.items())),
            "groups": self.groups,
            "groups_resumed": self.groups_resumed,
            "wall_s": round(self.wall_s, 3),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"by_split": self.by_split, **self.counters()}


class _Merged:
    """Exact running merge of finished groups and the run's snapshot (the checkpoint body)."""

    def __init__(self, names: Mapping[str, str]) -> None:
        self.names = names  # label -> lane name
        self.state: dict[str, dict[str, Any]] = {}
        self.counters = dict.fromkeys(_COUNTERS, 0)
        self.done: list[str] = []
        self.snapshot: dict[str, dict[str, int]] = {}  # group -> file key -> size

    def absorb(self, key: str, res: Mapping[str, Any]) -> None:
        for part, lanes in res["state"].items():
            into = self.state.setdefault(part, {})
            for label, rep in lanes.items():
                name = self.names.get(label, label)
                if name in MERGERS:
                    into[label] = (
                        rep if label not in into else merge_reports(name, into[label], rep)
                    )
                else:
                    into[label] = _merge(into.get(label), _exact(rep))
        for k in _COUNTERS:
            self.counters[k] += int(res["counters"].get(k, 0))
        self.done.append(key)

    def dump(self, fingerprint: str) -> dict[str, Any]:
        return {
            "version": 2,
            "kind": "e1-checkpoint",
            "fingerprint": fingerprint,
            "snapshot": self.snapshot,
            "done": sorted(self.done),
            "counters": self.counters,
            "state": _encode(self.state),
        }

    def load(self, doc: Any, fingerprint: str) -> None:
        if not isinstance(doc, dict) or doc.get("fingerprint") != fingerprint:
            raise CheckpointError(
                "the checkpoint belongs to a different run (lanes, config, package version, "
                "dates, split or group selection differ)"
            )
        snapshot, done = doc.get("snapshot"), doc.get("done")
        if not isinstance(snapshot, dict) or not isinstance(done, list):
            raise CheckpointError("the checkpoint is incomplete")
        self.snapshot = {
            str(g): {str(f): int(s) for f, s in files.items()}
            for g, files in snapshot.items()
            if isinstance(files, dict)
        }
        self.done = [str(k) for k in done]
        self.state = _decode(doc.get("state") or {})
        counters = doc.get("counters") or {}
        self.counters = {k: int(counters.get(k, 0)) for k in _COUNTERS}

    def result(self, split: str) -> dict[str, dict[str, Any]]:
        parts = [p for p in replay.SPLITS if split in ("all", p)]
        out: dict[str, dict[str, Any]] = {p: dict(self.state.get(p, {})) for p in parts}
        every: dict[str, Any] = {}
        for p in parts:
            for label, rep in out[p].items():
                if label in every:
                    name = self.names.get(label, label)
                    every[label] = (
                        merge_reports(name, every[label], rep)
                        if name in MERGERS
                        else _merge(every[label], rep)
                    )
                else:
                    every[label] = rep
        out["all"] = every
        plain: dict[str, dict[str, Any]] = _plain(out)
        return plain


def _fingerprint(
    specs: Sequence[LaneSpec],
    cfg: Config,
    cutoff: date,
    dogfood_start: date,
    split: str,
    groups: set[str] | None,
) -> str:
    config = json.dumps(cfg.data, sort_keys=True, default=repr)
    body = {
        "version": 2,
        "package": token_sparen.__version__,
        "config": hashlib.sha256(config.encode()).hexdigest(),
        "specs": [[n, p] for n, p in specs],
        "cutoff": cutoff.isoformat(),
        "dogfood_start": dogfood_start.isoformat(),
        "split": split,
        "groups": sorted(groups) if groups is not None else None,
        "max_line": MAX_LINE_BYTES,
    }
    text = json.dumps(body, sort_keys=True, default=repr)
    return hashlib.sha256(text.encode()).hexdigest()


def _snapshot(found: Mapping[str, Sequence[Path]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for key, files in found.items():
        row = out.setdefault(key, {})
        for f in files:
            try:
                row[_file_key(f)] = f.stat().st_size
            except OSError:
                continue
    return out


def run_parallel(
    paths: Sequence[Path],
    specs: Sequence[LaneSpec],
    *,
    jobs: int,
    cutoff: date = DEFAULT_CUTOFF,
    dogfood_start: date = DOGFOOD_START,
    groups: set[str] | None = None,
    checkpoint: Path | None = None,
    split: str = "all",
    cfg: Config | None = None,
) -> RunResult:
    """Replay ``paths`` (files or directories) through the lanes ``specs``, one group per
    worker task on ``jobs`` processes (1 = in this process). ``groups`` selects group keys;
    ``split`` limits what is reported; ``cfg`` (picklable) is the config every worker uses
    (default: loaded once here). With ``checkpoint``, the run's snapshot and finished groups are
    recorded and an existing checkpoint resumes."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"unknown split {split!r}")
    if not isinstance(jobs, int) or jobs < 1:
        raise ValueError("jobs must be >= 1")
    names: dict[str, str] = {}
    for name, params in specs:
        lane_factory(name, _module(params))
        label = _label((name, params))
        if label in names:
            raise ValueError(f"lane {label!r} given twice (set a distinct 'label')")
        names[label] = name
    started = time.perf_counter()
    base = cfg if cfg is not None else load_config(Path.cwd(), os.environ)
    fingerprint = _fingerprint(specs, base, cutoff, dogfood_start, split, groups)
    found = group_files(paths)
    if groups is not None:
        found = {k: v for k, v in found.items() if k in groups}
    merged = _Merged(names)
    resumed = 0
    if checkpoint is not None and checkpoint.is_file():
        try:
            doc = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CheckpointError("the checkpoint is unreadable") from None
        merged.load(doc, fingerprint)
        resumed = len(merged.done)
    else:
        merged.snapshot = _snapshot(found)
        if checkpoint is not None:
            write_private(checkpoint, merged.dump(fingerprint))
    by_key = {key: {_file_key(f): f for f in found.get(key, [])} for key in merged.snapshot}
    finished = set(merged.done)
    tasks = [
        _Task(
            key,
            tuple(
                (str(by_key[key][fk]), size)
                for fk, size in sorted(merged.snapshot[key].items())
                if fk in by_key[key]
            ),
            tuple((n, dict(p)) for n, p in specs),
            base,
            cutoff,
            dogfood_start,
            split,
        )
        for key in sorted(merged.snapshot)
        if key not in finished
    ]
    errors: dict[str, int] = {}
    failed: dict[str, int] = {}

    def finish(res: Mapping[str, Any], final: bool) -> bool:
        """Absorb a group result; False if it is a dead worker to retry."""
        if res.get("error"):
            name = str(res["error"])
            errors[name] = errors.get(name, 0) + 1
            if res.get(_DEAD) and not final:
                return False
            failed[name] = failed.get(name, 0) + 1
            return True
        merged.absorb(str(res["key"]), res)
        if checkpoint is not None:
            write_private(checkpoint, merged.dump(fingerprint))
        return True

    if jobs == 1 or len(tasks) <= 1:
        for task in tasks:
            finish(_work_group(task), final=True)
    else:
        retry: list[_Task] = []
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks)), mp_context=ctx) as pool:
            futures: dict[Future[dict[str, Any]], _Task] = {
                pool.submit(_work_group, task): task for task in tasks
            }
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001 - a dead worker: type only
                    res = {"key": futures[fut].key, "error": type(exc).__name__, _DEAD: True}
                if not finish(res, final=False):
                    retry.append(futures[fut])
        if retry:
            with ThreadPoolExecutor(max_workers=min(jobs, len(retry))) as threads:
                for res in threads.map(_isolated, sorted(retry, key=lambda t: t.key)):
                    finish(res, final=True)
    c = merged.counters
    return RunResult(
        by_split=merged.result(split),
        files=sum(len(v) for v in merged.snapshot.values()),
        sessions=c["sessions"],
        sessions_failed=c["sessions_failed"],
        excluded_dogfood=c["excluded_dogfood"],
        malformed_lines=c["malformed_lines"],
        oversize_lines=c["oversize_lines"],
        skipped_records=c["skipped_records"],
        worker_errors=errors,
        failed_groups=failed,
        groups=len(merged.snapshot),
        groups_resumed=resumed,
        wall_s=time.perf_counter() - started,
    )
