"""Opt-in learned tool pruning with in-session recovery (spec section 6.10, tier high).

A tool definition the agent never uses still costs tokens on every request. Per project, a tool
that was **available and never used** across enough evidence is removed from the model's context
by a host deny rule (Claude Code: a bare ``permissions.deny`` entry such as ``"WebFetch"`` or
``"mcp__server"`` in ``<project>/.claude/settings.local.json``, written by the host adapter's
``DenyWriter``; this module is host-neutral).

**Opt-in.** Nothing happens (no evidence, no writes) unless ``token-sparen prune enable`` recorded
the project opt-in in ``projects/<key>/prune.json`` or the user's own config / env sets
``[prune] enabled = true``. A project's ``token-sparen.toml`` can never enable it; its only key is
``allow = false`` (off here, and our rules are reverted at the next fold or session start).

**Candidates and evidence** (fold time, one streaming read of the transcript and its subagent
transcripts): the session's tool list (``transcript.tool_inventory``: an SDK ``init`` record or
``deferred_tools_delta`` attachments; a session without one counts for the tools other sessions of
the project listed) gives the available tools. Built-in tools are candidates by name; MCP tools
only as a **whole server** (``mcp__<server>``) none of whose tools was ever used: with tool search
on, a single MCP tool costs little, a server's names and instructions more. Protected, never
pruned: ``PROTECTED``, every ``Task*`` tool, every tool (and server) ever used in the project, and
every tool restored once. Each session a candidate was available and unused in adds its prompts
as zero-regret observations to a per-candidate Beta over "pruning it would have caused a
restore" (prior Beta(1, 1)). A candidate is pruned only with ``>= min_sessions`` sessions
spanning ``>= min_prompts`` prompts **and** the high-tier gate ``ucb(ucb_q) <= tau`` (0.95,
0.01: about 300 unused prompts).

**When.** Rules change the tool list, which invalidates the host's prompt cache, so they are
written only when the project has been idle ``>= idle_minutes`` (no prompt or session start in
any session of the project): at fold, or else (``pending``) at the next session start after such
an idle gap, before its first request. Restores never wait: an agent that needs a tool gets it.

**Recovery.** SessionStart shows one line (to the model and the user) naming the pruned tools and
``token-sparen prune restore <tool>``. A direct use of a pruned tool (PreToolUse, PostToolUse
failure, PermissionDenied: defensive, a removed tool cannot normally be called), a ToolSearch
query naming it, a user prompt naming it, or (at fold) a transcript showing a call to it or the
host's "No such tool available" for it restores it at once: our rule is removed, regret 1.0 is
recorded and the tool is kept (never pruned again).

**Writes.** Only rules we wrote are ever removed (``owned``); a rule the user already had is
left alone and never owned; a rule the user removed counts as a restore. The host writer backs
the settings file up once, re-checks it before replacing, and refuses a file that is not a JSON
object or is tracked by git. Savings are not estimated: hooks cannot see definition sizes, so
only the number of pruned tools is reported.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from token_sparen.config import Config, flag, log_error, state_home
from token_sparen.emit import Gate
from token_sparen.events import Action, Event
from token_sparen.learn.beta import Beta
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonstore, paths
from token_sparen.transcript import ToolInventory, tool_inventory

PRUNE_FILE = "prune.json"
VERSION = 1
PRIOR = Beta(1.0, 1.0)
CAP = 1000.0  # posterior mass cap (mean-preserving rescale)
MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024
MAX_SUBAGENT_FILES = 50
MAX_NAMES = 5000
MAX_NOTICES = 5
APPLIED_MAX = 200
HOOK_LOCK_S = 0.1
MIN_SERVER_WORD = 4  # an MCP server's last name segment matches a prompt only from this length
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
RESTORE_HINT = "`token-sparen prune restore <tool>`"
GATE_KIND = "prune"
SAVINGS_NOTE = (
    "savings: token-sparen cannot measure tool definition sizes from hooks; "
    "it reports the number of pruned tools only"
)


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
        """``"tracked"`` (never write), ``"unignored"`` (warn) or None."""
        ...

    def add(self, project_dir: Path, rules: Sequence[str]) -> WriteResult: ...

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


WRITER: DenyWriter | None = None  # set by the host adapter (``adapters.claude.settings``)


def register_writer(writer: DenyWriter) -> None:
    global WRITER
    WRITER = writer


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    enabled: bool
    allow: bool
    min_sessions: int
    min_prompts: int
    idle_s: float
    tau: float
    ucb_q: float


def _real(raw: object, default: float, lo: float, hi: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw != raw:
        return default
    return min(hi, max(lo, float(raw)))


def settings(params: Mapping[str, Any] | None) -> Settings:
    raw = params.get("prune") if isinstance(params, Mapping) else None
    p: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Settings(
        enabled=flag(p.get("enabled"), False),
        allow=flag(p.get("allow"), True),
        min_sessions=int(_real(p.get("min_sessions"), 3, 1, 1e6)),
        min_prompts=int(_real(p.get("min_prompts"), 15, 1, 1e9)),
        idle_s=_real(p.get("idle_minutes"), 60.0, 0.0, 1e6) * 60.0,
        tau=_real(p.get("tau"), 0.01, 0.0, 0.5),
        ucb_q=_real(p.get("ucb_q"), 0.95, 0.51, 0.999),
    )


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


def owned(doc: Mapping[str, Any]) -> list[str]:
    return _strs(doc.get("owned"))


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


def is_candidate(name: str, doc: Mapping[str, Any]) -> bool:
    shaped = _is_server(name) or not name.startswith("mcp__")
    return bool(name) and shaped and not is_protected(name, doc)


def _rules_for_tool(name: str, rules: Iterable[str]) -> list[str]:
    server = server_of(name)
    return [r for r in rules if r == name or (server is not None and r == server)]


def _words(name: str) -> list[str]:
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", name)


def _patterns(rule: str) -> list[re.Pattern[str]]:
    if _is_server(rule):
        out = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(rule)}(?:__|(?![A-Za-z0-9_]))")]
        last = re.split(r"[_-]", rule[5:])[-1]
        if len(last) >= MIN_SERVER_WORD:
            out.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(last)}(?![A-Za-z0-9])", re.I))
        return out
    words = _words(rule) or [rule]
    joined = r"[\s_-]?".join(re.escape(w) for w in words)
    return [re.compile(rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])", re.I)]


def mentions(text: str | None, rules: Iterable[str]) -> list[str]:
    """The rules ``text`` names: the exact name, its words (``web search``), or for an MCP
    server its full name or its last name segment (>= 4 characters, ``gmail``)."""
    if not text:
        return []
    return [r for r in rules if any(p.search(text) for p in _patterns(r))]


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


def ready(entry: Mapping[str, Any], s: Settings) -> bool:
    return (
        _int(entry.get("s")) >= s.min_sessions
        and _int(entry.get("p")) >= s.min_prompts
        and ucb(entry, s) <= s.tau
    )


def plan(doc: Mapping[str, Any], s: Settings) -> list[str]:
    """Candidates that pass every gate and are not pruned (or the user's rule) yet."""
    tools = doc.get("tools")
    if not isinstance(tools, dict):
        return []
    skip = set(owned(doc)) | set(_strs(doc.get("foreign")))
    return sorted(
        name
        for name, entry in tools.items()
        if isinstance(entry, dict)
        and name not in skip
        and is_candidate(name, doc)
        and ready(entry, s)
    )


def _add_names(doc: dict[str, Any], key: str, names: Iterable[str]) -> None:
    merged = set(_strs(doc.get(key))) | {n for n in names if n}
    doc[key] = sorted(merged)[:MAX_NAMES]


def observe_session(doc: dict[str, Any], inv: ToolInventory) -> list[str]:
    """Fold one session's inventory into ``doc``; returns the pruned rules it tried to use."""
    pruned = owned(doc)
    attempted = sorted(
        {r for name in (*inv.used, *inv.unknown) for r in _rules_for_tool(name, pruned)}
    )
    if inv.listed is not None:
        _add_names(doc, "known", inv.listed)
    _add_names(doc, "used", inv.used - inv.unknown)
    available = inv.listed if inv.listed is not None else set(_strs(doc.get("known")))
    tools = _tools(doc)
    for name in list(tools):
        if is_protected(name, doc) and name not in _kept(doc):
            del tools[name]  # used now: protected for good
    if inv.prompts <= 0:
        return attempted
    for rule in candidate_names(available) | set(pruned):
        if not is_candidate(rule, doc) or rule in attempted:
            continue
        entry = _entry(doc, rule)
        entry["s"] = _int(entry.get("s")) + 1
        entry["p"] = _int(entry.get("p")) + inv.prompts
        entry["r"] = _observe(_beta(entry), 0.0, inv.prompts).to_list()
    return attempted


def scan(transcript: Path, env: Mapping[str, str]) -> ToolInventory:
    """The main transcript plus its subagent transcripts (``<stem>/subagents/*.jsonl``)."""

    def cut() -> None:
        log_error(env, "prune", f"transcript over {MAX_TRANSCRIPT_BYTES} bytes, rest not read")

    inv = tool_inventory(transcript, max_bytes=MAX_TRANSCRIPT_BYTES, on_limit=cut)
    sub = transcript.with_suffix("") / "subagents"
    with contextlib.suppress(OSError):
        for i, path in enumerate(sorted(sub.glob("*.jsonl"))):
            if i >= MAX_SUBAGENT_FILES:
                break
            inv.merge(tool_inventory(path, max_bytes=MAX_TRANSCRIPT_BYTES, on_limit=cut))
    return inv


# --------------------------------------------------------------------------- store
def store_path(home: Path, project_key: str) -> Path:
    return paths.project_dir_for(home, project_key) / PRUNE_FILE


def _fresh(doc: dict[str, Any]) -> dict[str, Any]:
    if not doc:
        doc.update({"version": VERSION, "enabled": False, "tools": {}, "owned": []})
    doc.setdefault("owned", [])
    doc.setdefault("kept", {})
    doc.setdefault("pending", False)
    return doc


def _notice(doc: dict[str, Any], text: str) -> None:
    notes = _strs(doc.get("notices"))
    if text not in notes:
        doc["notices"] = [*notes, text][-MAX_NOTICES:]


def _idle(home: Path, project_key: str, s: Settings) -> bool:
    _, fresh = tracking.last_active(home, project_key, s.idle_s)
    return not fresh


def _created(doc: Mapping[str, Any]) -> dict[str, bool]:
    raw = doc.get("created")
    return {k: v is True for k, v in raw.items()} if isinstance(raw, dict) else {}


def _restore(
    doc: dict[str, Any],
    project_dir: Path,
    rules: Sequence[str],
    reason: str | None,
    writer: DenyWriter,
    env: Mapping[str, str],
) -> list[str]:
    """Remove our ``rules`` from the settings file; with a ``reason`` each one is a regret
    (1.0) and kept for good. Rules the user already removed count as restored too."""
    mine = owned(doc)
    rules = [r for r in mine if r in set(rules)]
    if not rules:
        return []
    cleanup = set(rules) == set(mine)
    created = _created(doc)
    res = writer.remove(project_dir, rules, created, cleanup)
    if not res.ok:
        log_error(env, "prune", f"restore: {res.message}")
        return []
    doc["owned"] = [r for r in mine if r not in rules]
    if cleanup:
        doc["created"] = {}
    for rule in rules:
        if reason is not None:
            entry = _entry(doc, rule)
            entry["r"] = _observe(_beta(entry), 1.0, 1).to_list()
            doc["kept"] = {**_kept(doc), rule: reason}
    return rules


def _apply(
    doc: dict[str, Any],
    project_dir: Path,
    rules: Sequence[str],
    writer: DenyWriter,
    env: Mapping[str, str],
) -> list[str]:
    doc["pending"] = False
    exposure = writer.exposure(project_dir)
    target = writer.settings_file(project_dir)
    if exposure == "tracked":
        _notice(
            doc,
            f"token-sparen: {target} is tracked by git, so tool pruning did not write it "
            "(our rules would be committed); untrack it or `token-sparen prune disable`.",
        )
        return []
    res = writer.add(project_dir, rules)
    if not res.ok:
        log_error(env, "prune", f"apply: {res.message}")
        return []
    doc["foreign"] = sorted(set(_strs(doc.get("foreign"))) | set(res.skipped))
    doc["owned"] = [*owned(doc), *(r for r in res.changed if r not in owned(doc))]
    old = _created(doc)
    doc["created"] = {k: bool(old.get(k)) or bool(res.created.get(k)) for k in {*old, *res.created}}
    doc["settings_file"] = str(target)
    if res.changed:
        _notice(
            doc,
            f"token-sparen pruned unused tools: {', '.join(res.changed)} (deny rules in "
            f"{target}); {RESTORE_HINT} or `token-sparen prune disable` undoes it.",
        )
    if exposure == "unignored":
        _notice(
            doc,
            f"token-sparen: {target} is not covered by .gitignore; add it there so the deny "
            "rules token-sparen writes are never committed.",
        )
    return res.changed


def _check_owned(
    doc: dict[str, Any], project_dir: Path, writer: DenyWriter, env: Mapping[str, str]
) -> None:
    """A rule of ours the user deleted is a restore (regret, kept); we no longer own it."""
    mine = owned(doc)
    if not mine:
        return
    current = writer.present(project_dir)
    if current is None:
        return
    gone = [r for r in mine if r not in current]
    if gone:
        doc["owned"] = [r for r in mine if r in current]
        for rule in gone:
            entry = _entry(doc, rule)
            entry["r"] = _observe(_beta(entry), 1.0, 1).to_list()
            doc["kept"] = {**_kept(doc), rule: "removed by the user"}


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
) -> list[str]:
    """Fold step: evidence from the session transcript, restores for attempted uses, then the
    idle-gated apply. Returns the rules added. Raises only ``jsonstore.StoreBusy``."""
    s = settings(params)
    path = store_path(home, project_key)
    raw_dir = staged.get(project_dir_key)
    writer = WRITER
    if not path.is_file() and not s.enabled:
        return []  # never opted in here: no work, nothing written
    if not isinstance(raw_dir, str) or not raw_dir or writer is None:
        return []
    project_dir = Path(raw_dir)
    doc = jsonstore.read(path, {}, env)
    if not active(doc, s):
        if owned(doc):
            with jsonstore.locked(path, timeout):
                doc = _fresh(jsonstore.read_locked(path, {}, env))
                _restore(doc, project_dir, owned(doc), None, writer, env)
                jsonstore.write_locked(path, doc)
        return []
    raw_tr = staged.get(transcript_key)
    inv = scan(Path(raw_tr), env) if isinstance(raw_tr, str) and raw_tr else None
    with jsonstore.locked(path, timeout):
        doc = _fresh(jsonstore.read_locked(path, {}, env))
        if fold_id in _applied(doc):
            return []
        _check_owned(doc, project_dir, writer, env)
        if inv is not None:
            attempted = observe_session(doc, inv)
            _restore(doc, project_dir, attempted, "attempted use", writer, env)
        added: list[str] = []
        todo = plan(doc, s)
        if todo:
            if _idle(home, project_key, s):
                added = _apply(doc, project_dir, todo, writer, env)
            else:
                doc["pending"] = True
        doc["applied"] = [*_applied(doc), fold_id][-APPLIED_MAX:]
        jsonstore.write_locked(path, doc)
    return added


