"""Run metrics from Claude Code's ``--output-format stream-json --verbose --include-hook-events``.

Field names follow the documented Agent SDK message types: ``system/init`` (``session_id``,
``model``, ``claude_code_version``, ``plugins[{name, path}]``, ``mcp_servers[{name, status}]``,
``skills``), ``assistant`` (``message.usage``, optional ``error``), ``system/hook_response``
(``hook_event``, ``hook_name``, ``exit_code``, ``outcome``), ``system/api_retry``
(``attempt``, ``max_retries``, ``error_status``), ``rate_limit_event`` (``rate_limit_info``:
``status``, ``rateLimitType``, ``resetsAt``, ``utilization``) and ``result`` (``subtype``,
``is_error``, ``total_cost_usd``, ``usage``, ``modelUsage``, ``num_turns``, ``duration_ms``,
``errors``). Fields that are not documented and are read only when present (``plugin_errors``
on init, ``command`` on hook events, ``api_error_status`` on result) are calibrated against real
streams in Task 13. `parse_stream` never raises: a line that is not a JSON object, or whose
fields have the wrong types, is counted in ``malformed_lines``.

Cost per run (§10.5) = ``total_cost_usd`` of the main session + the plugin's side-call sessions
found in the run's config dir (`overhead_from_config_dir`), list price, cache-aware.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from token_sparen.estimate import Prices

FIVE_HOURS_S = 5 * 3600.0
MAX_ERROR_TEXT = 2000
# USD per input token by model family (list price; the other classes follow
# `token_sparen.estimate.Prices` ratios). First matching key (substring of the model id) wins;
# an unknown model is priced at the highest rate (never understates a side call). Rates measured
# from the run's own ``modelUsage`` take precedence (`usd_rates`); Task 13 checks this table.
USD_PER_INPUT_TOKEN: Mapping[str, float] = {
    "opus-4-1": 15e-6,
    "opus-4-2025": 15e-6,
    "opus-3": 15e-6,
    "opus": 5e-6,
    "sonnet": 3e-6,
    "haiku-3-5": 0.8e-6,
    "haiku-3": 0.25e-6,
    "haiku": 1e-6,
}
_API_ERROR = re.compile(r"API Error: (\d{3})\b")
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(
            self.input + other.input,
            self.output + other.output,
            self.cache_creation + other.cache_creation,
            self.cache_read + other.cache_read,
        )

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_creation + self.cache_read

    def weighted(self, prices: Prices) -> float:
        """Input-token equivalents at ``prices`` (relative list-price ratios)."""
        return (
            self.input * prices.input
            + self.output * prices.output
            + self.cache_creation * prices.cache_write
            + self.cache_read * prices.cache_read
        )


@dataclass(frozen=True)
class RateLimit:
    status: str
    kind: Literal["five_hour", "weekly", "unknown"]
    resets_at: float | None
    utilization: float | None


@dataclass(frozen=True)
class HookEvent:
    event: str
    command: str
    exit_code: int | None
    ok: bool


@dataclass(frozen=True)
class ApiRetry:
    attempt: int | None
    max_retries: int | None
    error_status: int | None


@dataclass(frozen=True)
class ModelUsage:
    tokens: Tokens
    cost_usd: float | None


@dataclass(frozen=True)
class RunMetrics:
    session_id: str | None = None
    model: str | None = None
    claude_code_version: str | None = None
    has_init: bool = False
    plugins: dict[str, str] = field(default_factory=dict)  # name -> path
    plugin_errors: list[str] = field(default_factory=list)
    mcp_servers: dict[str, str] = field(default_factory=dict)  # name -> status
    skills: list[str] = field(default_factory=list)
    hook_events: list[HookEvent] = field(default_factory=list)
    subtype: str | None = None
    is_error: bool = False
    api_error_status: int | None = None
    error_text: str | None = None
    total_cost_usd: float | None = None
    tokens: Tokens = Tokens()
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)
    num_turns: int | None = None
    duration_ms: int | None = None
    rate_limits: list[RateLimit] = field(default_factory=list)
    api_retries: list[ApiRetry] = field(default_factory=list)
    malformed_lines: int = 0
    overhead: Tokens = Tokens()
    overhead_cost_usd: float = 0.0

    @property
    def api_retry_exhausted(self) -> bool:
        return any(
            r.attempt is not None and r.max_retries is not None and r.attempt >= r.max_retries
            for r in self.api_retries
        )


class _Malformed(Exception):
    pass


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(value: object) -> int:
    n = _int(value)
    return n if n is not None and n > 0 else 0


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _usage_tokens(usage: Mapping[str, Any]) -> Tokens:
    return Tokens(
        input=_count(usage.get("input_tokens")),
        output=_count(usage.get("output_tokens")),
        cache_creation=_count(usage.get("cache_creation_input_tokens")),
        cache_read=_count(usage.get("cache_read_input_tokens")),
    )


def _model_usage_tokens(usage: Mapping[str, Any]) -> Tokens:
    return Tokens(
        input=_count(usage.get("inputTokens")),
        output=_count(usage.get("outputTokens")),
        cache_creation=_count(usage.get("cacheCreationInputTokens")),
        cache_read=_count(usage.get("cacheReadInputTokens")),
    )


def _named(items: object, value_key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _list(items):
        if isinstance(item, str):
            out[item] = ""
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = str(item.get(value_key) or "")
    return out


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [
        b["text"]
        for b in _list(content)
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]


def _resets_at(value: object) -> float | None:
    t = _num(value)
    if t is not None and t > 1e11:  # milliseconds
        t /= 1000.0
    return t


def _rate_kind(
    rate_type: str | None, resets_at: float | None, observed_at: float
) -> Literal["five_hour", "weekly", "unknown"]:
    if rate_type is not None:
        if rate_type == "five_hour":
            return "five_hour"
        if rate_type.startswith("seven_day") or rate_type == "weekly":
            return "weekly"
        return "unknown"
    if resets_at is None:
        return "unknown"
    return "weekly" if resets_at - observed_at > FIVE_HOURS_S else "five_hour"


class _Parser:
    def __init__(self, observed_at: float) -> None:
        self.observed_at = observed_at
        self.fields: dict[str, Any] = {}
        self.hooks: list[HookEvent] = []
        self.rates: list[RateLimit] = []
        self.retries: list[ApiRetry] = []
        self.errors: list[str] = []
        self.stray: list[str] = []
        self.assistant: dict[str, Tokens] = {}
        self.result: dict[str, Any] | None = None
        self.malformed = 0

    def line(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            return
        try:
            event = json.loads(text)
            if not isinstance(event, dict):
                raise _Malformed
            self.event(event)
        except (ValueError, TypeError, AttributeError, KeyError, _Malformed):
            self.malformed += 1
            self.stray = [*self.stray[-4:], text[:300]]

    def event(self, e: dict[str, Any]) -> None:
        kind, sub = e.get("type"), e.get("subtype")
        if kind == "system" and sub == "init":
            self.init(e)
        elif kind == "system" and sub == "hook_response":
            self.hook(e)
        elif kind == "system" and sub == "api_retry":
            self.retries.append(
                ApiRetry(
                    _int(e.get("attempt")), _int(e.get("max_retries")), _int(e.get("error_status"))
                )
            )
        elif kind == "rate_limit_event":
            info = e.get("rate_limit_info")
            if not isinstance(info, dict):
                raise _Malformed
            resets = _resets_at(info.get("resetsAt"))
            self.rates.append(
                RateLimit(
                    status=str(info.get("status") or "unknown"),
                    kind=_rate_kind(_str(info.get("rateLimitType")), resets, self.observed_at),
                    resets_at=resets,
                    utilization=_num(info.get("utilization")),
                )
            )
        elif kind == "assistant":
            self.assistant_event(e)
        elif kind == "result":
            self.result = e

    def init(self, e: dict[str, Any]) -> None:
        if self.fields.get("has_init"):
            return
        plugins, servers = e.get("plugins", []), e.get("mcp_servers", [])
        if not isinstance(plugins, list) or not isinstance(servers, list):
            raise _Malformed
        self.fields.update(
            has_init=True,
            session_id=_str(e.get("session_id")),
            model=_str(e.get("model")),
            claude_code_version=_str(e.get("claude_code_version")),
            plugins=_named(plugins, "path"),
            plugin_errors=[
                p if isinstance(p, str) else json.dumps(p, sort_keys=True)
                for p in _list(e.get("plugin_errors"))
            ],
            mcp_servers=_named(servers, "status"),
            skills=[
                s if isinstance(s, str) else str(_dict(s).get("name", ""))
                for s in _list(e.get("skills"))
            ],
        )

    def hook(self, e: dict[str, Any]) -> None:
        name = _str(e.get("hook_name")) or ""
        event = _str(e.get("hook_event")) or name.split(":", 1)[0]
        code = _int(e.get("exit_code"))
        outcome = _str(e.get("outcome"))
        ok = outcome == "success" if outcome is not None else code == 0
        command = _str(e.get("command")) or _str(e.get("hook_command")) or name
        self.hooks.append(HookEvent(event=event, command=command, exit_code=code, ok=ok))

    def assistant_event(self, e: dict[str, Any]) -> None:
        msg = e.get("message")
        if not isinstance(msg, dict):
            raise _Malformed
        error = _str(e.get("error"))
        if error is not None or msg.get("model") == SYNTHETIC_MODEL:
            self.errors.extend(_texts(msg.get("content")))
            if error is not None:
                self.errors.append(f"assistant error: {error}")
        if msg.get("model") == SYNTHETIC_MODEL:
            return
        mid = _str(msg.get("id")) or f"#{len(self.assistant)}"
        self.assistant[mid] = _usage_tokens(_dict(msg.get("usage")))

    def metrics(self) -> RunMetrics:
        res = self.result or {}
        model_usage = {
            name: ModelUsage(_model_usage_tokens(_dict(u)), _num(_dict(u).get("costUSD")))
            for name, u in _dict(res.get("modelUsage")).items()
            if isinstance(name, str)
        }
        if model_usage:
            tokens = sum((u.tokens for u in model_usage.values()), Tokens())
        elif isinstance(res.get("usage"), dict):
            tokens = _usage_tokens(res["usage"])
        else:
            tokens = sum(self.assistant.values(), Tokens())
        is_error = res.get("is_error") is True
        errors = list(self.errors)
        if is_error:
            errors.extend(_texts(res.get("result")))
            errors.extend(str(x) for x in _list(res.get("errors")))
        if self.result is None:
            errors.extend(self.stray)
        text = " | ".join(t for t in errors if t)[:MAX_ERROR_TEXT] or None
        status = _int(res.get("api_error_status"))
        if status is None and text is not None:
            found = _API_ERROR.search(text)
            status = int(found.group(1)) if found else None
        return RunMetrics(
            **self.fields,
            hook_events=self.hooks,
            subtype=_str(res.get("subtype")),
            is_error=is_error,
            api_error_status=status,
            error_text=text,
            total_cost_usd=_num(res.get("total_cost_usd")),
            tokens=tokens,
            model_usage=model_usage,
            num_turns=_int(res.get("num_turns")),
            duration_ms=_int(res.get("duration_ms")),
            rate_limits=self.rates,
            api_retries=self.retries,
            malformed_lines=self.malformed,
        )


def parse_stream(lines: Iterable[str], *, observed_at: float) -> RunMetrics:
    """`RunMetrics` of one run's stream (never raises). ``observed_at`` (epoch s) dates the
    rate-limit readings: a reading without ``rateLimitType`` is ``weekly`` iff it resets more
    than five hours later."""
    parser = _Parser(observed_at)
    for raw in lines:
        parser.line(raw if isinstance(raw, str) else str(raw))
    return parser.metrics()


def run_cost(m: RunMetrics) -> float | None:
    """``total_cost_usd`` + side-call overhead; None when the run reported no cost."""
    if m.total_cost_usd is None:
        return None
    return m.total_cost_usd + m.overhead_cost_usd


def usd_rates(m: RunMetrics, prices: Prices | None = None) -> dict[str, float]:
    """USD per input-token equivalent per model, measured from the run's ``modelUsage``."""
    p = prices or Prices()
    out: dict[str, float] = {}
    for name, usage in m.model_usage.items():
        weighted = usage.tokens.weighted(p)
        if usage.cost_usd is not None and weighted > 0:
            out[name] = usage.cost_usd / weighted
    return out


