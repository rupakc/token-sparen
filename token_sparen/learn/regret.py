"""Regret detection and savings attribution (spec section 7.5).

Regret signals are the only way the learner finds out that a trim hurt the agent. Every trimming
decision is tracked in the session overlay under ``regret`` (bounded to the last 200 decisions)
with what is needed to recognise regret later: the recovery id, the *normalised* command
signature (prefixed with the effective run directory unless it is the project root) and its
filter-free base, up to 64 distinctive tokens of the elided span (8+ characters, absent from the
kept text), stored only as 8-byte blake2b digests, and up to 32 file paths the output mentions
(kept text first). Signals:

1. recovery read of its saved full output (``Read`` of the file, ``cat``/``head``/``tail``/
   ``less``/``sed``/``grep`` of the path, any other tool input ending in the path,
   ``token-sparen show <id>``) -> 1.0;
2. re-run of the same normalised command (or the same command under another trailing filter
   pipeline) within ``[learn] regret_window_turns`` turns -> 1.0 if within 2 turns and nothing
   changed since (no edit elsewhere, no state-changing command), else 0.5; none if a file the
   output mentions was edited since the decision (fix-and-rerun is normal);
3. a prompt or a non-edit tool input holding >= 2 distinct elided distinctive tokens -> 0.5;
4. a failure loop in the decision's turn after compressing an error-bearing output (``on_loop``,
   called by the loop guard) -> 0.5.

A regret on a read de-dupe pointer also forgets the pointed-at read, so it is delivered in full.

A decision keeps the max of its signals. A full (1.0) regret is paid at once; a partial one when
the window expires, which is also when a decision without regret is paid an explicit 0 (silence
is not evidence). SessionEnd settles everything. Each decision is paid at most once and then
forgotten. Rewards go to the policy only if it has a ``reward`` method (the learning engine); the
ledger ``regrets`` counter is kept either way. Every settled payout is also appended to the
overlay's ``regret_outcomes`` (last 1000) for the session fold (circuit breaker, pre-trim
candidates; ``learn.overlay``). Everything fails open.

Savings at decision time: ``min(1, removed * expected_resends / p95)`` with the p95 of the last
64 values of ``removed * expected_resends`` per (point, bucket) in the project store
(``savings_scale.json``); the first observation of a bucket is worth 0.5.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen import recovery
from token_sparen.config import Config, log_error, state_home
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event, SessionCtx
from token_sparen.learn import pending
from token_sparen.store import jsonstore, paths, session

REGRET_KEY = "regret"
# settled [decision_id, point, arm, command_sig, value, bucket], for the fold
OUTCOMES_KEY = "regret_outcomes"
MAX_OUTCOMES = 1000
WINDOW_TURNS = 6
FULL_WITHIN_TURNS = 2
MAX_TRACKED = 200
MAX_TOKENS = 64
MAX_FILES = 32
MAX_EDITS = 8
MAX_SIG_CHARS = 500
SCAN_CHARS = 200_000  # elided text scanned for tokens and paths (sampled evenly beyond this)
INPUT_SCAN_CHARS = 8000  # prompt / tool input text scanned for elided tokens
MIN_TOKEN_HITS = 2
FULL = 1.0
PARTIAL = 0.5
ERROR_CTYPES = frozenset({"stacktrace", "test_report", "compiler", "build_log", "log"})
SCALE_FILE = "savings_scale.json"
SCALE_KEEP = 64
SCALE_FIRST = 0.5
DEFAULT_RESENDS = 10.0
_READ_TOOLS = frozenset({"Read", "NotebookRead"})
_READERS = frozenset({"cat", "head", "tail", "less", "more", "sed", "grep", "egrep", "rg", "bat"})

Payout = tuple[str, float]

# --------------------------------------------------------------------------- commands
_WS = re.compile(r"\s+")
_WORD = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|[^\s;&|'"])+"""
_LEAD_CD = re.compile(rf"^cd\s+({_WORD})\s*(?:&&|;)\s*")
_LEAD_ENV = re.compile(r"^env\s+(?=[A-Za-z_][A-Za-z0-9_]*=)")
_LEAD_ASSIGN = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]*=(?:{_WORD})?\s+")
_TRAIL_TAIL = re.compile(r"\s*\|\s*tail(?:\s+(?:-n\s*\d+|-\d+|--lines=\d+))?\s*$")
_TRAIL_REDIR = re.compile(r"\s+2>&1\s*$")


