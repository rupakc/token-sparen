"""Build the static documentation site from ``docs/user`` (M6 Task 7).

Python-Markdown plus an own template: no JavaScript, no external scripts, fonts or analytics.
Pages are listed in ``nav.toml``; ``x.md`` links become ``x.html``; heading anchors use GitHub's
slug rules; the ``<!-- evidence -->`` block is filled by ``render_evidence``. The landing page
demos run the real compressors on synthetic fixtures at build time, so no number is hand-made.

Usage:
    python scripts/build_site.py --out _site [--evidence PATH]
"""

from __future__ import annotations

import argparse
import html
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import markdown  # noqa: E402

from scripts.render_evidence import DEFAULT_EVIDENCE, render_evidence  # noqa: E402

SITE_URL = "https://rupakc.github.io/token-sparen/"
REPO_URL = "https://github.com/rupakc/token-sparen"
DOCS_SRC = ROOT / "docs" / "user"
SITE_DIR = ROOT / "site"
EVIDENCE_BLOCK = re.compile(r"<!--\s*evidence\s*-->.*?<!--\s*/evidence\s*-->", re.S)
LINK = re.compile(r'(href=")([^"#:]*?)\.md((?:#[^"]*)?")')
DEMO_LEVEL = "standard"
SHOWN_LINES = 14


@dataclass(frozen=True)
class Demo:
    name: str
    before_lines: int
    after_lines: int
    before_tokens: int
    after_tokens: int
    before_text: str
    after_text: str


def slugify(value: str, separator: str = "-") -> str:
    """GitHub heading slug: lower-case, drop punctuation, spaces to hyphens."""
    s = re.sub(r"<[^>]+>", "", value).strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", separator)


def _samples() -> list[tuple[str, str]]:
    import sampleoutputs as so

    return [
        ("pytest run", so.big_pytest()),
        ("git diff", so.big_diff()),
        ("build log", so.big_log()),
        ("JSON listing", so.big_json()),
    ]


def build_demos() -> list[Demo]:
    """Run the real compressors on the synthetic fixtures (isolated default configuration)."""
    from token_sparen.compress import CompressContext, detect, load_level, registry
    from token_sparen.config import load_config
    from token_sparen.estimate import estimate_tokens

    out: list[Demo] = []
    with tempfile.TemporaryDirectory() as tmp:
        env = {"TOKEN_SPAREN_HOME": str(Path(tmp) / "home")}
        cfg = load_config(Path(tmp), env)
        for name, text in _samples():
            ctype = detect(text)
            res = registry.for_type(ctype).compress(
                text, load_level(cfg, DEMO_LEVEL), CompressContext(None, (), cfg)
            )
            out.append(
                Demo(
                    name,
                    len(text.splitlines()),
                    res.kept_lines,
                    estimate_tokens(text, str(ctype)),
                    estimate_tokens(res.text, str(ctype)),
                    text,
                    res.text,
                )
            )
    return out


