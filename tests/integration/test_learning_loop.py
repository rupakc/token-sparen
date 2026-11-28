"""The learning loop through the real hook path (Task 23): ``entry.main`` with crafted payloads.

Every decision point asks the per-hook Engine; rewards arrive through later hooks (recovery
reads, expired regret windows); sessions are folded like the SessionEnd spawn would. The rng is
seeded via ``TOKEN_SPAREN_TEST_SEED`` (honoured only with a non-default ``TOKEN_SPAREN_HOME``).
"""

from __future__ import annotations

import collections
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from sampleoutputs import big_pytest
from userconf import to_user_config

from token_sparen.adapters.claude import entry, statusline
from token_sparen.learn import overlay
from token_sparen.store import session

# 10-27 KB (~2.5-7k tokens): above the 2000-token floor, all in the "sm" size band. Sizes vary
# like real runs, so the bucket's savings scale (p95) does not saturate every reward at 1.
SIZES = [10_000 + (i * 7919) % 17_000 for i in range(97)]
TEXTS = {n: big_pytest(n) for n in sorted(set(SIZES))}


class Driver:
    """One Claude Code session of one project, driven through ``entry.main``."""

    def __init__(self, home: Path, proj: Path, sid: str, seed: str = "7", **env: str) -> None:
        proj.mkdir(parents=True, exist_ok=True)
        self.home, self.proj, self.sid = home, proj, sid
        self.env = {
            "TOKEN_SPAREN_HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(proj),
            "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
            "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "10000",
            "TOKEN_SPAREN_TEST_SEED": seed,
            **env,
        }
        # widening settings live in the user's file (a TOKEN_SPAREN_* variable may only narrow)
        self.env = to_user_config(self.env)
        self.n = 0

    def hook(self, event: str, **payload: Any) -> dict[str, Any]:
        body = {"session_id": self.sid, "cwd": str(self.proj), "hook_event_name": event}
        body.update(payload)
        out = io.StringIO()
        assert entry.main(event, io.StringIO(json.dumps(body)), out, self.env) == 0
        text = out.getvalue().strip()
        return json.loads(text) if text.startswith("{") else {"text": text}

    def prompt(self) -> None:
        self.hook("UserPromptSubmit", prompt="continue with the next step")

    def bash(self) -> tuple[str, str | None]:
        """One distinct pytest run (never a re-run); (arm, recovery id or None)."""
        self.n += 1
        use_id = f"{self.sid}-tu-{self.n}"
        out = self.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": f"pytest -q tests/test_mod_{self.n:04d}.py"},
            tool_use_id=use_id,
            tool_response={
                "stdout": TEXTS[SIZES[self.n % len(SIZES)]],
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        )
        rec = [r for r in decisions(self.home) if r["tool_use_id"] == use_id]
        assert len(rec) == 1, rec
        replaced = "hookSpecificOutput" in out
        assert replaced == (rec[0]["recovery_id"] is not None)
        return rec[0]["arm"], rec[0]["recovery_id"]

    def recover(self, rid: str) -> None:
        """The agent reads the saved full output (a full regret of that trim)."""
        self.n += 1
        path = self.proj / ".token-sparen" / "outputs" / f"{rid}.txt"
        content = path.read_text(encoding="utf-8")
        self.hook(
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(path)},
            tool_use_id=f"{self.sid}-tu-{self.n}",
            tool_response={
                "type": "text",
                "file": {"filePath": str(path), "content": content, "numLines": 1},
            },
        )

    def end(self) -> None:
        self.hook("SessionEnd", reason="exit")
        key = project_key(self.home, self.proj)
        overlay.fold(self.sid, key, self.home)  # what the SessionEnd spawn runs


def decisions(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(home.glob("projects/*/decisions.jsonl")):
        out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in out if "kind" not in r]  # outcome lines are not decisions


def project_key(home: Path, proj: Path) -> str:
    for rec_path in home.glob("projects/*/decisions.jsonl"):
        return rec_path.parent.name
    raise AssertionError("no decision log yet")


