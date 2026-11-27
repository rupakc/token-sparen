"""Arms registry and per-arm state semantics (M5 Task 4). No install, no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bench import arms as am
from token_sparen.mechanisms.outputstyle import DIRECTIVES

EXPECTED = {
    "baseline": "baseline",
    "concise": "prompt",
    "token-sparen": "plugin",
    "ts-style": "plugin",
    "ts-cold": "plugin",
    "rtk": "hook",
    "headroom": "wrapper",
    "caveman": "plugin",
    "context-mode": "plugin",
    "tokeneconomizer": "plugin",
}
COMMON_EXCLUDES = {
    ".token-sparen/",
    ".claude/",
    "CLAUDE.md",
    "RTK.md",
    ".rtk/",
    ".context-mode/",
    ".tokeneconomizer/",
    ".headroom/",
}


def test_arms_registry_validates() -> None:
    arms = am.load_arms()
    assert {name: arm.kind for name, arm in arms.items()} == EXPECTED
    for name, arm in arms.items():
        assert arm.name == name
        assert COMMON_EXCLUDES <= set(arm.patch_excludes)
        assert arm.liveness in am.LIVENESS_KINDS
        assert (arm.state == "stream") == bool(arm.state_paths)
        assert arm.pin is None or arm.pin  # no empty pin
        for key in arm.env:
            assert key not in am.HARNESS_ENV
    ts = [a for a in arms.values() if a.uses_frozen_plugin]
    assert {a.name for a in ts} == {"token-sparen", "ts-style", "ts-cold"}
    for arm in ts:
        assert arm.env["TOKEN_SPAREN_PYTHON"] == "/opt/bench/bin/python3.12"
        assert arm.liveness == "token_sparen"
    assert arms["ts-style"].ts_config == {"output_style": {"enabled": True}}
    assert arms["token-sparen"].ts_config == {}
    assert arms["headroom"].wrapper == ("headroom", "wrap", "claude", "--")
    assert arms["rtk"].install == ("rtk init -g",)
    assert arms["caveman"].install[-1] == "claude plugin install caveman@caveman"
    assert arms["context-mode"].install[-1] == "claude plugin install context-mode@context-mode"
    assert arms["tokeneconomizer"].install[-1].endswith("tokeneconomizer@tokeneconomizer")
    for name in ("rtk", "headroom", "caveman", "context-mode", "tokeneconomizer"):
        assert arms[name].pin and arms[name].source, name  # competitors are pinned


def test_state_semantics_per_arm() -> None:
    arms = am.load_arms()
    assert arms["token-sparen"].state_paths == (".token-sparen",)
    assert arms["ts-style"].state_paths == (".token-sparen",)
    assert arms["ts-cold"].state == "none"
    assert arms["tokeneconomizer"].state_paths == (".tokeneconomizer", ".claude/projects")
    stream = {n for n, a in arms.items() if a.state == "stream"}
    assert stream == {"token-sparen", "ts-style", "tokeneconomizer"}


def test_concise_uses_directive_text() -> None:
    arms = am.load_arms()
    assert arms["concise"].append_system_prompt == DIRECTIVES["concise"]
    assert all(a.append_system_prompt is None for n, a in arms.items() if n != "concise")


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """
[common]
patch_excludes = [".claude/"]

