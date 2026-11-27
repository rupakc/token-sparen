"""Schema versions of the persisted JSON documents that carry one (by file name).

A document with a ``version`` key keeps it; one without gets none added (absent = version 1).
A reader that meets an older known version loads it (compat) or logs and starts afresh. A
document whose version is newer than the one listed here was written by a newer token-sparen:
it is read-only for this code, never rewritten (``jsonstore.read_locked`` refuses it with
``NewerSchema``), so an older CLI cannot downgrade state a newer plugin wrote.

The owning modules keep their own constants; ``tests/store/test_upgrade.py`` checks they match.
"""

from __future__ import annotations

from typing import Any

KNOWN_VERSIONS: dict[str, int] = {
    "posteriors.json": 2,  # learn.hierarchy.SCHEMA_VERSION (project and global/)
    "facts.json": 1,  # facts.miners.VERSION
    "prune.json": 2,  # mechanisms.prune.VERSION
    "prune_rules.json": 1,  # mechanisms.prune.REGISTRY_FILE
    "statusline_install.json": 1,  # adapters.claude.settings.META_NAME
    "savings_scale.json": 1,  # learn.regret.SCALE_FILE
}


def newer_version(name: str, doc: Any) -> int | None:
    """The document's version when it is newer than this code knows for ``name``, else None."""
    known = KNOWN_VERSIONS.get(name)
    if known is None or not isinstance(doc, dict):
        return None
    version = doc.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > known:
        return version
    return None
