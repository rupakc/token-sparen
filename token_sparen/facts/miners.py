"""Deterministic repository-fact miners and the per-project fact store (spec section 6.8).

Mining runs at fold time only (``learn.overlay``), never in a hook. It streams one session's
Claude Code transcript (main chain only, at most ``MAX_TRANSCRIPT_BYTES``) and proposes
candidate facts, each passed through ``sanitize`` before it is kept:

- ``command_alias``: a Bash call fails with ``command not found: X`` / ``X: not found`` /
  ``'X' is not recognized`` (program ``X``) or ``No module named X`` (for ``python -m X``), and
  within the next 3 tool calls of the same turn a Bash call succeeds whose argv tail is exactly
  the failed call's argv tail and whose head launches ``X`` differently (``python -m X``,
  ``npx X``, ``uv run X``, ``./X``...). No fuzzy matching. A downloading launcher (``npx``,
  ``uvx``, ``pipx``, ``bunx``, ``pnpx``, ``dlx``) only for a package the project declares in
  ``package.json`` or ``pyproject.toml``;
- ``requires_env``: an error names an ALL_CAPS variable (``KeyError: 'X'``, ``X is not set``,
  ``missing environment variable X``, ``Environment variable X``) and the same command later
  succeeds in the same turn with ``X=...`` in front of it or after ``export X=...``. The fact is
  the variable **name** only; its value is never read into a fact, and the subject is the
  program + subcommand only (``mysql``, ``make run``), never its options or arguments;
- ``test_command``: the runner invocation (``test_invocation``: program + subcommand/module
  only, from the ``TEST_RUNNERS`` table; every path, test id and option dropped except the
  watch-mode stoppers ``KEEP_FLAGS`` and a leading ``CI=true``) with the most successful runs.
  A command that matches no table row, runs in another directory (``cd <dir> &&``) or has a
  directory / package option (``SCOPE_FLAGS``) yields no fact: no fact beats a narrow one;
- ``build_first``: a test command that failed earlier succeeds right after a build-family
  command succeeded (the tool call immediately before it is that Bash call: any other tool
  call in between, such as the Edit that fixed the test, resets it; lint / type-check
  commands are not builds); both as program + subcommand, and never a side-effect step (push,
  deploy, release, publish, upload, install);
- ``generated_dir``: Write/Edit targets under ``dist|build|generated|gen|out|target|.next|
  __pycache__`` (relative to the project root) at least ``GEN_MIN_PER_SESSION`` times, and
  positive evidence of generation at fold time: ``git check-ignore`` says the directory is
  ignored, or every edited file that still exists carries a generated marker (``DO NOT
  EDIT``, ``@generated``, ``auto-generated``, ``Code generated``) in its first 5 lines. Edits
  alone are never evidence (they suggest hand-written files); missing git or a git timeout
  counts as "not ignored".

In-project absolute paths in a candidate are made project-relative (POSIX) before it is
sanitised and stored; any other absolute path is rejected.

Evidence is a set of session-id digests (blake2b, 8 bytes) per fact in
``projects/<key>/facts.json``: ``{"version": 1, "facts": {id: {kind, subject, value, sessions,
first_seen, last_seen, contradictions}}, "applied": [fold ids]}``. A fact needs at least
``MIN_SESSIONS`` distinct sessions to be eligible for activation (Task 26 decides activation).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen.compress.families import command_family
from token_sparen.config import log_error
from token_sparen.facts.sanitize import KINDS, Fact, FactKind, fact_id, sanitize
from token_sparen.learn.regret import normalize_command
from token_sparen.store import jsonstore
from token_sparen.transcript import ToolResultRecord, iter_tool_results

FACTS_FILE = "facts.json"
VERSION = 1
MIN_SESSIONS = 2
MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024
MAX_FACTS = 500  # least recently seen dropped beyond this
MAX_SESSIONS = 64  # session digests kept per fact (most recent)
APPLIED_MAX = 200
ALIAS_WINDOW = 3  # tool calls after the failure in which the working spelling must appear
GEN_MIN_PER_SESSION = 2
MAX_KEYS = 2000  # distinct commands / directories counted per session
ERROR_SCAN_CHARS = 4000  # error text scanned at each end of an output
GEN_DIRS = frozenset({"dist", "build", "generated", "gen", "out", "target", ".next", "__pycache__"})
GEN_MARKER_LINES = 5  # a generated marker must appear in the first lines of a file
GEN_MARKER_BYTES = 4096
GEN_MARKER = re.compile(r"(?i:do not edit|@generated|auto-generated|code generated)")
GEN_MAX_FILES = 20  # files sampled per directory for markers
GEN_MAX_DIRS = 8  # directories checked with git per fold
GIT_TIMEOUT_S = 2.0
_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_SHELL_CHARS = re.compile(r"""[;&|<>()$`'"\\*?{}\[\]!#~]""")
_NOT_FOUND = (
    re.compile(r"command not found: ([\w.+/-]+)"),
    re.compile(r"([\w.+/-]+): (?:command )?not found"),
    re.compile(r"'([\w.+/-]+)' is not recognized"),
)
_NO_MODULE = re.compile(r"No module named '?([\w.]+)'?")
_PYTHON = re.compile(r"(?:python|python3|python3\.\d+|py)")
_ENV_ERRORS = (
    re.compile(r"KeyError: '([A-Z][A-Z0-9_]{2,})'"),
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b (?:is )?not set"),
    re.compile(
        r"(?i:missing (?:required )?env(?:ironment)? var(?:iable)?)[:\s'\"]+([A-Z][A-Z0-9_]{2,})\b"
    ),
    re.compile(r"(?i:environment variable)[:\s'\"]+([A-Z][A-Z0-9_]{2,})\b"),
)
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=\S*")
_LEAD_ASSIGNS = re.compile(r"^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_EXPORT = re.compile(r"(?:^|[;&|]\s*)export\s+((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s*)+)")
_LEAD_EXPORT = re.compile(r"^(?:export\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s*)+(?:&&|;)\s*)+")
# Launcher prefixes that run program X another way (the head must be one of these + X).
_LAUNCHERS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("npx",),
        ("bunx",),
        ("uvx",),
        ("pnpm", "exec"),
        ("pnpm", "dlx"),
        ("yarn",),
        ("yarn", "exec"),
        ("npm", "exec"),
        ("uv", "run"),
        ("poetry", "run"),
        ("pipenv", "run"),
        ("pdm", "run"),
        ("hatch", "run"),
        ("pipx", "run"),
        ("bundle", "exec"),
    }
)


# Launchers that may download and run a package that is not part of the project.
_DOWNLOADERS = frozenset({"npx", "uvx", "pipx", "bunx", "pnpx", "dlx"})
MANIFEST_MAX_BYTES = 1024 * 1024
_REQ_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_small(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(MANIFEST_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MANIFEST_MAX_BYTES:
        return None
    return data.decode("utf-8", errors="replace")


def _req_names(items: object) -> set[str]:
    out: set[str] = set()
    for item in items if isinstance(items, list) else ():
        if isinstance(item, str):
            m = _REQ_NAME.match(item)
            if m is not None:
                out.add(_pkg(m.group(1)))
    return out


def _table(doc: object, *keys: str) -> dict[str, Any]:
    for key in keys:
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc if isinstance(doc, dict) else {}


def declared_packages(project_dir: str | None) -> set[str]:
    """Package names the project declares: ``package.json`` dependencies / devDependencies and
    ``pyproject.toml`` (PEP 621 dependencies and extras, dependency groups, Poetry)."""
    names: set[str] = set()
    if project_dir is None:
        return names
    root = Path(project_dir)
    text = _read_small(root / "package.json")
    if text is not None:
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        for key in ("dependencies", "devDependencies"):
            names.update(_pkg(k) for k in _table(doc, key))
    text = _read_small(root / "pyproject.toml")
    if text is not None:
        try:
            py: dict[str, Any] = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, RecursionError):
            py = {}
        project = _table(py, "project")
        names.update(_req_names(project.get("dependencies")))
        for extra in _table(project, "optional-dependencies").values():
            names.update(_req_names(extra))
        for group in _table(py, "dependency-groups").values():
            names.update(_req_names(group))
        poetry = _table(py, "tool", "poetry")
        for key in ("dependencies", "dev-dependencies"):
            names.update(_pkg(k) for k in _table(poetry, key))
        for group in _table(poetry, "group").values():
            names.update(_pkg(k) for k in _table(group, "dependencies"))
    return names


def session_digest(session_id: str) -> str:
    """The stored form of a session id: blake2b, 8 bytes, hex."""
    return hashlib.blake2b(session_id.encode("utf-8"), digest_size=8).hexdigest()


# --------------------------------------------------------------------------- helpers
def _simple(norm: str) -> bool:
    """One plain command (no shell operators, quoting, expansion or leading ``cd``)."""
    return bool(norm) and _SHELL_CHARS.search(norm) is None


def _launches(head: list[str], program: str) -> bool:
    """``head`` runs ``program``: a launcher prefix + program, ``python -m program``, or a path
    whose last component is the program (``./foo``, ``.venv/bin/pytest``)."""
    if not head:
        return False
    last = head[-1]
    if len(head) == 1:
        return last != program and "/" in last and posixpath.basename(last) == program
    if last != program:
        return False
    pre = tuple(head[:-1])
    if pre in _LAUNCHERS:
        return True
    return len(pre) == 2 and _PYTHON.fullmatch(pre[0]) is not None and pre[1] == "-m"


_SUBCOMMAND = re.compile(r"[a-z][a-z:_-]{0,19}")  # no digits: a token like hunter2 is no subcommand
_KNOWN_SUBCOMMANDS = frozenset({"build", "test", "run", "start", "serve", "dev", "check", "lint"})
_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})


def _subcommand(word: str) -> bool:
    return word in _KNOWN_SUBCOMMANDS or _SUBCOMMAND.fullmatch(word) is not None


def _program(norm: str) -> str:
    """Program + subcommand only (``mysql``, ``make build``, ``npm run build``,
    ``python -m pytest``): options, files and arguments, which may carry secrets, are dropped.
    Empty (no fact) for a compound command (``cd x && ...``, pipes)."""
    if not _simple(norm):
        return ""
    words = norm.split(" ")
    if len(words) >= 3 and _PYTHON.fullmatch(words[0]) and words[1] == "-m":
        return " ".join(words[:3])
    if len(words) >= 3 and words[0] in _RUNNERS and words[1] == "run":
        if _subcommand(words[2]):
            return " ".join(words[:3])
    if len(words) >= 2 and _subcommand(words[1]):
        return " ".join(words[:2])
    return words[0]


def _error_text(text: str) -> str:
    if len(text) <= 2 * ERROR_SCAN_CHARS:
        return text
    return text[:ERROR_SCAN_CHARS] + "\n" + text[-ERROR_SCAN_CHARS:]


def _missing_program(words: list[str], text: str) -> tuple[list[str], str] | None:
    """(failed head, program) when the output says the program of ``words`` does not exist."""
    if not words:
        return None
    prog = posixpath.basename(words[0])
    for pat in _NOT_FOUND:
        if prog in pat.findall(text):
            return [words[0]], prog
    if len(words) >= 3 and _PYTHON.fullmatch(words[0]) and words[1] == "-m":
        mods = _NO_MODULE.findall(text)
        if words[2] in mods:
            return words[:3], words[2]
    return None


def _env_names(text: str) -> list[str]:
    out: list[str] = []
    for pat in _ENV_ERRORS:
        for name in pat.findall(text):
            if name not in out:
                out.append(name)
    return out[:8]


def _set_vars(cmd: str) -> set[str]:
    """Variables a command sets for itself: leading ``X=..`` assignments and ``export X=..``."""
    names: set[str] = set()
    lead = _LEAD_ASSIGNS.match(cmd.strip())
    if lead is not None:
        names.update(_ASSIGN.findall(lead.group(0)))
    for m in _EXPORT.finditer(cmd):
        names.update(_ASSIGN.findall(m.group(1)))
    return names


def _relative(path: str, project_dir: str | None) -> str | None:
    path = path.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        if project_dir is None:
            return None
        root = posixpath.normpath(project_dir.replace("\\", "/")).rstrip("/") + "/"
        if not path.startswith(root):
            return None
        path = path[len(root) :]
    if path.startswith("~") or ".." in path.split("/"):
        return None
    return posixpath.normpath(path)


def _gen_dir(path: str, project_dir: str | None) -> str | None:
    rel = _relative(path, project_dir)
    if rel is None:
        return None
    parts = rel.split("/")[:-1]
    for i, part in enumerate(parts):
        if part in GEN_DIRS:
            return "/".join(parts[: i + 1])
    return None


# --------------------------------------------------------------------------- test runners
# Recognised test-runner invocations (program + subcommand / module), data-driven: a test
# command's fact value is the matching row as written (the program token keeps its spelling,
# ``.venv/bin/pytest``, ``./gradlew``); every other argument is dropped. A row token starting
# with ``./`` (``go test ./...``) must appear among the remaining arguments instead of in place.
# The first token matches by basename; ``python`` matches any interpreter spelling.
TEST_RUNNERS: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("pytest",),
    ("tox",),
    ("nox",),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("pnpm", "test"),
    ("pnpm", "run", "test"),
    ("yarn", "test"),
    ("yarn", "run", "test"),
    ("npx", "jest"),
    ("npx", "vitest", "run"),  # vitest's one-shot (non-watch) mode
    ("npx", "vitest"),
    ("cargo", "test"),
    ("go", "test", "./..."),
    ("make", "test"),
    ("gradle", "test"),
    ("gradlew", "test"),
    ("mvn", "test"),
    ("dotnet", "test"),
    ("bundle", "exec", "rspec"),
    ("rake", "test"),
)
# Environment launchers kept in front of a runner (``uv run pytest``).
TEST_LAUNCHERS: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("pdm", "run"),
    ("hatch", "run"),
)
_CD_PREFIX = re.compile(r"cd (\S+) && (.+)")
_CONTROL = re.compile(r"[;&|<>`$]")
# Options that run the suite somewhere else or for a part of the workspace (a directory, a
# package, a manifest, a config root): such a run may be narrow, and no fact beats a narrow one.
# Matched as the whole word or ``flag=value``, for every runner; ``SCOPE_FLAGS_BY`` adds
# per-program ones (``-p`` is a package for cargo / gradle but a plugin for pytest).
SCOPE_FLAGS: frozenset[str] = frozenset(
    {
        "-C",
        "--directory",
        "--dir",
        "--cwd",
        "--prefix",
        "--manifest-path",
        "--package",
        "--project-dir",
        "--rootdir",
        "--workspace",
        "--projects",
    }
)
SCOPE_FLAGS_BY: dict[str, frozenset[str]] = {
    "cargo": frozenset({"-p"}),
    "gradle": frozenset({"-p"}),
    "gradlew": frozenset({"-p"}),
    "mvn": frozenset({"-pl", "-f", "--file"}),
    "make": frozenset({"-f", "--file", "--makefile"}),
    "pnpm": frozenset({"-w", "--filter", "-F"}),
    "yarn": frozenset({"-w"}),
    "npm": frozenset({"-w"}),
}
# Options that never narrow the selection but stop a watch / interactive mode: kept and
# rendered with the runner (after ``--`` when they were passed through it). ``CI=true`` in
# front of the command is kept likewise.
KEEP_FLAGS: tuple[str, ...] = ("--watchAll=false", "--watch=false", "--no-watch", "--run", "--ci")
CI_PREFIX = "CI=true"


