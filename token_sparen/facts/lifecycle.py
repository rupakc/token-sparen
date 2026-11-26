"""Fact lifecycle as a bandit (spec section 6.8): candidate -> active -> retired.

Each stored fact carries a Beta posterior ``effect`` over "injecting this fact prevents the
waste it targets, in sessions where it is relevant", a ``baseline`` Beta over "the waste is
absent when the fact is withheld", a ``status`` and a ``contradictions`` count (all in
``facts.json``, prior Beta(1, 1)).

**Session outcome** (applied once per fold, ``apply_session``). Only relevant sessions count:

=============  ============================  ============================================
kind           relevant when the session...  waste it targets
=============  ============================  ============================================
command_alias  ran the subject or the alias  ``command not found`` for the subject
test_command   ran any test-family command   a not-found/usage test-runner failure before
                                             a successful test run
requires_env   ran the subject program       an error naming the variable
build_first    ran the subject (test) prog.  a subject failure fixed by running the build
generated_dir  wrote under the directory     not detectable from the transcript: a
                                             relevant session rewards 0.5 (uninformative)
=============  ============================  ============================================

- injected & relevant: ``effect`` += 1 when the waste was absent, 0 when present;
- withheld & relevant (propensity floor ``[facts] withhold_floor``, decided per active fact
  at SessionStart, seeded by session + fact so a resume decides the same, logged in the
  session overlay with its propensity): ``baseline`` += the same reward; the effect estimate is
  ``effect.mean - baseline.mean``;
- a candidate that the miners re-derived this session (not injected) is a **confirmation**:
  ``effect`` += 1, for the waste -> fix kinds only (command_alias, requires_env, build_first;
  a test command or generated directory is a frequency, not a fix);
- a **contradiction** (counted only in sessions the fact was injected): command_alias /
  test_command / build_first - the fact's value command fails with not-found or a usage
  error; requires_env - the subject ran successfully without the variable being set by the
  command or an earlier ``export``. Reward 0.

**Transitions** (every fold, every fact):

- candidate -> active: >= 2 distinct sessions of evidence and either ``effect.lcb(0.1) >
  [facts] activate_lcb`` (0.55: Beta(1, 1) + 3 confirmations) or, the **bootstrap rule**,
  while no withheld baseline exists, >= 3 sessions of evidence (test_command and
  generated_dir have no confirmations; with confirmations both rules agree at 3 sessions);
- active -> retired: ``effect.ucb(0.9) < [facts] retire_ucb`` (0.4), or contradictions >= 1
  (requires_env, build_first: high stakes) / >= 2 (others). Retirement is final (``facts
  forget`` removes the fact; it is then never re-learned).
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
HIGH_STAKES = frozenset({"requires_env", "build_first"})
RETIRE_AFTER = {True: 1, False: 2}  # contradictions, by high stakes
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


def _evidence(entry: Mapping[str, Any]) -> int:
    raw = entry.get("sessions")
    return len({s for s in raw if isinstance(s, str)}) if isinstance(raw, list) else 0


def _observe(post: Beta, x: float) -> Beta:
    return post.update(x, gamma=1.0, cap=CAP, prior=PRIOR)


@dataclass(frozen=True)
class Obs:
    """One session's view of one fact. ``waste`` None = not detectable (reward 0.5)."""

    relevant: bool = False
    waste: bool | None = False
    contradicted: bool = False

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
) -> None:
    """Apply one session's outcome to the stored facts (in place), then the transitions."""
    for fid, entry in table.items():
        if not isinstance(entry, dict):
            continue
        o = obs.get(fid, Obs())
        st = status(entry)
        if fid in shown:
            if o.contradicted:
                entry["contradictions"] = _count(entry, "contradictions") + 1
                entry["effect"] = _observe(effect(entry), 0.0).to_list()
            elif o.relevant:
                entry["effect"] = _observe(effect(entry), o.reward).to_list()
        elif fid in withheld:
            if o.relevant:
                entry["baseline"] = _observe(baseline(entry), o.reward).to_list()
        elif st == "candidate" and fid in mined and entry.get("kind") in CONFIRMING:
            entry["effect"] = _observe(effect(entry), 1.0).to_list()
        entry["status"] = _transition(entry, st, settings)


def _transition(entry: Mapping[str, Any], st: Status, s: Settings) -> Status:
    if st == "retired":
        return st
    post = effect(entry)
    limit = RETIRE_AFTER[entry.get("kind") in HIGH_STAKES]
    if _count(entry, "contradictions") >= limit:
        return "retired"
    if st == "active":
        return "retired" if post.ucb(RETIRE_Q) < s.retire_ucb else "active"
    n = _evidence(entry)
    if n < miners.MIN_SESSIONS:
        return "candidate"
    if baseline(entry).n == 0 and n >= BOOTSTRAP_SESSIONS:
        return "active"
    return "active" if post.lcb(ACTIVATE_Q) > s.activate_lcb else "candidate"


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
        if isinstance(fid, str) and isinstance(raw, dict) and status(raw) == "active":
            ranked.append((-_when(raw), -effect(raw).mean, fid, raw))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    for _, _, fid, raw in ranked:
        fact = miners.parse_entry(raw)
        if fact is not None and fact_id(fact) == fid:
            yield fid, fact


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


