"""Subagent shaping (spec 6.7; decision point ``subagent_brief``).

PreToolUse(Task|Agent) with a string ``prompt``: the policy decides whether to append a one-line
brief asking for conclusions rather than raw dumps (``updatedInput`` = the original input with the
extended prompt). Idempotent: a prompt that already carries the brief is left alone and no
decision is logged. Oversized subagent results go through ordinary PostToolUse compression when
the host's result shape can be replaced.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping

from token_sparen.config import Config
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Action, Event
from token_sparen.learn import decisions, features
from token_sparen.learn.policy import SUBAGENT_BRIEF, DecisionRecord, Policy
from token_sparen.mechanisms import governor, tracking
from token_sparen.store import session

__all__ = ["BRIEF", "SUBAGENT_BRIEF", "brief", "handle_pre_tool"]

BRIEF = "Return conclusions, file paths and line numbers rather than raw dumps."
AGENT_TOOLS = frozenset({"Task", "Agent"})


def brief() -> str:
    return BRIEF


def handle_pre_tool(
    event: Event, policy: Policy, cfg: Config, *, env: Mapping[str, str] | None = None
) -> Action:
    env = os.environ if env is None else env
    tool = event.tool
    if tool is None or tool.name not in AGENT_TOOLS or cfg.get("subagent.enabled", True) is False:
        return Action()
    prompt = tool.input.get("prompt")
    if not isinstance(prompt, str) or not prompt or BRIEF in prompt:
        return Action()
    overlay_file = tracking.overlay_path(event, env)
    sctx = governor.decision_ctx(event, session.read_session(overlay_file), overlay_file, cfg, env)
    decision = policy.decide(
        SUBAGENT_BRIEF, features.for_point(SUBAGENT_BRIEF, event, sctx, None), sctx
    )
    new_prompt = f"{prompt}\n\n{BRIEF}"
    on = decision.arm == "on"
    before = estimate_tokens(prompt)
    rec = DecisionRecord(
        decision_id=decision.decision_id,
        point=decision.point,
        arm=decision.arm,
        propensity=decision.propensity,
        bucket=decision.bucket,
        tokens_before=before,
        tokens_after=estimate_tokens(new_prompt) if on else before,
        recovery_id=None,
        tool_use_id=tool.use_id,
        command_sig=tool.name,
        ts=time.time(),
        tool_name=tool.name,
    )
    decisions.append(event.project_key, rec, cfg, env, "subagent")
    if not on:
        return Action()
    return Action(rewrite_input={**tool.input, "prompt": new_prompt})
