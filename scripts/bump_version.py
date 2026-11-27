"""Bump the token-sparen version in every place that carries it.

``python scripts/bump_version.py 1.0.0``            release heading ``## [1.0.0] - Unreleased``
``python scripts/bump_version.py 1.0.0 --date D``   release heading with a date
``python scripts/bump_version.py 1.0.1.dev0``       development version
``python scripts/bump_version.py --set-date 1.0.0 2026-12-01``  replace ``Unreleased`` by a date

Writes ``token_sparen/__init__.py``, ``.claude-plugin/plugin.json`` and ``CHANGELOG.md``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INIT_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
PLUGIN_RE = re.compile(r'("version"\s*:\s*")([^"]+)(")')
MARKER = "<!-- pre-1.0 milestones -->"
UNRELEASED = "## [Unreleased]"


def _key(version: str) -> tuple[int, int, int, int, int]:
    match = VERSION_RE.match(version)
    if not match:
        raise ValueError(f"bad version {version!r}: expected X.Y.Z or X.Y.Z.devN")
    major, minor, patch, dev = match.groups()
    return (int(major), int(minor), int(patch), 0 if dev is not None else 1, int(dev or 0))


def current_version(root: Path) -> str:
    text = (root / "token_sparen" / "__init__.py").read_text(encoding="utf-8")
    match = INIT_RE.search(text)
    if not match:
        raise ValueError("no __version__ in token_sparen/__init__.py")
    return match.group(1)


def _changelog_text(text: str, new: str, date: str | None) -> str:
    is_dev = ".dev" in new
    start = text.find(UNRELEASED)
    if is_dev:
        if start != -1:
            return text
        first = re.search(r"^## \[", text, re.MULTILINE)
        at = first.start() if first else len(text)
        head = text[:at] if first else text.rstrip("\n") + "\n\n"
        return f"{head}{UNRELEASED}\n\n{text[at:] if first else ''}"
    if start == -1:
        raise ValueError(f"CHANGELOG.md has no {UNRELEASED} heading")
    body_start = start + len(UNRELEASED)
    end = text.find(MARKER, body_start)
    if end == -1:
        nxt = re.search(r"^## \[", text[body_start:], re.MULTILINE)
        end = body_start + nxt.start() if nxt else len(text)
    body = text[body_start:end]
    heading = f"## [{new}] - {date or 'Unreleased'}"
    if re.search(rf"^## \[{re.escape(new)}\]", text, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md already has a heading for {new}")
    return f"{text[:start]}{UNRELEASED}\n\n{heading}{body}{text[end:]}"


def is_dirty(root: Path) -> bool:
    """True when ``git status`` shows uncommitted changes (or git cannot tell)."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(proc.stdout.strip())


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def bump(root: Path, new: str, *, date: str | None, dry_run: bool = False) -> list[Path]:
    """Set the version everywhere; return the files written (or that would be, on a dry run)."""
    if _key(new) <= _key(current_version(root)):
        raise ValueError(f"{new} is not greater than the current {current_version(root)}")
    if date is not None and not DATE_RE.match(date):
        raise ValueError(f"bad date {date!r}: expected YYYY-MM-DD")
    init = root / "token_sparen" / "__init__.py"
    plugin = root / ".claude-plugin" / "plugin.json"
    changelog = root / "CHANGELOG.md"
    init_text = init.read_text(encoding="utf-8")
    plugin_text = plugin.read_text(encoding="utf-8")
    if not PLUGIN_RE.search(plugin_text):
        raise ValueError("no version in .claude-plugin/plugin.json")
    log_text = _changelog_text(changelog.read_text(encoding="utf-8"), new, date)
    if dry_run:
        return [init, plugin, changelog]
    _write(init, INIT_RE.sub(f'__version__ = "{new}"', init_text, count=1))
    _write(plugin, PLUGIN_RE.sub(lambda m: f"{m[1]}{new}{m[3]}", plugin_text, count=1))
    _write(changelog, log_text)
    return [init, plugin, changelog]


def set_date(root: Path, version: str, date: str) -> Path:
    """Replace ``## [version] - Unreleased`` by ``## [version] - date``; nothing else changes."""
    if not DATE_RE.match(date):
        raise ValueError(f"bad date {date!r}: expected YYYY-MM-DD")
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    old = f"## [{version}] - Unreleased"
    if text.count(old) != 1:
        raise ValueError(f"CHANGELOG.md has no single {old!r} heading")
    _write(changelog, text.replace(old, f"## [{version}] - {date}"))
    return changelog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("version", nargs="?")
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true", help="list the files, write nothing")
    parser.add_argument("--allow-dirty", action="store_true", help="skip the clean-tree check")
    parser.add_argument("--set-date", nargs=2, metavar=("VERSION", "DATE"))
    args = parser.parse_args(argv)
    if not args.dry_run and not args.allow_dirty and is_dirty(ROOT):
        print("error: uncommitted changes; commit or pass --allow-dirty", file=sys.stderr)
        return 1
    try:
        if args.set_date:
            print(set_date(ROOT, *args.set_date).relative_to(ROOT))
        elif args.version:
            for path in bump(ROOT, args.version, date=args.date, dry_run=args.dry_run):
                print(path.relative_to(ROOT))
        else:
            parser.error("give a version or --set-date VERSION DATE")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
