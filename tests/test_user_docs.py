"""Guards for the single user-documentation source (docs/user)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "user"
README = ROOT / "README.md"

# ---------------------------------------------------------------- figures

# Both marker spellings: docs pages (evidence, /evidence) and the README renderer (evidence:start,
# evidence:end).
_EVIDENCE = re.compile(
    r"<!--\s*evidence(?::start)?\s*-->.*?<!--\s*(?:/evidence|evidence:end)\s*-->", re.S
)
_FENCE = re.compile(r"^(?P<intro>(?:[^\n]+\n)+)\n```[^\n]*\n.*?\n```", re.M | re.S)
_KEYWORDS = re.compile(
    r"\b(?:saved|savings|reduction|reduces|cheaper|faster|resolved|pass rate|fewer tokens)\b", re.I
)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
_MULTIPLIER = re.compile(r"\b\d+(?:\.\d+)?x\b")
_CURRENCY = re.compile(r"[$€£]\s?\d|\b\d[\d.,]*\s?(?:USD|EUR|GBP|dollars?)\b", re.I)
_NUMBER = re.compile(r"\d")

# Configuration values, limits and the spec's fixed method constants: never results.
_ALLOWED = [
    "95 %",
    "100 %",
    "−5 pp",
    "90 %",
    "15 %",  # compression.min_saving_ratio
    "50 % and 80 %",  # governor budget notices
    "60 % and 80 %",  # governor context notices
    "5 % for compression",  # learner regret target
    "80 %, until measured",  # loop guard assumption
    "about 5 % of",  # loop guard exploration
    "80 % of recent decisions",  # statusline `exploiting`
    ">= 80 %",
    "0.3",
    # README wording (retention and the min_saving_ratio table row)
    "whenever a new one is saved: those older than 14 days",
    "minimum share saved for a replacement",
]


def _strip_exempt(text: str) -> str:
    """Drop the Evidence block and fenced examples that announce themselves as made up."""
    text = _EVIDENCE.sub(" ", text)

    def keep_fence(m: re.Match[str]) -> str:
        return " " if re.search(r"made up|illustrative", m.group("intro"), re.I) else m.group(0)

    return _FENCE.sub(keep_fence, text)


def figure_violations(text: str) -> list[str]:
    """Savings/quality/cost results (percentages, multipliers, money, numbers near result words)."""
    text = _strip_exempt(text)
    text = re.sub(r"^\s*\d+\.\s", " ", text, flags=re.M)  # list numbers
    text = re.sub(r"\s+", " ", text)
    for phrase in _ALLOWED:
        text = text.replace(phrase, " ")
    found = [m.group(0) for pat in (_PERCENT, _MULTIPLIER, _CURRENCY) for m in pat.finditer(text)]
    text = re.sub(r"`[^`]*`", " `code` ", text)  # digits in code spans are not results
    words = list(re.finditer(r"\S+", text))
    for kw in _KEYWORDS.finditer(text):
        idx = next((i for i, w in enumerate(words) if w.start() <= kw.start() < w.end()), None)
        if idx is None:
            continue
        window = words[max(0, idx - 5) : idx + 6]
        if any(_NUMBER.search(w.group(0)) for w in window):
            found.append(" ".join(w.group(0) for w in window))
    return found


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
