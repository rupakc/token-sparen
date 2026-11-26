"""Repository facts are data, never instructions (spec section 6.9).

A fact is mined from repository-controlled output (command errors, file paths), so a malicious
repository could try to plant text that a later session reads as an instruction (persistent
prompt injection). Every fact passes ``sanitize`` before it is stored and again before it is
rendered: ``subject`` and ``value`` must be one plain command-token sequence (allow-listed
characters, single spaces, at most 120 characters, printable ASCII) with no URL, no command
substitution, no shell metacharacters, no instruction words, nothing that looks like a secret and
no path outside the project. ``render`` only fills fixed templates, one per kind.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from typing import Literal, get_args

FactKind = Literal["test_command", "command_alias", "requires_env", "build_first", "generated_dir"]
KINDS: frozenset[str] = frozenset(get_args(FactKind))
MAX_CHARS = 120

# One command-token sequence: allow-listed characters, tokens separated by exactly one space.
_TOKENS = re.compile(r"[A-Za-z0-9_./:@=+-]+(?: [A-Za-z0-9_./:@=+-]+)*")
_INSTRUCTION = re.compile(
    r"ignore|instruction|system|assistant|prompt|must|always|never", re.IGNORECASE
)
_SECRET = re.compile(
    r"[A-Za-z0-9_\-]{32,}"  # long opaque token (API keys, hashes, JWT parts)
    r"|(?<![A-Za-z0-9])sk-"  # OpenAI-style / Stripe-style secret keys
    r"|gh[pousr]_|github_pat_"  # GitHub tokens
    r"|AKIA|ASIA"  # AWS access key ids
    r"|(?<![A-Za-z0-9])xox"  # Slack tokens
)
# Credentials passed on the command line: a credential-named option or assignment with a value,
# user:password@host, and well-known token prefixes.
_ARGV_SECRET = re.compile(
    r"(?i:(?:pass(?:word)?|passwd|pwd|token|secret|api[-_]?key|auth|cred|bearer)[^ ]*[=:])"
    r"|[^ ]+:[^ ]+@"
    r"|(?<![A-Za-z0-9])(?:sk_live_|sk_test_|glpat-|AIza|npm_|xapp-|xoxb-|xoxp-|ghs_|github_pat_)"
)
_PW_CLIENTS = frozenset({"mysql", "psql", "mariadb"})
_SHORT_PW = re.compile(r"-p.+")
_DRIVE = re.compile(r"^[A-Za-z]:")
_VAR_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")

TEMPLATES: dict[str, str] = {
    "test_command": "Run tests with: `{value}`",
    "command_alias": "Use `{value}` instead of `{subject}` here",
    "requires_env": "`{subject}` requires the environment variable `{value}`",
    "build_first": "Build first with `{value}` before `{subject}`",
    "generated_dir": "Generated files live in `{value}`",
}


@dataclass(frozen=True)
class Fact:
    """One candidate repository fact and the (digested) sessions that showed it."""

    kind: FactKind
    subject: str
    value: str
    evidence_sessions: frozenset[str]


def fact_id(fact: Fact) -> str:
    """sha1(kind|subject|value), first 12 hex characters."""
    raw = f"{fact.kind}|{fact.subject}|{fact.value}".encode()
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:12]


def _inside(path: str, project_dir: str | None) -> bool:
    if project_dir is None:
        return False
    root = posixpath.normpath(project_dir.replace("\\", "/"))
    if not root.startswith("/") or root == "/":
        return False
    norm = posixpath.normpath(path)
    return norm.startswith(root.rstrip("/") + "/") and ".." not in path.split("/")


def _paths_ok(text: str, project_dir: str | None) -> bool:
    for token in text.split(" "):
        for part in token.split("="):  # PY=/usr/bin/python is a path too
            if _DRIVE.match(part):
                return False
            if part.startswith("/") and not _inside(part, project_dir):
                return False
            if ".." in part.split("/"):
                return False
    return True


def _clean(text: object, project_dir: str | None) -> bool:
    if not isinstance(text, str) or not 0 < len(text) <= MAX_CHARS:
        return False
    if not text.isascii() or not text.isprintable():
        return False
    if "://" in text or "`" in text or "$(" in text:
        return False
    if _TOKENS.fullmatch(text) is None:  # also rejects ; | & > < $ quotes and whitespace runs
        return False
    if _INSTRUCTION.search(text) or _SECRET.search(text) or _ARGV_SECRET.search(text):
        return False
    tokens = text.split(" ")
    if any(posixpath.basename(t) in _PW_CLIENTS for t in tokens) and any(
        _SHORT_PW.fullmatch(t) for t in tokens
    ):
        return False  # mysql -pSECRET
    return _paths_ok(text, project_dir)


def sanitize(fact: Fact, *, project_dir: str | None = None) -> Fact | None:
    """The fact if both its subject and value are safe plain data, else None.

    An absolute path is allowed only inside ``project_dir`` (never without one); ``~`` paths,
    drive letters and ``..`` segments are always rejected."""
    if fact.kind not in KINDS:
        return None
    if not _clean(fact.subject, project_dir) or not _clean(fact.value, project_dir):
        return None
    if fact.kind == "requires_env" and _VAR_NAME.fullmatch(fact.value) is None:
        return None
    return fact


def render(fact: Fact, *, project_dir: str | None = None) -> str:
    """The fact as one line from its kind's fixed template; raises ValueError if it is unsafe."""
    safe = sanitize(fact, project_dir=project_dir)
    if safe is None:
        raise ValueError("unsafe fact")
    return TEMPLATES[safe.kind].format(subject=safe.subject, value=safe.value)
