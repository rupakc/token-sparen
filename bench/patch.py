"""The graded patch of one run (M5 Task 6; Review Focus 2).

`extract_patch` stages everything the run left in the task repository (``git add -A``), then
unstages the tool artefacts of ``excludes`` (`bench.arms.Arm.patch_excludes`) **only** where the
path was untracked at ``base_commit``: a file the agent created is kept, a file an arm's tool
created (``.token-sparen/``, ``CLAUDE.md``, ``RTK.md``, …) is dropped, and an edit to a file the
repository already tracked is always the agent's. The patch is ``git diff --cached --binary
<base>``, so commits the agent made anyway are included. Exclude entries are relative to the
repository root: ``dir/`` matches everything below that directory, any other entry one file.

Stdlib only and runnable as a script: `entry.sh` runs it inside the task container with the
toolchain Python (``python -I patch.py --repo /testbed --base <commit> --excludes-env
TSB_PATCH_EXCLUDES --out /run-out/patch.diff``); the fake backend calls `extract_patch` directly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

Run = Callable[[list[str]], str]
# environment the git calls inherit (an explicit allow-list: no token, no GIT_DIR redirects)
_GIT_ENV_KEYS = ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "HOME")
_BATCH = 200


class PatchError(RuntimeError):
    """A git command failed while extracting the patch."""


def git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The git environment: allow-listed keys of ``environ``, no system or global config."""
    env = {k: environ[k] for k in _GIT_ENV_KEYS if k in environ}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def git_runner(environ: Mapping[str, str] | None = None) -> Run:
    """A `Run` that executes git with `git_env` and returns stdout (surrogate-escaped)."""
    env = git_env(os.environ if environ is None else environ)

    def run(argv: list[str]) -> str:
        proc = subprocess.run(argv, capture_output=True, env=env, check=False)  # noqa: S603
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise PatchError(f"{' '.join(argv[:4])}… failed: {err[:500]}")
        return proc.stdout.decode("utf-8", "surrogateescape")

    return run


def excluded(path: str, excludes: Sequence[str]) -> bool:
    """``path`` (repo-relative POSIX) is a tool artefact named by ``excludes``."""
    for entry in excludes:
        if entry.endswith("/") and path.startswith(entry):
            return True
        if not entry.endswith("/") and path == entry:
            return True
    return False


def extract_patch(repo: str, base_commit: str, excludes: Sequence[str], *, run: Run) -> str:
    """The run's patch against ``base_commit`` without tool artefacts (module docstring)."""
    git = ["git", "--literal-pathspecs", "-C", repo]
    run([*git, "add", "-A"])
    added = run([*git, "diff", "--cached", "--name-only", "--diff-filter=A", "-z", base_commit])
    drop = [p for p in added.split("\0") if p and excluded(p, excludes)]
    for i in range(0, len(drop), _BATCH):
        run([*git, "rm", "--cached", "-q", "--ignore-unmatch", "--", *drop[i : i + _BATCH]])
    return run([*git, "diff", "--cached", "--binary", base_commit])


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Extract the graded patch of one run.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--excludes-env", help="variable holding newline-separated excludes")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    excludes = list(args.exclude)
    if args.excludes_env:
        excludes += [e for e in env.get(args.excludes_env, "").splitlines() if e]
    try:
        patch = extract_patch(args.repo, args.base, excludes, run=git_runner(env))
    except PatchError as exc:
        print(f"patch: {exc}", file=sys.stderr)
        return 1
    with open(args.out, "wb") as fh:
        fh.write(patch.encode("utf-8", "surrogateescape"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
