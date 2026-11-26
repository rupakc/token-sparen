"""Command families of shell tool calls, shared by production gating and E1 replay.

``command_family(cmd)`` normalises the command (``regret.normalize_command``: leading ``cd``,
assignments and ``env`` dropped), splits it into simple commands, skips leading setup commands
that are chained (not piped) to a later one (``cd``, ``pushd``, ``source``/``.``, ``export``,
``set``, ``echo``, ``printf``, ``mkdir``, ...), takes the first remaining pipeline, skips
wrappers with their options (``sudo -u U``, ``timeout N``, ``nice -n N``, ``env -u V``, ``time``,
``nohup``, ``command``, ``exec``, ``npx``/``bunx``/``uvx``, ``uv``/``poetry``/``pipenv``/``pdm``/
``hatch run``, ``python[3[.X]] -m``), classifies a shell's inline script (``bash -c '...'``,
``sh -lc``) recursively and the program (plus its subcommand for git, npm/pnpm/yarn/bun incl.
``exec``/``dlx``/``x``, cargo, go, pip/uv, make, gradle, mvn). Linters and type checkers count as
``build``. A later pipeline stage ``xargs <file dump>`` (``... | xargs cat``) makes the whole
command a ``file_dump``, as do ``git show <rev>:<path>`` and ``git cat-file -p|blob``.

``file_dump`` is the family whose output is a file's contents: by default it is never trimmed
(``compression.gated``). No command text is ever reported, only the family name.
"""

from __future__ import annotations

import os
import re
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
MAX_DEPTH = 3  # nested `bash -c` scripts classified at most this deep
_PIPES = frozenset({"|", "|&"})
_SEPS = frozenset({"||", "&&", ";", "&", "(", ")", *_PIPES})
# Chained setup commands that produce no output worth classifying (skipped unless piped or last).
_SETUP = frozenset(
    {
        "cd",
        "pushd",
        "popd",
        "source",
        ".",
        "export",
        "set",
        "unset",
        "echo",
        "printf",
        "true",
        ":",
        "mkdir",
        "ulimit",
        "umask",
        "alias",
        "trap",
        "clear",
    }
)
_NO_OUTPUT = frozenset({"cd", "pushd", "popd"})  # never classified, even when last
# Wrappers and the options that take a separate argument.
_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-t", "-T"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a", "--max-args"}),
    "time": frozenset({"-f", "-o"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "npx": frozenset({"-p", "--package"}),
    "bunx": frozenset(),
    "uvx": frozenset({"--from", "--with", "--python"}),
    "stdbuf": frozenset(),
}
_POSITIONAL = {"timeout": 1}  # wrapper arguments after the options (the duration)
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run"}
_PY = re.compile(r"python(?:\d+(?:\.\d+)?)?|py")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
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
        "vite",
    }
)
_INSTALL = frozenset({"apt", "apt-get", "brew", "dnf", "yum", "apk", "conda"})
_LISTING = frozenset(
    {"ls", "find", "tree", "rg", "grep", "egrep", "fgrep", "fd", "ag", "du", "wc", "locate"}
)
_DUMP = frozenset(
    {
        "cat",
        "head",
        "tail",
        "sed",
        "less",
        "more",
        "bat",
        "nl",
        "awk",
        "jq",
        "xxd",
        "od",
        "hexdump",
        "tac",
        "zcat",
    }
)
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
_JS_EXEC = frozenset({"exec", "x", "dlx"})


