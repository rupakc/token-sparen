"""Typed run statuses (ruling M5-B2, Task 5 review rulings I1–I3) and load verification.

`classify` maps one finished run to ``ok`` / ``agent_fail`` / ``infra_fail`` with a sub-status;
the first matching row of the table below wins. An ``infra_fail`` is retried (Task 7) and never
graded as an agent failure. The infra heuristics read only the final ``result`` record's error
fields (`RunMetrics.error_text`, ``api_error_status``) and structured events, never tool output
(ruling I1); retries and rate-limit rejections before a successful final result are counters
only (ruling I2).

| condition                                                                 | status               |
|---------------------------------------------------------------------------|----------------------|
| token leak in the run's outputs                                           | infra_fail/leak      |
| docker exit 125/126/127 (daemon unreachable) or host backstop kill        | infra_fail/docker    |
| container OOM-killed                                                      | infra_fail/docker    |
| timeout fired while the last API event was a retry / rejection (round 2)  | infra_fail/api, rate |
| in-container ``timeout`` fired otherwise (I1: wins; the patch is graded)  | agent_fail/timeout   |
| final result failed with API status 401/403 or auth/login text            | infra_fail/auth      |
| … with 429, "hit your … limit", a ``rejected`` event or retries on 429    | infra_fail/rate      |
| … with status ≥ 500 (incl. 529), overloaded, or API retries exhausted     | infra_fail/api       |
| … in a wrapper arm with connection refused/reset (the local proxy)        | infra_fail/proxy     |
| no ``system/init``, load problems, or the arm not live                    | infra_fail/load      |
| free disk below the 30 GB floor                                           | infra_fail/disk      |
| final ``subtype == "success"`` and not ``is_error`` (I2)                  | ok/success           |
| prompt too long / context window exceeded (I3: graded)                    | agent_fail/context   |
| ``subtype == "error_max_turns"``                                          | agent_fail/max_turns |
| ``subtype == "error_during_execution"`` with no API status                | agent_fail/execution |
| anything else (Task 7 retries it a bounded number of times, reported)     | infra_fail/unknown   |
"""

from __future__ import annotations

import re
from typing import Literal

from bench.arms import Arm
from bench.streamjson import RunMetrics, unpriced_models

RunStatus = tuple[Literal["ok", "agent_fail", "infra_fail"], str]
INFRA_SUBSTATUSES = frozenset(
    {"leak", "docker", "auth", "rate", "api", "proxy", "load", "disk", "unknown"}
)
DOCKER_EXITS = frozenset({125, 126, 127})
FROZEN_MOUNT = "/opt/plugin/token-sparen"  # Task 6 `--plugin-dir`
TOKEN_SPAREN_PLUGIN = "token-sparen"
# MCP servers an arm registers by default (a server whose name contains the needle must be
# `connected`); names are calibrated against real init events in Task 13.
EXPECTED_MCP: dict[str, tuple[str, ...]] = {
    "context_mode": ("context-mode",),
    "headroom": ("headroom",),
}
# what identifies each tool in a hook command or an MCP server name (keyed by `Arm.liveness`);
# a run may show only its own arm's tool (review M2)
TOOL_MARKERS: dict[str, re.Pattern[str]] = {
    "token_sparen": re.compile(r"token-sparen", re.I),
    "rtk": re.compile(r"(?:^|[\s/])rtk\s+hook\b"),
    "headroom": re.compile(r"headroom", re.I),
    "context_mode": re.compile(r"context-mode", re.I),
    "tokeneconomizer": re.compile(r"tokeneconomizer", re.I),
    "caveman": re.compile(r"caveman", re.I),
}

_AUTH = re.compile(
    r"authentication_failed|authentication_error|permission_error|invalid api key"
    r"|invalid bearer token|invalid x-api-key|oauth token (?:has )?(?:expired|been revoked)"
    r"|token (?:has )?expired|please run /login|not logged in|login required|invalid_grant",
    re.I,
)
_RATE = re.compile(
    r"hit your .{0,60}?limit|usage limit reached|rate_limit_error"
    r"|rate limit(?:ed| reached| exceeded)",
    re.I,
)
_API = re.compile(r"overloaded_error|api_error|internal server error", re.I)
_PROXY = re.compile(
    r"ECONNREFUSED|ECONNRESET|connection ?refused|connection reset|socket hang up", re.I
)
_CONTEXT = re.compile(
    r"prompt is too long|context (?:window|length) (?:exceeded|limit)"
    r"|exceeds? (?:the )?(?:model'?s? )?(?:maximum )?context|maximum context length",
    re.I,
)
_INSTALL = re.compile(r"^claude plugin install ([A-Za-z0-9._-]+)(?:@\S+)?$")


def _final_success(m: RunMetrics) -> bool:
    return m.subtype == "success" and not m.is_error


def _infra_from_result(m: RunMetrics, wrapper: bool) -> str | None:
    """The infra sub-status a failed final result shows, or None."""
    if _final_success(m) or (m.subtype is None and not m.is_error):
        return None  # success, or no final result at all: nothing to read (I1, I2)
    text, status = m.error_text or "", m.api_error_status
    last_retry = m.api_retries[-1].error_status if m.api_retries else None
    if status in (401, 403) or _AUTH.search(text):
        return "auth"
    if (
        status == 429
        or _RATE.search(text)
        or any(r.status == "rejected" for r in m.rate_limits)
        or (m.api_retry_exhausted and last_retry == 429)
    ):
        return "rate"
    if (status is not None and status >= 500) or _API.search(text) or m.api_retry_exhausted:
        return "api"
    if wrapper and _PROXY.search(text):
        return "proxy"
    return None


