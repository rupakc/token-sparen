"""Deny-by-default project layer (Task 23 review rounds 1-3): a cloned repository's config may
only make token-sparen intervene less or keep more, never trim more, recover less or block more."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from token_sparen.config import (
    PROJECT_POLICY,
    _is_num,
    flag_off,
    load_config,
    packaged_defaults,
    policy_for,
)


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


def test_learning_enabled_is_user_only(tmp_path: Path) -> None:
    """A project switching learning off would fall back to the static arms (standard
    compression), ignoring the user's holds, tau and floor: more trimming. Use a hold instead."""
    proj, env = _setup(tmp_path / "a", project="[learn]\nenabled = false\n")
    assert load_config(proj, env).get("learn.enabled") is True
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


STRICT_USER = """
[learn]
enabled = false
ladder_obs = 40
regret_window_turns = 9
gamma = 0.9
kappa_regret = 30
pretrim_min_decisions = 9
breaker_reentry_scale = 0.5
[learn.tau]
medium = 0.02
[learn.floor]
medium = 0.3
[learn.hold]
compression = true
[learn.defaults.compression.light]
regret = 0.2
"""


@pytest.mark.parametrize(
    "project",
    [
        "learn = 1\n",
        'learn = "x"\n',
        "[learn]\ntau = 0\n",
        "[learn]\nfloor = 0\n",
        "[learn]\nhold = 0\n",
        "[learn]\ndefaults = 0\n",
        "[learn.defaults.compression.light]\nregret = 0.0\n",
        "[learn.defaults]\ncompression = 1\n",
        "[learn]\nregret_window_turns = 1\n",
        "[learn]\ngamma = 0.5\n",
        "[learn]\nkappa_regret = 2\nkappa_default = 1\n",
        "[learn]\npretrim_min_decisions = 1\npretrim_max_ratio = 0.9\n",
        "[learn]\nbreaker_reentry_scale = 1.0\nbreaker_window = 5\n",
        "[learn]\nenabled = true\nladder_obs = 1\n",
        '[learn]\nenabled = "false"\nladder_obs = "50"\n',
        "[learn.tau]\nmedium = true\n",
        "[learn.tau.medium]\nx = 0\n",
        "[learn.hold]\ncompression = false\n",
        "[learn.backoff.compression]\nlevels = [[]]\n",
        "[learn.optin]\ncompression = true\n",
    ],
)
def test_project_cannot_loosen_or_replace_user_learning(tmp_path: Path, project: str) -> None:
    """Scalars replacing tables, unknown keys, wrong types: dropped; the user's values stand."""
    proj, env = _setup(tmp_path, user=STRICT_USER)
    want = load_config(proj, env).data["learn"]
    (proj / "token-sparen.toml").write_text(project, encoding="utf-8")
    got = load_config(proj, env).data["learn"]
    assert got == want
    assert _errors(tmp_path)  # logged
    errors = len(_errors(tmp_path))
    load_config(proj, env)
    assert len(_errors(tmp_path)) == errors  # once


def test_project_learn_scalar_keeps_other_sections(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="learn = 1\n[guards]\nenabled = false\n")
    cfg = load_config(proj, env)
    assert cfg.get("guards.enabled") is False and cfg.get("learn.enabled") is True
    assert cfg.get("learn.tau.medium") == 0.05


def test_project_tightening_still_applies_over_a_strict_user(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path,
        user=STRICT_USER,
        project="[learn]\nladder_obs = 60\n[learn.tau]\nmedium = 0.01\n[learn.hold]\n"
        "subagent_brief = true\nread_dedupe = true\n",
    )
    cfg = load_config(proj, env)
    assert cfg.get("learn.ladder_obs") == 60 and cfg.get("learn.tau.medium") == 0.01
    assert cfg.get("learn.hold.read_dedupe") is True and cfg.get("learn.enabled") is False


