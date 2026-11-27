"""Append-only E4 ledger of one campaign stage (M5 Task 7).

One JSON object per line, private (0600). The first line is the header::

    {"kind": "e4-ledger", "schema": 1, "campaign", "stage", "harness_commit", "plugin_hash",
     "plugin_ref", "pins", "model", "claude_code_version", "price_table_version"}

Every later record has ``kind`` (one of `RECORD_KINDS`) and ``at`` (epoch seconds):

* ``start``: ``key`` (`RunKey.id`), ``attempt``, ``container``, ``plugin_hash`` (re-verified
  just before the run, None without a frozen plugin), ``run_dir``.
* ``end``: ``key``, ``attempt``, ``status`` (``ok`` / ``agent_fail`` / ``infra_fail``), ``sub``
  (the sub-status, e.g. ``success``, ``timeout``, ``api``), ``metrics`` (`RunMetrics` as a
  mapping, or None), ``run_cost`` (USD, None when unknown), ``patch_sha256``, ``wall_s``,
  ``repo``, ``model``, ``claude_code_version``, ``plugin_hash``, ``live`` (True for a graded
  outcome: an inert arm is ``infra_fail/load``), ``problems`` and ``run_dir`` (relative to the
  ledger's directory; the patch is ``<run_dir>/patch.diff``).
* ``quota``: a `bench.quota.QuotaReading` (``source``, ``quota_kind``, ``utilization_pct``,
  ``resets_at``, ``status``; ``at`` = when it was read).
* ``pause``: ``reason`` (``five_hour``, ``weekly``, ``window_cap``, ``breaker``, ``disk``,
  ``auth``, ``leak``, ``plugin_changed``) and details; a disk pause carries
  ``status = "infra_fail"``, ``sub = "disk"`` (never counted by the breaker).
* ``invalidate`` (operator command): ``key`` or a time range ``from`` / ``to`` over the ``at`` of
  earlier ``end`` records, and ``reason``. It reopens the key(s): their earlier ends no longer
  count, as final or as infra attempts.
* ``promote``: ``stream`` and ``key``: the run's learner state became the stream's state.
* ``graded``: ``stage``, ``instance_id``: a task's block was graded and its image released.

A key's final outcome is its last non-invalidated ``end`` with status ``ok`` or ``agent_fail``;
``infra_fail`` is never final. These shapes are what `bench.stats.summarise` (Task 9) reads.

Appends hold ``<ledger>.lock`` and are fsynced; a torn last line (a crash mid-write) is ignored
on read and the next append starts on a fresh line.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.streamjson import PRICE_TABLE_VERSION
from token_sparen.store.jsonstore import locked

LEDGER_KIND = "e4-ledger"
SCHEMA = 1
HEADER_KEYS = (
    "campaign",
    "stage",
    "harness_commit",
    "plugin_hash",
    "plugin_ref",
    "pins",
    "model",
    "claude_code_version",
)
RECORD_KINDS = frozenset({"start", "end", "quota", "pause", "invalidate", "promote", "graded"})
GRADED = frozenset({"ok", "agent_fail"})
MAX_INFRA_ATTEMPTS = 3
# infra sub-statuses that never count as an attempt (re-queued / paused, not the key's fault)
NOT_AN_ATTEMPT = frozenset({"rate", "disk"})
LOCK_TIMEOUT_S = 30.0


class LedgerError(ValueError):
    """The ledger is malformed, does not match the campaign, or a record is invalid."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True)
class RunKey:
    campaign: str
    stage: str
    arm: str
    seed: int
    instance_id: str

    def __post_init__(self) -> None:
        for part in (self.campaign, self.stage, self.arm, self.instance_id):
            if not part or "/" in part:
                raise ValueError(f"run key part must be non-empty without '/': {part!r}")

    @property
    def id(self) -> str:
        return "/".join((self.campaign, self.stage, self.arm, str(self.seed), self.instance_id))

    @classmethod
    def from_id(cls, key_id: str) -> RunKey:
        parts = key_id.split("/")
        if len(parts) != 5:
            raise ValueError(f"not a run key id: {key_id!r}")
        try:
            seed = int(parts[3])
        except ValueError as exc:
            raise ValueError(f"run key seed is not an integer: {key_id!r}") from exc
        return cls(parts[0], parts[1], parts[2], seed, parts[4])


@dataclass(frozen=True)
class FinalRun:
    """A key whose final outcome is graded (``ok`` / ``agent_fail``), with its patch text
    (what Task 8's grading reads through ``final_runs``)."""

    key: str
    stage: str
    arm: str
    seed: int
    instance_id: str
    patch: str


