"""Upgrade path: state written by every 0.x milestone loads (or is quarantined) on this code,
and a file written by a newer token-sparen is never rewritten (no downgrade).

Fixtures: ``tests/fixtures/state/<tag>/`` were produced by ``scripts/make_state_fixtures.py``
(each tag's own hooks and CLI driven on synthetic input); ``legacy`` holds hand-written shapes
of intermediate 0.x commits. Paths in them are placeholders (``<project>``, ``<home>``, ...).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sampleoutputs import big_pytest

from token_sparen.adapters.claude import entry
from token_sparen.cli import main as cli_main
from token_sparen.facts import miners
from token_sparen.learn import hierarchy
from token_sparen.mechanisms import prune
from token_sparen.store import jsonstore, paths, schema

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "state"
REMOTE = "https://example.invalid/fixtures/demo.git"
KEY = "8ca332a20e230ac8"  # project key of REMOTE
TAGS = ("v0.1.0-m1", "v0.2.0-m2", "v0.3.0-m3", "v0.4.0-m4")
SID = "upgrade-s3"


def _git_project(project: Path) -> None:
    project.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", REMOTE]):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def _copy(src: Path, dest: Path, subst: dict[str, str]) -> None:
    if not src.is_dir():
        return
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            target.write_bytes(data)
            continue
        for old, new in subst.items():
            text = text.replace(old, json.dumps(new)[1:-1] if path.suffix != ".sh" else new)
        target.write_bytes(text.encode("utf-8"))
        if path.suffix == ".sh":
            os.chmod(target, 0o755)


def _install(tag: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    where = {n: tmp / n for n in ("home", "proj", "claude", "transcripts")}
    _git_project(where["proj"])
    where["home"].mkdir(mode=0o700)
    where["claude"].mkdir()
    where["transcripts"].mkdir()
    subst = {
        "<project>": str(where["proj"]),
        "<home>": str(where["home"]),
        "<claude>": str(where["claude"]),
        "<transcripts>": str(where["transcripts"]),
        "<plugin>": str(ROOT),
        "<tmp>": str(tmp),
    }
    src = FIXTURES / tag
    _copy(src / "home", where["home"], subst)
    _copy(src / "project", where["proj"] / ".token-sparen", subst)
    _copy(src / "claude", where["claude"], subst)
    if not (where["home"] / "config.toml").is_file():
        (where["home"] / "config.toml").write_text(
            "[adapter]\nlatency_budget_ms = 5000\n", encoding="utf-8"
        )
    for name, value in (
        ("TOKEN_SPAREN_HOME", where["home"]),
        ("CLAUDE_PROJECT_DIR", where["proj"]),
        ("CLAUDE_CONFIG_DIR", where["claude"]),
    ):
        monkeypatch.setenv(name, str(value))
    monkeypatch.chdir(where["proj"])
    return where


def _env(where: dict[str, Path]) -> dict[str, str]:
    return {
        "TOKEN_SPAREN_HOME": str(where["home"]),
        "CLAUDE_PROJECT_DIR": str(where["proj"]),
        "CLAUDE_CONFIG_DIR": str(where["claude"]),
    }


def _hook(event: str, payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    out = io.StringIO()
    rc = entry.main(event, io.StringIO(json.dumps(payload)), out, env, exit_fn=lambda _: None)
    assert rc == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, out.getvalue()
    doc: dict[str, Any] = json.loads(lines[0])
    return doc


def _session(where: dict[str, Path]) -> None:
    """One SessionStart + PostToolUse + SessionEnd on the current code, then the session's
    fold (SessionEnd only spawns it)."""
    env = _env(where)
    transcript = where["transcripts"] / f"{SID}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "run the tests"}})
        + "\n",
        encoding="utf-8",
    )
    base = {"session_id": SID, "cwd": str(where["proj"]), "transcript_path": str(transcript)}
    _hook("SessionStart", {**base, "hook_event_name": "SessionStart", "source": "startup"}, env)
    text = big_pytest(12_000)
    post = {
        **base,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -v"},
        "tool_use_id": "tu-upgrade-1",
        "tool_response": {"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
    }
    _hook("PostToolUse", post, env)
    _hook("SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "other"}, env)
    rc = cli_main(["fold", "--session", SID, "--project", str(where["proj"]), "--prune-apply"])
    assert rc == 0


def _errors(home: Path) -> list[dict[str, Any]]:
    path = home / "logs" / "errors.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _json_docs(home: Path) -> dict[Path, bytes]:
    return {
        p.relative_to(home): p.read_bytes()
        for p in sorted(home.rglob("*.json"))
        if p.is_file() and ".corrupt-" not in p.name
    }


def _nodes(doc: Any) -> set[tuple[str, str, str]]:
    points = doc.get("points") if isinstance(doc, dict) else None
    if not isinstance(points, dict):
        return set()
    out = set()
    for point, buckets in points.items():
        if isinstance(buckets, dict):
            for bucket, arms in buckets.items():
                if isinstance(arms, dict):
                    out |= {(point, bucket, arm) for arm in arms}
    return out


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def test_fixture_project_key_is_stable(tmp_path: Path) -> None:
    _git_project(tmp_path / "p")
    assert paths.project_key(tmp_path / "p") == KEY


def test_fixtures_hold_no_absolute_paths() -> None:
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in ("/Users/", "/private/", "/var/folders/", "/tmp/", "\\Users\\"):
                assert needle not in text, f"{path.relative_to(FIXTURES)}: {needle}"


@pytest.mark.parametrize("tag", [*TAGS, "legacy"])
def test_milestone_state_loads_or_quarantines(
    tag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    where = _install(tag, tmp_path, monkeypatch)
    home = where["home"]
    before = _json_docs(home)
    assert before, "fixture is empty"
    baseline = len(_errors(home))  # lines the milestone itself logged (e.g. the floor warning)
    _session(where)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    errors = _errors(home)[baseline:]
    assert not any("Traceback" in json.dumps(e) for e in errors), errors
    after = _json_docs(home)
    for rel in before:
        path = home / rel
        if rel in after:
            try:
                doc = json.loads(after[rel].decode("utf-8"))
            except ValueError:  # still unreadable (never rewritten): it must have been logged
                assert any(rel.name in json.dumps(e) for e in errors), rel
                continue
            assert isinstance(doc, dict), rel
        else:  # moved aside: quarantined with a log line
            assert list(path.parent.glob(f"{path.name}.corrupt-*")), rel
            assert any(rel.name in json.dumps(e) for e in errors), rel
    # only quarantine / older-version lines, nothing else went wrong
    for e in errors:
        text = json.dumps(e)
        assert "corrupt" in text or "version" in text, e
    if tag != "legacy":
        assert errors == [], errors


@pytest.mark.parametrize("tag", TAGS)
def test_milestone_state_is_kept(tag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing learned is lost: evidence nodes, facts, prune opt-in and records, the statusline
    install record, session files, the secret and the recovery files all survive."""
    where = _install(tag, tmp_path, monkeypatch)
    home, pdir = where["home"], where["home"] / "projects" / KEY
    kept = {
        "secret": (home / "secret").read_bytes() if (home / "secret").is_file() else None,
        "statusline": _load(home / "statusline_install.json"),
        "post": _nodes(_load(pdir / "posteriors.json")),
        "global": _nodes(_load(home / "global" / "posteriors.json")),
        "facts": set((_load(pdir / "facts.json") or {}).get("facts", {})),
        "prune": _load(pdir / "prune.json"),
        "sessions": {p.name for p in (pdir / "sessions").glob("*.json")},
        "outputs": {p.name for p in (where["proj"] / ".token-sparen" / "outputs").glob("*")},
        "trust": _load(home / "trusted_extensions.json"),
        "floor": _load(pdir / "floor_warned.json"),
        "open": _load(pdir / "sessions" / "fixture-s3.json"),
    }
    _session(where)
    # user settings and bookkeeping: trusted drop-ins, the floor warning record
    assert kept["trust"] and _load(home / "trusted_extensions.json") == kept["trust"]
    assert kept["floor"] and _load(pdir / "floor_warned.json") == kept["floor"]
    # a session left with its turn open keeps its overlay (loop-guard turn state included)
    if kept["open"] is not None:
        now = _load(pdir / "sessions" / "fixture-s3.json")
        assert now is not None
        assert {k: v for k, v in kept["open"].items() if k == "loopguard"} == {
            k: v for k, v in now.items() if k == "loopguard"
        }
    if kept["secret"] is not None:
        assert (home / "secret").read_bytes() == kept["secret"]
    assert _load(home / "statusline_install.json") == kept["statusline"]
    assert kept["post"] <= _nodes(_load(pdir / "posteriors.json"))
    assert kept["global"] <= _nodes(_load(home / "global" / "posteriors.json"))
    assert kept["facts"] <= set((_load(pdir / "facts.json") or {}).get("facts", {}))
    if kept["prune"] is not None:
        assert _load(pdir / "prune.json")["enabled"] is kept["prune"]["enabled"]
    assert kept["sessions"] <= {p.name for p in (pdir / "sessions").glob("*.json")}
    outputs = where["proj"] / ".token-sparen" / "outputs"
    assert kept["outputs"] <= {p.name for p in outputs.glob("*")}
    # files that never had a schema version get none added (absent = version 1)
    for name in ("breaker.json", "pretrim.json", "last_active.json", "pending.json"):
        doc = _load(pdir / name)
        if doc is not None:
            assert "version" not in doc, name


