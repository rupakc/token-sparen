"""Refuse a `release/claude` update that would not move every user forward (review M3).

`push-tree` in `.github/workflows/release.yml` runs this before it pushes: the version in the
`.claude-plugin/plugin.json` currently on `release/claude` must be strictly lower than the tag's
version. Re-running an older release's failed `push-tree` after a newer one shipped would
otherwise put the older tree on top (a fast-forward, but a downgrade for every marketplace user).
An unreadable or non-``X.Y.Z`` version on the branch fails closed. No branch yet passes. The
same version passes only when the branch holds the identical plugin (``--current-hash`` equals
``--new-hash``): the first release seeds `release/claude` with its measured tree before the
release-prep commit lands on `main` (runbook, review I2).

Usage: ``python scripts/push_tree_guard.py --tag vX.Y.Z [--current-manifest FILE]
[--current-hash H --new-hash H]``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def downgrade_problems(
    tag: str,
    current: str | None,
    *,
    current_hash: str | None = None,
    new_hash: str | None = None,
) -> list[str]:
    """Why the tree of ``tag`` must not replace a branch whose plugin.json text is ``current``
    (``None``: the branch does not exist yet).

    The same version is accepted only when both plugin hashes are given and equal: the branch
    was seeded with this very plugin before the first release's prep commit (review I2), and
    the release commit only replaces the unhashed files (README, LICENSE, ...)."""
    new = RELEASE_TAG.fullmatch(tag)
    if new is None:
        return [f"{tag!r} is not a release tag vX.Y.Z"]
    if current is None:
        return []
    try:
        data = json.loads(current)
    except ValueError as exc:
        return [f"release/claude plugin.json is not valid JSON ({exc}); refusing"]
    version = data.get("version") if isinstance(data, dict) else None
    old = RELEASE_VERSION.fullmatch(version) if isinstance(version, str) else None
    if old is None:
        return [f"release/claude holds version {version!r}, not X.Y.Z; refusing"]
    old_key, new_key = tuple(map(int, old.groups())), tuple(map(int, new.groups()))
    if old_key == new_key and current_hash and new_hash:
        if current_hash == new_hash:
            return []  # the seeded tree of this release: same plugin, a fast-forward
        return [
            f"release/claude already holds {version} with plugin {current_hash[:12]}, but "
            f"{tag} built plugin {new_hash[:12]}: a different plugin under the same version; "
            "refusing"
        ]
    if old_key >= new_key:
        return [
            f"release/claude already holds {version}; {tag} is not newer (pushing it would "
            "downgrade every marketplace user); refusing"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="push_tree_guard", description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--current-manifest",
        type=Path,
        help="plugin.json from the current release/claude head (omit for the first release)",
    )
    parser.add_argument("--current-hash", help="plugin hash of the current release/claude tree")
    parser.add_argument("--new-hash", help="plugin hash of the tree about to be pushed")
    args = parser.parse_args(argv)
    current = None
    if args.current_manifest is not None:
        try:
            current = args.current_manifest.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"push_tree_guard: cannot read {args.current_manifest}: {exc}", file=sys.stderr)
            return 1
    problems = downgrade_problems(
        args.tag, current, current_hash=args.current_hash, new_hash=args.new_hash
    )
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok {args.tag} is newer than release/claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
