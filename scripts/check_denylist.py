"""Fail if any tracked text file contains a deny-listed pattern.

Built-in patterns flag local home-directory paths. Private words are read at
runtime from the env var ``TOKEN_SPAREN_DENYLIST`` (comma-separated,
case-insensitive) and are never stored in the repository.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_PATTERNS = (r"/Users/[A-Za-z0-9._-]+/", r"C:\\Users\\[^\\]+\\")
EXCLUDED_PREFIXES = (".superpowers/",)


def build_patterns(env_value: str) -> list[re.Pattern[str]]:
    patterns = [re.compile(p) for p in BUILTIN_PATTERNS]
    for word in env_value.split(","):
        word = word.strip()
        if word:
            patterns.append(re.compile(re.escape(word), re.IGNORECASE))
    return patterns


def scan_text(text: str, patterns: Sequence[re.Pattern[str]]) -> list[int]:
    """Return 1-based line numbers containing any pattern hit."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if any(p.search(line) for p in patterns)
    ]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f and not f.startswith(EXCLUDED_PREFIXES)]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    del argv
    environ = os.environ if env is None else env
    patterns = build_patterns(environ.get("TOKEN_SPAREN_DENYLIST", ""))
    failures = 0
    for rel in tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary file
        for line_no in scan_text(text, patterns):
            # Report location only; never echo the matched content.
            print(f"deny-list hit: {rel}:{line_no}")
            failures += 1
    if failures:
        print(f"{failures} deny-list hit(s)")
        return 1
    print("deny-list clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
