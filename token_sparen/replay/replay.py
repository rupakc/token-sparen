"""E1: counterfactual replay of Claude Code transcripts through the production compression path
(spec section 10.1). Private by construction: only aggregates leave this module.

For every tool result of a session's own chain (``transcript.iter_events``) the result is gated
exactly like the PostToolUse hook (tool deny-set, recovery reads, Claude Code's replaceable
shapes, failed results, the 2000-token floor), its content type detected, an arm chosen by the
policy under test (a ``StaticPolicy`` arm, or a frozen ``Engine`` snapshot: side-effect-free
``Engine.choose``, no pending entries, no rewards, no store writes) and compressed with
``compression.pick_candidate`` (the hook's never-larger rule). Nothing is saved.

**Units.** Tokens are estimates (``estimate_tokens``, chars per token by content type), the
same as the hook books; the baseline is the API's reported usage. Cache TTL is ignored: every
later call is assumed to read the cache, so a removed token is priced as a cheap cache read rather
than a re-write after an expiry (conservative).

**Projected re-sends.** A removed token is not sent again on every later API call of the session
until the next compaction (``transcript.is_compaction``: a ``compact_boundary`` system record or
an ``isCompactSummary`` user record). ``tokens_resent_removed`` = sum over trims of removed x
later calls (distinct message ids, counted once per run: a resumed session copies its history,
and copied tool results are not replayed again, ``duplicate_results``) before the next
compaction. Cost-weighted (``estimate.Prices``): the first later call writes the removed tokens
to the prompt cache (``cache_write``), every further call reads them (``cache_read``); a result
followed by no call saves nothing. ``baseline_context_cost`` prices the context of every call the
same way (input, cache write, cache read; output excluded), so the saved share is comparable.

**Proxy regret** (aligned with the live detector where cheap). Per trim, up to 64 distinctive
tokens of its elided span (``regret.distinctive_tokens``: 8+ characters with a letter, absent
from the kept text and the tool input), sampled *after* dropping tokens already in the session's
context (prompts, assistant text, tool inputs, visible result text; first ``SCAN_CHARS`` each, up
to ``MAX_SEEN`` digests) and common English words (``STOPWORDS``). A path-like token also
matches by its basename (one token either way). A token that later reappears in a prompt or a
tool result stops counting (the agent has it from elsewhere). Within ``[learn]
regret_window_turns`` user turns: a later assistant text block or tool input (edits included:
Read -> Edit of text only the elided part held) with >= 2 such tokens, or a re-run of the same
``regret.signature`` (normalised command, same file read), is a full regret (1.0); a user prompt
with >= 2 is partial (0.5, the span stays watched). A trim keeps its maximum; partials are paid
at expiry, eviction or the session end. At most ``MAX_WATCHED`` spans are watched (the oldest is
evicted and paid, counted in ``evicted``). ``proxy_regrets`` is the sum of values.

**Memory.** One record at a time; per session only counters, the last prompt (<= 2000 chars),
recent tool categories and paths, and the watched spans' digests.

**Split.** A session's date is its first record ``timestamp`` (else the file's mtime); dates
before ``date_cutoff`` are ``dev``, the rest ``test``.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
import uuid
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from token_sparen.adapters.claude import translate
from token_sparen.compress import CompressContext, detect, load_level, registry
from token_sparen.config import Config, load_config, redacted_logs, state_home
from token_sparen.estimate import Prices, estimate_tokens
from token_sparen.events import Event, SessionCtx, ToolCall
from token_sparen.learn import features, regret
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, Decision, DecisionPoint, Policy, StaticPolicy
from token_sparen.mechanisms import compression, governor
from token_sparen.store import paths as store_paths
from token_sparen.transcript import (
    AssistantRecord,
    Compaction,
    Prompt,
    ToolResultRecord,
    first_field,
    iter_events,
)

Split = Literal["dev", "test", "all"]
DEFAULT_CUTOFF = date(2026, 11, 1)
MAX_WATCHED = 200
PROMPT_CHARS = 2000
RECENT_PATHS = 20
RECENT_CATS = 10
MAX_ENGINES = 64
MAX_SEEN = 500_000  # token digests already in the session's context (no more added beyond)
MAX_RUN_IDS = 5_000_000  # message / tool-use id digests per run (beyond: no more de-duplication)
# Common English words of 8+ letters: never distinctive enough to show a lookup.
STOPWORDS = frozenset(
    """
    configuration successfully initialized initializing information available following
    required function functions parameter parameters returned returning response requests
    completed complete different directory directories documentation environment everything
    implementation important including instance language location messages modified necessary
    original optional possible previous processing properties property received security
    specified standard statement template together variable variables warnings whatever
    although continue attribute attributes generated internal external settings finished
    starting updating undefined deprecated exception exceptions currently actually
    """.split()
)
_PATH_TOOLS = frozenset({"read", "edit"})
_NULL_OVERLAY = Path(os.devnull)
SPLITS: tuple[str, ...] = ("dev", "test")


# --------------------------------------------------------------------------- policies


class FrozenEngine:
    """A learned policy replayed as a snapshot: decisions come from ``Engine.choose`` (no
    pending entries, no rewards, nothing written); one engine per project key."""

    def __init__(self, cfg: Config, home: Path, seed: int = 0) -> None:
        self.cfg = cfg
        self.home = home
        self.seed = seed
        self._engines: dict[str, Engine] = {}

    def engine(self, project_key: str) -> Engine:
        eng = self._engines.get(project_key)
        if eng is None:
            if len(self._engines) >= MAX_ENGINES:
                self._engines.clear()
            stores = StoreChain(project_key, self.home, None, params=self.cfg.data)
            rng = random.Random(f"{self.seed}|{project_key}")
            eng = self._engines[project_key] = Engine(stores, self.cfg, rng)
        return eng

    def decide(
        self, point: DecisionPoint, feats: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        eng = self.engine(session.project_key)
        if not eng.enabled():
            return StaticPolicy({}).decide(point, feats, session)
        arm, p = eng.choose(point, feats)
        return Decision(uuid.uuid4().hex, point.name, arm, p, eng.bucket(point, feats))


def resolve_policy(policy: Policy | str, cfg: Config, seed: int = 0) -> tuple[str, Policy]:
    """``(name, policy)``: ``static`` (the fixed defaults), ``engine`` (frozen snapshot of the
    local learner state) or a compression arm name; a ``Policy`` object is used as given (an
    ``Engine`` is frozen)."""
    if isinstance(policy, Engine):
        frozen = FrozenEngine(policy.cfg, policy.stores.home, seed)
        frozen._engines[policy.stores.project_key] = policy
        return "engine", frozen
    if not isinstance(policy, str):
        return type(policy).__name__, policy
    if policy == "static":
        return policy, StaticPolicy({})
    if policy == "engine":
        return policy, FrozenEngine(cfg, state_home(os.environ), seed)
    if policy in COMPRESSION.arms:
        return policy, StaticPolicy({COMPRESSION.name: policy})
    raise ValueError(f"unknown policy {policy!r}: static, engine or one of {COMPRESSION.arms}")


# --------------------------------------------------------------------------- accounting

_CT_FIELDS = (
    "eligible",
    "trimmed",
    "tokens_before",
    "tokens_removed",
    "tokens_resent_removed",
    "projected_cost_weighted_saved",
    "proxy_regrets",
    "proxy_regret_full",
    "proxy_regret_partial",
    "watched",
)


@dataclass
class _Acc:
    """Aggregates of a set of sessions (counts and sums only)."""

    sessions: int = 0
    sessions_failed: int = 0
    calls: int = 0
    tool_results: int = 0
    duplicate_results: int = 0
    ids_capped: bool = False
    evicted: int = 0
    baseline_context_cost: float = 0.0
    arms: dict[str, int] = field(default_factory=dict)
    per_ctype: dict[str, dict[str, float]] = field(default_factory=dict)
    per_tool: dict[str, dict[str, float]] = field(default_factory=dict)  # "<category>/<ctype>"
    signals: dict[str, int] = field(default_factory=dict)
    regret_turns: dict[str, int] = field(default_factory=dict)

    def add(self, ctype: str, key: str, value: float, cat: str = "other") -> None:
        for table, name in ((self.per_ctype, ctype), (self.per_tool, f"{cat}/{ctype}")):
            row = table.get(name)
            if row is None:
                row = table[name] = dict.fromkeys(_CT_FIELDS, 0.0)
            row[key] += value

    def count(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    def total(self, key: str) -> float:
        return sum(row[key] for row in self.per_ctype.values())

    def to_dict(self) -> dict[str, Any]:
        trimmed = self.total("trimmed")
        regrets = self.total("proxy_regrets")
        saved = self.total("projected_cost_weighted_saved")
        out: dict[str, Any] = {
            "sessions": self.sessions,
            "sessions_failed": self.sessions_failed,
            "calls": self.calls,
            "tool_results": self.tool_results,
            "duplicate_results": self.duplicate_results,
            "ids_capped": self.ids_capped,
            "eligible": int(self.total("eligible")),
            "trimmed": int(trimmed),
            "tokens_before_eligible": int(self.total("tokens_before")),
            "tokens_removed": int(self.total("tokens_removed")),
            "tokens_resent_removed": int(self.total("tokens_resent_removed")),
            "projected_cost_weighted_saved": round(saved, 3),
            "baseline_context_cost": round(self.baseline_context_cost, 3),
            "saved_share_of_context_cost": _ratio(saved, self.baseline_context_cost),
            "proxy_regrets": round(regrets, 1),
            "proxy_regret_full": int(self.total("proxy_regret_full")),
            "proxy_regret_partial": int(self.total("proxy_regret_partial")),
            "proxy_regret_rate": _ratio(regrets, trimmed),
            "watched_spans": int(self.total("watched")),
            "evicted": self.evicted,
            "arms": dict(sorted(self.arms.items())),
        }
        out["per_ctype"] = {ct: _ct_row(row) for ct, row in sorted(self.per_ctype.items())}
        out["per_tool"] = {k: _ct_row(row) for k, row in sorted(self.per_tool.items())}
        out["regret_signals"] = dict(sorted(self.signals.items()))
        out["regret_turns"] = {b: self.regret_turns.get(b, 0) for b in TURN_BUCKETS}
        return out


_FLOAT_FIELDS = frozenset({"projected_cost_weighted_saved", "proxy_regrets"})
TURN_BUCKETS = ("0", "1", "2", "3-6", "7+")
SIGNALS = (
    "assistant_text",
    "tool_input",
    "edit_input",
    "user_prompt",
    "rerun",
    "path_mention",
)


def _turn_bucket(lag: int) -> str:
    if lag <= 2:
        return str(max(0, lag))
    return "3-6" if lag <= 6 else "7+"


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den > 0 else 0.0


def _ct_row(row: Mapping[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {
        k: (round(v, 3) if k in _FLOAT_FIELDS else int(v)) for k, v in row.items()
    }
    out["proxy_regret_rate"] = _ratio(row["proxy_regrets"], row["trimmed"])
    return out


@dataclass(frozen=True)
class ReplayReport:
    """E1 aggregates; ``to_dict`` is the private report body."""

    policy: str
    split: str
    cutoff: date
    totals: dict[str, Any]
    by_split: dict[str, dict[str, Any]]
    by_arm: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        totals = self.__dict__.get("totals", {})
        if name in totals:
            return totals[name]
        raise AttributeError(name)

    @property
    def projected_cost_weighted_saved(self) -> float:
        return float(self.totals["projected_cost_weighted_saved"])

    @property
    def per_ctype(self) -> dict[str, Any]:
        return dict(self.totals["per_ctype"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "e1-offline-replay",
            "policy": self.policy,
            "split": self.split,
            "cutoff": self.cutoff.isoformat(),
            "generated": datetime.now(UTC).date().isoformat(),
            "heuristics": HEURISTICS,
            "totals": self.totals,
            "by_split": self.by_split,
            **({"by_arm": self.by_arm} if self.by_arm else {}),
        }


HEURISTICS = {
    "units": "tokens are estimates (chars per token by content type, as the hook books them); "
    "baseline_context_cost comes from the API usage the transcripts record",
    "cache_ttl": "cache TTL ignored: every later call is priced as a cache read of the removed "
    "tokens (no re-write after expiry), so savings are conservative",
    "resends": "later API calls (distinct message ids, own chain) before the next compaction "
    "(compact_boundary system record or isCompactSummary user record)",
    "cost": "first later call prices removed tokens as cache_write, each further call as "
    "cache_read (estimate.Prices); baseline prices every call's context the same way",
    "proxy_regret": "full (1.0): >= 2 distinctive tokens found only in the elided span (not in "
    "the context before the trim, not in a later prompt or tool result; common words dropped; a "
    "path also matches by basename) in one later assistant text block or tool input, or a re-run "
    "of the same signature; partial (0.5): >= 2 in a user prompt; within regret_window_turns "
    "user turns; sum of values",
    "dedupe": "message and tool-use ids counted once per run (resumed sessions copy history)",
    "breakdowns": "per_tool keys are <tool category>/<content type> (categories: shell = Bash, "
    "read, search, web, agent, mcp, other); regret_signals counts the signal that settled each "
    "regretted trim (path_mention: a matched token was a path or its basename, overlaps the "
    "others); regret_turns: user turns from the trim to that signal; by_arm: every arm of a "
    "comma-separated --policy replayed in the same pass (totals and by_split are the first arm)",
    "gates": "PostToolUse gates: skip_tools/edit category, recovery reads, Claude Code "
    "replaceable shapes, failed results, 2000-token floor; compression.pick_candidate",
}


# --------------------------------------------------------------------------- sessions


def iter_transcripts(paths: Iterable[Path]) -> Iterator[Path]:
    """Files as given; directories recursively (``*.jsonl``, sorted)."""
    for p in paths:
        p = Path(os.path.expanduser(str(p)))
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*.jsonl") if f.is_file())
        elif p.is_file():
            yield p


def session_date(path: Path) -> date:
    ts = first_field(path, "timestamp")
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).date()
    except OSError:
        return date.min


class RunIds:
    """Ids seen in this run (8-byte digests): message ids (``m:``) and tool-use ids (``t:``)."""

    def __init__(self) -> None:
        self._ids: set[bytes] = set()
        self.cap = MAX_RUN_IDS
        self.capped = False

    def add(self, key: str) -> bool:
        """Record ``key``; True if it was not seen before in this run."""
        d = hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=8).digest()
        if d in self._ids:
            return False
        if len(self._ids) >= self.cap:
            self.capped = True  # stop growing: later duplicates are counted again
            return True
        self._ids.add(d)
        return True


_SEP = re.compile(r"[/\\]")


def _variants(token: str) -> Iterator[str]:
    """A token, plus its basename when it is path-like (and itself distinctive)."""
    yield token
    base = _SEP.split(token)[-1]
    if base != token and len(base) >= 8 and any(c.isalpha() for c in base):
        yield base


def _digests(text: str, limit: int) -> set[str]:
    """Digests of the distinctive tokens (and path basenames) of ``text[:limit]``."""
    return {
        regret.token_digest(v) for t in set(regret.iter_tokens(text[:limit])) for v in _variants(t)
    }


@dataclass
class _Span:
    digests: dict[str, int]  # digest -> token index (a path and its basename are one token)
    ctype: str
    turn: int
    sig: str  # digest of the producing call's re-run signature
    cat: str = "other"
    paths: frozenset[int] = frozenset()  # indices of path-like tokens
    best: float = 0.0
    signal: str = ""
    lag: int = 0
    path_hit: bool = False

    def matched(self, digests: set[str]) -> set[int]:
        return {i for d, i in self.digests.items() if d in digests}

    def hits(self, digests: set[str]) -> int:
        return len(self.matched(digests))

    def mark(self, value: float, signal: str, lag: int, matched: set[int]) -> None:
        if value > self.best:
            self.best, self.signal, self.lag = value, signal, lag
            self.path_hit = bool(matched & self.paths)

    def forget(self, digests: set[str]) -> None:
        if any(d in digests for d in self.digests):
            self.digests = {d: i for d, i in self.digests.items() if d not in digests}


class _Session:
    """Replays one transcript into the accumulators (totals and its split)."""

    def __init__(
        self,
        accs: Sequence[_Acc],
        policy: Policy,
        cfg: Config,
        prices: Prices,
        window: int,
        run_ids: RunIds | None = None,
        cwd: str | None = None,
    ) -> None:
        self.run_ids = run_ids
        self.cwd = cwd
        self.accs = accs
        self.policy = policy
        self.cfg = cfg
        self.prices = prices
        self.window = window
        self.min_tokens = compression.min_tokens(cfg)
        self.turn = 0
        self.prompt = ""
        self.cats: deque[str] = deque(maxlen=RECENT_CATS)
        self.paths: deque[str] = deque(maxlen=RECENT_PATHS)
        # removed tokens already sent once / not sent yet, per (category, ctype)
        self.sent: dict[tuple[str, str], int] = {}
        self.awaiting: dict[tuple[str, str], int] = {}
        self.watched: deque[_Span] = deque()
        self.seen: set[str] = set()
        self.ctx_tokens: int | None = None
        self.model: str | None = None
        self.project_dir = Path(os.devnull).parent
        self.project_key = "replay"
        self._keys: dict[str, str] = {}

    def _add(self, ctype: str, key: str, value: float, cat: str = "other") -> None:
        for acc in self.accs:
            acc.add(ctype, key, value, cat)

    # events --------------------------------------------------------------------------------

    def on_call(self, rec: AssistantRecord) -> None:
        for acc in self.accs:
            acc.calls += 1
        if rec.usage is not None:
            u = rec.usage
            self.ctx_tokens = u.input + u.cache_read + u.cache_write
            cost = (
                u.input * self.prices.input
                + u.cache_write * self.prices.cache_write
                + u.cache_read * self.prices.cache_read
            )
            for acc in self.accs:
                acc.baseline_context_cost += cost
        if rec.model:
            self.model = rec.model
        for (cat, ctype), removed in self.sent.items():
            self._add(ctype, "tokens_resent_removed", removed, cat)
            saved = removed * self.prices.cache_read
            self._add(ctype, "projected_cost_weighted_saved", saved, cat)
        for key, removed in self.awaiting.items():
            cat, ctype = key
            self._add(ctype, "tokens_resent_removed", removed, cat)
            saved = removed * self.prices.cache_write
            self._add(ctype, "projected_cost_weighted_saved", saved, cat)
            self.sent[key] = self.sent.get(key, 0) + removed
        self.awaiting.clear()

    def on_compaction(self) -> None:
        self.sent.clear()
        self.awaiting.clear()

    def _settle(self, span: _Span) -> None:
        """Pay a span's best value (it leaves the watch)."""
        if span.best <= 0:
            return
        kind = "proxy_regret_full" if span.best >= regret.FULL else "proxy_regret_partial"
        self._add(span.ctype, kind, 1, span.cat)
        self._add(span.ctype, "proxy_regrets", span.best, span.cat)
        for acc in self.accs:
            acc.count(acc.signals, span.signal)
            if span.path_hit:
                acc.count(acc.signals, "path_mention")
            acc.count(acc.regret_turns, _turn_bucket(span.lag))

    def _full(self, matched: list[tuple[_Span, set[int]]], signal: str) -> None:
        if not matched:
            return
        for span, idx in matched:
            span.mark(regret.FULL, signal, self.turn - span.turn, idx)
            self._settle(span)
        gone = {id(span) for span, _ in matched}
        self.watched = deque(span for span in self.watched if id(span) not in gone)

    def _matches(self, digests: set[str]) -> list[tuple[_Span, set[int]]]:
        out = []
        for span in self.watched:
            idx = span.matched(digests)
            if len(idx) >= regret.MIN_TOKEN_HITS:
                out.append((span, idx))
        return out

    def _see(self, text: str, *, refresh: bool) -> set[str]:
        """Record ``text`` as visible context; with ``refresh``, its tokens no longer count as
        found only in a watched span (the agent has them from elsewhere now)."""
        digests = _digests(text, regret.SCAN_CHARS) if text else set()
        if digests and len(self.seen) < MAX_SEEN:
            self.seen |= digests
        if refresh and digests:
            for span in self.watched:
                span.forget(digests)
        return digests

    def on_prompt(self, ev: Prompt) -> None:
        self.turn = ev.turn
        if self.watched and ev.text:
            asked = _digests(ev.text, regret.INPUT_SCAN_CHARS)
            for span, idx in self._matches(asked):  # partial: the span stays watched
                span.mark(regret.PARTIAL, "user_prompt", self.turn - span.turn, idx)
        self._see(ev.text, refresh=True)
        self.prompt = ev.text[:PROMPT_CHARS]
        if any(self.turn - s.turn > self.window for s in self.watched):
            keep: deque[_Span] = deque()
            for span in self.watched:
                if self.turn - span.turn > self.window:
                    self._settle(span)
                else:
                    keep.append(span)
            self.watched = keep

    def on_said(self, text: str, signal: str = "assistant_text") -> None:
        if not text:
            return
        digests = _digests(text, regret.INPUT_SCAN_CHARS)
        if len(self.seen) < MAX_SEEN:
            self.seen |= digests
        if self.watched and len(digests) >= regret.MIN_TOKEN_HITS:
            self._full(self._matches(digests), signal)

    def _signature(self, name: str, inp: Mapping[str, Any]) -> str:
        sig = regret.signature(name, inp, project_dir=self.cwd, cwd=self.cwd)
        return regret.token_digest(sig)

    def on_assistant(self, rec: AssistantRecord) -> None:
        if rec.new_call:
            self.on_call(rec)
        for text in rec.texts:
            self.on_said(text)
        for _tid, name, inp in rec.tool_uses:
            edit = translate.tool_category(name) == "edit"
            if self.watched and not edit:
                sig = self._signature(name, inp)
                self._full([(s, set()) for s in self.watched if s.sig == sig], "rerun")
            self.on_said(regret.input_text(inp), "edit_input" if edit else "tool_input")

    def finish(self) -> None:
        """Session end: pay every watched span its best value."""
        for span in self.watched:
            self._settle(span)
        self.watched.clear()

    def on_result(self, rec: ToolResultRecord, duplicate: bool = False) -> None:
        if duplicate:
            for acc in self.accs:
                acc.duplicate_results += 1  # copied history of a resumed session
            self._see(rec.text, refresh=True)
            return
        for acc in self.accs:
            acc.tool_results += 1
        category = translate.tool_category(rec.tool_name) if rec.tool_name else "other"
        self.cats.append(f"{category}!" if rec.is_error else category)
        path = rec.input.get("file_path")
        if category in _PATH_TOOLS and isinstance(path, str) and path:
            self.paths.append(path)
        if rec.is_error or not rec.tool_name:
            self._see(rec.text, refresh=True)
            return  # failures cannot be replaced on Claude Code
        tool = ToolCall(rec.tool_name, category, rec.input, rec.tool_use_id)
        event = Event(
            host="claude",
            kind="post_tool",
            session_id="replay",
            project_dir=self.project_dir,
            project_key=self.project_key,
            transcript_path=None,
            tool=tool,
            output_text=rec.text,
            raw_response=rec.raw if rec.raw is not None else rec.text,
        )
        if (
            compression.skipped(event, self.cfg)
            or regret.is_recovery_access(event)
            or not translate.replaceable(event)
            or estimate_tokens(rec.text) < self.min_tokens
            or not self._replay(event, tool, rec, category)
        ):
            self._see(rec.text, refresh=True)  # shown in full

    def _ctx_pct(self) -> float | None:
        if self.ctx_tokens is None:
            return None
        window = governor.resolve_window(self.cfg, os.environ, "replay", self.model)
        return self.ctx_tokens / window if window > 0 else None

    def _replay(self, event: Event, tool: ToolCall, rec: ToolResultRecord, cat: str) -> bool:
        """True if the result was trimmed (then only its kept text became visible)."""
        text = rec.text
        ctype = detect(text, tool)
        est = compression.est_type(ctype)
        before = estimate_tokens(text, est)
        self._add(ctype, "eligible", 1, cat)
        self._add(ctype, "tokens_before", before, cat)
        sctx = SessionCtx(
            "replay",
            self.project_key,
            self.turn,
            self._ctx_pct(),
            self.model,
            _NULL_OVERLAY,
            tuple(self.cats),
        )
        feats = features.for_point(COMPRESSION, event, sctx, ctype, tokens=before)
        arm = self.policy.decide(COMPRESSION, feats, sctx).arm
        for acc in self.accs:
            acc.arms[arm] = acc.arms.get(arm, 0) + 1
        if arm == COMPRESSION.safe_arm:
            return False
        ctx = CompressContext(
            prompt=self.prompt or None, recent_paths=tuple(self.paths), config=self.cfg
        )
        picked = compression.pick_candidate(
            None,
            text,
            load_level(self.cfg, arm),
            registry.for_type(ctype),
            ctx,
            self.cfg,
            est,
            before,
            self.project_dir,
        )
        if picked is None:
            return False
        removed = before - picked.tokens_after
        self._add(ctype, "trimmed", 1, cat)
        self._add(ctype, "tokens_removed", removed, cat)
        self.awaiting[(cat, ctype)] = self.awaiting.get((cat, ctype), 0) + removed
        kept = picked.result.text
        seen = self.seen

        def skip(token: str) -> bool:
            return token.lower() in STOPWORDS or regret.token_digest(token) in seen

        tokens = regret.distinctive_tokens(
            text, kept, exclude=regret.input_text(rec.input), skip=skip
        )
        self._see(kept, refresh=True)
        digests: dict[str, int] = {}
        paths: set[int] = set()
        for i, token in enumerate(tokens):
            if _SEP.search(token):
                paths.add(i)
            for v in _variants(token):
                d = regret.token_digest(v)
                if d not in self.seen:
                    digests[d] = i
        if len(set(digests.values())) >= regret.MIN_TOKEN_HITS:
            self._add(ctype, "watched", 1, cat)
            sig = self._signature(rec.tool_name, rec.input)
            self.watched.append(_Span(digests, ctype, self.turn, sig, cat, frozenset(paths)))
            while len(self.watched) > MAX_WATCHED:
                self._settle(self.watched.popleft())
                for acc in self.accs:
                    acc.evicted += 1
        return True


