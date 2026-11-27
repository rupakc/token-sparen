# token-sparen M6 — Docs, wiki, site, release v1.0 and distribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship token-sparen **v1.0.0 (Claude Code)**: one version source, a tag-driven release that builds, checks, attests and publishes (PyPI behind a manual approval), one docs source rendered to the gh-pages site and the GitHub Wiki, a README whose Evidence section cites only public E4 results, a security and privacy review, a triage of the deferred minors, a cold whole-repo review — and a prepared, owner-run distribution (Anthropic plugin directory, PyPI project, GitHub settings).

**Architecture:** `token_sparen/__init__.py` holds the only hand-edited version; `scripts/bump_version.py` writes it into `plugin.json` and the CHANGELOG heading, and `tests/test_release_consistency.py` fails on any drift. User docs live once, in `docs/user/` (ordered by `docs/user/nav.toml`); `scripts/build_site.py` renders them with `site/` templates into `_site/` (Pages), `scripts/build_wiki.py` renders the same files into wiki pages. A release is an owner-signed tag `vX.Y.Z` → `.github/workflows/release.yml`: CI gate → build wheel/sdist → `check_wheel` → provenance → draft GitHub Release with notes from the CHANGELOG → PyPI Trusted Publishing in the approval-gated `pypi` environment → finalise the release and push the generated `release/claude` branch (the plugin-only tree the directory tracks).

**Tech Stack:** Python ≥ 3.11 stdlib only at runtime (unchanged). Dev/docs only: `Markdown` (Python-Markdown, pinned, new `docs` extra), `twine` and `pip-audit` (dev extra). GitHub Actions: `actions/checkout`, `actions/setup-python`, `actions/upload-artifact`, `actions/download-artifact`, `actions/configure-pages`, `actions/upload-pages-artifact`, `actions/deploy-pages`, `actions/attest-build-provenance`, `pypa/gh-action-pypi-publish` — every one pinned to a full commit SHA with the tag in a comment.

**Spec:** `docs/specs/2026-11-25-token-sparen-design.md` (§10.5 claims policy and release gate, §11 repo/CI/release/docs, §12 distribution, §14 M6, §15 future work). Format and history: `docs/plans/2026-11-27-token-sparen-m4.md`, `docs/reports/drift-m4.md`, `MEMORY.md`. M5 (E4) runs in parallel and produces the evidence this plan only renders.

**Out of M6 (recorded deferrals):** per-host zips, npm/OpenCode shim, Gemini/Qwen/Codex manifests and host-matrix CI → M7 (v1.1), as §14 already says; an npm package for v1.0 is **not** recommended (no consumer: Claude Code installs from git via the marketplace and the CLI from PyPI; a name-reserving placeholder is not worth the maintenance) — revisit with the OpenCode shim. Release notes come from the curated CHANGELOG, not generated from conventional commits (spec §11 deviation, recorded in Task 10). A CycloneDX SBOM is not produced: the runtime has zero dependencies (asserted by a test) and the wheel/sdist get build provenance plus PyPI's default PEP 740 attestations instead.

## Global Constraints

- Everything in the M0–M4 global constraints still binds (CLAUDE.md "Invariants", "Never commit private data").
- **No private data anywhere M6 writes** — docs, site, wiki, release notes, workflow logs, commit messages: no organisation names, local absolute paths, people other than the declared author, transcript ids, or E1/E3 figures. `python scripts/check_denylist.py` runs on the repo **and** on every generated tree (`_site/`, the wiki checkout, release notes, the `release/claude` tree) before it is published.
- **Evidence = E4 only.** A savings or quality number appears in README/site/wiki only through `scripts/render_evidence.py` from `docs/evidence/e4.json`, and only with CI, n (tasks), seeds, model, date and plugin hash (spec §10.5). Never an E1, E2-derived savings or E3 figure. Until M5 publishes, the Evidence section says there are no published results.
- **Release gate (spec §10.5, release skill step 3):** v1.0.0 is tagged only when `docs/evidence/e4.json` exists, its `plugin_hash` equals `scripts/plugin_hash.py` on the release commit, and the resolved-rate difference is non-inferior at −5 pp. Otherwise the controller stops and asks the owner (no implicit override).
- **Runtime freeze:** after the commit whose plugin hash M5's confirmation run measures, no M6 task changes a file counted by `plugin_hash.py`. Runtime fixes (Tasks 2, 3) land before that point or move to v1.0.1.
- **Owner actions are never automated.** Anything that creates an account-level resource, publishes to a third party, signs, or approves (PyPI project/pending publisher, GitHub environments/Pages/wiki/secret scanning, tag signing, environment approvals, directory submission, any npm action) is an **OWNER ACTION** checkpoint: the controller prepares exact instructions, stops, and resumes only after the owner confirms.
- Workflows: top-level `permissions: {}` (release) or `contents: read` (ci, docs); job-level grants only where needed; `id-token: write` only in the PyPI, provenance and Pages jobs; no `pull_request_target`; no secrets other than `TOKEN_SPAREN_DENYLIST`.
- Version strings: PEP 440 in Python, identical string in `plugin.json`; release tags match `^v\d+\.\d+\.\d+$` exactly (milestone tags such as `v0.4.0-m4` never trigger a release).

