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

Error text (ruling I1 of the Task 5 review) comes **only** from the final ``result`` record's
error fields (``result`` text when ``is_error``, ``errors``, ``api_error_status``): never from
tool output, assistant text or stray lines, which the agent's own tests can fill with
"401 Unauthorized" or "500 Internal Server Error". Assistant ``error`` kinds, retries and
rate-limit events are kept as counters (`classify` lets the final result decide, ruling I2).

Cost per run (§10.5) = ``total_cost_usd`` of the main session + the plugin's side-call sessions
found in the run's config dir (`overhead_from_config_dir`), list price per token class from
`PRICE_TABLE` (cache-aware: 5-minute writes 1.25x input, 1-hour writes 2x, reads per model).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from token_sparen.estimate import Prices

FIVE_HOURS_S = 5 * 3600.0
MAX_ERROR_TEXT = 2000
_API_ERROR = re.compile(r"API Error: (\d{3})\b")
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class ModelPrice:
    """List price in USD per million tokens. Cache writes are derived from ``input``
    (5-minute 1.25x, 1-hour 2x). Above ``long_threshold`` prompt tokens a request is billed at
    the ``long_*`` rates."""

    input: float
    output: float
    cache_read: float
    long_threshold: int | None = None
    long_input: float = 0.0
    long_output: float = 0.0
    long_cache_read: float = 0.0


PRICE_TABLE_VERSION = (
    "claude-api skill model table (cached 2026-11-03); confirm in Task 13 against the live "
    "pricing page before any public cost claim"
)
# machine-readable id of the same table (published in e4.json; bump with every price change)
PRICE_TABLE_ID = "claude-api-2026-11-03"
# keyed by model id prefix; the longest matching prefix wins (date and `[1m]` suffixes match)
PRICE_TABLE: Mapping[str, ModelPrice] = {
    "claude-fable-5-2": ModelPrice(8.0, 40.0, 0.20),
    "claude-mythos-5-2": ModelPrice(8.0, 40.0, 0.20),
    "claude-fable-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-mythos-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-fable-5": ModelPrice(10.0, 50.0, 1.00),
    "claude-mythos-5": ModelPrice(10.0, 50.0, 1.00),
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 0.20),
    "claude-opus-5": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-5": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-1": ModelPrice(15.0, 75.0, 1.50),
    "claude-opus-4": ModelPrice(15.0, 75.0, 1.50),  # claude-opus-4-YYYYMMDD
    "claude-3-opus": ModelPrice(15.0, 75.0, 1.50),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, 0.10),
    "claude-sonnet-5": ModelPrice(2.0, 10.0, 0.20),
    "claude-sonnet-4": ModelPrice(3.0, 15.0, 0.30),  # 4, 4.5, 4.6
    "claude-3-7-sonnet": ModelPrice(3.0, 15.0, 0.30),
    # Haiku 5.5: cache read assumed 0.1x of input in both tiers (not in the skill table)
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.01, 100_000, 0.50, 2.50, 0.05),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 0.10),
    "claude-3-5-haiku": ModelPrice(0.80, 4.0, 0.08),
    "claude-3-haiku": ModelPrice(0.25, 1.25, 0.03),
}
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


class UnpricedModel(ValueError):
    """A model id with no entry in `PRICE_TABLE` (add it before costing a run)."""


def price_for(model: str) -> ModelPrice:
    best = max((k for k in PRICE_TABLE if model.startswith(k)), key=len, default=None)
    if best is None:
        raise UnpricedModel(f"no list price for model {model!r} ({PRICE_TABLE_VERSION})")
    return PRICE_TABLE[best]


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
    error_text: str | None = None  # final result's error fields only
    assistant_errors: list[str] = field(default_factory=list)  # counters, never classified
    # the last API-related event was an `api_retry` ("api", "rate" on 429) or a `rejected`
    # rate-limit event ("rate") with no successful assistant turn after it (round-2 ruling)
    pending_api_failure: Literal["api", "rate"] | None = None
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


def _max_tokens(a: Tokens, b: Tokens) -> Tokens:
    """Per-class maximum: records of one streamed message may carry partial usage."""
    return Tokens(
        max(a.input, b.input),
        max(a.output, b.output),
        max(a.cache_creation, b.cache_creation),
        max(a.cache_read, b.cache_read),
    )