def _window(cfg: Config) -> int:
    raw = cfg.get("learn.regret_window_turns", regret.WINDOW_TURNS)
    ok = isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw)
    return max(0, int(raw)) if ok else regret.WINDOW_TURNS


def _project_key(cwd: str, cache: dict[str, str]) -> str:
    key = cache.get(cwd)
    if key is None:
        if len(cache) >= 256:
            cache.clear()
        key = cache[cwd] = store_paths.project_key(Path(cwd))
    return key


def _replay_file(path: Path, lanes: Sequence[_Session], run_ids: RunIds | None) -> bool:
    """Stream one transcript once into every lane (one per policy); True if it held any event
    of its chain. Ids are de-duplicated once per run, before the lanes see the event."""
    seen = False
    is_new = run_ids.add if run_ids is not None else None
    for ev in iter_events(path, is_new_id=is_new):
        seen = True
        if isinstance(ev, AssistantRecord):
            for sess in lanes:
                sess.on_assistant(ev)
        elif isinstance(ev, ToolResultRecord):
            dup = run_ids is not None and not run_ids.add(f"t:{ev.tool_use_id}")
            for sess in lanes:
                sess.on_result(ev, dup)
        elif isinstance(ev, Prompt):
            for sess in lanes:
                sess.on_prompt(ev)
        elif isinstance(ev, Compaction):
            for sess in lanes:
                sess.on_compaction()
    for sess in lanes:
        sess.finish()
    return seen


