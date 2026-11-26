"""Task-boundary detection and compaction advice (spec 6.5). Advisory only; never compacts.

On UserPromptSubmit the prompt's identifiers (file paths as basenames, ``snake_case`` and
``camelCase`` tokens of >= 4 chars, backticked words; lower-cased) are compared with the union of
the last ``history`` prompts' identifiers and the recently touched paths. Containment (share of
the prompt's identifiers already in that union) below ``min_overlap`` with context above
``min_ctx_pct`` (and a known window) from turn ``min_turn`` on means the user
probably started a new task: a terminal-only message recommends ``/compact`` (at most once per
``cooldown_turns``). A prompt with fewer than ``min_identifiers`` identifiers ("yes, go on") is
never a boundary.

PreCompact: the summary is asked to keep every recovery marker's full-output path and the list of
files edited, so trimmed outputs stay recoverable after compaction. Nothing is emitted when the
session has neither recovery markers (``trims``) nor edited files.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from token_sparen.config import Config, flag, log_error
from token_sparen.events import Action, Event, SessionCtx
from token_sparen.mechanisms import governor, tracking
from token_sparen.store import jsonstore, session

STATE = "boundary"
MAX_IDS = 200
MAX_EDITED = 20
_PATH = re.compile(r"(?:[\w.-]*/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7}\b")
_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")
_TICK = re.compile(r"`([^`\n]{1,80})`")
_MIN_LEN = 4

INSTRUCTIONS = (
    "token-sparen: when summarising, keep verbatim the full output path and the "
    "'token-sparen show <id>' command of every '[token-sparen: kept X of Y lines ...]' recovery "
    "marker, so trimmed tool outputs stay recoverable after compaction."
)


def identifiers(text: str) -> set[str]:
    """Lower-cased identifiers referenced by ``text`` (see module docstring)."""
    found: set[str] = set()
    for m in _PATH.finditer(text):
        found.add(m.group(0).rsplit("/", 1)[-1])
    for rx in (_SNAKE, _CAMEL):
        found.update(m.group(0) for m in rx.finditer(text))
    for m in _TICK.finditer(text):
        word = m.group(1).strip()
        found.add(word.rsplit("/", 1)[-1] if "/" in word else word)
    return {w.lower() for w in found if len(w) >= _MIN_LEN}


def containment(current: set[str], prior: set[str]) -> float:
    """Share of ``current``'s identifiers already seen in ``prior`` (|A ∩ B| / |A|).

    Unlike Jaccard it does not shrink as the recent-history union grows, so a prompt that only
    names known identifiers is never mistaken for a new task.
    """
    return len(current & prior) / len(current) if current else 0.0


def _int_cfg(cfg: Config, key: str, default: int) -> int:
    value = cfg.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _num_cfg(cfg: Config, key: str, default: float) -> float:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _state(overlay: Mapping[str, Any]) -> dict[str, Any]:
    raw = overlay.get(STATE)
    return raw if isinstance(raw, dict) else {}


def _history(overlay: Mapping[str, Any]) -> list[list[str]]:
    raw = _state(overlay).get("recent")
    if not isinstance(raw, list):
        return []
    return [[w for w in item if isinstance(w, str)] for item in raw if isinstance(item, list)]


def _context_set(overlay: Mapping[str, Any], history: int) -> set[str]:
    words: set[str] = set()
    for item in _history(overlay)[-history:] if history > 0 else []:
        words.update(item)
    for path in tracking.recent_paths(overlay):
        words.update(identifiers(path.replace("\\", "/")))
    return words


def detect(
    prompt: str,
    session_ctx: SessionCtx,
    cfg: Config | None = None,
    *,
    ctx_pct: float | None = None,
) -> bool:
    """Whether ``prompt`` starts a new task while context is large (pure: no state is written)."""
    cfg = cfg or Config()
    pct = session_ctx.ctx_pct if ctx_pct is None else ctx_pct
    if pct is None or pct <= _num_cfg(cfg, "boundary.min_ctx_pct", 0.40):
        return False
    if session_ctx.turn < _int_cfg(cfg, "boundary.min_turn", 3):
        return False
    overlay = session.read_session(session_ctx.overlay_path)
    last = _state(overlay).get("last_advice_turn")
    cooldown = _int_cfg(cfg, "boundary.cooldown_turns", 10)
    if isinstance(last, int) and session_ctx.turn - last < cooldown:
        return False
    current = identifiers(prompt)
    prior = _context_set(overlay, _int_cfg(cfg, "boundary.history", 3))
    if len(current) < _int_cfg(cfg, "boundary.min_identifiers", 2) or not prior:
        return False
    return containment(current, prior) < _num_cfg(cfg, "boundary.min_overlap", 0.15)


def _remember(
    event: Event, env: Mapping[str, str], words: Iterable[str], history: int, advised: int | None
) -> None:
    kept = sorted(words)[:MAX_IDS]

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        mine = dict(_state(state))
        mine["recent"] = [*_history(state), kept][-max(1, history) :]
        if advised is not None:
            mine["last_advice_turn"] = advised
        state[STATE] = mine
        return state

    session.update_session(tracking.overlay_path(event, env), apply)


def _enabled(cfg: Config) -> bool:
    return flag(cfg.get("boundary.enabled"), True)


def on_prompt(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """UserPromptSubmit (after tracking and the governor): maybe recommend /compact."""
    env = os.environ if env is None else env
    if not _enabled(cfg) or event.prompt is None:
        return Action()
    try:
        path = tracking.overlay_path(event, env)
        overlay = session.read_session(path)
        st = governor.cached(overlay, cfg, env, event.session_id)
        sctx = tracking.session_ctx(event, overlay, path)
        prompt = event.prompt[: tracking.PROMPT_MAX_CHARS]
        hit = (
            st is not None
            and not st.catching_up
            and not st.window_unknown
            and detect(prompt, sctx, cfg, ctx_pct=st.ctx_pct)
        )
        history = _int_cfg(cfg, "boundary.history", 3)
        _remember(event, env, identifiers(prompt), history, sctx.turn if hit else None)
    except (jsonstore.StoreBusy, OSError) as exc:  # advisory: fail open
        log_error(env, "boundary", f"{type(exc).__name__}: {exc}")
        return Action()
    if not hit or st is None:
        return Action()
    return Action(
        user_message=(
            f"token-sparen: this prompt looks like a new task and context is "
            f"{round(st.ctx_pct * 100)} % full; /compact (or /clear) before starting it keeps "
            "the next turns cheaper."
        )
    )


def on_pre_compact(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """Summary instructions: keep recovery paths and the list of files edited."""
    env = os.environ if env is None else env
    try:
        overlay = session.read_session(tracking.overlay_path(event, env))
    except OSError as exc:
        log_error(env, "boundary", f"{type(exc).__name__}: {exc}")
        return Action()
    edited = tracking.edited_paths(overlay)
    trims = overlay.get("trims")
    if not edited and not (isinstance(trims, int) and trims > 0):
        return Action()  # nothing to keep: let the host summarise as usual
    text = INSTRUCTIONS
    if edited:
        listed = "\n".join(f"- {p}" for p in edited[-MAX_EDITED:])
        text += f"\nAlso keep the list of files edited in this session:\n{listed}"
    return Action(compact_instructions=text[:8000])
