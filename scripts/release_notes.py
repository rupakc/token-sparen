"""Release notes: the curated CHANGELOG section of a version plus a fixed footer.

Footer: install lines, the documentation site, the Evidence table (`render_evidence`, M6 Task 6)
and the plugin hash of the `release/claude` tree. An empty section is refused, and the notes
outside the Evidence table must pass the docs figure rule (`scripts.docs_lint.figure_violations`,
M6 Task 5): savings claims appear only in the Evidence table, with their confidence intervals.

Usage: ``python scripts/release_notes.py --version X.Y.Z [--changelog CHANGELOG.md]
[--evidence docs/evidence/e4.json] [--tree DIR] [--out NOTES.md]``.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_claude_tree import SITE_URL, build_claude_tree  # noqa: E402
from scripts.docs_lint import figure_violations  # noqa: E402
from scripts.plugin_hash import plugin_hash  # noqa: E402
from scripts.release_check import HEADING, section  # noqa: E402
from scripts.render_evidence import render_evidence  # noqa: E402

REPO = "rupakc/token-sparen"


def _load_render_evidence() -> Callable[..., str]:
    """`render_evidence.render_evidence(path, tree)` (a seam for tests)."""
    return render_evidence


def _section(changelog: Path, version: str) -> str:
    text = changelog.read_text(encoding="utf-8")
    count = sum(1 for m in HEADING.finditer(text) if m["v"] == version)
    if count == 0:
        raise ValueError(f"no CHANGELOG section for {version} in {changelog}")
    if count > 1:
        raise ValueError(f"{count} CHANGELOG sections for {version} in {changelog}")
    body = section(text, version).strip("\n")
    content = [ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not content:
        raise ValueError(f"the CHANGELOG section for {version} is empty")
    return body


def _footer(version: str, digest: str) -> str:
    return (
        "## Install\n\n"
        "Claude Code plugin:\n\n"
        "```\n"
        # release_check refuses a release whose marketplace entry is not release/claude (ruling
        # B1), so this resolves to the released tree
        f"claude plugin marketplace add {REPO}\n"
        "claude plugin install token-sparen@token-sparen\n"
        "```\n\n"
        "Command-line tool:\n\n"
        "```\n"
        f"pipx install token-sparen=={version}\n"
        "```\n\n"
        f"Documentation: {SITE_URL}\n\n"
        f"Plugin hash (`release/claude` tree): `{digest}`\n"
    )


def release_notes(
    changelog: Path, version: str, *, evidence: Path | None, tree: Path | None = None
) -> str:
    """Notes for ``version``. ``tree`` is the built `release/claude` tree (built from the
    changelog's repository when omitted). Raises `ValueError` on an empty or missing section
    or a figure outside the Evidence table."""
    body = _section(changelog, version)
    render = _load_render_evidence()
    if tree is None:
        with tempfile.TemporaryDirectory(prefix="token-sparen-tree-") as tmp:
            built = Path(tmp) / "tree"
            build_claude_tree(changelog.resolve().parent, built)
            digest = plugin_hash(built)
            # with the tree, the renderer notes when the evidence measured another build
            table = render(evidence, built).strip("\n")
    else:
        digest = plugin_hash(tree)
        table = render(evidence, tree).strip("\n")
    prose = f"{body}\n\n{_footer(version, digest)}"
    violations = figure_violations(prose)
    if violations:
        raise ValueError(
            "release notes state a figure outside the Evidence table: " + "; ".join(violations)
        )
    notes = f"{body}\n\n## Evidence\n\n{table}\n\n{_footer(version, digest)}"
    return re.sub(r"\n{3,}", "\n\n", notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_notes", description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", default=ROOT / "CHANGELOG.md", type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--tree", type=Path, default=None, help="built release/claude tree")
    parser.add_argument("--out", type=Path, default=None, help="write here (default: stdout)")
    args = parser.parse_args(argv)
    try:
        notes = release_notes(args.changelog, args.version, evidence=args.evidence, tree=args.tree)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    if args.out is None:
        sys.stdout.write(notes)
    else:
        args.out.write_text(notes, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
