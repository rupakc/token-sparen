"""Opt-in learned tool pruning with in-session recovery (spec section 6.10, tier high).

Tools the agent never uses still cost tokens on every request. Per project, a candidate that was
**available and never used** across enough evidence is removed from the model's context by a host
deny rule (Claude Code: a bare ``permissions.deny`` entry such as ``"mcp__server"`` in
``<checkout>/.claude/settings.local.json``, written by the host adapter's ``DenyWriter``; this
module is host-neutral).

**Scope.** In interactive Claude Code built-in tools are deferred (a name, a few tokens each), so
pruning one saves almost nothing for real risk. Candidates are therefore **MCP servers**
(``mcp__<server>``, none of whose tools was ever used: their tool names and server instructions
go). Built-in tools are candidates only with ``[prune] builtins = true`` in the user's own
``config.toml`` **and** only when a session's SDK ``init`` record listed them (always loaded).
Savings are modest and not estimated (hooks cannot see definition sizes): only the number of
pruned tools is reported.

**Opt-in.** Nothing happens (no evidence, no writes) unless ``token-sparen prune enable``
recorded the project opt-in in ``projects/<key>/prune.json`` or the user's own
``~/.token-sparen/config.toml`` sets ``[prune] enabled = true``. ``enabled`` and ``builtins`` are
read from that file only: never from ``TOKEN_SPAREN_*`` variables (a repository's committed
Claude Code settings can set ``env``), session overrides or a project's ``token-sparen.toml``,
whose only key here is ``allow = false`` (off, our rules reverted at the next fold or start).
Enabling scans the project's earlier transcripts (last 60 days, at most 50 MB) once: a tool used
ever is never a candidate.

**Evidence** (fold time, one streaming read of the transcript and its subagent transcripts): the
session's tool list (``transcript.tool_inventory``; a session without one counts for the tools
other sessions listed) gives the available tools. Protected, never pruned: ``PROTECTED``, every
``Task*`` tool, every tool (and server) ever used in the project, every tool restored once. Each
session a candidate was available and unused in adds its prompts as zero-regret observations to a
Beta over "pruning it would have caused a restore" (prior Beta(1, 1)). A candidate is pruned only
with ``>= min_sessions`` sessions spanning ``>= min_prompts`` prompts **and** the high-tier gate
``ucb(ucb_q) <= tau`` (0.95, 0.01: about 300 unused prompts).

**Checkouts.** Sibling clones and worktrees share the project key but each has its own settings
file: owned rules, created containers and the user's own rules are recorded per settings-file
real path in the user-level registry ``~/.token-sparen/prune_rules.json`` (``files``, each with
the project key it was written under), never only under the project key: a changed git remote
never orphans a rule, since a checkout always sees its own record. Hooks and the fold act on the
current checkout's file only; ``restore --all`` and ``disable`` act on every record of the
project and the current checkout's; the ``allow = false`` revert on the records of its own key.

**When.** Rules change the tool list, which invalidates the prompt cache, so new ones are written
only by the detached SessionEnd fold of a session (``fold --prune-apply``; effective from the
next session) and only when no other session of the project that has not ended was active in
the last ``idle_minutes`` (``tracking.others_active``); otherwise they stay ``pending`` for a
later session end. No hook and no other fold writes a new rule; git is asked before the store
locks are taken, so a hook restore never waits on it. Restores never wait. A settings file is
written only when git confirms it is untracked and ignored.

**Crash safety.** Just before a settings write the rules and the containers it will create are
journaled in the file's record (``pending_add``, ``pending_created``, under the locks); after it
they move to ``owned`` / ``created``. Every read reconciles a leftover journal (the process was
killed in between): a journaled rule present in the settings file is ours (``owned``, with the
journaled containers), an absent one is dropped. A journaled rule is never classified as the
user's (``foreign``). Only rules absent from the file are journaled.

**Lease.** A pruned tool is not in the model's list, so "unused" while pruned is produced by the
intervention itself: pruned rules accrue no evidence. Each prune is a lease of
``lease_sessions`` folded sessions of that checkout; then the rule is restored (no regret) for
at least one session so its use is observed again, and it is pruned again only on fresh
evidence (``min_sessions`` un-pruned sessions spanning ``min_prompts`` prompts).

**Recovery.** SessionStart shows one line (to the model and the user) naming the pruned tools and
the restore command (the plugin's own launcher when ``CLAUDE_PLUGIN_ROOT`` is known; pre-approved
in the skill). A ToolSearch query naming a pruned tool, a user prompt naming it (exact name, or
its multi-word form), a direct call (PreToolUse, PostToolUse failure, PermissionDenied:
defensive, a denied tool is not in the model's list) or, at fold, a transcript showing an attempt
restores it at once: our rule is removed, regret 1.0 is recorded and the tool is kept for good.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from token_sparen.config import Config, flag, log_error, state_home, user_config
from token_sparen.emit import Gate
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn.beta import Beta
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, paths, schema
from token_sparen.transcript import ToolInventory, tool_inventory

PRUNE_FILE = "prune.json"
REGISTRY_FILE = "prune_rules.json"  # at the state root, beside the statusline install record
VERSION = 2
PRIOR = Beta(1.0, 1.0)
CAP = 1000.0  # posterior mass cap (mean-preserving rescale)
MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024
MAX_SUBAGENT_FILES = 50
HISTORY_DAYS = 60
HISTORY_BYTES = 50 * 1024 * 1024
MAX_NAMES = 5000
MAX_NOTICES = 5
APPLIED_MAX = 200
HOOK_LOCK_S = 0.1
NOTICE_TOKENS = 100  # the SessionStart line's own budget (names are truncated to fit)
PROTECTED = frozenset(
    {
        "Read",
        "Edit",
        "MultiEdit",
        "Write",
        "Bash",
        "Grep",
        "Glob",
        "LS",
        "Agent",
        "Task",
        "TodoWrite",
        "AskUserQuestion",
        "Skill",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
        "SendMessage",
    }
)
PROTECTED_PREFIXES = ("Task",)  # Task, TaskCreate, TaskUpdate, TaskGet, TaskList, TaskStop, ...
CLI = "{cli}"  # placeholder in stored notices: the CLI command, rendered when shown
GATE_KIND = "prune"
SAVINGS_NOTE = (
    "savings: modest (the names and server instructions of unused MCP servers); token-sparen "
    "cannot measure tool definition sizes from hooks, so it reports the number of pruned tools only"
)
_NO_SUCH_TOOL = re.compile(r"No such tool available: ([A-Za-z0-9_.:-]+)")


# --------------------------------------------------------------------------- host writer
@dataclass(frozen=True)
class WriteResult:
    """What a host writer did. ``changed``: rules added / removed; ``skipped``: on add, rules
    already present (the user's, never ours), on remove, rules no longer present."""

    ok: bool
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    created: dict[str, bool] = field(default_factory=dict)
    message: str = ""


class DenyWriter(Protocol):
    def settings_file(self, project_dir: Path) -> Path: ...

    def exposure(self, project_dir: Path) -> str | None:
        """None only when the settings file is confirmed untracked and ignored; otherwise why
        it is exposed (``"tracked"``, ``"unignored"``, ``"backup"``, ``"unknown"``): never
        written."""
        ...

    def add(
        self,
        project_dir: Path,
        rules: Sequence[str],
        fresh_backup: bool = False,
        journal: Callable[[list[str], dict[str, bool]], None] | None = None,
    ) -> WriteResult:
        """``journal(new rules, containers about to be created)`` runs right before the write."""
        ...

    def remove(
        self,
        project_dir: Path,
        rules: Sequence[str],
        created: Mapping[str, bool],
        cleanup: bool,
    ) -> WriteResult: ...

    def present(self, project_dir: Path) -> set[str] | None:
        """The deny rules in the settings file now (None: unreadable)."""
        ...

    def history_dirs(self, project_dir: Path, env: Mapping[str, str]) -> list[Path]:
        """Directories holding the project's earlier transcripts."""
        ...


WRITER: DenyWriter | None = None  # set by the host adapter (``adapters.claude.settings``)


def register_writer(writer: DenyWriter) -> None:
    global WRITER
    WRITER = writer


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    enabled: bool  # the user's own config.toml only
    builtins: bool  # the user's own config.toml only
    allow: bool
    min_sessions: int
    min_prompts: int
    idle_s: float
    tau: float
    ucb_q: float
    lease: int = 15


def _real(raw: object, default: float, lo: float, hi: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw != raw:
        return default
    return min(hi, max(lo, float(raw)))


def _section(data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = data.get("prune") if isinstance(data, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def settings(params: Mapping[str, Any] | None, user: Mapping[str, Any] | None = None) -> Settings:
    """Thresholds and ``allow`` from the merged config; ``enabled`` / ``builtins`` only from
    ``user`` (the parsed ``~/.token-sparen/config.toml``)."""
    p, u = _section(params), _section(user)
    return Settings(
        enabled=flag(u.get("enabled"), False),
        builtins=flag(u.get("builtins"), False),
        allow=flag(p.get("allow"), True),
        min_sessions=int(_real(p.get("min_sessions"), 3, 1, 1e6)),
        min_prompts=int(_real(p.get("min_prompts"), 15, 1, 1e9)),
        idle_s=_real(p.get("idle_minutes"), 60.0, 0.0, 1e6) * 60.0,
        tau=_real(p.get("tau"), 0.01, 0.0, 0.5),
        ucb_q=_real(p.get("ucb_q"), 0.95, 0.51, 0.999),
        lease=int(_real(p.get("lease_sessions"), 15, 1, 1e6)),
    )


def settings_for(params: Mapping[str, Any] | None, env: Mapping[str, str]) -> Settings:
    return settings(params, user_config(env))


def active(doc: Mapping[str, Any], s: Settings) -> bool:
    return s.allow and (s.enabled or doc.get("enabled") is True)


# --------------------------------------------------------------------------- names
def server_of(name: str) -> str | None:
    """``mcp__<server>`` of an MCP tool name ``mcp__<server>__<tool>``, else None."""
    if not name.startswith("mcp__"):
        return None
    parts = name[5:].split("__")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"mcp__{parts[0]}"
    return None


def _is_server(rule: str) -> bool:
    return rule.startswith("mcp__") and "__" not in rule[5:] and len(rule) > 5


def candidate_names(available: Iterable[str]) -> set[str]:
    """Built-in names as they are; MCP tools as their server; other ``mcp__`` names dropped."""
    out: set[str] = set()
    for name in available:
        if not name.startswith("mcp__"):
            out.add(name)
            continue
        server = server_of(name)
        if server is not None:
            out.add(server)
    return out


def _strs(raw: object) -> list[str]:
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def _kept(doc: Mapping[str, Any]) -> dict[str, Any]:
    raw = doc.get("kept")
    return raw if isinstance(raw, dict) else {}


def _tools(doc: dict[str, Any]) -> dict[str, Any]:
    raw = doc.get("tools")
    if not isinstance(raw, dict):
        raw = doc["tools"] = {}
    return raw


def _used_by(rule: str, used: Iterable[str]) -> bool:
    if _is_server(rule):
        return any(u == rule or u.startswith(rule + "__") for u in used)
    return rule in used


def is_protected(name: str, doc: Mapping[str, Any]) -> bool:
    if name in PROTECTED or name.startswith(PROTECTED_PREFIXES):
        return True
    return name in _kept(doc) or _used_by(name, _strs(doc.get("used")))


def is_candidate(name: str, doc: Mapping[str, Any], s: Settings | None = None) -> bool:
    """A server never used, or (``builtins`` on) a built-in an SDK init record listed."""
    if not name or is_protected(name, doc):
        return False
    if _is_server(name):
        return True
    if name.startswith("mcp__"):
        return False
    return s is not None and s.builtins and name in _strs(doc.get("always"))


def tool_rule(name: str, rules: Iterable[str]) -> list[str]:
    """The pruned rules that cover tool ``name`` (itself, or its server)."""
    server = server_of(name)
    return [r for r in rules if r == name or (server is not None and r == server)]


def _words(name: str) -> list[str]:
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", name)


def _patterns(rule: str) -> list[re.Pattern[str]]:
    """Exact name (case-sensitive), and a multi-word form only when the name has >= 2 words."""
    if _is_server(rule):
        out = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(rule)}(?:__|(?![A-Za-z0-9_]))")]
        words = [w for part in re.split(r"[_-]+", rule[5:]) for w in _words(part)]
    else:
        out = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(rule)}(?![A-Za-z0-9_])")]
        words = _words(rule)
    if len(words) >= 2:
        joined = r"[\s_-]*".join(re.escape(w) for w in words)
        out.append(re.compile(rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])", re.I))
    return out


def mentions(text: str | None, rules: Iterable[str]) -> list[str]:
    """The rules ``text`` names: the exact name (``WebFetch``, ``mcp__github``) or its
    multi-word form (``web search``, ``claude ai gmail``); a single common word never counts."""
    if not text:
        return []
    return [r for r in rules if any(p.search(text) for p in _patterns(r))]


# --------------------------------------------------------------------------- per-file records
def file_key(settings_file: Path) -> str:
    """The settings file's real path (sibling clones and worktrees have one each)."""
    try:
        return str(settings_file.resolve())
    except (OSError, RuntimeError):
        return str(settings_file.absolute())


def _files(doc: Mapping[str, Any]) -> dict[str, Any]:
    raw = doc.get("files")
    return raw if isinstance(raw, dict) else {}


def _file(doc: dict[str, Any], fkey: str, project_dir: Path) -> dict[str, Any]:
    files = doc.get("files")
    if not isinstance(files, dict):
        files = doc["files"] = {}
    entry = files.get(fkey)
    if not isinstance(entry, dict):
        entry = files[fkey] = {"dir": str(project_dir), "owned": [], "created": {}}
    entry["dir"] = str(project_dir)
    return entry


def owned_in(doc: Mapping[str, Any], fkey: str) -> list[str]:
    entry = _files(doc).get(fkey)
    return _strs(entry.get("owned")) if isinstance(entry, dict) else []


def all_owned(doc: Mapping[str, Any]) -> list[str]:
    out: set[str] = set()
    for entry in _files(doc).values():
        if isinstance(entry, dict):
            out.update(_strs(entry.get("owned")))
    return sorted(out)


def _created(entry: Mapping[str, Any]) -> dict[str, bool]:
    raw = entry.get("created")
    return {k: v is True for k, v in raw.items()} if isinstance(raw, dict) else {}


# --------------------------------------------------------------------------- evidence
def _beta(entry: Mapping[str, Any]) -> Beta:
    try:
        return Beta.from_list(entry.get("r", PRIOR.to_list()))
    except ValueError:
        return PRIOR


def _observe(post: Beta, x: float, k: int) -> Beta:
    """``k`` observations of reward ``x`` (no discount), then the mean-preserving cap."""
    if k <= 0:
        return post
    a, b = post.a + x * k, post.b + (1.0 - x) * k
    total = a + b
    if total > CAP:
        a, b = a * CAP / total, b * CAP / total
    return Beta(max(a, 1e-6), max(b, 1e-6), post.n + k)


def _entry(doc: dict[str, Any], rule: str) -> dict[str, Any]:
    tools = _tools(doc)
    entry = tools.get(rule)
    if not isinstance(entry, dict):
        entry = tools[rule] = {"s": 0, "p": 0, "r": PRIOR.to_list()}
    return entry


def _int(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def ucb(entry: Mapping[str, Any], s: Settings) -> float:
    return _beta(entry).ucb(s.ucb_q)


def _fresh_ok(entry: Mapping[str, Any], s: Settings) -> bool:
    """After a lease ended: enough evidence from un-pruned sessions since then."""
    fresh = entry.get("fresh")
    if not isinstance(fresh, dict):
        return True
    return _int(fresh.get("s")) >= s.min_sessions and _int(fresh.get("p")) >= s.min_prompts


def ready(entry: Mapping[str, Any], s: Settings) -> bool:
    return (
        _int(entry.get("s")) >= s.min_sessions
        and _int(entry.get("p")) >= s.min_prompts
        and ucb(entry, s) <= s.tau
        and _fresh_ok(entry, s)
    )


def plan(doc: Mapping[str, Any], s: Settings, fkey: str) -> list[str]:
    """Candidates that pass every gate and are neither ours nor the user's in this file. Nothing
    before the project's history has been read once (``seeded``): a tool used before pruning
    was enabled must never be pruned."""
    tools = doc.get("tools")
    if not isinstance(tools, dict) or doc.get("seeded") is not True:
        return []
    entry = _files(doc).get(fkey)
    foreign = _strs(entry.get("foreign")) if isinstance(entry, dict) else []
    journal = _strs(entry.get("pending_add")) if isinstance(entry, dict) else []
    skip = set(owned_in(doc, fkey)) | set(foreign) | set(journal)
    return sorted(
        name
        for name, row in tools.items()
        if isinstance(row, dict)
        and name not in skip
        and is_candidate(name, doc, s)
        and ready(row, s)
    )


def _add_names(doc: dict[str, Any], key: str, names: Iterable[str]) -> None:
    merged = set(_strs(doc.get(key))) | {n for n in names if n}
    doc[key] = sorted(merged)[:MAX_NAMES]


def observe_session(doc: dict[str, Any], inv: ToolInventory, s: Settings, fkey: str) -> list[str]:
    """Fold one session's inventory into ``doc``; returns this file's pruned rules it tried."""
    pruned = owned_in(doc, fkey)
    attempted = sorted({r for name in (*inv.used, *inv.unknown) for r in tool_rule(name, pruned)})
    if inv.listed is not None:
        _add_names(doc, "known", inv.listed)
    if inv.always is not None:
        _add_names(doc, "always", (n for n in inv.always if not n.startswith("mcp__")))
    _add_names(doc, "used", inv.used - inv.unknown)
    available = inv.listed if inv.listed is not None else set(_strs(doc.get("known")))
    tools = _tools(doc)
    for name in list(tools):
        if is_protected(name, doc) and name not in _kept(doc):
            del tools[name]  # used now: protected for good
    if inv.prompts <= 0:
        return attempted
    # a pruned rule is not in the model's list: "unused" would be produced by the pruning
    # itself, so it accrues no evidence while pruned here (review M6)
    for rule in candidate_names(available) - set(pruned):
        if not is_candidate(rule, doc, s) or rule in attempted:
            continue
        entry = _entry(doc, rule)
        entry["s"] = _int(entry.get("s")) + 1
        entry["p"] = _int(entry.get("p")) + inv.prompts
        entry["r"] = _observe(_beta(entry), 0.0, inv.prompts).to_list()
        fresh = entry.get("fresh")
        if isinstance(fresh, dict):
            fresh["s"] = _int(fresh.get("s")) + 1
            fresh["p"] = _int(fresh.get("p")) + inv.prompts
    return attempted


def _cut(env: Mapping[str, str]) -> None:
    log_error(env, "prune", f"transcript over {MAX_TRANSCRIPT_BYTES} bytes, rest not read")


def scan(transcript: Path, env: Mapping[str, str]) -> ToolInventory:
    """The main transcript plus its subagent transcripts (``<stem>/subagents/*.jsonl``)."""
    inv = tool_inventory(transcript, max_bytes=MAX_TRANSCRIPT_BYTES, on_limit=lambda: _cut(env))
    sub = transcript.with_suffix("") / "subagents"
    with contextlib.suppress(OSError):
        for i, path in enumerate(sorted(sub.glob("*.jsonl"))):
            if i >= MAX_SUBAGENT_FILES:
                break
            inv.merge(tool_inventory(path, max_bytes=MAX_TRANSCRIPT_BYTES))
    return inv


def history_files(dirs: Iterable[Path], now: float | None = None) -> list[Path]:
    """Transcripts (and their subagent transcripts) of the last ``HISTORY_DAYS``, newest first,
    at most ``HISTORY_BYTES`` in all."""
    cutoff = (time.time() if now is None else now) - HISTORY_DAYS * 86400
    found: dict[Path, tuple[float, int]] = {}
    for d in dirs:
        with contextlib.suppress(OSError):
            for path in [*d.glob("*.jsonl"), *d.glob("*/subagents/*.jsonl")]:
                with contextlib.suppress(OSError):
                    st = path.stat()
                    if st.st_mtime >= cutoff:
                        found[path] = (st.st_mtime, st.st_size)
    out: list[Path] = []
    total = 0
    for path, (_, size) in sorted(found.items(), key=lambda kv: -kv[1][0]):
        if total + size > HISTORY_BYTES:
            break
        total += size
        out.append(path)
    return out


def seed(doc: dict[str, Any], files: Sequence[Path]) -> int:
    """Add every tool the earlier transcripts used to ``used`` (never a candidate). ``seeded``
    is set only when at least one transcript was read: a miss (no history folder found) stays
    unseeded, so the first fold seeds from its own transcript's folder. Returns the sessions
    (transcripts) read."""
    used: set[str] = set()
    for path in files:
        inv = tool_inventory(path, max_bytes=HISTORY_BYTES)
        used |= inv.used - inv.unknown
    _add_names(doc, "used", used)
    if files:
        doc["seeded"] = True
    return len(files)


def _history_dirs(writer: DenyWriter, project_dir: Path, env: Mapping[str, str]) -> list[Path]:
    return writer.history_dirs(project_dir, {**os.environ, **env})


# --------------------------------------------------------------------------- store
def store_path(home: Path, project_key: str) -> Path:
    return paths.project_dir_for(home, project_key) / PRUNE_FILE


def registry_path(home: Path) -> Path:
    """User-level record of the deny rules we own, per settings-file real path (``files``),
    independent of the project key: a changed git remote never orphans a rule."""
    return home / REGISTRY_FILE


def _reg_files(reg: dict[str, Any]) -> dict[str, Any]:
    raw = reg.get("files")
    if not isinstance(raw, dict):
        raw = reg["files"] = {}
    return raw


def attach(doc: dict[str, Any], reg: dict[str, Any], key: str, fkey: str | None) -> None:
    """Point ``doc["files"]`` at the registry records of project ``key`` (shared dicts, so
    changes reach ``reg``). Records still inside ``doc`` (older layout) move to ``reg``; the
    record of this checkout's settings file (``fkey``) is included whatever key it was written
    under (a changed git remote): hooks, notices, ``restore`` and ``disable`` see it."""
    files = _reg_files(reg)
    legacy = doc.get("files")
    if isinstance(legacy, dict):
        for fk, entry in legacy.items():
            if isinstance(fk, str) and isinstance(entry, dict) and fk not in files:
                files[fk] = {**entry, "key": key}
    view: dict[str, Any] = {}
    for fk, entry in files.items():
        if not isinstance(entry, dict):
            continue
        if fk == fkey or entry.get("key") == key:
            view[fk] = entry
    doc["files"] = view


@dataclass
class _Store:
    path: Path
    reg_path: Path
    key: str
    doc: dict[str, Any]
    reg: dict[str, Any]

    def save(self) -> None:
        """Registry first (ownership and the journal), then the evidence store."""
        files = _reg_files(self.reg)
        for fk, entry in _files(self.doc).items():
            if isinstance(entry, dict):
                entry.setdefault("key", self.key)
                files[fk] = entry
        self.reg["version"] = 1
        jsonstore.write_locked(self.reg_path, self.reg)
        jsonstore.write_locked(self.path, {k: v for k, v in self.doc.items() if k != "files"})


@contextlib.contextmanager
def _open(
    home: Path,
    project_key: str,
    fkey: str | None,
    env: Mapping[str, str],
    timeout: float | None,
) -> Iterator[_Store]:
    """Both stores under their locks (project store first, then the registry)."""
    path, reg_path = store_path(home, project_key), registry_path(home)
    with jsonstore.locked(path, timeout), jsonstore.locked(reg_path, timeout):
        doc = _fresh(jsonstore.read_locked(path, {}, env))
        reg = jsonstore.read_locked(reg_path, {}, env)
        attach(doc, reg, project_key, fkey)
        yield _Store(path, reg_path, project_key, doc, reg)


def _read(home: Path, project_key: str, fkey: str | None, env: Mapping[str, str]) -> dict[str, Any]:
    """Lock-free view: the evidence store with this project's records attached."""
    path, reg_path = store_path(home, project_key), registry_path(home)
    doc = jsonstore.read(path, {}, env) if path.is_file() else {}
    reg = jsonstore.read(reg_path, {}, env) if reg_path.is_file() else {}
    # a store written by a newer token-sparen is not this code's layout: read as empty (no-op)
    if schema.newer_version(path.name, doc) is not None:
        doc = {}
    if schema.newer_version(reg_path.name, reg) is not None:
        reg = {}
    attach(doc, reg, project_key, fkey)
    return doc


def check_current(home: Path, project_key: str, env: Mapping[str, str]) -> None:
    """Raise ``jsonstore.NewerSchema`` when the project store or the registry was written by a
    newer token-sparen (lock-free: a command that only reads can then tell the user)."""
    for path in (store_path(home, project_key), registry_path(home)):
        if path.is_file():
            jsonstore.refuse_newer(path, jsonstore.read(path, {}, env), env)


def _registered(home: Path, fkey: str, env: Mapping[str, str]) -> bool:
    reg_path = registry_path(home)
    if not reg_path.is_file():
        return False
    reg = jsonstore.read(reg_path, {}, env)
    if schema.newer_version(reg_path.name, reg) is not None:
        return False
    entry = _reg_files(reg).get(fkey)
    return isinstance(entry, dict) and bool(
        _strs(entry.get("owned")) or _strs(entry.get("pending_add")) or entry.get("notices")
    )


def _fresh(doc: dict[str, Any]) -> dict[str, Any]:
    if not doc:
        doc.update({"version": VERSION, "enabled": False, "tools": {}})
    doc["version"] = VERSION
    doc.setdefault("kept", {})
    doc.setdefault("pending", False)
    return doc


def _notice(entry: dict[str, Any], text: str) -> None:
    """A user notice for one checkout (its settings-file record): shown there only."""
    notes = _strs(entry.get("notices"))
    if text not in notes:
        entry["notices"] = [*notes, text][-MAX_NOTICES:]


def reconcile(doc: dict[str, Any], writer: DenyWriter | None) -> bool:
    """Settle a journal (``pending_add``) a killed apply left: a journaled rule present in its
    settings file is ours, an absent one is dropped; never the user's. A file that cannot be
    read keeps its journal (decided at a later read). Returns whether ``doc`` changed."""
    if writer is None:
        return False
    changed = False
    for entry in _files(doc).values():
        if not isinstance(entry, dict) or not _strs(entry.get("pending_add")):
            continue
        if not isinstance(entry.get("dir"), str):
            continue
        current = writer.present(Path(entry["dir"]))
        if current is None:
            continue
        mine = _strs(entry.get("owned"))
        new = [r for r in _strs(entry.get("pending_add")) if r in current and r not in mine]
        entry["owned"] = [*mine, *new]
        lease = _lease(entry)
        for rule in new:
            lease[rule] = 0
        if new:  # the containers that write created, as journaled just before it
            raw = entry.get("pending_created")
            journaled = {k: v is True for k, v in raw.items()} if isinstance(raw, dict) else {}
            old = _created(entry)
            entry["created"] = {
                k: old.get(k, False) or journaled.get(k, False) for k in {*old, *journaled}
            }
        entry.pop("pending_add", None)
        entry.pop("pending_created", None)
        changed = True
    return changed


def _lease(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("lease")
    if not isinstance(raw, dict):
        raw = entry["lease"] = {}
    return raw


def _idle(home: Path, project_key: str, s: Settings, session_id: str) -> bool:
    """No other session of the project (not ended, not ``session_id``, the one being folded)
    was active in the last ``idle_minutes``: a changed tool list breaks no live prompt cache."""
    return not tracking.others_active(home, project_key, session_id, s.idle_s)


def _restore_file(
    doc: dict[str, Any],
    fkey: str,
    rules: Sequence[str] | None,
    reason: str | None,
    writer: DenyWriter,
    env: Mapping[str, str],
) -> list[str]:
    """Remove our ``rules`` (None: all) from one recorded file; with a ``reason`` each one is a
    regret (1.0) and kept for good."""
    entry = _files(doc).get(fkey)
    if not isinstance(entry, dict) or not isinstance(entry.get("dir"), str):
        return []
    mine = _strs(entry.get("owned"))
    want = mine if rules is None else [r for r in mine if r in set(rules)]
    if not want:
        return []
    cleanup = set(want) == set(mine)
    res = writer.remove(Path(entry["dir"]), want, _created(entry), cleanup)
    if not res.ok:
        log_error(env, "prune", f"restore: {res.message}")
        return []
    entry["owned"] = [r for r in mine if r not in want]
    lease = _lease(entry)
    for rule in want:
        lease.pop(rule, None)
    if cleanup:
        entry["created"] = {}
    if reason is not None:
        for rule in want:
            _regret(doc, rule, reason)
    return want


def _regret(doc: dict[str, Any], rule: str, reason: str) -> None:
    entry = _entry(doc, rule)
    entry["r"] = _observe(_beta(entry), 1.0, 1).to_list()
    doc["kept"] = {**_kept(doc), rule: reason}


def _revert_policy(
    doc: dict[str, Any],
    key: str,
    writer: DenyWriter,
    env: Mapping[str, str],
    here: str | None = None,
) -> list[str]:
    """Pruning is off for project ``key``: remove the rules recorded under it, and (``here``,
    at fold) those of this checkout's record written under another key (a changed git remote:
    the opt-in is never carried across keys, so its rules are restored, with a notice)."""
    done: set[str] = set()
    for fkey, entry in list(_files(doc).items()):
        if not isinstance(entry, dict):
            continue
        other = entry.get("key", key) != key
        if other and fkey != here:
            continue
        removed = _restore_file(doc, fkey, None, None, writer, env)
        done.update(removed)
        if other and removed:
            _notice(
                entry,
                f"token-sparen restored {', '.join(removed)}: tool pruning is not enabled for "
                "this project under its current identity (its git remote changed); "
                f"`{CLI} prune enable` opts it in again.",
            )
    return sorted(done)


def _foreign_key_record(doc: Mapping[str, Any], key: str, fkey: str) -> bool:
    entry = _files(doc).get(fkey)
    return (
        isinstance(entry, dict)
        and entry.get("key", key) != key
        and bool(_strs(entry.get("owned")) or _strs(entry.get("pending_add")))
    )


def _owned_under(doc: Mapping[str, Any], key: str) -> bool:
    return any(
        isinstance(e, dict) and e.get("key", key) == key and _strs(e.get("owned"))
        for e in _files(doc).values()
    )


def _restore_all(
    doc: dict[str, Any], reason: str | None, writer: DenyWriter, env: Mapping[str, str]
) -> list[str]:
    done: set[str] = set()
    for fkey in list(_files(doc)):
        done.update(_restore_file(doc, fkey, None, reason, writer, env))
    return sorted(done)


def _apply(
    st: _Store,
    project_dir: Path,
    rules: Sequence[str],
    exposure: str | None,
    writer: DenyWriter,
    env: Mapping[str, str],
) -> list[str]:
    """Write ``rules`` (``st`` holds both locks; ``exposure`` was checked before they were
    taken): journal, write, record."""
    doc = st.doc
    doc["pending"] = False
    target = writer.settings_file(project_dir)
    entry = _file(doc, file_key(target), project_dir)
    if exposure is not None:
        _notice(entry, _exposure_notice(exposure, target))
        return []
    mine = _strs(entry.get("owned"))
    present = writer.present(project_dir) or set()
    already = [r for r in rules if r in present]  # the user's own rules: never journaled
    todo = [r for r in rules if r not in present]
    if already:
        entry["foreign"] = sorted(set(_strs(entry.get("foreign"))) | set(already))
    if not todo:
        return []

    def journal(new: list[str], created: dict[str, bool]) -> None:
        entry["pending_add"] = list(new)
        entry["pending_created"] = dict(created)
        st.save()  # the intent is on disk before the settings write

    res = writer.add(project_dir, todo, fresh_backup=not mine, journal=journal)
    entry.pop("pending_add", None)
    entry.pop("pending_created", None)
    if not res.ok:
        log_error(env, "prune", f"apply: {res.message}")
        if entry.get("refused") != res.message:
            _notice(entry, f"token-sparen: tool pruning could not write {target}: {res.message}")
        entry["refused"] = res.message
        return []
    entry.pop("refused", None)
    entry["foreign"] = sorted(set(_strs(entry.get("foreign"))) | set(res.skipped))
    entry["owned"] = [*mine, *(r for r in res.changed if r not in mine)]
    lease = _lease(entry)
    tools = _tools(doc)
    for rule in res.changed:
        lease[rule] = 0
        row = tools.get(rule)
        if isinstance(row, dict):
            row.pop("fresh", None)
    old = _created(entry)
    entry["created"] = {
        k: old.get(k, False) or res.created.get(k, False) for k in {*old, *res.created}
    }
    if res.changed:
        _notice(
            entry,
            f"token-sparen pruned unused tools: {', '.join(res.changed)} (deny rules in "
            f"{target}); `{CLI} prune restore <tool>` or `{CLI} prune disable` undoes it.",
        )
    return res.changed


def _exposure_notice(exposure: str, target: Path) -> str:
    if exposure == "tracked":
        return (
            f"token-sparen: {target} is tracked by git, so tool pruning did not write it "
            f"(its rules would be committed); untrack it or run `{CLI} prune disable`."
        )
    if exposure == "unignored":
        return (
            f"token-sparen: {target} is not covered by .gitignore, so tool pruning did not "
            "write it (its rules could be committed); add it to .gitignore to allow it."
        )
    if exposure == "backup":
        return (
            f"token-sparen: git does not ignore .token-sparen/ next to {target}, where tool "
            "pruning keeps the settings backup, so it did not write it (the backup could be "
            "committed); add .token-sparen/ to .gitignore to allow it."
        )
    return (
        f"token-sparen: git could not confirm that {target} is untracked and ignored (not a "
        "git work tree, or git missing, failing or too slow), so tool pruning did not write it."
    )


def _end_leases(
    doc: dict[str, Any],
    fkey: str,
    s: Settings,
    writer: DenyWriter,
    env: Mapping[str, str],
) -> None:
    """One more folded session for each rule pruned in this checkout; a lease that reached
    ``s.lease`` sessions is restored (no regret) and needs fresh evidence to be pruned again."""
    entry = _files(doc).get(fkey)
    if not isinstance(entry, dict):
        return
    lease = _lease(entry)
    due: list[str] = []
    for rule in _strs(entry.get("owned")):
        lease[rule] = _int(lease.get(rule)) + 1
        if lease[rule] >= s.lease:
            due.append(rule)
    done = _restore_file(doc, fkey, due, None, writer, env)
    for rule in done:
        _entry(doc, rule)["fresh"] = {"s": 0, "p": 0}
    if done:
        _notice(
            entry,
            f"token-sparen: the lease on pruned {', '.join(done)} ended after {s.lease} "
            "sessions; restored so its use is observed again (pruned again only on fresh "
            "evidence).",
        )


def _check_owned(doc: dict[str, Any], project_dir: Path, fkey: str, writer: DenyWriter) -> None:
    """A rule of ours the user deleted is a restore (regret, kept); we no longer own it."""
    mine = owned_in(doc, fkey)
    if not mine:
        return
    current = writer.present(project_dir)
    if current is None:
        return
    gone = [r for r in mine if r not in current]
    if gone:
        entry = _file(doc, fkey, project_dir)
        entry["owned"] = [r for r in mine if r in current]
        lease = _lease(entry)
        for rule in gone:
            lease.pop(rule, None)
        for rule in gone:
            _regret(doc, rule, "removed by the user")


def _applied(doc: Mapping[str, Any]) -> list[str]:
    return _strs(doc.get("applied"))


def fold_session(
    fold_id: str,
    staged: Mapping[str, Any],
    home: Path,
    project_key: str,
    *,
    params: Mapping[str, Any] | None,
    env: Mapping[str, str],
    timeout: float | None = None,
    transcript_key: str = "transcript",
    project_dir_key: str = "project_dir",
    apply: bool = False,
    session_id: str = "",
) -> list[str]:
    """Fold step: history seeding (once), evidence from the session transcript, restores for
    attempted uses, lease ends, then the idle-gated apply for this checkout. Returns the rules
    added. Only the session-end fold (``apply``, folding ``session_id``) writes new rules or ends
    leases; any other fold leaves them ``pending``. git is asked before the locks are taken.
    Raises only ``jsonstore.StoreBusy``."""
    s = settings_for(params, env)
    path = store_path(home, project_key)
    raw_dir = staged.get(project_dir_key)
    writer = WRITER
    if not isinstance(raw_dir, str) or not raw_dir or writer is None:
        return []
    project_dir = Path(raw_dir)
    fkey = file_key(writer.settings_file(project_dir))
    if not path.is_file() and not s.enabled and not _registered(home, fkey, env):
        return []  # never opted in here: no work, nothing written
    view = _read(home, project_key, fkey, env)
    if not active(view, s):
        if (
            _owned_under(view, project_key)
            or _foreign_key_record(view, project_key, fkey)
            or _strs(_files(view).get(fkey, {}).get("pending_add"))
        ):
            with _open(home, project_key, fkey, env, timeout) as st:
                reconcile(st.doc, writer)
                _revert_policy(st.doc, project_key, writer, env, here=fkey)
                st.save()
        return []
    raw_tr = staged.get(transcript_key)
    transcript = Path(raw_tr) if isinstance(raw_tr, str) and raw_tr else None
    inv = scan(transcript, env) if transcript is not None else None
    # git may be slow: never while holding the locks a hook restore waits 0.1 s for
    exposure = writer.exposure(project_dir) if apply else None
    with _open(home, project_key, fkey, env, timeout) as st:
        doc = st.doc
        if fold_id in _applied(doc):
            if reconcile(doc, writer):
                st.save()
            return []
        reconcile(doc, writer)
        own = _files(doc).get(fkey)
        if isinstance(own, dict):
            own["key"] = project_key  # active here too (user-wide opt-in): adopted, lease goes on
        if transcript is not None and doc.get("seeded") is not True:
            dirs = [*_history_dirs(writer, project_dir, env), transcript.parent]
            seed(doc, history_files(list(dict.fromkeys(dirs))))
        _check_owned(doc, project_dir, fkey, writer)
        if inv is not None:
            attempted = observe_session(doc, inv, s, fkey)
            _restore_file(doc, fkey, attempted, "attempted use", writer, env)
        stale = [r for r in owned_in(doc, fkey) if not is_candidate(r, doc, s)]
        _restore_file(doc, fkey, stale, None, writer, env)  # e.g. `builtins` switched off
        if inv is not None and apply:
            _end_leases(doc, fkey, s, writer, env)
        added: list[str] = []
        todo = plan(doc, s, fkey)
        if todo:
            if apply and _idle(home, project_key, s, session_id):
                added = _apply(st, project_dir, todo, exposure, writer, env)
            else:
                doc["pending"] = True
        doc["applied"] = [*_applied(doc), fold_id][-APPLIED_MAX:]
        st.save()
    return added


# --------------------------------------------------------------------------- CLI helpers
def enable(home: Path, project_key: str, project_dir: Path, env: Mapping[str, str]) -> int | None:
    """Record the project opt-in and seed ``used`` from earlier transcripts. Returns the
    earlier sessions read, or None when the project was seeded before."""
    writer = WRITER
    count: list[int | None] = [None]

    def fn(doc: dict[str, Any]) -> dict[str, Any]:
        doc = _fresh(doc)
        doc["enabled"] = True
        if writer is not None and doc.get("seeded") is not True:
            count[0] = seed(doc, history_files(_history_dirs(writer, project_dir, env)))
        return doc

    jsonstore.update(store_path(home, project_key), fn, {}, env)
    return count[0]


def _fkey_of(checkout: Path | None) -> str | None:
    writer = WRITER
    if checkout is None or writer is None:
        return None
    return file_key(writer.settings_file(checkout))


def _any_state(home: Path, project_key: str) -> bool:
    return store_path(home, project_key).is_file() or registry_path(home).is_file()


def disable(
    home: Path, project_key: str, env: Mapping[str, str], checkout: Path | None = None
) -> list[str]:
    """Opt out and remove our rules from every recorded checkout (no regret); ``checkout``'s
    record is included even when it was written under another project key."""
    writer = WRITER
    done: list[str] = []
    with _open(home, project_key, _fkey_of(checkout), env, None) as st:
        st.doc["enabled"] = False
        st.doc["pending"] = False
        if writer is not None:
            reconcile(st.doc, writer)
            done.extend(_restore_all(st.doc, None, writer, env))
        st.save()
    return done


def restore(
    home: Path,
    project_key: str,
    project_dir: Path | None,
    rules: Sequence[str] | None,
    env: Mapping[str, str],
    *,
    reason: str | None = "restored by the user",
    timeout: float | None = None,
    checkout: Path | None = None,
) -> list[str]:
    """Remove our ``rules`` now from ``project_dir``'s settings file, or with ``project_dir``
    None from every recorded checkout (``checkout``'s own record included); ``rules`` None: all
    of them. Returns the rules removed."""
    writer = WRITER
    if writer is None or not _any_state(home, project_key):
        return []
    here = _fkey_of(project_dir if project_dir is not None else checkout)
    with _open(home, project_key, here, env, timeout) as st:
        doc = st.doc
        reconciled = reconcile(doc, writer)
        if project_dir is None:
            done: list[str] = []
            for fkey in list(_files(doc)):
                done += _restore_file(doc, fkey, rules, reason, writer, env)
            done = sorted(set(done))
        else:
            done = _restore_file(doc, str(here), rules, reason, writer, env)
        if done or reconciled:
            st.save()
    return done


def restore_everywhere(
    home: Path,
    project_key: str,
    rules: Sequence[str],
    env: Mapping[str, str],
    *,
    reason: str | None = "restored by the user",
) -> dict[str, list[str]]:
    """Remove our ``rules`` from every recorded checkout that holds them: {settings file:
    rules removed there}."""
    writer = WRITER
    out: dict[str, list[str]] = {}
    if writer is None or not _any_state(home, project_key):
        return out
    with _open(home, project_key, None, env, None) as st:
        reconciled = reconcile(st.doc, writer)
        for fkey in list(_files(st.doc)):
            done = _restore_file(st.doc, fkey, rules, reason, writer, env)
            if done:
                out[fkey] = done
        if out or reconciled:
            st.save()
    return out


def holders(doc: Mapping[str, Any], tool: str) -> dict[str, list[str]]:
    """{settings file: our rules covering ``tool`` there} over every recorded checkout."""
    out: dict[str, list[str]] = {}
    for fkey in _files(doc):
        rules = tool_rule(tool, owned_in(doc, fkey))
        if rules:
            out[fkey] = rules
    return out


def read_doc(
    home: Path, project_key: str, env: Mapping[str, str], checkout: Path | None = None
) -> dict[str, Any]:
    """The store as of now with this project's records (and ``checkout``'s), a killed apply's
    journal reconciled in memory."""
    doc = _read(home, project_key, _fkey_of(checkout), env)
    reconcile(doc, WRITER)
    return doc


def pruned_count(
    home: Path, project_key: str, env: Mapping[str, str], settings_file: Path | None = None
) -> int:
    """Rules of ours in ``settings_file`` (None: in any recorded checkout)."""
    if not _any_state(home, project_key):
        return 0
    fkey = None if settings_file is None else file_key(settings_file)
    doc = _read(home, project_key, fkey, env)
    reconcile(doc, WRITER)
    if fkey is None:
        return len(all_owned(doc))
    return len(owned_in(doc, fkey))


def render(text: str, env: Mapping[str, str]) -> str:
    return text.replace(CLI, cli_command(env))


def cli_command(env: Mapping[str, str]) -> str:
    """The CLI as the user can run it: the plugin's own launcher when the plugin root is known
    (a plugin-only install has no ``token-sparen`` on PATH), else ``token-sparen``."""
    root = env.get("CLAUDE_PLUGIN_ROOT")
    return f'python3 -I "{root}/hooks/run_cli.py"' if root else "token-sparen"


def status_lines(
    doc: Mapping[str, Any], s: Settings, settings_file: Path | None, env: Mapping[str, str]
) -> list[str]:
    if not s.allow:
        head = "off: this project's token-sparen.toml sets [prune] allow = false"
    elif doc.get("enabled") is True:
        head = "enabled (project opt-in)"
    elif s.enabled:
        head = "enabled (your config.toml)"
    else:
        head = "disabled (opt in with `token-sparen prune enable`)"
    scope = "MCP servers and always-loaded built-ins" if s.builtins else "MCP servers"
    lines = [f"tool pruning: {head}; candidates: {scope}"]
    mine: list[str] = []
    if settings_file is not None:
        lines.append(f"settings file: {settings_file}")
        mine = owned_in(doc, file_key(settings_file))
    lines.append(f"pruned ({len(mine)}): {', '.join(mine)}" if mine else "pruned: none")
    others = [k for k in _files(doc) if settings_file is None or k != file_key(settings_file)]
    others = [k for k in others if owned_in(doc, k)]
    if others:
        lines.append(f"other checkouts with pruned tools: {len(others)} (`restore --all` reverts)")
    if doc.get("pending") is True:
        lines.append(f"pending: waits for {round(s.idle_s / 60)} idle minutes in this project")
    tools = doc.get("tools")
    rows = []
    if isinstance(tools, dict):
        for name, entry in sorted(tools.items()):
            if not isinstance(entry, dict) or name in mine or not is_candidate(name, doc, s):
                continue
            rows.append(
                f"  {name}: {_int(entry.get('s'))} session(s), {_int(entry.get('p'))} prompts, "
                f"regret ucb {ucb(entry, s):.3f}"
            )
    if rows:
        lines.append(
            f"candidates (need {s.min_sessions} sessions, {s.min_prompts} prompts, ucb <= {s.tau}):"
        )
        lines.extend(rows)
    kept = _kept(doc)
    if kept:
        lines.append(f"kept (never pruned again): {', '.join(sorted(kept))}")
    if settings_file is not None:
        entry = _files(doc).get(file_key(settings_file))
        for note in _strs(entry.get("notices")) if isinstance(entry, dict) else []:
            lines.append(f"notice: {render(note, env)}")
    lines.append(SAVINGS_NOTE)
    return lines


# --------------------------------------------------------------------------- hooks
def _hook_doc(event: Event, env: Mapping[str, str]) -> tuple[dict[str, Any], str] | None:
    """(doc, this checkout's file key) when this project has a prune store or the registry
    has records (also a record written under another project key for this checkout)."""
    home = state_home(env)
    writer = WRITER
    if writer is None or not _any_state(home, event.project_key):
        return None
    fkey = file_key(writer.settings_file(event.project_dir))
    doc = _read(home, event.project_key, fkey, env)
    reconcile(doc, writer)  # in memory; a restore settles it on disk under the locks
    return doc, fkey


def _restore_now(
    event: Event, rules: Sequence[str], reason: str, env: Mapping[str, str]
) -> list[str]:
    if not rules:
        return []
    try:
        return restore(
            state_home(env),
            event.project_key,
            event.project_dir,
            rules,
            env,
            reason=reason,
            timeout=HOOK_LOCK_S,
        )
    except (jsonstore.StoreBusy, OSError) as exc:
        log_error(env, "prune", f"restore: {type(exc).__name__}: {exc}")
        return []


def _restored_message(rules: Sequence[str], why: str, where: Path | None = None) -> str:
    """No claim that the host re-reads its deny rules mid-session (unverified)."""
    names = ", ".join(f"`{r}`" for r in rules)
    verb = "is" if len(rules) == 1 else "are"
    place = f" in {where}" if where is not None else ""
    return (
        f"token-sparen restored {', '.join(rules)} ({why}): {names} {verb} no longer denied"
        f"{place}; Claude Code may need a new session to show it again."
    )


def _where(event: Event) -> Path | None:
    writer = WRITER
    return None if writer is None else writer.settings_file(event.project_dir)


def on_tool(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """PreToolUse / PostToolUseFailure / PermissionDenied: a pruned tool was called."""
    found = _hook_doc(event, env)
    if found is None or event.tool is None:
        return Action()
    mine = owned_in(found[0], found[1])
    if not mine:
        return Action()
    rules = tool_rule(event.tool.name, mine)
    if event.kind == "post_tool_failure":
        error = str(event.extra.get("error") or event.output_text or "")
        for name in _NO_SUCH_TOOL.findall(error):
            rules += [r for r in tool_rule(name, mine) if r not in rules]
    done = _restore_now(event, rules, "attempted use", env)
    if not done:
        return Action()
    msg = _restored_message(done, "the agent tried to use it", _where(event))
    return Action(user_message=msg)


def on_tool_search(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """PostToolUse of ToolSearch: a query naming a pruned tool restores it."""
    if event.tool is None or event.tool.name != "ToolSearch":
        return Action()
    found = _hook_doc(event, env)
    if found is None:
        return Action()
    query = event.tool.input.get("query")
    hits = mentions(query if isinstance(query, str) else None, owned_in(found[0], found[1]))
    done = _restore_now(event, hits, "attempted use", env)
    if not done:
        return Action()
    msg = _restored_message(done, "the agent searched for it", _where(event))
    hint = " Run ToolSearch again; if it is not listed yet, it needs a new session."
    return Action(inject_context=msg + hint, user_message=msg)


def on_prompt(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """UserPromptSubmit: the user named a pruned tool."""
    found = _hook_doc(event, env)
    if found is None:
        return Action()
    hits = mentions(event.prompt, owned_in(found[0], found[1]))
    done = _restore_now(event, hits, "user mention", env)
    if not done:
        return Action()
    msg = _restored_message(done, "you named it", _where(event))
    return Action(inject_context=msg, user_message=msg)


def notice_line(rules: Sequence[str], env: Mapping[str, str] | None = None) -> str:
    """One line within ``NOTICE_TOKENS`` (names truncated, never the whole line dropped)."""
    cli = cli_command({} if env is None else env)
    tail = f"run `{cli} prune restore <tool>` if needed."
    full = f"Pruned (unused here): {', '.join(rules)} — {tail}"
    if estimate_tokens(full) <= NOTICE_TOKENS or len(rules) <= 1:
        return full
    for k in range(len(rules) - 1, 0, -1):
        line = (
            f"Pruned (unused here): {', '.join(rules[:k])}, … +{len(rules) - k} more — run "
            f"`{cli} prune status` to list them; {tail}"
        )
        if estimate_tokens(line) <= NOTICE_TOKENS:
            return line
    return line


def on_session_start(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """Never writes a deny rule (latency-capped hook; new rules come from the session-end fold
    only): a policy revert (a restore), then the one-line notice and any stored notices."""
    found = _hook_doc(event, env)
    if found is None:
        return Action()
    doc, fkey = found
    s = settings_for(cfg.data, env)
    writer = WRITER
    home = state_home(env)
    if writer is not None and not active(doc, s) and _owned_under(doc, event.project_key):
        try:
            with _open(home, event.project_key, fkey, env, HOOK_LOCK_S) as st:
                reconcile(st.doc, writer)
                _revert_policy(st.doc, event.project_key, writer, env)
                st.save()
                doc = st.doc
        except (jsonstore.StoreBusy, OSError) as exc:
            log_error(env, "prune", f"session start: {type(exc).__name__}: {exc}")
    entry = _files(doc).get(fkey)
    notes = [render(n, env) for n in _strs(entry.get("notices"))] if isinstance(entry, dict) else []
    if notes:
        with contextlib.suppress(jsonstore.StoreBusy, OSError):
            with _open(home, event.project_key, fkey, env, HOOK_LOCK_S) as st:
                own = _files(st.doc).get(fkey)
                if isinstance(own, dict):
                    own.pop("notices", None)
                st.save()
    mine = owned_in(doc, fkey)
    line = notice_line(mine, env) if mine else None
    context: str | None = None
    if line is not None and event.session_id != "unknown":
        gate = Gate(tracking.overlay_path(event, env), env)
        source = str(event.extra.get("source", "startup"))
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]
        nonce = f":{time.time_ns()}" if source in ("clear", "compact") else ""
        gate.add(GATE_KIND, line, f"prune:{digest}{nonce}")
        context = gate.render(max(NOTICE_TOKENS, estimate_tokens(line)))
    user = "\n".join([*notes, *([line] if line else [])]) or None
    return Action(inject_context=context, user_message=user)
