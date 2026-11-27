"""Does a tool call return a file's contents? Gating only (``compression.gated``), conservative
and best-effort: a false positive leaves an output untouched, a false negative may trim a file.

``is_file_dump(cmd)`` is true when **any** simple command of a shell command prints a file:

- every segment (split on ``&&``, ``||``, ``;``, ``|``, ``&`` and newlines), ``do``/``then``
  bodies, ``find -exec``/``fd -x`` commands, ``xargs <dump>``, ``bash|sh|zsh -c`` scripts,
  ``eval``, command substitutions ``$(...)`` and backticks (also inside double quotes);
- stdin redirected from a file (``< f``, ``0< f``, ``<f``, ``done < f``, ``$(< f)``); here-docs
  and here-strings are not files;
- the file-dump programs with a file operand (cat, head, tail, sed, awk, jq, yq, less, more, bat,
  nl, xxd, od, hexdump, tac, zcat, sort, uniq, cut, rev, strings, fold, fmt, paste, column, pr,
  expand, unexpand, base64, xzcat, bzcat, zstdcat, view, most, batcat, highlight, pygmentize,
  glow, mdcat, ``g``-prefixed coreutils), decompressors to stdout (``gunzip -c``, ``xz -dc``
  ...), ``tar -xO``, ``unzip -p``, ``dd if=``, ``cp f /dev/stdout|-``, ``gsutil cat``,
  ``gcloud storage cat``, ``aws s3 cp <src> -``, ``git show <rev>:<path>``, ``git cat-file -p``,
  PowerShell ``Get-Content``/``gc``/``type`` and ``[IO.File]::ReadAll*``;
- interpreters whose inline code reads a file (python ``open(``/``read_text(``/``fileinput``,
  node ``readFileSync``/``createReadStream``, ruby ``File.read``/``IO.read``, perl ``open(``) or
  that loop over a file operand (``perl -ne '...' f``, ``ruby -ne``);
- through wrappers: ``ssh host CMD``, ``su -c CMD``, ``docker|podman|nerdctl exec|run``,
  ``docker compose exec|run``, ``kubectl exec ... -- CMD``, ``adb shell``, ``script -c``,
  ``watch``, ``unbuffer``, ``ionice``, ``strace``, ``chroot``, ``busybox``, ``coreutils``,
  ``powershell|pwsh -Command`` (plus the shared wrappers: sudo, env, timeout, nice, ...).

Fix round 3 adds: ``kubectl|oc exec POD CMD`` without ``--``, ``oc rsh``, ``docker-compose``,
``fd -x|-X CMD`` (implicit path operand), nsenter, flock (``-c``), setsid, taskset, sshpass,
``sg -c``, ``vagrant ssh -c``, ``gcloud compute ssh --command``, ``multipass exec``, lima,
``parallel CMD :::``, chrt, numactl, firejail, unshare, ``ssh host <<EOF`` and ``<<EOF |
python`` here-documents; ``docker|kubectl cp SRC -|/dev/stdout``, ``hg cat``, ``openssl -in`` to
stdout, iconv, brotli/lz4/lzma, zless-likes, ``dos2unix f /dev/stdout``; php ``-r``
``file_get_contents``/``fopen``, lua ``io.open``, awk ``getline < f``; here-document bodies of
interpreters (a shell body is classified, other bodies searched for reads; a ``cat <<EOF`` body is
data); match-everything searches (grep/rg/ag/ack/zgrep with ``''``, ``.``, ``^``, ``$`` or
``rg --passthru``, and a file/dir operand; not with -l/-c/-q/-o).

``is_file_tool(name)`` is the tool-name rule (MCP tools included). ``families.command_family``
(E1) keeps classifying by the first program only.
"""

from __future__ import annotations

import os
import re

from token_sparen.compress import families as fam

MAX_DEPTH = 4
# --------------------------------------------------------------------------- tool names

