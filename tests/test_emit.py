"""Emit gate: per-session dedupe, token budget, injected-token ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from token_sparen import emit
from token_sparen.estimate import estimate_tokens
from token_sparen.store import jsonstore, session


def _path(tmp_path: Path) -> Path:
    return session.session_path(tmp_path, "0123456789abcdef", "s-1")


def test_emit_dedupes_and_budgets(tmp_path: Path) -> None:
    path = _path(tmp_path)
    g = emit.Gate(path)
    g.add("fact", "A" * 40, "k-a", priority=10)  # 10 tokens
    g.add("fact", "B" * 40, "k-b", priority=90)  # lowest priority, dropped over budget
    g.add("fact", "C" * 40, "k-c", priority=20)
    g.add("fact", "dup", "k-a")  # duplicate key within this render
    out = g.render(max_tokens=22)
    assert out == "A" * 40 + "\n\n" + "C" * 40
    state = session.read_session(path)
    assert state["injected_tokens"] == estimate_tokens(out)
    assert set(state["emitted_keys"]) == {"k-a", "k-c"}

    # second gate in a later hook in the same session: emitted keys are not repeated,
    # the previously dropped item may now go out
    g2 = emit.Gate(path)
    g2.add("fact", "A" * 40, "k-a")
    g2.add("fact", "B" * 40, "k-b", priority=90)
    out2 = g2.render(max_tokens=100)
    assert out2 == "B" * 40
    state = session.read_session(path)
    assert state["injected_tokens"] == estimate_tokens(out) + estimate_tokens(out2)

    g3 = emit.Gate(path)
    g3.add("fact", "A" * 40, "k-a")
    assert g3.render(max_tokens=100) is None
    assert emit.Gate(path).render(max_tokens=100) is None


def test_emit_priority_ties_keep_insertion_order(tmp_path: Path) -> None:
    g = emit.Gate(_path(tmp_path))
    g.add("a", "first", "1")
    g.add("a", "second", "2")
    assert g.render(max_tokens=100) == "first\n\nsecond"


def test_emit_nothing_fits(tmp_path: Path) -> None:
    path = _path(tmp_path)
    g = emit.Gate(path)
    g.add("a", "x" * 400, "big")
    assert g.render(max_tokens=5) is None
    assert session.read_session(path) == {}


def test_emit_store_busy_injects_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*a: Any, **k: Any) -> Any:
        raise jsonstore.StoreBusy("x")

    monkeypatch.setattr(jsonstore, "update", busy)
    g = emit.Gate(_path(tmp_path), env={"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    g.add("a", "text", "k")
    assert g.render(max_tokens=100) is None
    assert (tmp_path / "h" / "logs" / "errors.jsonl").exists()


def test_emit_tolerates_bad_overlay_types(tmp_path: Path) -> None:
    path = _path(tmp_path)
    session.update_session(path, lambda d: {"emitted_keys": "oops", "injected_tokens": "x"})
    g = emit.Gate(path)
    g.add("a", "text", "k")
    assert g.render(max_tokens=100) == "text"
    state = session.read_session(path)
    assert state["emitted_keys"] == ["k"] and state["injected_tokens"] == estimate_tokens("text")