def test_legacy_shapes_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = _install("legacy", tmp_path, monkeypatch)
    home, pdir = where["home"], where["home"] / "projects" / KEY
    _session(where)
    errors = json.dumps(_errors(home))
    # v1 posteriors: logged and started afresh as v2
    assert _load(pdir / "posteriors.json")["version"] == hierarchy.SCHEMA_VERSION
    assert "unsupported version 1" in errors
    # the flat global ``applied`` list becomes the per-project table
    assert isinstance(_load(home / "global" / "posteriors.json")["applied"], dict)
    # the v1 prune store's per-checkout records moved to the registry; opt-in kept
    doc = _load(pdir / "prune.json")
    assert doc["version"] == prune.VERSION and doc["enabled"] is True and "files" not in doc
    assert "settings-a" in _load(home / prune.REGISTRY_FILE)["files"]
    # ... after a copy of the old document is kept beside it
    legacy = json.loads(
        (FIXTURES / "legacy" / "home" / "projects" / KEY / "posteriors.json").read_text()
    )
    assert _load(pdir / "posteriors.v1.bak.json") == legacy
    # a truncated document is quarantined (moved aside) or left alone, always logged
    assert "pretrim.json" in errors
    assert _load(pdir / "facts.json")["disabled"] is True


# --------------------------------------------------------------------------- no downgrade
VERSIONED = {
    "projects/{key}/posteriors.json": hierarchy.SCHEMA_VERSION,
    "global/posteriors.json": hierarchy.SCHEMA_VERSION,
    "projects/{key}/facts.json": miners.VERSION,
    "projects/{key}/prune.json": prune.VERSION,
    "projects/{key}/savings_scale.json": 1,
    prune.REGISTRY_FILE: 1,
    "statusline_install.json": 1,
}