# --------------------------------------------------------------------------- CLI helpers
def set_enabled(home: Path, project_key: str, on: bool, env: Mapping[str, str]) -> None:
    def fn(doc: dict[str, Any]) -> dict[str, Any]:
        doc = _fresh(doc)
        doc["enabled"] = on
        if not on:
            doc["pending"] = False
        return doc

    jsonstore.update(store_path(home, project_key), fn, {}, env)


def restore(
    home: Path,
    project_key: str,
    project_dir: Path,
    rules: Sequence[str] | None,
    env: Mapping[str, str],
    *,
    reason: str | None = "restored by the user",
    timeout: float | None = None,
) -> list[str]:
    """Remove our rules (``None``: all of them) now; returns the rules removed."""
    path = store_path(home, project_key)
    writer = WRITER
    if writer is None or not path.is_file():
        return []
    with jsonstore.locked(path, timeout):
        doc = _fresh(jsonstore.read_locked(path, {}, env))
        mine = owned(doc)
        want = mine if rules is None else [r for r in mine if r in set(rules)]
        done = _restore(doc, project_dir, want, reason, writer, env)
        if done:
            jsonstore.write_locked(path, doc)
    return done


def read_doc(home: Path, project_key: str, env: Mapping[str, str]) -> dict[str, Any]:
    return jsonstore.read(store_path(home, project_key), {}, env)


