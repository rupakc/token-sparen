# AGENTS.md (host-neutral contributor guide)

For Codex, Gemini CLI, OpenCode and other coding agents working on token-sparen. The Claude Code
equivalent is `CLAUDE.md`; the two must stay consistent.

## Project
token-sparen is an MIT open-source token optimizer for coding agents (Claude Code first). Shipped
(M0/M1): recoverable tool-output compression, read de-duplication, guards, budget governor,
boundary advice, live statusline, trusted extensions. M2: a learning engine (contextual
Thompson-sampling bandit with a hard quality constraint) steers every hook decision
(`learn/hookpolicy.py`, built lazily per hook; `[learn] enabled = false` -> `StaticPolicy`), with
regret detection, a session `fold` and a circuit breaker; `replay` (E1) and `ope` (E3) evaluate
offline into private reports. Defaults are quality-first: file contents are never trimmed and only
an allow-list of content types is offered for compression. M3: repo-fact learning, opt-in
tool pruning and the learned failure-loop guard (`mechanisms/loopguard.py`) are in. M5: the live
benchmark E4 lives in `bench/` (dev-time, never imported by the package): `python -m bench` runs
screening, the power step, the confirmation and the release gate (`bench/README.md`, method
`docs/reports/m5-method.md`, owner runbook `docs/release/benchmark-runbook.md`); `run --dry-run`
exercises the whole pipeline with fakes in CI.
Spec: `docs/specs/2026-11-25-token-sparen-design.md`; plan: `docs/plans/2026-11-25-token-sparen-m0-m3.md`;
decisions: `MEMORY.md`.

## Release and docs
Release process: `docs/release/runbook.md` and `.claude/skills/release/` (owner-signed tag, two
approvals `release` then `pypi`, curated notes from the `CHANGELOG.md` `[Unreleased]` section;
the plugin installs from the `release/claude` branch). User documentation has one source,
`docs/user/` (templates in `site/`, built by `scripts/build_site.py`, `docs` extra); the site
deploys from release tags. Dev install: `claude --plugin-dir .` and `pip install -e ".[dev,docs]"`.

## Layout
`token_sparen/` (core, stdlib only: `store/ compress/ mechanisms/ learn/ replay/ adapters/
testing/`; `learn/` holds policy, decision log, engine, regret, `overlay` (session fold), `breaker`
and `ope`; `replay/` holds E1, its parallel lane runner and the private report writer), `token_sparen/params/defaults.toml`
(all tunables), `hooks/`, `skills/token-sparen/` (the plugin's `/token-sparen` skill; no
`commands/`), `evals/e2/` (fact-preservation suite), `evals/challengers/` (offline learners for the replay's
`sim` lane), `tests/` (mirrors the package, synthetic
fixtures only), `scripts/`. `facts/` (M3) holds the repo-fact sanitiser, the fold-time miners (`facts.json`), the per-fact lifecycle bandit and SessionStart injection.

## Commands (use the repo `.venv`; all must pass before a commit)
```
pytest
ruff check .
ruff format --check .
mypy --strict token_sparen
mypy --strict bench
python evals/e2/run.py --strict
TOKEN_SPAREN_PERF_FACTOR=1.5 pytest -m perf --no-cov
python scripts/check_wheel.py
python scripts/check_denylist.py
```

## Invariants
- Python >= 3.11, runtime stdlib only.
- Hooks fail open (no-op response plus one line in `~/.token-sparen/logs/errors.jsonl`); non-LLM p95 < 50 ms.
- Outputs under 2000 estimated tokens are never modified (`min_tokens` is floored at 2000); error lines (`is_error_line`: `ERROR_PATTERNS`; a stack-frame head only when it matches outside its file location) are never elided.
- Never larger: a replacement must not exceed the host's preview of the output.
- Failed tool results are not replaceable on Claude Code (only `additionalContext`, e.g. the loop guard); no PreToolUse command rewriting (pre-trim is a PostToolUse arm).
- Mechanisms and compressors never import `token_sparen.adapters`.
- Every elision carries the recovery marker and the full text is saved under `<project>/.token-sparen/outputs/`.
- `TOKEN_SPAREN_DISABLE=1` disables every hook.
- Config precedence: defaults < user config < project `token-sparen.toml` < env `TOKEN_SPAREN_*` < session overrides (`token-sparen config set`). Project file and env may only narrow (`PROJECT_POLICY`; `sanitise_project` / `sanitise_env`).
- The project file is deny-by-default: it may only make token-sparen intervene less or keep more,
  and an unlisted key is user-only (`PROJECT_POLICY` in `config.py`).
- Quality first: file contents are never trimmed by default (`compression.trim_file_contents`
  false) and only `compression.allow_ctypes` are offered; the user may widen, a project may narrow.
- `[learn.hold] compression = true` stops every output trim (pre-trim included), with learning on
  or off; `allow_ctypes = []` is the config-only switch.
- State root `~/.token-sparen/` (`TOKEN_SPAREN_HOME`); project key is a hash, never a raw path in
  shared stores (user-local bookkeeping may hold paths).
- Settings writes are opt-in, backed up, reversible and documented.
- Quality is a constraint, never a weighted term. Never tune on the test split.
- Reimplement, never copy code from references or competitors.

## Never commit private data
No employer or organisation names, internal URLs, people, local absolute paths, transcript ids or
private figures. Private inputs (the owner's local sessions) and E1/E3 reports stay outside the
repo (`--out` must be outside any git tree); never commit numbers derived from them.
Run `python scripts/check_denylist.py` before every commit.

## Process
Elephant-Goldfish: draft spec/plan (no code) -> cold zero-context review for gaps -> resolve ->
TDD implementation -> drift report comparing code to spec -> self-review. Role prompts live in
`.claude/agents/*.md` and `.claude/skills/*/SKILL.md`; they are plain Markdown and usable by any host.
Conventional commits. Record decisions in `MEMORY.md` with a date.
