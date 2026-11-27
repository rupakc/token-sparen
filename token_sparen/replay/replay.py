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

**Split.** A session's date is the ``timestamp`` of its first record that is not copied history
(else its first record, else the file's mtime); dates before ``date_cutoff`` are ``dev``, the rest
``test``; sessions with any record from ``DOGFOOD_START`` on, or any ``[token-sparen:`` marker,
are excluded whole with their subagent files (``runner``).

**Runs.** ``run`` drives ``runner.run_parallel`` (groups by project directory, chronological
order and id de-duplication per group, bounded reads, optional processes and checkpoint); this
module's replay is the runner's ``compression`` lane (``CompressionLane``).
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import random
import re
import uuid
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from token_sparen.adapters.claude import translate
from token_sparen.compress import CompressContext, detect, load_level, registry
from token_sparen.compress.families import FAMILIES, command_family
from token_sparen.config import Config, load_config, state_home
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
    SessionEvent,
    ToolResultRecord,
)

if TYPE_CHECKING:
    from token_sparen.replay.runner import RunResult, SessionInfo

Split = Literal["dev", "test", "all"]
DEFAULT_CUTOFF = date(2026, 11, 1)
DOGFOOD_START = date(2026, 11, 26)  # sessions from here on ran with the plugin: excluded
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


GATED_POLICIES = frozenset({"default", "engine"})


def resolve_policy(policy: Policy | str, cfg: Config, seed: int = 0) -> tuple[str, Policy]:
    """``(name, policy)``: ``default`` (production: the quality gate, then the static arm),
    ``static`` (the static arm on every output, ungated), ``engine`` (frozen snapshot of the
    local learner state, gated like production) or a compression arm name (ungated); a
    ``Policy`` object is used as given (an ``Engine`` is frozen). Lanes named in
    ``GATED_POLICIES`` apply ``compression.gated``."""
    if isinstance(policy, Engine):
        frozen = FrozenEngine(policy.cfg, policy.stores.home, seed)
        frozen._engines[policy.stores.project_key] = policy
        return "engine", frozen
    if not isinstance(policy, str):
        return type(policy).__name__, policy
    if policy in ("static", "default"):
        return policy, StaticPolicy({})
    if policy == "engine":
        return policy, FrozenEngine(cfg, state_home(os.environ), seed)
    if policy in COMPRESSION.arms:
        return policy, StaticPolicy({COMPRESSION.name: policy})
    raise ValueError(
        f"unknown policy {policy!r}: default, static, engine or one of {COMPRESSION.arms}"
    )


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
    "recovery_tokens_resent",
    "recovery_cost_weighted",
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
    per_family: dict[str, dict[str, float]] = field(default_factory=dict)  # shell commands
    signals: dict[str, int] = field(default_factory=dict)
    regret_turns: dict[str, int] = field(default_factory=dict)
    gated: dict[str, int] = field(default_factory=dict)  # quality-gate reason -> results
    recovery_signals: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(
        self, ctype: str, key: str, value: float, cat: str = "other", fam: str | None = None
    ) -> None:
        tables = [(self.per_ctype, ctype), (self.per_tool, f"{cat}/{ctype}")]
        if fam is not None:
            tables.append((self.per_family, fam))
        for table, name in tables:
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
        recovery = self.total("recovery_cost_weighted")
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
            "recovery_tokens_resent": int(self.total("recovery_tokens_resent")),
            "recovery_cost_weighted": round(recovery, 3),
            "net_cost_weighted_saved": round(saved - recovery, 3),
            "net_saved_share_of_context_cost": _ratio(saved - recovery, self.baseline_context_cost),
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
        out["per_family"] = {
            f: _ct_row(self.per_family[f]) for f in FAMILIES if f in self.per_family
        }
        out["regret_signals"] = dict(sorted(self.signals.items()))
        out["regret_turns"] = {b: self.regret_turns.get(b, 0) for b in TURN_BUCKETS}
        out["gated"] = dict(sorted(self.gated.items()))
        out["recovery_by_signal"] = {
            sig: {
                "recovery_tokens_resent": int(row["recovery_tokens_resent"]),
                "recovery_cost_weighted": round(row["recovery_cost_weighted"], 3),
            }
            for sig, row in sorted(self.recovery_signals.items())
        }
        return out


_FLOAT_FIELDS = frozenset(
    {"projected_cost_weighted_saved", "proxy_regrets", "recovery_cost_weighted"}
)
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
    net = row["projected_cost_weighted_saved"] - row["recovery_cost_weighted"]
    out["net_cost_weighted_saved"] = round(net, 3)
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
    lanes: dict[str, dict[str, Any]] = field(default_factory=dict)  # other lanes, by split
    run: dict[str, Any] = field(default_factory=dict)  # runner counters (no paths)

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
            **({"lanes": self.lanes} if self.lanes else {}),
            **({"run": self.run} if self.run else {}),
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
    "net": "net_cost_weighted_saved = saved - recovery: each regretted trim (except a rerun) is "
    "assumed to be followed by one recovery read of the full output (tokens_before, times the "
    "regret value; a partial regret charges half) in the turn the regret fires, priced like a "
    "removed token (cache write on the next call, cache read after) until the next compaction",
    "families": "per_family: shell (Bash) results only, by the first program of the normalised "
    "command (+ subcommand for git, npm/pnpm/yarn/bun, cargo, go, pip/uv, make, gradle, mvn); "
    "linters and type checkers count as build",
    "breakdowns": "per_tool keys are <tool category>/<content type> (categories: shell = Bash, "
    "read, search, web, agent, mcp, other); regret_signals counts the signal that settled each "
    "regretted trim (path_mention: a matched token was a path or its basename, overlaps the "
    "others); regret_turns: user turns from the trim to that signal; by_arm: every arm of a "
    "comma-separated --policy replayed in the same pass (totals and by_split are the first arm)",
    "gates": "PostToolUse gates: skip_tools/edit category, recovery reads, Claude Code "
    "replaceable shapes, failed results, 2000-token floor; compression.pick_candidate",
    "gating": "policies default and engine also apply the production quality gate "
    "(compression.gated: file contents - Read results and file-dump shell commands - and content "
    "types outside [compression] allow_ctypes are never offered to the decision point; counted "
    "in gated by reason, not eligible); static and arm names replay every eligible output",
    "rerun": "a rerun regret counts as regret but charges no recovery read: the re-run's "
    "tokens are already in the baseline; recovery_by_signal splits the recovery cost by the "
    "signal that charged it",
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

    def has(self, key: str) -> bool:
        """True if ``key`` was recorded (nothing is added)."""
        d = hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=8).digest()
        return d in self._ids


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
    fam: str | None = None
    before: int = 0  # tokens of the full output (what a recovery read brings back)
    charged: float = 0.0  # recovery weight already charged
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


