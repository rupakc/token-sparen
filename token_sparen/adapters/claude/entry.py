"""Claude Code hook entry point: parse, dispatch, render; fail open on everything.

Contract with the host: exactly one JSON object on stdout, exit code 0, within the latency budget.
Any exception, bad payload, kill switch or budget overrun yields the no-op ``{}``.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from token_sparen.adapters.claude.translate import EVENT_KINDS, ClaudeAdapter
from token_sparen.config import Config, load_config, log_error, state_home
from token_sparen.events import Action, Event, EventKind
from token_sparen.store import jsonl

Handler = Callable[[Event, Config], Action]
HANDLERS: dict[EventKind, Handler] = {}
ADAPTER = ClaudeAdapter()
DEFAULT_BUDGET_MS = 400
DEFAULT_SAMPLE_RATE = 0.1
DEFAULT_LATENCY_LOG_BYTES = 1_000_000
NOOP = "{}"
_TRUE = {"1", "true"}


def register(kind: EventKind, fn: Handler) -> None:
    """Install the pipeline handler for ``kind`` (later registrations replace earlier ones)."""
    HANDLERS[kind] = fn


def _noop(event: Event, cfg: Config) -> Action:
    return Action()


register("pre_tool", _noop)
register("post_tool", _noop)


def handle(event: Event, cfg: Config) -> Action:
    """The core pipeline: dispatch to the handler registered for the event kind."""
    fn = HANDLERS.get(event.kind)
    return Action() if fn is None else fn(event, cfg)


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


def _respond(
    event_name: str, stdin: TextIO, env: Mapping[str, str], dog: _Watchdog
) -> tuple[str, Config | None, str]:
    """(response JSON, config, outcome); raises on any failure (caller fails open)."""
    if env.get("TOKEN_SPAREN_DISABLE", "").strip().lower() in _TRUE:
        return NOOP, None, "disabled"
    if event_name not in EVENT_KINDS:
        return NOOP, None, "unsupported"
    payload = json.loads(stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    event = ADAPTER.parse(event_name, payload, env)
    cfg = load_config(event.project_dir, env)
    budget = _budget_ms(cfg)
    if budget != dog.budget_ms:
        dog.arm(budget)
    if cfg.disabled:
        return NOOP, cfg, "disabled"
    action = handle(event, cfg)
    if action.is_noop():
        return NOOP, cfg, "noop"
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
        _log_latency(env, cfg, event_name, dog.elapsed_ms(), outcome)
    finally:
        dog.cancel()
    return 0
