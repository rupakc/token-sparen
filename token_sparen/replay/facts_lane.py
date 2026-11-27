"""E1 facts lane: how often each repo-fact kind would be mined, contradicted and needed.

Past sessions never had fact injections, so nothing here simulates activation or the effect of
an injected fact; the lane measures the raw material from transcripts alone
(``miners`` / ``lifecycle.Observer`` in ``evidence="transcript"`` mode: no file of the
project is read and no process runs, see ``miners``). One lane instance replays one project
group in chronological order; copied history of a resumed session (``duplicate`` results) is
neither mined nor observed again.

**Splits never mix.** Every counter of a split comes from sessions of that split only, so a
split's row is the same whether or not the run also covers the other split. A dev session
sees only facts mined in dev sessions; a test session also sees the facts mined in dev
(``history = True``: production would have learned them), and what it observes about them is
reported under separate ``*_cross_split`` keys of the test row, never in ``dev`` or in the
test-only keys. Per session, the visible facts mined in *earlier* sessions are observed by the
production ``lifecycle.Observer``; then the session is mined with the production miners (same
thresholds), in the order the fold uses. Per split and fact kind (``kinds``):

- ``candidates``: distinct facts first mined in that split (test: not mined in dev before);
  ``eligible``: those mined in ``miners.MIN_SESSIONS`` sessions of that split;
- ``contradicted_later``: candidates of the split that a later session of the same split
  contradicted (the Observer's ``contradicted``, which production counts only for an injected
  fact), once per candidate: the wrong-fact risk of mining that kind. Test row only:
  ``contradicted_cross_split``, dev candidates contradicted by a test session.
  ``contradiction_observable`` is false for ``generated_dir``: proving a directory hand-written
  needs git, so its ``contradicted_later`` is 0 by construction, not by evidence;
- ``waste_events``: (fact, later session) pairs where calls showed the waste the fact targets
  (``Observer.waste_calls``: not-found of an alias's subject before its spelling worked, a
  wrong test-runner spelling or runner (never red runs of the right one), an error naming a
  required variable, failures a build step fixed, edits a build regenerated);
  ``waste_tokens``: an **upper bound** of what a fact of the kind could avoid: the estimated
  tokens (input + output) of those calls, each call once per kind. One call can show the waste
  of two kinds (a not-found ``pytest`` for both an alias and a test command): never sum
  ``waste_tokens`` across kinds. ``waste_unmeasured``: generated_dir facts whose directory was
  edited in a session without a regenerating build (waste not measurable, not counted).
  Test row only: ``waste_events_cross_split`` / ``waste_tokens_cross_split`` for facts mined
  in dev;
- ``render_tokens``: the estimated tokens each candidate adds to the SessionStart block
  (``inject.build`` of the fact alone, without the token budget, minus the block's frame);
- ``skipped_needs_fs``: command aliases through a downloading launcher not proposed because
  they need the project's manifest (``command_alias`` only).

Plus ``sessions`` and ``sessions_failed`` per split. Aggregates only: no fact text, path or id.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from token_sparen.config import Config
from token_sparen.estimate import estimate_tokens
from token_sparen.facts import inject, lifecycle, miners
from token_sparen.facts.sanitize import KINDS, Fact, fact_id
from token_sparen.replay import runner
from token_sparen.replay.replay import SPLITS
from token_sparen.replay.runner import Event, SessionInfo
from token_sparen.transcript import ToolResultRecord

EVIDENCE: miners.Evidence = "transcript"
KIND_FIELDS = (
    "candidates",
    "eligible",
    "contradicted_later",
    "waste_events",
    "waste_tokens",
    "waste_unmeasured",
    "render_tokens",
    "skipped_needs_fs",
)
CROSS_FIELDS = ("contradicted_cross_split", "waste_events_cross_split", "waste_tokens_cross_split")
UNOBSERVABLE_CONTRADICTION = frozenset({"generated_dir"})  # needs git (transcript mode)
RENDER_BUDGET = 1_000_000  # render_tokens: a fact's line is costed even over [facts] max_tokens
FRAME_TOKENS = estimate_tokens("\n".join([inject.OPEN, inject.DATA_LINE, inject.CLOSE]))
DEV, TEST = SPLITS


def call_tokens(call: ToolResultRecord) -> int:
    """Estimated tokens of one tool call: its input (JSON) and its result text."""
    inp = json.dumps(call.input, sort_keys=True, ensure_ascii=False, default=str)
    return estimate_tokens(inp) + estimate_tokens(call.text)


def _part(split: str) -> dict[str, Any]:
    fields = KIND_FIELDS + (CROSS_FIELDS if split == TEST else ())
    kinds: dict[str, Any] = {}
    for k in sorted(KINDS):
        row: dict[str, Any] = dict.fromkeys(fields, 0)
        row["contradiction_observable"] = k not in UNOBSERVABLE_CONTRADICTION
        kinds[k] = row
    return {
        "sessions": 0,
        "sessions_failed": 0,
        "waste_tokens_upper_bound": True,
        "kinds": kinds,
    }


@dataclass
class _Known:
    fact: Fact
    mined: dict[str, int] = field(default_factory=dict)  # split -> sessions that mined it
    last: dict[str, int] = field(default_factory=dict)  # split -> last mining session index
    candidate: set[str] = field(default_factory=set)  # splits it was counted a candidate in
    contradicted: set[tuple[str, str]] = field(default_factory=set)  # (split, key) credited

    def home(self) -> str:
        """The split whose candidate a test session's observation concerns (dev wins)."""
        return DEV if DEV in self.mined else TEST


