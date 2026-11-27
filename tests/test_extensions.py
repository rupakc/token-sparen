"""Extension loading (entry points + trusted drop-ins) and the conformance kit (spec section 9)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from userconf import write_user_config

from token_sparen import config as config_mod
from token_sparen import extensions
from token_sparen.cli import main
from token_sparen.compress import (
    CompressContext,
    CompressResult,
    Level,
    Registry,
    TextCompressor,
    registry,
)
from token_sparen.config import SESSION_KEY, Config, load_config
from token_sparen.learn import policy
from token_sparen.testing import conformance

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink/ownership/permission bits are POSIX semantics"
)

LEVEL = Level(head=2, tail=2, keep_ratio=0.3)
CTX = CompressContext(prompt=None, recent_paths=())

# A drop-in whose import leaves a sentinel file behind, so "never imported" is observable.
_DROPIN = """\
from pathlib import Path
from token_sparen.compress import CompressResult

Path({sentinel!r}).write_text("imported", encoding="utf-8")


class FirstLine:
    name = "first-line"
    handles = frozenset({handles!r})

    def compress(self, text, level, ctx):
        body = text[:-1] if text.endswith("\\n") else text
        lines = body.split("\\n")
        return CompressResult(
            text={body}, kept_lines=1, total_lines=len(lines) if text else 0, lossless=False
        )