def _missing(text: str) -> bool:
    """Not-found or usage error text (the value command no longer works as stated)."""
    err = miners.error_text(text)
    if any(p.search(err) for p in miners.NOT_FOUND) or miners.NO_MODULE.search(err):
        return True
    return _USAGE.search(err) is not None


@dataclass
class _State:
    relevant: bool = False
    waste: bool = False
    contradicted: bool = False
    pending: bool = False  # test_command: a not-found runner failure seen; build_first: failed
    built: bool = False  # build_first: the build ran after the failure


class Observer:
    """Streams one session's main-chain tool calls and reports per fact (``results``)."""

    def __init__(self, facts: Mapping[str, Fact], project_dir: str | None) -> None:
        self._facts = dict(facts)
        self._root = project_dir
        self._state = {fid: _State() for fid in self._facts}
        self._exported: set[str] = set()

    def feed(self, call: ToolResultRecord) -> None:
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

    def _edit(self, target: str) -> None:
        rel = miners.relative_path(target, self._root)
        if rel is None:
            return
        for fid, fact in self._facts.items():
            if fact.kind == "generated_dir" and rel.startswith(fact.value.rstrip("/") + "/"):
                self._state[fid].relevant = True

    def _bash(self, cmd: str, ok: bool, text: str) -> None:
        norm = normalize_command(cmd, project_dir=self._root)
        words = _words(norm)
        program = miners.program_of(norm)
        missing = not ok and _missing(text)
        for fid, fact in self._facts.items():
            st = self._state[fid]
            kind = fact.kind
            if kind == "command_alias":
                self._alias(st, fact, words, ok, text, missing)
            elif kind == "test_command":
                if command_family(cmd) == "test":
                    st.relevant = True
                    if missing:
                        st.pending = True
                    elif ok and st.pending:
                        st.waste = True
                if missing and _starts(words, _words(fact.value)):
                    st.contradicted = True
            elif kind == "requires_env":
                self._env(st, fact, cmd, norm, program, ok, text)
            elif kind == "build_first":
                self._build(st, fact, norm, program, ok, missing)
        for m in miners.EXPORT.finditer(cmd):
            self._exported.update(miners.ASSIGN.findall(m.group(1)))

    @staticmethod
    def _alias(
        st: _State, fact: Fact, words: list[str], ok: bool, text: str, missing: bool
    ) -> None:
        subject, value = _words(fact.subject), _words(fact.value)
        prog = posixpath.basename(subject[-1])
        if _starts(words, value):
            st.relevant = True
            if missing:
                st.contradicted = True
            return
        if _starts(words, subject) or (words and posixpath.basename(words[0]) == prog):
            st.relevant = True
            if not ok and miners.missing_program(words, miners.error_text(text)) is not None:
                st.waste = True

    def _env(
        self, st: _State, fact: Fact, cmd: str, norm: str, program: str, ok: bool, text: str
    ) -> None:
        if fact.value in miners.env_names(miners.error_text(text)) and not ok:
            st.waste = True
        if program != fact.subject and not _starts(_words(norm), _words(fact.subject)):
            return
        st.relevant = True
        if ok and fact.value not in miners.set_vars(cmd) | self._exported:
            st.contradicted = True

    @staticmethod
    def _build(st: _State, fact: Fact, norm: str, program: str, ok: bool, missing: bool) -> None:
        if program == fact.value or _starts(_words(norm), _words(fact.value)):
            if missing:
                st.contradicted = True
            if ok and st.pending:
                st.built = True
            return
        if program != fact.subject:
            return
        st.relevant = True
        if not ok:
            st.pending, st.built = True, False
        elif st.built:
            st.waste, st.pending, st.built = True, False, False

    def results(self) -> dict[str, Obs]:
        out: dict[str, Obs] = {}
        for fid, st in self._state.items():
            undetectable = self._facts[fid].kind == "generated_dir"
            waste: bool | None = None if undetectable and st.relevant else st.waste
            out[fid] = Obs(relevant=st.relevant, waste=waste, contradicted=st.contradicted)
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
    """The stored (re-sanitised, id-verified) facts among ``ids``."""
    facts = doc.get("facts")
    out: dict[str, Fact] = {}
    for fid in ids:
        raw = facts.get(fid) if isinstance(facts, dict) else None
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
