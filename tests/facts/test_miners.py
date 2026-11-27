"""Deterministic fact miners over synthetic session transcripts (spec section 6.8)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from token_sparen.facts import lifecycle, miners
from token_sparen.facts.sanitize import Fact, fact_id
from token_sparen.transcript import ToolResultRecord

PROJECT = "/work/proj"


class Log:
    """Builds a synthetic Claude Code transcript: prompts, tool calls and their results."""

    def __init__(self) -> None:
        self.recs: list[dict[str, Any]] = []
        self.n = 0

    def prompt(self, text: str = "go") -> Log:
        self.recs.append({"type": "user", "message": {"role": "user", "content": text}})
        return self

    def call(
        self, tool: str, inp: dict[str, Any], out: str = "ok", err: bool = False, **rec: Any
    ) -> Log:
        self.n += 1
        tid = f"t{self.n}"
        use = {"type": "tool_use", "id": tid, "name": tool, "input": inp}
        self.recs.append(
            {"type": "assistant", "message": {"id": f"m{self.n}", "content": [use]}, **rec}
        )
        res = {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}
        self.recs.append({"type": "user", "message": {"content": [res]}, **rec})
        return self

    def bash(self, cmd: str, out: str = "ok", err: bool = False, **rec: Any) -> Log:
        return self.call("Bash", {"command": cmd}, out, err, **rec)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.recs), encoding="utf-8")
        return path


def _mine(
    tmp_path: Path, log: Log, sid: str = "s1", project: str = PROJECT, **kw: Any
) -> list[Fact]:
    path = log.write(tmp_path / f"{sid}.jsonl")
    return miners.mine_transcript(path, sid, project_dir=project, **kw)


def _node_project(tmp_path: Path, *deps: str) -> str:
    root = tmp_path / "nodeproj"
    root.mkdir(exist_ok=True)
    doc = {"devDependencies": {d: "1.0.0" for d in deps}}
    (root / "package.json").write_text(json.dumps(doc), encoding="utf-8")
    return str(root)


def _kinds(facts: list[Fact], kind: str) -> list[tuple[str, str]]:
    return [(f.subject, f.value) for f in facts if f.kind == kind]


# --------------------------------------------------------------------------- command_alias
def test_miner_command_alias(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("pytest tests -q", "zsh: command not found: pytest", err=True)
        .call("Read", {"file_path": f"{PROJECT}/setup.cfg"})
        .bash("python -m pytest tests -q", "3 passed")
        .bash("tsc --noEmit", "/bin/sh: 1: tsc: not found", err=True)
        .bash("npx tsc --noEmit")
        .bash("foo build", "bash: foo: command not found", err=True)
        .bash("./foo build")
    )
    facts = _mine(tmp_path, log, project=_node_project(tmp_path, "tsc"))
    assert sorted(_kinds(facts, "command_alias")) == [
        ("foo", "./foo"),
        ("pytest", "python -m pytest"),
        ("tsc", "npx tsc"),
    ]
    sid = miners.session_digest("s1")
    assert all(f.evidence_sessions == frozenset({sid}) for f in facts)


def test_miner_command_alias_module_and_windows_forms(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("python -m black src", "No module named black", err=True)
        .bash("uv run black src")
        .bash("tsc -b", "'tsc' is not recognized as an internal or external command", err=True)
        .bash("npx tsc -b")
    )
    facts = _mine(tmp_path, log, project=_node_project(tmp_path, "tsc"))
    assert sorted(_kinds(facts, "command_alias")) == [
        ("python -m black", "uv run black"),
        ("tsc", "npx tsc"),
    ]


def test_miner_command_alias_requires_exact_tail_same_turn_and_window(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("pytest tests", "command not found: pytest", err=True)
        .bash("python -m pytest other")  # argv tail differs
        .prompt()
        .bash("tsc", "command not found: tsc", err=True)
        .prompt()
        .bash("npx tsc")  # next turn
        .bash("jest a", "command not found: jest", err=True)
        .bash("ls")
        .bash("ls")
        .bash("ls")
        .bash("npx jest a")  # 4th call after the failure
        .bash("vite b", "command not found: vite", err=True)
        .bash("npx vite b", "boom", err=True)  # the retry failed too
        .bash("rg x", "command not found: rg", err=True)
        .bash("echo rg x")  # different intent: X must be in the new head
    )
    assert _kinds(_mine(tmp_path, log), "command_alias") == []


# --------------------------------------------------------------------------- requires_env
def test_miner_requires_env(tmp_path: Path) -> None:
    secret = "s3cr3t-value-never-stored"
    log = (
        Log()
        .prompt()
        .bash("npm test", "Error: Environment variable DATABASE_URL is missing", err=True)
        .bash(f"DATABASE_URL={secret} npm test", "ok")
        .bash("python app.py", "KeyError: 'API_TOKEN'", err=True)
        .bash(f"export API_TOKEN={secret}")
        .bash("python app.py")
        .bash("make run", "REDIS_HOST is not set", err=True)
        .bash(f"export REDIS_HOST={secret} && make run")
        .bash("make x", "missing environment variable: Lower_case", err=True)
        .bash(f"Lower_case={secret} make x")
    )
    facts = _mine(tmp_path, log)
    assert sorted(_kinds(facts, "requires_env")) == [
        ("make run", "REDIS_HOST"),
        ("npm test", "DATABASE_URL"),
        ("python", "API_TOKEN"),
    ]
    assert all(secret not in f.value and secret not in f.subject for f in facts)


def test_miner_requires_env_needs_the_variable_set(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("npm test", "DATABASE_URL is not set", err=True)
        .bash("npm test")  # passed without setting it (flaky): no fact
        .bash("make", "KeyError: 'HOME_DIR'", err=True)
        .prompt()
        .bash("export HOME_DIR=x")
        .bash("make")  # export in another turn
    )
    assert _kinds(_mine(tmp_path, log), "requires_env") == []


# --------------------------------------------------------------------------- test_command
def test_miner_test_command(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("python -m pytest tests -q", "1 failed", err=True)
        .bash("python -m pytest tests -q", "1 failed", err=True)
        .bash("python -m pytest tests -q")
        .bash("pytest tests/test_a.py")
        .bash("pytest tests/test_a.py")
        .bash("ls -la")
        .bash("ls -la")
        .bash("ls -la")
    )
    assert _kinds(_mine(tmp_path, log), "test_command") == [("tests", "pytest")]


def test_miner_test_command_skips_unsafe_commands(tmp_path: Path) -> None:
    log = Log().prompt()
    for _ in range(3):
        log.bash("pytest -q | tail -5 && echo done")
    log.bash("go test ./...")
    assert _kinds(_mine(tmp_path, log), "test_command") == [("tests", "go test ./...")]


# --------------------------------------------------------------------------- build_first
def test_miner_build_first(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("npm test", "Cannot find module '../dist/index.js'", err=True)
        .bash("npm run build")
        .bash("npm test", "12 passing")
        .bash("cargo test", "error", err=True)
        .bash("cargo build")
        .bash("ls")
        .bash("cargo test")  # the build was not immediately before
    )
    assert _kinds(_mine(tmp_path, log), "build_first") == [("npm test", "npm run build")]


# --------------------------------------------------------------------------- generated_dir
def test_miner_generated_dir(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for rel in ("src/gen/a.py", "src/gen/b.py", "dist/x.js", "docs/readme.md", "docs/other.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# @generated by a tool\n", encoding="utf-8")
    log = (
        Log()
        .prompt()
        .call("Write", {"file_path": f"{root}/src/gen/a.py", "content": "x"})
        .call("Edit", {"file_path": f"{root}/src/gen/b.py"})
        .call("Write", {"file_path": f"{root}/dist/x.js"})  # once only
        .call("Write", {"file_path": "/elsewhere/build/y.js"})  # outside the project
        .call("Write", {"file_path": "/elsewhere/build/z.js"})
        .call("Edit", {"file_path": f"{root}/docs/readme.md"})  # not a generated dir name
        .call("Edit", {"file_path": f"{root}/docs/other.md"})
    )
    got = _mine(tmp_path, log, project=str(root))
    assert _kinds(got, "generated_dir") == [("generated", "src/gen")]


# --------------------------------------------------------------------------- evidence store
def test_fact_requires_two_sessions(tmp_path: Path) -> None:
    pdir = tmp_path / "proj"
    log = Log().prompt().bash("pytest", "command not found: pytest", err=True)
    log.bash("python -m pytest")
    one = _mine(tmp_path, log, "s1")
    miners.record(pdir, one, now=1.0, env={})
    assert miners.eligible(miners.load(pdir)) == []
    miners.record(pdir, one, now=2.0, env={})  # the same session again: still one
    assert miners.eligible(miners.load(pdir)) == []
    miners.record(pdir, _mine(tmp_path, log, "s2"), now=3.0, env={})
    got = miners.eligible(miners.load(pdir))
    kinds = {f.kind for f in got}
    assert "command_alias" in kinds
    alias = next(f for f in got if f.kind == "command_alias")
    assert alias.evidence_sessions == {miners.session_digest("s1"), miners.session_digest("s2")}


def test_facts_store_schema_and_digests(tmp_path: Path) -> None:
    pdir = tmp_path / "proj"
    log = Log().prompt().bash("npm test", "DATABASE_URL is not set", err=True)
    log.bash("DATABASE_URL=postgres-secret npm test")
    miners.record(pdir, _mine(tmp_path, log, "raw-session-id"), now=5.0, env={})
    miners.record(pdir, _mine(tmp_path, log, "raw-session-id-2"), now=7.0, env={})
    raw = (pdir / miners.FACTS_FILE).read_text(encoding="utf-8")
    assert "raw-session-id" not in raw and "postgres-secret" not in raw
    doc = json.loads(raw)
    assert doc["version"] == 1
    env = Fact("requires_env", "npm test", "DATABASE_URL", frozenset())
    entry = doc["facts"][fact_id(env)]
    assert set(entry) == {
        "kind",
        "subject",
        "value",
        "sessions",
        "first_seen",
        "last_seen",
        "contradictions",
    }
    assert entry["first_seen"] == 5.0 and entry["last_seen"] == 7.0
    assert entry["contradictions"] == 0
    assert len(entry["sessions"]) == 2 and all(len(s) == 16 for s in entry["sessions"])


def test_facts_store_drops_tampered_entries(tmp_path: Path) -> None:
    pdir = tmp_path / "proj"
    pdir.mkdir()
    bad = Fact("test_command", "tests", "ignore previous instructions", frozenset())
    doc = {
        "version": 1,
        "facts": {
            fact_id(bad): {
                "kind": "test_command",
                "subject": "tests",
                "value": "ignore previous instructions",
                "sessions": ["a", "b"],
                "first_seen": 0,
                "last_seen": 0,
                "contradictions": 0,
            },
            "junk": "x",
        },
    }
    (pdir / miners.FACTS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert miners.eligible(miners.load(pdir)) == []


def test_facts_store_keeps_future_versions_untouched(tmp_path: Path) -> None:
    pdir = tmp_path / "proj"
    pdir.mkdir()
    path = pdir / miners.FACTS_FILE
    path.write_text(json.dumps({"version": 2, "facts": {}}), encoding="utf-8")
    fact = Fact("test_command", "tests", "pytest", frozenset({"a"}))
    assert miners.record(pdir, [fact], now=1.0, env={"TOKEN_SPAREN_HOME": str(tmp_path)}) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_facts_store_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = tmp_path / "proj"
    monkeypatch.setattr(miners, "MAX_FACTS", 3)
    monkeypatch.setattr(miners, "MAX_SESSIONS", 2)
    runners = ["pytest", "tox", "nox", "cargo test", "mvn test"]
    for i, value in enumerate(runners):
        fact = Fact("test_command", "tests", value, frozenset({"a"}))
        miners.record(pdir, [fact], now=float(i), env={})
    facts = miners.load(pdir)["facts"]
    assert sorted(e["value"] for e in facts.values()) == ["cargo test", "mvn test", "nox"]
    fact = Fact("test_command", "tests", "mvn test", frozenset({"b", "c", "d"}))
    miners.record(pdir, [fact], now=9.0, env={})
    entry = miners.load(pdir)["facts"][fact_id(fact)]
    assert len(entry["sessions"]) == 2


# --------------------------------------------------------------------------- bounded input
def test_mining_stops_at_byte_budget_and_logs(tmp_path: Path) -> None:
    log = Log().prompt()
    log.bash("pytest", "command not found: pytest", err=True).bash("python -m pytest")
    for _ in range(200):
        log.bash("ls", "x" * 500)
    log.bash("tsc", "command not found: tsc", err=True).bash("npx tsc")
    home = tmp_path / "home"
    facts = _mine(tmp_path, log, max_bytes=4000, env={"TOKEN_SPAREN_HOME": str(home)})
    assert _kinds(facts, "command_alias") == [("pytest", "python -m pytest")]
    errors = (home / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "byte budget" in errors


def test_mining_ignores_sidechains_and_bad_input(tmp_path: Path) -> None:
    log = Log().prompt()
    log.bash("pytest", "command not found: pytest", err=True, isSidechain=True)
    log.bash("python -m pytest", isSidechain=True)
    log.call("Bash", {"command": 3}).call("Write", {"file_path": 3})
    log.recs.append({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
    path = log.write(tmp_path / "s.jsonl")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert miners.mine_transcript(path, "s", project_dir=PROJECT) == []
    assert miners.mine_transcript(tmp_path / "missing.jsonl", "s", project_dir=None) == []


def test_session_digest_is_blake2b_8() -> None:
    d = miners.session_digest("abc")
    assert len(d) == 16 and d != "abc" and d == miners.session_digest("abc")


# --------------------------------------------------------------------------- fix round 1
def test_requires_env_and_build_first_keep_program_and_subcommand_only(tmp_path: Path) -> None:
    log = (
        Log()
        .prompt()
        .bash("mysql -u admin -pS3cretPw shop", "MYSQL_HOST is not set", err=True)
        .bash("MYSQL_HOST=db mysql -u admin -pS3cretPw shop")
        .bash("make test ARGS=x1", "1 failed", err=True)
        .bash("make build OPT=fast")
        .bash("make test ARGS=x1")
        .bash("npm test -- --grep slow", "failed", err=True)
        .bash("npm run build -- --prod")
        .bash("npm test -- --grep slow")
    )
    facts = _mine(tmp_path, log)
    assert _kinds(facts, "requires_env") == [("mysql", "MYSQL_HOST")]
    assert sorted(_kinds(facts, "build_first")) == [
        ("make test", "make build"),
        ("npm test", "npm run build"),
    ]
    assert all("S3cret" not in f.subject + f.value for f in facts)


def test_program_of_compound_command_is_empty() -> None:
    assert miners._program("cd sub && make test") == ""
    assert miners._program("python -m pytest -q") == "python -m pytest"
    assert miners._program("yarn run lint --fix") == "yarn run lint"
    assert miners._program("mysql -u x") == "mysql"


def _launcher_log() -> Log:
    log = Log().prompt()
    log.bash("tsc -b", "command not found: tsc", err=True).bash("npx tsc -b")
    log.bash("black src", "command not found: black", err=True).bash("uvx black src")
    log.bash("cowsay hi", "command not found: cowsay", err=True).bash("pnpm dlx cowsay hi")
    return log


def test_download_launcher_alias_needs_a_declared_dependency(tmp_path: Path) -> None:
    assert (
        _kinds(_mine(tmp_path, _launcher_log(), project=str(tmp_path / "none")), "command_alias")
        == []
    )
    root = Path(_node_project(tmp_path, "tsc"))
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2"]\n'
        '[project.optional-dependencies]\ndev = ["Black[d]==24.1"]\n',
        encoding="utf-8",
    )
    got = sorted(_kinds(_mine(tmp_path, _launcher_log(), project=str(root)), "command_alias"))
    assert got == [("black", "uvx black"), ("tsc", "npx tsc")]  # cowsay is not declared
    (root / "package.json").write_text("{not json", encoding="utf-8")
    (root / "pyproject.toml").write_text("not = [toml", encoding="utf-8")
    assert _kinds(_mine(tmp_path, _launcher_log(), project=str(root)), "command_alias") == []


def test_build_first_skips_side_effect_commands(tmp_path: Path) -> None:
    log = Log().prompt()
    log.bash("make test", "failed", err=True).bash("make install").bash("make test")
    log.bash("cargo test", "failed", err=True).bash("make publish").bash("cargo test")
    assert _kinds(_mine(tmp_path, log), "build_first") == []


def test_test_command_in_a_subdirectory_is_no_fact(tmp_path: Path) -> None:
    """Fix round 1 (I2): a directory-changing run may narrow the suite: no fact beats a narrow
    one, so only the plain run counts."""
    log = Log().prompt()
    for _ in range(2):
        log.bash("cd packages/api && npm test")
        log.bash("cd tests/unit && pytest")
    log.bash("npm test")
    assert _kinds(_mine(tmp_path, log), "test_command") == [("tests", "npm test")]
    only_cd = Log().prompt().bash("cd tests/unit && pytest").bash("cd tests/unit && pytest")
    assert _kinds(_mine(tmp_path, only_cd), "test_command") == []


def test_space_separated_credentials_never_become_a_test_command(tmp_path: Path) -> None:
    log = Log().prompt()
    for _ in range(3):
        log.bash("pytest --token hunter2 -q")
    log.bash("pytest -q")
    assert _kinds(_mine(tmp_path, log), "test_command") == [("tests", "pytest")]


def test_env_subject_drops_secret_looking_subcommands(tmp_path: Path) -> None:
    assert miners._program("tool hunter2pw x") == "tool"
    assert miners._program("tool s3cr3t") == "tool"
    assert miners._program("docker compose up") == "docker compose"
    log = Log().prompt()
    log.bash("tool hunter2pw", "API_HOST is not set", err=True)
    log.bash("API_HOST=x tool hunter2pw")
    assert _kinds(_mine(tmp_path, log), "requires_env") == [("tool", "API_HOST")]


# --------------------------------------------------------------------------- fix wave B
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path: Path, ignore: str = "") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text(ignore, encoding="utf-8")
    return root


def _edits(root: Path, *rels: str, content: str = "x = 1\n") -> Log:
    log = Log().prompt()
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.call("Edit", {"file_path": str(path)})
    return log


@needs_git
def test_generated_dir_never_from_edits_alone(tmp_path: Path) -> None:
    """M2: a tracked, marker-less ``build/`` the agent edits by hand is no generated dir."""
    root = _repo(tmp_path)
    log = _edits(root, "build/helpers.gradle", "build/docker.sh", "gen/api.go")
    assert _kinds(_mine(tmp_path, log, project=str(root)), "generated_dir") == []


@needs_git
def test_generated_dir_from_a_git_ignored_dir(tmp_path: Path) -> None:
    root = _repo(tmp_path, "dist/\n")
    log = _edits(root, "dist/a.js", "dist/b.js", "build/x.sh", "build/y.sh")
    assert _kinds(_mine(tmp_path, log, project=str(root)), "generated_dir") == [
        ("generated", "dist")
    ]


@needs_git
@pytest.mark.parametrize(
    "header",
    [
        "// Code generated by protoc-gen-go. DO NOT EDIT.\n",
        "# @generated by tool\n",
        "/* This file is auto-generated */\n",
        "\n\n\n\n// do not edit: produced by make gen\n",
    ],
)
def test_generated_dir_from_generated_markers(tmp_path: Path, header: str) -> None:
    root = _repo(tmp_path)
    log = _edits(root, "gen/a.go", "gen/b.go", content=header + "package gen\n")
    assert _kinds(_mine(tmp_path, log, project=str(root)), "generated_dir") == [
        ("generated", "gen")
    ]


@needs_git
def test_generated_markers_must_be_in_the_first_five_lines_of_every_edited_file(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    late = "\n" * 5 + "// DO NOT EDIT\n"
    log = _edits(root, "gen/a.go", content=late)
    log.recs.extend(_edits(root, "gen/b.go", content=late).recs)
    assert _kinds(_mine(tmp_path, log, project=str(root)), "generated_dir") == []
    mixed = _edits(root, "out/a.py", content="# @generated\n")
    mixed.recs.extend(_edits(root, "out/b.py", content="def f(): ...\n").recs)
    assert _kinds(_mine(tmp_path, mixed, project=str(root)), "generated_dir") == []


@pytest.mark.parametrize("failure", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 2)])
def test_generated_dir_without_git_is_no_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Missing git or a git timeout never counts as 'ignored' (never assume generated)."""
    root = tmp_path / "plain"
    log = _edits(root, "dist/a.js", "dist/b.js")
    calls: list[list[str]] = []

    def broken(args: list[str], **_k: object) -> None:
        calls.append(args)
        raise failure

    monkeypatch.setattr(miners.subprocess, "run", broken)
    assert _kinds(_mine(tmp_path, log, project=str(root)), "generated_dir") == []
    assert calls and calls[0][:2] == ["git", "-C"]
    assert miners.git_ignored(str(root), "dist") is None


