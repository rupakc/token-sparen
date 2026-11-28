"""Release tooling (M6 Task 9): `release_check`, `release_notes` and `build_claude_tree`.

Every repository here is synthetic: a git repo in ``tmp_path`` (``core.autocrlf=false``) holding
the real plugin files plus synthetic dev-time files. No network.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sampleoutputs import big_pytest
from userconf import to_user_config

from bench.freeze import freeze
from scripts import build_claude_tree as bct
from scripts import release_check as rc
from scripts import release_notes as rn
from scripts.plugin_hash import plugin_files, plugin_hash

ROOT = Path(__file__).resolve().parent.parent
E4_EXAMPLE = ROOT / "tests" / "bench" / "fixtures" / "e4_example.json"
PIN = "v1.0.0-rc1"
RELEASE_SOURCE = {"source": "github", "repo": "rupakc/token-sparen", "ref": "release/claude"}

README = """# token-sparen

token-sparen keeps the useful part of long tool outputs in a coding session and saves the full
text to a local recovery file, so nothing is lost and the agent reads less. It learns from your
own sessions which outputs are safe to shorten and never shortens error lines or small outputs.

## Evidence

<!-- evidence:start -->
| metric | estimate |
|---|---|
| table | rendered |
<!-- evidence:end -->

## Install

```
claude plugin marketplace add rupakc/token-sparen
```
"""

CHANGELOG = """# Changelog

## [Unreleased]

## [{version}] - {date}

### Runtime changes

- none

### Added

- The first release.

<!-- pre-1.0 milestones -->

## [0.4.0] - 2026-11-20