def _runner_match(words: list[str]) -> list[str] | None:
    if not words:
        return None
    base = posixpath.basename(words[0])
    name = "python" if _PYTHON.fullmatch(base) else base
    for row in TEST_RUNNERS:
        if row[0] != name:
            continue
        fixed = [t for t in row[1:] if not t.startswith("./")]
        anywhere = [t for t in row[1:] if t.startswith("./")]
        if words[1 : 1 + len(fixed)] != fixed:
            continue
        rest = words[1 + len(fixed) :]
        if all(t in rest for t in anywhere):
            return [words[0], *fixed, *anywhere]
    return None


def _scoped(words: list[str]) -> bool:
    """A directory / package / config-root option anywhere in the command."""
    base = posixpath.basename(words[0]) if words else ""
    flags = SCOPE_FLAGS | SCOPE_FLAGS_BY.get(base, frozenset())
    return any(w.split("=", 1)[0] in flags for w in words[1:])


def test_invocation(norm: str) -> str | None:
    """The runner invocation of a (normalised) test command, or None when it matches no
    ``TEST_RUNNERS`` row or may be narrowed to a part of the project: ``pytest tests/unit -q
    --no-cov`` -> ``pytest``, ``go test -v ./...`` -> ``go test ./...``; a ``cd <dir> &&``
    prefix or a ``SCOPE_FLAGS`` option -> None. A ``TEST_LAUNCHERS`` prefix, a leading
    ``CI=true`` and ``KEEP_FLAGS`` are kept."""
    if _CD_PREFIX.fullmatch(norm) or _CONTROL.search(norm):
        return None  # another directory, a pipeline or a command list: not one plain full run
    words = [w for w in norm.split(" ") if w]
    ci = words[:1] == [CI_PREFIX]
    if ci:
        words = words[1:]
    head: list[str] = []
    for pre in TEST_LAUNCHERS:
        if tuple(words[: len(pre)]) == pre:
            head, words = list(pre), words[len(pre) :]
            break
    found = _runner_match(words)
    if found is None or _scoped(words):
        return None
    sep = words.index("--") if "--" in words else len(words)
    before = [w for w in words[:sep] if w in KEEP_FLAGS]
    after = [w for w in words[sep:] if w in KEEP_FLAGS]
    tail = [*before, *(["--", *after] if after else [])]
    return " ".join([*([CI_PREFIX] if ci else []), *head, *found, *tail])


