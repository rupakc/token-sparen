"""Quality-first gating (owner decision 2026-11-26): file contents and unproven content types are
never offered to the compression decision point by default; the user may widen, a project may
only narrow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sampleoutputs import (
    big_diff,
    big_json,
    big_listing,
    big_log,
    big_pytest,
    plain_text,
    python_source,
)
from userconf import to_user_config

from token_sparen import config as config_mod
from token_sparen.compress import detect
from token_sparen.config import Config, load_config
from token_sparen.events import Event, SessionCtx, ToolCall
from token_sparen.learn.policy import Decision, DecisionPoint, StaticPolicy
from token_sparen.mechanisms import compression

KEY = "0123456789abcdef"


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return env


def _cfg(tmp_path: Path, **over: str) -> Config:
    (tmp_path / "proj").mkdir(exist_ok=True)
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _user_file(tmp_path: Path, body: str) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body, encoding="utf-8")


def _project_file(tmp_path: Path, body: str) -> None:
    (tmp_path / "proj").mkdir(exist_ok=True)
    (tmp_path / "proj" / "token-sparen.toml").write_text(body, encoding="utf-8")


def _tool(name: str, inp: Mapping[str, Any]) -> ToolCall:
    from token_sparen.adapters.claude.translate import tool_category

    return ToolCall(name, tool_category(name), dict(inp), "tu-1")


def _event(tmp_path: Path, tool: ToolCall, text: str) -> Event:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind="post_tool",
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=tool,
        output_text=text,
        raw_response=text,
    )


def _bash(tmp_path: Path, cmd: str, text: str) -> Event:
    return _event(tmp_path, _tool("Bash", {"command": cmd}), text)


def _read(tmp_path: Path, text: str, path: str = "/repo/big.py") -> Event:
    return _event(tmp_path, _tool("Read", {"file_path": path}), text)


def _handle(tmp_path: Path, ev: Event, cfg: Config | None = None) -> Any:
    cfg = cfg or _cfg(tmp_path)
    return compression.handle_post_tool(ev, StaticPolicy({}), cfg, env=_env(tmp_path))


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _outputs(tmp_path: Path) -> list[Path]:
    out = tmp_path / "proj" / ".token-sparen" / "outputs"
    return sorted(out.glob("*.txt")) if out.exists() else []


def _untouched(tmp_path: Path, ev: Event, cfg: Config | None = None) -> None:
    """Left as is, and the decision point was never consulted: no log, no recovery file."""
    assert _handle(tmp_path, ev, cfg).is_noop()
    assert _decisions(tmp_path) == []
    assert _outputs(tmp_path) == []


def _trimmed(tmp_path: Path, ev: Event, cfg: Config | None = None) -> None:
    act = _handle(tmp_path, ev, cfg)
    assert act.replace_output is not None and "[token-sparen: kept" in act.replace_output
    assert len(_outputs(tmp_path)) == 1


# --------------------------------------------------------------------------- hook path


def test_read_tool_untouched(tmp_path: Path) -> None:
    _untouched(tmp_path, _read(tmp_path, python_source()))


def test_read_tool_untouched_whatever_the_content_type(tmp_path: Path) -> None:
    text = big_pytest()
    assert detect(text) == "test_report"
    _untouched(tmp_path, _read(tmp_path, text, "/repo/run.log"))


def test_cat_of_a_big_file_untouched(tmp_path: Path) -> None:
    _untouched(tmp_path, _bash(tmp_path, "cat big.py", python_source()))


@pytest.mark.parametrize(
    "cmd",
    [
        "tail -n 2000 app.log",
        "bash -c 'cat app.log'",
        "find . -name '*.log' | xargs cat",
        "git show HEAD~1:app.log",
    ],
)
def test_file_dump_of_an_allowed_type_untouched(tmp_path: Path, cmd: str) -> None:
    text = big_log()
    ev = _bash(tmp_path, cmd, text)
    assert detect(text, ev.tool) == "log"
    _untouched(tmp_path, ev)


def test_git_diff_compressed(tmp_path: Path) -> None:
    ev = _bash(tmp_path, "git diff", big_diff())
    assert detect(big_diff(), ev.tool) == "diff"
    _trimmed(tmp_path, ev)


def test_pytest_report_compressed(tmp_path: Path) -> None:
    _trimmed(tmp_path, _bash(tmp_path, "pytest -v", big_pytest()))
    (rec,) = _decisions(tmp_path)
    assert rec["ctype"] == "test_report"


def test_json_api_output_compressed(tmp_path: Path) -> None:
    ev = _bash(tmp_path, "curl -s https://api.example.org/users", big_json())
    assert detect(big_json(), ev.tool) == "json"
    _trimmed(tmp_path, ev)


def test_mcp_json_output_compressed(tmp_path: Path) -> None:
    _trimmed(tmp_path, _event(tmp_path, _tool("mcp__api__list_users", {}), big_json()))


def test_ls_recursive_listing_is_text_and_untouched(tmp_path: Path) -> None:
    """A recursive listing sniffs as plain text, which is not allowed by default (and the search
    compressor would not trim bare paths anyway)."""
    ev = _bash(tmp_path, "ls -R", big_listing())
    ctype = detect(big_listing(), ev.tool)
    assert ctype == "text"
    assert compression.gated(ev, ctype, _cfg(tmp_path)) == compression.GATE_CTYPE
    _untouched(tmp_path, ev)


def test_plain_text_from_an_arbitrary_command_untouched(tmp_path: Path) -> None:
    _untouched(tmp_path, _bash(tmp_path, "./tool --dump", plain_text()))


@pytest.mark.parametrize("tool", ["Agent", "WebFetch"])
def test_plain_text_from_other_tools_untouched(tmp_path: Path, tool: str) -> None:
    _untouched(tmp_path, _event(tmp_path, _tool(tool, {"prompt": "x"}), plain_text()))


ALL_TYPES = [
    "json",
    "diff",
    "stacktrace",
    "test_report",
    "compiler",
    "build_log",
    "search",
    "pplx",
    "log",
    "table",
    "text",
]


def _agent_log_report(lines: int = 400) -> str:
    """The review's probe: a subagent report made of log lines (sniffed as ``log``)."""
    return "\n".join(
        f"2026-11-28T10:{i // 60 % 60:02d}:{i % 60:02d}Z INFO worker-{i % 7} processed batch {i}"
        for i in range(lines)
    )


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("make", [_agent_log_report, big_pytest, big_json, big_diff])
def test_agent_results_are_never_trimmed(tmp_path: Path, tool: str, make: Any) -> None:
    """v1 review I1: a subagent's report is the main agent's only view of its work, so no
    generic compressor trims it, whatever it looks like (log, test report, JSON, diff), even with
    every content type allowed and the static policy (which always trims an allowed type)."""
    ev = _event(tmp_path, _tool(tool, {"prompt": "x"}), make())
    _untouched(tmp_path, ev)
    wide = _cfg(tmp_path)
    wide = Config(
        data={**wide.data, "compression": {**wide.data["compression"], "allow_ctypes": ALL_TYPES}}
    )
    _untouched(tmp_path, ev, wide)