- Milestone history.
"""

DEV_FILES = {
    "tests/test_example.py": "def test_x() -> None:\n    pass\n",
    "bench/runner.py": "X = 1\n",
    "evals/case.txt": "case\n",
    "docs/user/index.md": "# Docs\n",
    "scripts/tool.py": "Y = 2\n",
    ".github/workflows/ci.yml": "name: ci\n",
    ".claude/skills/release/SKILL.md": "# release\n",
    ".gitattributes": "* text=auto eol=lf\n",
    "CHANGELOG_EXTRA.md": "extra\n",
    "MEMORY.md": "# Memory\n",
}


# --- synthetic repositories -------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(repo.parent),
    )
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _set_version(repo: Path, version: str, *, date: str = "2026-12-01") -> None:
    init = repo / "token_sparen" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    lines = [
        f'__version__ = "{version}"' if line.startswith("__version__ = ") else line
        for line in text.splitlines()
    ]
    init.write_text("\n".join(lines) + "\n", encoding="utf-8")
    plugin = repo / ".claude-plugin" / "plugin.json"
    data = json.loads(plugin.read_text(encoding="utf-8"))
    data["version"] = version
    plugin.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG.format(version=version, date=date), encoding="utf-8"
    )


def _marketplace(repo: Path, source: Any) -> None:
    doc = {
        "name": "token-sparen",
        "owner": {"name": "Test"},
        "plugins": [{"name": "token-sparen", "source": source, "description": "d"}],
    }
    path = repo / ".claude-plugin" / "marketplace.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _make_base(dest: Path) -> Path:
    dest.mkdir(parents=True)
    tracked = _git(ROOT, "ls-files", "-z", "--", *bct.PLUGIN_SOURCES).split("\0")
    for rel in [r for r in tracked if r] + ["LICENSE", "NOTICE", "SECURITY.md"]:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    _marketplace(dest, RELEASE_SOURCE)
    (dest / "README.md").write_text(README, encoding="utf-8")
    for rel, text in DEV_FILES.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(dest, "init", "-q", "-b", "main")
    _set_version(dest, "1.0.0")
    _commit(dest, "chore(release): v1.0.0")
    _git(dest, "tag", PIN)
    return dest


@pytest.fixture(scope="module")
def base_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_base(tmp_path_factory.mktemp("base") / "repo")


@pytest.fixture
def repo(base_repo: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(base_repo, dest, symlinks=True)
    return dest


def _tree_hash(repo: Path, tmp_path: Path) -> str:
    out = tmp_path / "hash-tree"
    shutil.rmtree(out, ignore_errors=True)
    bct.build_claude_tree(repo, out)
    return plugin_hash(out)


def _write_evidence(repo: Path, digest: str, *, verdict: str = "passed") -> Path:
    doc = json.loads(E4_EXAMPLE.read_text(encoding="utf-8"))
    doc["plugin_hash"] = digest
    if verdict != "passed":
        doc["gate"]["verdict"] = verdict
        doc["power"]["verdict"] = "underpowered"
    path = repo / "docs" / "evidence" / "e4.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


class FakeGate(NamedTuple):
    ok: bool
    reason: str


def _gate(ok: bool, calls: list[tuple[Any, ...]] | None = None) -> Callable[..., FakeGate]:
    def check(evidence: Path | None, tree: Path, decision: Path | None) -> FakeGate:
        if calls is not None:
            calls.append((evidence, tree, decision))
        return FakeGate(ok, "passed" if ok else "gate result failed: not releasable")

    return check


@pytest.fixture
def passing_gate(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True, calls))
    return calls


def _released(repo: Path, tmp_path: Path, *, verdict: str = "passed") -> Path:
    """``repo`` at 1.0.0 with evidence measuring its tree, committed."""
    _write_evidence(repo, _tree_hash(repo, tmp_path), verdict=verdict)
    _commit(repo, "docs: evidence")
    return repo


# --- release_check ------------------------------------------------------------------------------


def test_release_check_refuses_tag_version_mismatch(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]]
) -> None:
    _released(repo, tmp_path)
    assert rc.release_check(repo, tag="v1.0.0", mode="tag") == []  # positive control
    assert passing_gate, "the gate ran for an X.Y.0 tag"
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("v1.0.1" in p and "1.0.0" in p for p in problems), problems


@pytest.mark.parametrize("mode", ["tag", "dry-run"])
def test_release_check_refuses_stale_plugin_json(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]], mode: str
) -> None:
    _released(repo, tmp_path)
    plugin = repo / ".claude-plugin" / "plugin.json"
    data = json.loads(plugin.read_text(encoding="utf-8"))
    data["version"] = "0.9.0"
    plugin.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    problems = rc.release_check(repo, tag="v1.0.0", mode=mode)  # type: ignore[arg-type]
    assert any("plugin.json" in p and "0.9.0" in p for p in problems), problems


@pytest.mark.parametrize(
    "tag", ["v1.0.0-rc1", "v0.5.0-screen1", "v0.4.0-m4", "v1.0.0\n", "1.0.0", "v1.0", "V1.0.0"]
)
def test_release_check_rejects_milestone_and_rc_tags(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]], tag: str
) -> None:
    _released(repo, tmp_path)
    problems = rc.release_check(repo, tag=tag, mode="tag")
    assert any("not a release tag" in p for p in problems), problems
    assert not rc.is_release_tag(tag)
    assert rc.is_release_tag("v1.0.0") and rc.is_release_tag("v12.3.45")


def test_tag_mode_requires_dated_heading(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]]
) -> None:
    _released(repo, tmp_path)
    _set_version(repo, "1.0.0", date="Unreleased")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("dated" in p and "Unreleased" in p for p in problems), problems
    assert rc.release_check(repo, tag=None, mode="dry-run") == []
    _set_version(repo, "1.0.0", date="2026-13-45")  # not a real date
    assert any("dated" in p for p in rc.release_check(repo, tag="v1.0.0", mode="tag"))
    changelog = repo / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    changelog.write_text(text.replace("## [Unreleased]", "## [1.0.0] - 2026-12-02"), "utf-8")
    problems = rc.release_check(repo, tag=None, mode="dry-run")
    assert any("exactly one" in p for p in problems), problems


def _patch(
    repo: Path,
    tmp_path: Path,
    touched: str,
    *,
    runtime: str = "- Fixed a crash.",
    version: str = "1.0.1",
    verdict: str = "passed",
    before: Callable[[], None] | None = None,
) -> None:
    _released(repo, tmp_path, verdict=verdict)
    if before is not None:
        before()
    target = repo / touched
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".json":
        data = json.loads(target.read_text(encoding="utf-8"))
        data["patched"] = True
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n# patch fix\n")
    evidence = json.loads((repo / "docs/evidence/e4.json").read_text("utf-8"))
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text("utf-8").replace(
            "| table | rendered |",
            # the claims sentence as `render_evidence --write README.md` writes it (review m-R1)
            f"| table | rendered |\n\nMeasured on 1 task. Plugin version "
            f"{evidence['plugin_version']}, plugin hash `{evidence['plugin_hash']}`, harness.",
        ),
        encoding="utf-8",
    )
    _set_version(repo, version, date="2026-12-10")
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text("utf-8").replace("- none", runtime), encoding="utf-8")
    _commit(repo, f"chore(release): v{version}")


def test_patch_rule_allows_labelled_runtime_fix(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(False, calls))
    _patch(repo, tmp_path, "docs/user/index.md")
    assert rc.release_check(repo, tag="v1.0.1", mode="tag") == []
    assert calls == [], "a patch release is judged by the patch rule, not a new E4 gate"
    # (b) the runtime changes must be listed
    changelog = repo / "CHANGELOG.md"
    text = changelog.read_text("utf-8")
    changelog.write_text(text.replace("### Runtime changes\n\n- Fixed a crash.\n", ""), "utf-8")
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("Runtime changes" in p for p in problems), problems
    changelog.write_text(text, "utf-8")
    # (a) README must state the measured version and plugin hash
    readme = repo / "README.md"
    readme.write_text(readme.read_text("utf-8").replace("Plugin version 1.0.0", "Plugin"), "utf-8")
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("(a)" in p and "Plugin version 1.0.0, plugin hash" in p for p in problems), problems


@pytest.mark.parametrize(
    "touched",
    [
        "token_sparen/params/defaults.toml",
        "token_sparen/learn/engine.py",
        # ruling I2: every behaviour file, not only defaults and learning
        "token_sparen/mechanisms/compression.py",
        "token_sparen/compress/_shared.py",
        "token_sparen/adapters/claude/entry.py",
        "token_sparen/config.py",
        "hooks/run.py",
        "skills/token-sparen/SKILL.md",
        # review M1: the rule is an allow-list, so files nobody listed are behaviour too
        "token_sparen/facts/inject.py",
        "token_sparen/estimate.py",
        "token_sparen/recovery.py",
        "token_sparen/cli.py",
        "token_sparen/store/new_module.py",
        "token_sparen/__init__.py",  # more than the version line
        ".claude-plugin/plugin.json",  # more than the version field
        ".github/workflows/ci.yml",
        ".claude/skills/release/SKILL.md",
        "pyproject.toml",
    ],
)
def test_patch_rule_refuses_behaviour_change(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, touched: str
) -> None:
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, touched)
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any(touched in p and "new E4" in p for p in problems), problems


@pytest.mark.parametrize(
    "touched",
    [
        "docs/user/index.md",
        "docs/release/new-page.md",
        "tests/test_example.py",
        "bench/runner.py",
        "evals/case.txt",
        "scripts/tool.py",
        "MEMORY.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "CLAUDE.md",
        "NOTICE",
        "SECURITY.md",
        "LICENSE",
    ],
)
def test_patch_rule_allows_non_behaviour_files(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, touched: str
) -> None:
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, touched)
    assert rc.release_check(repo, tag="v1.0.1", mode="tag") == []


def test_patch_tree_hash_is_authoritative(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I3: a plugin change is caught by the tree comparison even if the allow-list
    filter were relaxed."""
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    monkeypatch.setattr(rc, "behaviour_changes", lambda root, base, head="HEAD": [])
    _patch(repo, tmp_path, "token_sparen/cli.py")
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("(d)" in p and "beyond the version" in p for p in problems), problems


