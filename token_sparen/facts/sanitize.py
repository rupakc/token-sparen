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
# Instruction-like words, matched as whole words (plus simple inflections) after normalising:
# lowercase, leetspeak digits mapped back, and once more with - _ . separators dropped.
_DENY_WORDS = frozenset(
    {
        "ignore",
        "instruction",
        "system",
        "assistant",
        "prompt",
        "must",
        "always",
        "never",
        "disregard",
        "forget",
        "override",
        "previous",
        "rule",
        "should",
        "delete",
        "credential",
        "developer",
        "mode",
        "pretend",
        "jailbreak",
        "youare",
        "donot",
        "dont",
    }
)
_DENY_PREFIXES = ("exfil",)
_DENY_PAIRS = frozenset({("you", "are"), ("do", "not")})
_SUFFIXES = frozenset({"", "s", "es", "ed", "d", "ing", "ly"})
_LEET = str.maketrans("013457", "oieast")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_NON_TOKEN = re.compile(r"[^a-z0-9._-]+")
_SEPARATORS = re.compile(r"[._-]")
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_SECRET = re.compile(
    r"(?<![A-Za-z0-9])sk-"  # OpenAI-style / Stripe-style secret keys
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
_PW_CLIENTS = frozenset({"mysql", "psql", "mariadb", "redis-cli", "mongosh"})
_SHORT_PW = re.compile(r"-p.+")
_PW_FLAGS = frozenset({"-W", "-a"})  # take the password as the next argument
# A credential-named option (whole words of its name): any value, attached or the next token.
_CRED_OPTION = re.compile(
    r"-{1,2}(?:[a-z0-9]+[-_])*"
    r"(?:pass(?:word)?|passwd|pwd|pw|token|secret|key|api[-_]?key|apikey|auth|cred(?:ential)?s?"
    r"|bearer|private[-_]key|client[-_]secret|access[-_]key)"
    r"(?:[-_][a-z0-9]+)*",
    re.IGNORECASE,
)
_AUTH_SCHEME = re.compile(r"(?:(?i:bearer)|Basic)[ :=]+[^ ]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SIDE_EFFECTS = re.compile(r"(?i:push|deploy|release|publish|upload|install)")
_CD_PREFIX = re.compile(r"cd ([^ ]+) && (.+)")
_SYSTEM_PATH = re.compile(r"(?i:/users/|/home/|/root(?![A-Za-z0-9_.-])|/etc/|file:)")
_ABS_SEGMENT = re.compile(r"(?:^|(?<=[^A-Za-z0-9_.-]))(/[^=:@+ ]*)")
_SHORT_OPT_PATH = re.compile(r"-[A-Za-z]/")
_PATH_SPLIT = re.compile(r"[/=:@+]")
_VALUE_SPLIT = re.compile(r"[=@+]")
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
    """No absolute path segment anywhere in a token unless inside the project, no well-known
    system or home locations, no ``file:``, drive letters or ``..`` segments."""
    if _SYSTEM_PATH.search(text):
        return False
    for token in text.split(" "):
        if _SHORT_OPT_PATH.match(token):
            return False  # -o/tmp/x
        parts = _PATH_SPLIT.split(token)
        if ".." in parts or any(_DRIVE.match(p) for p in _VALUE_SPLIT.split(token)):
            return False
        for m in _ABS_SEGMENT.finditer(token):
            if not _inside(m.group(1), project_dir):
                return False
    return True


def _denied(word: str) -> bool:
    if word.startswith(_DENY_PREFIXES):
        return True
    for d in _DENY_WORDS:
        if word.startswith(d) and word[len(d) :] in _SUFFIXES:
            return True
        if d.endswith("e") and word == d[:-1] + "ing":
            return True
    return False


def _instruction(text: str) -> bool:
    """Instruction-like wording: whole denied words after lowercasing and mapping leetspeak
    digits back (0 o, 1 i, 3 e, 4 a, 5 s, 7 t), split on every non-alphanumeric character and,
    separately, with ``-``, ``_`` and ``.`` dropped inside a token (``i.g.n.o.r.e``, ``do_not``).
    ``systemctl`` or ``.gitignore`` are not words of the list."""
    base = text.lower().translate(_LEET)
    words = [w for w in _NON_WORD.split(base) if w]
    if any(_denied(w) for w in words):
        return True
    if any(pair in _DENY_PAIRS for pair in zip(words, words[1:], strict=False)):
        return True
    for piece in _NON_TOKEN.split(base):
        joined = _SEPARATORS.sub("", piece)
        if joined and _denied(joined):
            return True
    return False


def _long_secret(text: str) -> bool:
    """A long opaque run (API keys, hashes, JWT parts): >= 32 word characters with at least two
    digits or no ``_``/``-`` separator at all. A long test name (``test_returns_empty_...``) is
    not one."""
    for m in _LONG_RUN.finditer(text):
        run = m.group(0)
        if sum(c.isdigit() for c in run) >= 2 or not any(c in "_-" for c in run):
            return True
    return False


def _clean(text: object, project_dir: str | None) -> bool:
    if not isinstance(text, str) or not 0 < len(text) <= MAX_CHARS:
        return False
    if not text.isascii() or not text.isprintable():
        return False
    if "://" in text or "`" in text or "$(" in text:
        return False
    if _TOKENS.fullmatch(text) is None:  # also rejects ; | & > < $ quotes and whitespace runs
        return False
    if _instruction(text) or _long_secret(text):
        return False
    if _SECRET.search(text) or _ARGV_SECRET.search(text):
        return False
    if _AUTH_SCHEME.search(text):
        return False  # Bearer <token>, Basic <base64>
    tokens = text.split(" ")
    for i, token in enumerate(tokens):
        name = token.split("=", 1)[0].split(":", 1)[0]
        has_value = name != token or i + 1 < len(tokens)
        if has_value and _CRED_OPTION.fullmatch(name):
            return False  # --token hunter2, --password=x, --auth-header Bearer ...
    if any(posixpath.basename(t) in _PW_CLIENTS for t in tokens):
        if any(_SHORT_PW.fullmatch(t) for t in tokens):
            return False  # mysql -pSECRET
        if any(t in _PW_FLAGS for t in tokens[:-1]):
            return False  # psql -W secret, redis-cli -a secret
    return _paths_ok(text, project_dir)


def _value_ok(fact: Fact, project_dir: str | None) -> bool:
    """``_clean``; a test command may carry exactly one ``cd <project-relative dir> && `` prefix
    (monorepos)."""
    value = fact.value
    if fact.kind == "test_command" and isinstance(value, str):
        m = _CD_PREFIX.fullmatch(value)
        if m is not None and len(value) <= MAX_CHARS:
            where, rest = m.group(1), m.group(2)
            relative = not where.startswith("/") and _DRIVE.match(where) is None
            return relative and _clean(where, None) and _clean(rest, project_dir)
    return _clean(value, project_dir)


def sanitize(fact: Fact, *, project_dir: str | None = None) -> Fact | None:
    """The fact if both its subject and value are safe plain data, else None.

    An absolute path is allowed only inside ``project_dir`` (never without one); ``~`` paths,
    drive letters and ``..`` segments are always rejected. A ``build_first`` value never names a
    side-effect step (push, deploy, release, publish, upload, install)."""
    if fact.kind not in KINDS:
        return None
    if not _clean(fact.subject, project_dir) or not _value_ok(fact, project_dir):
        return None
    if fact.kind == "build_first" and _SIDE_EFFECTS.search(fact.value):
        return None  # never suggest a step with side effects outside the build
    if fact.kind == "requires_env" and _VAR_NAME.fullmatch(fact.value) is None:
        return None
    return fact


def render(fact: Fact, *, project_dir: str | None = None) -> str:
    """The fact as one line from its kind's fixed template; raises ValueError if it is unsafe."""
    safe = sanitize(fact, project_dir=project_dir)
    if safe is None:
        raise ValueError("unsafe fact")
    return TEMPLATES[safe.kind].format(subject=safe.subject, value=safe.value)