TOKEN_SPAREN_EXTENSION = {{"compressors": [FirstLine]}}
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    for k in list(os.environ):
        if k.startswith(("TOKEN_SPAREN_", "CLAUDE_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    monkeypatch.chdir(proj)
    monkeypatch.setattr(extensions, "_entry_points", lambda group: [])
    extensions.reset()
    yield {"TOKEN_SPAREN_HOME": str(home), "CLAUDE_PROJECT_DIR": str(proj)}
    extensions.reset()


def _home(env: dict[str, str]) -> Path:
    return Path(env["TOKEN_SPAREN_HOME"])


def _proj(env: dict[str, str]) -> Path:
    return Path(env["CLAUDE_PROJECT_DIR"])


def _dropin(
    where: Path,
    name: str = "ext.py",
    handles: tuple[str, ...] = ("table",),
    body: str = 'lines[0] + "|ext"',
    source: str | None = None,
) -> tuple[Path, Path]:
    where.mkdir(parents=True, exist_ok=True)
    sentinel = where.parent / f"{name}.imported"
    path = where / name
    text = source or _DROPIN.format(sentinel=str(sentinel), handles=set(handles), body=body)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)
    return path, sentinel


def _proj_ext(env: dict[str, str]) -> Path:
    return _proj(env) / ".token-sparen" / "extensions"


def _load(env: dict[str, str], cfg: Config | None = None) -> extensions.LoadedExtensions:
    cfg = cfg if cfg is not None else load_config(_proj(env), env)
    return extensions.load_extensions(_proj(env), _home(env), cfg, env=env)


def _logged(env: dict[str, str]) -> list[dict[str, Any]]:
    path = _home(env) / "logs" / "errors.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- brief tests --------------------------------------------------------------------------


def test_untrusted_dropin_ignored(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_proj_ext(env))
    loaded = _load(env)
    (found,) = loaded.dropins
    assert found.status == "untrusted"
    assert found.sha256 == _sha(path)
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert not sentinel.exists()
    assert f"token_sparen_ext_{_sha(path)[:12]}" not in sys.modules


def test_trusted_dropin_loaded_lazily(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_home(env) / "extensions")
    sha = extensions.trust(path, _home(env), env)
    assert sha == _sha(path)
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["trusted"]
    assert not sentinel.exists()  # discovery never imports
    assert isinstance(loaded.compressor_for("text"), TextCompressor)  # built-in, no trigger
    assert not sentinel.exists()
    comp = loaded.compressor_for("table")
    assert sentinel.exists()
    assert f"token_sparen_ext_{sha[:12]}" in sys.modules
    out = comp.compress("alpha\nbeta\ngamma", LEVEL, CTX)
    assert out.text == "alpha|ext"
    assert (out.kept_lines, out.total_lines) == (1, 3)
    assert _load(env) is loaded  # discovery at most once per process


def test_faulty_extension_disabled_not_crash(env: dict[str, str]) -> None:
    ext = _home(env) / "extensions"
    bad_import, _ = _dropin(ext, "a_bad.py", source="raise RuntimeError('boom at import')\n")
    raising, _ = _dropin(ext, "b_raise.py", body="1 / 0")
    wrong, _ = _dropin(ext, "c_wrong.py", source="TOKEN_SPAREN_EXTENSION = {'compressors': [42]}\n")
    for p in (bad_import, raising, wrong):
        extensions.trust(p, _home(env), env)
    loaded = _load(env)
    text = "one\ntwo\nthree\n" * 50
    comp = loaded.compressor_for("table")
    first = comp.compress(text, LEVEL, CTX)
    expected = TextCompressor().compress(text, LEVEL, CTX)
    assert first == expected  # fell back to the built-in for this content type
    again = loaded.compressor_for("table")
    assert isinstance(again, TextCompressor)  # the faulty one stays disabled
    assert again.compress(text, LEVEL, CTX) == expected
    errors = [e["error"] for e in _logged(env) if e["where"].startswith("extensions")]
    assert any("boom at import" in e for e in errors)
    assert any("ZeroDivisionError" in e for e in errors)
    assert any("not a Compressor" in e for e in errors)
    assert sum("ZeroDivisionError" in e for e in errors) == 1  # logged once


def test_builtin_compressors_pass_conformance() -> None:
    builtins = registry.builtins()
    assert len(builtins) >= 10
    for comp in builtins:
        assert conformance.check_compressor(comp) == [], comp.name


# --- trust model --------------------------------------------------------------------------


def test_trust_revoked_when_file_edited(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_home(env) / "extensions")
    extensions.trust(path, _home(env), env)
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["changed-since-trust"]
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert not sentinel.exists()


def test_project_cannot_pre_trust_itself(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_proj_ext(env), handles=("text",))
    sha = _sha(path)
    fake = {"trusted": {sha: {"path": str(path.resolve()), "ts": 1}}}
    (_proj(env) / ".token-sparen" / "trusted_extensions.json").write_text(
        json.dumps(fake), encoding="utf-8"
    )
    (_proj(env) / "token-sparen.toml").write_text(
        "[extensions]\nallow_override = true\n", encoding="utf-8"
    )
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["untrusted"]
    assert isinstance(loaded.compressor_for("text"), TextCompressor)
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert not sentinel.exists()


def test_project_allow_override_is_ignored(env: dict[str, str]) -> None:
    path, _ = _dropin(_home(env) / "extensions", handles=("text",))
    extensions.trust(path, _home(env), env)
    (_proj(env) / "token-sparen.toml").write_text(
        "[extensions]\nallow_override = true\n", encoding="utf-8"
    )
    loaded = _load(env)
    assert loaded.allow_override is False
    assert isinstance(loaded.compressor_for("text"), TextCompressor)
    assert any("allow_override" in e["error"] for e in _logged(env))


def test_override_flag_lets_extension_replace_builtin(env: dict[str, str]) -> None:
    path, _ = _dropin(_home(env) / "extensions", handles=("text",))
    extensions.trust(path, _home(env), env)
    assert isinstance(_load(env).compressor_for("text"), TextCompressor)  # built-ins win
    extensions.reset()
    _home(env).mkdir(parents=True, exist_ok=True)
    (_home(env) / "config.toml").write_text(
        "[extensions]\nallow_override = true\n", encoding="utf-8"
    )
    loaded = _load(env)
    assert loaded.allow_override is True
    comp = loaded.compressor_for("text")
    assert not isinstance(comp, TextCompressor)
    assert comp.compress("a\nb", LEVEL, CTX).text == "a|ext"


def test_override_flag_never_from_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository's committed Claude Code settings can set environment variables."""
    monkeypatch.setenv("TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE", "true")
    env = {**env, "TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE": "true"}
    assert _load(env).allow_override is False


def test_override_flag_never_from_session(env: dict[str, str]) -> None:
    session = {SESSION_KEY: {"extensions": {"allow_override": True}}}
    assert _load(env, load_config(_proj(env), env, session=session)).allow_override is False


@posix_only
def test_symlinked_dropin_and_dir_skipped(env: dict[str, str], tmp_path: Path) -> None:
    real, sentinel = _dropin(tmp_path / "elsewhere")
    extensions.trust(real, _home(env), env)
    ext = _proj_ext(env)
    ext.mkdir(parents=True)
    (ext / "link.py").symlink_to(real)
    (_home(env) / "extensions").symlink_to(tmp_path / "elsewhere")
    loaded = _load(env)
    statuses = sorted(d.status for d in loaded.dropins)
    assert statuses == ["skipped:symlink", "skipped:symlinked-dir"]
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert not sentinel.exists()


@posix_only
def test_symlinked_project_state_dir_skipped(env: dict[str, str], tmp_path: Path) -> None:
    real, sentinel = _dropin(tmp_path / "other" / "extensions")
    extensions.trust(real, _home(env), env)
    (_proj(env) / ".token-sparen").symlink_to(tmp_path / "other")
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["skipped:symlinked-dir"]
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert not sentinel.exists()


@posix_only
def test_world_writable_and_foreign_files_skipped(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = _home(env) / "extensions"
    ww, _ = _dropin(ext, "ww.py")
    extensions.trust(ww, _home(env), env)
    ww.chmod(0o666)
    assert [d.status for d in _load(env).dropins] == ["skipped:world-writable"]
    ww.chmod(0o644)
    extensions.reset()
    monkeypatch.setattr(extensions, "_current_uid", lambda: os.getuid() + 1)
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["skipped:not-owned"]
    assert isinstance(loaded.compressor_for("table"), TextCompressor)


@posix_only
def test_world_writable_trust_file_ignored(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_home(env) / "extensions")
    extensions.trust(path, _home(env), env)
    (_home(env) / "trusted_extensions.json").chmod(0o666)
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["untrusted"]
    assert not sentinel.exists()


def test_no_extension_dirs_costs_no_hashing(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not read files")

    monkeypatch.setattr(extensions, "_read_checked", boom)
    monkeypatch.setattr(extensions, "_read_trust", boom)
    loaded = _load(env)
    assert loaded.dropins == []
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert loaded.decision_points() == []


def test_unusual_dropin_files(env: dict[str, str]) -> None:
    ext = _home(env) / "extensions"
    ext.mkdir(parents=True)
    (ext / "notes.txt").write_text("x", encoding="utf-8")
    (ext / "pkg.py").mkdir()
    big = ext / "big.py"
    big.write_bytes(b"#" * (extensions.MAX_DROPIN_BYTES + 1))
    loaded = _load(env)
    assert [(d.path.name, d.status) for d in loaded.dropins] == [
        ("big.py", "skipped:too-large"),
        ("pkg.py", "skipped:not-a-file"),
    ]


def test_same_dir_for_home_and_project_listed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extensions, "_entry_points", lambda group: [])
    extensions.reset()
    proj = tmp_path / "user"
    home = proj / ".token-sparen"
    _dropin(home / "extensions")
    env = {"TOKEN_SPAREN_HOME": str(home)}
    loaded = extensions.load_extensions(proj, home, Config(), env=env)
    assert len(loaded.dropins) == 1


def test_identical_content_imported_once(env: dict[str, str]) -> None:
    path, sentinel = _dropin(_home(env) / "extensions")
    twin = _proj_ext(env)
    twin.mkdir(parents=True)
    (twin / "ext.py").write_bytes(path.read_bytes())
    extensions.trust(path, _home(env), env)
    loaded = _load(env)
    assert [d.status for d in loaded.dropins] == ["trusted", "trusted"]
    loaded.compressor_for("table")
    assert sentinel.read_text(encoding="utf-8") == "imported"


def test_extension_prints_never_reach_stdout(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    src = _DROPIN.format(sentinel=str(_home(env) / "s"), handles={"table"}, body="lines[0]")
    src = src.replace("class FirstLine:", "print('noise at import')\n\n\nclass FirstLine:")
    src = src.replace("body = text[:-1]", "print('noise in compress')\n        body = text[:-1]")
    path, _ = _dropin(_home(env) / "extensions", source=src)
    extensions.trust(path, _home(env), env)
    _load(env).compressor_for("table").compress("a\nb", LEVEL, CTX)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "noise at import" in captured.err and "noise in compress" in captured.err


# --- runtime safety -----------------------------------------------------------------------


def test_runtime_safety_disables_error_dropping_extension(env: dict[str, str]) -> None:
    path, _ = _dropin(_home(env) / "extensions")  # keeps only the first line
    extensions.trust(path, _home(env), env)
    loaded = _load(env)
    text = "head\n" + "noise\n" * 40 + "ERROR: disk full\n" + "noise\n" * 40
    comp = loaded.compressor_for("table")
    result = comp.compress(text, LEVEL, CTX)
    assert "ERROR: disk full" in result.text
    assert result == TextCompressor().compress(text, LEVEL, CTX)
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert any("error line" in e["error"] for e in _logged(env))


def test_runtime_safety_rejects_bad_result(env: dict[str, str]) -> None:
    ext = _home(env) / "extensions"
    for name, body in (("a.py", "None"), ("b.py", "'x'")):
        src = _DROPIN.format(sentinel=str(ext.parent / name), handles={"table"}, body=body)
        if name == "a.py":
            src = src.replace("return CompressResult(", "return ('not', ")
        else:
            src = src.replace("kept_lines=1", "kept_lines=99")
        p, _ = _dropin(ext, name, source=src)
        extensions.trust(p, _home(env), env)
    loaded = _load(env)
    text = "x\ny\nz"
    assert loaded.compressor_for("table").compress(text, LEVEL, CTX) == TextCompressor().compress(
        text, LEVEL, CTX
    )
    assert loaded.compressor_for("table").compress(text, LEVEL, CTX) == TextCompressor().compress(
        text, LEVEL, CTX
    )
    assert isinstance(loaded.compressor_for("table"), TextCompressor)


def test_registry_builtins_win_over_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "log_error", lambda *a: None)

    class _Json:
        name = "ep-json"
        handles = frozenset({"json", "table"})

        def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
            return CompressResult(text=text[:1], kept_lines=1, total_lines=1, lossless=False)

    class _EP:
        name = "json-ep"

        def load(self) -> Any:
            return _Json

    builtin_text = TextCompressor()

    class _BuiltinJson(TextCompressor):
        name = "builtin-json"
        handles = frozenset({"json"})

    builtin_json = _BuiltinJson()
    reg = Registry(entry_points_fn=lambda: [_EP()])
    reg.register(builtin_text)  # register() is the built-in / in-process path
    reg.register(builtin_json)
    assert reg.for_type("json") is builtin_json  # built-ins win by default
    assert reg.for_type("text") is builtin_text
    assert extensions.unwrap(reg.for_type("table")).name == "ep-json"  # new content type
    assert extensions.unwrap(reg.for_type("json", allow_override=True)).name == "ep-json"
    assert reg.for_type("text", allow_override=True) is builtin_text  # ep lacks "text"
    assert reg.builtins() == [builtin_text, builtin_json]


def _count_dropin(env: dict[str, str], name: str, result_expr: str) -> None:
    src = (
        "from token_sparen.compress import CompressResult\n"
        "class C:\n"
        "    name = 'counts'\n"
        "    handles = frozenset({'table'})\n"
        "    def compress(self, text, level, ctx):\n"
        f"        return {result_expr}\n"
        "TOKEN_SPAREN_EXTENSION = {'compressors': [C]}\n"
    )
    path, _ = _dropin(_home(env) / "extensions", name, source=src)
    extensions.trust(path, _home(env), env)


@pytest.mark.parametrize(
    "result_expr",
    [
        "CompressResult(text=text.split('\\n')[0], kept_lines=1, total_lines=10**9,"
        " lossless=False)",
        "CompressResult(text=text.split('\\n')[0], kept_lines=0, total_lines=1, lossless=False)",
        "CompressResult(text=text.split('\\n')[0], kept_lines=3, total_lines=3, lossless=False)",
        "CompressResult(text='… [3 lines elided]', kept_lines=1, total_lines=3, lossless=False)",
    ],
    ids=["inflated-total", "deflated-total", "kept-beyond-output", "kept-only-marker"],
)
def test_runtime_rejects_inconsistent_counts(env: dict[str, str], result_expr: str) -> None:
    _count_dropin(env, "counts.py", result_expr)
    loaded = _load(env)
    text = "alpha\nbeta\ngamma\n"
    assert loaded.compressor_for("table").compress(text, LEVEL, CTX) == TextCompressor().compress(
        text, LEVEL, CTX
    )
    assert isinstance(loaded.compressor_for("table"), TextCompressor)  # disabled
    assert any("lines" in e["error"] for e in _logged(env) if e["where"].startswith("ext"))


def test_runtime_accepts_consistent_counts(env: dict[str, str]) -> None:
    expr = (
        "CompressResult(text='alpha\\n… [2 lines elided]', kept_lines=1, total_lines=3,"
        " lossless=False)"
    )
    _count_dropin(env, "ok.py", expr)
    loaded = _load(env)
    out = loaded.compressor_for("table").compress("alpha\nbeta\ngamma\n", LEVEL, CTX)
    assert out.text == "alpha\n… [2 lines elided]"
    assert not isinstance(loaded.compressor_for("table"), TextCompressor)


def test_error_line_check_matches_whole_lines() -> None:
    from token_sparen.compress import missing_error_lines

    src = "start\nFAIL\nend\nERROR x   \n"
    assert missing_error_lines(src, "start\nFAIL: not the same line\nERROR x") == ["FAIL"]
    assert missing_error_lines(src, "FAIL  \nERROR x\n") == []  # trailing whitespace ignored
    assert missing_error_lines(src, "  FAIL\nERROR x") == ["FAIL"]  # leading is significant


def test_runtime_rejects_error_line_only_inside_longer_line(env: dict[str, str]) -> None:
    expr = "CompressResult(text='x FAIL y', kept_lines=1, total_lines=3, lossless=False)"
    _count_dropin(env, "sub.py", expr)
    loaded = _load(env)
    text = "ok\nFAIL\nok2"
    assert "FAIL" in loaded.compressor_for("table").compress(text, LEVEL, CTX).text.split("\n")
    assert isinstance(loaded.compressor_for("table"), TextCompressor)


@pytest.mark.parametrize("value", ["1", "true", "TRUE"])
def test_override_env_values_ignored(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE", value)
    assert _load({**env, "TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE": value}).allow_override is False


def test_override_user_file_int_one(env: dict[str, str]) -> None:
    _home(env).mkdir(parents=True, exist_ok=True)
    (_home(env) / "config.toml").write_text("[extensions]\nallow_override = 1\n", encoding="utf-8")
    assert _load(env).allow_override is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_override_env_false_values(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE", value)
    assert _load({**env, "TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE": value}).allow_override is False


# --- other entry-point groups -------------------------------------------------------------


class _EPObj:
    def __init__(self, name: str, obj: Any, value: str = "pkg.mod:obj") -> None:
        self.name = name
        self.value = value
        self._obj = obj

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


class _Adapter:
    name = "fake-host"
    capabilities = frozenset({"post_tool"})

    def parse(self, event_name: str, payload: Any, env: Any) -> Any:
        raise ValueError("unused")

    def render(self, event: Any, action: Any) -> dict[str, Any]:
        return {}


def test_entry_point_groups(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    point = policy.DecisionPoint("ext_point", ("off", "on"), safe_arm="off", tier="low")
    clash = policy.DecisionPoint("compression", ("off", "x"), safe_arm="off", tier="low")
    bad_point = policy.DecisionPoint("bad_point", ("on",), safe_arm="off", tier="low")

    def signal(record: Any) -> float:
        return 1.0

    groups = {
        "token_sparen.decision_points": [
            _EPObj("p", point),
            _EPObj("clash", clash),
            _EPObj("bad", bad_point),
            _EPObj("junk", 7),
            _EPObj("err", ImportError("missing dep")),
        ],
        "token_sparen.signals": [_EPObj("s", signal), _EPObj("nope", "not callable")],
        "token_sparen.adapters": [_EPObj("a", _Adapter), _EPObj("bad", object())],
    }
    monkeypatch.setattr(extensions, "_entry_points", lambda group: groups.get(group, []))
    loaded = _load(env)
    assert loaded.decision_points() == [point]
    assert loaded.decision_points() == [point]  # cached, logged once
    assert policy.get_point("ext_point") == point
    assert policy.get_point("compression") == policy.COMPRESSION  # built-ins win
    assert loaded.signals() == [signal]
    assert list(loaded.adapters()) == ["fake-host"]
    errors = [e["error"] for e in _logged(env)]
    assert sum("missing dep" in e for e in errors) == 1
    assert any("safe_arm" in e for e in errors)
    assert any("clash" in e or "compression" in e for e in errors)


def test_dropin_decision_points_and_signals(env: dict[str, str]) -> None:
    src = (
        "from token_sparen.learn.policy import DecisionPoint\n"
        "P = DecisionPoint('dropin_point', ('off', 'on'), safe_arm='off', tier='low')\n"
        "def sig(x):\n    return 0.0\n"
        "TOKEN_SPAREN_EXTENSION = {'decision_points': [P], 'signals': [sig]}\n"
    )
    path, _ = _dropin(_home(env) / "extensions", source=src)
    extensions.trust(path, _home(env), env)
    loaded = _load(env)
    (p,) = loaded.decision_points()
    assert p.name == "dropin_point"
    assert len(loaded.signals()) == 1
    assert isinstance(loaded.compressor_for("table"), TextCompressor)


def test_dropin_bad_manifest(env: dict[str, str]) -> None:
    ext = _home(env) / "extensions"
    for name, src in (
        ("a.py", "X = 1\n"),
        ("b.py", "TOKEN_SPAREN_EXTENSION = ['compressors']\n"),
        ("c.py", "TOKEN_SPAREN_EXTENSION = {'compressors': 'nope'}\n"),
        ("d.py", "TOKEN_SPAREN_EXTENSION = {'widgets': []}\n"),
    ):
        p, _ = _dropin(ext, name, source=src)
        extensions.trust(p, _home(env), env)
    loaded = _load(env)
    assert isinstance(loaded.compressor_for("table"), TextCompressor)
    assert loaded.decision_points() == []
    errors = [e["error"] for e in _logged(env)]
    assert len([e for e in errors if "TOKEN_SPAREN_EXTENSION" in e]) == 4


def test_point_registry_validation() -> None:
    assert policy.get_point("compression") is policy.COMPRESSION
    assert {"compression", "pretrim", "read_dedupe", "subagent_brief"} <= set(policy.points())
    with pytest.raises(ValueError):
        policy.register_point(policy.COMPRESSION)
    with pytest.raises(ValueError):
        policy.register_point(policy.DecisionPoint("zz", ("on",), safe_arm="off", tier="low"))
    with pytest.raises(ValueError):
        policy.register_point(policy.DecisionPoint("zz", ("off",), safe_arm="off", tier="bad"))  # type: ignore[arg-type]


# --- CLI ----------------------------------------------------------------------------------


def test_cli_extensions_list(
    env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted, _ = _dropin(_home(env) / "extensions", "good.py")
    extensions.trust(trusted, _home(env), env)
    _dropin(_proj_ext(env), "other.py", body="'x'")
    groups = {"token_sparen.compressors": [_EPObj("fancy", None, "fancy_pkg:Comp")]}
    monkeypatch.setattr(extensions, "_entry_points", lambda group: groups.get(group, []))
    assert main(["extensions", "list"]) == 0
    out = capsys.readouterr().out
    assert "trusted" in out and "good.py" in out
    assert "untrusted" in out and "other.py" in out
    assert "token_sparen.compressors" in out and "fancy_pkg:Comp" in out


def test_cli_extensions_list_empty(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extensions", "list"]) == 0
    assert "no extensions" in capsys.readouterr().out


def test_cli_trust_requires_yes(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    src = "".join(f"x{i} = {i}\n" for i in range(60)) + "y = '\x1b[31m'\n"
    path, _ = _dropin(_proj_ext(env), source=src)
    assert main(["extensions", "trust", str(path)]) == 1
    captured = capsys.readouterr()
    assert _sha(path) in captured.out
    assert "x39 = 39" in captured.out and "x40 = 40" not in captured.out
    assert "--yes" in captured.err
    assert not (_home(env) / "trusted_extensions.json").exists()
    assert main(["extensions", "trust", str(path), "--yes"]) == 0
    data = json.loads((_home(env) / "trusted_extensions.json").read_text(encoding="utf-8"))
    entry = data["trusted"][_sha(path)]
    assert entry["path"] == os.path.realpath(path)
    assert isinstance(entry["ts"], float | int)
    assert [d.status for d in _load(env).dropins] == ["trusted"]


def test_cli_trust_escapes_control_characters(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = _dropin(_proj_ext(env), source="s = '\x1b]0;pwn\x07'\n")
    main(["extensions", "trust", str(path)])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\\x1b" in out


def test_cli_trust_errors(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extensions", "trust", str(_proj(env) / "missing.py"), "--yes"]) == 2
    assert "missing.py" in capsys.readouterr().err


@posix_only
def test_cli_trust_refuses_symlink(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real, _ = _dropin(tmp_path / "x")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    assert main(["extensions", "trust", str(link), "--yes"]) == 2
    assert "symlink" in capsys.readouterr().err


def test_cli_untrust_by_path_and_sha(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    a, _ = _dropin(_home(env) / "extensions", "a.py")
    b, _ = _dropin(_home(env) / "extensions", "b.py", body="'b'")
    sha_a = extensions.trust(a, _home(env), env)
    sha_b = extensions.trust(b, _home(env), env)
    assert main(["extensions", "untrust", str(a)]) == 0
    assert main(["extensions", "untrust", sha_b]) == 0
    assert main(["extensions", "untrust", sha_b]) == 1
    data = json.loads((_home(env) / "trusted_extensions.json").read_text(encoding="utf-8"))
    assert sha_a not in data["trusted"] and sha_b not in data["trusted"]
    capsys.readouterr()


def test_untrust_path_of_edited_file_by_recorded_path(env: dict[str, str]) -> None:
    a, _ = _dropin(_home(env) / "extensions")
    sha = extensions.trust(a, _home(env), env)
    a.write_text("changed = 1\n", encoding="utf-8")
    assert extensions.untrust(str(a), _home(env), env) == 1
    assert sha not in extensions.trusted_hashes(_home(env), env)


# --- hook integration ---------------------------------------------------------------------


def test_hook_uses_trusted_override(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.events import ToolCall
    from token_sparen.mechanisms.compression import compress_text

    body = "lines[0] + ' [ext]'"
    path, _ = _dropin(_home(env) / "extensions", handles=("text", "log"), body=body)
    extensions.trust(path, _home(env), env)
    write_user_config(
        _home(env),
        {"extensions": {"allow_override": True}, "compression": {"allow_ctypes": ["text"]}},
    )  # both widen: the user's own file (plain text is opt-in)
    run_env = dict(env)
    text = "".join(f"plain line number {i} with some padding words here\n" for i in range(3000))
    cfg = load_config(_proj(env), run_env)
    out, info = compress_text(
        text,
        ToolCall(name="Bash", category="shell", input={"command": "gen"}, use_id=None),
        cfg,
        _proj(env),
        run_env,
    )
    assert info is not None
    assert out.startswith("plain line number 0 with some padding words here [ext]")


# --- conformance kit ----------------------------------------------------------------------


class _Base:
    name = "probe"
    handles = frozenset({"table"})


class _DropsErrors(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines = [x for x in text.split("\n") if "ERROR" not in x and "Error" not in x]
        return CompressResult("\n".join(lines[:3]), 3, max(len(text.split("\n")), 3), False)


class _Raises(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        if not text or "\x00" in text:
            raise ValueError("odd input")
        return TextCompressor().compress(text, level, ctx)


class _Reverses(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines = text.split("\n")
        return CompressResult("\n".join(reversed(lines)), len(lines), len(lines), False)


class _Grows(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        n = len(text.split("\n"))
        return CompressResult(text + text, n + 1, n, False)


class _BadShape:
    name = 3
    handles = ["table"]

    def compress(self, text: str, level: Level, ctx: CompressContext) -> Any:
        return {"text": text}


class _HugeTotal(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        r = TextCompressor().compress(text, level, ctx)
        return CompressResult(r.text, r.kept_lines, 10**9, r.lossless)


class _InflatedKept(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        r = TextCompressor().compress(text, level, ctx)
        return CompressResult(r.text, r.total_lines, r.total_lines, False)


class _WithMarker(_Base):
    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        r = TextCompressor().compress(text, level, ctx)
        return CompressResult(r.text + "\n[token-sparen: kept 1 of 2 lines]", 1, 2, False)


@pytest.mark.parametrize(
    ("comp", "needle"),
    [
        (_DropsErrors(), "error line"),
        (_Raises(), "raised"),
        (_Reverses(), "order"),
        (_Grows(), "kept_lines"),
        (_Grows(), "longer"),
        (_BadShape(), "CompressResult"),
        (_BadShape(), "handles"),
        (_WithMarker(), "marker"),
        (_HugeTotal(), "total_lines"),
        (_InflatedKept(), "kept_lines"),
    ],
)
def test_conformance_catches_bad_compressors(comp: Any, needle: str) -> None:
    issues = conformance.check_compressor(comp)
    assert issues
    assert any(needle in issue for issue in issues), issues


# --- interface version ---------------------------------------------------------------------


def test_extension_api_version_constant() -> None:
    import token_sparen

    assert token_sparen.EXTENSION_API_VERSION == "1.0"


@pytest.mark.parametrize(
    ("declared", "ok"),
    [
        (None, True),
        ("1.x", True),
        ("1.0", True),
        ("1", True),
        ("1.5", False),  # newer minor than the host supports
        ("1.x.2", False),
        ("2.0", False),
        ("0.9", False),
        ("x.1", False),
        (2, False),
        ("", False),
    ],
)
def test_api_compatible(declared: Any, ok: bool) -> None:
    assert extensions.api_compatible(declared)[0] is ok


def test_dropin_with_incompatible_api_is_skipped(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    ext = _home(env) / "extensions"
    good_src = _DROPIN.format(
        sentinel=str(ext.parent / "g.imported"), handles={"table"}, body="'g'"
    )
    good_src = good_src.replace(
        '{"compressors": [FirstLine]}', '{"api": "1.x", "compressors": [FirstLine]}'
    )
    good, _ = _dropin(ext, "a_good.py", source=good_src)
    bad_src = _DROPIN.format(sentinel=str(ext.parent / "b.imported"), handles={"log"}, body="'b'")
    bad_src = bad_src.replace(
        '{"compressors": [FirstLine]}', '{"api": "2.0", "compressors": [FirstLine]}'
    )
    bad, bad_sentinel = _dropin(ext, "b_future.py", source=bad_src)
    for p in (good, bad):
        extensions.trust(p, _home(env), env)
    loaded = _load(env)
    statuses = {d.path.name: d.status for d in loaded.dropins}
    assert statuses["a_good.py"] == "trusted"
    assert statuses["b_future.py"] == "skipped:requires api 2.0 (host 1.0)"
    assert loaded.compressor_for("table").compress("x\ny", LEVEL, CTX).text == "g"
    assert loaded.compressor_for("log").name != "first-line"
    assert not bad_sentinel.exists()  # never executed
    extensions.reset()
    assert main(["extensions", "list"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "b_future.py" in ln)
    assert "requires api 2.0" in line


def test_dynamic_incompatible_api_disabled_at_import(env: dict[str, str]) -> None:
    src = "V = '3' + '.0'\nTOKEN_SPAREN_EXTENSION = {'api': V, 'signals': [print]}\n"
    path, _ = _dropin(_home(env) / "extensions", source=src)
    extensions.trust(path, _home(env), env)
    loaded = _load(env)
    assert loaded.signals() == []
    assert any("api" in e["error"] and "3.0" in e["error"] for e in _logged(env))


def test_entry_point_incompatible_api_skipped(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    mod = types.ModuleType("fake_future_ext")
    mod.TOKEN_SPAREN_EXTENSION = {"api": "2.1"}  # type: ignore[attr-defined]

    def future_signal(record: Any) -> float:
        return 0.0

    def current_signal(record: Any) -> float:
        return 1.0

    future_signal.__module__ = "fake_future_ext"
    monkeypatch.setitem(sys.modules, "fake_future_ext", mod)
    groups = {
        "token_sparen.signals": [_EPObj("future", future_signal), _EPObj("now", current_signal)]
    }
    monkeypatch.setattr(extensions, "_entry_points", lambda group: groups.get(group, []))
    assert _load(env).signals() == [current_signal]
    assert any("future" in e["error"] and "2.1" in e["error"] for e in _logged(env))


def test_newer_minor_api_reason() -> None:
    assert extensions.api_compatible("1.5") == (False, "requires api 1.5 (host 1.0)")


class _Dist:
    def __init__(self, root: Path, files: list[str]) -> None:
        self._root = root
        self.files = [Path(f) for f in files]

    def locate_file(self, f: Any) -> Path:
        return self._root / f


def test_entry_point_api_status_from_metadata(
    env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = tmp_path / "site"
    (site / "newpkg").mkdir(parents=True)
    sentinel = tmp_path / "newpkg.imported"
    (site / "newpkg" / "comp.py").write_text(
        f"open({sentinel.as_posix()!r}, 'w').close()\n"
        "TOKEN_SPAREN_EXTENSION = {'api': '1.5'}\nclass C: pass\n",
        encoding="utf-8",
    )
    (site / "okpkg.py").write_text("TOKEN_SPAREN_EXTENSION = {'api': '1.x'}\n", encoding="utf-8")
    new = _EPObj("new", None, "newpkg.comp:C")
    new.dist = _Dist(site, ["newpkg/__init__.py", "newpkg/comp.py"])  # type: ignore[attr-defined]
    ok = _EPObj("ok", None, "okpkg:C")
    ok.dist = _Dist(site, ["okpkg.py"])  # type: ignore[attr-defined]
    nodist = _EPObj("mystery", None, "gone.mod:C")
    groups = {"token_sparen.compressors": [new, ok, nodist]}
    monkeypatch.setattr(extensions, "_entry_points", lambda group: groups.get(group, []))
    assert main(["extensions", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    line = {name: next(ln for ln in out if name in ln) for name in ("new", "okpkg", "mystery")}
    assert "requires api 1.5" in line["new"]
    assert "api ok" in line["okpkg"]
    assert "unknown (checked at load)" in line["mystery"]
    assert not sentinel.exists()  # listing imports nothing