def test_patch_refuses_a_changed_tree_builder(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I3: a builder change (scripts/ is on the allow-list) that alters what ships makes
    the measured commit build a different plugin than the evidence measured."""
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, "docs/user/index.md")
    assert rc.release_check(repo, tag="v1.0.1", mode="tag") == []
    real = rc.build_claude_tree

    def builder(root: Path, out: Path, *, ref: str = "HEAD") -> list[Path]:
        written = real(root, out, ref=ref)
        (out / "token_sparen" / "injected.py").write_text("X = 1\n", encoding="utf-8")
        return written

    monkeypatch.setattr(rc, "build_claude_tree", builder)
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("(d)" in p and "tree builder" in p for p in problems), problems


def test_normalised_plugin_hash_ignores_only_the_version(tmp_path: Path) -> None:
    def tree(name: str, version: str, extra: str = "") -> Path:
        root = tmp_path / name
        (root / ".claude-plugin").mkdir(parents=True)
        (root / "token_sparen").mkdir()
        manifest = {"name": "token-sparen", "version": version}
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), "utf-8")
        init = f'"""Doc."""\n__version__ = "{version}"\n{extra}'
        (root / "token_sparen" / "__init__.py").write_text(init, encoding="utf-8")
        return root

    a, b, c = tree("a", "1.0.0"), tree("b", "1.0.1"), tree("c", "1.0.1", "X = 1\n")
    assert plugin_hash(a) != plugin_hash(b)
    assert rc.normalised_plugin_hash(a) == rc.normalised_plugin_hash(b)
    assert rc.normalised_plugin_hash(a) != rc.normalised_plugin_hash(c)


def test_patch_with_its_own_evidence_takes_the_full_gate(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]]
) -> None:
    """A runtime patch cannot reuse evidence; measured itself, it is judged like an X.Y.0."""
    _set_version(repo, "1.0.1", date="2026-12-10")
    with (repo / "token_sparen" / "cli.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# runtime fix\n")
    _commit(repo, "fix: runtime")
    path = _write_evidence(repo, _tree_hash(repo, tmp_path))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["plugin_version"] = "1.0.1"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _commit(repo, "chore(release): v1.0.1")
    assert rc.release_check(repo, tag="v1.0.1", mode="tag") == []
    assert passing_gate, "the full E4 gate judged the measured patch"


def test_behaviour_changes_allow_list(repo: Path) -> None:
    """Version-only edits and the marketplace switch are not behaviour; a rename out of the
    plugin counts on its old side; a plugin path never counts as documentation."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _set_version(repo, "1.0.1")
    _marketplace(repo, "./")
    _commit(repo, "chore: version and marketplace only")
    assert rc.behaviour_changes(repo, base) == []
    _git(repo, "mv", "token_sparen/estimate.py", "docs/estimate.py")
    _commit(repo, "move a module out of the plugin")
    assert rc.behaviour_changes(repo, base) == ["token_sparen/estimate.py"]
    with pytest.raises(ValueError):
        rc.behaviour_changes(repo, "no-such-ref")
    assert rc._in_plugin("token_sparen/docs/x.md") and rc._in_plugin(".claude-plugin/plugin.json")
    assert not rc._in_plugin("docs/x.md") and not rc._in_plugin(".claude-plugin/marketplace.json")


def test_version_only_comparison() -> None:
    assert rc._version_only('{"a": 1, "version": "1"}', '{"version": "2", "a": 1}', "p.json")
    assert not rc._version_only('{"a": 1}', '{"a": 2}', "p.json")
    assert not rc._version_only("{bad", "{}", "p.json")
    assert not rc._version_only("[1]", "[1]", "p.json")
    init = '"""Doc."""\n__version__ = "{}"\n'
    assert rc._version_only(init.format("1.0.0"), init.format("1.0.1"), "x/__init__.py")
    assert not rc._version_only(init.format("1"), init.format("1") + "X = 1\n", "x/__init__.py")


@pytest.mark.parametrize("mode", ["tag", "dry-run"])
def test_release_requires_marketplace_on_release_branch(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]], mode: str
) -> None:
    """Ruling B1: a release (a dated X.Y.Z heading) installs the gated tree, never main."""
    _released(repo, tmp_path)
    assert rc.release_check(repo, tag="v1.0.0", mode=mode) == []  # type: ignore[arg-type]
    for source in ("./", {"source": "github", "repo": "rupakc/token-sparen"}, None):
        _marketplace(repo, source)
        _commit(repo, "chore: marketplace")
        problems = rc.release_check(repo, tag="v1.0.0", mode=mode)  # type: ignore[arg-type]
        assert any("marketplace.json" in p and "release/claude" in p for p in problems), problems
    (repo / ".claude-plugin" / "marketplace.json").write_text("{", encoding="utf-8")
    _commit(repo, "chore: broken marketplace")
    problems = rc.release_check(repo, tag="v1.0.0", mode=mode)  # type: ignore[arg-type]
    assert any("marketplace.json unreadable" in p for p in problems), problems


