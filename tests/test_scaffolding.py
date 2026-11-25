"""Tests for the Claude development scaffolding (agents, skills, CLAUDE.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
EXPECTED_AGENTS = {
    "goldfish-reviewer",
    "drift-checker",
    "compressor-author",
    "eval-hillclimber",
    "bench-runner",
    "host-adapter-author",
}
EXPECTED_SKILLS = {"add-compressor", "run-evals", "hill-climb", "release", "egm-cycle"}
READ_ONLY = {"goldfish-reviewer", "drift-checker"}


def parse_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path} lacks front-matter"
    end = lines.index("---", 1)
    out: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_expected_files_exist() -> None:
    assert {p.stem for p in AGENTS} == EXPECTED_AGENTS
    assert {p.parent.name for p in SKILLS} == EXPECTED_SKILLS


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_frontmatter(path: Path) -> None:
    fm = parse_frontmatter(path)
    assert fm["name"] == path.stem
    assert fm["description"]
    assert fm["tools"]
    if path.stem in READ_ONLY:
        tools = {t.strip() for t in fm["tools"].split(",")}
        assert not tools & {"Edit", "Write", "NotebookEdit"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(path: Path) -> None:
    fm = parse_frontmatter(path)
    assert fm["name"] == path.parent.name
    assert fm["description"].startswith("Use when")


def test_claude_md_lists_commands() -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for cmd in ("pytest", "ruff check .", "mypy --strict token_sparen", "python evals/e2/run.py"):
        assert cmd in text
    assert len(text.splitlines()) <= 120


def test_agents_and_memory_exist() -> None:
    assert (ROOT / "AGENTS.md").read_text(encoding="utf-8").strip()
    memory = (ROOT / "MEMORY.md").read_text(encoding="utf-8")
    assert "2026-11-25" in memory and "3.11" in memory
