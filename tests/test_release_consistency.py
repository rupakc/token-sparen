"""Release consistency: one version, three files, one changelog heading per version."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HEADING = re.compile(r"^## \[(?P<v>[^\]]+)\](?: - (?P<d>.+))?$", re.MULTILINE)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+)?$")
MARKER = "<!-- pre-1.0 milestones -->"
RELEASE_SOURCE = {"source": "github", "repo": "rupakc/token-sparen", "ref": "release/claude"}
DEV_MARKETPLACE = json.dumps(
    {
        "name": "token-sparen",
        "owner": {"name": "Rupak Chakraborty"},
        "plugins": [{"name": "token-sparen", "source": "./", "description": "d"}],
    },
    indent=2,
)


def _bump_module() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import bump_version
    finally:
        sys.path.pop(0)
    return bump_version


def current_version(root: Path = ROOT) -> str:
    """The version in ``token_sparen/__init__.py`` (the single source of truth)."""
    text = (root / "token_sparen" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    assert match, "no __version__ in token_sparen/__init__.py"
    return match.group(1)


def _headings(changelog: str) -> list[tuple[str, str | None]]:
    return [(m["v"], m["d"]) for m in HEADING.finditer(changelog)]


def _make_repo(tmp_path: Path) -> Path:
    for rel in ("token_sparen/__init__.py", ".claude-plugin/plugin.json", "CHANGELOG.md"):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text(DEV_MARKETPLACE, encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if MARKER not in text:
        text = text.rstrip("\n") + f"\n\n{MARKER}\n\n## [0.0.9] - 2026-01-01\n\n- ancient history\n"
        changelog.write_text(text, encoding="utf-8")
    return tmp_path


def test_versions_agree() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == current_version()


def test_marketplace_entry_has_no_version() -> None:
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["plugins"]
    for entry in market["plugins"]:
        assert "version" not in entry


def test_changelog_has_single_heading_per_version() -> None:
    heads = _headings((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    names = [v for v, _ in heads]
    assert len(names) == len(set(names)), f"duplicate changelog headings: {names}"
    version = current_version()
    if ".dev" in version:
        assert "Unreleased" in names
    else:
        dates = [d for v, d in heads if v == version]
        assert len(dates) == 1
        assert dates[0] == "Unreleased" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", dates[0] or "")


def test_set_date_only_touches_heading(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    mod.bump(root, "1.0.0", date=None)
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.0.0] - Unreleased" in before
    changed = mod.set_date(root, "1.0.0", "2026-12-01")
    assert changed == root / "CHANGELOG.md"
    after = changed.read_text(encoding="utf-8")
    assert after == before.replace("## [1.0.0] - Unreleased", "## [1.0.0] - 2026-12-01")
    with pytest.raises(ValueError):
        mod.set_date(root, "1.0.0", "2026-12-02")  # no longer Unreleased


def test_no_stale_token_sparen_version() -> None:
    current = current_version()
    found = {v for v, _ in _headings((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))}
    past = {v for v in found if VERSION_RE.match(v)} | {"0.1.0.dev0"}
    past.discard(current)
    files = [ROOT / "README.md", ROOT / "SECURITY.md"]
    docs = ROOT / "docs" / "user"
    if docs.is_dir():
        files += sorted(docs.rglob("*.md"))
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for version in past:
            pattern = rf"(?<![\w.]){re.escape(version)}(?!\w|\.\d)"
            assert not re.search(pattern, text), f"{path.name} mentions stale version {version}"


def test_bump_round_trip(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    old = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    history_before = old[old.index(MARKER) :]
    start = old.index("## [Unreleased]") + len("## [Unreleased]")
    unreleased_body = old[start : old.index(MARKER)]
    changed = mod.bump(root, "1.0.0", date=None)
    assert sorted(p.relative_to(root).as_posix() for p in changed) == [
        ".claude-plugin/plugin.json",
        "CHANGELOG.md",
        "token_sparen/__init__.py",
    ]
    assert current_version(root) == "1.0.0"
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == "1.0.0"
    new = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert new[new.index(MARKER) :] == history_before
    top = new[: new.index(MARKER)]
    above, below = top.split("## [1.0.0] - Unreleased", 1)
    assert "## [Unreleased]" in above
    assert "- " not in above.split("## [Unreleased]", 1)[1]  # Unreleased is now empty
    assert below.strip() == unreleased_body.strip()
    with pytest.raises(ValueError):
        mod.bump(root, "1.0.0", date=None)


def test_bump_dev_adds_unreleased_only(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    mod.bump(root, "1.0.0", date="2026-12-01")
    assert "## [1.0.0] - 2026-12-01" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    mod.bump(root, "1.0.1.dev0", date=None)
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count("## [Unreleased]") == 1
    assert current_version(root) == "1.0.1.dev0"


@pytest.mark.parametrize("bad", ["1.0", "1.0.0rc1", "v1.0.0", "0.0.1"])
def test_bump_rejects_bad_versions(tmp_path: Path, bad: str) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    version = current_version(root)
    with pytest.raises(ValueError):
        mod.bump(root, bad, date=None)
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert current_version(root) == version


def test_bump_writes_lf_only(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    for rel in ("token_sparen/__init__.py", ".claude-plugin/plugin.json", "CHANGELOG.md"):
        path = root / rel
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    changed = mod.bump(root, "1.0.0", date=None)
    for path in changed:
        assert b"\r" not in path.read_bytes()


def test_bump_dry_run_writes_nothing(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    changed = mod.bump(root, "1.0.0", date=None, dry_run=True)
    assert len(changed) == 3
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_cli_refuses_dirty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "is_dirty", lambda root: True)
    assert mod.main(["1.0.0"]) == 1
    assert "uncommitted" in capsys.readouterr().err
    assert current_version(root) != "1.0.0"
    assert mod.main(["1.0.0", "--allow-dirty"]) == 0
    assert current_version(root) == "1.0.0"


def test_readme_has_no_literal_version() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert current_version() not in text


def test_release_skill_mentions_owner_signed_tag_and_evidence_gate() -> None:
    skill = (ROOT / ".claude" / "skills" / "release" / "SKILL.md").read_text(encoding="utf-8")
    assert "docs/release/runbook.md" in skill
    assert "release.yml" in skill
    assert "owner" in skill and "signed" in skill and "tag" in skill
    assert "render_evidence.py --check" in skill and "gate-decision.toml" in skill
    for env in ("`release`", "`pypi`"):
        assert env in skill
    assert "Re-run failed jobs" in skill


def test_changelog_has_pre1_marker_below_unreleased() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count(MARKER) == 1
    assert text.index("## [Unreleased]") < text.index(MARKER) < text.index("## Pre-1.0 milestones")
    assert "[1.0.0]" not in text.split(MARKER, 1)[0]  # the bump creates it, nothing writes it


def _market(root: Path) -> dict[str, Any]:
    data = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_marketplace_tracks_release_branch() -> None:
    """Ruling B1: from the release-prep commit of the first release on, the marketplace users
    add from ``main`` installs the gated ``release/claude`` tree, never ``main`` itself. Before
    that (no dated release heading above the milestone marker) ``./`` is the dev install."""
    from scripts import release_check as rc

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    (entry,) = [p for p in _market(ROOT)["plugins"] if p["name"] == "token-sparen"]
    required = rc.marketplace_required(changelog)
    if required:
        assert entry["source"] == RELEASE_SOURCE
    else:
        assert entry["source"] in ("./", RELEASE_SOURCE)
    assert rc.marketplace_problems(ROOT, required=required) == []


def test_marketplace_required_once_a_release_is_dated() -> None:
    from scripts import release_check as rc

    head = "# Changelog\n\n## [Unreleased]\n\n"
    tail = f"{MARKER}\n\n## [0.4.0] - 2026-11-20\n"
    assert not rc.marketplace_required(head + tail)  # pre-1.0 milestones never count
    assert not rc.marketplace_required(head + "## [1.0.0] - Unreleased\n\n" + tail)  # rc freeze
    assert rc.marketplace_required(head + "## [1.0.0] - 2026-12-01\n\n" + tail)
    assert rc.marketplace_required(head + "## [1.0.1] - Unreleased\n\n## [1.0.0] - 2026-12-01\n")


def test_prepare_release_dates_and_tracks_release_branch(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    mod.bump(root, "1.0.0", date=None)
    before = _market(root)
    changed = mod.prepare_release(root, "1.0.0", "2026-12-01")
    assert sorted(p.relative_to(root).as_posix() for p in changed) == [
        ".claude-plugin/marketplace.json",
        "CHANGELOG.md",
    ]
    assert "## [1.0.0] - 2026-12-01" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    after = _market(root)
    (entry,) = after["plugins"]
    assert entry["source"] == RELEASE_SOURCE
    old_entry = before["plugins"][0]
    assert {k: v for k, v in entry.items() if k != "source"} == {
        k: v for k, v in old_entry.items() if k != "source"
    }
    assert {k: v for k, v in after.items() if k != "plugins"} == {
        k: v for k, v in before.items() if k != "plugins"
    }
    # the next dev bump keeps tracking release/claude (nothing points it back at ./)
    mod.bump(root, "1.0.1.dev0", date=None)
    assert _market(root)["plugins"][0]["source"] == RELEASE_SOURCE
    assert mod.track_release_branch(root) == root / ".claude-plugin" / "marketplace.json"
    assert _market(root) == after  # idempotent
    assert b"\r" not in (root / ".claude-plugin" / "marketplace.json").read_bytes()


def test_prepare_release_refuses_and_writes_nothing(tmp_path: Path) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    with pytest.raises(ValueError, match="X.Y.Z"):
        mod.prepare_release(root, "1.0.1.dev0", "2026-12-01")
    with pytest.raises(ValueError, match="current version"):
        mod.prepare_release(root, "1.0.0", "2026-12-01")  # not bumped yet
    mod.bump(root, "1.0.0", date=None)
    market = root / ".claude-plugin" / "marketplace.json"
    market.write_text('{"plugins": []}', encoding="utf-8")
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="exactly one"):
        mod.prepare_release(root, "1.0.0", "2026-12-01")
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == snapshot


def test_cli_prepare_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _bump_module()
    root = _make_repo(tmp_path)
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "is_dirty", lambda root: False)
    assert mod.main(["1.0.0"]) == 0
    assert mod.main(["--prepare-release", "1.0.0", "2026-12-01"]) == 0
    out = capsys.readouterr().out
    assert "marketplace.json" in out and "CHANGELOG.md" in out
    assert _market(root)["plugins"][0]["source"] == RELEASE_SOURCE
