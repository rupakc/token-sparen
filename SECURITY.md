# Security Policy

## Supported versions

Security fixes are made for the latest `1.x` release. Older releases do not receive fixes;
please upgrade to the latest release first and check whether the problem still occurs.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository (Security tab,
"Report a vulnerability") rather than opening a public issue, a pull request or a discussion.
Include the affected version, your platform and the steps to reproduce.

You can expect an acknowledgement within a few days. Fixes are released as a patch version and
credited in the release notes unless you prefer otherwise.

## Scope

In scope: the token-sparen plugin and Python package (hooks, CLI, skill, state and recovery
files, settings writes, extension loading, fact injection) and this repository's release
pipeline. The threat model, with each mitigation and the test that pins it, is in
[docs/user/privacy-security.md](docs/user/privacy-security.md).

Out of scope: secrets that a command you ran printed (recovery files keep tool outputs as they
were, see the README's "What it writes to disk"), a compromised host process or user account,
and the behaviour of Claude Code itself or of third-party extensions you chose to trust.