def test_generated_dir_without_project_dir_is_no_candidate(tmp_path: Path) -> None:
    log = Log().prompt()
    log.call("Edit", {"file_path": "dist/a.js"}).call("Edit", {"file_path": "dist/b.js"})
    assert _kinds(_mine(tmp_path, log, project=None), "generated_dir") == []  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cmd", "want"),
    [
        ("pytest tests/unit -q --no-cov", "pytest"),
        ("pytest -x --lf -p no:cacheprovider tests/test_a.py::test_b", "pytest"),
        ("python -m pytest -k slow -m 'not e2e'", "python -m pytest"),
        ("python3.12 -m pytest tests", "python3.12 -m pytest"),
        (".venv/bin/python -m pytest -q", ".venv/bin/python -m pytest"),
        (".venv/bin/pytest tests", ".venv/bin/pytest"),
        ("uv run pytest -q", "uv run pytest"),
        ("poetry run pytest tests", "poetry run pytest"),
        ("tox -e py311", "tox"),
        ("nox -s tests", "nox"),
        ("npm test -- --grep parser", "npm test"),
        ("npm run test -- -t x", "npm run test"),
        ("pnpm test", "pnpm test"),
        ("yarn test src/a.test.ts", "yarn test"),
        ("npx jest src/a.test.ts", "npx jest"),
        ("npx vitest run", "npx vitest run"),
        ("cargo test parser --release", "cargo test"),
        ("go test ./...", "go test ./..."),
        ("go test -v -run TestX ./...", "go test ./..."),
        ("make test PY=python3.12", "make test"),
        ("gradle test --tests Foo", "gradle test"),
        ("./gradlew test", "./gradlew test"),
        ("mvn test -Dtest=Foo", "mvn test"),
        ("dotnet test --filter X", "dotnet test"),
        ("bundle exec rspec spec/a_spec.rb", "bundle exec rspec"),
        ("rake test TEST=x", "rake test"),
        ("cd packages/api && npm test -- --grep x", None),
        ("cd tests/unit && pytest", None),
        ("cd tests/integration && python -m pytest -x", None),
        ("pushd tests && pytest", None),
        ("make -C sub test", None),
        ("make test -C sub", None),
        ("make test --directory=sub", None),
        ("npm test --prefix packages/api", None),
        ("npm --prefix packages/api test", None),
        ("pnpm test --filter api", None),
        ("cargo test -p core", None),
        ("cargo test --package core", None),
        ("cargo test --package=core", None),
        ("cargo test --manifest-path crates/a/Cargo.toml", None),
        ("go test -C sub ./...", None),
        ("gradle test -p sub", None),
        ("mvn test -pl core", None),
        ("pytest --rootdir sub", None),
        ("uv run --directory sub pytest", None),
        ("npm test -- --watchAll=false", "npm test -- --watchAll=false"),
        ("npm test -- --grep x --watchAll=false", "npm test -- --watchAll=false"),
        ("npx jest --ci --watch=false src/a.test.ts", "npx jest --ci --watch=false"),
        ("npx vitest --run src/x", "npx vitest --run"),
        ("npx vitest run src/x", "npx vitest run"),
        ("npx vitest --no-watch", "npx vitest --no-watch"),
        ("CI=true npm test", "CI=true npm test"),
        ("CI=true npm test -- --watchAll=false", "CI=true npm test -- --watchAll=false"),
        ("CI=1 npm test", None),
        ("go test --run TestX ./...", "go test ./..."),
        ("go test -run TestX ./...", "go test ./..."),
        ("cargo test --run x", "cargo test"),
        ("npm test -- --run", "npm test"),
        ("pytest --run slow", "pytest"),
        ("npx vitest --run --root packages/a", None),
        ("npx vitest --config vitest.unit.ts", None),
        ("npx jest --rootDir src", None),
        ("npx jest --config=jest.e2e.js", None),
        ("npx jest --project unit", None),
        ("npx vitest --configFile x.ts", None),
        ("npm test -- --config jest.unit.js", None),
        ("pytest -c tests/pytest.ini", None),
        ("pytest --rootdir=sub", None),
        ("python -m pytest --config-file x.toml", None),
        ("go test ./pkg/parser", None),
        ("go test", None),
        ("npm run test:unit", None),
        ("bazel test //...", None),
        ("jest", None),
        ("make check", None),
        ("python -X dev -m pytest", None),
        ("echo pytest", None),
        ("pytest -q | tail -5 && echo done", None),
        ("pytest; rm -rf x", None),
        ("", None),
    ],
)
def test_test_invocation_is_the_runner_only(cmd: str, want: str | None) -> None:
    assert miners.test_invocation(cmd) == want