def _segments(cmd: str) -> list[tuple[list[str], str]]:
    """Simple commands with the separator that follows each (``;`` after the last)."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    out: list[tuple[list[str], str]] = []
    current: list[str] = []
    for w in [*words, ";"]:
        if w not in _SEPS:
            current.append(w)
            continue
        if current:
            out.append((current, w))
        current = []
    return out


def _strip(words: list[str]) -> list[str]:
    """Drop ``VAR=value`` assignments, wrappers (with their options) and runners."""
    while words:
        head = os.path.basename(words[0])
        if "=" in words[0] and not words[0].startswith("="):
            words = words[1:]  # VAR=value
        elif head in _WRAPPERS:
            takes_arg = _WRAPPERS[head]
            words = words[1:]
            while words and words[0].startswith("-"):
                opt = words[0]
                words = words[1:]
                if opt == "--":
                    break
                combined = (  # `-iu U`: the last flag of a short-option group takes the arg
                    len(opt) > 2 and opt[1] != "-" and "=" not in opt and f"-{opt[-1]}" in takes_arg
                )
                if (opt in takes_arg or combined) and words:
                    words = words[1:]
            words = words[_POSITIONAL.get(head, 0) :]
        elif head in _RUNNERS and len(words) > 1 and words[1] == _RUNNERS[head]:
            words = words[2:]
        elif _PY.fullmatch(head) and len(words) > 2 and words[1] == "-m":
            words = words[2:]
        else:
            break
    return words


def _sub(words: list[str]) -> str:
    """The first non-option argument after the program."""
    return next((w for w in words[1:] if not w.startswith("-")), "")


def _js(words: list[str], depth: int) -> str:
    sub = _sub(words)
    if sub in _JS_EXEC:
        rest = words[words.index(sub) + 1 :]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _classify(rest, depth) if rest else "other"
    if sub in ("test", "t"):
        return "test"
    if sub in ("install", "i", "add", "ci"):
        return "install"
    if sub in _TEST:
        return "test"  # `yarn jest`
    if sub in _BUILD:
        return "build"  # `yarn eslint`
    if sub == "run" or sub:
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
    if not rest:
        return "other"
    args = [w for w in rest[1:] if not w.startswith("-")]
    if rest[0] == "show" and any(":" in a and "://" not in a for a in args):
        return "file_dump"  # git show <rev>:<path>
    if rest[0] == "cat-file":
        flags = set(rest[1:])
        return "file_dump" if "-p" in flags or "blob" in args else "other"
    return "vcs_read" if rest[0] in _GIT_READ else "other"


def _shell_script(words: list[str]) -> str | None:
    """The inline script of ``bash -c '...'`` (``-lc``, ``-ec`` ...), else None."""
    for i, w in enumerate(words[1:], start=1):
        if w == "--" or not w.startswith("-"):
            return None
        if not w.startswith("--") and "c" in w[1:]:
            return words[i + 1] if i + 1 < len(words) else None
    return None


def _classify(words: list[str], depth: int) -> str:
    """Family of one simple command (wrappers not yet stripped)."""
    words = _strip(words)
    if not words:
        return "other"
    prog = os.path.basename(words[0])
    sub = _sub(words)
    if prog in _SHELLS:
        script = _shell_script(words)
        if script is None or depth >= MAX_DEPTH:
            return "other"
        return _family(script, depth + 1)
    if prog == "git":
        return _git(words)
    if prog in _JS:
        return _js(words, depth)
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


def _xargs_dump(words: list[str]) -> bool:
    """A pipeline stage ``xargs [opts] <file dump program>``."""
    if not words or os.path.basename(words[0]) != "xargs":
        return False
    rest = _strip(words)
    return bool(rest) and os.path.basename(rest[0]) in _DUMP


def _family(cmd: str, depth: int) -> str:
    segments = _segments(regret.normalize_command(cmd))
    start = 0
    while start < len(segments):
        words, sep = segments[start]
        head = os.path.basename(_strip(words)[0]) if _strip(words) else ""
        last = start == len(segments) - 1
        if head in _NO_OUTPUT or (head in _SETUP and sep not in _PIPES and not last):
            start += 1
            continue
        break
    if start >= len(segments):
        return "other"
    family = _classify(segments[start][0], depth)
    i = start
    while segments[i][1] in _PIPES and i + 1 < len(segments):
        i += 1
        if _xargs_dump(segments[i][0]):
            return "file_dump"
    return family


def command_family(cmd: str) -> str:
    """One of ``FAMILIES`` for a shell command; ``other`` when unknown or unparsable."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "other"
    return _family(cmd, 0)


def is_file_dump(cmd: str) -> bool:
    """For gating only: ``filedump.is_file_dump`` (any part of the command prints a file)."""
    from token_sparen.compress import filedump

    return filedump.is_file_dump(cmd)
