"""PreToolUse guards (block clearly unbounded scans) and learned pre-trims (decision ``pretrim``).

Guards block exactly these, each with a one-sentence reason naming the bounded alternative:
``find /`` or ``find ~`` without ``-maxdepth``; recursive ``grep``/``rg`` over a vendored or build
directory (``node_modules``, ``.git``, ``dist``, ``build``, ``.venv``) without an include filter;
``cat``/``Read`` of a file larger than ``[guards] max_read_bytes`` without a range; ``ls -R`` on
``/`` or ``~``. A command that does not parse never blocks. Every simple command of a pipeline or
``&&``/``||``/``;`` chain is checked separately.

Commands are split on unquoted newlines (backslash-newline joins lines) and heredoc bodies are
skipped, so text fed to another program is never mistaken for a command.

Pre-trims (decision ``pretrim``) never touch the command: a shell rewrite would mask the exit
status and drop unrecoverable output. Instead, a learned candidate command's *output* gets a
tail-biased compression level on PostToolUse (``pretrim_lines`` / ``pretrim_level``, used by
``mechanisms.compression``), with the usual recovery marker and error pinning.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.compress.base import Level, load_level
from token_sparen.config import Config
from token_sparen.events import Action, Event
from token_sparen.learn.policy import PRETRIM
from token_sparen.learn.regret import MAX_SIG_CHARS, normalize_command
from token_sparen.store import jsonstore

__all__ = ["PRETRIM", "check", "pretrim_level", "pretrim_lines"]

MAX_READ_BYTES = 5_000_000
PRETRIM_LINES = 120
PRETRIM_HEAD = 10
PRETRIM_FILE = "pretrim.json"

_SEPARATORS = frozenset({"|", "|&", "||", "&&", ";", "&", ";;", ";&", ";;&"})
_REDIR_OPS = frozenset(
    {">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<-", "<<<", "<>", "<&", "&>|"}
)
_STDOUT_OPS = frozenset({">", ">>", ">|", "&>", "&>>", ">&", "&>|"})
_NON_TEXT = frozenset(
    {".pdf", ".ipynb", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico"}
)
_PREFIXES = frozenset({"sudo", "time", "command", "nohup", "exec"})
_ROOTS = frozenset({"/", "~", "~/"})
_VENDORED = frozenset({"node_modules", ".git", "dist", "build", ".venv"})
_GREP_ARG_SHORT = frozenset("efABCDdm")  # short options that take an argument
_GREP_ARG_LONG = frozenset(
    {"--regexp", "--file", "--after-context", "--before-context", "--context", "--max-count"}
)
_GREP_INCLUDE = ("--include",)
_RG_ARG_SHORT = frozenset("efgtTABCmjMEr")
_RG_INCLUDE_SHORT = frozenset("gt")
_RG_INCLUDE_LONG = ("--glob", "--iglob", "--type", "--type-add")
_FIND_OPTS = frozenset({"-H", "-L", "-P"})

REASON_FIND = (
    "token-sparen guard: an unbounded find over / or ~ walks the whole filesystem; "
    "add -maxdepth 3 or search a project subdirectory."
)
REASON_GREP = (
    "token-sparen guard: a recursive search over {dir} floods the context; "
    "add an include filter (grep --include='*.ext', rg -g '*.ext' or -t type) "
    "or search a narrower path."
)
REASON_CAT = (
    "token-sparen guard: {name} is {mb:.1f} MB; "
    "read a slice instead, e.g. head -n 200, tail -n 200 or sed -n '1,200p'."
)
REASON_READ = (
    "token-sparen guard: this file is {mb:.1f} MB; "
    "read a range by passing offset and limit (e.g. limit 2000)."
)
REASON_LS = (
    "token-sparen guard: ls -R on / or ~ lists the whole filesystem; "
    "use find with -maxdepth 2 or list a project subdirectory."
)


def _enabled(cfg: Config) -> bool:
    return cfg.get("guards.enabled", True) is not False


def _int(cfg: Config, key: str, default: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _tokens(command: str) -> list[str] | None:
    """Shell tokens of one line with operators split out; None if the line does not parse."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _line_end(command: str, pos: int) -> int | None:
    """Index of the next unquoted newline (or the end); None if a quote is left open."""
    quote: str | None = None
    i = pos
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = None if ch == "'" else quote
        elif ch == "\\":
            i += 1  # escaped char (a backslash-newline is a line continuation)
        elif quote == '"':
            quote = None if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            return i
        i += 1
    return None if quote is not None else len(command)


def _heredocs(tokens: list[str]) -> list[tuple[str, bool]]:
    """(delimiter, strip leading tabs) for each heredoc a line opens."""
    out: list[tuple[str, bool]] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok not in ("<<", "<<-"):
            continue
        delim, strip = tokens[i + 1], tok == "<<-"
        if delim.startswith("-"):  # `<<-EOF` lexes as `<<` + `-EOF`
            delim, strip = delim[1:], True
        out.append((delim, strip))
    return out