def _write_line(path: Path, doc: Mapping[str, Any]) -> None:
    line = (json.dumps(doc, sort_keys=True, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":  # a torn last line: never glue onto it
                line = b"\n" + line
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue  # a torn line (crash mid-append): not a record
        if isinstance(doc, dict):
            out.append(doc)
    return out


class Ledger:
    """The ledger at ``path``. With ``header`` a missing file is created with it; an existing
    one must carry the same values for every header key (a resumed campaign cannot silently
    change its plugin, model or harness). Without ``header`` the file must exist."""

    def __init__(self, path: Path, *, header: Mapping[str, Any] | None = None) -> None:
        self.path = path
        self._cache: list[dict[str, Any]] = []
        self._stamp: tuple[int, int] | None = None
        lines = _read_lines(path)
        if not lines:
            if header is None:
                raise LedgerError(f"no ledger at {path}")
            missing = [k for k in HEADER_KEYS if k not in header]
            if missing:
                raise LedgerError(f"ledger header lacks {', '.join(missing)}")
            doc = {"kind": LEDGER_KIND, "schema": SCHEMA, **dict(header)}
            doc.setdefault("price_table_version", PRICE_TABLE_VERSION)
            path.parent.mkdir(parents=True, exist_ok=True)
            with locked(path, timeout=LOCK_TIMEOUT_S):
                _write_line(path, doc)
            self.header: dict[str, Any] = doc
            return
        first = lines[0]
        if first.get("kind") != LEDGER_KIND or first.get("schema") != SCHEMA:
            raise LedgerError(f"not an E4 ledger (schema {SCHEMA}): {path}")
        if header is not None:
            for k in (*HEADER_KEYS, "price_table_version"):
                if k in header and header[k] != first.get(k):
                    raise LedgerError(f"ledger header {k} differs: {first.get(k)!r}")
        self.header = first

    @property
    def campaign(self) -> str:
        return str(self.header["campaign"])

    def append(self, rec: Mapping[str, Any]) -> None:
        if rec.get("kind") not in RECORD_KINDS:
            raise LedgerError(f"unknown ledger record kind: {rec.get('kind')!r}")
        doc = dict(rec)
        doc.setdefault("at", time.time())
        doc["at"] = float(doc["at"])
        with locked(self.path, timeout=LOCK_TIMEOUT_S):
            _write_line(self.path, doc)

    def records(self) -> list[dict[str, Any]]:
        """Every record after the header, in append order (re-read when the file changed;
        callers get copies, the cache is never mutated)."""
        try:
            st = self.path.stat()
            stamp: tuple[int, int] | None = (st.st_size, st.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp is None or stamp != self._stamp:
            self._cache = _read_lines(self.path)[1:]
            self._stamp = stamp
        return [dict(r) for r in self._cache]

    def resolve(self, rel: str) -> Path:
        """A ``run_dir`` of a record as a path (relative to the ledger's directory)."""
        return self.path.parent / rel

    # --- derived state -------------------------------------------------------------------

    def _ends(self, records: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
        """key id → its non-invalidated ``end`` records, in order."""
        ends: dict[str, list[dict[str, Any]]] = {}
        for rec in self.records() if records is None else records:
            kind = rec.get("kind")
            if kind == "end" and isinstance(rec.get("key"), str):
                ends.setdefault(rec["key"], []).append(rec)
            elif kind == "invalidate":
                if isinstance(rec.get("key"), str):
                    ends[rec["key"]] = []
                elif rec.get("from") is not None and rec.get("to") is not None:
                    lo, hi = float(rec["from"]), float(rec["to"])
                    for k, lst in ends.items():
                        ends[k] = [e for e in lst if not lo <= float(e.get("at", 0.0)) <= hi]
        return ends

    def final(self, key: RunKey) -> dict[str, Any] | None:
        return _final_of(self._ends().get(key.id, []))

    def infra_attempts(self, key: RunKey) -> int:
        """Counted infra failures of ``key`` (not ``rate`` / ``disk``) since it was reopened."""
        return _infra_count(self._ends().get(key.id, []))

    def unrunnable(self, key: RunKey) -> bool:
        ends = self._ends().get(key.id, [])
        return _final_of(ends) is None and _infra_count(ends) >= MAX_INFRA_ATTEMPTS

    def next_attempt(self, key: RunKey) -> int:
        """1 + every ``start`` of the key ever (container names and run dirs never reused)."""
        starts = sum(1 for r in self.records() if r.get("kind") == "start" and r["key"] == key.id)
        return starts + 1

    def finals(self, *, stage: str, instance_id: str | None = None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key_id, ends in self._ends().items():
            final = _final_of(ends)
            if final is None:
                continue
            key = RunKey.from_id(key_id)
            if key.stage == stage and instance_id in (None, key.instance_id):
                out[key_id] = final
        return out

    def final_runs(self, stage: str) -> list[FinalRun]:
        """Every final run of ``stage`` with its patch (``<run_dir>/patch.diff``, checked
        against the recorded sha256; a missing file is an empty patch only if that is what
        was recorded)."""
        runs: list[FinalRun] = []
        for key_id, end in sorted(self.finals(stage=stage).items()):
            key = RunKey.from_id(key_id)
            patch = ""
            if end.get("run_dir"):
                path = self.resolve(str(end["run_dir"])) / "patch.diff"
                if path.is_file():
                    patch = path.read_bytes().decode("utf-8", "surrogateescape")
            if end.get("patch_sha256") != sha256_text(patch):
                raise LedgerError(f"patch of {key_id} differs from its recorded sha256")
            runs.append(FinalRun(key_id, key.stage, key.arm, key.seed, key.instance_id, patch))
        return runs

    def invalidate(
        self,
        key: RunKey | None,
        *,
        reason: str,
        start: float | None = None,
        end: float | None = None,
    ) -> None:
        """Operator command: reopen ``key``, or every key whose ``end`` lies in [start, end]."""
        if key is not None:
            self.append({"kind": "invalidate", "key": key.id, "reason": reason})
        elif start is not None and end is not None and start <= end:
            self.append({"kind": "invalidate", "from": start, "to": end, "reason": reason})
        else:
            raise LedgerError("invalidate needs a key or a time range (start <= end)")

    def iter_kind(self, kind: str) -> Iterator[dict[str, Any]]:
        return (r for r in self.records() if r.get("kind") == kind)


def _final_of(ends: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rec in reversed(ends):
        if rec.get("status") in GRADED:
            return dict(rec)
    return None


def _infra_count(ends: list[dict[str, Any]]) -> int:
    return sum(
        1 for e in ends if e.get("status") == "infra_fail" and e.get("sub") not in NOT_AN_ATTEMPT
    )
