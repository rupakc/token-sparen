"""Failure-loop guard (spec 6.11; decision point ``loopguard``, tier medium).

Within one turn (between two UserPromptSubmit events), separately for every agent of the turn
(``agent_id``; the main agent is ``""``), the guard watches two deterministic signals:

* **a repeated error**: the same ``fingerprint`` of a failed tool call (PostToolUseFailure)
  ``k`` times, where ``k`` is the learned arm (``patient`` 5, ``standard`` 3, ``eager`` 2; never
  below ``[loopguard] min_k``). The fingerprint mixes the tool name, its target (for Bash the
  normalised command head, for file tools the file path) and the most specific error line. A
  failure without an informative error line (``Exit code 1`` and nothing that looks like an
  error) has no fingerprint and is never counted;
* **edit oscillation**: the same edit made and undone repeatedly on one file region: an Edit
  pair (old, new) whose reverse was the region's previous edit, or a Write switching back and
  forth between two contents. One add-then-remove is one reversal and never counts; A -> B ->
  A -> B is two. It triggers at ``max(2, k - 1)`` reversals.

The arm is decided once per agent and turn, at the first repeat (an error's second occurrence or
an edit's second reversal), and the decision is logged then, for every arm, whether or not its
``k`` is reached. The first trigger adds one short redirect as ``additionalContext`` to the
triggering tool result (the tool output itself is never touched); a second detection of that
agent in the same turn (counted on events after the redirect) tells the user only, at most once
per turn.

Reward (counterfactual, review M1): a redirect delivered while its fingerprint had >= 3
occurrences is "broken" (1) if the fingerprint does not recur within that agent's next
``[loopguard] watch_calls`` (5) tool calls of the turn (the turn ending first counts as not
recurring), else 0; its savings are ``max(0, broken - p0)``, where ``p0`` is the natural break
rate: how often a loop ends by itself. Every other decision (a redirect at fewer occurrences, a
decision whose ``k`` was never reached) gets savings 0. ``p0`` is observed on turns where an
error fingerprint reached 3 occurrences with no redirect for that agent (an arm whose ``k`` was
not reached yet: patient at 3-4): the same 5-call window decides broken / not broken, once per
agent and turn, into the overlay's ``natural`` evidence; the fold merges it into the project and
(weighted) global stores. ``p0`` is the mean of ``Beta(8, 2)`` (prior mean 0.8, strength 10:
pessimistic until data) plus the project's observations (this session's included), joined by
the global ones while the project has fewer than ``P0_MIN_OBS``. Regret 1 if the user interrupts
a tool call after the redirect, or the user's next prompt starts with stop / wait / undo /
revert / that's wrong / no, don't, or is at most ``STOP_MAX_WORDS`` (8) words and contains one
(whole words, any case); else an explicit 0 at the next prompt or SessionEnd (always 0 without
a redirect). An error-loop trigger also tells the regret detector (``on_loop``): this turn's
compression decisions on error-bearing outputs get 0.5 regret.

State is a bounded per-turn dict in the session overlay (``loopguard``: at most ``MAX_AGENTS``
agents, at most ``MAX_FINGERPRINTS`` fingerprints and as many files in total across them,
``MAX_PAIRS`` edit pairs per file; hashes only, about 15 KB at worst); each event costs at most
one overlay read and one or two updates, independent of the session length.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen.compress.reports import failing_tests
from token_sparen.config import Config, flag, flag_on, log_error, state_home
from token_sparen.emit import LEDGER_FIELD
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn import decisions, features, pending, regret
from token_sparen.learn.beta import Evidence
from token_sparen.learn.hierarchy import NATURAL_KEY, POSTERIORS_FILE, natural_evidence
from token_sparen.learn.policy import LOOPGUARD, Decision, DecisionRecord, Policy
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, paths, session

__all__ = [
    "ARM_K",
    "LOOPGUARD",
    "MAX_AGENTS",
    "MAX_FINGERPRINTS",
    "P0_MIN_OBS",
    "P0_PRIOR_MEAN",
    "P0_PRIOR_STRENGTH",
    "STATE_KEY",
    "command_head",
    "fingerprint",
    "is_stop",
    "natural_rate",
    "on_prompt",
    "on_session_end",
    "on_tool",
]

STATE_KEY = "loopguard"
ARM_K: dict[str, int] = {"patient": 5, "standard": 3, "eager": 2}
MAX_AGENTS = 16  # agents (main + subagents) tracked per turn; later ones are not counted
MAX_FINGERPRINTS = 50  # fingerprints, and files watched for oscillation, per turn (all agents)
MAX_PAIRS = 4  # edit pairs kept per file
MAX_AFTER = 10  # fingerprints counted after an agent's redirect (for the user notice)
MIN_REVERSALS = 2  # A -> B -> A -> B: one add-then-remove is never oscillation
SAVINGS_MIN_N = 3  # a redirect earns savings only if the loop had this many occurrences
P0_PRIOR_MEAN = 0.8  # natural break rate prior: pessimistic for the redirect until data
P0_PRIOR_STRENGTH = 10.0
P0_MIN_OBS = 5.0  # project observations before the global ones stop joining
P0_CAP = 50.0  # session natural evidence mass cap (as the engine's posterior_cap)
STOP_MAX_WORDS = 8  # a prompt this short counts a stop phrase anywhere, a longer one at its start
SCAN_HEAD = 8000  # characters of an error text scanned (head + tail; exception types end a trace)
SCAN_TAIL = 4000
MAX_LINE = 300
DEFAULT_MIN_K = 2
DEFAULT_WATCH = 5
MAIN_AGENT = ""
NUDGE = (
    "[token-sparen: this error has repeated {n}× this turn ({fp}). Consider stepping back: "
    "re-read the error, check assumptions, or try a different approach.]"
)
OSC_NUDGE = (
    "[token-sparen: the same edit has been made and undone {n}× this turn ({fp}). Consider "
    "stepping back: re-read the goal, check assumptions, or try a different approach.]"
)
USER_NOTICE = (
    "token-sparen: the agent is repeating the same error ({n}×); you may want to intervene."
)
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "Read", "NotebookEdit", "NotebookRead"})
_POST = frozenset({"post_tool", "post_tool_failure"})

# --- fingerprint --------------------------------------------------------------------------------

_TAGS = re.compile(r"</?[a-z_]+>")
_BOILER = re.compile(
    r"^(?:exit code \d+|traceback \(most recent call last\):?|error: command failed"
    r"|command failed with exit code \d+)$",
    re.IGNORECASE,
)
# Test-run summaries ("3 failed, 2 passed", "FAILED (failures=2)", "Tests: 2 failed") stay the
# same while a suite is fixed one test at a time: never an error line.
_SUMMARY = re.compile(
    r"^(?:=+\s*)?\d+ (?:failed|passed|errors?|skipped)\b"
    r"|^FAILED \((?:failures|errors)=\d+"
    r"|^(?:Tests|Test Suites|Snapshots):\s"
    r"|^test result: "
    r"|^(?:FAIL|ok|PASS)(?:\t.*)?$",
    re.IGNORECASE,
)
_EXC = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\b")
_EXC_MSG = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*:\s*\S")
_DETAIL = re.compile(r"^E\s{2,}\S")  # pytest's assertion detail
_CODED = re.compile(r"\berror(?:\s*\[?[A-Z]+\d+\]?)?\s*:|\berror\s+[A-Z]+\d+", re.IGNORECASE)
_KEYWORD = re.compile(
    r"\b(?:errors?|exception|fail(?:ed|ure|s)?|fatal|panic(?:ked)?|denied|not found|no such"
    r"|cannot|can't|unable|invalid|refused|timed out|err!|undefined|unresolved)\b",
    re.IGNORECASE,
)
_PATH = re.compile(r"(?:[a-z]:)?[\w.~-]*(?:[/\\][\w.~-]+)+")
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b")
_LINE_REF = re.compile(r":\d+(?::\d+)?\b|\bline \d+|\(\d+,\s*\d+\)")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min)\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b")
_LONG_NUM = re.compile(r"\d{5,}")
_SPACE = re.compile(r"\s+")
_STOP_PHRASE = r"(?:stop|wait|undo|revert|that's wrong|no,?\s+don't)\b"
_STOP_START = re.compile(rf"^\W*{_STOP_PHRASE}", re.IGNORECASE)
_STOP_ANY = re.compile(rf"\b{_STOP_PHRASE}", re.IGNORECASE)
_LEAD_CD = re.compile(r"^cd\s+\S+\s*&&\s*")
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9_-]*$")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:10]


def _basename(match: re.Match[str]) -> str:
    return match.group(0).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _normalise(line: str) -> str:
    """Drop what changes between repeats of one error (directories, line:col, hex ids,
    durations, clock times, long numbers); keep what identifies it (names, quoted values,
    literals, the file's base name)."""
    s = line.lower()
    s = _PATH.sub(_basename, s)
    s = _HEX.sub("<h>", s)
    s = _DURATION.sub("<t>", s)
    s = _CLOCK.sub("<t>", s)
    s = _LINE_REF.sub(":<n>", s)
    s = _LONG_NUM.sub("<n>", s)
    return _SPACE.sub(" ", s).strip()[:MAX_LINE]


def _error_line(lines: list[str]) -> str | None:
    """The most specific error line: an exception with its message (last), a pytest detail
    line (first), a coded compiler/tool error (first), else the first line naming an error."""
    for ln in reversed(lines):
        if _EXC_MSG.search(ln):
            return ln
    for pattern in (_DETAIL, _CODED, _EXC, _KEYWORD):
        for ln in lines:
            if pattern.search(ln):
                return ln
    return None


def _informative(raw: list[str]) -> list[str]:
    """Lines that can identify an error: boilerplate and test-run summaries removed."""
    out = []
    for line in raw:
        ln = line.strip()
        if ln and not _BOILER.match(ln) and not _SUMMARY.match(ln):
            out.append(line.rstrip())
    return out


def _last_exception(lines: list[str]) -> str:
    for ln in reversed(lines):
        found = _EXC.findall(ln)
        if found:
            return str(found[-1]).lower()
    return ""


def fingerprint(text: str, *, tool: str = "", target: str = "") -> str | None:
    """Ten hex characters identifying a failure, or None when the text has no informative error
    line (boilerplate such as ``Exit code 1``, ``<tool_use_error>`` tags and test-run summary
    lines is skipped).

    A pytest / jest / go test / cargo test report (``compress.reports.failing_tests``) is keyed
    by the number of failing test ids and a hash of their whole sorted set (within the scanned
    text) plus the first failure's error line, so fixing a suite one test at a time changes the
    fingerprint. Anything else is keyed by
    the last exception type named and the most specific error line. Both also mix in the tool
    name and its ``target`` (Bash: ``command_head``; file tools: the path) and are normalised by
    ``_normalise``. Only the first ``SCAN_HEAD`` and last ``SCAN_TAIL`` characters are read.
    Never raises."""
    if len(text) > SCAN_HEAD + SCAN_TAIL:
        text = f"{text[:SCAN_HEAD]}\n{text[-SCAN_TAIL:]}"
    raw = _TAGS.sub("\n", text).splitlines()
    report = failing_tests(raw)
    if report is not None and report[0]:
        ids, block = report
        body = _informative(block[1:]) or _informative(block[:1])
        err = _error_line(body) or ""
        unique = sorted(set(ids))
        key = f"{len(unique)}:{_sha(chr(10).join(unique))}"  # every id counts, none is stored
        return _sha(f"{tool}|{target}|tests|{key}|{_last_exception(body)}|{_normalise(err)}")
    lines = _informative(raw)
    line = _error_line(lines)
    if line is None:
        return None
    return _sha(f"{tool}|{target}|{_last_exception(lines)}|{_normalise(line)}")


def command_head(command: str) -> str:
    """The program of a shell command (base name) plus its first plain-word argument (a
    subcommand such as ``git diff``, ``npm test``), after the normalisation of the regret
    detector's command signature (assignments, ``env``, leading ``cd``s dropped)."""
    text = regret.normalize_command(command)
    while (m := _LEAD_CD.match(text)) is not None:
        text = text[m.end() :]
    words = text.split()
    if not words:
        return ""
    head = posixpath.basename(words[0])
    for word in words[1:]:
        if word.startswith("-"):
            continue
        return f"{head} {word}" if _SUBCOMMAND.match(word) else head
    return head


def _target(event: Event) -> str:
    tool = event.tool
    if tool is None:
        return ""
    if tool.name == "Bash":
        cmd = tool.input.get("command")
        return command_head(cmd) if isinstance(cmd, str) else ""
    if tool.name in FILE_TOOLS:
        path = tool.input.get("file_path") or tool.input.get("notebook_path")
        return path if isinstance(path, str) else ""
    return ""


def is_stop(prompt: str | None) -> bool:
    """Does the user's prompt start with stop / wait / undo / revert / that's wrong / no, don't,
    or contain one while being at most ``STOP_MAX_WORDS`` words ("add a stop button to ..." does
    not count)?"""
    if not prompt:
        return False
    text = str(prompt).replace("’", "'").strip()
    if _STOP_START.match(text):
        return True
    return len(text.split()) <= STOP_MAX_WORDS and _STOP_ANY.search(text) is not None


# --- config -------------------------------------------------------------------------------------


def _enabled(cfg: Config) -> bool:
    return flag(cfg.get("loopguard.enabled"), True)


def _param(cfg: Config, key: str, default: int) -> int:
    value = cfg.get(f"loopguard.{key}")
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 1:
        return int(value)
    return default


def _k(cfg: Config, arm: object) -> int:
    base = ARM_K.get(str(arm), ARM_K[LOOPGUARD.safe_arm])
    return max(base, _param(cfg, "min_k", DEFAULT_MIN_K))


# --- per-turn state -----------------------------------------------------------------------------

# (decision id, savings, regret, counterfactual): a counterfactual savings value is "broken"
# (0 / 1) and is paid as max(0, broken - p0)
Payout = tuple[str, float | None, float | None, bool]


@dataclass
class _Step:
    """What one state update decided (applied outside the lock)."""

    payouts: list[Payout] = field(default_factory=list)
    outcomes: list[list[Any]] = field(default_factory=list)  # regret rows for the decision log
    need_decision: bool = False
    decided: Decision | None = None  # adopted by this hook (logged by it)
    nudge: str | None = None
    error_loop: bool = False
    notice: str | None = None
    natural: list[float] = field(default_factory=list)  # natural break observations (0 / 1)
    session_natural: Evidence | None = None  # the overlay's natural evidence after this update
    indexed: bool = False  # natural evidence was added: the overlay awaits a fold


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _agent_id(event: Event) -> str:
    value = event.extra.get("agent_id")
    return value if isinstance(value, str) else MAIN_AGENT


def _settle_agent(ag: dict[str, Any], step: _Step, signal: str, regret_value: float) -> None:
    """Close an agent's turn: an open watch settles savings (1 only if eligible), a decision
    without a redirect gets savings 0; regret is paid (0 without a redirect)."""
    if isinstance(ag.get("natural"), dict):
        step.natural.append(1.0)  # the turn ended first: the loop ended by itself
        ag["natural"] = None
    dec = ag.get("decision")
    if not isinstance(dec, list) or len(dec) < 4 or not isinstance(dec[0], str):
        return
    did = dec[0]
    watch = ag.get("watch")
    if isinstance(watch, dict):
        eligible = bool(watch.get("eligible"))
        step.payouts.append((did, 1.0 if eligible else 0.0, None, eligible))
        ag["watch"] = None
    elif not ag.get("nudge") and not ag.get("savings_paid"):
        step.payouts.append((did, 0.0, None, False))
    ag["savings_paid"] = True
    if not ag.get("regret_paid"):
        ag["regret_paid"] = True
        value = regret_value if ag.get("nudge") else 0.0
        reason = signal if ag.get("nudge") else "no_redirect"
        step.payouts.append((did, None, value, False))
        step.outcomes.append([did, LOOPGUARD.name, str(dec[1]), "", value, dec[3], reason])


def _settle(lg: dict[str, Any], step: _Step, signal: str, regret_value: float) -> None:
    for ag in _dict(lg.get("agents")).values():
        if isinstance(ag, dict):
            _settle_agent(ag, step, signal, regret_value)


def _state(state: dict[str, Any], turn: int, step: _Step) -> dict[str, Any]:
    """This turn's loop-guard state; a stale one (no prompt hook ran) is settled first."""
    lg = state.get(STATE_KEY)
    if isinstance(lg, dict) and lg.get("turn") == turn and isinstance(lg.get("agents"), dict):
        return lg
    if isinstance(lg, dict):
        _settle(lg, step, "expiry", 0.0)
    lg = {"turn": turn, "agents": {}}
    state[STATE_KEY] = lg
    return lg


def _agent(lg: dict[str, Any], agent: str) -> dict[str, Any] | None:
    agents = _dict(lg.get("agents"))
    lg["agents"] = agents
    ag = agents.get(agent)
    if isinstance(ag, dict):
        return ag
    if len(agents) >= MAX_AGENTS:
        return None
    ag = {"fps": {}, "files": {}}
    agents[agent] = ag
    return ag


def _bounded_add(table: dict[str, Any], key: str, value: Any, room: bool) -> bool:
    """Set ``table[key]`` if it is already there or there is ``room`` for a new key."""
    if key not in table and not room:
        return False
    table[key] = value
    return True


def _total(lg: dict[str, Any], name: str) -> int:
    """Entries of ``name`` (``fps`` / ``files``) across all agents of the turn (<= 16)."""
    return sum(len(_dict(ag.get(name))) for ag in _dict(lg.get("agents")).values())


def _transitions(event: Event) -> tuple[str, list[tuple[str, str]], str | None]:
    """``(path, [(from, to)], write)`` of an Edit/MultiEdit/Write (hashes); ``write`` is the new
    content hash of a Write (its ``from`` is the file's last written content)."""
    tool = event.tool
    if tool is None or tool.name not in EDIT_TOOLS:
        return "", [], None
    path = tool.input.get("file_path")
    if not isinstance(path, str) or not path:
        return "", [], None
    if tool.name == "Write":
        content = tool.input.get("content")
        return (path, [], _sha(content)) if isinstance(content, str) else ("", [], None)
    edits = tool.input.get("edits") if tool.name == "MultiEdit" else [tool.input]
    out: list[tuple[str, str]] = []
    for e in edits if isinstance(edits, list) else []:
        old, new = _dict(e).get("old_string"), _dict(e).get("new_string")
        if isinstance(old, str) and isinstance(new, str) and old != new:
            out.append((_sha(old), _sha(new)))
    return path, out, None


def _oscillation(
    lg: dict[str, Any],
    ag: dict[str, Any],
    path: str,
    moves: list[tuple[str, str]],
    write: str | None,
) -> tuple[str, int] | None:
    """Record the edits of one file; ``(fingerprint, reversals)`` of its most oscillating pair
    once it has been reversed ``MIN_REVERSALS`` times. With ``MAX_FINGERPRINTS`` files tracked in
    the turn, a new file evicts this agent's oldest one (or is not tracked if it has none)."""
    files = _dict(ag.get("files"))
    ag["files"] = files
    key = _sha(f"file|{path}")
    entry = files.pop(key, None)
    if not isinstance(entry, dict):
        if _total(lg, "files") >= MAX_FINGERPRINTS:
            if not files:
                return None
            del files[next(iter(files))]  # most recent last: the oldest file is evicted first
        entry = {"last": "", "pairs": {}}
    files[key] = entry
    if write is not None:
        last = entry.get("last") if isinstance(entry.get("last"), str) else ""
        entry["last"] = write
        moves = [] if last == write else [(str(last), write)]
    pairs = _dict(entry.get("pairs"))
    entry["pairs"] = pairs
    best: tuple[str, int] | None = None
    for a, b in moves:
        pk = "|".join(sorted((a, b)))
        cur = pairs.pop(pk, None)
        direction, reversals = (cur[0], _num(cur[1])) if isinstance(cur, list) else ("", 0)
        if direction == f"{b}>{a}":
            reversals += 1
        pairs[pk] = [f"{a}>{b}", reversals]
        while len(pairs) > MAX_PAIRS:
            del pairs[next(iter(pairs))]
        if reversals >= MIN_REVERSALS and (best is None or reversals > best[1]):
            best = (_sha(f"revert|{path}|{pk}"), reversals)
    return best


def _evaluate(ag: dict[str, Any], hit: _Hit, cfg: Config, step: _Step, lg: dict[str, Any]) -> None:
    """Apply the agent's arm to this event: the first trigger becomes the redirect; a later
    detection (counted on events after the redirect) becomes the user notice."""
    dec = ag.get("decision")
    if not isinstance(dec, list) or len(dec) < 4 or hit.fp is None:
        return
    k = _k(cfg, dec[1])
    need = max(MIN_REVERSALS, k - 1) if hit.osc else k
    nudge = ag.get("nudge")
    if isinstance(nudge, dict):
        after = _dict(ag.get("after"))
        ag["after"] = after
        repeat = _num(after.get(hit.fp)) + 1
        _bounded_add(after, hit.fp, repeat, len(after) < MAX_AFTER)
        repeat += 1 if hit.fp == nudge.get("fp") else 0
        if repeat >= need and not lg.get("warned"):
            lg["warned"] = True
            step.notice = USER_NOTICE.format(n=hit.n)
        return
    if hit.n < need:
        return
    occurrences = hit.n + 1 if hit.osc else hit.n
    ag["nudge"] = {"fp": hit.fp, "n": hit.n}
    ag["natural"] = None  # a redirect now shapes what follows: no longer a natural observation
    ag["watch"] = {
        "fp": hit.fp,
        "left": _param(cfg, "watch_calls", DEFAULT_WATCH),
        "eligible": occurrences >= SAVINGS_MIN_N,
    }
    step.nudge = (OSC_NUDGE if hit.osc else NUDGE).format(n=hit.n, fp=hit.fp)
    step.error_loop = not hit.osc


def _watch(ag: dict[str, Any], fp: str | None, step: _Step) -> None:
    """One tool call of this agent after the redirect: a recurrence settles savings 0; the end
    of the window without one settles savings 1 (when the redirect was eligible)."""
    watch, dec = ag.get("watch"), ag.get("decision")
    if not isinstance(watch, dict) or not isinstance(dec, list) or not dec:
        return
    eligible = bool(watch.get("eligible"))
    if fp is not None and fp == watch.get("fp"):
        step.payouts.append((str(dec[0]), 0.0, None, eligible))
        ag["watch"], ag["savings_paid"] = None, True
        return
    left = _num(watch.get("left")) - 1
    if left <= 0:
        step.payouts.append((str(dec[0]), 1.0 if eligible else 0.0, None, eligible))
        ag["watch"], ag["savings_paid"] = None, True
    else:
        watch["left"] = left


def _natural_watch(ag: dict[str, Any], fp: str | None, step: _Step) -> None:
    """One tool call of this agent while its loop is watched without a redirect: a recurrence
    is "not broken" (0), the end of the window without one "broken" (1)."""
    nat = ag.get("natural")
    if not isinstance(nat, dict):
        return
    if fp is not None and fp == nat.get("fp"):
        step.natural.append(0.0)
        ag["natural"] = None
        return
    left = _num(nat.get("left")) - 1
    if left <= 0:
        step.natural.append(1.0)
        ag["natural"] = None
    else:
        nat["left"] = left


def _natural_open(ag: dict[str, Any], hit: _Hit, cfg: Config) -> None:
    """An error fingerprint at >= ``SAVINGS_MIN_N`` occurrences with no redirect for this agent
    (its arm's ``k`` not reached): watch it once per agent and turn for the natural break rate."""
    if hit.osc or hit.fp is None or hit.n < SAVINGS_MIN_N or ag.get("nudge"):
        return
    if ag.get("natural_seen"):
        return
    ag["natural_seen"] = True
    ag["natural"] = {"fp": hit.fp, "left": _param(cfg, "watch_calls", DEFAULT_WATCH)}


@dataclass
class _Hit:
    fp: str | None = None
    n: int = 0  # occurrences (error) or reversals (oscillation)
    osc: bool = False


# --- effects outside the lock -------------------------------------------------------------------


def natural_rate(
    project_key: str, env: Mapping[str, str], session_ev: Evidence | None = None
) -> float:
    """``p0``: the posterior mean natural break rate of a loop (no redirect). Prior
    ``Beta(P0_PRIOR_MEAN * P0_PRIOR_STRENGTH, ...)`` plus the project's evidence and this
    session's (not yet folded); while those are fewer than ``P0_MIN_OBS`` observations the
    global evidence joins them. Unreadable stores count as empty."""
    home = state_home(env)
    local = natural_evidence(
        jsonstore.read(paths.project_dir_for(home, project_key) / POSTERIORS_FILE, {}, env),
        LOOPGUARD.name,
    )
    if session_ev is not None:
        local = local + session_ev
    data = local
    if local.n < P0_MIN_OBS:
        glob = jsonstore.read(home / "global" / POSTERIORS_FILE, {}, env)
        data = local + natural_evidence(glob, LOOPGUARD.name)
    a = P0_PRIOR_MEAN * P0_PRIOR_STRENGTH + max(0.0, data.ea)
    b = (1.0 - P0_PRIOR_MEAN) * P0_PRIOR_STRENGTH + max(0.0, data.eb)
    return a / (a + b)


def _savings(step: _Step, event: Event, env: Mapping[str, str]) -> list[Payout]:
    """Counterfactual savings: ``max(0, broken - p0)`` (``p0`` read only when needed)."""
    out: list[Payout] = []
    p0: float | None = None
    for did, savings, regret_value, cf in step.payouts:
        if cf and savings is not None and savings > 0.0:
            if p0 is None:
                try:
                    p0 = natural_rate(event.project_key, env, step.session_natural)
                except (OSError, ValueError) as exc:
                    log_error(env, "loopguard", f"p0: {type(exc).__name__}: {exc}")
                    p0 = 1.0  # unknown: no credit (intervene less when unsure)
            savings = max(0.0, savings - p0)
        out.append((did, savings, regret_value, False))
    return out


def _pay(policy: object, step: _Step, event: Event, cfg: Config, env: Mapping[str, str]) -> None:
    if step.indexed:
        pending.add_overlay(tracking.overlay_path(event, env), env)
    fn: Callable[[str, float | None, float | None], None] | None = regret.reward_fn(policy)
    if fn is not None:
        for did, savings, regret_value, _ in _savings(step, event, env):
            fn(did, savings, regret_value)
    if step.outcomes:
        now = time.time()
        recs = [decisions.outcome(r[0], float(r[4]), str(r[6]), now) for r in step.outcomes]
        pdir = paths.project_dir_for(state_home(env), event.project_key)
        decisions.append_outcomes(pdir, recs, cfg, env, "loopguard")


def _record(state: dict[str, Any], step: _Step) -> None:
    """Settled regrets also go to the overlay's ``regret_outcomes`` (fold: circuit breaker);
    natural break observations to its ``natural`` evidence (fold: project and global p0)."""
    rows = step.outcomes
    if rows:
        outs = state.get(regret.OUTCOMES_KEY)
        outs = [*(outs if isinstance(outs, list) else []), *rows]
        state[regret.OUTCOMES_KEY] = outs[-regret.MAX_OUTCOMES :]
    ev = natural_evidence(state, LOOPGUARD.name)
    if step.natural:
        for x in step.natural:
            ev = ev.observe(x, gamma=1.0, cap=P0_CAP)
        table = state.get(NATURAL_KEY)
        table = table if isinstance(table, dict) else {}
        table[LOOPGUARD.name] = ev.to_list()
        state[NATURAL_KEY] = table
        step.natural = []
        step.indexed = True
    step.session_natural = ev


def _update(path: Path, env: Mapping[str, str], fn: Callable[[dict[str, Any]], None]) -> bool:
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        fn(state)
        return state

    try:
        jsonstore.update(path, apply, {}, env)
    except (jsonstore.StoreBusy, OSError) as exc:  # best effort: no redirect without state
        log_error(env, "loopguard", f"{type(exc).__name__}: {exc}")
        return False
    return True


def _account(state: dict[str, Any], step: _Step) -> None:
    """The redirect costs the user tokens: add it to the injected-tokens ledger."""
    state[LEDGER_FIELD] = _num(state.get(LEDGER_FIELD)) + estimate_tokens(step.nudge or "")


def _log_decision(event: Event, decision: Decision, cfg: Config, env: Mapping[str, str]) -> None:
    tool = event.tool
    rec = DecisionRecord(
        decision_id=decision.decision_id,
        point=LOOPGUARD.name,
        arm=decision.arm,
        propensity=decision.propensity,
        bucket=decision.bucket,
        tokens_before=0,
        tokens_after=0,  # the redirect (if any) is accounted in the injected-tokens ledger
        recovery_id=None,
        tool_use_id=None if tool is None else tool.use_id,
        command_sig="" if tool is None else tool.name,
        ts=time.time(),
        tool_name=None if tool is None else tool.name,
    )
    decisions.append(event.project_key, rec, cfg, env, "loopguard")


# --- hook entry points --------------------------------------------------------------------------


def on_tool(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    """PostToolUse / PostToolUseFailure. Returns the redirect as ``inject_context`` (never an
    output replacement) or the user notice."""
    env = os.environ if env is None else env
    tool = event.tool
    if tool is None or event.kind not in _POST or not _enabled(cfg):
        return Action()
    failed = event.kind == "post_tool_failure"
    interrupt = failed and flag_on(event.extra.get("is_interrupt"))
    fp: str | None = None
    if failed and not interrupt:
        fp = fingerprint(event.output_text or "", tool=tool.name, target=_target(event))
    edit_path, moves, write = ("", [], None) if failed else _transitions(event)
    editing = bool(edit_path)
    agent = _agent_id(event)
    path = tracking.overlay_path(event, env)
    overlay = session.read_session(path)
    turn = _num(overlay.get("turn"))
    if fp is None and not editing and not interrupt:
        lg = _dict(overlay.get(STATE_KEY))
        ag = _dict(_dict(lg.get("agents")).get(agent))
        if lg.get("turn") != turn or not (ag.get("watch") or ag.get("natural")):
            return Action()  # nothing to count and no open window: no write
    step = _Step()
    hit = _Hit()

    def phase1(state: dict[str, Any]) -> None:
        lg = _state(state, turn, step)
        ag = _agent(lg, agent)
        if ag is None:
            _record(state, step)
            return
        if interrupt:
            dec = ag.get("decision")
            if ag.get("nudge") and not ag.get("regret_paid") and isinstance(dec, list):
                ag["regret_paid"] = True
                step.payouts.append((dec[0], None, 1.0, False))
                step.outcomes.append([dec[0], LOOPGUARD.name, dec[1], "", 1.0, dec[3], "interrupt"])
            _record(state, step)
            return
        if editing:
            osc = _oscillation(lg, ag, edit_path, moves, write)
            if osc is not None:
                hit.fp, hit.n, hit.osc = osc[0], osc[1], True
        elif fp is not None:
            fps = _dict(ag.get("fps"))
            ag["fps"] = fps
            n = _num(fps.get(fp)) + 1
            room = _total(lg, "fps") < MAX_FINGERPRINTS
            hit.fp, hit.n = fp, (n if _bounded_add(fps, fp, n, room) else 1)
        _watch(ag, hit.fp, step)
        _natural_watch(ag, hit.fp, step)
        if ag.get("decision") is None:
            step.need_decision = hit.fp is not None and hit.n >= (MIN_REVERSALS if hit.osc else 2)
        else:
            _evaluate(ag, hit, cfg, step, lg)
            if step.nudge is not None:
                _account(state, step)
            _natural_open(ag, hit, cfg)
        _record(state, step)

    if not _update(path, env, phase1):
        return Action()
    if step.need_decision:
        sctx = tracking.session_ctx(event, overlay, path)
        decision = policy.decide(LOOPGUARD, features.for_point(LOOPGUARD, event, sctx, None), sctx)

        def phase2(state: dict[str, Any]) -> None:
            lg = _state(state, turn, step)
            ag = _agent(lg, agent)
            if ag is None:
                return
            if ag.get("decision") is None:  # a concurrent hook may have decided first
                ag["decision"] = [
                    decision.decision_id,
                    decision.arm,
                    decision.propensity,
                    decision.bucket,
                ]
                step.decided = decision
            _evaluate(ag, hit, cfg, step, lg)
            if step.nudge is not None:
                _account(state, step)

        _update(path, env, phase2)
        if step.decided is not None:
            _log_decision(event, step.decided, cfg, env)
    if step.error_loop:
        # this turn's error-bearing compressions get 0.5; paid when their window settles
        regret.RegretDetector(cfg, env).on_loop(tracking.session_ctx(event, overlay, path), None)
    _pay(policy, step, event, cfg, env)
    return Action(inject_context=step.nudge, user_message=step.notice)


def _close(event: Event, policy: object, cfg: Config, env: Mapping[str, str], prompt: bool) -> None:
    path = tracking.overlay_path(event, env)
    if not isinstance(session.read_session(path).get(STATE_KEY), dict):
        return
    step = _Step()
    stop = prompt and is_stop(event.prompt)
    signal = ("user_stop" if stop else "next_prompt") if prompt else "session_end"

    def apply(state: dict[str, Any]) -> None:
        lg = state.pop(STATE_KEY, None)
        if isinstance(lg, dict):
            _settle(lg, step, signal, 1.0 if stop else 0.0)
        _record(state, step)

    if _update(path, env, apply):
        _pay(policy, step, event, cfg, env)


def on_prompt(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    """UserPromptSubmit (after the turn is counted): settle the previous turn's decisions
    (savings of open windows, regret from the user's words) and start the new turn empty."""
    _close(event, policy, cfg, os.environ if env is None else env, prompt=True)
    return Action()


def on_session_end(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    """SessionEnd: settle what is open (no next prompt: regret 0)."""
    _close(event, policy, cfg, os.environ if env is None else env, prompt=False)
    return Action()
