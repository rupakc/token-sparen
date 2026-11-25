# Contributing

Thanks for your interest in token-sparen.

## Development setup

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Checks (all must pass)

```
pytest -q --cov=token_sparen
ruff check . && ruff format --check .
mypy --strict token_sparen
python scripts/check_denylist.py
```

## Guidelines

- Python 3.11+; the runtime core is standard library only.
- Write a failing test first, then the implementation.
- Use conventional commit messages (`feat:`, `fix:`, `chore:`, `docs:`, ...).
- Never commit local paths, credentials, or private identifiers. The deny-list
  check enforces this; extra private words can be supplied locally through the
  `TOKEN_SPAREN_DENYLIST` environment variable (comma-separated).
