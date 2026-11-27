"""Claude Code hook entry point: parse, dispatch, render; fail open on everything.

Contract with the host: exactly one JSON object on stdout, exit code 0, within the latency budget.
Any exception, bad payload, kill switch or budget overrun yields the no-op ``{}``. The one
exception is PreCompact with summary instructions: Claude Code reads that stdout as plain text.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any, TextIO, cast

from token_sparen.adapters.claude import settings as _settings  # noqa: F401 - registers the deny writer
from token_sparen.adapters.claude.translate import EVENT_KINDS, ClaudeAdapter
from token_sparen.config import Config, flag_on, load_config, log_error, state_home
from token_sparen.events import Action, Event, EventKind
from token_sparen.facts import inject
from token_sparen.learn import breaker, overlay, regret
from token_sparen.learn.hookpolicy import HookPolicy
from token_sparen.learn.policy import Policy, StaticPolicy
from token_sparen.mechanisms import (
    boundary,
    compression,
    governor,
    guards,
    loopguard,
    prune,
    read_dedupe,
    subagent,
    tracking,
)
from token_sparen.store import jsonl, session

Handler = Callable[[Event, Config], Action]
# Ordered handler chain per event kind; see ``handle`` for how the actions are composed.
HANDLERS: dict[EventKind, list[Handler]] = {}
ADAPTER = ClaudeAdapter()
DEFAULT_BUDGET_MS = 400
DEFAULT_SAMPLE_RATE = 0.1
DEFAULT_LATENCY_LOG_BYTES = 1_000_000
NOOP = "{}"
_POST_KINDS = frozenset({"post_tool", "post_tool_failure"})


# Handlers that still run after a PostToolUse replacement ended the chain (bookkeeping).
ALWAYS_RUN: set[Handler] = set()


def register(kind: EventKind, fn: Handler, *, always: bool = False) -> None:
    """Append ``fn`` to the handler chain for ``kind`` (a new list, so snapshots stay valid).

    ``always``: on PostToolUse(+Failure) the handler also runs after an output replacement
    (its own replacement is ignored); use it for bookkeeping placed at the end of the chain.
    """
    HANDLERS[kind] = [*HANDLERS.get(kind, []), fn]
    if always:
        ALWAYS_RUN.add(fn)


# The environment of the running hook (``main``'s ``env``), for handlers that touch state.
HOOK_ENV: ContextVar[Mapping[str, str]] = ContextVar("HOOK_ENV", default=os.environ)
# ``time.monotonic()`` at hook start (None outside a hook: handlers then use their own start).
HOOK_STARTED: ContextVar[float | None] = ContextVar("HOOK_STARTED", default=None)
# A fixed policy for every hook (tests); None (default): each hook gets its own ``HookPolicy``,
# the learning Engine built on first use (``StaticPolicy`` when ``[learn] enabled = false``).
POLICY: Policy | None = None
_STATIC = StaticPolicy({})
HOOK_POLICY: ContextVar[Policy | None] = ContextVar("HOOK_POLICY", default=None)


def _policy() -> Policy:
    if POLICY is not None:
        return POLICY
    return HOOK_POLICY.get() or _STATIC


def _hook_policy(event: Event, cfg: Config, env: Mapping[str, str]) -> HookPolicy:
    tool = event.tool
    salt = f"{event.kind}|{tool.use_id if tool is not None else ''}"
    return HookPolicy(cfg, env, event.project_key, event.session_id, salt=salt)


def _contained(fn: Handler) -> Handler:
    """A built-in mechanism never raises: an error is logged and the mechanism is a no-op."""

    @functools.wraps(fn)
    def run(event: Event, cfg: Config) -> Action:
        try:
            return fn(event, cfg)
        except Exception as exc:  # noqa: BLE001 - fail open per mechanism
            where = f"claude.{getattr(fn, '__name__', 'handler')}"
            log_error(HOOK_ENV.get(), where, f"{type(exc).__name__}: {exc}")
            return Action()

    return run


def _on_prompt(event: Event, cfg: Config) -> Action:
    return tracking.handle_prompt(event, cfg, env=HOOK_ENV.get())


def _on_regret(event: Event, cfg: Config) -> Action:
    return regret.handle(event, cfg, _policy(), env=HOOK_ENV.get())


def _on_track(event: Event, cfg: Config) -> Action:
    tracking.record_paths(event, env=HOOK_ENV.get())
    return Action()


def _on_read_dedupe(event: Event, cfg: Config) -> Action:
    return read_dedupe.handle_post_tool(
        event, _policy(), cfg, env=HOOK_ENV.get(), replaceable=ADAPTER.replaceable
    )


def _on_compress(event: Event, cfg: Config) -> Action:
    env = HOOK_ENV.get()
    action = compression.handle_post_tool(
        event,
        _policy(),
        cfg,
        env=env,
        replaceable=ADAPTER.replaceable,
        started=HOOK_STARTED.get(),
    )
    if action.replace_output is not None and event.tool is not None and event.tool.name == "Read":
        read_dedupe.forget(event, env=env)  # the model never saw this read in full
    return action


def _on_loopguard(event: Event, cfg: Config) -> Action:
    """Failure-loop guard: the redirect goes out as additionalContext, never as a replacement."""
    return loopguard.on_tool(event, _policy(), cfg, env=HOOK_ENV.get())


def _on_loopguard_prompt(event: Event, cfg: Config) -> Action:
    return loopguard.on_prompt(event, _policy(), cfg, env=HOOK_ENV.get())


def _on_loopguard_end(event: Event, cfg: Config) -> Action:
    return loopguard.on_session_end(event, _policy(), cfg, env=HOOK_ENV.get())


def _on_pre_invalidate(event: Event, cfg: Config) -> Action:
    return read_dedupe.invalidate(event, env=HOOK_ENV.get())


def _on_guard(event: Event, cfg: Config) -> Action:
    return guards.check(event, cfg)


def _on_pre_compact(event: Event, cfg: Config) -> Action:
    return read_dedupe.on_pre_compact(event, env=HOOK_ENV.get())


def _on_governor_prompt(event: Event, cfg: Config) -> Action:
    return governor.on_prompt(event, cfg, env=HOOK_ENV.get())


def _on_boundary(event: Event, cfg: Config) -> Action:
    return boundary.on_prompt(event, cfg, env=HOOK_ENV.get())


def _on_governor_post(event: Event, cfg: Config) -> Action:
    return governor.on_post_tool(event, cfg, env=HOOK_ENV.get())


def _on_governor_stop(event: Event, cfg: Config) -> Action:
    return governor.check_pre_tool(event, cfg, env=HOOK_ENV.get())


def _on_subagent(event: Event, cfg: Config) -> Action:
    return subagent.handle_pre_tool(event, _policy(), cfg, env=HOOK_ENV.get())


def _on_compact_instructions(event: Event, cfg: Config) -> Action:
    return boundary.on_pre_compact(event, cfg, env=HOOK_ENV.get())


def _on_session_start(event: Event, cfg: Config) -> Action:
    return read_dedupe.on_session_start(event, env=HOOK_ENV.get())


def _on_facts(event: Event, cfg: Config) -> Action:
    """Active repository facts as a labelled data block (``facts.inject``)."""
    return inject.on_session_start(event, cfg, env=HOOK_ENV.get())


def _on_breaker_notice(event: Event, cfg: Config) -> Action:
    env = HOOK_ENV.get()
    msgs = breaker.notices(event.project_key, state_home(env), env)
    return Action(user_message="\n".join(msgs) if msgs else None)


def _on_fold_pending(event: Event, cfg: Config) -> Action:
    """Fold left-over sessions of this project (idle >= 1 h; at most 3 per start)."""
    env = HOOK_ENV.get()
    overlay.fold_pending(
        event.project_key,
        state_home(env),
        event.project_dir,
        exclude=event.session_id,
        env=env,
        params=cfg.data,
    )
    return Action()


def _on_fold_spawn(event: Event, cfg: Config) -> Action:
    """SessionEnd: fold this session in a detached process (never waits for it); the only fold
    that writes new tool-pruning deny rules (effective from the next session)."""
    env = HOOK_ENV.get()
    tracking.record_end(event, env=env)  # no longer live for the pruning idle gate
    args = ["fold", "--session", event.session_id, "--project", str(event.project_dir)]
    overlay.spawn_fold([*args, "--prune-apply"], env)
    return Action()


def _on_session_active(event: Event, cfg: Config) -> Action:
    tracking.record_active(event, env=HOOK_ENV.get())
    tracking.record_transcript(event, env=HOOK_ENV.get())
    return Action()


def _on_prune_start(event: Event, cfg: Config) -> Action:
    """Notice + policy revert; never writes a new deny rule (the session-end fold does)."""
    return prune.on_session_start(event, cfg, env=HOOK_ENV.get())


def _on_prune_prompt(event: Event, cfg: Config) -> Action:
    return prune.on_prompt(event, cfg, env=HOOK_ENV.get())


def _on_prune_tool(event: Event, cfg: Config) -> Action:
    return prune.on_tool(event, cfg, env=HOOK_ENV.get())


def _on_activity(event: Event, cfg: Config) -> Action:
    """Tool calls refresh the project's last activity (throttled): the pruning idle gate."""
    tracking.touch_active(event, env=HOOK_ENV.get())
    return Action()