def test_non_narrowing_flags_and_ci_prefix_are_kept(tmp_path: Path) -> None:
    """Owner call: flags that stop watch / interactive modes are rendered with the runner
    (they never narrow the selection); ``CI=true`` survives command normalisation."""
    log = Log().prompt()
    for _ in range(2):
        log.bash("CI=true npm test -- --watchAll=false src/a.test.js", "ok")
    log.bash("npm test", "ok")
    got = _kinds(_mine(tmp_path, log), "test_command")
    assert got == [("tests", "CI=true npm test -- --watchAll=false")]
    assert miners.runner("CI=true npm test -- --watchAll=false") == miners.NPM_TEST


def test_test_command_fact_is_the_runner_invocation(tmp_path: Path) -> None:
    """M3: a focused run is stored as the runner invocation, never as the narrowed argv."""
    log = Log().prompt()
    for _ in range(3):
        log.bash("pytest tests/unit -q --no-cov", "3 passed")
    log.bash("pytest", "40 passed")
    log.bash("bazel test //x", "ok")
    log.bash("bazel test //x", "ok")
    assert _kinds(_mine(tmp_path, log), "test_command") == [("tests", "pytest")]
    only_unknown = Log().prompt().bash("bazel test //x").bash("go test ./pkg")
    assert _kinds(_mine(tmp_path, only_unknown), "test_command") == []