def pruned_count(home: Path, project_key: str, env: Mapping[str, str]) -> int:
    path = store_path(home, project_key)
    return len(owned(jsonstore.read(path, {}, env))) if path.is_file() else 0


def status_lines(doc: Mapping[str, Any], s: Settings, settings_file: Path | None) -> list[str]:
    if not s.allow:
        head = "off: this project's token-sparen.toml sets [prune] allow = false"
    elif doc.get("enabled") is True:
        head = "enabled (project opt-in)"
    elif s.enabled:
        head = "enabled (user config)"
    else:
        head = "disabled (opt in with `token-sparen prune enable`)"
    lines = [f"tool pruning: {head}"]
    if settings_file is not None:
        lines.append(f"settings file: {settings_file}")
    mine = owned(doc)
    lines.append(f"pruned ({len(mine)}): {', '.join(mine)}" if mine else "pruned: none")
    if doc.get("pending") is True:
        lines.append(f"pending: waits for {round(s.idle_s / 60)} idle minutes in this project")
    tools = doc.get("tools")
    rows = []
    if isinstance(tools, dict):
        for name, entry in sorted(tools.items()):
            if not isinstance(entry, dict) or name in mine or not is_candidate(name, doc):
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
    for note in _strs(doc.get("notices")):
        lines.append(f"notice: {note}")
    lines.append(SAVINGS_NOTE)
    return lines