def _on_prune_search(event: Event, cfg: Config) -> Action:
    return prune.on_tool_search(event, cfg, env=HOOK_ENV.get())


for _kind, _fn in (
    ("prompt", _on_prompt),  # counts the turn first; the governor and boundary see it
    ("prompt", _on_governor_prompt),
    ("prompt", _on_boundary),
    ("prompt", _on_regret),  # after the turn count: expires regret windows (explicit 0)
    ("prompt", _on_prune_prompt),  # the user named a pruned tool: restore it
    ("prompt", _on_loopguard_prompt),  # after the turn count: settles the previous turn
    ("pre_tool", _on_prune_tool),  # first: a later block ends the chain; never blocks itself
    ("pre_tool", _on_activity),
    ("pre_tool", _on_pre_invalidate),
    ("pre_tool", _on_governor_stop),
    ("pre_tool", _on_guard),
    ("pre_tool", _on_subagent),
    ("post_tool", _on_prune_search),  # ToolSearch for a pruned tool: restore it
    ("post_tool", _on_governor_post),  # before any replacement ends the chain
    ("post_tool", _on_regret),  # sees this call before it is itself trimmed or tracked
    ("post_tool", _on_read_dedupe),
    ("post_tool", _on_compress),
    ("post_tool", _on_loopguard),  # always: counts every call, adds context only
    ("post_tool", _on_activity),  # always (also after a replacement)
    ("post_tool", _on_track),  # last + always: its lock wait never eats compression's budget
    # same chain minus de-dupe; the host decides whether a failure result can be replaced
    ("post_tool_failure", _on_prune_tool),
    ("post_tool_failure", _on_governor_post),
    ("post_tool_failure", _on_regret),
    ("post_tool_failure", _on_compress),
    ("post_tool_failure", _on_loopguard),
    ("post_tool_failure", _on_track),
    ("pre_compact", _on_pre_compact),  # sets last_compact_turn, clears the read table
    ("pre_compact", _on_compact_instructions),
    ("session_start", _on_session_start),
    ("session_start", _on_prune_start),  # before _on_session_active records this session
    ("session_start", _on_session_active),
    ("session_start", _on_facts),
    ("session_start", _on_fold_pending),
    ("session_start", _on_breaker_notice),  # after the fold: a trip it causes is shown now
    ("session_end", _on_regret),  # settles every open regret window
    ("session_end", _on_loopguard_end),
    ("session_end", _on_fold_spawn),  # after settling: the fold sees every regret outcome
    ("permission_denied", _on_prune_tool),
):
    always = _fn in (_on_track, _on_loopguard) or (_kind == "post_tool" and _fn is _on_activity)
    register(cast(EventKind, _kind), _contained(cast(Handler, _fn)), always=always)


