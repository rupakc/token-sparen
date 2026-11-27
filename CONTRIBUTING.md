# Contributing

Thanks for your interest in token-sparen. Read `CLAUDE.md` / `AGENTS.md` for the layout and the
invariants that must never break, and `MEMORY.md` for the dated decision log.

## Development setup

Python >= 3.11 (the runtime core is standard library only).

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The documentation build needs the `docs` extra (`pip install -e ".[docs]"`, Python-Markdown).
To try the plugin from a checkout, run Claude Code with `claude --plugin-dir .` (a development
install); users install from the marketplace, which tracks the `release/claude` branch.

## Documentation

`docs/user/` is the single source of the user documentation. `site/` holds only the HTML
template and stylesheet. `python scripts/build_site.py` renders the site from `docs/user/`; never edit the
generated pages. The site is deployed from release tags, not from every push to `main`. Savings and quality results appear
only in the rendered Evidence table (`scripts/docs_lint.py` enforces the figure rule).

## Releases

Releases are cut by the maintainer from tags; see `docs/release/runbook.md` and the `release`
skill. In short: an owner-signed annotated tag `vX.Y.Z` on a `chore(release): vX.Y.Z` commit on
`main` starts the workflow, which needs two owner approvals (`release`, then `pypi`). Release
notes are the curated `CHANGELOG.md` section; contributors add user-facing entries under
`[Unreleased]` and keep milestone detail below `<!-- pre-1.0 milestones -->`.

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

## Offline evaluation (E1 and E3): private-only rules

`token-sparen replay` (E1) and `token-sparen ope` (E3) read real Claude Code sessions and decision
logs. Those contain confidential material, so:

- **Nothing derived from anyone's real sessions is ever committed**: no transcript text, paths,
  ids, and no aggregate numbers either (not in the README, `MEMORY.md`, issues-as-files, test
  fixtures or commit messages). Aggregates from the maintainer's sessions stay in a private folder
  outside the repository. Tests and fixtures are synthetic.
- Write reports with `--out` to a path outside any git work tree; the command refuses paths inside
  one and writes mode 0600.
- Never tune on the test split: choose parameters on `--split dev`, look at `--split test` once.
  `--cutoff YYYY-MM-DD` sets the date boundary (sessions before it are dev).
- Treat an `ope` verdict of `insufficient` as "no answer", not as a pass.

```
token-sparen replay <transcript files or dirs> --split dev --policy default,static --out <report>
token-sparen ope --log <decisions.jsonl and decisions.jsonl.1 ...> --target engine-snapshot \
    --holdout-after YYYY-MM-DD --out <report>
```

Decision logs are in `~/.token-sparen/projects/<key>/`; `~/.token-sparen/project_keys.json` maps a
project to its `<key>`. The deterministic evals (E2, `python evals/e2/run.py --strict`) are the only
ones whose numbers belong in the repo, because their corpus is synthetic.

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
- Never commit E1/E3 results or numbers derived from real sessions (see above).
- Commit messages follow conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`,
  `refactor:`, `ci:`); one logical change per commit.
- Contributors open pull requests; maintainers may push to `main` after the checks pass.
- Reimplement ideas, never copy code from references or competitors; credit goes in `NOTICE`.