_SAVED = "saved"
_RECOVERY = "recovery"
_Key = tuple[str, str, str | None, str, str]  # category, ctype, family, kind, signal
_FIELDS_BY_KIND = {
    _SAVED: ("tokens_resent_removed", "projected_cost_weighted_saved"),
    _RECOVERY: ("recovery_tokens_resent", "recovery_cost_weighted"),
}


def _family(rec: ToolResultRecord) -> str | None:
    """Command family of a Bash result (None for every other tool)."""
    if rec.tool_name != "Bash":
        return None
    cmd = rec.input.get("command")
    return command_family(cmd) if isinstance(cmd, str) else "other"


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
        gate: bool = False,
    ) -> None:
        self.gate = gate
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
        # tokens already sent once / not sent yet, per (category, ctype, family, kind): kind
        # _SAVED = removed by a trim, _RECOVERY = brought back by an assumed recovery read
        self.sent: dict[_Key, float] = {}
        self.awaiting: dict[_Key, float] = {}
        self.watched: deque[_Span] = deque()
        self.seen: set[str] = set()
        self.ctx_tokens: int | None = None
        self.model: str | None = None
        self.project_dir = Path(os.devnull).parent
        self.project_key = "replay"
        self._keys: dict[str, str] = {}

    def _add(
        self, ctype: str, key: str, value: float, cat: str = "other", fam: str | None = None
    ) -> None:
        for acc in self.accs:
            acc.add(ctype, key, value, cat, fam)

    def _charge(self, span: _Span, signal: str) -> None:
        """Queue the recovery read the regret implies (weighted by its value so far). A rerun
        charges nothing: its tokens are already in the baseline."""
        extra = span.best - span.charged
        if extra > 0 and signal != "rerun":
            key = (span.cat, span.ctype, span.fam, _RECOVERY, signal)
            self.awaiting[key] = self.awaiting.get(key, 0.0) + span.before * extra
            span.charged = span.best

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
        for (cat, ctype, fam, kind, signal), n in self.sent.items():
            self._book(cat, ctype, fam, kind, signal, n, self.prices.cache_read)
        for key, n in self.awaiting.items():
            cat, ctype, fam, kind, signal = key
            self._book(cat, ctype, fam, kind, signal, n, self.prices.cache_write)
            self.sent[key] = self.sent.get(key, 0.0) + n
        self.awaiting.clear()

    def _book(
        self, cat: str, ctype: str, fam: str | None, kind: str, signal: str, n: float, price: float
    ) -> None:
        tok_field, cost_field = _FIELDS_BY_KIND[kind]
        self._add(ctype, tok_field, n, cat, fam)
        self._add(ctype, cost_field, n * price, cat, fam)
        if kind == _RECOVERY:
            for acc in self.accs:
                row = acc.recovery_signals.setdefault(
                    signal, dict.fromkeys(_FIELDS_BY_KIND[kind], 0.0)
                )
                row[tok_field] += n
                row[cost_field] += n * price

    def on_compaction(self) -> None:
        self.sent.clear()
        self.awaiting.clear()

    def _settle(self, span: _Span) -> None:
        """Pay a span's best value (it leaves the watch)."""
        if span.best <= 0:
            return
        kind = "proxy_regret_full" if span.best >= regret.FULL else "proxy_regret_partial"
        self._add(span.ctype, kind, 1, span.cat, span.fam)
        self._add(span.ctype, "proxy_regrets", span.best, span.cat, span.fam)
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
            self._charge(span, signal)
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
                self._charge(span, "user_prompt")
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
            or not self._replay(event, tool, rec, category, _family(rec))
        ):
            self._see(rec.text, refresh=True)  # shown in full

    def _ctx_pct(self) -> float | None:
        if self.ctx_tokens is None:
            return None
        window = governor.band_window(
            self.cfg, os.environ, "replay", (self.model,), self.ctx_tokens
        )  # the hook's band window (a 1M-context session is not mis-banded)
        return self.ctx_tokens / window if window > 0 else None

    def _replay(
        self, event: Event, tool: ToolCall, rec: ToolResultRecord, cat: str, fam: str | None
    ) -> bool:
        """True if the result was trimmed (then only its kept text became visible)."""
        text = rec.text
        ctype = detect(text, tool)
        if self.gate:
            reason = compression.gated(event, ctype, self.cfg)
            if reason is not None:
                for acc in self.accs:
                    acc.count(acc.gated, reason)
                return False
        est = compression.est_type(ctype)
        before = estimate_tokens(text, est)
        self._add(ctype, "eligible", 1, cat, fam)
        self._add(ctype, "tokens_before", before, cat, fam)
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
        self._add(ctype, "trimmed", 1, cat, fam)
        self._add(ctype, "tokens_removed", removed, cat, fam)
        key = (cat, ctype, fam, _SAVED, "")
        self.awaiting[key] = self.awaiting.get(key, 0.0) + removed
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
            self._add(ctype, "watched", 1, cat, fam)
            sig = self._signature(rec.tool_name, rec.input)
            span = _Span(digests, ctype, self.turn, sig, cat, frozenset(paths), fam, before)
            self.watched.append(span)
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


