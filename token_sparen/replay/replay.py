"""E1: counterfactual replay of Claude Code transcripts through the production compression path
(spec section 10.1). Private by construction: only aggregates leave this module.

For every tool result of a session's own chain (``transcript.iter_events``) the result is gated
exactly like the PostToolUse hook (tool deny-set, recovery reads, Claude Code's replaceable
shapes, failed results, the 2000-token floor), its content type detected, an arm chosen by the
policy under test (a ``StaticPolicy`` arm, or a frozen ``Engine`` snapshot: side-effect-free
``Engine.choose``, no pending entries, no rewards, no store writes) and compressed with
``compression.pick_candidate`` (the hook's never-larger rule). Nothing is saved.

**Projected re-sends.** A removed token is not sent again on every later API call of the session
until the next compaction (``transcript.is_compaction``: a ``compact_boundary`` system record or
an ``isCompactSummary`` user record). ``tokens_resent_removed`` = sum over trims of removed x
later calls (distinct message ids of the chain) before the next compaction. Cost-weighted
(``estimate.Prices``): the first later call writes the removed tokens to the prompt cache
(``cache_write``), every further call reads them (``cache_read``); a result followed by no call
saves nothing. ``baseline_context_cost`` prices the context of every call the same way (input,
cache write, cache read; output excluded), so the saved share is comparable.

**Proxy regret.** A trim is regretted when a later assistant text block or non-edit tool input
of the same session (edit inputs repeat code being changed; the live detector ignores them too)
contains >= 2 distinctive tokens of its elided span (``regret.distinctive_tokens``: 8+
characters with a letter, absent from the kept text and the tool input) that are found **only**
in that span: tokens already in the session's context before the trim (prompts, assistant text,
tool inputs, the visible text of earlier results; first ``SCAN_CHARS`` of each, up to
``MAX_SEEN`` digests) are dropped, and tokens that reappear in a later prompt or tool result
are dropped from the span before they can count. Matched through ``regret.token_digest`` within
``[learn] regret_window_turns`` user turns. At most ``MAX_WATCHED`` spans are watched (the
oldest is evicted unregretted, counted in ``evicted``).

**Memory.** One record at a time; per session only counters, the last prompt (<= 2000 chars),
recent tool categories and paths, and the watched spans' digests.

**Split.** A session's date is its first record ``timestamp`` (else the file's mtime); dates
before ``date_cutoff`` are ``dev``, the rest ``test``.
"""

from __future__ import annotations

import math
import os
import random
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
    "watched",
)


@dataclass
class _Acc:
    """Aggregates of a set of sessions (counts and sums only)."""

    sessions: int = 0
    sessions_failed: int = 0
    calls: int = 0
    tool_results: int = 0
    evicted: int = 0
    baseline_context_cost: float = 0.0
    arms: dict[str, int] = field(default_factory=dict)
    per_ctype: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, ctype: str, key: str, value: float) -> None:
        row = self.per_ctype.get(ctype)
        if row is None:
            row = self.per_ctype[ctype] = dict.fromkeys(_CT_FIELDS, 0.0)
        row[key] += value

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
            "eligible": int(self.total("eligible")),
            "trimmed": int(trimmed),
            "tokens_before_eligible": int(self.total("tokens_before")),
            "tokens_removed": int(self.total("tokens_removed")),
            "tokens_resent_removed": int(self.total("tokens_resent_removed")),
            "projected_cost_weighted_saved": round(saved, 3),
            "baseline_context_cost": round(self.baseline_context_cost, 3),
            "saved_share_of_context_cost": _ratio(saved, self.baseline_context_cost),
            "proxy_regrets": int(regrets),
            "proxy_regret_rate": _ratio(regrets, trimmed),
            "watched_spans": int(self.total("watched")),
            "evicted": self.evicted,
            "arms": dict(sorted(self.arms.items())),
        }
        out["per_ctype"] = {ct: _ct_row(row) for ct, row in sorted(self.per_ctype.items())}
        return out