## Review Focus

1. **Tag and version disagree** (tag `v1.0.0`, package still `0.1.0.dev0`, or `plugin.json` stale): the release must refuse before building anything. → Task 8 `test_release_check_refuses_tag_version_mismatch`, `test_release_check_refuses_stale_plugin_json`.
2. **Approval rejected or a job fails midway** (PyPI approval denied, network error after the draft release): nothing public changes except a draft release; a re-run of the same tag resumes idempotently (draft reused, assets replaced, `release/claude` pushed only after PyPI). → Task 9 `test_release_job_order_publishes_last`, `test_draft_release_step_is_idempotent`.
3. **Private data in generated output** (a CHANGELOG line, a doc page or the wiki sidebar carrying a local path or a figure): the build fails before upload. → Task 4 `test_generated_trees_pass_denylist`, Task 7 `test_evidence_renderer_rejects_non_e4`.
4. **Links break in translation** (relative `.md` links and anchors work on GitHub, the site and the wiki differently): every internal link resolves in all three. → Task 5 `test_site_internal_links_resolve`, Task 6 `test_wiki_links_resolve_to_pages`.
5. **The directory sees a different plugin than users install** (tests, evals, `.gitattributes`, files > 256 KiB or > 512 files in the tracked tree; hook paths outside `${CLAUDE_PLUGIN_ROOT}`): the `release/claude` tree is checked against the directory's rules and smoke-loaded. → Task 8 `test_claude_tree_meets_directory_limits`, `test_claude_tree_hooks_run`.

---

### Task 1: Version single source of truth and consistency test

**Files:** Create `scripts/bump_version.py`, `tests/test_release_consistency.py`; modify `pyproject.toml` (`dynamic = ["version"]`, `[tool.setuptools.dynamic] version = {attr = "token_sparen.__version__"}`, setuptools floor `>=77` for the SPDX licence field — deferred minor), `.claude-plugin/marketplace.json` (no `version` in the plugin entry: `plugin.json` is authoritative), `tests/test_meta.py` (`test_cli_version_prints` asserts `token_sparen.__version__`, not a literal).

**Interfaces (produces):**
- `scripts/bump_version.py`: `bump(root: Path, new: str, *, date: str) -> list[Path]` (returns changed files) and `main(argv) -> int` (`python scripts/bump_version.py 1.0.0 --date 2026-MM-DD`). Validates `new` against PEP 440 *and* `^\d+\.\d+\.\d+(\.dev\d+)?$`; writes `token_sparen/__init__.py`, `.claude-plugin/plugin.json` (`version`), and, for a non-dev version, turns `## [Unreleased]` into `## [Unreleased]\n\n## [X.Y.Z] - DATE` (content below moves into the new section). Refuses a version not greater than the current one.
- `tests/test_release_consistency.py`: `current_version() -> str` (helper reused by Tasks 7–9).

- [ ] **Step 1:** Failing tests: `test_versions_agree` (`__init__`, built metadata via `importlib.metadata.version` when installed, `plugin.json`), `test_marketplace_entry_has_no_version`, `test_changelog_heading_matches_release_version` (non-dev → top released heading is `## [X.Y.Z] - YYYY-MM-DD`; dev → `## [Unreleased]` exists), `test_readme_and_security_have_no_stale_version` (no `\d+\.\d+\.\d+(\.dev\d+)?` in README.md/SECURITY.md other than the current one), `test_bump_round_trip` (tmp copy: bump to `1.0.0` changes exactly the three files; second bump to `1.0.0` refused), `test_bump_rejects_non_semver_pep440` (`1.0`, `1.0.0rc1`, `v1.0.0`).
- [ ] **Step 2:** `pytest tests/test_release_consistency.py -v` → FAIL. **Step 3:** implement. **Step 4:** full battery (CLAUDE.md "Commands") incl. `python scripts/check_wheel.py` (dynamic version must reach the wheel). **Step 5:** commit `chore(release): single version source and consistency test`.

### Task 2: Deferred-minors triage (rule, fix wave, public record)

**Files:** Create `docs/reports/m6-minors-triage.md`; modify per item (runtime files only before the runtime freeze); `MEMORY.md`.