def test_dry_run_allows_dev_marketplace_before_the_first_release(repo: Path) -> None:
    _set_version(repo, "1.0.0", date="Unreleased")  # the rc freeze: not yet prepared
    _marketplace(repo, "./")
    assert rc.release_check(repo, tag=None, mode="dry-run") == []
    _marketplace(repo, "../elsewhere")
    problems = rc.release_check(repo, tag=None, mode="dry-run")
    assert any("neither" in p for p in problems), problems
    doc = json.loads((repo / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    doc["plugins"] = []
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(doc), "utf-8")
    problems = rc.release_check(repo, tag=None, mode="dry-run")
    assert any("exactly one" in p for p in problems), problems


@pytest.mark.parametrize("verdict", ["failed", "underpowered"])
def test_patch_refuses_evidence_that_never_passed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    """Review C1: a patch must not ride on failed or un-overridden underpowered evidence."""
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, "docs/user/index.md", verdict=verdict)
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("patch rule" in p and ("verdict" in p or "decision" in p) for p in problems), (
        problems
    )


@pytest.mark.skipif(sys.platform == "win32", reason="ssh signing setup is POSIX-only here")
def test_patch_reuses_underpowered_evidence_with_signed_ship_decision(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))

    def decide() -> None:
        _ssh_signing(repo, tmp_path)
        _decision(repo)  # version 1.0.0: the measured release, not the patch
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-S", "-m", "docs: owner decision")

    _patch(repo, tmp_path, "docs/user/index.md", verdict="underpowered", before=decide)
    assert rc.release_check(repo, tag="v1.0.1", mode="tag") == []
    _decision(repo, decision="hold")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-S", "-m", "docs: owner holds")
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("hold" in p for p in problems), problems


@pytest.mark.parametrize("version", ["1.1.1", "2.0.1"])
def test_patch_rule_same_major_minor_only(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """Review I1: evidence for 1.0.0 never covers an unmeasured 1.1.x or 2.0.x."""
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, "docs/user/index.md", version=version)
    problems = rc.release_check(repo, tag=f"v{version}", mode="tag")
    assert any("own E4 confirmation" in p for p in problems), problems


def test_tag_mode_requires_clean_work_tree(
    repo: Path, tmp_path: Path, passing_gate: list[tuple[Any, ...]]
) -> None:
    """Review M1: tag mode judges committed content only."""
    _released(repo, tmp_path)
    assert rc.release_check(repo, tag="v1.0.0", mode="tag") == []
    (repo / "NOTICE").write_text("edited\n", encoding="utf-8")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("not clean" in p and "NOTICE" in p for p in problems), problems
    _git(repo, "checkout", "--", "NOTICE")
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("not clean" in p and "stray.txt" in p for p in problems), problems
    assert rc.release_check(repo, tag=None, mode="dry-run") == []  # dry runs may be dirty


def test_patch_rule_needs_resolvable_measured_ref(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _patch(repo, tmp_path, "docs/user/index.md")
    _git(repo, "tag", "-d", PIN)
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any(PIN in p and "resolve" in p for p in problems), problems
    (repo / "docs/evidence/e4.json").unlink()
    _commit(repo, "drop evidence")
    problems = rc.release_check(repo, tag="v1.0.1", mode="tag")
    assert any("evidence" in p for p in problems), problems


def test_dry_run_mode_needs_no_tag_or_evidence(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_gate() -> Any:
        raise AssertionError("dry-run must not consult the release gate")

    monkeypatch.setattr(rc, "_load_gate", no_gate)
    assert not (repo / "docs" / "evidence").exists()
    _set_version(repo, "1.0.0", date="Unreleased")
    assert rc.release_check(repo, tag=None, mode="dry-run") == []
    # a development version on main passes the dry run too
    _set_version(repo, "1.0.1.dev0")
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text("utf-8").replace("## [1.0.1.dev0] - 2026-12-01", "## [1.0.0] - x"),
        "utf-8",
    )
    assert rc.release_check(repo, tag=None, mode="dry-run") == []
    assert rc.main(["--mode", "dry-run", "--root", str(repo)]) == 0
    # tag mode refuses a development version and a missing tag
    assert any("development" in p for p in rc.release_check(repo, tag="v1.0.1", mode="tag"))
    assert any("--tag" in p for p in rc.release_check(repo, tag=None, mode="tag"))


def test_tag_mode_has_no_gate_skip(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _released(repo, tmp_path)
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(False))
    for name in (
        "TOKEN_SPAREN_SKIP_GATE",
        "TOKEN_SPAREN_NO_GATE",
        "RELEASE_SKIP_GATE",
        "SKIP_GATE",
        "TOKEN_SPAREN_RELEASE_FORCE",
    ):
        monkeypatch.setenv(name, "1")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("release gate" in p for p in problems), problems
    assert set(inspect.signature(rc.release_check).parameters) == {"root", "tag", "mode"}
    with pytest.raises(SystemExit):
        rc.main(["--help"])
    help_text = capsys.readouterr().out.lower()
    for word in ("skip", "force", "no-gate", "override"):
        assert word not in help_text
    assert rc.main(["--mode", "tag", "--tag", "v1.0.0", "--root", str(repo)]) == 1

    # fail closed: no gate implementation -> no tag release
    def missing() -> Any:
        raise ImportError("no render_evidence")

    monkeypatch.setattr(rc, "_load_gate", missing)
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("release gate unavailable" in p for p in problems), problems
    # the gate's ok is not enough when the evidence measured another tree
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True))
    _write_evidence(repo, "0" * 64)
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("plugin hash" in p for p in problems), problems
    with pytest.raises(ValueError):
        rc.release_check(repo, tag=None, mode="publish")  # type: ignore[arg-type]