def test_schema_registry_matches_the_modules() -> None:
    known = schema.KNOWN_VERSIONS
    for rel, version in VERSIONED.items():
        assert known[Path(rel).name] == version, rel


def test_older_reader_never_downgrades_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    where = _install("v0.4.0-m4", tmp_path, monkeypatch)
    home = where["home"]
    bumped: dict[Path, bytes] = {}
    for rel, version in VERSIONED.items():
        path = home / rel.format(key=KEY)
        doc = _load(path) or {}
        doc["version"] = version + 1
        doc["from_the_future"] = {"keep": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        bumped[path] = path.read_bytes()
    claude_settings = where["claude"] / "settings.json"
    settings_bytes = claude_settings.read_bytes()
    _session(where)
    fact = next(iter((_load(home / "projects" / KEY / "facts.json") or {}).get("facts", {})), "")
    for argv in (
        ["facts", "disable"],
        ["facts", "enable"],
        ["facts", "forget", fact or "0" * 12],
        ["prune", "disable", "--project", str(where["proj"])],
        ["prune", "enable", "--project", str(where["proj"])],
        ["statusline", "uninstall"],
        ["statusline", "install"],
        ["fold"],
    ):
        assert cli_main(argv) in (0, 1, 2), argv
    out = capsys.readouterr()
    assert "Traceback" not in out.err + out.out
    for path, data in bumped.items():
        assert path.read_bytes() == data, path.relative_to(home)
        assert not list(path.parent.glob(f"{path.name}.corrupt-*")), path
    # `statusline uninstall` still reverted the settings file (recognised by the launcher):
    # it is back to what it was before the install, and `install` refused the newer record
    assert "statusLine" not in json.loads(claude_settings.read_text(encoding="utf-8"))
    assert json.loads(claude_settings.read_bytes()) == json.loads(
        (where["claude"] / "settings.json.token-sparen.bak").read_bytes()
    )
    assert settings_bytes != claude_settings.read_bytes()
    errors = json.dumps(_errors(home))
    assert "newer" in errors


def test_newer_version_refused_by_read_locked(tmp_path: Path) -> None:
    path = tmp_path / "posteriors.json"
    path.write_text(json.dumps({"version": 99, "points": {}}), encoding="utf-8")
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    with pytest.raises(jsonstore.NewerSchema):
        jsonstore.update(path, lambda d: {"version": 2}, {}, env)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 99
    # lock-free readers still see it (they check the version themselves)
    assert jsonstore.read(path, {}, env)["version"] == 99
    # an unversioned or unregistered file is never refused
    other = tmp_path / "breaker.json"
    other.write_text(json.dumps({"version": 99}), encoding="utf-8")
    jsonstore.update(other, lambda d: d, {}, env)


def test_fixture_generator_is_importable() -> None:
    """The generator stays runnable (its constants match the tests)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_state_fixtures", ROOT / "scripts" / "make_state_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TAGS == TAGS and mod.REMOTE == REMOTE
    assert shutil.which("git") is not None


# --------------------------------------------------------------------------- fix round 1
def _bump(path: Path, version: int, writer: str = "1.3.0") -> bytes:
    doc = _load(path) or {}
    doc.update({"version": version, schema.WRITTEN_BY: writer, "from_the_future": True})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path.read_bytes()


def test_fold_isolates_a_newer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """I-1: a newer global store skips only the global step; the project store, facts and
    pretrim still fold, the session stays pending (bounded) and a re-fold does not re-apply."""
    from token_sparen.learn import overlay, pending

    where = _install("v0.4.0-m4", tmp_path, monkeypatch)
    home, pdir = where["home"], where["home"] / "projects" / KEY
    gpath = home / "global" / "posteriors.json"
    gbytes = _bump(gpath, hierarchy.SCHEMA_VERSION + 1)
    applied_post = len(_load(pdir / "posteriors.json")["applied"])
    applied_facts = len(_load(pdir / "facts.json")["applied"])
    _session(where)
    assert gpath.read_bytes() == gbytes
    assert len(_load(pdir / "posteriors.json")["applied"]) == applied_post + 1
    assert len(_load(pdir / "facts.json")["applied"]) == applied_facts + 1
    assert SID in pending.entries(home, KEY, _env(where))  # retained for a later fold
    staged = _load(pdir / "sessions" / f"{SID}.json")[overlay.FOLDING_KEY]
    assert "global" not in staged[overlay.DONE_KEY] and "project" in staged[overlay.DONE_KEY]
    # a re-fold resumes after the completed steps: nothing is applied twice
    assert cli_main(["fold", "--session", SID, "--project", str(where["proj"])]) == 0
    assert len(_load(pdir / "posteriors.json")["applied"]) == applied_post + 1
    # bounded: past RETAIN_MAX pending sessions the fold clears it (evidence for the newer
    # store dropped)
    monkeypatch.setattr(overlay, "RETAIN_MAX", 0)
    assert cli_main(["fold", "--session", SID, "--project", str(where["proj"])]) == 0
    assert SID not in pending.entries(home, KEY, _env(where))
    assert overlay.FOLDING_KEY not in _load(pdir / "sessions" / f"{SID}.json")
    assert gpath.read_bytes() == gbytes


def _lines_about(home: Path, needle: str) -> int:
    return sum(needle in json.dumps(e) for e in _errors(home))


def test_store_problems_logged_once_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (ruling T4-c2): a newer or corrupt store is logged once per store per session,
    across the session's hook processes, not once per hook."""
    where = _install("v0.4.0-m4", tmp_path, monkeypatch)
    home, pdir = where["home"], where["home"] / "projects" / KEY
    _bump(home / "global" / "posteriors.json", hierarchy.SCHEMA_VERSION + 1)
    (pdir / "pretrim.json").write_text('{"x": [1,', encoding="utf-8")
    text = big_pytest(12_000)

    def post(sid: str, n: int) -> None:
        jsonstore._SEEN.clear()  # a fresh hook process
        payload = {
            "session_id": sid,
            "cwd": str(where["proj"]),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -v"},
            "tool_use_id": f"tu-{sid}-{n}",
            "tool_response": {"stdout": text, "stderr": "", "interrupted": False},
        }
        _hook("PostToolUse", payload, _env(where))

    for n in range(3):
        post("once-a", n)
    assert _lines_about(home, "global/posteriors.json") == 1
    assert _lines_about(home, "pretrim.json") == 1
    for n in range(2):
        post("once-b", n)  # another session: one more line each
    assert _lines_about(home, "global/posteriors.json") == 2
    assert _lines_about(home, "pretrim.json") == 2
    marker = home / "logs" / jsonstore.ONCE_FILE
    assert "once-a" not in marker.read_text(encoding="utf-8")  # no raw session id or path
    assert str(home) not in marker.read_text(encoding="utf-8")


def _deny(path: Path, rules: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"deny": rules}}), encoding="utf-8")


