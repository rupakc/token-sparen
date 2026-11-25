# AGENTS.md (host-neutral contributor guide)

For Codex, Gemini CLI, OpenCode and other coding agents working on token-sparen. The Claude Code
equivalent is `CLAUDE.md`; the two must stay consistent.

## Project
token-sparen is an MIT open-source token optimizer for coding agents: recoverable tool-output
compression, read de-duplication, guards, learned repository facts, tool pruning and a live
statusline, all driven by one learning engine (contextual Thompson-sampling bandit with a hard
quality constraint). Spec: `docs/specs/2026-11-25-token-sparen-design.md`; plan:
`docs/plans/2026-11-25-token-sparen-m0-m3.md`; decisions: `MEMORY.md`.

## Layout
`token_sparen/` (core, stdlib only: `store/ compress/ mechanisms/ facts/ learn/ replay/ adapters/`),
`params/defaults.toml` (all tunables), `hooks/`, `commands/`, `skills/` (plugin), `evals/e2/`
(fact-preservation suite), `tests/` (mirrors the package, synthetic fixtures only), `scripts/`.

## Commands (use the repo `.venv`; all must pass before a commit)
```
pytest
ruff check .
ruff format --check .
mypy --strict token_sparen
python evals/e2/run.py
python scripts/check_denylist.py
```

## Invariants
- Python >= 3.11, runtime stdlib only.
- Hooks fail open (no-op response plus one line in `~/.token-sparen/logs/errors.jsonl`); non-LLM p95 < 50 ms.
- Outputs under 2000 estimated tokens are never modified; lines matching `ERROR_PATTERNS` are never elided.
- Every elision carries the recovery marker and the full text is saved under `<project>/.token-sparen/outputs/`.
- `TOKEN_SPAREN_DISABLE=1` disables every hook.
- Config precedence: defaults < user config < project `token-sparen.toml` < env `TOKEN_SPAREN_*` < session command.
- State root `~/.token-sparen/` (`TOKEN_SPAREN_HOME`); project key is a hash, never a raw path.
- Settings writes are opt-in, backed up, reversible and documented.
- Quality is a constraint, never a weighted term. Never tune on the test split.
- Reimplement, never copy code from references or competitors.

## Never commit private data
No employer or organisation names, internal URLs, people, local absolute paths, transcript ids or
private figures. Private inputs (the owner's local sessions) stay outside the repo.
Run `python scripts/check_denylist.py` before every commit.

## Process
Elephant-Goldfish: draft spec/plan (no code) -> cold zero-context review for gaps -> resolve ->
TDD implementation -> drift report comparing code to spec -> self-review. Role prompts live in
`.claude/agents/*.md` and `.claude/skills/*/SKILL.md`; they are plain Markdown and usable by any host.
Conventional commits. Record decisions in `MEMORY.md` with a date.
