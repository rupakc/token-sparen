"""Typed run statuses (ruling M5-B2) and plugin load verification.

`classify` maps one finished run to ``ok`` / ``agent_fail`` / ``infra_fail`` with a sub-status;
the first matching row of the table below wins. An ``infra_fail`` is retried (Task 7) and never
graded as an agent failure.

| condition                                                              | status              |
|------------------------------------------------------------------------|---------------------|
| token leak in the run's outputs                                        | infra_fail/leak     |
| docker exit 125/126/127, daemon unreachable, host backstop kill        | infra_fail/docker   |
| container OOM-killed                                                   | infra_fail/docker   |
| API status 401/403 or auth/login/token-expired text                    | infra_fail/auth     |
| rate_limit_event ``rejected``, API status 429 or "hit your … limit"     | infra_fail/rate     |
| API status ≥ 500 (incl. 529), overloaded/server error, retries exhausted | infra_fail/api    |
| wrapper arm and connection refused/reset (the local proxy)             | infra_fail/proxy    |
| no ``system/init``, load problems, or the arm not live                 | infra_fail/load     |
| free disk below the 30 GB floor                                        | infra_fail/disk     |
| in-container ``timeout`` fired                                         | agent_fail/timeout  |
| ``subtype == "error_max_turns"``                                       | agent_fail/max_turns|
| ``subtype == "error_during_execution"`` with no API status             | agent_fail/execution|
| ``subtype == "success"`` and not ``is_error``                          | ok/success          |
| anything else                                                          | infra_fail/unknown  |
"""

from __future__ import annotations

import re
from typing import Literal

from bench.arms import Arm
from bench.streamjson import RunMetrics

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

_DOCKER = re.compile(
    r"cannot connect to the docker daemon|docker daemon is not running|error during connect", re.I
)
_AUTH = re.compile(
    r"authentication_failed|authentication_error|invalid api key|invalid bearer token"
    r"|invalid x-api-key|oauth token (?:has )?(?:expired|been revoked)|token (?:has )?expired"
    r"|please run /login|not logged in|login required|invalid_grant|unauthori[sz]ed",
    re.I,
)
_RATE = re.compile(
    r"hit your .{0,60}?limit|usage limit reached|rate_limit|rate limit(?:ed| reached| exceeded)"
    r"|too many requests",
    re.I,
)
_API = re.compile(r"overloaded_error|assistant error: server_error|internal server error", re.I)
_PROXY = re.compile(
    r"ECONNREFUSED|ECONNRESET|connection ?refused|connection reset|socket hang up", re.I
)
_INSTALL = re.compile(r"^claude plugin install ([A-Za-z0-9._-]+)(?:@\S+)?$")


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
    text = (m.error_text if m else None) or ""
    status = m.api_error_status if m else None
    if leak:
        return ("infra_fail", "leak")
    if exit_code in DOCKER_EXITS or backstop_killed or _DOCKER.search(text):
        return ("infra_fail", "docker")
    if oom_killed:
        return ("infra_fail", "docker")
    if status in (401, 403) or _AUTH.search(text):
        return ("infra_fail", "auth")
    if status == 429 or _RATE.search(text) or _rejected(m):
        return ("infra_fail", "rate")
    if (
        (status is not None and status >= 500)
        or _API.search(text)
        or (m is not None and m.api_retry_exhausted)
    ):
        return ("infra_fail", "api")
    if wrapper and _PROXY.search(text):
        return ("infra_fail", "proxy")
    if m is None or not m.has_init or load_problems or not live:
        return ("infra_fail", "load")
    if disk_low:
        return ("infra_fail", "disk")
    if killed_for_timeout:
        return ("agent_fail", "timeout")
    if m.subtype == "error_max_turns":
        return ("agent_fail", "max_turns")
    if m.subtype == "error_during_execution" and status is None:
        return ("agent_fail", "execution")
    if m.subtype == "success" and not m.is_error:
        return ("ok", "success")
    return ("infra_fail", "unknown")


def _rejected(m: RunMetrics | None) -> bool:
    return m is not None and any(r.status == "rejected" for r in m.rate_limits)


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


def verify_load(m: RunMetrics, arm: Arm, frozen_hash: str | None) -> list[str]:
    """Load problems of a run (empty = loaded as the arm expects): no init event, an expected
    plugin missing or a plugin the arm does not install, token-sparen not loaded from the frozen
    read-only mount or without a recorded ``frozen_hash``, any ``plugin_errors``, an expected
    MCP server missing or not ``connected``, any MCP server ``failed``."""
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
        path = loaded.get(TOKEN_SPAREN_PLUGIN)
        if path and path.rstrip("/") != FROZEN_MOUNT:
            problems.append(f"token-sparen loaded from {path}, not the frozen mount")
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