def _legacy(pdir: Path, *entries: tuple[str, dict[str, Any]]) -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / miners.FACTS_FILE
    path.write_text(json.dumps({"version": 1, "facts": dict(entries)}), encoding="utf-8")
    return path


def _stored(kind: str, value: str, n: int = 5, **extra: Any) -> tuple[str, dict[str, Any]]:
    fact = Fact(kind, "tests", value, frozenset())  # type: ignore[arg-type]
    tag = fact_id(fact)[:3]
    entry = {
        "kind": kind,
        "subject": "tests",
        "value": value,
        "sessions": [f"{tag}{i:013x}" for i in range(n)],
        "first_seen": 1.0,
        "last_seen": 2.0,
        "contradictions": 0,
        **extra,
    }
    return fact_id(fact), entry


def test_stored_narrow_test_commands_are_renormalised_or_retired(tmp_path: Path) -> None:
    pdir = tmp_path / "p"
    narrow = _stored("test_command", "pytest tests/unit -q", status="active", effect=[5, 1, 4])
    unknown = _stored("test_command", "bazel test //x", status="active")
    cd = _stored("test_command", "cd tests/unit && pytest", status="candidate")
    junk = ("junk", {"kind": "test_command", "value": 3})
    path = _legacy(pdir, narrow, unknown, cd, junk)
    # before any fold re-normalises the store, neither is offered for injection
    assert [f.value for _, f in lifecycle.active(miners.load(pdir))] == []
    miners.record(pdir, [], env={"TOKEN_SPAREN_HOME": str(tmp_path)}, fold_id="f1")
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    assert narrow[0] not in facts
    moved = facts[fact_id(Fact("test_command", "tests", "pytest", frozenset()))]
    assert moved["value"] == "pytest" and moved["status"] == "active"
    assert moved["effect"] == [5, 1, 4] and len(moved["sessions"]) == 5
    assert facts[unknown[0]]["status"] == "retired"
    assert facts[cd[0]]["status"] == "retired"
    assert [f.value for _, f in lifecycle.active(miners.load(pdir))] == ["pytest"]


