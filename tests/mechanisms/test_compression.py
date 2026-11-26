"""Compression decision point: static policy, PostToolUse mechanism, session tracking."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from perfutil import best_of, perf_factor
from sampleoutputs import big_pytest

from token_sparen.compress import CompressContext, CompressResult, Level, registry
from token_sparen.config import Config, load_config
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Event, EventKind, SessionCtx, ToolCall
from token_sparen.learn import policy as policy_mod
from token_sparen.learn.policy import (
    COMPRESSION,
    Decision,
    DecisionPoint,
    DecisionRecord,
    Policy,
    StaticPolicy,
)
from token_sparen.mechanisms import compression, tracking
from token_sparen.store import session

KEY = "0123456789abcdef"
MARKER = re.compile(
    r"\[token-sparen: kept (\d+) of (\d+) lines · full output: (.+) · "
    r"`token-sparen show ([0-9a-f]{12})`\]$"
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}


def _cfg(tmp_path: Path, **over: str) -> Config:
    env = _env(tmp_path)
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return load_config(tmp_path / "proj", env)


def _event(
    tmp_path: Path,
    *,
    tool: str = "Bash",
    tool_input: Mapping[str, Any] | None = None,
    response: Any = None,
    text: str | None = None,
    kind: EventKind = "post_tool",
    prompt: str | None = None,
    session_id: str = "s-1",
) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind=kind,
        session_id=session_id,
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall(
            name=tool,
            category=tool_category(tool),
            input=dict(tool_input if tool_input is not None else {"command": "pytest -v"}),
            use_id="tu-1",
        ),
        output_text=text,
        raw_response=response,
        prompt=prompt,
    )


def _bash(tmp_path: Path, stdout: str, **resp: Any) -> Event:
    response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, **resp}
    return _event(tmp_path, response=response, text=stdout)


def _handle(tmp_path: Path, ev: Event, cfg: Config | None = None, **kw: Any) -> Any:
    cfg = cfg or _cfg(tmp_path)
    return compression.handle_post_tool(ev, StaticPolicy({}), cfg, env=_env(tmp_path), **kw)


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _overlay(tmp_path: Path, sid: str = "s-1") -> dict[str, Any]:
    return session.read_session(session.session_path(tmp_path / "home", KEY, sid))


def _outputs(tmp_path: Path) -> list[Path]:
    out = tmp_path / "proj" / ".token-sparen" / "outputs"
    return sorted(out.glob("*.txt")) if out.exists() else []


# --- policy ------------------------------------------------------------------------------


def _sctx(tmp_path: Path) -> SessionCtx:
    return SessionCtx("s", KEY, 0, None, None, tmp_path / "o.json")


def test_compression_point() -> None:
    assert COMPRESSION == DecisionPoint(
        "compression", ("off", "light", "standard", "aggressive"), safe_arm="off", tier="medium"
    )


def test_static_policy_defaults(tmp_path: Path) -> None:
    p: Policy = StaticPolicy({})
    d = p.decide(COMPRESSION, {"content_type": "text"}, _sctx(tmp_path))
    assert isinstance(d, Decision)
    assert (d.point, d.arm, d.propensity, d.bucket) == ("compression", "standard", 1.0, "static")
    assert re.fullmatch(r"[0-9a-f]{32}", d.decision_id)
    assert d.decision_id != p.decide(COMPRESSION, {}, _sctx(tmp_path)).decision_id


def test_static_policy_overrides_and_safe_fallback(tmp_path: Path) -> None:
    other = DecisionPoint("guard", ("off", "on"), safe_arm="off", tier="low")
    p = StaticPolicy({"compression": "light", "guard": "bogus"})
    assert p.decide(COMPRESSION, {}, _sctx(tmp_path)).arm == "light"
    assert p.decide(other, {}, _sctx(tmp_path)).arm == "off"  # invalid arm -> safe arm
    unknown = DecisionPoint("new", ("a", "b"), safe_arm="b", tier="none")
    assert StaticPolicy({}).decide(unknown, {}, _sctx(tmp_path)).arm == "b"


def test_decision_record_fields() -> None:
    names = [f.name for f in fields(DecisionRecord)]
    assert names[:11] == [
        "decision_id",
        "point",
        "arm",
        "propensity",
        "bucket",
        "tokens_before",
        "tokens_after",
        "recovery_id",
        "tool_use_id",
        "command_sig",
        "ts",
    ]
    assert names[11:] == ["ctype", "tool_name", "tokens_full", "host"]
    assert policy_mod.STATIC_BUCKET == "static"


# --- compression mechanism ---------------------------------------------------------------


def test_small_output_untouched(tmp_path: Path) -> None:
    act = _handle(tmp_path, _bash(tmp_path, "ok\n" * 50))
    assert act.is_noop()
    assert _outputs(tmp_path) == [] and _decisions(tmp_path) == []


def test_large_pytest_output_compressed_with_marker(tmp_path: Path) -> None:
    text = big_pytest()
    act = _handle(tmp_path, _bash(tmp_path, text))
    out = act.replace_output
    assert out is not None and len(out) < len(text) * 0.85
    last = out.rstrip("\n").splitlines()[-1]
    m = MARKER.fullmatch(last)
    assert m is not None
    kept, total, path, rid = m.groups()
    assert int(total) == len(text.splitlines()) and int(kept) < int(total)
    assert Path(path).read_text(encoding="utf-8") == text
    assert Path(path) == (tmp_path / "proj" / ".token-sparen" / "outputs" / f"{rid}.txt").resolve()
    assert "FAILED tests/test_cache.py::test_cache_eviction" in out
    assert "2 failed, 140 passed" in out
    ov = _overlay(tmp_path)
    assert ov["trims"] == 1 and ov["tokens_removed"] > 0
    _handle(tmp_path, _bash(tmp_path, text))
    assert _overlay(tmp_path)["trims"] == 2


def test_decision_logged(tmp_path: Path) -> None:
    text = big_pytest()
    act = _handle(tmp_path, _bash(tmp_path, text))
    assert act.replace_output is not None
    (rec,) = _decisions(tmp_path)
    assert rec["point"] == "compression" and rec["arm"] == "standard"
    assert rec["propensity"] == 1.0 and rec["bucket"] == "static"
    assert re.fullmatch(r"[0-9a-f]{32}", rec["decision_id"])
    assert rec["tokens_before"] > rec["tokens_after"] > 0
    assert rec["recovery_id"] == MARKER.search(act.replace_output).group(4)  # type: ignore[union-attr]
    assert rec["tool_use_id"] == "tu-1"
    assert rec["command_sig"] == "pytest -v"
    assert rec["ctype"] == "test_report" and rec["tool_name"] == "Bash"
    assert isinstance(rec["ts"], float)
    assert _overlay(tmp_path)["tokens_removed"] == rec["tokens_before"] - rec["tokens_after"]


def test_command_sig_truncated_and_tool_name(tmp_path: Path) -> None:
    cmd = "pytest -v " + "x" * 900
    ev = _event(
        tmp_path,
        tool_input={"command": cmd},
        response={"stdout": big_pytest(), "stderr": ""},
        text=big_pytest(),
    )
    _handle(tmp_path, ev)
    assert _decisions(tmp_path)[-1]["command_sig"] == cmd[:500]
    text = big_pytest()
    ev2 = _event(tmp_path, tool="mcp__srv__run", tool_input={}, response=text, text=text)
    _handle(tmp_path, ev2)
    assert _decisions(tmp_path)[-1]["command_sig"] == "mcp__srv__run"


def test_persisted_output_used(tmp_path: Path) -> None:
    full = big_pytest(80_000)
    saved = tmp_path / "persisted.txt"
    saved.write_text(full, encoding="utf-8")
    preview = full[:30_000]
    ev = _bash(tmp_path, preview, persistedOutputPath=str(saved), persistedOutputSize=len(full))
    act = _handle(tmp_path, ev)
    assert act.replace_output is not None
    assert "2 failed, 140 passed" in act.replace_output  # only in the persisted full text
    (rec_file,) = _outputs(tmp_path)
    assert rec_file.read_text(encoding="utf-8") == full


def test_persisted_output_missing_or_too_big_falls_back(tmp_path: Path) -> None:
    text = big_pytest()
    ev = _bash(tmp_path, text, persistedOutputPath=str(tmp_path / "gone.txt"))
    assert _handle(tmp_path, ev).replace_output is not None
    assert _outputs(tmp_path)[0].read_text(encoding="utf-8") == text
    big = tmp_path / "big.txt"
    big.write_text(big_pytest(60_000), encoding="utf-8")
    cfg = _cfg(tmp_path, COMPRESSION__PERSISTED_MAX_BYTES="1000")
    other = text.replace("[  0%]", "[  1%]")
    ev2 = _bash(tmp_path, other, persistedOutputPath=str(big))
    assert _handle(tmp_path, ev2, cfg).replace_output is not None
    assert other in [p.read_text(encoding="utf-8") for p in _outputs(tmp_path)]
    assert _decisions(tmp_path)[-1]["tokens_full"] is None


def test_persisted_max_bytes_default() -> None:
    from token_sparen.config import packaged_defaults

    assert packaged_defaults()["compression"]["persisted_max_bytes"] == 5_000_000


def _generic(target: int, seed: int = 3) -> str:
    import random

    rng = random.Random(seed)
    words = ["alpha", "beta", "gamma", "delta", "omega", "lorem", "ipsum", "kappa", "zeta"]
    out: list[str] = []
    size = 0
    i = 0
    while size < target:
        ln = f"{i} " + " ".join(rng.choice(words) for _ in range(rng.randint(3, 12)))
        out.append(ln)
        size += len(ln) + 1
        i += 1
    return "\n".join(out)


def test_persisted_generic_never_exceeds_preview(tmp_path: Path) -> None:
    full = _generic(439_000)
    saved = tmp_path / "persisted.txt"
    saved.write_text(full, encoding="utf-8")
    preview = full[:30_000]
    ev = _event(
        tmp_path,
        tool_input={"command": "cat big.txt"},
        response={"stdout": preview, "stderr": "", "persistedOutputPath": str(saved)},
        text=preview,
    )
    widen = _cfg(  # plain text and file contents are opt-in
        tmp_path, COMPRESSION__ALLOW_CTYPES="text", COMPRESSION__TRIM_FILE_CONTENTS="true"
    )
    act = _handle(tmp_path, ev, widen)
    if act.replace_output is not None:
        assert len(act.replace_output) <= len(preview)
    rec = _decisions(tmp_path)[-1]
    assert rec["tokens_before"] == estimate_tokens(preview)
    assert rec["tokens_after"] <= rec["tokens_before"]
    assert rec["tokens_full"] == estimate_tokens(full)


def test_persisted_falls_back_to_compressing_preview(tmp_path: Path) -> None:
    preview = big_pytest(30_000)
    full = preview + "\n" + _generic(400_000)
    saved = tmp_path / "persisted.txt"
    saved.write_text(full, encoding="utf-8")
    ev = _bash(tmp_path, preview, persistedOutputPath=str(saved))
    act = _handle(tmp_path, ev)
    assert act.replace_output is not None
    assert len(act.replace_output) <= len(preview) * 0.85
    assert MARKER.fullmatch(act.replace_output.splitlines()[-1])
    (saved_copy,) = _outputs(tmp_path)
    assert saved_copy.read_text(encoding="utf-8") == full  # recovery still has everything
    rec = _decisions(tmp_path)[-1]
    assert rec["tokens_before"] == estimate_tokens(preview, "text")
    assert _overlay(tmp_path)["tokens_removed"] == rec["tokens_before"] - rec["tokens_after"]


def test_persisted_log_compresses_with_preview_accounting(tmp_path: Path) -> None:
    full = big_pytest(80_000)
    saved = tmp_path / "persisted.txt"
    saved.write_text(full, encoding="utf-8")
    preview = full[:30_000]
    act = _handle(tmp_path, _bash(tmp_path, preview, persistedOutputPath=str(saved)))
    assert act.replace_output is not None and len(act.replace_output) < len(preview) // 4
    assert "2 failed, 140 passed" in act.replace_output
    rec = _decisions(tmp_path)[-1]
    assert rec["tokens_before"] == estimate_tokens(preview)
    assert rec["tokens_full"] == estimate_tokens(full) > rec["tokens_before"]


def test_persisted_file_not_doubled_with_stderr(tmp_path: Path) -> None:
    full = big_pytest(60_000)
    saved = tmp_path / "persisted.txt"
    saved.write_text(full, encoding="utf-8")
    preview = full[:30_000] + "\nWARN-ON-STDERR"
    ev = _bash(tmp_path, preview, persistedOutputPath=str(saved), stderr="WARN-ON-STDERR")
    assert _handle(tmp_path, ev).replace_output is not None
    (saved_copy,) = _outputs(tmp_path)
    assert saved_copy.read_text(encoding="utf-8") == full


def test_slow_hook_skips_replacement(tmp_path: Path) -> None:
    import time as _time

    ev = _bash(tmp_path, big_pytest())
    act = _handle(tmp_path, ev, started=_time.monotonic() - 1.0)
    assert act.is_noop() and _outputs(tmp_path) == []
    assert _decisions(tmp_path)[-1]["recovery_id"] is None


def test_failure_event_compressed(tmp_path: Path) -> None:
    text = big_pytest()
    ev = _event(tmp_path, kind="post_tool_failure", response=None, text=text)
    act = _handle(tmp_path, ev)
    assert act.replace_output is not None
    assert MARKER.fullmatch(act.replace_output.splitlines()[-1])


def test_unreplaceable_shape_does_no_work(tmp_path: Path) -> None:
    text = big_pytest()
    from token_sparen.adapters.claude.translate import ClaudeAdapter

    ev = _event(tmp_path, response={"stdout": text, "isImage": True}, text=text)
    assert _handle(tmp_path, ev, replaceable=ClaudeAdapter().replaceable).is_noop()
    assert _outputs(tmp_path) == [] and _decisions(tmp_path) == []


def test_default_predicate_is_host_neutral(tmp_path: Path) -> None:
    ev = _event(tmp_path, response={"opaque": True}, text=big_pytest())
    assert compression.default_replaceable(ev)  # no host shape knowledge in the mechanism
    assert not compression.default_replaceable(Event(**{**ev.__dict__, "kind": "pre_tool"}))


def test_host_replaceable_predicate_vetoes(tmp_path: Path) -> None:
    ev = _bash(tmp_path, big_pytest())
    assert _handle(tmp_path, ev, replaceable=lambda e: False).is_noop()
    assert _outputs(tmp_path) == [] and _decisions(tmp_path) == []


@pytest.mark.parametrize(
    "tool",
    ["Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite", "AskUserQuestion", "ExitPlanMode"],
)
def test_skip_tools_never_compressed(tmp_path: Path, tool: str) -> None:
    text = big_pytest()
    ev = _event(tmp_path, tool=tool, tool_input={}, response=text, text=text)
    assert _handle(tmp_path, ev).is_noop()
    assert _outputs(tmp_path) == []


def test_skip_tools_configurable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, COMPRESSION__SKIP_TOOLS="Read, Bash")
    assert _handle(tmp_path, _bash(tmp_path, big_pytest()), cfg).is_noop()
    text = big_pytest()
    todo = _event(tmp_path, tool="TodoWrite", tool_input={}, response=text, text=text)
    assert _handle(tmp_path, todo, Config()).is_noop()  # no config value -> packaged deny-set
    for tool in ("TodoWrite", "AskUserQuestion", "ExitPlanMode", "Edit"):  # hard floor
        ev = _event(tmp_path, tool=tool, tool_input={}, response=text, text=text)
        assert _handle(tmp_path, ev, cfg).is_noop()
    assert _outputs(tmp_path) == []


def test_read_content_compressed(tmp_path: Path) -> None:
    content = big_pytest()
    n = len(content.splitlines())
    resp = {
        "type": "text",
        "file": {
            "filePath": "/repo/log.txt",
            "content": content,
            "numLines": n,
            "startLine": 1,
            "totalLines": n,
        },
    }
    ev = _event(tmp_path, tool="Read", tool_input={"file_path": "/repo/log.txt"}, response=resp)
    ev = Event(**{**ev.__dict__, "output_text": content})
    assert _handle(tmp_path, ev).is_noop()  # file contents are never trimmed by default
    act = _handle(tmp_path, ev, _cfg(tmp_path, COMPRESSION__TRIM_FILE_CONTENTS="true"))
    assert act.replace_output is not None
    m = MARKER.fullmatch(act.replace_output.splitlines()[-1])
    assert m is not None and int(m.group(2)) == n


def test_min_saving_ratio_noop_but_logged(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, COMPRESSION__MIN_SAVING_RATIO="0.99")
    assert _handle(tmp_path, _bash(tmp_path, big_pytest()), cfg).is_noop()
    assert _outputs(tmp_path) == []
    (rec,) = _decisions(tmp_path)
    assert rec["recovery_id"] is None and rec["tokens_after"] == rec["tokens_before"]
    assert "trims" not in _overlay(tmp_path)


def test_off_arm_noop_but_logged(tmp_path: Path) -> None:
    ev = _bash(tmp_path, big_pytest())
    act = compression.handle_post_tool(
        ev, StaticPolicy({"compression": "off"}), _cfg(tmp_path), env=_env(tmp_path)
    )
    assert act.is_noop() and _outputs(tmp_path) == []
    assert _decisions(tmp_path)[0]["arm"] == "off"


def test_min_tokens_from_config(tmp_path: Path) -> None:
    small = big_pytest(6_000)  # ~1500 tokens: below the default threshold
    assert _handle(tmp_path, _bash(tmp_path, small)).is_noop()
    text = big_pytest(12_000)
    assert 2000 < estimate_tokens(text) < 4000
    assert _handle(tmp_path, _bash(tmp_path, text)).replace_output is not None
    cfg = _cfg(tmp_path, COMPRESSION__MIN_TOKENS="4000")  # raising the threshold is honoured
    assert _handle(tmp_path, _bash(tmp_path, text), cfg).is_noop()


def test_lossless_result_is_noop(tmp_path: Path) -> None:
    text = "\n".join(f"ERROR line {i} failed" for i in range(1500))
    assert _handle(tmp_path, _bash(tmp_path, text)).is_noop()
    assert _outputs(tmp_path) == []


class _Spy:
    name = "spy"
    handles = frozenset({"test_report"})

    def __init__(self) -> None:
        self.ctx: CompressContext | None = None

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        self.ctx = ctx
        return CompressResult(text="tiny", kept_lines=1, total_lines=9, lossless=False)


def test_context_has_prompt_paths_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    cfg = _cfg(tmp_path)
    tracking.handle_prompt(_event(tmp_path, kind="prompt", prompt="why " * 1000), cfg, env=env)
    for i in range(25):
        read = _event(tmp_path, tool="Read", tool_input={"file_path": f"/r/f{i}.py"})
        tracking.record_paths(read, env=env)
    again = _event(tmp_path, tool="Edit", tool_input={"file_path": "/r/f10.py"})
    tracking.record_paths(again, env=env)
    tracking.record_paths(_event(tmp_path, tool="Bash", tool_input={"file_path": "/x"}), env=env)
    spy = _Spy()
    monkeypatch.setattr(registry, "for_type", lambda t: spy)
    act = compression.handle_post_tool(
        _bash(tmp_path, big_pytest()), StaticPolicy({}), cfg, env=env
    )
    assert act.replace_output is not None and act.replace_output.startswith("tiny\n")
    assert spy.ctx is not None and spy.ctx.config is cfg
    assert spy.ctx.prompt is not None and len(spy.ctx.prompt) == 2000
    paths = list(spy.ctx.recent_paths)
    assert len(paths) == 20 and paths[-1] == "/r/f10.py" and "/x" not in paths
    assert paths.count("/r/f10.py") == 1 and "/r/f0.py" not in paths
    ov = _overlay(tmp_path)
    assert ov["turn"] == 1


def test_prompt_handler_counts_turns(tmp_path: Path) -> None:
    env = _env(tmp_path)
    cfg = _cfg(tmp_path)
    for p in ("one", "two"):
        assert tracking.handle_prompt(
            _event(tmp_path, kind="prompt", prompt=p), cfg, env=env
        ).is_noop()
    ov = _overlay(tmp_path)
    assert ov["turn"] == 2 and ov["last_prompt"] == "two"


def test_overlay_busy_still_compresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.store import jsonstore

    def busy(*a: Any, **k: Any) -> Any:
        raise jsonstore.StoreBusy("held")

    monkeypatch.setattr(jsonstore, "update", busy)
    act = _handle(tmp_path, _bash(tmp_path, big_pytest()))
    assert act.replace_output is not None and len(_decisions(tmp_path)) == 1


@pytest.mark.perf
def test_handler_latency_50kb(tmp_path: Path) -> None:
    text = big_pytest(50_000)
    cfg = _cfg(tmp_path)
    env = _env(tmp_path)
    counter = iter(range(10_000))

    def run() -> Any:
        body = text.replace("rootdir: /srv/demo", f"rootdir: /srv/demo{next(counter)}")
        ev = _bash(tmp_path, body)
        return compression.handle_post_tool(ev, StaticPolicy({}), cfg, env=env)

    best, act = best_of(run, 7)
    assert act.replace_output is not None
    assert best < 0.050 * perf_factor()


def test_min_tokens_floor_is_enforced(tmp_path: Path) -> None:
    lines = big_pytest().splitlines()
    text = "\n".join(lines[: len(lines) // 8])
    tokens = estimate_tokens(text)
    assert 600 < tokens < 2000
    cfg = _cfg(tmp_path, COMPRESSION__MIN_TOKENS="500")
    for _ in range(2):
        assert _handle(tmp_path, _bash(tmp_path, text), cfg).is_noop()
    assert _outputs(tmp_path) == [] and _decisions(tmp_path) == []
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and "min_tokens" in rows[0]["error"] and "2000" in rows[0]["error"]
    # at or above the floor the configured value is used as before
    assert _handle(tmp_path, _bash(tmp_path, big_pytest()), cfg).replace_output is not None


def _floor_rows(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if "min_tokens" in r["error"]]


def test_floor_warning_once_per_project_across_processes(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    proj = tmp_path / "proj"
    proj.mkdir()
    lines = big_pytest().splitlines()
    payload = json.dumps(
        {
            "session_id": "s-floor",
            "cwd": proj.as_posix(),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -v"},
            "tool_use_id": "t1",
            "tool_response": {"stdout": "\n".join(lines[: len(lines) // 8]), "stderr": ""},
        }
    )
    base = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    base.update(
        TOKEN_SPAREN_HOME=str(tmp_path / "home"),
        CLAUDE_PROJECT_DIR=str(proj),
        TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="5000",
    )

    def hook(min_tokens: str) -> None:
        env = {**base, "TOKEN_SPAREN_COMPRESSION__MIN_TOKENS": min_tokens}
        subprocess.run(
            [sys.executable, "-I", str(root / "hooks" / "run.py"), "PostToolUse"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=True,
        )

    hook("500")
    hook("500")  # a second process: still one warning for this project
    assert len(_floor_rows(tmp_path)) == 1
    hook("800")  # the configured value changed: warn again
    rows = _floor_rows(tmp_path)
    assert len(rows) == 2 and "800" in rows[1]["error"]


class _Learner:
    """A learning policy stub: a fixed arm, records every reward."""

    def __init__(self, arm: str) -> None:
        self.arm = arm
        self.rewards: list[tuple[str, float | None, float | None]] = []
        self.features: list[dict[str, str]] = []

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        self.features.append(dict(features))
        arm = self.arm if self.arm in point.arms else point.safe_arm
        return Decision(f"d-{len(self.features)}", point.name, arm, 0.5, "b")

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        self.rewards.append((decision_id, savings, regret))


def test_learning_policy_gets_the_shared_feature_buckets(tmp_path: Path) -> None:
    pol = _Learner("standard")
    ev = _bash(tmp_path, big_pytest())
    compression.handle_post_tool(ev, pol, _cfg(tmp_path), env=_env(tmp_path))
    assert pol.features == [
        {"band": "unk", "cat": "shell", "size": "md", "ctype": "test_report", "phase": "mixed"}
    ]
    (reward,) = pol.rewards  # one savings reward for the trimming decision
    assert reward[0] == "d-1" and reward[1] is not None and reward[1] > 0 and reward[2] is None


def test_safe_arm_is_never_rewarded(tmp_path: Path) -> None:
    pol = _Learner("off")
    compression.handle_post_tool(
        _bash(tmp_path, big_pytest()), pol, _cfg(tmp_path), env=_env(tmp_path)
    )
    assert pol.rewards == []


def test_trimming_arm_past_the_deadline_gets_zero_rewards(tmp_path: Path) -> None:
    pol = _Learner("standard")
    cfg = _cfg(tmp_path, COMPRESSION__SAVE_DEADLINE_MS="0")
    act = compression.handle_post_tool(
        _bash(tmp_path, big_pytest()), pol, cfg, env=_env(tmp_path), started=0.0
    )
    assert act.replace_output is None
    assert pol.rewards == [("d-1", 0.0, 0.0)]  # nothing removed, nothing to regret