def test_downgrade_undo_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-3: with every store from a newer version, the opt-out commands still undo what the
    settings files identify, and otherwise say what to do; never a raw exception."""
    where = _install("v0.4.0-m4", tmp_path, monkeypatch)
    home, pdir, proj = where["home"], where["home"] / "projects" / KEY, where["proj"]
    bumped = {
        p: _bump(p, schema.KNOWN_VERSIONS[p.name] + 1)
        for p in (
            pdir / "prune.json",
            home / prune.REGISTRY_FILE,
            home / "statusline_install.json",
            pdir / "facts.json",
        )
    }
    local = proj / ".claude" / "settings.local.json"
    _deny(local, ["Bash(rm:*)", "mcp__mine", "mcp__unused", "NotebookEdit"])
    _deny(local.with_name(local.name + ".token-sparen.bak"), ["Bash(rm:*)", "mcp__mine"])
    capsys.readouterr()

    assert cli_main(["prune", "restore", "NotebookEdit", "--project", str(proj)]) == 0
    out = capsys.readouterr()
    assert "removed NotebookEdit" in out.out
    assert "state written by token-sparen 1.3.0" in out.err
    assert "run `token-sparen prune restore NotebookEdit` from that version" in out.err
    assert cli_main(["prune", "disable", "--project", str(proj)]) == 0
    out = capsys.readouterr()
    assert "removed mcp__unused" in out.out and "token-sparen prune disable" in out.err
    deny = json.loads(local.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert deny == ["Bash(rm:*)", "mcp__mine"]  # the user's rules (in the backup) stay

    assert cli_main(["statusline", "uninstall"]) == 0
    out = capsys.readouterr()
    assert "statusline removed" in out.out and "state written by token-sparen 1.3.0" in out.out
    assert "statusLine" not in json.loads((where["claude"] / "settings.json").read_text())

    assert cli_main(["facts", "disable"]) == 0
    out = capsys.readouterr()
    assert "already off" in out.out and "token-sparen facts disable" in out.err
    assert cli_main(["facts", "forget", "0" * 12]) == 1
    out = capsys.readouterr()
    assert "state written by token-sparen 1.3.0" in out.err and "NewerSchema" not in out.err
    assert "Traceback" not in out.err
    for path, data in bumped.items():
        assert path.read_bytes() == data, path


def test_downgrade_undo_without_backup_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    where = _install("v0.4.0-m4", tmp_path, monkeypatch)
    proj = where["proj"]
    _bump(where["home"] / "projects" / KEY / "prune.json", prune.VERSION + 1, writer="")
    local = proj / ".claude" / "settings.local.json"
    _deny(local, ["mcp__unused"])
    assert cli_main(["prune", "disable", "--project", str(proj)]) == 1
    out = capsys.readouterr()
    assert "no backup" in out.err and "a newer token-sparen" in out.err
    assert json.loads(local.read_text(encoding="utf-8"))["permissions"]["deny"] == ["mcp__unused"]


def test_prune_lockfree_reads_ignore_a_newer_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"TOKEN_SPAREN_HOME": str(home)}
    path = prune.store_path(home, KEY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "enabled": True, "tools": {"X": {}}}))
    reg = prune.registry_path(home)
    reg.write_text(json.dumps({"version": 99, "files": {"f": {"key": KEY, "owned": ["X"]}}}))
    doc = prune.read_doc(home, KEY, env)
    assert doc.get("enabled") is not True and doc["files"] == {}
    assert prune.pruned_count(home, KEY, env) == 0


def test_written_by_is_recorded(tmp_path: Path) -> None:
    import token_sparen

    path = tmp_path / "facts.json"
    jsonstore.update(path, lambda d: {"version": 1, "facts": {}}, {}, {})
    assert _load(path)[schema.WRITTEN_BY] == token_sparen.__version__
    other = tmp_path / "breaker.json"
    jsonstore.update(other, lambda d: {"x": 1}, {}, {})
    assert schema.WRITTEN_BY not in _load(other)
