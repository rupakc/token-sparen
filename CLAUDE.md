# token-sparen

MIT open-source token optimizer plugin for coding agents. One learning engine (contextual
Thompson-sampling bandit with a quality constraint) drives recoverable output compression, read
de-dupe, guards, repo-fact learning, tool pruning and a live statusline. North star: maximise tokens
saved subject to no measurable quality loss. Spec: `docs/specs/2026-11-25-token-sparen-design.md`.
Plan: `docs/plans/2026-11-25-token-sparen-m0-m3.md`. Decisions: `MEMORY.md`.

## Layout
- `token_sparen/` core (stdlib only): `cli.py events.py config.py estimate.py transcript.py emit.py recovery.py`
  - `store/` JSON+JSONL state, `compress/` compressors, `mechanisms/` decision-point mechanisms
  - `facts/` repo-fact mining and injection, `learn/` bandit engine, `replay/` offline replay
  - `adapters/` host translation; `adapters/claude/` Claude Code (entry, translate, statusline, settings)
  - `extensions.py` trusted extension loading; `testing/conformance.py` compressor conformance kit
- `params/defaults.toml` every tunable (hill-climbing surface)
- `hooks/` plugin hooks (`hooks.json`, `run.py`); `commands/`, `skills/token-sparen/` user-facing plugin parts
- `evals/e2/` fact-preservation suite (corpus, `needles.toml`, `run.py`)
- `tests/` mirrors the package; fixtures are synthetic only
- `.claude/agents/`, `.claude/skills/` dev-time agents and skills

## Commands
```
pytest                          # tests + coverage gate (>= 90 %)
ruff check .                    # lint
ruff format --check .           # format
mypy --strict token_sparen      # types
python evals/e2/run.py          # E2 fact-preservation, must stay 100 %
python scripts/check_denylist.py  # private-data deny-list, run before every commit
```
Use the repo `.venv`. All must pass before a commit.

## Invariants (never break)
- Python >= 3.11; runtime is stdlib only. Dev deps live in the `dev` extra.
- Hooks fail open: any exception or latency overrun returns the host no-op and logs one line to
  `~/.token-sparen/logs/errors.jsonl`. Non-LLM hook p95 < 50 ms.
- Outputs under 2000 estimated tokens are never modified.
- Never elide lines matching `ERROR_PATTERNS`.
- Every elision appends the `[token-sparen: kept X of Y lines ...]` marker and saves the full text
  to `<project>/.token-sparen/outputs/<id>.txt`.
- `TOKEN_SPAREN_DISABLE=1` makes every hook a no-op.
- Config precedence: defaults < `~/.token-sparen/config.toml` < `<project>/token-sparen.toml` < env
  `TOKEN_SPAREN_*` < session command.
- State root `~/.token-sparen/` (override `TOKEN_SPAREN_HOME`); project key = first 16 hex of
  sha256(normalised git remote), else sha256(realpath). Never store raw paths in shared stores.
- Hook commands reference files only via `${CLAUDE_PLUGIN_ROOT}`; no top-level `bin/`, no unpinned
  `npx`/`uvx`, no non-image plugin file > 256 KiB.
- Settings writes (statusline, tool pruning) are opt-in, backed up, reversible, disclosed in README.
- Quality is a hard constraint, never traded in a weighted sum. Never tune on the test split.
- Reimplement ideas, copy no code from any reference or competitor; credit in `NOTICE`.

## Never commit private data
No employer or organisation names, internal URLs, people, local absolute paths, transcript ids or
private figures. Owner's local sessions and E1 results stay outside the repo; refer to them only
generically. `python scripts/check_denylist.py` runs in CI and must pass locally first.

## Workflow: Elephant-Goldfish (EGM), see `.claude/skills/egm-cycle`
1. Elephant drafts spec/plan, no code.
2. Fresh zero-context `goldfish-reviewer` reads it cold and ranks gaps; resolve them.
3. Implement with TDD (failing test first, then minimal code).
4. `drift-checker` compares code to spec and reports divergences with file:line.
5. Self-review the diff and run `code-review` before pushing.

Other skills: `add-compressor`, `run-evals`, `hill-climb`, `release`.
Agents: `goldfish-reviewer`, `drift-checker`, `compressor-author`, `eval-hillclimber`,
`bench-runner`, `host-adapter-author`.

## Conventions
- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`); push to `main` after each task
  once checks pass locally.
- End commit messages with the Co-Authored-By trailer your agent harness specifies (if any).
- Typed, small modules; `main(argv: list[str] | None = None) -> int` style entry points.
- Record any design decision or owner call in `MEMORY.md` with a date.