def share(arms: Iterable[str], wanted: set[str]) -> float:
    arms = list(arms)
    return sum(a in wanted for a in arms) / len(arms)


def rounds(d: Driver, n: int, *, recover: bool) -> list[str]:
    arms = []
    for _ in range(n):
        d.prompt()
        arm, rid = d.bash()
        arms.append(arm)
        if recover and rid is not None:
            d.recover(rid)
    return arms


# --------------------------------------------------------------------------- learning curves


def test_learns_conservative_after_recovery_reads(tmp_path: Path) -> None:
    """Every trim is followed by a recovery read: the engine stops trimming in that bucket."""
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-cons")
    first = rounds(d, 30, recover=True)
    last = rounds(d, 100, recover=True)
    assert first[0] == "light"  # cold: the ladder's first arm is explored
    assert share(last, {"off", "light"}) == 1.0  # the ladder never unlocked a stronger arm
    assert share(last, {"off"}) >= 0.8  # trimming arms are rarely chosen
    recs = [r for r in decisions(d.home) if r["point"] == "compression"]
    assert all(r["bucket"] != "static" and r["propensity"] <= 1.0 for r in recs)
    assert len({r["bucket"] for r in recs}) == 1  # one bucket throughout


def _aggressive_run(home: Path, proj: Path, sessions: int, per: int) -> list[str]:
    arms: list[str] = []
    for s in range(sessions):
        d = Driver(home, proj, f"s-agg-{s}")
        d.n = s * 10_000
        arms += rounds(d, per, recover=False)
        d.end()
    return arms


def test_learns_aggressive_when_never_regretted(tmp_path: Path) -> None:
    """No trim is ever regretted: the ladder unlocks standard, then aggressive."""
    arms = _aggressive_run(tmp_path / "home", tmp_path / "proj", sessions=6, per=40)
    assert "standard" in arms[:120], collections.Counter(arms[:120])
    assert "aggressive" in arms, collections.Counter(arms)
    first_std = arms.index("standard")
    first_agg = arms.index("aggressive")
    assert first_std < first_agg
    tail = arms[-40:]
    assert share(tail, {"standard", "aggressive"}) >= 0.5, collections.Counter(tail)


def test_learning_disabled_uses_static(tmp_path: Path) -> None:
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-off", TOKEN_SPAREN_LEARN__ENABLED="false")
    arms = rounds(d, 5, recover=True)
    assert arms == ["standard"] * 5
    recs = decisions(d.home)
    assert all(r["bucket"] == "static" and r["propensity"] == 1.0 for r in recs)
    state = session.read_session(next(d.home.glob("projects/*/sessions/*.json")))
    assert not state.get("evidence") and not list(d.home.glob("projects/*/sessions/pending/*"))


def test_cross_project_prior_transfer(tmp_path: Path) -> None:
    """Project A learns that stronger arms are never regretted (folded into global); a fresh
    project B in the same home starts from A's preferences (the ladder is already unlocked via
    the global back-off evidence), unlike a fresh project in an untrained home."""
    home = tmp_path / "home"
    a_arms = _aggressive_run(home, tmp_path / "proj-a", sessions=6, per=40)
    assert share(a_arms[-40:], {"standard", "aggressive"}) >= 0.5
    assert share(a_arms[:20], {"standard", "aggressive"}) == 0.0  # A itself started cold
    b = Driver(home, tmp_path / "proj-b", "s-b")
    b_arms = rounds(b, 20, recover=False)
    c = Driver(tmp_path / "home-c", tmp_path / "proj-c", "s-c")
    c_arms = rounds(c, 20, recover=False)
    assert share(c_arms, {"standard", "aggressive"}) == 0.0  # cold ladder: light/off only
    assert "standard" in b_arms[:5], b_arms
    assert share(b_arms, {"standard", "aggressive"}) >= 0.25, collections.Counter(b_arms)
    assert (home / "global" / "posteriors.json").is_file()  # B's only source: the global store