def _words(cmd: str) -> list[str]:
    return [w for w in cmd.split(" ") if w]


_INTERPRETER = re.compile(r"(?:.*/)?(?:python(?:3(?:\.\d+)?)?|py)")
_LAUNCH_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("pdm", "run"),
    ("hatch", "run"),
    ("pnpm", "exec"),
    ("yarn", "exec"),
    ("npm", "exec"),
    ("npx",),
    ("bunx",),
    ("yarn",),
)
_SCRIPT_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
NPM_TEST = "npm-test-script"
# Lint / type-check commands are build-family but never regenerate files.
_CHECKERS = frozenset({"ruff", "mypy", "flake8", "eslint", "pyright", "pylint", "prettier"})
_TASK_RUNNERS = frozenset({"make", "just", "task"})
_LINTISH = re.compile(
    r"(?:^|[:_.-])(?:lint|typecheck|type-check|check|clippy|format|fmt|vet|style)(?:$|[:_.-])"
)


def runner(norm: str) -> str:
    """The normalised program of a command, independent of how it was launched and of its
    options, files and run directory: interpreter, ``python -m``, ``uv run``/``poetry run``/
    ``pipenv run``, ``npx``/``pnpm exec``/``yarn`` and paths are stripped (``pytest`` for
    ``.venv/bin/python -m pytest -q``); ``npm test``/``npm run test``/``pnpm test``/``yarn
    test`` are one ``npm-test-script``."""
    m = _CD_PREFIX.fullmatch(norm)
    words = _words(m.group(2) if m is not None else norm)
    while words and _ASSIGN.fullmatch(words[0]):
        words = words[1:]  # CI=true npm test
    if len(words) >= 2 and words[0] in _SCRIPT_RUNNERS:
        if words[1] == "test" or words[1:3] == ["run", "test"]:
            return NPM_TEST
    changed = True
    while changed and words:
        changed = False
        if _INTERPRETER.fullmatch(words[0]):
            words = words[2:] if words[1:2] == ["-m"] else words[1:]
            changed = True
            continue
        for pre in _LAUNCH_PREFIXES:
            if len(words) > len(pre) and tuple(words[: len(pre)]) == pre:
                if pre == ("yarn",) and words[1] in {"run", "test"}:
                    continue
                words = words[len(pre) :]
                changed = True
                break
    if not words:
        return ""
    if len(words) >= 2 and words[0] in _SCRIPT_RUNNERS and words[1:3] == ["run", "test"]:
        return NPM_TEST
    return posixpath.basename(words[0])