def _strip_all(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    pats = tuple(patterns)
    while True:
        before = text
        for pat in pats:
            text = pat.sub("", text, count=1)
        if text == before:
            return text


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


_TRAIL_FILTER = re.compile(r"\s*\|\s*(?:grep|egrep|fgrep|rg|head|tail|sed|awk|less|more)\b[^|]*$")


def filter_base(sig: str) -> str:
    """A normalised command without its trailing filter pipeline (``| grep …``, ``| head …``,
    ``| sed …``, ``| awk …``, ``| tail …``, ``| less``): re-filtering the same output is a
    re-run."""
    return _strip_all(sig, (_TRAIL_FILTER, _TRAIL_REDIR))


_READ_ONLY = frozenset(
    {"ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "find", "pwd", "echo", "which",
     "wc", "cd"}
)  # fmt: skip
_GIT_READ_ONLY = frozenset({"status", "diff", "log", "show"})
_GIT_ARG_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree"})
_FIND_WRITES = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_SEPS = frozenset({";", "&&", "||", "|", "&", "|&", "(", ")", ";;", "\n"})
_DEV_NULL = re.compile(r"\d?>>?\s*/dev/null|\d?>&\d")


def _simple_read_only(words: list[str]) -> bool:
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if words and words[0] == "env":
        words = words[1:]
        while words and "=" in words[0]:
            words = words[1:]
    if not words:
        return True
    prog = os.path.basename(words[0])
    if prog == "git":
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in _GIT_ARG_OPTS else rest[1:]
        return bool(rest) and rest[0] in _GIT_READ_ONLY
    if prog == "find":
        return not any(w in _FIND_WRITES for w in words)
    return prog in _READ_ONLY


def read_only(cmd: str) -> bool:
    """True when every simple command of ``cmd`` is on the read-only allowlist (``ls``, ``cat``,
    ``head``, ``tail``, ``grep``, ``rg``, ``find`` without actions, ``git status|diff|log|show``,
    ``pwd``, ``echo``, ``which``, ``wc``, ``cd``) and nothing is redirected to a file. Anything
    else, or a command that does not parse, may have changed state."""
    text = _DEV_NULL.sub(" ", cmd)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    words: list[str] = []
    for tok in [*tokens, ";"]:
        if tok in _SEPS:
            if not _simple_read_only(words):
                return False
            words = []
        elif tok and set(tok) <= set("<>&|;()"):
            if ">" in tok:
                return False  # writes a file
        else:
            words.append(tok)
    return True


_DRIVE = re.compile(r"^[A-Za-z]:[/\\]")


def _posix(path: str) -> str:
    """A host path (Windows backslashes allowed) in POSIX form."""
    return path.replace("\\", "/")


def _is_abs(path: str) -> bool:
    """Absolute on every platform: ``/x/...`` (POSIX, Git Bash) or a drive letter."""
    return path.startswith("/") or _DRIVE.match(path) is not None


def _run_dir(dirs: list[str], project_dir: str | None, cwd: str | None) -> str | None:
    """The effective directory a command runs in (the hook's cwd, then any leading ``cd``s),
    relative to the project when inside it; None when it is the project root (or unknown).

    Resolved with POSIX semantics and rendered with forward slashes on every OS (Claude Code's
    Bash uses POSIX paths on Windows), so signatures do not depend on the host platform."""
    cur = None if not (cwd or project_dir) else _posix(cwd or project_dir or "")
    for d in dirs:
        d = _posix(d) if _DRIVE.match(d) else d
        if cur is None or _is_abs(d) or d.startswith("~") or d == "-":
            cur = d
        else:
            cur = posixpath.join(cur, d)
    if cur is None:
        return None
    if not cur.startswith(("~", "-")):
        cur = posixpath.normpath(cur)
    if project_dir:
        root = posixpath.normpath(_posix(project_dir))
        if cur == root:
            return None
        prefix = root.rstrip("/") + "/"
        if _is_abs(cur) and cur.startswith(prefix):
            return cur[len(prefix) :]
    return cur


def normalize_command(
    cmd: str, *, project_dir: str | os.PathLike[str] | None = None, cwd: str | None = None
) -> str:
    """Signature of a shell command: whitespace collapsed; ``A=b`` assignments and ``env A=b``
    removed; a trailing ``| tail -n N`` and ``2>&1`` dropped; every other argument kept (``sudo``
    too); prefixed with ``cd <dir> &&`` for the effective run directory (the hook's ``cwd`` plus
    any leading ``cd``s, relative to ``project_dir``), omitted only at the project root. Without
    context, leading ``cd``s are kept as written (collapsed to one). At most 500 characters."""
    text = _WS.sub(" ", cmd).strip()
    dirs: list[str] = []
    while True:
        m = _LEAD_CD.match(text)
        if m is not None:
            dirs.append(_unquote(m.group(1)))
            text = text[m.end() :]
            continue
        stripped = _strip_all(text, (_LEAD_ENV, _LEAD_ASSIGN))
        if stripped == text:
            break
        text = stripped
    text = _strip_all(text, (_TRAIL_TAIL, _TRAIL_REDIR))
    root = None if project_dir is None else os.fspath(project_dir)
    if dirs or cwd:
        where = _run_dir(dirs, root, cwd)
        if where is not None:
            text = f"cd {where} && {text}"
    return text[:MAX_SIG_CHARS]


def _realpath(value: str) -> str:
    try:
        return os.path.realpath(value)
    except (OSError, ValueError):
        return value


def signature(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    project_dir: str | os.PathLike[str] | None = None,
    cwd: str | None = None,
) -> str:
    """Re-run key of a tool call: the normalised command for Bash, ``Read:<realpath>`` for a
    file read (any range), else the tool name and its input."""
    if tool_name == "Bash":
        cmd = tool_input.get("command")
        if not isinstance(cmd, str):
            return tool_name
        return normalize_command(cmd, project_dir=project_dir, cwd=cwd)
    if tool_name in _READ_TOOLS:
        path = tool_input.get("file_path")
        if isinstance(path, str) and path:
            return f"Read:{_realpath(path)}"[:MAX_SIG_CHARS]
    try:
        body = json.dumps(dict(tool_input), sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        body = ""
    return f"{tool_name}:{body}"[:MAX_SIG_CHARS]


def event_signature(event: Event) -> str:
    """``signature`` of the event's tool call, resolved against its project dir and cwd."""
    assert event.tool is not None
    cwd = event.extra.get("cwd")
    return signature(
        event.tool.name,
        event.tool.input,
        project_dir=event.project_dir,
        cwd=cwd if isinstance(cwd, str) and cwd else None,
    )


# --------------------------------------------------------------------------- elided span
_TOKEN = re.compile(r"[A-Za-z0-9_./-]{8,}")
_FILE = re.compile(
    r"(?<![\w./-])(?:[A-Za-z]:)?/?(?:[\w.-]+/)*[\w-][\w.-]*\.([A-Za-z][A-Za-z0-9]{0,7})(?![\w/])"
)
# Without a path separator a dotted word is a file only with one of these extensions
# (``cache.put`` or ``pytest.mark`` are not files).
_FILE_EXTS = frozenset(
    "py js ts tsx jsx go rs java kt rb php c h cc cpp cs swift scala md json yaml yml toml txt "
    "sh sql html css".split()
)


def token_digest(token: str) -> str:
    """8-byte blake2b hex digest of a distinctive token: the overlay never holds the token."""
    data = token.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def _tokens(text: str) -> Iterator[str]:
    for m in _TOKEN.finditer(text):
        tok = m.group().strip("./-")
        if len(tok) >= 8 and any(c.isalpha() for c in tok):
            yield tok


def _spread(items: list[str], k: int) -> list[str]:
    """At most ``k`` items, evenly spread over ``items`` (order kept)."""
    if len(items) <= k:
        return items
    return [items[i * len(items) // k] for i in range(k)]


def _elided(full: str, kept: str) -> str:
    """Lines of ``full`` that are not in ``kept``, sampled evenly to ``SCAN_CHARS``."""
    kept_lines = set(kept.splitlines())
    lines = [ln for ln in full.splitlines() if ln not in kept_lines]
    size = sum(len(ln) + 1 for ln in lines)
    if size > SCAN_CHARS and lines:
        step = math.ceil(size / SCAN_CHARS)
        lines = lines[::step]
    return "\n".join(lines)


def distinctive_tokens(full: str, kept: str, exclude: str = "") -> list[str]:
    """Up to 64 distinctive tokens of the elided span: 8+ characters of ``[A-Za-z0-9_./-]`` with
    a letter, absent from the kept text and from ``exclude`` (the command), spread over the span.
    """
    return _span_tokens(_elided(full, kept), kept, exclude)


def _span_tokens(span: str, kept: str, exclude: str) -> list[str]:
    banned = set(_tokens(kept)) | set(_tokens(exclude))
    seen: dict[str, None] = {}
    for tok in _tokens(span):
        if tok not in banned and tok not in seen:
            seen[tok] = None
            if len(seen) >= 64 * MAX_TOKENS:
                break
    return _spread(list(seen), MAX_TOKENS)


def _files(text: str) -> Iterator[str]:
    for m in _FILE.finditer(text):
        path = m.group()
        if "/" not in path and m.group(1).lower() not in _FILE_EXTS:
            continue
        if _REC_PATH.search(path):
            continue  # our own recovery files are not part of the agent's code
        yield path[2:] if path.startswith("./") else path


def span_files(full: str, kept: str) -> list[str]:
    """Up to 32 file paths the output mentions: the kept text first (what the agent sees and
    fixes; re-running after fixing it is normal), then the elided span."""
    return _span_files(_elided(full, kept), kept)


def _span_files(span: str, kept: str) -> list[str]:
    out: dict[str, None] = {}
    for text in (kept[:SCAN_CHARS], span):
        for path in _files(text):
            out.setdefault(path, None)
            if len(out) >= MAX_FILES:
                return list(out)
    return list(out)


def _path_match(edited: str, mentioned: str) -> bool:
    e = edited.replace("\\", "/")
    m = mentioned.replace("\\", "/")
    if e == m or e.endswith("/" + m.lstrip("/")):
        return True
    # a bare file name (no directory in the mention) matches any file of that name
    return "/" not in m and e.rsplit("/", 1)[-1] == m


# --------------------------------------------------------------------------- recovery reads
_REC_PATH = re.compile(r"\.token-sparen[/\\]outputs[/\\]([0-9a-f]{12})\.txt")
_SHOW = re.compile(r"(?:token-sparen|token_sparen|run_cli\.py)\b.*?\bshow\s+([0-9a-f]{12})\b")
_SPLIT = re.compile(r"[\s|;&()]+")
_REC_PATH_END = re.compile(_REC_PATH.pattern + r"\s*$")


def recovery_reads(event: Event) -> tuple[set[str], set[str]]:
    """(ids read from recovery files directly, ids read through ``token-sparen show``)."""
    tool = event.tool
    if tool is None:
        return set(), set()
    if tool.name in _READ_TOOLS:
        path = tool.input.get("file_path")
        if isinstance(path, str) and path:
            rid = recovery.is_recovery_read(event.project_dir, Path(path))
            if rid is not None:
                return {rid}, set()
        return set(), set()
    if tool.name != "Bash":
        if tool.category == "edit":
            return set(), set()
        found: set[str] = set()
        for value in tool.input.values():  # e.g. Grep/Glob `path` at a saved full output
            if isinstance(value, str):
                m = _REC_PATH_END.search(value)
                if m is not None:
                    found.add(m.group(1))
        return found, set()
    cmd = tool.input.get("command")
    if not isinstance(cmd, str) or not cmd:
        return set(), set()
    shown = set(_SHOW.findall(cmd))
    direct: set[str] = set()
    ids = set(_REC_PATH.findall(cmd))
    if ids and any(os.path.basename(w) in _READERS for w in _SPLIT.split(cmd)):
        direct = ids
    return direct, shown


def is_recovery_access(event: Event) -> bool:
    """True when the tool call reads a saved full output (never trim or de-dupe those)."""
    direct, shown = recovery_reads(event)
    return bool(direct or shown)


# --------------------------------------------------------------------------- detector
def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _best(entry: Mapping[str, Any]) -> float:
    value = entry.get("best")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _strs(value: Any) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _table(state: dict[str, Any]) -> dict[str, Any]:
    table = state.get(REGRET_KEY)
    if not isinstance(table, dict):
        table = {}
    state[REGRET_KEY] = table
    return table


def _outcome(did: str, entry: Any, value: float) -> list[Any]:
    e = entry if isinstance(entry, dict) else {}
    vals = [e.get("point"), e.get("arm"), e.get("command_sig")]
    bucket = e.get("bucket")
    return [
        did,
        *(v if isinstance(v, str) else "" for v in vals),
        value,
        bucket if isinstance(bucket, str) else "",
    ]


def _record_outcomes(state: dict[str, Any], rows: list[list[Any]]) -> None:
    """Append settled payouts to the overlay's ``regret_outcomes`` (bounded, oldest dropped)."""
    if not rows:
        return
    outs = state.get(OUTCOMES_KEY)
    outs = [*(outs if isinstance(outs, list) else []), *rows]
    state[OUTCOMES_KEY] = outs[-MAX_OUTCOMES:]


def _bump(state: dict[str, Any], key: str, inc: int) -> None:
    if inc:
        state[key] = _int(state.get(key)) + inc


def _entry(
    point: str,
    bucket: str,
    arm: str,
    recovery_id: str | None,
    command_sig: str,
    full: str,
    kept: str,
    ctype: str | None,
    files: Iterable[str] | None,
) -> dict[str, Any]:
    span = _elided(full, kept) if full else ""
    mentioned: dict[str, None] = dict.fromkeys(files or ())
    if full:
        mentioned.update(dict.fromkeys(_span_files(span, kept)))
    return {
        "point": point,
        "bucket": bucket,
        "arm": arm,
        "recovery_id": recovery_id,
        "command_sig": command_sig[:MAX_SIG_CHARS],
        "command_base": filter_base(command_sig[:MAX_SIG_CHARS]),
        "elided_tokens": [token_digest(t) for t in _span_tokens(span, kept, command_sig)]
        if full
        else [],
        "files_in_span": list(mentioned)[:MAX_FILES],
        "edited_paths_since": [],
        "decided_turn": 0,
        "ctype": ctype,
        "best": 0.0,
    }


@dataclass
class _Info:
    """What one event can tell the detector."""

    sig: str | None = None
    base: str | None = None
    state_change: bool = False  # an edit of an unmentioned file or a state-changing command
    recovery_ids: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    edit_path: str | None = None
    read_tokens: int = 0

    def empty(self) -> bool:
        return (
            self.sig is None
            and not self.recovery_ids
            and len(self.tokens) < MIN_TOKEN_HITS
            and self.edit_path is None
            and not self.read_tokens
        )


def _input_text(tool_input: Mapping[str, Any]) -> str:
    parts: list[str] = []
    size = 0
    for value in tool_input.values():
        if isinstance(value, str):
            parts.append(value[: INPUT_SCAN_CHARS - size])
            size += len(parts[-1])
            if size >= INPUT_SCAN_CHARS:
                break
    return "\n".join(parts)


def _info(event: Event) -> _Info:
    info = _Info()
    tool = event.tool
    if event.kind == "prompt" and event.prompt:
        info.tokens = {token_digest(t) for t in set(_tokens(event.prompt[:INPUT_SCAN_CHARS]))}
        return info
    if tool is None or event.kind not in ("post_tool", "post_tool_failure"):
        return info
    if tool.category == "edit":
        path = tool.input.get("file_path") or tool.input.get("notebook_path")
        if isinstance(path, str) and path:
            info.edit_path = path
        return info
    info.sig = event_signature(event)
    info.base = filter_base(info.sig) if tool.name == "Bash" else info.sig
    cmd = tool.input.get("command") if tool.name == "Bash" else None
    info.state_change = isinstance(cmd, str) and not read_only(cmd)
    direct, shown = recovery_reads(event)
    info.recovery_ids = direct | shown
    if direct and event.output_text:  # `token-sparen show` counts its own tokens
        info.read_tokens = estimate_tokens(event.output_text)
    info.tokens = {token_digest(t) for t in set(_tokens(_input_text(tool.input)))}
    return info


def reward_fn(policy: object) -> Callable[[str, float | None, float | None], None] | None:
    """The policy's ``reward`` method when it learns (duck-typed), else None."""
    fn = getattr(policy, "reward", None)
    return fn if callable(fn) else None


def pay(policy: object, payouts: Iterable[Payout]) -> None:
    """Report regret values to a learning policy (no-op for the static policy)."""
    fn = reward_fn(policy)
    if fn is None:
        return
    for decision_id, value in payouts:
        fn(decision_id, None, value)


class RegretDetector:
    """Tracks trimming decisions in the session overlay and turns later events into regret."""

    def __init__(self, cfg: Config | None = None, env: Mapping[str, str] | None = None) -> None:
        self.cfg = cfg or Config()
        self.env: Mapping[str, str] = os.environ if env is None else env

    @property
    def window(self) -> int:
        value = self.cfg.get("learn.regret_window_turns", WINDOW_TURNS)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return WINDOW_TURNS

    def _overlay(self, event: Event) -> Path:
        return session.session_path(state_home(self.env), event.project_key, event.session_id)

    # --- decisions --------------------------------------------------------------------------

    def track(
        self,
        ctx: SessionCtx,
        decision_id: str,
        *,
        point: str,
        bucket: str,
        arm: str,
        recovery_id: str | None,
        command_sig: str,
        full: str,
        kept: str,
        ctype: str | None,
        files: Iterable[str] | None = None,
        read_hash: str | None = None,
    ) -> list[Payout]:
        """Start watching a trimming decision; returns the payouts of decisions evicted to keep
        the table at 200 (their best regret so far, usually 0). Never raises."""
        try:
            entry = _entry(point, bucket, arm, recovery_id, command_sig, full, kept, ctype, files)
            entry["decided_turn"] = ctx.turn
            if read_hash is not None:
                entry["read_hash"] = read_hash  # read de-dupe: forget the read on regret
            return self._track(ctx, decision_id, entry)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a hook
            log_error(self.env, "regret", f"track: {type(exc).__name__}: {exc}")
            return []

    def _track(self, ctx: SessionCtx, decision_id: str, entry: dict[str, Any]) -> list[Payout]:
        evicted: list[Payout] = []

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            evicted.clear()
            table = _table(state)
            table.pop(decision_id, None)
            table[decision_id] = entry
            rows: list[list[Any]] = []
            while len(table) > MAX_TRACKED:
                old = next(iter(table))
                gone = table.pop(old)
                evicted.append((old, _best(gone) if isinstance(gone, dict) else 0.0))
                rows.append(_outcome(old, gone, evicted[-1][1]))
            _record_outcomes(state, rows)
            _bump(state, "regrets", sum(1 for _, v in evicted if v > 0))
            return state

        session.update_session(ctx.overlay_path, apply)
        if evicted:
            pending.add_overlay(ctx.overlay_path, self.env)
        return evicted

    # --- signals ----------------------------------------------------------------------------

    def _process(
        self,
        table: dict[str, Any],
        info: _Info,
        turn: int,
        *,
        settle: bool,
        final: bool,
        outcomes: list[list[Any]] | None = None,
    ) -> tuple[list[Payout], list[Payout], bool, set[str]]:
        """Apply one event to the table in place: (signals, payouts, changed, read hashes of
        regretted read de-dupe decisions). Settled payouts are also appended to ``outcomes``
        as ``[decision_id, point, arm, command_sig, value, bucket]``."""
        forget: set[str] = set()
        signals: list[Payout] = []
        payouts: list[Payout] = []
        changed = False
        window = self.window
        for did, entry in list(table.items()):
            if not isinstance(entry, dict):
                del table[did]
                changed = True
                continue
            value = 0.0
            decided = _int(entry.get("decided_turn"))
            if info.recovery_ids and entry.get("recovery_id") in info.recovery_ids:
                value = FULL
            edited = _strs(entry.get("edited_paths_since"))
            if info.edit_path is not None:
                if any(_path_match(info.edit_path, f) for f in _strs(entry.get("files_in_span"))):
                    if len(edited) < MAX_EDITS:
                        entry["edited_paths_since"] = [*edited, info.edit_path]
                        changed = True
                elif entry.get("changed_since") is not True:
                    entry["changed_since"] = True  # an edit elsewhere: a re-run may be a check
                    changed = True
            match = self._same_command(entry, info) if info.sig is not None else None
            rerun = match is not None
            if rerun and not edited:
                age = turn - decided
                if 0 <= age <= window:
                    full = (
                        match == "exact"  # a different filter on the same output: partial
                        and age <= FULL_WITHIN_TURNS
                        and entry.get("changed_since") is not True
                    )
                    value = max(value, FULL if full else PARTIAL)
            elif not rerun and info.state_change and entry.get("changed_since") is not True:
                entry["changed_since"] = True
                changed = True
            if len(info.tokens) >= MIN_TOKEN_HITS:
                hits = 0
                for tok in _strs(entry.get("elided_tokens")):
                    if tok in info.tokens:
                        hits += 1
                        if hits >= MIN_TOKEN_HITS:
                            value = max(value, PARTIAL)
                            break
            best = _best(entry)
            if value > 0.0:
                signals.append((did, value))
                read_hash = entry.get("read_hash")
                if isinstance(read_hash, str):
                    forget.add(read_hash)
                if value > best:
                    entry["best"] = best = value
                    changed = True
            if settle and (best >= FULL or final or turn - decided > window):
                payouts.append((did, best))
                if outcomes is not None:
                    outcomes.append(_outcome(did, entry, best))
                del table[did]
                changed = True
        return signals, payouts, changed, forget

    @staticmethod
    def _same_command(entry: Mapping[str, Any], info: _Info) -> str | None:
        """ "exact" (same signature), "base" (same once trailing filters are stripped) or None."""
        sig = entry.get("command_sig")
        if not isinstance(sig, str):
            return None
        if sig == info.sig:
            return "exact"
        base = entry.get("command_base")
        return "base" if (base if isinstance(base, str) else sig) == info.base else None

    def _apply(self, path: Path, info: _Info, turn: int | None, settle: bool, final: bool) -> Any:
        """Lock-free pre-check, then (only if something changes) one locked update.
        Returns (signals, payouts)."""
        snapshot = session.read_session(path)
        now = _int(snapshot.get("turn")) if turn is None else turn
        table = snapshot.get(REGRET_KEY)
        if not isinstance(table, dict) or not table:
            if not info.read_tokens:
                return [], []
            table = {}
        _, _, changed, _ = self._process(table, info, now, settle=settle, final=final)
        if not changed and not info.read_tokens:
            return [], []
        out: dict[str, list[Payout]] = {"signals": [], "payouts": []}

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            cur = _int(state.get("turn")) if turn is None else turn
            table = _table(state)
            rows: list[list[Any]] = []
            sig, payouts, _, forget = self._process(
                table, info, cur, settle=settle, final=final, outcomes=rows
            )
            _record_outcomes(state, rows)
            if forget:
                from token_sparen.mechanisms import read_dedupe  # it owns the `reads` schema

                read_dedupe.drop_reads(state, forget)
            _bump(state, "recovery_read_tokens", info.read_tokens)
            _bump(state, "regrets", sum(1 for _, v in payouts if v > 0))
            out["signals"], out["payouts"] = sig, payouts
            return state

        session.update_session(path, apply)
        if out["payouts"]:
            pending.add_overlay(path, self.env)  # settled outcomes await a fold
        return out["signals"], out["payouts"]

    def on_event(self, event: Event, ctx: SessionCtx) -> list[Payout]:
        """Record the regret signals of ``event`` (max per decision); returns them. Nothing is
        paid here (``handle`` settles)."""
        info = _info(event)
        if info.empty():
            return []
        signals, _ = self._apply(ctx.overlay_path, info, ctx.turn, settle=False, final=False)
        return list(signals)

    def on_loop(self, ctx: SessionCtx, decision_ids: Iterable[str] | None) -> list[Payout]:
        """Failure loop in this turn: 0.5 regret for compression decisions of this turn that
        trimmed an error-bearing output (``decision_ids`` None = every tracked decision)."""
        wanted = None if decision_ids is None else set(decision_ids)
        out: list[Payout] = []

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            for did, entry in _table(state).items():
                if wanted is not None and did not in wanted:
                    continue
                if not isinstance(entry, dict) or entry.get("point") != "compression":
                    continue
                if entry.get("ctype") not in ERROR_CTYPES:
                    continue
                if _int(entry.get("decided_turn")) != ctx.turn:
                    continue
                entry["best"] = max(_best(entry), PARTIAL)
                out.append((did, PARTIAL))
            return state

        try:
            session.update_session(ctx.overlay_path, apply)
        except Exception as exc:  # noqa: BLE001 - fail open
            log_error(self.env, "regret", f"on_loop: {type(exc).__name__}: {exc}")
            return []
        return out

    def run(self, event: Event, policy: object) -> list[Payout]:
        """Signals of ``event``, then settle (full regrets, expired windows, everything at
        SessionEnd) and pay the policy. Returns the payouts."""
        info = _info(event)
        final = event.kind == "session_end"
        if info.empty() and event.kind not in ("prompt", "session_end"):
            # only turn boundaries expire windows; a CLI-recorded full regret waits for one
            return []
        _, payouts = self._apply(self._overlay(event), info, None, settle=True, final=final)
        pay(policy, payouts)
        return list(payouts)


def handle(
    event: Event, cfg: Config, policy: object, *, env: Mapping[str, str] | None = None
) -> Action:
    """Hook dispatch (UserPromptSubmit, PostToolUse(+Failure), SessionEnd). Never raises."""
    env = os.environ if env is None else env
    try:
        RegretDetector(cfg, env).run(event, policy)
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "regret", f"{type(exc).__name__}: {exc}")
    return Action()


def record_recovery_read(
    overlay: Path, recovery_id: str, tokens: int, env: Mapping[str, str] | None = None
) -> None:
    """``token-sparen show <id>`` inside a session: full regret for the decisions that saved
    this output (paid by the next hook) and the read's tokens in the ledger. Only touches an
    existing overlay. Never raises."""
    env = os.environ if env is None else env
    try:
        if not overlay.is_file():
            return

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            for entry in _table(state).values():
                if isinstance(entry, dict) and entry.get("recovery_id") == recovery_id:
                    entry["best"] = FULL
            _bump(state, "recovery_read_tokens", max(0, tokens))
            return state

        session.update_session(overlay, apply)
    except Exception as exc:  # noqa: BLE001 - never break `show`
        log_error(env, "regret", f"show: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- savings
def expected_resends(remaining_turns: float | None) -> float:
    """How often a trimmed output would have been re-sent: the governor's projected remaining
    turns (at least 1), or 10 when unknown."""
    if remaining_turns is None or not math.isfinite(remaining_turns):
        return DEFAULT_RESENDS
    return max(1.0, float(remaining_turns))


def _scale_values(raw: Any) -> list[float]:
    if not isinstance(raw, list):
        return []
    return [
        float(v)
        for v in raw
        if not isinstance(v, bool) and isinstance(v, int | float) and math.isfinite(v) and v > 0
    ]


def savings_value(
    home: Path,
    project_key: str,
    point: str,
    bucket: str,
    removed: float,
    resends: float,
    env: Mapping[str, str] | None = None,
) -> float:
    """``min(1, removed * resends / p95)`` over the bucket's last 64 values (this one
    included); 0.5 for a bucket's first observation, 0 when nothing was removed."""
    x = max(0.0, float(removed)) * max(1.0, float(resends))
    if x <= 0.0:
        return 0.0
    out = {"v": SCALE_FIRST}

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        pts = state.get("points")
        pts = pts if isinstance(pts, dict) else {}
        buckets = pts.get(point)
        buckets = buckets if isinstance(buckets, dict) else {}
        values = _scale_values(buckets.get(bucket))
        if values:
            ranked = sorted([*values, x])
            p95 = ranked[max(0, math.ceil(0.95 * len(ranked)) - 1)]
            out["v"] = min(1.0, x / p95)
        buckets[bucket] = [*values, x][-SCALE_KEEP:]
        pts[point] = buckets
        return {"version": 1, "points": pts}

    path = paths.project_dir_for(home, project_key) / SCALE_FILE
    jsonstore.update(path, apply, {}, env)
    return out["v"]


def reward_savings(
    policy: object,
    decision_id: str,
    point: str,
    bucket: str,
    removed: float,
    resends: float,
    *,
    home: Path,
    project_key: str,
    env: Mapping[str, str],
) -> None:
    """Savings reward at decision time, only for a learning policy. Never raises."""
    fn = reward_fn(policy)
    if fn is None:
        return
    try:
        value = savings_value(home, project_key, point, bucket, removed, resends, env)
        fn(decision_id, value, None)
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "regret", f"savings: {type(exc).__name__}: {exc}")
