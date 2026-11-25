"""Emit gate for injected context: per-session dedupe, a token budget and accounting.

Every token we inject costs the user, so the gate (a) never repeats an item already injected in
this session (keys persisted in the session overlay), (b) keeps the highest-priority items that fit
``max_tokens`` (lower ``priority`` int first, ties in insertion order) and drops the rest, and (c)
adds what it emits to the overlay's ``injected_tokens`` ledger, which net savings subtract.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from token_sparen.config import log_error
from token_sparen.estimate import estimate_tokens
from token_sparen.store import jsonstore

SEPARATOR = "\n\n"
KEYS_FIELD = "emitted_keys"
LEDGER_FIELD = "injected_tokens"


@dataclass(frozen=True)
class _Item:
    kind: str
    text: str
    key: str
    priority: int
    order: int


class Gate:
    def __init__(self, overlay_path: Path, env: Mapping[str, str] | None = None) -> None:
        self._path = overlay_path
        self._env = os.environ if env is None else env
        self._items: list[_Item] = []

    def add(self, kind: str, text: str, key: str, priority: int = 50) -> None:
        """Queue ``text`` under dedupe ``key``; a repeated key within this gate is ignored."""
        if any(item.key == key for item in self._items):
            return
        self._items.append(_Item(kind, text, key, priority, len(self._items)))

    def render(self, max_tokens: int) -> str | None:
        """The text to inject (None = nothing); persists keys and the ledger before returning."""
        if not self._items:
            return None
        chosen: list[str] = []
        texts: list[str] = []

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            chosen.clear()
            texts.clear()
            raw_keys = state.get(KEYS_FIELD)
            seen = [k for k in raw_keys if isinstance(k, str)] if isinstance(raw_keys, list) else []
            for item in sorted(self._items, key=lambda i: (i.priority, i.order)):
                if item.key in seen:
                    continue
                candidate = SEPARATOR.join([*texts, item.text])
                if estimate_tokens(candidate) > max_tokens:
                    continue  # over budget: dropped this time, may go out in a later hook
                texts.append(item.text)
                chosen.append(item.key)
            if not texts:
                return state
            ledger = state.get(LEDGER_FIELD)
            prior = ledger if isinstance(ledger, int) and not isinstance(ledger, bool) else 0
            state[KEYS_FIELD] = [*seen, *chosen]
            state[LEDGER_FIELD] = prior + estimate_tokens(SEPARATOR.join(texts))
            return state

        try:
            jsonstore.update(self._path, apply, {}, self._env)
        except (jsonstore.StoreBusy, OSError) as exc:
            # Without accounting and dedupe we would risk repeats: inject nothing.
            log_error(self._env, "emit", f"{type(exc).__name__}: {exc}")
            return None
        return SEPARATOR.join(texts) if texts else None