def rate_for(model: str, measured: Mapping[str, float]) -> float:
    if model in measured:
        return measured[model]
    low = model.lower()
    for key, rate in USD_PER_INPUT_TOKEN.items():
        if key in low:
            return rate
    return max(USD_PER_INPUT_TOKEN.values())


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _transcripts(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in sorted(filenames):
            path = here / name
            if name.endswith(".jsonl") and not path.is_symlink() and path.is_file():
                yield path


def overhead_from_config_dir(
    config_dir: Path,
    main_session: str,
    since: float,
    *,
    prices: Prices | None = None,
    usd_per_input: Mapping[str, float] | None = None,
) -> tuple[Tokens, float]:
    """Assistant usage of every session other than ``main_session`` in the run's transcripts
    (``<config_dir>/projects/**/*.jsonl``) timestamped at or after ``since``, and its list
    cost: tokens weighted by ``prices`` (default `Prices()`) times the model's USD rate
    (``usd_per_input`` measured rates, else `USD_PER_INPUT_TOKEN`). A message repeated per
    content block counts once; records without a parseable timestamp, synthetic messages and
    links are skipped. Carried-in transcripts (e.g. a stream arm's ``.claude/projects``) are
    excluded by ``since``."""
    p = prices or Prices()
    measured = usd_per_input or {}
    seen: set[tuple[str, str]] = set()
    total, cost = Tokens(), 0.0
    for path in _transcripts(config_dir / "projects"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            session = rec.get("sessionId")
            if not isinstance(session, str) or session == main_session:
                continue
            at = _timestamp(rec.get("timestamp"))
            msg = rec.get("message")
            if at is None or at < since or not isinstance(msg, dict):
                continue
            model = _str(msg.get("model")) or ""
            if model == SYNTHETIC_MODEL:
                continue
            mid = _str(msg.get("id")) or _str(rec.get("uuid")) or f"{path}:{len(seen)}"
            if (session, mid) in seen:
                continue
            seen.add((session, mid))
            tokens = _usage_tokens(_dict(msg.get("usage")))
            total = total + tokens
            cost += tokens.weighted(p) * rate_for(model, measured)
    return total, cost


def with_overhead(m: RunMetrics, config_dir: Path, since: float) -> RunMetrics:
    """``m`` with its side-call overhead from ``config_dir`` (priced at the run's own measured
    per-model rates where available)."""
    tokens, cost = overhead_from_config_dir(
        config_dir, m.session_id or "", since, usd_per_input=usd_rates(m)
    )
    return replace(m, overhead=tokens, overhead_cost_usd=cost)
