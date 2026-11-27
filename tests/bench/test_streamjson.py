"""Stream-json metrics and side-call overhead (M5 Task 5). Synthetic fixtures only."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench import streamjson as sj
from token_sparen.estimate import Prices

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_794_990_000.0


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()


def test_parses_cost_and_all_token_classes() -> None:
    m = sj.parse_stream(_lines("stream_success"), observed_at=NOW)
    assert m.has_init
    assert m.session_id == "sess-main"
    assert m.model == "claude-sonnet-4-6"
    assert m.claude_code_version == "2.1.150"
    assert m.plugins == {"token-sparen": "/opt/plugin/token-sparen"}
    assert m.plugin_errors == []
    assert m.mcp_servers == {"context-mode": "connected"}
    assert m.skills == ["token-sparen"]
    assert m.subtype == "success" and m.is_error is False
    assert m.api_error_status is None and m.error_text is None
    assert m.total_cost_usd == pytest.approx(0.75)
    assert m.num_turns == 12 and m.duration_ms == 120000
    # every token class kept, summed over every model the run used (side models included)
    assert m.tokens == sj.Tokens(input=1500, output=3450, cache_creation=20000, cache_read=400000)
    assert set(m.model_usage) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert m.model_usage["claude-haiku-4-5"].cost_usd == pytest.approx(0.05)
    assert [(h.event, h.ok) for h in m.hook_events] == [
        ("SessionStart", True),
        ("PreToolUse", True),
    ]
    assert "/opt/plugin/token-sparen/hooks/run.py" in m.hook_events[0].command
    assert m.hook_events[0].exit_code == 0
    assert m.malformed_lines == 0
    assert m.overhead == sj.Tokens() and m.overhead_cost_usd == 0.0


def test_tokens_fall_back_to_usage_then_assistant_messages() -> None:
    base = [json.loads(line) for line in _lines("stream_success")]
    res = base[-1]
    del res["modelUsage"]
    m = sj.parse_stream([json.dumps(e) for e in base], observed_at=NOW)
    assert m.tokens == sj.Tokens(input=1200, output=3400, cache_creation=20000, cache_read=400000)
    # no result at all: assistant usage, one count per message id (Claude Code repeats the
    # usage on every content block of a message)
    assistant = base[3]
    lines = [json.dumps(base[1]), json.dumps(assistant), json.dumps(assistant)]
    m = sj.parse_stream(lines, observed_at=NOW)
    assert m.tokens == sj.Tokens(input=10, output=5, cache_creation=100, cache_read=1000)
    assert m.subtype is None and m.total_cost_usd is None


def test_tokens_add() -> None:
    a = sj.Tokens(1, 2, 3, 4)
    assert a + sj.Tokens(10, 20, 30, 40) == sj.Tokens(11, 22, 33, 44)
    assert a.total == 10
    assert a.weighted(Prices()) == pytest.approx(1 + 2 * 5.0 + 3 * 1.25 + 4 * 0.1)


def test_run_cost_includes_overhead() -> None:
    m = sj.parse_stream(_lines("stream_success"), observed_at=NOW)
    assert sj.run_cost(m) == pytest.approx(0.75)
    with_side = replace(m, overhead=sj.Tokens(input=100), overhead_cost_usd=0.10)
    assert sj.run_cost(with_side) == pytest.approx(0.85)
    no_result = sj.parse_stream(_lines("stream_success")[:-1], observed_at=NOW)
    assert sj.run_cost(replace(no_result, overhead_cost_usd=0.10)) is None


def _rate_event(info: dict[str, object]) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def test_rate_limit_kind_from_type_or_reset() -> None:
    hour = 3600.0
    lines = [
        _rate_event({"status": "allowed", "rateLimitType": "five_hour", "resetsAt": NOW + 9e5}),
        _rate_event(
            {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.8}
        ),
        _rate_event({"status": "rejected", "rateLimitType": "seven_day_opus"}),
        _rate_event({"status": "allowed", "resetsAt": NOW + 2 * hour}),
        _rate_event({"status": "allowed", "resetsAt": NOW + 30 * hour}),
        _rate_event({"status": "allowed", "resetsAt": NOW + 5 * hour}),  # not > 5 h
        _rate_event({"status": "allowed"}),
        _rate_event({"status": "allowed", "rateLimitType": "overage", "resetsAt": NOW + 1}),
    ]
    m = sj.parse_stream(lines, observed_at=NOW)
    assert [r.kind for r in m.rate_limits] == [
        "five_hour",  # the event's type wins over its reset time
        "weekly",
        "weekly",
        "five_hour",
        "weekly",
        "five_hour",
        "unknown",
        "unknown",
    ]
    assert m.rate_limits[1].utilization == pytest.approx(0.8)
    assert m.rate_limits[2].status == "rejected"
    assert m.rate_limits[0].resets_at == pytest.approx(NOW + 9e5)
    # a reset given in milliseconds is read as seconds
    ms = sj.parse_stream(
        [_rate_event({"status": "allowed", "resetsAt": (NOW + 2 * hour) * 1000})], observed_at=NOW
    )
    assert ms.rate_limits[0].resets_at == pytest.approx(NOW + 2 * hour)
    assert ms.rate_limits[0].kind == "five_hour"


def test_malformed_lines_counted() -> None:
    good = _lines("stream_success")
    lines = [
        good[0],
        "not json at all",
        "",
        "   ",
        "[1, 2, 3]",
        '{"type": "result", "num_turns": "twelve"',
        json.dumps({"type": "system", "subtype": "init", "plugins": 7, "mcp_servers": "x"}),
        *good[1:],
        "\x00\xff garbage",
    ]
    m = sj.parse_stream(lines, observed_at=NOW)
    assert m.malformed_lines == 5  # blank lines are not malformed
    assert m.subtype == "success" and m.session_id == "sess-main"
    assert m.tokens.cache_read == 400000
    assert m.error_text is None  # stray lines are only error text when no result arrived


def test_parse_never_raises_and_keeps_stderr_without_result() -> None:
    m = sj.parse_stream(["Error: Invalid API key · Please run /login"], observed_at=NOW)
    assert not m.has_init and m.subtype is None
    assert m.malformed_lines == 1
    assert m.error_text is not None and "/login" in m.error_text
    empty = sj.parse_stream([], observed_at=NOW)
    assert not empty.has_init and empty.tokens == sj.Tokens()

    weird = [
        json.dumps({"type": "result", "subtype": 3, "is_error": "yes", "total_cost_usd": "1"}),
        json.dumps({"type": "assistant", "message": "nope"}),
        json.dumps({"type": "system", "subtype": "hook_response", "exit_code": "0"}),
        json.dumps({"type": "system", "subtype": "api_retry", "attempt": True}),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": None}),
        json.dumps({"type": "result", "modelUsage": {"m": {"inputTokens": -5}}, "usage": []}),
    ]
    m = sj.parse_stream(weird, observed_at=NOW)
    assert m.total_cost_usd is None and m.tokens == sj.Tokens()


def test_api_error_status_and_retries() -> None:
    m = sj.parse_stream(_lines("status_auth_401"), observed_at=NOW)
    assert m.api_error_status == 401 and m.is_error
    assert m.error_text is not None and "authentication_failed" in m.error_text
    explicit = [json.dumps({"type": "result", "subtype": "success", "is_error": True,
                            "api_error_status": 503, "result": "API Error: 500"})]  # fmt: skip
    assert sj.parse_stream(explicit, observed_at=NOW).api_error_status == 503
    r = sj.parse_stream(_lines("status_api_retry"), observed_at=NOW)
    assert [(a.attempt, a.max_retries) for a in r.api_retries] == [(9, 10), (10, 10)]
    assert r.api_retry_exhausted
    ok = sj.parse_stream(_lines("stream_success"), observed_at=NOW)
    assert not ok.api_retry_exhausted


def _transcript(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _msg(session: str, mid: str, ts: str, model: str = "claude-haiku-4-5", **usage: int) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": ts,
        "message": {"id": mid, "model": model, "usage": usage},
    }


def test_overhead_sums_other_sessions_only(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude"
    since = 1_795_000_000.0  # 2026-11-17T08:26:40Z
    after, before = "2026-11-20T10:00:00.000Z", "2026-11-01T10:00:00.000Z"
    proj = cfg / "projects" / "-testbed"
    _transcript(
        proj / "sess-main.jsonl",
        [_msg("sess-main", "m0", after, "claude-sonnet-4-6", input_tokens=999, output_tokens=9)],
    )
    # the main session's subagent transcript is part of total_cost_usd already
    _transcript(
        proj / "sess-main" / "subagents" / "agent-1.jsonl",
        [_msg("sess-main", "m9", after, input_tokens=500)],
    )
    side = _msg("side-1", "m1", after, input_tokens=100, output_tokens=10,
                cache_creation_input_tokens=20, cache_read_input_tokens=30)  # fmt: skip
    _transcript(
        proj / "side-1.jsonl",
        [
            side,
            side,  # the same message repeated per content block counts once
            {"type": "user", "sessionId": "side-1", "timestamp": after},
            _msg("side-1", "m2", after, "<synthetic>", input_tokens=7),
            _msg("side-1", "m3", "not a time", input_tokens=7),
        ],
    )
    _transcript(
        cfg / "projects" / "-other" / "side-2.jsonl",
        [
            _msg("side-2", "m4", after, input_tokens=1, output_tokens=2),
            _msg("side-2", "m5", before, input_tokens=10_000),  # carried-in state, before run
        ],
    )
    (proj / "junk.jsonl").write_text("{not json\n[]\n", encoding="utf-8")
    tokens, cost = sj.overhead_from_config_dir(cfg, "sess-main", since)
    assert tokens == sj.Tokens(input=101, output=12, cache_creation=20, cache_read=30)
    weighted = tokens.weighted(Prices())
    assert cost == pytest.approx(weighted * sj.USD_PER_INPUT_TOKEN["haiku"])
    # a measured per-model rate (from the main run's modelUsage) wins over the table
    _, cost2 = sj.overhead_from_config_dir(
        cfg, "sess-main", since, usd_per_input={"claude-haiku-4-5": 2e-6}
    )
    assert cost2 == pytest.approx(weighted * 2e-6)
    assert sj.overhead_from_config_dir(tmp_path / "missing", "x", since) == (sj.Tokens(), 0.0)


def test_with_overhead_prices_from_the_runs_own_model_usage(tmp_path: Path) -> None:
    m = sj.parse_stream(_lines("stream_success"), observed_at=NOW)
    rates = sj.usd_rates(m)
    haiku = sj.Tokens(input=300, output=50).weighted(Prices())
    assert rates["claude-haiku-4-5"] == pytest.approx(0.05 / haiku)
    cfg = tmp_path / ".claude"
    _transcript(
        cfg / "projects" / "p" / "side.jsonl",
        [_msg("side", "m1", "2026-11-20T10:00:00Z", input_tokens=1000)],
    )
    out = sj.with_overhead(m, cfg, since=1_795_000_000.0)
    assert out.overhead == sj.Tokens(input=1000)
    assert out.overhead_cost_usd == pytest.approx(1000 * rates["claude-haiku-4-5"])
    assert sj.run_cost(out) == pytest.approx(0.75 + out.overhead_cost_usd)


def test_unknown_model_priced_at_the_highest_rate(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude"
    _transcript(
        cfg / "projects" / "p" / "side.jsonl",
        [_msg("side", "m1", "2026-11-20T10:00:00Z", "mystery-model", input_tokens=10)],
    )
    _, cost = sj.overhead_from_config_dir(cfg, "main", 0.0)
    assert cost == pytest.approx(10 * max(sj.USD_PER_INPUT_TOKEN.values()))
    assert sj.rate_for("claude-opus-4-1-20250805", {}) == sj.USD_PER_INPUT_TOKEN["opus-4-1"]
    assert sj.rate_for("claude-sonnet-4-6", {}) == sj.USD_PER_INPUT_TOKEN["sonnet"]
