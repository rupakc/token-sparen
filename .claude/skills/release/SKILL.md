---
name: release
description: Use when cutting a token-sparen release, bumping the version and publishing the plugin.
---
# Release
1. Gate: `pytest` (coverage >= 90 %), `ruff check .`, `ruff format --check .`,
   `mypy --strict token_sparen`, `python evals/e2/run.py` = 100 %, `python scripts/check_denylist.py`.
2. Plugin checks: hook commands use only `${CLAUDE_PLUGIN_ROOT}`; no top-level `bin/`; no unpinned
   `npx`/`uvx`; no non-image file > 256 KiB.
3. Quality gate: published results must be non-inferior at -5 pp (CI shown) for the released hash.
4. Bump the version in `pyproject.toml` and `token_sparen/__init__.py` together; update `CHANGELOG.md`.
5. Commit `chore(release): vX.Y.Z`, tag `vX.Y.Z`, push; the release workflow publishes.
6. Record the release and any open decisions in `MEMORY.md`.