**Sources:** every "minor (deferred)" line in `.superpowers/sdd/*/progress.md` plus the "Deferred minors (review list)" in `MEMORY.md` (M4 final review). Items already fixed (e.g. M4 Task 1 (a)–(e), setuptools floor in Task 1, `ci.yml` permissions in Task 3) are marked `done` with the commit.

**Rule (fix before 1.0 if any holds, else defer to v1.0.x/v1.1 with a one-line reason):**
1. It can lose or corrupt user data, or makes recovery (`show`, marker path) fail.
2. It can break the fail-open boundary (an uncaught exception class in a hook path, e.g. `config.state_home` `Path.home()` outside the try; non-UTF-8 store `_load`; `json` `RecursionError`) or make a hook exceed its latency budget.
3. It weakens a security/privacy property (settings writes, project-config deny-by-default, fact sanitiser, state-root ownership, newline injection into rendered headers).
4. It makes a documented, user-visible statement false (README, `doctor`, `stats`, marker text) on a supported platform (macOS, Linux; Windows as documented).
5. It is a test that cannot fail (e.g. a tautological `or True` assertion) guarding one of 1–4.
Never in scope: anything that changes a tuned parameter in `token_sparen/params/defaults.toml`, offline-evaluation methodology items (lane counting, harness statistics: documented limitations, v1.1), and pure refactors. Items 1–5 that would land after the runtime freeze go to v1.0.1 unless the owner re-runs the M5 confirmation.

- [ ] **Step 1:** Write the triage table in `docs/reports/m6-minors-triage.md`: `item | source (milestone/task) | rule hit (1–5 or none) | decision (fix / defer v1.0.x / defer v1.1 / done) | test name`. No figures, no ledger paths. Controller rules on borderline items before Step 2.
- [ ] **Step 2:** For each `fix` item: failing test first (named in the table), then the minimal fix; one commit per cohesive group, `fix: M6 triage — <group>`.
- [ ] **Step 3:** Full battery; E2 strict = 100 %; perf job budgets unchanged.
- [ ] **Step 4:** `MEMORY.md` entry "2026-MM-DD M6 minors triage" (rule, counts per decision category only as words "fixed / deferred", pointer to the doc). Commit `docs: M6 minors triage record`.

### Task 3: Security and privacy review (threat model, supply chain, settings-write safety)

**Files:** Create `docs/user/privacy-security.md` (threat model; also a wiki/site page), `.github/dependabot.yml`, `tests/test_supply_chain.py`; modify `.github/workflows/ci.yml` (top-level `permissions: contents: read`, `pip` cache, `on: workflow_call` with `secrets: TOKEN_SPAREN_DENYLIST` optional, all `uses:` pinned to SHAs, new job `supply-chain`), `SECURITY.md` (supported versions: latest 1.x; disclosure via private vulnerability reporting; what is in scope), `tests/adapters/claude/test_settings.py`, `tests/test_config_safety.py` (gap tests only).

**Threat model content (decided):** assets — user source and tool outputs, recovery files, `~/.token-sparen/`, Claude Code settings files, the user's git state; adversaries — a malicious repository (committed `token-sparen.toml`, `.claude/settings.json` env vars, crafted file names), malicious tool output/prompt injection aimed at facts, a malicious extension, another local user on a shared host, a compromised dependency/action/release pipeline; for each: entry point → mitigation → code location (module/function, no line numbers) → test that pins it. Explicit non-goals: protecting secrets that a command prints (recovery files store them; README says so), defending against a compromised host process. Settings writes: statusline install and tool-pruning deny rules — opt-in, journaled, backed up, reversible, refuse symlinks/invalid JSON/non-finite constants, deny rules only into untracked and git-ignored files; the doc states this is the only place token-sparen changes Claude Code permission settings (the directory's security scan checks exactly this).

**Supply-chain decisions:** runtime dependencies stay empty; GitHub Actions pinned by SHA and updated by Dependabot (`github-actions` weekly, `pip` dev extra weekly); `supply-chain` CI job runs `pip-audit` on the dev+docs extras (fails on known vulnerabilities) and the tests below.

