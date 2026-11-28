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

`verify-tag` (the reusable `.github/workflows/verify-tag.yml`) checks the tag twice: GitHub must
report a verified signature by the tagger email in `RELEASE_TAGGER`, and git must verify the
signature with **only** the public key in `RELEASE_SIGNING_KEY` trusted
(`scripts/release_trust.py`); the tagged commit must be on `main` with the subject
`chore(release): vX.Y.Z`. `build` trusts the same single key when `release_check --mode tag`
checks the signature on `docs/release/gate-decision.toml`.

`push-tree` only ever moves `release/claude` forward: it refuses when the branch already holds
the tag's version or a newer one (`scripts/push_tree_guard.py`, so re-running an old release's
failed `push-tree` can never downgrade users), and it pushes a fast-forward of the current head
without force.

**The docs site and wiki go live before the approvals.** The same tag push also starts
`.github/workflows/docs.yml`:

| Job | Needs approval | Writes |
|---|---|---|
| `tag` | no | nothing (picks the tag; exact `vX.Y.Z` on `main` only) |
| `verify` | no | nothing (the same `verify-tag.yml` as above) |
| `build` | no | the site and wiki artifacts (deny-list scanned) |
| `deploy-pages` | the `github-pages` environment (tag rule only, no reviewer) | the public site |
| `wiki` | no | the repository wiki |

Nothing is built or deployed unless `verify` passes, so only an owner-signed release tag
publishes docs. The docs jobs do not wait for `release_check` or for the `release`/`pypi`
approvals: push the tag only when `release_check --mode tag` already passes locally (step 6
below). If a release then stops, the site and wiki describe a version that is not installable;
re-run `docs.yml` by dispatch from the previous release tag to put them back.

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

This is the procedure of record, in order. Two facts fix the order:

- The plugin hash covers `.claude-plugin/plugin.json`, **its version included**
  (`scripts/plugin_hash.py`). The release gate compares the evidence's hash with the hash of the
  tree being released, so the commit the benchmark measures must already carry the release
  version. A version bump after the benchmark breaks the match (and a second bump to the same
  version is refused).
- Only files outside the hash may change between the measured commit and the release commit:
  the CHANGELOG date, the README Evidence block, `docs/evidence/`,
  `docs/release/gate-decision.toml` and `.claude-plugin/marketplace.json`.

So the order is: **freeze at the release version → benchmark campaign → evidence committed →
release-prep commit → signed tag.**

### A new minor or major release (`X.Y.0`, needs its own E4 run)

1. **Freeze.** On `main`, curate `CHANGELOG.md` `[Unreleased]` (no measured figures), then
   `python scripts/bump_version.py X.Y.0` (writes `## [X.Y.0] - Unreleased`). Commit as
   `chore: freeze X.Y.0`, push, wait for CI. `python scripts/release_check.py --mode dry-run`
   passes. From here on nothing under `.claude-plugin/plugin.json`, `hooks/`, `skills/` or
   `token_sparen/` changes.
2. **RC tag.** `git tag -s vX.Y.0-rc1 -m vX.Y.0-rc1 <that commit>`, push it. An RC tag never
   starts a release (the workflows match only `vX.Y.Z`).
3. **Benchmark campaign.** Follow [benchmark-runbook.md](benchmark-runbook.md): screening, the
   confirmation frozen at `--ref vX.Y.0-rc1`, the gate, and *Publish*, which writes
   `docs/evidence/e4.json` and the report into the main checkout.
4. **Evidence committed.** In the main checkout: `python scripts/render_evidence.py --write
   README.md`, run the full battery, commit the evidence and the README, push. If the gate
   verdict is `failed`: no release. If it is `underpowered`: decide in
   `docs/release/gate-decision.toml` (`version` = `X.Y.0`, `decision` = `ship` or `hold`, a
   non-empty `rationale`, ISO `date`, `e4_id` = the first 12 hex of the sha256 of
   `docs/evidence/e4.json`) and commit it **signed by your release key** (`git commit -S`).