def checker(norm: str) -> bool:
    """A lint, format or type-check run: ruff, mypy, ``tsc --noEmit``, ``black --check``,
    ``cargo clippy``/``fmt``, ``go vet``/``fmt``, and npm/pnpm/yarn/bun scripts or make/just/
    task targets named like lint, typecheck, check, format, fmt, vet, style."""
    prog = runner(norm)
    m = _CD_PREFIX.fullmatch(norm)
    words = _words(m.group(2) if m is not None else norm)
    if prog in _CHECKERS:
        return True
    if (prog == "tsc" and "--noEmit" in words) or (prog == "black" and "--check" in words):
        return True
    head = words[0] if words else ""
    args = [w for w in words[1:] if not w.startswith("-")]
    if head in {"cargo", "go"}:
        return bool(args) and args[0] in {"clippy", "fmt", "vet"}
    if head in _SCRIPT_RUNNERS:
        script = args[1:2] if args[:1] == ["run"] else args[:1]
        return any(_LINTISH.search(a) for a in script)
    if head in _TASK_RUNNERS:
        return bool(args) and all(_LINTISH.search(a) for a in args)
    return False


# --------------------------------------------------------------------------- generated dirs
def _git(root: str, *args: str) -> int | None:
    """Return code of a git command in ``root``; None when git is missing, fails to start or is
    slower than ``GIT_TIMEOUT_S``. Repository-location variables are dropped so ``root``
    decides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def git_ignored(root: str, rel_dir: str) -> bool | None:
    """True / False when ``git check-ignore`` says the project-relative directory is / is not
    ignored; None when git cannot tell (missing, timeout, not a repository)."""
    code = _git(root, "check-ignore", "-q", "--", rel_dir.rstrip("/") + "/")
    return True if code == 0 else False if code == 1 else None


def _has_marker(path: Path) -> bool | None:
    """A generated marker in the first ``GEN_MARKER_LINES`` lines; None if unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as fh:
            head = fh.read(GEN_MARKER_BYTES)
    except OSError:
        return None
    lines = head.decode("utf-8", errors="replace").splitlines()[:GEN_MARKER_LINES]
    return any(GEN_MARKER.search(line) for line in lines)


