# Privacy and security

token-sparen runs as Claude Code hooks on every tool call, reads tool outputs, writes files on
your machine and can, when you opt in, change two Claude Code settings. This page lists what it
protects, from whom, and how; every row names the code that enforces the rule and the test that
fails if the rule breaks. Report a vulnerability as described in [SECURITY.md](../../SECURITY.md).

## Assets

- **Your source and tool outputs**: what the model's tools print, which token-sparen may shorten.
- **Recovery files**: the full text of every shortened output, in
  `<project>/.token-sparen/outputs/`.
- **State under `~/.token-sparen/`**: configuration, learned statistics, decision logs (the last
  prompt, file paths, normalised commands), trusted extension hashes, install records.
- **Claude Code settings files**: your user `settings.json` and a project's
  `.claude/settings.local.json`.
- **Git state**: what ends up in a commit of the project you work on.

## Adversaries

- **A malicious repository** you clone and open: it controls a committed `token-sparen.toml`, a
  committed `.claude/settings.json` (whose `env` block sets environment variables for every hook),
  crafted file and directory names, symbolic links, and everything its commands print.
- **Prompt injection through tool output**, aimed at the facts token-sparen remembers between
  sessions and re-injects at session start.
- **A malicious extension**: a drop-in `*.py` file or an installed entry-point package.
- **Other local users** on a shared machine.
- **A compromised dependency, GitHub Action or release pipeline.**

## Threats and mitigations

