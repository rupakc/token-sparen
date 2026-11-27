# token-sparen

MIT open-source token optimizer plugin for coding agents (Claude Code first). Shipped (M0/M1):
recoverable output compression, read de-dupe, guards, budget governor, boundary advice, live
statusline, extensions. M2: one learning engine (contextual Thompson-sampling bandit
with a quality constraint) steers every hook decision (`learn/hookpolicy.py`, built lazily per
hook; `[learn] enabled = false` -> `StaticPolicy`), with regret detection, session fold and
circuit breaker; `replay` (E1) and `ope` (E3) evaluate offline into private reports. M3: repo-fact
learning (miners, lifecycle bandit, SessionStart injection) and opt-in tool pruning
(`mechanisms/prune.py`, deny rules via `adapters/claude/settings.py`) and the learned
failure-loop guard (`mechanisms/loopguard.py`) are in. North star:
maximise tokens saved subject to no measurable quality loss.
Spec: `docs/specs/2026-11-25-token-sparen-design.md`. Plan: `docs/plans/2026-11-25-token-sparen-m0-m3.md`.
Decisions: `MEMORY.md`.

## Layout
- `token_sparen/` core (stdlib only): `cli.py events.py config.py estimate.py transcript.py emit.py recovery.py extensions.py`
  (`deferred.py`: work that runs only after a hook's response reached the host)
  - `store/` JSON+JSONL state, `compress/` compressors, `mechanisms/` decision-point mechanisms
    (compression, guards, read_dedupe, governor, boundary, subagent, tracking, prune, loopguard)
  - `learn/` policy, decision log, engine, regret, session fold (`overlay`), circuit `breaker`,
    `ope` (off-policy evaluation)
  - `replay/` E1 offline replay and the private report writer (`--out` outside any git tree);
    `runner.py` runs it at scale (groups per project dir, processes, checkpoint, lanes)
  - `adapters/` host protocol; `adapters/claude/` Claude Code (entry, translate, statusline, settings)
  - `testing/conformance.py` compressor conformance kit
  - `token_sparen/params/defaults.toml` every tunable (hill-climbing surface; packaged inside
    the package)
  - `facts/` (M3) repo-fact sanitiser (`sanitize`), fold-time miners + `facts.json` store
    (`miners`), the per-fact bandit lifecycle (`lifecycle`) and SessionStart injection as a
    labelled data block (`inject`)
- `hooks/` plugin hooks (`hooks.json`, `run.py`, `run_cli.py`); `skills/token-sparen/` is the
  `/token-sparen` skill (there is no `commands/` directory)
- `evals/e2/` fact-preservation suite (corpus, `needles.toml`, `run.py`); `evals/challengers/`
  offline learners run through the replay's `sim` lane (LinTS; never imported by the package)
- `tests/` mirrors the package; fixtures are synthetic only; `tests/perf/` is the perf marker
- `scripts/` `check_denylist.py`, `check_wheel.py`; `.claude/agents/`, `.claude/skills/` dev-time
  agents and skills

## Commands
```
pytest                          # tests + coverage gate (>= 90 %)
ruff check .                    # lint
ruff format --check .           # format
mypy --strict token_sparen      # types
python evals/e2/run.py --strict # E2 fact-preservation, must stay 100 %
TOKEN_SPAREN_PERF_FACTOR=1.5 pytest -m perf --no-cov   # strict latency budgets (CI runs it)
python scripts/check_wheel.py   # build wheel, install in fresh venv, smoke-test CLI
python scripts/check_denylist.py  # private-data deny-list, run before every commit
```
Use the repo `.venv`. All must pass before a commit.

## Invariants (never break)
- Python >= 3.11; runtime is stdlib only. Dev deps live in the `dev` extra.
- Hooks fail open: any exception or latency overrun returns the host no-op and logs one line to
  `~/.token-sparen/logs/errors.jsonl`. Non-LLM hook p95 < 50 ms.
- Outputs under 2000 estimated tokens are never modified: `compression.min_tokens` and
  `read_dedupe.min_tokens` are floored at 2000 (a higher value is honoured).
- Never larger: a replacement is used only if it is no longer than the host's preview of the
  output and saves >= `min_saving_ratio`.
- Failed tool results are not replaceable on Claude Code (PostToolUseFailure); do no replacement
  work for them (only `additionalContext`, e.g. the loop guard's redirect).
- No PreToolUse command rewriting: a pre-trim is a PostToolUse compression arm, never a shell edit.
- Mechanisms and compressors never import `token_sparen.adapters` (hosts own tool-output shapes).
- Never elide error lines (`is_error_line`: `ERROR_PATTERNS`; a stack-frame head counts only
  when it matches outside its file location).
- Every elision appends the `[token-sparen: kept X of Y lines ...]` marker and saves the full text
  to `<project>/.token-sparen/outputs/<id>.txt`.
- `TOKEN_SPAREN_DISABLE=1` makes every hook a no-op.
- Config precedence: defaults < `~/.token-sparen/config.toml` < `<project>/token-sparen.toml` < env
  `TOKEN_SPAREN_*` < session overrides (`token-sparen config set`).
- The project file is deny-by-default: it may only make token-sparen intervene less or keep more
  (never trim more, recover less or block more); an unlisted key is user-only (`PROJECT_POLICY`
  in `config.py`).
- Quality first: file contents are never trimmed by default (`compression.trim_file_contents` is
  false) and only `compression.allow_ctypes` are offered for compression. The user may widen
  both; a project may only narrow them.
- `[learn.hold] compression = true` stops every output trim (pre-trim included), with learning on
  or off; `compression.allow_ctypes = []` is the config-only switch.
- State root `~/.token-sparen/` (override `TOKEN_SPAREN_HOME`, ignored when it resolves inside
  the current project or its git work tree (by file identity) or, on POSIX, is not owned by the
  user, unless it is a not-yet-created path under a root-owned sticky dir such as /tmp);
  project key = first 16 hex of sha256(normalised git remote), else sha256(realpath).
  `global/` and project keys hold no raw paths, commands or session ids. Per-project files under
  `projects/<key>/` are plain text and may hold user-local data (last prompt <= 2000 chars, file
  paths, normalised commands, 500 chars of Bash commands in `decisions.jsonl`), as does
  user-local bookkeeping (trusted extensions, statusline install record, `prune_rules.json`: the
  tool-pruning ownership registry keyed by settings-file path). Keep README's disk disclosure
  ("What it writes to disk") accurate.
- Hook commands reference files only via `${CLAUDE_PLUGIN_ROOT}`; no top-level `bin/`, no unpinned
  `npx`/`uvx`, no non-image plugin file > 256 KiB.
- Settings writes (statusline, tool pruning) are opt-in, backed up, reversible, disclosed in README.
  Tool-pruning deny rules are written only by the session-end fold and only on positive git
  confirmation that the settings file is untracked and ignored.
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
- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`); maintainers push to `main` after each
  task once checks pass locally, contributors open PRs.
- End commit messages with the Co-Authored-By trailer your agent harness specifies (if any).
- Typed, small modules; `main(argv: list[str] | None = None) -> int` style entry points.
- Record any design decision or owner call in `MEMORY.md` with a date.
