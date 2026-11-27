"""E1 at real-data scale: parallel, resumable, contamination-safe replay runs with lanes.

**Groups.** Transcripts are grouped by their parent directory (one Claude Code project
directory, or one session's subagent directory); the group key is the first 16 hex of the
sha256 of that directory's path, so neither reports nor checkpoints hold a path. One worker
replays one group: its files in chronological order (first record ``timestamp``, else the
file's mtime; ties by file name), with one ``RunIds`` de-duplication over the whole group
*before* split attribution, so a resumed session's copied history is counted once, in the
session that first held it, whatever split the resume falls in.

**Splits and contamination.** A session's day is its first record's timestamp: before
``cutoff`` it is ``dev``, else ``test``. A session whose day is on or after ``dogfood_start`` or
that holds any tool result with a ``[token-sparen:`` marker (it ran with the plugin on) is
excluded whole (``excluded_dogfood``); the decision is made by a pre-scan of the file before any
lane sees an event. ``split="dev"`` never feeds a test session; ``split="test"`` still reads the
dev sessions (their ids de-duplicate later copies) but feeds them only to lanes with
``history = True``.

**Malformed data.** Lines are read with a bounded ``readline``: a line over ``MAX_LINE_BYTES``
is skipped in chunks (``oversize_lines``), a line that is not UTF-8, not JSON or not an object
(a truncated last line included) is skipped (``malformed_lines``), and records of a type the
replay does not consume are counted (``skipped_records``). Nothing of their content is kept.

**Lanes.** A lane (``Lane``) is built inside the worker from a picklable ``LaneSpec`` =
``(name, params)`` by the ``LANES`` registry (a missing name is looked up once as the module
``token_sparen.replay.<name>_lane``, which registers itself on import). ``params`` may hold
``overlay`` (config keys applied over the loaded config), ``seed`` and ``label`` (the report key,
default the name). A lane's ``report()`` maps a split to a mergeable dict; reports of different
groups are combined by ``merge_reports``. The default merge adds numbers (exactly: floats are
summed as fractions, so a run's figures do not depend on scheduling or resumes), ORs booleans
and merges dicts key by key; a lane may register its own in ``MERGERS``.

**Failures.** An exception in a group is recorded as its type name only
(``worker_errors[type] += 1``) and the group contributes nothing; a resumed run retries it.

**Checkpoint.** After each finished group the whole checkpoint (merged state, counters, finished
group keys with a digest of their files' names, sizes and mtimes) is rewritten with
``report.write_private``. A run given an existing checkpoint resumes from it: finished groups
are skipped; a checkpoint of a different run, or a finished group whose files changed, raises
``CheckpointError``.
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
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from token_sparen.config import Config, load_config, redacted_logs
from token_sparen.replay import replay
from token_sparen.replay.report import write_private
from token_sparen.transcript import (
    SessionEvent,
    ToolResultRecord,
    events_from_records,
    tool_result_texts,
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
HEAD_RECORDS = 50  # the first timestamp / cwd is looked for in this many records
CONSUMED_TYPES = frozenset({"user", "assistant", "system"})
_COUNTERS = ("sessions", "excluded_dogfood", "malformed_lines", "oversize_lines", "skipped_records")
_FRACTION = "__fraction__"
_NAME = re.compile(r"[a-z][a-z0-9_]*")


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
    """One measurement over the replayed sessions. Optional attribute ``history = True``: the
    lane is also fed sessions of a split the run does not report (it needs the full history)."""

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


def lane_factory(name: str) -> LaneFactory:
    """The registered factory of lane ``name`` (importing its module once if needed)."""
    if name not in LANES and _NAME.fullmatch(name):
        try:
            importlib.import_module(f"token_sparen.replay.{name}_lane")
        except ImportError:
            pass
    factory = LANES.get(name)
    if factory is None:
        raise ValueError(f"unknown lane {name!r}: one of {', '.join(sorted(LANES))}")
    return factory


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
    oversize_marked: int = 0  # oversize lines that held the dogfood marker


def iter_lines(path: Path, stats: Stats) -> Iterator[bytes]:
    """Raw lines of ``path``; a line longer than ``MAX_LINE_BYTES`` is skipped in bounded chunks
    (counted, and checked for the dogfood marker on the way). A read error ends the file."""
    limit = MAX_LINE_BYTES
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        while True:
            try:
                raw = fh.readline(limit + 1)
            except (OSError, ValueError):
                return
            if not raw:
                return
            if len(raw) > limit and not raw.endswith(b"\n"):
                marked = _MARKER_BYTES in raw
                tail = raw[-len(_MARKER_BYTES) :]
                raw = b""
                while True:
                    try:
                        chunk = fh.readline(_CHUNK)
                    except (OSError, ValueError):
                        return
                    if not chunk:
                        break
                    marked = marked or _MARKER_BYTES in tail + chunk
                    tail = chunk[-len(_MARKER_BYTES) :]
                    if chunk.endswith(b"\n"):
                        break
                stats.oversize_lines += 1
                stats.oversize_marked += marked
                continue
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


def iter_records(path: Path, stats: Stats) -> Iterator[dict[str, Any]]:
    """Records of ``path`` (bounded, counted; see the module docstring)."""
    for raw in iter_lines(path, stats):
        rec = _parse(raw, stats)
        if rec is None:
            continue
        if rec.get("type") not in CONSUMED_TYPES:
            stats.skipped_records += 1
        yield rec


@dataclass(frozen=True)
class _Scan:
    path: Path
    start: float  # sort key: POSIX seconds of the first timestamp (else mtime)
    day: date
    cwd: str | None
    dogfood: bool


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _marked(rec: dict[str, Any]) -> bool:
    return any(MARKER in text for text in tool_result_texts(rec))


def scan(path: Path) -> _Scan:
    """Pre-scan: first timestamp and cwd (first ``HEAD_RECORDS`` records) and the dogfood
    marker (whole file; only lines holding the marker bytes are decoded past the head)."""
    stats = Stats()
    first: datetime | None = None
    cwd: str | None = None
    seen = 0
    dogfood = False
    for raw in iter_lines(path, stats):
        head = seen < HEAD_RECORDS
        if not head and _MARKER_BYTES not in raw:
            continue
        rec = _parse(raw, stats)
        if rec is None:
            continue
        if head:
            seen += 1
            first = first or _when(rec.get("timestamp"))
            value = rec.get("cwd")
            cwd = cwd or (value if isinstance(value, str) and value else None)
        if _MARKER_BYTES in raw and _marked(rec):
            dogfood = True
            break
    dogfood = dogfood or stats.oversize_marked > 0
    if first is None:
        try:
            first = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            first = datetime.min.replace(tzinfo=UTC)
    try:
        start = first.timestamp()
    except (OverflowError, ValueError, OSError):
        start = float("-inf")
    return _Scan(path, start, first.date(), cwd, dogfood)


# --------------------------------------------------------------------------- groups


def group_key(directory: Path) -> str:
    """First 16 hex of the sha256 of the (absolute) directory path."""
    return hashlib.sha256(str(directory.absolute()).encode("utf-8", "surrogatepass")).hexdigest()[
        :16
    ]


def group_files(paths: Iterable[Path]) -> dict[str, list[Path]]:
    """Transcripts (files as given; directories recursively, ``*.jsonl``) by group key."""
    out: dict[str, list[Path]] = {}
    seen: set[Path] = set()
    for f in replay.iter_transcripts(paths):
        a = f.absolute()
        if a in seen:
            continue
        seen.add(a)
        out.setdefault(group_key(a.parent), []).append(a)
    return out


def _signature(files: Sequence[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.name):
        try:
            st = f.stat()
            h.update(f"{f.name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "replace"))
        except OSError:
            h.update(f"{f.name}\0-\n".encode("utf-8", "replace"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- worker


@dataclass(frozen=True)
class _Task:
    key: str
    files: tuple[str, ...]
    specs: tuple[LaneSpec, ...]
    cfg: Config | None
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


def _replay_group(task: _Task, counters: dict[str, int]) -> dict[str, dict[str, Any]]:
    base = task.cfg if task.cfg is not None else load_config(Path.cwd(), os.environ)
    lanes: list[tuple[str, Lane]] = []
    for name, params in task.specs:
        cfg = apply_overlay(base, params.get("overlay"))
        lanes.append((_label((name, params)), lane_factory(name)(cfg, dict(params))))
    history = [lane for _, lane in lanes if getattr(lane, "history", False)]
    scans = sorted((scan(Path(f)) for f in task.files), key=lambda s: (s.start, s.path.name))
    run_ids = replay.RunIds()
    stats = Stats()
    for index, sc in enumerate(scans):
        if sc.dogfood or sc.day >= task.dogfood_start:
            counters["excluded_dogfood"] += 1
            continue
        split = "dev" if sc.day < task.cutoff else "test"
        reported = task.split in ("all", split)
        if task.split == "dev" and not reported:
            continue  # later than every dev session: needed by nothing reported
        fed = [lane for _, lane in lanes] if reported else history
        info = SessionInfo(task.key, index, split, sc.day, sc.cwd, run_ids)
        for ev in events_from_records(iter_records(sc.path, stats), is_new_id=run_ids.add):
            if isinstance(ev, ToolResultRecord):
                ev.duplicate = not run_ids.add(f"t:{ev.tool_use_id}")
            for lane in fed:
                lane.feed(ev, info)
        for lane in fed:
            lane.end_session(info)
        counters["sessions"] += reported
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


# --------------------------------------------------------------------------- parent


@dataclass
class RunResult:
    """Merged lane reports by split (``all`` = the reported splits merged) and run counters."""

    by_split: dict[str, dict[str, dict[str, Any]]]
    files: int = 0
    sessions: int = 0
    excluded_dogfood: int = 0
    malformed_lines: int = 0
    oversize_lines: int = 0
    skipped_records: int = 0
    worker_errors: dict[str, int] = field(default_factory=dict)
    groups: int = 0
    groups_resumed: int = 0
    wall_s: float = 0.0

    def counters(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "sessions": self.sessions,
            "excluded_dogfood": self.excluded_dogfood,
            "malformed_lines": self.malformed_lines,
            "oversize_lines": self.oversize_lines,
            "skipped_records": self.skipped_records,
            "worker_errors": dict(sorted(self.worker_errors.items())),
            "groups": self.groups,
            "groups_resumed": self.groups_resumed,
            "wall_s": round(self.wall_s, 3),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"by_split": self.by_split, **self.counters()}


class _Merged:
    """Exact running merge of finished groups (the checkpoint body)."""

    def __init__(self, names: Mapping[str, str]) -> None:
        self.names = names  # label -> lane name
        self.state: dict[str, dict[str, Any]] = {}
        self.counters = dict.fromkeys(_COUNTERS, 0)
        self.done: dict[str, str] = {}

    def absorb(self, key: str, sig: str, res: Mapping[str, Any]) -> None:
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
        self.done[key] = sig

    def dump(self, fingerprint: str) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "e1-checkpoint",
            "fingerprint": fingerprint,
            "done": dict(sorted(self.done.items())),
            "counters": self.counters,
            "state": _encode(self.state),
        }

    def load(self, doc: Any, fingerprint: str, sigs: Mapping[str, str]) -> None:
        if not isinstance(doc, dict) or doc.get("fingerprint") != fingerprint:
            raise CheckpointError("the checkpoint belongs to a different run (start afresh)")
        done = doc.get("done")
        if not isinstance(done, dict) or any(sigs.get(k) != v for k, v in done.items()):
            raise CheckpointError("transcripts changed since the checkpoint (start afresh)")
        self.done = {str(k): str(v) for k, v in done.items()}
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
    cutoff: date,
    dogfood_start: date,
    split: str,
    groups: set[str] | None,
) -> str:
    body = {
        "version": 1,
        "specs": [[n, p] for n, p in specs],
        "cutoff": cutoff.isoformat(),
        "dogfood_start": dogfood_start.isoformat(),
        "split": split,
        "groups": sorted(groups) if groups is not None else None,
        "max_line": MAX_LINE_BYTES,
    }
    text = json.dumps(body, sort_keys=True, default=repr)
    return hashlib.sha256(text.encode()).hexdigest()


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
    ``split`` limits what is reported; ``cfg`` (picklable) replaces the config each worker
    would load. With ``checkpoint``, finished groups are recorded and an existing checkpoint
    resumes."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"unknown split {split!r}")
    if not isinstance(jobs, int) or jobs < 1:
        raise ValueError("jobs must be >= 1")
    names: dict[str, str] = {}
    for name, _params in specs:
        lane_factory(name)
        label = _label((name, _params))
        if label in names:
            raise ValueError(f"lane {label!r} given twice (set a distinct 'label')")
        names[label] = name
    started = time.perf_counter()
    found = group_files(paths)
    if groups is not None:
        found = {k: v for k, v in found.items() if k in groups}
    sigs = {k: _signature(v) for k, v in found.items()}
    merged = _Merged(names)
    fingerprint = _fingerprint(specs, cutoff, dogfood_start, split, groups)
    resumed = 0
    if checkpoint is not None and checkpoint.is_file():
        try:
            doc = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CheckpointError("the checkpoint is unreadable (start afresh)") from None
        merged.load(doc, fingerprint, sigs)
        resumed = len(merged.done)
    tasks = [
        _Task(
            key,
            tuple(str(f) for f in found[key]),
            tuple((n, dict(p)) for n, p in specs),
            cfg,
            cutoff,
            dogfood_start,
            split,
        )
        for key in sorted(found)
        if key not in merged.done
    ]
    errors: dict[str, int] = {}

    def finish(res: Mapping[str, Any]) -> None:
        if res.get("error"):
            name = str(res["error"])
            errors[name] = errors.get(name, 0) + 1
            return
        key = str(res["key"])
        merged.absorb(key, sigs[key], res)
        if checkpoint is not None:
            write_private(checkpoint, merged.dump(fingerprint))

    if jobs == 1 or len(tasks) <= 1:
        for task in tasks:
            finish(_work_group(task))
    else:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks)), mp_context=ctx) as pool:
            futures: dict[Future[dict[str, Any]], str] = {
                pool.submit(_work_group, task): task.key for task in tasks
            }
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001 - a dead worker: type only
                    res = {"key": futures[fut], "error": type(exc).__name__}
                finish(res)
    c = merged.counters
    return RunResult(
        by_split=merged.result(split),
        files=sum(len(v) for v in found.values()),
        sessions=c["sessions"],
        excluded_dogfood=c["excluded_dogfood"],
        malformed_lines=c["malformed_lines"],
        oversize_lines=c["oversize_lines"],
        skipped_records=c["skipped_records"],
        worker_errors=errors,
        groups=len(found),
        groups_resumed=resumed,
        wall_s=time.perf_counter() - started,
    )