# --------------------------------------------------------------------------- hooks
def _hook_doc(event: Event, env: Mapping[str, str]) -> tuple[Path, dict[str, Any]] | None:
    """(store path, doc) when this project has rules of ours or pending work, else None."""
    path = store_path(state_home(env), event.project_key)
    if not path.is_file():
        return None
    return path, jsonstore.read(path, {}, env)


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


def _restored_message(rules: Sequence[str], why: str) -> str:
    return (
        f"token-sparen restored {', '.join(rules)} ({why}); it was pruned as unused here "
        "and is available again from the next request."
    )


def _tool_rules(event: Event, env: Mapping[str, str]) -> list[str]:
    found = _hook_doc(event, env)
    if found is None or event.tool is None:
        return []
    return _rules_for_tool(event.tool.name, owned(found[1]))


def on_tool(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """PreToolUse / PostToolUseFailure / PermissionDenied: a pruned tool was called."""
    rules = _tool_rules(event, env)
    found = _hook_doc(event, env) if event.kind == "post_tool_failure" else None
    if found is not None:
        error = str(event.extra.get("error") or event.output_text or "")
        for name in re.findall(r"No such tool available: ([A-Za-z0-9_.:-]+)", error):
            rules += [r for r in _rules_for_tool(name, owned(found[1])) if r not in rules]
    done = _restore_now(event, rules, "attempted use", env)
    if not done:
        return Action()
    return Action(user_message=_restored_message(done, "the agent tried to use it"))


def on_tool_search(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """PostToolUse of ToolSearch: a query naming a pruned tool restores it."""
    if event.tool is None or event.tool.name != "ToolSearch":
        return Action()
    found = _hook_doc(event, env)
    if found is None:
        return Action()
    query = event.tool.input.get("query")
    hits = mentions(query if isinstance(query, str) else None, owned(found[1]))
    done = _restore_now(event, hits, "attempted use", env)
    if not done:
        return Action()
    msg = _restored_message(done, "the agent searched for it")
    return Action(inject_context=f"{msg} Run ToolSearch again to load it.", user_message=msg)


def on_prompt(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """UserPromptSubmit: the user named a pruned tool."""
    found = _hook_doc(event, env)
    if found is None:
        return Action()
    done = _restore_now(event, mentions(event.prompt, owned(found[1])), "user mention", env)
    if not done:
        return Action()
    msg = _restored_message(done, "you named it")
    return Action(inject_context=msg, user_message=msg)


def notice_line(rules: Sequence[str]) -> str:
    return f"Pruned (unused here): {', '.join(rules)} — run {RESTORE_HINT} if needed."


def on_session_start(event: Event, cfg: Config, *, env: Mapping[str, str]) -> Action:
    """Deferred apply after an idle gap (before ``tracking`` records this session as active),
    a policy revert, then the one-line notice and any stored notices."""
    found = _hook_doc(event, env)
    s = settings(cfg.data)
    if found is None:
        return Action()
    path, doc = found
    writer = WRITER
    home = state_home(env)
    want_apply = active(doc, s) and doc.get("pending") is True
    want_revert = not active(doc, s) and bool(owned(doc))
    if writer is not None and (want_revert or (want_apply and _idle(home, event.project_key, s))):
        try:
            with jsonstore.locked(path, HOOK_LOCK_S):
                doc = _fresh(jsonstore.read_locked(path, {}, env))
                if want_revert:
                    _restore(doc, event.project_dir, owned(doc), None, writer, env)
                else:
                    todo = plan(doc, s)
                    if todo:
                        _apply(doc, event.project_dir, todo, writer, env)
                    doc["pending"] = False
                jsonstore.write_locked(path, doc)
        except (jsonstore.StoreBusy, OSError) as exc:
            log_error(env, "prune", f"session start: {type(exc).__name__}: {exc}")
    notes = _strs(doc.get("notices"))
    if notes:
        with contextlib.suppress(jsonstore.StoreBusy, OSError):
            jsonstore.update(path, _clear_notices, {}, env, timeout=HOOK_LOCK_S)
    mine = owned(doc)
    line = notice_line(mine) if mine else None
    context: str | None = None
    if line is not None and event.session_id != "unknown":
        gate = Gate(tracking.overlay_path(event, env), env)
        source = str(event.extra.get("source", "startup"))
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]
        nonce = f":{time.time_ns()}" if source in ("clear", "compact") else ""
        gate.add(GATE_KIND, line, f"prune:{digest}{nonce}")
        context = gate.render(200)
    user = "\n".join([*notes, *([line] if line else [])]) or None
    return Action(inject_context=context, user_message=user)


def _clear_notices(doc: dict[str, Any]) -> dict[str, Any]:
    doc.pop("notices", None)
    return doc
