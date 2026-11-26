"""Session budget governor and burn meter (spec 6.4). Advisory by default.

Burn = sum of billed context tokens (input + cache_read + cache_write) over the session's API
calls, read from the transcript. Two kinds of thresholds:

- *risk*: ``ctx_pct`` = the last call's context / the context window (``ctx_notice``,
  ``ctx_advise``);
- *economy*: burn / ``session_budget_tokens`` (``econ_notice``, ``econ_advise``; ``stop`` at
  >= 100 % only when the user sets ``hard_stop = true``).

Each level is announced once per session as a terminal-only ``user_message`` (zero model tokens).
The opt-in hard stop denies context-adding tools on PreToolUse, never edits.

The transcript is parsed incrementally: the session overlay caches the byte offset reached, the
running burn, the last call and the EWMA of per-turn burn, so a hook parses only new bytes. A
smaller file, a changed head or another path restarts from 0. A first contact with more than
``max_unparsed_bytes`` unparsed is skipped (``burn_unknown``; the last call is looked up in the
tail); a catch-up parse stops after ``parse_budget_ms`` and continues in the next hook.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from token_sparen.config import Config, log_error, state_home
from token_sparen.events import Action, Event, SessionCtx
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, session

Level = Literal["ok", "notice", "advise", "stop"]
DEFAULT_WINDOW = 200_000
DEFAULT_BUDGET = 500_000_000
ALPHA = 0.3
HEAD_BYTES = 256
MAX_IDS = 64
CHUNK = 4 << 20
CACHE = "governor"
ADVISED = "governor_advised"
# Context-adding tools the opt-in hard stop denies (plus every MCP tool); never edit tools.
STOP_TOOLS = frozenset(
    {"Read", "Bash", "WebFetch", "WebSearch", "Task", "Agent", "Grep", "Glob", "LS"}
)


def _stops(name: str) -> bool:
    return name in STOP_TOOLS or name.startswith("mcp__")


# The parse-budget clock (module level so tests can drive the deadline deterministically).
_monotonic = time.monotonic
_RANK: dict[Level, int] = {"ok": 0, "notice": 1, "advise": 2, "stop": 3}


@dataclass(frozen=True)
class GovernorState:
    burn: int
    per_turn_ewma: float
    window: int
    ctx_pct: float
    level: Level
    budget: int = DEFAULT_BUDGET
    economy_pct: float = 0.0
    burn_unknown: bool = False
    catching_up: bool = False
    last_turn_burn: int | None = None
    last_ctx: int = 0
    ctx_level: Level = "ok"
    econ_level: Level = "ok"
    window_unknown: bool = False  # the last call exceeded the resolved window: no ctx advice


# --------------------------------------------------------------------------- config helpers


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pos_int(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    v = _int(value)
    return v if v is not None and v > 0 else None


def _num(cfg: Config, key: str, default: float) -> float:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _enabled(cfg: Config) -> bool:
    return cfg.get("governor.enabled", True) is not False


def _hard_stop(cfg: Config) -> bool:
    return cfg.get("governor.hard_stop", False) is True


# --------------------------------------------------------------------------- transcript parsing


def _head(fh: BinaryIO, length: int) -> str:
    """Digest of the first ``min(length, HEAD_BYTES)`` bytes (detects a replaced file)."""
    fh.seek(0)
    data = fh.read(max(0, min(length, HEAD_BYTES)))
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def _path_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _usage(line: bytes) -> tuple[str, int, str | None] | None:
    """(message id, context tokens, model) of a main-chain assistant record, else None."""
    if b'"assistant"' not in line or b'"usage"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    mid, usage, model = msg.get("id"), msg.get("usage"), msg.get("model")
    if not isinstance(mid, str) or not isinstance(usage, dict) or model == "<synthetic>":
        return None
    ctx = sum(
        _int(usage.get(k)) or 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return mid, ctx, model if isinstance(model, str) else None


def _fresh() -> dict[str, Any]:
    return {"offset": 0, "burn": 0, "ids": {}, "last_ctx": 0, "model": None}


def _apply_line(cache: dict[str, Any], line: bytes, count_burn: bool = True) -> None:
    got = _usage(line)
    if got is None:
        return
    mid, ctx, model = got
    ids: dict[str, int] = cache["ids"]
    if count_burn:
        prior = ids.pop(mid, None)
        cache["burn"] += ctx - (prior or 0)
        ids[mid] = ctx
        while len(ids) > MAX_IDS:
            ids.pop(next(iter(ids)))
    cache["last_ctx"] = ctx
    if model is not None:
        cache["model"] = model


def _next_newline(fh: BinaryIO, pos: int, size: int) -> int | None:
    """Position of the first ``\n`` at or after ``pos`` (streamed, nothing kept), else None."""
    fh.seek(pos)
    while pos < size:
        piece = fh.read(min(CHUNK, size - pos))
        if not piece:
            return None
        hit = piece.find(b"\n")
        if hit >= 0:
            return pos + hit
        pos += len(piece)
    return None


def _scan(fh: BinaryIO, cache: dict[str, Any], size: int, deadline: float, max_line: int) -> bool:
    """Parse complete lines from ``cache['offset']``; False if stopped early by the deadline.

    Every read starts at the offset reached (end of the last complete line), so a record split
    by a chunk boundary is read whole by the next read. A line longer than a chunk is read whole
    when it is at most ``max_line`` bytes; a longer one is skipped as unparseable. An incomplete
    last line waits for its newline.
    """
    offset = int(cache["offset"])
    while offset < size:
        fh.seek(offset)
        chunk = fh.read(min(CHUNK, size - offset))
        if not chunk:
            break
        end = chunk.rfind(b"\n")
        if end >= 0:
            for line in chunk[: end + 1].splitlines():
                _apply_line(cache, line)
            offset += end + 1
        else:
            newline = _next_newline(fh, offset + len(chunk), size)
            if newline is None:
                break  # an incomplete last line: wait for its newline
            if newline - offset <= max_line:
                fh.seek(offset)
                _apply_line(cache, fh.read(newline - offset))
            offset = newline + 1
        cache["offset"] = offset
        if _monotonic() > deadline and offset < size:
            return False
    return True


def _tail_last_call(fh: BinaryIO, cache: dict[str, Any], size: int, tail: int) -> None:
    start = max(0, size - tail)
    fh.seek(start)
    data = fh.read(size - start)
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line is cut
    for line in reversed(lines):
        if _usage(line) is not None:
            _apply_line(cache, line, count_burn=False)
            return


def _advance(
    cache: dict[str, Any] | None, transcript: Path, cfg: Config, started: float
) -> dict[str, Any] | None:
    """Bring ``cache`` up to date with the transcript (a new dict), or None if unreadable."""
    try:
        fh = transcript.open("rb")
    except OSError:
        return None
    with fh:
        size = os.fstat(fh.fileno()).st_size
        pid = _path_id(transcript)
        offset = _int(cache.get("offset")) if cache is not None else None
        if (
            cache is None
            or offset is None
            or cache.get("path") != pid
            or offset > size
            or cache.get("head") != _head(fh, offset)
            or not isinstance(cache.get("ids"), dict)
            or _int(cache.get("burn")) is None
        ):
            new = {**_fresh(), "path": pid}  # restart: burn, EWMA and turn state reset
        else:
            new = {**cache, "ids": dict(cache["ids"])}
        new["size"] = size
        unparsed = size - int(new["offset"])
        max_unparsed = _num(cfg, "governor.max_unparsed_bytes", 50_000_000)
        if new["offset"] == 0 and unparsed > max_unparsed:
            # first contact with a huge transcript: skip, the burn is unknown for this session
            tail = int(_num(cfg, "governor.tail_scan_bytes", 1_000_000))
            _tail_last_call(fh, new, size, tail)
            new["offset"] = size
            new["burn_unknown"] = True
            new["catching_up"] = False
        else:
            budget = _num(cfg, "governor.parse_budget_ms", 150) / 1000
            done = _scan(fh, new, size, started + budget, int(max_unparsed))
            new["catching_up"] = not done
        new["head"] = _head(fh, int(new["offset"]))
        return new


# --------------------------------------------------------------------------- window and levels


def _windows_json(env: Mapping[str, str], session_id: str) -> int | None:
    path = state_home(env) / "windows.json"
    if not path.is_file():
        return None
    entry = jsonstore.read(path, {}, env).get(session_id)
    if isinstance(entry, dict):
        entry = entry.get("window")
    return _pos_int(entry) if not isinstance(entry, str) else None


def resolve_window(cfg: Config, env: Mapping[str, str], session_id: str, model: str | None) -> int:
    """Statusline-recorded window -> CLAUDE_CODE_MAX_CONTEXT_TOKENS -> model table -> 200 000."""
    recorded = _windows_json(env, session_id)
    if recorded is not None:
        return recorded
    from_env = _pos_int(env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
    if from_env is not None:
        return from_env
    table = cfg.get("governor.windows")
    if isinstance(table, dict):
        if model:
            for pattern, value in table.items():
                if pattern != "default" and fnmatch.fnmatchcase(model, pattern):
                    got = _pos_int(value)
                    if got is not None:
                        return got
        got = _pos_int(table.get("default"))
        if got is not None:
            return got
    return DEFAULT_WINDOW


def _level(value: float, notice: float, advise: float) -> Level:
    if value >= advise:
        return "advise"
    return "notice" if value >= notice else "ok"


def _build(
    cache: Mapping[str, Any], cfg: Config, env: Mapping[str, str], session_id: str
) -> GovernorState:
    model = cache.get("model")
    window = resolve_window(cfg, env, session_id, model if isinstance(model, str) else None)
    last_ctx = _int(cache.get("last_ctx")) or 0
    burn = _int(cache.get("burn")) or 0
    budget = _pos_int(cfg.get("governor.session_budget_tokens")) or DEFAULT_BUDGET
    unknown = cache.get("burn_unknown") is True
    ctx_pct = last_ctx / window
    econ_pct = 0.0 if unknown else burn / budget
    window_unknown = last_ctx > window  # the resolved window is wrong for this model
    ctx_level: Level = (
        "ok"
        if window_unknown
        else _level(
            ctx_pct, _num(cfg, "governor.ctx_notice", 0.6), _num(cfg, "governor.ctx_advise", 0.8)
        )
    )
    econ_level = _level(
        econ_pct, _num(cfg, "governor.econ_notice", 0.5), _num(cfg, "governor.econ_advise", 0.8)
    )
    if not unknown and econ_pct >= 1.0 and _hard_stop(cfg):
        econ_level = "stop"
    level = max(ctx_level, econ_level, key=_RANK.__getitem__)
    ewma = cache.get("ewma")
    return GovernorState(
        burn=burn,
        per_turn_ewma=float(ewma) if isinstance(ewma, int | float) else 0.0,
        window=window,
        ctx_pct=ctx_pct,
        level=level,
        budget=budget,
        economy_pct=econ_pct,
        burn_unknown=unknown,
        catching_up=cache.get("catching_up") is True,
        last_turn_burn=_int(cache.get("last_turn_burn")),
        last_ctx=last_ctx,
        ctx_level=ctx_level,
        econ_level=econ_level,
        window_unknown=window_unknown,
    )


def cached(
    overlay: Mapping[str, Any],
    cfg: Config | None = None,
    env: Mapping[str, str] | None = None,
    session_id: str = "",
) -> GovernorState | None:
    """The state as of the last update stored in the overlay (no transcript I/O)."""
    cache = overlay.get(CACHE)
    if not isinstance(cache, dict):
        return None
    return _build(cache, cfg or Config(), os.environ if env is None else env, session_id)


def remaining_turns(overlay: Mapping[str, Any], cfg: Config | None = None) -> float | None:
    """Projected turns left in the session budget at the per-turn burn EWMA (no I/O), or None
    when unknown (no cache, no EWMA yet, burn unknown)."""
    cache = overlay.get(CACHE)
    if not isinstance(cache, dict) or cache.get("burn_unknown") is True:
        return None
    ewma = cache.get("ewma")
    if isinstance(ewma, bool) or not isinstance(ewma, int | float) or ewma <= 0:
        return None
    burn = _int(cache.get("burn")) or 0
    budget = _pos_int((cfg or Config()).get("governor.session_budget_tokens")) or DEFAULT_BUDGET
    return max(0.0, (budget - burn) / float(ewma))


def _turn_boundary(cache: dict[str, Any], cfg: Config) -> None:
    """UserPromptSubmit: close the previous turn (its burn feeds the EWMA), open a new one."""
    burn = int(cache["burn"])
    start = _int(cache.get("turn_start_burn"))
    if start is not None and not cache.get("burn_unknown"):
        turn_burn = max(0, burn - start)
        alpha = _num(cfg, "governor.ewma_alpha", ALPHA)
        prior = cache.get("ewma")
        cache["ewma"] = (
            float(turn_burn)
            if not isinstance(prior, int | float)
            else alpha * turn_burn + (1 - alpha) * float(prior)
        )
        cache["last_turn_burn"] = turn_burn
    cache["turn_start_burn"] = burn


def state(
    session_ctx: SessionCtx,
    transcript: Path,
    cfg: Config | None = None,
    env: Mapping[str, str] | None = None,
    *,
    turn_boundary: bool = False,
) -> GovernorState:
    """Update the cached burn meter from new transcript bytes and return the current state.

    Raises OSError / StoreBusy when the overlay cannot be updated (callers fail open).
    """
    cfg = cfg or Config()
    env = os.environ if env is None else env
    started = _monotonic()
    overlay = session.read_session(session_ctx.overlay_path)
    prior = overlay.get(CACHE)
    if not turn_boundary and isinstance(prior, dict) and _unchanged(prior, transcript):
        return _build(prior, cfg, env, session_ctx.session_id)
    result: dict[str, Any] = {}

    def apply(st: dict[str, Any]) -> dict[str, Any]:
        current = st.get(CACHE)
        new = _advance(current if isinstance(current, dict) else None, transcript, cfg, started)
        if new is None:
            new = current if isinstance(current, dict) else {**_fresh()}
        if turn_boundary and not new.get("catching_up"):
            _turn_boundary(new, cfg)
        st[CACHE] = new
        result.update(new)
        return st

    session.update_session(session_ctx.overlay_path, apply)
    return _build(result, cfg, env, session_ctx.session_id)


def _unchanged(cache: Mapping[str, Any], transcript: Path) -> bool:
    """Nothing new to parse (cheap stat; the head is checked whenever the size changed)."""
    if cache.get("path") != _path_id(transcript) or cache.get("catching_up"):
        return False
    try:
        size = transcript.stat().st_size
    except OSError:
        return True  # unreadable now: keep the cached view
    return cache.get("size") == size and cache.get("offset") == size


# --------------------------------------------------------------------------- advisories


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(int(n))


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def _message(kind: str, level: Level, st: GovernorState) -> str:
    if kind == "ctx":
        head = (
            f"token-sparen: context is {_pct(st.ctx_pct)} full "
            f"({_fmt(st.last_ctx)} of {_fmt(st.window)} tokens); "
        )
        if level == "notice":
            return head + "consider /compact at the next natural break."
        return head + "run /compact now (or /clear before an unrelated task)."
    head = (
        f"token-sparen: this session has used {_fmt(st.burn)} of its {_fmt(st.budget)} "
        f"token budget ({_pct(st.economy_pct)})"
    )
    if level == "notice":
        return head + "; /compact or a fresh session keeps each turn cheaper."
    eta = ""
    if st.per_turn_ewma > 0:
        turns = max(0.0, (st.budget - st.burn) / st.per_turn_ewma)
        eta = (
            ", under 1 turn left at the current pace"
            if turns < 1
            else f", about {int(turns)} turns left at the current pace"
        )
    return head + eta + "; /compact or start a fresh session."


def _seen(overlay: Mapping[str, Any]) -> list[str]:
    raw = overlay.get(ADVISED)
    return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []


def _advise(
    event: Event, st: GovernorState, env: Mapping[str, str], overlay: Mapping[str, Any]
) -> str | None:
    """Announce each level once per session (the overlay records what was said).

    ``overlay`` is the lock-free read taken before this hook's update: when every wanted level
    is already in it, no lock is taken (the common case on every PostToolUse).
    """
    if st.catching_up:
        return None
    wanted: list[tuple[str, Level]] = []
    for kind, level in (("ctx", st.ctx_level), ("econ", st.econ_level)):
        if kind == "econ" and st.burn_unknown:
            continue
        if level == "stop":
            level = "advise"
        if level != "ok":
            wanted.append((kind, level))
    already = _seen(overlay)
    if all(f"{kind}:{level}" in already for kind, level in wanted):
        return None
    lines: list[str] = []

    def apply(state_: dict[str, Any]) -> dict[str, Any]:
        lines.clear()
        seen = _seen(state_)
        for kind, level in wanted:
            key = f"{kind}:{level}"
            if key in seen:
                continue
            lines.append(_message(kind, level, st))
            implied = [f"{kind}:notice", key] if level == "advise" else [key]
            seen.extend(k for k in implied if k not in seen)
        state_[ADVISED] = seen
        return state_

    session.update_session(tracking.overlay_path(event, env), apply)
    return " ".join(lines) if lines else None


def _session_state(
    event: Event, cfg: Config, env: Mapping[str, str], *, turn_boundary: bool = False
) -> tuple[GovernorState, dict[str, Any]] | None:
    """(state, the overlay as read before the update), or None when there is nothing to meter."""
    if event.transcript_path is None or not _enabled(cfg):
        return None
    if not event.transcript_path.is_file():
        return None
    path = tracking.overlay_path(event, env)
    overlay = session.read_session(path)
    sctx = tracking.session_ctx(event, overlay, path)
    st = state(sctx, event.transcript_path, cfg, env, turn_boundary=turn_boundary)
    return st, overlay


def _advisory(event: Event, cfg: Config, env: Mapping[str, str] | None, boundary: bool) -> Action:
    env = os.environ if env is None else env
    try:
        got = _session_state(event, cfg, env, turn_boundary=boundary)
        if got is None:
            return Action()
        return Action(user_message=_advise(event, got[0], env, got[1]))
    except (jsonstore.StoreBusy, OSError) as exc:  # advisory: fail open
        log_error(env, "governor", f"{type(exc).__name__}: {exc}")
        return Action()


def on_prompt(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """UserPromptSubmit: close the turn for the EWMA, then advise if a new level was reached."""
    return _advisory(event, cfg, env, True)


def on_post_tool(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """PostToolUse: context can grow fast inside one long turn; advise as soon as it does."""
    return _advisory(event, cfg, env, False)


def check_pre_tool(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """Opt-in hard stop: deny a context-adding tool once the economy budget is used up."""
    env = os.environ if env is None else env
    if not _hard_stop(cfg) or event.tool is None or not _stops(event.tool.name):
        return Action()
    try:
        got = _session_state(event, cfg, env)
    except (jsonstore.StoreBusy, OSError) as exc:
        log_error(env, "governor", f"{type(exc).__name__}: {exc}")
        return Action()
    if got is None or got[0].level != "stop":
        return Action()
    st = got[0]
    return Action(
        block_reason=(
            f"token-sparen: session budget reached ({_fmt(st.burn)} of {_fmt(st.budget)} "
            f"tokens); {event.tool.name} is paused by the opt-in hard stop. Edits still work. "
            "To lift it, set `[governor] hard_stop = false` or raise `session_budget_tokens` in "
            "~/.token-sparen/config.toml or <project>/token-sparen.toml."
        )
    )
