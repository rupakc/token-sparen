"""Guards for the single user-documentation source (docs/user)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from scripts.docs_lint import figure_violations

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "user"
README = ROOT / "README.md"

# ---------------------------------------------------------------- figures


def test_figure_guard_positive_controls() -> None:
    assert figure_violations("It cut usage by 43 % on average.")
    assert figure_violations("A 2.5x improvement.")
    assert figure_violations("It costs $0.84 less.")
    assert figure_violations("Tokens saved: 31200 in total.")
    assert figure_violations("Trims 12 runs, a reduction in tokens.")
    assert figure_violations("Config floor stays 2000 tokens; the test needs 100 % recall.") == []
    assert figure_violations("<!-- evidence -->\nsaved 43 %\n<!-- /evidence -->") == []
    assert figure_violations("<!-- evidence:start -->\nsaved 43 %\n<!-- evidence:end -->") == []


# ---------------------------------------------------------------- pages and links


def _pages() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _nav() -> list[dict[str, str]]:
    return tomllib.loads((DOCS / "nav.toml").read_text(encoding="utf-8"))["page"]


def _strip_code(text: str) -> str:
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def _slug(heading: str) -> str:
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip().lower())
    heading = heading.replace("`", "")
    heading = re.sub(r"[^\w\- ]", "", heading)
    return heading.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out: set[str] = set()
    body = re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.S | re.M)
    for m in re.finditer(r"^#{1,6}\s+(.*?)\s*#*$", body, re.M):
        base = _slug(m.group(1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    return out


def _links(path: Path) -> list[str]:
    text = _strip_code(path.read_text(encoding="utf-8"))
    return [m.group(1).strip() for m in re.finditer(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)", text)]


def test_nav_lists_every_page_once() -> None:
    nav = _nav()
    listed = [e["path"] for e in nav]
    assert len(listed) == len(set(listed)), "a page is listed twice"
    on_disk = {p.relative_to(DOCS).as_posix() for p in _pages()}
    assert set(listed) == on_disk
    assert nav[0]["path"] == "index.md" and nav[0]["wiki"] == "Home"
    wiki = [e["wiki"] for e in nav]
    assert len(wiki) == len(set(wiki))
    assert all(re.fullmatch(r"[A-Za-z0-9-]+", w) for w in wiki)
    for e in nav:
        assert e["title"].strip() and e["section"].strip()


def _check_link(page: Path, target: str) -> str | None:
    if target.startswith("https://"):
        return None
    if re.match(r"[a-z][a-z0-9+.-]*:", target):
        return f"{target}: use https:// for outside links"
    path_part, _, anchor = target.partition("#")
    if not path_part:
        dest = page
    else:
        dest = (page.parent / path_part).resolve()
        if not (DOCS in dest.parents and dest.is_file()):
            if path_part.startswith((".", "/")):
                return f"{target}: not a page of docs/user"
            dest = (ROOT / path_part).resolve()
            if ROOT not in dest.parents or not dest.exists():
                return f"{target}: no such file"
    if anchor:
        if dest.suffix != ".md":
            return f"{target}: anchor on a non-markdown file"
        if anchor not in _anchors(dest):
            return f"{target}: no such heading"
    return None


@pytest.mark.parametrize("page", _pages() + [README], ids=lambda p: p.relative_to(ROOT).as_posix())
def test_internal_links_resolve(page: Path) -> None:
    problems = [m for t in _links(page) if (m := _check_link(page, t))]
    assert problems == []


def test_link_checker_catches_bad_links() -> None:
    page = DOCS / "index.md"
    assert _check_link(page, "nope.md")
    assert _check_link(page, "install.md#no-such-heading")
    assert _check_link(page, "http://example.org")
    assert _check_link(page, "install.md") is None
    assert _check_link(page, "token_sparen/params/defaults.toml") is None


# ---------------------------------------------------------------- content guards


def test_user_docs_have_no_figures() -> None:
    for page in [*_pages(), README]:
        assert figure_violations(page.read_text(encoding="utf-8")) == [], page.name


def test_benchmarks_page_has_evidence_marker() -> None:
    text = (DOCS / "benchmarks-methodology.md").read_text(encoding="utf-8")
    assert re.search(r"<!--\s*evidence\s*-->", text) and "<!-- /evidence -->" in text


def test_support_matrix_matches_ci() -> None:
    support = (DOCS / "support.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    m = re.search(r"python-version:\s*\[([^\]]*)\]", ci)
    assert m, "ci.yml test matrix has no python-version list"
    ci_versions = set(re.findall(r"\d+\.\d+", m.group(1)))
    line = next(ln for ln in support.splitlines() if "Tested in CI on Python" in ln)
    assert set(re.findall(r"\b\d+\.\d+\b", line)) == ci_versions
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    minimum = manifest["engines"]["claude-code"].lstrip(">=")
    line = next(ln for ln in support.splitlines() if "Minimum Claude Code" in ln)
    assert re.findall(r"\b\d+\.\d+\.\d+\b", line) == [minimum]


def test_surfaces_are_claude_code_only() -> None:
    text = (DOCS / "support.md").read_text(encoding="utf-8")
    assert "Claude Code only" in text and "Cowork" in text
    trouble = (DOCS / "troubleshooting.md").read_text(encoding="utf-8")
    assert "hooks need python ≥ 3.11" in trouble.lower()


def test_configuration_page_states_env_narrows_only() -> None:
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert "can only narrow" in text
    assert "token-sparen config set" in text and "~/.token-sparen/config.toml" in text
