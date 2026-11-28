"""Wiki generated from docs/user and its sync job in the docs workflow (M6 Task 8)."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest

if not os.environ.get("TOKEN_SPAREN_REQUIRE_DOCS"):
    pytest.importorskip("markdown")

from scripts import build_wiki as bw  # noqa: E402
from tests.test_build_site import NAV, PAGES, _active  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _src(tmp: Path, nav: str = NAV) -> Path:
    src = tmp / "repo" / "docs" / "user"
    src.mkdir(parents=True)
    (src / "nav.toml").write_text(nav, encoding="utf-8")
    for rel, body in PAGES.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return src


def _build(tmp: Path) -> Path:
    out = tmp / "_wiki"
    bw.build_wiki(_src(tmp), out, evidence=None, commit="v1.2.3")
    return out


def test_wiki_links_resolve_to_pages(tmp_path: Path) -> None:
    out = _build(tmp_path)
    names = {p.stem for p in out.glob("*.md")}
    assert {"Home", "Install", "Mechanism-compression", "Benchmarks-methodology"} <= names
    home = (out / "Home.md").read_text(encoding="utf-8")
    assert "](Install)" in home and "](Mechanism-compression#how-it-trims)" in home
    comp = (out / "Mechanism-compression.md").read_text(encoding="utf-8")
    assert "](Install)" in comp
    assert "](https://github.com/rupakc/token-sparen/blob/v1.2.3/LICENSE)" in comp
    for md in out.glob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
            if not target.startswith("http"):
                assert target.split("#")[0] in names, (md.name, target)


def test_wiki_sidebar_in_nav_order(tmp_path: Path) -> None:
    out = _build(tmp_path)
    side = (out / "_Sidebar.md").read_text(encoding="utf-8")
    order = re.findall(r"\]\(([^)]+)\)", side)
    assert order == ["Home", "Install", "Mechanism-compression", "Benchmarks-methodology"]
    assert side.index("**Start**") < side.index("**Mechanisms**") < side.index("**Reference**")


def test_wiki_footer_names_source(tmp_path: Path) -> None:
    out = _build(tmp_path)
    assert (out / "_Footer.md").read_text(encoding="utf-8").strip() == (
        "Generated from docs/user at v1.2.3; edit there, wiki edits are overwritten."
    )


def test_wiki_names_valid_and_unique(tmp_path: Path) -> None:
    for i, bad in enumerate(("Bad Name", "a/b", "Home\\n")):
        nav = NAV.replace('wiki = "Install"', f'wiki = "{bad}"')
        with pytest.raises(ValueError):
            bw.build_wiki(_src(tmp_path / f"x{i}", nav), tmp_path / "o", evidence=None, commit="v1")
    dup = NAV.replace('wiki = "Install"', 'wiki = "home"')
    with pytest.raises(ValueError):
        bw.build_wiki(_src(tmp_path / "y", dup), tmp_path / "o", evidence=None, commit="v1")
    real = tomllib.loads((ROOT / "docs/user/nav.toml").read_text(encoding="utf-8"))["page"]
    assert all(bw.NAME_RE.fullmatch(p["wiki"]) for p in real)


def test_wiki_code_fences_comments_and_evidence(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (src / "install.md").write_text(
        "# Install\n\n```\n[x](index.md)\n```\n\n`[y](index.md)` <!-- note -->\n",
        encoding="utf-8",
    )
    out = tmp_path / "w"
    bw.build_wiki(src, out, evidence=None, commit="v1")
    body = (out / "Install.md").read_text(encoding="utf-8")
    assert body.count("](index.md)") == 2 and "note" not in body
    bench = (out / "Benchmarks-methodology.md").read_text(encoding="utf-8")
    assert "old text" not in bench


def test_real_docs_wiki_has_no_dangling_links(tmp_path: Path) -> None:
    out = tmp_path / "_wiki"
    bw.build_wiki(ROOT / "docs" / "user", out, evidence=None, commit="v1.0.0")
    names = {p.stem for p in out.glob("*.md")}
    for md in out.glob("*.md"):
        for target in re.findall(r"\]\(([^)\s]+)\)", md.read_text(encoding="utf-8")):
            if not re.match(r"[a-z]+:|#", target):
                assert not target.endswith(".md"), (md.name, target)
                assert target.split("#")[0] in names, (md.name, target)


def test_docs_workflow_wiki_job() -> None:
    text = _active(ROOT / ".github" / "workflows" / "docs-deploy.yml")
    job = text.split("\n  wiki:")[1]
    assert "needs: build" in job and "contents: write" in job
    assert "pages: write" not in job and "id-token" not in job
    assert "repository: ${{ github.repository }}.wiki" in job
    assert "OWNER ACTION O3" in job
    assert "docs: sync wiki from" in job and "git diff --cached --quiet" in job
    top = text.split("jobs:")[0]
    assert "write" not in top
    build = text.split("\n  build:")[1].split("\n  deploy-pages:")[0]
    assert "build_wiki.py" in build and "--path _wiki" in build
    assert "name: wiki" in build and "--require-private-words" in build