class _Parser:
    def __init__(self, observed_at: float) -> None:
        self.observed_at = observed_at
        self.fields: dict[str, Any] = {}
        self.hooks: list[HookEvent] = []
        self.rates: list[RateLimit] = []
        self.retries: list[ApiRetry] = []
        self.assistant_errors: list[str] = []
        self.pending: Literal["api", "rate"] | None = None
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
            self.malformed += 1  # counted only: stray text is never error text (ruling I1)

    def event(self, e: dict[str, Any]) -> None:
        kind, sub = e.get("type"), e.get("subtype")
        if kind == "system" and sub == "init":
            self.init(e)
        elif kind == "system" and sub == "hook_response":
            self.hook(e)
        elif kind == "system" and sub == "api_retry":
            retry = ApiRetry(
                _int(e.get("attempt")), _int(e.get("max_retries")), _int(e.get("error_status"))
            )
            self.retries.append(retry)
            self.pending = "rate" if retry.error_status == 429 else "api"
        elif kind == "rate_limit_event":
            info = e.get("rate_limit_info")
            if not isinstance(info, dict):
                raise _Malformed
            resets = _resets_at(info.get("resetsAt"))
            if info.get("status") == "rejected":
                self.pending = "rate"
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
        if error is not None:
            self.assistant_errors.append(error)
        if msg.get("model") == SYNTHETIC_MODEL:
            return
        if error is None:
            self.pending = None  # a successful assistant turn after any retry/rejection
        mid = _str(msg.get("id")) or f"#{len(self.assistant)}"
        usage = _usage_tokens(_dict(msg.get("usage")))
        self.assistant[mid] = _max_tokens(self.assistant.get(mid, Tokens()), usage)

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
        errors: list[str] = []
        if is_error:  # only the final result's error fields (ruling I1)
            errors.extend(_texts(res.get("result")))
            errors.extend(str(x) for x in _list(res.get("errors")))
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
            assistant_errors=self.assistant_errors,
            pending_api_failure=self.pending,
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


def message_cost_usd(model: str, usage: Mapping[str, Any]) -> float:
    """List cost of one API response's ``usage`` (Messages API field names). Cache writes are
    split by TTL when ``usage.cache_creation`` reports it (else all 5-minute). `UnpricedModel`
    for a model not in `PRICE_TABLE`."""
    p = price_for(model)
    inp = _count(usage.get("input_tokens"))
    out = _count(usage.get("output_tokens"))
    read = _count(usage.get("cache_read_input_tokens"))
    write = _count(usage.get("cache_creation_input_tokens"))
    split = _dict(usage.get("cache_creation"))
    w1h = _count(split.get("ephemeral_1h_input_tokens"))
    w5m = _count(split.get("ephemeral_5m_input_tokens")) if split else write
    if split and w5m + w1h < write:
        w5m = write - w1h
    rate_in, rate_out, rate_read = p.input, p.output, p.cache_read
    if p.long_threshold is not None and inp + read + write > p.long_threshold:
        rate_in, rate_out, rate_read = p.long_input, p.long_output, p.long_cache_read
    return (
        inp * rate_in
        + out * rate_out
        + read * rate_read
        + w5m * rate_in * CACHE_WRITE_5M
        + w1h * rate_in * CACHE_WRITE_1H
    ) / 1e6


def unpriced_models(m: RunMetrics) -> list[str]:
    """Models the run used (``modelUsage``) that `PRICE_TABLE` cannot price."""
    out = []
    for name in sorted(m.model_usage):
        try:
            price_for(name)
        except UnpricedModel:
            out.append(name)
    return out


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if at.tzinfo is None:  # Claude Code writes UTC; never read a naive stamp as host-local
        at = at.replace(tzinfo=UTC)
    return at.timestamp()


def _transcripts(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in sorted(filenames):
            path = here / name
            if name.endswith(".jsonl") and not path.is_symlink() and path.is_file():
                yield path


def overhead_from_config_dir(
    config_dir: Path, main_session: str, since: float
) -> tuple[Tokens, float]:
    """Assistant usage of every session other than ``main_session`` in the run's transcripts
    (``<config_dir>/projects/**/*.jsonl``) timestamped at or after ``since`` (naive stamps read
    as UTC), and its list cost (`message_cost_usd`, `PRICE_TABLE`). Records of one message
    (same session and message id) count once, with the per-class maximum (streamed records may
    carry partial usage). Records without a parseable timestamp, synthetic messages and links
    are skipped; carried-in transcripts (a stream arm's ``.claude/projects``) are excluded by
    ``since``. `UnpricedModel` when a side call used a model the table does not price."""
    messages: dict[tuple[str, str], tuple[str, dict[str, Any], Tokens]] = {}
    for path in _transcripts(config_dir / "projects"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, raw in enumerate(lines):
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
            mid = _str(msg.get("id")) or _str(rec.get("uuid")) or f"{path}:{n}"
            usage = _dict(msg.get("usage"))
            tokens = _usage_tokens(usage)
            prev = messages.get((session, mid))
            if prev is not None and prev[2].total >= tokens.total:
                tokens = _max_tokens(prev[2], tokens)
                usage = {**prev[1], **_usage_fields(tokens)}
            elif prev is not None:
                tokens = _max_tokens(prev[2], tokens)
                usage = {**usage, **_usage_fields(tokens)}
            messages[(session, mid)] = (model, usage, tokens)
    total, cost = Tokens(), 0.0
    for model, usage, tokens in messages.values():
        total = total + tokens
        cost += message_cost_usd(model, usage)
    return total, cost


def _usage_fields(t: Tokens) -> dict[str, int]:
    return {
        "input_tokens": t.input,
        "output_tokens": t.output,
        "cache_creation_input_tokens": t.cache_creation,
        "cache_read_input_tokens": t.cache_read,
    }


def with_overhead(m: RunMetrics, config_dir: Path, since: float) -> RunMetrics:
    """``m`` with its side-call overhead from ``config_dir``."""
    tokens, cost = overhead_from_config_dir(config_dir, m.session_id or "", since)
    return replace(m, overhead=tokens, overhead_cost_usd=cost)
