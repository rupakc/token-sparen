"""Fact lifecycle as a bandit (spec section 6.8): candidate -> active -> retired.

Each stored fact carries a Beta posterior ``effect`` over "injecting this fact prevents the
waste it targets, in sessions where it is relevant" (injected sessions only), a ``baseline``
Beta over "the waste is absent when the fact is withheld", a ``status`` and counters
(``truth``, ``contradictions``, ``unneeded``, ``waste_events``, ``stale``), all in
``facts.json``, prior Beta(1, 1). A wrong fact costs
quality, so facts activate on solid evidence and retire fast.

**Session outcome** (applied once per fold, ``apply_session``). Only relevant sessions count:

=============  ============================  ============================================
kind           relevant when the session...  waste (reward 0 when present)
=============  ============================  ============================================
command_alias  ran the subject or the alias  ``command not found`` for the subject
test_command   ran its own runner (``runner``:  the own runner failed with not-found or a
               the normalised program) or    usage error, or it was replaced (another
               was replaced by another one   runner succeeded while it never ran). A
                                             session where the own runner only had
                                             ordinary test failures is neutral
requires_env   ran the subject program       an error naming the variable
build_first    ran the subject (test) prog.  a subject failure fixed by running the build
generated_dir  wrote under the directory     >= 2 writes under it, or a write followed by a
                                             successful build that is not a lint/type
                                             check (regenerated: lint/format/type-check
                                             tools, scripts and make/just/task targets
                                             excluded); a single write is
                                             undetectable: reward 0.5
=============  ============================  ============================================

- injected & relevant: ``effect`` += 1 when the waste was absent, 0 when present;
- withheld & relevant (propensity floor ``[facts] withhold_floor``, decided per active fact
  at SessionStart, seeded by session + fact so a resume decides the same, logged in the
  session overlay with its propensity): ``baseline`` += the same reward; the effect estimate is
  ``effect.mean - baseline.mean``;
- a candidate that the miners re-derived with new session evidence (not injected) is a
  **confirmation**: ``truth`` += 1 (the fact is true; it says nothing about what injecting
  it does, so ``effect`` is untouched), for the waste -> fix kinds only (command_alias,
  requires_env, build_first). ``migrate`` moves the confirmations a legacy entry (no
  ``truth`` field) had put into ``effect``;
- a **contradiction** (counted only when injected; reward 0): command_alias / test_command /
  build_first - the fact's value command fails with not-found or a usage error; test_command
  also - replaced: another normalised runner succeeded while the fact's own runner never ran;
  requires_env - the subject ran successfully without the variable being set by the command
  or an earlier ``export``; generated_dir (counted whenever observed, injected or not, and
  re-checked every fold it is in play, edited or not) - git says the directory is not ignored
  and a file there (an edited one, else a sample) lacks a generated marker;
- **unneeded**: command_alias - the subject itself ran successfully (injected or withheld
  sessions; retire at 2); build_first - the tests passed before any build, counted only in
  withheld sessions and retiring once seen on 2 distinct UTC days;
- **waste event** (counted for candidates too): test_command - another runner failed before
  the fact's own runner succeeded; generated_dir - a write under the directory was followed by
  a successful non-lint build. Frequency-kind candidates with >= 2 sessions are observed for this;
- **staleness** (active test_command / generated_dir): a relevant injected or withheld session
  without new evidence adds 1 to ``stale``; new evidence resets it.

**Transitions** (every fold, every fact):

- candidate -> active: >= 2 distinct sessions of evidence and either ``Beta(1 + truth,
  1).lcb(0.1) > [facts] activate_lcb`` (0.55: 3 confirmations) or, the **bootstrap rule**,
  while no withheld baseline exists, >= 3 sessions of evidence. The frequency kinds
  (test_command, generated_dir: mined from counts, not from a fix) have no confirmations, so
  the lcb rule is unreachable for them as candidates (their posterior moves only once
  injected); they additionally need >= 5 sessions and >= 1 observed waste event. For the
  confirming kinds both rules agree at 3 sessions;
- one active test_command per subject: activating one retires the others;
- active -> retired: contradictions >= 1 (requires_env, build_first, generated_dir: high
  stakes) / >= 2 (others); unneeded as above; ``effect.ucb(0.9) < [facts] retire_ucb`` (0.4); a
  baseline of >= 3 withheld relevant sessions with ``baseline.mean >= effect.mean - 0.1`` (it
  does not help); after >= 8 injected relevant sessions (``effect.n``), ``effect.ucb(0.9) <
  baseline.mean + 0.1`` (the baseline is its prior 0.5 until withheld sessions are seen);
  frequency kinds also after 10 stale sessions or 30 days since last mined. Retirement is
  final (``facts forget`` removes the fact; it is then never re-learned).
"""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from token_sparen.compress.families import command_family
from token_sparen.config import flag
from token_sparen.facts import miners
from token_sparen.facts.sanitize import Fact, fact_id
from token_sparen.learn.beta import Beta
from token_sparen.learn.regret import normalize_command
from token_sparen.transcript import ToolResultRecord