def _lines(command: str) -> list[list[str]] | None:
    """Tokens per logical line, heredoc bodies dropped; None if any line does not parse."""
    result: list[list[str]] = []
    pos, n = 0, len(command)
    while pos <= n:
        end = _line_end(command, pos)
        if end is None:
            return None
        tokens = _tokens(command[pos:end].replace("\\\n", ""))
        if tokens is None:
            return None
        result.append(tokens)
        pos = end + 1
        for delim, strip in _heredocs(tokens):
            while pos <= n:
                nl = command.find("\n", pos)
                stop = n if nl == -1 else nl
                body = command[pos:stop]
                pos = stop + 1
                if (body.lstrip("\t") if strip else body) == delim:
                    break
    return result


def _split_redirects(words: list[str]) -> tuple[list[str], bool]:
    """(words without redirection operators and targets, stdout is redirected)."""
    out: list[str] = []
    stdout = False
    i = 0
    while i < len(words):
        word = words[i]
        if word in _REDIR_OPS:
            fd = out[-1] if out and out[-1].isdigit() else None
            if word in _STDOUT_OPS and (word.startswith("&") or fd in (None, "1")):
                stdout = True
            i += 2
            continue
        out.append(word)
        i += 1
    return out, stdout


def _segments(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Simple commands as (words, stdout reaches the tool result)."""
    out: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    for tok in [*tokens, ";"]:
        if tok in _SEPARATORS:
            if cur:
                words, redirected = _split_redirects(cur)
                out.append((words, tok not in {"|", "|&"} and not redirected))
            cur = []
        else:
            cur.append(tok)
    return out


def _strip_prefix(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        word = words[i]
        name, eq, _ = word.partition("=")
        if eq and name.isidentifier():
            i += 1
        elif word in _PREFIXES:
            i += 1
        else:
            break
    return words[i:]


def _base_dir(event: Event) -> Path:
    """The Bash working directory from the payload ``cwd`` if absolute, else the project dir."""
    cwd = event.extra.get("cwd")
    if isinstance(cwd, str) and cwd and Path(cwd).is_absolute():
        return Path(cwd)
    return event.project_dir


def _resolve(event: Event, arg: str) -> Path:
    path = Path(arg)
    return path if path.is_absolute() else _base_dir(event) / path


def _check_find(words: list[str]) -> str | None:
    if "-maxdepth" in words:
        return None
    i = 1
    while i < len(words) and (words[i] in _FIND_OPTS or words[i].startswith(("-O", "-D"))):
        i += 2 if words[i] == "-D" else 1
    while i < len(words) and not words[i].startswith(("-", "(", "!")):
        if words[i] in _ROOTS:
            return REASON_FIND
        i += 1
    return None


def _vendored_dir(event: Event, arg: str) -> str | None:
    parts = [p for p in arg.split("/") if p not in ("", ".")]
    hit = next((p for p in parts if p in _VENDORED), None)
    if hit is None:
        return None
    try:
        return hit if _resolve(event, arg).is_dir() else None
    except OSError:
        return None


def _check_grep(event: Event, words: list[str], rg: bool) -> str | None:
    recursive = rg
    has_pattern_opt = False
    include = False
    positional: list[str] = []
    i = 1
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            positional.extend(words[i:])
            break
        if word.startswith("--"):
            name, eq, _ = word.partition("=")
            if name in ("--recursive", "--dereference-recursive"):
                recursive = True
            if name in _GREP_INCLUDE or (rg and name in _RG_INCLUDE_LONG):
                include = True
            if name in ("--regexp", "--file"):
                has_pattern_opt = True
            takes_arg = name in _GREP_ARG_LONG or name in _GREP_INCLUDE
            takes_arg = takes_arg or (rg and name in _RG_INCLUDE_LONG)
            if takes_arg and not eq:
                i += 1
            continue
        if word.startswith("-") and len(word) > 1:
            arg_short = _RG_ARG_SHORT if rg else _GREP_ARG_SHORT
            for pos, ch in enumerate(word[1:]):
                if ch in "rR":
                    recursive = True if not rg else recursive
                if ch in "ef":
                    has_pattern_opt = True
                if rg and ch in _RG_INCLUDE_SHORT:
                    include = True
                if ch in arg_short:
                    if pos == len(word) - 2:  # argument is the next word
                        i += 1
                    break
            continue
        positional.append(word)
    if not recursive or include:
        return None
    paths = positional if has_pattern_opt else positional[1:]
    for arg in paths:
        hit = _vendored_dir(event, arg)
        if hit is not None:
            return REASON_GREP.format(dir=hit)
    return None


def _check_cat(event: Event, words: list[str], limit: int) -> str | None:
    for arg in words[1:]:
        if arg.startswith("-") and arg != "-":
            continue
        try:
            path = _resolve(event, arg)
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            continue
        if size > limit:
            return REASON_CAT.format(name=path.name, mb=size / 1_000_000)
    return None


def _check_ls(words: list[str]) -> str | None:
    recursive = any(
        w == "--recursive" or (w.startswith("-") and not w.startswith("--") and "R" in w)
        for w in words[1:]
    )
    if recursive and any(w in _ROOTS for w in words[1:]):
        return REASON_LS
    return None


def _check_segment(event: Event, words: list[str], to_context: bool, limit: int) -> str | None:
    words = _strip_prefix(words)
    if not words:
        return None
    name = os.path.basename(words[0])
    if name == "find":
        return _check_find(words)
    if name in ("grep", "egrep", "fgrep", "rg"):
        return _check_grep(event, words, rg=name == "rg")
    if name == "ls":
        return _check_ls(words)
    if name == "cat" and to_context:
        return _check_cat(event, words, limit)
    return None


def _check_read(tool_input: Mapping[str, Any], limit: int) -> str | None:
    if any(tool_input.get(k) is not None for k in ("offset", "limit", "pages")):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if Path(file_path).suffix.lower() in _NON_TEXT:  # read natively; Read pages them itself
        return None
    try:
        path = Path(file_path)
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        return None
    return REASON_READ.format(mb=size / 1_000_000) if size > limit else None


def check(event: Event, cfg: Config) -> Action:
    """Block a clearly unbounded scan with a reason naming the bounded form; else no-op."""
    tool = event.tool
    if event.kind != "pre_tool" or tool is None or not _enabled(cfg):
        return Action()
    limit = _int(cfg, "guards.max_read_bytes", MAX_READ_BYTES)
    if tool.name == "Read":
        reason = _check_read(tool.input, limit)
        return Action() if reason is None else Action(block_reason=reason)
    if tool.name != "Bash":
        return Action()
    command = tool.input.get("command")
    if not isinstance(command, str):
        return Action()
    lines = _lines(command)
    if lines is None:
        return Action()
    for tokens in lines:
        for words, to_context in _segments(tokens):
            reason = _check_segment(event, words, to_context, limit)
            if reason is not None:
                return Action(block_reason=reason)
    return Action()


# --- learned pre-trims (applied on PostToolUse by mechanisms.compression) -------------


def _normalise(cmd: str, event: Event | None = None) -> str:
    """Candidate lookup key: the normalised command signature (``learn.regret``), with ``cd``
    targets resolved against the event's project dir and cwd, truncated to the decision log's
    500 characters (candidates are learned from the log's ``command_sig``)."""
    if event is None:
        return normalize_command(cmd)[:MAX_SIG_CHARS]
    cwd = event.extra.get("cwd")
    sig = normalize_command(
        cmd, project_dir=event.project_dir, cwd=cwd if isinstance(cwd, str) and cwd else None
    )
    return sig[:MAX_SIG_CHARS]


def _candidate(store: Path, key: str, env: Mapping[str, str]) -> dict[str, Any] | None:
    try:
        data = jsonstore.read(store / PRETRIM_FILE, {}, env)
    except (jsonstore.StoreBusy, OSError, ValueError):
        return None
    cands = data.get("candidates") if isinstance(data, dict) else None
    entry = cands.get(key) if isinstance(cands, dict) else None
    return entry if isinstance(entry, dict) else None


def pretrim_lines(
    event: Event, store: Path, cfg: Config, *, env: Mapping[str, str] | None = None
) -> int | None:
    """K (tail lines) if this Bash command is a learned pre-trim candidate, else None.

    ``store`` is the project state directory (``projects/<key>/``) holding ``pretrim.json``.
    A candidate's own positive ``lines`` wins over ``[guards] pretrim_lines``.
    """
    env = os.environ if env is None else env
    tool = event.tool
    if tool is None or tool.name != "Bash":
        return None
    command = tool.input.get("command")
    if not isinstance(command, str):
        return None
    cand = _candidate(store, _normalise(command, event), env)
    if cand is None:
        return None
    lines = cand.get("lines")
    if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
        return _int(cfg, "guards.pretrim_lines", PRETRIM_LINES)
    return lines


def pretrim_level(cfg: Config, lines: int) -> Level:
    """Tail-biased level: short head, ``lines`` tail, the standard arm's keep ratio."""
    head = _int(cfg, "guards.pretrim_head", PRETRIM_HEAD)
    return Level(head=head, tail=lines, keep_ratio=load_level(cfg, "standard").keep_ratio)
