"""Recoverable output compression on PostToolUse (decision point ``compression``).

Flow: cheap gates (tool deny-set, replaceable shape, size) -> detect content type -> quality
gate (``gated``: file contents and content types outside ``allow_ctypes`` are never offered to
the decision point, so nothing is logged or learned for them) -> ask the policy for an arm ->
compress -> keep only if it saves at least ``min_saving_ratio`` -> save the full text, append the
recovery marker, update the session ledger. Every decision the policy made
is logged with its propensity, including the ones that ended as a no-op.

Baseline is always the *preview*: the text the host would show the model (``output_text``). When
Claude Code persisted a longer Bash output, the full file is compressed first (it recovers lines
past the truncation) and the preview second; a candidate is used only if it is no longer than the
preview and saves at least ``min_saving_ratio`` of the preview's tokens. The hook therefore never
returns more than the model would have seen, and savings are booked against the preview.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from token_sparen import extensions, recovery
from token_sparen.compress import (
    CompressContext,
    Compressor,
    CompressResult,
    Level,
    detect,
    load_level,
)
from token_sparen.compress.filedump import is_file_dump, is_file_tool
from token_sparen.config import Config, flag, log_error, state_home
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event, SessionCtx, ToolCall
from token_sparen.learn import decisions, features, regret
from token_sparen.learn.policy import (
    COMPRESSION,
    PRETRIM,
    Decision,
    DecisionPoint,
    DecisionRecord,
    Policy,
)
from token_sparen.mechanisms import governor, guards, tracking
from token_sparen.store import jsonl, jsonstore, keycache, paths, session

MIN_TOKENS = 2000  # invariant floor: smaller outputs are never modified, whatever the config says
PERSISTED_MAX_BYTES = 5_000_000
SAVE_DEADLINE_MS = 250
COMMAND_SIG_CHARS = 500
DEFAULT_SKIP_TOOLS = (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "TodoWrite",
    "AskUserQuestion",
    "ExitPlanMode",
)
# Never compressed, whatever `skip_tools` says: interaction tools whose result the user answers.
HARD_SKIP_TOOLS = frozenset({"TodoWrite", "AskUserQuestion", "ExitPlanMode"})
# Quality first (owner decision 2026-11-26): the content types offered to the decision point by
# default. text, table and stacktrace are not (too little evidence that trimming them is
# harmless); file contents are never trimmed unless `trim_file_contents` is on.
DEFAULT_ALLOW_CTYPES = (
    "diff",
    "search",
    "test_report",
    "compiler",
    "build_log",
    "json",
    "pplx",
    "log",
)
GATE_FILE_CONTENT = "file_content"
GATE_CTYPE = "ctype_not_allowed"
_COMMAND_TOOLS = frozenset({"powershell", "pwsh"})  # shells outside the `shell` category
_EST_TYPE = {"json": "json", "pplx": "json", "log": "log", "build_log": "log"}
_POST_KINDS = frozenset({"post_tool", "post_tool_failure"})

Replaceable = Callable[[Event], bool]


def _num(cfg: Config, key: str, default: float) -> float:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def default_replaceable(event: Event) -> bool:
    """Host-neutral default: any tool result event. Hosts pass ``HostAdapter.replaceable``,
    which knows their result shapes (mechanisms never do)."""
    return event.tool is not None and event.kind in _POST_KINDS


FLOOR_FILE = "floor_warned.json"


def _min_tokens(cfg: Config, env: Mapping[str, str], project_key: str) -> float:
    """``compression.min_tokens``, never below the 2000-token invariant.

    A lower configured value is logged once per project (``projects/<key>/floor_warned.json``
    keeps the value warned about) and again only when the configured value changes.
    """
    configured = _num(cfg, "compression.min_tokens", MIN_TOKENS)
    if configured < MIN_TOKENS:
        path = paths.project_dir_for(state_home(env), project_key) / FLOOR_FILE
        if jsonstore.read(path, {}).get("min_tokens") != configured:
            warned = False

            def apply(state: dict[str, Any]) -> dict[str, Any]:
                nonlocal warned
                warned = state.get("min_tokens") != configured
                return {"min_tokens": configured}

            try:
                jsonstore.update(path, apply, {})
            except (jsonstore.StoreBusy, OSError) as exc:
                log_error(env, "compression", f"floor flag: {type(exc).__name__}: {exc}")
            if warned:
                log_error(
                    env,
                    "compression",
                    f"min_tokens = {configured:g} is below the floor; using {MIN_TOKENS}",
                )
    return max(float(MIN_TOKENS), configured)


def _skipped(event: Event, cfg: Config) -> bool:
    tool = event.tool
    if tool is None or tool.category == "edit":
        return True
    raw = cfg.get("compression.skip_tools")
    if isinstance(raw, str):  # env override: comma-separated names
        names = {x.strip() for x in raw.split(",")}
    elif isinstance(raw, list):
        names = {x for x in raw if isinstance(x, str)}
    else:
        names = set(DEFAULT_SKIP_TOOLS)
    return tool.name in names or tool.name in HARD_SKIP_TOOLS


def _persisted_text(event: Event, cfg: Config) -> str | None:
    """Bash: the full output Claude Code saved before truncating stdout, when readable.

    Whether that file also holds stderr is not documented, so stderr is never appended to it
    (avoids duplicating it); the preview still carries stderr as the host showed it.
    """
    resp = event.raw_response
    if event.tool is None or event.tool.name != "Bash" or not isinstance(resp, dict):
        return None
    where = resp.get("persistedOutputPath")
    if not isinstance(where, str) or not where:
        return None
    try:
        path = Path(where)
        if path.stat().st_size > _num(cfg, "compression.persisted_max_bytes", PERSISTED_MAX_BYTES):
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _command_sig(event: Event) -> str:
    tool = event.tool
    assert tool is not None
    cmd = tool.input.get("command")
    if tool.name == "Bash" and isinstance(cmd, str):
        return regret.event_signature(event)[:COMMAND_SIG_CHARS]
    return tool.name


def _log_no_op(event: Event, cfg: Config, env: Mapping[str, str], decision_id: str) -> None:
    """A trimming arm that replaced nothing: its regret is settled at 0 at once (an outcome
    line after its decision line)."""
    try:
        pdir = paths.project_dir_for(state_home(env), event.project_key)
    except ValueError as exc:
        log_error(env, "compression", f"decision log: {exc}")
        return
    decisions.append_outcomes(
        pdir, [decisions.outcome(decision_id, 0.0, "no_op")], cfg, env, "compression"
    )


def _log_decision(
    event: Event,
    cfg: Config,
    env: Mapping[str, str],
    decision: Decision,
    ctype: str,
    before: int,
    after: int,
    recovery_id: str | None,
    tokens_full: int | None,
) -> None:
    assert event.tool is not None
    rec = DecisionRecord(
        decision_id=decision.decision_id,
        point=decision.point,
        arm=decision.arm,
        propensity=decision.propensity,
        bucket=decision.bucket,
        tokens_before=before,
        tokens_after=after,
        recovery_id=recovery_id,
        tool_use_id=event.tool.use_id,
        command_sig=_command_sig(event),
        ts=time.time(),
        ctype=ctype,
        tool_name=event.tool.name,
        tokens_full=tokens_full,
        host=event.host,
    )
    limit = int(_num(cfg, "learn.decision_log_max_bytes", 20_000_000))
    path = paths.project_dir_for(state_home(env), event.project_key) / "decisions.jsonl"
    try:
        jsonl.append(path, asdict(rec), limit, env)
    except (jsonstore.StoreBusy, OSError) as exc:  # a lost log line must not undo the trim
        log_error(env, "compression", f"decision log: {type(exc).__name__}: {exc}")


def _count_trim(overlay: Path, removed: int, env: Mapping[str, str]) -> None:
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        for key, inc in (("trims", 1), ("tokens_removed", removed)):
            prior = state.get(key)
            base = prior if isinstance(prior, int) and not isinstance(prior, bool) else 0
            state[key] = base + inc
        return state

    try:
        session.update_session(overlay, apply)
    except (jsonstore.StoreBusy, OSError) as exc:  # statusline counters are best effort
        log_error(env, "compression", f"ledger: {type(exc).__name__}: {exc}")


def _marker_len_ref(project_dir: Path) -> recovery.RecoveryRef:
    """A stand-in ref whose marker has exactly the length of the real one."""
    rid = "0" * 12
    return recovery.RecoveryRef(rid, (project_dir / ".token-sparen" / "outputs" / f"{rid}.txt"))


@dataclass(frozen=True)
class Candidate:
    """A compression the never-larger rule accepts: ``result`` of ``source`` (the persisted full
    output or the preview); ``tokens_after`` counts the body plus a marker of the real length."""

    result: CompressResult
    source: str
    tokens_after: int


def est_type(ctype: str) -> str:
    """The token-estimation type of a content type."""
    return _EST_TYPE.get(ctype, "text")


def pick_candidate(
    full: str | None,
    preview: str,
    level: Level,
    compressor: Compressor,
    ctx: CompressContext,
    cfg: Config,
    est: str,
    before: int,
    project_dir: Path,
) -> Candidate | None:
    """Compress the full output first, then the preview; the first candidate that is lossy, no
    longer than the preview (marker included) and saves >= ``min_saving_ratio`` of ``before``
    wins. None when no candidate qualifies. Pure: nothing is saved or logged (the hook path and
    offline replay share it)."""
    max_after = before * (1 - _num(cfg, "compression.min_saving_ratio", 0.15))
    stand_in = _marker_len_ref(project_dir)
    for source in (full, preview):
        if source is None:
            continue
        result = compressor.compress(source, level, ctx)
        if result.lossless or result.kept_lines >= result.total_lines:
            continue
        probe = f"{result.text}\n{recovery.marker(result.kept_lines, result.total_lines, stand_in)}"
        after = estimate_tokens(probe, est)
        if len(probe) <= len(preview) and after <= max_after:
            return Candidate(result, source, after)
    return None


def min_tokens(cfg: Config) -> float:
    """``compression.min_tokens`` floored at the 2000-token invariant (no warning, no I/O)."""
    return max(float(MIN_TOKENS), _num(cfg, "compression.min_tokens", MIN_TOKENS))


def skipped(event: Event, cfg: Config) -> bool:
    """Never compressed: no tool, the edit category, ``skip_tools`` or the hard skip floor."""
    return _skipped(event, cfg)


def allowed_ctypes(cfg: Config) -> frozenset[str]:
    """``[compression] allow_ctypes`` (env form: comma-separated); the defaults when unset or
    not a list/string. Non-string entries are dropped."""
    raw = cfg.get("compression.allow_ctypes")
    if isinstance(raw, str):
        return frozenset(x.strip() for x in raw.split(",") if x.strip())
    if isinstance(raw, list):
        return frozenset(x for x in raw if isinstance(x, str))
    return frozenset(DEFAULT_ALLOW_CTYPES)


def trims_file_contents(cfg: Config) -> bool:
    """``[compression] trim_file_contents``: only an explicit on value opts in."""
    return flag(cfg.get("compression.trim_file_contents"), False)


def is_file_content(tool: ToolCall) -> bool:
    """A file's contents, best effort (``compress/filedump.py``): the read category (Read tool);
    a tool whose name says it reads a file (``filedump.is_file_tool``, MCP tools included); or a
    shell/PowerShell command any part of which prints a file (``filedump.is_file_dump``)."""
    if tool.category == "read" or is_file_tool(tool.name):
        return True
    cmd = tool.input.get("command")
    shell = tool.category == "shell" or tool.name.lower() in _COMMAND_TOOLS
    return shell and isinstance(cmd, str) and is_file_dump(cmd)


def gated(event: Event, ctype: str, cfg: Config) -> str | None:
    """Why an output is never offered to the compression decision point, else None. Pure (no
    I/O); the hook and offline replay (``--policy default``) share it. Reasons:
    ``file_content`` (unless ``trim_file_contents``), ``ctype_not_allowed`` (``allow_ctypes``)."""
    tool = event.tool
    if tool is None:
        return None
    if not trims_file_contents(cfg) and is_file_content(tool):
        return GATE_FILE_CONTENT
    if ctype not in allowed_ctypes(cfg):
        return GATE_CTYPE
    return None


def handle_post_tool(
    event: Event,
    policy: Policy,
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    replaceable: Replaceable | None = None,
    started: float | None = None,
) -> Action:
    """Compress a large tool output recoverably; any non-qualifying output is left untouched.

    ``started`` is the hook's ``time.monotonic()`` start; past ``save_deadline_ms`` no recovery
    file is written and nothing is replaced (the watchdog would discard the answer anyway).
    """
    t0 = time.monotonic() if started is None else started
    env = os.environ if env is None else env
    if event.tool is None or event.output_text is None or _skipped(event, cfg):
        return Action()
    if not (replaceable or default_replaceable)(event):
        return Action()  # checked before any work: nothing to save or log if we cannot replace
    if regret.is_recovery_access(event):
        return Action()  # the agent asked for the saved full output: never trim it again
    preview = event.output_text
    if estimate_tokens(preview) < _min_tokens(cfg, env, event.project_key):
        return Action()
    ctype = detect(preview, event.tool)
    if gated(event, ctype, cfg) is not None:
        return Action()  # quality gate: no decision, no log, no learning, no recovery file
    full = _persisted_text(event, cfg)
    if full == preview:
        full = None
    est = est_type(ctype)
    before = estimate_tokens(preview, est)
    tokens_full = None if full is None else estimate_tokens(full, est)
    overlay_file = tracking.overlay_path(event, env)
    overlay = session.read_session(overlay_file)
    sctx = governor.decision_ctx(event, overlay, overlay_file, cfg, env)
    level: Level | None = None
    decision: Decision | None = None
    point: DecisionPoint = COMPRESSION
    store = paths.project_dir_for(state_home(env), event.project_key)
    pretrim_k = guards.pretrim_lines(event, store, cfg, env=env)
    if pretrim_k is not None:  # learned noisy command: maybe a tail-biased level
        pre_features = features.for_point(PRETRIM, event, sctx, ctype, tokens=before)
        pre = policy.decide(PRETRIM, pre_features, sctx)
        if pre.arm == "bounded":
            decision, level, point = pre, guards.pretrim_level(cfg, pretrim_k), PRETRIM
        else:
            _log_decision(event, cfg, env, pre, ctype, before, before, None, tokens_full)
    if decision is None:
        feats = features.for_point(COMPRESSION, event, sctx, ctype, tokens=before)
        decision = policy.decide(COMPRESSION, feats, sctx)
    chosen_decision, chosen_point = decision, point

    def no_op() -> Action:
        """Nothing replaced. A trimming arm that ended as a no-op gets savings 0 and regret 0
        at once (nothing was removed, so nothing can be regretted); the safe arm nothing."""
        if chosen_decision.arm != chosen_point.safe_arm:
            fn = regret.reward_fn(policy)
            if fn is not None:
                fn(chosen_decision.decision_id, 0.0, 0.0)
        _log_decision(event, cfg, env, chosen_decision, ctype, before, before, None, tokens_full)
        if chosen_decision.arm != chosen_point.safe_arm:
            _log_no_op(event, cfg, env, chosen_decision.decision_id)
        return Action()

    if level is None:
        if decision.arm == COMPRESSION.safe_arm:
            return no_op()
        level = load_level(cfg, decision.arm)
    ctx = CompressContext(
        prompt=tracking.last_prompt(overlay),
        recent_paths=tuple(tracking.recent_paths(overlay)),
        config=cfg,
    )
    home = state_home(env)
    compressor = extensions.load_extensions(event.project_dir, home, cfg, env).compressor_for(ctype)
    picked = pick_candidate(
        full, preview, level, compressor, ctx, cfg, est, before, event.project_dir
    )
    if picked is None:  # a trimming arm that found nothing worth trimming
        return no_op()
    chosen, chosen_source = picked.result, picked.source
    deadline = _num(cfg, "compression.save_deadline_ms", SAVE_DEADLINE_MS) / 1000
    if time.monotonic() - t0 > deadline:
        log_error(env, "compression", "save deadline exceeded; output left untouched")
        return no_op()
    ref = recovery.save_full(event.project_dir, full if full is not None else preview)
    body = f"{chosen.text}\n{recovery.marker(chosen.kept_lines, chosen.total_lines, ref)}"
    if len(body) > len(preview):  # hard invariant: never more than the model would have seen
        log_error(env, "compression", "replacement longer than preview; output left untouched")
        return no_op()
    recovery.prune_outputs(
        event.project_dir,
        ttl_days=_num(cfg, "recovery.ttl_days", 14),
        max_files=int(_num(cfg, "recovery.max_files", 200)),
        max_bytes=int(_num(cfg, "recovery.max_bytes", 100_000_000)),
    )
    after = estimate_tokens(body, est)
    _count_trim(overlay_file, before - after, env)
    _log_decision(event, cfg, env, decision, ctype, before, after, ref.id, tokens_full)
    _learn(event, policy, cfg, env, decision, point, sctx, ref.id, chosen_source, body, ctype)

    def max_removed() -> float:
        """Counterfactual mode only: the largest removal over the point's trimming arms on this
        output, every arm measured the same way (``Candidate.tokens_after``, stand-in marker)."""
        best = float(before - picked.tokens_after)
        if point is not COMPRESSION:
            return best  # the pre-trim point has one trimming arm
        for arm in COMPRESSION.arms:
            if arm in (COMPRESSION.safe_arm, decision.arm):
                continue
            other = pick_candidate(
                full,
                preview,
                load_level(cfg, arm),
                compressor,
                ctx,
                cfg,
                est,
                before,
                event.project_dir,
            )
            if other is not None:
                best = max(best, float(before - other.tokens_after))
        return best

    regret.reward_savings(
        policy,
        decision.decision_id,
        decision.point,
        decision.bucket,
        before - after,
        regret.expected_resends(governor.remaining_turns(overlay, cfg)),
        home=home,
        project_key=event.project_key,
        env=env,
        cfg=cfg,
        before=before,
        max_removed=max_removed,
    )
    return Action(replace_output=body)


def _learn(
    event: Event,
    policy: Policy,
    cfg: Config,
    env: Mapping[str, str],
    decision: Decision,
    point: DecisionPoint,
    sctx: SessionCtx,
    recovery_id: str,
    full: str,
    kept: str,
    ctype: str,
) -> None:
    """Watch a trim for regret (always: the ledger counts regrets for any policy)."""
    assert event.tool is not None
    if decision.arm == point.safe_arm:
        return
    path = event.tool.input.get("file_path") if event.tool.name == "Read" else None
    evicted = regret.RegretDetector(cfg, env).track(
        sctx,
        decision.decision_id,
        point=decision.point,
        bucket=decision.bucket,
        arm=decision.arm,
        recovery_id=recovery_id,
        command_sig=regret.event_signature(event),
        full=full,
        kept=kept,
        ctype=ctype,
        files=[path] if isinstance(path, str) and path else None,
    )
    regret.pay(policy, evicted)


_SHOW_ID = re.compile(r"`token-sparen show ([0-9a-f]{12})`\]\s*$")


@dataclass(frozen=True)
class CompressInfo:
    """What a successful ``compress_text`` did."""

    recovery_id: str


def compress_text(
    text: str,
    tool: ToolCall,
    cfg: Config,
    project_dir: Path,
    env: Mapping[str, str],
) -> tuple[str, CompressInfo | None]:
    """Run ``text`` through the hook's detect -> policy -> compress -> save -> marker path.

    Used by ``token-sparen run``. Returns the original text and ``None`` when nothing was elided.
    """
    event = Event(
        host="cli",
        kind="post_tool",
        session_id=env.get("CLAUDE_SESSION_ID") or "cli",
        project_dir=project_dir,
        project_key=keycache.cached_project_key(project_dir, env),
        transcript_path=None,
        tool=tool,
        output_text=text,
        raw_response=text,
    )
    policy = features.static_policy(cfg.data)  # honours [learn.hold]
    action = handle_post_tool(event, policy, cfg, env=env, replaceable=lambda _e: True)
    body = action.replace_output
    if body is None:
        return text, None
    found = _SHOW_ID.search(body)
    return body, CompressInfo(found.group(1) if found else "")