# --------------------------------------------------------------------------- wiring details


def test_engine_drives_compression_by_default(tmp_path: Path) -> None:
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-def")
    rounds(d, 3, recover=False)
    (rec, *_) = [r for r in decisions(d.home) if r["point"] == "compression"]
    assert rec["bucket"] == "band=unk|cat=shell|size=sm|ctype=test_report|phase=mixed"
    side = session.read_session(next(d.home.glob("projects/*/sessions/pending/*.json")))
    assert side.get("pending")


def test_savings_reward_once_per_trimming_decision(tmp_path: Path) -> None:
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-rew")
    arms = rounds(d, 12, recover=False)
    state = session.read_session(next(d.home.glob("projects/*/sessions/*.json")))
    pending = session.read_session(next(d.home.glob("projects/*/sessions/pending/*.json")))[
        "pending"
    ]
    recs = [r for r in decisions(d.home) if r["point"] == "compression"]
    for rec in recs:
        kinds = pending[rec["decision_id"]][4] if len(pending[rec["decision_id"]]) > 4 else []
        if rec["arm"] == "off":
            assert kinds == []  # the safe arm is never rewarded
        else:
            assert "savings" in kinds
    trims = sum(a != "off" for a in arms)
    ev = state["evidence"]["compression"]
    (bucket,) = ev
    n_sav = sum(v["savings"][2] for v in ev[bucket].values())
    assert trims > 0 and round(n_sav, 6) <= trims  # discounted count of one reward per trim
    assert n_sav > trims * 0.98**trims - 1e-9