def classify(
    m: RunMetrics | None,
    *,
    exit_code: int,
    killed_for_timeout: bool,
    backstop_killed: bool,
    disk_low: bool,
    oom_killed: bool,
    load_problems: list[str],
    live: bool,
    leak: bool,
    wrapper: bool = False,
) -> RunStatus:
    """The status of one run (first matching row of the module table wins). ``m`` is None when
    the run left no stream; ``wrapper`` marks a ``kind = "wrapper"`` arm (local proxy)."""
    if leak:
        return ("infra_fail", "leak")
    if exit_code in DOCKER_EXITS or backstop_killed or oom_killed:
        return ("infra_fail", "docker")
    if killed_for_timeout:
        # round-2 ruling: killed while still in API retry/backoff or after a rejection (no
        # successful assistant turn since) is infrastructure, not the agent's timeout
        if m is not None and m.pending_api_failure is not None:
            return ("infra_fail", m.pending_api_failure)
        return ("agent_fail", "timeout")
    infra = _infra_from_result(m, wrapper) if m is not None else None
    if infra is not None:
        return ("infra_fail", infra)
    if m is None or not m.has_init or load_problems or not live:
        return ("infra_fail", "load")
    if disk_low:
        return ("infra_fail", "disk")
    if _final_success(m):
        return ("ok", "success")
    if m.is_error and _CONTEXT.search(m.error_text or ""):
        return ("agent_fail", "context")
    if m.subtype == "error_max_turns":
        return ("agent_fail", "max_turns")
    if m.subtype == "error_during_execution" and m.api_error_status is None:
        return ("agent_fail", "execution")
    return ("infra_fail", "unknown")


def _base(name: str) -> str:
    return name.split("@", 1)[0]


def expected_plugins(arm: Arm) -> set[str]:
    """Plugin names the arm must load: the frozen token-sparen plugin, or the plugins its
    ``install`` lines add (``claude plugin install <name>@<marketplace>``)."""
    names = {TOKEN_SPAREN_PLUGIN} if arm.uses_frozen_plugin else set()
    for line in arm.install:
        found = _INSTALL.match(line.strip())
        if found:
            names.add(found.group(1))
    return names


def verify_load(
    m: RunMetrics, arm: Arm, frozen_hash: str | None, *, stage_hash: str | None = None
) -> list[str]:
    """Load problems of a run (empty = loaded as the arm expects): no init event, an expected
    plugin missing or a plugin the arm does not install, token-sparen not confirmed loaded from
    the frozen read-only mount, without a recorded ``frozen_hash`` or with one that differs from
    the stage's ``stage_hash``, any ``plugin_errors``, an expected MCP server missing or not
    ``connected``, any MCP server ``failed``, a hook command or MCP server of another arm's tool
    (`TOOL_MARKERS`), and a model the price table cannot price (the run could not be costed)."""
    problems: list[str] = []
    if not m.has_init:
        return ["no system/init event"]
    want = expected_plugins(arm)
    loaded = {_base(name): path for name, path in m.plugins.items()}
    for name in sorted(want - set(loaded)):
        problems.append(f"expected plugin {name} not loaded")
    for name in sorted(set(loaded) - want):
        problems.append(f"unexpected plugin {name} loaded")
    if arm.uses_frozen_plugin:
        if frozen_hash is None:
            problems.append("frozen-plugin arm without a recorded plugin hash")
        elif stage_hash is not None and frozen_hash != stage_hash:
            problems.append("frozen plugin hash differs from the stage's plugin hash")
        path = loaded.get(TOKEN_SPAREN_PLUGIN)
        if TOKEN_SPAREN_PLUGIN in loaded and path is not None and path.rstrip("/") != FROZEN_MOUNT:
            where = path or "an unreported path"
            problems.append(f"token-sparen loaded from {where}, not the frozen mount")
    for kind, marker in sorted(TOOL_MARKERS.items()):
        if kind == arm.liveness:
            continue
        hooks = sorted({h.command for h in m.hook_events if marker.search(h.command)})
        servers = sorted(n for n in m.mcp_servers if marker.search(n))
        if hooks or servers:
            problems.append(f"{kind} tool present in arm {arm.name}: {', '.join(hooks + servers)}")
    problems.extend(f"no list price for model {name}" for name in unpriced_models(m))
    problems.extend(f"plugin error: {err}" for err in m.plugin_errors)
    for needle in EXPECTED_MCP.get(arm.liveness, ()):
        matches = {n: s for n, s in m.mcp_servers.items() if needle in n}
        if not matches:
            problems.append(f"expected MCP server {needle} missing")
        for name, status in sorted(matches.items()):
            if status != "connected":
                problems.append(f"MCP server {name} is {status or 'unknown'}, not connected")
    for name, status in sorted(m.mcp_servers.items()):
        if status == "failed" and not any(n in name for n in EXPECTED_MCP.get(arm.liveness, ())):
            problems.append(f"MCP server {name} failed")
    return problems