- [ ] **Step 1:** Failing tests in `tests/test_supply_chain.py`: `test_runtime_dependencies_empty` (`pyproject` `dependencies == []`), `test_actions_pinned_by_sha` (every `uses:` in `.github/workflows/*.yml` is `./…` or `owner/repo@<40 hex>`), `test_workflows_least_privilege` (each workflow declares top-level `permissions`; `id-token: write` occurs only in jobs named `publish-pypi`, `provenance`, `deploy-pages`; no `pull_request_target`), `test_no_unpinned_launchers_in_plugin` (no `npx|uvx|pipx run|uv run|bunx|dlx` in `hooks/`, `skills/`, `.claude-plugin/`), `test_hook_commands_only_plugin_root` (every hook command matches `^python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[a-z_]+\.py"( \w+)?$`).
- [ ] **Step 2:** Review each threat-model row against code; for each row without a pinning test, add one (candidates: `test_settings_write_refuses_tracked_settings_file`, `test_project_config_cannot_enable_pruning`, `test_state_home_env_inside_repo_ignored`, `test_fact_with_instruction_text_rejected`). A real gap found here is a rule-3 fix under Task 2's freeze rule.
- [ ] **Step 3:** Implement CI/dependabot/SECURITY.md changes; write the threat model. **Step 4:** full battery + `check_denylist`. **Step 5:** commit `docs(security): threat model and supply-chain hardening`.

### Task 4: Single docs source `docs/user/`

**Files:** Create `docs/user/nav.toml` and pages `index.md` (Home), `concepts.md`, `architecture.md`, `learning-engine.md`, `mechanisms/{compression,guards,read-dedupe,governor,boundary,subagent,facts,tool-pruning,loop-guard,statusline}.md`, `host-guides/claude-code.md` (other hosts: "planned for v1.1", one line), `configuration.md`, `extending.md`, `benchmarks-methodology.md` (E1–E4 methods, claims policy; links `docs/reports/m4-method.md`; Evidence via Task 7 include marker), `faq.md`, `troubleshooting.md` (+ `privacy-security.md` from Task 3); `tests/test_user_docs.py`.

