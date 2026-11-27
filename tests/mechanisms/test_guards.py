"""Pre-tool guards (block unbounded scans) and the learned pre-trim decision point (post-tool)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Event, ToolCall
from token_sparen.learn.policy import PRETRIM, StaticPolicy
from token_sparen.mechanisms import guards

KEY = "0123456789abcdef"
BIG = 5_000_001


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return env


def _cfg(tmp_path: Path, **over: str) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _event(tmp_path: Path, tool: str, tool_input: Mapping[str, Any]) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind="pre_tool",
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall(tool, tool_category(tool), dict(tool_input), "tu-1"),
    )


def _bash(tmp_path: Path, command: str, **extra: Any) -> Event:
    return _event(tmp_path, "Bash", {"command": command, **extra})


def _check(tmp_path: Path, command: str, **over: str) -> str | None:
    return guards.check(_bash(tmp_path, command), _cfg(tmp_path, **over)).block_reason


def _big_file(tmp_path: Path, name: str = "big.log", size: int = BIG) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    path = proj / name
    with path.open("wb") as fh:
        fh.truncate(size)  # sparse: cheap to create
    return path


# --- find -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "find / -name foo",
        "find ~ -type f",
        "find ~/ -name '*.py'",
        "find -L / -name x",
        "find / -path '*/x/*' -prune -o -print",
        "find / -name x 2>/dev/null | head -5",
        "cd src && find / -name y",
        "ls; find ~ -name z",
        "FOO=1 find / -name x",
        "sudo find / -name x",
        "find . -name a && find / -name b",
    ],
)
def test_blocks_find_root(tmp_path: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None
    assert "-maxdepth" in reason and reason.count(".") >= 1


@pytest.mark.parametrize(
    "command",
    [
        "find / -maxdepth 2 -name x",
        "find ~ -name x -maxdepth 3",
        "find . -name x",
        "find src -type f",
        "find /usr/lib -name x",
        "find ./ -name '/'",
        "echo find /",
        "grep -r 'find /' src",
        "find",
        "fd x /",
    ],
)
def test_allows_find_with_maxdepth(tmp_path: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- grep / rg --------------------------------------------------------------------------


@pytest.fixture()
def vendored(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("node_modules", ".git", "dist", "build", ".venv", "src"):
        (proj / name).mkdir(parents=True)
    (proj / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    return proj


@pytest.mark.parametrize(
    "command",
    [
        "grep -r foo node_modules",
        "grep -rn foo ./node_modules/",
        "grep -R foo .git",
        "grep --recursive foo dist",
        "grep -nir foo build",
        "rg foo .venv",
        "rg -n foo src node_modules",
        "grep -r -e foo node_modules",
        "cat x | grep -r foo node_modules",
    ],
)
def test_blocks_recursive_grep_over_vendored(tmp_path: Path, vendored: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None and "include" in reason


@pytest.mark.parametrize(
    "command",
    [
        "grep -r --include=*.js foo node_modules",
        "grep -r --include '*.js' foo node_modules",
        "rg -g '*.ts' foo node_modules",
        "rg --glob=*.ts foo node_modules",
        "rg -t js foo node_modules",
        "grep -r node_modules src",
        "rg node_modules src",
        "grep foo node_modules/x.js",
        "grep -r foo node_modules/x.js",
        "grep -r foo src",
        "grep -r foo .",
        "rg -e node_modules src",
        "grep -r foo missing/node_modules",
    ],
)
def test_allows_bounded_grep(tmp_path: Path, vendored: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- cat / Read -------------------------------------------------------------------------


def test_blocks_cat_huge_file(tmp_path: Path) -> None:
    big = _big_file(tmp_path)
    (tmp_path / "proj" / "small.txt").write_text("hi", encoding="utf-8")
    reason = _check(tmp_path, "cat big.log")
    assert reason is not None and ("head" in reason or "tail" in reason)
    # shells (and posix shlex) treat unquoted backslashes as escapes: use the posix spelling,
    # which on Windows is the drive-letter form `C:/...`
    assert _check(tmp_path, f"cat -n {big.as_posix()}") is not None
    assert _check(tmp_path, "cat small.txt big.log") is not None
    assert _check(tmp_path, "cat small.txt") is None
    assert _check(tmp_path, "cat missing.txt") is None
    # the output is bounded or does not reach the context
    assert _check(tmp_path, "cat big.log | head -n 50") is None
    assert _check(tmp_path, "cat big.log > copy.log") is None
    # below the threshold, and a raised threshold
    _big_file(tmp_path, "edge.log", 5_000_000)
    assert _check(tmp_path, "cat edge.log") is None
    assert _check(tmp_path, "cat big.log", GUARDS__MAX_READ_BYTES="9000000") is None


def test_drive_letter_posix_path_resolves(tmp_path: Path) -> None:
    big = _big_file(tmp_path)
    ev = _bash(tmp_path, "cat x")
    assert guards._resolve(ev, big.as_posix()) == big
    assert guards._resolve(ev, big.as_posix()).is_file()
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        assert big.as_posix()[1:3] == ":/"
        assert _check(tmp_path, f"cat {big.as_posix()}") is not None


def test_blocks_read_tool_huge_file_without_range(tmp_path: Path) -> None:
    big = _big_file(tmp_path)
    cfg = _cfg(tmp_path)
    reason = guards.check(_event(tmp_path, "Read", {"file_path": str(big)}), cfg).block_reason
    assert reason is not None and "offset" in reason and "limit" in reason
    for rng in ({"limit": 200}, {"offset": 10}, {"offset": 1, "limit": 5}):
        ev = _event(tmp_path, "Read", {"file_path": str(big), **rng})
        assert guards.check(ev, cfg).is_noop()
    small = tmp_path / "proj" / "s.txt"
    small.write_text("x", encoding="utf-8")
    for path in (str(small), str(tmp_path / "nope"), 3):
        assert guards.check(_event(tmp_path, "Read", {"file_path": path}), cfg).is_noop()


# --- ls -R ------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["ls -R /", "ls -laR ~", "ls --recursive /", "ls -R / | wc"])
def test_blocks_ls_recursive_root(tmp_path: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None and "depth" in reason


@pytest.mark.parametrize("command", ["ls -R src", "ls /", "ls -la ~", "ls -R"])
def test_allows_ls(tmp_path: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- robustness -------------------------------------------------------------------------


def test_shlex_parse_error_never_blocks(tmp_path: Path) -> None:
    assert _check(tmp_path, 'find / -name "unterminated') is None
    assert _check(tmp_path, "find / -name 'x") is None


def test_guards_disabled_flag(tmp_path: Path) -> None:
    assert _check(tmp_path, "find / -name x", GUARDS__ENABLED="false") is None
    assert _check(tmp_path, "find / -name x", GUARDS__ENABLED="true") is not None


def test_guards_ignore_other_tools_and_bad_input(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert guards.check(_event(tmp_path, "Grep", {"pattern": "x", "path": "/"}), cfg).is_noop()
    assert guards.check(_event(tmp_path, "Bash", {"command": 3}), cfg).is_noop()
    assert guards.check(_event(tmp_path, "Bash", {}), cfg).is_noop()
    ev = _bash(tmp_path, "find / -name x")
    post = Event(**{**ev.__dict__, "kind": "post_tool"})
    assert guards.check(post, cfg).is_noop()
    no_tool = Event(**{**ev.__dict__, "tool": None})
    assert guards.check(no_tool, cfg).is_noop()


# --- multi-line commands, heredocs, redirects, cwd -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn TODO src\nls node_modules",
        "cat notes.txt\nwc -l big.log",
        "echo 'a\nfind / -name x'",
        "cat <<EOF\nfind / -name x\ncat big.log\nEOF",
        "cat <<-'END'\n\tfind / -name x\n\tEND\nls",
        "python - <<EOF\nls -R /\nEOF",
        "cat > notes.md <<'EOF'\ngrep -r foo node_modules\nEOF",
    ],
)
def test_multiline_no_false_blocks(tmp_path: Path, vendored: Path, command: str) -> None:
    _big_file(tmp_path)
    (vendored / "notes.txt").write_text("n", encoding="utf-8")
    assert _check(tmp_path, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd src\nfind / -name x",
        "ls\nls -R /",
        "find \\\n  / -name x",
        "cat <<EOF\nx\nEOF\nfind / -name y",
        "grep -rn TODO src\ngrep -r foo node_modules",
        "echo 'multi\nline'\ncat big.log",
    ],
)
def test_multiline_checks_every_line(tmp_path: Path, vendored: Path, command: str) -> None:
    _big_file(tmp_path)
    assert _check(tmp_path, command) is not None


def test_cat_stderr_redirect_is_not_stdout(tmp_path: Path) -> None:
    _big_file(tmp_path)
    assert _check(tmp_path, "cat big.log 2>/dev/null") is not None
    assert _check(tmp_path, "cat big.log 2>&1") is not None
    assert _check(tmp_path, "cat big.log 2> err.txt") is not None
    for command in (
        "cat big.log >/dev/null",
        "cat big.log 1>out",
        "cat big.log &>out",
        "cat big.log >> out",
        "cat big.log >&2",
    ):
        assert _check(tmp_path, command) is None, command


def test_relative_paths_use_payload_cwd(tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / "node_modules").mkdir(parents=True)
    _big_file(tmp_path, "../other/huge.log")
    cfg = _cfg(tmp_path)

    def ev(command: str, cwd: str | None) -> Event:
        base = _bash(tmp_path, command)
        extra = {} if cwd is None else {"cwd": cwd}
        return Event(**{**base.__dict__, "extra": MappingProxyType(extra)})

    assert guards.check(ev("grep -r foo node_modules", str(other)), cfg).block_reason
    assert guards.check(ev("grep -r foo node_modules", None), cfg).is_noop()
    assert guards.check(ev("cat huge.log", str(other)), cfg).block_reason
    assert guards.check(ev("cat huge.log", None), cfg).is_noop()
    assert guards.check(ev("grep -r foo node_modules", "relative/dir"), cfg).is_noop()


def test_read_guard_ranges_and_non_text(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def read(name: str, **inp: Any) -> str | None:
        path = _big_file(tmp_path, name, 6_000_000)
        return guards.check(
            _event(tmp_path, "Read", {"file_path": str(path), **inp}), cfg
        ).block_reason

    assert read("doc.pdf", pages="1-3") is None
    assert read("doc.pdf") is None
    assert read("shot.PNG") is None
    assert read("img.jpeg") is None
    assert read("nb.ipynb") is None
    assert read("data.csv", pages="1") is None
    assert read("data.csv") is not None


# --- pretrim (PostToolUse compression arm) ------------------------------------------------


def _store(tmp_path: Path, candidates: Mapping[str, Any] | str) -> Path:
    store = tmp_path / "home" / "projects" / KEY
    store.mkdir(parents=True, exist_ok=True)
    body = candidates if isinstance(candidates, str) else json.dumps({"candidates": candidates})
    (store / "pretrim.json").write_text(body, encoding="utf-8")
    return store


def _lines(tmp_path: Path, ev: Event, store: Path, **over: str) -> int | None:
    return guards.pretrim_lines(ev, store, _cfg(tmp_path, **over), env=_env(tmp_path))


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_pretrim_point_contract(tmp_path: Path) -> None:
    from token_sparen.events import SessionCtx

    assert guards.PRETRIM is PRETRIM
    assert PRETRIM.name == "pretrim" and PRETRIM.arms == ("off", "bounded")
    assert PRETRIM.safe_arm == "off" and PRETRIM.tier == "medium"
    assert guards._normalise("pytest -q") == "pytest -q"
    sctx = SessionCtx("s", KEY, 0, None, None, tmp_path / "o.json")
    assert StaticPolicy({}).decide(PRETRIM, {}, sctx).arm == "off"  # static default


def test_pretrim_lines_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path, {"pytest -q": {"lines": 80}, "make test": {}})
    assert _lines(tmp_path, _bash(tmp_path, "pytest -q"), store) == 80
    assert _lines(tmp_path, _bash(tmp_path, "make test"), store) == 120
    assert _lines(tmp_path, _bash(tmp_path, "make test"), store, GUARDS__PRETRIM_LINES="40") == 40
    assert _lines(tmp_path, _bash(tmp_path, "pytest -x"), store) is None
    assert _lines(tmp_path, _event(tmp_path, "Read", {"command": "pytest -q"}), store) is None
    assert _lines(tmp_path, _bash(tmp_path, 3), store) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", '{"candidates": []}', '{"candidates": {"pytest": 3}}'],
)
def test_pretrim_bad_store_is_not_a_candidate(tmp_path: Path, body: str) -> None:
    store = _store(tmp_path, body)
    assert _lines(tmp_path, _bash(tmp_path, "pytest"), store) is None


@pytest.mark.parametrize("lines", [0, -5, "12", True, 10.5])
def test_pretrim_bad_lines_falls_back(tmp_path: Path, lines: Any) -> None:
    store = _store(tmp_path, {"pytest": {"lines": lines}})
    assert _lines(tmp_path, _bash(tmp_path, "pytest"), store) == 120


def test_pretrim_missing_store(tmp_path: Path) -> None:
    store = tmp_path / "home" / "projects" / KEY
    assert _lines(tmp_path, _bash(tmp_path, "pytest"), store) is None


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "pytest | tee out",
        "pytest > out.txt",
        "make && make test",
        "make; make test",
        "echo $(date)",
        "cat <<EOF\nx\nEOF",
        "sleep 10 &",
        "vim x.py",
        "less log",
        "python",
        "python run.py",
    ],
)
def test_pretool_never_rewrites_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from token_sparen.adapters.claude import entry

    _store(tmp_path, {command: {"lines": 50}})
    monkeypatch.setattr(entry, "POLICY", StaticPolicy({"pretrim": "bounded"}))
    token = entry.HOOK_ENV.set(_env(tmp_path))
    try:
        act = entry.handle(_bash(tmp_path, command, description="d"), _cfg(tmp_path))
    finally:
        entry.HOOK_ENV.reset(token)
    assert act.rewrite_input is None and act.block_reason is None


def _noisy(n: int = 3000) -> str:
    verbs = ("compiling", "linking", "scanning", "packing", "hashing", "copying", "checking")
    nouns = ("alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa", "theta", "zeta")
    body = [f"{verbs[i % 7]} {nouns[i % 9]} unit {i} of the bundle" for i in range(n)]
    body[3] = "ERROR: failed to resolve dependency module_x (code 7)"
    return "\n".join(body)


def _post(tmp_path: Path, command: str, stdout: str) -> Event:
    base = _bash(tmp_path, command)
    resp = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}
    return Event(
        **{**base.__dict__, "kind": "post_tool", "output_text": stdout, "raw_response": resp}
    )


def _compress(tmp_path: Path, ev: Event, arms: dict[str, str], **over: str) -> Any:
    from token_sparen.mechanisms import compression

    cfg = _cfg(tmp_path, **over)
    return compression.handle_post_tool(ev, StaticPolicy(arms), cfg, env=_env(tmp_path))


def test_pretrim_bounded_is_tail_biased_compression(tmp_path: Path) -> None:
    _store(tmp_path, {"make build": {}})
    out = _noisy()
    act = _compress(
        tmp_path,
        _post(tmp_path, "make build", out),
        {"pretrim": "bounded"},
        COMPRESSION__ALLOW_CTYPES="text",  # plain text is opt-in
    )
    body = act.replace_output
    assert body is not None and "[token-sparen: kept " in body
    assert "ERROR: failed to resolve dependency module_x (code 7)" in body  # early error kept
    lines = out.splitlines()
    assert all(line in body for line in lines[-120:])  # tail K kept
    assert lines[0] in body and lines[9] in body  # head 10 kept
    (rec,) = _decisions(tmp_path)
    assert rec["point"] == "pretrim" and rec["arm"] == "bounded" and rec["recovery_id"]
    assert rec["command_sig"] == "make build"
    saved = tmp_path / "proj" / ".token-sparen" / "outputs" / f"{rec['recovery_id']}.txt"
    assert saved.read_text(encoding="utf-8") == out  # fully recoverable


def test_pretrim_off_or_not_candidate_uses_compression_point(tmp_path: Path) -> None:
    _store(tmp_path, {"make build": {}})
    out = _noisy()
    widen = {"COMPRESSION__ALLOW_CTYPES": "text"}  # plain text is opt-in
    _compress(tmp_path, _post(tmp_path, "make build", out), {}, **widen)  # static pretrim = off
    _compress(tmp_path, _post(tmp_path, "make other", out), {"pretrim": "bounded"}, **widen)
    recs = _decisions(tmp_path)
    assert [(r["point"], r["arm"]) for r in recs] == [
        ("pretrim", "off"),  # logged no-op (propensity kept for off-policy evaluation)
        ("compression", "standard"),
        ("compression", "standard"),
    ]
    assert recs[0]["tokens_after"] == recs[0]["tokens_before"]


def test_pretrim_never_consulted_for_gated_output(tmp_path: Path) -> None:
    """Plain text is not an allowed content type by default: no pre-trim decision at all."""
    _store(tmp_path, {"make build": {}})
    act = _compress(tmp_path, _post(tmp_path, "make build", _noisy()), {"pretrim": "bounded"})
    assert act.is_noop() and _decisions(tmp_path) == []


def test_pretrim_respects_compression_gates(tmp_path: Path) -> None:
    _store(tmp_path, {"make build": {}})
    small = "\n".join(f"line {i}" for i in range(50))
    assert _compress(
        tmp_path, _post(tmp_path, "make build", small), {"pretrim": "bounded"}
    ).is_noop()
    assert _decisions(tmp_path) == []


@pytest.mark.perf
def test_guard_check_fast(tmp_path: Path) -> None:
    from perfutil import best_of, perf_factor

    cfg = _cfg(tmp_path)
    ev = _bash(tmp_path, " && ".join(f"grep -rn 'x{i}' src/{i} | head -5" for i in range(50)))
    best, act = best_of(lambda: guards.check(ev, cfg), 5)
    assert act.is_noop()
    assert best < 0.005 * perf_factor()


# --- M4 Task 6: bounded pre-trim keep ratio ----------------------------------------------


def test_bounded_pretrim_default_reproduces_current(tmp_path: Path) -> None:
    from token_sparen.compress import Level, load_level

    def old(cfg: Config, lines: int) -> Level:  # the pre-M4 formula
        head = cfg.get("guards.pretrim_head", 10)
        return Level(head=head, tail=lines, keep_ratio=load_level(cfg, "standard").keep_ratio)

    home = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    cfg = load_config(tmp_path / "proj", home)
    assert cfg.get("guards.pretrim_bounded_keep_ratio") is None  # no default: follow standard
    assert guards.pretrim_level(cfg, 120) == old(cfg, 120)
    # unset it follows the standard arm, exactly as before
    std = load_config(
        tmp_path / "proj", {**home, "TOKEN_SPAREN_COMPRESSION__LEVELS__STANDARD__KEEP_RATIO": "0.5"}
    )
    assert guards.pretrim_level(std, 50) == old(std, 50)
    assert guards.pretrim_level(std, 50).keep_ratio == 0.5
    # set: it is the bounded arm's keep ratio
    own = load_config(
        tmp_path / "proj", {**home, "TOKEN_SPAREN_GUARDS__PRETRIM_BOUNDED_KEEP_RATIO": "0.2"}
    )
    assert guards.pretrim_level(own, 50).keep_ratio == 0.2
    # any explicit number is used as is, 0.35 included (no sentinel)
    explicit = load_config(
        tmp_path / "proj",
        {
            **home,
            "TOKEN_SPAREN_GUARDS__PRETRIM_BOUNDED_KEEP_RATIO": "0.35",
            "TOKEN_SPAREN_COMPRESSION__LEVELS__STANDARD__KEEP_RATIO": "0.2",
        },
    )
    assert guards.pretrim_level(explicit, 50).keep_ratio == 0.35
    bad = load_config(
        tmp_path / "proj", {**home, "TOKEN_SPAREN_GUARDS__PRETRIM_BOUNDED_KEEP_RATIO": "7"}
    )
    assert guards.pretrim_level(bad, 50) == old(bad, 50)  # out of range: ignored


def test_bounded_pretrim_project_may_only_raise(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    # unset by the user: the project's number is compared with the standard keep ratio (0.35)
    (proj / "token-sparen.toml").write_text("[guards]\npretrim_bounded_keep_ratio = 0.1\n", "utf-8")
    assert guards.pretrim_level(load_config(proj, env), 50).keep_ratio == 0.35
    (proj / "token-sparen.toml").write_text("[guards]\npretrim_bounded_keep_ratio = 0.6\n", "utf-8")
    assert load_config(proj, env).get("guards.pretrim_bounded_keep_ratio") == 0.6
    assert guards.pretrim_level(load_config(proj, env), 50).keep_ratio == 0.6
    # set by the user: a project may only raise it
    user = {**env, "TOKEN_SPAREN_GUARDS__PRETRIM_BOUNDED_KEEP_RATIO": "0.5"}
    (proj / "token-sparen.toml").write_text("[guards]\npretrim_bounded_keep_ratio = 0.4\n", "utf-8")
    assert load_config(proj, user).get("guards.pretrim_bounded_keep_ratio") == 0.5
    # a project raising it above the default but below the user's standard ratio keeps the
    # user's (larger) ratio: a project never trims more
    std = {**env, "TOKEN_SPAREN_COMPRESSION__LEVELS__STANDARD__KEEP_RATIO": "0.7"}
    assert guards.pretrim_level(load_config(proj, std), 50).keep_ratio == 0.7
