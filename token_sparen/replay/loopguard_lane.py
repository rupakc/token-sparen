"""E1 loop-guard lane: how often error loops happen, how often they end on their own, the most a
perfect nudge could save and how often a trigger would fire on a loop already converging.

Past sessions had no loop-guard redirects, so nothing here simulates one; the lane measures the
loops as they ran. Results may only move loop-guard defaults toward **less** intervention
(M4 Global Constraints).

**As production counts.** A failure is a tool result with ``is_error`` (the transcript's record
of a PostToolUseFailure); its fingerprint is the production ``loopguard.fingerprint`` of the
result text with the tool name and ``loopguard.tool_target`` (Bash: command head; file tools:
the path), so an exit-only failure has none and is never counted. A user interrupt
(``[Request interrupted by user``) is not a call, as production skips it. Counting is per turn
(a user prompt starts one) and per agent chain: every transcript the runner feeds is its own
chain (a subagent transcript replays as its own session). Inline sidechain records of a main
transcript (older transcripts without ``subagents/`` files) are not part of any chain the
runner feeds: those subagents' loops are excluded, not counted. At most
``loopguard.MAX_FINGERPRINTS`` fingerprints are counted per chain and turn (production bounds
them per turn across all agents; a later new one never accumulates). Every non-interrupt tool
call of the chain is one call of the watch window (``loopguard.DEFAULT_WATCH`` calls; the turn
ending first counts as not recurring). Copied history of a resumed session (``duplicate``
results) is skipped.

**The production population.** Production redirects at most once per agent and turn: under an
arm with k, at the first call where an error fingerprint reaches k occurrences or an edit pair
reaches ``max(2, k - 1)`` reversals (``loopguard.reversals``). The lane replays each arm k in
``KS`` (eager 2, standard 3, patient 5) on that rule, and samples the natural break rate exactly
as the live gate's p0: under the patient arm, once per chain and turn, on the first fingerprint
reaching ``loopguard.SAVINGS_MIN_N`` (3) occurrences before any redirect, with the same window
(a redirect delivered while the window is still open discards the observation).

**Splits never mix.** All state is per session, so every counter of a split comes from that
split's sessions only (no ``history``). Per split and tool category of the failing tool
(``translate.tool_category``; ``"*"`` = all categories), for k in ``KS`` as string keys:

- ``loops_reaching[k]``: turns whose arm-k redirect is an error-loop trigger (production's
  population: one per chain and turn);
- ``exposure_tokens[k]``: an **upper bound** of what a perfect redirect at that trigger could
  save: the estimated tokens (input + result, ``facts_lane.call_tokens``) of every call after
  the trigger up to the last occurrence of the loop, i.e. until the fingerprint no longer
  recurs within the window (0 if it does not recur after the trigger);
- ``converging_triggers[k]``: those triggers followed, within the window, by a successful call
  of the same tool and the same non-empty target (Bash: command head; file tools: the path):
  the loop was converging. An **upper bound** of the false-trigger risk: the loop need not have
  stopped after that success. Tools without a target (Grep, WebFetch, MCP tools, ...) are never
  counted converging (a success of the same tool may concern something else);
- ``oscillations_reaching[k]`` (category ``edit``): turns whose arm-k redirect is an edit
  oscillation;
- ``natural_observed`` / ``natural_broken`` / ``natural_break_rate``: the live gate's p0
  sample (above); the rate is recomputed from the counts on every merge (``None`` without
  observations).

Diagnostics outside the production population: ``loops_reaching_all[k]`` ((turn, fingerprint)
pairs with at least k occurrences) and ``natural_observed_all`` / ``natural_broken_all`` /
``natural_break_rate_all`` (every fingerprint at its third occurrence, not recurring within the
window).

Plus ``sessions``, ``sessions_failed`` and ``exposure_upper_bound: true`` per split. Aggregates
only: no fingerprint, command, path or id.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from token_sparen.adapters.claude.translate import tool_category
from token_sparen.config import Config
from token_sparen.mechanisms import loopguard
from token_sparen.replay import runner
from token_sparen.replay.facts_lane import call_tokens
from token_sparen.replay.runner import Event, SessionInfo
from token_sparen.transcript import Prompt, ToolResultRecord

NAME = "loopguard"
KS = (2, 3, 5)
WATCH = loopguard.DEFAULT_WATCH
NATURAL_AT = loopguard.SAVINGS_MIN_N
ALL = "*"
INTERRUPT = "[Request interrupted by user"
PER_K = (
    "loops_reaching",
    "exposure_tokens",
    "converging_triggers",
    "oscillations_reaching",
    "loops_reaching_all",
)
RATES = (("natural_observed", "natural_broken", "natural_break_rate"),) + (
    ("natural_observed_all", "natural_broken_all", "natural_break_rate_all"),
)


@dataclass(frozen=True)
class _Call:
    fp: str | None
    cat: str
    tool: str
    target: str
    ok: bool
    tokens: int
    n: int = 0  # occurrences of ``fp`` in the turn so far (this one included)
    osc: int = 0  # the most reversals of the edited file's pairs so far (edits only)


@dataclass
class _Turn:
    calls: list[_Call] = field(default_factory=list)
    counted: set[str] = field(default_factory=set)  # fingerprints within the production bound
    edits: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)  # file -> edits
    written: dict[str, str] = field(default_factory=dict)  # file -> last Write content hash
    seen: dict[str, int] = field(default_factory=dict)  # fingerprint -> occurrences


def _row() -> dict[str, Any]:
    row: dict[str, Any] = {name: {str(k): 0 for k in KS} for name in PER_K}
    for observed, broken, rate in RATES:
        row.update({observed: 0, broken: 0, rate: None})
    return row


def _part() -> dict[str, Any]:
    return {"sessions": 0, "sessions_failed": 0, "exposure_upper_bound": True, "categories": {}}


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()


def _edits(call: ToolResultRecord) -> list[tuple[str, str]]:
    """``(old, new)`` of a successful Edit / MultiEdit (``loopguard.EDIT_TOOLS`` but Write)."""
    inp = call.input
    edits = inp.get("edits") if call.tool_name == "MultiEdit" else [inp]
    out = []
    for e in edits if isinstance(edits, list) else []:
        if isinstance(e, Mapping):
            old, new = e.get("old_string"), e.get("new_string")
            if isinstance(old, str) and isinstance(new, str):
                out.append((_hash(old), _hash(new)))
    return out


def _rate(row: dict[str, Any]) -> None:
    for observed, broken, rate in RATES:
        seen = row.get(observed)
        if isinstance(seen, int | float) and not isinstance(seen, bool):
            row[rate] = row.get(broken, 0) / seen if seen else None


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """``runner.merge_reports`` for this lane: counts add, rates are recomputed from them."""
    a, b = copy.deepcopy(a), copy.deepcopy(b)
    for doc in (a, b):
        for row in _rows(doc):
            for _, _, rate in RATES:
                row[rate] = None  # never summed
    out: dict[str, Any] = runner._merge(a, b)
    for row in _rows(out):
        _rate(row)
    return out


def _rows(doc: Any) -> list[dict[str, Any]]:
    """Every category row of a report (one split) or of a split-keyed report."""
    if not isinstance(doc, dict):
        return []
    cats = doc.get("categories")
    if isinstance(cats, dict):
        return [r for r in cats.values() if isinstance(r, dict)]
    return [row for v in doc.values() for row in _rows(v)]


def _trigger(calls: Sequence[_Call], k: int) -> int | None:
    """The call of the turn where production's arm with ``k`` redirects (once per agent and
    turn): an error fingerprint at ``k`` occurrences or an edit pair at ``max(2, k - 1)``
    reversals, whichever comes first."""
    need = max(loopguard.MIN_REVERSALS, k - 1)
    for i, c in enumerate(calls):
        if (c.fp is not None and c.n >= k) or c.osc >= need:
            return i
    return None


class LoopguardLane:
    """Runner lane ``"loopguard"`` (module docstring). Params: none besides the runner's."""

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.parts: dict[str, dict[str, Any]] = {}
        self._cur: SessionInfo | None = None
        self._failed = False
        self._turn = _Turn()
        self._acc: dict[str, dict[str, Any]] = {}  # this session's rows, credited at its end

    def _start(self, info: SessionInfo) -> None:
        self._cur = info
        self._failed = False
        self._turn = _Turn()
        self._acc = {}

    def feed(self, event: Event, session: SessionInfo) -> None:
        if session is not self._cur:
            self._start(session)
        if self._failed:
            return
        try:
            if isinstance(event, Prompt):
                self._close_turn()
            elif isinstance(event, ToolResultRecord) and not event.duplicate:
                self._call(event)
        except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
            self._failed = True

    def _call(self, ev: ToolResultRecord) -> None:
        if ev.is_error and ev.text.lstrip().startswith(INTERRUPT):
            return
        turn = self._turn
        target = loopguard.tool_target(ev.tool_name, ev.input)
        fp: str | None = None
        osc = 0
        if ev.is_error:
            fp = loopguard.fingerprint(ev.text, tool=ev.tool_name, target=target)
            if fp is not None and fp not in turn.counted:
                if len(turn.counted) >= loopguard.MAX_FINGERPRINTS:
                    fp = None  # production: never accumulates past the bound
                else:
                    turn.counted.add(fp)
        elif ev.tool_name in loopguard.EDIT_TOOLS:
            osc = self._edit(ev)
        n = 0
        if fp is not None:
            n = turn.seen[fp] = turn.seen.get(fp, 0) + 1
        cat = tool_category(ev.tool_name)
        ok = not ev.is_error
        turn.calls.append(_Call(fp, cat, ev.tool_name, target, ok, call_tokens(ev), n, osc))

    def _edit(self, ev: ToolResultRecord) -> int:
        """Record an edit; the most reversals of the file's pairs so far (``reversals`` over
        the file's edits of the turn; it never decreases, so the first call where it reaches a
        threshold is the call whose own move reached it, as production's per-event count)."""
        path = ev.input.get("file_path")
        if not isinstance(path, str) or not path:
            return 0
        turn = self._turn
        if ev.tool_name == "Write":
            content = ev.input.get("content")
            if not isinstance(content, str):
                return 0
            new = _hash(content)
            moves = [(turn.written.get(path, ""), new)]
            turn.written[path] = new
        else:
            moves = _edits(ev)
        edits = turn.edits.setdefault(path, [])
        edits.extend((path, a, b) for a, b in moves)
        return loopguard.reversals(edits) if moves else 0

    def _rows_for(self, cat: str) -> list[dict[str, Any]]:
        return [self._acc.setdefault(c, _row()) for c in (cat, ALL)]

    def _close_turn(self) -> None:
        turn, self._turn = self._turn, _Turn()
        calls = turn.calls
        seq = [c.fp for c in calls]
        occurrences: dict[str, list[int]] = {}
        for i, fp in enumerate(seq):
            if fp is not None:
                occurrences.setdefault(fp, []).append(i)
        triggers = {k: _trigger(calls, k) for k in KS}
        for k, at in triggers.items():
            if at is not None:
                self._credit(calls, seq, occurrences, k, at)
        self._natural(calls, seq, triggers[max(KS)])
        for occ in occurrences.values():  # diagnostics: every fingerprint
            rows = self._rows_for(calls[occ[0]].cat)
            for k in KS:
                if len(occ) >= k:
                    for row in rows:
                        row["loops_reaching_all"][str(k)] += 1
            if len(occ) >= NATURAL_AT:
                broken = not loopguard.recurs_within(seq, occ[NATURAL_AT - 1], WATCH)
                for row in rows:
                    row["natural_observed_all"] += 1
                    row["natural_broken_all"] += broken

    def _credit(
        self,
        calls: list[_Call],
        seq: list[str | None],
        occurrences: dict[str, list[int]],
        k: int,
        at: int,
    ) -> None:
        """The arm-k redirect of the turn, at call ``at``."""
        key = str(k)
        trig = calls[at]
        if trig.fp is None:  # an edit oscillation
            for row in self._rows_for("edit"):
                row["oscillations_reaching"][key] += 1
            return
        occ = occurrences[trig.fp]
        end = occ.index(at)
        while end + 1 < len(occ) and loopguard.recurs_within(seq, occ[end], WATCH):
            end += 1
        exposure = sum(c.tokens for c in calls[at + 1 : occ[end] + 1])
        converging = bool(trig.target) and any(
            c.ok and c.tool == trig.tool and c.target == trig.target
            for c in calls[at + 1 : at + 1 + WATCH]
        )
        for row in self._rows_for(trig.cat):
            row["loops_reaching"][key] += 1
            row["exposure_tokens"][key] += exposure
            row["converging_triggers"][key] += converging

    def _natural(self, calls: list[_Call], seq: list[str | None], redirect: int | None) -> None:
        """The live gate's p0 sample (patient arm, redirect at call ``redirect``): the first
        fingerprint at ``NATURAL_AT`` occurrences before the redirect, watched for ``WATCH``
        calls; a recurrence is "not broken", the window's end or the turn's "broken", the
        redirect while it is open discards it (production: ``_natural_watch`` runs before the
        call's redirect is decided)."""
        opened = next((i for i, c in enumerate(calls) if c.n >= NATURAL_AT), None)
        if opened is None or (redirect is not None and redirect <= opened):
            return
        fp = seq[opened]
        broken: bool | None = True  # the turn ends first
        for step, j in enumerate(range(opened + 1, len(calls)), 1):
            if seq[j] == fp:
                broken = False
                break
            if step >= WATCH:
                break
            if j == redirect:
                broken = None
                break
        if broken is None:
            return
        for row in self._rows_for(calls[opened].cat):
            row["natural_observed"] += 1
            row["natural_broken"] += broken

    def end_session(self, session: SessionInfo) -> None:
        if session is not self._cur:
            return  # no event of its chain: not a session
        part = self.parts.setdefault(session.split, _part())
        part["sessions"] += 1
        try:
            if self._failed:
                raise RuntimeError
            self._close_turn()
            cats = part["categories"]
            for cat, row in self._acc.items():
                cats[cat] = runner._merge(cats[cat], row) if cat in cats else row
        except Exception:  # noqa: BLE001 - counted, never described
            part["sessions_failed"] += 1
        self._cur = None
        self._acc = {}
        self._turn = _Turn()

    def abort_session(self, session: SessionInfo) -> None:
        """The runner failed mid-session: count it failed."""
        if session is self._cur:
            self._failed = True
            self.end_session(session)

    def report(self) -> dict[str, Any]:
        out = copy.deepcopy(self.parts)
        for row in _rows(out):
            _rate(row)
        return out


def _factory(cfg: Config, params: dict[str, Any]) -> runner.Lane:
    return LoopguardLane(cfg, params)


runner.LANES[NAME] = _factory
runner.MERGERS[NAME] = merge
