"""A project file may only tighten learning safety parameters (Task 23 review round 1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from token_sparen.config import flag_off, load_config, safety_param


def _setup(tmp_path: Path, *, user: str = "", project: str = "") -> tuple[Path, dict[str, str]]:
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)
    if user:
        (home / "config.toml").write_text(user, encoding="utf-8")
    if project:
        (proj / "token-sparen.toml").write_text(project, encoding="utf-8")
    return proj, {"TOKEN_SPAREN_HOME": str(home)}


def _errors(tmp_path: Path) -> list[str]:
    path = tmp_path / "home" / "logs" / "errors.jsonl"
    if not path.exists():
        return []
    return [json.loads(x)["error"] for x in path.read_text(encoding="utf-8").splitlines()]


def test_project_cannot_release_a_hold(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[learn.hold]\nsubagent_brief = false\n")
    for _ in range(3):
        cfg = load_config(proj, env)
        assert cfg.get("learn.hold.subagent_brief") is True
    logged = [e for e in _errors(tmp_path) if "learn.hold.subagent_brief" in e]
    assert len(logged) == 1  # once, not once per hook


@pytest.mark.parametrize("where", ["user", "env", "session"])
def test_user_env_or_session_may_release_a_hold(tmp_path: Path, where: str) -> None:
    user = "[learn.hold]\nsubagent_brief = false\n" if where == "user" else ""
    proj, env = _setup(tmp_path, user=user, project="[learn.hold]\nsubagent_brief = true\n")
    session: dict[str, Any] | None = None
    if where == "env":
        env["TOKEN_SPAREN_LEARN__HOLD__SUBAGENT_BRIEF"] = "false"
    if where == "session":
        session = {"config_overrides": {"learn": {"hold": {"subagent_brief": False}}}}
    if where == "user":  # the project's true still tightens the user's false
        assert load_config(proj, env).get("learn.hold.subagent_brief") is True
        (tmp_path / "proj" / "token-sparen.toml").unlink()
    cfg = load_config(proj, env, session=session)
    assert cfg.get("learn.hold.subagent_brief") is False


def test_project_hold_true_on_a_new_point_is_honoured(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[learn.hold]\ncompression = true\n")
    assert load_config(proj, env).get("learn.hold.compression") is True


@pytest.mark.parametrize(
    ("key", "toml", "tight", "loose"),
    [
        ("learn.tau.medium", "[learn.tau]\nmedium = {}\n", 0.01, 0.2),
        ("learn.floor.medium", "[learn.floor]\nmedium = {}\n", 0.2, 0.0),
        ("learn.breaker_lcb_q", "[learn]\nbreaker_lcb_q = {}\n", 0.01, 0.00001),
        ("learn.breaker_min_obs", "[learn]\nbreaker_min_obs = {}\n", 10, 40),
        ("learn.breaker_every", "[learn]\nbreaker_every = {}\n", 10, 100),
        ("learn.breaker_sessions", "[learn]\nbreaker_sessions = {}\n", 5, 1),
        ("learn.breaker_reentry_scale", "[learn]\nbreaker_reentry_scale = {}\n", 0.5, 0.1),
        ("learn.ladder_obs", "[learn]\nladder_obs = {}\n", 40, 2),
    ],
)
def test_project_numbers_only_tighten(
    tmp_path: Path, key: str, toml: str, tight: float, loose: float
) -> None:
    proj, env = _setup(tmp_path / "a", project=toml.format(tight))
    assert load_config(proj, env).get(key) == tight
    proj, env = _setup(tmp_path / "b", project=toml.format(loose))
    default = load_config(tmp_path / "nowhere", env).get(key)
    assert load_config(proj, env).get(key) == default != loose


def test_project_cannot_change_the_breaker_window(tmp_path: Path) -> None:
    for value in (10, 500):
        proj, env = _setup(tmp_path / str(value), project=f"[learn]\nbreaker_window = {value}\n")
        assert load_config(proj, env).get("learn.breaker_window") == 50


def test_env_may_loosen(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[learn.tau]\nmedium = 0.5\n")
    env["TOKEN_SPAREN_LEARN__TAU__MEDIUM"] = "0.2"
    assert load_config(proj, env).get("learn.tau.medium") == 0.2


def test_learning_enabled_project_may_only_disable(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path / "a", project="[learn]\nenabled = false\n")
    assert load_config(proj, env).get("learn.enabled") is False
    proj, env = _setup(
        tmp_path / "b", user="[learn]\nenabled = false\n", project="[learn]\nenabled = true\n"
    )
    assert flag_off(load_config(proj, env).get("learn.enabled"))


def test_project_cannot_opt_in_or_allow_overrides(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path,
        project="[learn.optin]\ncompression = true\n[extensions]\nallow_override = true\n",
    )
    cfg = load_config(proj, env)
    assert not cfg.get("learn.optin.compression")
    assert cfg.get("extensions.allow_override") is False


def test_malformed_project_value_is_ignored(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project='[learn.tau]\nmedium = "lots"\n')
    assert load_config(proj, env).get("learn.tau.medium") == 0.05


def test_safety_param_reads_the_resolved_value(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[learn.tau]\nmedium = 0.5\nlow = 0.01\n")
    cfg = load_config(proj, env)
    assert safety_param(cfg, "learn.tau.medium") == 0.05
    assert safety_param(cfg, "learn.tau.low") == 0.01


@pytest.mark.parametrize("value", [False, 0, "0", "no", "false", "off", "FALSE", " No "])
def test_flag_off_values(value: object) -> None:
    assert flag_off(value)


@pytest.mark.parametrize("value", [True, 1, "1", "yes", "true", None, "on", 2.5])
def test_flag_on_values(value: object) -> None:
    assert not flag_off(value)