def test_renormalised_test_command_merges_into_an_existing_fact(tmp_path: Path) -> None:
    pdir = tmp_path / "p"
    narrow = _stored("test_command", "pytest -x", n=2, status="active")
    plain = _stored("test_command", "pytest", n=3)
    path = _legacy(pdir, narrow, plain)
    miners.record(pdir, [], env={"TOKEN_SPAREN_HOME": str(tmp_path)}, fold_id="f1")
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    assert list(facts) == [plain[0]]
    assert len(facts[plain[0]]["sessions"]) == 5 and "status" not in facts[plain[0]]
    gone = _stored("test_command", "tox -e py", n=2)
    doc = {
        "version": 1,
        "facts": dict([gone]),
        "forgotten": [fact_id(Fact("test_command", "tests", "tox", frozenset()))],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    miners.record(pdir, [], env={"TOKEN_SPAREN_HOME": str(tmp_path)}, fold_id="f2")
    assert json.loads(path.read_text(encoding="utf-8"))["facts"] == {}  # forgotten: dropped


def test_build_first_is_not_credited_across_an_edit_or_to_a_checker(tmp_path: Path) -> None:
    """minor 1: test fails -> Edit fixes it -> build -> test passes: the edit was the fix."""
    log = Log().prompt()
    log.bash("cargo test", "1 failed", err=True)
    log.call("Edit", {"file_path": f"{PROJECT}/src/lib.rs"})
    log.bash("cargo build").bash("cargo test")
    log.bash("npm test", "1 failed", err=True).bash("npm run lint").bash("npm test")
    log.bash("pytest", "1 failed", err=True).bash("ruff check .").bash("pytest")
    log.bash("make test", "1 failed", err=True).bash("tsc --noEmit").bash("make test")
    assert _kinds(_mine(tmp_path, log), "build_first") == []
    real = Log().prompt().bash("npm test", "Cannot find module", err=True)
    real.bash("npm run build").bash("npm test")
    assert _kinds(_mine(tmp_path, real), "build_first") == [("npm test", "npm run build")]


def test_in_project_absolute_paths_are_relativised_and_stored(tmp_path: Path) -> None:
    """minor 2: an in-project absolute path becomes project-relative before record()."""
    log = Log().prompt()
    log.bash("tsc --noEmit", "command not found: tsc", err=True)
    log.bash(f"{PROJECT}/node_modules/.bin/tsc --noEmit")
    log.bash("eslint .", "command not found: eslint", err=True)
    log.bash("/opt/tools/bin/eslint .")  # outside the project: rejected as before
    for _ in range(2):
        log.bash(f"{PROJECT}/.venv/bin/pytest {PROJECT}/tests -q")
    facts = _mine(tmp_path, log)
    assert _kinds(facts, "command_alias") == [("tsc", "node_modules/.bin/tsc")]
    assert _kinds(facts, "test_command") == [("tests", ".venv/bin/pytest")]
    pdir = tmp_path / "store"
    assert miners.record(pdir, facts, env={}) == 2


def test_relativise_only_touches_in_project_paths() -> None:
    assert miners.relativise(f"{PROJECT}/bin/x --c={PROJECT}/a", PROJECT) == "bin/x --c=a"
    assert miners.relativise(f"/other{PROJECT}/x", PROJECT) == f"/other{PROJECT}/x"
    assert miners.relativise("/work/project/x", PROJECT) == "/work/project/x"
    assert miners.relativise(f"{PROJECT}/x", None) == f"{PROJECT}/x"
    assert miners.relativise("/x/y", "relative/root") == "/x/y"


def test_marker_check_skips_symlinks_and_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "gen").mkdir(parents=True)
    target = tmp_path / "outside.go"
    target.write_text("// DO NOT EDIT\n", encoding="utf-8")
    (root / "gen" / "link.go").symlink_to(target)
    assert miners.generated_evidence(str(root), "gen", ["gen/link.go", "gen/gone.go"]) is None
    assert miners.generated_evidence(None, "gen", []) is None
    assert miners.generated_evidence("relative", "gen", []) is None


