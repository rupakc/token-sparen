"""Output-style directive (spec 6.6, high tier): one short prose-style line at SessionStart.

Off by default (``[output_style] enabled = false``); enabled only from the user's
``config.toml`` or a session override (``enabled`` is ``false_only`` in ``PROJECT_POLICY``: a
project's ``token-sparen.toml`` may only switch it off; ``directive`` is user-only). It is
static: no decision point, no learning, no exploration; the text is a fixed entry of ``DIRECTIVES``.

Sources: ``startup``, ``clear`` and ``compact`` (the context is new, so the line goes out, once
per context epoch); never ``resume`` (the conversation, and the line, are still there). The
epoch counter lives in the session overlay and the line goes out through ``emit.Gate``, keyed
by epoch, so it is deduplicated within one context and counted as injected tokens. Each
injection is recorded in the overlay (``LEDGER_KEY``: directive name and context count) so the
E4 harness can tell which sessions ran with the directive.

An unknown directive name means off; it is logged once per project and name
(``projects/<key>/style_warned.json`` keeps only a hash of the name).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config, flag, log_error, state_home
from token_sparen.emit import Gate
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.store import jsonstore, paths, session

DIRECTIVES: dict[str, str] = {
    "concise": "Be concise in prose. Never shorten code, commands, file paths, error messages "
    "or quoted tool output.",
}
PREFIX = "[token-sparen output style] "
SOURCES = frozenset({"startup", "clear", "compact"})
NEW_CONTEXT = frozenset({"clear", "compact"})
LEDGER_KEY = "output_style"  # session overlay: {"directive": name, "injected": n contexts}
EPOCH_KEY = "output_style_epoch"  # session overlay: context epoch (bumped by clear / compact)
GATE_KIND = "output_style"
WARNED_FILE = "style_warned.json"


def on_session_start(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """The directive line once per context epoch when the user enabled it (module docstring)."""
    if not flag(cfg.get("output_style.enabled"), False):
        return Action()
    source = event.extra.get("source", "startup")
    if source not in SOURCES or event.session_id == "unknown":
        return Action()
    name = cfg.get("output_style.directive")
    directive = DIRECTIVES.get(name) if isinstance(name, str) else None
    if directive is None:
        _warn_unknown(env, event.project_key, name)
        return Action()
    text = PREFIX + directive
    home = state_home(env)
    ov = session.session_path(home, event.project_key, event.session_id)
    epoch = _epoch(ov, env, new_context=source in NEW_CONTEXT)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    gate = Gate(ov, env)
    gate.add(GATE_KIND, text, f"{GATE_KIND}:{epoch}:{digest}")
    out = gate.render(estimate_tokens(text))
    if out is not None:
        _record(ov, env, name)
    return Action(inject_context=out)


def _record(ov: Path, env: Mapping[str, str], name: str) -> None:
    """Note in the session overlay that (and how often) the directive went out (for E4)."""

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get(LEDGER_KEY)
        cur: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
        n = cur.get("injected")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
        state[LEDGER_KEY] = {"directive": name, "injected": n + 1}
        return state

    jsonstore.update(ov, apply, {}, env)


def _epoch(ov: Path, env: Mapping[str, str], *, new_context: bool) -> int:
    """This context's epoch; ``new_context`` (clear, compact) starts the next one."""
    if not new_context:
        raw = jsonstore.read(ov, {}, env).get(EPOCH_KEY)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    out: list[int] = []

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get(EPOCH_KEY)
        cur = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        state[EPOCH_KEY] = cur + 1
        out.append(cur + 1)
        return state

    jsonstore.update(ov, apply, {}, env)
    return out[-1]


def _warn_unknown(env: Mapping[str, str], project_key: str, name: object) -> None:
    shown = repr(name)[:80]
    tag = hashlib.sha256(shown.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    path = paths.project_dir_for(state_home(env), project_key) / WARNED_FILE
    if jsonstore.read(path, {}, env).get("directive") == tag:
        return  # already reported: no lock, no write
    warned = False

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal warned
        warned = state.get("directive") != tag
        return {**state, "directive": tag}

    try:
        jsonstore.update(path, apply, {}, env)
    except (jsonstore.StoreBusy, OSError) as exc:
        log_error(env, "outputstyle", f"warn flag: {type(exc).__name__}: {exc}")
        return
    if warned:
        log_error(
            env,
            "outputstyle",
            f"output_style.directive = {shown} is not a known directive "
            f"({', '.join(sorted(DIRECTIVES))}); output style is off",
        )