def _excerpt(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= SHOWN_LINES:
        return text
    head = lines[: SHOWN_LINES // 2]
    tail = lines[-(SHOWN_LINES // 2) :]
    return "\n".join(
        [*head, f"... ({len(lines) - SHOWN_LINES} more lines not shown here) ...", *tail]
    )


def _demo_html(demos: list[Demo]) -> str:
    parts = []
    for d in demos:
        parts.append(
            f'<article class="demo"><h3>{html.escape(d.name)}</h3>'
            f"<p>{d.before_lines} lines (about {d.before_tokens} tokens) became "
            f"{d.after_lines} lines (about {d.after_tokens} tokens) at level "
            f"<code>{DEMO_LEVEL}</code>. Errors are never dropped; the full text stays on "
            f"disk.</p>"
            f'<div class="cols"><div><h4>Before</h4><pre>{html.escape(_excerpt(d.before_text))}'
            f"</pre></div><div><h4>After</h4><pre>{html.escape(_excerpt(d.after_text))}</pre>"
            f"</div></div></article>"
        )
    return "\n".join(parts)


LANDING = """<section class="landing">
<p class="pitch">token-sparen is an open-source Claude Code plugin that trims noisy tool output
before the model sees it, keeps the full text on disk so nothing is lost, and never trims
file contents by default. Everything runs locally.</p>
<h2 id="quick-start">Quick start</h2>
<pre>claude plugin marketplace add rupakc/token-sparen
claude plugin install token-sparen@token-sparen</pre>
<p>Start a new session, run <code>/token-sparen doctor</code>, then ask Claude to run a big
test suite and look for the <code>[token-sparen: kept ...]</code> marker. See
<a href="install.html">Install</a>.</p>
<h2 id="where-it-shines">Where it shines</h2>
<p>Long test runs, build logs, JSON listings and diffs, where most lines are routine and the
few that matter are kept. The examples below are generated when this site is built, by running
the real compressors on synthetic sample outputs. They illustrate the mechanism; they are not
benchmark results (see <a href="benchmarks-methodology.html#evidence">Evidence</a>).</p>
{demos}
</section>
"""


def _md(text: str) -> tuple[str, str]:
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc"],
        extension_configs={"toc": {"slugify": slugify, "permalink": False}},
    )
    body = md.convert(text)
    return body, ""


def _rewrite_links(body: str) -> str:
    return LINK.sub(r"\1\2.html\3", body)


def _fix_repo_links(body: str, page: Path, src: Path) -> str:
    """Links that leave docs/user (LICENSE, ../../docs/...) point at the repository on GitHub."""
    repo_root = src.resolve().parent.parent
    page_dir = posixpath.dirname(page.resolve().relative_to(src.resolve()).as_posix())

    def sub(m: re.Match[str]) -> str:
        href = m.group(1)
        if re.match(r"[a-z][a-z0-9+.-]*:|#", href):
            return m.group(0)
        path_part, _, frag = href.partition("#")
        if not path_part or path_part.endswith((".html", ".css")):
            return m.group(0)
        if (page.parent / path_part).resolve().is_file() and (
            page.parent / path_part
        ).resolve().is_relative_to(src.resolve()):
            return m.group(0)
        # relative to docs/user, or written relative to the repository root
        rel = posixpath.normpath(posixpath.join("docs/user", page_dir, path_part))
        alt = posixpath.normpath(path_part)
        if rel.startswith(("..", "docs/user/")) and (repo_root / alt).is_file():
            rel = alt
        if rel.startswith(".."):
            return m.group(0)
        return f'href="{REPO_URL}/blob/main/{rel}{"#" + frag if frag else ""}"'

    return re.sub(r'href="([^"]+)"', sub, body)


def _load_nav(src: Path) -> list[dict[str, Any]]:
    data = tomllib.loads((src / "nav.toml").read_text(encoding="utf-8"))
    pages: list[dict[str, Any]] = data["page"]
    return pages


def _rel(from_page: str, to_page: str) -> str:
    depth = from_page.count("/")
    return "../" * depth + to_page


def _nav_html(pages: list[dict[str, Any]], current: str) -> str:
    out: list[str] = []
    section = None
    for p in pages:
        if p["section"] != section:
            if section is not None:
                out.append("</ul>")
            section = p["section"]
            out.append(f"<h2>{html.escape(section)}</h2><ul>")
        href = _rel(current, str(Path(p["path"]).with_suffix(".html").as_posix()))
        cur = ' aria-current="page"' if p["path"] == current.replace(".html", ".md") else ""
        out.append(f'<li><a href="{href}"{cur}>{html.escape(p["title"])}</a></li>')
    out.append("</ul>")
    return "\n".join(out)


def _render_page(
    template: str,
    pages: list[dict[str, Any]],
    page: dict[str, Any],
    body: str,
    version: str,
    commit: str,
) -> str:
    rel_html = str(Path(page["path"]).with_suffix(".html").as_posix())
    edit = f"{REPO_URL}/edit/main/docs/user/{page['path']}"
    values = {
        "title": html.escape(page["title"]),
        "css": _rel(rel_html, "style.css"),
        "nav": _nav_html(pages, rel_html),
        "content": body,
        "version": html.escape(version),
        "commit": html.escape(commit[:7]),
        "edit_url": edit,
        "home": _rel(rel_html, "index.html"),
    }
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def build_site(
    src: Path, out: Path, *, evidence: Path | None, version: str, commit: str
) -> list[Path]:
    """Write the site into ``out`` (replaced); returns the files written, sorted."""
    pages = _load_nav(src)
    template = (SITE_DIR / "template.html").read_text(encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    evidence_md = render_evidence(evidence)
    evidence_html, _ = _md(evidence_md)
    written: list[Path] = []
    for page in pages:
        text = (src / page["path"]).read_text(encoding="utf-8")
        body, _ = _md(EVIDENCE_BLOCK.sub(lambda _m: "\n" + evidence_md + "\n", text))
        body = _rewrite_links(_fix_repo_links(body, src / page["path"], src))
        if page["path"] == "index.md":
            body = LANDING.replace("{demos}", _demo_html(build_demos())) + body
        target = out / Path(page["path"]).with_suffix(".html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _render_page(template, pages, page, body, version, commit), encoding="utf-8"
        )
        written.append(target)
    shutil.copyfile(SITE_DIR / "style.css", out / "style.css")
    written.append(out / "style.css")
    return sorted(written)


def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=20
        )
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _version() -> str:
    from token_sparen import __version__

    return str(__version__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--out", type=Path, default=ROOT / "_site")
    ap.add_argument("--src", type=Path, default=DOCS_SRC)
    ap.add_argument("--evidence", type=Path, default=None)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    evidence = args.evidence
    if evidence is None and DEFAULT_EVIDENCE.is_file():
        evidence = DEFAULT_EVIDENCE
    if not (args.src / "nav.toml").is_file():
        print(f"build_site: {args.src}/nav.toml not found")
        return 2
    files = build_site(
        args.src,
        args.out,
        evidence=evidence,
        version=_version(),
        commit=_git("rev-parse", "HEAD") or "unknown",
    )
    print(f"built {len(files)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
