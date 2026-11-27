"""Failure-loop guard (spec 6.11; decision point ``loopguard``, tier medium).

Within one turn (between two UserPromptSubmit events) the guard watches deterministic signals
only:

* the same normalised error ``fingerprint`` of a failed tool call (PostToolUseFailure) ``k``
  times, where ``k`` is the learned arm (``patient`` 5, ``standard`` 3, ``eager`` 2; never below
  ``[loopguard] min_k``);
* ``[loopguard] burst`` (8) failures of any kind;
* edit-revert oscillation: an Edit whose (old, new) pair reverses an earlier Edit of the same file,
  or a Write whose content returns to an earlier content of that file. Each reversal is one more
  occurrence of a per-file pseudo-fingerprint (the first reversal counts 2: the edit and its
  revert), so the arm's ``k`` applies to it too.

The arm is decided once per turn, when the first repeat (or burst) appears, and holds for the
rest of the turn. The first trigger adds one short redirect as ``additionalContext`` to the
triggering tool result (the tool output itself is never touched); a second detection in the same
turn (counted on what happened after the redirect) tells the user only, once. A turn that never
triggers logs no decision.

Reward: savings 1 if the redirected fingerprint does not recur within the next
``[loopguard] watch_calls`` (5) tool calls of the turn (the turn ending first counts as broken),
else 0. Regret 1 if the user interrupts a tool call after the redirect, or the user's next prompt
says stop / undo / revert / that's wrong / no, don't (whole words, any case); else an explicit 0
at the next prompt or SessionEnd. A trigger also tells the regret detector (``on_loop``): this
turn's compression decisions on error-bearing outputs get 0.5 regret.

State is a bounded per-turn dict in the session overlay (``loopguard``: at most
``MAX_FINGERPRINTS`` fingerprints and files, ``MAX_HASHES`` hashes per file); each event costs at
most one overlay read and one or two updates, independent of the session length.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen.config import Config, flag, flag_on, log_error, state_home
from token_sparen.emit import LEDGER_FIELD
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn import decisions, features, regret
from token_sparen.learn.policy import LOOPGUARD, DecisionRecord, Policy
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, paths, session

__all__ = [
    "ARM_K",
    "LOOPGUARD",
    "MAX_FINGERPRINTS",
    "STATE_KEY",
    "fingerprint",
    "is_stop",
    "on_prompt",
    "on_session_end",
    "on_tool",
]

STATE_KEY = "loopguard"
ARM_K: dict[str, int] = {"patient": 5, "standard": 3, "eager": 2}
MAX_FINGERPRINTS = 50  # fingerprints, and files watched for oscillation, per turn
MAX_HASHES = 8  # edit / write hashes kept per file
SCAN_HEAD = 8000  # characters of an error text scanned (head + tail; exception types end a trace)
SCAN_TAIL = 4000
MAX_LINE = 300
DEFAULT_MIN_K = 2
DEFAULT_BURST = 8
DEFAULT_WATCH = 5
NUDGE = (
    "[token-sparen: this error has repeated {n}× this turn ({fp}). Consider stepping back: "
    "re-read the error, check assumptions, or try a different approach.]"
)
USER_NOTICE = (
    "token-sparen: the agent is repeating the same error ({n}×); you may want to intervene."
)
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
_POST = frozenset({"post_tool", "post_tool_failure"})

_EXC = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\b")
_ERR_LINE = re.compile(
    r"error|exception|fail|fatal|panic|denied|not found|cannot|unable|invalid", re.IGNORECASE
)
_QUOTED = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`")
_PATH = re.compile(r"(?:[a-z]:)?[\w.~-]*(?:[/\\][\w.~-]+)+")
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_STOP = re.compile(r"\b(?:stop|undo|revert|that's wrong|no,?\s+don't)\b", re.IGNORECASE)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:10]


def _normalise(line: str) -> str:
    s = _QUOTED.sub("<s>", line.lower())
    s = _PATH.sub("<p>", s)
    s = _HEX.sub("<h>", s)
    s = _DIGITS.sub("<n>", s)
    return _SPACE.sub(" ", s).strip()[:MAX_LINE]


def fingerprint(text: str) -> str:
    """Ten hex characters identifying an error modulo numbers, hex ids, paths, quoted values
    and line:column: the exception type plus the error line (the line naming the last exception
    type, else the first line that looks like an error, else the first non-empty line). Only
    the first ``SCAN_HEAD`` and last ``SCAN_TAIL`` characters are read. Never raises."""
    if len(text) > SCAN_HEAD + SCAN_TAIL:
        text = f"{text[:SCAN_HEAD]}\n{text[-SCAN_TAIL:]}"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    exc, line = "", ""
    for ln in reversed(lines):
        found = _EXC.findall(ln)
        if found:
            exc, line = found[-1].lower(), ln
            break
    if not line:
        line = next((ln for ln in lines if _ERR_LINE.search(ln)), lines[0] if lines else "")
    return _sha(f"{exc}|{_normalise(line)}")


def is_stop(prompt: str | None) -> bool:
    """Does the user's prompt say stop / undo / revert / that's wrong / no, don't?"""
    return bool(prompt) and _STOP.search(str(prompt).replace("’", "'")) is not None


