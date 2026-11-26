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


# ----------------------------------------------------------------------- gating (any segment)

# Programs that print a file when given a file argument (the options that take a separate value).
_FILE_ARG: dict[str, frozenset[str]] = {
    "sort": frozenset({"-k", "-t", "-o", "-S", "-T", "--key", "--field-separator", "--output"}),
    "uniq": frozenset({"-f", "-s", "-w"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields"}),
    "rev": frozenset(),
    "strings": frozenset({"-n", "-t", "-e"}),
    "tac": frozenset({"-s"}),
}
# File-dump programs: (options taking a value, leading script operands, options that replace the
# script operand). Without a file operand they read stdin (`... | head`), which is not a file dump.
_N = frozenset({"-n", "-c", "--lines", "--bytes"})
_DUMP_ARGS: dict[str, tuple[frozenset[str], int, frozenset[str]]] = {
    "head": (_N, 0, frozenset()),
    "tail": (_N | {"-s", "--pid"}, 0, frozenset()),
    "sed": (frozenset({"-e", "-f", "-l"}), 1, frozenset({"-e", "-f"})),
    "awk": (frozenset({"-F", "-v", "-f"}), 1, frozenset({"-f"})),
    "jq": (frozenset({"-f", "--indent", "--arg", "--argjson"}), 1, frozenset({"-f"})),
    "bat": (frozenset({"-l", "-r", "-H", "--language", "--line-range"}), 0, frozenset()),
    "xxd": (frozenset({"-l", "-s", "-c", "-g"}), 0, frozenset()),
    "od": (frozenset({"-N", "-j", "-t", "-A", "-w"}), 0, frozenset()),
    "hexdump": (frozenset({"-n", "-s", "-e"}), 0, frozenset()),
    "nl": (frozenset({"-w", "-s", "-b", "-v", "-i"}), 0, frozenset()),
}
_PS_DUMP = frozenset({"get-content", "gc", "type"})  # PowerShell / cmd.exe
_CODE_READS = re.compile(r"\bopen\s*\(|read_text\s*\(|read_bytes\s*\(|readFileSync|readFile\s*\(")
_CODE_FLAGS = {"python": ("-c",), "node": ("-e", "-p", "--eval", "--print")}
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "{", "}", "!", "done"})
_LOOP_HEADS = frozenset({"for", "select", "case"})
_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _all_segments(cmd: str) -> list[list[str]]:
    """Every simple command, split on any separator and on newlines."""
    return [words for words, _sep in _segments(cmd.replace("\n", " ; "))]


def _has_file_arg(
    words: list[str],
    takes_arg: frozenset[str],
    script: int = 0,
    script_opts: frozenset[str] = frozenset(),
) -> bool:
    """A file operand after options (and ``script`` leading script operands, none when one of
    ``script_opts`` was given), or a ``< file`` redirection; a here-document is not a file."""
    rest = words[1:]
    operands = 0
    skip = script
    only_operands = False
    while rest:
        w = rest[0]
        rest = rest[1:]
        if w in ("<<", "<<<", "<<-", ">", ">>", "2>", "&>") or w.endswith(">"):
            rest = rest[1:]  # here-doc / output redirection and its word
            continue
        if w == "<":
            return bool(rest)  # stdin from a file
        if w == "--" and not only_operands:
            only_operands = True
            continue
        if not only_operands and w.startswith("-") and len(w) > 1:
            if w in script_opts:
                skip = 0
            if w in takes_arg and rest:
                rest = rest[1:]
            continue
        operands += 1
        if operands > skip:
            return True
    return False


def _code_reads_file(prog: str, words: list[str]) -> bool:
    kind = "python" if _PY.fullmatch(prog) else prog if prog in ("node", "deno", "bun") else ""
    flags = _CODE_FLAGS.get("node" if kind in ("deno", "bun") else kind, ())
    for i, w in enumerate(words[1:-1], start=1):
        if w in flags and _CODE_READS.search(words[i + 1]):
            return True
    return False


def _segment_dumps(words: list[str], depth: int) -> bool:
    while words and words[0] in _KEYWORDS:
        words = words[1:]  # `do cat x`, `then tail x`
    if not words or words[0] in _LOOP_HEADS:
        return False  # `for f in a b`: a word list, not a command
    piped_args = os.path.basename(words[0]) == "xargs"
    words = _strip(words)
    if not words:
        return False
    prog = os.path.basename(words[0])
    low = prog.lower()
    if low in _PS_DUMP:
        return True
    if prog in _SHELLS:
        script = _shell_script(words)
        return script is not None and depth < MAX_DEPTH and _any_dump(script, depth + 1)
    if prog in ("find", "fd"):
        for i, w in enumerate(words):
            if w in _EXEC or w in ("-x", "-X", "--exec", "--exec-batch"):
                if _segment_dumps(words[i + 1 :], depth):
                    return True
    if prog in _FILE_ARG:
        return _has_file_arg(words, _FILE_ARG[prog])
    if prog in _DUMP:
        if piped_args:
            return True  # `xargs cat`: the operands come from the pipe
        takes, operands, script_opts = _DUMP_ARGS.get(prog, (frozenset(), 0, frozenset()))
        return _has_file_arg(words, takes, operands, script_opts)
    if _code_reads_file(prog, words):
        return True
    return _classify(words, depth) == "file_dump"


def _any_dump(cmd: str, depth: int) -> bool:
    return any(_segment_dumps(w, depth) for w in _all_segments(cmd))


def command_family(cmd: str) -> str:
    """One of ``FAMILIES`` for a shell command; ``other`` when unknown or unparsable."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "other"
    return _family(cmd, 0)


def is_file_dump(cmd: str) -> bool:
    """For gating only (conservative): True when **any** simple command of ``cmd`` prints a
    file's contents. Every segment counts (split on ``&&``, ``||``, ``;``, ``|``, ``&`` and
    newlines; ``do``/``then`` bodies; ``find -exec``/``fd -x`` commands; ``bash -c`` scripts;
    ``xargs <dump>``): the file-dump family, ``sort``/``uniq``/``cut``/``rev``/``strings``/``tac``
    with a file argument, ``python -c``/``node -e`` code that reads a file (``open(``,
    ``read_text(``, ``readFileSync`` ...) and PowerShell/cmd ``Get-Content``/``gc``/``type``.
    ``command_family`` keeps the first program (E1 comparability)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    return _any_dump(cmd, 0)  # not normalised: that collapses newlines and truncates