[arms.base]
kind = "baseline"
liveness = "none"
"""


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('[arms.x]\nkind = "nope"\nliveness = "none"\n', "kind"),
        ('[arms.x]\nkind = "baseline"\nliveness = "magic"\n', "liveness"),
        ('[arms.x]\nkind = "baseline"\nliveness = "none"\nstate = "stream"\n', "state_paths"),
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\nstate_paths = [".a"]\n',
            "state_paths",
        ),
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\nstate = "stream"\n'
            'state_paths = ["../up"]\n',
            "state path",
        ),
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\nstate = "stream"\n'
            'state_paths = ["/abs"]\n',
            "state path",
        ),
        ('[arms.x]\nkind = "wrapper"\nliveness = "none"\n', "wrapper"),
        ('[arms.x]\nkind = "prompt"\nliveness = "none"\n', "append_system_prompt"),
        ('[arms.x]\nkind = "baseline"\nliveness = "none"\nuses_frozen_plugin = true\n', "plugin"),
        ('[arms.x]\nkind = "baseline"\nliveness = "none"\nbogus = 1\n', "unknown"),
        ('[arms.X_bad]\nkind = "baseline"\nliveness = "none"\n', "name"),
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\n'
            '[arms.x.env]\nCLAUDE_CODE_OAUTH_TOKEN = "t"\n',
            "harness",
        ),
        ('[arms.x]\nkind = "baseline"\nliveness = "none"\n[arms.x.env]\nA = 1\n', "env"),
        *[
            (f'[arms.x]\nkind = "baseline"\nliveness = "none"\n[arms.x.env]\n{k} = "v"\n', "env")
            for k in (
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_BEDROCK_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK",
                "PATH",
                "PYTHONPATH",
                "LD_PRELOAD",
                "DYLD_INSERT_LIBRARIES",
                "GIT_CONFIG_GLOBAL",
                "NODE_OPTIONS",
            )
        ],
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\n'
            '[arms.x.env]\nTOKEN_SPAREN_PYTHON = "/usr/bin/python3"\n',
            "/opt/bench",
        ),
        (
            '[arms.x]\nkind = "baseline"\nliveness = "none"\n[arms.x.ts_config.a]\nb = 1\n',
            "ts_config",
        ),
        ('[arms.x]\nkind = "baseline"\nliveness = "none"\npin = ""\n', "pin"),
    ],
)
def test_registry_rejects_bad_arms(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(am.ArmsError, match=message):
        am.load_arms(_write(tmp_path / "arms.toml", GOOD + extra))


def test_registry_rejects_bad_files(tmp_path: Path) -> None:
    with pytest.raises(am.ArmsError):
        am.load_arms(_write(tmp_path / "a.toml", "[arms\n"))
    with pytest.raises(am.ArmsError):
        am.load_arms(_write(tmp_path / "b.toml", "[common]\n"))
    with pytest.raises(am.ArmsError):
        am.load_arms(tmp_path / "absent.toml")
    good = am.load_arms(_write(tmp_path / "c.toml", GOOD))
    assert good["base"].patch_excludes == (".claude/",)


def _home(root: Path) -> Path:
    files = {
        ".token-sparen/projects/k/state.json": "s",
        ".tokeneconomizer/db.json": "t",
        ".claude/projects/p/session.jsonl": "x",
        ".claude/settings.json": "{}",
        ".claude.json": "{}",
        ".context-mode/cache": "c",
        "notes.txt": "n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_only_state_paths_persist(tmp_path: Path) -> None:
    arms = am.load_arms()
    home = _home(tmp_path / "home")
    expected = {
        "token-sparen": {".token-sparen/projects/k/state.json"},
        "ts-style": {".token-sparen/projects/k/state.json"},
        "tokeneconomizer": {".tokeneconomizer/db.json", ".claude/projects/p/session.jsonl"},
    }
    for name, arm in arms.items():
        dest = tmp_path / "state" / name
        copied = am.carry_state(arm, home, dest)
        want = expected.get(name, set())
        assert _tree(dest) == want, name
        assert set(copied) == {p for p in arm.state_paths if (home / p).exists()}


def test_carry_state_skips_symlinks_and_replaces_dest(tmp_path: Path) -> None:
    arm = am.load_arms()["token-sparen"]
    home = _home(tmp_path / "home")
    outside = _write(tmp_path / "secret.txt", "secret")
    if os.name != "nt":
        (home / ".token-sparen" / "link").symlink_to(outside)
    dest = tmp_path / "dest"
    dest.mkdir()
    _write(dest / "stale.txt", "old")
    am.carry_state(arm, home, dest)
    assert _tree(dest) == {".token-sparen/projects/k/state.json"}
    assert not (dest / ".token-sparen" / "link").exists()
    assert not any(p.is_symlink() for p in dest.rglob("*"))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_carry_state_refuses_symlinked_state_path_or_parent(tmp_path: Path) -> None:
    """Review I1: a run (agent or competitor hook) can plant `.claude -> <real home>/.claude`
    in its HOME; carry_state must never copy through it."""
    real = tmp_path / "realhome"
    (real / ".claude" / "projects" / "p").mkdir(parents=True)
    _write(real / ".claude" / "projects" / "p" / "private.jsonl", "owner transcript")
    (real / ".token-sparen").mkdir()
    arms = am.load_arms()
    # symlinked parent of a state path
    home = tmp_path / "home"
    (home / ".tokeneconomizer").mkdir(parents=True)
    (home / ".claude").symlink_to(real / ".claude")
    with pytest.raises(am.StateError, match="symlink"):
        am.carry_state(arms["tokeneconomizer"], home, tmp_path / "d1")
    assert not list((tmp_path / "d1").rglob("private.jsonl"))
    # the state path itself
    home2 = tmp_path / "home2"
    home2.mkdir()
    (home2 / ".token-sparen").symlink_to(real / ".token-sparen")
    with pytest.raises(am.StateError, match="symlink"):
        am.carry_state(arms["token-sparen"], home2, tmp_path / "d2")
    # the run home itself is a symlink
    with pytest.raises(am.StateError, match="symlink"):
        alias = tmp_path / "alias"
        alias.symlink_to(home2)
        am.carry_state(arms["token-sparen"], alias, tmp_path / "d3")
    # a symlinked directory deep inside the copied tree is skipped, not followed
    home3 = _home(tmp_path / "home3")
    (home3 / ".token-sparen" / "projects" / "evil").symlink_to(real / ".claude")
    am.carry_state(arms["token-sparen"], home3, tmp_path / "d4")
    assert _tree(tmp_path / "d4") == {".token-sparen/projects/k/state.json"}
