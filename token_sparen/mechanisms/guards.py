"""PreToolUse guards (block clearly unbounded scans) and learned pre-trims (decision ``pretrim``).

Guards block exactly these, each with a one-sentence reason naming the bounded alternative:
``find /`` or ``find ~`` without ``-maxdepth``; recursive ``grep``/``rg`` over a vendored or build
directory (``node_modules``, ``.git``, ``dist``, ``build``, ``.venv``) without an include filter;
``cat``/``Read`` of a file larger than ``[guards] max_read_bytes`` without a range; ``ls -R`` on
``/`` or ``~``. A command that does not parse never blocks. Every simple command of a pipeline or
``&&``/``||``/``;`` chain is checked separately.

Pre-trims rewrite a Bash ``command`` to ``<cmd> 2>&1 | tail -n K`` only when the project store
lists the (normalised) command as a candidate and the policy picks ``bounded``. Compound,
redirected, substituted or interactive commands are never rewritten.
"""

from __future__ import annotations

import os
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config
from token_sparen.events import Action, Event
from token_sparen.learn import decisions
from token_sparen.learn.policy import PRETRIM, DecisionRecord, Policy
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, session

__all__ = ["PRETRIM", "check", "pretrim"]

MAX_READ_BYTES = 5_000_000
PRETRIM_LINES = 120
COMMAND_SIG_CHARS = 500
PRETRIM_FILE = "pretrim.json"

_SEPARATORS = frozenset({"|", "|&", "||", "&&", ";", "&", ";;", "\n"})
_REDIRECTS = frozenset({">", ">>", "&>", ">&", "&>>", ">|"})
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
    """Shell tokens with operators split out; None if the command does not parse."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Simple commands as (words, output reaches the tool result)."""
    out: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    for tok in [*tokens, ";"]:
        if tok in _SEPARATORS:
            if cur:
                out.append((cur, tok not in {"|", "|&"}))
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


def _resolve(event: Event, arg: str) -> Path:
    path = Path(arg)
    return path if path.is_absolute() else event.project_dir / path


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
    if name == "cat" and to_context and not any(w in _REDIRECTS for w in words):
        return _check_cat(event, words, limit)
    return None


def _check_read(tool_input: Mapping[str, Any], limit: int) -> str | None:
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
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
    tokens = _tokens(command)
    if tokens is None:
        return Action()
    for words, to_context in _segments(tokens):
        reason = _check_segment(event, words, to_context, limit)
        if reason is not None:
            return Action(block_reason=reason)
    return Action()


# --- learned pre-trims ------------------------------------------------------------------

_NEVER_REWRITE = ("|", ">", "<", "&", ";", "`", "$(", "\n", "\r")
_INTERACTIVE = frozenset(
    {"vim", "vi", "nvim", "nano", "emacs", "less", "more", "most", "top", "htop", "watch", "ssh"}
)
_REPLS = frozenset({"python", "python3", "node", "irb", "ipython", "bash", "sh", "zsh"})


def _normalise(cmd: str) -> str:
    """Candidate lookup key; exact match until the command normaliser lands (swap here)."""
    return cmd


def _rewritable(command: str) -> bool:
    if not command.strip() or any(s in command for s in _NEVER_REWRITE):
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    words = _strip_prefix(words)
    if not words:
        return False
    name = os.path.basename(words[0])
    if name in _INTERACTIVE:
        return False
    return not (name in _REPLS and len(words) == 1)


def _candidate(store: Path, key: str, env: Mapping[str, str]) -> dict[str, Any] | None:
    try:
        data = jsonstore.read(store / PRETRIM_FILE, {}, env)
    except (jsonstore.StoreBusy, OSError, ValueError):
        return None
    cands = data.get("candidates") if isinstance(data, dict) else None
    entry = cands.get(key) if isinstance(cands, dict) else None
    return entry if isinstance(entry, dict) else None


def pretrim(
    event: Event,
    policy: Policy,
    store: Path,
    cfg: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Action:
    """Rewrite a learned-noisy Bash command to a bounded form when the policy says ``bounded``.

    ``store`` is the project state directory (``projects/<key>/``) holding ``pretrim.json``.
    """
    cfg = cfg or Config()
    env = os.environ if env is None else env
    tool = event.tool
    if event.kind != "pre_tool" or tool is None or tool.name != "Bash" or not _enabled(cfg):
        return Action()
    command = tool.input.get("command")
    if not isinstance(command, str) or tool.input.get("run_in_background"):
        return Action()
    if not _rewritable(command):
        return Action()
    cand = _candidate(store, _normalise(command), env)
    if cand is None:
        return Action()
    lines = cand.get("lines")
    if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
        lines = _int(cfg, "guards.pretrim_lines", PRETRIM_LINES)
    overlay_file = tracking.overlay_path(event, env)
    overlay = session.read_session(overlay_file)
    decision = policy.decide(
        PRETRIM,
        {"tool_category": tool.category},
        tracking.session_ctx(event, overlay, overlay_file),
    )
    rec = DecisionRecord(
        decision_id=decision.decision_id,
        point=decision.point,
        arm=decision.arm,
        propensity=decision.propensity,
        bucket=decision.bucket,
        tokens_before=0,  # unknown before the tool runs; joined with PostToolUse later
        tokens_after=0,
        recovery_id=None,
        tool_use_id=tool.use_id,
        command_sig=command[:COMMAND_SIG_CHARS],
        ts=time.time(),
        tool_name=tool.name,
    )
    decisions.append(event.project_key, rec, cfg, env, "guards")
    if decision.arm != "bounded":
        return Action()
    new_input = dict(tool.input)
    new_input["command"] = f"{command} 2>&1 | tail -n {lines}"
    return Action(rewrite_input=new_input)