class FactsLane:
    """Runner lane ``"facts"`` (module docstring). Params: none besides the runner's."""

    history = True

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.settings = lifecycle.settings(cfg.data)
        self.parts = {p: _part(p) for p in SPLITS}
        self.known: dict[str, _Known] = {}
        self._cur: SessionInfo | None = None
        self._failed = False
        self._miner = miners.SessionMiner(None, evidence=EVIDENCE)
        self._observer = lifecycle.Observer({}, None, evidence=EVIDENCE)
        self._tokens: dict[int, int] = {}  # fed call number -> tokens (failed calls, edits)
        self._n = 0

    def _row(self, split: str, kind: str) -> dict[str, Any]:
        row: dict[str, Any] = self.parts[split]["kinds"][kind]
        return row

    def _visible(self, split: str) -> dict[str, Fact]:
        """Facts a session of ``split`` may observe (dev: dev-mined only), most recently mined
        first, at most ``miners.MAX_FACTS``."""
        seen = (DEV,) if split == DEV else SPLITS
        ranked = sorted(
            (
                (-max(k.last[s] for s in seen if s in k.last), fid)
                for fid, k in self.known.items()
                if any(s in k.mined for s in seen)
            ),
        )
        return {fid: self.known[fid].fact for _, fid in ranked[: miners.MAX_FACTS]}

    def _start(self, info: SessionInfo) -> None:
        self._cur = info
        self._failed = False
        self._tokens, self._n = {}, 0
        self._miner = miners.SessionMiner(info.cwd, evidence=EVIDENCE)
        self._observer = lifecycle.Observer(self._visible(info.split), info.cwd, evidence=EVIDENCE)

    def feed(self, event: Event, session: SessionInfo) -> None:
        if session is not self._cur:
            self._start(session)
        if self._failed or not isinstance(event, ToolResultRecord) or event.duplicate:
            return
        try:
            self._miner.feed(event)
            self._observer.feed(event)  # numbers the call self._n, like this lane
            if event.is_error or event.tool_name in miners.EDIT_TOOLS:
                self._tokens[self._n] = call_tokens(event)
            self._n += 1
        except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
            self._failed = True

    def end_session(self, session: SessionInfo) -> None:
        if session is not self._cur:
            return  # no event of its chain: not a session
        part = self.parts[session.split]
        part["sessions"] += 1
        try:
            if self._failed:
                raise RuntimeError
            self._observe(session.split)
            self._mine(session)
        except Exception:  # noqa: BLE001 - counted, never described
            part["sessions_failed"] += 1
        self._cur = None
        self._tokens = {}

    def abort_session(self, session: SessionInfo) -> None:
        """The runner failed mid-session: count it failed."""
        if session is self._cur:
            self._failed = True
            self.end_session(session)

    def _observe(self, split: str) -> None:
        wasted: dict[tuple[str, str], set[int]] = {}  # (kind, suffix) -> call numbers
        waste = self._observer.waste_calls
        unsettled = self._observer.unsettled
        for fid, obs in self._observer.results().items():
            known = self.known[fid]
            kind = known.fact.kind
            cross = split == TEST and known.home() == DEV
            suffix = "_cross_split" if cross else ""
            row = self._row(split, kind)
            if obs.contradicted:
                key = "contradicted_cross_split" if cross else "contradicted_later"
                if (split, key) not in known.contradicted:
                    known.contradicted.add((split, key))
                    row[key] += 1
            if fid in waste:
                row["waste_events" + suffix] += 1
                wasted.setdefault((kind, suffix), set()).update(waste[fid])
            elif fid in unsettled and kind == "generated_dir" and not cross:
                row["waste_unmeasured"] += 1
        for (name, suffix), calls in wasted.items():
            tokens = sum(self._tokens.get(n, 0) for n in calls)
            self._row(split, name)["waste_tokens" + suffix] += tokens

    def _mine(self, session: SessionInfo) -> None:
        split = session.split
        found = self._miner.finish(f"{session.group}:{session.index}")
        self._row(split, "command_alias")["skipped_needs_fs"] += self._miner.skipped_needs_fs
        for fact in found:
            fid = fact_id(fact)
            known = self.known.setdefault(fid, _Known(fact))
            if known.last.get(split) == session.index:
                continue
            new_here = not known.mined.get(split) and (split == DEV or DEV not in known.mined)
            known.mined[split] = known.mined.get(split, 0) + 1
            known.last[split] = session.index
            row = self._row(split, fact.kind)
            if new_here:
                known.candidate.add(split)
                row["candidates"] += 1
                row["render_tokens"] += self._render(fid, fact)
            if split in known.candidate and known.mined[split] == miners.MIN_SESSIONS:
                row["eligible"] += 1

    def _render(self, fid: str, fact: Fact) -> int:
        block, _ = inject.build([(fid, fact)], RENDER_BUDGET, 1)
        return max(0, estimate_tokens(block) - FRAME_TOKENS) if block else 0

    def report(self) -> dict[str, Any]:
        return copy.deepcopy(self.parts)


def _factory(cfg: Config, params: dict[str, Any]) -> runner.Lane:
    return FactsLane(cfg, params)


runner.LANES["facts"] = _factory