def _sample(root: str, rel_dir: str) -> list[str]:
    """Up to ``GEN_MAX_FILES`` project-relative files under the directory (no symlinks)."""
    out: list[str] = []
    base = Path(root) / rel_dir
    for here, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            out.append(posixpath.join(rel_dir, Path(here).relative_to(base).as_posix(), name))
            if len(out) >= GEN_MAX_FILES:
                return [posixpath.normpath(p) for p in out]
    return [posixpath.normpath(p) for p in out]


def generated_evidence(root: str | None, rel_dir: str, files: Iterable[str]) -> bool | None:
    """Positive evidence that ``rel_dir`` holds generated files: True when git ignores it or
    every existing file of ``files`` (project-relative; the directory's own files when empty)
    carries a generated marker; False when git positively says it is not ignored and a file
    lacks the marker (hand-written: a contradiction); None when nothing can be said."""
    if root is None or not os.path.isabs(root):
        return None
    ignored = git_ignored(root, rel_dir)
    if ignored:
        return True
    names = list(files)[:GEN_MAX_FILES] or _sample(root, rel_dir)
    marks = [m for m in (_has_marker(Path(root) / n) for n in names) if m is not None]
    if marks and all(marks):
        return True
    if ignored is False and marks:
        return False
    return None


def _with_ci(cmd: str, norm: str) -> str:
    """``norm`` with ``CI=true`` back in front when the raw command set it as a leading
    assignment (``normalize_command`` drops assignments)."""
    lead = _LEAD_ASSIGNS.match(cmd.strip())
    if lead is not None and CI_PREFIX in lead.group(0).split():
        return f"{CI_PREFIX} {norm}"
    return norm


def _bump(counter: Counter[str], key: str) -> None:
    if key in counter or len(counter) < MAX_KEYS:
        counter[key] += 1


# --------------------------------------------------------------------------- miner
@dataclass
class _Alias:
    head: list[str]
    tail: list[str]
    program: str
    left: int = ALIAS_WINDOW