def _join(a: str | None, b: str | None, sep: str) -> str | None:
    return b if a is None else a if b is None else f"{a}{sep}{b}"


def _merge_side(into: Action, add: Action) -> None:
    into.inject_context = _join(into.inject_context, add.inject_context, "\n\n")
    into.user_message = _join(into.user_message, add.user_message, "\n")
    into.compact_instructions = _join(into.compact_instructions, add.compact_instructions, "\n\n")


def handle(event: Event, cfg: Config) -> Action:
    """Run the chain for ``event.kind`` and compose the actions.

    PreToolUse: the first block wins (any rewrite is dropped); otherwise the first rewrite wins and
    later handlers see the rewritten input. PostToolUse(+Failure): the first output replacement
    ends the chain (read de-dupe runs before compression, so a pointer skips compression); only
    handlers registered ``always`` (tracking) still run after it.
    ``inject_context`` and ``user_message`` of all handlers that ran are joined in order.
    """
    result = Action()
    current = event
    for fn in HANDLERS.get(event.kind, []):
        action = fn(current, cfg)
        _merge_side(result, action)
        if event.kind == "pre_tool":
            if action.block_reason is not None:
                result.block_reason = action.block_reason
                result.rewrite_input = None
                return result
            if action.rewrite_input is not None and result.rewrite_input is None:
                result.rewrite_input = action.rewrite_input
                if current.tool is not None:
                    tool = dataclasses.replace(current.tool, input=dict(action.rewrite_input))
                    current = dataclasses.replace(current, tool=tool)
        elif event.kind in _POST_KINDS:
            if action.replace_output is not None:
                result.replace_output = action.replace_output
                _run_always(event, cfg, result, after=fn)
                return result
        else:
            if result.replace_output is None:
                result.replace_output = action.replace_output
            if result.block_reason is None:
                result.block_reason = action.block_reason
            if result.rewrite_input is None:
                result.rewrite_input = action.rewrite_input
    return result


