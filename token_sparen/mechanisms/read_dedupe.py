"""Read de-duplication (decision point ``read_dedupe``).

On PostToolUse(Read) each read is recorded in the session overlay as ``(path, mtime_ns, size,
sha1 of the returned content, offset, limit, turn)``, keyed by a hash of the realpath, the range and
the agent (a subagent has its own context). A later read with the same key whose file metadata and
returned content are unchanged, and with no compaction since the earlier read, is replaced by a
short pointer. Conservative by construction:

- the earlier output must have reached the model in full (``forget`` drops an entry whose output
  another mechanism replaced, e.g. compression);
- pointers stay within one context epoch (v1 review M13): PreCompact and
  SessionStart(compact|clear|resume|fork) clear the table, and so does a prompt that repeats an
  earlier one of the session (``tracking.handle_prompt``: /rewind fires no hook but puts the
  earlier prompt back into the input); an entry older than ``last_compact_turn`` is never used;
- the earlier read is at most ``MAX_AGE_S`` old (Claude Code may clear old tool results after an
  idle gap without any hook);
- an Edit/Write/MultiEdit/NotebookEdit of the path (PreToolUse) drops its entries;
- the earlier read is at most ``[read_dedupe] max_turn_gap`` turns old;
- outputs below 2000 estimated tokens are never replaced.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from token_sparen.config import Config, flag, log_error, state_home
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn import decisions, features, regret
from token_sparen.learn.policy import READ_DEDUPE, DecisionRecord, Policy
from token_sparen.mechanisms import governor, tracking
from token_sparen.store import jsonstore, session

__all__ = ["POINTER", "READ_DEDUPE", "drop_reads", "forget", "handle_post_tool", "invalidate"]

POINTER = (
    "[token-sparen: unchanged since turn {n} — content already in context above; "
    "re-read with a different range or after edits]"
)
MIN_TOKENS = 2000  # invariant floor: smaller outputs are never modified
MAX_ENTRIES = 200
MAX_TURN_GAP = 8  # older reads may have scrolled out of the model's attention; re-send them
# Claude Code may clear older tool results after an idle gap (reportedly about an hour) and no
# hook says so: a read older than this is delivered in full.
MAX_AGE_S = 30 * 60
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# a resumed or forked conversation may come back with old tool results cleared
_RESET_SOURCES = frozenset({"compact", "clear", "resume", "fork"})

Replaceable = Callable[[Event], bool]


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cfg_int(cfg: Config, key: str, default: int) -> int:
    value = _int_or_none(cfg.get(key, default))
    return default if value is None or value <= 0 else value


def _path_hash(file_path: str) -> str:
    real = os.path.realpath(file_path)
    return hashlib.sha256(real.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _agent(event: Event) -> str:
    agent = event.extra.get("agent_id")
    return agent if isinstance(agent, str) else ""


def _file_path(event: Event, *keys: str) -> str | None:
    if event.tool is None:
        return None
    for key in keys:
        value = event.tool.input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _entry_key(event: Event, phash: str) -> str:
    assert event.tool is not None
    offset = _int_or_none(event.tool.input.get("offset"))
    limit = _int_or_none(event.tool.input.get("limit"))
    return f"{phash}:{offset}:{limit}:{_agent(event)}"


def _update(event: Event, env: Mapping[str, str], fn: Callable[[dict[str, Any]], None]) -> None:
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        fn(state)
        return state

    session.update_session(tracking.overlay_path(event, env), apply)


def _reads(state: dict[str, Any]) -> dict[str, Any]:
    reads = state.get("reads")
    if not isinstance(reads, dict):
        reads = {}
        state["reads"] = reads
    return reads


def _turn(state: Mapping[str, Any], key: str) -> int:
    return _int_or_none(state.get(key)) or 0


def _fresh(ts: Any, now: float) -> bool:
    """A read recorded at ``ts`` is recent enough to point at (no timestamp: never)."""
    if not isinstance(ts, int | float) or isinstance(ts, bool):
        return False
    return 0 <= now - ts <= MAX_AGE_S


def _enabled(cfg: Config) -> bool:
    return flag(cfg.get("read_dedupe.enabled"), False)  # opt-in (v1 ruling on M13)


def handle_post_tool(
    event: Event,
    policy: Policy,
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    replaceable: Replaceable | None = None,
) -> Action:
    """Record a Read; replace an unchanged, still-in-context re-read with ``POINTER``."""
    env = os.environ if env is None else env
    tool = event.tool
    if event.kind != "post_tool" or tool is None or tool.name != "Read" or not _enabled(cfg):
        return Action()
    content = event.output_text
    file_path = _file_path(event, "file_path")
    if content is None or file_path is None:
        return Action()
    if replaceable is not None and not replaceable(event):
        return Action()
    if regret.is_recovery_access(event):
        return Action()  # a saved full output the agent asked for: always deliver it
    try:
        st = os.stat(file_path)
    except OSError:
        return Action()
    phash = _path_hash(file_path)
    key = _entry_key(event, phash)
    digest = hashlib.sha1(content.encode("utf-8", "surrogatepass"), usedforsecurity=False)
    sha1 = digest.hexdigest()
    max_entries = _cfg_int(cfg, "read_dedupe.max_entries", MAX_ENTRIES)
    gap = _int_or_none(cfg.get("read_dedupe.max_turn_gap", MAX_TURN_GAP))
    max_gap = MAX_TURN_GAP if gap is None or gap < 0 else gap
    found: dict[str, int] = {}
    now = time.time()

    def apply(state: dict[str, Any]) -> None:
        reads = _reads(state)
        turn = _turn(state, "turn")
        prior = reads.get(key)
        if (
            isinstance(prior, dict)
            and _fresh(prior.get("ts"), now)
            and prior.get("mtime_ns") == st.st_mtime_ns
            and prior.get("size") == st.st_size
            and prior.get("sha1") == sha1
            and _turn(prior, "turn") >= _turn(state, "last_compact_turn")
            and turn - _turn(prior, "turn") <= max_gap
            and prior.get("pointed") is not True  # a pointer is served once per stored read
        ):
            found["turn"] = _turn(prior, "turn")
            found["now"] = turn
            return
        reads.pop(key, None)  # re-insert so dict order stays oldest-first
        reads[key] = {
            "p": phash,
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "sha1": sha1,
            "offset": _int_or_none(tool.input.get("offset")),
            "limit": _int_or_none(tool.input.get("limit")),
            "turn": turn,
            "ts": now,
        }
        while len(reads) > max_entries:
            reads.pop(next(iter(reads)))

    _update(event, env, apply)
    if "turn" not in found:
        return Action()
    before = estimate_tokens(content)
    if before < max(MIN_TOKENS, _cfg_int(cfg, "read_dedupe.min_tokens", MIN_TOKENS)):
        return Action()
    overlay_file = tracking.overlay_path(event, env)
    overlay = session.read_session(overlay_file)
    sctx = governor.decision_ctx(event, overlay, overlay_file, cfg, env)
    feats = features.for_point(READ_DEDUPE, event, sctx, None, tokens=before)
    decision = policy.decide(READ_DEDUPE, feats, sctx)
    pointer = POINTER.format(n=found["turn"])
    replaced = decision.arm == "pointer"
    after = estimate_tokens(pointer) if replaced else before
    rec = DecisionRecord(
        decision_id=decision.decision_id,
        point=decision.point,
        arm=decision.arm,
        propensity=decision.propensity,
        bucket=decision.bucket,
        tokens_before=before,
        tokens_after=after,
        recovery_id=None,
        tool_use_id=tool.use_id,
        command_sig=tool.name,
        ts=time.time(),
        tool_name=tool.name,
    )
    decisions.append(event.project_key, rec, cfg, env, "read_dedupe")
    if not replaced:
        return Action()
    _count(event, env, before - after, key)
    evicted = regret.RegretDetector(cfg, env).track(
        sctx,
        decision.decision_id,
        point=decision.point,
        bucket=decision.bucket,
        arm=decision.arm,
        recovery_id=None,
        command_sig=regret.event_signature(event),
        full="",
        kept="",
        ctype=None,
        files=[file_path],
        read_hash=phash,
    )
    regret.pay(policy, evicted)
    regret.reward_savings(
        policy,
        decision.decision_id,
        decision.point,
        decision.bucket,
        before - after,
        regret.expected_resends(governor.remaining_turns(overlay, cfg)),
        home=state_home(env),
        project_key=event.project_key,
        env=env,
        cfg=cfg,
        before=before,
    )
    return Action(replace_output=pointer)


def _count(event: Event, env: Mapping[str, str], removed: int, key: str) -> None:
    """Ledger counters, and mark the read as pointed at: the next identical read is delivered
    in full (the agent asked again, so the pointer did not serve it) and refreshes the entry."""

    def apply(state: dict[str, Any]) -> None:
        for name, inc in (("dedupes", 1), ("tokens_removed", removed)):
            state[name] = _turn(state, name) + inc
        entry = _reads(state).get(key)
        if isinstance(entry, dict):
            entry["pointed"] = True

    try:
        _update(event, env, apply)
    except (jsonstore.StoreBusy, OSError) as exc:  # statusline counters are best effort
        log_error(env, "read_dedupe", f"ledger: {type(exc).__name__}: {exc}")


def drop_reads(state: dict[str, Any], path_hashes: set[str]) -> None:
    """Drop every recorded read of these files (path hashes) from an overlay ``state`` in place;
    used when a pointer turned out to be regret, so the next read is delivered in full."""
    reads = state.get("reads")
    if not isinstance(reads, dict):
        return
    for key in [k for k, v in reads.items() if isinstance(v, dict) and v.get("p") in path_hashes]:
        del reads[key]


def forget(event: Event, *, env: Mapping[str, str] | None = None) -> None:
    """Drop this read's entry: its output was replaced, so the model never saw it in full."""
    env = os.environ if env is None else env
    file_path = _file_path(event, "file_path")
    if event.tool is None or file_path is None:
        return
    key = _entry_key(event, _path_hash(file_path))

    def apply(state: dict[str, Any]) -> None:
        _reads(state).pop(key, None)

    _update(event, env, apply)