@dataclass
class _Miner:
    project_dir: str | None
    found: list[tuple[FactKind, str, str]] = field(default_factory=list)
    turn: int = -1
    aliases: list[_Alias] = field(default_factory=list)
    env_waits: list[tuple[str, str]] = field(default_factory=list)  # (normalised cmd, VAR)
    exported: set[str] = field(default_factory=set)
    failed_tests: set[str] = field(default_factory=set)
    prev: tuple[str, bool, str] | None = None  # previous Bash call: (norm, ok, family)
    tests_ok: Counter[str] = field(default_factory=Counter)
    gen: Counter[str] = field(default_factory=Counter)
    gen_files: dict[str, list[str]] = field(default_factory=dict)  # dir -> edited files
    declared: set[str] | None = None  # declared_packages, read on first need

    def _allowed_launch(self, head: list[str], program: str) -> bool:
        """A downloading launcher (``npx X``, ``uvx X``, ``pnpm dlx X``...) only for a package
        the project declares; any other spelling is fine."""
        if not any(w in _DOWNLOADERS for w in head[:-1]):
            return True
        if self.declared is None:
            self.declared = declared_packages(self.project_dir)
        return _pkg(program) in self.declared

    def emit(self, kind: FactKind, subject: str, value: str) -> None:
        if (kind, subject, value) not in self.found and len(self.found) < MAX_KEYS:
            self.found.append((kind, subject, value))

    def feed(self, call: ToolResultRecord) -> None:
        if call.turn_index != self.turn:
            self.turn = call.turn_index
            self.aliases.clear()
            self.env_waits.clear()
            self.exported.clear()
        new: _Alias | None = None
        if call.tool_name == "Bash":
            cmd = call.input.get("command")
            if isinstance(cmd, str):
                new = self._bash(cmd, not call.is_error, call.text)
        else:
            self.prev = None  # build_first: only a build immediately before the test counts
        if call.tool_name in _EDIT_TOOLS:
            self.failed_tests.clear()  # the edit may be the fix: a later pass proves no build
            target = call.input.get("file_path") or call.input.get("notebook_path")
            if isinstance(target, str):
                where = _gen_dir(target, self.project_dir)
                if where is not None:
                    _bump(self.gen, where)
                    files = self.gen_files.setdefault(where, [])
                    rel = _relative(target, self.project_dir)
                    if rel is not None and rel not in files and len(files) < GEN_MAX_FILES:
                        files.append(rel)
        for a in self.aliases:
            a.left -= 1
        self.aliases = [a for a in self.aliases if a.left > 0]
        if new is not None:
            self.aliases.append(new)

    def _bash(self, cmd: str, ok: bool, text: str) -> _Alias | None:
        norm = normalize_command(cmd, project_dir=self.project_dir)
        words = norm.split(" ")
        fam = command_family(cmd)
        new: _Alias | None = None
        if ok:
            self._alias_ok(norm, words)
            self._env_ok(cmd)
            invocation = test_invocation(_with_ci(cmd, norm))
            if invocation is not None:
                _bump(self.tests_ok, invocation)
            if fam == "test":
                if norm in self.failed_tests and self.prev is not None:
                    pnorm, pok, pfam = self.prev
                    if pok and pfam == "build" and not checker(pnorm):
                        self.emit("build_first", _program(norm), _program(pnorm))
                self.failed_tests.discard(norm)
        else:
            err = _error_text(text)
            if _simple(norm):
                miss = _missing_program(words, err)
                if miss is not None:
                    head, prog = miss
                    new = _Alias(head, words[len(head) :], prog)
            for name in _env_names(err):
                if len(self.env_waits) < 64:
                    self.env_waits.append((norm, name))
            if fam == "test" and len(self.failed_tests) < 200:
                self.failed_tests.add(norm)
        self.prev = (norm, ok, fam)
        return new

    def _alias_ok(self, norm: str, words: list[str]) -> None:
        if not _simple(norm):
            return
        keep: list[_Alias] = []
        for a in self.aliases:
            n = len(a.tail)
            if len(words) > n and words[len(words) - n :] == a.tail:
                head = words[: len(words) - n]
                if head != a.head and _launches(head, a.program):
                    if self._allowed_launch(head, a.program):
                        self.emit("command_alias", " ".join(a.head), " ".join(head))
                    continue
            keep.append(a)
        self.aliases = keep

    def _env_ok(self, cmd: str) -> None:
        own = _set_vars(cmd)
        if self.env_waits:
            base = normalize_command(
                _LEAD_EXPORT.sub("", cmd.strip()), project_dir=self.project_dir
            )
            left: list[tuple[str, str]] = []
            for fnorm, name in self.env_waits:
                if base == fnorm and (name in own or name in self.exported):
                    self.emit("requires_env", _program(fnorm), name)
                else:
                    left.append((fnorm, name))
            self.env_waits = left
        for m in _EXPORT.finditer(cmd):
            self.exported.update(_ASSIGN.findall(m.group(1)))

    def finish(self) -> list[tuple[FactKind, str, str]]:
        out = list(self.found)
        safe = [
            c
            for c in self.tests_ok
            if sanitize(Fact("test_command", "tests", c, frozenset()), project_dir=self.project_dir)
        ]
        if safe:
            best = max(safe, key=lambda c: self.tests_ok[c])  # first seen wins a tie
            out.append(("test_command", "tests", best))
        checked = 0
        for where, n in self.gen.items():
            if n < GEN_MIN_PER_SESSION or checked >= GEN_MAX_DIRS:
                continue
            checked += 1
            if generated_evidence(self.project_dir, where, self.gen_files.get(where, ())):
                out.append(("generated_dir", "generated", where))
        return out