def test_grep_tool_search_results_allowed(tmp_path: Path) -> None:
    ev = _event(tmp_path, _tool("Grep", {"pattern": "x"}), "x")
    assert compression.gated(ev, "search", _cfg(tmp_path)) is None


class _Recorder:
    """A learning policy stub that records every consultation."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        self.calls += 1
        return Decision(f"d-{self.calls}", point.name, "standard", 0.5, "b")

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        pass


def test_learning_policy_never_consulted_for_gated_outputs(tmp_path: Path) -> None:
    pol = _Recorder()
    cfg = _cfg(tmp_path)
    env = _env(tmp_path)
    for ev in (
        _read(tmp_path, python_source()),
        _bash(tmp_path, "cat big.py", python_source()),
        _bash(tmp_path, "./tool", plain_text()),
    ):
        assert compression.handle_post_tool(ev, pol, cfg, env=env).is_noop()
    assert pol.calls == 0 and _decisions(tmp_path) == []
    compression.handle_post_tool(_bash(tmp_path, "pytest", big_pytest()), pol, cfg, env=env)
    assert pol.calls == 1


# --------------------------------------------------------------------------- gated()


def test_gated_is_pure_and_labels_reasons(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    read = _read(tmp_path, "x")
    assert compression.gated(read, "diff", cfg) == compression.GATE_FILE_CONTENT == "file_content"
    assert compression.gated(_bash(tmp_path, "cat a", "x"), "json", cfg) == "file_content"
    assert compression.gated(_bash(tmp_path, "./x", "x"), "text", cfg) == "ctype_not_allowed"
    assert compression.gated(_bash(tmp_path, "./x", "x"), "table", cfg) == "ctype_not_allowed"
    assert compression.gated(_bash(tmp_path, "./x", "x"), "stacktrace", cfg) == (
        compression.GATE_CTYPE
    )
    for ctype in compression.DEFAULT_ALLOW_CTYPES:
        assert compression.gated(_bash(tmp_path, "./x", "x"), ctype, cfg) is None
    assert set(compression.DEFAULT_ALLOW_CTYPES) == {
        "diff",
        "search",
        "test_report",
        "compiler",
        "build_log",
        "json",
        "pplx",
        "log",
    }
    assert _decisions(tmp_path) == [] and _outputs(tmp_path) == []


def test_defaults_toml_matches_the_gate_defaults() -> None:
    comp = config_mod.packaged_defaults()["compression"]
    assert comp["allow_ctypes"] == list(compression.DEFAULT_ALLOW_CTYPES)
    assert comp["trim_file_contents"] is False


# --------------------------------------------------------------------------- config


def test_user_opt_in_widens(tmp_path: Path) -> None:
    _user_file(
        tmp_path,
        '[compression]\nallow_ctypes = ["text", "test_report"]\ntrim_file_contents = true\n',
    )
    _trimmed(tmp_path, _bash(tmp_path, "cat big.txt", plain_text()))
    cfg = _cfg(tmp_path)
    assert compression.gated(_read(tmp_path, "x"), "text", cfg) is None
    assert compression.gated(_bash(tmp_path, "./x", "x"), "diff", cfg) == "ctype_not_allowed"


@pytest.mark.env_drop_expected
def test_user_file_widens_env_cannot(tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()
    assert _cfg(tmp_path / "e", COMPRESSION__ALLOW_CTYPES="text,diff").get(
        "compression.allow_ctypes"
    ) == _cfg(tmp_path / "e").get("compression.allow_ctypes")  # env may only narrow
    tmp_path = tmp_path / "u"
    to_user_config(_env(tmp_path, COMPRESSION__ALLOW_CTYPES="text,diff"))
    cfg = _cfg(tmp_path)
    _trimmed(tmp_path, _bash(tmp_path, "./tool", plain_text()), cfg)
    assert compression.gated(_read(tmp_path, "x"), "text", cfg) == "file_content"
    (tmp_path / "home" / "config.toml").unlink()
    to_user_config(_env(tmp_path, COMPRESSION__TRIM_FILE_CONTENTS="true"))
    cfg = _cfg(tmp_path)
    assert compression.gated(_read(tmp_path, "x"), "diff", cfg) is None
    assert compression.gated(_read(tmp_path, "x"), "text", cfg) == "ctype_not_allowed"


def test_project_may_narrow_allow_ctypes(tmp_path: Path) -> None:
    _project_file(tmp_path, '[compression]\nallow_ctypes = ["diff"]\n')
    cfg = _cfg(tmp_path)
    assert cfg.get("compression.allow_ctypes") == ["diff"]
    _untouched(tmp_path, _bash(tmp_path, "pytest -v", big_pytest()), cfg)


def test_project_cannot_widen_allow_ctypes(tmp_path: Path) -> None:
    _project_file(tmp_path, '[compression]\nallow_ctypes = ["diff", "text"]\n')
    cfg = _cfg(tmp_path)
    assert cfg.get("compression.allow_ctypes") == list(compression.DEFAULT_ALLOW_CTYPES)
    _untouched(tmp_path, _bash(tmp_path, "./tool", plain_text()), cfg)
    errors = (tmp_path / "home" / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "compression.allow_ctypes" in errors


def test_project_narrows_against_the_users_wider_list(tmp_path: Path) -> None:
    _user_file(tmp_path, '[compression]\nallow_ctypes = ["text", "diff", "log"]\n')
    _project_file(tmp_path, '[compression]\nallow_ctypes = ["text"]\n')
    assert _cfg(tmp_path).get("compression.allow_ctypes") == ["text"]


def test_project_cannot_turn_on_file_trimming(tmp_path: Path) -> None:
    _project_file(tmp_path, "[compression]\ntrim_file_contents = true\n")
    cfg = _cfg(tmp_path)
    assert cfg.get("compression.trim_file_contents") is False
    _untouched(tmp_path, _bash(tmp_path, "tail -n 2000 app.log", big_log()), cfg)


def test_project_may_turn_off_the_users_file_trimming(tmp_path: Path) -> None:
    _user_file(tmp_path, "[compression]\ntrim_file_contents = true\n")
    _project_file(tmp_path, "[compression]\ntrim_file_contents = false\n")
    assert _cfg(tmp_path).get("compression.trim_file_contents") is False


def test_env_beats_a_narrowing_project(tmp_path: Path) -> None:
    _project_file(tmp_path, '[compression]\nallow_ctypes = ["diff"]\n')
    cfg = _cfg(tmp_path, COMPRESSION__ALLOW_CTYPES="test_report")
    _trimmed(tmp_path, _bash(tmp_path, "pytest -v", big_pytest()), cfg)


def test_project_policy_kinds() -> None:
    assert config_mod.policy_for(("compression", "allow_ctypes")) == "subset"
    assert config_mod.policy_for(("compression", "trim_file_contents")) == "false_only"


@pytest.mark.parametrize(
    ("value", "base", "ok", "merged"),
    [
        (["a"], ["a", "b"], True, ["a"]),
        ([], ["a", "b"], True, []),
        (["b", "a"], ["a", "b"], True, ["a", "b"]),
        (["a", "c"], ["a", "b"], False, None),
        ("a", ["a", "b"], False, None),  # a project file gives a list, never a string
        (["a"], "a,b", True, ["a"]),  # trusted env form: comma-separated
        ([1], ["a"], False, None),
    ],
)
def test_subset_kind(value: Any, base: Any, ok: bool, merged: Any) -> None:
    accepted, out = config_mod._accept("subset", None, value, base)
    assert accepted is ok
    if ok:
        assert out == merged


@pytest.mark.parametrize("raw", ["bogus", 3, ["text", 3]])
def test_invalid_allow_ctypes_falls_back_to_defaults(tmp_path: Path, raw: Any) -> None:
    cfg = Config(data={"compression": {"allow_ctypes": raw}})
    allowed = compression.allowed_ctypes(cfg)
    if raw == "bogus":  # a string is the env form: one (unknown) type, nothing else allowed
        assert allowed == frozenset({"bogus"})
    elif isinstance(raw, list):
        assert allowed == frozenset({"text"})  # non-strings dropped
    else:
        assert allowed == frozenset(compression.DEFAULT_ALLOW_CTYPES)


# --------------------------------------------------------------------------- fix round 1


@pytest.mark.parametrize(
    "cmd",
    [
        "wc -l app.log && tail -n 3000 app.log",
        'for f in *.log; do cat "$f"; done',
        "find . -name '*.log' -exec cat {} \\;",
        "python3 -c \"print(open('app.log').read())\"",
    ],
)
def test_chained_and_looped_dumps_of_an_allowed_type_untouched(tmp_path: Path, cmd: str) -> None:
    ev = _bash(tmp_path, cmd, big_log())
    assert detect(big_log(), ev.tool) == "log"
    _untouched(tmp_path, ev)


@pytest.mark.parametrize(
    "name",
    [
        "mcp__fs__read_file",
        "mcp__files__ReadFile",
        "mcp__repo__get_file_contents",
        "mcp__x__file_read",
        "mcp__x__read_text_file",
        "mcp__x__cat_file",
        "mcp__editor__view_file",
        "mcp__editor__open_file",
        "read_file",
        "mcp__docs__fetch_document",
        "mcp__notes__read_page",
        "NotebookRead",
    ],
)
def test_file_reading_tools_are_file_content(tmp_path: Path, name: str) -> None:
    ev = _event(tmp_path, _tool(name, {"path": "a.log"}), big_log())
    assert compression.gated(ev, "log", _cfg(tmp_path)) == "file_content"
    _untouched(tmp_path, ev)


def test_other_mcp_tools_are_not_file_content(tmp_path: Path) -> None:
    ev = _event(tmp_path, _tool("mcp__api__list_users", {}), big_json())
    assert compression.gated(ev, "json", _cfg(tmp_path)) is None


@pytest.mark.parametrize(
    "cmd",
    [
        'echo "$(cat app.log)"',
        "wc -c < app.log",
        "docker exec web cat /app/app.log",
        "kubectl exec pod -- tail -n 3000 /app/app.log",
        "gunzip -c app.log.gz",
    ],
)
def test_substitution_redirect_and_wrapped_dumps_untouched(tmp_path: Path, cmd: str) -> None:
    _untouched(tmp_path, _bash(tmp_path, cmd, big_log()))


@pytest.mark.parametrize("cmd", ["Get-Content app.log", "gc app.log", "type app.log"])
def test_powershell_tool_dumps_are_file_content(tmp_path: Path, cmd: str) -> None:
    ev = _event(tmp_path, _tool("PowerShell", {"command": cmd}), big_log())
    assert compression.gated(ev, "log", _cfg(tmp_path)) == "file_content"
