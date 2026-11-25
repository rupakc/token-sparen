import json
from pathlib import Path

import pytest

from token_sparen.config import load_config


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(home), **extra}


def test_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "proj", _env(tmp_path / "home"))
    assert cfg.get("compression.min_tokens") == 2000
    assert cfg.get("compression.min_saving_ratio") == 0.15
    assert cfg.get("nope.missing") is None
    assert cfg.get("nope.missing", 7) == 7
    assert cfg.get("compression")["min_tokens"] == 2000


def test_config_precedence(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    (home / "config.toml").write_text("[compression]\nmin_tokens = 3000\n")
    assert load_config(proj, _env(home)).get("compression.min_tokens") == 3000
    (proj / "token-sparen.toml").write_text("[compression]\nmin_tokens = 4000\n")
    env = _env(home, TOKEN_SPAREN_COMPRESSION__MIN_TOKENS="5000")
    assert load_config(proj, env).get("compression.min_tokens") == 5000
    assert load_config(proj, _env(home)).get("compression.min_tokens") == 4000
    # sibling keys survive a partial override
    assert load_config(proj, _env(home)).get("compression.min_saving_ratio") == 0.15


def test_kill_switch(tmp_path: Path) -> None:
    home = tmp_path / "h"
    assert load_config(tmp_path, _env(home, TOKEN_SPAREN_DISABLE="1")).disabled
    assert load_config(tmp_path, _env(home, TOKEN_SPAREN_DISABLE="true")).disabled
    assert not load_config(tmp_path, _env(home, TOKEN_SPAREN_DISABLE="0")).disabled
    assert not load_config(tmp_path, _env(home)).disabled


def test_bad_toml_falls_back(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    proj.mkdir()
    (proj / "token-sparen.toml").write_text("[compression\nmin_tokens = =")
    cfg = load_config(proj, _env(home))
    assert cfg.get("compression.min_tokens") == 2000
    lines = (home / "logs" / "errors.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["where"] == "config"


def test_env_value_parsing(tmp_path: Path) -> None:
    env = _env(
        tmp_path / "h",
        TOKEN_SPAREN_A__INT="7",
        TOKEN_SPAREN_A__FLOAT="0.5",
        TOKEN_SPAREN_A__T="true",
        TOKEN_SPAREN_A__F="False",
        TOKEN_SPAREN_A__ONE="1",
        TOKEN_SPAREN_A__STR="hello",
        TOKEN_SPAREN_A__B__DEEP="9",
        TOKEN_SPAREN_NOSECTION="x",
    )
    cfg = load_config(tmp_path, env)
    assert cfg.get("a.int") == 7
    assert cfg.get("a.float") == 0.5
    assert cfg.get("a.t") is True
    assert cfg.get("a.f") is False
    assert cfg.get("a.one") == 1
    assert cfg.get("a.str") == "hello"
    assert cfg.get("a.b.deep") == 9


def test_env_overrides_non_table(tmp_path: Path) -> None:
    env = _env(tmp_path / "h", TOKEN_SPAREN_COMPRESSION__MIN_TOKENS__X="1")
    assert load_config(tmp_path, env).get("compression.min_tokens.x") == 1


def test_logging_failure_swallowed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.write_text("a file, not a dir")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "token-sparen.toml").write_text("= bad")
    cfg = load_config(proj, _env(home))
    assert cfg.get("compression.min_tokens") == 2000


def test_default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(tmp_path / "p", {})
    assert cfg.get("compression.min_tokens") == 2000