# --- config -------------------------------------------------------------------------------------


def _enabled(cfg: Config) -> bool:
    return flag(cfg.get("loopguard.enabled"), True)


def _param(cfg: Config, key: str, default: int) -> int:
    value = cfg.get(f"loopguard.{key}")
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 1:
        return int(value)
    return default


# --- per-turn state -----------------------------------------------------------------------------

Payout = tuple[str, float | None, float | None]  # (decision id, savings, regret)


@dataclass
class _Step:
    """What one state update decided (applied outside the lock)."""

    payouts: list[Payout] = field(default_factory=list)
    outcomes: list[list[Any]] = field(default_factory=list)  # regret rows for the decision log
    need_decision: bool = False
    nudge: str | None = None
    notice: str | None = None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _turn(overlay: Mapping[str, Any]) -> int:
    return _num(overlay.get("turn"))


def _settle(lg: dict[str, Any], step: _Step, signal: str, regret_value: float) -> None:
    """Close a turn: an open watch is a broken loop (savings 1); an unpaid regret is paid."""
    dec = lg.get("decision")
    if not isinstance(dec, list) or len(dec) < 4 or not isinstance(dec[0], str):
        return
    did = dec[0]
    if isinstance(lg.get("watch"), dict):
        step.payouts.append((did, 1.0, None))
        lg["watch"] = None
    if lg.get("nudge") and not lg.get("regret_paid"):
        lg["regret_paid"] = True
        step.payouts.append((did, None, regret_value))
        step.outcomes.append([did, LOOPGUARD.name, str(dec[1]), "", regret_value, dec[3], signal])


def _state(state: dict[str, Any], turn: int, step: _Step) -> dict[str, Any]:
    """This turn's loop-guard state; a stale one (no prompt hook ran) is settled first."""
    lg = state.get(STATE_KEY)
    if isinstance(lg, dict) and lg.get("turn") == turn:
        return lg
    if isinstance(lg, dict):
        _settle(lg, step, "expiry", 0.0)
    lg = {"turn": turn, "fps": {}, "fails": 0, "files": {}}
    state[STATE_KEY] = lg
    return lg


def _bounded_add(table: dict[str, Any], key: str, value: Any) -> bool:
    """Set ``table[key]`` unless that would exceed ``MAX_FINGERPRINTS`` keys."""
    if key not in table and len(table) >= MAX_FINGERPRINTS:
        return False
    table[key] = value
    return True


def _edit_marks(event: Event) -> list[tuple[str, str, str]]:
    """``(file, mark, reverse)`` of an Edit/MultiEdit/Write; reverse = the mark of a revert."""
    tool = event.tool
    if tool is None or tool.name not in EDIT_TOOLS:
        return []
    path = tool.input.get("file_path")
    if not isinstance(path, str) or not path:
        return []
    if tool.name == "Write":
        content = tool.input.get("content")
        return [(path, f"w:{_sha(content)}", "")] if isinstance(content, str) else []
    edits = tool.input.get("edits") if tool.name == "MultiEdit" else [tool.input]
    out: list[tuple[str, str, str]] = []
    for e in edits if isinstance(edits, list) else []:
        old, new = _dict(e).get("old_string"), _dict(e).get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            a, b = _sha(old), _sha(new)
            out.append((path, f"e:{a}:{b}", f"e:{b}:{a}"))
    return out


def _oscillation(lg: dict[str, Any], marks: list[tuple[str, str, str]]) -> str | None:
    """Record the edits; the file's pseudo-fingerprint if one reverts an earlier state."""
    files = _dict(lg.get("files"))
    lg["files"] = files
    hit: str | None = None
    for path, mark, reverse in marks:
        key = _sha(f"file|{path}")
        seen = [m for m in files.get(key, []) if isinstance(m, str)]
        if reverse:
            back = reverse in seen
        else:  # Write: back to an earlier content, not just the same content again
            writes = [m for m in seen if m.startswith("w:")]
            back = mark in writes and writes[-1] != mark
        if back:
            hit = _sha(f"revert|{path}")
        seen = [m for m in seen if m != mark] + [mark]
        if key in files:
            del files[key]  # most recent last: the oldest file is evicted first
        while len(files) >= MAX_FINGERPRINTS:
            del files[next(iter(files))]
        files[key] = seen[-MAX_HASHES:]
    return hit


