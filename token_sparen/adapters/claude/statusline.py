"""Live Claude Code statusline (spec 6.12): one line per refresh, zero model tokens.

``render`` reads the statusline JSON Claude Code sends on stdin and is strictly bound to its
``session_id`` / ``transcript_path`` (never "the latest transcript"). It is also the governor's
sensor: the real context window per session is recorded in ``~/.token-sparen/windows.json``
(``{session_id: {"window": N, "model": id, "ts": epoch}}``, entries older than 7 days pruned).

Burn and EWMA come from the governor's incremental cache (only new transcript bytes are parsed,
with a short parse budget); trims, regrets and net savings from the session overlay ledger. Any
failure renders the minimal ``⛁ ts`` line. ``main`` adds compose mode: the user's original
statusline runs with the same stdin and ours is appended after `` │ ``.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from token_sparen.config import Config, flag, load_config, log_error, state_home
from token_sparen.events import SessionCtx
from token_sparen.mechanisms import governor, tracking
from token_sparen.store import jsonstore, keycache, session

GLYPH = "⛁ ts"
GLYPH_ASCII = "ts"
SEP = " │ "
SEP_ASCII = " | "
SEGMENTS = ("ctx", "burn", "rate", "saved", "trims", "learn")
LEARN_STATES = frozenset({"cold", "adapting", "exploiting", "safe-mode"})
WINDOWS = "windows.json"
WINDOW_TTL_S = 7 * 86400
WINDOW_REFRESH_S = 3600  # rewrite an unchanged entry at most hourly (keeps ts fresh for pruning)
COMPOSE_TIMEOUT_S = 1.0
DEFAULT_PARSE_BUDGET_MS = 20
GREEN, YELLOW, RED, RESET = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[0m"
_TRUE = {"1", "true"}


# --------------------------------------------------------------------------- small helpers


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _trim(text: str) -> str:
    return text[:-2] if text.endswith(".0") else text


def fmt_tokens(n: float) -> str:
    """Compact token count: 812, 2.4k, 31.2k, 168k, 48M, 1.5M, 1.2B (sign kept)."""
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        text = str(round(n))
    elif n < 99_950:
        text = _trim(f"{n / 1000:.1f}") + "k"
    elif n < 999_500:
        text = f"{round(n / 1000)}k"
    elif n < 999_950_000:
        text = _trim(f"{n / 1_000_000:.1f}") + "M"
    else:
        text = _trim(f"{n / 1_000_000_000:.1f}") + "B"
    return sign + text


def _colour(pct: float) -> str:
    return GREEN if pct < 50 else YELLOW if pct < 75 else RED


def _minimal(ascii_: bool) -> str:
    return GLYPH_ASCII if ascii_ else GLYPH


def _disabled(env: Mapping[str, str]) -> bool:
    return env.get("TOKEN_SPAREN_DISABLE", "").strip().lower() in _TRUE


def _binding(payload: Mapping[str, Any]) -> tuple[str, Path] | None:
    sid, transcript = _str(payload.get("session_id")), _str(payload.get("transcript_path"))
    if sid is None or transcript is None:
        return None
    return sid, Path(transcript)


def _project_dir(payload: Mapping[str, Any], env: Mapping[str, str]) -> Path | None:
    """Same basis as the hooks: CLAUDE_PROJECT_DIR, else the workspace project dir, else cwd."""
    raw = (
        _str(env.get("CLAUDE_PROJECT_DIR"))
        or _str(_dict(payload.get("workspace")).get("project_dir"))
        or _str(payload.get("cwd"))
    )
    return Path(raw) if raw else None


# --------------------------------------------------------------------------- sensor


def record_window(
    env: Mapping[str, str], session_id: str, model: str | None, window: int, now: float
) -> None:
    """Record the session's real context window for the governor (locked, pruned)."""
    path = state_home(env) / WINDOWS
    if path.is_file():
        entry = jsonstore.read(path, {}, env).get(session_id)
        if isinstance(entry, dict):
            ts = _num(entry.get("ts"))
            if (
                entry.get("window") == window
                and entry.get("model") == model
                and ts is not None
                and 0 <= now - ts < WINDOW_REFRESH_S
            ):
                return  # nothing new: no lock, no write

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        cutoff = now - WINDOW_TTL_S
        kept: dict[str, Any] = {}
        for key, value in data.items():
            ts = _num(value.get("ts")) if isinstance(value, dict) else None
            if ts is not None and ts < cutoff:
                continue
            kept[key] = value
        kept[session_id] = {"window": window, "model": model, "ts": now}
        return kept

    jsonstore.update(path, apply, {}, env)


# --------------------------------------------------------------------------- state sources


_LEARNER: list[Callable[[str, Path], Any] | None] = []