def _run_always(event: Event, cfg: Config, result: Action, *, after: Handler) -> None:
    chain = HANDLERS.get(event.kind, [])
    for fn in chain[chain.index(after) + 1 :]:
        if fn in ALWAYS_RUN:
            _merge_side(result, fn(event, cfg))


class _Output:
    """Writes at most one response, whichever of the main thread and the watchdog is first."""

    def __init__(self, stdout: TextIO) -> None:
        self._stdout = stdout
        self._lock = threading.Lock()
        self.written = False

    def write(self, text: str) -> bool:
        with self._lock:
            if self.written:
                return False
            self.written = True
            try:
                self._stdout.write(text + "\n")
                self._stdout.flush()
            except Exception:  # noqa: BLE001 - nothing more we can do; still exit 0
                pass
            return True


class _Watchdog:
    def __init__(
        self,
        out: _Output,
        env: Mapping[str, str],
        event_name: str,
        exit_fn: Callable[[int], object],
    ) -> None:
        self._out = out
        self._env = env
        self._event = event_name
        self._exit = exit_fn
        self._start = time.monotonic()
        self._timer: threading.Timer | None = None
        self.budget_ms = DEFAULT_BUDGET_MS

    def arm(self, budget_ms: int) -> None:
        """(Re)start the timer so it fires ``budget_ms`` after the hook started."""
        self.cancel()
        self.budget_ms = budget_ms
        remaining = max(0.0, budget_ms / 1000 - (time.monotonic() - self._start))
        self._timer = threading.Timer(remaining, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    @property
    def started(self) -> float:
        """``time.monotonic()`` when the hook started."""
        return self._start

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000

    def _fire(self) -> None:
        self._out.write(NOOP)
        log_error(
            self._env,
            "claude.entry",
            f"{self._event}: latency budget {self.budget_ms} ms exceeded",
        )
        self._exit(0)


def _budget_ms(cfg: Config) -> int:
    try:
        value = int(cfg.get("adapter.latency_budget_ms", DEFAULT_BUDGET_MS))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_MS
    return value if value > 0 else DEFAULT_BUDGET_MS


def _log_latency(
    env: Mapping[str, str], cfg: Config | None, event_name: str, ms: float, outcome: str
) -> None:
    try:
        cfg = cfg or Config()
        rate = float(cfg.get("adapter.latency_sample_rate", DEFAULT_SAMPLE_RATE))
        if random.random() >= rate:  # noqa: S311 - sampling, not crypto
            return
        limit = int(cfg.get("adapter.latency_log_max_bytes", DEFAULT_LATENCY_LOG_BYTES))
        rec = {"ts": time.time(), "event": event_name, "ms": round(ms, 3), "outcome": outcome}
        path = state_home(env) / "logs" / "latency.jsonl"
        jsonl.append(path, rec, limit, env)
    except Exception as exc:  # noqa: BLE001 - logging must never break a hook
        log_error(env, "claude.entry", f"latency log: {type(exc).__name__}: {exc}")


def _disabled(env: Mapping[str, str]) -> bool:
    """The kill switch, any on spelling (``config.flag_on``)."""
    return flag_on(env.get("TOKEN_SPAREN_DISABLE", ""))


def _respond(
    event_name: str, stdin: TextIO, env: Mapping[str, str], dog: _Watchdog
) -> tuple[str, Config | None, str]:
    """(response JSON, config, outcome); raises on any failure (caller fails open)."""
    if _disabled(env):
        return NOOP, None, "disabled"
    if event_name not in EVENT_KINDS:
        return NOOP, None, "unsupported"
    payload = json.loads(stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    event = ADAPTER.parse(event_name, payload, env)
    # The session overlay holds the per-session kill switch (`token-sparen session disable`)
    # and the session config layer (`token-sparen config set`), merged last.
    overlay = session.read_session(tracking.overlay_path(event, env))
    cfg = load_config(event.project_dir, env, session=overlay)
    budget = _budget_ms(cfg)
    if budget != dog.budget_ms:
        dog.arm(budget)
    if cfg.disabled or overlay.get("disabled") is True:
        return NOOP, cfg, "disabled"
    token = HOOK_ENV.set(env)
    started = HOOK_STARTED.set(dog.started)
    policy = HOOK_POLICY.set(_hook_policy(event, cfg, env))
    try:
        action = handle(event, cfg)
    finally:
        HOOK_POLICY.reset(policy)
        HOOK_STARTED.reset(started)
        HOOK_ENV.reset(token)
    if action.is_noop():
        return NOOP, cfg, "noop"
    text = ADAPTER.render_text(event, action)
    if text is not None:  # e.g. PreCompact summary instructions: plain-text stdout
        return text, cfg, "action"
    rendered = ADAPTER.render(event, action)
    if not rendered:
        return NOOP, cfg, "noop"
    return json.dumps(rendered, ensure_ascii=True, allow_nan=False), cfg, "action"


def main(
    event_name: str,
    stdin: TextIO,
    stdout: TextIO,
    env: Mapping[str, str],
    *,
    exit_fn: Callable[[int], Any] = os._exit,
) -> int:
    """Run one hook. Always returns 0 and writes exactly one JSON object line to ``stdout``."""
    out = _Output(stdout)
    dog = _Watchdog(out, env, event_name, exit_fn)
    cfg: Config | None = None
    outcome = "error"
    try:
        dog.arm(DEFAULT_BUDGET_MS)
        text, cfg, outcome = _respond(event_name, stdin, env, dog)
        out.write(text)
    except BaseException as exc:  # noqa: BLE001 - fail open, whatever happened
        out.write(NOOP)
        log_error(env, "claude.entry", f"{event_name}: {type(exc).__name__}: {exc}")
    try:
        if not _disabled(env):  # the kill switch writes nothing at all
            _log_latency(env, cfg, event_name, dog.elapsed_ms(), outcome)
    finally:
        dog.cancel()
    return 0