def _ratio(num: float, den: float) -> float:
    return round(num / den, 6) if den > 0 else 0.0


def _ct_row(row: Mapping[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {
        k: (round(v, 3) if k == "projected_cost_weighted_saved" else int(v)) for k, v in row.items()
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
        }


HEURISTICS = {
    "resends": "later API calls (distinct message ids, own chain) before the next compaction "
    "(compact_boundary system record or isCompactSummary user record)",
    "cost": "first later call prices removed tokens as cache_write, each further call as "
    "cache_read (estimate.Prices); baseline prices every call's context the same way",
    "proxy_regret": ">= 2 distinctive tokens found only in the elided span (not in the context "
    "before the trim, not in a later prompt or tool result) in one later assistant text block or "
    "non-edit tool input within regret_window_turns user turns",
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


@dataclass
class _Span:
    digests: set[str]
    ctype: str
    turn: int


class _Session:
    """Replays one transcript into the accumulators (totals and its split)."""

    def __init__(
        self, accs: Sequence[_Acc], policy: Policy, cfg: Config, prices: Prices, window: int
    ) -> None:
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
        self.sent: dict[str, int] = {}  # removed tokens already sent once, per ctype
        self.awaiting: dict[str, int] = {}  # removed tokens not sent yet, per ctype
        self.watched: deque[_Span] = deque()
        self.seen: set[str] = set()
        self.ctx_tokens: int | None = None
        self.model: str | None = None
        self.project_dir = Path(os.devnull).parent
        self.project_key = "replay"
        self._keys: dict[str, str] = {}

    def _add(self, ctype: str, key: str, value: float) -> None:
        for acc in self.accs:
            acc.add(ctype, key, value)

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
        for ctype, removed in self.sent.items():
            self._add(ctype, "tokens_resent_removed", removed)
            self._add(ctype, "projected_cost_weighted_saved", removed * self.prices.cache_read)
        for ctype, removed in self.awaiting.items():
            self._add(ctype, "tokens_resent_removed", removed)
            self._add(ctype, "projected_cost_weighted_saved", removed * self.prices.cache_write)
            self.sent[ctype] = self.sent.get(ctype, 0) + removed
        self.awaiting.clear()

    def on_compaction(self) -> None:
        self.sent.clear()
        self.awaiting.clear()

    def _see(self, text: str, *, refresh: bool) -> set[str]:
        """Record ``text`` as visible context; with ``refresh``, its tokens no longer count as
        found only in a watched span (the agent has them from elsewhere now)."""
        digests = regret.text_digests(text, regret.SCAN_CHARS) if text else set()
        if digests and len(self.seen) < MAX_SEEN:
            self.seen |= digests
        if refresh and digests:
            for span in self.watched:
                span.digests -= digests
        return digests

    def on_prompt(self, ev: Prompt) -> None:
        self._see(ev.text, refresh=True)
        self.turn = ev.turn
        self.prompt = ev.text[:PROMPT_CHARS]
        if any(self.turn - s.turn > self.window for s in self.watched):
            self.watched = deque(s for s in self.watched if self.turn - s.turn <= self.window)

    def on_said(self, text: str, *, counts: bool = True) -> None:
        if not text:
            return
        digests = regret.text_digests(text)
        if len(self.seen) < MAX_SEEN:
            self.seen |= digests
        if not counts or not self.watched:
            return
        if len(digests) < regret.MIN_TOKEN_HITS:
            return
        keep: deque[_Span] = deque()
        for span in self.watched:
            if len(span.digests & digests) >= regret.MIN_TOKEN_HITS:
                self._add(span.ctype, "proxy_regrets", 1)
            else:
                keep.append(span)
        self.watched = keep

    def on_assistant(self, rec: AssistantRecord) -> None:
        if rec.new_call:
            self.on_call(rec)
        for text in rec.texts:
            self.on_said(text)
        for _tid, name, inp in rec.tool_uses:
            edit = translate.tool_category(name) == "edit"
            self.on_said(regret.input_text(inp), counts=not edit)

    def on_result(self, rec: ToolResultRecord) -> None:
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
            or not self._replay(event, tool, rec)
        ):
            self._see(rec.text, refresh=True)  # shown in full

    def _ctx_pct(self) -> float | None:
        if self.ctx_tokens is None:
            return None
        window = governor.resolve_window(self.cfg, os.environ, "replay", self.model)
        return self.ctx_tokens / window if window > 0 else None

    def _replay(self, event: Event, tool: ToolCall, rec: ToolResultRecord) -> bool:
        """True if the result was trimmed (then only its kept text became visible)."""
        text = rec.text
        ctype = detect(text, tool)
        est = compression.est_type(ctype)
        before = estimate_tokens(text, est)
        self._add(ctype, "eligible", 1)
        self._add(ctype, "tokens_before", before)
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
        self._add(ctype, "trimmed", 1)
        self._add(ctype, "tokens_removed", removed)
        self.awaiting[ctype] = self.awaiting.get(ctype, 0) + removed
        kept = picked.result.text
        tokens = regret.distinctive_tokens(text, kept, exclude=regret.input_text(rec.input))
        self._see(kept, refresh=True)
        digests = {regret.token_digest(t) for t in tokens} - self.seen
        if len(digests) >= regret.MIN_TOKEN_HITS:
            self._add(ctype, "watched", 1)
            self.watched.append(_Span(digests, ctype, self.turn))
            while len(self.watched) > MAX_WATCHED:
                self.watched.popleft()
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


def _replay_file(path: Path, sess: _Session) -> bool:
    """Stream one transcript; True if it held any event of its chain."""
    seen = False
    for ev in iter_events(path):
        seen = True
        if isinstance(ev, AssistantRecord):
            sess.on_assistant(ev)
        elif isinstance(ev, ToolResultRecord):
            sess.on_result(ev)
        elif isinstance(ev, Prompt):
            sess.on_prompt(ev)
        elif isinstance(ev, Compaction):
            sess.on_compaction()
    return seen


def _cwd(path: Path) -> str | None:
    """The session's working directory (first ``cwd`` field): the engine snapshot's project."""
    cwd = first_field(path, "cwd")
    return cwd if isinstance(cwd, str) and cwd else None


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
    """Replay ``transcripts`` (files or directories) under ``policy``; aggregates only."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"unknown split {split!r}")
    cfg = cfg or load_config(Path.cwd(), os.environ)
    prices = prices or Prices.load(cfg)
    name, pol = resolve_policy(policy, cfg, seed)
    window = _window(cfg)
    total = _Acc()
    parts = {s: _Acc() for s in SPLITS if split in ("all", s)}
    keys: dict[str, str] = {}
    needs_key = isinstance(pol, FrozenEngine)
    with redacted_logs():
        for path in iter_transcripts(transcripts):
            part = "dev" if session_date(path) < date_cutoff else "test"
            if part not in parts:
                continue
            accs = (total, parts[part])
            sess = _Session(accs, pol, cfg, prices, window)
            if needs_key:
                cwd = _cwd(path)
                sess.project_key = _project_key(cwd, keys) if cwd else "replay"
            try:
                if _replay_file(path, sess):
                    for acc in accs:
                        acc.sessions += 1
            except Exception:  # noqa: BLE001 - one bad session never stops the run (no details)
                for acc in accs:
                    acc.sessions_failed += 1
    return ReplayReport(
        policy=name,
        split=split,
        cutoff=date_cutoff,
        totals=total.to_dict(),
        by_split={s: acc.to_dict() for s, acc in parts.items()},
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
    for ct, row in rep.totals["per_ctype"].items():
        keys = ("eligible", "trimmed", "tokens_removed", "tokens_resent_removed")
        rate = 100 * row["proxy_regret_rate"]
        lines.append(
            f"  {ct}: " + ", ".join(f"{k} {row[k]}" for k in keys) + f", regret {rate:.2f} %"
        )
    return lines