# --------------------------------------------------------------------------- evidence modes
def _calls(log: Log) -> list[ToolResultRecord]:
    """The log's tool calls as the transcript reader yields them (in memory)."""
    uses: dict[str, tuple[str, dict[str, Any]]] = {}
    out: list[ToolResultRecord] = []
    turn = 0
    for rec in log.recs:
        content = rec["message"]["content"]
        if isinstance(content, str):
            turn += 1
            continue
        for b in content:
            if b["type"] == "tool_use":
                uses[b["id"]] = (b["name"], b["input"])
            elif b["type"] == "tool_result":
                name, inp = uses[b["tool_use_id"]]
                out.append(
                    ToolResultRecord(b["tool_use_id"], name, inp, b["content"], turn, b["is_error"])
                )
    return out


def _forbid_fs(monkeypatch: pytest.MonkeyPatch, *roots: str) -> None:
    """Any stat / open / read / walk under ``roots`` (default: ``PROJECT``) and any subprocess
    raises; other paths (pytest's own) work as usual."""
    under = [r.rstrip("/") + "/" for r in (roots or (PROJECT,))]

    def guard(fn: Any) -> Any:
        def inner(path: Any, *a: Any, **k: Any) -> Any:
            if any(str(path).rstrip("/") + "/" == r or str(path).startswith(r) for r in under):
                raise AssertionError("transcript evidence touched the project")
            return fn(path, *a, **k)

        return inner

    for name in ("open", "stat", "is_file", "is_symlink", "read_text", "exists"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))
    monkeypatch.setattr(miners.os, "walk", guard(miners.os.walk))

    def no_run(*_a: object, **_k: object) -> None:
        raise AssertionError("transcript evidence ran a subprocess")

    monkeypatch.setattr(miners.subprocess, "run", no_run)