def _learner_state(project_key: str, env: Mapping[str, str]) -> str:
    """``learn.overlay.learner_state`` for the project (state root from ``env``), else ``cold``."""
    if not _LEARNER:
        try:
            module = importlib.import_module("token_sparen.learn.overlay")
            fn = getattr(module, "learner_state", None)
        except ImportError:
            fn = None
        _LEARNER.append(fn if callable(fn) else None)
    fn = _LEARNER[0]
    if fn is None:
        return "cold"
    try:
        value = fn(project_key, state_home(env))
    except Exception as exc:  # noqa: BLE001 - a learner bug must not break the statusline
        log_error(env, "statusline", f"learner_state: {type(exc).__name__}: {exc}")
        return "cold"
    return value if isinstance(value, str) and value in LEARN_STATES else "cold"


def _governor_cfg(cfg: Config) -> Config:
    """The project config with the statusline's (shorter) transcript parse budget."""
    budget = _num(cfg.get("statusline.parse_budget_ms", DEFAULT_PARSE_BUDGET_MS))
    gov = _dict(cfg.get("governor"))
    current = _num(gov.get("parse_budget_ms"))
    limit = budget if budget is not None and budget > 0 else DEFAULT_PARSE_BUDGET_MS
    value = min(limit, current) if current is not None and current > 0 else limit
    return Config(data={**cfg.data, "governor": {**gov, "parse_budget_ms": value}})


def _ascii(cfg: Config) -> bool:
    return flag(cfg.get("statusline.ascii"), False)


def _use_colour(cfg: Config, env: Mapping[str, str]) -> bool:
    return not _str(env.get("NO_COLOR")) and flag(cfg.get("statusline.color"), True)


def _governor_state(
    sctx: SessionCtx, transcript: Path, cfg: Config, env: Mapping[str, str]
) -> governor.GovernorState | None:
    if not flag(cfg.get("governor.enabled"), True):
        return None
    if transcript.is_file():
        try:
            return governor.state(sctx, transcript, _governor_cfg(cfg), env)
        except (jsonstore.StoreBusy, OSError) as exc:
            log_error(env, "statusline", f"governor: {type(exc).__name__}: {exc}")
    return governor.cached(session.read_session(sctx.overlay_path), cfg, env, sctx.session_id)


def _ledger(overlay: Mapping[str, Any], key: str) -> int:
    return _int(overlay.get(key)) or 0


# --------------------------------------------------------------------------- render


def _segments(cfg: Config) -> list[str]:
    raw = cfg.get("statusline.segments")
    if isinstance(raw, list) and all(isinstance(s, str) for s in raw):
        return [s for s in raw if s in SEGMENTS]
    return list(SEGMENTS)


def _ctx_pct(cw: Mapping[str, Any], st: governor.GovernorState | None) -> float | None:
    used = _num(cw.get("used_percentage"))
    if used is not None and used >= 0:
        return used
    if st is not None and st.last_ctx > 0 and not st.window_unknown:
        return st.ctx_pct * 100
    return None


def _usd_per_token(payload: Mapping[str, Any]) -> float | None:
    """Estimate from this session's own spend: total cost / (input + output tokens)."""
    cost = _num(_dict(payload.get("cost")).get("total_cost_usd"))
    cw = _dict(payload.get("context_window"))
    tokens = (_int(cw.get("total_input_tokens")) or 0) + (_int(cw.get("total_output_tokens")) or 0)
    if cost is None or cost <= 0 or tokens <= 0:
        return None
    return cost / max(1, tokens)


def _usd(value: float) -> str:
    return f"≈-${-value:.2f}" if value < 0 else f"≈${value:.2f}"