def relativise(text: str, project_dir: str | None) -> str:
    """In-project absolute paths in a fact field made project-relative (POSIX); anything else
    is left for ``sanitize`` to judge (an outside absolute path is still rejected)."""
    if project_dir is None:
        return text
    root = posixpath.normpath(project_dir.replace("\\", "/"))
    if not root.startswith("/") or root == "/":
        return text
    pat = re.compile(r"(?:^|(?<=[ =:@+]))" + re.escape(root.rstrip("/") + "/"))
    return pat.sub("", text)


def mine_calls(
    calls: Iterable[ToolResultRecord], session_id: str, *, project_dir: str | None
) -> list[Fact]:
    """Sanitised candidate facts of one session's main-chain tool calls (in file order)."""
    miner = _Miner(project_dir)
    for call in calls:
        if not call.sidechain:
            miner.feed(call)
    ev = frozenset({session_digest(session_id)})
    out: list[Fact] = []
    for kind, subject, value in _relativised(miner.finish(), project_dir):
        fact = sanitize(Fact(kind, subject, value, ev))
        if fact is not None:
            out.append(fact)
    return out


def _relativised(
    found: list[tuple[FactKind, str, str]], project_dir: str | None
) -> list[tuple[FactKind, str, str]]:
    out: list[tuple[FactKind, str, str]] = []
    for kind, subject, value in found:
        item = (kind, relativise(subject, project_dir), relativise(value, project_dir))
        if item not in out:
            out.append(item)
    return out


def mine_transcript(
    path: Path,
    session_id: str,
    *,
    project_dir: str | None,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    env: Mapping[str, str] | None = None,
    tap: Callable[[ToolResultRecord], None] | None = None,
) -> list[Fact]:
    """Mine one transcript, reading at most ``max_bytes`` (the cut is logged). ``tap`` sees
    every call too (the fact lifecycle observes the session in the same single read)."""
    log_env: Mapping[str, str] = env if env is not None else {}

    def on_limit() -> None:
        log_error(log_env, "facts", f"transcript over the {max_bytes} byte budget: mined a prefix")

    calls = iter_tool_results(path, max_bytes=max_bytes, on_limit=on_limit)
    return mine_calls(_tapped(calls, tap), session_id, project_dir=project_dir)


def _tapped(
    calls: Iterable[ToolResultRecord], tap: Callable[[ToolResultRecord], None] | None
) -> Iterator[ToolResultRecord]:
    for call in calls:
        if tap is not None:
            tap(call)
        yield call


# --------------------------------------------------------------------------- store
def _entry(raw: object) -> Fact | None:
    """A stored entry as a sanitised Fact (tampered or malformed entries are dropped)."""
    if not isinstance(raw, dict):
        return None
    kind, subject, value = raw.get("kind"), raw.get("subject"), raw.get("value")
    sessions = raw.get("sessions")
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(sessions, list):
        return None
    if not isinstance(subject, str) or not isinstance(value, str):
        return None
    ev = frozenset(s for s in sessions if isinstance(s, str))
    return sanitize(Fact(kind, subject, value, ev))  # type: ignore[arg-type]