Status = Literal["candidate", "active", "retired"]
STATUSES: frozenset[str] = frozenset({"candidate", "active", "retired"})
SESSION_KEY = "facts_session"  # session overlay: {"shown": {id: p}, "withheld": {id: p}, ...}
PRIOR = Beta(1.0, 1.0)
CAP = 50.0  # posterior mass cap (mean-preserving rescale, as everywhere in the engine)
ACTIVATE_Q = 0.1
RETIRE_Q = 0.9
BOOTSTRAP_SESSIONS = 3
CONFIRMING = frozenset({"command_alias", "requires_env", "build_first"})
# Retire on the first contradiction: a wrong env / build step, or a hand-written directory
# labelled generated (the agent would avoid files it must edit).
HIGH_STAKES = frozenset({"requires_env", "build_first", "generated_dir"})
RETIRE_AFTER = {True: 1, False: 2}  # contradictions, by high stakes
UNNEEDED_AFTER = 2  # sessions showing the fact was not needed (alias, build_first)
UNNEEDED_DAYS = 2  # build_first: withheld unneeded sessions on this many distinct UTC days
REPLACED_RUNS = 1  # test_command: another runner's successful runs that count as replacing it
FREQUENCY = frozenset({"test_command", "generated_dir"})  # mined from counts, not from a fix
FREQUENCY_SESSIONS = 5  # evidence sessions a frequency fact needs before activation
FREQUENCY_WASTE = 1  # ... and observed waste events
EXCLUSIVE = frozenset({"test_command"})  # one active fact per (kind, subject)
BASELINE_N = 3  # withheld relevant sessions before the baseline can retire a fact
BASELINE_MARGIN = 0.1  # retire when baseline.mean >= effect.mean - this
GEN_RECHECKS = 3  # generated_dir facts re-checked (git + markers) per fold, round robin
EFFECT_HORIZON = 8  # injected relevant sessions after which the effect ucb alone can retire
STALE_SESSIONS = 10  # relevant sessions without new evidence (frequency kinds)
STALE_SECONDS = 30 * 86400.0  # ... or this long since the fact was last mined
MAX_WITHHOLD = 0.5
_USAGE = re.compile(
    r"(?im)^\s*usage:|unrecognized arguments|unknown (?:command|option)|invalid option"
    r"|no such option|missing script"
)


@dataclass(frozen=True)
class Settings:
    """``[facts]`` settings (``settings`` reads them from a config mapping)."""

    enabled: bool = True
    withhold_floor: float = 0.1
    activate_lcb: float = 0.55
    retire_ucb: float = 0.4
    max_tokens: int = 300
    max_facts: int = 8


def _real(value: object, default: float, lo: float, hi: float) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    if not ok or not math.isfinite(float(value)):  # type: ignore[arg-type]
        return default
    return min(hi, max(lo, float(value)))  # type: ignore[arg-type]


def settings(params: Mapping[str, Any] | None) -> Settings:
    """The ``[facts]`` table of a merged config (``Config.data``); malformed values fall back to
    the defaults, ``withhold_floor`` is clamped to [0, 0.5], counts below 0 mean 0."""
    d = Settings()
    raw = params.get("facts") if isinstance(params, Mapping) else None
    t: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Settings(
        enabled=flag(t.get("enabled"), d.enabled),
        withhold_floor=_real(t.get("withhold_floor"), d.withhold_floor, 0.0, MAX_WITHHOLD),
        activate_lcb=_real(t.get("activate_lcb"), d.activate_lcb, 0.0, 1.0),
        retire_ucb=_real(t.get("retire_ucb"), d.retire_ucb, 0.0, 1.0),
        max_tokens=int(_real(t.get("max_tokens"), d.max_tokens, 0.0, 1e6)),
        max_facts=int(_real(t.get("max_facts"), d.max_facts, 0.0, 1e4)),
    )