def invalidate(event: Event, *, env: Mapping[str, str] | None = None) -> Action:
    """PreToolUse of an edit tool: forget every recorded read of the target path."""
    env = os.environ if env is None else env
    if event.tool is None or event.tool.name not in _EDIT_TOOLS:
        return Action()
    file_path = _file_path(event, "file_path", "notebook_path")
    if file_path is None:
        return Action()
    phash = _path_hash(file_path)

    def apply(state: dict[str, Any]) -> None:
        reads = _reads(state)
        for key in [k for k, v in reads.items() if isinstance(v, dict) and v.get("p") == phash]:
            del reads[key]

    _update(event, env, apply)
    return Action()


def on_pre_compact(event: Event, *, env: Mapping[str, str] | None = None) -> Action:
    """Everything read so far is about to be summarised away."""
    env = os.environ if env is None else env

    def apply(state: dict[str, Any]) -> None:
        state["last_compact_turn"] = _turn(state, "turn")
        state["reads"] = {}

    _update(event, env, apply)
    return Action()


def on_session_start(event: Event, *, env: Mapping[str, str] | None = None) -> Action:
    """SessionStart after a compaction, /clear, resume or fork: the earlier reads may no longer
    be in context."""
    env = os.environ if env is None else env
    if event.extra.get("source") not in _RESET_SOURCES:
        return Action()

    def apply(state: dict[str, Any]) -> None:
        state["reads"] = {}

    _update(event, env, apply)
    return Action()
