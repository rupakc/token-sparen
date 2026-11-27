"""Static documentation site builder and the release-tag Pages workflow (M6 Task 7)."""

from __future__ import annotations

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
import sampleoutputs

from scripts import build_claude_tree
from scripts import build_site as bs

ROOT = Path(__file__).resolve().parent.parent

NAV = """
[[page]]
path = "index.md"
title = "Home"
wiki = "Home"
section = "Start"

[[page]]
path = "install.md"
title = "Install"
wiki = "Install"
section = "Start"

[[page]]
path = "mechanisms/compression.md"
title = "Output compression"
wiki = "Mechanism-compression"
section = "Mechanisms"

[[page]]
path = "benchmarks-methodology.md"
title = "Benchmarks"
wiki = "Benchmarks-methodology"
section = "Reference"
"""

PAGES = {
    "index.md": "# Home\n\nStart with [Install](install.md) or "
    "[compression](mechanisms/compression.md#how-it-trims).\n",
    "install.md": "# Install\n\nBack to [home](index.md). "
    "See [Benchmarks](benchmarks-methodology.md#evidence).\nCode: `a < b`.\n",
    "mechanisms/compression.md": "# Compression\n\n## How it trims\n\n"
    "See [install](../install.md) and [LICENSE](../../../LICENSE).\n",
    "benchmarks-methodology.md": "# Benchmarks\n\n## Evidence\n\n<!-- evidence -->\nold text\n"
    "<!-- /evidence -->\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
}


def make_src(root: Path) -> Path:
    src = root / "user"
    if src.exists():
        return src
    for rel, body in PAGES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body, encoding="utf-8")
    (src / "nav.toml").write_text(NAV, encoding="utf-8")
    return src


def build(tmp: Path, name: str = "out", evidence: Path | None = None) -> Path:
    out = tmp / name
    bs.build_site(make_src(tmp), out, evidence=evidence, version="1.2.3", commit="abcdef1234567890")
    return out


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if v is None:
                continue
            if k in ("id", "name"):
                self.ids.add(v)
            if k in ("href", "src"):
                self.refs.append(v)


def _parse(path: Path) -> _Collect:
    p = _Collect()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def test_site_internal_links_resolve(tmp_path: Path) -> None:
    out = build(tmp_path)
    pages = sorted(out.rglob("*.html"))
    assert {p.relative_to(out).as_posix() for p in pages} >= {
        "index.html",
        "install.html",
        "mechanisms/compression.html",
        "benchmarks-methodology.html",
    }
    parsed = {p.resolve(): _parse(p) for p in pages}
    checked = 0
    for page, info in parsed.items():
        for ref in info.refs:
            if re.match(r"[a-z][a-z0-9+.-]*:", ref):
                continue
            path_part, _, frag = ref.partition("#")
            target = page if not path_part else (page.parent / path_part).resolve()
            assert target.is_file(), f"{page.name}: {ref} does not resolve"
            if frag:
                assert frag in parsed[target].ids, f"{page.name}: #{frag} missing in {target.name}"
            checked += 1
    assert checked > 10
    text = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="mechanisms/compression.html#how-it-trims"' in text
    assert not (out / "index.md").exists()
    comp = (out / "mechanisms" / "compression.html").read_text(encoding="utf-8")
    assert 'href="https://github.com/rupakc/token-sparen/blob/main/LICENSE"' in comp


def test_site_has_no_external_resources_or_scripts(tmp_path: Path) -> None:
    out = build(tmp_path)
    assert (out / "style.css").is_file()
    for f in out.rglob("*"):
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        assert "<script" not in text.lower(), f
        assert "@import" not in text and "url(http" not in text, f
        if f.suffix == ".html":
            assert not re.search(r"<(iframe|object|embed|form)\b", text, re.I), f
            assert not re.search(r"<link[^>]+href=[\"']?(https?:)?//", text, re.I), f
            assert not re.search(r"\bsrc=[\"']?(https?:)?//", text, re.I), f
            assert not re.search(r"<[^>]*\son\w+=", text), f


def test_site_without_evidence_says_no_results(tmp_path: Path) -> None:
    out = build(tmp_path)
    text = (out / "benchmarks-methodology.html").read_text(encoding="utf-8")
    assert "No published benchmark results yet" in text
    assert "old text" not in text
    assert "evidence" not in re.findall(r"<!--.*?-->", text, re.S).__str__()


def test_site_with_evidence_expands_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = tmp_path / "e4.json"
    ev.write_text("{}", encoding="utf-8")
    seen: list[Path | None] = []

    def fake(path: Path | None, tree: Path | None = None) -> str:
        seen.append(path)
        return "| Metric | Estimate |\n| --- | --- |\n| m | 1.5 |\n"

    monkeypatch.setattr(bs, "render_evidence", fake)
    out = build(tmp_path, evidence=ev)
    text = (out / "benchmarks-methodology.html").read_text(encoding="utf-8")
    assert seen == [ev]
    assert "<td>1.5</td>" in text and "old text" not in text


def test_site_build_is_deterministic(tmp_path: Path) -> None:
    a = build(tmp_path, "a")
    b = build(tmp_path, "b")
    fa = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    fb = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert fa == fb
    for rel in fa:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_footer_has_version_short_commit_and_edit_link(tmp_path: Path) -> None:
    out = build(tmp_path)
    text = (out / "install.html").read_text(encoding="utf-8")
    assert "1.2.3" in text and "abcdef1" in text and "abcdef12" not in text
    assert "https://github.com/rupakc/token-sparen/edit/main/docs/user/install.md" in text