**Interfaces (produces):**
- `nav.toml`: `[[page]] file = "concepts.md" title = "Concepts" wiki = "Concepts"` (order = navigation order; `wiki` = wiki page name, `[A-Za-z0-9-]+`); `[[page]]` may have `children`.
- Link rule: pages link to each other only by relative path to a `.md` in `docs/user/` (optionally `#anchor`), to repo files by repo-relative path, and to the outside by `https://`. The Evidence include marker is the line `<!-- evidence -->` (expanded by Task 7's renderer at build time).
- Content moves out of README (deep "How it learns", configuration reference, offline measurement) into these pages; README keeps the operational sections (Task 7). Prose derives from README, spec and method doc; no new claims; no figures except the fixed parameters already in README/spec.

- [ ] **Step 1:** Failing tests: `test_nav_lists_every_page_once`, `test_internal_links_resolve` (file and anchor, GitHub slug rules), `test_user_docs_have_no_figures_outside_evidence` (reuse `tests/test_docs.py::_violations` with an allow-list of parameter phrases), `test_generated_trees_pass_denylist` (runs `check_denylist.scan_text` over every page and, after Tasks 5–6 exist, over the built site and wiki trees in tmp).
- [ ] **Step 2–4:** fail → write pages → battery. **Step 5:** commit `docs: user documentation source (docs/user)`.

### Task 5: Site builder and GitHub Pages workflow

**Files:** Create `scripts/build_site.py`, `site/template.html`, `site/style.css`, `site/explorer.js`, `.github/workflows/docs.yml`, `tests/test_build_site.py`; modify `pyproject.toml` (`docs = ["Markdown==<pinned>"]`), `.gitignore` (`_site/`), `.github/workflows/ci.yml` (job `docs`: build site and wiki into tmp, run their tests).

**Decision:** Python-Markdown + own template (not MkDocs): one pure-Python dev dependency, no theme/plugin ecosystem to track, full control of the no-CDN, no-tracker page. No external scripts, fonts or analytics; site works without JS except the explorer.

**Interfaces:**
- `build_site(src: Path, out: Path, *, evidence: Path | None, version: str, commit: str) -> list[Path]`; CLI `python scripts/build_site.py --out _site [--evidence docs/evidence/e4.json]`. Rewrites `x.md` links to `x.html`, adds heading anchors matching GitHub's slugs, renders nav from `nav.toml`, landing page = `index.md` plus "where it shines" scenarios and before/after demos built from existing synthetic fixtures (`tests/fixtures/`, regenerated through the real compressors at build time — no hand-made numbers). Footer: version, short commit, "edit on GitHub" link.
- `explorer.js`: reads `evidence.json` (copied only if present) and lets the reader switch arm/metric; shows estimate with its CI, n, seeds, model, date, plugin hash; with no file it shows "No published benchmark results yet."
- `docs.yml`: on push to `main` touching `docs/user/**`, `site/**`, `scripts/build_*.py`, `docs/evidence/**`, plus `workflow_dispatch`; jobs `build` (contents: read; build, denylist over `_site`, `actions/upload-pages-artifact`), `deploy-pages` (`pages: write`, `id-token: write`, environment `github-pages`), `wiki` (Task 6).

- [ ] **Step 1:** Failing tests: `test_site_internal_links_resolve` (every `href` to a local file exists in `_site`), `test_site_has_no_external_resources` (no `<script src=`/`<link href=` with a scheme), `test_site_without_evidence_says_no_results`, `test_site_build_is_deterministic` (two builds byte-identical given the same commit string), `test_demo_blocks_come_from_compressors` (demo "after" text equals the compressor output for the fixture).
- [ ] **Step 2–4:** fail → implement → battery (`pip install -e ".[dev,docs]"`). **Step 5:** commit `feat(docs): static site builder and Pages workflow`.

### Task 6: Wiki generated from `docs/user/`

**Files:** Create `scripts/build_wiki.py`, `tests/test_build_wiki.py`; modify `.github/workflows/docs.yml` (job `wiki`).

**Interfaces:**
- `build_wiki(src: Path, out: Path, *, evidence: Path | None, commit: str) -> list[Path]`: one `<wiki name>.md` per nav page (`index.md` → `Home.md`), links rewritten to `[[Page Name]]`-compatible `Page-Name` / `Page-Name#anchor`, repo-file links to `https://github.com/rupakc/token-sparen/blob/main/<path>`, generated `_Sidebar.md` (nav order) and `_Footer.md` ("Generated from docs/user at <commit>; edit there, wiki edits are overwritten.").
- `wiki` job (needs `build`; `contents: write`): clone `https://x-access-token:${{ github.token }}@github.com/${{ github.repository }}.wiki.git`, delete tracked `*.md`, copy the build, denylist the tree, commit `docs: sync wiki from <short sha>` only if the diff is non-empty, push. If the clone fails (wiki not initialised) the job fails with a message naming OWNER ACTION O3.

- [ ] **Step 1:** Failing tests: `test_wiki_links_resolve_to_pages`, `test_wiki_has_sidebar_in_nav_order`, `test_wiki_footer_names_source`, `test_wiki_names_are_valid` (`[A-Za-z0-9-]+`, unique case-insensitively).
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(docs): wiki generated from docs/user`.

### Task 7: README polish and the Evidence section

**Files:** Create `scripts/render_evidence.py`, `scripts/plugin_hash.py`, `tests/test_evidence.py`; modify `README.md`, `docs/user/benchmarks-methodology.md`.

**Interfaces:**
- `scripts/plugin_hash.py`: `plugin_hash(root: Path) -> str` = sha256 hex over the sorted `(posix relative path, bytes)` of the plugin runtime set — `token_sparen/**` (`*.py`, `*.toml`, `*.sh`), `hooks/**`, `skills/**`, `.claude-plugin/plugin.json` — with the version string replaced by `0.0.0` in `token_sparen/__init__.py` and `plugin.json` (so the bump does not change the hash), CRLF normalised to LF. CLI prints the hash. **M5 consumes this definition** for the hash it records; if M5 already fixed a different definition, M5's wins and this task adopts it (controller reconciles, MEMORY entry).
- `docs/evidence/e4.json` (written by M5, never by hand) required keys: `source == "E4"`, `suite`, `n_tasks`, `seeds`, `model`, `date`, `plugin_version`, `plugin_hash`, `script` (path under `bench/`), `arms`, `results[]` each `{metric, arm, baseline, estimate, ci95: [lo, hi], method}`, and one result `metric == "resolved_diff_pp"` with `margin_pp == -5` and `non_inferior: bool`.
- `render_evidence(path: Path | None) -> str` (Markdown block) and `check_release_gate(path: Path | None, root: Path) -> GateResult(ok: bool, reason: str)` (file present, schema valid, `plugin_hash == plugin_hash(root)`, `non_inferior is True`). CLI `--check` (exit 1 with reason) and `--write README.md` (replaces the block between `<!-- evidence:start -->` and `<!-- evidence:end -->`).
- No file → block reads: "No published benchmark results yet. Results will appear here only from the live benchmark (E4), each with its 95 % CI, number of tasks, seeds, model, date and the plugin hash it measured." Never mentions E1/E3 figures.

**README structure (decided):** title + one-paragraph pitch; Quick start; Status (v1.0, Claude Code; other hosts v1.1); **Evidence** (generated block); Requirements; Install (marketplace and `pipx install token-sparen` for the CLI); What changes in your session; Recovering trimmed output; Turning it off; What it writes to disk (unchanged substance, invariant); Settings it may change (statusline, tool pruning: opt-in, backed up, reversible — directory scan disclosure); Privacy (no network, no telemetry); Documentation (links to site, wiki, `docs/user`); Credits and licence. Deep sections move to `docs/user/` (Task 4) with links; README stays ≥ 40 words outside code and keeps every operational command.

- [ ] **Step 1:** Failing tests: `test_evidence_renderer_rejects_non_e4`, `test_evidence_requires_ci_n_seeds_model_date_hash` (each key removed → error naming it), `test_readme_evidence_block_matches_renderer`, `test_placeholder_without_results`, `test_release_gate_fails_on_hash_mismatch_or_inferior`, `test_plugin_hash_ignores_version_and_crlf`, `test_readme_keeps_operational_sections` (headings: Quick start, Evidence, Install, Recovering trimmed output, Turning it off, What it writes to disk, Settings it may change, Privacy).
- [ ] **Step 2–4:** fail → implement, rewrite README → battery. **Step 5:** commit `docs: README for v1.0 with generated Evidence section`.

### Task 8: Release tooling (checks, notes, `release/claude` tree)

**Files:** Create `scripts/release_check.py`, `scripts/release_notes.py`, `scripts/build_claude_tree.py`, `tests/test_release_tools.py`; modify `pyproject.toml` (dev: `twine`).

**Interfaces:**
- `release_check(tag: str, root: Path) -> list[str]` (problems; empty = ok): tag matches `^v\d+\.\d+\.\d+$` and equals `v` + `__version__` and `plugin.json` version; CHANGELOG has `## [X.Y.Z] - YYYY-MM-DD`; `render_evidence.check_release_gate` ok (skippable only with `--no-evidence-gate`, used by dry runs). CLI exits 1 printing each problem.
- `release_notes(changelog: Path, version: str) -> str`: the section body for `version` + a fixed footer (install lines, links to site, Evidence pointer, plugin hash); refuses an empty section.
- `build_claude_tree(root: Path, out: Path) -> list[Path]`: copies the plugin runtime set (same file set as `plugin_hash`) plus `README.md`, `LICENSE`, `NOTICE`, `SECURITY.md`, `CHANGELOG.md`, `.claude-plugin/marketplace.json`; excludes `tests/`, `evals/`, `docs/`, `scripts/`, `.claude/`, `.github/`, `.gitattributes`, `__pycache__`, `.DS_Store`. Checks (raise on failure): ≤ 512 files, no non-image file > 256 KiB, no symlinks, names valid on Windows and macOS, every hook command path under `${CLAUDE_PLUGIN_ROOT}`.

- [ ] **Step 1:** Failing tests: `test_release_check_refuses_tag_version_mismatch`, `test_release_check_refuses_stale_plugin_json`, `test_release_check_rejects_milestone_tag` (`v0.4.0-m4`), `test_release_notes_extracts_section`, `test_claude_tree_meets_directory_limits`, `test_claude_tree_hooks_run` (in the built tree with `CLAUDE_PLUGIN_ROOT` set and an isolated `TOKEN_SPAREN_HOME`, `hooks/run.py PostToolUse` on a synthetic large pytest payload emits the marker; `SessionStart` exits 0), `test_claude_tree_hash_equals_repo_hash`.
- [ ] **Step 2–4:** fail → implement → battery. **Step 5:** commit `feat(release): release checks, notes and plugin tree builder`.

### Task 9: Release workflow

**Files:** Create `.github/workflows/release.yml`, `tests/test_release_workflow.py`.

**Workflow (decided):** `on: push: tags: ['v[0-9]+.[0-9]+.[0-9]+']` and `workflow_dispatch` (input `dry_run`, default true: runs `gate`, `build`, `provenance` only, with `--no-evidence-gate`). Top-level `permissions: {}`. Jobs, in order:
1. `gate`: `uses: ./.github/workflows/ci.yml` (`secrets: inherit`).
2. `verify-tag` (contents: read): `release_check.py $GITHUB_REF_NAME`; tag is annotated and `gh api repos/{repo}/git/tags/{sha}` reports `verification.verified == true` (signed by the owner; O5).
3. `build` (needs 1–2; contents: read): `python -m build`, `twine check --strict dist/*`, `scripts/check_wheel.py`, `plugin_hash.py` → `dist/PLUGIN_HASH`, `release_notes.py` → `dist/NOTES.md`, `build_claude_tree.py` → `dist/token-sparen-claude-plugin-X.Y.Z.zip`, `check_denylist` over notes and tree; `upload-artifact` `dist`.
4. `provenance` (attestations: write, id-token: write): `actions/attest-build-provenance` on wheel, sdist, plugin zip.
5. `draft-release` (contents: write): create the draft if absent (`gh release view` || `gh release create --draft --verify-tag --notes-file`), then `gh release upload --clobber` the assets.
6. `publish-pypi` (environment `pypi` with required reviewer = owner; id-token: write; GitHub-hosted ubuntu, no container): download `dist`, `pypa/gh-action-pypi-publish` (SHA-pinned; attestations default on), wheel + sdist only.
7. `finalize` (needs 6; environment `release` with required reviewer; contents: write): `gh release edit --draft=false --latest`; force-push the plugin tree as a single commit to branch `release/claude` (commit message `release: vX.Y.Z plugin tree (<plugin hash 12>)`).

- [ ] **Step 1:** Failing tests (text-level, stdlib): `test_release_tag_filter_is_strict_semver`, `test_release_job_order_publishes_last` (`needs` chain: publish-pypi needs draft-release needs provenance/build; finalize needs publish-pypi), `test_publish_jobs_use_environments` (`publish-pypi` → `environment: pypi`, `finalize` → `environment: release`), `test_draft_release_step_is_idempotent` (contains the `gh release view ||` guard and `--clobber`), `test_dry_run_publishes_nothing` (publish-pypi/draft-release/finalize carry `if: github.event_name == 'push'`).
- [ ] **Step 2–4:** fail → implement → battery; `workflow_dispatch` dry run on `main` after push (controller) must pass `gate`, `build`, `provenance`. **Step 5:** commit `ci: tag-driven release workflow with gated PyPI publish`.

### Task 10: Contributor docs, release skill, CHANGELOG and spec sync

**Files:** Modify `.claude/skills/release/SKILL.md` (new procedure: bump script → PR-free commit → **owner** signs tag → workflow → approvals → post-release checks; never tag without the evidence gate), `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` (docs extra, `docs/user` is the single docs source, site/wiki generated), `CHANGELOG.md` (restructure for 1.0: an `## [1.0.0]` summary of what v1.0 contains, then the milestone history kept verbatim under "Pre-1.0 milestones"), `docs/specs/2026-11-25-token-sparen-design.md` (revision-log entry: `docs/user` + `site/` templates as the docs source; Python-Markdown; curated CHANGELOG notes; npm/per-host zips → v1.1; directory submission via the developer portal tracking `release/claude`; plugin-hash definition), `MEMORY.md`.

- [ ] **Step 1:** Edit; `tests/test_scaffolding.py` and `tests/test_docs.py` stay green; add `test_release_skill_mentions_owner_signed_tag_and_evidence_gate`.
- [ ] **Step 2:** Battery + denylist. **Step 3:** commit `docs: M6 contributor docs, release skill and spec sync`.

### Task 11: Cold whole-repo review and drift report

**Files:** Create `docs/reports/drift-m6.md`; fix wave as needed.

- [ ] **Step 1:** Goldfish cold review (controller-dispatched, zero context) of the **whole repository** at HEAD against the spec (all sections; M6 focus §10.5, §11, §12) and the README/site/wiki claims: every user-facing statement must be true of the code; every disclosure (disk, settings, network) complete. Ranked blocker/major/minor.
- [ ] **Step 2:** Fix wave: blockers and majors fixed (runtime ones under the freeze rule; else v1.0.1 and stated in the CHANGELOG "Known issues"); minors through Task 2's rule.
- [ ] **Step 3:** `drift-checker` → `docs/reports/drift-m6.md` (match / diverge / missing / deferred per spec section, function/section references, no line numbers).
- [ ] **Step 4:** commit `docs: M6 cold review fixes and drift report`; push; CI green; `docs.yml` deploys (after O2/O3).

### Task 12: OWNER ACTION checkpoint — platform setup (controller stops here)

The controller writes `docs/release/owner-checklist.md` (public, no personal data, no email addresses) with the exact steps below, posts it to the owner and waits. Nothing in this task is scripted against the owner's accounts.

- [ ] **O1 PyPI:** on PyPI, add a *pending trusted publisher*: project `token-sparen`, owner `rupakc`, repository `token-sparen`, workflow `release.yml`, environment `pypi`. (Confirm the name is free first; optionally the same on TestPyPI.) Enable 2FA if not already.
- [ ] **O2 GitHub environments:** create `pypi` and `release`, each with required reviewer = owner, "prevent self-review" off (sole maintainer), deployment branches/tags limited to `v*.*.*` tags. Pages: Settings → Pages → Source = GitHub Actions (creates `github-pages`).
- [ ] **O3 Wiki:** enable Wiki, create one placeholder page in the web UI (initialises `token-sparen.wiki.git`), restrict editing to collaborators.
- [ ] **O4 Repository hygiene:** enable secret scanning + push protection, private vulnerability reporting, Dependabot alerts; branch protection on `main` optional (sole maintainer pushes after local checks, per CLAUDE.md) but tag protection for `v*` recommended; protect `release/claude` from manual pushes except the workflow.
- [ ] **O5 Signing:** register an SSH or GPG signing key on GitHub; confirm `git tag -s` works locally (test on a throw-away tag, deleted locally, never pushed).
- [ ] **O6 npm decision:** confirm "no npm package for v1.0" (recommended) or instruct otherwise; any npm action is the owner's.
- [ ] Owner confirms O1–O6 → controller records date and outcomes (no account identifiers beyond the public repo) in `MEMORY.md`.

### Task 13: Cut v1.0.0

- [ ] **Step 1:** Gate: `python scripts/render_evidence.py --check` must pass (M5 evidence present, hash equal, non-inferior). If it fails → STOP, report the reason to the owner (options: wait for M5, re-run confirmation on the current hash, or an explicit owner override recorded in `MEMORY.md` and README stating no published results). No silent override.
- [ ] **Step 2:** `python scripts/bump_version.py 1.0.0 --date <today>`; `python scripts/render_evidence.py --write README.md`; README Status/SECURITY.md wording for 1.0; full battery + `check_wheel` + `build_claude_tree` + `release_check v1.0.0`; commit `chore(release): v1.0.0`; push; CI green.
- [ ] **Step 3 (OWNER ACTION O7):** owner creates and pushes the signed tag: `git tag -s v1.0.0 -m "token-sparen v1.0.0"` then `git push origin v1.0.0`.
- [ ] **Step 4 (OWNER ACTION O8):** owner reviews the draft release (notes, assets, attestations) and approves `pypi`, then `release`.
- [ ] **Step 5:** Post-release verification (controller): `pipx install token-sparen==1.0.0` in a clean tmp HOME → `token-sparen --version` = `1.0.0`, `token-sparen doctor` runs; `pip download` shows provenance on PyPI's file page; `claude plugin marketplace add rupakc/token-sparen` + install in an isolated `CLAUDE_CONFIG_DIR` → `/token-sparen doctor`; site and wiki show 1.0.0; `release/claude` HEAD tree hash equals the release's `PLUGIN_HASH`.
- [ ] **Step 6:** `MEMORY.md` "v1.0.0 released" entry; bump `main` to `1.0.1.dev0` (`bump_version.py`), commit `chore: open 1.0.1.dev0`; push.

### Task 14: OWNER ACTION checkpoint — Anthropic plugin directory submission

Prepared by the controller in `docs/release/directory-submission.md` (public; listing text and data-handling answers only, no contact details): plugin path = repository root of branch `release/claude`; listing description from `plugin.json`/README; data-handling answers — reads tool outputs and transcripts locally; stores recovery files and learning state locally as listed in README "What it writes to disk"; sends no data anywhere (no network); retention = until the user deletes the folders; not directed at under-18s; settings changes = opt-in statusline and opt-in tool-pruning deny rules, backed up and reversible. Expected portal results: hooks run a Python file → "Scripts the validator couldn't follow" policy hold (reviewer reads it; not a rejection).

- [ ] **O9:** owner connects GitHub on claude.ai (account with push access), opens claude.ai/directory/manage → Submit new → Plugin bundle → repository `rupakc/token-sparen`, tracked branch `release/claude`, **Validate**. Blocking findings → controller fixes in `main`, cuts v1.0.1 through Task 13 Steps 2–6, owner re-validates.
- [ ] **O10:** owner completes listing details, data handling (from the prepared answers), compliance (own contact email, four acknowledgements), chooses update delivery (push webhook recommended; needs repo admin) and keeps auto-publish **off** for the first version; **Submit for review**.
- [ ] **O11:** after approval, owner selects **Publish**; controller adds the directory install line to README/site in the next patch release and records the outcome in `MEMORY.md` (status words only).

---

## Self-review notes

- Spec coverage: §11 CI additions (docs/site build, security scan, plugin validation) → Tasks 3, 5, 9; `claude plugin eval` cases and TS shim build stay out (no eval cases defined yet; TS is v1.1) — recorded in Task 10's spec revision entry. §11 release (PyPI trusted publishing, release assets, `release/claude`, changelog) → Tasks 8–9, 13. §11 Wiki page list → Task 4 pages = wiki pages (Task 6). §11 gh-pages (explorer, scenarios, demos, quick start, docs) → Task 5. §12 → Tasks 12–14 (Claude parts), others v1.1. §10.5 → Global Constraints, Task 7, Task 13 Step 1. §14 M6 → all. §15 untouched (future work), linked from `docs/user/index.md` roadmap.
- Interfaces used across tasks: `current_version` (T1→T7–9), `plugin_hash` (T7→T8, T9, T13, M5), `check_release_gate` (T7→T8, T13), `build_claude_tree` (T8→T9, T13), `build_site`/`build_wiki` (T5/T6→T4 test, T9 none).