def load(pdir: Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The project's fact store (an empty v1 document when missing or unreadable)."""
    doc = jsonstore.read(pdir / FACTS_FILE, {}, env)
    if doc.get("version") != VERSION or not isinstance(doc.get("facts"), dict):
        return {"version": VERSION, "facts": {}}
    return doc


def eligible(doc: Mapping[str, Any], min_sessions: int = MIN_SESSIONS) -> list[Fact]:
    """Stored facts seen in at least ``min_sessions`` distinct sessions (re-sanitised)."""
    facts = doc.get("facts")
    out: list[Fact] = []
    for fid, raw in facts.items() if isinstance(facts, dict) else ():
        fact = _entry(raw)
        if (
            fact is not None
            and fact_id(fact) == fid
            and len(fact.evidence_sessions) >= min_sessions
        ):
            out.append(fact)
    return out


def _num(value: object, default: float) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    return float(value) if ok else default  # type: ignore[arg-type]


def record(
    pdir: Path,
    facts: list[Fact],
    *,
    now: float | None = None,
    env: Mapping[str, str],
    fold_id: str | None = None,
    timeout: float | None = None,
    after: Callable[[dict[str, Any], set[str]], None] | None = None,
) -> int:
    """Merge candidate facts into ``facts.json`` (sessions unioned, idempotent per fold id).
    Returns the facts written; a future schema version is left untouched (logged). Lifecycle
    fields of an entry (``facts.lifecycle``) and the document's ``disabled`` / ``forgotten``
    are kept; a forgotten fact id is never recorded again. ``after(table, fresh)`` runs under
    the same lock, once per fold id (the lifecycle step); ``fresh`` = the ids that gained new
    session evidence."""
    if not facts and fold_id is None:
        return 0
    when = time.time() if now is None else now
    written: list[int] = [0]

    def fn(doc: dict[str, Any]) -> dict[str, Any]:
        version = doc.get("version")
        if isinstance(version, int) and not isinstance(version, bool) and version > VERSION:
            log_error(env, "facts", f"facts.json version {version} is newer: not updated")
            return doc
        store = doc.get("facts") if version == VERSION else None
        table: dict[str, Any] = store if isinstance(store, dict) else {}
        raw_applied = doc.get("applied")
        applied: list[Any] = raw_applied if isinstance(raw_applied, list) else []
        if fold_id is not None and fold_id in applied:
            return doc
        raw_forgotten = doc.get("forgotten")
        forgotten = (
            [f for f in raw_forgotten if isinstance(f, str)]
            if isinstance(raw_forgotten, list)
            else []
        )
        _renormalise(table, forgotten)
        fresh: set[str] = set()
        for fact in facts:
            safe = sanitize(fact)
            if safe is None:
                continue
            fid = fact_id(safe)
            if fid in forgotten:
                continue
            cur = table.get(fid)
            old = _entry(cur)
            sessions = list(cur["sessions"]) if old is not None and isinstance(cur, dict) else []
            sessions = [s for s in sessions if isinstance(s, str)]
            for s in sorted(safe.evidence_sessions):
                if s not in sessions:
                    sessions.append(s)
                    fresh.add(fid)  # new session evidence (a re-fold of a session adds none)
            prev = cur if isinstance(cur, dict) and old is not None else {}
            table[fid] = {
                **prev,
                "kind": safe.kind,
                "subject": safe.subject,
                "value": safe.value,
                "sessions": sessions[-MAX_SESSIONS:],
                "first_seen": _num(prev.get("first_seen"), when),
                "last_seen": when,
                "contradictions": int(_num(prev.get("contradictions"), 0.0)),
            }
            if safe.kind == "generated_dir":
                table[fid]["verified"] = True  # mined only with git / marker evidence
            written[0] += 1
        if after is not None:
            after(table, fresh)
        if len(table) > MAX_FACTS:
            order = sorted(
                table,
                key=lambda k: (
                    _num(table[k].get("last_seen"), 0.0) if isinstance(table[k], dict) else 0.0
                ),
            )
            for k in order[: len(table) - MAX_FACTS]:
                del table[k]
        out: dict[str, Any] = {"version": VERSION, "facts": table}
        if doc.get("disabled") is True:
            out["disabled"] = True
        if forgotten:
            out["forgotten"] = forgotten[-MAX_FACTS:]
        if fold_id is not None:
            out["applied"] = [*[a for a in applied if isinstance(a, str)], fold_id][-APPLIED_MAX:]
        elif applied:
            out["applied"] = applied
        return out

    jsonstore.update(pdir / FACTS_FILE, fn, {}, env, timeout=timeout)
    return written[0]


def _renormalise(table: dict[str, Any], forgotten: Collection[str] = ()) -> None:
    """Stored test_command facts whose value is not a runner invocation (stored before fix
    wave B) are moved to the invocation (in place): sessions are unioned into an existing
    entry, which keeps its own lifecycle; otherwise the entry moves with its lifecycle. A value
    with no invocation, or one that fails the sanitiser, is retired; one whose invocation was
    forgotten is dropped."""
    for fid in list(table):
        entry = table.get(fid)
        if not isinstance(entry, dict) or entry.get("kind") != "test_command":
            continue
        value, subject = entry.get("value"), entry.get("subject")
        if not isinstance(value, str) or not isinstance(subject, str):
            continue
        inv = test_invocation(value)
        if inv == value:
            continue
        new = sanitize(Fact("test_command", subject, inv, frozenset())) if inv else None
        if new is None:
            entry["status"] = "retired"
            continue
        del table[fid]
        nid = fact_id(new)
        if nid in forgotten:
            continue
        raw = entry.get("sessions")
        sessions = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
        target = table.get(nid)
        if isinstance(target, dict):
            old = target.get("sessions")
            have = [s for s in old if isinstance(s, str)] if isinstance(old, list) else []
            merged = [*have, *(s for s in sessions if s not in have)]
            target["sessions"] = merged[-MAX_SESSIONS:]
            target["last_seen"] = max(
                _num(target.get("last_seen"), 0.0), _num(entry.get("last_seen"), 0.0)
            )
        else:
            table[nid] = {**entry, "value": inv, "sessions": sessions[-MAX_SESSIONS:]}


# --------------------------------------------------------------------------- shared
# Used by ``facts.lifecycle`` to observe a session with the miners' own vocabulary.
EDIT_TOOLS = _EDIT_TOOLS
NOT_FOUND = _NOT_FOUND
NO_MODULE = _NO_MODULE
EXPORT = _EXPORT
ASSIGN = _ASSIGN
error_text = _error_text
missing_program = _missing_program
env_names = _env_names
set_vars = _set_vars
relative_path = _relative
program_of = _program
parse_entry = _entry