def test_demo_blocks_come_from_compressors(tmp_path: Path) -> None:
    from token_sparen.compress import CompressContext, detect, load_level, registry
    from token_sparen.config import load_config

    demos = bs.build_demos()
    by_name = {d.name: d for d in demos}
    assert {"pytest run", "git diff", "build log", "JSON listing"} <= set(by_name)
    sample = sampleoutputs.big_pytest()
    d = by_name["pytest run"]
    assert d.before_lines == len(sample.splitlines())
    assert d.after_lines < d.before_lines and d.after_tokens < d.before_tokens
    cfg = load_config(tmp_path, {"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    comp = registry.for_type(detect(sample))
    res = comp.compress(sample, load_level(cfg, "standard"), CompressContext(None, (), cfg))
    assert d.after_text == res.text and d.after_lines == res.kept_lines
    landing = (build(tmp_path) / "index.html").read_text(encoding="utf-8")
    assert f"{d.before_lines} lines" in landing and f"{d.after_lines} lines" in landing
    assert "Where it shines" in landing and "Quick start" in landing


def test_demos_are_stable() -> None:
    assert bs.build_demos() == bs.build_demos()


def _active(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def test_docs_workflow_deploys_only_release_tags() -> None:
    text = _active(ROOT / ".github" / "workflows" / "docs.yml")
    flat = " ".join(text.split())
    assert "tags: ['v[0-9]+.[0-9]+.[0-9]+']" in text
    assert "workflow_dispatch" in text
    assert "branches" not in text and "pull_request" not in text
    assert not re.search(r"^\s*schedule:", text, re.M)
    # dispatch (and tag pushes) build the release tag, never main
    assert "--sort=-v:refname" in text
    assert "ref: ${{ needs.tag.outputs.tag }}" in text
    assert "ref: main" not in text and "refs/heads/main" not in text
    assert "ref: ${{ github.sha }}" not in text
    # least privilege
    top = text.split("jobs:")[0]
    assert re.search(r"^permissions:\n  contents: read$", top, re.M)
    assert "write" not in top
    deploy = text.split("  deploy-pages:")[1]
    assert "pages: write" in deploy and "id-token: write" in deploy
    assert "environment:" in deploy and "github-pages" in deploy
    before_deploy = text.split("  deploy-pages:")[0]
    assert "write" not in before_deploy
    assert before_deploy.count("persist-credentials: false") >= 2
    # deny-list over the generated tree: secret passed explicitly, fails closed, before upload
    assert "scripts/check_denylist.py --path _site --require-private-words" in flat
    assert "TOKEN_SPAREN_DENYLIST: ${{ secrets.TOKEN_SPAREN_DENYLIST }}" in text
    assert "secrets: inherit" not in text
    assert text.index("check_denylist.py") < text.index("upload-pages-artifact")
    assert "python scripts/build_site.py --out _site" in flat


def test_docs_extra_pins_markdown() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    docs = data["project"]["optional-dependencies"]["docs"]
    assert len(docs) == 1 and re.fullmatch(r"Markdown==\d+\.\d+(\.\d+)?", docs[0])
    assert data["project"]["dependencies"] == []
    assert "_site/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_ci_has_docs_job_without_deploy() -> None:
    text = _active(ROOT / ".github" / "workflows" / "ci.yml")
    job = text.split("\n  docs:")[1].split("\n  supply-chain:")[0]
    assert "build_site.py" in job and "test_build_site.py" in job
    assert "pages" not in job.replace("pip", "") and "write" not in job


def test_raw_html_and_unsafe_urls_never_reach_the_site(tmp_path: Path) -> None:
    src = make_src(tmp_path)
    (src / "install.md").write_text(
        "# Install\n\n<script>alert(1)</script>\n\n"
        '<img src=x onerror="alert(2)">\n\n'
        "[bad](javascript:alert(3)) [bad2](JaVa&#9;ScRiPt:alert(4)) "
        "[ok](https://example.org/x)\n\n"
        'inline <b onclick="x()">bold</b> text\n\n<!-- note -->\n\n'
        "```\n<script>shown</script>\n```\n",
        encoding="utf-8",
    )
    text = (build(tmp_path) / "install.html").read_text(encoding="utf-8")
    body = text.split("<main>")[1].split("</main>")[0]
    assert "<script" not in body.lower() and "<img" not in body.lower()
    assert not re.search(r"<[^>]*\son\w+=", body)
    assert 'href="javascript' not in body.lower()
    assert 'href="https://example.org/x"' in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body  # shown as text
    assert "&lt;script&gt;shown&lt;/script&gt;" in body  # code fence untouched
    assert "note" not in body


def test_docs_workflow_refuses_prereleases_and_off_main_tags() -> None:
    text = _active(ROOT / ".github" / "workflows" / "docs.yml")
    assert "grep -Eq '^v[0-9]+\\.[0-9]+\\.[0-9]+$'" in text
    assert "git merge-base --is-ancestor" in text and "origin/main" in text
    assert "needs: tag" in text and "needs: build" in text


def test_site_url_matches_release_tooling() -> None:
    assert bs.SITE_URL == build_claude_tree.SITE_URL == "https://rupakc.github.io/token-sparen/"
