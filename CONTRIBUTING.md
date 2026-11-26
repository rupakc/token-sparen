# Contributing

Thanks for your interest in token-sparen. Read `CLAUDE.md` / `AGENTS.md` for the layout and the
invariants that must never break, and `MEMORY.md` for the dated decision log.

## Development setup

Python >= 3.11 (the runtime core is standard library only).

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Checks (all must pass before a commit)

```
pytest                                   # tests + coverage gate (>= 90 %); perf tests are deselected
ruff check . && ruff format --check .    # lint and format
mypy --strict token_sparen               # types
python evals/e2/run.py --strict          # E2 fact-preservation suite, must stay 100 %
python scripts/check_denylist.py         # private-data deny-list
```

Also run these when they are relevant, CI runs them too:

- **Perf budgets** (tests marked `perf`, e.g. hook p95 latency): `pytest -m perf --no-cov`. On a
  slow or busy machine scale the budgets with `TOKEN_SPAREN_PERF_FACTOR` (CI uses `1.5`), e.g.
  `TOKEN_SPAREN_PERF_FACTOR=1.5 pytest -m perf --no-cov`.
- **Wheel check** (packaging, run after touching `pyproject.toml` or package data):
  `pip install build && python scripts/check_wheel.py` builds the wheel, installs it in a fresh
  venv outside the checkout and smoke-tests the CLI.

## Deny-list (no private data)

Never commit local paths, credentials, organisation names, internal URLs, transcript ids or
private figures; fixtures are synthetic only. `scripts/check_denylist.py` enforces this in CI. Extra
private words can be supplied locally through the `TOKEN_SPAREN_DENYLIST` environment variable
(comma-separated); they are never stored in the repo.

## Workflow: Elephant-Goldfish review (EGM)

1. Draft the design or plan first, without code.
2. Have a fresh reader with no context review it cold and rank the gaps; resolve them.
3. Implement with TDD: a failing test first, then the minimal code.
4. Compare code to spec and list divergences with file and line (the drift check).
5. Self-review the diff before opening a PR.

The role prompts live in `.claude/agents/` and `.claude/skills/egm-cycle`; they are plain Markdown
and work with any agent. Record any design decision in `MEMORY.md` with a date (append-only).

## Guidelines

- Write a failing test first, then the implementation. Keep modules small and typed.
- Commit messages follow conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`,
  `refactor:`, `ci:`); one logical change per commit.
- Contributors open pull requests; maintainers may push to `main` after the checks pass.
- Reimplement ideas, never copy code from references or competitors; credit goes in `NOTICE`.