def resolve_policies(policy: Policy | str, cfg: Config, seed: int = 0) -> list[tuple[str, Policy]]:
    """One ``(name, policy)`` per lane: ``light,standard,aggressive`` (comma-separated) replays
    each in the same pass; anything else is a single lane (``resolve_policy``)."""
    if isinstance(policy, str) and "," in policy:
        names = [n.strip() for n in policy.split(",") if n.strip()]
        if not names or len(set(names)) != len(names):
            raise ValueError(f"policy list {policy!r} must name distinct policies")
        return [resolve_policy(n, cfg, seed) for n in names]
    return [resolve_policy(policy, cfg, seed)]


class CompressionLane:
    """The E1 compression replay as a runner lane (``runner.LANES["compression"]``). Params:
    ``policy`` (a name or comma-separated list; a ``Policy`` object only in-process), ``seed``,
    ``prices`` (in-process). ``report()`` maps a split to ``{policy: accumulator state}``
    (``acc_view`` renders one as the report's ``totals`` shape)."""

    history = False

    def __init__(self, cfg: Config, params: Mapping[str, Any]) -> None:
        self.cfg = cfg
        prices = params.get("prices")
        self.prices = prices if isinstance(prices, Prices) else Prices.load(cfg)
        seed = params.get("seed", 0)
        self.policies = resolve_policies(
            params.get("policy", "static"), cfg, seed if isinstance(seed, int) else 0
        )
        self.window = _window(cfg)
        self.accs = {part: {name: _Acc() for name, _ in self.policies} for part in SPLITS}
        self.needs_key = any(isinstance(pol, FrozenEngine) for _, pol in self.policies)
        self.keys: dict[str, str] = {}
        self.capped = False
        self._current: SessionInfo | None = None
        self._sessions: list[_Session] = []
        self._failed = False

    def _start(self, info: SessionInfo) -> None:
        cwd = info.cwd
        key = (_project_key(cwd, self.keys) if cwd else "replay") if self.needs_key else "replay"
        self._sessions = []
        for name, pol in self.policies:
            accs = (self.accs[info.split][name],)
            sess = _Session(
                accs, pol, self.cfg, self.prices, self.window, None, cwd, name in GATED_POLICIES
            )
            sess.project_key = key
            self._sessions.append(sess)
        self._current = info
        self._failed = False

    def feed(self, event: SessionEvent, session: SessionInfo) -> None:
        if session is not self._current:
            self._start(session)
        if self._failed:
            return
        try:
            if isinstance(event, AssistantRecord):
                for sess in self._sessions:
                    sess.on_assistant(event)
            elif isinstance(event, ToolResultRecord):
                for sess in self._sessions:
                    sess.on_result(event, event.duplicate)
            elif isinstance(event, Prompt):
                for sess in self._sessions:
                    sess.on_prompt(event)
            elif isinstance(event, Compaction):
                for sess in self._sessions:
                    sess.on_compaction()
        except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
            self._failed = True

    def end_session(self, session: SessionInfo) -> None:
        self.capped = self.capped or session.run_ids.capped
        if session is not self._current:
            return  # no event of its chain: not a session
        if not self._failed:
            try:
                for sess in self._sessions:
                    sess.finish()
            except Exception:  # noqa: BLE001 - counted, never described
                self._failed = True
        for name, _ in self.policies:
            acc = self.accs[session.split][name]
            if self._failed:
                acc.sessions_failed += 1
            else:
                acc.sessions += 1
        self._current, self._sessions = None, []

    def abort_session(self, session: SessionInfo) -> None:
        """The runner failed mid-session: count it failed (partial sums stay, as before)."""
        if session is self._current:
            self._failed = True
            self.end_session(session)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for part, by_name in self.accs.items():
            out[part] = {}
            for name, acc in by_name.items():
                acc.ids_capped = self.capped
                out[part][name] = asdict(acc)
        return out


