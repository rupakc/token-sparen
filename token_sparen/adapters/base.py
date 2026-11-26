"""The host-adapter protocol every host integration implements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from token_sparen.events import Action, Event


class HostAdapter(Protocol):
    """Translate one host's hook payloads to canonical events and actions back to host JSON."""

    name: str
    capabilities: frozenset[str]

    def parse(self, event_name: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> Event:
        """Canonical event for a host hook payload; raises ValueError when it cannot."""
        ...

    def render(self, event: Event, action: Action) -> dict[str, Any]:
        """Host JSON for ``action``; ``{}`` is the no-op. Unsupported fields are dropped."""
        ...

    def render_tool_output(self, tool_name: str, original: Any, new_text: str) -> Any | None:
        """A replacement tool result in the same shape as ``original``, or None (= keep it)."""
        ...

    def replaceable(self, event: Event) -> bool:
        """Whether this host can replace ``event``'s tool result (mechanisms skip work if not)."""
        ...