# --------------------------------------------------------------------------- entry state
def status(entry: Mapping[str, Any]) -> Status:
    raw = entry.get("status")
    return raw if isinstance(raw, str) and raw in STATUSES else "candidate"  # type: ignore[return-value]


def _beta(entry: Mapping[str, Any], key: str) -> Beta:
    raw = entry.get(key)
    try:
        return Beta.from_list(raw) if isinstance(raw, list) else PRIOR
    except ValueError:
        return PRIOR


def effect(entry: Mapping[str, Any]) -> Beta:
    """Posterior that injecting the fact prevents its waste (prior Beta(1, 1))."""
    return _beta(entry, "effect")


def baseline(entry: Mapping[str, Any]) -> Beta:
    """Posterior that the waste is absent when the fact is withheld (prior Beta(1, 1))."""
    return _beta(entry, "baseline")


def estimated_effect(entry: Mapping[str, Any]) -> float | None:
    """``effect.mean - baseline.mean``, None while no withheld relevant session was seen."""
    base = baseline(entry)
    return None if base.n == 0 else effect(entry).mean - base.mean


def _count(entry: Mapping[str, Any], key: str) -> int:
    raw = entry.get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _days(entry: Mapping[str, Any]) -> list[int]:
    raw = entry.get("unneeded_days")
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, int) and not isinstance(d, bool)]


def truth(entry: Mapping[str, Any]) -> int:
    """Confirmations: sessions where the miners re-derived the fact while it was not injected
    (the waste happened and was fixed). Gates activation, never the injection effect."""
    return _count(entry, "truth")


def _evidence(entry: Mapping[str, Any]) -> int:
    raw = entry.get("sessions")
    return len({s for s in raw if isinstance(s, str)}) if isinstance(raw, list) else 0


def _observe(post: Beta, x: float) -> Beta:
    return post.update(x, gamma=1.0, cap=CAP, prior=PRIOR)


@dataclass(frozen=True)
class Obs:
    """One session's view of one fact. ``waste`` None = not detectable (reward 0.5);
    ``contradicted``: the fact looked wrong (counted when injected); ``unneeded``: the session
    did fine without what the fact says (counted when injected or withheld); ``waste_event``:
    the waste a frequency fact targets was seen (counted for candidates too, for activation);
    ``checked``: a generated_dir fact was re-checked this fold, ``generated`` its result
    (``miners.generated_evidence``: True positive evidence, False hand-written, None unknown)."""

    relevant: bool = False
    waste: bool | None = False
    contradicted: bool = False
    unneeded: bool = False
    waste_event: bool = False
    checked: bool = False
    generated: bool | None = None

    @property
    def reward(self) -> float:
        return 0.5 if self.waste is None else 0.0 if self.waste else 1.0