def acc_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The report shape (``totals``) of one compression accumulator state."""
    return _Acc(**copy.deepcopy(dict(raw))).to_dict()


def build_report(
    result: RunResult, names: Sequence[str], split: Split, cutoff: date
) -> ReplayReport:
    """A ``ReplayReport`` from a ``runner.RunResult`` (compression policies ``names``; the
    first is ``totals``); every other lane's merged reports go to ``lanes``."""
    parts = [s for s in SPLITS if split in ("all", s)]
    empty = asdict(_Acc())

    def view(part: str, name: str) -> dict[str, Any]:
        lane = result.by_split.get(part, {}).get("compression", {})
        return acc_view(lane.get(name, empty))

    reports = {
        name: {"totals": view("all", name), "by_split": {p: view(p, name) for p in parts}}
        for name in names
    }
    lanes: dict[str, dict[str, Any]] = {}
    for part in (*parts, "all"):
        for label, rep in result.by_split.get(part, {}).items():
            if label != "compression":
                lanes.setdefault(label, {})[part] = rep
    first = reports[names[0]]
    return ReplayReport(
        policy=",".join(names),
        split=split,
        cutoff=cutoff,
        totals=first["totals"],
        by_split=first["by_split"],
        by_arm=reports if len(names) > 1 else {},
        lanes=lanes,
        run=result.counters(),
    )


