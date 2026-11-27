"""Generate the GitHub Wiki pages from ``docs/user`` (M6 Task 8).

One ``<wiki name>.md`` per page of ``nav.toml`` (``index.md`` becomes ``Home.md``), plus
``_Sidebar.md`` in nav order and ``_Footer.md``. Links between pages become wiki links
(``Page-Name#anchor``); links to other repository files become GitHub ``blob`` URLs at the
release tag. Reuses ``build_site``'s nav loading, comment stripping and evidence block. The wiki
is never edited by hand: the docs workflow overwrites it from the release tag.

Usage:
    python scripts/build_wiki.py --out _wiki [--evidence PATH] [--ref TAG]
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.build_site import (  # noqa: E402
    DOCS_SRC,
    EVIDENCE_BLOCK,
    REPO_URL,
    _git,
    _load_nav,
    _strip_comments,
)
from scripts.render_evidence import DEFAULT_EVIDENCE, render_evidence  # noqa: E402

NAME_RE = re.compile(r"[A-Za-z0-9-]+")
_FENCE = re.compile(r"(^```.*?^```[^\n]*$)", re.S | re.M)
_INLINE_CODE = re.compile(r"(`+[^`\n]*`+)")
_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:|#|//", re.I)


def _wiki_names(pages: list[dict[str, str]]) -> dict[str, str]:
    names: dict[str, str] = {}
    seen: set[str] = set()
    for p in pages:
        name = p["wiki"]
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid wiki name {name!r} for {p['path']}")
        if name.lower() in seen:
            raise ValueError(f"duplicate wiki name {name!r}")
        seen.add(name.lower())
        names[p["path"]] = name
    return names


def _rewrite(text: str, page: str, names: dict[str, str], src: Path, ref: str) -> str:
    repo_root = src.resolve().parent.parent
    page_dir = posixpath.dirname(page)

    def link(m: re.Match[str]) -> str:
        href = m.group(2)
        if _SCHEME.match(href):
            return m.group(0)
        path_part, _, frag = href.partition("#")
        suffix = "#" + frag if frag else ""
        target = posixpath.normpath(posixpath.join(page_dir, path_part))
        if target in names:
            return f"{m.group(1)}{names[target]}{suffix}{m.group(3)}"
        rel = posixpath.join("docs/user", target)
        alt = posixpath.normpath(path_part)
        if (repo_root / alt).is_file() and not (src / target).is_file():
            rel = alt
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            return m.group(0)
        return f"{m.group(1)}{REPO_URL}/blob/{ref}/{rel}{suffix}{m.group(3)}"

    def prose(chunk: str) -> str:
        return "".join(
            c if i % 2 else _LINK.sub(link, c) for i, c in enumerate(_INLINE_CODE.split(chunk))
        )

    return "".join(c if i % 2 else prose(c) for i, c in enumerate(_FENCE.split(text)))


def build_wiki(src: Path, out: Path, *, evidence: Path | None, commit: str) -> list[Path]:
    """Write the wiki pages into ``out`` (replaced); ``commit`` is the tag the links point at."""
    pages = _load_nav(src)
    names = _wiki_names(pages)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    evidence_md = render_evidence(evidence)
    written: list[Path] = []
    for p in pages:
        text = (src / p["path"]).read_text(encoding="utf-8")
        text = _strip_comments(EVIDENCE_BLOCK.sub(lambda _m: "\n" + evidence_md + "\n", text))
        target = out / f"{names[p['path']]}.md"
        target.write_text(_rewrite(text, p["path"], names, src, commit), encoding="utf-8")
        written.append(target)
    side: list[str] = []
    section = None
    for p in pages:
        if p["section"] != section:
            section = p["section"]
            side += ["", f"**{section}**", ""]
        side.append(f"- [{p['title']}]({names[p['path']]})")
    (out / "_Sidebar.md").write_text("\n".join(side).lstrip("\n") + "\n", encoding="utf-8")
    (out / "_Footer.md").write_text(
        f"Generated from docs/user at {commit}; edit there, wiki edits are overwritten.\n",
        encoding="utf-8",
    )
    written += [out / "_Sidebar.md", out / "_Footer.md"]
    return sorted(written)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--out", type=Path, default=ROOT / "_wiki")
    ap.add_argument("--src", type=Path, default=DOCS_SRC)
    ap.add_argument("--evidence", type=Path, default=None)
    ap.add_argument("--ref", default=None, help="release tag the footer and file links name")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    evidence = args.evidence
    if evidence is None and DEFAULT_EVIDENCE.is_file():
        evidence = DEFAULT_EVIDENCE
    if not (args.src / "nav.toml").is_file():
        print(f"build_wiki: {args.src}/nav.toml not found")
        return 2
    ref = args.ref or _git("describe", "--tags", "--exact-match") or _git("rev-parse", "HEAD")
    files = build_wiki(args.src, args.out, evidence=evidence, commit=ref or "main")
    print(f"built {len(files)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
