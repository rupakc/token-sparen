"""Output-style directive (spec 6.6): off by default, once per context epoch, user opt-in only."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from token_sparen.adapters.claude import entry
from token_sparen.config import load_config, policy_for
from token_sparen.estimate import estimate_tokens
from token_sparen.mechanisms import outputstyle
from token_sparen.store import jsonstore, keycache, session

TEXT = (
    "[token-sparen output style] Be concise in prose. Never shorten code, commands, file paths, "
    "error messages or quoted tool output."
)


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in list(os.environ):
        if k.startswith(("TOKEN_SPAREN_", "CLAUDE_")):
            monkeypatch.delenv(k)
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _env(tmp_path: Path, proj: Path, **extra: str) -> dict[str, str]:
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(proj),
        "TOKEN_SPAREN_FACTS__ENABLED": "false",  # isolate this mechanism's context
    }
    env.update(extra)
    return env


def _on(tmp_path: Path, proj: Path, **extra: str) -> dict[str, str]:
    return _env(tmp_path, proj, TOKEN_SPAREN_OUTPUT_STYLE__ENABLED="true", **extra)


def _start(env: dict[str, str], source: str = "startup", sid: str = "s1") -> str | None:
    out = io.StringIO()
    payload = {"session_id": sid, "source": source}
    assert entry.main("SessionStart", io.StringIO(json.dumps(payload)), out, env) == 0
    doc = json.loads(out.getvalue())
    ctx = doc.get("hookSpecificOutput", {}).get("additionalContext")
    return ctx if isinstance(ctx, str) else None


def _overlay(env: dict[str, str], proj: Path, sid: str = "s1") -> dict[str, Any]:
    key = keycache.cached_project_key(proj, env)
    return session.read_session(session.session_path(Path(env["TOKEN_SPAREN_HOME"]), key, sid))


def _errors(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "logs" / "errors.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_directive_table_is_exact() -> None:
    assert outputstyle.DIRECTIVES == {
        "concise": "Be concise in prose. Never shorten code, commands, file paths, error "
        "messages or quoted tool output."
    }
    assert outputstyle.PREFIX + outputstyle.DIRECTIVES["concise"] == TEXT


def test_off_by_default_injects_nothing(tmp_path: Path, proj: Path) -> None:
    env = _env(tmp_path, proj)
    cfg = load_config(proj, env)
    assert cfg.get("output_style.enabled") is False
    assert cfg.get("output_style.directive") == "concise"
    for source in ("startup", "clear", "compact", "resume"):
        assert _start(env, source) is None
    assert outputstyle.EPOCH_KEY not in _overlay(env, proj)


def test_on_injects_once_per_context_epoch(tmp_path: Path, proj: Path) -> None:
    env = _on(tmp_path, proj)
    assert _start(env) == TEXT
    assert _overlay(env, proj)["injected_tokens"] == estimate_tokens(TEXT)
    assert _start(env) is None  # a second startup hook in the same context: deduped
    assert _overlay(env, proj)["injected_tokens"] == estimate_tokens(TEXT)
    assert _start(env, "clear") == TEXT  # a new context: injected and counted again
    assert _start(env, "clear") == TEXT
    assert _overlay(env, proj)["injected_tokens"] == 3 * estimate_tokens(TEXT)
    assert _start(env, sid="s2") == TEXT  # another session has its own context


def test_resume_does_not_reinject_compact_does(tmp_path: Path, proj: Path) -> None:
    env = _on(tmp_path, proj)
    assert _start(env) == TEXT
    assert _start(env, "resume") is None
    assert _start(env, "compact") == TEXT
    assert _start(env, "resume") is None
    assert _overlay(env, proj)["injected_tokens"] == 2 * estimate_tokens(TEXT)
    # a resume never injects, even when nothing was sent yet in this session
    assert _start(env, "resume", sid="s3") is None


def test_project_file_cannot_enable(tmp_path: Path, proj: Path) -> None:
    assert policy_for(("output_style", "enabled")) == "false_only"
    assert policy_for(("output_style", "directive")) == "user_only"
    (proj / "token-sparen.toml").write_text(
        '[output_style]\nenabled = true\ndirective = "concise"\n', encoding="utf-8"
    )
    env = _env(tmp_path, proj)
    assert load_config(proj, env).get("output_style.enabled") is False
    assert _start(env) is None
    # the user opted in (config.toml); the project file may switch it off again
    home = Path(env["TOKEN_SPAREN_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[output_style]\nenabled = true\n", encoding="utf-8")
    assert _start(env, sid="s2") == TEXT
    (proj / "token-sparen.toml").write_text("[output_style]\nenabled = false\n", encoding="utf-8")
    assert _start(env, sid="s3") is None


def test_project_file_cannot_change_directive(tmp_path: Path, proj: Path) -> None:
    (proj / "token-sparen.toml").write_text(
        '[output_style]\ndirective = "other"\n', encoding="utf-8"
    )
    env = _on(tmp_path, proj)
    assert load_config(proj, env).get("output_style.directive") == "concise"
    assert _start(env) == TEXT


def test_unknown_directive_is_off(tmp_path: Path, proj: Path) -> None:
    env = _on(tmp_path, proj, TOKEN_SPAREN_OUTPUT_STYLE__DIRECTIVE="shouty")
    assert _start(env) is None
    assert _start(env, "clear") is None
    assert _start(env, "compact") is None
    assert _start(env, sid="s2") is None
    logged = [e for e in _errors(tmp_path) if e["where"] == "outputstyle"]
    assert len(logged) == 1
    assert "shouty" in logged[0]["error"]
    assert "injected_tokens" not in _overlay(env, proj)


def test_disable_env_noop(tmp_path: Path, proj: Path) -> None:
    env = _on(tmp_path, proj, TOKEN_SPAREN_DISABLE="1")
    out = io.StringIO()
    payload = json.dumps({"session_id": "s1", "source": "startup"})
    assert entry.main("SessionStart", io.StringIO(payload), out, env) == 0
    assert out.getvalue() == "{}\n"
    assert not (tmp_path / "home").exists()


def test_unknown_session_is_skipped(tmp_path: Path, proj: Path) -> None:
    env = _on(tmp_path, proj)
    out = io.StringIO()
    assert entry.main("SessionStart", io.StringIO('{"source": "startup"}'), out, env) == 0
    assert "output style" not in out.getvalue()


def test_store_failure_injects_nothing(
    tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*a: Any, **kw: Any) -> Any:
        raise jsonstore.StoreBusy("busy")

    env = _on(tmp_path, proj)
    monkeypatch.setattr(jsonstore, "update", busy)
    assert _start(env) is None