5. **Release prep.** `python scripts/bump_version.py --prepare-release X.Y.0 YYYY-MM-DD`. It
   dates the CHANGELOG heading and points `.claude-plugin/marketplace.json` at the
   `release/claude` branch (`{"source": "github", "repo": "rupakc/token-sparen", "ref":
   "release/claude"}`). Until the first release's prep commit the marketplace entry on `main`
   is `./`, which installs the development head of `main`. From this commit on it installs the
   gated tree for good. `release_check` refuses a release without it, and so does
   `tests/test_release_consistency.py::test_marketplace_tracks_release_branch` once a dated
   release heading exists. Commit as exactly `chore(release): vX.Y.0`, push, wait for CI.
6. **Check locally:** `python scripts/release_check.py --mode tag --tag vX.Y.0` must pass on
   that commit with a clean work tree (your git must trust your own signing key for the
   decision-file check).
7. **Signed tag.** `git tag -s vX.Y.0 -m vX.Y.0 <that commit>` and `git push origin vX.Y.0`.
8. When the draft release exists, approve **`release`**: provenance is attested and
   `release/claude` is updated, so the plugin is released.
9. Approve **`pypi`**: the CLI is published; `finalize` checks PyPI's sha256 against
   `SHA256SUMS` and publishes the GitHub Release (marked *latest* only if it is the highest
   `vX.Y.Z`).
10. **Open the next version.** `python scripts/bump_version.py X.Y.1.dev0`, commit
    `chore: open X.Y.1.dev0`, push. The marketplace keeps tracking `release/claude`. Record the
    release in `MEMORY.md`.

Denying `pypi` after `release` leaves the plugin released and the GitHub Release a draft until
`pypi` is approved or the owner publishes the draft by hand.

### A patch release (`X.Y.Z`, Z > 0)

A patch may reuse the `X.Y.*` evidence only if, between the measured commit (the `rc` tag the
evidence names) and the release commit, nothing changed except non-behaviour files: `docs/`,
`tests/`, `bench/`, `evals/`, `scripts/`, `README.md`, `CHANGELOG.md`, `LICENSE`, `NOTICE`,
`SECURITY.md`, `CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md`, `MEMORY.md`,
`.claude-plugin/marketplace.json` and the version line of `token_sparen/__init__.py` and
`.claude-plugin/plugin.json`. Everything else counts as behaviour, `.github/`, `pyproject.toml`
and every file of the shipped plugin included. The reused evidence must also be `passed`, or
`underpowered` with a signed `ship` decision for it. The README Evidence must be the one
`render_evidence` wrote for it, and the `X.Y.Z` CHANGELOG section must have a
`### Runtime changes` list (`- none` for a docs-only patch).

- **Docs-only patch:** `bump_version.py X.Y.Z`, `--prepare-release X.Y.Z YYYY-MM-DD`, commit
  `chore(release): vX.Y.Z`, then steps 6 to 10.
- **Behaviour changed:** this is a measured release. Freeze at `X.Y.Z`, tag `vX.Y.Z-rc1` and
  follow steps 1 to 10. Once `docs/evidence/e4.json` measured `X.Y.Z` itself,
  `release_check` applies the full gate, as for `X.Y.0`.

## After a failure

Use **"Re-run failed jobs"** only: the `dist` artifact is reused, so hashes stay the same; PyPI
uses `skip-existing` and `finalize` still compares hashes. A full re-run of a tag is allowed only
before `publish-pypi` succeeded (a rebuilt archive would differ from the one on PyPI).
`push-tree` is idempotent (a re-run finds the same tree and message and pushes nothing),
refuses if `release/claude` moved while it ran, and refuses once a newer release holds the
branch (never re-run an old release's `push-tree` to "fix" a newer one).

## Build tools

The release build installs hash-pinned tools from `requirements/release.txt` (no pip cache) and
builds without isolation, so the published archives use the pinned backend. Regenerate the file
with the command in its header when the tools need an update.