| Entry point | Mitigation | Where | Pinning test |
|---|---|---|---|
| Committed `token-sparen.toml` | Deny-by-default: a project value is used only if it moves in the allowed direction (intervene less, keep more); unlisted keys are user-only; ignored values are logged once | `config.PROJECT_POLICY`, `config.sanitise_project` | `tests/test_config_safety.py::test_project_cannot_release_a_hold`, `test_unknown_keys_are_user_only`, `test_every_default_key_has_a_project_policy` |
| Committed `.claude/settings.json` setting `TOKEN_SPAREN_*` variables | Environment values follow the same policy as the project file (narrowing only), measured from your own config and session overrides; widening comes only from `~/.token-sparen/config.toml` or `token-sparen config set`; kill switches (`TOKEN_SPAREN_DISABLE`) stay | `config.sanitise_env`, `config.load_config` | `tests/test_config_safety.py::test_env_cannot_widen`, `test_env_may_narrow`, `test_env_cannot_release_a_hold` |
| The same, opting in to tool pruning, high-tier learning or extension overrides | Read from the user's `config.toml` only (not env, project or session) | `config.user_config`, `mechanisms.prune.settings_for`, `learn.engine.Engine.opted_in`, `extensions._allow_override` | `tests/test_config_safety.py::test_project_config_cannot_enable_pruning`, `tests/learn/test_engine.py::test_high_tier_optin_by_user_config_only`, `tests/test_extensions.py::test_override_flag_never_from_env` |
| The same, pointing `TOKEN_SPAREN_HOME` / `TOKEN_SPAREN_ROOT` into the repository (planted config, trust file or launcher) | Ignored when it resolves inside the project or its git work tree (by file identity) or, on POSIX, is not owned by you. **Residual risk:** a path elsewhere that you own is accepted, e.g. `../sibling-clone/state` inside a second malicious clone that planted a `config.toml` there; this needs two cooperating repositories | `config.env_path`, `assets/statusline.sh` | `tests/test_config_safety.py::test_state_home_env_inside_repo_ignored`, `tests/test_env_paths.py::test_home_inside_project_dir_is_ignored`, `test_launcher_ignores_a_root_inside_the_project` |
| Committed symbolic links (`.token-sparen`, `outputs`, `outputs/<id>.txt`, `.gitignore`) or planted recovery files (ids are predictable content hashes) | `.token-sparen` and `outputs` must be real directories (lstat, re-checked after creation); an existing `<id>.txt` must be a regular file, else nothing is saved (the hook fails open); new files are written to a fresh `O_EXCL`/`O_NOFOLLOW` temporary file and renamed into place; the marker resolves the directories, never the file; `show` reads only ids this install recorded for this checkout (`<state root>/recovery/<hash>/`), without following links; a planted file at an id we write is replaced; pruning never follows links; the `.gitignore` is created with `O_EXCL` | `recovery.save_full`, `recovery.read_full`, `recovery.marker`, `recovery.prune_outputs`, `cli._show` | `tests/test_private_state.py::test_recovery_refuses_a_committed_symlinked_output_file`, `test_recovery_refuses_a_committed_symlinked_root`, `test_recovery_refuses_a_symlinked_outputs_dir`, `test_planted_regular_file_is_rewritten_not_served`, `test_read_full_refuses_links`, `test_registry_is_per_checkout`, `test_marker_path_stays_inside_the_project`, `test_recovery_gitignore_never_written_through_a_link` |
| Tool-pruning deny rules reaching a commit | Written only by the session-end fold, only on positive git confirmation that `.claude/settings.local.json` is untracked and ignored (tracked, unignored, git missing, slow or redirected by `GIT_DIR`-style variables all refuse); a symlinked file or folder is refused | `adapters.claude.settings.git_exposure`, `add_deny_rules` | `tests/adapters/claude/test_settings.py::test_settings_write_refuses_tracked_settings_file`, `test_git_exposure_refuses_location_variables`, `test_add_deny_rules_refuses_a_symlink`, `tests/mechanisms/test_prune.py::test_unignored_and_tracked_files_are_refused` |
| Prompt injection aimed at remembered facts | Facts are plain command tokens that pass a strict sanitiser (no URLs, shell operators, paths outside the project, instruction-like words, secret-like values), re-checked at render time, rendered from fixed templates inside a block labelled as data, not instructions | `facts.sanitize.sanitize`, `facts.sanitize.render`, `facts.inject.build` | `tests/facts/test_sanitize.py::test_sanitize_rejects_injection`, `test_sanitize_rejects_instruction_words_after_normalising`, `test_render_refuses_unsanitised_fact`, `tests/facts/test_inject.py::test_block_skips_unsafe_facts_by_resanitising` |
| Malicious drop-in extension | Imported only if the sha256 of its exact bytes is in your `trusted_extensions.json` (any edit revokes); the trust file is read only from your state root; symlinked, foreign-owned and world-writable files are skipped; built-ins win unless you allow overrides in your own config | `extensions.discover`, `extensions._read_checked`, `extensions.trust` | `tests/test_extensions.py::test_untrusted_dropin_ignored`, `test_trust_revoked_when_file_edited`, `test_project_cannot_pre_trust_itself`, `test_symlinked_dropin_and_dir_skipped`, `test_world_writable_and_foreign_files_skipped` |
| Other local users reading state or recovery files | Directories token-sparen creates under the state root and `<project>/.token-sparen/` are mode 0700; an existing state root, `.token-sparen`, `outputs` or other directory it writes into that you own and that is looser is tightened to 0700 on the next write (best effort; directories above them, such as your home or the project, are never changed); a directory not owned by you, or one that appeared as someone else's directory or a link while creating, is refused; the salting secret and offline reports are 0600; `TOKEN_SPAREN_HOME` owned by someone else is ignored. Files inside a directory created by an older version keep their modes but are unreachable once the directory is 0700 | `config.private_mkdir`, `config.state_home`, `learn.overlay.project_tag`, `replay.report` | `tests/test_private_state.py::test_state_home_created_private_by_every_writer`, `test_recovery_dirs_created_private`, `test_existing_loose_dirs_we_own_are_tightened`, `test_private_mkdir_refuses_a_foreign_directory`, `test_private_mkdir_refuses_a_raced_directory`, `tests/replay/test_replay.py::test_report_written_0600_with_aggregates`, `tests/test_env_paths.py::test_home_owned_by_someone_else_is_ignored` |
| Hooks on every tool call breaking a session | Fail open: any exception or latency overrun returns the host no-op and logs one line; `TOKEN_SPAREN_DISABLE=1` makes every hook a no-op | `adapters.claude.entry.main`, `hooks/run.py` | `tests/adapters/claude/test_entry.py::test_readonly_home_fail_open`, `test_store_busy_fail_open`, `test_disable_env_noop` |
| Hook commands and launchers | Hook commands reference files only via `${CLAUDE_PLUGIN_ROOT}`; no top-level `bin/`; no unpinned `npx`/`uvx`; the package never spawns a downloading launcher | `hooks/hooks.json` | `tests/test_supply_chain.py::test_hook_commands_only_plugin_root`, `test_no_unpinned_launchers_in_plugin` |
| The skill's pre-approved commands | Only read-only (`stats`, `show`, `doctor`) or "do less" (`session disable`, `prune restore`) subcommands run without a permission prompt; `show` accepts only a 12-hex id | `skills/token-sparen/SKILL.md`, `cli._show` | `tests/test_supply_chain.py::test_skill_preapprovals_are_read_only_or_restoring` |
| Changes to Claude Code settings | Statusline install and tool-pruning deny rules are the **only** changes token-sparen makes to Claude Code settings: both opt-in, journaled, backed up once, reversible, and deny rules go only into untracked and git-ignored files | `adapters.claude.settings` | `tests/adapters/claude/test_settings.py::test_install_fresh_and_uninstall_removes_key`, `test_backup_never_overwritten`, `test_uninstall_leaves_statusline_changed_by_user` |
| Compromised dependency | The runtime has no dependencies (stdlib only); dev and docs tooling is audited weekly (`pip-audit`, advisory) and updated by Dependabot | `pyproject.toml`, `.github/dependabot.yml`, CI `supply-chain` | `tests/test_supply_chain.py::test_runtime_dependencies_empty`, `test_dependabot_watches_actions_and_pip` |
| Compromised GitHub Action or workflow | Every action pinned to a full commit SHA; top-level read-only permissions; write and OIDC grants only in the named release and docs jobs; no `secrets: inherit`, no `pull_request_target`; checkouts drop the token | `.github/workflows/` | `tests/test_supply_chain.py::test_actions_pinned_by_sha`, `test_workflows_least_privilege`, `test_ci_is_reusable_and_fails_closed_on_main` |
| Private data in published output (repository, site, wiki, release trees, wheels and sdists) | `scripts/check_denylist.py` scans the repository and, with `--path`, any generated tree, file names and the members of zip and tar archives (binary files are skipped); CI runs it on every push and pull request and fails closed on pushes to `main` when its private word list is missing (`--require-private-words`). Release and docs jobs (scanning `dist/`, the site and the wiki with `--path --require-private-words`) are added by the release tasks; until then a tag push is not checked fail-closed | `scripts/check_denylist.py`, CI `denylist` | `tests/test_meta.py::test_denylist_path_mode_scans_tree`, `test_denylist_scans_archive_members`, `test_require_private_words_fails_closed`, `tests/test_supply_chain.py::test_ci_is_reusable_and_fails_closed_on_main` |

## Non-goals

- **Secrets a command prints.** Recovery files keep tool outputs exactly as they were, so a
  token a command printed is in that file (in an owner-only folder that git ignores). Delete
  `<project>/.token-sparen/` whenever you like; the README's "What it writes to disk" lists
  every file.
- **A compromised host process or user account.** Anything that runs as you can read and change
  token-sparen's files and Claude Code's settings without token-sparen's help.
- **Claude Code's own trust decisions**, for example whether to run a repository's committed
  hooks or settings at all.
- **Extensions you trusted.** Trusting a drop-in or installing an entry-point package gives it
  full code execution as you.