def _count(lg: dict[str, Any], fp: str, oscillation: bool) -> int:
    fps = _dict(lg.get("fps"))
    lg["fps"] = fps
    cur = _num(fps.get(fp))
    new = max(cur, 1) + 1 if oscillation else cur + 1
    return new if _bounded_add(fps, fp, new) else 1


def _evaluate(
    lg: dict[str, Any], fp: str | None, n: int, failed: bool, cfg: Config, step: _Step
) -> bool:
    """Apply the arm's threshold to this event; True if this is the first (redirect) trigger."""
    dec = lg.get("decision")
    if not isinstance(dec, list) or len(dec) < 4:
        return False
    k = max(ARM_K.get(str(dec[1]), ARM_K[LOOPGUARD.safe_arm]), _param(cfg, "min_k", DEFAULT_MIN_K))
    burst = _param(cfg, "burst", DEFAULT_BURST)
    nudge = lg.get("nudge")
    if isinstance(nudge, dict):
        after = _dict(lg.get("after"))
        lg["after"] = after
        repeat = 0
        if fp is not None:
            repeat = _num(after.get(fp)) + 1
            _bounded_add(after, fp, repeat)
            repeat += 1 if fp == nudge.get("fp") else 0
        if failed:
            lg["fails_after"] = _num(lg.get("fails_after")) + 1
        again = (fp is not None and repeat >= k) or _num(lg.get("fails_after")) >= burst
        if again and not lg.get("warned"):
            lg["warned"] = True
            step.notice = USER_NOTICE.format(n=n if fp is not None else _num(lg.get("fails")))
        return False
    fails = _num(lg.get("fails"))
    if fp is not None and n >= k:
        label, times, watch = fp, n, [fp]
    elif failed and fails >= burst:
        fps = _dict(lg.get("fps"))
        label = max(fps, key=lambda f: _num(fps[f])) if fps else "burst"
        times, watch = fails, list(fps)
    else:
        return False
    lg["nudge"] = {"fp": label, "n": times}
    lg["watch"] = {"fps": watch, "left": _param(cfg, "watch_calls", DEFAULT_WATCH)}
    step.nudge = NUDGE.format(n=times, fp=label)
    return True


def _watch(lg: dict[str, Any], fp: str | None, step: _Step) -> None:
    """One tool call after the redirect: a recurrence settles savings 0; the end of the window
    without one settles savings 1."""
    watch, dec = lg.get("watch"), lg.get("decision")
    if not isinstance(watch, dict) or not isinstance(dec, list) or not dec:
        return
    fps = watch.get("fps")
    if fp is not None and isinstance(fps, list) and fp in fps:
        step.payouts.append((str(dec[0]), 0.0, None))
        lg["watch"] = None
        return
    left = _num(watch.get("left")) - 1
    if left <= 0:
        step.payouts.append((str(dec[0]), 1.0, None))
        lg["watch"] = None
    else:
        watch["left"] = left


# --- effects outside the lock -------------------------------------------------------------------


def _pay(policy: object, step: _Step, event: Event, cfg: Config, env: Mapping[str, str]) -> None:
    fn: Callable[[str, float | None, float | None], None] | None = regret.reward_fn(policy)
    if fn is not None:
        for did, savings, regret_value in step.payouts:
            fn(did, savings, regret_value)
    if step.outcomes:
        now = time.time()
        recs = [decisions.outcome(r[0], float(r[4]), str(r[6]), now) for r in step.outcomes]
        pdir = paths.project_dir_for(state_home(env), event.project_key)
        decisions.append_outcomes(pdir, recs, cfg, env, "loopguard")