def _render(payload: Mapping[str, Any], env: Mapping[str, str]) -> tuple[str, bool]:
    """(line, ascii); raises on internal errors (``render`` turns them into the minimal line)."""
    if _disabled(env):
        return GLYPH, False
    bound = _binding(payload)
    if bound is None:
        return GLYPH, False
    session_id, transcript = bound
    project_dir = _project_dir(payload, env)
    cfg = load_config(project_dir if project_dir is not None else state_home(env), env)
    ascii_ = _ascii(cfg)
    if cfg.disabled:
        return _minimal(ascii_), ascii_
    model_id = _str(_dict(payload.get("model")).get("id"))
    cw = _dict(payload.get("context_window"))
    size = _int(cw.get("context_window_size"))
    if size is not None and size > 0:
        try:
            record_window(env, session_id, model_id, size, time.time())
        except (jsonstore.StoreBusy, OSError) as exc:
            log_error(env, "statusline", f"windows: {type(exc).__name__}: {exc}")

    st: governor.GovernorState | None = None
    overlay: dict[str, Any] = {}
    project_key = ""
    if project_dir is not None:
        project_key = keycache.cached_project_key(project_dir, env)
        path = session.session_path(state_home(env), project_key, session_id)
        sctx = SessionCtx(session_id, project_key, 0, None, model_id, path)
        st = _governor_state(sctx, transcript, cfg, env)
        overlay = session.read_session(path)

    colour = _use_colour(cfg, env)
    head = _minimal(ascii_)
    parts: list[str] = []
    for name in _segments(cfg):
        if name == "ctx":
            pct = _ctx_pct(cw, st)
            if pct is not None:
                text = f"{round(pct)}% ctx"
                head += " " + (f"{_colour(pct)}{text}{RESET}" if colour else text)
        elif name == "burn" and st is not None:
            burn = "?" if st.burn_unknown else fmt_tokens(st.burn)
            pct_text = "" if st.burn_unknown else f" {round(st.economy_pct * 100)}%"
            parts.append(f"burn {burn}/{fmt_tokens(st.budget)}{pct_text}")
        elif name == "rate":
            bits = []
            if st is not None and st.per_turn_ewma > 0:
                bits.append(f"+{fmt_tokens(st.per_turn_ewma)}/turn")
            five = _num(
                _dict(_dict(payload.get("rate_limits")).get("five_hour")).get("used_percentage")
            )
            if five is not None:
                bits.append(f"5h {round(five)}%")
            if bits:
                parts.append(" ".join(bits))
        elif name == "saved":
            net = tracking.ledger_savings(overlay)[1]
            text = f"saved {fmt_tokens(net)}"
            per_token = _usd_per_token(payload)
            if per_token is not None and net != 0:
                text += f" ({_usd(net * per_token)})"
            parts.append(text)
        elif name == "trims":
            trim, regret = ("x", "r") if ascii_ else ("✂", "↺")
            parts.append(
                f"{trim} {_ledger(overlay, 'trims')} {regret} {_ledger(overlay, 'regrets')}"
            )
        elif name == "learn":
            parts.append(f"learn: {_learner_state(project_key, env)}")
    line = (SEP_ASCII if ascii_ else SEP).join([head, *parts])
    return " ".join(line.splitlines()), ascii_


def render(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """One statusline line; never raises (any failure renders the minimal ``⛁ ts``)."""
    return _render_safe(payload, os.environ if env is None else env)[0]


def _render_safe(payload: Mapping[str, Any], env: Mapping[str, str]) -> tuple[str, bool]:
    try:
        return _render(payload, env)
    except Exception as exc:  # noqa: BLE001 - the statusline must always print a line
        log_error(env, "statusline", f"{type(exc).__name__}: {exc}")
        return GLYPH, False


# --------------------------------------------------------------------------- compose + main


def _start_original(command: str, env: Mapping[str, str]) -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        return None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)  # the whole group: no orphan holds the pipe
        else:  # pragma: no cover - compose mode is POSIX-only in v1
            proc.kill()
    with contextlib.suppress(Exception):
        proc.communicate(timeout=1)


def _original_line(proc: subprocess.Popen[bytes], stdin: bytes, deadline: float) -> str | None:
    try:
        out, _ = proc.communicate(stdin, timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill(proc)
        return None
    except (OSError, ValueError):
        _kill(proc)
        return None
    if proc.returncode != 0:
        return None
    lines = out.decode("utf-8", "replace").splitlines()
    first = lines[0].rstrip() if lines else ""
    return first or None


def _read_bytes(stdin: TextIO) -> bytes:
    """The exact stdin bytes (forwarded unchanged to a composed original)."""
    try:
        buffer = getattr(stdin, "buffer", None)
        if buffer is not None:
            data = buffer.read()
            return data if isinstance(data, bytes) else b""
        return stdin.read().encode("utf-8", "surrogateescape")
    except Exception:  # noqa: BLE001
        return b""


def _compose_command(argv: list[str]) -> str | None:
    if argv[:1] != ["--compose"]:
        return None
    rest = argv[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return " ".join(rest) if rest else None


def main(
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Statusline command: read the JSON payload, print one line, exit 0."""
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    started = time.monotonic()
    raw = _read_bytes(stdin)
    command = _compose_command(argv)
    proc = _start_original(command, env) if command else None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - incl. RecursionError on absurdly nested input
        payload = {}
    line, ascii_ = _render_safe(payload if isinstance(payload, dict) else {}, env)
    if proc is not None:
        first = _original_line(proc, raw, started + COMPOSE_TIMEOUT_S)
        if first is not None:
            line = f"{first}{SEP_ASCII if ascii_ else SEP}{line}"
    with contextlib.suppress(Exception):
        stdout.write(line + "\n")
        stdout.flush()
    return 0