# The single source of the tool-name rule.
_VERBS = ("read", "get", "fetch", "download", "view", "open", "cat", "load", "show")
_NOUNS = (
    "file",
    "files",
    "document",
    "documents",
    "media",
    "resource",
    "resources",
    "content",
    "blob",
    "raw",
)
_BARE = frozenset({"read", "cat", "view", "open"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _CAMEL.sub("_", name).lower()) if t]


def is_file_tool(name: str) -> bool:
    """A tool whose name says it returns a file's contents (case-insensitive, word-ish): a
    ``read`` token; a verb (get, fetch, download, view, open, cat) with a noun (file(s),
    document, media, resource, content), also run together (``readfile``); or a bare ``cat``,
    ``view``, ``read`` or ``open`` (the last ``__`` part of an MCP name)."""
    if name.rsplit("__", 1)[-1].lower() in _BARE:
        return True
    tokens = _name_tokens(name)
    if "read" in tokens:
        return True
    verbs = [t for t in tokens if t in _VERBS]
    if verbs and any(t in _NOUNS or t.rstrip("s") in _NOUNS for t in tokens):
        return True
    return any(t.startswith(v) and t[len(v) :] in _NOUNS for t in tokens for v in _VERBS)


# --------------------------------------------------------------------------- commands

_N = frozenset({"-n", "-c", "--lines", "--bytes"})
# program -> (options taking a value, leading script operands, options replacing the script)
_ARGS: dict[str, tuple[frozenset[str], int, frozenset[str]]] = {
    "head": (_N, 0, frozenset()),
    "tail": (_N | {"-s", "--pid"}, 0, frozenset()),
    "sed": (frozenset({"-e", "-f", "-l"}), 1, frozenset({"-e", "-f"})),
    "awk": (frozenset({"-F", "-v", "-f"}), 1, frozenset({"-f"})),
    "jq": (frozenset({"-f", "--indent", "--arg", "--argjson"}), 1, frozenset({"-f"})),
    "yq": (frozenset({"-I", "--indent", "-o", "--output-format"}), 1, frozenset()),
    "bat": (frozenset({"-l", "-r", "-H", "--language", "--line-range"}), 0, frozenset()),
    "batcat": (frozenset({"-l", "-r", "-H", "--language", "--line-range"}), 0, frozenset()),
    "xxd": (frozenset({"-l", "-s", "-c", "-g"}), 0, frozenset()),
    "od": (frozenset({"-N", "-j", "-t", "-A", "-w"}), 0, frozenset()),
    "hexdump": (frozenset({"-n", "-s", "-e"}), 0, frozenset()),
    "nl": (frozenset({"-w", "-s", "-b", "-v", "-i"}), 0, frozenset()),
    "sort": (
        frozenset({"-k", "-t", "-o", "-S", "-T", "--key", "--field-separator", "--output"}),
        0,
        frozenset(),
    ),
    "uniq": (frozenset({"-f", "-s", "-w"}), 0, frozenset()),
    "cut": (frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields"}), 0, frozenset()),
    "strings": (frozenset({"-n", "-t", "-e"}), 0, frozenset()),
    "tac": (frozenset({"-s"}), 0, frozenset()),
    "fold": (frozenset({"-w", "--width"}), 0, frozenset()),
    "fmt": (frozenset({"-w", "-g", "-p"}), 0, frozenset()),
    "paste": (frozenset({"-d", "--delimiters"}), 0, frozenset()),
    "column": (frozenset({"-s", "-c", "-o", "-N", "-R", "-H", "-W"}), 0, frozenset()),
    "pr": (frozenset({"-w", "-l", "-h", "-o", "-s", "-e", "-i", "-n"}), 0, frozenset()),
    "expand": (frozenset({"-t", "--tabs"}), 0, frozenset()),
    "unexpand": (frozenset({"-t", "--tabs"}), 0, frozenset()),
    "base64": (frozenset({"-w", "-b", "-i", "-o", "--wrap"}), 0, frozenset()),
    "highlight": (frozenset({"-O", "-S", "-s", "--out-format", "--syntax"}), 0, frozenset()),
    "pygmentize": (frozenset({"-l", "-f", "-O", "-P", "-o", "-x"}), 0, frozenset()),
    "glow": (frozenset({"-s", "-w", "--style", "--width"}), 0, frozenset()),
    "iconv": (frozenset({"-f", "-t", "-o", "--from-code", "--to-code"}), 0, frozenset()),
}
_PLAIN = frozenset(
    {"cat", "less", "more", "rev", "zcat", "xzcat", "bzcat", "zstdcat", "view", "most", "mdcat"}
    | {"zless", "zmore", "bzless", "bzmore", "xzless", "xzmore", "zstdless", "lzcat", "gzcat"}
)
_DUMPS = frozenset(_ARGS) | _PLAIN
_GPREFIXED = frozenset(f"g{p}" for p in _DUMPS if p not in ("awk",))
_DECOMPRESS = frozenset(
    {"gzip", "gunzip", "xz", "unxz", "bzip2", "bunzip2", "zstd", "unzstd", "brotli", "lz4"}
    | {"lzma", "unlzma", "lzip"}
)
_GREPS = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep", "zegrep", "bzgrep", "xzgrep"}
)
_GREP_ARGS = frozenset(
    {"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "-g", "-t", "-T", "-M", "-E", "-j"}
    | {"--regexp", "--file", "--max-count", "--glob", "--type", "--type-not", "--context"}
)
# grep and its z/bz/xz variants: -E, -T, -M, -j, -g, -t are flags or absent, not value options
_GNU_GREP_ARGS = frozenset(
    {"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D"}
    | {"--regexp", "--file", "--max-count", "--context"}
)
_GREP_NOT_CONTENT = frozenset(
    {"-l", "-L", "-c", "-q", "-o", "--count", "--files-with-matches", "--files-without-match"}
    | {"--quiet", "--only-matching", "--files", "--count-matches"}
)
_MATCH_ALL = frozenset({"", ".", "^", "$", ".*", "^.*$", "^.*"})
_HEREDOC_LINE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_INTERPS = r"(python[\d.]*|node|deno|ruby|perl|php|lua|bash|sh|zsh|dash|ksh)\b"
_HEREDOC_INTERP = re.compile(rf"\b{_INTERPS}")
_HEREDOC_SSH = re.compile(r"(?:^|[\s;&|(])ssh\s")  # `ssh host <<EOF`: the body runs as a shell
_PIPED_INTERP = re.compile(rf"\s*(?:\S*/)?{_INTERPS}")  # `<<EOF | python`
_PS_DUMP = frozenset({"get-content", "gc", "type"})
_PS_READ = re.compile(r"\[(?:system\.)?io\.file\]::read", re.IGNORECASE)
_CODE_READS = re.compile(
    r"\bopen\s*\(|read_text\s*\(|read_bytes\s*\(|readFileSync|readFile\s*\(|createReadStream"
    r"|\bfileinput\b|File\.(?:read|readlines|open|foreach|binread)\b|IO\.(?:read|readlines)\b"
    r"|file_get_contents|\bfopen\s*\(|\bio\.(?:open|lines)\b|getline(?:\s+[A-Za-z_]\w*)?\s*<"
)
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "{", "}", "!", "done"})
_LOOP_HEADS = frozenset({"for", "select", "case"})
_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-x", "-X", "--exec", "--exec-batch"})
_HEREDOC = frozenset({"<<", "<<<", "<<-"})
_STDOUT = frozenset({"/dev/stdout", "/dev/stderr", "-", "/dev/fd/1", "/dev/fd/2"})
_SSH_ARGS = frozenset(
    {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-E", "-b", "-c", "-e", "-m", "-O"}
    | {"-Q", "-S", "-W", "-w", "-B"}
)
_DOCKER_ARGS = frozenset(
    {
        "-e",
        "--env",
        "--env-file",
        "-u",
        "--user",
        "-w",
        "--workdir",
        "--detach-keys",
        "-v",
        "--volume",
        "-p",
        "--publish",
        "--name",
        "--network",
        "--net",
        "--entrypoint",
        "--mount",
        "-l",
        "--label",
        "--platform",
        "-h",
        "--hostname",
        "--add-host",
        "--cpus",
        "-m",
        "--memory",
        "--pull",
        "--device",
        "--gpus",
        "--ulimit",
        "--tmpfs",
        "--log-driver",
        "--restart",
        "--cap-add",
        "--cap-drop",
        "--security-opt",
        "-f",
        "--file",
        "--project-name",
        "--index",
    }
)
_CONTAINERS = frozenset({"docker", "podman", "nerdctl"})
_KUBE_ARGS = frozenset({"-c", "--container", "-n", "--namespace", "-f", "--filename", "--context"})


def _skip_opts(words: list[str], takes: frozenset[str]) -> list[str]:
    """Drop leading options (and the values of ``takes``); a ``--`` ends them."""
    while words and words[0].startswith("-") and len(words[0]) > 1:
        opt = words[0]
        words = words[1:]
        if opt == "--":
            break
        if opt in takes and words:
            words = words[1:]
    return words


def _substitutions(cmd: str) -> list[str]:
    """Inner commands of ``$(...)`` (not ``$((...))``) and backticks, outside single quotes."""
    out: list[str] = []
    i, n, quote = 0, len(cmd), ""
    while i < n:
        c = cmd[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "`":
            end = cmd.find("`", i + 1)
            if end < 0:
                break
            out.append(cmd[i + 1 : end])
            i = end
        elif cmd.startswith("$(", i) and not cmd.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            out.append(cmd[i + 2 : j - 1])
            i = j - 1
        i += 1
    return out


def _stdin_from_file(words: list[str]) -> bool:
    """``< file`` (any fd-less or ``0<`` form): shlex gives ``<`` as its own token."""
    return any(w == "<" and i + 1 < len(words) for i, w in enumerate(words))


def _file_operand(
    words: list[str],
    takes: frozenset[str],
    script: int = 0,
    script_opts: frozenset[str] = frozenset(),
) -> bool:
    """A file operand after options (``script`` leading script operands skipped unless one of
    ``script_opts`` was given); redirections and here-docs are not operands."""
    rest, operands, skip, only = words[1:], 0, script, False
    while rest:
        w, rest = rest[0], rest[1:]
        if w in _HEREDOC or w in (">", ">>", "<", "&>", ">&") or w.endswith(">"):
            rest = rest[1:]
            continue
        if w == "--" and not only:
            only = True
            continue
        if not only and w.startswith("-") and len(w) > 1:
            if w in script_opts:
                skip = 0
            if w in takes and rest:
                rest = rest[1:]
            continue
        operands += 1
        if operands > skip:
            return True
    return False


def _script_code(words: list[str], flag: str) -> tuple[str, list[str]] | None:
    """(inline code, the words after it) for ``-<flag>`` or a short-option group ending in it
    (``perl -ne``), else None."""
    for i, w in enumerate(words[1:-1], start=1):
        if w.startswith("-") and not w.startswith("--") and w.endswith(flag):
            return words[i + 1], words[i + 2 :]
    return None


def _interpreter(prog: str, words: list[str]) -> bool:
    if fam._PY.fullmatch(prog):
        found = _script_code(words, "c")
    elif prog in ("node", "deno", "bun"):
        found = _script_code(words, "e") or _script_code(words, "p")
    elif prog == "php":
        found = _script_code(words, "r")
    elif prog in ("lua", "luajit"):
        found = _script_code(words, "e")
    elif prog in ("perl", "ruby"):
        found = _script_code(words, "e")
        if found is not None and _file_operand(["x", *found[1]], frozenset()):
            return True  # `perl -ne '...' file`
    else:
        return False
    return found is not None and bool(_CODE_READS.search(found[0]))


def _special(prog: str, words: list[str]) -> bool:
    """Programs that print a file only in some modes."""
    args = words[1:]
    if prog in _DECOMPRESS:
        to_stdout = any(
            a == "--stdout" or (a.startswith("-") and not a.startswith("--") and "c" in a)
            for a in args
        )
        return to_stdout and _file_operand(words, frozenset({"-S", "--suffix"}))
    if prog == "tar":
        flags = args[0] if args and not args[0].startswith("-") else ""
        dashed = [a for a in args if a.startswith("-") and not a.startswith("--")]
        return "--to-stdout" in args or "O" in flags or any("O" in a for a in dashed)
    if prog == "unzip":
        return "-p" in args
    if prog == "dd":
        src = any(a.startswith("if=") for a in args)
        outs = [a[3:] for a in args if a.startswith("of=")]
        return src and all(o in _STDOUT for o in outs)
    if prog == "cp":
        operands = [a for a in args if not a.startswith("-") or a == "-"]
        return len(operands) >= 2 and operands[-1] in _STDOUT
    if prog in _CONTAINERS or prog == "kubectl" or prog == "oc":  # docker cp C:f -
        operands = [a for a in args if not a.startswith("-") or a == "-"]
        return operands[:1] == ["cp"] and len(operands) >= 3 and operands[-1] in _STDOUT
    if prog in ("dos2unix", "unix2dos"):
        operands = [a for a in args if not a.startswith("-") or a == "-"]
        return len(operands) >= 2 and operands[-1] in _STDOUT
    if prog == "hg":
        return fam._sub(words) == "cat"
    if prog == "openssl":
        outs = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-out"]
        return "-in" in args and all(o in _STDOUT for o in outs)
    if prog == "gsutil":
        return fam._sub(words) == "cat"
    if prog == "gcloud":
        return args[:2] == ["storage", "cat"]
    if prog == "aws":
        operands = [a for a in args if not a.startswith("-") or a == "-"]
        return operands[:2] == ["s3", "cp"] and len(operands) >= 4 and operands[-1] in _STDOUT
    return False


def _unwrap(words: list[str], depth: int) -> tuple[list[str], bool] | str | None:
    """Peel wrappers: ``(words, piped_operands)`` of the wrapped program, a script string to
    classify as a whole command, or None when nothing is left."""
    piped = False
    for _ in range(16):
        if not words:
            return None
        piped = piped or os.path.basename(words[0]) == "xargs"
        words = fam._strip(words)
        if not words:
            return None
        prog = os.path.basename(words[0])
        low = prog.lower()
        if prog in _GPREFIXED:
            words = [prog[1:], *words[1:]]
            prog = prog[1:]
        if prog in ("busybox", "coreutils", "unbuffer"):
            words = words[1:]
        elif prog == "ionice":
            words = _skip_opts(words[1:], frozenset({"-c", "-n", "-p", "-P", "-u"}))
        elif prog in ("strace", "ltrace"):
            takes = frozenset({"-e", "-o", "-p", "-s", "-u", "-E", "-a", "-I", "-P", "-X", "-b"})
            words = _skip_opts(words[1:], takes)
        elif prog == "chroot":
            words = _skip_opts(words[1:], frozenset())[1:]
        elif prog == "eval":
            return " ".join(words[1:])
        elif prog in fam._SHELLS:
            return fam._shell_script(words)
        elif prog in ("su", "script"):
            code = _script_code(words, "c")
            return code[0] if code is not None else None
        elif low in ("powershell", "pwsh", "powershell.exe", "pwsh.exe"):
            for i, w in enumerate(words[1:-1], start=1):
                if w.lower() in ("-command", "-c"):
                    return " ".join(words[i + 1 :])
            return None
        elif prog == "watch":
            takes = frozenset({"-n", "--interval", "-q", "--equexit"})
            return " ".join(_skip_opts(words[1:], takes))
        elif prog == "ssh":
            rest = _skip_opts(words[1:], _SSH_ARGS)
            return " ".join(rest[1:]) if len(rest) > 1 else None
        elif prog == "adb":
            rest = _skip_opts(words[1:], frozenset({"-s", "-H", "-P"}))
            return " ".join(rest[1:]) if rest[:1] == ["shell"] else None
        elif prog in _CONTAINERS or prog == "docker-compose":
            rest = _skip_opts(words[1:], frozenset({"-H", "--host", "--context", "-c"}))
            if prog == "docker-compose":
                rest = _skip_opts(words[1:], _DOCKER_ARGS)
            elif rest[:1] == ["compose"]:
                rest = _skip_opts(rest[1:], _DOCKER_ARGS)
            if rest[:1] not in (["exec"], ["run"]):
                return words, piped
            rest = _skip_opts(rest[1:], _DOCKER_ARGS)
            words = rest[1:]  # drop the container / image / service
        elif prog in ("kubectl", "oc") and ("exec" in words or "rsh" in words):
            if "--" in words:
                words = words[words.index("--") + 1 :]
            else:  # deprecated form: kubectl exec POD CMD
                sub = "exec" if "exec" in words else "rsh"
                words = _skip_opts(words[words.index(sub) + 1 :], _KUBE_ARGS)[1:]
        elif prog == "multipass" and fam._sub(words) == "exec":
            rest = words[words.index("exec") + 1 :]
            words = rest[rest.index("--") + 1 :] if "--" in rest else rest[1:]
        elif prog in ("lima", "setsid"):
            words = _skip_opts(words[1:], frozenset())
        elif prog == "nsenter":
            takes = frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid", "-w", "-r"})
            words = _skip_opts(words[1:], takes)
        elif prog == "flock":
            rest = _skip_opts(words[1:], frozenset({"-w", "--timeout", "-E"}))[1:]
            if rest[:1] in (["-c"], ["--command"]):
                return rest[1] if len(rest) > 1 else None
            words = rest
        elif prog == "chrt":
            words = _skip_opts(words[1:], frozenset({"-T", "-P", "-D"}))[1:]  # drop the priority
        elif prog == "numactl":
            takes = frozenset({"-N", "-m", "-C", "-p", "-i", "-P", "-w"})
            words = _skip_opts(words[1:], takes)
        elif prog in ("firejail", "unshare"):
            takes = frozenset({"-S", "-G", "-R", "-w", "--setuid", "--setgid", "--root", "--wd"})
            words = _skip_opts(words[1:], takes if prog == "unshare" else frozenset())
        elif prog == "taskset":
            words = _skip_opts(words[1:], frozenset())[1:]  # drop the mask / cpu list
        elif prog == "sshpass":
            words = _skip_opts(words[1:], frozenset({"-p", "-f", "-d", "-P"}))
        elif prog == "sg":
            code = _script_code(words, "c")
            return code[0] if code is not None else " ".join(words[2:])
        elif prog in ("vagrant", "gcloud"):
            for i, w in enumerate(words):
                if w.startswith("--command="):
                    return w.split("=", 1)[1]
                if w in ("--command", "-c") and i + 1 < len(words):
                    return words[i + 1]
            return words, piped
        elif prog == "parallel":
            takes = frozenset({"-j", "--jobs", "-S", "--sshlogin", "-I", "--colsep", "-d", "-n"})
            rest = _skip_opts(words[1:], takes)
            cut = next((i for i, w in enumerate(rest) if w in (":::", "::::")), len(rest))
            words, piped = rest[:cut], True
        else:
            return words, piped
    return None


def _segment(words: list[str], depth: int) -> bool:
    if _stdin_from_file(words):
        return True
    while words and words[0] in _KEYWORDS:
        words = words[1:]
    if not words or words[0] in _LOOP_HEADS:
        return False
    peeled = _unwrap(words, depth)
    if peeled is None:
        return False
    if isinstance(peeled, str):
        return depth < MAX_DEPTH and _any(peeled, depth + 1)
    words, piped = peeled
    prog = os.path.basename(words[0])
    if prog.lower() in _PS_DUMP:
        return True
    if prog in ("find", "fd"):
        for i, w in enumerate(words):
            if w not in _EXEC:
                continue
            run = words[i + 1 :]
            if prog == "fd":  # fd appends the path when the command has no `{}`
                run = ["xargs", *run]
            if _segment(run, depth):
                return True
        return False
    if prog in _GREPS:
        return _match_all(prog, words)
    if prog in ("awk", "gawk", "mawk") and _awk_reads(words):
        return True
    if prog in _DUMPS:
        if piped:
            return True  # `xargs cat`: the operands come from the pipe
        takes, script, script_opts = _ARGS.get(prog, (frozenset(), 0, frozenset()))
        return _file_operand(words, takes, script, script_opts)
    if _special(prog, words) or _interpreter(prog, words):
        return True
    return fam._classify(words, depth) == "file_dump"  # git show <rev>:<path>, git cat-file -p


def _match_all(prog: str, words: list[str]) -> bool:
    """A search whose pattern matches every line (``''``, ``.``, ``^``, ``$``) or ``rg
    --passthru``, with a file or directory operand: it prints whole files."""
    args = words[1:]
    takes = _GREP_ARGS if prog in ("rg", "ag", "ack") else _GNU_GREP_ARGS
    if any(a in _GREP_NOT_CONTENT for a in args):
        return False
    if any(
        a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("lLcqo") for a in args
    ):
        return False
    patterns: list[str] = []
    operands: list[str] = []
    rest, only = args, False
    while rest:
        a, rest = rest[0], rest[1:]
        if a == "--" and not only:
            only = True
        elif not only and a.startswith("--regexp="):
            patterns.append(a.split("=", 1)[1])
        elif not only and a in ("-e", "--regexp") and rest:
            patterns.append(rest[0])
            rest = rest[1:]
        elif not only and a.startswith("-") and len(a) > 1:
            if a in takes and rest:
                rest = rest[1:]
        else:
            operands.append(a)
    passthru = prog == "rg" and ("--passthru" in args or "--passthrough" in args)
    if not patterns and operands:
        patterns, operands = [operands[0]], operands[1:]
    if not operands:
        return False
    return passthru or any(p in _MATCH_ALL for p in patterns)


def _awk_reads(words: list[str]) -> bool:
    """awk program text that reads a file itself (``getline < "f"``)."""
    rest = _skip_opts(words[1:], frozenset({"-F", "-v"}))
    return bool(rest) and bool(_CODE_READS.search(rest[0]))


def _heredocs(cmd: str) -> tuple[str, bool]:
    """(the command without here-document bodies, whether an interpreter's body reads a file).
    A shell's body is classified as commands; another interpreter's is searched for file
    reads; any other body (``cat <<EOF``) is data and dropped."""
    lines = cmd.split("\n")
    kept: list[str] = []
    reads = False
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        m = _HEREDOC_LINE.search(line)
        i += 1
        if m is None or "<<<" in line[max(0, m.start() - 1) : m.start() + 3]:
            continue
        delim = m.group(2)
        body: list[str] = []
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        i += 1  # the delimiter line
        text = "\n".join(body)
        head, tail = line[: m.start()], line[m.end() :]
        piped = _PIPED_INTERP.match(tail.split("|", 1)[1]) if "|" in tail else None
        interp = _HEREDOC_INTERP.search(head) or piped
        ssh = _HEREDOC_SSH.search(head) is not None
        if interp is None and not ssh:
            continue
        name = "sh" if ssh or interp is None else interp.group(1)
        if name in fam._SHELLS:
            reads = reads or _any(text, MAX_DEPTH - 1)
        else:
            reads = reads or bool(_CODE_READS.search(text))
    return "\n".join(kept), reads


def _segments(cmd: str) -> list[list[str]]:
    return [words for words, _sep in fam._segments(cmd.replace("\n", " ; "))]


def _any(cmd: str, depth: int) -> bool:
    if _PS_READ.search(cmd):
        return True
    if "<<" in cmd:
        cmd, reads = _heredocs(cmd)
        if reads:
            return True
    if depth < MAX_DEPTH and any(_any(inner, depth + 1) for inner in _substitutions(cmd)):
        return True
    return any(_segment(words, depth) for words in _segments(cmd))


def is_file_dump(cmd: str) -> bool:
    """True when any part of the shell command prints a file's contents (module docstring).
    The raw command is used: normalising collapses newlines and truncates."""
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    return _any(cmd, 0)