def test_transcript_mode_skips_declared_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _node_project(tmp_path, "eslint")
    log = Log().prompt()
    log.bash("eslint src", "zsh: command not found: eslint", err=True).bash("npx eslint src")
    log.bash("tsc --noEmit", "tsc: not found", err=True).bash("npx tsc --noEmit")
    full = miners.mine_calls(_calls(log), "s", project_dir=root)
    assert _kinds(full, "command_alias") == [("eslint", "npx eslint")]
    _forbid_fs(monkeypatch, root)
    miner = miners.SessionMiner(root, evidence="transcript")
    for call in _calls(log):
        miner.feed(call)
    assert _kinds(miner.finish("s"), "command_alias") == []
    assert miner.skipped_needs_fs == 2  # both downloader aliases needed the manifest


def test_transcript_mode_generated_dir_needs_a_read_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_fs(monkeypatch)
    edits = Log().prompt()
    edits.call("Edit", {"file_path": f"{PROJECT}/gen/a.go"})
    edits.call("Edit", {"file_path": f"{PROJECT}/gen/b.go"})
    tr = {"project_dir": PROJECT, "evidence": "transcript"}
    assert _kinds(miners.mine_calls(_calls(edits), "s", **tr), "generated_dir") == []  # type: ignore[arg-type]
    head = "     1\t// Code generated by tool. DO NOT EDIT.\n     2\tpackage gen\n"
    seen = Log().prompt().call("Read", {"file_path": f"{PROJECT}/gen/a.go"}, head)
    seen.recs.extend(edits.recs[1:])
    got = miners.mine_calls(_calls(seen), "s", **tr)  # type: ignore[arg-type]
    assert _kinds(got, "generated_dir") == [("generated", "gen")]
    plain = Log().prompt().call("Read", {"file_path": f"{PROJECT}/gen/b.go"}, "     1\tpackage x\n")
    plain.recs.extend(seen.recs[1:])
    got = miners.mine_calls(_calls(plain), "s", **tr)  # type: ignore[arg-type]
    assert _kinds(got, "generated_dir") == []  # a seen head without the marker: not all marked


