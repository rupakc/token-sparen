"""Command families of shell tool calls, for E1 aggregates (no command text is ever reported).

``command_family(cmd)`` normalises the command (``regret.normalize_command``: leading ``cd``,
assignments and ``env`` dropped), takes the first simple command of a pipeline or chain, skips
wrappers (``sudo``, ``time``, ``nohup``, ``command``, ``exec``, ``npx``/``bunx``/``uvx``,
``uv run``, ``poetry run``, ``pipenv run``, ``python -m``) and classifies the program (plus its
subcommand for git, npm/pnpm/yarn/bun, cargo, go, pip/uv, docker, kubectl, make, gradle, mvn).
Linters and type checkers count as ``build`` (static checks of the build).
"""

from __future__ import annotations

import os
import shlex

from token_sparen.learn import regret

FAMILIES = (
    "test",
    "build",
    "install",
    "listing",
    "file_dump",
    "vcs_read",
    "container_cloud",
    "http",
    "other",
)
_SEPS = frozenset({"|", "||", "&&", ";", "&", "|&", "(", ")"})
_WRAPPERS = frozenset({"sudo", "time", "nohup", "command", "exec", "npx", "bunx", "uvx", "env"})
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run"}
_PY = frozenset({"python", "python3", "py"})
_TEST = frozenset(
    {
        "pytest",
        "py.test",
        "jest",
        "vitest",
        "mocha",
        "tox",
        "nox",
        "unittest",
        "rspec",
        "phpunit",
        "ctest",
        "playwright",
        "cypress",
        "ava",
        "tap",
        "nextest",
    }
)
_BUILD = frozenset(
    {
        "tsc",
        "make",
        "gradle",
        "gradlew",
        "./gradlew",
        "mvn",
        "javac",
        "gcc",
        "g++",
        "clang",
        "cmake",
        "ninja",
        "webpack",
        "rollup",
        "esbuild",
        "mypy",
        "ruff",
        "eslint",
        "pyright",
        "flake8",
        "pylint",
        "black",
        "prettier",
        "rustc",
        "swiftc",
        "bazel",
    }
)
_INSTALL = frozenset({"apt", "apt-get", "brew", "dnf", "yum", "apk", "conda"})
_LISTING = frozenset(
    {"ls", "find", "tree", "rg", "grep", "egrep", "fgrep", "fd", "ag", "du", "wc", "locate"}
)
_DUMP = frozenset({"cat", "head", "tail", "sed", "less", "more", "bat", "nl", "awk", "jq", "xxd"})
_CLOUD = frozenset(
    {
        "docker",
        "docker-compose",
        "podman",
        "kubectl",
        "helm",
        "gcloud",
        "gsutil",
        "bq",
        "aws",
        "az",
        "terraform",
        "minikube",
        "kind",
    }
)
_HTTP = frozenset({"curl", "wget", "http", "https", "httpie", "xh"})
_GIT_READ = frozenset({"log", "diff", "show", "status", "blame", "ls-files", "shortlog", "grep"})
_GIT_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree"})
_JS = frozenset({"npm", "pnpm", "yarn", "bun"})


def _words(cmd: str) -> list[str]:
    """The first simple command that is not a ``cd``/``pushd`` (those only set the directory)."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    current: list[str] = []
    for w in [*words, ";"]:
        if w not in _SEPS:
            current.append(w)
            continue
        if current and current[0] not in ("cd", "pushd"):
            return current
        current = []
    return []


def _strip(words: list[str]) -> list[str]:
    while words:
        head = os.path.basename(words[0])
        if "=" in words[0] and not words[0].startswith("="):
            words = words[1:]  # VAR=value
        elif head in _WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        elif head in _RUNNERS and len(words) > 1 and words[1] == _RUNNERS[head]:
            words = words[2:]
        elif head in _PY and len(words) > 2 and words[1] == "-m":
            words = words[2:]
        else:
            break
    return words


def _sub(words: list[str]) -> str:
    """The first non-option argument after the program."""
    return next((w for w in words[1:] if not w.startswith("-")), "")


def _js(words: list[str]) -> str:
    sub = _sub(words)
    if sub in ("test", "t"):
        return "test"
    if sub in ("install", "i", "add", "ci"):
        return "install"
    if sub == "run" or (sub and sub not in ("exec", "x", "dlx")):
        rest = [w for w in words[1:] if not w.startswith("-")]
        script = rest[1] if sub == "run" and len(rest) > 1 else sub
        if "test" in script:
            return "test"
        if any(k in script for k in ("build", "lint", "typecheck", "compile", "tsc")):
            return "build"
    return "other"


def _git(words: list[str]) -> str:
    rest = words[1:]
    while rest:
        if rest[0] in _GIT_OPTS:
            rest = rest[2:]
        elif rest[0].startswith("-"):
            rest = rest[1:]
        else:
            break
    return "vcs_read" if rest and rest[0] in _GIT_READ else "other"


def command_family(cmd: str) -> str:
    """One of ``FAMILIES`` for a shell command; ``other`` when unknown or unparsable."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "other"
    words = _strip(_words(regret.normalize_command(cmd)))
    if not words:
        return "other"
    prog = os.path.basename(words[0])
    sub = _sub(words)
    if prog == "git":
        return _git(words)
    if prog in _JS:
        return _js(words)
    if prog == "cargo":
        if sub in ("test", "nextest", "bench"):
            return "test"
        if sub in ("build", "check", "clippy", "fmt"):
            return "build"
        return "install" if sub in ("install", "add", "fetch") else "other"
    if prog == "go":
        if sub == "test":
            return "test"
        if sub in ("build", "vet"):
            return "build"
        return "install" if sub in ("get", "install", "mod") else "other"
    if prog in ("pip", "pip3", "uv"):
        if prog == "uv" and sub == "pip":
            sub = next((w for w in words[2:] if not w.startswith("-")), "")
        return "install" if sub in ("install", "sync", "add") else "other"
    if prog in ("make", "gradle", "gradlew", "./gradlew", "mvn"):
        return "test" if sub in ("test", "check", "verify") else "build"
    if prog in _TEST:
        return "test"
    if prog in _BUILD:
        return "build"
    if prog in _INSTALL or (prog in ("poetry", "pipenv") and sub in ("install", "add", "sync")):
        return "install"
    if prog in _LISTING:
        return "listing"
    if prog in _DUMP:
        return "file_dump"
    if prog in _CLOUD:
        return "container_cloud"
    if prog in _HTTP:
        return "http"
    return "other"
