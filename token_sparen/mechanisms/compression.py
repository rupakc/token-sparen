"""Recoverable output compression on PostToolUse (decision point ``compression``).

Flow: cheap gates (tool deny-set, replaceable shape, size) -> detect content type -> ask the
policy for an arm -> compress -> keep only if it saves at least ``min_saving_ratio`` -> save the
full text, append the recovery marker, update the session ledger. Every decision the policy made
is logged with its propensity, including the ones that ended as a no-op.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from token_sparen import recovery
from token_sparen.adapters.claude.translate import render_tool_output
from token_sparen.compress import CompressContext, detect, load_level, registry
from token_sparen.config import Config, log_error, state_home
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn.policy import COMPRESSION, Decision, DecisionRecord, Policy
from token_sparen.mechanisms import tracking
from token_sparen.store import jsonl, jsonstore, paths, session

PERSISTED_MAX_BYTES = 20 * 1024 * 1024
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
_EST_TYPE = {"json": "json", "pplx": "json", "log": "log", "build_log": "log"}
_POST_KINDS = frozenset({"post_tool", "post_tool_failure"})

Replaceable = Callable[[Event], bool]


def _num(cfg: Config, key: str, default: float) -> float:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _original(event: Event) -> Any:
    """What the host would show the model: the tool response, or the failure's error text."""
    if event.kind == "post_tool_failure" and event.raw_response is None:
        return event.output_text
    return event.raw_response


def shape_replaceable(event: Event) -> bool:
    """Host-neutral check: the response has a shape we can render a replacement for."""
    if event.tool is None or event.kind not in _POST_KINDS:
        return False
    return render_tool_output(event.tool.name, _original(event), "") is not None


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
    return tool.name in names


def _persisted_text(event: Event) -> str | None:
    """Bash: the full output Claude Code saved before truncating stdout, when readable."""
    resp = event.raw_response
    if event.tool is None or event.tool.name != "Bash" or not isinstance(resp, dict):
        return None
    where = resp.get("persistedOutputPath")
    if not isinstance(where, str) or not where:
        return None
    try:
        path = Path(where)
        if path.stat().st_size > PERSISTED_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    stderr = resp.get("stderr")
    return f"{text}\n{stderr}" if isinstance(stderr, str) and stderr else text


def _size_band(tokens: int) -> str:
    if tokens < 8000:
        return "s"
    return "m" if tokens < 32000 else "l"


def _command_sig(event: Event) -> str:
    tool = event.tool
    assert tool is not None
    cmd = tool.input.get("command")
    if tool.name == "Bash" and isinstance(cmd, str):
        return cmd[:COMMAND_SIG_CHARS]
    return tool.name


def _log_decision(
    event: Event,
    cfg: Config,
    env: Mapping[str, str],
    decision: Decision,
    ctype: str,
    before: int,
    after: int,
    recovery_id: str | None,
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


def handle_post_tool(
    event: Event,
    policy: Policy,
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    replaceable: Replaceable | None = None,
) -> Action:
    """Compress a large tool output recoverably; any non-qualifying output is left untouched."""
    env = os.environ if env is None else env
    if event.tool is None or event.output_text is None or _skipped(event, cfg):
        return Action()
    if not (replaceable or shape_replaceable)(event):
        return Action()  # checked before any work: nothing to save or log if we cannot replace
    text = _persisted_text(event) or event.output_text
    if estimate_tokens(text) < _num(cfg, "compression.min_tokens", 2000):
        return Action()
    ctype = detect(text, event.tool)
    est_type = _EST_TYPE.get(ctype, "text")
    before = estimate_tokens(text, est_type)
    overlay_file = tracking.overlay_path(event, env)
    overlay = session.read_session(overlay_file)
    features = {
        "tool_category": event.tool.category,
        "content_type": ctype,
        "size_band": _size_band(before),
    }
    decision = policy.decide(
        COMPRESSION, features, tracking.session_ctx(event, overlay, overlay_file)
    )

    def no_op() -> Action:
        _log_decision(event, cfg, env, decision, ctype, before, before, None)
        return Action()

    if decision.arm == COMPRESSION.safe_arm:
        return no_op()
    ctx = CompressContext(
        prompt=tracking.last_prompt(overlay),
        recent_paths=tuple(tracking.recent_paths(overlay)),
        config=cfg,
    )
    result = registry.for_type(ctype).compress(text, load_level(cfg, decision.arm), ctx)
    if result.lossless or result.kept_lines >= result.total_lines:
        return no_op()
    probe = recovery.marker(
        result.kept_lines, result.total_lines, _marker_len_ref(event.project_dir)
    )
    after = estimate_tokens(f"{result.text}\n{probe}", est_type)
    if before <= 0 or (before - after) / before < _num(cfg, "compression.min_saving_ratio", 0.15):
        return no_op()
    ref = recovery.save_full(event.project_dir, text)
    body = f"{result.text}\n{recovery.marker(result.kept_lines, result.total_lines, ref)}"
    recovery.prune_outputs(
        event.project_dir,
        ttl_days=_num(cfg, "recovery.ttl_days", 14),
        max_files=int(_num(cfg, "recovery.max_files", 200)),
        max_bytes=int(_num(cfg, "recovery.max_bytes", 100_000_000)),
    )
    after = estimate_tokens(body, est_type)
    _count_trim(overlay_file, before - after, env)
    _log_decision(event, cfg, env, decision, ctype, before, after, ref.id)
    return Action(replace_output=body)
