# Extending token-sparen

Installed Python packages can add compressors, decision points, signals or host adapters through
the entry-point groups `token_sparen.compressors`, `.decision_points`, `.signals` and `.adapters`.
Installing such a package is itself the trust decision: entry points have no separate trust step,
so install only packages you trust.
The extension interface is versioned (`token_sparen.EXTENSION_API_VERSION`, currently `1.0`);
an extension may declare `TOKEN_SPAREN_EXTENSION = {"api": "1.0"}` and one that needs a newer or
different major is skipped. Check a compressor with
`token_sparen.testing.conformance.check_compressor(c) == []`.

Drop-in `*.py` files in `<project>/.token-sparen/extensions/` or `~/.token-sparen/extensions/` run
only after you trust their exact content: `token-sparen extensions trust <path>` shows the sha256
and the first 40 lines, and `--yes` records it in `~/.token-sparen/trusted_extensions.json`. Any
edit revokes trust, and a repository cannot trust its own files. Symlinked, foreign-owned and
world-writable files are skipped. `token-sparen extensions list` shows each drop-in's status;
`extensions untrust <path|sha256>` revokes. Built-in compressors win unless
`[extensions] allow_override = true` is set in `~/.token-sparen/config.toml`; a project
`token-sparen.toml`, the environment (which can only narrow, see [Configuration](configuration.md#precedence)) and `config set` cannot
enable it.

<!-- TODO(merge): until the narrow-only environment rule is on main, TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE still enables it. -->

The Python package API is versioned independently of the plugin; see the [design spec](docs/specs/2026-11-25-token-sparen-design.md) section 9 for the extension model.

**What trust means: trusting a drop-in gives it full code execution as you**, with your files and
credentials, every time a hook uses it. Only trust code you have read or whose author you trust.
The runtime checks (error lines kept, line counts consistent, failures disabled) protect you from
*buggy* extensions, not *malicious* ones. Trust follows content, not location: an identical copy of
a trusted file in another project also loads.
