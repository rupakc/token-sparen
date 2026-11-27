# Release runbook (maintainer)

How a token-sparen release is cut and what the repository owner sets up once. The workflow is
`.github/workflows/release.yml`; the release skill points here. Every step marked **owner** is a
manual action on an account the owner controls; automation never does it.

## What the workflow does

A release is an owner-signed annotated tag `vX.Y.Z` (exactly; RC and milestone tags such as
`v1.0.0-rc1` or `v0.4.0-m4` never match) on a `main` commit whose subject is
`chore(release): vX.Y.Z`.

| Job | Runs on | Needs approval | Writes |
|---|---|---|---|
| `gate` | tag push and dispatch | no | nothing (reusable CI) |
| `verify-tag` | tag push | no | nothing |
| `build` | tag push and dispatch | no | the `dist` artifact (30 days) |
| `draft-release` | tag push | no | a **draft** GitHub Release with the archives |
| `provenance` | tag push | **`release`** | build-provenance attestations |
| `push-tree` | tag push | (after `release`) | a fast-forward commit on `release/claude` |
| `publish-pypi` | tag push | **`pypi`** | the wheel and sdist on PyPI (trusted publishing) |
| `finalize` | tag push | (after `pypi`) | un-drafts the release once PyPI's hashes match |

A manual dispatch (`workflow_dispatch`, no inputs) is a dry run: only `gate` and `build` run.

`verify-tag` checks the tag twice: GitHub must report a verified signature by the tagger email in
`RELEASE_TAGGER`, and git must verify the signature with **only** the public key in
`RELEASE_SIGNING_KEY` trusted (`scripts/release_trust.py`). `build` trusts the same single key
when `release_check --mode tag` checks the signature on `docs/release/gate-decision.toml`.

## One-time setup (owner)

1. **Signing key.** Create an SSH (recommended) or OpenPGP signing key, register it on the GitHub
   account as a *signing* key, and make sure the tagger email is verified on the account. Check
   locally with a throw-away tag: `git tag -s vtest -m test`, `git verify-tag vtest`, then
   `git tag -d vtest` (never push it).
2. **Repository variables** (Settings → Secrets and variables → Actions → *Variables*):
   - `RELEASE_TAGGER`: the email used as tagger on release tags (compared case-insensitively).
   - `RELEASE_SIGNING_KEY`: the *public* key: one SSH line (`ssh-ed25519 AAAA…`) or an
     ASCII-armoured OpenPGP public key with exactly one primary key and a user id carrying the
     `RELEASE_TAGGER` email.

   Why variables, not a file in the repository: a key file would be read from the tagged commit
   itself, so anyone able to land a commit on `main` could swap the key in the very commit they
   tag. On a personal repository only the owner can edit Actions variables. A public key needs no
   secret. A missing or malformed variable fails the release (closed).
3. **Secret** `TOKEN_SPAREN_DENYLIST` (comma-separated private words): the release build runs
   `check_denylist --require-private-words` and fails without it.
4. **Environments** (Settings → Environments): create `release` and `pypi`. For each: required
   reviewer = the owner; leave "Prevent self-review" off (the owner is the only reviewer);
   deployment branches and tags = *Selected*, with one **tag** rule `v*` and no branch rule.
5. **PyPI trusted publisher** (pypi.org → Account → Publishing → add a *pending* publisher):
   project `token-sparen`, owner `rupakc`, repository `token-sparen`, workflow `release.yml`,
   environment `pypi`. Optional rehearsal: the same on TestPyPI.
6. **Rulesets** (Settings → Rules → Rulesets):
   - **Tags `v*` (load-bearing):** only the owner may create them (no bypass for other roles);
     block updates and deletions. This rule matters: whoever can push a `v*` tag runs *that
     commit's own* `release.yml`, and its jobs outside an environment (`draft-release` with
     `contents: write`) are not approval-gated. The environments' `v*` tag rule does not replace
     it.
   - **Branch `release/claude`:** block force pushes and deletion. Do *not* restrict updates: the
     workflow's `GITHUB_TOKEN` fast-forwards it. Only `push-tree` ever writes this branch.
7. **Rehearsal** (recommended before the first release): run the workflow by dispatch on `main`
   (only `gate` and `build` run). The tag-only jobs cannot run in a dispatch; to exercise them,
   run one release in a throw-away fork with its own environments, variables and a TestPyPI
   publisher.

## Cutting a release (owner)

1. On `main`: bump and date the version (`scripts/bump_version.py`, then `--set-date`), commit
   as `chore(release): vX.Y.Z`, push, wait for CI to be green.
2. `git tag -s vX.Y.Z -m vX.Y.Z <that commit>` and `git push origin vX.Y.Z`.
3. When the draft release exists, approve **`release`**: provenance is attested and
   `release/claude` is updated, so the plugin is released.
4. Approve **`pypi`**: the CLI is published; `finalize` checks PyPI's sha256 against
   `SHA256SUMS` and publishes the GitHub Release (marked *latest* only if it is the highest
   `vX.Y.Z`).

Denying `pypi` after `release` leaves the plugin released and the GitHub Release a draft until
`pypi` is approved or the owner publishes the draft by hand.

## After a failure

Use **"Re-run failed jobs"** only: the `dist` artifact is reused, so hashes stay the same; PyPI
uses `skip-existing` and `finalize` still compares hashes. A full re-run of a tag is allowed only
before `publish-pypi` succeeded (a rebuilt archive would differ from the one on PyPI).
`push-tree` is idempotent (a re-run finds the same tree and message and pushes nothing) and
refuses if `release/claude` moved while it ran.

## Build tools

The release build installs hash-pinned tools from `requirements/release.txt` (no pip cache) and
builds without isolation, so the published archives use the pinned backend. Regenerate the file
with the command in its header when the tools need an update.
