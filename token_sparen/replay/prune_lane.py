"""E1 pruning lane: what opt-in MCP tool pruning would have done over past sessions, in memory.

Past sessions ran without pruning, so every server was available: a session that used, named
or searched for a server the simulation had pruned by then shows a need pruning would have hit.
One lane instance replays one project group in chronological order with the production logic of
``mechanisms.prune`` (spec 6.10) on an **in-memory** store doc: ``observe_session`` folds each
session's ``ToolInventory`` (built record by record with ``transcript.add_inventory_record``,
the same fold ``prune.scan`` runs, from the records the runner reads), ``plan`` / ``ready`` /
``is_candidate`` decide, and at each simulated session end the rules ``plan`` returns are marked
pruned in the doc (the simulated apply, lease 0). Nothing is written: no settings writer, no
git, no store, no file outside the transcripts. Results may only move pruning defaults towards
less intervention.

**Simulation.** The doc starts seeded and empty when the group's first session starts (pruning
"enabled" then; earlier history does not exist for the replay). Thresholds and
``lease_sessions`` come from the effective ``[prune]`` config (a lane ``overlay`` may change
them); ``builtins`` is honoured from it as well. Per session, while a rule is pruned:

- a **need** is a tool call of the pruned server's tools (or a "No such tool available" error
  naming one), a user prompt naming it (``prune.mentions``) or a ToolSearch query naming it,
  each from records that are not copied history (``run_ids``; a resumed session's copies are
  not live events). Production restores at once on a need (regret, kept for good): so does the
  simulation, and the rule is never pruned again;
- at the end of a main session (not a subagent file), in fold order: ``observe_session``,
  restore of rules no longer candidates, the lease tick (a rule pruned for ``lease_sessions``
  sessions is restored and needs fresh evidence), then the apply of ``plan``.

Approximations, all towards **more** simulated pruning (so the measured risk is an upper bound):
the idle gate (``idle_minutes``) is taken as always open, and a subagent file replays after its
main session's end (the runner orders files by first timestamp), so its use of a server pruned
at that end counts as a need, where production would fold it with the main session first.

**Unobservable sessions.** Availability is read only from what production reads: SDK ``init``
records and ``deferred_tools_delta`` attachments. A main session with neither is counted in
``unobservable_sessions`` and accrues no evidence (production would assume the tools other
sessions listed: never guessed here); its needs and lease ticks still count, its listing
savings do not.

**Splits never mix.** Every counter of a split comes from sessions of that split only. A test
session also runs with the state dev sessions built (``history = True``); a need, lease end or
pruned session concerning a rule pruned in dev is reported under ``*_cross_split`` keys of the
test row, never in ``dev`` or in the test-only keys. Per split:

- ``sessions`` (main sessions simulated), ``sessions_failed`` (any file that failed),
  ``subagent_files``, ``unobservable_sessions``;
- ``servers_seen``: distinct MCP servers listed in sessions of the split;
- ``would_prune``: simulated applies (a rule pruned again after a lease counts again);
- ``sessions_pruned``: main sessions that ran with at least one rule pruned;
- ``needed_while_pruned`` (and ``needed_by``: ``tool_call`` / ``user_mention`` /
  ``tool_search``): needs, at most one per rule per prune (the first restores it);
- ``lease_restores``: leases that ended;
- ``listing_tokens_saved``: per pruned session, the estimated tokens of the pruned servers' tool
  names as the session listed them (one listing per session, not per request; MCP tool
  definitions are deferred by the host, so names are all a listing carries).

Aggregates only: no server name, prompt, path or id.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any

from token_sparen.config import Config
from token_sparen.estimate import estimate_tokens
from token_sparen.mechanisms import prune
from token_sparen.replay import runner
from token_sparen.replay.replay import SPLITS
from token_sparen.replay.runner import Event, SessionInfo
from token_sparen.transcript import ToolInventory, add_inventory_record, is_prompt

DEV, TEST = SPLITS
FKEY = "replay"  # the one simulated checkout's record key (never a path)
REASONS = ("tool_call", "tool_search", "user_mention")
FIELDS = (
    "sessions",
    "sessions_failed",
    "subagent_files",
    "unobservable_sessions",
    "servers_seen",
    "would_prune",
    "sessions_pruned",
    "needed_while_pruned",
    "lease_restores",
    "listing_tokens_saved",
)
CROSS_FIELDS = (
    "sessions_pruned_cross_split",
    "needed_while_pruned_cross_split",
    "lease_restores_cross_split",
    "listing_tokens_saved_cross_split",
)


def _part(split: str) -> dict[str, Any]:
    out: dict[str, Any] = dict.fromkeys(FIELDS, 0)
    out["needed_by"] = dict.fromkeys(REASONS, 0)
    if split == TEST:
        out.update(dict.fromkeys(CROSS_FIELDS, 0))
        out["needed_by_cross_split"] = dict.fromkeys(REASONS, 0)
    return out


def _settings(cfg: Config) -> prune.Settings:
    raw = cfg.data.get("prune")
    builtins = raw.get("builtins") if isinstance(raw, Mapping) else None
    return prune.settings(cfg.data, {"prune": {"enabled": True, "builtins": builtins is True}})


def _blocks(rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _prompt_text(rec: Mapping[str, Any]) -> str:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    return "\n".join(
        b["text"] for b in _blocks(rec) if b.get("type") == "text" and isinstance(b["text"], str)
    )


class PruneLane:
    """Runner lane ``"prune"`` (module docstring). Params: none besides the runner's."""

    history = True

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.s = _settings(cfg)
        self.parts = {p: _part(p) for p in SPLITS}
        self.doc: dict[str, Any] = {
            "version": prune.VERSION,
            "enabled": True,
            "seeded": True,
            "tools": {},
            "kept": {},
            "files": {FKEY: {"owned": [], "lease": {}, "created": {}}},
        }
        self.origin: dict[str, str] = {}  # pruned rule -> split of the session that pruned it
        self.seen: dict[str, set[str]] = {p: set() for p in SPLITS}
        self._cur: SessionInfo | None = None
        self._failed = False
        self._inv = ToolInventory()
        self._chain: bool | None = None
        self._start_pruned: list[str] = []
        self._needs: list[tuple[str, str]] = []  # (rule, reason) restored this session

    # ------------------------------------------------------------------ in-memory record
    def _entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = self.doc["files"][FKEY]
        return entry

    def _owned(self) -> list[str]:
        return prune.owned_in(self.doc, FKEY)

    def _unprune(self, rules: list[str]) -> None:
        entry = self._entry()
        entry["owned"] = [r for r in self._owned() if r not in rules]
        for rule in rules:
            entry["lease"].pop(rule, None)

    def _keep(self, rule: str, reason: str) -> None:
        """Production's restore on a need: regret, kept for good."""
        self._unprune([rule])
        self.doc["kept"] = {**self.doc["kept"], rule: reason}

    # ------------------------------------------------------------------ feeding
    def _start(self, info: SessionInfo) -> None:
        self._cur = info
        self._failed = False
        self._inv = ToolInventory()
        self._chain = None
        self._start_pruned = self._owned()
        self._needs = []

    def feed(self, event: Event, session: SessionInfo) -> None:
        """Events carry no tool listing: everything is read from ``feed_record``."""

    def feed_record(self, rec: dict[str, Any], session: SessionInfo) -> None:
        if session is not self._cur:
            self._start(session)
        if self._failed:
            return
        try:
            self._record(rec, session)
        except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
            self._failed = True

    def _record(self, rec: dict[str, Any], session: SessionInfo) -> None:
        kind = rec.get("type")
        if kind in ("user", "assistant") and self._chain is None:
            self._chain = bool(rec.get("isSidechain"))
        before = set(self._inv.unknown)
        add_inventory_record(self._inv, rec)
        if not self._owned():
            return
        ids = session.run_ids
        if kind == "assistant":
            for block in _blocks(rec):
                tid, name = block.get("id"), block.get("name")
                if block.get("type") != "tool_use" or not isinstance(name, str):
                    continue
                if isinstance(tid, str) and ids.has(f"t:{tid}"):
                    continue  # copied history
                self._need(prune.tool_rule(name, self._owned()), "tool_call")
                inp = block.get("input")
                query = inp.get("query") if name == "ToolSearch" and isinstance(inp, dict) else None
                if isinstance(query, str):
                    self._need(prune.mentions(query, self._owned()), "tool_search")
        elif kind == "user":
            if is_prompt(rec):
                uid = rec.get("uuid")
                if not (isinstance(uid, str) and ids.has(f"u:{uid}")):
                    self._need(prune.mentions(_prompt_text(rec), self._owned()), "user_mention")
                return
            fresh = [
                b
                for b in _blocks(rec)
                if b.get("type") == "tool_result"
                and not (isinstance(b.get("tool_use_id"), str) and ids.has(f"t:{b['tool_use_id']}"))
            ]
            if fresh:
                for name in sorted(self._inv.unknown - before):
                    self._need(prune.tool_rule(name, self._owned()), "tool_call")

    def _need(self, rules: list[str], reason: str) -> None:
        for rule in rules:
            self._keep(rule, reason)
            self._needs.append((rule, reason))

    # ------------------------------------------------------------------ session end
    def end_session(self, session: SessionInfo) -> None:
        if session is not self._cur:
            return  # no record at all: not a session
        part = self.parts[session.split]
        try:
            if self._failed:
                raise RuntimeError
            if self._chain is True:
                part["subagent_files"] += 1
                self._credit(session.split)
                self._fold(self._inv)
            else:
                self._main(session.split)
                part["sessions"] += 1
        except Exception:  # noqa: BLE001 - counted, never described
            part["sessions_failed"] += 1
        self._cur = None
        self._inv = ToolInventory()

    def abort_session(self, session: SessionInfo) -> None:
        """The runner failed mid-session: count it failed."""
        if session is self._cur:
            self._failed = True
            self.end_session(session)

    def _cross(self, split: str, rule: str) -> bool:
        return split == TEST and self.origin.get(rule) == DEV

    def _credit(self, split: str) -> None:
        part = self.parts[split]
        for rule, reason in self._needs:
            suffix = "_cross_split" if self._cross(split, rule) else ""
            part["needed_while_pruned" + suffix] += 1
            part["needed_by" + suffix][reason] += 1

    def _fold(self, inv: ToolInventory) -> None:
        """``observe_session`` and the restores the fold does before the lease tick."""
        attempted = prune.observe_session(self.doc, inv, self.s, FKEY)
        for rule in attempted:  # left over: copies of uses seen before (kept, not a need)
            self._keep(rule, "attempted use")
        self._unprune([r for r in self._owned() if not prune.is_candidate(r, self.doc, self.s)])

    def _main(self, split: str) -> None:
        part = self.parts[split]
        inv = self._inv
        listed = inv.listed
        if listed is None:
            part["unobservable_sessions"] += 1
        else:
            servers = {r for r in prune.candidate_names(listed) if r.startswith("mcp__")}
            part["servers_seen"] += len(servers - self.seen[split])
            self.seen[split] |= servers
        same = [r for r in self._start_pruned if not self._cross(split, r)]
        cross = [r for r in self._start_pruned if self._cross(split, r)]
        part["sessions_pruned"] += bool(same)
        if cross:
            part["sessions_pruned_cross_split"] += 1
        if listed is not None:
            for rule in self._start_pruned:
                names = sorted(n for n in listed if prune.tool_rule(n, [rule]))
                key = "listing_tokens_saved" + ("_cross_split" if rule in cross else "")
                part[key] += estimate_tokens("\n".join(names)) if names else 0
        self._credit(split)
        self._fold(inv if listed is not None else dataclasses.replace(inv, prompts=0))
        self._tick(split)
        self._apply(split)

    def _tick(self, split: str) -> None:
        """One more session under each lease; an ended lease restores (no regret)."""
        lease = self._entry()["lease"]
        due: list[str] = []
        for rule in self._owned():
            lease[rule] = int(lease.get(rule, 0)) + 1
            if lease[rule] >= self.s.lease:
                due.append(rule)
        part = self.parts[split]
        for rule in due:
            part["lease_restores" + ("_cross_split" if self._cross(split, rule) else "")] += 1
        self._unprune(due)
        for rule in due:
            row = self.doc["tools"].get(rule)
            if isinstance(row, dict):
                row["fresh"] = {"s": 0, "p": 0}

    def _apply(self, split: str) -> None:
        """The simulated apply: ``plan`` marks rules pruned in memory only."""
        todo = prune.plan(self.doc, self.s, FKEY)
        entry = self._entry()
        for rule in todo:
            entry["owned"] = [*self._owned(), rule]
            entry["lease"][rule] = 0
            row = self.doc["tools"].get(rule)
            if isinstance(row, dict):
                row.pop("fresh", None)
            self.origin[rule] = split
        self.parts[split]["would_prune"] += len(todo)

    def report(self) -> dict[str, Any]:
        return copy.deepcopy(self.parts)


def _factory(cfg: Config, params: dict[str, Any]) -> runner.Lane:
    return PruneLane(cfg, params)


runner.LANES["prune"] = _factory