def run(
    transcripts: Sequence[Path],
    policy: Policy | str,
    split: Split,
    date_cutoff: date,
    *,
    cfg: Config | None = None,
    prices: Prices | None = None,
    seed: int = 0,
    jobs: int = 1,
    lanes: Sequence[tuple[str, dict[str, Any]]] = (),
    checkpoint: Path | None = None,
    groups: set[str] | None = None,
    dogfood_start: date = DOGFOOD_START,
) -> ReplayReport:
    """Replay ``transcripts`` (files or directories) under ``policy`` (or several, comma
    separated, in one pass) with ``runner.run_parallel``: the compression lane plus any other
    ``lanes``; aggregates only."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"unknown split {split!r}")
    cfg = cfg or load_config(Path.cwd(), os.environ)
    names = [name for name, _ in resolve_policies(policy, cfg, seed)]
    if jobs != 1 and not isinstance(policy, str):
        raise ValueError("a policy object replays only in-process (jobs=1)")
    from token_sparen.replay import runner  # noqa: PLC0415 - runner imports this module

    params: dict[str, Any] = {"policy": policy, "seed": seed}
    if prices is not None:
        params["prices"] = prices
    result = runner.run_parallel(
        list(transcripts),
        [("compression", params), *lanes],
        jobs=jobs,
        cutoff=date_cutoff,
        dogfood_start=dogfood_start,
        groups=groups,
        checkpoint=checkpoint,
        split=split,
        cfg=cfg,
    )
    return build_report(result, names, split, date_cutoff)


def summary_lines(rep: ReplayReport) -> list[str]:
    """Aggregate numbers for stdout: no paths, ids or text (content-type names are ours)."""
    lines = [f"token-sparen replay: policy {rep.policy}, split {rep.split}, cutoff {rep.cutoff}"]
    if rep.run:
        r = rep.run
        errors = ", ".join(f"{k} {v}" for k, v in r["worker_errors"].items()) or "none"
        lines.append(
            f"[run] files {r['files']}, sessions {r['sessions']}, "
            f"excluded_dogfood {r['excluded_dogfood']}, malformed_lines {r['malformed_lines']}, "
            f"oversize_lines {r['oversize_lines']}, skipped_records {r['skipped_records']}, "
            f"sessions_failed {r.get('sessions_failed', 0)}, "
            f"groups {r['groups']} (resumed {r['groups_resumed']}), worker_errors {errors}, "
            f"complete {str(r.get('complete', True)).lower()}"
        )
    for lane in rep.lanes:
        lines.append(f"[lane {lane}] see the report")
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
            f"net {100 * t['net_saved_share_of_context_cost']:.2f} %, "
            f"proxy_regret_rate {100 * t['proxy_regret_rate']:.2f} %"
        )
    for ct, row in rep.totals["per_ctype"].items():
        keys = ("eligible", "trimmed", "tokens_removed", "tokens_resent_removed")
        rate = 100 * row["proxy_regret_rate"]
        lines.append(
            f"  {ct}: " + ", ".join(f"{k} {row[k]}" for k in keys) + f", regret {rate:.2f} %"
        )
    return lines