def _decision(repo: Path, **override: str) -> Path:
    e4_id = hashlib.sha256((repo / "docs/evidence/e4.json").read_bytes()).hexdigest()[:12]
    fields = {
        "version": "1.0.0",
        "decision": "ship",
        "rationale": "Not failed; confidence interval documented in the README.",
        "date": "2026-12-01",
        "e4_id": e4_id,
        **override,
    }
    path = repo / "docs" / "release" / "gate-decision.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'{k} = "{v}"\n' for k, v in fields.items()), encoding="utf-8")
    return path


def _ssh_signing(repo: Path, tmp_path: Path) -> None:
    """Configure ``repo`` to sign commits with a throwaway ssh key it also trusts."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not available")
    key = tmp_path / "owner_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "owner", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    allowed = tmp_path / "allowed_signers"
    pub = (tmp_path / "owner_key.pub").read_text("utf-8").strip()
    allowed.write_text(f"test@example.invalid {pub}\n", encoding="utf-8")
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(key))
    _git(repo, "config", "gpg.ssh.allowedSignersFile", str(allowed))


@pytest.mark.skipif(sys.platform == "win32", reason="ssh signing setup is POSIX-only here")
def test_underpowered_gate_needs_signed_owner_decision(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(True, calls))
    _write_evidence(repo, _tree_hash(repo, tmp_path), verdict="underpowered")
    _commit(repo, "docs: underpowered evidence")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("no owner decision file" in p for p in problems), problems
    assert calls[-1][2] is None
    # committed but unsigned: not honoured
    _decision(repo)
    _commit(repo, "docs: owner decision (unsigned)")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("not signed" in p for p in problems), problems
    assert calls[-1][2] is None
    # signed by the trusted owner key: handed to the gate, release ok
    _ssh_signing(repo, tmp_path)
    (repo / "docs/release/gate-decision.toml").write_text(
        (repo / "docs/release/gate-decision.toml").read_text("utf-8") + "\n", "utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-S", "-m", "docs: owner decision")
    assert _git(repo, "log", "-1", "--format=%G?").strip() == "G"
    assert rc.release_check(repo, tag="v1.0.0", mode="tag") == []
    assert calls[-1][2] == (repo / "docs/release/gate-decision.toml").resolve()
    # an uncommitted edit is not the signed decision
    _decision(repo, rationale="changed after signing")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert any("uncommitted" in p for p in problems), problems
    # content rules (reported whatever the signature)
    for override, expected in (
        ({"decision": "hold"}, "hold"),
        ({"decision": "maybe"}, "'ship' or 'hold'"),
        ({"version": "0.9.0"}, "version"),
        ({"rationale": "  "}, "rationale"),
        ({"date": "soon"}, "ISO date"),
        ({"e4_id": "0" * 12}, "e4_id"),
    ):
        _decision(repo, **override)
        problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
        assert any(expected in p for p in problems), (override, problems)
    path = _decision(repo)
    path.write_text(path.read_text("utf-8") + 'extra = "x"\n', "utf-8")
    assert any("unknown key" in p for p in rc.release_check(repo, tag="v1.0.0", mode="tag"))
    path.write_text('version = "1.0.0"\n', "utf-8")
    assert any("missing key" in p for p in rc.release_check(repo, tag="v1.0.0", mode="tag"))
    path.write_text("version = \n", "utf-8")
    assert any("invalid TOML" in p for p in rc.release_check(repo, tag="v1.0.0", mode="tag"))


def test_decision_ignored_unless_underpowered(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rc, "_load_gate", lambda: _gate(False, calls))
    _write_evidence(repo, _tree_hash(repo, tmp_path), verdict="failed")
    _decision(repo)
    _commit(repo, "docs: failed evidence and a decision")
    problems = rc.release_check(repo, tag="v1.0.0", mode="tag")
    assert calls[-1][2] is None, "a decision never reaches the gate for a failed verdict"
    assert any("release gate" in p for p in problems)


def test_release_check_reports_tree_problems(repo: Path) -> None:
    big = repo / "token_sparen" / "blob.txt"
    big.write_text("x" * (bct.MAX_FILE_BYTES + 1), encoding="utf-8")
    _commit(repo, "big file")
    problems = rc.release_check(repo, tag=None, mode="dry-run")
    assert any("blob.txt" in p for p in problems), problems


# --- release_notes ------------------------------------------------------------------------------


@pytest.fixture
def notes_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(path: Path | None, tree: Path | None = None) -> str:
        assert tree is not None, "the renderer gets the tree (notes a different measured build)"
        return "| arm | estimate |\n|---|---|\n| token-sparen | 43 % (95 % CI 40 to 46) |"

    def figures(text: str) -> list[str]:
        return [line for line in text.splitlines() if "%" in line]

    monkeypatch.setattr(rn, "_load_render_evidence", lambda: render)
    monkeypatch.setattr(rn, "figure_violations", figures)


def test_release_notes_extracts_single_section(
    repo: Path, tmp_path: Path, notes_deps: None
) -> None:
    changelog = repo / "CHANGELOG.md"
    tree = tmp_path / "tree"
    bct.build_claude_tree(repo, tree)
    notes = rn.release_notes(changelog, "1.0.0", evidence=None, tree=tree)
    assert "The first release." in notes and "### Runtime changes" in notes
    assert "Milestone history" not in notes and "## [0.4.0]" not in notes
    assert "## [1.0.0]" not in notes  # the release title carries the version
    assert "claude plugin marketplace add rupakc/token-sparen" in notes
    assert "pipx install token-sparen==1.0.0" in notes
    assert bct.SITE_URL in notes
    assert "| token-sparen | 43 %" in notes  # the Evidence table is exempt from the figure rule
    assert plugin_hash(tree) in notes
    # without a tree the notes build one from the changelog's repository
    assert plugin_hash(tree) in rn.release_notes(changelog, "1.0.0", evidence=None)
    text = changelog.read_text("utf-8")
    changelog.write_text(text.replace("- The first release.", "- Saves 43 % of tokens."), "utf-8")
    with pytest.raises(ValueError, match="figure"):
        rn.release_notes(changelog, "1.0.0", evidence=None, tree=tree)
    empty = text.split("### Runtime changes")[0] + "<!-- pre-1.0 milestones -->\n"
    changelog.write_text(empty, "utf-8")
    with pytest.raises(ValueError, match="empty"):
        rn.release_notes(changelog, "1.0.0", evidence=None, tree=tree)
    with pytest.raises(ValueError, match="no CHANGELOG section"):
        rn.release_notes(changelog, "2.0.0", evidence=None, tree=tree)
    changelog.write_text(text + "\n## [1.0.0] - 2026-12-03\n\n- twice\n", "utf-8")
    with pytest.raises(ValueError, match="2 CHANGELOG sections"):
        rn.release_notes(changelog, "1.0.0", evidence=None, tree=tree)


def test_release_notes_cli_and_fail_closed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notes_deps: None
) -> None:
    out = tmp_path / "NOTES.md"
    tree = tmp_path / "tree"
    bct.build_claude_tree(repo, tree)
    argv = ["--changelog", str(repo / "CHANGELOG.md"), "--version", "1.0.0", "--tree", str(tree)]
    assert rn.main([*argv, "--out", str(out)]) == 0
    assert "The first release." in out.read_text("utf-8")
    assert rn.main([*argv, "--version", "9.9.9"]) == 1


# --- build_claude_tree --------------------------------------------------------------------------


def test_claude_tree_meets_directory_limits(repo: Path, tmp_path: Path) -> None:
    (repo / "token_sparen" / "untracked_note.py").write_text("Z = 3\n", encoding="utf-8")
    cache = repo / "token_sparen" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "x.cpython-312.pyc").write_bytes(b"\0")
    out = tmp_path / "tree"
    written = bct.build_claude_tree(repo, out)
    rels = sorted(p.relative_to(out).as_posix() for p in written)
    on_disk = sorted(
        p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() or p.is_symlink()
    )
    assert rels == on_disk
    assert len(rels) <= bct.MAX_FILES
    for rel in ("LICENSE", "NOTICE", "SECURITY.md", "README.md", ".claude-plugin/plugin.json"):
        assert rel in rels
    assert "hooks/hooks.json" in rels and "skills/token-sparen/SKILL.md" in rels
    assert "token_sparen/params/defaults.toml" in rels
    top = {r.split("/")[0] for r in rels}
    assert top == {
        ".claude-plugin", "hooks", "skills", "token_sparen",
        "LICENSE", "NOTICE", "SECURITY.md", "README.md",
    }  # fmt: skip
    for gone in (
        ".claude-plugin/marketplace.json",
        ".gitattributes",
        "CHANGELOG.md",
        "MEMORY.md",
        "token_sparen/untracked_note.py",
    ):
        assert gone not in rels
    assert not any("__pycache__" in r or r.endswith(".pyc") for r in rels)
    readme = (out / "README.md").read_text("utf-8")
    assert "| table | rendered |" not in readme and "evidence:start" not in readme
    assert bct.EVIDENCE_URL in readme
    assert bct.tree_problems(out) == []
    with pytest.raises(FileExistsError):
        bct.build_claude_tree(repo, out)  # never mixes into an existing tree


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "t"
    shutil.copytree(ROOT / "hooks", tree / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "README.md").write_text(README, encoding="utf-8")
    return tree


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t: (t / "big.json").write_text("x" * (256 * 1024 + 1)), "256 KiB"),
        (lambda t: (t / "aux.py").write_text("x"), "Windows"),
        (lambda t: (t / "a:b.py").write_text("x") if os.name != "nt" else None, "Windows"),
        (lambda t: (t / "trail.").write_text("x") if os.name != "nt" else None, "Windows"),
        (
            lambda t: (t / "README.md").write_text("Too short.\n\n```\nmany words in code\n```"),
            "40",
        ),
        (
            lambda t: (t / "hooks" / "hooks.json").write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "python3 /opt/x.py"}]}]}})
            ),
            "CLAUDE_PLUGIN_ROOT",
        ),
        (
            lambda t: (t / "hooks" / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"command": 'npx tool "${CLAUDE_PLUGIN_ROOT}/hooks/run.py"'}
                                    ]
                                }
                            ]
                        }
                    }  # noqa: E501
                )
            ),
            "npx",
        ),
        (
            lambda t: (t / "hooks" / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/gone.py"'}
                                    ]
                                }
                            ]
                        }
                    }  # noqa: E501
                )
            ),
            "gone.py",
        ),
    ],
)
def test_tree_checks_refuse(tmp_path: Path, mutate: Callable[[Path], Any], expected: str) -> None:
    tree = _tree(tmp_path)
    assert bct.tree_problems(tree) == []
    if mutate(tree) is None and expected == "Windows" and os.name == "nt":
        pytest.skip("name cannot exist on Windows")
    problems = bct.tree_problems(tree)
    assert any(expected in p for p in problems), problems


def test_case_clashes_are_reported() -> None:
    assert bct.case_clashes(["README.md", "hooks/run.py"]) == []
    clashes = bct.case_clashes(["README.md", "Readme.md", "hooks/Run.py", "hooks/run.py"])
    assert len(clashes) == 2 and all("case-insensitive" in c for c in clashes)


def test_tree_checks_count_and_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = _tree(tmp_path)
    (tree / "logo.png").write_bytes(b"\x89PNG" + b"\0" * (300 * 1024))  # images may be large
    assert bct.tree_problems(tree) == []
    monkeypatch.setattr(bct, "MAX_FILES", 3)
    assert any("512" in p or "files" in p for p in bct.tree_problems(tree))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_tree_refuses_symlinks(repo: Path, tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "link.py").symlink_to(tree / "hooks" / "run.py")
    assert any("symlink" in p for p in bct.tree_problems(tree))
    (repo / "skills" / "evil.md").symlink_to("/etc/hosts")
    _commit(repo, "tracked symlink")
    with pytest.raises(bct.TreeError, match="symlink"):
        bct.build_claude_tree(repo, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_build_raises_and_cleans_up_on_check_failure(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("# Short\n", encoding="utf-8")
    _commit(repo, "short readme")
    with pytest.raises(bct.TreeError, match="40"):
        bct.build_claude_tree(repo, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert bct.main(["--root", str(repo), "--out", str(tmp_path / "o2")]) == 1


def test_build_cli(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert bct.main(["--root", str(repo), "--out", str(tmp_path / "o")]) == 0
    assert plugin_hash(tmp_path / "o") in capsys.readouterr().out
    # --ref builds an earlier commit (the runbook seeds release/claude from the rc tag)
    with (repo / "token_sparen" / "cli.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# later\n")
    _commit(repo, "later")
    assert bct.main(["--root", str(repo), "--ref", PIN, "--out", str(tmp_path / "p")]) == 0
    assert plugin_hash(tmp_path / "p") == plugin_hash(tmp_path / "o")


def _hook_env(tmp_path: Path, tree: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    env.update(
        HOME=str(tmp_path / "userhome"),
        TOKEN_SPAREN_HOME=str(tmp_path / "home"),
        CLAUDE_PROJECT_DIR=str(proj),
        CLAUDE_PLUGIN_ROOT=str(tree),
        TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="5000",
        TOKEN_SPAREN_LEARN__ENABLED="false",
        TOKEN_SPAREN_NO_UV="1",
        TOKEN_SPAREN_PYTHON=sys.executable,
    )
    return to_user_config(env)


def _hook_argv(tree: Path, event: str) -> list[str]:
    """The hooks.json command for ``event`` as Claude Code runs it (plugin root expanded)."""
    hooks = json.loads((tree / "hooks" / "hooks.json").read_text("utf-8"))["hooks"]
    command = hooks[event][0]["hooks"][0]["command"]
    argv = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", tree.as_posix()))
    assert argv[0] == "python3"
    return [sys.executable, *argv[1:]]


def test_claude_tree_hooks_run(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    bct.build_claude_tree(ROOT, tree)
    env = _hook_env(tmp_path, tree)
    proj = tmp_path / "proj"
    start = subprocess.run(
        _hook_argv(tree, "SessionStart"),
        input=json.dumps({"session_id": "s1", "cwd": str(proj), "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=proj,
        env=env,
        timeout=60,
        check=False,
    )
    assert start.returncode == 0, start.stderr
    payload = {
        "session_id": "s1",
        "cwd": str(proj),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -v"},
        "tool_use_id": "tu-1",
        "tool_response": {
            "stdout": big_pytest(50_000),
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    }
    post = subprocess.run(
        _hook_argv(tree, "PostToolUse"),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=proj,
        env=env,
        timeout=60,
        check=False,
    )
    assert post.returncode == 0, post.stderr
    assert "[token-sparen: kept " in post.stdout, post.stdout[:300] + post.stderr
    assert list((proj / ".token-sparen" / "outputs").glob("*.txt"))


def test_claude_tree_hash_is_m5_plugin_hash(base_repo: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    shutil.copytree(base_repo, work, symlinks=True)
    (work / "token_sparen" / "stray.py").write_text("untracked\n", encoding="utf-8")
    (work / "hooks" / "__pycache__").mkdir()
    (work / "hooks" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\0")
    first = tmp_path / "t1"
    bct.build_claude_tree(work, first)
    clean = tmp_path / "clean"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(base_repo), str(clean))
    second = tmp_path / "t2"
    bct.build_claude_tree(clean, second)
    # like with like: M5's freeze exports the same commit's blobs byte for byte
    frozen = freeze(clean, "HEAD", tmp_path / "frozen", dry_run=True)
    assert plugin_hash(first) == plugin_hash(second) == frozen.plugin_hash
    tracked = sorted(
        r
        for r in _git(clean, "ls-files", "-z").split("\0")
        if r.startswith(("hooks/", "skills/", "token_sparen/")) or r == ".claude-plugin/plugin.json"
    )
    assert plugin_files(first) == tracked
    shutil.rmtree(second)
    bct.build_claude_tree(clean, second)
    assert plugin_hash(second) == plugin_hash(first)  # deterministic rebuild


def test_claude_tree_is_blob_bytes_under_crlf_checkout(base_repo: Path, tmp_path: Path) -> None:
    """A CRLF checkout (``core.autocrlf=true`` + ``eol=crlf`` attributes, as on Windows CI)
    builds the same tree and hash as an LF one: files come from git blobs, not the work tree."""
    lf_tree = tmp_path / "lf"
    bct.build_claude_tree(base_repo, lf_tree)
    crlf = tmp_path / "crlf"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(base_repo), str(crlf))
    _git(crlf, "config", "core.autocrlf", "true")
    (crlf / ".git" / "info").mkdir(exist_ok=True)
    (crlf / ".git" / "info" / "attributes").write_text("* text eol=crlf\n", encoding="utf-8")
    (crlf / ".git" / "index").unlink()
    _git(crlf, "-c", "core.autocrlf=true", "reset", "-q", "--hard")
    on_disk = (crlf / "hooks" / "run.py").read_bytes()
    assert b"\r\n" in on_disk, "setup: the work tree must be CRLF"
    (crlf / "token_sparen" / "cli.py").write_text("uncommitted edit\n", encoding="utf-8")
    tree = tmp_path / "tree"
    bct.build_claude_tree(crlf, tree)
    blob = _git(crlf, "cat-file", "blob", "HEAD:hooks/run.py").encode("utf-8")
    assert (tree / "hooks" / "run.py").read_bytes() == blob
    assert b"\r\n" not in (tree / "hooks" / "run.py").read_bytes()
    assert (tree / "token_sparen" / "cli.py").read_bytes() != b"uncommitted edit\n"
    assert plugin_hash(tree) == plugin_hash(lf_tree)
    assert (tree / "README.md").read_bytes() == (lf_tree / "README.md").read_bytes()


def test_claude_tree_from_ref(base_repo: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    bct.build_claude_tree(base_repo, tree, ref=PIN)
    assert plugin_hash(tree) == _tree_hash(base_repo, tmp_path)
    for bad in ("no-such-ref", "--all"):
        with pytest.raises(bct.TreeError):
            bct.build_claude_tree(base_repo, tmp_path / "bad", ref=bad)


# --- sdist ----------------------------------------------------------------------------------------

PRIVATE = ("MEMORY.md", "docs/", "bench/", "evals/", ".superpowers/", ".claude/")


def test_sdist_excludes_private_dirs(tmp_path: Path) -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    for entry in PRIVATE:
        rule = f"prune {entry.rstrip('/')}" if entry.endswith("/") else f"exclude {entry}"
        assert rule in manifest, rule
    src = tmp_path / "src"
    for rel in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "NOTICE"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, src / rel)
    shutil.copytree(
        ROOT / "token_sparen", src / "token_sparen", ignore=shutil.ignore_patterns("__pycache__")
    )
    for entry in PRIVATE:
        target = src / entry / "private.py" if entry.endswith("/") else src / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PRIVATE = 1\n", encoding="utf-8")
    # an explicit include must not override the prunes (they come last)
    (src / "MANIFEST.in").write_text(
        "graft docs\ngraft bench\ninclude MEMORY.md\n"
        + (ROOT / "MANIFEST.in").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(dist)],
        cwd=src,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    (sdist,) = dist.glob("*.tar.gz")
    with tarfile.open(sdist) as tar:
        names = [n.split("/", 1)[1] for n in tar.getnames() if "/" in n]
    assert "token_sparen/__init__.py" in names
    leaked = [n for n in names if n.startswith(PRIVATE) or n in PRIVATE]
    assert leaked == []