def test_session_may_loosen_over_a_project(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[learn.tau]\nmedium = 0.01\n")
    session = {"config_overrides": {"learn": {"tau": {"medium": 0.1}, "gamma": 0.9}}}
    cfg = load_config(proj, env, session=session)
    assert cfg.get("learn.tau.medium") == 0.1 and cfg.get("learn.gamma") == 0.9


def test_project_cannot_allow_extension_overrides_even_as_a_scalar(tmp_path: Path) -> None:
    for text in ("[extensions]\nallow_override = true\n", "extensions = 1\n"):
        proj, env = _setup(tmp_path / str(len(text)), project=text)
        assert load_config(proj, env).get("extensions.allow_override") is False


@pytest.mark.parametrize("value", [False, 0, "0", "no", "false", "off", "FALSE", " No "])
def test_flag_off_values(value: object) -> None:
    assert flag_off(value)


@pytest.mark.parametrize("value", [True, 1, "1", "yes", "true", None, "on", 2.5])
def test_flag_on_values(value: object) -> None:
    assert not flag_off(value)


# --------------------------------------------------------------------------- round 3: every key


def _default_leaves(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(node, dict) and node:
        return [leaf for k, v in node.items() for leaf in _default_leaves(v, (*path, k))]
    return [path]


def test_every_default_key_has_a_project_policy() -> None:
    """New keys cannot silently become project-loosenable: each needs an explicit entry."""
    missing = [".".join(p) for p in _default_leaves(packaged_defaults()) if policy_for(p) is None]
    assert missing == []


def test_unknown_keys_are_user_only(tmp_path: Path) -> None:
    assert policy_for(("compression", "brand_new_knob")) == "user_only"  # section catch-all
    assert policy_for(("newsection", "x")) is None  # unlisted: treated as user-only
    proj, env = _setup(tmp_path, project="[newsection]\nx = 1\n[compression]\nnew_knob = 0\n")
    cfg = load_config(proj, env)
    assert cfg.get("newsection") is None and cfg.get("compression.new_knob") is None


# key, kind-proof values: (tighter (accepted), looser (dropped), garbage (dropped))
CASES: list[tuple[str, Any, Any, Any]] = [
    ("recovery.ttl_days", 30, 0, "forever"),
    ("recovery.max_files", 500, 0, -1),
    ("recovery.max_bytes", 200_000_000, 0, 1e400),
    ("compression.levels.standard.keep_ratio", 0.5, 0.0, 7.0),
    ("compression.json.items.standard", 8, 1, "8"),
    ("compression.diff.context.standard", 3, 0, 10**300),
    ("compression.stacktrace.edge_frames.aggressive", 5, 1, None),
    ("compression.test_report.block_lines.standard", 20, 1, {}),
    ("compression.search.per_file.light", 30, 1, -5),
    ("compression.pplx.results.standard", 6, 1, 0.5),
    ("compression.log.tail", 60, 1, "x"),
    ("compression.min_saving_ratio", 0.3, 0.0, 2.0),
    ("compression.min_tokens", 5000, 2000, "big"),
    ("guards.max_read_bytes", 9_000_000, 10, -1),
    ("read_dedupe.max_turn_gap", 3, 50, -1),
    ("read_dedupe.max_entries", 50, 500, 0),  # the reader maps <= 0 to the default (200)
    ("adapter.latency_budget_ms", 200, 1000, 0),  # <= 0 -> default 400
    ("compression.save_deadline_ms", 100, 500, -1),
    ("guards.max_read_bytes", 9_000_001, 10, 0),  # <= 0 -> default
    ("read_dedupe.min_tokens", 5000, 2000, "x"),
    ("learn.tau.medium", 0.01, 0.2, 5.0),
    ("learn.floor.medium", 0.2, 0.0, 1.5),
    ("learn.ladder_obs", 40, 2, "x"),
    ("learn.breaker_min_obs", 10, 40, -3),
    ("learn.breaker_every", 10, 100, 0.0),
    ("learn.breaker_lcb_q", 0.01, 0.00001, 0.9),
    ("learn.breaker_sessions", 5, 1, "x"),
    ("learn.hold.compression", True, False, 1),
    ("guards.enabled", False, True, 0),
    ("read_dedupe.enabled", False, True, "no"),
    ("subagent.enabled", False, True, 0),
    ("boundary.enabled", False, True, None),
    ("governor.enabled", False, True, "false"),
    ("compression.compiler.warnings.aggressive", "keep", "count", "drop"),
]


def _toml(key: str, value: Any) -> str:
    *table, leaf = key.split(".")
    import json as _json

    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        return f"[{'.'.join(table)}]\n"  # TOML has no null: an empty table
    elif isinstance(value, dict):
        return f"[{key}]\n"
    elif isinstance(value, float) and value == float("inf"):
        text = "inf"
    else:
        text = _json.dumps(value)
    return f"[{'.'.join(table)}]\n{leaf} = {text}\n"


@pytest.mark.parametrize(("key", "tight", "loose", "junk"), CASES, ids=[c[0] for c in CASES])
def test_project_direction_per_key(
    tmp_path: Path, key: str, tight: Any, loose: Any, junk: Any
) -> None:
    proj, env = _setup(tmp_path / "base")
    base = load_config(proj, env).get(key)
    if key == "learn.hold.compression":
        base = None
    for name, value, accepted in (("t", tight, True), ("l", loose, False), ("j", junk, False)):
        proj, env = _setup(tmp_path / name, project=_toml(key, value))
        got = load_config(proj, env).get(key)
        if accepted:
            assert got == value, (key, value, got)
        else:
            assert got == base, (key, value, got)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("governor.hard_stop", True),
        ("governor.session_budget_tokens", 1000),
        ("governor.windows.default", 1000),
        ("governor.ewma_alpha", 0.9),
        ("governor.parse_budget_ms", 100000),
        ("compression.persisted_max_bytes", 10),
        ("compression.json.max_bytes", 10),
        ("compression.levels.standard.head", 329),  # head/tail displace relevant lines
        ("compression.levels.light.tail", 300),
        ("guards.pretrim_head", 300),
        ("guards.pretrim_lines", 400),
        ("adapter.latency_log_max_bytes", 10),
        ("adapter.latency_sample_rate", 1.0),
        ("learn.breaker_reentry_scale", 1.0),
        ("learn.breaker_window", 5),
        ("learn.regret_window_turns", 1),
        ("learn.enabled", False),
        ("extensions.allow_override", True),
        ("facts.anything", 1),
    ],
)
def test_user_only_keys_ignored_from_project(tmp_path: Path, key: str, value: Any) -> None:
    proj, env = _setup(tmp_path / "base")
    base = load_config(proj, env).get(key)
    proj, env = _setup(tmp_path / "p", project=_toml(key, value))
    assert load_config(proj, env).get(key) == base
    assert any(key in e for e in _errors(tmp_path / "p"))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("statusline.ascii", True),
        ("statusline.segments", ["ctx", "saved"]),
        ("boundary.min_turn", 5),
        ("boundary.history", 20),
        ("statusline.parse_budget_ms", 10),
        ("governor.ctx_notice", 0.7),
        ("pricing.output", 4.0),
    ],
)
def test_free_keys_follow_normal_precedence(tmp_path: Path, key: str, value: Any) -> None:
    proj, env = _setup(tmp_path, project=_toml(key, value))
    assert load_config(proj, env).get(key) == value