def test_ctx_band_from_governor_cache(tmp_path: Path) -> None:
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-band")
    transcript = tmp_path / "t.jsonl"
    usage = {"input_tokens": 1000, "cache_read_input_tokens": 120_000, "output_tokens": 10}
    line = {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": usage}}
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    d.hook(
        "PostToolUse",
        transcript_path=str(transcript),
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        tool_use_id="tu-band",
        tool_response={
            "stdout": TEXTS[SIZES[0]],
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    )
    (rec,) = [r for r in decisions(d.home) if r["tool_use_id"] == "tu-band"]
    assert rec["bucket"].startswith("band=50_75|"), rec["bucket"]


def test_breaker_trip_via_fold_makes_next_session_safe(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    d = Driver(home, proj, "s-trip")
    rounds(d, 1, recover=False)
    key = project_key(home, proj)
    bucket = "band=unk|cat=shell|size=sm|ctype=test_report|phase=mixed"
    path = session.session_path(home, key, "s-trip")
    state = session.read_session(path)
    state["regret_outcomes"] = [
        [f"x{i}", "compression", "light", "pytest", 1.0, bucket] for i in range(30)
    ]
    path.write_text(json.dumps(state), encoding="utf-8")
    overlay.fold("s-trip", key, home)
    nxt = Driver(home, proj, "s-after")
    start = nxt.hook("SessionStart", source="startup")
    assert "token-sparen" in json.dumps(start) and "compression" in json.dumps(start)
    nxt.n = 500
    rounds(nxt, 8, recover=False)
    recs = [r for r in decisions(home) if r["tool_use_id"].startswith("s-after")]
    assert recs and all(r["arm"] == "off" and r["propensity"] == 1.0 for r in recs)
    line = statusline.render(
        {
            "session_id": "s-after",
            "transcript_path": str(tmp_path / "none.jsonl"),
            "workspace": {"project_dir": str(proj)},
        },
        nxt.env,
    )
    assert "learn: safe-mode" in line


def test_engine_error_falls_back_to_static(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.learn import engine as engine_mod

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(engine_mod.Engine, "_decide", boom)
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-err")
    arms = rounds(d, 2, recover=False)
    assert arms == ["standard", "standard"]
    recs = decisions(d.home)
    assert all(r["bucket"] == "static" for r in recs)
    errors = (d.home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert errors.count("store exploded") == 2  # once per hook


def test_engine_construction_error_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from token_sparen.learn import hookpolicy

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("no store")

    monkeypatch.setattr(hookpolicy, "StoreChain", boom)
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-err2")
    assert rounds(d, 1, recover=False) == ["standard"]
    errors = (d.home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert errors.count("no store") == 1


def test_engine_built_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.learn import hookpolicy

    built: list[int] = []
    real = hookpolicy.Engine

    def counting(*args: Any, **kwargs: Any) -> Any:
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hookpolicy, "Engine", counting)
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-lazy")
    d.hook("SessionStart", source="startup")
    d.prompt()
    d.hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="p1")
    assert built == []
    d.bash()
    assert built == [1]


def test_subagent_brief_held_at_safe_arm(tmp_path: Path) -> None:
    """Spec 6.7: the brief stays off until the live benchmark; decided, never explored."""
    d = Driver(tmp_path / "home", tmp_path / "proj", "s-sub")
    for i in range(10):
        out = d.hook(
            "PreToolUse",
            tool_name="Task",
            tool_input={"prompt": f"investigate module {i}", "description": "x"},
            tool_use_id=f"sub-{i}",
        )
        assert "updatedInput" not in json.dumps(out)
    recs = [r for r in decisions(d.home) if r["point"] == "subagent_brief"]
    assert len(recs) == 10
    assert all(r["arm"] == "off" and r["bucket"] == "phase=mixed" for r in recs)


def test_read_dedupe_goes_through_engine(tmp_path: Path) -> None:
    # Read outputs are not compressed here, so the first read reaches the model in full
    d = Driver(
        tmp_path / "home", tmp_path / "proj", "s-rd", TOKEN_SPAREN_COMPRESSION__SKIP_TOOLS="Read"
    )
    f = d.proj / "big.py"
    f.write_text("\n".join(f"value_{i} = {i}  # padding text" for i in range(1200)), "utf-8")
    content = f.read_text(encoding="utf-8")
    for i in range(2):
        # distinct prompts: a re-sent earlier prompt (as after /rewind) starts a new epoch
        d.hook("UserPromptSubmit", prompt=f"continue with step {i}")
        d.hook(
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(f)},
            tool_use_id=f"rd-{i}",
            tool_response={
                "type": "text",
                "file": {"filePath": str(f), "content": content, "numLines": 1200},
            },
        )
    (rec,) = [r for r in decisions(d.home) if r["point"] == "read_dedupe"]
    assert rec["bucket"].startswith("cat=read|size=")


@pytest.mark.real_seed
def test_test_seed_ignored_for_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from token_sparen.learn import hookpolicy

    assert hookpolicy.test_seed({"TOKEN_SPAREN_TEST_SEED": "1"}) is None
    monkeypatch.setenv("HOME", str(tmp_path))  # even a default root inside the temp dir
    default = str(Path.home() / ".token-sparen")
    env = {"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": default}
    assert hookpolicy.test_seed(env) is None
    env = {"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": str(tmp_path / "ts-home")}
    assert hookpolicy.test_seed(env) == "1"
    env = {"TOKEN_SPAREN_TEST_SEED": "1", "TOKEN_SPAREN_HOME": "/elsewhere/ts-home"}
    assert hookpolicy.test_seed(env) is None  # outside the temp dir


def test_project_file_cannot_release_the_subagent_hold(tmp_path: Path) -> None:
    """A committed project file must not switch the held brief on (review round 1)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "token-sparen.toml").write_text("[learn.hold]\nsubagent_brief = false\n", "utf-8")
    d = Driver(tmp_path / "home", proj, "s-hold")
    for i in range(20):
        out = d.hook(
            "PreToolUse",
            tool_name="Task",
            tool_input={"prompt": f"investigate module {i}", "description": "x"},
            tool_use_id=f"hold-{i}",
        )
        assert "updatedInput" not in json.dumps(out)