def _cwd(path: Path) -> str | None:
    """The session's working directory (first ``cwd`` field): the engine snapshot's project."""
    cwd = first_field(path, "cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def resolve_policies(policy: Policy | str, cfg: Config, seed: int = 0) -> list[tuple[str, Policy]]:
    """One ``(name, policy)`` per lane: ``light,standard,aggressive`` (comma-separated) replays
    each in the same pass; anything else is a single lane (``resolve_policy``)."""
    if isinstance(policy, str) and "," in policy:
        names = [n.strip() for n in policy.split(",") if n.strip()]
        if not names or len(set(names)) != len(names):
            raise ValueError(f"policy list {policy!r} must name distinct policies")
        return [resolve_policy(n, cfg, seed) for n in names]
    return [resolve_policy(policy, cfg, seed)]


@dataclass
class _Lane:
    name: str
    policy: Policy
    total: _Acc
    parts: dict[str, _Acc]


def run(
    transcripts: Sequence[Path],
    policy: Policy | str,
    split: Split,
    date_cutoff: date,
    *,
    cfg: Config | None = None,
    prices: Prices | None = None,
    seed: int = 0,
) -> ReplayReport:
    """Replay ``transcripts`` (files or directories) under ``policy`` (or several, comma
    separated, in one pass); aggregates only."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"unknown split {split!r}")
    cfg = cfg or load_config(Path.cwd(), os.environ)
    prices = prices or Prices.load(cfg)
    lanes = [
        _Lane(name, pol, _Acc(), {s: _Acc() for s in SPLITS if split in ("all", s)})
        for name, pol in resolve_policies(policy, cfg, seed)
    ]
    window = _window(cfg)
    keys: dict[str, str] = {}
    run_ids = RunIds()
    needs_key = any(isinstance(lane.policy, FrozenEngine) for lane in lanes)
    with redacted_logs():
        for path in iter_transcripts(transcripts):
            part = "dev" if session_date(path) < date_cutoff else "test"
            if part not in lanes[0].parts:
                continue
            cwd = _cwd(path)
            key = (_project_key(cwd, keys) if cwd else "replay") if needs_key else "replay"
            sessions = []
            for lane in lanes:
                accs = (lane.total, lane.parts[part])
                sess = _Session(accs, lane.policy, cfg, prices, window, run_ids, cwd)
                sess.project_key = key
                sessions.append(sess)
            try:
                ok = _replay_file(path, sessions, run_ids)
            except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
                ok = None
            for lane in lanes:
                for acc in (lane.total, lane.parts[part]):
                    if ok is None:
                        acc.sessions_failed += 1
                    elif ok:
                        acc.sessions += 1
    reports = {}
    for lane in lanes:
        for acc in (lane.total, *lane.parts.values()):
            acc.ids_capped = run_ids.capped
        reports[lane.name] = {
            "totals": lane.total.to_dict(),
            "by_split": {s: acc.to_dict() for s, acc in lane.parts.items()},
        }
    first = reports[lanes[0].name]
    return ReplayReport(
        policy=",".join(lane.name for lane in lanes),
        split=split,
        cutoff=date_cutoff,
        totals=first["totals"],
        by_split=first["by_split"],
        by_arm=reports if len(lanes) > 1 else {},
    )


def summary_lines(rep: ReplayReport) -> list[str]:
    """Aggregate numbers for stdout: no paths, ids or text (content-type names are ours)."""
    lines = [f"token-sparen replay: policy {rep.policy}, split {rep.split}, cutoff {rep.cutoff}"]
    for label, t in [("all", rep.totals), *sorted(rep.by_split.items())]:
        counts = ("sessions", "sessions_failed", "calls", "tool_results", "eligible", "trimmed")
        lines.append(f"[{label}] " + ", ".join(f"{k} {t[k]}" for k in counts))
        share = 100 * t["saved_share_of_context_cost"]
        lines.append(
            f"[{label}] "
            + ", ".join(
                [
                    f"tokens_removed {t['tokens_removed']}",
                    f"tokens_resent_removed {t['tokens_resent_removed']}",
                    f"projected_cost_weighted_saved {t['projected_cost_weighted_saved']:.1f}",
                    f"saved_share_of_context_cost {share:.2f} %",
                    f"proxy_regret_rate {100 * t['proxy_regret_rate']:.2f} %",
                ]
            )
        )
    for arm, rows in rep.by_arm.items():
        t = rows["totals"]
        lines.append(
            f"[arm {arm}] trimmed {t['trimmed']}, tokens_removed {t['tokens_removed']}, "
            f"saved_share_of_context_cost {100 * t['saved_share_of_context_cost']:.2f} %, "
            f"proxy_regret_rate {100 * t['proxy_regret_rate']:.2f} %"
        )
    for ct, row in rep.totals["per_ctype"].items():
        keys = ("eligible", "trimmed", "tokens_removed", "tokens_resent_removed")
        rate = 100 * row["proxy_regret_rate"]
        lines.append(
            f"  {ct}: " + ", ".join(f"{k} {row[k]}" for k in keys) + f", regret {rate:.2f} %"
        )
    return lines
