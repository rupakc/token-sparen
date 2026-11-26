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
    src = tmp_path / "out.txt"
    src.write_text(text, encoding="utf-8")
    prog = (
        "import sys;"
        f"sys.stdout.write(open({src.as_posix()!r}, encoding='utf-8').read());sys.stdout.flush();"
        f"sys.stderr.write({err!r});sys.exit({code})"
    )
    return [sys.executable, "-c", prog]


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


@pytest.mark.parametrize("cmd", ["fold", "replay", "facts", "prune"])
def test_stubs(cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([cmd]) == 0
    assert "available after M2/M3" in capsys.readouterr().out


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


def test_slash_command_file() -> None:
    text = (ROOT / "commands" / "token-sparen.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "description:" in front and "argument-hint:" in front
    tools = [ln for ln in front.splitlines() if ln.startswith("allowed-tools:")]
    assert tools == ["allowed-tools: Bash(token-sparen:*)"]
    for bare in ("python3:*", "python:*", "sh:*", "bash:*"):
        assert bare not in front
    for needle in ("token-sparen stats", "statusline install", "session disable", "run_cli.py"):
        assert needle in text


def test_skill_file() -> None:
    text = (ROOT / "skills" / "token-sparen" / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "name: token-sparen" in front
    assert "Use when the user asks about token usage, trimmed outputs" in front
    assert len(text.splitlines()) <= 60
    for needle in ("token-sparen show", "token-sparen stats", "TOKEN_SPAREN_DISABLE"):
        assert needle in text
