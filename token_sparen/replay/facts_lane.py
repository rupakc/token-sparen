"""E1 facts lane: how often each repo-fact kind would be mined, contradicted and needed.

Past sessions never had fact injections, so nothing here simulates activation or the effect of
an injected fact; the lane measures the raw material from transcripts alone
(``miners`` / ``lifecycle.Observer`` in ``evidence="transcript"`` mode: no file of the
project is read and no process runs, see ``miners``). One lane instance replays one project
group in chronological order (``history = True``: with ``split="test"`` the dev sessions are
mined too, as production would have, but only the test split is reported).

Per session, the facts mined in *earlier* sessions of the group are observed by the production
``lifecycle.Observer``; then the session is mined with the production miners (same thresholds),
in the same order the fold uses. Copied history of a resumed session (``duplicate`` results) is
neither mined nor observed again. Per split and fact kind (``kinds``):

- ``candidates``: distinct facts first mined in a session of that split; ``eligible``: those
  that reached ``miners.MIN_SESSIONS`` sessions of evidence (counted in the mining split);
- ``contradicted_later``: candidates that a later session contradicted (the Observer's
  ``contradicted``, whether or not production would have counted it: it only counts it for an
  injected fact). Counted once per candidate, in the split where it was mined: the wrong-fact
  risk of mining that kind;
- ``waste_events``: (fact, later session) pairs where the Observer saw the waste the fact
  targets (``waste`` or ``waste_event``), in the split of that session;
- ``waste_tokens``: an **upper bound** of the tokens a fact of the kind could have avoided: the
  estimated tokens (input + output) of the failed calls (and, for ``generated_dir``, the edits)
  that bore on a wasted fact (``Observer.hits``) in those sessions, each call once per kind.
  The split flag ``waste_tokens_upper_bound`` marks it;
- ``render_tokens``: the estimated tokens each candidate adds to the SessionStart block
  (``inject.build`` of the fact alone, minus the block's fixed frame);
- ``skipped_needs_fs``: command aliases through a downloading launcher not proposed because
  they need the project's manifest (``command_alias`` only).

Plus ``sessions`` and ``sessions_failed`` per split. Aggregates only: no fact text, path or id.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
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
    "render_tokens",
    "skipped_needs_fs",
)
MAX_HITS = 2000  # attributed calls kept per fact and session
FRAME_TOKENS = estimate_tokens("\n".join([inject.OPEN, inject.DATA_LINE, inject.CLOSE]))


def call_tokens(call: ToolResultRecord) -> int:
    """Estimated tokens of one tool call: its input (JSON) and its result text."""
    inp = json.dumps(call.input, sort_keys=True, ensure_ascii=False, default=str)
    return estimate_tokens(inp) + estimate_tokens(call.text)


def _part() -> dict[str, Any]:
    return {
        "sessions": 0,
        "sessions_failed": 0,
        "waste_tokens_upper_bound": True,
        "kinds": {k: dict.fromkeys(KIND_FIELDS, 0) for k in sorted(KINDS)},
    }


@dataclass
class _Known:
    fact: Fact
    split: str  # where it was first mined
    sessions: int = 1  # sessions of evidence (counted up to MIN_SESSIONS)
    last: int = 0  # index of the last session that mined it
    contradicted: bool = False


class FactsLane:
    """Runner lane ``"facts"`` (module docstring). Params: none besides the runner's."""

    history = True

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.settings = lifecycle.settings(cfg.data)
        self.parts = {p: _part() for p in SPLITS}
        self.known: dict[str, _Known] = {}
        self._cur: SessionInfo | None = None
        self._failed = False
        self._miner = miners.SessionMiner(None, evidence=EVIDENCE)
        self._observer = lifecycle.Observer({}, None, evidence=EVIDENCE)
        self._tokens: dict[int, int] = {}  # attributed call number -> tokens
        self._hits: dict[str, list[int]] = {}  # fact id -> attributed call numbers
        self._n = 0

    def _row(self, split: str, kind: str) -> dict[str, int]:
        row: dict[str, int] = self.parts[split]["kinds"][kind]
        return row

    def _start(self, info: SessionInfo) -> None:
        self._cur = info
        self._failed = False
        self._tokens, self._hits, self._n = {}, {}, 0
        self._miner = miners.SessionMiner(info.cwd, evidence=EVIDENCE)
        recent = sorted(self.known.items(), key=lambda kv: (-kv[1].last, kv[0]))
        watched = {fid: k.fact for fid, k in recent[: miners.MAX_FACTS]}
        self._observer = lifecycle.Observer(watched, info.cwd, evidence=EVIDENCE)

    def feed(self, event: Event, session: SessionInfo) -> None:
        if session is not self._cur:
            self._start(session)
        if self._failed or not isinstance(event, ToolResultRecord) or event.duplicate:
            return
        try:
            self._miner.feed(event)
            self._observer.feed(event)
            hits = self._observer.hits
            if hits and (event.is_error or event.tool_name in miners.EDIT_TOOLS):
                self._tokens[self._n] = call_tokens(event)
                for fid in hits:
                    calls = self._hits.setdefault(fid, [])
                    if len(calls) < MAX_HITS:
                        calls.append(self._n)
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
        self._tokens, self._hits = {}, {}

    def abort_session(self, session: SessionInfo) -> None:
        """The runner failed mid-session: count it failed."""
        if session is self._cur:
            self._failed = True
            self.end_session(session)

    def _observe(self, split: str) -> None:
        wasted: dict[str, set[int]] = {}
        for fid, obs in self._observer.results().items():
            known = self.known[fid]
            kind = known.fact.kind
            if obs.contradicted and not known.contradicted:
                known.contradicted = True
                self._row(known.split, kind)["contradicted_later"] += 1
            if obs.waste is True or obs.waste_event:
                self._row(split, kind)["waste_events"] += 1
                wasted.setdefault(kind, set()).update(self._hits.get(fid, ()))
        for name, calls in wasted.items():
            self._row(split, name)["waste_tokens"] += sum(self._tokens[n] for n in calls)

    def _mine(self, session: SessionInfo) -> None:
        split = session.split
        found = self._miner.finish(f"{session.group}:{session.index}")
        self._row(split, "command_alias")["skipped_needs_fs"] += self._miner.skipped_needs_fs
        for fact in found:
            fid = fact_id(fact)
            known = self.known.get(fid)
            if known is None:
                known = self.known[fid] = _Known(fact, split, last=session.index)
                self._row(split, fact.kind)["candidates"] += 1
                self._row(split, fact.kind)["render_tokens"] += self._render(fid, fact)
            elif known.last != session.index:
                known.last = session.index
                known.sessions += 1
                if known.sessions == miners.MIN_SESSIONS:
                    self._row(known.split, fact.kind)["eligible"] += 1

    def _render(self, fid: str, fact: Fact) -> int:
        block, _ = inject.build([(fid, fact)], self.settings.max_tokens, 1)
        return max(0, estimate_tokens(block) - FRAME_TOKENS) if block else 0

    def report(self) -> dict[str, Any]:
        return copy.deepcopy(self.parts)


def _factory(cfg: Config, params: dict[str, Any]) -> runner.Lane:
    return FactsLane(cfg, params)


runner.LANES["facts"] = _factory
