"""User-facing CLI: show, run, stats, doctor, session, stubs, plugin surface files."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sampleoutputs import big_pytest

from token_sparen import recovery
from token_sparen.adapters.claude import entry
from token_sparen.cli import main
from token_sparen.store import keycache, session

ROOT = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell/signal behaviour")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    for k in list(os.environ):
        if k.startswith(("TOKEN_SPAREN_", "CLAUDE_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(proj)
    return {"TOKEN_SPAREN_HOME": str(home), "CLAUDE_PROJECT_DIR": str(proj)}


def _child(tmp_path: Path, text: str, code: int = 0, err: str = "") -> list[str]:
    """A child that emits exactly these bytes (no newline translation on any OS)."""
    src = tmp_path / "out.txt"
    src.write_bytes(text.encode("utf-8"))
    prog = (
        "import sys\n"
        f"sys.stdout.buffer.write(open({src.as_posix()!r}, 'rb').read())\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write({err.encode('utf-8')!r});sys.stderr.buffer.flush()\n"
        f"sys.exit({code})\n"
    )
    # a script file, not `python -c "...open(...)..."`: inline code that opens a file is a
    # file dump the quality gate never trims, and these tests are about `run` itself
    script = tmp_path / "emit.py"
    script.write_text(prog, encoding="utf-8")
    return [sys.executable, str(script)]


def test_show_prints_recovery(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    ref = recovery.save_full(Path.cwd(), "line one\nline é two\n")
    assert main(["show", ref.id]) == 0
    assert capsys.readouterr().out == "line one\nline é two\n"


def test_show_missing_and_invalid(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "0" * 12]) == 1
    assert main(["show", "../../etc/passwd"]) == 2
    assert main(["show", "ABCDEF012345"]) == 2
    assert "token-sparen:" in capsys.readouterr().err


def test_show_project_option(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    ref = recovery.save_full(other, "elsewhere")
    assert main(["show", ref.id]) == 1
    assert main(["show", ref.id, "--project", str(other)]) == 0
    assert "elsewhere" in capsys.readouterr().out


def test_run_preserves_exit_code_and_compresses(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = big_pytest()
    rc = main(["run", "--", *_child(tmp_path, text, code=3)])
    out = capsys.readouterr().out
    assert rc == 3
    assert "[token-sparen: kept " in out and len(out) < len(text)
    assert "FAILED tests/test_cache.py::test_cache_eviction" in out
    m = re.search(r"token-sparen show ([0-9a-f]{12})", out)
    assert m
    saved = Path.cwd() / ".token-sparen" / "outputs" / f"{m.group(1)}.txt"
    assert saved.read_text(encoding="utf-8") == text


def test_run_small_output_unchanged_with_stderr_merged(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--", *_child(tmp_path, "hello\n", err="oops\n")]) == 0
    assert capsys.readouterr().out == "hello\noops\n"


def test_run_tags_cli_host_and_not_counted_as_session(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["run", "--", *_child(tmp_path, big_pytest())])
    capsys.readouterr()
    (log,) = (Path(env["TOKEN_SPAREN_HOME"]) / "projects").glob("*/decisions.jsonl")
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["host"] == "cli"
    assert main(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"] == 0


def test_run_honours_session_disabled(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s-off")
    assert main(["session", "disable"]) == 0
    text = big_pytest()
    assert main(["run", "--", *_child(tmp_path, text)]) == 0
    assert capsys.readouterr().out.endswith(text)


def test_run_missing_command(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--", "definitely-not-a-command-xyz"]) == 127
    assert "token-sparen:" in capsys.readouterr().err
    assert main(["run"]) == 2


@posix_only
def test_run_shell_string_and_signal(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--", "echo a | tr a b; exit 4"]) == 4
    assert capsys.readouterr().out == "b\n"
    assert main(["run", "--", "kill -TERM $$"]) == 128 + 15


def test_run_disabled_prints_original(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_DISABLE", "1")
    text = big_pytest()
    assert main(["run", "--", *_child(tmp_path, text)]) == 0
    assert capsys.readouterr().out == text


def _seed_decisions(env: dict[str, str]) -> str:
    key = keycache.cached_project_key(Path.cwd(), env)
    pdir = Path(env["TOKEN_SPAREN_HOME"]) / "projects" / key
    pdir.mkdir(parents=True)
    rows = [
        {"point": "compression", "arm": "standard", "tokens_before": 1000, "tokens_after": 400},
        {"point": "compression", "arm": "standard", "tokens_before": 500, "tokens_after": 500},
        {"point": "compression", "arm": "off", "tokens_before": 300, "tokens_after": 300},
        {"point": "read_dedupe", "arm": "pointer", "tokens_before": 200, "tokens_after": 20},
    ]
    lines = [json.dumps(r) for r in rows] + ["not json"]
    (pdir / "decisions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ov = session.session_path(Path(env["TOKEN_SPAREN_HOME"]), key, "s1")
    session.update_session(
        ov, lambda s: {**s, "trims": 2, "tokens_removed": 780, "recovery_read_tokens": 5}
    )
    return key


def test_stats_json_schema(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    key = _seed_decisions(env)
    assert main(["stats", "--json", "--session", "s1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {
        "project_key",
        "sessions",
        "decisions",
        "tokens_before",
        "tokens_after",
        "saved",
        "gross_saved",
        "net_saved",
        "recovery_read_tokens",
        "injected_tokens",
        "session",
    }
    assert data["project_key"] == key and data["sessions"] == 1
    assert data["decisions"] == {
        "compression": {"standard": 2, "off": 1},
        "read_dedupe": {"pointer": 1},
    }
    assert (data["tokens_before"], data["tokens_after"], data["saved"]) == (2000, 1220, 780)
    assert data["session"]["trims"] == 2 and data["session"]["recovery_read_tokens"] == 5


def test_stats_empty_and_text(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["stats", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session"] is None and data["saved"] == 0 and data["decisions"] == {}
    _seed_decisions(env)
    assert main(["stats", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "saved" in out and "780" in out and "compression" in out


def test_stats_net_matches_the_statusline(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from token_sparen.mechanisms import tracking

    key = _seed_decisions(env)
    home = Path(env["TOKEN_SPAREN_HOME"])
    ov2 = session.session_path(home, key, "s2")
    session.update_session(
        ov2,
        lambda s: {
            **s,
            "tokens_removed": 100,
            "recovery_read_tokens": 30,
            "injected_tokens": 20,
        },
    )
    assert main(["stats", "--json", "--session", "s2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["gross_saved"] == 880  # tokens_removed over the project's sessions
    assert data["recovery_read_tokens"] == 35 and data["injected_tokens"] == 20
    assert data["net_saved"] == 880 - 35 - 20
    assert data["saved"] == 780  # old key: the decision log's tokens_before - tokens_after
    ledger = data["session"]
    assert (ledger["gross_saved"], ledger["net_saved"]) == (100, 50)
    # the statusline's `saved` segment uses the same helper
    assert tracking.ledger_savings(session.read_session(ov2)) == (100, 50)
    assert main(["stats", "--session", "s2"]) == 0
    out = capsys.readouterr().out
    assert "net 825" in out and "gross 880" in out
    assert "net 50" in out  # the session line, as the statusline shows it


def _doctor_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS", "5000")


def test_doctor_ok_in_tmp_home(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doctor_env(env, monkeypatch)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "✗" not in out and out.count("✓") >= 4
    assert "round-trip" in out


def test_doctor_reports_and_fixes_tmp_files(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doctor_env(env, monkeypatch)
    home = Path(env["TOKEN_SPAREN_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    stale = home / "x.json.tmp-123-ab"
    stale.write_text("x", encoding="utf-8")
    assert main(["doctor"]) == 1
    assert "✗" in capsys.readouterr().out and stale.exists()
    assert main(["doctor", "--fix"]) == 0
    assert not stale.exists()


def test_doctor_fails_on_broken_plugin_root(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _doctor_env(env, monkeypatch)
    root = tmp_path / "plug"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main(["doctor"]) == 1
    assert "✗ plugin" in capsys.readouterr().out


def test_doctor_pip_only_install_skips_plugin_checks(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import token_sparen.cli as cli

    monkeypatch.setattr(cli, "PACKAGE_ROOT", tmp_path / "site-packages")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "skipped (pip-only install" in out and "✗" not in out


def test_check_wheel_script_covers_data_and_run() -> None:
    text = (ROOT / "scripts" / "check_wheel.py").read_text(encoding="utf-8")
    assert "token_sparen/params/defaults.toml" in text and "seq" in text


def test_session_disable_makes_hooks_noop(env: dict[str, str]) -> None:
    payload = {
        "session_id": "sess-9",
        "cwd": str(Path.cwd()),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -v"},
        "tool_use_id": "t1",
        "tool_response": {
            "stdout": big_pytest(),
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    hook_env = {**env, "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000"}

    def call() -> str:
        out = io.StringIO()
        entry.main("PostToolUse", io.StringIO(json.dumps(payload)), out, hook_env)
        return out.getvalue().strip()

    assert main(["session", "disable", "--session", "sess-9"]) == 0
    assert call() == "{}"
    assert main(["session", "enable", "--session", "sess-9"]) == 0
    assert "updatedToolOutput" in call()


def test_session_id_from_env_and_missing(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["session", "disable"]) == 2
    assert "session id" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    assert main(["session", "disable"]) == 0
    key = keycache.cached_project_key(Path.cwd(), env)
    ov = session.session_path(Path(env["TOKEN_SPAREN_HOME"]), key, "from-env")
    assert session.read_session(ov)["disabled"] is True


@pytest.mark.parametrize("cmd", ["prune"])
def test_stubs(cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([cmd]) == 0
    assert "available after M3" in capsys.readouterr().out


def test_facts_is_no_longer_a_stub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["facts"]) == 2  # an action is required
    assert "list" in capsys.readouterr().err


def test_errors_never_traceback(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("token_sparen.cli._stats", boom)
    assert main(["stats"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("token-sparen: ") and "Traceback" not in err


def test_python_dash_m_and_run_cli(env: dict[str, str]) -> None:
    for argv in (
        [sys.executable, "-m", "token_sparen", "--version"],
        [sys.executable, "-I", str(ROOT / "hooks" / "run_cli.py"), "--version"],
    ):
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=False
        )
        assert proc.returncode == 0 and "token-sparen" in proc.stdout


def _skill_front() -> tuple[str, str]:
    text = (ROOT / "skills" / "token-sparen" / "SKILL.md").read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    return front, body


def test_slash_command_is_a_skill() -> None:
    # A plugin skill substitutes ${CLAUDE_PLUGIN_ROOT} in allowed-tools and ${CLAUDE_SESSION_ID}
    # in its body; a command and a skill with the same name would shadow each other.
    assert not (ROOT / "commands" / "token-sparen.md").exists()
    front, body = _skill_front()
    lines = front.splitlines()
    start = lines.index("allowed-tools:")
    tools = []
    for ln in lines[start + 1 :]:
        if not ln.startswith("  - "):
            break
        tools.append(ln[4:].strip().strip("'"))
    # Model-invocable skill: pre-approve only read-only / low-risk subcommands.
    # `session disable` only: re-enabling trimming (`session enable`) needs a permission prompt
    safe = ("stats", "show", "doctor", "session disable")
    boot = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py"'
    assert tools == [f"Bash(token-sparen {c}:*)" for c in safe] + [
        f"Bash({boot} {c}:*)" for c in safe
    ]
    assert not any("session:" in t or "session enable" in t for t in tools)
    assert "argument-hint:" in front
    for bare in ("python3:*", "python:*", "sh:*", "bash:*", "token-sparen:*", 'run_cli.py":*'):
        assert bare not in front
    for risky in ("extensions", "config", "statusline", "prune", "trust", "run"):
        assert not any(f" {risky}" in t.split("run_cli.py")[-1] for t in tools), risky
    assert "permission prompt" in body
    assert "explicitly" in body and "/token-sparen" in body
    assert "$ARGUMENTS" in body
    assert "session disable --session ${CLAUDE_SESSION_ID}" in body
    assert 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_cli.py"' in body
    for needle in ("token-sparen stats", "statusline install", "extensions trust"):
        assert needle in body


def test_skill_file() -> None:
    text = (ROOT / "skills" / "token-sparen" / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "name: token-sparen" in front
    assert "Use when the user asks about token usage, trimmed outputs" in front
    assert len(text.splitlines()) <= 80
    for needle in ("token-sparen show", "token-sparen stats", "TOKEN_SPAREN_DISABLE"):
        assert needle in text


def _hook(event: str, payload: dict[str, object], env: dict[str, str]) -> str:
    out = io.StringIO()
    hook_env = {**env, "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000"}
    entry.main(event, io.StringIO(json.dumps(payload)), out, hook_env)
    return out.getvalue().strip()


def _overlay(env: dict[str, str], sid: str) -> dict[str, object]:
    key = keycache.cached_project_key(Path.cwd(), env)
    return session.read_session(session.session_path(Path(env["TOKEN_SPAREN_HOME"]), key, sid))


def test_session_disable_defaults_to_last_active_session(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    cwd = str(Path.cwd())
    _hook("SessionStart", {"session_id": "old-1", "cwd": cwd, "source": "startup"}, env)
    _hook("UserPromptSubmit", {"session_id": "live-2", "cwd": cwd, "prompt": "hi"}, env)
    assert main(["session", "disable"]) == 0
    assert "live-2" in capsys.readouterr().out
    assert _overlay(env, "live-2")["disabled"] is True
    assert "disabled" not in _overlay(env, "old-1")
    assert main(["session", "enable", "--session", ""]) == 0  # empty flag: fall back too
    assert "disabled" not in _overlay(env, "live-2")


def test_session_last_active_expires(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    cwd = str(Path.cwd())
    _hook("UserPromptSubmit", {"session_id": "stale-3", "cwd": cwd, "prompt": "hi"}, env)
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 6 * 3600 + 5)
    assert main(["session", "disable"]) == 2
    err = capsys.readouterr().err
    assert "--session" in err and "6 h" in err
    assert "disabled" not in _overlay(env, "stale-3")


def test_explicit_session_beats_last_active(env: dict[str, str]) -> None:
    cwd = str(Path.cwd())
    _hook("UserPromptSubmit", {"session_id": "live-4", "cwd": cwd, "prompt": "hi"}, env)
    assert main(["session", "disable", "--session", "other-5"]) == 0
    assert _overlay(env, "other-5")["disabled"] is True
    assert "disabled" not in _overlay(env, "live-4")


@pytest.mark.parametrize(
    ("platform", "found", "needles"),
    [
        ("win32", {"py", "python"}, ("py -3 -m token_sparen", "python3")),
        ("win32", {"python"}, ("python -m token_sparen", "python3")),
        ("win32", set(), ("python.org", "python3")),
        ("linux", set(), ("hooks run python3",)),
    ],
)
def test_python3_missing_hint(platform: str, found: set[str], needles: tuple[str, ...]) -> None:
    import token_sparen.cli as cli

    text = cli.python3_missing_hint(platform, lambda name: f"/x/{name}" if name in found else None)
    for needle in needles:
        assert needle in text
    if platform != "win32":
        assert "py -3" not in text


def test_doctor_windows_hint(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import token_sparen.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/py.exe" if name == "py" else None)
    monkeypatch.setattr(cli, "_host_platform", lambda: "win32")
    main(["doctor"])
    captured = capsys.readouterr()
    assert "py -3 -m token_sparen" in captured.out, captured.err


def test_config_set_session_layer_reaches_hooks(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    cwd = Path.cwd().as_posix()
    find = {
        "session_id": "cfg-1",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "find / -name x"},
        "tool_use_id": "t1",
    }
    assert "deny" in _hook("PreToolUse", find, env)  # guards on by default
    assert main(["config", "set", "guards.enabled", "false", "--session", "cfg-1"]) == 0
    assert "cfg-1" in capsys.readouterr().out
    assert _overlay(env, "cfg-1")["config_overrides"] == {"guards": {"enabled": False}}
    assert _hook("PreToolUse", find, env) == "{}"
    other = {**find, "session_id": "cfg-2"}
    assert "deny" in _hook("PreToolUse", other, env)  # other sessions unaffected


def test_config_set_rejects_unknown_keys(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "set", "guards.enabeld", "false", "--session", "s"]) == 2
    assert "unknown config key" in capsys.readouterr().err
    assert main(["config", "set", "guards", "false", "--session", "s"]) == 2
    assert main(["config", "set", "compression.min_tokens", "3000", "--session", "s"]) == 0
    assert _overlay(env, "s")["config_overrides"] == {"compression": {"min_tokens": 3000}}


@pytest.mark.parametrize("point", ["compression", "pretrim", "read_dedupe"])
def test_config_set_can_hold_a_point_per_session(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], point: str
) -> None:
    key = f"learn.hold.{point}"
    assert main(["config", "set", key, "true", "--session", "h1"]) == 0, capsys.readouterr().err
    assert _overlay(env, "h1")["config_overrides"] == {"learn": {"hold": {point: True}}}


def test_config_set_compression_hold_stops_trimming_in_that_session(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from sampleoutputs import big_pytest

    def post(sid: str, n: int) -> str:
        payload = {
            "session_id": sid,
            "cwd": Path.cwd().as_posix(),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"pytest -q t{n}.py"},
            "tool_use_id": f"{sid}-{n}",
            "tool_response": {
                "stdout": big_pytest(20_000),
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
        return _hook("PostToolUse", payload, env)

    learn_off = {**env, "TOKEN_SPAREN_LEARN__ENABLED": "false"}
    env.update(learn_off)
    assert "updatedToolOutput" in post("h2", 1)  # static standard trims
    assert main(["config", "set", "learn.hold.compression", "yes", "--session", "h2"]) == 0
    assert post("h2", 2) == "{}"
    assert "updatedToolOutput" in post("h3", 3)  # other sessions unaffected


def test_config_set_uses_last_active_session(env: dict[str, str]) -> None:
    cwd = Path.cwd().as_posix()
    _hook("UserPromptSubmit", {"session_id": "live-9", "cwd": cwd, "prompt": "hi"}, env)
    assert main(["config", "set", "governor.hard_stop", "true"]) == 0
    assert _overlay(env, "live-9")["config_overrides"] == {"governor": {"hard_stop": True}}


def test_doctor_reports_min_tokens_below_floor(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import token_sparen.cli as cli

    monkeypatch.setattr(cli, "PACKAGE_ROOT", Path.cwd() / "no-plugin")  # skip the round-trip
    home = Path(env["TOKEN_SPAREN_HOME"])  # a project file may only raise it
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[compression]\nmin_tokens = 700\n", encoding="utf-8")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "min_tokens" in ln)
    assert line.startswith("!") and "700" in line and "2000" in line


def test_launcher_ships_inside_the_package() -> None:
    import tomllib

    import token_sparen.cli as cli

    assert cli.LAUNCHER_TEMPLATE.is_file()
    assert cli.LAUNCHER_TEMPLATE.parent.name == "assets"
    assert cli.LAUNCHER_TEMPLATE.parent.parent.name == "token_sparen"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = pyproject["tool"]["setuptools"]["package-data"]["token_sparen"]
    assert "assets/statusline.sh" in data
    assert not (ROOT / "scripts" / "statusline.sh").exists()


def test_check_wheel_script_installs_the_statusline() -> None:
    text = (ROOT / "scripts" / "check_wheel.py").read_text(encoding="utf-8")
    assert "statusline" in text and "--settings" in text and "uninstall" in text


def test_doctor_round_trip_does_not_depend_on_learning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthetic round trip checks the pipeline: an engine that happened to draw its safe
    arm must not make `doctor` report a broken install, so it runs with learning off."""
    from token_sparen import cli

    seen: dict[str, str] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/python3")
    cli._round_trip(Path(__file__).resolve().parents[1])
    assert seen["TOKEN_SPAREN_LEARN__ENABLED"] == "false"