def _record_outcomes(state: dict[str, Any], rows: list[list[Any]]) -> None:
    """Settled regrets also go to the overlay's ``regret_outcomes`` (fold: circuit breaker)."""
    if rows:
        outs = state.get(regret.OUTCOMES_KEY)
        outs = [*(outs if isinstance(outs, list) else []), *rows]
        state[regret.OUTCOMES_KEY] = outs[-regret.MAX_OUTCOMES :]


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
    fp = fingerprint(event.output_text or "") if failed and not interrupt else None
    marks = [] if failed else _edit_marks(event)
    path = tracking.overlay_path(event, env)
    overlay = session.read_session(path)
    turn = _turn(overlay)
    if fp is None and not marks and not interrupt:
        lg = overlay.get(STATE_KEY)
        if not isinstance(lg, dict) or lg.get("turn") != turn or not lg.get("watch"):
            return Action()  # nothing to count and no open reward window: no write
    step = _Step()
    seen: dict[str, Any] = {}

    def phase1(state: dict[str, Any]) -> None:
        lg = _state(state, turn, step)
        if interrupt:
            dec = lg.get("decision")
            if lg.get("nudge") and not lg.get("regret_paid") and isinstance(dec, list):
                lg["regret_paid"] = True
                step.payouts.append((dec[0], None, 1.0))
                step.outcomes.append([dec[0], LOOPGUARD.name, dec[1], "", 1.0, dec[3], "interrupt"])
            _record_outcomes(state, step.outcomes)
            return
        cur, osc = fp, False
        if marks:
            cur = _oscillation(lg, marks)
            osc = cur is not None
        _watch(lg, cur, step)
        if failed:
            lg["fails"] = _num(lg.get("fails")) + 1
        n = _count(lg, cur, osc) if cur is not None else 0
        seen.update(fp=cur, n=n)
        if lg.get("decision") is None:
            burst = failed and _num(lg.get("fails")) >= _param(cfg, "burst", DEFAULT_BURST)
            step.need_decision = n >= 2 or burst
        elif _evaluate(lg, cur, n, failed, cfg, step):
            _account(state, step)
        _record_outcomes(state, step.outcomes)

    if not _update(path, env, phase1):
        return Action()
    if step.need_decision:
        sctx = tracking.session_ctx(event, overlay, path)
        decision = policy.decide(LOOPGUARD, features.for_point(LOOPGUARD, event, sctx, None), sctx)

        def phase2(state: dict[str, Any]) -> None:
            lg = _state(state, turn, step)
            if lg.get("decision") is None:  # a concurrent hook may have decided first
                lg["decision"] = [
                    decision.decision_id,
                    decision.arm,
                    decision.propensity,
                    decision.bucket,
                ]
            if _evaluate(lg, seen.get("fp"), _num(seen.get("n")), failed, cfg, step):
                _account(state, step)

        _update(path, env, phase2)
    if step.nudge is not None:
        _on_trigger(event, overlay, path, step, policy, cfg, env)
    _pay(policy, step, event, cfg, env)
    return Action(inject_context=step.nudge, user_message=step.notice)


def _account(state: dict[str, Any], step: _Step) -> None:
    """The redirect costs the user tokens: add it to the injected-tokens ledger."""
    state[LEDGER_FIELD] = _num(state.get(LEDGER_FIELD)) + estimate_tokens(step.nudge or "")


def _on_trigger(
    event: Event,
    overlay: Mapping[str, Any],
    path: Path,
    step: _Step,
    policy: object,
    cfg: Config,
    env: Mapping[str, str],
) -> None:
    """Log the (now consequential) decision and tell the regret detector about the loop."""
    lg = _dict(session.read_session(path).get(STATE_KEY))
    dec = lg.get("decision")
    tool = event.tool
    if isinstance(dec, list) and len(dec) >= 4 and tool is not None:
        rec = DecisionRecord(
            decision_id=str(dec[0]),
            point=LOOPGUARD.name,
            arm=str(dec[1]),
            propensity=float(dec[2]),
            bucket=str(dec[3]),
            tokens_before=0,
            tokens_after=estimate_tokens(step.nudge or ""),
            recovery_id=None,
            tool_use_id=tool.use_id,
            command_sig=tool.name,
            ts=time.time(),
            tool_name=tool.name,
        )
        decisions.append(event.project_key, rec, cfg, env, "loopguard")
    ctx = tracking.session_ctx(event, overlay, path)
    # this turn's error-bearing compressions get 0.5; paid when their window settles
    regret.RegretDetector(cfg, env).on_loop(ctx, None)


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
        _record_outcomes(state, step.outcomes)

    if _update(path, env, apply):
        _pay(policy, step, event, cfg, env)


def on_prompt(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    """UserPromptSubmit (after the turn is counted): settle the previous turn's redirect (savings
    of an open window, regret from the user's words) and start the new turn empty."""
    _close(event, policy, cfg, os.environ if env is None else env, prompt=True)
    return Action()


def on_session_end(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    """SessionEnd: settle what is open (no next prompt: regret 0)."""
    _close(event, policy, cfg, os.environ if env is None else env, prompt=False)
    return Action()
