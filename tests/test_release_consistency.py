"""Release consistency: one version, three files, one changelog heading per version."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HEADING = re.compile(r"^## \[(?P<v>[^\]]+)\](?: - (?P<d>.+))?$", re.MULTILINE)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+)?$")
MARKER = "<!-- pre-1.0 milestones -->"


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