def test_free_keys_still_type_checked(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project='[statusline]\nascii = "yes"\nsegments = 3\n')
    cfg = load_config(proj, env)
    assert cfg.get("statusline.ascii") is False
    assert cfg.get("statusline.segments") == packaged_defaults()["statusline"]["segments"]


def test_skip_tools_project_may_only_add(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path / "a", project='[compression]\nskip_tools = ["WebFetch"]\n')
    got = load_config(proj, env).get("compression.skip_tools")
    assert "WebFetch" in got and "Edit" in got and "TodoWrite" in got
    proj, env = _setup(tmp_path / "b", project="[compression]\nskip_tools = []\n")
    assert "Edit" in load_config(proj, env).get("compression.skip_tools")
    proj, env = _setup(tmp_path / "c", project="[compression]\nskip_tools = 3\n")
    assert "Edit" in load_config(proj, env).get("compression.skip_tools")


def test_direction_is_relative_to_the_user(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path,
        user="[recovery]\nttl_days = 60\n[read_dedupe]\nenabled = false\n",
        project="[recovery]\nttl_days = 30\n[read_dedupe]\nenabled = true\n",
    )
    cfg = load_config(proj, env)
    assert cfg.get("recovery.ttl_days") == 60 and cfg.get("read_dedupe.enabled") is False


def test_is_num_rejects_overflowing_ints() -> None:
    assert not _is_num(10**300 * 10**300 * 10**300)
    assert not _is_num(int("9" * 400))
    assert _is_num(10**12) and not _is_num(True)


# --------------------------------------------------------------------------- e2e probes


def _hook(proj: Path, env: dict[str, str], text: str, use_id: str) -> str:
    import io

    from token_sparen.adapters.claude import entry

    payload = {
        "session_id": "s-probe",
        "cwd": str(proj),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"seq 1 3000 # {use_id}"},
        "tool_use_id": use_id,
        "tool_response": {"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
    }
    out = io.StringIO()
    entry.main(
        "PostToolUse",
        io.StringIO(json.dumps(payload)),
        out,
        {**env, "CLAUDE_PROJECT_DIR": str(proj)},
    )
    return out.getvalue()


_LINES = "\n".join(f"line {i} of the synthetic output with some padding words" for i in range(3000))


def test_probe_project_disabling_learning_cannot_override_user_hold(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path, user="[learn.hold]\ncompression = true\n", project="[learn]\nenabled = false\n"
    )
    outs = [_hook(proj, env, _LINES, f"a{i}") for i in range(10)]
    assert all("kept " not in o for o in outs)


def test_probe_project_cannot_make_recovery_impossible(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path,
        user="[learn]\nenabled = false\n",
        project="[recovery]\nttl_days = 0\nmax_files = 0\nmax_bytes = 0\n",
    )
    out = _hook(proj, env, _LINES, "b1")
    assert "kept " in out
    saved = list((proj / ".token-sparen" / "outputs").glob("*.txt"))
    assert len(saved) == 1


def test_probe_project_cannot_zero_the_levels(tmp_path: Path) -> None:
    proj, env = _setup(
        tmp_path,
        user="[learn]\nenabled = false\n",
        project="[compression.levels.standard]\nkeep_ratio = 0.0\nhead = 0\ntail = 0\n",
    )
    out = _hook(proj, env, _LINES, "c1")
    assert "kept " in out and "kept 0 of" not in out


def test_free_rules_are_explicit_keys() -> None:
    """No wildcard free rule: a new key must be classified (the completeness test catches it)."""
    free = [pattern for pattern, kind, _ in PROJECT_POLICY if kind == "free"]
    assert free and all("*" not in part and "**" not in part for p in free for part in p)


def test_boundary_history_is_bounded(tmp_path: Path) -> None:
    proj, env = _setup(tmp_path, project="[boundary]\nhistory = 50\n")
    assert load_config(proj, env).get("boundary.history") == 3
