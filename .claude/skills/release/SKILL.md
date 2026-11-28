---
name: release
description: Use when cutting a token-sparen release (bump, owner-signed tag, two approvals, post-release checks) or resuming one that failed part-way.
---
# Release

The procedure of record is `docs/release/runbook.md`; the automation is
`.github/workflows/release.yml`. Read the runbook before acting. Never create, sign or push the
`vX.Y.Z` tag yourself and never approve the `release` or `pypi` environments: those are **owner**
actions (the tag is an owner-signed annotated tag).

1. **Preconditions.** The runtime is frozen and the freeze commit is the one the live benchmark
   measured. `python scripts/render_evidence.py --check` reports ok, or
   `docs/release/gate-decision.toml` holds a valid, signed gate decision (an `underpowered`
   verdict plus the owner's decision file is the only override; a failed gate blocks).
   Locally: `pytest`, `ruff check .`, `ruff format --check .`, `mypy --strict token_sparen`,
   `python evals/e2/run.py --strict`, `python scripts/check_denylist.py`.
2. **Curate notes.** `CHANGELOG.md` `[Unreleased]` is a user-facing summary (what you get, what
   changed, upgrade notes) with no measured figures; milestone history stays below
   `<!-- pre-1.0 milestones -->`. `python scripts/release_notes.py --version X.Y.Z` must pass on
   the bumped tree (a result figure outside the Evidence table is refused).
3. **Freeze, measure, prepare** (order of record in the runbook's *Cutting a release*): the
   version bump `python scripts/bump_version.py X.Y.Z` comes **before** the `vX.Y.Z-rc1` freeze
   the benchmark measures (the plugin hash includes the version); after the evidence is
   committed (first release only: the owner seeds `release/claude` from the rc tree first),
   `python scripts/bump_version.py --prepare-release X.Y.Z YYYY-MM-DD` dates the
   heading and points the marketplace at `release/claude` (release_check refuses otherwise).
   Commit as `chore(release): vX.Y.Z`, push `main`, wait for CI to be green, and run
   `python scripts/release_check.py --mode tag --tag vX.Y.Z` locally.
4. **Owner signs and pushes the tag** (`git tag -s vX.Y.Z`, `git push origin vX.Y.Z`).
5. **Owner approves `release`** (provenance, `release/claude` updated), **then `pypi`** (CLI
   published, release un-drafted once PyPI's hashes match).
6. **Post-release checks.** `release/claude` plugin hash equals the one in the notes; the
   marketplace entry resolves to `release/claude`; `pipx install token-sparen==X.Y.Z` works in a
   fresh environment; the docs site shows the release.
7. **Resume rule.** After a failure use "Re-run failed jobs" only (the `dist` artifact is
   reused); a full re-run is allowed only before `publish-pypi` succeeded. See the runbook.
8. Record the release and any open decisions in `MEMORY.md`.
