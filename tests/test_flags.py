"""One truthy parser for every boolean-ish config read (``config.flag_on`` / ``flag_off``)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from token_sparen import extensions
from token_sparen.config import Config, flag, flag_off, flag_on, load_config
from token_sparen.learn.engine import Engine
from token_sparen.learn.hierarchy import StoreChain
from token_sparen.learn.policy import COMPRESSION, SUBAGENT_BRIEF
from token_sparen.mechanisms import compression, governor, guards, read_dedupe

ON = [True, 1, 1.0, "1", "true", "TRUE", "yes", " Yes ", "on", "ON"]
OFF = [False, 0, 0.0, "0", "false", "False", "no", " NO ", "off", "Off"]
NEITHER = [None, 2, -1, 0.5, "", "maybe", "y", "n", [], {}]


@pytest.mark.parametrize("value", ON)
def test_flag_on_spellings(value: object) -> None:
    assert flag_on(value) and not flag_off(value)
    assert flag(value, False) is True


@pytest.mark.parametrize("value", OFF)
def test_flag_off_spellings(value: object) -> None:
    assert flag_off(value) and not flag_on(value)
    assert flag(value, True) is False


@pytest.mark.parametrize("value", NEITHER)
def test_unrecognised_values_keep_the_default(value: object) -> None:
    assert not flag_on(value) and not flag_off(value)
    assert flag(value, True) is True and flag(value, False) is False


def _cfg(section: str, key: str, value: Any) -> Config:
    return Config(data={section: {key: value}})


# (reader, section, key, default when unset)
READERS: list[tuple[Callable[[Config], bool], str, str, bool]] = [
    (guards._enabled, "guards", "enabled", True),
    (read_dedupe._enabled, "read_dedupe", "enabled", True),
    (governor._enabled, "governor", "enabled", True),
    (governor._hard_stop, "governor", "hard_stop", False),
    (compression.trims_file_contents, "compression", "trim_file_contents", False),
]


@pytest.mark.parametrize(("reader", "section", "key", "default"), READERS)
def test_mechanism_switches_use_the_shared_parser(
    reader: Callable[[Config], bool], section: str, key: str, default: bool
) -> None:
    for value in ON:
        assert reader(_cfg(section, key, value)) is True, (key, value)
    for value in OFF:
        assert reader(_cfg(section, key, value)) is False, (key, value)
    for value in NEITHER:
        assert reader(_cfg(section, key, value)) is default, (key, value)
    assert reader(Config()) is default


def _engine(tmp_path: Path, learn: dict[str, Any]) -> Engine:
    import random

    home = tmp_path / "home"
    stores = StoreChain("0" * 16, home, None, params={"learn": learn})
    cfg = Config(data={"learn": learn})
    return Engine(stores, cfg, random.Random(0), env={"TOKEN_SPAREN_HOME": str(home)})


@pytest.mark.parametrize("value", ON)
def test_hold_accepts_every_on_spelling(tmp_path: Path, value: object) -> None:
    assert _engine(tmp_path, {"hold": {"compression": value}}).held(COMPRESSION)


@pytest.mark.parametrize("value", OFF)
def test_hold_released_by_every_off_spelling(tmp_path: Path, value: object) -> None:
    # subagent_brief is held by the packaged defaults; any off spelling releases it
    assert not _engine(tmp_path, {"hold": {"subagent_brief": value}}).held(SUBAGENT_BRIEF)


@pytest.mark.parametrize("value", ["maybe", 2, None])
def test_unrecognised_hold_value_falls_back_to_the_default(tmp_path: Path, value: object) -> None:
    eng = _engine(tmp_path, {"hold": {"subagent_brief": value, "compression": value}})
    assert eng.held(SUBAGENT_BRIEF) and not eng.held(COMPRESSION)


@pytest.mark.parametrize("raw", ["1", "yes", "on", "TRUE"])
def test_env_hold_spellings_reach_the_engine(tmp_path: Path, raw: str) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "h"), "TOKEN_SPAREN_LEARN__HOLD__COMPRESSION": raw}
    cfg = load_config(tmp_path, env)
    eng = _engine(tmp_path, cfg.data["learn"])
    assert eng.held(COMPRESSION)


@pytest.mark.parametrize("raw", ["yes", "on", "YES"])
def test_kill_switch_accepts_every_on_spelling(tmp_path: Path, raw: str) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "h"), "TOKEN_SPAREN_DISABLE": raw}
    assert load_config(tmp_path, env).disabled


@pytest.mark.parametrize("raw", ["no", "off", "0", "false", ""])
def test_kill_switch_off_spellings(tmp_path: Path, raw: str) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "h"), "TOKEN_SPAREN_DISABLE": raw}
    assert not load_config(tmp_path, env).disabled


@pytest.mark.parametrize("value", ["yes", "on", 1, True])
def test_optin_accepts_every_on_spelling(tmp_path: Path, value: object) -> None:
    home = tmp_path / "home"
    home.mkdir()
    text = f"[learn.optin]\nx = {value!r}\n".replace("True", "true").replace("'", '"')
    (home / "config.toml").write_text(text, encoding="utf-8")
    eng = _engine(tmp_path, {"optin": {"x": value}})
    assert eng._user_optin("x")


@pytest.mark.parametrize("value", ["yes", "on", "1", 1, True])
def test_extension_override_accepts_every_on_spelling(value: object) -> None:
    assert extensions._truthy(value)


def test_statusline_flags_use_the_shared_parser() -> None:
    from token_sparen.adapters.claude import statusline

    assert statusline._ascii(_cfg("statusline", "ascii", "yes"))
    assert not statusline._ascii(_cfg("statusline", "ascii", "maybe"))
    assert not statusline._use_colour(_cfg("statusline", "color", "off"), {})
    assert statusline._use_colour(_cfg("statusline", "color", "maybe"), {})
    assert not statusline._use_colour(Config(), {"NO_COLOR": "1"})


@pytest.mark.parametrize("value", ["no", "off", "0", 0])
def test_boundary_and_subagent_switch_off_in_any_spelling(tmp_path: Path, value: object) -> None:
    from token_sparen.mechanisms import boundary, subagent

    assert not boundary._enabled(_cfg("boundary", "enabled", value))
    assert not subagent._enabled(_cfg("subagent", "enabled", value))
    assert boundary._enabled(Config()) and subagent._enabled(Config())
