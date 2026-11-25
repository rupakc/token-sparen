---
name: add-compressor
description: Use when adding or changing an output compressor in token_sparen/compress/ for a new tool-output type.
---
# Add a compressor
1. Read `token_sparen/compress/base.py` and one existing compressor.
2. Create a synthetic fixture under `tests/fixtures/` (no private data) and a failing golden test.
3. Add E2 cases: corpus entry under `evals/e2/corpus/` and its needles in `evals/e2/needles.toml`
   (failing test name, error line, key field). Needles must survive or be reachable via the marker.
4. Implement the compressor (stdlib only); register it in `compress/detect.py`; put tunables in
   `params/defaults.toml`.
5. Verify: `pytest`, `ruff check .`, `ruff format --check .`, `mypy --strict token_sparen`,
   `python evals/e2/run.py` (must be 100 %), `python scripts/check_denylist.py`.
6. Commit `feat(compress): <name>`.
Invariants: never elide `ERROR_PATTERNS` lines; skip outputs under 2000 estimated tokens; always
append the recovery marker and save the full output.