def apply_session(
    table: dict[str, Any],
    *,
    shown: Collection[str],
    withheld: Mapping[str, float],
    obs: Mapping[str, Obs],
    mined: Collection[str],
    settings: Settings,
    now: float | None = None,
) -> None:
    """Apply one session's outcome to the stored facts (in place), then the transitions.
    ``mined``: ids that gained new session evidence; ``now`` enables the age rule."""
    activated: list[str] = []
    for fid, entry in table.items():
        if not isinstance(entry, dict):
            continue
        o = obs.get(fid, Obs())
        st = status(entry)
        kind = entry.get("kind")
        in_play = fid in shown or fid in withheld
        if fid in shown:
            if o.contradicted:
                entry["contradictions"] = _count(entry, "contradictions") + 1
                entry["effect"] = _observe(effect(entry), 0.0).to_list()
            elif o.relevant:
                entry["effect"] = _observe(effect(entry), o.reward).to_list()
        elif fid in withheld:
            if o.relevant:
                entry["baseline"] = _observe(baseline(entry), o.reward).to_list()
        elif st == "candidate" and fid in mined and kind in CONFIRMING:
            entry["truth"] = truth(entry) + 1  # the fact is true; says nothing about injecting
        if o.contradicted and kind == "generated_dir" and fid not in shown and st != "retired":
            entry["contradictions"] = _count(entry, "contradictions") + 1  # a hand-written dir
        entry.setdefault("truth", truth(entry))
        if kind == "generated_dir" and o.checked and st != "retired":
            entry["checked_at"] = 0.0 if now is None else now
            if o.generated is True:
                entry["verified"] = True
            elif entry.get("verified") is not True:  # mined from edits alone (before wave B)
                entry["contradictions"] = max(1, _count(entry, "contradictions"))
        if o.unneeded and kind == "build_first":
            if fid in withheld and now is not None:  # injected sessions may owe it to the fact
                days = _days(entry)
                entry["unneeded_days"] = sorted({*days, int(now // 86400)})[-UNNEEDED_AFTER:]
        elif in_play and o.unneeded:
            entry["unneeded"] = _count(entry, "unneeded") + 1
        if o.waste_event:
            entry["waste_events"] = _count(entry, "waste_events") + 1
        if kind in FREQUENCY and st == "active":
            if fid in mined:
                entry["stale"] = 0
            elif in_play and o.relevant:
                entry["stale"] = _count(entry, "stale") + 1
        new = _transition(entry, st, settings, now)
        entry["status"] = new
        if st == "candidate" and new == "active" and kind in EXCLUSIVE:
            activated.append(fid)
    _exclusive(table, activated)


def _exclusive(table: dict[str, Any], activated: list[str]) -> None:
    """One active fact per (kind, subject) for ``EXCLUSIVE`` kinds: a newly activated fact
    retires the others (among several new ones, the one with most evidence, then most recent,
    wins)."""
    best = sorted(
        activated,
        key=lambda f: (-_evidence(table[f]), -_when(table[f]), f),
    )
    keep: dict[tuple[object, object], str] = {}
    for fid in best:
        key = (table[fid].get("kind"), table[fid].get("subject"))
        keep.setdefault(key, fid)
    for fid, entry in table.items():
        if not isinstance(entry, dict) or status(entry) != "active":
            continue
        winner = keep.get((entry.get("kind"), entry.get("subject")))
        if winner is not None and winner != fid:
            entry["status"] = "retired"


def _transition(entry: Mapping[str, Any], st: Status, s: Settings, now: float | None) -> Status:
    if st == "retired":
        return st
    kind = entry.get("kind")
    post = effect(entry)
    if _count(entry, "contradictions") >= RETIRE_AFTER[kind in HIGH_STAKES]:
        return "retired"
    if _count(entry, "unneeded") >= UNNEEDED_AFTER or len(_days(entry)) >= UNNEEDED_DAYS:
        return "retired"
    if st == "active":
        if post.ucb(RETIRE_Q) < s.retire_ucb:
            return "retired"
        base = baseline(entry)
        if base.n >= BASELINE_N and base.mean >= post.mean - BASELINE_MARGIN:
            return "retired"  # withheld sessions do as well: the fact does not help
        if post.n >= EFFECT_HORIZON and post.ucb(RETIRE_Q) < base.mean + BASELINE_MARGIN:
            return "retired"  # enough injected sessions: not clearly better than without
        if kind in FREQUENCY:
            if _count(entry, "stale") >= STALE_SESSIONS:
                return "retired"
            if now is not None and now - _when(entry) > STALE_SECONDS:
                return "retired"
        return "active"
    n = _evidence(entry)
    if n < miners.MIN_SESSIONS:
        return "candidate"
    if kind in FREQUENCY and (
        n < FREQUENCY_SESSIONS or _count(entry, "waste_events") < FREQUENCY_WASTE
    ):
        return "candidate"
    if baseline(entry).n == 0 and n >= BOOTSTRAP_SESSIONS:
        return "active"
    confirmed = Beta(PRIOR.a + truth(entry), PRIOR.b)
    return "active" if confirmed.lcb(ACTIVATE_Q) > s.activate_lcb else "candidate"


def migrate(table: dict[str, Any]) -> None:
    """Legacy entries (no ``truth`` field) put confirmations into ``effect``: move them out
    (in place, idempotent). A candidate's effect successes were all confirmations; an active
    fact had at most ``BOOTSTRAP_SESSIONS`` of them (both activation rules fire by then), so at
    most that many successes are moved (when unsure, the effect gets more pessimistic)."""
    for entry in table.values():
        if not isinstance(entry, dict) or "truth" in entry:
            continue
        post = effect(entry)
        moved = 0
        if entry.get("kind") in CONFIRMING and isinstance(entry.get("effect"), list):
            limit = _evidence(entry)
            if status(entry) != "candidate":
                limit = min(limit, BOOTSTRAP_SESSIONS)
            moved = max(0, min(limit, int(post.a - PRIOR.a + 1e-9), int(post.n)))
        if moved:
            entry["effect"] = Beta(post.a - moved, post.b, post.n - moved).to_list()
        entry["truth"] = moved


# --------------------------------------------------------------------------- selection
def withhold(session_id: str, fid: str, floor: float) -> bool:
    """Withhold this fact in this session? Probability ``floor``, seeded by (session, fact)."""
    if floor <= 0.0:
        return False
    raw = hashlib.blake2b(f"{session_id}|{fid}".encode(), digest_size=8).digest()
    return int.from_bytes(raw, "big") / 2.0**64 < floor


def _when(raw: Mapping[str, Any]) -> float:
    last = raw.get("last_seen")
    ok = isinstance(last, int | float) and not isinstance(last, bool) and math.isfinite(last)
    return float(last) if ok else 0.0  # type: ignore[arg-type]


def active(doc: Mapping[str, Any]) -> Iterator[tuple[str, Fact]]:
    """Active facts, most recent evidence first, each re-sanitised and id-verified lazily (the
    hook stops once it has enough)."""
    facts = doc.get("facts")
    ranked: list[tuple[float, float, str, dict[str, Any]]] = []
    for fid, raw in facts.items() if isinstance(facts, dict) else ():
        if (
            isinstance(fid, str)
            and isinstance(raw, dict)
            and status(raw) == "active"
            and current(raw)
        ):
            ranked.append((-_when(raw), -effect(raw).mean, fid, raw))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    for _, _, fid, raw in ranked:
        fact = miners.parse_entry(raw)
        if fact is not None and fact_id(fact) == fid:
            yield fid, fact


def current(raw: Mapping[str, Any]) -> bool:
    """False for a stored test_command whose value is not a runner invocation (a narrowed
    command stored before fix wave B: never offered until a fold re-normalises it) and for a
    generated_dir fact without a ``verified`` stamp (mined from edits alone: never offered; the
    next fold verifies or retires it)."""
    kind = raw.get("kind")
    if kind == "generated_dir":
        return raw.get("verified") is True
    if kind != "test_command":
        return True
    value = raw.get("value")
    return isinstance(value, str) and miners.test_invocation(value) == value


def recheck_order(doc: Mapping[str, Any]) -> dict[str, tuple[int, float]]:
    """Re-check priority of the stored generated_dir facts (lower first): unverified ones,
    then the longest since their last re-check (``checked_at``)."""
    facts = doc.get("facts")
    out: dict[str, tuple[int, float]] = {}
    for fid, raw in facts.items() if isinstance(facts, dict) else ():
        if isinstance(raw, dict) and raw.get("kind") == "generated_dir":
            at = raw.get("checked_at")
            ok = isinstance(at, int | float) and not isinstance(at, bool) and math.isfinite(at)
            out[fid] = (int(raw.get("verified") is True), float(at) if ok else 0.0)  # type: ignore[arg-type]
    return out


def select(
    doc: Mapping[str, Any], session_id: str, s: Settings
) -> tuple[list[tuple[str, Fact]], dict[str, float]]:
    """(facts to offer for injection, ranked; withheld fact ids -> withhold propensity). Only
    the ranked prefix that could be shown is considered: at most ``max_facts`` offered, and
    the facts withheld among them (a fact ranked below would not have been shown anyway)."""
    if doc.get("disabled") is True or s.max_facts <= 0:
        return [], {}
    offer: list[tuple[str, Fact]] = []
    held: dict[str, float] = {}
    for fid, fact in active(doc):
        if withhold(session_id, fid, s.withhold_floor):
            held[fid] = s.withhold_floor
        else:
            offer.append((fid, fact))
            if len(offer) >= s.max_facts:
                break
    return offer, held


# --------------------------------------------------------------------------- observation
def _words(cmd: str) -> list[str]:
    return [w for w in cmd.split(" ") if w]


def _starts(words: list[str], prefix: list[str]) -> bool:
    return bool(prefix) and words[: len(prefix)] == prefix


def _strip_ci(value: str) -> str:
    """A test command without its leading ``CI=true`` (``normalize_command`` drops it from the
    session's commands, so the value is compared without it)."""
    prefix = miners.CI_PREFIX + " "
    return value[len(prefix) :] if value.startswith(prefix) else value


def _base(value: str) -> str:
    """The runner row of a test command value: no ``CI=true``, no kept watch-mode flags."""
    kept = {"--", *miners.KEEP_FLAGS, *(f for fs in miners.KEEP_FLAGS_BY.values() for f in fs)}
    return " ".join(w for w in _words(_strip_ci(value)) if w not in kept)


def _missing(text: str) -> bool:
    """Not-found or usage error text (the value command no longer works as stated)."""
    err = miners.error_text(text)
    if any(p.search(err) for p in miners.NOT_FOUND) or miners.NO_MODULE.search(err):
        return True
    return _USAGE.search(err) is not None


NPM_TEST = miners.NPM_TEST
runner = miners.runner
_checker = miners.checker


@dataclass
class _State:
    relevant: bool = False
    waste: bool = False
    contradicted: bool = False
    unneeded: bool = False
    waste_event: bool = False
    pending: bool = False  # test_command: another runner failed; build_first: subject failed
    built: bool = False  # build_first: the build ran after the failure
    seen: bool = False  # build_first: the subject or the build already ran
    own_ok: bool = False  # test_command: the fact's own runner succeeded
    own_ran: bool = False  # test_command: the fact's own runner ran at all
    own_missing: bool = False  # test_command: the own runner failed with not-found / usage
    writes: int = 0  # generated_dir: writes under the directory
    files: list[str] | None = None  # generated_dir: edited project-relative files
    edited: bool = False  # generated_dir: a write happened (a later build regenerates)
    runs: dict[str, int] | None = None  # test_command: successful runs per normalised runner


class Observer:
    """Streams one session's main-chain tool calls and reports per fact (``results``).
    ``evidence`` as in ``miners``: ``"transcript"`` re-checks generated_dir facts only from the
    file heads the session showed (no git, no file access). ``hits``: the facts the last fed
    call bore on (ran, contradicted or showed the waste of; for replay attribution)."""

    def __init__(
        self,
        facts: Mapping[str, Fact],
        project_dir: str | None,
        order: Mapping[str, tuple[int, float]] | None = None,
        *,
        evidence: miners.Evidence = "full",
    ) -> None:
        self._facts = dict(facts)
        self._root = project_dir
        self._order = dict(order or {})
        self._state = {fid: _State() for fid in self._facts}
        self._exported: set[str] = set()
        self._evidence = miners.check_evidence(evidence)
        self._heads: dict[str, bool] = {}
        self._hits: set[str] = set()

    @property
    def hits(self) -> frozenset[str]:
        return frozenset(self._hits)

    def feed(self, call: ToolResultRecord) -> None:
        self._hits = set()
        if call.sidechain:
            return
        if call.tool_name == "Bash":
            cmd = call.input.get("command")
            if isinstance(cmd, str):
                self._bash(cmd, not call.is_error, call.text)
        elif call.tool_name in miners.EDIT_TOOLS:
            target = call.input.get("file_path") or call.input.get("notebook_path")
            if isinstance(target, str):
                self._edit(target)
        elif self._evidence == "transcript":
            seen = miners.read_head(call, self._root)
            if seen is not None and (seen[0] in self._heads or len(self._heads) < miners.MAX_KEYS):
                self._heads[seen[0]] = seen[1]

    def _edit(self, target: str) -> None:
        rel = miners.relative_path(target, self._root)
        if rel is None:
            return
        for fid, fact in self._facts.items():
            if fact.kind == "generated_dir" and rel.startswith(fact.value.rstrip("/") + "/"):
                st = self._state[fid]
                self._hits.add(fid)
                st.relevant, st.edited = True, True
                st.writes += 1
                st.files = st.files or []
                if rel not in st.files and len(st.files) < miners.GEN_MAX_FILES:
                    st.files.append(rel)

    def _bash(self, cmd: str, ok: bool, text: str) -> None:
        norm = normalize_command(cmd, project_dir=self._root)
        words = _words(norm)
        program = miners.program_of(norm)
        missing = not ok and _missing(text)
        family = command_family(cmd)
        for fid, fact in self._facts.items():
            st = self._state[fid]
            kind = fact.kind
            hit = False
            if kind == "command_alias":
                hit = self._alias(st, fact, words, ok, text, missing)
            elif kind == "test_command":
                if family == "test":
                    self._test(st, fact, norm, ok, missing)
                    hit = True
                core = _base(fact.value)
                inv = miners.test_invocation(norm)
                if missing and (
                    (inv is not None and _base(inv) == core)
                    or _starts(_words(_strip_ci(norm)), _words(core))
                ):
                    st.contradicted = hit = True
            elif kind == "generated_dir":
                if family == "build" and ok and st.edited and not _checker(norm):
                    st.waste_event = hit = True  # the edited files were regenerated
            elif kind == "requires_env":
                hit = self._env(st, fact, cmd, norm, program, ok, text)
            elif kind == "build_first":
                hit = self._build(st, fact, norm, program, ok, missing)
            if hit:
                self._hits.add(fid)
        for m in miners.EXPORT.finditer(cmd):
            self._exported.update(miners.ASSIGN.findall(m.group(1)))

    @staticmethod
    def _test(st: _State, fact: Fact, norm: str, ok: bool, missing: bool) -> None:
        """Relevant: any test-family command. Waste event: another runner failed, or any
        spelling failed with not-found / a usage error, before the fact's own runner
        succeeded (a red test run of the own runner is not one)."""
        st.relevant = True
        name = runner(norm)
        own = name == runner(fact.value)
        st.own_ran = st.own_ran or own
        if own and missing:
            st.own_missing = True
        if ok:
            st.runs = st.runs or {}
            st.runs[name] = st.runs.get(name, 0) + 1
            if own:
                st.own_ok = True
                if st.pending:
                    st.waste_event = True
        elif (missing or not own) and not st.own_ok:
            st.pending = True

    @staticmethod
    def _alias(
        st: _State, fact: Fact, words: list[str], ok: bool, text: str, missing: bool
    ) -> bool:
        subject, value = _words(fact.subject), _words(fact.value)
        prog = posixpath.basename(subject[-1])
        if _starts(words, value):
            st.relevant = True
            if missing:
                st.contradicted = True
            return True
        if _starts(words, subject) or (words and posixpath.basename(words[0]) == prog):
            st.relevant = True
            if not ok and miners.missing_program(words, miners.error_text(text)) is not None:
                st.waste = True
            elif ok and _starts(words, subject):
                st.unneeded = True  # the subject itself works now
            return True
        return False

    def _env(
        self, st: _State, fact: Fact, cmd: str, norm: str, program: str, ok: bool, text: str
    ) -> bool:
        named = not ok and fact.value in miners.env_names(miners.error_text(text))
        if named:
            st.waste = True
        if program != fact.subject and not _starts(_words(norm), _words(fact.subject)):
            return named
        st.relevant = True
        if ok and fact.value not in miners.set_vars(cmd) | self._exported:
            st.contradicted = True
        return True

    @staticmethod
    def _build(st: _State, fact: Fact, norm: str, program: str, ok: bool, missing: bool) -> bool:
        if program == fact.value or _starts(_words(norm), _words(fact.value)):
            st.seen = True
            if missing:
                st.contradicted = True
            if ok and st.pending:
                st.built = True
            return True
        if program != fact.subject:
            return False
        st.relevant = True
        if ok and not st.seen:
            st.unneeded = True  # the tests passed before any build
        st.seen = True
        if not ok:
            st.pending, st.built = True, False
        elif st.built:
            st.waste, st.pending, st.built = True, False, False
        return True

    def _rechecks(self) -> set[str]:
        """At most ``GEN_RECHECKS`` generated_dir facts per fold: edited this session first,
        then by ``recheck_order`` (unverified, least recently checked)."""
        gens = [f for f, fact in self._facts.items() if fact.kind == "generated_dir"]
        gens.sort(key=lambda f: (not self._state[f].edited, self._order.get(f, (0, 0.0)), f))
        return set(gens[:GEN_RECHECKS])

    def results(self) -> dict[str, Obs]:
        out: dict[str, Obs] = {}
        rechecks = self._rechecks()
        for fid, st in self._state.items():
            checked, generated = False, None
            fact = self._facts[fid]
            waste: bool | None = st.waste
            contradicted = st.contradicted
            if fact.kind == "test_command" and st.relevant:
                # Replaced: another runner succeeded while the fact's own runner never ran
                # (contradiction). Waste: the own runner failed with not-found / a usage error.
                # Success: the own runner succeeded. Anything else (a red phase: ordinary
                # test failures of the own runner) says nothing about the fact: neutral.
                mine = runner(fact.value)
                replaced = not st.own_ran and any(
                    n >= REPLACED_RUNS for r, n in (st.runs or {}).items() if r != mine
                )
                contradicted = contradicted or replaced
                waste = replaced or st.own_missing
                if not (waste or st.own_ok or contradicted):
                    out[fid] = Obs(waste_event=st.waste_event)  # neutral
                    continue
            elif fact.kind == "generated_dir":
                # Re-verified every fold it is in play (edited or not: an injected label stops
                # the edits that would expose it): git says tracked and a file lacks a marker.
                if fid in rechecks:
                    checked = True
                    generated = miners.generated_evidence(
                        self._root,
                        fact.value,
                        st.files or (),
                        evidence=self._evidence,
                        heads=self._heads,
                    )
                    contradicted = contradicted or generated is False
                if st.relevant:
                    repeated = st.writes >= miners.GEN_MIN_PER_SESSION
                    waste = True if repeated or st.waste_event else None  # one write: unknown
            out[fid] = Obs(
                relevant=st.relevant,
                waste=waste,
                contradicted=contradicted,
                unneeded=st.unneeded,
                waste_event=st.waste_event,
                checked=checked,
                generated=generated,
            )
        return out


# --------------------------------------------------------------------------- session log
DONE_KEY = "facts_done"  # session overlay: fact ids already judged by a fold of this session
MAX_LOGGED = 1000


def _probs(raw: object) -> dict[str, float]:
    out: dict[str, float] = {}
    for fid, p in raw.items() if isinstance(raw, dict) else ():
        if isinstance(fid, str) and isinstance(p, int | float) and not isinstance(p, bool):
            out[fid] = float(p)
    return out


def parse_log(raw: object) -> tuple[dict[str, float], dict[str, float]]:
    """(shown id -> injection propensity, withheld id -> withhold propensity) of a session log;
    an id both shown and withheld counts as shown."""
    if not isinstance(raw, dict):
        return {}, {}
    shown = _probs(raw.get("shown"))
    withheld = {k: v for k, v in _probs(raw.get("withheld")).items() if k not in shown}
    return shown, withheld


def log_decisions(
    state: dict[str, Any], shown: Mapping[str, float], withheld: Mapping[str, float]
) -> None:
    """SessionStart: add this start's decisions to the overlay's session log (in place)."""
    old_shown, old_held = parse_log(state.get(SESSION_KEY))
    new_shown = {**old_shown, **shown}
    new_held = {k: v for k, v in {**old_held, **withheld}.items() if k not in new_shown}
    state[SESSION_KEY] = {
        "shown": dict(list(new_shown.items())[:MAX_LOGGED]),
        "withheld": dict(list(new_held.items())[:MAX_LOGGED]),
    }


def take_log(state: dict[str, Any]) -> dict[str, Any] | None:
    """Fold staging: remove the session log from the overlay (in place) and return the part not
    yet judged by an earlier fold of this session (None when empty); those ids become done."""
    shown, withheld = parse_log(state.pop(SESSION_KEY, None))
    raw_done = state.get(DONE_KEY)
    done = {d for d in raw_done if isinstance(d, str)} if isinstance(raw_done, list) else set()
    shown = {k: v for k, v in shown.items() if k not in done}
    withheld = {k: v for k, v in withheld.items() if k not in done}
    if not shown and not withheld:
        return None
    state[DONE_KEY] = sorted(done | set(shown) | set(withheld))[:MAX_LOGGED]
    return {"shown": shown, "withheld": withheld}


def in_play(doc: Mapping[str, Any], ids: Collection[str]) -> dict[str, Fact]:
    """The stored (re-sanitised, id-verified) facts among ``ids``, plus the frequency-kind
    candidates with >= 2 sessions of evidence (their waste events gate activation)."""
    facts = doc.get("facts")
    out: dict[str, Fact] = {}
    watch = [
        fid
        for fid, raw in (facts.items() if isinstance(facts, dict) else ())
        if isinstance(raw, dict)
        and (
            (
                raw.get("kind") in FREQUENCY
                and status(raw) == "candidate"
                and _evidence(raw) >= miners.MIN_SESSIONS
            )
            or (  # unverified generated_dir: verified or retired by the re-check
                raw.get("kind") == "generated_dir"
                and status(raw) != "retired"
                and raw.get("verified") is not True
            )
        )
    ]
    for fid in dict.fromkeys([*ids, *watch]):
        raw = facts.get(fid) if isinstance(facts, dict) else None
        if not isinstance(raw, dict) or (raw.get("kind") == "test_command" and not current(raw)):
            continue
        fact = miners.parse_entry(raw)
        if fact is not None and fact_id(fact) == fid:
            out[fid] = fact
    return out


def restore_log(state: dict[str, Any], staged: object) -> None:
    """Undo ``take_log`` after a failed fold step (in place): the staged decisions go back into
    the overlay's log and leave the done list, so the next fold judges them."""
    shown, withheld = parse_log(staged)
    if not shown and not withheld:
        return
    log_decisions(state, shown, withheld)
    raw_done = state.get(DONE_KEY)
    if isinstance(raw_done, list):
        back = set(shown) | set(withheld)
        state[DONE_KEY] = [d for d in raw_done if d not in back]