@pytest.mark.parametrize(
    ("inp", "text", "want"),
    [
        ({"file_path": f"{PROJECT}/gen/a.go"}, "     1\t// DO NOT EDIT\n", ("gen/a.go", True)),
        (
            {"file_path": f"{PROJECT}/gen/a.go"},
            "1→package x\n2→// @generated\n",
            ("gen/a.go", True),
        ),
        ({"file_path": "gen/a.go", "offset": 1}, "     1\tpackage x\n", ("gen/a.go", False)),
        ({"file_path": f"{PROJECT}/gen/a.go", "offset": 40}, "    40\t// DO NOT EDIT\n", None),
        ({"file_path": f"{PROJECT}/gen/a.go"}, "<system-reminder>empty</system-reminder>", None),
        ({"file_path": "/elsewhere/a.go"}, "     1\t// DO NOT EDIT\n", None),
        ({}, "     1\tx\n", None),
    ],
)
def test_read_head(inp: dict[str, Any], text: str, want: tuple[str, bool] | None) -> None:
    call = ToolResultRecord("t", "Read", inp, text)
    assert miners.read_head(call, PROJECT) == want
    assert miners.read_head(ToolResultRecord("t", "Bash", inp, text), PROJECT) is None
    assert miners.read_head(ToolResultRecord("t", "Read", inp, text, 0, True), PROJECT) is None


def test_transcript_generated_evidence_uses_heads_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_fs(monkeypatch)
    ev = miners.generated_evidence
    heads = {"gen/a.go": True, "gen/b.go": False, "out/x.js": True}
    t = {"evidence": "transcript"}
    assert ev(PROJECT, "gen", ["gen/a.go"], heads=heads, **t) is True  # type: ignore[arg-type]
    assert ev(PROJECT, "gen", ["gen/a.go", "gen/b.go"], heads=heads, **t) is None  # type: ignore[arg-type]
    assert ev(PROJECT, "out", [], heads=heads, **t) is True  # type: ignore[arg-type]
    assert ev(PROJECT, "gen", ["gen/c.go"], heads=heads, **t) is None  # type: ignore[arg-type]
    assert ev(PROJECT, "gen", ["gen/a.go"], **t) is None  # type: ignore[arg-type]
    assert ev(None, "gen", ["gen/a.go"], heads=heads, **t) is None  # type: ignore[arg-type]


def test_unknown_evidence_mode_rejected() -> None:
    with pytest.raises(ValueError, match="evidence"):
        miners.SessionMiner(PROJECT, evidence="disk")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evidence"):
        lifecycle.Observer({}, PROJECT, evidence="disk")  # type: ignore[arg-type]


def test_observer_transcript_mode_attributes_targeted_waste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fs(monkeypatch)
    gen = Fact("generated_dir", "generated", "gen", frozenset())
    env = Fact("requires_env", "make run", "API_HOST", frozenset())
    alias = Fact("command_alias", "pytest", "python -m pytest", frozenset())
    facts = {fact_id(f): f for f in (gen, env, alias)}
    obs = lifecycle.Observer(facts, PROJECT, evidence="transcript")
    log = Log().prompt()
    log.call("Read", {"file_path": f"{PROJECT}/gen/a.go"}, "     1\t// DO NOT EDIT\n")  # 0
    log.call("Edit", {"file_path": f"{PROJECT}/gen/a.go"})  # 1
    log.bash("make run", "KeyError: 'API_HOST'", err=True)  # 2
    log.bash("pytest -q", "command not found: pytest", err=True)  # 3
    log.bash("python -m pytest -q", "1 failed", err=True)  # 4: red run of the spelling
    log.bash("python -m pytest -q", "ok")  # 5
    log.call("Edit", {"file_path": f"{PROJECT}/gen/b.go"})  # 6: never regenerated
    for call in _calls(log):
        obs.feed(call)
    assert obs.waste_calls == {fact_id(env): (2,), fact_id(alias): (3,)}
    assert obs.unsettled == {fact_id(gen)}
    res = obs.results()
    assert res[fact_id(gen)].checked and res[fact_id(gen)].generated is True
    full = lifecycle.Observer(facts, PROJECT)
    for call in _calls(log):
        full.feed(call)
    assert full.waste_calls == {} and full.unsettled == frozenset()  # the fold never pays
